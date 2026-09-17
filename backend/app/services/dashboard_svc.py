"""审计与数据看板（M08，FUNCTION-MAP §3 F-08.02/F-08.03）。

时区纪律（PRD/前端契约原文）：图表数据用 **UTC 存储**、服务端生成
**上海日/周分桶**——前端不二次按本机时区聚合。实现：把上海日/周边界换算成
UTC 再查库，桶标签回上海日期字符串。

聚合方式（诚实边界）：Python 侧聚合而非 SQL 窗口函数——演示级数据量下
足够且可测；数据量上来后换 SQL 聚合，桶语义不变。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.response import BizError
from app.models import ChatMessage, ChatRequest, KnowledgeUnit, MessageSource, ModelCallUsage, QaAudit

#: Asia/Shanghai = UTC+8（无夏令时）。
_SHANGHAI = timezone(timedelta(hours=8))

_RANGE_ERROR = BizError("INVALID_ARGUMENT", 'range 必须为 "day" 或 "week"')


def _parse_anchor(anchor_date: str) -> date:
    try:
        return date.fromisoformat(anchor_date)
    except (TypeError, ValueError) as exc:
        raise BizError("INVALID_ARGUMENT", "anchor_date 必须为 YYYY-MM-DD") from exc


def _range_bounds(anchor: date, rng: str) -> tuple[datetime, datetime]:
    """上海日/周的 **UTC** 边界（半开区间 [start, end)）。周 = ISO 周（周一起）。"""
    if rng == "day":
        start_sh = datetime(anchor.year, anchor.month, anchor.day, tzinfo=_SHANGHAI)
        return start_sh.astimezone(timezone.utc).replace(tzinfo=None), (
            start_sh + timedelta(days=1)
        ).astimezone(timezone.utc).replace(tzinfo=None)
    if rng == "week":
        monday = anchor - timedelta(days=anchor.isoweekday() - 1)
        start_sh = datetime(monday.year, monday.month, monday.day, tzinfo=_SHANGHAI)
        return start_sh.astimezone(timezone.utc).replace(tzinfo=None), (
            start_sh + timedelta(days=7)
        ).astimezone(timezone.utc).replace(tzinfo=None)
    raise _RANGE_ERROR


def _bucket_label(moment: datetime) -> str:
    """UTC 存储时刻 → 上海日期桶标签。"""
    return moment.replace(tzinfo=timezone.utc).astimezone(_SHANGHAI).date().isoformat()


def _day_labels(anchor: date, rng: str) -> list[str]:
    if rng == "day":
        return [anchor.isoformat()]
    monday = anchor - timedelta(days=anchor.isoweekday() - 1)
    return [(monday + timedelta(days=offset)).isoformat() for offset in range(7)]


def _normalize(question: str) -> str:
    return " ".join(question.split()).casefold()


# ---------------------------------------------------------------- F-08.02


async def get_summary(
    session: AsyncSession, ctx: Any, *, rng: str, anchor_date: str
) -> dict[str, Any]:
    """F-08.02：指标摘要。唯一请求算 PV/UV；零分母返回 0 并显示无样本。"""
    if rng not in ("day", "week"):
        raise _RANGE_ERROR
    anchor = _parse_anchor(anchor_date)
    start, end = _range_bounds(anchor, rng)

    requests = (
        (
            await session.execute(
                select(ChatRequest).where(
                    ChatRequest.accepted_at >= start, ChatRequest.accepted_at < end
                )
            )
        )
        .scalars()
        .all()
    )
    pv = len(requests)
    uv = len({int(r.user_id) for r in requests})
    request_by_id = {int(r.id): r for r in requests}

    audits = (
        (
            await session.execute(
                select(QaAudit).where(QaAudit.asked_at >= start, QaAudit.asked_at < end)
            )
        )
        .scalars()
        .all()
    )
    terminal = [a for a in audits if int(a.request_id) in request_by_id]
    total = len(terminal)
    faq_hit = answered = failed = 0
    for audit in terminal:
        row = request_by_id[int(audit.request_id)]
        if row.result_type == "faq_hit":
            faq_hit += 1
        if row.result_type in ("answered", "faq_hit"):
            answered += 1
        if row.status == "failed":
            failed += 1

    knowledge_count = int(
        (
            await session.execute(
                select(func.count())
                .select_from(KnowledgeUnit)
                .where(KnowledgeUnit.is_deleted.is_(False))
            )
        ).scalar_one()
    )
    return {
        "pv": pv,
        "uv": uv,
        "faq_hit_rate": round(faq_hit / total, 4) if total else 0.0,
        "coverage": round(answered / total, 4) if total else 0.0,
        "knowledge_count": knowledge_count,
        "error_rate": round(failed / total, 4) if total else 0.0,
        "unknown_usage_count": sum(1 for a in terminal if a.usage_status == "unknown"),
    }


# ---------------------------------------------------------------- F-08.03


async def get_charts(
    session: AsyncSession, ctx: Any, *, rng: str, anchor_date: str, top_n: int
) -> dict[str, Any]:
    """F-08.03：六类图表（上海日桶；零样本桶也返回，前端不补造）。"""
    if rng not in ("day", "week"):
        raise _RANGE_ERROR
    anchor = _parse_anchor(anchor_date)
    if not 1 <= top_n <= 20:
        raise BizError("INVALID_ARGUMENT", "top_n 必须为 1—20")
    start, end = _range_bounds(anchor, rng)
    labels = _day_labels(anchor, rng)

    requests = (
        (
            await session.execute(
                select(ChatRequest).where(
                    ChatRequest.accepted_at >= start, ChatRequest.accepted_at < end
                )
            )
        )
        .scalars()
        .all()
    )
    # ---- traffic：PV/UV 按上海日桶 ----
    traffic: dict[str, dict[str, int]] = {label: {"pv": 0, "uv": 0} for label in labels}
    users_by_day: dict[str, set[int]] = {label: set() for label in labels}
    for row in requests:
        label = _bucket_label(row.accepted_at)
        if label in traffic:
            traffic[label]["pv"] += 1
            users_by_day[label].add(int(row.user_id))
    for label in labels:
        traffic[label]["uv"] = len(users_by_day[label])

    # ---- questions：高频问题（规范化聚合）----
    question_counts: dict[str, int] = {}
    for row in requests:
        key = _normalize(row.question)
        question_counts[key] = question_counts.get(key, 0) + 1
    questions = sorted(
        ({"question": key, "count": count} for key, count in question_counts.items()),
        key=lambda item: (-item["count"], item["question"]),
    )[:top_n]

    # ---- knowledge_heat：每轮去重引用（request × unit 只算一次）----
    request_ids = [int(r.id) for r in requests]
    heat: dict[int, set[int]] = {}
    titles: dict[int, str] = {}
    if request_ids:
        assistant_ids = [
            int(m.id)
            for m in (
                await session.execute(
                    select(ChatMessage).where(
                        ChatMessage.request_id.in_(request_ids),
                        ChatMessage.role == "assistant",
                    )
                )
            ).scalars()
        ]
        if assistant_ids:
            source_rows = (
                (
                    await session.execute(
                        select(MessageSource).where(MessageSource.message_id.in_(assistant_ids))
                    )
                )
                .scalars()
                .all()
            )
            message_to_request = {
                int(m.id): int(m.request_id)
                for m in (
                    await session.execute(
                        select(ChatMessage).where(ChatMessage.id.in_(assistant_ids))
                    )
                ).scalars()
            }
            for source in source_rows:
                rid = message_to_request.get(int(source.message_id))
                if rid is not None:
                    heat.setdefault(int(source.unit_id), set()).add(rid)
        unit_rows = (
            (
                await session.execute(
                    select(KnowledgeUnit).where(KnowledgeUnit.id.in_(list(heat) or [0]))
                )
            )
            .scalars()
            .all()
        )
        titles = {int(u.id): u.title for u in unit_rows}
    knowledge_heat = sorted(
        (
            {"unit_id": uid, "title": titles.get(uid, f"#{uid}"), "count": len(rids)}
            for uid, rids in heat.items()
        ),
        key=lambda item: (-item["count"], item["unit_id"]),
    )[:top_n]

    # ---- usage：分类型用量（model_call_usage）----
    usage_rows = (
        (
            await session.execute(
                select(ModelCallUsage).where(
                    ModelCallUsage.created_at >= start, ModelCallUsage.created_at < end
                )
            )
        )
        .scalars()
        .all()
    )
    usage: dict[str, dict[str, int]] = {}
    for row in usage_rows:
        bucket = usage.setdefault(
            row.kind, {"calls": 0, "input_tokens": 0, "output_tokens": 0, "unknown": 0}
        )
        bucket["calls"] += 1
        if row.input_tokens is not None:
            bucket["input_tokens"] += int(row.input_tokens)
        if row.output_tokens is not None:
            bucket["output_tokens"] += int(row.output_tokens)
        if row.status == "unknown":
            bucket["unknown"] += 1

    # ---- latency：成功/失败延迟分布 ----
    audits = (
        (
            await session.execute(
                select(QaAudit).where(QaAudit.asked_at >= start, QaAudit.asked_at < end)
            )
        )
        .scalars()
        .all()
    )
    latency: dict[str, dict[str, Any]] = {}
    for audit in audits:
        if audit.duration_ms is None:
            continue
        bucket = latency.setdefault(audit.status, {"count": 0, "total_ms": 0, "max_ms": 0})
        bucket["count"] += 1
        bucket["total_ms"] += int(audit.duration_ms)
        bucket["max_ms"] = max(bucket["max_ms"], int(audit.duration_ms))
    latency_out = [
        {"status": status, "count": bucket["count"],
         "avg_ms": bucket["total_ms"] // bucket["count"], "max_ms": bucket["max_ms"]}
        for status, bucket in sorted(latency.items())
    ]

    # ---- knowledge_counts：台账状态计数 ----
    unit_rows = (
        (await session.execute(select(KnowledgeUnit).where(KnowledgeUnit.is_deleted.is_(False))))
        .scalars()
        .all()
    )
    knowledge_counts = {
        "total": len(unit_rows),
        "enabled": sum(1 for u in unit_rows if u.enabled),
        "indexed": sum(1 for u in unit_rows if u.index_status == "indexed"),
    }

    return {
        "traffic": [{"date": label, **traffic[label]} for label in labels],
        "questions": questions,
        "knowledge_heat": knowledge_heat,
        "usage": [{"kind": kind, **values} for kind, values in sorted(usage.items())],
        "latency": latency_out,
        "knowledge_counts": knowledge_counts,
    }

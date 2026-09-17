"""知识缺口闭环（M07，FUNCTION-MAP §3 F-07.01—F-07.04）。

三条硬规则（模型层已固化，这里只是遵守）：
1. **服务故障不进缺口**（OutboxEvent 注释）：只有正常 no_evidence/low_confidence
   登记缺口——把运维事故当知识空白去补档是双重错误。
2. **`(department_key, fingerprint)` 唯一**：同部门同义问法只累积一行，
   频次由 `gap_request` 关联行数表达，`request_id` 去重靠联合主键。
3. **缺口状态不写进知识单元**（BC-07.03）：processing 是缺口自己的状态。
"""

from __future__ import annotations

import hashlib
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.response import BizError
from app.db.base import utcnow
from app.db.repository import UnitOfWork
from app.engines.permission import UserCtx
from app.models import (
    ChatRequest,
    GapRequest,
    KnowledgeGap,
    KnowledgeUnit,
    OutboxEvent,
    RolePermission,
    SupplementTask,
    User,
    UserRole,
)
from app.services.ingest_svc import _log

#: "近期频次"统计窗口（首版 30 天；不以全量累计冒充近期热度）。
RECENT_WINDOW_DAYS = 30
#: 回放验证的达标阈值（RRF 融合序，非置信度——只作排序内比较的粗门槛）。
VERIFY_MIN_SCORE = 0.0  # 首版不做分数门槛：命中补充版本即视为有据（记 reason）


def _fingerprint(question: str) -> str:
    """问题指纹：去**全部空白与标点**后 casefold 散列。

    ★ 只折叠空白不够——"年假怎么申请？"与"年假 怎么申请"必须同指纹
      （实测曾产生两行缺口）；"同义问法归集"的第一步就是标点/空白不敏感。
    """
    import re

    normalized = re.sub(r"[\W_]+", "", question, flags=re.UNICODE).casefold()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- F-08.01 补全


async def enqueue_gap_outbox(
    session: AsyncSession, *, request_id: int, question: str, dept_id: int | None
) -> None:
    """终态事务内登记缺口通知（幂等键 = request_id；重复 finalize 不会重复登记）。"""
    session.add(
        OutboxEvent(
            event_key=f"gap:{request_id}",
            kind="knowledge_gap",
            payload={
                "request_id": request_id,
                "question": question,
                "dept_id": dept_id,
            },
        )
    )


async def consume_outbox(factory: Any) -> int:
    """消费 pending 缺口通知 → F-07.01。消费者按 request_id 幂等（event_key 唯一）。"""
    processed = 0
    while True:
        async with factory() as session:
            async with UnitOfWork(session).transaction():
                event = (
                    await session.execute(
                        select(OutboxEvent)
                        .where(OutboxEvent.status == "pending", OutboxEvent.kind == "knowledge_gap")
                        .order_by(OutboxEvent.id.asc())
                        .limit(1)
                        .with_for_update(skip_locked=True)
                    )
                ).scalar_one_or_none()
                if event is None:
                    return processed
                payload = dict(event.payload)
                try:
                    await record_gap(
                        session,
                        request_id=int(payload["request_id"]),
                        result_type="no_evidence",
                        question=str(payload["question"]),
                        dept_id=payload.get("dept_id"),
                        score=None,
                        score_type="none",
                        model_version=get_settings().embedding_model_version,
                    )
                except Exception:  # noqa: BLE001 — 单条失败重试，不阻塞队列
                    await session.execute(
                        update(OutboxEvent)
                        .where(OutboxEvent.id == int(event.id))
                        .values(attempts=OutboxEvent.attempts + 1)
                    )
                    continue
                await session.execute(
                    update(OutboxEvent)
                    .where(OutboxEvent.id == int(event.id))
                    .values(status="sent")
                )
                processed += 1


# ---------------------------------------------------------------- F-07.01


async def record_gap(
    session: AsyncSession,
    *,
    request_id: int,
    result_type: str,
    question: str,
    dept_id: int | None,
    score: float | None,
    score_type: str,
    model_version: str,
) -> dict[str, Any]:
    """F-07.01：缺口登记。仅正常 no_evidence/low_confidence；request_id 去重。"""
    if result_type not in ("no_evidence", "low_confidence"):
        return {"gap_id": None}
    duplicate = (
        await session.execute(
            select(GapRequest.gap_id).where(GapRequest.request_id == request_id)
        )
    ).scalar_one_or_none()
    if duplicate is not None:
        return {"gap_id": int(duplicate)}  # 已登记过：不虚增频次

    now = utcnow()
    department_key = int(dept_id) if dept_id else 0
    fingerprint = _fingerprint(question)
    gap = (
        await session.execute(
            select(KnowledgeGap).where(
                KnowledgeGap.department_key == department_key,
                KnowledgeGap.fingerprint == fingerprint,
            )
        )
    ).scalar_one_or_none()
    if gap is None:
        gap = KnowledgeGap(
            department_key=department_key,
            fingerprint=fingerprint,
            question=question,
            status="open",
            first_seen_at=now,
            last_seen_at=now,
            max_similarity=score,
            score_type=score_type,
            model_version=model_version,
        )
        session.add(gap)
        await session.flush()
    else:
        gap.last_seen_at = now
        if score is not None and (gap.max_similarity is None or score > gap.max_similarity):
            gap.max_similarity = score
    session.add(GapRequest(gap_id=int(gap.id), request_id=request_id))
    return {"gap_id": int(gap.id)}


# ---------------------------------------------------------------- F-07.02


async def list_gaps(
    session: AsyncSession,
    ctx: UserCtx,
    *,
    status: str | None,
    dept_id: int | None,
    page: int,
    size: int,
) -> dict[str, Any]:
    """F-07.02：缺口查询（gap:handle）。近期频次按窗口统计，不附受限答案。"""
    if page < 1 or size < 1 or size > 100:
        raise BizError("INVALID_ARGUMENT", f"分页参数不合法：page={page}, size={size}")
    if status is not None and status not in ("open", "processing", "closed"):
        raise BizError("INVALID_ARGUMENT", f"status 取值不合法：{status!r}")

    conditions = []
    if status:
        conditions.append(KnowledgeGap.status == status)
    if dept_id is not None:
        conditions.append(KnowledgeGap.department_key == int(dept_id))
    if not conditions:
        conditions = [KnowledgeGap.id > 0]

    total = int(
        (
            await session.execute(
                select(func.count()).select_from(KnowledgeGap).where(*conditions)
            )
        ).scalar_one()
    )
    rows = (
        (
            await session.execute(
                select(KnowledgeGap)
                .where(*conditions)
                .order_by(KnowledgeGap.last_seen_at.desc(), KnowledgeGap.id.desc())
                .offset((page - 1) * size)
                .limit(size)
            )
        )
        .scalars()
        .all()
    )
    items: list[dict[str, Any]] = []
    for gap in rows:
        recent = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(GapRequest)
                    .where(
                        GapRequest.gap_id == int(gap.id),
                        # 近期窗口：join chat_request.accepted_at 判定
                        GapRequest.request_id.in_(
                            select(ChatRequest.id).where(
                                ChatRequest.accepted_at
                                >= utcnow() - timedelta(days=RECENT_WINDOW_DAYS)
                            )
                        ),
                    )
                )
            ).scalar_one()
        )
        items.append(
            {
                "id": int(gap.id),
                "question": gap.question,
                "dept_id": int(gap.department_key) if gap.department_key else None,
                "recent_frequency": recent,
                "max_similarity": gap.max_similarity,
                "suggested_category": gap.suggested_category,
                "last_seen_at": gap.last_seen_at,
                "status": gap.status,
            }
        )
    return {"items": items, "total": total}


# ---------------------------------------------------------------- F-07.03 / F-07.04


async def _require_gap(session: AsyncSession, gap_id: int) -> KnowledgeGap:
    gap = (
        await session.execute(select(KnowledgeGap).where(KnowledgeGap.id == gap_id))
    ).scalar_one_or_none()
    if gap is None:
        raise BizError("NOT_FOUND")
    return gap


async def convert_gap(
    session: AsyncSession, ctx: UserCtx, *, gap_id: int, client_action_id: str
) -> dict[str, Any]:
    """F-07.03：转建补充任务。gap_id 唯一 → 重复转建返回既有任务（幂等）。"""
    gap = await _require_gap(session, gap_id)
    if gap.status == "processing":
        existing = (
            await session.execute(
                select(SupplementTask).where(SupplementTask.gap_id == gap_id)
            )
        ).scalar_one_or_none()
        if existing is not None:
            return {"task_id": int(existing.id), "gap_id": gap_id, "status": existing.status}
    if gap.status == "closed":
        raise BizError("TASK_STATE_CONFLICT", "已关闭的缺口不能转建")
    if gap.status == "open":
        gap.status = "processing"
    task = (
        await session.execute(
            select(SupplementTask).where(SupplementTask.gap_id == gap_id)
        )
    ).scalar_one_or_none()
    if task is None:
        task = SupplementTask(gap_id=gap_id, status="pending")
        session.add(task)
        await session.flush()
    # processing 不写 knowledge.index_status（BC-07.03，模型注释第 1 条）
    _log(session, ctx.user_id, "gap.convert", "knowledge_gap", gap_id,
         {"status": "open"}, {"status": "processing", "client_action_id": client_action_id})
    return {"task_id": int(task.id), "gap_id": gap_id, "status": task.status}


async def bind_source(
    session: AsyncSession, ctx: UserCtx, *, task_id: int, unit_id: int, expected_revision: int
) -> dict[str, Any]:
    """API-S06：补充任务绑定当前版本（gap:handle + 来源读权；禁止绑定不存在/已删除单元）。"""
    from app.engines.permission import authorize_units

    task = (
        await session.execute(select(SupplementTask).where(SupplementTask.id == task_id))
    ).scalar_one_or_none()
    if task is None:
        raise BizError("NOT_FOUND")
    allowed, _denied, _unavailable, _versions = await authorize_units(session, ctx, [unit_id])
    if unit_id not in allowed:
        raise BizError("NOT_FOUND")
    unit = (
        await session.execute(select(KnowledgeUnit).where(KnowledgeUnit.id == unit_id))
    ).scalar_one()
    if unit.revision != expected_revision:
        raise BizError("REVISION_CONFLICT")
    task.unit_id = unit_id
    task.target_version = int(unit.content_version)
    task.status = "supplied"
    _log(session, ctx.user_id, "gap.bind_source", "supplement_task", task_id,
         None, {"unit_id": unit_id, "target_version": int(unit.content_version)})
    return {
        "task_id": task_id,
        "unit_id": unit_id,
        "target_version": int(unit.content_version),
        "revision": int(unit.revision),
    }


async def verify_gap(session: AsyncSession, ctx: UserCtx, *, gap_id: int) -> dict[str, Any]:
    """F-07.04：回放验证。用**原提问用户**的当前合法身份上下文检索（停用 → blocked）。"""
    gap = await _require_gap(session, gap_id)
    task = (
        await session.execute(select(SupplementTask).where(SupplementTask.gap_id == gap_id))
    ).scalar_one_or_none()
    if task is None or task.unit_id is None or task.target_version is None:
        return {"passed": False, "state": gap.status, "reason": "未绑定补充资料"}
    if gap.status == "closed":
        return {"passed": True, "state": "closed", "reason": None}

    # 原提问用户 = 该缺口任一关联请求的 user（历史部门快照原则：按提问当时归集）
    original_user_id = (
        await session.execute(
            select(ChatRequest.user_id)
            .join(GapRequest, GapRequest.request_id == ChatRequest.id)
            .where(GapRequest.gap_id == gap_id)
            .limit(1)
        )
    ).scalar_one_or_none()
    original_user = (
        await session.execute(select(User).where(User.id == int(original_user_id or 0)))
    ).scalar_one_or_none()
    if original_user is None or not original_user.enabled:
        return {"passed": False, "state": "blocked", "reason": "原提问账号已停用，无法回放"}

    # 身份从 DB 重装配（与执行侧同源）
    role_rows = (
        await session.execute(
            select(UserRole.role_id).where(UserRole.user_id == int(original_user.id))
        )
    ).all()
    role_ids = {int(r[0]) for r in role_rows}
    codes: set[str] = set()
    if role_ids:
        perm_rows = (
            await session.execute(
                select(RolePermission.code).where(RolePermission.role_id.in_(role_ids))
            )
        ).all()
        codes = {str(r[0]) for r in perm_rows}
    replay_ctx = UserCtx(
        user_id=int(original_user.id),
        session_id=ctx.session_id,  # 回放是运营动作，借用执行会话标识
        dept_id=int(original_user.dept_id) if original_user.dept_id is not None else None,
        role_ids=frozenset(role_ids),
        permission_codes=frozenset(codes),
        identity_revision=int(original_user.identity_revision),
    )

    from app.engines.retrieval import retrieve_authorized

    settings = get_settings()
    retrieval = await retrieve_authorized(
        session,
        replay_ctx,
        gap.question,
        {"settings": settings, "vector_top_k": 20, "keyword_top_k": 20,
         "answer_top_k": 5, "rrf_k": 60},
    )
    hit = [
        chunk
        for chunk in retrieval.allowed
        if chunk.unit_id == int(task.unit_id) and chunk.version == int(task.target_version)
    ]
    passed = bool(hit)
    if passed:
        gap.status = "closed"
        task.status = "verified"
        _log(session, ctx.user_id, "gap.verify_passed", "knowledge_gap", gap_id,
             {"status": "processing"}, {"status": "closed"})
    return {
        "passed": passed,
        "state": gap.status,
        "reason": None if passed else "补充版本未命中回放检索",
    }

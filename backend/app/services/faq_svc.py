"""FAQ 沉淀、审核与缓存（M06，FUNCTION-MAP §3 F-06.01—F-06.07）。

两条贯穿规则：
1. **FAQ 无独立 ACL**：可见性由 `faq_source` → 单元四维授权逐次推导，
   publish/匹配时都复核——绝不给 FAQ 自己的扩权配置。
2. **模型调用不占长事务**：起草 job 先落 pending，模型调用在事务外，
   答案由 H25 提交时回填。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.core.errors import TaskError
from app.core.response import BizError
from app.db.base import utcnow
from app.db.repository import UnitOfWork
from app.engines.mining import (
    QuestionVector,
    claim_logs,
    cluster_questions,
    draft_candidate,
    mark_drafts_done,
)
from app.engines.permission import authorize_units
from app.models import (
    ChatMessage,
    ChatRequest,
    ClusterMember,
    Faq,
    FaqSource,
    KnowledgeVersion,
    MessageSource,
    MiningConsumption,
    MiningRun,
    QuestionCluster,
    User,
)
from app.providers import embedding as embedding_provider
from app.services.ingest_svc import _log

logger = logging.getLogger("app.faq")

#: 语义匹配相似度门槛（首版保守值；演进走 config_revision）。
SEMANTIC_THRESHOLD = 0.92
#: 聚类相似度门槛（H23）。
CLUSTER_THRESHOLD = 0.90
#: 频次初值（H24："频次初值5"——设计常数）。
FREQUENCY_FLOOR = 5


def _engine_factory():
    settings = get_settings()
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def _check_revision(current: int, expected: int) -> None:
    if current != expected:
        raise BizError("REVISION_CONFLICT")


# ---------------------------------------------------------------- F-06.01


async def create_mining_run(
    session: AsyncSession, ctx: Any, *, pipeline_version: str
) -> dict[str, Any]:
    """登记挖掘运行（faq:review）；执行由路由层 spawn 后台任务。"""
    run = MiningRun(trigger="manual", pipeline_version=pipeline_version, status="queued")
    session.add(run)
    await session.flush()
    _log(session, ctx.user_id, "faq.create_mining_run", "mining_run", int(run.id),
         None, {"pipeline_version": pipeline_version})
    return {"run_id": int(run.id), "status": run.status}


def spawn_mining(run_id: int) -> None:
    try:
        asyncio.get_running_loop().create_task(_run_mining_task(run_id))
    except RuntimeError:
        pass  # 无事件循环（脚本环境）：调用方自行调度


async def _run_mining_task(run_id: int) -> None:
    engine, _factory = _engine_factory()
    try:
        await run_mining(run_id, engine=engine)
    except Exception:  # noqa: BLE001 — 后台兜底留日志
        logger.exception("run_mining 异常 run_id=%s", run_id)
    finally:
        await engine.dispose()


async def get_mining_run(ctx: Any, session: AsyncSession, run_id: int) -> dict[str, Any]:
    """API-S07：仅统计与安全错误。"""
    run = (
        await session.execute(select(MiningRun).where(MiningRun.id == run_id))
    ).scalar_one_or_none()
    if run is None:
        raise BizError("NOT_FOUND")
    return {
        "run_id": int(run.id),
        "status": run.status,
        "consumed": int(run.consumed),
        "candidates": int(run.candidates),
        "failed": int(run.failed),
        "error_code": run.error_code,
    }


async def run_mining(run_id: int, engine: Any | None = None) -> dict[str, Any]:
    """F-06.01：执行挖掘（H22 领取 → H11 向量化 → H23 聚类 → H24 起草 → H25 提交）。"""
    own = engine is None
    if own:
        engine, factory = _engine_factory()
    else:
        factory = async_sessionmaker(engine, expire_on_commit=False)
    settings = get_settings()
    try:
        # ---- 领取运行（queued → running，条件更新防双跑）----
        async with factory() as session:
            async with UnitOfWork(session).transaction():
                claimed = await session.execute(
                    update(MiningRun)
                    .where(MiningRun.id == run_id, MiningRun.status == "queued")
                    .values(status="running")
                )
                if claimed.rowcount != 1:
                    return {"run_id": run_id, "consumed": 0, "candidates": 0,
                            "failed": 0, "error_code": None}
                run_row = (
                    await session.execute(select(MiningRun).where(MiningRun.id == run_id))
                ).scalar_one()
                pipeline_version = run_row.pipeline_version

        consumed = candidates = failed = 0
        error_code: str | None = None
        try:
            # ---- H22：领取未消费日志 ----
            async with factory() as session:
                async with UnitOfWork(session).transaction():
                    batch = await claim_logs(session, run_id, pipeline_version, limit=50)

            questions: list[QuestionVector] = []
            # ---- 问题与来源装配（assistant 消息的 message_source）----
            async with factory() as session:
                for log_id in batch.log_ids:
                    request_row = (
                        await session.execute(
                            select(ChatRequest).where(ChatRequest.id == log_id)
                        )
                    ).scalar_one_or_none()
                    if request_row is None:
                        continue
                    user = (
                        await session.execute(
                            select(User).where(User.id == int(request_row.user_id))
                        )
                    ).scalar_one_or_none()
                    sources = [
                        {"unit_id": int(s.unit_id), "version": int(s.version), "chunk_id": None}
                        for s in (
                            await session.execute(
                                select(MessageSource)
                                .join(ChatMessage, ChatMessage.id == MessageSource.message_id)
                                .where(
                                    ChatMessage.request_id == log_id,
                                    ChatMessage.role == "assistant",
                                )
                            )
                        ).scalars()
                    ]
                    questions.append(
                        QuestionVector(
                            log_id=log_id,
                            question=request_row.question,
                            vector=[],
                            dept_id=int(user.dept_id) if user is not None and user.dept_id is not None else None,
                            sources=sources,
                        )
                    )
                # ---- H11：向量化 ----
                if questions:
                    vectors, _usage, _mv = await embedding_provider.embed_batches(
                        [q.question for q in questions], settings
                    )
                    questions = [
                        QuestionVector(q.log_id, q.question, v, q.dept_id, q.sources)
                        for q, v in zip(questions, vectors, strict=True)
                    ]

            # ---- H23：聚类（纯函数；来源为空的问题成单簇但不会产生候选）----
            clusters = cluster_questions(questions, CLUSTER_THRESHOLD) if questions else []

            # ---- 持久化簇与成员（tx）----
            cluster_ids: dict[int, int] = {}
            async with factory() as session:
                async with UnitOfWork(session).transaction():
                    for index, cluster in enumerate(clusters):
                        row = QuestionCluster(
                            representative=cluster.representative, version=pipeline_version
                        )
                        session.add(row)
                        await session.flush()
                        cluster_ids[index] = int(row.id)
                        for member in cluster.members:
                            session.add(
                                ClusterMember(cluster_id=int(row.id), log_id=member.log_id)
                            )

            # ---- H24：起草（job pending 落库 → 模型调用在事务外 → H25 回填）----
            drafts = []
            for index, cluster in enumerate(clusters):
                if not cluster.sources:
                    continue  # 无来源不产生可发布候选
                try:
                    async with factory() as session:
                        async with UnitOfWork(session).transaction():
                            drafts.append(
                                await draft_candidate(
                                    session,
                                    run_id=run_id,
                                    cluster=cluster,
                                    cluster_id=cluster_ids[index],
                                    pipeline_version=pipeline_version,
                                    model_version=settings.embedding_model_version,
                                    stream_llm=__import__(
                                        "app.providers.generation", fromlist=["stream_llm"]
                                    ).stream_llm,
                                )
                            )
                except TaskError as exc:
                    failed += 1
                    logger.warning("起草失败 run=%s cluster=%s: %s", run_id, index, exc.code)

            # ---- H25：提交（候选/归簇/消费/计数同一事务）----
            async with factory() as session:
                async with UnitOfWork(session).transaction():
                    await mark_drafts_done(session, drafts)
                    for draft in drafts:
                        faq = Faq(
                            question=draft.question,
                            answer=draft.answer,
                            status="candidate",
                            frequency=FREQUENCY_FLOOR,
                            confidence=draft.confidence,
                            cluster_id=draft.cluster_id,
                        )
                        session.add(faq)
                        await session.flush()
                        for source in draft.sources:
                            session.add(
                                FaqSource(
                                    faq_id=int(faq.id),
                                    unit_id=int(source["unit_id"]),
                                    version=int(source["version"]),
                                )
                            )
                        candidates += 1
                    for log_id in batch.log_ids:
                        await session.execute(
                            update(MiningConsumption)
                            .where(
                                MiningConsumption.log_id == log_id,
                                MiningConsumption.pipeline_version == pipeline_version,
                            )
                            .values(status="done")
                        )
                    consumed = len(batch.log_ids)
                    await session.execute(
                        update(MiningRun)
                        .where(MiningRun.id == run_id)
                        .values(consumed=consumed, candidates=candidates,
                                failed=failed, status="succeeded")
                    )
            return {"run_id": run_id, "consumed": consumed, "candidates": candidates,
                    "failed": failed, "error_code": None}
        except TaskError as exc:
            error_code = exc.code
        async with factory() as session:
            async with UnitOfWork(session).transaction():
                await session.execute(
                    update(MiningRun)
                    .where(MiningRun.id == run_id)
                    .values(status="failed", error_code=error_code)
                )
        return {"run_id": run_id, "consumed": consumed, "candidates": candidates,
                "failed": failed, "error_code": error_code}
    finally:
        if own:
            await engine.dispose()


# ---------------------------------------------------------------- F-06.02—F-06.05


async def _require_faq(session: AsyncSession, faq_id: int) -> Faq:
    faq = (
        await session.execute(select(Faq).where(Faq.id == faq_id))
    ).scalar_one_or_none()
    if faq is None:
        raise BizError("NOT_FOUND")
    return faq


async def _faq_sources(session: AsyncSession, faq_id: int) -> list[dict[str, Any]]:
    rows = (
        await session.execute(select(FaqSource).where(FaqSource.faq_id == faq_id))
    ).scalars()
    return [
        {"unit_id": int(s.unit_id), "version": int(s.version), "chunk_id": None}
        for s in rows
    ]


async def edit_candidate(
    session: AsyncSession,
    ctx: Any,
    *,
    faq_id: int,
    question: str,
    answer: str,
    source_ids: list[int],
    expected_revision: int,
) -> dict[str, Any]:
    """F-06.02：候选编辑（faq:review + 全部来源读权）。已发布先下线再编辑。"""
    faq = await _require_faq(session, faq_id)
    if faq.status != "candidate":
        raise BizError("TASK_STATE_CONFLICT", f"当前状态 {faq.status} 不允许编辑（已发布请先下线）")
    question = question.strip()
    answer = answer.strip()
    if not question or not answer:
        raise BizError("INVALID_ARGUMENT", "question 与 answer 均不能为空")
    if len(answer) > 20000:
        raise BizError("INVALID_ARGUMENT", "answer 最长 20000 字")
    unique_units = sorted({int(v) for v in source_ids})
    if not unique_units:
        raise BizError("INVALID_ARGUMENT", "来源不能为空")
    _check_revision(int(faq.revision), expected_revision)

    allowed, denied, _unavailable, _versions = await authorize_units(session, ctx, unique_units)
    if denied:
        raise BizError("PERM_DENIED", "存在无权来源，无法编辑")  # 契约明示 403

    faq.question = question
    faq.answer = answer
    await session.execute(delete(FaqSource).where(FaqSource.faq_id == faq_id))
    for unit_id in unique_units:
        version = int(
            (
                await session.execute(
                    select(KnowledgeVersion.version).where(
                        KnowledgeVersion.unit_id == unit_id
                    )
                )
            ).scalar_one_or_none()
            or 0
        )
        if not version:
            raise BizError("NOT_FOUND", f"来源单元不存在：{unit_id}")
        session.add(FaqSource(faq_id=faq_id, unit_id=unit_id, version=version))
    faq.revision = faq.revision + 1
    _log(session, ctx.user_id, "faq.edit_candidate", "faq", faq_id, None,
         {"question": question[:50], "sources": unique_units})
    return {"faq_id": faq_id, "revision": int(faq.revision), "status": faq.status}


async def publish(
    session: AsyncSession, ctx: Any, *, faq_id: int, expected_revision: int
) -> dict[str, Any]:
    """F-06.03：发布（faq:publish + 全部来源读权）。来源为空/已删除/变更 → 409。"""
    faq = await _require_faq(session, faq_id)
    if faq.status != "candidate":
        raise BizError("TASK_STATE_CONFLICT", f"当前状态 {faq.status} 不允许发布")
    _check_revision(int(faq.revision), expected_revision)
    sources = await _faq_sources(session, faq_id)
    if not sources:
        raise BizError("FAQ_SOURCE_MISSING")
    allowed, _denied, _unavailable, _versions = await authorize_units(
        session, ctx, sorted({int(s["unit_id"]) for s in sources})
    )
    if any(int(s["unit_id"]) not in allowed for s in sources):
        raise BizError("PERM_DENIED", "存在无权来源，无法发布")
    # 禁止扩展来源 ACL：FAQ 的可见性 = 来源授权的交集，这里不做任何放宽配置。

    faq.status = "published"
    faq.published_at = utcnow()
    faq.revision = faq.revision + 1
    _log(session, ctx.user_id, "faq.publish", "faq", faq_id, None, {"status": "published"})
    return {"faq_id": faq_id, "status": faq.status, "revision": int(faq.revision)}


async def change_status(
    session: AsyncSession, ctx: Any, *, faq_id: int, action: str, reason: str,
    expected_revision: int,
) -> dict[str, Any]:
    """F-06.04：驳回/下线/重新提交。理由必填；驳回不自动创造缺口。"""
    faq = await _require_faq(session, faq_id)
    reason = reason.strip()
    if not reason or len(reason) > 1000:
        raise BizError("INVALID_ARGUMENT", "理由必须为 1—1000 字")
    _check_revision(int(faq.revision), expected_revision)

    transitions = {"reject": ("candidate", "rejected"), "offline": ("published", "offline")}
    if action == "resubmit":
        if faq.status not in ("rejected", "offline", "stale"):
            raise BizError("TASK_STATE_CONFLICT", f"当前状态 {faq.status} 不允许重新提交")
        # 复核来源后回 candidate
        sources = await _faq_sources(session, faq_id)
        if not sources:
            raise BizError("FAQ_SOURCE_MISSING")
        allowed, _denied, _unavailable, _versions = await authorize_units(
            session, ctx, sorted({int(s["unit_id"]) for s in sources})
        )
        if any(int(s["unit_id"]) not in allowed for s in sources):
            raise BizError("PERM_DENIED", "存在无权来源，无法重新提交")
        faq.status = "candidate"
    elif action in transitions:
        required, target = transitions[action]
        if faq.status != required:
            raise BizError("TASK_STATE_CONFLICT", f"{action} 仅允许 {required} 状态")
        faq.status = target
    else:
        raise BizError("INVALID_ARGUMENT", f"action 必须为 reject/offline/resubmit，当前 {action!r}")
    faq.revision = faq.revision + 1
    _log(session, ctx.user_id, f"faq.{action}", "faq", faq_id, None,
         {"status": faq.status, "reason": reason})
    return {"faq_id": faq_id, "status": faq.status}


async def set_cache_enabled(
    session: AsyncSession, ctx: Any, *, faq_id: int, enabled: bool, expected_revision: int
) -> dict[str, Any]:
    """F-06.05：缓存启停（faq:publish）。审核状态独立；stale 不能靠开关恢复直出。"""
    faq = await _require_faq(session, faq_id)
    _check_revision(int(faq.revision), expected_revision)
    faq.cache_enabled = bool(enabled)
    faq.revision = faq.revision + 1
    _log(session, ctx.user_id, "faq.set_cache_enabled", "faq", faq_id, None,
         {"cache_enabled": bool(enabled)})
    return {"faq_id": faq_id, "enabled": bool(enabled), "status": faq.status}


# ---------------------------------------------------------------- F-06.06 / F-06.07


async def match_authorized(session: AsyncSession, ctx: Any, question: str) -> dict[str, Any]:
    """F-06.06：授权缓存匹配。精确规范化匹配优先 → 语义候选 → 逐项来源复核。"""
    settings = get_settings()
    normalized = " ".join(question.split()).casefold()
    candidates: list[tuple[float, Faq]] = []

    rows = (
        (
            await session.execute(
                select(Faq)
                .where(Faq.status == "published", Faq.cache_enabled.is_(True))
                .limit(50)
            )
        )
        .scalars()
        .all()
    )
    exact_hit = next(
        (f for f in rows if " ".join(f.question.split()).casefold() == normalized), None
    )
    if exact_hit is not None:
        candidates.append((1.0, exact_hit))
    else:
        # 语义候选：小规模场景直接对候选问题做即时向量化比较
        if rows:
            vectors, _usage, _mv = await embedding_provider.embed_batches(
                [f.question for f in rows], settings
            )
            qv, _u2, _m2 = await embedding_provider.embed_batches([question], settings)
            from app.engines.mining import _cosine

            scored = sorted(
                (
                    (_cosine(qv[0], vector), faq)
                    for faq, vector in zip(rows, vectors, strict=True)
                ),
                key=lambda item: -item[0],
            )
            candidates = [item for item in scored if item[0] >= SEMANTIC_THRESHOLD]

    for _score, faq in candidates:
        # 逐项当前来源授权与状态复核（高分无权跳过）
        if faq.status != "published" or not faq.cache_enabled:
            continue
        sources = await _faq_sources(session, int(faq.id))
        if not sources:
            continue
        allowed, _denied, _unavailable, _versions = await authorize_units(
            session, ctx, sorted({int(s["unit_id"]) for s in sources})
        )
        if any(int(s["unit_id"]) not in allowed for s in sources):
            continue
        usage = {
            "prompt_tokens": None, "completion_tokens": None,
            "embedding_tokens": None, "rerank_units": None, "status": "not_applicable",
        }
        return {
            "hit": True,
            "faq_id": int(faq.id),
            "answer": faq.answer,
            "sources": sources,
            "usage": usage,
        }
    return {
        "hit": False, "faq_id": None, "answer": None, "sources": [],
        "usage": {"prompt_tokens": None, "completion_tokens": None,
                  "embedding_tokens": None, "rerank_units": None, "status": "not_applicable"},
    }


async def invalidate_sources(session: AsyncSession, *, unit_id: int, change: str) -> dict[str, Any]:
    """F-06.07：来源变化失效。正文或删除 → published 转 stale；缓存失败不影响 DB 权威拒绝。"""
    staled = 0
    if change in ("content", "delete", "disable"):
        result = await session.execute(
            update(Faq)
            .where(
                Faq.status == "published",
                Faq.id.in_(select(FaqSource.faq_id).where(FaqSource.unit_id == unit_id)),
            )
            .values(status="stale")
        )
        staled = int(result.rowcount)
    # ACL 变化 → 无缓存层可逐出（匹配时逐次复核 DB 权威）；相关输出取消属后续能力。
    return {"staled": staled, "evicted": 0, "cancelled": 0}


async def list_faqs(
    session: AsyncSession,
    ctx: Any,
    *,
    status: str | None,
    q: str | None,
    page: int,
    size: int,
) -> dict[str, Any]:
    """H34：FAQ 列表（faq:review 或 faq:publish，路由层 require_any_permission）。

    ★ **来源读权过滤在分页之前**（"不先分页再过滤造成总数泄露"）：候选全量
      拉出后批量 H04 `authorize_units` 判定，`total` = 过滤后实数。FAQ 总量由
      审核流约束、全量内存判定可接受；量大时需改为可下推的授权子查询。
    """
    conditions = []
    if status:
        conditions.append(Faq.status == status)
    if q:
        conditions.append(Faq.question.like(f"%{q}%"))
    rows = (
        (await session.execute(select(Faq).where(*conditions).order_by(Faq.id.desc())))
        .scalars()
        .all()
    )
    if not rows:
        return {"items": [], "total": 0}

    source_map: dict[int, list[dict[str, Any]]] = {}
    unit_ids: set[int] = set()
    src_rows = (
        await session.execute(
            select(FaqSource).where(FaqSource.faq_id.in_([int(r.id) for r in rows]))
        )
    ).scalars().all()
    for src in src_rows:
        source_map.setdefault(int(src.faq_id), []).append(
            {"unit_id": int(src.unit_id), "version": int(src.version), "chunk_id": None}
        )
        unit_ids.add(int(src.unit_id))

    allowed, _, _, _ = await authorize_units(session, ctx, sorted(unit_ids))
    allowed_set = set(allowed)
    visible = [
        r for r in rows if {s["unit_id"] for s in source_map.get(int(r.id), [])} <= allowed_set
    ]
    total = len(visible)
    start = (page - 1) * size
    items = [
        {
            "id": int(r.id),
            "question": r.question,
            "answer": r.answer,
            "status": r.status,
            "frequency": int(r.frequency),
            "confidence": float(r.confidence) if r.confidence is not None else None,
            "source_refs": source_map.get(int(r.id), []),
            # faq 表无命中计数列（DATA-CONTRACTS §2 无 hit_count）；问答侧命中
            # 统计在 qa_audit（无 faq 关联），属看板指标——不伪造该数，记 0。
            "hit_count": 0,
        }
        for r in visible[start:start + size]
    ]
    return {"items": items, "total": total}

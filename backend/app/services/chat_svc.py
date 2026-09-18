"""会话检索与流式问答（M05，FUNCTION-MAP §3 F-05.01—F-05.09）。

模块级决策（都能在对应位置找到理由）：

1. **执行模型**：`accept_question` 受理后由路由层 spawn **后台任务**执行
   `run_answer`（202 立即返回）。崩溃残留的 running 由 H29 `recover_requests`
   条件终结 failed——"孤儿 running 标 failed，不自动重启已输出生成"。
2. **身份不信任令牌快照**：`run_answer` 执行时**从 DB 重新装配** UserCtx
   （角色/权限/停用状态）——撤权立即生效，不依赖 2 小时 access token。
3. **FAQ 路（F-06.06）未建**：M06 未实现，`run_answer` 当前只有授权检索一路，
   `faq_hit` 分支留待 M06——诚实边界，不是降级。
4. **流式终态**：终态更新、来源、审计与最终 done/error 事件**同一事务**提交
   （F-08.01），网络发送在提交之后——`stream_events` 轮询 DB 实现（首版无
   进程内广播；多 worker 下依然正确，代价是亚秒级延迟）。
5. **取消是 DB 标记 + 后台轮询**：`cancel_request` 只原子置 `cancel_requested`；
   生成期间一个轮询协程每 0.5s 刷新进程内标志，生成循环在 delta 之间检查——
   强杀无法保证"已生成部分留痕 + 终态可提交"。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.core.errors import TaskError
from app.core.response import BizError
from app.db.base import utcnow
from app.db.repository import UnitOfWork
from app.engines.permission import ACCESS_RESTRICTED_NOTICE, UserCtx, authorize_units
from app.engines.retrieval import ScoredChunk, build_messages, retrieve_authorized
from app.models import (
    ChatEvent,
    ChatMessage,
    ChatRequest,
    ChatSession,
    ConfigRevision,
    KnowledgeUnit,
    MessageSource,
    QaAudit,
    RolePermission,
    User,
    UserRole,
)
from app.providers import generation as generation_provider
from app.services import chat_store
from app.services.ingest_svc import _log

logger = logging.getLogger("app.chat")

#: FUNCTION-MAP F-05.04 边界："问题1–8000字"。
#: ★ API-CONTRACTS §1 写的是"question 1—4000"，两处冲突——按文档层级
#:   FUNCTION-MAP > API-CONTRACTS 取 8000，差异已记 WORKLOG。
QUESTION_MAX = 8000
#: 同一会话只允许一个活动请求（API-CONTRACTS §4）。
_ACTIVE_STATUSES = ("accepted", "running")
#: 事件保留期（PRD：事件 7 天；过期 410 转安全快照）。
EVENT_RETENTION_DAYS = 7
#: Prompt 字符预算（首版保守值；演进走 config_revision）。
PROMPT_BUDGET = 24000
#: 执行租约时长（H29 据此回收孤儿 running）。
_LEASE_TTL = timedelta(minutes=10)

_STATUS_BY_RESULT = {
    "answered": "completed",
    "faq_hit": "completed",
    "no_evidence": "completed",
    "low_confidence": "completed",
    "access_restricted": "rejected",
    "service_error": "failed",
}


def _engine_factory():
    settings = get_settings()
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


# ---------------------------------------------------------------- 内部工具


def _check_paging(page: int, size: int) -> None:
    if page < 1 or size < 1 or size > 100:
        raise BizError("INVALID_ARGUMENT", f"分页参数不合法：page={page}, size={size}")


async def _require_owned_session(session: AsyncSession, ctx: UserCtx, session_id: int) -> ChatSession:
    row = (
        await session.execute(select(ChatSession).where(ChatSession.id == session_id))
    ).scalar_one_or_none()
    if row is None or int(row.user_id) != int(ctx.user_id) or row.is_deleted:
        raise BizError("NOT_FOUND")
    return row


async def _require_owned_request(session: AsyncSession, ctx: UserCtx, request_id: int) -> ChatRequest:
    row = (
        await session.execute(select(ChatRequest).where(ChatRequest.id == request_id))
    ).scalar_one_or_none()
    if row is None or int(row.user_id) != int(ctx.user_id):
        raise BizError("NOT_FOUND")
    return row


async def _load_ctx(session: AsyncSession, request: ChatRequest) -> UserCtx:
    """从 DB 重新装配身份（执行侧 H01：不信令牌快照，撤权立即生效）。"""
    user = (
        await session.execute(select(User).where(User.id == int(request.user_id)))
    ).scalar_one()
    if not user.enabled:
        raise TaskError("USER_DISABLED", "账号已停用", transient=False)
    role_rows = (
        await session.execute(select(UserRole.role_id).where(UserRole.user_id == int(user.id)))
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
    return UserCtx(
        user_id=int(user.id),
        session_id=request.auth_session_id,
        dept_id=int(user.dept_id) if user.dept_id is not None else None,
        role_ids=frozenset(role_ids),
        permission_codes=frozenset(codes),
        identity_revision=int(user.identity_revision),
    )


async def _authorize_sources(
    session: AsyncSession, ctx: UserCtx, sources: list[dict[str, Any]]
) -> bool:
    """H05：来源非空 + 逐项当前有效且 judge 允许（AND；任一失败整份不返回）。"""
    if not sources:
        return False
    unit_ids = sorted({int(s["unit_id"]) for s in sources})
    _allowed, denied, _unavailable, _versions = await authorize_units(session, ctx, unit_ids)
    return not denied


# ---------------------------------------------------------------- F-05.01/02/03


async def create_session(
    session: AsyncSession, ctx: UserCtx, *, title: str | None
) -> dict[str, Any]:
    """F-05.01：创建会话。所有者 = 当前用户（不接受客户端指定）；标题 ≤100 字。"""
    clean_title = (title or "").strip() or "新的会话"
    if len(clean_title) > 100:
        raise BizError("INVALID_ARGUMENT", "标题最多 100 字")
    row = ChatSession(user_id=int(ctx.user_id), title=clean_title)
    session.add(row)
    await session.flush()
    _log(session, ctx.user_id, "chat.create_session", "chat_session", int(row.id),
         None, {"title": clean_title})
    return {"session_id": int(row.id), "title": clean_title}


async def read_history(
    session: AsyncSession, ctx: UserCtx, *, session_id: int, page: int, size: int
) -> dict[str, Any]:
    """F-05.02：历史查询。每份派生答案的**全部来源重新授权**，失败整条受限占位。"""
    _check_paging(page, size)
    await _require_owned_session(session, ctx, session_id)
    total = int(
        (
            await session.execute(
                select(func.count())
                .select_from(ChatMessage)
                .where(ChatMessage.session_id == session_id)
            )
        ).scalar_one()
    )
    rows = (
        (
            await session.execute(
                select(ChatMessage)
                .where(ChatMessage.session_id == session_id)
                .order_by(ChatMessage.id.asc())
                .offset((page - 1) * size)
                .limit(size)
            )
        )
        .scalars()
        .all()
    )
    message_ids = [int(row.id) for row in rows]
    sources_by_message: dict[int, list[dict[str, Any]]] = {}
    if message_ids:
        source_rows = (
            await session.execute(
                select(MessageSource)
                .where(MessageSource.message_id.in_(message_ids))
                .order_by(MessageSource.message_id, MessageSource.no)
            )
        ).scalars()
        for source in source_rows:
            sources_by_message.setdefault(int(source.message_id), []).append(
                {"unit_id": int(source.unit_id), "version": int(source.version),
                 "chunk_id": int(source.chunk_id) if source.chunk_id is not None else None}
            )

    items: list[dict[str, Any]] = []
    for row in rows:
        restricted = bool(row.restricted)
        if row.role == "assistant" and not restricted:
            sources = sources_by_message.get(int(row.id), [])
            if not sources or not await _authorize_sources(session, ctx, sources):
                restricted = True  # 无来源旧答案不回填；来源已无权 → 整条受限占位
        items.append(
            {"id": int(row.id), "role": row.role,
             "text": None if restricted else row.text, "restricted": restricted}
        )
    return {"items": items, "total": total}


async def mutate_session(
    session: AsyncSession, ctx: UserCtx, *, session_id: int, action: str, title: str | None
) -> dict[str, Any]:
    """F-05.03：重命名 / 逻辑删除。删除时取消活动请求；重复删除幂等；审计保留。"""
    row = await _require_owned_session(session, ctx, session_id)
    if action == "rename":
        clean = (title or "").strip()
        if not clean or len(clean) > 100:
            raise BizError("INVALID_ARGUMENT", "标题必须为 1—100 字")
        row.title = clean
        _log(session, ctx.user_id, "chat.rename_session", "chat_session", session_id,
             None, {"title": clean})
        return {"session_id": session_id, "deleted": False}
    if action == "delete":
        if not row.is_deleted:
            row.is_deleted = True
            await session.execute(
                update(ChatRequest)
                .where(
                    ChatRequest.session_id == session_id,
                    ChatRequest.status.in_(_ACTIVE_STATUSES),
                )
                .values(cancel_requested=True)
            )
            _log(session, ctx.user_id, "chat.delete_session", "chat_session", session_id,
                 {"is_deleted": False}, {"is_deleted": True})
        return {"session_id": session_id, "deleted": True}  # 重复删除幂等
    raise BizError("INVALID_ARGUMENT", f"action 必须为 rename/delete，当前 {action!r}")


# ---------------------------------------------------------------- F-05.04


def _payload_hash(session_id: int, question: str) -> str:
    """载荷指纹：session_id + 规范化问题（API-CONTRACTS §4）。"""
    normalized = " ".join(question.split())
    return hashlib.sha256(f"{session_id}\x00{normalized}".encode("utf-8")).hexdigest()


async def accept_question(
    session: AsyncSession,
    ctx: UserCtx,
    *,
    session_id: int,
    client_request_id: str,
    question: str,
) -> dict[str, Any]:
    """F-05.04：幂等受理。同键同载荷复用；异载荷 409；会话并发 409。

    受理只登记（status=accepted）+ 初始审计 + user 消息——**不是**生成成功；
    执行由 `spawn_answer` 在事务提交后启动。
    """
    await _require_owned_session(session, ctx, session_id)
    question = question.strip()
    if not question or len(question) > QUESTION_MAX:
        raise BizError("INVALID_ARGUMENT", f"问题长度必须为 1—{QUESTION_MAX} 字")

    # ★ 幂等查询必须先于会话忙检查：同键重放是"同一个请求"，不能被自己
    #   的活动状态挡在门外（否则网络重试在生成期间会拿到 409 而非原 ID）。
    payload_hash = _payload_hash(session_id, question)
    existing = (
        await session.execute(
            select(ChatRequest).where(
                ChatRequest.user_id == int(ctx.user_id),
                ChatRequest.client_request_id == client_request_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.payload_hash != payload_hash:
            raise BizError("IDEMPOTENCY_CONFLICT")
        return {"request_id": int(existing.id), "status": existing.status, "created": False}

    busy = int(
        (
            await session.execute(
                select(func.count())
                .select_from(ChatRequest)
                .where(
                    ChatRequest.session_id == session_id,
                    ChatRequest.status.in_(_ACTIVE_STATUSES),
                )
            )
        ).scalar_one()
    )
    if busy > 0:
        raise BizError("SESSION_BUSY")

    config_revision_id = (
        await session.execute(
            select(ConfigRevision.id).order_by(ConfigRevision.id.desc()).limit(1)
        )
    ).scalar_one_or_none()
    if config_revision_id is None:
        raise BizError("DEPENDENCY_UNAVAILABLE", "缺少配置修订快照，请先执行种子数据")

    request_row = ChatRequest(
        user_id=int(ctx.user_id),
        auth_session_id=ctx.session_id,
        session_id=session_id,
        client_request_id=client_request_id,
        payload_hash=payload_hash,
        question=question,
        config_revision=int(config_revision_id),
        accepted_at=utcnow(),
    )
    session.add(request_row)
    await session.flush()
    session.add(
        ChatMessage(session_id=session_id, request_id=int(request_row.id),
                    role="user", text=question)
    )
    session.add(
        QaAudit(
            request_id=int(request_row.id),
            user_id=int(ctx.user_id),
            asked_at=utcnow(),
            question=question,
            recall_snapshot={},
            allowed_snapshot={},
            denied_snapshot={},
            status="accepted",
            usage_status="not_applicable",
        )
    )
    _log(session, ctx.user_id, "chat.accept_question", "chat_request", int(request_row.id),
         None, {"session_id": session_id, "question_chars": len(question)})
    return {"request_id": int(request_row.id), "status": request_row.status, "created": True}


def spawn_answer(request_id: int) -> None:
    """受理事务提交后启动后台执行。无事件循环的环境（脚本）交调用方自行调度。"""
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_run_answer_task(request_id))
    except RuntimeError:
        logger.warning("spawn_answer：无运行中事件循环，request_id=%s 未调度", request_id)


async def _run_answer_task(request_id: int) -> None:
    engine, _factory = _engine_factory()
    try:
        await run_answer(request_id, engine=engine)
    except Exception:  # noqa: BLE001 — 后台任务兜底：日志留痕，终态由 H29 兜底回收
        logger.exception("run_answer 后台任务异常 request_id=%s", request_id)
    finally:
        await engine.dispose()


# ---------------------------------------------------------------- F-05.05


def _spawn_cancel_poller(factory, request_id: int, state: dict[str, bool]) -> asyncio.Task:
    """每 0.5s 刷新进程内取消标志（生成循环做同步检查，不能每帧查库）。"""

    async def _poll() -> None:
        while not state["cancelled"]:
            await asyncio.sleep(0.5)
            try:
                async with factory() as session:
                    value = (
                        await session.execute(
                            select(ChatRequest.cancel_requested)
                            .where(ChatRequest.id == request_id)
                        )
                    ).scalar_one_or_none()
                state["cancelled"] = bool(value)
            except Exception:  # noqa: BLE001 — 查询失败不视为取消（fail-safe 生成继续）
                pass

    return asyncio.create_task(_poll())


async def run_answer(request_id: int, engine: Any | None = None) -> dict[str, Any]:
    """F-05.05：执行授权回答（内部任务；终态收敛在 F-08.01）。"""
    from app.services import metrics_svc

    own_engine = engine is None
    if own_engine:
        engine, factory = _engine_factory()
    else:
        factory = async_sessionmaker(engine, expire_on_commit=False)
    started = time.monotonic()

    retrieval = None
    result_type = "service_error"
    answer_text = ""
    sources: list[dict[str, Any]] = []
    citations: list[dict[str, Any]] = []
    usage_generation: dict[str, Any] | None = None
    first_token_ms: int | None = None
    error_code: str | None = None
    cancelled = False
    recall_ids: list[int] = []
    allowed_ids: list[int] = []
    denied_ids: list[int] = []
    poller: asyncio.Task | None = None

    try:
        # ---- 领取：accepted → running（条件更新，防双 worker）+ meta 事件 ----
        async with factory() as session:
            async with UnitOfWork(session).transaction():
                claimed = await session.execute(
                    update(ChatRequest)
                    .where(ChatRequest.id == request_id, ChatRequest.status == "accepted")
                    .values(status="running", execution_lease_until=utcnow() + _LEASE_TTL)
                )
                if claimed.rowcount != 1:
                    return {"status": "skipped", "result_type": None, "error_code": None}
                request_row = (
                    await session.execute(
                        select(ChatRequest).where(ChatRequest.id == request_id)
                    )
                ).scalar_one()
                session_id = int(request_row.session_id)
                question = request_row.question
                await chat_store.append_event(
                    session, request_id, "meta",
                    {"request_id": request_id, "session_id": session_id, "status": "running"},
                )

        try:
            # ---- 身份重装配 + 功能权限 ----
            async with factory() as session:
                request_row = (
                    await session.execute(
                        select(ChatRequest).where(ChatRequest.id == request_id)
                    )
                ).scalar_one()
                ctx = await _load_ctx(session, request_row)
            if "ai:ask" not in ctx.permission_codes:
                raise TaskError("PERM_AI_DENIED", "未开通 AI 问答权限", transient=False)

            # ---- 授权检索（F-06.06 FAQ 路未建——见模块注释）----
            settings = get_settings()
            retrieval_config = {
                "settings": settings,
                "vector_top_k": 20, "keyword_top_k": 20,
                "answer_top_k": 5, "rrf_k": 60,
            }
            async with factory() as session:
                retrieval = await retrieve_authorized(session, ctx, question, retrieval_config)
                recall_ids = sorted(
                    {chunk.unit_id for chunk in retrieval.allowed} | set(retrieval.denied_ids)
                )
                allowed_ids = sorted({chunk.unit_id for chunk in retrieval.allowed})
                denied_ids = list(retrieval.denied_ids)
                titles: dict[int, str] = {}
                if allowed_ids:
                    title_rows = (
                        await session.execute(
                            select(KnowledgeUnit.id, KnowledgeUnit.title).where(
                                KnowledgeUnit.id.in_(allowed_ids)
                            )
                        )
                    ).all()
                    titles = {int(r[0]): str(r[1]) for r in title_rows}
                # 历史（Prompt 用）：H05 逐轮再授权
                latest_user = (
                    select(func.max(ChatMessage.id)).where(
                        ChatMessage.session_id == session_id, ChatMessage.role == "user"
                    )
                ).scalar_subquery()
                history_rows = (
                    (
                        await session.execute(
                            select(ChatMessage)
                            .where(
                                ChatMessage.session_id == session_id,
                                ChatMessage.id < latest_user,
                            )
                            .order_by(ChatMessage.id.asc())
                            .limit(20)
                        )
                    )
                    .scalars()
                    .all()
                )
                assistant_ids = [int(r.id) for r in history_rows if r.role == "assistant"]
                sources_by_message: dict[int, list[dict[str, Any]]] = {}
                if assistant_ids:
                    src_rows = (
                        await session.execute(
                            select(MessageSource).where(
                                MessageSource.message_id.in_(assistant_ids)
                            )
                        )
                    ).scalars()
                    for s in src_rows:
                        sources_by_message.setdefault(int(s.message_id), []).append(
                            {"unit_id": int(s.unit_id), "version": int(s.version),
                             "chunk_id": int(s.chunk_id) if s.chunk_id is not None else None}
                        )
                history: list[dict[str, Any]] = []
                for row in history_rows:
                    if row.role == "assistant":
                        sources_h = sources_by_message.get(int(row.id), [])
                        if not sources_h or not await _authorize_sources(session, ctx, sources_h):
                            continue  # 无来源旧答案不回填
                    history.append({"role": row.role, "text": row.text})

            messages: list[dict[str, str]] = []
            if retrieval.result_type == "answered" and retrieval.allowed:
                evidence: list[ScoredChunk] = retrieval.allowed
                messages = build_messages(history, evidence, question, PROMPT_BUDGET)
                result_type = "answered"
                sources = [
                    {"unit_id": chunk.unit_id, "version": chunk.version,
                     "chunk_id": chunk.chunk_id}
                    for chunk in evidence
                ]
                citations = [
                    {"no": i, "unit_id": chunk.unit_id, "version": chunk.version,
                     "chunk_id": chunk.chunk_id, "title": titles.get(chunk.unit_id, "")}
                    for i, chunk in enumerate(evidence, start=1)
                ]
            elif retrieval.result_type == "access_restricted":
                result_type = "access_restricted"
            else:
                result_type = "no_evidence"

            # ---- citations / denied 事件（事务内持久，提交后由 SSE 轮询发出）----
            async with factory() as session:
                async with UnitOfWork(session).transaction():
                    if citations:
                        await chat_store.append_event(
                            session, request_id, "citations", {"items": citations}
                        )

            # ---- 生成（H13）；无证据/受限不走模型 ----
            if result_type == "answered":
                state = {"cancelled": False}
                poller = _spawn_cancel_poller(factory, request_id, state)
                deltas: list[str] = []
                try:
                    async for delta in generation_provider.stream_llm(
                        messages, settings, _StateSignal(state)
                    ):
                        if delta.text:
                            if first_token_ms is None:
                                first_token_ms = int((time.monotonic() - started) * 1000)
                            deltas.append(delta.text)
                            if delta.usage:
                                usage_generation = delta.usage
                            async with factory() as session:
                                async with UnitOfWork(session).transaction():
                                    await chat_store.append_event(
                                        session, request_id, "delta", {"text": delta.text}
                                    )
                        if delta.finish_reason:
                            break
                    answer_text = "".join(deltas)
                    cancelled = state["cancelled"]
                except TaskError as exc:
                    error_code = exc.code
                    result_type = "service_error"
            elif result_type == "access_restricted":
                async with factory() as session:
                    async with UnitOfWork(session).transaction():
                        await chat_store.append_event(
                            session, request_id, "denied", {"message": ACCESS_RESTRICTED_NOTICE}
                        )
            else:  # no_evidence：固定说明，不用模型编造
                answer_text = "知识库中没有找到与该问题相关的内容。"
                async with factory() as session:
                    async with UnitOfWork(session).transaction():
                        await chat_store.append_event(
                            session, request_id, "delta", {"text": answer_text}
                        )
        except TaskError as exc:
            error_code = exc.code
            result_type = "service_error"
        except Exception:  # noqa: BLE001 — 异常不冒充"无知识"，留日志、终态 failed
            logger.exception("run_answer 异常 request_id=%s", request_id)
            error_code = "INTERNAL_ERROR"
            result_type = "service_error"

        status = "cancelled" if cancelled else _STATUS_BY_RESULT.get(result_type, "failed")
        if cancelled:
            result_type = None  # 取消是请求状态，不是业务结果（DB 允许 NULL）

        duration_ms = int((time.monotonic() - started) * 1000)
        usage = {
            "prompt_tokens": (usage_generation or {}).get("prompt_tokens"),
            "completion_tokens": (usage_generation or {}).get("completion_tokens"),
            "embedding_tokens": None,  # H11 的 usage 未接入 RetrievalResult（诚实边界）
            "rerank_units": None,      # H12 rerank 未建
            "status": "known" if usage_generation else "unknown",
        }
        await metrics_svc.finalize_request(
            request_id,
            {
                "status": status,
                "result_type": result_type,
                "answer": answer_text if result_type == "answered" or result_type == "no_evidence" else None,
                "restricted": result_type == "access_restricted",
                "sources": sources,
                "citations": citations,
                "error_code": error_code,
                "duration_ms": duration_ms,
                "first_token_ms": first_token_ms,
                "recall_ids": recall_ids,
                "allowed_ids": allowed_ids,
                "denied_ids": denied_ids,
            },
            usage,
            factory=factory,
        )
        # 缺口消费：终态登记的 outbox 就地消费（消费者按 request_id 幂等）。
        from app.services import gap_svc

        await gap_svc.consume_outbox(factory)
        return {"status": status, "result_type": result_type, "error_code": error_code}
    finally:
        if poller is not None:
            poller.cancel()
        if own_engine:
            await engine.dispose()


class _StateSignal:
    """适配轮询协程状态的取消信号（与 core.cancel.CancelSignal 同语义）。"""

    def __init__(self, state: dict[str, bool]) -> None:
        self._state = state

    @property
    def cancelled(self) -> bool:
        return self._state["cancelled"]


# ---------------------------------------------------------------- F-05.06


async def stream_events(ctx: UserCtx, request_id: int, after_seq: int) -> AsyncSession:  # noqa: ANN201 — 生成器
    """F-05.06：SSE 事件流（归属/保留期校验在路由层完成后进入本生成器）。

    逐批读新事件发送；终态（done/error）后自然结束；15s 无事件发注释帧心跳。
    每批复核身份有效（停用 → 安全 error 帧，无 seq，不推进游标）。
    """
    engine, factory = _engine_factory()
    last_beat = time.monotonic()
    cursor = after_seq
    try:
        while True:
            async with factory() as session:
                snapshot = await chat_store.get_request(session, ctx, request_id)
                user = (
                    await session.execute(select(User).where(User.id == int(ctx.user_id)))
                ).scalar_one()
                events = (
                    (
                        await session.execute(
                            select(ChatEvent)
                            .where(ChatEvent.request_id == request_id, ChatEvent.seq > cursor)
                            .order_by(ChatEvent.seq.asc())
                        )
                    )
                    .scalars()
                    .all()
                )
            if not user.enabled:
                yield 'event: error\ndata: {"code":"ACCESS_RESTRICTED","message":"登录状态已变更"}\n\n'
                return
            for row in events:
                cursor = int(row.seq)
                data = json.dumps(row.payload, ensure_ascii=False)
                yield f"id: {row.seq}\nevent: {row.event}\ndata: {data}\n\n"
                if row.event in ("done", "error"):
                    return
            if not events:
                if time.monotonic() - last_beat >= 15:
                    yield ": heartbeat\n\n"
                    last_beat = time.monotonic()
                await asyncio.sleep(0.3)
            else:
                last_beat = time.monotonic()
    finally:
        await engine.dispose()


# ---------------------------------------------------------------- F-05.07/08/09


async def cancel_request(
    session: AsyncSession, ctx: UserCtx, *, request_id: int
) -> dict[str, Any]:
    """F-05.07：原子取消标记。完成态不改（幂等）；断网不等于主动取消。"""
    row = await _require_owned_request(session, ctx, request_id)
    if row.status in _ACTIVE_STATUSES:
        await session.execute(
            update(ChatRequest)
            .where(ChatRequest.id == request_id, ChatRequest.status.in_(_ACTIVE_STATUSES))
            .values(cancel_requested=True)
        )
        _log(session, ctx.user_id, "chat.cancel_request", "chat_request", request_id,
             None, {"from_status": row.status})
    return {"request_id": request_id, "status": row.status}


async def read_citation(
    session: AsyncSession, ctx: UserCtx, *, request_id: int, no: int
) -> dict[str, Any]:
    """F-05.08：引用详情。按**已记录版本**取正文；当前无权 → 404（不返回标题正文）。"""
    await _require_owned_request(session, ctx, request_id)
    assistant = (
        await session.execute(
            select(ChatMessage).where(
                ChatMessage.request_id == request_id, ChatMessage.role == "assistant"
            )
        )
    ).scalar_one_or_none()
    if assistant is None or assistant.restricted:
        raise BizError("NOT_FOUND")
    source = (
        await session.execute(
            select(MessageSource).where(
                MessageSource.message_id == int(assistant.id), MessageSource.no == no
            )
        )
    ).scalar_one_or_none()
    if source is None:
        raise BizError("NOT_FOUND")

    unit = (
        await session.execute(
            select(KnowledgeUnit).where(KnowledgeUnit.id == int(source.unit_id))
        )
    ).scalar_one_or_none()
    allowed_units, _denied, _unavailable, _versions = await authorize_units(
        session, ctx, [int(source.unit_id)]
    )
    if unit is None or unit.is_deleted or int(source.unit_id) not in allowed_units:
        raise BizError("NOT_FOUND")

    from app.models import Chunk

    snippet = ""
    page_no: int | None = None
    offset: int | None = None
    if source.chunk_id is not None:
        chunk = (
            await session.execute(select(Chunk).where(Chunk.id == int(source.chunk_id)))
        ).scalar_one_or_none()
        if chunk is not None:
            snippet = chunk.text[:200]
            page_no = chunk.location.get("page_no")
            offset = chunk.location.get("start_offset")
    return {
        "no": int(no),
        "unit_id": int(source.unit_id),
        "version": int(source.version),
        "chunk_id": int(source.chunk_id) if source.chunk_id is not None else None,
        "title": unit.title,
        "snippet": snippet,
        "page_no": page_no,
        "offset": offset,
    }


async def suggest(
    session: AsyncSession, ctx: UserCtx, *, prefix: str, limit: int
) -> dict[str, Any]:
    """F-05.09：联想。可读知识标题（H04 逐项授权）+ 本人历史；FAQ 候选属 M06。"""
    prefix = (prefix or "").strip()
    if not 1 <= limit <= 10:
        raise BizError("INVALID_ARGUMENT", "limit 必须为 1—10")
    if not prefix:
        return {"items": []}
    if len(prefix) > 200:
        raise BizError("INVALID_ARGUMENT", "prefix 最长 200 字符")

    candidates: list[str] = []
    unit_rows = (
        (
            await session.execute(
                select(KnowledgeUnit)
                .where(
                    KnowledgeUnit.is_deleted.is_(False),
                    KnowledgeUnit.enabled.is_(True),
                    KnowledgeUnit.title.like(f"%{prefix}%"),
                )
                .limit(30)
            )
        )
        .scalars()
        .all()
    )
    if unit_rows:
        allowed, _denied, _unavailable, _versions = await authorize_units(
            session, ctx, [int(u.id) for u in unit_rows]
        )
        titles = {int(u.id): u.title for u in unit_rows}
        candidates.extend(titles[uid] for uid in allowed if uid in titles)
    history_rows = (
        await session.execute(
            select(ChatRequest.question)
            .where(
                ChatRequest.user_id == int(ctx.user_id),
                ChatRequest.question.like(f"%{prefix}%"),
            )
            .order_by(ChatRequest.accepted_at.desc())
            .limit(20)
        )
    ).all()

    seen: set[str] = set()
    items: list[str] = []
    for candidate in [*candidates, *(str(r[0]) for r in history_rows)]:
        key = candidate.casefold()
        if key not in seen:
            seen.add(key)
            items.append(candidate)
        if len(items) >= limit:
            break
    return {"items": items}


async def get_request_snapshot(
    session: AsyncSession, ctx: UserCtx, *, request_id: int
) -> dict[str, Any]:
    """H27：请求安全快照（API-CONTRACTS §4 末行：`GET /api/chat/requests/{id}`→H27）。

    归属：他人请求 404（`_require_owned_request`，防枚举）。
    受限语义：以**助手消息**的 restricted 标记为准；受限时不返回正文——
    快照只用于断线/410 恢复状态与游标，正文恢复走 events 重放。
    """
    request = await _require_owned_request(session, ctx, request_id)
    restricted = request.result_type == "access_restricted"
    answer: str | None = None
    message = (
        await session.execute(
            select(ChatMessage)
            .where(ChatMessage.request_id == request_id, ChatMessage.role == "assistant")
            .order_by(ChatMessage.id.desc())
        )
    ).scalars().first()
    if message is not None:
        restricted = bool(message.restricted)
        answer = None if message.restricted else message.text
    return {
        "status": request.status,
        "last_seq": int(request.last_seq),
        "answer": answer,
        "restricted": restricted,
    }

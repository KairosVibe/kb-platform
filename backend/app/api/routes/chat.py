"""会话与流式问答路由（M05：F-05.01—F-05.09）。

两条路由层纪律之外的两处特殊：
1. **SSE 不套 JSON 外壳**（API-CONTRACTS §1）：`/events` 返回 `text/event-stream`；
   归属/游标/保留期校验在**返回 StreamingResponse 之前**完成——
   "不能将 HTTP 已开始后的错误写成第二个 HTTP 响应"。
2. **`Last-Event-ID` 兼容**：与 `after_seq` 同时提供必须相同，否则 422。
"""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_ctx, require_permission
from app.core.response import BizError, ok
from app.db.base import utcnow
from app.db.repository import UnitOfWork
from app.db.session import get_session
from app.engines.permission import UserCtx
from app.schemas.chat import AcceptQuestionRequest, CreateSessionRequest, MutateSessionRequest
from app.services import chat_store, chat_svc

router = APIRouter(tags=["chat"])


@router.post("/sessions", dependencies=[Depends(require_permission("ai:ask"))], status_code=201)
async def create_session(
    session: Annotated[AsyncSession, Depends(get_session)],
    ctx: Annotated[UserCtx, Depends(get_current_ctx)],
    body: CreateSessionRequest,
):
    """F-05.01：创建会话（同步资源 201）。"""
    async with UnitOfWork(session).transaction():
        data = await chat_svc.create_session(session, ctx, title=body.title)
    return ok(data)


@router.get("/sessions", dependencies=[Depends(require_permission("ai:ask"))])
async def list_sessions(
    session: Annotated[AsyncSession, Depends(get_session)],
    ctx: Annotated[UserCtx, Depends(get_current_ctx)],
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
):
    """H28：我的会话侧栏。"""
    async with UnitOfWork(session).transaction():
        data = await chat_store.list_sessions(session, ctx, page=page, size=size)
    return ok(data)


@router.get(
    "/sessions/{session_id}/messages",
    dependencies=[Depends(require_permission("ai:ask"))],
)
async def read_history(
    session_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    ctx: Annotated[UserCtx, Depends(get_current_ctx)],
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
):
    """F-05.02：历史查询（来源重授权，受限占位）。"""
    async with UnitOfWork(session).transaction():
        data = await chat_svc.read_history(session, ctx, session_id=session_id, page=page, size=size)
    return ok(data)


@router.patch("/sessions/{session_id}", dependencies=[Depends(require_permission("ai:ask"))])
async def mutate_session(
    session_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    ctx: Annotated[UserCtx, Depends(get_current_ctx)],
    body: MutateSessionRequest,
):
    """F-05.03：重命名 / 删除。"""
    async with UnitOfWork(session).transaction():
        data = await chat_svc.mutate_session(
            session, ctx, session_id=session_id, action=body.action, title=body.title
        )
    return ok(data)


@router.post(
    "/chat/requests",
    dependencies=[Depends(require_permission("ai:ask"))],
    status_code=202,
)
async def accept_question(
    session: Annotated[AsyncSession, Depends(get_session)],
    ctx: Annotated[UserCtx, Depends(get_current_ctx)],
    body: AcceptQuestionRequest,
):
    """F-05.04：幂等受理。新建 202（已登记，非生成成功）；**同键同载荷重放 200 + 原 ID**
    （API-CONTRACTS §1"已存在且同载荷的幂等请求返回 200"）。"""
    async with UnitOfWork(session).transaction():
        data = await chat_svc.accept_question(
            session, ctx, session_id=body.session_id,
            client_request_id=body.client_request_id, question=body.question,
        )
    created = data.pop("created", True)
    envelope = ok(data)
    if not created:
        return JSONResponse(status_code=200, content=envelope)
    chat_svc.spawn_answer(int(data["request_id"]))  # 事务提交后再调度；重放不重复执行
    return JSONResponse(status_code=202, content=envelope)


@router.get(
    "/chat/requests/{request_id}/events",
    dependencies=[Depends(require_permission("ai:ask"))],
)
async def stream_events(
    request_id: int,
    request: Request,
    ctx: Annotated[UserCtx, Depends(get_current_ctx)],
    db: Annotated[AsyncSession, Depends(get_session)],
    after_seq: int = Query(0, ge=0),
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
):
    """F-05.06：SSE 事件订阅（text/event-stream，不套 JSON 外壳）。"""
    # 双游标一致性：同时提供必须相同（API-CONTRACTS §4）。
    if last_event_id is not None:
        try:
            header_seq = int(last_event_id)
        except ValueError as exc:
            raise BizError("INVALID_ARGUMENT", "Last-Event-ID 必须为整数") from exc
        if header_seq != after_seq:
            raise BizError("INVALID_ARGUMENT", "after_seq 与 Last-Event-ID 不一致")

    snapshot = await chat_store.get_request(db, ctx, request_id)
    if after_seq > snapshot["last_seq"]:
        raise BizError("INVALID_ARGUMENT", "after_seq 超过当前 last_seq")
    finished_at = snapshot.get("finished_at")
    if finished_at is not None and finished_at < utcnow() - timedelta(
        days=chat_svc.EVENT_RETENTION_DAYS
    ):
        raise BizError("EVENT_CURSOR_EXPIRED")

    return StreamingResponse(
        chat_svc.stream_events(ctx, request_id, after_seq),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post(
    "/chat/requests/{request_id}/cancel",
    dependencies=[Depends(require_permission("ai:ask"))],
)
async def cancel_request(
    request_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    ctx: Annotated[UserCtx, Depends(get_current_ctx)],
):
    """F-05.07：取消请求。"""
    async with UnitOfWork(session).transaction():
        data = await chat_svc.cancel_request(session, ctx, request_id=request_id)
    return ok(data)


@router.get(
    "/chat/requests/{request_id}/citations/{no}",
    dependencies=[Depends(require_permission("ai:ask"))],
)
async def read_citation(
    request_id: int,
    no: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    ctx: Annotated[UserCtx, Depends(get_current_ctx)],
):
    """F-05.08：引用详情（当前授权不过 → 404，不返回标题正文）。"""
    async with UnitOfWork(session).transaction():
        data = await chat_svc.read_citation(session, ctx, request_id=request_id, no=no)
    return ok(data)


@router.get("/chat/suggestions", dependencies=[Depends(require_permission("ai:ask"))])
async def suggest(
    session: Annotated[AsyncSession, Depends(get_session)],
    ctx: Annotated[UserCtx, Depends(get_current_ctx)],
    prefix: str = "",
    limit: int = Query(5, ge=1, le=10),
):
    """F-05.09：智能联想。"""
    async with UnitOfWork(session).transaction():
        data = await chat_svc.suggest(session, ctx, prefix=prefix, limit=limit)
    return ok(data)


@router.get("/chat/requests/{request_id}", dependencies=[Depends(require_permission("ai:ask"))])
async def get_request_snapshot(
    request_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    ctx: Annotated[UserCtx, Depends(get_current_ctx)],
):
    """H27：请求安全快照（410/断线恢复入口；他人 404 防枚举，受限不给正文）。"""
    async with UnitOfWork(session).transaction():
        data = await chat_svc.get_request_snapshot(session, ctx, request_id=request_id)
    return ok(data)

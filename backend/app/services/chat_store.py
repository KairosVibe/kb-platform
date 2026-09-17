"""会话/请求/事件的安全读取与事件持久化（H26—H29）。

`append_event` 的 seq 分配用**请求行锁**（`SELECT ... FOR UPDATE`）：
主键 `(request_id, seq)` 是去重的最后防线，而行锁保证并发追加不会拿到同一个 seq——
应用层判断在并发下永远是错的，数据库约束+锁才是保证。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.response import BizError
from app.models import ChatEvent, ChatMessage, ChatRequest, ChatSession


async def append_event(
    session: AsyncSession, request_id: int, event: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """H26：分配单调 seq 并持久化事件。必须在调用方事务内执行。"""
    request_row = (
        await session.execute(
            select(ChatRequest).where(ChatRequest.id == request_id).with_for_update()
        )
    ).scalar_one()
    seq = int(request_row.last_seq) + 1
    session.add(
        ChatEvent(request_id=request_id, seq=seq, event=event, payload=payload)
    )
    await session.execute(
        update(ChatRequest)
        .where(ChatRequest.id == request_id)
        .values(last_seq=seq)
    )
    return {"request_id": request_id, "seq": seq, "event": event, "payload": payload}


async def get_request(session: AsyncSession, ctx: Any, request_id: int) -> dict[str, Any]:
    """H27：归属校验 + 安全快照（他人 404 防枚举）。"""
    row = (
        await session.execute(
            select(ChatRequest).where(ChatRequest.id == request_id)
        )
    ).scalar_one_or_none()
    if row is None or int(row.user_id) != int(ctx.user_id):
        raise BizError("NOT_FOUND")
    assistant = (
        await session.execute(
            select(ChatMessage).where(
                ChatMessage.request_id == request_id, ChatMessage.role == "assistant"
            )
        )
    ).scalar_one_or_none()
    return {
        "request_id": request_id,
        "session_id": int(row.session_id),
        "status": row.status,
        "result_type": row.result_type,
        "last_seq": int(row.last_seq),
        "answer": assistant.text if assistant is not None else None,
        "restricted": bool(assistant.restricted) if assistant is not None else False,
        "cancel_requested": bool(row.cancel_requested),
        "finished_at": row.finished_at,
    }


async def list_sessions(
    session: AsyncSession, ctx: Any, *, page: int, size: int
) -> dict[str, Any]:
    """H28：我的会话（owner 过滤，最近更新优先）。"""
    conditions = (ChatSession.user_id == int(ctx.user_id), ChatSession.is_deleted.is_(False))
    total = int(
        (
            await session.execute(
                select(func.count()).select_from(ChatSession).where(*conditions)
            )
        ).scalar_one()
    )
    rows = (
        (
            await session.execute(
                select(ChatSession)
                .where(*conditions)
                .order_by(ChatSession.updated_at.desc(), ChatSession.id.desc())
                .offset((page - 1) * size)
                .limit(size)
            )
        )
        .scalars()
        .all()
    )
    return {
        "items": [
            {"id": int(row.id), "title": row.title, "updated_at": row.updated_at}
            for row in rows
        ],
        "total": total,
    }


async def recover_requests(session: AsyncSession, now: datetime) -> int:
    """H29：孤儿 running（执行租约过期）条件终结 failed——不自动重启已输出生成。"""
    result = await session.execute(
        update(ChatRequest)
        .where(
            ChatRequest.status == "running",
            ChatRequest.execution_lease_until.is_not(None),
            ChatRequest.execution_lease_until < now,
        )
        .values(status="failed", result_type="service_error", finished_at=now)
    )
    return int(result.rowcount)

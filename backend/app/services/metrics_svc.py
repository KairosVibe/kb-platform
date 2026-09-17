"""运营指标与请求终态（M08；本文件先落地 F-08.01 finalize_request）。

终态**唯一事务**（API-CONTRACTS §4 原文要求）：
请求状态/结果 + 助手消息 + 最终来源 + 审计快照 + 用量 + 最终 done/error 事件，
全部在同一 DB 事务提交；网络发送在提交之后（由 stream_events 轮询读到）。

"条件更新唯一终态"：终态 UPDATE 带 `status IN (accepted, running)` 条件——
两个并发 finalize 只有一个生效，后来者幂等返回既有结果。
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.db.base import utcnow
from app.db.repository import UnitOfWork
from app.models import ChatMessage, ChatRequest, MessageSource, ModelCallUsage, QaAudit, User
from app.services import chat_store
from app.services.ingest_svc import _log


async def finalize_request(
    request_id: int,
    result: dict[str, Any],
    usage: dict[str, Any],
    *,
    factory: Any | None = None,
) -> dict[str, Any]:
    """F-08.01：条件更新唯一终态 + 来源/审计/用量/最终事件同事务。

    `result` 键：status/result_type/answer/restricted/sources/error_code/
    duration_ms/first_token_ms/recall_ids/allowed_ids/denied_ids。
    `usage` 键（FUNCTION-MAP §1 `Usage`）：prompt_tokens/completion_tokens/
    embedding_tokens/rerank_units/status/call_ids。
    """
    if factory is None:
        settings = get_settings()
        engine = create_async_engine(settings.database_url, poolclass=NullPool)
        factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as session:
        async with UnitOfWork(session).transaction():
            now = utcnow()
            request_row = (
                await session.execute(
                    select(ChatRequest).where(ChatRequest.id == request_id).with_for_update()
                )
            ).scalar_one_or_none()
            if request_row is None:
                raise ValueError(f"finalize_request：请求不存在 {request_id}")
            if request_row.status not in ("accepted", "running"):
                # 已终态：幂等返回既有结果，不产生第二个 done/error。
                return {"request_id": request_id, "status": request_row.status}

            await session.execute(
                update(ChatRequest)
                .where(
                    ChatRequest.id == request_id,
                    ChatRequest.status.in_(("accepted", "running")),
                )
                .values(
                    status=result["status"],
                    result_type=result["result_type"],
                    finished_at=now,
                    execution_lease_until=None,
                )
            )

            # ---- 助手消息（首版每轮一条；受限时 text=None，不用空串冒充内容）----
            assistant = (
                await session.execute(
                    select(ChatMessage).where(
                        ChatMessage.request_id == request_id, ChatMessage.role == "assistant"
                    )
                )
            ).scalar_one_or_none()
            if assistant is None:
                assistant = ChatMessage(
                    session_id=int(request_row.session_id),
                    request_id=request_id,
                    role="assistant",
                    text=result.get("answer"),
                    restricted=bool(result.get("restricted")),
                )
                session.add(assistant)
                await session.flush()
            else:
                assistant.text = result.get("answer")
                assistant.restricted = bool(result.get("restricted"))

            # ---- 最终来源（引用落在具体版本上）----
            await session.execute(
                delete(MessageSource).where(MessageSource.message_id == int(assistant.id))
            )
            for no, source in enumerate(result.get("sources") or [], start=1):
                session.add(
                    MessageSource(
                        message_id=int(assistant.id),
                        no=no,
                        unit_id=int(source["unit_id"]),
                        version=int(source["version"]),
                        chunk_id=source.get("chunk_id"),
                    )
                )

            # ---- 审计：三份快照 + 耗时 + 用量状态（未知不伪 0）----
            await session.execute(
                update(QaAudit)
                .where(QaAudit.request_id == request_id)
                .values(
                    status=result["status"],
                    recall_snapshot={"unit_ids": result.get("recall_ids") or []},
                    allowed_snapshot={"unit_ids": result.get("allowed_ids") or []},
                    denied_snapshot={"unit_ids": result.get("denied_ids") or []},
                    duration_ms=result.get("duration_ms"),
                    first_token_ms=result.get("first_token_ms"),
                    usage_status=str(usage.get("status") or "unknown"),
                )
            )

            # ---- 生成调用用量（call_id 唯一 → 重试不重复计入）----
            session.add(
                ModelCallUsage(
                    request_id=request_id,
                    call_id=str(uuid4()),
                    kind="generation",
                    model_version=get_settings().llm_model,
                    input_tokens=usage.get("prompt_tokens"),
                    output_tokens=usage.get("completion_tokens"),
                    status=str(usage.get("status") or "unknown"),
                )
            )

            # ---- 缺口 outbox（F-08.01 第 4 步）：仅正常 no_evidence/low_confidence；
            # 服务故障不进缺口（否则运维事故被当知识空白补档）。幂等键 = request_id。----
            if result.get("result_type") in ("no_evidence", "low_confidence"):
                from app.services.gap_svc import enqueue_gap_outbox

                dept_id = (
                    await session.execute(
                        select(User.dept_id).where(User.id == int(request_row.user_id))
                    )
                ).scalar_one_or_none()
                await enqueue_gap_outbox(
                    session,
                    request_id=request_id,
                    question=request_row.question,
                    dept_id=int(dept_id) if dept_id is not None else None,
                )

            # ---- 最终事件：与终态同事务（提交后由 SSE 轮询发出）----
            if result["status"] == "failed":
                await chat_store.append_event(
                    session, request_id, "error",
                    {"code": result.get("error_code") or "INTERNAL_ERROR",
                     "message": "本次回答未能完成，请稍后重试", "status": "failed"},
                )
            else:
                await chat_store.append_event(
                    session, request_id, "done",
                    {
                        "status": result["status"],
                        "result_type": result["result_type"],
                        "usage": {
                            "prompt_tokens": usage.get("prompt_tokens"),
                            "completion_tokens": usage.get("completion_tokens"),
                            "status": usage.get("status"),
                        },
                        "duration_ms": result.get("duration_ms"),
                        "first_token_ms": result.get("first_token_ms"),
                    },
                )
            _log(
                session, None, "chat.finalize_request", "chat_request", request_id,
                None,
                {"status": result["status"], "result_type": result["result_type"]},
            )
    return {"request_id": request_id, "status": result["status"]}

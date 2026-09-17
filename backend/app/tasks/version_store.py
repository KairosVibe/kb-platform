"""版本持久化与条件激活（H16/H17，FUNCTION-MAP §4）。

★ 事务边界为什么在这里而不在调用方：`run_pipeline` 里穿插着**外部副作用**
  （向量化调用、Milvus 写入）。如果把整条流水线包进一个大事务，H15 的续租
  写入要等事务提交才对外可见——租约就失去"防止双 worker"的作用。因此本模块
  每个函数**自开短事务**（UnitOfWork），外部调用发生在事务之外，符合
  FUNCTION-MAP §0 第 5 条"外部副作用持久任务化"。

两处"稳定"语义：
- **稳定 chunk_id**：同版本重跑时复用既有 `(unit_id,version,seq)` 行（条件更新文本），
  不删旧插新——历史引用 `message_source.chunk_id` 不能因为重试而悬空。
- **向量侧幂等**：Milvus 以 `(unit_id,version)` 先删后插（见 vector_store 模块注释），
  重试重跑不会产生重复向量。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import delete, select, update

from app.core.errors import TaskError
from app.db.base import utcnow
from app.db.repository import UnitOfWork
from app.models import Chunk, IndexTask, KnowledgeUnit, MessageSource
from app.providers import vector_store
from app.tasks.lease import TaskLease


async def upsert_version(
    session, *, unit_id: int, version: int, chunks: list[dict[str, Any]],
    lease: TaskLease, config: Any,
) -> tuple[int, int, bool]:
    """H16：MySQL 切片行（复用稳定 id）+ Milvus 向量写入 + 数量核对。

    返回 `(expected_count, written_count, verified)`。`verified=False` 视为瞬时失败
    （写入是先删后插，重试天然幂等）。
    """
    expected = len(chunks)
    if expected == 0:
        raise TaskError("PARSE_EMPTY", "没有可索引切片", transient=False)

    async with UnitOfWork(session).transaction():
        existing = {
            int(row.seq): row
            for row in (
                await session.execute(
                    select(Chunk).where(
                        Chunk.unit_id == unit_id, Chunk.version == version
                    )
                )
            ).scalars()
        }
        for chunk in chunks:
            row = existing.get(int(chunk["seq"]))
            if row is None:
                session.add(
                    Chunk(
                        unit_id=unit_id,
                        version=version,
                        seq=int(chunk["seq"]),
                        text=chunk["text"],
                        token_count=int(chunk["token_count"]),
                        location=chunk["location"],
                    )
                )
            else:
                # 文本没变就什么都不写（避免无意义的 updated_at 抖动）；变了则
                # 原地更新并**保留主键**——这就是"稳定 chunk_id"的落点。
                if row.text != chunk["text"]:
                    row.text = chunk["text"]
                    row.token_count = int(chunk["token_count"])
                    row.location = chunk["location"]

        # 清理孤儿行：MySQL 中该版本"多出来"的切片（seq 不在本次结果集里）必须删除，
        # 否则 read_chunks 会展示没有向量、也不属于本次解析结果的幽灵切片。
        # 场景：切片编辑版本先由服务层预建行，流水线重新解析后边界略有移动。
        # ★ 被 message_source 引用的切片除外（FK 为 RESTRICT，历史引用不能悬空）。
        kept_seqs = {int(chunk["seq"]) for chunk in chunks}
        referenced = (
            select(MessageSource.chunk_id).where(MessageSource.chunk_id.is_not(None))
        )
        await session.execute(
            delete(Chunk).where(
                Chunk.unit_id == unit_id,
                Chunk.version == version,
                Chunk.seq.not_in(kept_seqs),
                Chunk.id.not_in(referenced),
            )
        )

    written = await vector_store.upsert_version_chunks(
        config,
        rows=[
            {
                "unit_id": unit_id,
                "version": version,
                "seq": int(chunk["seq"]),
                "vector": chunk["vector"],
            }
            for chunk in chunks
        ],
    )
    if written != expected:
        return expected, written, False

    # 写入后的**真实核对**：以 Milvus 侧计数为准，不信 insert 响应的回执；
    # 带期望值轮询，区分"最终一致性尚未可见"与"真的丢了"（见 vector_store 注释）。
    counted = vector_store.count_version_chunks(
        config, unit_id=unit_id, version=version, expected=expected
    )
    return expected, written, counted == expected


async def activate_version(
    session, *, unit_id: int, version: int, lease: TaskLease
) -> tuple[bool, str]:
    """H17：短事务验证"当前版本、未删、租约匹配"，然后激活并完成任务。

    返回 `(activated, reason)`；`reason` 仅用于内部收敛方向（superseded/lease_lost），
    **不是**下发客户端的错误码。
    """
    async with UnitOfWork(session).transaction():
        unit_result = await session.execute(
            update(KnowledgeUnit)
            .where(
                KnowledgeUnit.id == unit_id,
                KnowledgeUnit.is_deleted.is_(False),
                KnowledgeUnit.content_version == version,
            )
            .values(indexed_version=version, index_status="indexed")
        )
        if unit_result.rowcount != 1:
            return False, "superseded"

        task_result = await session.execute(
            update(IndexTask)
            .where(
                IndexTask.id == lease.task_id,
                IndexTask.lease_token == lease.lease_token,
                IndexTask.status == "running",
                IndexTask.target_version == version,
            )
            .values(
                status="succeeded",
                stage=None,
                error_code=None,
                lease_token=None,
                lease_until=None,
                worker_id=None,
                next_retry_at=None,
            )
        )
        if task_result.rowcount != 1:
            return False, "lease_lost"
    # 倒排增量同步：全文检索组件属 M05（H08 检索链路），当前不存在，无动作可做；
    # 检索始终以 MySQL 复核版本（H04），不会因缺这一步而读到未激活数据。
    return True, "activated"

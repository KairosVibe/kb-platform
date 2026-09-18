"""H18 清理执行器（FUNCTION-MAP §4 `tasks.cleanup_deleted`）。

契约：`cleanup_deleted(unit_id, deletion_id) -> removed:int, status:str, error_code:str|null`；
逻辑：①确认墓碑 ②清理版本向量原文 ③保存完成记录 ④失败重试；
边界：**共享文件引用未归零不删除**；不复活单元。

★ "清理只删派生物，不复活单元"（knowledge_svc.delete_unit 注释）的落地口径：
  删 **Milvus 向量** 与 **受控存储文件**；MySQL 的 unit/version/chunk 行**保留**
  在墓碑态——chunk 行被 `message_source`（RESTRICT）引用，且保留行让历史引用的
  元数据（unit/version/chunk）永远可解析，与"已发送内容无法撤回"一致。
  unit 行物理删除（若产品将来要求）属 H18 之外的独立决策，不在本执行器内。

★ 租约与 IndexTask 同一哲学（lease.py 模块注释）：领取 = 单条条件 UPDATE，
  attempts 在领取时递增（执行中被杀也要计入尝试次数）。CleanupTask 无
  next_retry_at 列，瞬时失败回 `queued` 由下一次调度重试；attempts 耗尽 → `failed`。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import TaskError
from app.db.repository import UnitOfWork
from app.models import CleanupTask, KnowledgeUnit, KnowledgeVersion
from app.providers import vector_store
from app.services.ingest_svc import _log

#: 清理租约时长：单任务清理（向量删除 + 少量文件 unlink）耗时远小于索引任务。
CLEANUP_LEASE_TTL = timedelta(minutes=5)

#: 临时失败的最大尝试次数（与 IndexTask 的 F-03.06 边界一致）。
MAX_ATTEMPTS = 5


@dataclass(frozen=True, slots=True)
class CleanupLease:
    task_id: int
    unit_id: int
    deletion_id: UUID
    lease_token: UUID
    lease_until: datetime
    worker_id: str


async def claim_cleanup_task(
    session: AsyncSession, *, worker_id: str, now: datetime, exclude_ids: set[int] | None = None
) -> CleanupLease | None:
    """领取最老的一条 `queued` 清理任务。无任务或被抢 → None。

    `exclude_ids`：本轮调度已处理过的任务——瞬时失败回 `queued` 的任务
    **不能在同一次调度里被立即重领**（那会把"退避重试"变成"同步连打
    attempts 次然后 failed"，实测踩过）。

    两步但并发安全：先 SELECT 候选 id，再**单条条件 UPDATE** 占租约——
    并发下只有 rowcount==1 的一方真正拿到（先查再改的错误不在"查"，
    而在"改的时候不带条件"）。
    """
    excluded = exclude_ids or set()
    async with UnitOfWork(session).transaction():
        stmt = (
            select(CleanupTask.id)
            .where(CleanupTask.status == "queued")
            .order_by(CleanupTask.id.asc())
            .limit(1)
        )
        if excluded:
            stmt = stmt.where(CleanupTask.id.not_in(excluded))
        row = (await session.execute(stmt)).scalar_one_or_none()
        if row is None:
            return None
        task_id = int(row)
        token = uuid4()
        until = now + CLEANUP_LEASE_TTL
        result = await session.execute(
            update(CleanupTask)
            .where(
                CleanupTask.id == task_id,
                CleanupTask.status == "queued",
            )
            .values(
                status="running",
                lease_token=token,
                lease_until=until,
                attempts=CleanupTask.attempts + 1,
            )
        )
        if result.rowcount != 1:
            return None
        task = await session.get(CleanupTask, task_id)
        return CleanupLease(
            task_id=task_id,
            unit_id=int(task.unit_id),
            deletion_id=task.deletion_id,
            lease_token=token,
            lease_until=until,
            worker_id=worker_id,
        )


async def _finish(
    session: AsyncSession, *, lease: CleanupLease, status: str
) -> None:
    """条件于租约收敛终态：不持租约者无权写状态（与 F-03.04 同一纪律）。"""
    await session.execute(
        update(CleanupTask)
        .where(
            CleanupTask.id == lease.task_id,
            CleanupTask.lease_token == lease.lease_token,
            CleanupTask.status == "running",
        )
        .values(status=status, lease_token=None, lease_until=None)
    )


async def cleanup_deleted(
    session: AsyncSession, *, task_id: int, lease_token: UUID
) -> dict[str, object]:
    """H18：执行一次清理。返回 `{removed:int, status:str, error_code:str|null}`。

    异常分类与 F-03.04 一致：向量库不可用/超时 = 瞬时（回 `queued` 重试），
    attempts 耗尽或墓碑缺失 = 永久（`failed`）。
    """
    task = await session.get(CleanupTask, task_id)
    if task is None or task.lease_token != lease_token or task.status != "running":
        raise TaskError("LEASE_INVALID", "清理租约不匹配或已失效", transient=True)
    lease = CleanupLease(
        task_id=task_id,
        unit_id=int(task.unit_id),
        deletion_id=task.deletion_id,
        lease_token=lease_token,
        lease_until=task.lease_until,
        worker_id="cleanup",
    )

    removed = 0
    try:
        # ---- ①确认墓碑：未打墓碑的单元绝不清算（"不复活"的另一面是"不误删"）----
        unit = await session.get(KnowledgeUnit, lease.unit_id)
        if unit is None:
            raise TaskError("CLEAN_UNIT_MISSING", "知识单元不存在", transient=False)
        if not unit.is_deleted:
            raise TaskError("CLEAN_NO_TOMBSTONE", "单元未处于已删除状态", transient=False)

        # ---- ②清理版本向量 ----
        settings = None
        from app.core.config import get_settings

        settings = get_settings()
        removed += vector_store.delete_unit_vectors(settings, unit_id=lease.unit_id)
        left = vector_store.count_unit_vectors(settings, unit_id=lease.unit_id, expected=0)
        if left != 0:
            raise TaskError(
                "VECTOR_DELETE_MISMATCH",
                f"向量删除核对失败：残留 {left}",
                transient=True,
            )

        # ---- ③清理受控存储文件（共享引用未归零不删除）----
        file_keys = (
            (
                await session.execute(
                    select(KnowledgeVersion.file_key).where(
                        KnowledgeVersion.unit_id == lease.unit_id
                    )
                )
            )
            .scalars()
            .all()
        )
        removed_files = 0
        for file_key in file_keys:
            shared = (
                await session.execute(
                    select(KnowledgeVersion.id).where(
                        KnowledgeVersion.file_key == file_key,
                        KnowledgeVersion.unit_id != lease.unit_id,
                    )
                )
            ).scalars().first()
            if shared is not None:
                continue  # 其他单元仍在引用同一文件，跳过（契约边界）
            path = Path(settings.upload_dir) / file_key
            if path.is_file():
                path.unlink()
                removed_files += 1
        removed += removed_files
    except TaskError as exc:
        # 瞬时 → 回 queued（下次调度重试，attempts 已在领取时计入）；
        # 永久或次数耗尽 → failed。
        final = "queued" if exc.transient and task.attempts < MAX_ATTEMPTS else "failed"
        async with UnitOfWork(session).transaction():
            await _finish(session, lease=lease, status=final)
        _log(
            session, None, f"cleanup.{final}", "cleanup_task", task_id,
            None, {"error_code": exc.code, "transient": exc.transient},
        )
        return {"removed": removed, "status": final, "error_code": exc.code}

    # ---- ④保存完成记录（条件于租约）----
    async with UnitOfWork(session).transaction():
        await _finish(session, lease=lease, status="done")
    _log(
        session, None, "cleanup.done", "cleanup_task", task_id,
        None,
        {
            "deletion_id": str(lease.deletion_id),
            "vectors_and_files_removed": removed,
        },
    )
    return {"removed": removed, "status": "done", "error_code": None}


async def run_cleanup_due(
    session: AsyncSession, *, worker_id: str, now: datetime, limit: int = 10
) -> list[dict[str, object]]:
    """调度入口（F-03.06 调度循环/脚本调用）：领取并执行至多 `limit` 条清理任务。

    本轮已处理过（无论成败）的任务不重复领取：瞬时失败回 `queued` 是
    "**下次**调度再试"的语义，不是同步重试。
    """
    results: list[dict[str, object]] = []
    seen: set[int] = set()
    for _ in range(limit):
        lease = await claim_cleanup_task(
            session, worker_id=worker_id, now=now, exclude_ids=seen
        )
        if lease is None:
            break
        seen.add(lease.task_id)
        results.append(
            await cleanup_deleted(session, task_id=lease.task_id, lease_token=lease.lease_token)
        )
    return results

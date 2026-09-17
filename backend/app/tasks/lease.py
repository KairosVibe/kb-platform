"""任务租约原语（H14 / H15，FUNCTION-MAP §4）。

为什么租约要落在数据库而不是进程内存：`recover_tasks`（F-03.06）要求"重启恢复且同任务
只有一个有效租约"（AC-03.06-01）。进程内状态在重启后消失，"恢复"就无从谈起；
持久行 + 条件更新让"只有一个成功"由数据库行级语义保证，而不是靠调度器自觉。

两条实现的共同点：**都是单条条件 UPDATE**（`Repository.update_if` 同一思路）——
"领取"与"续租"的并发正确性完全取决于"条件不匹配就不覆盖"，任何"先查再改"的写法
在并发下都会让两个 worker 同时认为自己拿到了租约。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import IndexTask

#: 租约时长。★ 这是 D1 的**初始值**（PRD 未给出数值），选取原则：
#: 明显大于单任务最长正常耗时，又小于"worker 崩溃后运维可接受的恢复延迟"。
#: H15 续租让长任务不必一次租到底；调整它不影响正确性，只影响崩溃恢复的及时性。
LEASE_TTL = timedelta(minutes=10)

#: 临时失败的最大尝试次数（F-03.06 边界："临时失败最多5次"）。
MAX_ATTEMPTS = 5


@dataclass(frozen=True, slots=True)
class TaskLease:
    """任务租约（FUNCTION-MAP §1：task_id/lease_token/lease_until/worker_id/target_version）。"""

    task_id: int
    lease_token: UUID
    lease_until: datetime
    worker_id: str
    target_version: int


async def claim_task(
    session: AsyncSession, *, task_id: int, worker_id: str, now: datetime
) -> TaskLease | None:
    """H14：条件领取 `queued` 或已到期的 `retry_wait` 任务。失败（被别人抢先）返回 None。

    ★ `attempts` 在**领取时**递增而不是失败时：这样"临时失败最多 5 次"统计的是
      真实执行次数；若在失败时递增，进程在执行中途被杀的任务永远不会计入次数，
      会无限重试。
    """
    token = uuid4()
    result = await session.execute(
        update(IndexTask)
        .where(
            IndexTask.id == task_id,
            IndexTask.status.in_(("queued", "retry_wait")),
            # retry_wait 必须已到期才可领取；queued 无此条件。
            (IndexTask.next_retry_at.is_(None)) | (IndexTask.next_retry_at <= now),
        )
        .values(
            status="running",
            lease_token=token,
            lease_until=now + LEASE_TTL,
            worker_id=worker_id,
            attempts=IndexTask.attempts + 1,
        )
    )
    if result.rowcount != 1:
        return None

    row = (
        (await session.execute(_select_task(task_id).with_for_update())).scalars().first()
    )
    if row is None:  # 理论不可达（刚更新成功）；防御式处理，避免构造出假租约
        return None
    return TaskLease(
        task_id=int(row.id),
        lease_token=row.lease_token,
        lease_until=row.lease_until,
        worker_id=worker_id,
        target_version=int(row.target_version),
    )


async def renew_lease(
    session: AsyncSession, *, task_id: int, lease_token: UUID, now: datetime
) -> bool:
    """H15：条件匹配 `running` 与 token，延长到期时间。不匹配返回 False。

    ★ 返回 False 的语义是"租约已不属于你"（被 recover 回收或任务已易主）。
      调用方（F-03.04 run_pipeline）**必须**据此停止写入并放弃提交——
      继续写一个已失去租约的版本，会产生两个 worker 同时写同一版本的竞争。
    """
    result = await session.execute(
        update(IndexTask)
        .where(
            IndexTask.id == task_id,
            IndexTask.lease_token == lease_token,
            IndexTask.status == "running",
        )
        .values(lease_until=now + LEASE_TTL)
    )
    return result.rowcount == 1


def _select_task(task_id: int):
    from sqlalchemy import select

    return select(IndexTask).where(IndexTask.id == task_id)

"""后台任务执行支撑（任务租约、恢复），对应 FUNCTION-MAP §4 的 `tasks.*` 基础函数。

契约层（`app/tasks/` 在门禁的 CONTRACT_LAYERS 之列）：公开符号必须已登记 FUNCTION-MAP。
"""

from app.tasks.lease import LEASE_TTL, MAX_ATTEMPTS, TaskLease, claim_task, renew_lease

__all__ = [
    "LEASE_TTL",
    "MAX_ATTEMPTS",
    "TaskLease",
    "claim_task",
    "renew_lease",
]

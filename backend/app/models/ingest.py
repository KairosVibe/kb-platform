"""索引任务与清理任务实体。

对应 DATA-CONTRACTS.md §2 的 index_task / cleanup_task、FUNCTION-MAP.md §2.1 第 3 条
与 PRD §1.2 第 2 条 的状态机。

★ 状态机必须按 PRD §1.2 第 2 条 原样落地，两处最容易被写错：

1. **`status` 与 `stage` 是两件事**：`status` 是 queued→running→succeeded /
   running→retry_wait→running / failed / superseded；`stage` 只有
   parsing|embedding|indexing，**只表示阶段，不混进 status**。
2. **`superseded` 不是 `failed`**：旧任务被新版本取代属正常收敛，把它记成失败会污染
   失败率指标，也会诱导人去"重试"一个本来就不该完成的任务。

删除走 `cleanup_task` + 墓碑，而不是立即物理删除（DATA-CONTRACTS §2："删除用
cleanup_task与tombstone，不能立即删除恢复所需登记"）。
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, PKMixin, RevisionMixin, TimestampMixin
from app.db.types import LEN_ERROR_CODE, LEN_STATUS, LEN_TITLE, UTCDateTime

#: PRD §1.2 第 2 条 的任务状态集合。
TASK_STATUS_VALUES: tuple[str, ...] = (
    "queued",
    "running",
    "retry_wait",
    "succeeded",
    "failed",
    "superseded",
)
#: 阶段（与 status 正交）。NULL 表示尚未进入任何阶段。
TASK_STAGE_VALUES: tuple[str, ...] = ("parsing", "embedding", "indexing")


class IndexTask(PKMixin, TimestampMixin, RevisionMixin, Base):
    """索引任务。`(unit_id, target_version)` 唯一 → 同一版本不会有两个任务并行（§2.1.3）。"""

    __tablename__ = "index_task"

    unit_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("knowledge_unit.id", ondelete="CASCADE"), nullable=False
    )
    target_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(
        String(LEN_STATUS), nullable=False, default="queued", server_default="queued"
    )
    #: 阶段；与 status 正交，**不参与判定任务成败**。
    stage: Mapped[str | None] = mapped_column(String(LEN_STATUS), nullable=True)
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    next_retry_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    #: 租约：`lease_token` 同时写在任务上（§2.1.3）与 `TaskLease` 值对象里（FUNCTION-MAP §1）。
    lease_until: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    lease_token: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: **任务层**错误码（解析空文/加密/损坏、向量化失败…）。它随 payload 返回，
    #: 此时 HTTP 仍是成功——任务是"被接受后失败"，不是"请求失败"（FUNCTION-MAP §2）。
    error_code: Mapped[str | None] = mapped_column(String(LEN_ERROR_CODE), nullable=True)

    __table_args__ = (
        # 不传 name：由约定生成 `uq_index_task_unit_id_target_version`（见 knowledge.py 同类注释）。
        UniqueConstraint("unit_id", "target_version"),
        # 领取扫描路径（§2.1.9）。
        Index("ix_index_task_status_next_retry", "status", "next_retry_at"),
        CheckConstraint(
            "status IN ('queued','running','retry_wait','succeeded','failed','superseded')",
            name="status_values",
        ),
        CheckConstraint(
            "stage IS NULL OR stage IN ('parsing','embedding','indexing')",
            name="stage_values",
        ),
    )


class CleanupTask(PKMixin, TimestampMixin, Base):
    """删除清理任务。与墓碑同事务创建（DATA-CONTRACTS §3 第 1 条/§2）。

    `deletion_id` 唯一 → 同一次删除请求重复提交不会产生两个清理任务。
    """

    __tablename__ = "cleanup_task"

    unit_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("knowledge_unit.id", ondelete="CASCADE"), nullable=False
    )
    #: 墓碑标识（删除操作的幂等键）。
    deletion_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(
        String(LEN_STATUS), nullable=False, default="queued", server_default="queued"
    )
    lease_until: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    lease_token: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    error_code: Mapped[str | None] = mapped_column(String(LEN_ERROR_CODE), nullable=True)

    __table_args__ = (Index("ix_cleanup_task_status", "status"),)


class UploadBatch(PKMixin, TimestampMixin, Base):
    """批量上传的幂等登记（DATA-CONTRACTS §2）。

    ★ `UNIQUE(user_id, client_batch_id)` 是"网络失败后重试复用同一键，
    不对已接受文件创建第二任务"（API-CONTRACTS §3）的落地点：唯一键在数据库层
    挡住重复受理，而不是靠"先查再插"（并发下会双双通过）。

    列严格取自 DATA-CONTRACTS §2 的 `upload_batch` 行（`id,user_id,client_batch_id,
    payload_hash,status`）+ §1 的公共时间列；该行未声明 `revision`，故不引入。
    """

    __tablename__ = "upload_batch"

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("user.id", ondelete="RESTRICT"), nullable=False
    )
    client_batch_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    #: 规范化字段 + 文件哈希共同构成的载荷指纹：**同键不同载荷不是重放**（API-CONTRACTS §1）。
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(LEN_STATUS), nullable=False, default="received", server_default="received"
    )

    __table_args__ = (
        # 不传 name，由 MetaData 命名约定生成（见 knowledge.py 同类注释）。
        UniqueConstraint("user_id", "client_batch_id"),
        Index("ix_upload_batch_user_status", "user_id", "status"),
    )


class UploadItem(PKMixin, TimestampMixin, Base):
    """单个文件的受理登记（DATA-CONTRACTS §2）。

    ★ **单文件上传也要登记**（`batch_id` 可空）：DATA-CONTRACTS §2 原文
      "单文件也登记，失败文件能对应原清单"。若不登记，F-03.02 的逐项结果
      （`unit_id|null`/`task_id|null`/`error_code|null`）就无处可存，
      批量失败项将无法与客户端清单对应。

    ★ `UNIQUE(user_id, client_file_id)`：单文件上传的 `client_upload_id` 即映射到
      本表 `client_file_id`（API-CONTRACTS §3），保证重试不产生第二个任务。

    列严格取自 DATA-CONTRACTS §2 的 `upload_item` 行 + §1 的公共时间列。
    """

    __tablename__ = "upload_item"

    batch_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("upload_batch.id", ondelete="CASCADE"), nullable=True
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("user.id", ondelete="RESTRICT"), nullable=False
    )
    client_file_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    #: 相对路径：**只用于展示与分组**；拒绝绝对路径、盘符、`..` 与 NUL（API-CONTRACTS §3）。
    relative_path: Mapped[str] = mapped_column(String(LEN_TITLE), nullable=False, default="")
    unit_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("knowledge_unit.id", ondelete="SET NULL"), nullable=True
    )
    task_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("index_task.id", ondelete="SET NULL"), nullable=True
    )
    #: **任务层**错误码（与 `index_task.error_code` 同一体系，不进 HTTP 错误码表）。
    error_code: Mapped[str | None] = mapped_column(String(LEN_ERROR_CODE), nullable=True)

    __table_args__ = (
        UniqueConstraint("user_id", "client_file_id"),
        Index("ix_upload_item_batch", "batch_id"),
    )

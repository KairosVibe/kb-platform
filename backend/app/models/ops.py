"""运维与一致性辅助实体：配置修订、操作日志、发件箱、幂等记录。

对应 DATA-CONTRACTS.md §2 的 config_revision / operation_log / outbox_event /
idempotency_record。

四张表都是**append-only**（只有 `created_at`，没有 `updated_at`/`revision`）：
它们记录"发生过什么"，而不是"现在是什么"。给审计与幂等记录加 `updated_at`
会让人误以为可以原地修正历史——这正是审计最不能有的性质。

三条硬约定：

1. `operation_log` 与业务变更**同事务**（DATA-CONTRACTS §3 第 1 条、FUNCTION-MAP §0 第 5 条
   "写入与 operation_log 短事务一致"）。它必须能回答"谁在什么时候把什么改成了什么"，
   所以 `before`/`after` 存的是**脱敏后**的快照，且**禁止记录凭据**（DATA-CONTRACTS §2）。
2. `outbox_event.event_key` 唯一：终态之后的通知（缺口登记、FAQ 缓存失效）靠它
   **恰好送达一次**。没有唯一键，重试就会重复登记缺口。
3. `idempotency_record` 用于"重放同一次用户操作"（补档转建、手动挖掘等），
   以避免"用户刷新页面导致多建一个任务"。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, ForeignKey, Index, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, PKMixin, TimestampMixin, utcnow
from app.db.types import (
    LEN_ACTION,
    LEN_CODE,
    LEN_IDEMPOTENCY_KEY,
    LEN_RESOURCE_TYPE,
    LEN_STATUS,
    UTCDateTime,
)


class ConfigRevision(PKMixin, Base):
    """配置修订（append-only）。

    ★ 这是**可热改业务参数的持久实体**（`patch`/`snapshot` 只允许放非密钥参数与
      `secret_ref`，DATA-CONTRACTS §2）。问答请求会绑定当时的 `config_revision`
      （`chat_request.config_revision`），因此"同一次问答用哪套阈值"可事后复算——
      否则阈值一改，历史评测结论就不可解释。

    勘误：`app/core/config.py` 旧文中该表被写作 `sys_config`，该名称在全部文档中
    均不存在，属文档编号漂移，已更正（见 FUNCTION-MAP §2.1 第 10 条）。
    """

    __tablename__ = "config_revision"

    patch: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    actor_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("user.id", ondelete="RESTRICT"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)


class OperationLog(PKMixin, Base):
    """操作日志（append-only，与业务变更同事务）。

    `actor_id` 可空：系统任务（清理、挖掘）没有人类操作者，用 NULL 表达"系统发起"
    比伪造一个 admin 账号更容易审计。
    """

    __tablename__ = "operation_log"

    actor_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("user.id", ondelete="RESTRICT"), nullable=True
    )
    action: Mapped[str] = mapped_column(String(LEN_ACTION), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(LEN_RESOURCE_TYPE), nullable=False)
    resource_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    #: 链路追踪标识；与 HTTP 响应包里的 `request_id`（UUID）同源，便于把一次请求的
    #: 全部读写串起来（FUNCTION-MAP §2）。
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    #: 脱敏前后快照；**禁止写入凭据**（DATA-CONTRACTS §2）。
    before: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(LEN_STATUS), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    __table_args__ = (
        # 审计页按资源倒查，以及按人倒查。
        Index("ix_operation_log_resource", "resource_type", "resource_id", "created_at"),
        Index("ix_operation_log_actor", "actor_id", "created_at"),
        Index("ix_operation_log_trace", "trace_id"),
    )


class OutboxEvent(PKMixin, TimestampMixin, Base):
    """发件箱：确保终态之后的通知不丢（DATA-CONTRACTS §2/§3.7）。

    ★ 只有"正常 no_evidence/low_confidence"才登记缺口通知（DATA-CONTRACTS §3 第 7 条）——
      服务故障不得进缺口，否则运维事故会被当成知识空白去补档。
    """

    __tablename__ = "outbox_event"

    event_key: Mapped[str] = mapped_column(
        String(LEN_IDEMPOTENCY_KEY), nullable=False, unique=True
    )
    kind: Mapped[str] = mapped_column(String(LEN_CODE), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(
        String(LEN_STATUS), nullable=False, default="pending", server_default="pending"
    )
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    next_retry_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    __table_args__ = (Index("ix_outbox_event_status_next_retry", "status", "next_retry_at"),)


class IdempotencyRecord(Base):
    """幂等记录（联合主键，append-only，无公共时间列，DATA-CONTRACTS §2）。

    `payload_hash` 参与判定：同一个 `client_key` 配不同请求体**不是**重放，
    而是客户端复用了键——两者必须区分，否则改一次参数就会被当成重放而静默丢弃。
    """

    __tablename__ = "idempotency_record"

    actor_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("user.id", ondelete="RESTRICT"), primary_key=True
    )
    operation: Mapped[str] = mapped_column(String(LEN_CODE), primary_key=True)
    client_key: Mapped[str] = mapped_column(String(LEN_IDEMPOTENCY_KEY), primary_key=True)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)

    # 联合主键本身即唯一约束：不再重复声明 UniqueConstraint（同一列集合上两个唯一索引
    # 是纯粹的写入开销与维护噪声，没有任何额外保证）。

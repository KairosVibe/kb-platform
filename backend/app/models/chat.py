"""会话、请求、消息、事件与审计实体（M05 / M08）。

对应 DATA-CONTRACTS.md §2 的 chat_session / chat_request / chat_message /
message_source / chat_event / qa_audit / model_call_usage，以及 PRD §1.2 第 3 条的状态机。

四个字段级决定必须按原文落地，写错会让交付证据失去意义：

1. **请求状态与业务结果不是同一字段**（PRD §1.2 第 3 条原文："请求状态与业务结果不是
   同一字段"）：`status` 是 accepted→running→completed/rejected/failed/cancelled；
   `result_type` 是 answered/faq_hit/access_restricted/no_evidence/low_confidence/
   service_error。把两者合并会让"服务失败"与"无证据"无法区分——前者该告警，后者该进
   缺口，混起来就会把运维事故当成知识空白去补档。
2. **`chat_session` 与 `auth_session` 是两个外键**（DATA-CONTRACTS §2）：
   `session_id` 指聊天会话（侧栏），`auth_session_id` 用于**注销时取消该会话的活动请求**。
   只留其一就会出现"退出登录后仍在生成"或"换设备登录后旧设备继续刷新"。
3. **`chat_event(request_id, seq)` 联合主键**，`seq` 由请求行锁或原子计数分配
   （DATA-CONTRACTS §3 第 5 条）：主键即去重键，重发同一 `seq` 被数据库挡下，
   而不是靠应用判断——这是"事件不重复"的唯一可靠保证。
4. **`message_source` 的复合外键指向版本**：引用必须落在**具体版本**上，否则
   "历史读取重新授权"无法判断读的是哪一版正文（DATA-CONTRACTS §2）。
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, PKMixin, RevisionMixin, TimestampMixin, utcnow
from app.db.types import (
    LEN_CODE,
    LEN_IDEMPOTENCY_KEY,
    LEN_MODEL_VERSION,
    LEN_STATUS,
    LEN_TITLE,
    UTCDateTime,
)

#: PRD §1.2 第 3 条：请求状态机。
REQUEST_STATUS_VALUES: tuple[str, ...] = (
    "accepted",
    "running",
    "completed",
    "rejected",
    "failed",
    "cancelled",
)

#: PRD §1.2 第 3 条：业务结果类型（与 status 正交）。
RESULT_TYPE_VALUES: tuple[str, ...] = (
    "answered",
    "faq_hit",
    "access_restricted",
    "no_evidence",
    "low_confidence",
    "service_error",
)

#: 首版每轮 user 与 assistant 各一条（DATA-CONTRACTS §2 `chat_message`）。
MESSAGE_ROLE_VALUES: tuple[str, ...] = ("user", "assistant")

#: FUNCTION-MAP §1 `SseEvent`：**heartbeat 不入库**（它无 seq，仅供保活）。
#: 若把 heartbeat 也持久化，事件表会被心跳淹没且"应用后游标"语义混乱。
SSE_EVENT_VALUES: tuple[str, ...] = ("meta", "delta", "citations", "denied", "done", "error")

#: DATA-CONTRACTS §2：kind 只有三类；后台挖掘的用量同样落表，不丢弃。
MODEL_CALL_KIND_VALUES: tuple[str, ...] = ("embedding", "rerank", "generation")

#: FUNCTION-MAP §1 `Usage.status`。
USAGE_STATUS_VALUES: tuple[str, ...] = ("known", "unknown", "not_applicable")


class ChatSession(PKMixin, TimestampMixin, RevisionMixin, Base):
    """聊天会话（侧栏列表项）。**不是** `auth_session`（DATA-CONTRACTS §2 明确）。"""

    __tablename__ = "chat_session"

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("user.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(LEN_TITLE), nullable=False)
    #: 会话软删除：用墓碑而不是物理删除，避免历史引用失去归属。
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (
        # 侧栏按"我的会话、最近更新优先"翻页（FUNCTION-MAP §2.1 第 9 条）。
        Index("ix_chat_session_user_updated", "user_id", "updated_at", "id"),
    )


class ChatRequest(PKMixin, TimestampMixin, Base):
    """一次问答请求（幂等键 + 执行租约 + 事件游标）。

    ★ 幂等键是 `(user_id, client_request_id)` 而非全局唯一：不同客户端的计数器互不
      相干，全局唯一会让第二个用户永久提交失败。
    ★ 不设 `revision`：它不被用户编辑，状态迁移由条件更新控制（DATA-CONTRACTS §2
      字段表也未列 revision）。
    """

    __tablename__ = "chat_request"

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("user.id", ondelete="CASCADE"), nullable=False
    )
    #: 登录会话：注销时据此取消该会话的活动请求（DATA-CONTRACTS §2）。
    auth_session_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("auth_session.id", ondelete="RESTRICT"), nullable=False
    )
    #: 聊天会话（侧栏归属）；与 auth_session_id 不可互相替代。
    session_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("chat_session.id", ondelete="CASCADE"), nullable=False
    )
    client_request_id: Mapped[str] = mapped_column(String(LEN_IDEMPOTENCY_KEY), nullable=False)
    #: 请求体指纹：同一幂等键配不同请求体**不是重放**，而是客户端复用了键，
    #: 两者必须能区分（否则改一次参数就被当成重放而静默丢弃）。
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(LEN_STATUS), nullable=False, default="accepted", server_default="accepted"
    )
    #: 业务结果；未终结前为 NULL（与 status 分离，见模块 docstring 第 1 条）。
    result_type: Mapped[str | None] = mapped_column(String(LEN_STATUS), nullable=True)
    #: 该请求使用的配置修订号：阈值改动后历史结论仍可复算。
    config_revision: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("config_revision.id", ondelete="RESTRICT"), nullable=False
    )
    #: 执行租约：防同一请求被两个 worker 同时生成。
    execution_lease_until: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    #: 已持久化事件的最大 seq（客户端恢复游标的依据）。
    last_seq: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    accepted_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("user_id", "client_request_id"),
        Index("ix_chat_request_user_accepted", "user_id", "accepted_at", "id"),
        CheckConstraint(
            "status IN ('accepted','running','completed','rejected','failed','cancelled')",
            name="status_values",
        ),
        CheckConstraint(
            "result_type IS NULL OR result_type IN "
            "('answered','faq_hit','access_restricted','no_evidence','low_confidence','service_error')",
            name="result_type_values",
        ),
    )


class ChatMessage(PKMixin, TimestampMixin, Base):
    """消息。`(request_id, role)` 唯一 → 首版每轮 user / assistant 各一条。"""

    __tablename__ = "chat_message"

    session_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("chat_session.id", ondelete="CASCADE"), nullable=False
    )
    request_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("chat_request.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    #: 可空：全受限或服务失败时没有正文，**不用空字符串冒充内容**。
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: 该条是否因权限受限而未给出正文（前端据此渲染固定提示，不自行编造）。
    restricted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (
        UniqueConstraint("request_id", "role"),
        CheckConstraint("role IN ('user','assistant')", name="role_values"),
        Index("ix_chat_message_session_id_id", "session_id", "id"),
    )


class MessageSource(Base):
    """消息引用（关联表：无公共时间列）。

    ★ 复合外键指向 `knowledge_version`：引用落在**具体版本**上，否则
      "历史读取重新授权"无法判断读的是哪一版正文（DATA-CONTRACTS §2）。
    """

    __tablename__ = "message_source"

    message_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("chat_message.id", ondelete="CASCADE"), primary_key=True
    )
    #: 引用序号（对应 SSE `citations` 事件里的 `no`，从 1 开始）。
    no: Mapped[int] = mapped_column(Integer, primary_key=True)
    unit_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    chunk_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("chunk.id", ondelete="RESTRICT"), nullable=True
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["unit_id", "version"],
            ["knowledge_version.unit_id", "knowledge_version.version"],
            ondelete="CASCADE",
        ),
        Index("ix_message_source_unit_version", "unit_id", "version"),
    )


class ChatEvent(Base):
    """SSE 事件（append-only；主键即去重键）。

    `seq` 由请求行锁或原子计数分配（DATA-CONTRACTS §3 第 5 条）：主键
    `(request_id, seq)` 让重复插入直接被数据库拒绝，而不是靠应用层判断。
    """

    __tablename__ = "chat_event"

    request_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("chat_request.id", ondelete="CASCADE"), primary_key=True
    )
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    event: Mapped[str] = mapped_column(String(16), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)

    __table_args__ = (
        CheckConstraint(
            "event IN ('meta','delta','citations','denied','done','error')", name="event_values"
        ),
        CheckConstraint("seq > 0", name="seq_positive"),
    )


class QaAudit(TimestampMixin, Base):
    """问答审计：**接受时建记录、终态更新**，不只记录成功（DATA-CONTRACTS §2）。

    ★ 主键就是 `request_id`（"request_id唯一且FK"），**不另设自增 id**：
      加一个自增主键会允许同一请求出现多行审计，"一次请求一条审计"就不再是
      数据库保证的性质。

    ★ 三份快照（召回/放行/拒绝）是故意冗余的：只有留下"当时召回了哪些、放行了哪些、
      拒了哪些"，越权排查与召回质量分析才可能复算。`denied_snapshot` **只进受控审计**，
      不得下发客户端（DESIGN_REVISION §2.2）。
    """

    __tablename__ = "qa_audit"

    request_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("chat_request.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("user.id", ondelete="RESTRICT"), nullable=False
    )
    asked_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    recall_snapshot: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    allowed_snapshot: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    denied_snapshot: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(LEN_STATUS), nullable=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    first_token_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: 用量缺失必须显式记 unknown，**不能默认 0**（PRD："未知 Token 不记零"）。
    usage_status: Mapped[str] = mapped_column(String(LEN_STATUS), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "usage_status IN ('known','unknown','not_applicable')", name="usage_status_values"
        ),
        Index("ix_qa_audit_asked_at", "asked_at"),
        Index("ix_qa_audit_user_asked", "user_id", "asked_at"),
    )


class ModelCallUsage(PKMixin, TimestampMixin, Base):
    """模型调用用量。`call_id` 唯一 → 重试不会重复计入口径。"""

    __tablename__ = "model_call_usage"

    #: 关联问答请求（可空：后台挖掘的调用没有 request_id）。
    request_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("chat_request.id", ondelete="SET NULL"), nullable=True
    )
    #: 关联挖掘运行（可空：问答期间的调用没有 run）。外键指向 faq.py 的 mining_run。
    mining_run_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("mining_run.id", ondelete="SET NULL"), nullable=True
    )
    call_id: Mapped[str] = mapped_column(String(LEN_CODE), nullable=False, unique=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    model_version: Mapped[str] = mapped_column(String(LEN_MODEL_VERSION), nullable=False)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: rerank 的计量单位是"条数"而非 token（FUNCTION-MAP §1 `Usage.rerank_units`）。
    units: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: 用量是否已知；与 qa_audit.usage_status 同义，便于按调用粒度核对。
    status: Mapped[str] = mapped_column(String(LEN_STATUS), nullable=False)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (
        CheckConstraint("kind IN ('embedding','rerank','generation')", name="kind_values"),
        CheckConstraint(
            "status IN ('known','unknown','not_applicable')", name="status_values"
        ),
        Index("ix_model_call_usage_kind_created", "kind", "created_at"),
    )

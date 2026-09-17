"""知识缺口闭环实体（M07）。

对应 DATA-CONTRACTS.md §2 的 knowledge_gap / gap_request / supplement_task，
以及 PRD §1.2 第 5 条的状态机（open→processing→closed）。

三条容易写错、且错了会造成"数据被污染"的约定：

1. **缺口状态不写进知识单元**（PRD §1.2 第 5 条与 BC-07.03 原文：
   "processing不写knowledge.index_status"）。补充任务是**独立实体**——
   把"缺口处理中"记成单元状态，会让一个还没上传的文档看起来像"索引失败"。
2. **`department_key` 用 0 表示"无部门"**（DATA-CONTRACTS §2："department_key=0表示无部门，
   真实部门ID>0"）。为什么不直接用 NULL：MySQL 的唯一索引对 NULL 不去重，
   同一个无部门指纹可以插入任意多行，缺口就会重复累积。
3. **`gap_request` 固定历史部门快照**（DATA-CONTRACTS §2："历史部门快照固定，
   不随调岗重写统计"）：用户调岗后再回放，仍应按**提问当时**的部门归集，
   否则历史统计会被追溯改写。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, PKMixin, RevisionMixin, TimestampMixin
from app.db.types import LEN_MODEL_VERSION, LEN_NAME, LEN_STATUS, UTCDateTime

#: PRD §1.2 第 5 条：open→processing→closed。
GAP_STATUS_VALUES: tuple[str, ...] = ("open", "processing", "closed")


class KnowledgeGap(PKMixin, TimestampMixin, RevisionMixin, Base):
    """知识缺口。`(department_key, fingerprint)` 唯一 → 同一部门同一问题只累积一行。"""

    __tablename__ = "knowledge_gap"

    #: 0 = 无部门；>0 = 真实部门 ID（DATA-CONTRACTS §2）。**不用 NULL**，见模块注释第 2 条。
    department_key: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    #: 问题指纹（规范化后的稳定散列）：保证"同义问法"能归集到同一缺口。
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(LEN_STATUS), nullable=False, default="open", server_default="open"
    )
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    #: 最高相似度与所用分数类型：**不同分数类型不可直接比较**（召回/重排分数含义不同），
    #: 所以把 `score_type` 与 `model_version` 一起存下来，避免把两套分数混成一个指标。
    max_similarity: Mapped[float | None] = mapped_column(Float, nullable=True)
    score_type: Mapped[str] = mapped_column(String(LEN_NAME), nullable=False)
    model_version: Mapped[str] = mapped_column(String(LEN_MODEL_VERSION), nullable=False)
    suggested_category: Mapped[str | None] = mapped_column(String(LEN_NAME), nullable=True)

    __table_args__ = (
        UniqueConstraint("department_key", "fingerprint"),
        CheckConstraint(
            "status IN ('open','processing','closed')", name="status_values"
        ),
        # 看板按状态与最近出现时间排序（FUNCTION-MAP §2.1 第 9 条）。
        Index("ix_knowledge_gap_status_last_seen", "status", "last_seen_at"),
    )


class GapRequest(Base):
    """缺口 ← 请求（关联表）：`(gap_id, request_id)` 联合主键，用于**防重计**。

    同一请求重复登记不会虚增缺口频次（主键挡住），也不必依赖应用去重。
    """

    __tablename__ = "gap_request"

    gap_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("knowledge_gap.id", ondelete="CASCADE"), primary_key=True
    )
    request_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("chat_request.id", ondelete="CASCADE"), primary_key=True
    )

    __table_args__ = (Index("ix_gap_request_request", "request_id"),)


class SupplementTask(PKMixin, TimestampMixin, RevisionMixin, Base):
    """补档任务。`gap_id` 唯一 → 首版每个缺口只有一个补充任务（DATA-CONTRACTS §2）。"""

    __tablename__ = "supplement_task"

    gap_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("knowledge_gap.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    #: 任务状态。契约未固定取值集合（DATA-CONTRACTS §2 只列字段），故不加 CHECK；
    #: 新增枚举必须先在 DATA-CONTRACTS 固定取值，再补一次迁移。
    status: Mapped[str] = mapped_column(String(LEN_STATUS), nullable=False)
    #: 绑定上传的资料；绑定后回放验证（F-07.04）。
    unit_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("knowledge_unit.id", ondelete="SET NULL"), nullable=True
    )
    target_version: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    __table_args__ = (Index("ix_supplement_task_status_created", "status", "created_at"),)

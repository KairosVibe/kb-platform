"""FAQ、来源、挖掘运行/消费、聚类与草稿任务实体（M06）。

对应 DATA-CONTRACTS.md §2 的 faq / faq_source / mining_run / mining_consumption /
question_cluster / cluster_member / faq_draft_job，以及 PRD §1.2 第 4 条的 FAQ 状态机。

三条不能妥协的约定：

1. **FAQ 不建独立 ACL**（DATA-CONTRACTS §2："不建独立扩权ACL；published仍逐次检查来源"）。
   如果给 FAQ 加一份自己的可见性配置，就等于在四维权限之外开了第二个事实源，
   来源撤权后 FAQ 仍可能直出。可见性一律由 `faq_source` → 单元四维授权推导。
2. **`faq_source` 禁止混版本**（DATA-CONTRACTS §2："同一FAQ同一单元只引用一个版本，
   禁止混版本发布"）：主键是 `(faq_id, unit_id)`，版本是属性而不是键的一部分——
   这样"同一单元出现两个版本"在数据库层就不可能，而不是靠流程约束。
3. **模型调用不放长事务**（DATA-CONTRACTS §2 `faq_draft_job`）：起草结果单独持久化，
   重复执行可复用；把远程调用塞进事务会持有行锁直到超时。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, PKMixin, RevisionMixin, TimestampMixin
from app.db.types import (
    LEN_CODE,
    LEN_ERROR_CODE,
    LEN_MODEL_VERSION,
    LEN_NAME,
    LEN_STATUS,
    LEN_TITLE,
    UTCDateTime,
)

#: PRD §1.2 第 4 条：candidate→published|rejected；published→offline；来源失效→stale；
#: offline/stale 重新审核前转 candidate。开关 `cache_enabled` **不改变**审核状态。
FAQ_STATUS_VALUES: tuple[str, ...] = (
    "candidate",
    "published",
    "rejected",
    "offline",
    "stale",
)

#: 挖掘运行状态。★ 契约**未给出**取值集合（DATA-CONTRACTS §2 只列字段），
#: 因此这里刻意不加 CHECK：凭空造一个枚举并写进 DDL，会让后续按需求修正时
#: 必须再做一次迁移。取值集合在 FUNCTION-MAP M06 明确后再补约束。
MINING_RUN_STATUS_HINT = "契约未固定取值集合，暂不加 CHECK（见模块注释）"


class Faq(PKMixin, TimestampMixin, RevisionMixin, Base):
    """FAQ 条目。**无独立 ACL**（模块 docstring 第 1 条）。"""

    __tablename__ = "faq"

    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(LEN_STATUS), nullable=False, default="candidate", server_default="candidate"
    )
    #: 缓存开关：只影响"是否走缓存直出"，**不影响审核状态**（PRD §1.2 第 4 条）。
    cache_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: 相似问题频次（由聚类去重成员数计算，DATA-CONTRACTS §2 `cluster_member`）。
    frequency: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    cluster_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("question_cluster.id", ondelete="SET NULL"), nullable=True
    )
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('candidate','published','rejected','offline','stale')",
            name="status_values",
        ),
        Index("ix_faq_status_updated", "status", "updated_at"),
        Index("ix_faq_cluster", "cluster_id"),
    )


class FaqSource(Base):
    """FAQ → 知识版本（关联表：无公共时间列）。

    ★ 主键为 `(faq_id, unit_id)`，版本是普通列而非键的一部分：这样
      "同一 FAQ 对同一单元引用两个版本"在数据库层即不可能（模块 docstring 第 2 条）。
    ★ 发布后仍**逐次检查来源可读性**（DATA-CONTRACTS §2），因此这里存的
      `version` 是"发布时锁定的版本"，不是"永远可读的版本"。
    """

    __tablename__ = "faq_source"

    faq_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("faq.id", ondelete="CASCADE"), primary_key=True
    )
    unit_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(
            ["unit_id", "version"],
            ["knowledge_version.unit_id", "knowledge_version.version"],
            ondelete="CASCADE",
        ),
        # 反向查询：正文变更/删除时反查受影响的 FAQ（F-06.06 `invalidate_by_unit`）。
        Index("ix_faq_source_unit", "unit_id"),
    )


class MiningRun(PKMixin, TimestampMixin, Base):
    """一次挖掘运行。运行登记与领取分离（DATA-CONTRACTS §2）。

    ★ `trigger` 列名是 MySQL 保留字，DDL 中会被自动加反引号；
      直接手写 SQL 时必须写 `` `trigger` ``（与 `role` 表同理，见 WORKLOG 问题 #41）。
    """

    __tablename__ = "mining_run"

    #: 触发方式（manual/scheduled）。契约未固定取值集合，故不加 CHECK。
    trigger: Mapped[str] = mapped_column(String(LEN_CODE), nullable=False)
    #: 流水线版本：与 `question_cluster.version` 配对，避免不同算法版本的结果互相污染。
    pipeline_version: Mapped[str] = mapped_column(String(LEN_MODEL_VERSION), nullable=False)
    status: Mapped[str] = mapped_column(String(LEN_STATUS), nullable=False)
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    consumed: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    candidates: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    error_code: Mapped[str | None] = mapped_column(String(LEN_ERROR_CODE), nullable=True)

    __table_args__ = (Index("ix_mining_run_status_created", "status", "created_at"),)


class MiningConsumption(Base):
    """日志消费标记（关联表）：`(log_id, pipeline_version)` 联合主键。

    ★ 主键含 `pipeline_version`：换算法版本后同一批日志**需要重新消费**，
      若只用 `log_id` 做键，升级算法后就再也消费不到历史日志了。
    ★ `log_id` 指向 `qa_audit.request_id`（即该次问答请求）：DATA-CONTRACTS §2 的
      关联关系写作 "audit→consumption"，故按审计记录指向请求。这是**实现解读**，
      已在 FUNCTION-MAP M06 使用处保持一致。

    "晚提交的记录可被后续扫描领取"（DATA-CONTRACTS §2）也指望这张表：
    不用最大自增 ID 排除晚到日志（那样会永久漏掉它们）。
    """

    __tablename__ = "mining_consumption"

    log_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("qa_audit.request_id", ondelete="CASCADE"), primary_key=True
    )
    pipeline_version: Mapped[str] = mapped_column(String(LEN_MODEL_VERSION), primary_key=True)
    run_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("mining_run.id", ondelete="SET NULL"), nullable=True
    )
    #: 消费状态（claimed/done/failed…）。契约未固定取值集合，故不加 CHECK。
    status: Mapped[str] = mapped_column(String(LEN_STATUS), nullable=False)
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    __table_args__ = (Index("ix_mining_consumption_status_lease", "status", "lease_until"),)


class QuestionCluster(PKMixin, TimestampMixin, Base):
    """问题聚类。频次由**去重成员数**计算（DATA-CONTRACTS §2）。"""

    __tablename__ = "question_cluster"

    #: 代表问题文本（FUNCTION-MAP §1 `QuestionCluster.representative`）。
    representative: Mapped[str] = mapped_column(Text, nullable=False)
    #: 聚类流水线版本：与 `mining_consumption.pipeline_version` 对齐。
    version: Mapped[str] = mapped_column(String(LEN_MODEL_VERSION), nullable=False)

    __table_args__ = (Index("ix_question_cluster_version", "version"),)


class ClusterMember(Base):
    """聚类成员（关联表）：`(cluster_id, log_id)` 联合主键。

    频次 = 成员去重计数，因此**同一日志重复入簇不会虚增频次**（主键挡住）。
    """

    __tablename__ = "cluster_member"

    cluster_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("question_cluster.id", ondelete="CASCADE"), primary_key=True
    )
    log_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("qa_audit.request_id", ondelete="CASCADE"), primary_key=True
    )


class FaqDraftJob(TimestampMixin, Base):
    """模型起草任务。`job_key` 是**自然主键**（契约未列 `id`）。

    ★ 无自增主键是有意的：`job_key` 唯一即"同一聚类同一版本只起草一次"，
      模型成功结果可被复用；多一个自增 id 只会让"同一 job_key 出现两行"
      变成可能（`UNIQUE` 与主键在语义上就不再等价）。
    """

    __tablename__ = "faq_draft_job"

    job_key: Mapped[str] = mapped_column(String(LEN_CODE), primary_key=True)
    run_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("mining_run.id", ondelete="CASCADE"), nullable=False
    )
    cluster_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("question_cluster.id", ondelete="CASCADE"), nullable=False
    )
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: 起草时使用的来源快照（JSON）：审核人据此判断"模型当时看到了什么"。
    sources_snapshot: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(LEN_STATUS), nullable=False)
    model_version: Mapped[str] = mapped_column(String(LEN_MODEL_VERSION), nullable=False)

    __table_args__ = (
        Index("ix_faq_draft_job_run_status", "run_id", "status"),
        UniqueConstraint("run_id", "cluster_id"),
    )

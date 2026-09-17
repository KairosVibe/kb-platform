"""知识单元、四维 ACL、版本与切片实体。

对应 DATA-CONTRACTS.md §2 中的 knowledge_unit / knowledge_acl_department / role / user /
knowledge_version / chunk，以及 FUNCTION-MAP.md §2.1 第 2 条。

三个字段级决定值得单独说明：

1. **`global` → `is_global`**：`GLOBAL` 是 MySQL 保留字，裸列名必须反引号转义，
   一旦有人漏写就会变成语法错误或语义歧义；因此属性名与列名都用 `is_global`。
   该映射已登记在 FUNCTION-MAP §1 的名称映射表中（流程规则：不允许只在代码里默默改名）。
2. **`index_status` 只允许 `pending|indexed|stale` 三态**（PRD §1.2 第 1 条 原文），并落到
   `CHECK` 约束。★ 注意 `failed` **不属于单元状态**——失败是任务的结局（`index_task.status`），
   把两者混起来会让"单元到底可不可以被检索"这个问题失去唯一答案。
   DATA-CONTRACTS §1 允许"数据库/服务枚举校验"，此处选择数据库侧，让脏状态无法写入。
3. **`creator_id` 只用于审计，不赋读权**（DATA-CONTRACTS §2："creator仅审计不赋读权"）。
   它甚至不进 `UnitState` 判定类型（FUNCTION-MAP §1），从类型层面排除创建者旁路。
"""

from __future__ import annotations

from typing import Any

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
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, PKMixin, RevisionMixin, TimestampMixin
from app.db.types import LEN_CODE, LEN_FILE_KEY, LEN_MODEL_VERSION, LEN_NAME, LEN_PARSER_VERSION, LEN_STATUS, LEN_TITLE

#: PRD §1.2 第 1 条 固定三态。改这里必须同时改 PRD 与生成一次迁移（这正是 CHECK 的用意）。
INDEX_STATUS_VALUES: tuple[str, ...] = ("pending", "indexed", "stale")


class KnowledgeUnit(PKMixin, TimestampMixin, RevisionMixin, Base):
    """知识单元（一份文档的逻辑身份，跨版本稳定）。"""

    __tablename__ = "knowledge_unit"

    code: Mapped[str] = mapped_column(String(LEN_CODE), nullable=False, unique=True)
    title: Mapped[str] = mapped_column(String(LEN_TITLE), nullable=False)
    #: 首轮白名单 docx/pdf/md/txt（D-02 已确认）；DOC 与扫描 OCR 明确不支持。
    format: Mapped[str] = mapped_column(String(16), nullable=False)
    category: Mapped[str] = mapped_column(String(LEN_NAME), nullable=False, default="default")
    #: ★ 仅审计，不赋读权。
    creator_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("user.id", ondelete="RESTRICT"), nullable=False
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: 墓碑标记：与 `enabled` **独立**（PRD §1.2 第 1 条）。删除优先于停用（判定侧 DENY_DELETED）。
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: 契约字段名 `global`（四维之一），实现与列名用 `is_global`。
    is_global: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: 内容版本：替换文件或变更切片配置时 +1（PRD §1.2 第 1 条）。
    content_version: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=1, server_default=text("1")
    )
    #: 已成功建索引的版本。**NULL = 尚未索引**；必须满足 indexed_version <= content_version。
    indexed_version: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    index_status: Mapped[str] = mapped_column(
        String(LEN_STATUS), nullable=False, default="pending", server_default="pending"
    )
    #: ACL 修订号：每次授权变更 +1，供缓存与通知判断失效。
    acl_version: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=1, server_default=text("1")
    )

    __table_args__ = (
        CheckConstraint(
            "index_status IN ('pending', 'indexed', 'stale')",
            name="index_status_values",
        ),
        Index("ix_knowledge_unit_status", "is_deleted", "enabled", "index_status"),
    )


class _AclTableMixin:
    """四维 ACL 的三张强类型关联表共用形状（接口仍用四维数组，见 API-CONTRACTS §5 API-S04）。

    三张表而不是一张 `(subject_type, subject_id)`：强类型让外键真正生效，
    否则"角色 ID 写进部门维度"这类错误数据库根本不会拦。
    """

    unit_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("knowledge_unit.id", ondelete="CASCADE"), primary_key=True
    )


class KnowledgeAclDepartment(_AclTableMixin, Base):
    __tablename__ = "knowledge_acl_department"

    subject_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("department.id", ondelete="RESTRICT"), primary_key=True
    )

    __table_args__ = (
        # 反向查询：删除/停用部门前要检查是否仍被 ACL 引用（DATA-CONTRACTS §2）。
        Index("ix_knowledge_acl_department_subject", "subject_id"),
    )


class KnowledgeAclRole(_AclTableMixin, Base):
    __tablename__ = "knowledge_acl_role"

    subject_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("role.id", ondelete="RESTRICT"), primary_key=True
    )

    __table_args__ = (Index("ix_knowledge_acl_role_subject", "subject_id"),)


class KnowledgeAclUser(_AclTableMixin, Base):
    __tablename__ = "knowledge_acl_user"

    subject_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("user.id", ondelete="RESTRICT"), primary_key=True
    )

    __table_args__ = (Index("ix_knowledge_acl_user_subject", "subject_id"),)


class KnowledgeVersion(PKMixin, TimestampMixin, Base):
    """版本记录（不可变：不进 revision 体系，DATA-CONTRACTS §2 未列 revision）。

    `embedding_model_version` 与 `index_generation` 都来自配置的模型版本串
    （provider+model+dimension，FUNCTION-MAP §1 `config（Provider）`）。
    ★ 该串变化即等于换向量空间：必须新建代际并重建，**禁止在同一 collection 内混查**
      （PRD BC-09.02 → 409 REINDEX_REQUIRED）。
    """

    __tablename__ = "knowledge_version"

    unit_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("knowledge_unit.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    #: 受控存储键（UUID 路径），**不是用户提供的路径**（DATA-CONTRACTS §2）。
    file_key: Mapped[str] = mapped_column(String(LEN_FILE_KEY), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    parser_version: Mapped[str] = mapped_column(String(LEN_PARSER_VERSION), nullable=False)
    #: 切片配置快照（结构化元数据，允许 JSON；DATA-CONTRACTS §1）。
    chunk_config: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    embedding_model_version: Mapped[str] = mapped_column(
        String(LEN_MODEL_VERSION), nullable=False
    )
    index_generation: Mapped[str] = mapped_column(String(LEN_MODEL_VERSION), nullable=False)

    __table_args__ = (
        # 不传 name：由 MetaData 命名约定生成 `uq_knowledge_version_unit_id_version`。
        # ★ 显式传 name 会**绕过约定**（实测：名为 `unit_version` 的约束不带 uq_ 前缀），
        #   于是同一套库里出现两种命名风格，downgrade 时按名字删约束就会漏。
        UniqueConstraint("unit_id", "version"),
        Index("ix_knowledge_version_generation", "embedding_model_version", "index_generation"),
    )


class Chunk(PKMixin, TimestampMixin, Base):
    """切片。稳定向量键由 `(version, seq)` 推导（DATA-CONTRACTS §2）。

    ★ `id` 是自增 BIGINT，所谓"稳定 chunk_id"指**同一版本重新索引时不换 id**
      （服务层用条件更新/复用已有行实现），而不是"id 可预测"。重新索引若改成
      删旧插新，历史引用（`message_source.chunk_id`）就会指向空行——那是缺陷。
    """

    __tablename__ = "chunk"

    unit_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    #: Location 快照：page_no / start_offset / end_offset / original_offset（FUNCTION-MAP §1）。
    #: 偏移基于清洗文本的 Unicode 码点；无法可靠映射时 original_offset 为 null——
    #: **不伪造精确位置**（FORMAT-ACCEPTANCE §1）。
    location: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    __table_args__ = (
        UniqueConstraint("unit_id", "version", "seq"),
        # 复合 FK 指向版本表：物理上杜绝"切片挂在不存在的版本上"（DATA-CONTRACTS §2）。
        # 同样不传 name，由约定生成 `fk_chunk_unit_id_knowledge_version`。
        ForeignKeyConstraint(
            ["unit_id", "version"],
            ["knowledge_version.unit_id", "knowledge_version.version"],
            ondelete="CASCADE",
        ),
    )

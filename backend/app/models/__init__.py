"""ORM 实体总入口。

★ **导入本模块等于把全部表注册进 `Base.metadata`**。Alembic 的 `env.py` 只导入这里，
  所以任何新增实体都必须在本文件里出现——漏掉导入不会报错，只会让迁移"少一张表"，
  而且要到集成测试或线上才发现。

覆盖 DATA-CONTRACTS.md §2 实体字典的**全部 38 张表**：

- 身份与会话（7）：department / user / role / role_permission / user_role /
  auth_session / refresh_token —— 关闭审计 PA-07（持久化刷新撤销）所需；
- 知识与导入（10）：knowledge_unit / knowledge_acl_department / knowledge_acl_role /
  knowledge_acl_user / knowledge_version / chunk / index_task / cleanup_task /
  upload_batch / upload_item —— 前三段关闭 PA-06（`authorize_units` 读库）所需；
  末两张为 M03 的受理登记，**2026-09-17 补齐（迁移 `0002`）**；
- 会话与审计（7）：chat_session / chat_request / chat_message / message_source /
  chat_event / qa_audit / model_call_usage；
- FAQ 与沉淀（7）：faq / faq_source / mining_run / mining_consumption /
  question_cluster / cluster_member / faq_draft_job；
- 缺口闭环（3）：knowledge_gap / gap_request / supplement_task；
- 运维（4）：config_revision / operation_log / outbox_event / idempotency_record。

★ **勘误（2026-09-17）**：本文件原称"字典已满、DATA-CONTRACTS §2 列出的实体全部有对应声明"，
**该说法不成立**。集合比对（契约声明的表名 vs `Base.metadata.tables`）发现 `upload_batch` 与
`upload_item` 在 §2 中声明、但代码与初始迁移 `0001` 都没有——M03 开工前对账时才暴露。
两张表已补齐并新增迁移 `0002`。

**教训：实体是否齐备只能靠"契约集合 vs 元数据集合"的集合比对来证明，不能靠写法声明**
（与"完成声明必须附命令与输出"同一条纪律）。「看清单觉得齐了」与「做过差集」是两件事。

后续若新增实体，必须先改字典再改代码（FUNCTION-MAP §2.1 第 10 条）。
"""

from app.models.chat import (
    ChatEvent,
    ChatMessage,
    ChatRequest,
    ChatSession,
    MessageSource,
    ModelCallUsage,
    QaAudit,
)
from app.models.faq import (
    ClusterMember,
    Faq,
    FaqDraftJob,
    FaqSource,
    MiningConsumption,
    MiningRun,
    QuestionCluster,
)
from app.models.gap import GapRequest, KnowledgeGap, SupplementTask
from app.models.identity import (
    AuthSession,
    Department,
    RefreshToken,
    Role,
    RolePermission,
    User,
    UserRole,
    normalize_username,
)
from app.models.ingest import CleanupTask, IndexTask, UploadBatch, UploadItem
from app.models.knowledge import (
    Chunk,
    KnowledgeAclDepartment,
    KnowledgeAclRole,
    KnowledgeAclUser,
    KnowledgeUnit,
    KnowledgeVersion,
)
from app.models.ops import ConfigRevision, IdempotencyRecord, OperationLog, OutboxEvent

#: 供种子数据与体检测试用：全部已注册实体（顺序稳定，便于报告 diff）。
ALL_MODELS = (
    # 身份与会话
    Department,
    User,
    Role,
    RolePermission,
    UserRole,
    AuthSession,
    RefreshToken,
    # 知识与导入
    KnowledgeUnit,
    KnowledgeAclDepartment,
    KnowledgeAclRole,
    KnowledgeAclUser,
    KnowledgeVersion,
    Chunk,
    IndexTask,
    CleanupTask,
    UploadBatch,
    UploadItem,
    # 会话与审计
    ChatSession,
    ChatRequest,
    ChatMessage,
    MessageSource,
    ChatEvent,
    QaAudit,
    ModelCallUsage,
    # FAQ 与沉淀
    Faq,
    FaqSource,
    MiningRun,
    MiningConsumption,
    QuestionCluster,
    ClusterMember,
    FaqDraftJob,
    # 缺口闭环
    KnowledgeGap,
    GapRequest,
    SupplementTask,
    # 运维
    ConfigRevision,
    OperationLog,
    OutboxEvent,
    IdempotencyRecord,
)

__all__ = [
    "ALL_MODELS",
    "AuthSession",
    "ChatEvent",
    "ChatMessage",
    "ChatRequest",
    "ChatSession",
    "Chunk",
    "CleanupTask",
    "ClusterMember",
    "ConfigRevision",
    "Department",
    "Faq",
    "FaqDraftJob",
    "FaqSource",
    "GapRequest",
    "IdempotencyRecord",
    "IndexTask",
    "KnowledgeAclDepartment",
    "KnowledgeAclRole",
    "KnowledgeAclUser",
    "KnowledgeGap",
    "KnowledgeUnit",
    "KnowledgeVersion",
    "MessageSource",
    "MiningConsumption",
    "MiningRun",
    "ModelCallUsage",
    "OperationLog",
    "OutboxEvent",
    "QaAudit",
    "QuestionCluster",
    "RefreshToken",
    "Role",
    "RolePermission",
    "SupplementTask",
    "UploadBatch",
    "UploadItem",
    "User",
    "UserRole",
    "normalize_username",
]

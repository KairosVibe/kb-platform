"""种子数据的**定义**（纯数据，不碰数据库）。

把这些常量单独放一个模块，是为了让测试能在**不需要数据库**的前提下校验它们——
本机 MySQL 服务未启动，若定义与建库代码混在一起就无法验证。校验见
`tests/test_seed_definitions.py`。

★ 一条铁律：**种子数据里不允许出现任何"数据读权旁路"**。
  系统管理员角色的 14 个功能权限码只代表"能执行哪些操作"，**不代表能看哪些正文**——
  正文可见性一律由 knowledge_unit 的四维 ACL 决定（DESIGN_REVISION §2.1）。
  历史上 `SUPER_ADMIN_ROLE_CODE` 的旁路已按审计 PA-01 删除，不得借种子数据复活。
"""

from __future__ import annotations

# ---------------------------------------------------------------- 部门（演示资料）

#: 演示场景所需部门（PRD §1.4 财务/人事四维正反例用到人力资源部与管理层）。
#: 这些是**模拟资料**，不构成现实组织架构（DELIVERY-EVALUATION §1）。
DEPARTMENTS: tuple[str, ...] = (
    "管理层",
    "人力资源部",
    "财务部",
    "客服部",
)

# ---------------------------------------------------------------- 角色与功能权限

#: 普通提问者：只有问答准入。注意**不给 `kb:view`**——`kb:view` 是"管理元数据"能力
#: （PRD BC-04.01 明确"不能扩展为正文读权"），普通用户不需要。
ROLE_END_USER = "普通用户"
ROLE_KB_ADMIN = "知识管理员"
ROLE_SYS_ADMIN = "系统管理员"

ROLE_PERMISSIONS: dict[str, tuple[str, ...]] = {
    ROLE_END_USER: ("ai:ask",),
    ROLE_KB_ADMIN: (
        "ai:ask",
        "kb:view",
        "kb:upload",
        "kb:edit",
        "kb:delete",
        "kb:perm",
        "faq:review",
        "faq:publish",
        "gap:handle",
        "dashboard:view",
    ),
    # 系统管理员 = 全部 14 项**功能**权限。★ 这不是数据权限：
    # 它能管理用户/角色/部门/模型配置，但读不到任何未显式授权的正文。
    ROLE_SYS_ADMIN: (
        "ai:ask",
        "kb:view",
        "kb:upload",
        "kb:edit",
        "kb:delete",
        "kb:perm",
        "faq:review",
        "faq:publish",
        "gap:handle",
        "dashboard:view",
        "sys:user",
        "sys:role",
        "sys:dept",
        "sys:model",
    ),
}

# ---------------------------------------------------------------- 初始检索配置

#: `config_revision` 首行快照。取自 FUNCTION-MAP §1 `config（Retrieval）` 的契约默认值
#: （vector_top_k=20、keyword_top_k=20、max_per_route=100、answer_top_k=5、rrf_k=60）。
#: **只放非密钥参数**（DATA-CONTRACTS §2：config_revision 只保存允许的非密钥参数与 secret_ref）。
INITIAL_RETRIEVAL_CONFIG: dict[str, int] = {
    "vector_top_k": 20,
    "keyword_top_k": 20,
    "max_per_route": 100,
    "answer_top_k": 5,
    "rrf_k": 60,
}


def provider_snapshot(
    *, provider: str, model: str, dimension: int, model_version: str, endpoint: str
) -> dict[str, object]:
    """provider 快照（**不含任何密钥**，只含可公开的地址/模型/维度）。

    `model_version` 是 `provider:model:dimension` 串（`Settings.embedding_model_version`）。
    ★ 它变化即等于换向量空间，必须新建索引代际并重建，禁止在同一 collection 内混查
      （PRD BC-09.02 → 409 REINDEX_REQUIRED）。
    """
    return {
        "provider": provider,
        "model": model,
        "dimension": dimension,
        "model_version": model_version,
        "endpoint": endpoint,
        "secret_ref": "env:DASHSCOPE_API_KEY",  # 只存引用名，不存值
    }

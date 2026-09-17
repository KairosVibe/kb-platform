"""权限码静态定义（唯一来源，前后端共用）。

对应 FUNCTION-MAP.md §1（公共类型）、PRD.md §1（14 个功能码）、PLAN.md §2（已决事项）。

设计要点：
- 权限码是"功能权限"（能否执行某类操作），与"数据权限"（能否看某条知识）是两套体系，
  前者在这里定义，后者在 engines/permission.py 实现。
- 前端角色权限树直接渲染本表，保证前后端不会出现权限码漂移。
- ★ 不存在"超级管理员"功能码或数据旁路标记：系统管理员同样按普通用户规则逐项授权
  （DESIGN_REVISION §2.1）。曾有的 `SUPER_ADMIN_ROLE_CODE` 已按审计 PA-01 删除，
  不得重新引入。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class PermGroup(StrEnum):
    """权限码分组，用于前端权限树的分栏展示。"""

    QA = "问答"
    KB = "知识"
    OPS = "沉淀"
    DASH = "看板"
    SYS = "系统"


@dataclass(frozen=True, slots=True)
class PermissionDef:
    code: str
    name: str          # 中文名，前端直接展示
    group: PermGroup


# 14 个权限码。新增权限必须同时更新本表与前端权限树，否则角色无法授权。
PERMISSIONS: tuple[PermissionDef, ...] = (
    PermissionDef("ai:ask", "AI 访问", PermGroup.QA),
    PermissionDef("kb:view", "知识查看", PermGroup.KB),
    PermissionDef("kb:upload", "文档上传", PermGroup.KB),
    PermissionDef("kb:edit", "知识编辑", PermGroup.KB),
    PermissionDef("kb:delete", "知识删除", PermGroup.KB),
    PermissionDef("kb:perm", "数据权限配置", PermGroup.KB),
    PermissionDef("faq:review", "FAQ 审核", PermGroup.OPS),
    PermissionDef("faq:publish", "FAQ 发布", PermGroup.OPS),
    PermissionDef("gap:handle", "缺口处理", PermGroup.OPS),
    PermissionDef("dashboard:view", "看板查看", PermGroup.DASH),
    PermissionDef("sys:user", "用户管理", PermGroup.SYS),
    PermissionDef("sys:role", "角色管理", PermGroup.SYS),
    PermissionDef("sys:dept", "部门管理", PermGroup.SYS),
    PermissionDef("sys:model", "模型配置", PermGroup.SYS),
)

# 由列表派生，避免手写计数与实际条目不一致（WORKLOG 问题 #9 的教训）
PERMISSION_CODES: frozenset[str] = frozenset(p.code for p in PERMISSIONS)

# AI 问答工作台的准入码。单独提取是因为它有一个特殊错误码 PERM_AI_DENIED
# （定义见 app/core/response.py），需要与普通权限拒绝区分，前端据此给出更具体的提示。
AI_ASK = "ai:ask"

# ★ PA-01（2026-09-16 已关闭）：此处原有 `SUPER_ADMIN_ROLE_CODE = "sys_admin"`，
# 其语义是"持有该角色的用户跳过数据权限判定"。该旁路违反 DESIGN_REVISION §2.1
# "创建者和系统管理员没有自动正文读权"，已删除。若需要管理员可读，请为其角色显式
# 配置知识单元的四维授权，而不是恢复任何形式的旁路开关。
# 管理能力（如"保留最后一个可管理账号"）属于功能权限与账号保护，不属于数据读权，
# 由 org_svc 的账号保护规则处理，与本文件无关。


def all_codes() -> frozenset[str]:
    """返回全部合法权限码，用于校验角色配置（拒绝未注册的码）。"""
    return PERMISSION_CODES


def is_valid_code(code: str) -> bool:
    """判断权限码是否已注册。未知码应被拒绝写入 role_perm 表。"""
    return code in PERMISSION_CODES

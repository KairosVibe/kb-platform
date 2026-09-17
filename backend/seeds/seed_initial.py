"""初始种子数据写入（可重复执行）。

运行前提：
1. MySQL 8.0.26 服务已启动，且目标库已按 utf8mb4 建好（见 `seeds/README.md`）；
2. 已执行 `alembic upgrade head`（本脚本**不会**建表——建表只能由迁移完成）；
3. 环境变量 `KB_SEED_ADMIN_PASSWORD` 已设置。

    python -m seeds.seed_initial

三条有意为之的设计：

1. **不提供默认口令**。管理员口令必须由环境变量显式提供，且走与登录同一套
   密码策略（`hash_password`，12—72 UTF-8 字节）。种子脚本里的"admin/123456"
   是最常见的生产事故来源，这里从设计上就不给。
2. **可重复执行**：已存在的部门/角色/权限码/账号按规范化名跳过，不重复插入。
   重复执行不应报错，也不应产生重复行——否则"再跑一次"就成了危险动作。
3. **权限码必须在 `core.permissions` 注册过**才能写入。写库前的校验用同一份白名单，
   避免种子数据成为"绕过权限码唯一来源"的第二个事实源。
"""

from __future__ import annotations

import asyncio
import os
import sys

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.permissions import PERMISSION_CODES
from app.core.security import hash_password
from app.db.base import utcnow
from app.db.repository import UnitOfWork
from app.db.session import dispose_engine, get_sessionmaker
from app.models import (
    ConfigRevision,
    Department,
    Role,
    RolePermission,
    User,
    UserRole,
    normalize_username,
)
from seeds.definitions import (
    DEPARTMENTS,
    INITIAL_RETRIEVAL_CONFIG,
    ROLE_PERMISSIONS,
    ROLE_SYS_ADMIN,
    provider_snapshot,
)

ADMIN_USERNAME_ENV = "KB_SEED_ADMIN_USERNAME"
ADMIN_PASSWORD_ENV = "KB_SEED_ADMIN_PASSWORD"


class SeedError(RuntimeError):
    """种子数据前置条件不满足。**宁可失败也不静默降级**——半套种子数据比没有更难排查。"""


def _admin_credentials() -> tuple[str, str]:
    username = (os.environ.get(ADMIN_USERNAME_ENV) or "admin").strip()
    password = os.environ.get(ADMIN_PASSWORD_ENV) or ""
    if not password:
        raise SeedError(
            f"必须通过环境变量 {ADMIN_PASSWORD_ENV} 提供管理员初始口令（不提供默认口令）。"
        )
    return username, password


def validate_permission_codes() -> None:
    """写库前校验：种子数据里的每个权限码都必须在 `core.permissions` 注册过。"""
    unknown = sorted(
        {code for codes in ROLE_PERMISSIONS.values() for code in codes} - set(PERMISSION_CODES)
    )
    if unknown:
        raise SeedError(
            f"种子数据引用了未注册的权限码 {unknown}；"
            "请先在 app/core/permissions.py 的 PERMISSIONS 中注册（唯一来源）。"
        )


async def _seed_departments(uow: UnitOfWork) -> dict[str, int]:
    repo = uow.repo(Department)
    ids: dict[str, int] = {}
    for name in DEPARTMENTS:
        existing = await repo.find_one({"name": name})
        row = existing or await repo.insert({"name": name})
        ids[name] = row.id
    return ids


async def _seed_roles(uow: UnitOfWork) -> dict[str, int]:
    role_repo = uow.repo(Role)
    perm_repo = uow.repo(RolePermission)
    ids: dict[str, int] = {}
    for role_name, codes in ROLE_PERMISSIONS.items():
        norm = role_name.casefold()
        role = await role_repo.find_one({"name_norm": norm})
        if role is None:
            role = await role_repo.insert({"name": role_name, "name_norm": norm})
        ids[role_name] = role.id
        for code in codes:
            already = await perm_repo.find_one({"role_id": role.id, "code": code})
            if already is None:
                await perm_repo.insert({"role_id": role.id, "code": code})
    return ids


async def _seed_admin(uow: UnitOfWork, *, dept_id: int | None, sys_role_id: int) -> int:
    """创建初始系统管理员账号并授予系统管理员角色。

    ★ 只授予**角色**（功能权限），不写入任何 `knowledge_acl_*` 行：
      该账号对正文的可见性依然为空，符合"默认全空拒绝"与"无超管读权旁路"。
    """
    username, password = _admin_credentials()
    norm = normalize_username(username)
    user_repo = uow.repo(User)
    user = await user_repo.find_one({"username_norm": norm})
    if user is None:
        user = await user_repo.insert(
            {
                "username": username,
                "username_norm": norm,
                "password_hash": hash_password(password),
                "dept_id": dept_id,
                "enabled": True,
            }
        )
    user_role_repo = uow.repo(UserRole)
    link = await user_role_repo.find_one({"user_id": user.id, "role_id": sys_role_id})
    if link is None:
        await user_role_repo.insert({"user_id": user.id, "role_id": sys_role_id})
    return user.id


async def _seed_config_revision(uow: UnitOfWork, *, actor_id: int | None) -> None:
    """写入首条配置修订（非密钥参数快照）。"""
    repo = uow.repo(ConfigRevision)
    if await repo.find_one({"id": 1}) is not None:
        return
    settings = get_settings()
    snapshot: dict[str, object] = {
        "retrieval": dict(INITIAL_RETRIEVAL_CONFIG),
        "embedding": provider_snapshot(
            provider=settings.embed_provider,
            model=settings.embed_model,
            dimension=settings.embed_dim,
            model_version=settings.embedding_model_version,
            endpoint=settings.dashscope_base_url,
        ),
        "generation": {
            "model": settings.llm_model,
            "endpoint": settings.dashscope_base_url,
            "timeout_seconds": settings.llm_timeout_s,
        },
        "seeded_at": utcnow().isoformat(),
    }
    await repo.insert({"patch": {}, "snapshot": snapshot, "actor_id": actor_id})


async def run(session: AsyncSession) -> None:
    validate_permission_codes()
    uow = UnitOfWork(session)
    async with uow.transaction():
        departments = await _seed_departments(uow)
        roles = await _seed_roles(uow)
        admin_id = await _seed_admin(
            uow, dept_id=departments["管理层"], sys_role_id=roles[ROLE_SYS_ADMIN]
        )
        await _seed_config_revision(uow, actor_id=admin_id)
    print(
        f"种子数据完成：部门 {len(departments)}、角色 {len(roles)}、"
        f"管理员 user_id={admin_id}、配置修订 1 条。"
    )
    print(
        "★ 管理员默认对任何正文**不可见**（无 knowledge_acl_* 行）。"
        "需要可见时必须显式配置四维授权——这是设计要求，不是遗漏。"
    )


async def main() -> int:
    try:
        session_factory = get_sessionmaker()
        async with session_factory() as session:
            await run(session)
    finally:
        await dispose_engine()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except SeedError as exc:
        print(f"种子数据失败：{exc}", file=sys.stderr)
        raise SystemExit(2) from exc

"""组织、用户与功能权限服务（M02：F-02.01—F-02.09 + API-S01/S02/S03）。

对应 FUNCTION-MAP §3-M02 与 API-CONTRACTS §5（补充入口）、§7（角色/部门删除遇引用默认 409）。

四条**安全相关**的取舍，读代码前先看这里：

1. **删除是"先查引用再删"，不是"删了看数据库报不报错"**。三张 ACL 表与 `user_role` 都用
   `ondelete="RESTRICT"`，数据库确实能拦，但那样只能给客户端一个笼统的 500/409，
   无法区分"有子部门"还是"知识授权引用"。所以这里显式分三路检查并各自给出可操作提示
   （DESIGN_REVISION §7"删除遇到引用默认 409，要求先迁移成员或撤销引用"）。
2. **部门环检测在服务层**（MySQL 无原生环约束）：把 `parent_id` 指向自己的后代会让整棵树
   从根上失联，且此后任何"向上遍历"都会挂住。判定必须在写入前完成。
3. **"最后一个管理能力"是账号保护，不是数据读权旁路**。`SELF_LOCK` / `ROLE_PROTECTED` 保护的
   是"还能不能管理系统"这件事；它**不授予任何人读知识正文的权利**（`core/permissions.py`
   已按 PA-01 删除超级管理员旁路，不得在此以任何形式恢复）。
4. **写入与 `operation_log` 同事务**（DATA-CONTRACTS §3 第 1 条）。组织变更必须能回答
   "谁在什么时候把什么改成了什么"，因此 `before`/`after` 是**显式挑选的字段白名单**，
   绝不整行 dump——否则 `password_hash` 会随日志落库。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import ensure_request_id
from app.core.permissions import PERMISSIONS, is_valid_code
from app.core.response import BizError
from app.core.security import PasswordPolicyError, hash_password
from app.db.repository import Repository, UniqueViolationError
from app.engines.permission import UserCtx
from app.models.identity import (
    Department,
    Role,
    RolePermission,
    User,
    UserRole,
    normalize_username,
)
from app.models.knowledge import KnowledgeAclDepartment, KnowledgeAclRole
from app.models.ops import OperationLog

#: 构成"管理能力"的功能码。停用/删除若让启用账号中无人再持有其中任一码，
#: 平台将无法再被管理，因此拒绝（`SELF_LOCK` / `ROLE_PROTECTED`）。
ADMIN_CODES: tuple[str, ...] = ("sys:user", "sys:role", "sys:dept")

#: 目录选择器支持的 kind（F-02.09）。
DIRECTORY_KINDS: tuple[str, ...] = ("user", "role", "department")

#: 目录选择器的 kind → 所需功能码。**kind 决定校验哪一个码**，
#: 因此这个映射必须与路由层的用法一致；不用单一码是为了不把"看部门列表"
#: 也塞进"用户管理"的权限里（三面板独立授权，PRD §1.1 第 2 条）。
DIRECTORY_KIND_PERMISSION: dict[str, str] = {
    "user": "sys:user",
    "role": "sys:role",
    "department": "sys:dept",
}


# ---------------------------------------------------------------- 审计


def _log(
    session: AsyncSession,
    ctx: UserCtx,
    *,
    action: str,
    resource_type: str,
    resource_id: int | None,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
) -> None:
    """登记操作日志（与业务变更同事务）。

    `before`/`after` **只放脱敏字段**：调用方必须显式构造，禁止传 ORM 行或 `dict(row)`。
    """
    session.add(
        OperationLog(
            actor_id=ctx.user_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            trace_id=ensure_request_id(),
            before=before,
            after=after,
            status="ok",
        )
    )


def _department_snapshot(row: Department) -> dict[str, Any]:
    return {"id": row.id, "parent_id": row.parent_id, "name": row.name, "revision": row.revision}


def _user_snapshot(row: User, role_ids: Sequence[int]) -> dict[str, Any]:
    """★ 白名单快照：**没有 `password_hash`、没有 `username_norm`**。"""
    return {
        "id": row.id,
        "username": row.username,
        "dept_id": row.dept_id,
        "role_ids": list(role_ids),
        "enabled": row.enabled,
        "revision": row.revision,
    }


# ---------------------------------------------------------------- 部门（F-02.01—F-02.04）


async def list_departments(session: AsyncSession, ctx: UserCtx) -> list[Department]:
    """F-02.01：部门树。

    **不分页**：API-CONTRACTS §1 明确把"部门树"列为分页规则的例外——树必须一次给全，
    否则前端无法组装父子关系。
    """
    del ctx
    stmt = select(Department).order_by(Department.id.asc())
    return list((await session.execute(stmt)).scalars())


async def _require_department(session: AsyncSession, dept_id: int) -> Department:
    row = await Repository(session, Department).get(dept_id)
    if row is None:
        # 防枚举：不区分"不存在"与"存在但无权"。本能力已通过 H02（sys:dept），
        # 此处 404 只表达"该 ID 对应的组织对象不存在"。
        raise BizError("NOT_FOUND")
    return row


async def _assert_sibling_name_free(
    session: AsyncSession, *, parent_id: int | None, name: str, exclude_id: int | None = None
) -> None:
    """同级同名检查。

    ★ 用 `parent_id IS NULL` 与 `parent_id = ?` 两个分支，而不是 `parent_id = None`：
      SQL 里 `NULL = NULL` 为 UNKNOWN，写成等值查询会让**根部门之间永远查不出重名**。
    """
    stmt = select(func.count()).select_from(Department).where(Department.name == name)
    stmt = stmt.where(Department.parent_id.is_(None) if parent_id is None else Department.parent_id == parent_id)
    if exclude_id is not None:
        stmt = stmt.where(Department.id != exclude_id)
    if await session.scalar(stmt):
        raise BizError("DEPT_DUPLICATE")


async def _assert_no_cycle(session: AsyncSession, *, dept_id: int, new_parent_id: int | None) -> None:
    """禁止把部门移动到自身或自身后代之下（DESIGN_REVISION §7）。

    从 `new_parent_id` 向上回溯，遇到 `dept_id` 即构成环。带 `visited` 集合是为了在
    **数据已经被写坏**（历史脏数据已有环）时终止遍历并报错，而不是死循环挂住请求。
    """
    if new_parent_id is None:
        return
    if new_parent_id == dept_id:
        raise BizError("DEPT_CYCLE")

    visited: set[int] = set()
    cursor: int | None = new_parent_id
    while cursor is not None:
        if cursor == dept_id:
            raise BizError("DEPT_CYCLE")
        if cursor in visited:
            raise BizError("DEPT_CYCLE")
        visited.add(cursor)
        cursor = await session.scalar(select(Department.parent_id).where(Department.id == cursor))


async def create_department(
    session: AsyncSession, ctx: UserCtx, *, parent_id: int | None, name: str
) -> Department:
    """F-02.02。`parent_id=None` 建根部门。"""
    if parent_id is not None:
        await _require_department(session, parent_id)
    await _assert_sibling_name_free(session, parent_id=parent_id, name=name)

    try:
        row = await Repository(session, Department).insert({"parent_id": parent_id, "name": name})
    except UniqueViolationError as exc:
        # 并发下"查重通过但插入冲突"仍可能发生（检查与插入不在同一把锁上），
        # 由唯一约束兜底并翻成同一个 409，语义与前置检查一致。
        raise BizError("DEPT_DUPLICATE") from exc

    _log(
        session,
        ctx,
        action="org.department.create",
        resource_type="department",
        resource_id=row.id,
        before=None,
        after=_department_snapshot(row),
    )
    return row


async def update_department(
    session: AsyncSession,
    ctx: UserCtx,
    *,
    dept_id: int,
    parent_id: int | None,
    name: str,
    expected_revision: int,
) -> Department:
    """F-02.03。同时承担改名与移动。"""
    row = await _require_department(session, dept_id)
    before = _department_snapshot(row)
    await _assert_no_cycle(session, dept_id=dept_id, new_parent_id=parent_id)
    if parent_id is not None:
        await _require_department(session, parent_id)
    await _assert_sibling_name_free(session, parent_id=parent_id, name=name, exclude_id=dept_id)

    matched = await Repository(session, Department).update_if(
        dept_id,
        expected={"revision": expected_revision},
        patch={"parent_id": parent_id, "name": name, "revision": Department.revision + 1},
    )
    if not matched:
        # 行一定存在（前面刚读过），所以只可能是 revision 不匹配。
        raise BizError("REVISION_CONFLICT")

    await session.refresh(row)
    _log(
        session,
        ctx,
        action="org.department.update",
        resource_type="department",
        resource_id=dept_id,
        before=before,
        after=_department_snapshot(row),
    )
    return row


async def delete_department(
    session: AsyncSession, ctx: UserCtx, *, dept_id: int, expected_revision: int
) -> bool:
    """F-02.04。**三路引用检查**，任一命中即 409 `DEPT_IN_USE`。"""
    row = await _require_department(session, dept_id)
    if row.revision != expected_revision:
        raise BizError("REVISION_CONFLICT")

    if await Repository(session, Department).exists({"parent_id": dept_id}):
        raise BizError("DEPT_IN_USE", "该部门下仍有子部门，请先迁移或删除子部门")
    if await Repository(session, User).exists({"dept_id": dept_id}):
        raise BizError("DEPT_IN_USE", "该部门下仍有用户，请先把用户调整到其他部门")
    if await Repository(session, KnowledgeAclDepartment).exists({"subject_id": dept_id}):
        raise BizError("DEPT_IN_USE", "该部门仍被知识授权引用，请先在相应知识的权限配置中移除")

    before = _department_snapshot(row)
    result = await session.execute(
        delete(Department).where(Department.id == dept_id, Department.revision == expected_revision)
    )
    if result.rowcount != 1:
        # 检查与删除之间的并发修改：条件删除把它挡住了，语义是版本冲突。
        raise BizError("REVISION_CONFLICT")

    _log(
        session,
        ctx,
        action="org.department.delete",
        resource_type="department",
        resource_id=dept_id,
        before=before,
        after=None,
    )
    return True


# ---------------------------------------------------------------- 用户（F-02.05/F-02.06 + API-S01）


async def _role_ids_of(session: AsyncSession, user_ids: Sequence[int]) -> dict[int, list[int]]:
    """批量取用户的角色 ID。

    ★ 批量而不是逐个查：用户列表一页 20 行，逐个查就是 20 次往返（N+1）。
      这里一次 `IN` 查询拿全，再在内存里分组。
    """
    if not user_ids:
        return {}
    stmt = select(UserRole.user_id, UserRole.role_id).where(UserRole.user_id.in_(list(user_ids)))
    grouped: dict[int, list[int]] = {}
    for user_id, role_id in (await session.execute(stmt)).all():
        grouped.setdefault(user_id, []).append(role_id)
    return grouped


async def list_users(
    session: AsyncSession,
    ctx: UserCtx,
    *,
    q: str = "",
    page: int = 1,
    size: int = 20,
    enabled: bool | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """API-S01：用户分页。

    `q` 走 `username` 模糊匹配；**权限过滤在分页计数之前**（FUNCTION-MAP §2）——
    本入口已由 H02 约束为 `sys:user`，故此处没有额外的行级归属过滤。
    """
    del ctx
    settings = get_settings()
    page = max(1, page)
    size = min(max(1, size), settings.page_size_max)

    conditions = []
    if enabled is not None:
        conditions.append(User.enabled.is_(enabled))
    if q:
        needle = f"%{q.strip()}%"
        # `username` 与 `username_norm` 都匹配：前者保留用户看到的原始大小写，
        # 后者覆盖"输入 admin 想找 Admin"的常见预期。
        conditions.append(or_(User.username.like(needle), User.username_norm.like(needle.casefold())))

    total = int(await session.scalar(select(func.count()).select_from(User).where(*conditions)) or 0)
    stmt = (
        select(User)
        .where(*conditions)
        # 稳定排序：created_at DESC,id DESC（API-CONTRACTS §1）
        .order_by(User.created_at.desc(), User.id.desc())
        .offset((page - 1) * size)
        .limit(size)
    )
    rows = list((await session.execute(stmt)).scalars())
    role_map = await _role_ids_of(session, [r.id for r in rows])

    items = [
        {
            "id": row.id,
            "username": row.username,
            "dept_id": row.dept_id,
            "role_ids": sorted(role_map.get(row.id, [])),
            "enabled": row.enabled,
            "revision": row.revision,
        }
        for row in rows
    ]
    return items, total


async def _assert_roles_exist(session: AsyncSession, role_ids: Iterable[int]) -> None:
    ids = sorted(set(role_ids))
    if not ids:
        return
    found = set(
        (await session.execute(select(Role.id).where(Role.id.in_(ids)))).scalars()
    )
    missing = [rid for rid in ids if rid not in found]
    if missing:
        # 非法 ID 不静默丢弃（API-CONTRACTS §1），否则"授权少了一个角色"会无声无息。
        raise BizError("INVALID_ARGUMENT", f"角色不存在：{missing}")


async def _admin_user_ids(session: AsyncSession, *, overrides: dict[int, Sequence[str]] | None = None) -> set[int]:
    """计算**变更之后**仍持有管理能力的启用用户集合。

    `overrides` 用 `role_id -> 新的 codes` 表达"这次改动生效后的样子"，让前置校验与实际
    写入看到同一份事实。任何"改动后为空"的情况都必须被拦下（`SELF_LOCK`/`ROLE_PROTECTED`）。
    """
    enabled_users = set(
        (await session.execute(select(User.id).where(User.enabled.is_(True)))).scalars()
    )
    if not enabled_users:
        return set()

    pairs = list((await session.execute(select(UserRole.user_id, UserRole.role_id))).all())
    role_codes: dict[int, set[str]] = {}
    for role_id, code in (
        await session.execute(select(RolePermission.role_id, RolePermission.code))
    ).all():
        role_codes.setdefault(role_id, set()).add(code)
    if overrides:
        for role_id, codes in overrides.items():
            role_codes[role_id] = set(codes)

    admin: set[int] = set()
    for user_id, role_id in pairs:
        if user_id not in enabled_users:
            continue
        if user_id in admin:
            continue
        if role_codes.get(role_id, set()) & set(ADMIN_CODES):
            admin.add(user_id)
    return admin


async def create_user(
    session: AsyncSession,
    ctx: UserCtx,
    *,
    username: str,
    password: str,
    dept_id: int | None,
    role_ids: Sequence[int],
) -> User:
    """F-02.05。

    ★ 密码策略**只在此处**由 `core/security.hash_password` 施加：DTO 不校验长度，
      错误码因此稳定为 `PASSWORD_LENGTH_INVALID`（而不是 DTO 校验失败时的 `INVALID_ARGUMENT`）。
    ★ 上传/建号**不自动授予创建者任何读权**（DESIGN_REVISION §2.1），本函数只建账号与角色绑定。
    """
    if dept_id is not None:
        await _require_department(session, dept_id)
    await _assert_roles_exist(session, role_ids)

    try:
        password_hash = hash_password(password)
    except PasswordPolicyError as exc:
        raise BizError("PASSWORD_LENGTH_INVALID") from exc

    try:
        row = await Repository(session, User).insert(
            {
                "username": username,
                "username_norm": normalize_username(username),
                "password_hash": password_hash,
                "dept_id": dept_id,
                "enabled": True,
            }
        )
    except UniqueViolationError as exc:
        raise BizError("USER_DUPLICATE") from exc

    for role_id in sorted(set(role_ids)):
        session.add(UserRole(user_id=row.id, role_id=role_id))

    _log(
        session,
        ctx,
        action="org.user.create",
        resource_type="user",
        resource_id=row.id,
        before=None,
        after=_user_snapshot(row, sorted(set(role_ids))),
    )
    return row


async def update_user(
    session: AsyncSession,
    ctx: UserCtx,
    *,
    user_id: int,
    dept_id: int | None,
    role_ids: Sequence[int],
    enabled: bool,
    expected_revision: int,
) -> User:
    """F-02.06（改部门 / 改角色 / 启停）。"""
    repo = Repository(session, User)
    row = await repo.get(user_id)
    if row is None:
        raise BizError("NOT_FOUND")
    if row.revision != expected_revision:
        raise BizError("REVISION_CONFLICT")

    if dept_id is not None:
        await _require_department(session, dept_id)
    await _assert_roles_exist(session, role_ids)

    old_roles = sorted((await _role_ids_of(session, [user_id])).get(user_id, []))
    before = _user_snapshot(row, old_roles)

    # 停用是"对所有人（含创建者与管理员）不可检索"的动作，因此必须在最前面挡住
    # "把最后一个可管理账号停掉"。
    if row.enabled and not enabled:
        admins = await _admin_user_ids(session)
        if user_id in admins:
            admins.discard(user_id)
            if not admins:
                raise BizError("SELF_LOCK")

    # ★ 三个字段用条件更新一次写完；`identity_revision` 同步 +1，
    #   让"权限/归属已变"对任何缓存或既有会话可见。
    matched = await repo.update_if(
        user_id,
        expected={"revision": expected_revision},
        patch={
            "dept_id": dept_id,
            "enabled": enabled,
            "revision": User.revision + 1,
            "identity_revision": User.identity_revision + 1,
        },
    )
    if not matched:
        raise BizError("REVISION_CONFLICT")

    # 角色绑定是"整体替换"语义：先清后插。用 DELETE+INSERT 而不是 diff，
    # 因为 diff 需要额外的读，且没有唯一性收益。
    await session.execute(delete(UserRole).where(UserRole.user_id == user_id))
    new_roles = sorted(set(role_ids))
    for role_id in new_roles:
        session.add(UserRole(user_id=user_id, role_id=role_id))

    await session.refresh(row)
    _log(
        session,
        ctx,
        action="org.user.update",
        resource_type="user",
        resource_id=user_id,
        before=before,
        after=_user_snapshot(row, new_roles),
    )
    return row


# ---------------------------------------------------------------- 角色（F-02.07/F-02.08 + API-S02/S03）


async def list_roles(
    session: AsyncSession, ctx: UserCtx, *, q: str = "", page: int = 1, size: int = 20
) -> tuple[list[dict[str, Any]], int]:
    """API-S02。返回项含 `codes`，前端权限树据此回填。"""
    del ctx
    settings = get_settings()
    page = max(1, page)
    size = min(max(1, size), settings.page_size_max)

    conditions = []
    if q:
        conditions.append(Role.name.like(f"%{q.strip()}%"))

    total = int(await session.scalar(select(func.count()).select_from(Role).where(*conditions)) or 0)
    stmt = (
        select(Role)
        .where(*conditions)
        .order_by(Role.created_at.desc(), Role.id.desc())
        .offset((page - 1) * size)
        .limit(size)
    )
    rows = list((await session.execute(stmt)).scalars())

    codes_map: dict[int, list[str]] = {}
    if rows:
        stmt_codes = (
            select(RolePermission.role_id, RolePermission.code)
            .where(RolePermission.role_id.in_([r.id for r in rows]))
            .order_by(RolePermission.code.asc())
        )
        for role_id, code in (await session.execute(stmt_codes)).all():
            codes_map.setdefault(role_id, []).append(code)

    items = [
        {"id": row.id, "name": row.name, "codes": codes_map.get(row.id, []), "revision": row.revision}
        for row in rows
    ]
    return items, total


async def list_permission_codes(session: AsyncSession, ctx: UserCtx) -> list[dict[str, str]]:
    """API-S03。**固定 14 码，取自 `core/permissions.py`**（唯一来源），不从库里读。

    这样做的原因：角色表里可能暂时没有任何角色引用某个码，但"可授权的码集合"是契约常量。
    若改成 `SELECT DISTINCT code FROM role_permission`，删掉最后一个角色后权限树就会变空。
    """
    del session, ctx
    return [{"code": p.code, "label": p.name, "module": str(p.group)} for p in PERMISSIONS]


async def save_role(
    session: AsyncSession,
    ctx: UserCtx,
    *,
    role_id: int | None,
    name: str,
    codes: Sequence[str],
    expected_revision: int | None,
) -> tuple[int, list[str], int]:
    """F-02.07。`role_id=None` 为新建。

    边界（API-CONTRACTS §1 / §6）：
    - `codes` 含未注册码 → 422 `INVALID_PERM_CODE`（**不静默丢弃**）；
    - 新建时给 `expected_revision` → 422（两套版本输入不可混用）；
    - 修改时缺 `expected_revision` → 422；
    - 裁剪掉最后一个管理能力 → 409 `ROLE_PROTECTED`。
    """
    unique_codes = list(dict.fromkeys(codes))
    invalid = [c for c in unique_codes if not is_valid_code(c)]
    if invalid:
        raise BizError("INVALID_PERM_CODE", f"未注册的权限码：{sorted(invalid)}")

    repo = Repository(session, Role)
    if role_id is None:
        if expected_revision is not None:
            raise BizError("INVALID_ARGUMENT", "新建角色不应携带 expected_revision")
        try:
            row = await repo.insert({"name": name, "name_norm": normalize_username(name)})
        except UniqueViolationError as exc:
            raise BizError("INVALID_ARGUMENT", "角色名称已存在") from exc
        new_revision = row.revision
    else:
        if expected_revision is None:
            raise BizError("INVALID_ARGUMENT", "修改角色必须携带 expected_revision")
        row = await repo.get(role_id)
        if row is None:
            raise BizError("NOT_FOUND")
        if row.revision != expected_revision:
            raise BizError("REVISION_CONFLICT")
        try:
            matched = await repo.update_if(
                role_id,
                expected={"revision": expected_revision},
                patch={"name": name, "name_norm": normalize_username(name), "revision": Role.revision + 1},
            )
        except UniqueViolationError as exc:
            raise BizError("INVALID_ARGUMENT", "角色名称已存在") from exc
        if not matched:
            raise BizError("REVISION_CONFLICT")
        new_revision = expected_revision + 1

    # 只有"原本有人能管理、改完就没人能管理"才拒绝。
    # ★ **新建角色不检查**：它只可能增加能力；而空库引导时还没有任何管理账号，
    #   若一并检查，会连第一个管理角色都建不出来（把自己锁在门外）。
    # 检查必须在写入 role_permission 之前，否则一旦写入就已成事实，只能回滚。
    if role_id is not None:
        before_admins = await _admin_user_ids(session)
        after_admins = await _admin_user_ids(session, overrides={role_id: unique_codes})
        if before_admins and not after_admins:
            raise BizError("ROLE_PROTECTED")

    affected_users = select(UserRole.user_id).where(UserRole.role_id == role_id) if role_id else None
    await session.execute(delete(RolePermission).where(RolePermission.role_id == row.id))
    for code in unique_codes:
        session.add(RolePermission(role_id=row.id, code=code))

    # 角色码变化会影响该角色下所有用户的身份，批量抬升 identity_revision。
    if affected_users is not None:
        await session.execute(
            update(User)
            .where(User.id.in_(affected_users))
            .values(identity_revision=User.identity_revision + 1)
        )

    _log(
        session,
        ctx,
        action="org.role.save",
        resource_type="role",
        resource_id=row.id,
        before=None,
        after={"id": row.id, "name": name, "codes": unique_codes, "revision": new_revision},
    )
    return row.id, unique_codes, new_revision


async def delete_role(session: AsyncSession, ctx: UserCtx, *, role_id: int, expected_revision: int) -> bool:
    """F-02.08。被用户或知识授权引用时 409 `ROLE_IN_USE`；裁剪最后一个管理能力 409 `ROLE_PROTECTED`。"""
    row = await Repository(session, Role).get(role_id)
    if row is None:
        raise BizError("NOT_FOUND")
    if row.revision != expected_revision:
        raise BizError("REVISION_CONFLICT")

    if await Repository(session, UserRole).exists({"role_id": role_id}):
        raise BizError("ROLE_IN_USE", "该角色仍分配给用户，请先撤销这些用户的角色")
    if await Repository(session, KnowledgeAclRole).exists({"subject_id": role_id}):
        raise BizError("ROLE_IN_USE", "该角色仍被知识授权引用，请先在相应知识的权限配置中移除")

    remaining = await _admin_user_ids(session, overrides={role_id: ()})
    if not remaining:
        raise BizError("ROLE_PROTECTED")

    before = {"id": row.id, "name": row.name, "revision": row.revision}
    await session.execute(delete(RolePermission).where(RolePermission.role_id == role_id))
    result = await session.execute(
        delete(Role).where(Role.id == role_id, Role.revision == expected_revision)
    )
    if result.rowcount != 1:
        raise BizError("REVISION_CONFLICT")

    _log(
        session,
        ctx,
        action="org.role.delete",
        resource_type="role",
        resource_id=role_id,
        before=before,
        after=None,
    )
    return True


# ---------------------------------------------------------------- 目录选择器（F-02.09）


async def list_directory(
    session: AsyncSession,
    ctx: UserCtx,
    *,
    kind: str,
    q: str = "",
    page: int = 1,
    size: int = 20,
) -> tuple[list[dict[str, Any]], int]:
    """F-02.09：**仅最小选择器数据**（`id`/`label`/`enabled`）。

    完整用户列表走 API-S01；本入口刻意不返回角色/部门详情，避免选择器变成第二个管理台账。
    """
    del ctx
    if kind not in DIRECTORY_KINDS:
        raise BizError("INVALID_ARGUMENT", f"kind 必须是 {list(DIRECTORY_KINDS)} 之一")

    settings = get_settings()
    page = max(1, page)
    size = min(max(1, size), settings.page_size_max)
    needle = f"%{q.strip()}%" if q else None

    if kind == "department":
        conditions = [Department.name.like(needle)] if needle else []
        total = int(await session.scalar(select(func.count()).select_from(Department).where(*conditions)) or 0)
        rows = list(
            (
                await session.execute(
                    select(Department)
                    .where(*conditions)
                    .order_by(Department.id.asc())
                    .offset((page - 1) * size)
                    .limit(size)
                )
            ).scalars()
        )
        return [{"id": r.id, "label": r.name, "enabled": None} for r in rows], total

    if kind == "role":
        conditions = [Role.name.like(needle)] if needle else []
        total = int(await session.scalar(select(func.count()).select_from(Role).where(*conditions)) or 0)
        rows = list(
            (
                await session.execute(
                    select(Role)
                    .where(*conditions)
                    .order_by(Role.id.asc())
                    .offset((page - 1) * size)
                    .limit(size)
                )
            ).scalars()
        )
        return [{"id": r.id, "label": r.name, "enabled": None} for r in rows], total

    conditions = [User.username.like(needle)] if needle else []
    total = int(await session.scalar(select(func.count()).select_from(User).where(*conditions)) or 0)
    rows = list(
        (
            await session.execute(
                select(User)
                .where(*conditions)
                .order_by(User.id.asc())
                .offset((page - 1) * size)
                .limit(size)
            )
        ).scalars()
    )
    # 用户的 `enabled` 有实际含义，直接透出：选择器应能看出"这个账号已停用"。
    return [{"id": r.id, "label": r.username, "enabled": r.enabled} for r in rows], total


__all__ = [
    "ADMIN_CODES",
    "DIRECTORY_KINDS",
    "DIRECTORY_KIND_PERMISSION",
    "create_department",
    "create_user",
    "delete_department",
    "delete_role",
    "list_departments",
    "list_directory",
    "list_permission_codes",
    "list_roles",
    "list_users",
    "save_role",
    "update_department",
    "update_user",
]

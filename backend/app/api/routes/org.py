"""组织、用户与功能权限路由（M02：F-02.01—F-02.09 + API-S01/S02/S03）。

三条路由层纪律（同 `routes/auth.py`）：

1. **只做 DTO 校验 + 服务调用 + 统一响应包装**（FUNCTION-MAP §0 第 4 条）；
2. **功能检查走 H02 依赖**（`dependencies=[Depends(require_permission(...))]`），
   使校验发生在函数体之前——服务里不必重复写，也不会漏掉；
3. **`ctx` 不入 body**：当前身份一律来自 H01。

两处需要单独说明的写法：

- `/directory` 的功能码**由 `kind` 决定**（用户/角色/部门三个面板独立授权，PRD §1.1 第 2 条），
  因此它不能用固定的 `dependencies=[...]`，而是在函数体内按映射校验。
- 删除接口的 `expected_revision` 走 **body**（FUNCTION-MAP §2.2："首版统一 body，
  OpenAPI 中明确映射至 expected_revision，不同时接受冲突值"），不接受 `If-Match` 与 body 并存。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_ctx, require_permission
from app.core.response import BizError, ok
from app.db.repository import UnitOfWork
from app.db.session import get_session
from app.engines.permission import UserCtx
from app.schemas.org import (
    DepartmentCreateRequest,
    DepartmentDeleteRequest,
    DepartmentDeletedOut,
    DepartmentListOut,
    DepartmentOut,
    DepartmentUpdateRequest,
    DirectoryListOut,
    PermissionCodeListOut,
    RoleDeleteRequest,
    RoleDeletedOut,
    RoleOut,
    RoleSaveRequest,
    RoleSavedOut,
    UserCreateRequest,
    UserCreatedOut,
    UserOut,
    UserUpdateRequest,
    UserUpdatedOut,
)
from app.services import auth_svc, org_svc

router = APIRouter(tags=["组织与权限"])


# ---------------------------------------------------------------- 部门


@router.get(
    "/departments",
    status_code=status.HTTP_200_OK,
    summary="部门树（F-02.01）",
    dependencies=[Depends(require_permission("sys:dept"))],
)
async def list_departments(
    ctx: UserCtx = Depends(get_current_ctx),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    """只读端点：不开启显式事务（无需提交，也不应制造写事务）。"""
    rows = await org_svc.list_departments(session, ctx)
    # ★ 显式逐字段组装，不用 `model_validate(row, from_attributes=True)`：
    #   公开 DTO 必须是白名单（DATA-CONTRACTS §1），显式列出字段后，
    #   将来给 Department 加列（如 soft-delete 标记）也不会自动流到客户端。
    return ok(
        DepartmentListOut(
            items=[
                DepartmentOut(id=r.id, parent_id=r.parent_id, name=r.name, revision=r.revision)
                for r in rows
            ]
        )
    )


@router.post(
    "/departments",
    status_code=status.HTTP_201_CREATED,
    summary="创建部门（F-02.02）",
    dependencies=[Depends(require_permission("sys:dept"))],
)
async def create_department(
    payload: DepartmentCreateRequest,
    ctx: UserCtx = Depends(get_current_ctx),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    async with UnitOfWork(session).transaction():
        row = await org_svc.create_department(
            session, ctx, parent_id=payload.parent_id, name=payload.name
        )
    return ok(DepartmentOut(id=row.id, parent_id=row.parent_id, name=row.name, revision=row.revision))


@router.patch(
    "/departments/{dept_id}",
    status_code=status.HTTP_200_OK,
    summary="编辑或移动部门（F-02.03）",
    dependencies=[Depends(require_permission("sys:dept"))],
)
async def update_department(
    dept_id: int,
    payload: DepartmentUpdateRequest,
    ctx: UserCtx = Depends(get_current_ctx),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    async with UnitOfWork(session).transaction():
        row = await org_svc.update_department(
            session,
            ctx,
            dept_id=dept_id,
            parent_id=payload.parent_id,
            name=payload.name,
            expected_revision=payload.expected_revision,
        )
    return ok({"id": row.id, "revision": row.revision})


@router.delete(
    "/departments/{dept_id}",
    status_code=status.HTTP_200_OK,
    summary="删除部门（F-02.04）",
    dependencies=[Depends(require_permission("sys:dept"))],
)
async def delete_department(
    dept_id: int,
    payload: DepartmentDeleteRequest,
    ctx: UserCtx = Depends(get_current_ctx),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    async with UnitOfWork(session).transaction():
        deleted = await org_svc.delete_department(
            session, ctx, dept_id=dept_id, expected_revision=payload.expected_revision
        )
    return ok(DepartmentDeletedOut(deleted=deleted))


# ---------------------------------------------------------------- 用户


@router.get(
    "/users",
    status_code=status.HTTP_200_OK,
    summary="用户分页（API-S01）",
    dependencies=[Depends(require_permission("sys:user"))],
)
async def list_users(
    q: str = Query(default="", max_length=200),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    enabled: bool | None = Query(default=None),
    ctx: UserCtx = Depends(get_current_ctx),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    items, total = await org_svc.list_users(session, ctx, q=q, page=page, size=size, enabled=enabled)
    return ok(
        {
            "items": [UserOut.model_validate(i) for i in items],
            "total": total,
        }
    )


@router.post(
    "/users",
    status_code=status.HTTP_201_CREATED,
    summary="创建用户（F-02.05）",
    dependencies=[Depends(require_permission("sys:user"))],
)
async def create_user(
    payload: UserCreateRequest,
    ctx: UserCtx = Depends(get_current_ctx),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    async with UnitOfWork(session).transaction():
        row = await org_svc.create_user(
            session,
            ctx,
            username=payload.username,
            # 明文只在这一次调用里出现，交给 hash_password 后即不再持有
            # （`SecretStr` 的 `get_secret_value()` 是唯一的取值口）。
            password=payload.password.get_secret_value(),
            dept_id=payload.dept_id,
            role_ids=payload.role_ids,
        )
    return ok(UserCreatedOut(id=row.id, username=row.username, revision=row.revision))


@router.patch(
    "/users/{user_id}",
    status_code=status.HTTP_200_OK,
    summary="编辑用户与启停（F-02.06）",
    dependencies=[Depends(require_permission("sys:user"))],
)
async def update_user(
    user_id: int,
    payload: UserUpdateRequest,
    ctx: UserCtx = Depends(get_current_ctx),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    async with UnitOfWork(session).transaction():
        row = await org_svc.update_user(
            session,
            ctx,
            user_id=user_id,
            dept_id=payload.dept_id,
            role_ids=payload.role_ids,
            enabled=payload.enabled,
            expected_revision=payload.expected_revision,
        )
    return ok(UserUpdatedOut(id=row.id, enabled=row.enabled, revision=row.revision))


# ---------------------------------------------------------------- 角色与权限码


@router.get(
    "/roles",
    status_code=status.HTTP_200_OK,
    summary="角色分页（API-S02）",
    dependencies=[Depends(require_permission("sys:role"))],
)
async def list_roles(
    q: str = Query(default="", max_length=200),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    ctx: UserCtx = Depends(get_current_ctx),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    items, total = await org_svc.list_roles(session, ctx, q=q, page=page, size=size)
    return ok({"items": [RoleOut.model_validate(i) for i in items], "total": total})


@router.put(
    "/roles",
    status_code=status.HTTP_200_OK,
    summary="创建或保存角色（F-02.07）",
    dependencies=[Depends(require_permission("sys:role"))],
)
async def save_role(
    payload: RoleSaveRequest,
    ctx: UserCtx = Depends(get_current_ctx),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    """用 PUT 而非 POST：`id=null` 新建、非空修改，是同一资源的 upsert 语义（契约如此）。"""
    async with UnitOfWork(session).transaction():
        role_id, codes, revision = await org_svc.save_role(
            session,
            ctx,
            role_id=payload.id,
            name=payload.name,
            codes=payload.codes,
            expected_revision=payload.expected_revision,
        )
    return ok(RoleSavedOut(id=role_id, codes=codes, revision=revision))


@router.delete(
    "/roles/{role_id}",
    status_code=status.HTTP_200_OK,
    summary="删除角色（F-02.08）",
    dependencies=[Depends(require_permission("sys:role"))],
)
async def delete_role(
    role_id: int,
    payload: RoleDeleteRequest,
    ctx: UserCtx = Depends(get_current_ctx),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    async with UnitOfWork(session).transaction():
        deleted = await org_svc.delete_role(
            session, ctx, role_id=role_id, expected_revision=payload.expected_revision
        )
    return ok(RoleDeletedOut(deleted=deleted))


@router.get(
    "/permission-codes",
    status_code=status.HTTP_200_OK,
    summary="权限码清单（API-S03）",
    dependencies=[Depends(require_permission("sys:role"))],
)
async def list_permission_codes(
    ctx: UserCtx = Depends(get_current_ctx),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    """后端固定 14 码，前端据此组树（API-CONTRACTS §5）。"""
    items = await org_svc.list_permission_codes(session, ctx)
    return ok(PermissionCodeListOut.model_validate({"items": items}))


@router.get(
    "/directory",
    status_code=status.HTTP_200_OK,
    summary="目录选择器（F-02.09）",
)
async def list_directory(
    kind: str = Query(description="user | role | department"),
    q: str = Query(default="", max_length=200),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    ctx: UserCtx = Depends(get_current_ctx),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    """功能码随 `kind` 变化，因此 H02 在函数体内执行（见模块 docstring）。"""
    code = org_svc.DIRECTORY_KIND_PERMISSION.get(kind)
    if code is None:
        raise BizError("INVALID_ARGUMENT", f"kind 必须是 {list(org_svc.DIRECTORY_KINDS)} 之一")
    auth_svc.require_permission(ctx, code)

    items, total = await org_svc.list_directory(session, ctx, kind=kind, q=q, page=page, size=size)
    return ok(
        DirectoryListOut.model_validate(
            {"items": items, "total": total}
        )
    )

"""知识生命周期与四维权限路由（M04：F-04.01—F-04.09 + API-S04）。

路由层纪律与 `routes/org.py` 一致：
1. 只做 DTO 校验 + 服务调用 + 统一响应包装；
2. 功能检查走 H02 依赖（`dependencies=[Depends(require_permission(...))]`），
   服务里不重复写；
3. **数据读权在服务层**（H04）：无权 → 404 防枚举，路由层不做；
4. 删除/启停的 `expected_revision` 取 body（首版统一 body，不用 If-Match）。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_ctx, require_permission
from app.core.response import ok
from app.db.repository import UnitOfWork
from app.db.session import get_session
from app.engines.permission import UserCtx
from app.schemas.knowledge import (
    ChunkMutationRequest,
    DeleteUnitRequest,
    SetEnabledRequest,
    UpdateAclRequest,
    UpdateMetadataRequest,
)
from app.services import knowledge_svc

router = APIRouter(tags=["knowledge"])


@router.get("/knowledge-units", dependencies=[Depends(require_permission("kb:view"))])
async def list_units(
    session: Annotated[AsyncSession, Depends(get_session)],
    ctx: Annotated[UserCtx, Depends(get_current_ctx)],
    q: str = "",
    category: str | None = None,
    enabled: bool | None = None,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
):
    """F-04.01：台账查询。"""
    async with UnitOfWork(session).transaction():
        data = await knowledge_svc.list_units(
            session, ctx, q=q, category=category, enabled=enabled, page=page, size=size
        )
    return ok(data)


@router.patch("/knowledge-units/{unit_id}", dependencies=[Depends(require_permission("kb:edit"))])
async def update_metadata(
    unit_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    ctx: Annotated[UserCtx, Depends(get_current_ctx)],
    body: UpdateMetadataRequest,
):
    """F-04.02：修改标题与分类。"""
    async with UnitOfWork(session).transaction():
        data = await knowledge_svc.update_metadata(
            session, ctx, unit_id=unit_id, title=body.title, category=body.category,
            expected_revision=body.expected_revision,
        )
    return ok(data)


@router.get(
    "/knowledge-units/{unit_id}/chunks",
    dependencies=[Depends(require_permission("kb:view"))],
)
async def read_chunks(
    unit_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    ctx: Annotated[UserCtx, Depends(get_current_ctx)],
    version: int | None = None,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
):
    """F-04.03：查看正文切片（数据读权在服务层，无权 404）。"""
    async with UnitOfWork(session).transaction():
        data = await knowledge_svc.read_chunks(
            session, ctx, unit_id=unit_id, version=version, page=page, size=size
        )
    return ok(data)


@router.post(
    "/knowledge-units/{unit_id}/versions",
    dependencies=[Depends(require_permission("kb:edit"))],
)
async def replace_document(
    unit_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    ctx: Annotated[UserCtx, Depends(get_current_ctx)],
    file: Annotated[UploadFile, File()],
    expected_revision: Annotated[int, Form()],
):
    """F-04.04：替换文档（multipart：file + expected_revision）。"""
    content = await file.read()
    async with UnitOfWork(session).transaction():
        data = await knowledge_svc.replace_document(
            session, ctx, unit_id=unit_id, filename=file.filename or "",
            content=content, expected_revision=expected_revision,
        )
    return ok(data)


@router.post(
    "/knowledge-units/{unit_id}/chunk-mutations",
    dependencies=[Depends(require_permission("kb:edit"))],
)
async def mutate_chunks(
    unit_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    ctx: Annotated[UserCtx, Depends(get_current_ctx)],
    body: ChunkMutationRequest,
):
    """F-04.05：切片编辑/拆分/删除。"""
    async with UnitOfWork(session).transaction():
        data = await knowledge_svc.mutate_chunks(
            session, ctx, unit_id=unit_id, chunk_id=body.chunk_id, action=body.action,
            text=body.text, split_offset=body.split_offset,
            expected_revision=body.expected_revision,
        )
    return ok(data)


@router.put(
    "/knowledge-units/{unit_id}/enabled",
    dependencies=[Depends(require_permission("kb:edit"))],
)
async def set_enabled(
    unit_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    ctx: Annotated[UserCtx, Depends(get_current_ctx)],
    body: SetEnabledRequest,
):
    """F-04.06：知识启停。"""
    async with UnitOfWork(session).transaction():
        data = await knowledge_svc.set_enabled(
            session, ctx, unit_id=unit_id, enabled=body.enabled,
            expected_revision=body.expected_revision,
        )
    return ok(data)


@router.delete("/knowledge-units/{unit_id}", dependencies=[Depends(require_permission("kb:delete"))])
async def delete_unit(
    unit_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    ctx: Annotated[UserCtx, Depends(get_current_ctx)],
    body: DeleteUnitRequest,
):
    """F-04.07：知识删除（墓碑 + 清理任务同事务）。"""
    async with UnitOfWork(session).transaction():
        data = await knowledge_svc.delete_unit(
            session, ctx, unit_id=unit_id, expected_revision=body.expected_revision
        )
    return ok(data)


@router.get(
    "/knowledge-units/{unit_id}/acl",
    dependencies=[Depends(require_permission("kb:perm"))],
)
async def get_acl(
    unit_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    ctx: Annotated[UserCtx, Depends(get_current_ctx)],
):
    """API-S04：权限弹窗回填。"""
    async with UnitOfWork(session).transaction():
        data = await knowledge_svc.get_acl(session, ctx, unit_id=unit_id)
    return ok(data)


@router.put(
    "/knowledge-units/{unit_id}/acl",
    dependencies=[Depends(require_permission("kb:perm"))],
)
async def update_acl(
    unit_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    ctx: Annotated[UserCtx, Depends(get_current_ctx)],
    body: UpdateAclRequest,
):
    """F-04.08：四维授权配置。"""
    async with UnitOfWork(session).transaction():
        data = await knowledge_svc.update_acl(
            session, ctx, unit_id=unit_id, is_global=body.global_, depts=body.depts,
            roles=body.roles, users=body.users, expected_revision=body.expected_revision,
        )
    return ok(data)


@router.get("/acl-entities", dependencies=[Depends(require_permission("kb:perm"))])
async def list_acl_entities(
    session: Annotated[AsyncSession, Depends(get_session)],
    ctx: Annotated[UserCtx, Depends(get_current_ctx)],
    kind: str = Query(..., pattern="^(dept|role|user)$"),
    q: str = "",
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
):
    """F-04.09：权限弹窗实体选择。"""
    async with UnitOfWork(session).transaction():
        data = await knowledge_svc.list_acl_entities(
            session, ctx, kind=kind, q=q, page=page, size=size
        )
    return ok(data)

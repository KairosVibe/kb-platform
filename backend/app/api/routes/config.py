"""模型配置与运行控制路由（M09：F-09.01—F-09.04）。"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_ctx, require_permission
from app.core.response import ok
from app.db.base import utcnow
from app.db.repository import UnitOfWork
from app.db.session import get_session
from app.engines.permission import UserCtx
from app.services import config_svc

router = APIRouter(tags=["config"])


class ConfigUpdateRequest(BaseModel):
    patch: dict[str, Any]
    expected_revision: int


@router.get("/model-config", dependencies=[Depends(require_permission("sys:model"))])
async def read_config(
    session: Annotated[AsyncSession, Depends(get_session)],
    ctx: Annotated[UserCtx, Depends(get_current_ctx)],
):
    """F-09.01：读当前配置快照（密钥不回读）。"""
    async with UnitOfWork(session).transaction():
        data = await config_svc.read_config(session, ctx)
    return ok(data)


@router.patch("/model-config", dependencies=[Depends(require_permission("sys:model"))])
async def update_config(
    session: Annotated[AsyncSession, Depends(get_session)],
    ctx: Annotated[UserCtx, Depends(get_current_ctx)],
    body: ConfigUpdateRequest,
):
    """F-09.02：保存配置（白名单校验；embedding 冻结 409）。"""
    async with UnitOfWork(session).transaction():
        data = await config_svc.update_config(
            session, ctx, patch=body.patch, expected_revision=body.expected_revision
        )
    return ok(data)


@router.post("/model-config/probe", dependencies=[Depends(require_permission("sys:model"))])
async def probe_provider(
    session: Annotated[AsyncSession, Depends(get_session)],
    ctx: Annotated[UserCtx, Depends(get_current_ctx)],
    provider: str = "embedding",
):
    """F-09.03：连通探测（最小调用，有成本）。"""
    async with UnitOfWork(session).transaction():
        data = await config_svc.probe_provider(session, ctx, provider=provider)
    return ok(data)

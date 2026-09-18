"""数据看板路由（M08：F-08.02/F-08.03，dashboard:view）。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_ctx, require_permission
from app.core.response import ok
from app.db.repository import UnitOfWork
from app.db.session import get_session
from app.engines.permission import UserCtx
from app.services import dashboard_svc, metrics_svc

router = APIRouter(tags=["dashboard"])


@router.get("/dashboard/summary", dependencies=[Depends(require_permission("dashboard:view"))])
async def get_summary(
    session: Annotated[AsyncSession, Depends(get_session)],
    ctx: Annotated[UserCtx, Depends(get_current_ctx)],
    range: str = Query("day", pattern="^(day|week)$"),
    anchor_date: str = Query(...),
):
    """F-08.02：指标摘要（上海日/周归属）。"""
    async with UnitOfWork(session).transaction():
        data = await dashboard_svc.get_summary(session, ctx, rng=range, anchor_date=anchor_date)
    return ok(data)


@router.get("/dashboard/charts", dependencies=[Depends(require_permission("dashboard:view"))])
async def get_charts(
    session: Annotated[AsyncSession, Depends(get_session)],
    ctx: Annotated[UserCtx, Depends(get_current_ctx)],
    range: str = Query("day", pattern="^(day|week)$"),
    anchor_date: str = Query(...),
    top_n: int = Query(5, ge=1, le=20),
):
    """F-08.03：六类图表（服务端上海分桶，前端不二次聚合）。"""
    async with UnitOfWork(session).transaction():
        data = await dashboard_svc.get_charts(
            session, ctx, rng=range, anchor_date=anchor_date, top_n=top_n
        )
    return ok(data)


@router.get("/audit", dependencies=[Depends(require_permission("dashboard:view"))])
async def search_audit(
    session: Annotated[AsyncSession, Depends(get_session)],
    ctx: Annotated[UserCtx, Depends(get_current_ctx)],
    request_id: int | None = Query(None),
    action: str | None = Query(None),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
):
    """F-08.04：审计搜索（request_id 是问答业务 ID；读取本身留痕）。"""
    async with UnitOfWork(session).transaction():
        data = await metrics_svc.search_audit(
            session, ctx, request_id=request_id, action=action, page=page, size=size
        )
    return ok(data)

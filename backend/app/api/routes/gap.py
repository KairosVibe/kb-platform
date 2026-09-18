"""知识缺口闭环路由（M07：F-07.02/03/04 + API-S06；F-07.01 为内部消费者无路由）。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_ctx, require_permission
from app.core.response import ok
from app.db.repository import UnitOfWork
from app.db.session import get_session
from app.engines.permission import UserCtx
from app.services import gap_svc

router = APIRouter(tags=["gaps"])


class SupplementSourceRequest(BaseModel):
    """API-S06：绑定来源。"""

    unit_id: int
    expected_revision: int


@router.get("/knowledge-gaps", dependencies=[Depends(require_permission("gap:handle"))])
async def list_gaps(
    session: Annotated[AsyncSession, Depends(get_session)],
    ctx: Annotated[UserCtx, Depends(get_current_ctx)],
    status: str | None = None,
    dept_id: int | None = None,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
):
    """F-07.02：缺口查询。"""
    async with UnitOfWork(session).transaction():
        data = await gap_svc.list_gaps(
            session, ctx, status=status, dept_id=dept_id, page=page, size=size
        )
    return ok(data)


@router.post(
    "/knowledge-gaps/{gap_id}/convert",
    dependencies=[Depends(require_permission("gap:handle"))],
)
async def convert_gap(
    gap_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    ctx: Annotated[UserCtx, Depends(get_current_ctx)],
):
    """F-07.03：转建补充任务（幂等：重复转建返回既有任务）。"""
    async with UnitOfWork(session).transaction():
        data = await gap_svc.convert_gap(
            session, ctx, gap_id=gap_id, client_action_id=f"gap-convert-{gap_id}"
        )
    return ok(data)


@router.put(
    "/supplement-tasks/{task_id}/source",
    dependencies=[Depends(require_permission("gap:handle"))],
)
async def bind_source(
    task_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    ctx: Annotated[UserCtx, Depends(get_current_ctx)],
    body: SupplementSourceRequest,
):
    """API-S06：绑定来源（另需来源读权；禁止绑定不存在/已删除单元）。"""
    async with UnitOfWork(session).transaction():
        data = await gap_svc.bind_source(
            session, ctx, task_id=task_id, unit_id=body.unit_id,
            expected_revision=body.expected_revision,
        )
    return ok(data)


@router.post(
    "/knowledge-gaps/{gap_id}/verify",
    dependencies=[Depends(require_permission("gap:handle"))],
)
async def verify_gap(
    gap_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    ctx: Annotated[UserCtx, Depends(get_current_ctx)],
):
    """F-07.04：回放验证（原提问用户当前身份；失败保持 processing）。"""
    async with UnitOfWork(session).transaction():
        data = await gap_svc.verify_gap(session, ctx, gap_id=gap_id)
    return ok(data)


@router.get("/supplement-tasks", dependencies=[Depends(require_permission("gap:handle"))])
async def list_supplement_tasks(
    session: Annotated[AsyncSession, Depends(get_session)],
    ctx: Annotated[UserCtx, Depends(get_current_ctx)],
    gap_id: int | None = Query(None),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
):
    """API-S05：补档任务列表（补档绑定页轮询）。"""
    async with UnitOfWork(session).transaction():
        data = await gap_svc.list_supplement_tasks(session, ctx, gap_id=gap_id, page=page, size=size)
    return ok(data)

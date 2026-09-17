"""FAQ 沉淀审核与缓存路由（M06：F-06.01—F-06.05；F-06.06/07 为内部函数）。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_ctx, require_permission
from app.core.response import ok
from app.db.repository import UnitOfWork
from app.db.session import get_session
from app.engines.permission import UserCtx
from app.services import faq_svc

router = APIRouter(tags=["faq"])


class MiningRunRequest(BaseModel):
    pipeline_version: str = Field(min_length=1, max_length=64)


class FaqEditRequest(BaseModel):
    question: str = Field(min_length=1)
    answer: str = Field(min_length=1)
    source_ids: list[int]
    expected_revision: int


class FaqPublishRequest(BaseModel):
    expected_revision: int


class FaqStatusRequest(BaseModel):
    action: str = Field(pattern="^(reject|offline|resubmit)$")
    reason: str = Field(min_length=1, max_length=1000)
    expected_revision: int


class FaqCacheToggleRequest(BaseModel):
    enabled: bool
    expected_revision: int


@router.post("/mining/runs", dependencies=[Depends(require_permission("faq:review"))], status_code=202)
async def create_mining_run(
    session: Annotated[AsyncSession, Depends(get_session)],
    ctx: Annotated[UserCtx, Depends(get_current_ctx)],
    body: MiningRunRequest,
):
    """F-06.01：登记挖掘运行（202 = 已登记；执行为后台任务）。"""
    async with UnitOfWork(session).transaction():
        data = await faq_svc.create_mining_run(session, ctx, pipeline_version=body.pipeline_version)
    faq_svc.spawn_mining(int(data["run_id"]))
    return ok(data)


@router.get("/mining/runs/{run_id}", dependencies=[Depends(require_permission("faq:review"))])
async def get_mining_run(
    run_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    ctx: Annotated[UserCtx, Depends(get_current_ctx)],
):
    """API-S07：运行统计（仅统计与安全错误）。"""
    async with UnitOfWork(session).transaction():
        data = await faq_svc.get_mining_run(ctx, session, run_id)
    return ok(data)


@router.patch("/faqs/{faq_id}/candidate", dependencies=[Depends(require_permission("faq:review"))])
async def edit_candidate(
    faq_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    ctx: Annotated[UserCtx, Depends(get_current_ctx)],
    body: FaqEditRequest,
):
    """F-06.02：候选编辑（全部来源读权）。"""
    async with UnitOfWork(session).transaction():
        data = await faq_svc.edit_candidate(
            session, ctx, faq_id=faq_id, question=body.question, answer=body.answer,
            source_ids=body.source_ids, expected_revision=body.expected_revision,
        )
    return ok(data)


@router.post("/faqs/{faq_id}/publish", dependencies=[Depends(require_permission("faq:publish"))])
async def publish(
    faq_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    ctx: Annotated[UserCtx, Depends(get_current_ctx)],
    body: FaqPublishRequest,
):
    """F-06.03：发布（全部来源当前版本权限复核）。"""
    async with UnitOfWork(session).transaction():
        data = await faq_svc.publish(session, ctx, faq_id=faq_id, expected_revision=body.expected_revision)
    return ok(data)


@router.post("/faqs/{faq_id}/status", dependencies=[Depends(require_permission("faq:review"))])
async def change_status(
    faq_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    ctx: Annotated[UserCtx, Depends(get_current_ctx)],
    body: FaqStatusRequest,
):
    """F-06.04：驳回/下线/重新提交。"""
    async with UnitOfWork(session).transaction():
        data = await faq_svc.change_status(
            session, ctx, faq_id=faq_id, action=body.action, reason=body.reason,
            expected_revision=body.expected_revision,
        )
    return ok(data)


@router.put(
    "/faqs/{faq_id}/cache-enabled",
    dependencies=[Depends(require_permission("faq:publish"))],
)
async def set_cache_enabled(
    faq_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    ctx: Annotated[UserCtx, Depends(get_current_ctx)],
    body: FaqCacheToggleRequest,
):
    """F-06.05：缓存启停（审核状态独立）。"""
    async with UnitOfWork(session).transaction():
        data = await faq_svc.set_cache_enabled(
            session, ctx, faq_id=faq_id, enabled=body.enabled, expected_revision=body.expected_revision
        )
    return ok(data)

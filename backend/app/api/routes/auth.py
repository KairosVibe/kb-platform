"""认证路由（M01：F-01.01—F-01.04）。

对应 FUNCTION-MAP §2.2（薄路由契约）与 API-CONTRACTS §1（统一响应包 + 错误码）。

三条路由层纪律：

1. **只做 DTO 校验 + 服务调用 + 统一响应包装**（§0 第 4 条）。任何业务判断都不写在这里，
   否则同一个规则会在路由与服务两处各有一份。
2. **事务边界显式**：写操作用 `UnitOfWork.transaction()` 包住（§0 第 5 条"写入与
   operation_log 短事务一致"）。不依赖 session 自动提交——SQLAlchemy 的 autobegin
   会让"看起来提交了"变成"其实被回滚"（见 `UnitOfWork.transaction` 的说明）。
3. **`ctx` 不入 body**：当前身份一律来自 H01 依赖，不接受客户端提交（§2.2 原文）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import client_ip, get_current_ctx
from app.core.response import ok
from app.db.repository import UnitOfWork
from app.db.session import get_session
from app.engines.permission import UserCtx
from app.schemas.auth import (
    LoginRequest,
    LogoutRequest,
    MeResponse,
    RefreshRequest,
    TokenPair,
)
from app.services import auth_svc

router = APIRouter(prefix="/auth", tags=["认证"])


@router.post("/login", status_code=status.HTTP_200_OK, summary="登录（F-01.01）")
async def login(
    payload: LoginRequest,
    request_ip: str = Depends(client_ip),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    """公开端点：**不调用 H01/H02**。

    登录前没有身份，若套用功能鉴权会形成"必须先登录才能登录"的死结
    （审计里曾把这条列为文档缺陷）。限流在服务层按 IP 执行，返回 429。
    """
    async with UnitOfWork(session).transaction():
        access, refresh, expires_in = await auth_svc.login(
            session,
            username=payload.username,
            password=payload.password.get_secret_value(),
            ip=request_ip,
        )
    return ok(TokenPair(access_token=access, refresh_token=refresh, expires_in=expires_in))


@router.post("/refresh", status_code=status.HTTP_200_OK, summary="刷新令牌（F-01.02）")
async def rotate_refresh(
    payload: RefreshRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    """公开端点：凭 refresh token 自身即可，不需要 access token（它可能已过期）。"""
    async with UnitOfWork(session).transaction():
        access, refresh, expires_in = await auth_svc.rotate_refresh(
            session, refresh_token=payload.refresh_token.get_secret_value()
        )
    return ok(TokenPair(access_token=access, refresh_token=refresh, expires_in=expires_in))


@router.post("/logout", status_code=status.HTTP_200_OK, summary="退出（F-01.03）")
async def logout(
    payload: LogoutRequest,
    ctx: UserCtx = Depends(get_current_ctx),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    """需要登录态：`ctx` 来自 H01，body 只带 refresh token（用于定位要撤销的会话）。"""
    async with UnitOfWork(session).transaction():
        revoked = await auth_svc.logout(
            session, ctx=ctx, refresh_token=payload.refresh_token.get_secret_value()
        )
    return ok({"revoked": revoked})


@router.get("/me", status_code=status.HTTP_200_OK, summary="当前身份（F-01.04）")
async def get_me(
    ctx: UserCtx = Depends(get_current_ctx),
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    """只读端点：不开启显式事务（无需提交，也不应制造写事务）。"""
    view = await auth_svc.get_me(session, ctx=ctx)
    return ok(MeResponse.model_validate(view))

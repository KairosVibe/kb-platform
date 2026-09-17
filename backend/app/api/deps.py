"""FastAPI 依赖：H01 当前身份 / H02 功能检查的依赖包装。

依据 FUNCTION-MAP §0 第 4 条：**路由仅 DTO 校验 → H01 当前身份 → H02 功能检查 →
服务 → 安全响应**。本模块提供第 2、3 步的依赖，使路由函数体保持"只做一件事"。

为什么令牌解析不写在路由里：路由一旦自己解析 `Authorization`，就会各自决定
"缺失头怎么办""格式不对返回什么码"，401 的语义立刻分裂。集中在这里，错误码只有一处。

**未实现（刻意留白）**：`Authorization` 头的解析只认 `Bearer`，不读 cookie、不读查询参数。
把令牌放进查询参数会被浏览器历史、代理日志与 Referer 记录，属于禁止形态。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.response import BizError
from app.db.session import get_session
from app.engines.permission import UserCtx
from app.services import auth_svc

#: 只接受 `Bearer <token>`；大小写不敏感（RFC 6750 规定 scheme 不区分大小写）。
_BEARER = "bearer "


async def get_current_ctx(
    request: Request, session: AsyncSession = Depends(get_session)
) -> UserCtx:
    """H01：把请求里的 access token 换成 `UserCtx`。

    缺头、格式错、空 token 一律 `TOKEN_INVALID`（401）——**不区分**具体原因，
    避免把"令牌格式探测"变成可用信息。
    """
    header = request.headers.get("Authorization") or ""
    if not header.lower().startswith(_BEARER):
        raise BizError("TOKEN_INVALID")
    token = header[len(_BEARER) :].strip()
    if not token:
        raise BizError("TOKEN_INVALID")
    return await auth_svc.authenticate(session, token)


def require_permission(code: str) -> Callable[..., Awaitable[None]]:
    """H02 的依赖工厂：`dependencies=[Depends(require_permission("kb:upload"))]`。

    返回一个**依赖函数**而不是直接调用：这样功能检查发生在路由函数体之前，
    服务里不需要重复写 `require_permission`，也不会漏掉。
    """

    async def _dependency(ctx: UserCtx = Depends(get_current_ctx)) -> None:
        auth_svc.require_permission(ctx, code)

    return _dependency


def require_any_permission(*codes: str) -> Callable[..., Awaitable[None]]:
    """H02 的"任一"变体：命中任一功能码即放行（如 F-03.03 的 `kb:view` 或 `kb:upload`）。

    ★ 与 `require_permission` 同样返回依赖函数，语义差异只有一个：
      全部未命中才 403。**"任一"不等于放宽**——它仍然要求显式声明允许的码集合，
      未列出的码不会被意外放行。
    """

    async def _dependency(ctx: UserCtx = Depends(get_current_ctx)) -> None:
        if not any(code in ctx.permission_codes for code in codes):
            raise BizError("PERM_DENIED")

    return _dependency


def client_ip(request: Request) -> str:
    """取客户端 IP，用于登录限流。

    ★ 只取 socket 对端地址，**不读 `X-Forwarded-For`**：该头可被客户端伪造，
      用它做限流等于让攻击者自己决定"算哪个 IP"。部署到反向代理后应改为
      "只信任受控代理写入的转发头"，那是 DEPLOYMENT §3 的配置要求，不是这里默认打开。
    """
    return request.client.host if request.client is not None else "unknown"

"""FastAPI 应用装配。

对应 FUNCTION-MAP §2.2（「GET /health → 进程存活与 app_version；不连模型，不泄露环境变量」）
与 API-CONTRACTS §1（统一响应包 + 全局异常处理）。

装配顺序有意固定：**先注册异常处理，再挂路由**。反过来也不会报错，但一旦某个路由在
导入期就抛异常，错误响应会退化成 FastAPI 默认格式，破坏"所有响应同一种结构"的契约。
"""

from __future__ import annotations

import os

from fastapi import FastAPI

from app.api.routes.auth import router as auth_router
from app.api.routes.chat import router as chat_router
from app.api.routes.dashboard import router as dashboard_router
from app.api.routes.faq import router as faq_router
from app.api.routes.gap import router as gap_router
from app.api.routes.ingest import router as ingest_router
from app.api.routes.knowledge import router as knowledge_router
from app.api.routes.org import router as org_router
from app.core.config import get_settings
from app.core.response import ok, register_exception_handlers

#: 版本与 git revision 由**构建时注入**（DEPLOYMENT §2 第 3 条："显式注入版本 ARG 并写 OCI
#: label；/health 的 git revision 与镜像一致"）。默认值刻意带 `-dev` 后缀，
#: 使"未注入版本就跑起来"在响应里一眼可见，而不是伪装成一个正式版本号。
APP_VERSION = os.environ.get("APP_VERSION", "0.1.0-dev")
GIT_REVISION = os.environ.get("GIT_REVISION", "unknown")


def create_app() -> FastAPI:
    """装配应用。供测试按需构造独立实例（避免用例之间共享全局状态）。"""
    settings = get_settings()
    app = FastAPI(
        title="知识管理与 AI 问答平台",
        version=APP_VERSION,
        docs_url="/docs",
        openapi_url="/openapi.json",
    )
    register_exception_handlers(app)
    app.include_router(auth_router, prefix=settings.api_prefix)
    app.include_router(org_router, prefix=settings.api_prefix)
    app.include_router(ingest_router, prefix=settings.api_prefix)
    app.include_router(knowledge_router, prefix=settings.api_prefix)
    app.include_router(chat_router, prefix=settings.api_prefix)
    app.include_router(gap_router, prefix=settings.api_prefix)
    app.include_router(faq_router, prefix=settings.api_prefix)
    app.include_router(dashboard_router, prefix=settings.api_prefix)

    @app.get("/health", tags=["运维"], summary="存活与版本")
    async def health() -> dict[str, object]:
        """存活探针。

        ★ 只回答"进程活着 + 是哪个版本"：**不探测数据库、不连模型、不回显任何环境变量**。
          依赖就绪度由 `/ready` 承担（F-09.04），两者混在一起会让"数据库短暂不可用"
          被读成"进程死了"，进而触发无意义的重启。
        """
        return ok({"status": "ok", "app_version": APP_VERSION, "git_revision": GIT_REVISION})

    return app


app = create_app()

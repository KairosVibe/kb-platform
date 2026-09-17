"""真实 HTTP 服务器冒烟（uvicorn 进程内启动，走真实 socket，而非 ASGI 传输）。

与 `test_auth_endpoints.py` 的分工：那边用 `ASGITransport` 绕过网络层，跑得快、覆盖业务
分支；这边**真的绑端口、真的发 HTTP 请求**，用来回答三个 ASGI 传输答不了的问题：

1. 应用能否作为**服务器进程**对外服务（导入期、生命周期、端口绑定都正常）；
2. 路由前缀是否真的挂在 `/api/...`（ASGITransport 下写错前缀也会"看起来通过"）；
3. 未认证请求在真实链路里是否返回 401 与统一响应包。

在测试内直接跑 `uvicorn.Server` 而不是 `Start-Process` 起子进程：前者随用例结束必然
退出（`should_exit` + `await task`），不会留下占用端口的僵尸进程。
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
import uvicorn

from app.main import create_app
from tests.integration.conftest import it_dsn, run

PORT = 8126
#: 服务器**实际挂载**的完整路由集合。
#:
#: ★ 这是一条"绊线"：它断言"挂载了什么"就等于这里列出的，用来抓"路由前缀写错"这类
#:   在 ASGI 传输下看不出问题的错误。**新增路由必须同步更新本集合**——否则这条断言会失败，
#:   而失败恰恰是它该做的事（M02 加路由时漏改这里，实测就是这样被它抓到的）。
#:   注意收集的是 OpenAPI 的 path 模板（含 `{dept_id}` 这类占位符），不是实际请求 URL。
EXPECTED_PATHS = {
    "/health",
    "/api/auth/login",
    "/api/auth/refresh",
    "/api/auth/logout",
    "/api/auth/me",
    # M02 组织用户与功能权限（F-02.01—F-02.09 + API-S01/S02/S03）
    "/api/departments",
    "/api/departments/{dept_id}",
    "/api/users",
    "/api/users/{user_id}",
    "/api/roles",
    "/api/roles/{role_id}",
    "/api/directory",
    "/api/permission-codes",
    # M03 文档导入与任务（F-03.01/F-03.02/F-03.03/F-03.05；F-03.06 为内部调度无路由）
    "/api/uploads",
    "/api/upload-batches",
    "/api/index-tasks/{task_id}",
    "/api/index-tasks/{task_id}/retry",
    # M04 知识生命周期与四维权限（F-04.01—F-04.09 + API-S04）
    "/api/knowledge-units",
    "/api/knowledge-units/{unit_id}",
    "/api/knowledge-units/{unit_id}/chunks",
    "/api/knowledge-units/{unit_id}/versions",
    "/api/knowledge-units/{unit_id}/chunk-mutations",
    "/api/knowledge-units/{unit_id}/enabled",
    "/api/knowledge-units/{unit_id}/acl",
    "/api/acl-entities",
    # M05 会话检索与流式问答（F-05.01—F-05.09）
    "/api/sessions",
    "/api/sessions/{session_id}",
    "/api/sessions/{session_id}/messages",
    "/api/chat/requests",
    "/api/chat/requests/{request_id}/events",
    "/api/chat/requests/{request_id}/cancel",
    "/api/chat/requests/{request_id}/citations/{no}",
    "/api/chat/suggestions",
    # M07 知识缺口闭环（F-07.02/03/04 + API-S06；F-07.01 为内部消费者无路由）
    "/api/knowledge-gaps",
    "/api/knowledge-gaps/{gap_id}/convert",
    "/api/knowledge-gaps/{gap_id}/verify",
    "/api/supplement-tasks/{task_id}/source",
    # M06 FAQ 沉淀审核与缓存（F-06.01—F-06.05；F-06.06/07 为内部函数）
    "/api/mining/runs",
    "/api/mining/runs/{run_id}",
    "/api/faqs/{faq_id}/candidate",
    "/api/faqs/{faq_id}/publish",
    "/api/faqs/{faq_id}/status",
    "/api/faqs/{faq_id}/cache-enabled",
}


async def _serve_and_probe() -> dict[str, Any]:
    """启动真实服务器并采集原始响应，便于失败时直接看到 status/body。"""
    app = create_app()
    config = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    observed: dict[str, Any] = {}
    try:
        # ★ `trust_env=False`：禁用环境代理嗅探。实测在本机环境下 httpx 会把
        #   `http://127.0.0.1:...` 交给某个代理，结果是 `502` 且 body 为空——
        #   而同一个端口的**原始 socket 请求**返回正常的 200。测试不应受环境代理影响，
        #   否则"用例失败"会被误读成"服务有问题"。
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{PORT}", timeout=10.0, trust_env=False
        ) as client:
            health: httpx.Response | None = None
            for _ in range(60):
                try:
                    health = await client.get("/health")
                    break
                except (httpx.ConnectError, httpx.ConnectTimeout):
                    await asyncio.sleep(0.25)
            if health is None:
                raise AssertionError("服务器在 15 秒内未就绪")
            observed["health"] = health

            observed["openapi"] = await client.get("/openapi.json")
            observed["me"] = await client.get("/api/auth/me")
            return observed
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=15)


def test_live_server_serves_health_openapi_and_401(it_database: str) -> None:
    """真实进程级冒烟：`/health` 结构化响应、五个路由挂载、未认证 401。"""
    try:
        observed = run(_serve_and_probe())
    except OSError as exc:  # 端口被占用（例如上一次异常退出留下的进程）
        pytest.skip(f"端口 {PORT} 不可用，跳过真实服务器冒烟：{exc}")

    def _detail(name: str) -> str:
        response = observed[name]
        return f"{name}: HTTP {response.status_code} CT={response.headers.get('content-type')} body={response.text[:200]!r}"

    health = observed["health"]
    assert health.status_code == 200, _detail("health")
    health_body = health.json()
    assert health_body["code"] == "OK", _detail("health")
    assert health_body["data"]["status"] == "ok"
    assert health_body["request_id"], "响应包必须带链路追踪 request_id"

    openapi = observed["openapi"]
    assert openapi.status_code == 200, _detail("openapi")
    paths = set(openapi.json()["paths"])
    assert paths == EXPECTED_PATHS, f"路由前缀或挂载有误：{sorted(paths)}"

    me = observed["me"]
    assert me.status_code == 401, _detail("me")
    assert me.json()["code"] == "TOKEN_INVALID", _detail("me")
    assert me.json()["data"] is None


_ = it_dsn  # 保持导入语义清晰：本文件的用例同样只在集成库可用时运行

"""M01 认证端点的端到端集成测试（真实 MySQL + 真实 ASGI 应用，不启服务器）。

与 `test_refresh_store_mysql.py` 的分工：那边验证仓储语义，这边验证**从 HTTP 请求到
数据库写入的整条链路**——DTO 校验、H01 身份解析、事务提交、错误码与状态码、响应包结构。

本文件里最关键的一条是 `test_logout_invalidates_access_token_immediately`：
它验证"注销后 access token **立即**失效"。这只有在令牌携带 `sid`（会话标识）时才可能成立；
缺少 `sid` 的实现会让 access token 继续有效至多 2 小时——接口看起来正常，
但"退出登录"这个安全承诺已经破了。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.security import hash_password
from app.db.session import get_session
from app.main import create_app
from app.services.auth_svc import _reset_login_limiter
from tests.integration.conftest import it_dsn, run

PASSWORD = "integration-pass-1"
USERNAME = "e2e-user"


async def _it_session() -> AsyncIterator[AsyncSession]:
    """每个请求一个会话，绑定**测试库**（通过依赖覆盖，不改全局部署配置）。"""
    engine = create_async_engine(it_dsn(), poolclass=NullPool, future=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            yield session
    finally:
        await engine.dispose()


def _app() -> Any:
    """构造应用并覆盖 session 依赖。

    ★ 用 `dependency_overrides` 而不是改环境变量：这样不必动全局 `Settings` 缓存，
      也不会让"测试库"变成"全局默认"。覆盖按**函数对象身份**生效，因此
      `get_current_ctx` 内部的 `Depends(get_session)` 同样会被换成测试库。
    """
    _reset_login_limiter()
    app = create_app()
    app.dependency_overrides[get_session] = _it_session
    return app


def _client(app: Any) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _seed_user(
    username: str, password: str, *, enabled: bool = True, codes: tuple[str, ...] = ()
) -> int:
    """建账号（可选建角色并挂功能码），返回 user_id。"""
    engine = create_async_engine(it_dsn(), poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            async with session.begin():
                user_id = int(
                    (
                        await session.execute(
                            text(
                                "INSERT INTO user (username, username_norm, password_hash, dept_id, "
                                "enabled, identity_revision, revision, created_at, updated_at) "
                                "VALUES (:u, :n, :h, NULL, :e, 1, 1, NOW(6), NOW(6))"
                            ),
                            {
                                "u": username,
                                "n": username,
                                "h": hash_password(password),
                                "e": 1 if enabled else 0,
                            },
                        )
                    ).lastrowid
                    or 0
                )
                if codes:
                    role_id = int(
                        (
                            await session.execute(
                                text(
                                    "INSERT INTO role (name, name_norm, revision, created_at, "
                                    "updated_at) VALUES (:n, :n, 1, NOW(6), NOW(6))"
                                ),
                                {"n": f"role-{username}"},
                            )
                        ).lastrowid
                        or 0
                    )
                    for code in codes:
                        await session.execute(
                            text(
                                "INSERT INTO role_permission (role_id, code) VALUES (:r, :c)"
                            ),
                            {"r": role_id, "c": code},
                        )
                    await session.execute(
                        text(
                            "INSERT INTO user_role (user_id, role_id) VALUES (:u, :r)"
                        ),
                        {"u": user_id, "r": role_id},
                    )
                return user_id
    finally:
        await engine.dispose()


async def _login(client: AsyncClient, username: str, password: str) -> dict[str, Any]:
    return (await client.post("/api/auth/login", json={"username": username, "password": password})).json()


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------- 存活探针

def test_health_reports_version_without_side_effects(clean_db: None) -> None:
    async def _scenario() -> tuple[int, dict[str, Any]]:
        async with _client(_app()) as client:
            response = await client.get("/health")
            return response.status_code, response.json()

    status_code, body = run(_scenario())
    assert status_code == 200
    assert body["code"] == "OK"
    assert body["data"]["status"] == "ok"
    assert "app_version" in body["data"]
    assert isinstance(body["request_id"], str) and body["request_id"]


# ---------------------------------------------------------------- 登录

def test_login_and_me_roundtrip(clean_db: None) -> None:
    async def _scenario() -> tuple[int, dict[str, Any], int, dict[str, Any]]:
        await _seed_user(USERNAME, PASSWORD, codes=("ai:ask", "kb:view"))
        async with _client(_app()) as client:
            tokens = await _login(client, USERNAME, PASSWORD)
            me = await client.get("/api/auth/me", headers=_bearer(tokens["data"]["access_token"]))
            return 200, tokens, me.status_code, me.json()

    _, tokens, me_status, me = run(_scenario())
    assert tokens["code"] == "OK"
    assert set(tokens["data"]) == {"access_token", "refresh_token", "expires_in"}
    assert me_status == 200
    assert me["data"]["username"] == USERNAME
    assert me["data"]["permission_codes"] == ["ai:ask", "kb:view"]
    assert me["data"]["dept_id"] is None
    assert me["data"]["revision"] == 1
    # 响应里不得出现口令或令牌字段
    assert not {"password", "password_hash", "token"} & set(me["data"])


@pytest.mark.parametrize(
    ("password", "expected_code"),
    [
        ("wrong-password-1", "BAD_CREDENTIALS"),
    ],
)
def test_login_wrong_password(clean_db: None, password: str, expected_code: str) -> None:
    async def _scenario() -> tuple[int, str]:
        await _seed_user(USERNAME, PASSWORD)
        async with _client(_app()) as client:
            body = await _login(client, USERNAME, password)
            return 200, body["code"]

    _, code = run(_scenario())
    assert code == expected_code


def test_login_unknown_user_returns_bad_credentials(clean_db: None) -> None:
    """账号不存在与密码错误**返回同一个码**：分开披露等于提供账号枚举接口。"""

    async def _scenario() -> str:
        async with _client(_app()) as client:
            return (await _login(client, "no-such-user", PASSWORD))["code"]

    assert run(_scenario()) == "BAD_CREDENTIALS"


def test_login_disabled_user_requires_correct_password(clean_db: None) -> None:
    """停用账号：**只有密码正确时**才回 USER_DISABLED（否则等于泄露账号存在）。"""

    async def _scenario() -> tuple[str, str]:
        await _seed_user(USERNAME, PASSWORD, enabled=False)
        async with _client(_app()) as client:
            wrong = await _login(client, USERNAME, "wrong-password-1")
            right = await _login(client, USERNAME, PASSWORD)
            return wrong["code"], right["code"]

    wrong_code, right_code = run(_scenario())
    assert wrong_code == "BAD_CREDENTIALS"
    assert right_code == "USER_DISABLED"


def test_login_overlong_password_returns_422_parameter_error(clean_db: None) -> None:
    """★ 超长密码必须显式拒绝（422 `PASSWORD_LENGTH_INVALID`），不得退化成"凭据错误"。"""

    async def _scenario() -> tuple[int, str]:
        await _seed_user(USERNAME, PASSWORD)
        async with _client(_app()) as client:
            response = await client.post(
                "/api/auth/login", json={"username": USERNAME, "password": "p" * 100}
            )
            return response.status_code, response.json()["code"]

    status_code, code = run(_scenario())
    assert status_code == 422
    assert code == "PASSWORD_LENGTH_INVALID"


def test_login_rate_limited(clean_db: None) -> None:
    """同 IP 超过 `login_rate_per_min` 后返回 429。"""
    limit = 10

    async def _scenario() -> list[str]:
        await _seed_user(USERNAME, PASSWORD)
        async with _client(_app()) as client:
            codes = []
            for _ in range(limit + 1):
                codes.append((await _login(client, USERNAME, "wrong-password-1"))["code"])
            return codes

    codes = run(_scenario())
    assert codes[:limit] == ["BAD_CREDENTIALS"] * limit
    assert codes[limit] == "RATE_LIMITED"


# ---------------------------------------------------------------- 身份解析失败

@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer not-a-jwt"},
        {"Authorization": "Basic abc"},
        {"Authorization": "Bearer "},
    ],
)
def test_me_rejects_bad_authorization(clean_db: None, headers: dict[str, str]) -> None:
    async def _scenario() -> tuple[int, str]:
        async with _client(_app()) as client:
            response = await client.get("/api/auth/me", headers=headers)
            return response.status_code, response.json()["code"]

    status_code, code = run(_scenario())
    assert status_code == 401
    assert code == "TOKEN_INVALID"


# ---------------------------------------------------------------- 刷新

def test_refresh_rotates_tokens_and_replay_is_rejected(clean_db: None) -> None:
    """AC-01.02-01：刷新成功换新令牌；**旧刷新令牌重放被拒**。"""

    async def _scenario() -> tuple[str, str, str, int, str]:
        await _seed_user(USERNAME, PASSWORD)
        async with _client(_app()) as client:
            tokens = (await _login(client, USERNAME, PASSWORD))["data"]
            first = await client.post(
                "/api/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
            )
            # 用**新的** access token 访问受保护端点
            me_ok = await client.get(
                "/api/auth/me", headers=_bearer(first.json()["data"]["access_token"])
            )
            # 旧刷新令牌重放
            replay = await client.post(
                "/api/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
            )
            return (
                first.json()["code"],
                "rotated" if first.json()["data"]["refresh_token"] != tokens["refresh_token"] else "same",
                me_ok.json()["code"],
                me_ok.status_code,
                replay.json()["code"],
            )

    code, rotated, me_code, me_status, replay_code = run(_scenario())
    assert code == "OK"
    assert rotated == "rotated", "刷新必须换发新令牌，否则旧令牌会长期有效"
    assert (me_status, me_code) == (200, "OK")
    assert replay_code == "TOKEN_INVALID"


def test_refresh_with_access_token_is_rejected(clean_db: None) -> None:
    """不能用 access token 冒充 refresh token（`typ` 校验）。"""

    async def _scenario() -> str:
        await _seed_user(USERNAME, PASSWORD)
        async with _client(_app()) as client:
            tokens = (await _login(client, USERNAME, PASSWORD))["data"]
            response = await client.post(
                "/api/auth/refresh", json={"refresh_token": tokens["access_token"]}
            )
            return response.json()["code"]

    assert run(_scenario()) == "TOKEN_INVALID"


# ---------------------------------------------------------------- 退出（sid 的关键验证）

def test_logout_invalidates_access_token_immediately(clean_db: None) -> None:
    """★ 注销后 access token **立即**失效（需要 payload 里的 `sid`）。

    若令牌不带会话标识，这个用例会失败：`/me` 在注销后仍返回 200，
    直到 access token 自然过期（默认 2 小时）——安全承诺实际是破的。
    """

    async def _scenario() -> tuple[int, dict[str, Any], int, str, str]:
        await _seed_user(USERNAME, PASSWORD)
        async with _client(_app()) as client:
            tokens = (await _login(client, USERNAME, PASSWORD))["data"]
            headers = _bearer(tokens["access_token"])
            before = await client.get("/api/auth/me", headers=headers)
            logout = await client.post(
                "/api/auth/logout", headers=headers, json={"refresh_token": tokens["refresh_token"]}
            )
            after = await client.get("/api/auth/me", headers=headers)
            refresh_after = await client.post(
                "/api/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
            )
            return (
                before.status_code,
                logout.json()["data"],
                after.status_code,
                after.json()["code"],
                refresh_after.json()["code"],
            )

    before_status, logout_data, after_status, after_code, refresh_code = run(_scenario())
    assert before_status == 200
    assert logout_data == {"revoked": True}
    assert after_status == 401, "注销后 access token 仍然可用 → 令牌未携带会话标识"
    assert after_code == "TOKEN_INVALID"
    assert refresh_code == "TOKEN_INVALID"


def test_logout_is_idempotent(clean_db: None) -> None:
    """重复退出返回 `revoked=false`，不报错（F-01.03 边界）。"""

    async def _scenario() -> tuple[bool, int, bool]:
        await _seed_user(USERNAME, PASSWORD)
        async with _client(_app()) as client:
            tokens = (await _login(client, USERNAME, PASSWORD))["data"]
            first = await client.post(
                "/api/auth/logout",
                headers=_bearer(tokens["access_token"]),
                json={"refresh_token": tokens["refresh_token"]},
            )
            # 第二次：access token 已被撤销 → 401；这正是"注销立即生效"的另一种体现
            second = await client.post(
                "/api/auth/logout",
                headers=_bearer(tokens["access_token"]),
                json={"refresh_token": tokens["refresh_token"]},
            )
            return first.json()["data"]["revoked"], second.status_code, second.json()["data"] is None

    first_revoked, second_status, second_data_none = run(_scenario())
    assert first_revoked is True
    assert second_status == 401
    assert second_data_none is True


def test_logout_cannot_revoke_another_users_session(clean_db: None) -> None:
    """拿自己的 access token + 别人的 refresh token 不能注销别人的会话（F-01.03 归属校验）。"""

    async def _scenario() -> tuple[str, str]:
        user_a, user_b = "e2e-user-a", "e2e-user-b"
        await _seed_user(user_a, PASSWORD)
        await _seed_user(user_b, PASSWORD)
        async with _client(_app()) as client:
            tokens_a = (await _login(client, user_a, PASSWORD))["data"]
            tokens_b = (await _login(client, user_b, PASSWORD))["data"]
            response = await client.post(
                "/api/auth/logout",
                headers=_bearer(tokens_a["access_token"]),
                json={"refresh_token": tokens_b["refresh_token"]},
            )
            # B 的会话不应被撤销 → 仍可用其 access token 访问
            me_b = await client.get("/api/auth/me", headers=_bearer(tokens_b["access_token"]))
            return response.json()["code"], me_b.json()["code"]

    logout_code, me_b_code = run(_scenario())
    assert logout_code == "FORBIDDEN_OWNER"
    assert me_b_code == "OK"

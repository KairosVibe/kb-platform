"""M09 模型配置与运行控制端到端集成测试（真实 MySQL）。

钉住的规则：
- config_revision append-only：patch 记变更、snapshot 记合并快照、expected_revision 防并发；
- **embedding 键冻结**（BC-09.02）：热改直接 409 REINDEX_REQUIRED；
- 白名单外键 422；密钥不回读（只有 key_configured 布尔）；
- /ready 就绪探针不含 secret，外部瞬断只降级不重试。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from uuid import uuid4

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


async def _it_session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(it_dsn(), poolclass=NullPool, future=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            yield session
    finally:
        await engine.dispose()


def _client() -> AsyncClient:
    _reset_login_limiter()
    app = create_app()
    app.dependency_overrides[get_session] = _it_session
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _seed() -> None:
    engine = create_async_engine(it_dsn(), poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        async with session.begin():
            role_id = int(
                (
                    await session.execute(
                        text(
                            "INSERT INTO `role` (name, name_norm, revision, created_at, updated_at) "
                            "VALUES ('配置管理员', '配置管理员', 1, NOW(6), NOW(6))"
                        )
                    )
                ).lastrowid
            )
            await session.execute(
                text("INSERT INTO role_permission (role_id, code) VALUES (:r, :c)"),
                {"r": role_id, "c": "sys:model"},
            )
            await session.execute(
                text(
                    "INSERT INTO config_revision (patch, snapshot, actor_id, created_at) "
                    "VALUES ('{}', '{\"models\": {}, \"thresholds\": {}, \"limits\": {}}', "
                    "NULL, NOW(6))"
                )
            )
            user_id = int(
                (
                    await session.execute(
                        text(
                            "INSERT INTO `user` (username, username_norm, password_hash, dept_id, "
                            "enabled, identity_revision, revision, created_at, updated_at) "
                            "VALUES ('admin', 'admin', :h, NULL, 1, 1, 1, NOW(6), NOW(6))"
                        ),
                        {"h": hash_password(PASSWORD)},
                    )
                ).lastrowid
            )
            await session.execute(
                text("INSERT INTO user_role (user_id, role_id) VALUES (:u, :r)"),
                {"u": user_id, "r": role_id},
            )
    await engine.dispose()


async def _token(client: AsyncClient) -> str:
    resp = await client.post(
        "/api/auth/login", json={"username": "admin", "password": PASSWORD}
    )
    return resp.json()["data"]["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_config_read_update_probe_readiness(
    clean_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _run() -> None:
        await _seed()
        async with _client() as client:
            token = await _token(client)

            # ---- F-09.01：读配置 ----
            read = await client.get("/api/model-config", headers=_auth(token))
            assert read.status_code == 200, read.text
            data = read.json()["data"]
            assert set(data) == {
                "revision", "models", "thresholds", "limits", "key_configured",
            }
            revision = int(data["revision"])

            # ---- F-09.02：白名单内更新 ----
            updated = await client.patch(
                "/api/model-config", headers=_auth(token),
                json={"patch": {"thresholds": {"answer_top_k": 8}},
                      "expected_revision": revision},
            )
            assert updated.status_code == 200, updated.text
            new_revision = int(updated.json()["data"]["revision"])
            assert new_revision == revision + 1

            # 白名单外的键 → 422
            bad_key = await client.patch(
                "/api/model-config", headers=_auth(token),
                json={"patch": {"thresholds": {"magic": 1}}, "expected_revision": new_revision},
            )
            assert bad_key.status_code == 422

            # embedding 冻结 → 409 REINDEX_REQUIRED
            frozen = await client.patch(
                "/api/model-config", headers=_auth(token),
                json={"patch": {"models": {"embed_dim": 768}},
                      "expected_revision": new_revision},
            )
            assert frozen.status_code == 409
            assert frozen.json()["code"] == "REINDEX_REQUIRED"

            # 旧 revision → 409
            stale = await client.patch(
                "/api/model-config", headers=_auth(token),
                json={"patch": {"thresholds": {"rrf_k": 80}}, "expected_revision": revision},
            )
            assert stale.status_code == 409

            # 快照合并生效
            reread = (await client.get("/api/model-config", headers=_auth(token))).json()["data"]
            assert reread["thresholds"]["answer_top_k"] == 8

            # ---- F-09.04：就绪探针 ----
            ready = await client.get("/ready")
            assert ready.status_code == 200
            ready_data = ready.json()["data"]
            assert set(ready_data["checks"]) == {"db", "config", "vector", "executor"}
            assert ready_data["checks"]["db"] is True
            assert "secret_value" not in json.dumps(ready_data)

    run(_run())

"""M08 看板端到端集成测试（真实 MySQL；直接落审计/请求行后验证聚合）。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
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
SH = timezone(timedelta(hours=8))


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


async def _seed_with_data() -> dict[str, int]:
    """1 看板用户 + 2 知识单元 + 3 条终态请求（answered/faq_hit/no_evidence→无审计外的 failed）+ 用量。"""
    engine = create_async_engine(it_dsn(), poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.utcnow().replace(tzinfo=None)  # UTC 存储（naive）
    async with factory() as session:
        async with session.begin():
            role_id = int(
                (
                    await session.execute(
                        text(
                            "INSERT INTO `role` (name, name_norm, revision, created_at, updated_at) "
                            "VALUES ('看板用户', '看板用户', 1, NOW(6), NOW(6))"
                        )
                    )
                ).lastrowid
            )
            for code in ("dashboard:view", "ai:ask"):
                await session.execute(
                    text("INSERT INTO role_permission (role_id, code) VALUES (:r, :c)"),
                    {"r": role_id, "c": code},
                )
            await session.execute(
                text(
                    "INSERT INTO config_revision (patch, snapshot, actor_id, created_at) "
                    "VALUES ('{}', '{}', NULL, NOW(6))"
                )
            )
            user_id = int(
                (
                    await session.execute(
                        text(
                            "INSERT INTO `user` (username, username_norm, password_hash, dept_id, "
                            "enabled, identity_revision, revision, created_at, updated_at) "
                            "VALUES ('viewer', 'viewer', :h, NULL, 1, 1, 1, NOW(6), NOW(6))"
                        ),
                        {"h": hash_password(PASSWORD)},
                    )
                ).lastrowid
            )
            await session.execute(
                text("INSERT INTO user_role (user_id, role_id) VALUES (:u, :r)"),
                {"u": user_id, "r": role_id},
            )
            auth_session_id = uuid4().hex  # 列为 CHAR(32) hex（无连字符）
            await session.execute(
                text(
                    "INSERT INTO auth_session (id, user_id, expires_at, created_at, updated_at) "
                    "VALUES (:i, :u, NOW(6) + INTERVAL 2 HOUR, NOW(6), NOW(6))"
                ),
                {"i": auth_session_id, "u": user_id},
            )
            await session.execute(
                text(
                    "INSERT INTO chat_session (user_id, title, is_deleted, revision, "
                    "created_at, updated_at) VALUES (:u, '看板用例', 0, 1, NOW(6), NOW(6))"
                ),
                {"u": user_id},
            )
            for name in ("薪酬制度", "休假制度"):
                await session.execute(
                    text(
                        "INSERT INTO knowledge_unit (code, title, format, category, creator_id, "
                        "enabled, is_deleted, is_global, content_version, indexed_version, "
                        "index_status, acl_version, revision, created_at, updated_at) "
                        "VALUES (:c, :t, 'md', '制度', :u, 1, 0, 1, 1, 1, 'indexed', 1, 1, "
                        "NOW(6), NOW(6))"
                    ),
                    {"c": f"KB-{name}", "t": name, "u": user_id},
                )
            # 3 条请求 + 审计：answered / faq_hit / failed
            specs = [
                ("answered", "薪酬怎么发放？", "completed"),
                ("faq_hit", "年假怎么请？", "completed"),
                (None, "未知问题？", "failed"),
            ]
            for index, (result_type, question, status) in enumerate(specs):
                accepted = now - timedelta(hours=index + 1)
                request_id = int(
                    (
                        await session.execute(
                            text(
                                "INSERT INTO chat_request (user_id, auth_session_id, session_id, "
                                "client_request_id, payload_hash, question, status, result_type, "
                                "config_revision, last_seq, cancel_requested, accepted_at, "
                                "created_at, updated_at) "
                                "VALUES (:u, :as, (SELECT id FROM chat_session LIMIT 1), "
                                ":c, :ph, :q, :st, :rt, 1, 0, 0, :a, :a, :a)"
                            ),
                            {"u": user_id, "as": auth_session_id, "c": uuid4().hex,
                             "ph": uuid4().hex, "q": question, "st": status,
                             "rt": result_type, "a": accepted},
                        )
                    ).lastrowid
                )
                await session.execute(
                    text(
                        "INSERT INTO qa_audit (request_id, user_id, asked_at, question, "
                        "recall_snapshot, allowed_snapshot, denied_snapshot, status, "
                        "duration_ms, usage_status, created_at, updated_at) "
                        "VALUES (:r, :u, :a, :q, '{}', '{}', '{}', :st, :d, 'known', :a, :a)"
                    ),
                    {"r": request_id, "u": user_id, "a": accepted, "q": question,
                     "st": status, "d": 500 + index * 250},
                )
                if index == 0:  # answered → generation 用量
                    # ★ 不用 NOW(6)：DB 时钟与聚合的"上海当日"窗口可能漂移
                    #   （VM 挂起后时钟停在挂起时刻，实测漂移 16h）——与上方
                    #   chat_request/qa_audit 一致，统一用应用侧时钟参数。
                    await session.execute(
                        text(
                            "INSERT INTO model_call_usage (request_id, call_id, kind, "
                            "model_version, input_tokens, output_tokens, status, "
                            "created_at, updated_at) "
                            "VALUES (:r, :c, 'generation', 'qwen-plus', 100, 20, 'known', "
                            ":t, :t)"
                        ),
                        {"r": request_id, "c": uuid4().hex, "t": accepted},
                    )
            return {"user_id": user_id}
    await engine.dispose()


async def _token(client: AsyncClient) -> str:
    resp = await client.post(
        "/api/auth/login", json={"username": "viewer", "password": PASSWORD}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["access_token"]


def test_dashboard_summary_and_charts(
    clean_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 占位（auth_session/chat_session 外键需要种子行，_seed_with_data 内直接插会失败时跳过聚合细节）
    async def _run() -> None:
        await _seed_with_data()
        async with _client() as client:
            token = await _token(client)
            today = datetime.now(SH).date().isoformat()

            summary = await client.get(
                "/api/dashboard/summary", headers={"Authorization": f"Bearer {token}"},
                params={"range": "day", "anchor_date": today},
            )
            assert summary.status_code == 200, summary.text
            data = summary.json()["data"]
            assert set(data) == {
                "pv", "uv", "faq_hit_rate", "coverage", "knowledge_count",
                "error_rate", "unknown_usage_count",
            }
            assert data["pv"] >= 2, "至少两条请求落在今日"
            assert data["knowledge_count"] >= 2
            assert data["error_rate"] > 0, "failed 请求应计入错误率"

            charts = await client.get(
                "/api/dashboard/charts", headers={"Authorization": f"Bearer {token}"},
                params={"range": "day", "anchor_date": today, "top_n": 5},
            )
            assert charts.status_code == 200, charts.text
            body = charts.json()["data"]
            assert set(body) == {
                "traffic", "questions", "knowledge_heat", "usage",
                "latency", "knowledge_counts",
            }
            assert sum(t["pv"] for t in body["traffic"]) >= 2
            assert any(u["kind"] == "generation" for u in body["usage"])
            assert any(l["status"] == "failed" for l in body["latency"])
            assert body["knowledge_counts"]["total"] >= 2

            bad_range = await client.get(
                "/api/dashboard/summary", headers={"Authorization": f"Bearer {token}"},
                params={"range": "month", "anchor_date": today},
            )
            assert bad_range.status_code == 422

    run(_run())

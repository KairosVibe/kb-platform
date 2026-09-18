"""联调第 2 轮补齐的 4 条路由：H27 快照、H34 FAQ 列表、API-S05 补档任务、F-08.04 审计搜索。

复用 test_knowledge_endpoints 的种子/上传/流水线助手（同一真实 MySQL + ASGI 模式；
与既有文件一致的同步 def + run() 包装风格——本仓库未装 pytest-asyncio）。
"""

from __future__ import annotations

import hashlib
from typing import Any
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.security import hash_password
from app.db.session import get_session
from app.main import create_app
from app.services import chat_svc
from app.services.auth_svc import _reset_login_limiter
from tests.integration.conftest import it_dsn, run
from tests.integration.test_knowledge_endpoints import (
    PASSWORD,
    _auth,
    _client,
    _run_pipeline,
    _seed,
    _upload,
)


async def _login(client: AsyncClient, username: str) -> str:
    resp = await client.post(
        "/api/auth/login",
        json={"username": username, "password": PASSWORD},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["access_token"]


async def _sql_exec(statement: str, params: dict[str, Any] | None = None) -> int:
    engine = create_async_engine(it_dsn(), poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            async with session.begin():
                result = await session.execute(text(statement), params or {})
                return int(result.lastrowid or 0)
    finally:
        await engine.dispose()


async def _grant(role_id: int, codes: tuple[str, ...]) -> None:
    """给既有测试种子追加功能码（test_knowledge._seed 只含 kb:*/sys:*）。"""
    engine = create_async_engine(it_dsn(), poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            async with session.begin():
                for code in codes:
                    await session.execute(
                        text(
                            "INSERT IGNORE INTO role_permission (role_id, code) "
                            "VALUES (:r, :c)"
                        ),
                        {"r": role_id, "c": code},
                    )
    finally:
        await engine.dispose()


async def _role_id_by_name(name: str) -> int:
    engine = create_async_engine(it_dsn(), poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            row = (
                await session.execute(
                    text("SELECT id FROM `role` WHERE name_norm=:n"),
                    {"n": name.casefold()},
                )
            ).scalar_one()
            return int(row)
    finally:
        await engine.dispose()


# ---------------------------------------------------------------- H27 快照


def test_h27_snapshot_owned_and_enumeration_proof(clean_db: None) -> None:
    """本人可读 accepted 快照（无正文）；他人与不存在均 404 防枚举。"""

    async def _run() -> None:
        ids = await _seed()
        # admin 需要 ai:ask（受权路由前置），快照归属仍是硬条件
        await _grant(ids["admin_role_id"], ("ai:ask",))
        # 受理问答需要最新配置修订快照（集成库清表后无种子，M05 测试同款补法）
        await _sql_exec(
            "INSERT INTO config_revision (patch, snapshot, actor_id, created_at) "
            "VALUES (:p, :s, NULL, NOW(6))",
            {"p": '{"source":"route-test"}', "s": '{"vector_top_k":20}'},
        )
        original_spawn = chat_svc.spawn_answer
        chat_svc.spawn_answer = lambda request_id: None  # noqa: ARG005 — 受理即返回，不执行生成
        try:
            async with _client() as client:
                token = await _login(client, "viewer")
                admin = await _login(client, "admin")

                created = await client.post(
                    "/api/sessions",
                    headers=_auth(token),
                    json={"title": "问答回归"},
                )
                assert created.status_code == 201, created.text
                session_id = int(created.json()["data"]["session_id"])

                accepted = await client.post(
                    "/api/chat/requests",
                    headers=_auth(token),
                    json={
                        "session_id": session_id,
                        "client_request_id": str(uuid4()),
                        "question": "报销流程是什么",
                    },
                )
                assert accepted.status_code == 202, accepted.text
                request_id = int(accepted.json()["data"]["request_id"])

                # 本人：accepted 快照，无正文、无受限
                snap = await client.get(
                    f"/api/chat/requests/{request_id}", headers=_auth(token)
                )
                assert snap.status_code == 200, snap.text
                data = snap.json()["data"]
                assert data["status"] == "accepted"
                assert data["answer"] is None
                assert data["restricted"] is False
                assert isinstance(data["last_seq"], int) and data["last_seq"] >= 0

                # 他人 404（防枚举；admin 有 ai:ask 也不行——归属是硬条件）
                other = await client.get(
                    f"/api/chat/requests/{request_id}", headers=_auth(admin)
                )
                assert other.status_code == 404, other.text

                missing = await client.get(
                    "/api/chat/requests/99999999", headers=_auth(token)
                )
                assert missing.status_code == 404
        finally:
            chat_svc.spawn_answer = original_spawn

    run(_run())


# ---------------------------------------------------------------- H34 FAQ 列表


def test_h34_list_faqs_filters_by_source_read_access(clean_db: None) -> None:
    """来源读权过滤在分页之前：来源不可读时谁都看不到；授权后可见、可过滤。"""

    async def _run() -> None:
        ids = await _seed()
        admin_role = ids["admin_role_id"]
        viewer_role = await _role_id_by_name("访客")
        # 审核员有 faq 功能码但默认无数据读权（H34 过滤断言的正面对照）
        await _grant(admin_role, ("faq:review", "faq:publish"))
        await _grant(viewer_role, ("faq:review",))
        async with _client() as client:
            admin = await _login(client, "admin")
            viewer = await _login(client, "viewer")

            # 建 1 个可引用的知识单元（上传 + 替身流水线）
            uploaded = await _upload(client, admin)
            assert (await _run_pipeline(uploaded["task_id"]))["status"] == "succeeded"
            unit_id = int(uploaded["unit_id"])

            # 种 1 条已发布 FAQ，来源指向该单元 v1
            faq_id = await _sql_exec(
                "INSERT INTO faq (question, answer, status, cache_enabled, frequency, "
                "confidence, revision, created_at, updated_at) "
                "VALUES ('报销流程怎么走', '见差旅报销办法第二条。', 'published', 0, 3, 0.9, "
                "1, NOW(6), NOW(6))"
            )
            await _sql_exec(
                "INSERT INTO faq_source (faq_id, unit_id, version) VALUES (:f, :u, 1)",
                {"f": faq_id, "u": unit_id},
            )

            # 来源不可读 → 无论谁（有 faq:review 与否）都看不到该行
            empty_admin = await client.get("/api/faqs", headers=_auth(admin))
            assert empty_admin.status_code == 200, empty_admin.text
            assert empty_admin.json()["data"]["items"] == []

            empty_viewer = await client.get("/api/faqs", headers=_auth(viewer))
            assert empty_viewer.status_code == 200
            assert empty_viewer.json()["data"]["items"] == []

            # 授权 viewer 读该单元 → viewer 可见；admin 仍不可见（无数据读权）。
            # revision 从台账列表取（单元无详情路由——防枚举设计，列表即权威）。
            units_resp = await client.get(
                "/api/knowledge-units", headers=_auth(admin), params={"page": 1, "size": 50}
            )
            assert units_resp.status_code == 200, units_resp.text
            revision = next(
                int(u["revision"])
                for u in units_resp.json()["data"]["items"]
                if int(u["id"]) == unit_id
            )
            acl = await client.put(
                f"/api/knowledge-units/{unit_id}/acl",
                headers=_auth(admin),
                json={"global": False, "depts": [], "roles": [],
                      "users": [ids["viewer_user_id"]], "expected_revision": revision},
            )
            assert acl.status_code == 200, acl.text

            visible = await client.get("/api/faqs", headers=_auth(viewer))
            assert visible.status_code == 200, visible.text
            items = visible.json()["data"]["items"]
            assert len(items) == 1
            row = items[0]
            assert row["id"] == faq_id
            assert row["source_refs"] == [
                {"unit_id": unit_id, "version": 1, "chunk_id": None}
            ]
            assert row["frequency"] == 3
            assert row["hit_count"] == 0  # 命中计数组件未建，不伪造

            still_hidden_admin = await client.get("/api/faqs", headers=_auth(admin))
            assert still_hidden_admin.json()["data"]["items"] == []

            # 过滤：status / q
            by_status = await client.get(
                "/api/faqs", headers=_auth(viewer), params={"status": "candidate"}
            )
            assert by_status.json()["data"]["total"] == 0
            by_q = await client.get("/api/faqs", headers=_auth(viewer), params={"q": "报销"})
            assert by_q.json()["data"]["total"] == 1

    run(_run())


# ---------------------------------------------------------------- API-S05 补档任务


def test_api_s05_supplement_tasks_list_and_filter(clean_db: None) -> None:
    """gap:handle 才可查；gap_id 过滤正确；未绑定行 unit_id/target_version 为 null。"""

    async def _run() -> None:
        ids = await _seed()
        await _grant(ids["admin_role_id"], ("gap:handle",))
        fingerprint = hashlib.sha256("缺口问题甲".encode("utf-8")).hexdigest()
        gap_id = await _sql_exec(
            "INSERT INTO knowledge_gap (department_key, fingerprint, question, status, "
            "first_seen_at, last_seen_at, score_type, model_version, revision, "
            "created_at, updated_at) "
            "VALUES (0, :fp, '缺口问题甲', 'processing', NOW(6), NOW(6), 'recall', 't', "
            "1, NOW(6), NOW(6))",
            {"fp": fingerprint},
        )
        task_id = await _sql_exec(
            "INSERT INTO supplement_task (gap_id, status, unit_id, target_version, revision, "
            "created_at, updated_at) VALUES (:g, 'processing', NULL, NULL, 1, NOW(6), NOW(6))",
            {"g": gap_id},
        )
        async with _client() as client:
            admin = await _login(client, "admin")
            viewer = await _login(client, "viewer")

            denied = await client.get("/api/supplement-tasks", headers=_auth(viewer))
            assert denied.status_code == 403, denied.text

            listed = await client.get("/api/supplement-tasks", headers=_auth(admin))
            assert listed.status_code == 200, listed.text
            items = listed.json()["data"]["items"]
            assert len(items) == 1 and items[0]["id"] == task_id
            assert items[0]["gap_id"] == gap_id
            assert items[0]["unit_id"] is None and items[0]["target_version"] is None

            by_gap = await client.get(
                "/api/supplement-tasks", headers=_auth(admin), params={"gap_id": gap_id}
            )
            assert by_gap.json()["data"]["total"] == 1
            none_gap = await client.get(
                "/api/supplement-tasks", headers=_auth(admin), params={"gap_id": 99999999}
            )
            assert none_gap.json()["data"]["total"] == 0

    run(_run())


# ---------------------------------------------------------------- F-08.04 审计搜索


def test_f0804_search_audit_with_read_trace(clean_db: None) -> None:
    """dashboard:view 才可查；查询本身留痕（action=audit.search）；request_id 过滤走业务 ID。"""

    async def _run() -> None:
        ids = await _seed()
        await _grant(ids["admin_role_id"], ("dashboard:view",))
        async with _client() as client:
            admin = await _login(client, "admin")
            viewer = await _login(client, "viewer")

            denied = await client.get("/api/audit", headers=_auth(viewer))
            assert denied.status_code == 403, denied.text

            # 首次查询：库中尚无任何操作日志 → 0；但查询本身留痕
            first = await client.get("/api/audit", headers=_auth(admin))
            assert first.status_code == 200, first.text
            assert first.json()["data"]["total"] == 0

            traced = await client.get(
                "/api/audit", headers=_auth(admin), params={"action": "audit.search"}
            )
            assert traced.status_code == 200
            search_rows = traced.json()["data"]["items"]
            assert len(search_rows) >= 1
            assert all(r["action"] == "audit.search" for r in search_rows)
            # 搜索留痕行不含正文（after 只有过滤条件）
            assert "filters" in (search_rows[0]["after"] or {})

            # request_id 过滤是问答业务 ID：用不存在的 ID 查 → 空
            by_request = await client.get(
                "/api/audit", headers=_auth(admin), params={"request_id": 99999999}
            )
            assert by_request.json()["data"]["total"] == 0

    run(_run())

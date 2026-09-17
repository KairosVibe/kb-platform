"""M07 缺口闭环端到端集成测试（真实 MySQL；检索/生成用替身）。

钉住的闭环规则：
- 只有正常 no_evidence 登记缺口（服务失败不进缺口——否则运维事故被当知识空白补档）；
- `(department_key, fingerprint)` 唯一 + GapRequest 联合主键 → 频次不虚增；
- 回放验证用**原提问用户**的当前身份；命中补充版本才关闭。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.core.security import hash_password
from app.db.base import utcnow
from app.db.session import get_session
from app.main import create_app
from app.providers import embedding as embedding_module
from app.providers import generation as generation_module
from app.providers import vector_store as vector_store_module
from app.services import chat_svc, ingest_svc
from app.services.auth_svc import _reset_login_limiter
from app.tasks.lease import claim_task
from tests.integration.conftest import it_dsn, run

PASSWORD = "integration-pass-1"
ADMIN_CODES = ("ai:ask", "kb:view", "kb:upload", "kb:edit", "kb:perm", "gap:handle")
BODY = "# 薪酬制度\n\n" + "".join(
    f"第{i}条 员工薪酬按月发放，逢节假日提前，标准由财务部公布。" for i in range(1, 40)
)


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
                            "VALUES ('闭环用户', '闭环用户', 1, NOW(6), NOW(6))"
                        )
                    )
                ).lastrowid
            )
            for code in ADMIN_CODES:
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
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _install_fakes(monkeypatch: pytest.MonkeyPatch, *, generation_fails: bool = False) -> dict:
    dim = int(get_settings().embed_dim)
    stored: dict[tuple[int, int], list[int]] = {}

    async def fake_embed(texts, config):
        import hashlib as _h
        import math as _m

        def vec(t: str) -> list[float]:
            seed = _h.sha256(t.encode("utf-8")).digest()
            values: list[float] = []
            while len(values) < dim:
                seed = _h.sha256(seed).digest()
                values.extend(byte for byte in seed[:32])
                values = values[:dim] if len(values) >= dim else values
            raw = [v / 255.0 - 0.5 for v in values[:dim]]
            norm = _m.sqrt(sum(v * v for v in raw)) or 1.0
            return [v / norm for v in raw]

        return ([vec(t) for t in texts], {"prompt_tokens": 1, "total_tokens": 1},
                f"{config.embed_provider}:{config.embed_model}:{config.embed_dim}")

    async def fake_search(config, *, query_vector, top_k):
        hits: list[dict] = []
        for (uid, ver), seqs in sorted(stored.items()):
            for seq in seqs:
                hits.append({"unit_id": uid, "version": ver, "seq": seq, "score": 0.9})
        return hits[:top_k]

    async def write_and_count(config, *, rows):
        stored[(rows[0]["unit_id"], rows[0]["version"])] = [r["seq"] for r in rows]
        return len(rows)

    def fake_count(config, *, unit_id, version, expected=None):
        return len(stored.get((unit_id, version), []))

    async def fake_stream(messages, config, signal):
        # 按内容触发失败：含"报销"的问题走"有证据但生成失败"路径（服务故障不进缺口）
        if any("报销" in str(m.get("content", "")) for m in messages):
            raise __import__("app.core.errors", fromlist=["TaskError"]).TaskError(
                "GENERATION_REJECTED", "模拟生成失败", transient=True
            )
        for piece in ("答案", "内容。"):
            if signal.cancelled:
                return
            yield __import__("app.providers.generation", fromlist=["ModelDelta"]).ModelDelta(
                text=piece, usage={"prompt_tokens": 10, "completion_tokens": 4},
                finish_reason="stop" if piece.endswith("。") else None,
            )

    monkeypatch.setattr(embedding_module, "embed_batches", fake_embed)
    monkeypatch.setattr(vector_store_module, "search_similar", fake_search)
    monkeypatch.setattr(vector_store_module, "upsert_version_chunks", write_and_count)
    monkeypatch.setattr(vector_store_module, "count_version_chunks", fake_count)
    monkeypatch.setattr(generation_module, "stream_llm", fake_stream)
    monkeypatch.setattr(chat_svc, "spawn_answer", lambda request_id: None)

    engine = create_async_engine(it_dsn(), poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(chat_svc, "_engine_factory", lambda: (engine, factory))
    return {"engine": engine, "stored": stored}


async def _ask_and_run(client: AsyncClient, token: str, engine, question: str) -> int:
    session_id = int(
        (await client.post("/api/sessions", headers=_auth(token), json={}))
        .json()["data"]["session_id"]
    )
    request_id = int(
        (
            await client.post(
                "/api/chat/requests",
                headers=_auth(token),
                json={"session_id": session_id, "client_request_id": uuid4().hex,
                      "question": question},
            )
        ).json()["data"]["request_id"]
    )
    result = await chat_svc.run_answer(request_id, engine=engine)
    return request_id, result


def test_gap_closure_loop(clean_db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    fakes = _install_fakes(monkeypatch)

    async def _run() -> None:
        await _seed()
        async with _client() as client:
            token = await _token(client)
            engine = fakes["engine"]

            # 1) 两次同义无证据提问 → 同一缺口，频次 2；outbox 全部消费
            request_id, result = await _ask_and_run(client, token, engine, "年假怎么申请？")
            assert result["result_type"] == "no_evidence"
            _request_id2, _ = await _ask_and_run(client, token, engine, "年假 怎么申请")

            async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                gaps = (
                    await session.execute(
                        text("SELECT id, status, department_key FROM knowledge_gap")
                    )
                ).all()
                assert len(gaps) == 1, "同部门同义问法只累积一行"
                assert gaps[0].status == "open" and int(gaps[0].department_key) == 0
                freq = int(
                    (
                        await session.execute(
                            text("SELECT COUNT(*) FROM gap_request WHERE gap_id=:g"),
                            {"g": int(gaps[0].id)},
                        )
                    ).scalar_one()
                )
                assert freq == 2, "两条请求都计数，且不去重虚增"
                pending = (
                    await session.execute(
                        text("SELECT status FROM outbox_event WHERE kind='knowledge_gap'")
                    )
                ).all()
                assert all(row.status == "sent" for row in pending)

            # 2) 服务故障不进缺口：先造出候选（索引过的单元），再让生成失败
            _request_id3, _r3 = await _ask_and_run(client, token, engine, "报销流程？")
            assert _r3["result_type"] == "no_evidence", "尚无索引内容，应为无证据"
            upload2 = await client.post(
                "/api/uploads",
                headers=_auth(token),
                files={"file": ("报销制度.md", BODY.encode("utf-8"), "text/markdown")},
                data={"category": "财务制度", "client_upload_id": str(uuid4())},
            )
            unit2 = int(upload2.json()["data"]["unit_id"])
            task2 = int(upload2.json()["data"]["task_id"])
            factory0 = async_sessionmaker(engine, expire_on_commit=False)
            async with factory0() as session:
                lease0 = await claim_task(session, task_id=task2, worker_id="t", now=utcnow())
                assert (
                    await ingest_svc.run_pipeline(
                        session, task_id=task2, lease_token=lease0.lease_token
                    )
                )["status"] == "succeeded"
                rev2 = int(
                    (
                        await session.execute(
                            text("SELECT revision FROM knowledge_unit WHERE id=:u"),
                            {"u": unit2},
                        )
                    ).scalar_one()
                )
            await client.put(
                f"/api/knowledge-units/{unit2}/acl",
                headers=_auth(token),
                json={"global": True, "depts": [], "roles": [], "users": [],
                      "expected_revision": rev2},
            )
            request_id_f, result_f = await _ask_and_run(client, token, engine, "报销流程是什么？")
            assert result_f["status"] == "failed", result_f
            async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                gap_for_failed = (
                    await session.execute(
                        text(
                            "SELECT COUNT(*) FROM gap_request WHERE request_id=:r"
                        ),
                        {"r": request_id_f},
                    )
                ).scalar_one()
                assert int(gap_for_failed) == 0, "服务失败不进缺口"

            # 3) 转建 → 绑定来源 → 回放关闭
            gap_id = int(gaps[0].id)
            converted = await client.post(
                f"/api/knowledge-gaps/{gap_id}/convert", headers=_auth(token)
            )
            assert converted.status_code == 200, converted.text
            task_id = int(converted.json()["data"]["task_id"])
            again = await client.post(
                f"/api/knowledge-gaps/{gap_id}/convert", headers=_auth(token)
            )
            assert int(again.json()["data"]["task_id"]) == task_id, "重复转建幂等"

            # 建一个真单元并索引（替身向量），再绑定到补充任务
            upload = await client.post(
                "/api/uploads",
                headers=_auth(token),
                files={"file": ("休假制度.md", BODY.encode("utf-8"), "text/markdown")},
                data={"category": "人事制度", "client_upload_id": str(uuid4())},
            )
            unit_id = int(upload.json()["data"]["unit_id"])
            task_index_id = int(upload.json()["data"]["task_id"])
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as session:
                lease = await claim_task(session, task_id=task_index_id, worker_id="t", now=utcnow())
                assert lease is not None
                assert (
                    await ingest_svc.run_pipeline(
                        session, task_id=task_index_id, lease_token=lease.lease_token
                    )
                )["status"] == "succeeded"
                revision = int(
                    (
                        await session.execute(
                            text("SELECT revision FROM knowledge_unit WHERE id=:u"),
                            {"u": unit_id},
                        )
                    ).scalar_one()
                )
            await client.put(
                f"/api/knowledge-units/{unit_id}/acl",
                headers=_auth(token),
                json={"global": True, "depts": [], "roles": [], "users": [],
                      "expected_revision": revision},
            )
            bound = await client.post(
                f"/api/supplement-tasks/{task_id}/source",
                headers=_auth(token),
                json={"unit_id": unit_id, "expected_revision": revision + 1},
            )
            assert bound.status_code == 200, bound.text

            verified = await client.post(
                f"/api/knowledge-gaps/{gap_id}/verify", headers=_auth(token)
            )
            assert verified.status_code == 200, verified.text
            assert verified.json()["data"]["passed"] is True
            assert verified.json()["data"]["state"] == "closed"

    run(_run())
    run(fakes["engine"].dispose())

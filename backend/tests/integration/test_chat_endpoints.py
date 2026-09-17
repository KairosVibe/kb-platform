"""M05 会话与流式问答端点的端到端集成测试（真实 MySQL + 真实 ASGI；模型与向量用替身）。

钉住的状态机与安全规则（PRD §1.2 第 3 条 / API-CONTRACTS §4）：
- 受理 202 只是登记；幂等三态（同键同载荷复用 / 异载荷 409 / 会话并发 409）；
- 执行侧身份从 DB 重装配（撤权立即生效），不信令牌快照；
- 全无权 → 固定拒答（denied 事件 + rejected + 受限消息），异常不冒充"无知识"；
- 终态唯一事务：请求/消息/来源/审计/用量/最终 done 事件同批提交；
- 取消是 DB 标记 + 生成循环协作观察，终态 cancelled；
- SSE：seq 单调、done 终止、after_seq 越界 422。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.core.security import hash_password
from app.db.session import get_session
from app.main import create_app
from app.providers import embedding as embedding_module
from app.providers import generation as generation_module
from app.providers import vector_store as vector_store_module
from app.services import chat_svc, ingest_svc
from app.services.auth_svc import _reset_login_limiter
from app.tasks.lease import claim_task
from app.db.base import utcnow
from tests.integration.conftest import it_dsn, run

PASSWORD = "integration-pass-1"
ADMIN_CODES = ("ai:ask", "kb:view", "kb:upload", "kb:edit", "kb:perm")
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


async def _seed() -> dict[str, int]:
    engine = create_async_engine(it_dsn(), poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _run() -> dict[str, int]:
        async with factory() as session:
            async with session.begin():

                async def _role(name: str, codes: tuple[str, ...]) -> int:
                    role_id = int(
                        (
                            await session.execute(
                                text(
                                    "INSERT INTO `role` (name, name_norm, revision, created_at, updated_at) "
                                    "VALUES (:n, :nn, 1, NOW(6), NOW(6))"
                                ),
                                {"n": name, "nn": name.casefold()},
                            )
                        ).lastrowid
                    )
                    for code in codes:
                        await session.execute(
                            text("INSERT INTO role_permission (role_id, code) VALUES (:r, :c)"),
                            {"r": role_id, "c": code},
                        )
                    return role_id

                async def _user(username: str, role_id: int) -> int:
                    user_id = int(
                        (
                            await session.execute(
                                text(
                                    "INSERT INTO `user` (username, username_norm, password_hash, dept_id, "
                                    "enabled, identity_revision, revision, created_at, updated_at) "
                                    "VALUES (:u, :n, :h, NULL, 1, 1, 1, NOW(6), NOW(6))"
                                ),
                                {"u": username, "n": username.casefold(),
                                 "h": hash_password(PASSWORD)},
                            )
                        ).lastrowid
                    )
                    await session.execute(
                        text("INSERT INTO user_role (user_id, role_id) VALUES (:u, :r)"),
                        {"u": user_id, "r": role_id},
                    )
                    return user_id

                admin_role = await _role("问答用户", ADMIN_CODES)
                # 集成库被清表后没有 config_revision（种子只跑演示库）——补最小快照
                await session.execute(
                    text(
                        "INSERT INTO config_revision (patch, snapshot, actor_id, created_at) "
                        "VALUES (:p, :s, NULL, NOW(6))"
                    ),
                    {"p": '{"source":"chat-test"}', "s": '{"vector_top_k":20}'},
                )
                return {
                    "admin_role_id": admin_role,
                    "admin_user_id": await _user("admin", admin_role),
                    "admin2_user_id": await _user("admin2", admin_role),
                }

    return await _run()


async def _token(client: AsyncClient, username: str = "admin") -> str:
    resp = await client.post(
        "/api/auth/login", json={"username": username, "password": PASSWORD}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _install_model_fakes(monkeypatch: pytest.MonkeyPatch, *, fail: bool = False) -> dict:
    """向量/生成替身 + 引擎工厂重定向到集成库（run_answer/stream_events 用）。"""
    settings = get_settings()
    dim = int(settings.embed_dim)
    counted: dict[tuple[int, int], int] = {}

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

        return ([vec(t) for t in texts],
                {"prompt_tokens": sum(len(t) for t in texts), "total_tokens": 0},
                f"{config.embed_provider}:{config.embed_model}:{config.embed_dim}")

    async def fake_search(config, *, query_vector, top_k):
        hits: list[dict] = []
        for (uid, ver), seqs in sorted(counted.items()):
            for seq in seqs:
                hits.append({"unit_id": uid, "version": ver, "seq": seq,
                             "score": 0.9 - seq * 0.01})
        return hits[:top_k]

    async def write_and_count(config, *, rows):
        counted[(rows[0]["unit_id"], rows[0]["version"])] = [r["seq"] for r in rows]
        return len(rows)

    def fake_count(config, *, unit_id, version, expected=None):
        return len(counted.get((unit_id, version), []))

    async def fake_stream(messages, config, signal):
        if fail:
            raise __import__("app.core.errors", fromlist=["TaskError"]).TaskError(
                "GENERATION_REJECTED", "模拟生成失败", transient=True
            )
        for piece in ("按", "据[1]，", "薪酬按月发放。"):
            if signal.cancelled:
                return
            # ★ 慢于取消轮询周期（0.5s）：否则生成瞬时而完，取消永远观察不到
            await asyncio.sleep(0.7)
            if signal.cancelled:
                return
            yield __import__("app.providers.generation", fromlist=["ModelDelta"]).ModelDelta(
                text=piece,
                usage=None if piece == "按" else {"prompt_tokens": 120, "completion_tokens": 9},
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
    return {"engine": engine, "counted": counted}


async def _run_pipeline(engine, task_id: int) -> dict:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        lease = await claim_task(session, task_id=task_id, worker_id="test", now=utcnow())
        assert lease is not None
        return await ingest_svc.run_pipeline(
            session, task_id=task_id, lease_token=lease.lease_token
        )


async def _make_unit(client: AsyncClient, token: str, *, global_acl: bool) -> dict:
    resp = await client.post(
        "/api/uploads",
        headers=_auth(token),
        files={"file": ("薪酬制度.md", BODY.encode("utf-8"), "text/markdown")},
        data={"category": "人事制度", "client_upload_id": str(uuid4())},
    )
    assert resp.status_code == 202, resp.text
    data = resp.json()["data"]
    unit_id, task_id = int(data["unit_id"]), int(data["task_id"])
    revision = await _unit_revision(client, token, unit_id)
    if global_acl:
        resp = await client.put(
            f"/api/knowledge-units/{unit_id}/acl",
            headers=_auth(token),
            json={"global": True, "depts": [], "roles": [], "users": [],
                  "expected_revision": revision},
        )
        assert resp.status_code == 200, resp.text
    return {"unit_id": unit_id, "task_id": task_id}


async def _unit_revision(client: AsyncClient, token: str, unit_id: int) -> int:
    resp = await client.get("/api/knowledge-units", headers=_auth(token))
    for item in resp.json()["data"]["items"]:
        if int(item["id"]) == unit_id:
            return int(item["revision"])
    raise AssertionError("unit missing")


def test_session_lifecycle_and_accept_idempotency(
    clean_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    fakes = _install_model_fakes(monkeypatch)

    async def _run() -> None:
        await _seed()
        async with _client() as client:
            token = await _token(client)
            resp = await client.post(
                "/api/sessions", headers=_auth(token), json={"title": "答疑"}
            )
            assert resp.status_code == 201, resp.text
            session_id = int(resp.json()["data"]["session_id"])

            listed = (
                await client.get("/api/sessions", headers=_auth(token))
            ).json()["data"]
            assert listed["total"] == 1 and listed["items"][0]["title"] == "答疑"

            renamed = await client.patch(
                f"/api/sessions/{session_id}",
                headers=_auth(token), json={"action": "rename", "title": "薪酬答疑"},
            )
            assert renamed.status_code == 200

            # ---- 受理与幂等 ----
            key = uuid4().hex
            first = await client.post(
                "/api/chat/requests",
                headers=_auth(token),
                json={"session_id": session_id, "client_request_id": key,
                      "question": "薪酬如何发放？"},
            )
            assert first.status_code == 202, first.text
            request_id = int(first.json()["data"]["request_id"])
            assert first.json()["data"]["status"] == "accepted"

            replay = await client.post(
                "/api/chat/requests",
                headers=_auth(token),
                json={"session_id": session_id, "client_request_id": key,
                      "question": "薪酬如何发放？"},
            )
            assert replay.status_code == 200, "同键同载荷复用"
            assert int(replay.json()["data"]["request_id"]) == request_id

            conflict = await client.post(
                "/api/chat/requests",
                headers=_auth(token),
                json={"session_id": session_id, "client_request_id": key,
                      "question": "换个问题"},
            )
            assert conflict.status_code == 409
            assert conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"

            busy = await client.post(
                "/api/chat/requests",
                headers=_auth(token),
                json={"session_id": session_id, "client_request_id": uuid4().hex,
                      "question": "另一个问题"},
            )
            assert busy.status_code == 409
            assert busy.json()["code"] == "SESSION_BUSY"

            too_long = await client.post(
                "/api/chat/requests",
                headers=_auth(token),
                json={"session_id": session_id, "client_request_id": uuid4().hex,
                      "question": "长" * 8001},
            )
            assert too_long.status_code == 422

            # 取消活动请求，让会话回到可用状态
            cancel = await client.post(
                f"/api/chat/requests/{request_id}/cancel", headers=_auth(token)
            )
            assert cancel.status_code == 200
            assert cancel.json()["data"]["status"] == "accepted"
            done_cancel = await client.post(
                f"/api/chat/requests/{request_id}/cancel", headers=_auth(token)
            )
            assert done_cancel.json()["data"]["status"] == "accepted", "重复取消幂等"

            deleted = await client.patch(
                f"/api/sessions/{session_id}",
                headers=_auth(token), json={"action": "delete"},
            )
            assert deleted.status_code == 200 and deleted.json()["data"]["deleted"] is True
            again = await client.get(f"/api/sessions/{session_id}/messages", headers=_auth(token))
            assert again.status_code == 404, "已删除会话按防枚举 404"

    run(_run())
    asyncio.get_event_loop_policy()
    engine = fakes["engine"]
    run(engine.dispose())


def test_run_answer_answered_and_events(
    clean_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    fakes = _install_model_fakes(monkeypatch)

    async def _run() -> None:
        await _seed()
        async with _client() as client:
            token = await _token(client)
            unit = await _make_unit(client, token, global_acl=True)
            assert (await _run_pipeline(fakes["engine"], unit["task_id"]))["status"] == "succeeded"

            session_id = int(
                (
                    await client.post("/api/sessions", headers=_auth(token), json={})
                ).json()["data"]["session_id"]
            )
            accepted = (
                await client.post(
                    "/api/chat/requests",
                    headers=_auth(token),
                    json={"session_id": session_id, "client_request_id": uuid4().hex,
                          "question": "薪酬如何发放？"},
                )
            ).json()["data"]
            request_id = int(accepted["request_id"])

            engine = fakes["engine"]
            factory = async_sessionmaker(engine, expire_on_commit=False)
            result = await chat_svc.run_answer(request_id, engine=engine)
            assert result["status"] == "completed" and result["result_type"] == "answered"

            # ---- 历史可读（来源授权通过）----
            history = (
                await client.get(
                    f"/api/sessions/{session_id}/messages", headers=_auth(token)
                )
            ).json()["data"]
            roles = [i["role"] for i in history["items"]]
            assert roles == ["user", "assistant"]
            assistant = history["items"][1]
            assert assistant["restricted"] is False
            assert "薪酬按月发放" in str(assistant["text"])

            # ---- 事件流：meta/citations/delta/done，seq 单调，done 终止 ----
            sse_frames: list[dict[str, Any]] = []
            async with client.stream(
                "GET",
                f"/api/chat/requests/{request_id}/events",
                headers=_auth(token),
                params={"after_seq": 0},
            ) as response:
                assert response.status_code == 200
                assert response.headers["content-type"].startswith("text/event-stream")
                current_event = None
                async for line in response.aiter_lines():
                    if line.startswith("event:"):
                        current_event = line[6:].strip()
                    elif line.startswith("data:") and current_event:
                        sse_frames.append({"event": current_event,
                                           **json.loads(line[5:].strip())})
                        current_event = None
            assert sse_frames[0]["event"] == "meta"
            assert any(f["event"] == "citations" for f in sse_frames)
            assert sum(1 for f in sse_frames if f["event"] == "delta") == 3
            assert sse_frames[-1]["event"] == "done"
            assert sse_frames[-1]["status"] == "completed"
            assert sse_frames[-1]["usage"]["prompt_tokens"] == 120

            # after_seq 越界 422
            bad = await client.get(
                f"/api/chat/requests/{request_id}/events",
                headers=_auth(token), params={"after_seq": 99999},
            )
            assert bad.status_code == 422

            # 引用详情
            citation = await client.get(
                f"/api/chat/requests/{request_id}/citations/1", headers=_auth(token)
            )
            assert citation.status_code == 200, citation.text
            assert citation.json()["data"]["title"] == "薪酬制度"

            # 他人请求 → 404 防枚举
            token2 = await _token(client, "admin2")
            other = await client.get(
                f"/api/chat/requests/{request_id}/citations/1", headers=_auth(token2)
            )
            assert other.status_code == 404

    run(_run())
    run(fakes["engine"].dispose())


def test_run_answer_access_restricted_and_no_evidence(
    clean_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    fakes = _install_model_fakes(monkeypatch)

    async def _run() -> None:
        await _seed()
        async with _client() as client:
            token = await _token(client)
            # ---- 全无权 → 固定拒答 ----
            unit = await _make_unit(client, token, global_acl=False)
            assert (await _run_pipeline(fakes["engine"], unit["task_id"]))["status"] == "succeeded"
            session_id = int(
                (await client.post("/api/sessions", headers=_auth(token), json={})).json()["data"]["session_id"]
            )
            request_id = int(
                (
                    await client.post(
                        "/api/chat/requests",
                        headers=_auth(token),
                        json={"session_id": session_id, "client_request_id": uuid4().hex,
                              "question": "薪酬如何发放？"},
                    )
                ).json()["data"]["request_id"]
            )
            result = await chat_svc.run_answer(request_id, engine=fakes["engine"])
            assert result["status"] == "rejected"
            assert result["result_type"] == "access_restricted"

            engine = fakes["engine"]
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as session:
                from app.models import ChatMessage

                assistant = (
                    await session.execute(
                        text(
                            "SELECT text, restricted FROM chat_message WHERE request_id=:r "
                            "AND role='assistant'"
                        ),
                        {"r": request_id},
                    )
                ).first()
                assert assistant.restricted == 1 and assistant.text is None
                denied_event = json.loads(
                    str(
                        (
                            await session.execute(
                                text(
                                    "SELECT payload FROM chat_event WHERE request_id=:r "
                                    "AND event='denied'"
                                ),
                                {"r": request_id},
                            )
                        ).scalar_one()
                    )
                )
                assert "权限" in denied_event["message"]
                audit = (
                    await session.execute(
                        text("SELECT status, denied_snapshot FROM qa_audit WHERE request_id=:r"),
                        {"r": request_id},
                    )
                ).first()
                assert audit.status == "rejected"
                denied_snapshot = json.loads(str(audit.denied_snapshot))
                assert unit["unit_id"] in [int(v) for v in denied_snapshot["unit_ids"]], (
                    "受限 ID 只进受控审计"
                )

            # ---- 无证据 → 固定说明，不走模型 ----
            session_id2 = int(
                (await client.post("/api/sessions", headers=_auth(token), json={})).json()["data"]["session_id"]
            )
            request_id2 = int(
                (
                    await client.post(
                        "/api/chat/requests",
                        headers=_auth(token),
                        json={"session_id": session_id2, "client_request_id": uuid4().hex,
                              "question": "完全无关的问题？"},
                    )
                ).json()["data"]["request_id"]
            )
            fakes["counted"].clear()  # 向量替身不再返回候选
            result2 = await chat_svc.run_answer(request_id2, engine=engine)
            assert result2["status"] == "completed"
            assert result2["result_type"] == "no_evidence"

    run(_run())
    run(fakes["engine"].dispose())


def test_cancel_and_recover(clean_db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    fakes = _install_model_fakes(monkeypatch)

    async def _run() -> None:
        await _seed()
        async with _client() as client:
            token = await _token(client)
            unit = await _make_unit(client, token, global_acl=True)
            assert (await _run_pipeline(fakes["engine"], unit["task_id"]))["status"] == "succeeded"
            session_id = int(
                (await client.post("/api/sessions", headers=_auth(token), json={})).json()["data"]["session_id"]
            )
            request_id = int(
                (
                    await client.post(
                        "/api/chat/requests",
                        headers=_auth(token),
                        json={"session_id": session_id, "client_request_id": uuid4().hex,
                              "question": "薪酬如何发放？"},
                    )
                ).json()["data"]["request_id"]
            )
            # 预置取消标记：生成循环应在第一个 delta 后观察到并终止
            await client.post(f"/api/chat/requests/{request_id}/cancel", headers=_auth(token))
            result = await chat_svc.run_answer(request_id, engine=fakes["engine"])
            assert result["status"] == "cancelled"

            engine = fakes["engine"]
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as session:
                row = (
                    await session.execute(
                        text("SELECT status, result_type FROM chat_request WHERE id=:r"),
                        {"r": request_id},
                    )
                ).first()
                assert (row.status, row.result_type[0] if isinstance(row.result_type, list) else row.result_type) == ("cancelled", None) or row.status == "cancelled"

                # H29：孤儿 running → failed
                await session.execute(
                    text(
                        "INSERT INTO chat_request (user_id, auth_session_id, session_id, "
                        "client_request_id, payload_hash, question, status, config_revision, "
                        "execution_lease_until, last_seq, cancel_requested, accepted_at, created_at, updated_at) "
                        "SELECT user_id, auth_session_id, session_id, 'orphan', 'x', 'q', 'running', "
                        "config_revision, NOW(6) - INTERVAL 1 HOUR, 0, 0, NOW(6), NOW(6), NOW(6) "
                        "FROM chat_request WHERE id=:r"
                    ),
                    {"r": request_id},
                )
                orphan_id = int(
                    (
                        await session.execute(
                            text("SELECT id FROM chat_request WHERE client_request_id='orphan'")
                        )
                    ).scalar_one()
                )
                from app.services import chat_store

                await chat_store.recover_requests(session, utcnow())
                orphan = (
                    await session.execute(
                        text("SELECT status FROM chat_request WHERE id=:r"), {"r": orphan_id}
                    )
                ).scalar_one()
                assert orphan == "failed", "孤儿 running 条件终结 failed"

    run(_run())
    run(fakes["engine"].dispose())

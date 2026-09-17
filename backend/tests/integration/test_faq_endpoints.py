"""M06 FAQ 沉淀审核与缓存端到端集成测试（真实 MySQL；模型/向量用替身）。

钉住的规则：
- 挖掘管线：领取→向量化→聚类→起草→提交，相似问题归簇、答案起草不占长事务；
- FAQ 无独立 ACL：publish/匹配逐次复核来源授权（撤权即失效）；
- 缓存开关与审核状态独立；来源失效 → published 转 stale，匹配不再直出。
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.core.security import hash_password
from app.db.base import utcnow
from app.db.session import get_session
from app.main import create_app
from app.providers import embedding as embedding_module
from app.providers import generation as generation_module
from app.providers import vector_store as vector_store_module
from app.services import chat_svc, faq_svc, ingest_svc
from app.services.auth_svc import _reset_login_limiter
from app.tasks.lease import claim_task
from tests.integration.conftest import it_dsn, run

PASSWORD = "integration-pass-1"
ADMIN_CODES = ("ai:ask", "kb:view", "kb:upload", "kb:edit", "kb:perm", "faq:review", "faq:publish")
BODY = "# 薪酬制度\n\n" + "".join(
    f"第{i}条 员工薪酬按月发放，逢节假日提前，标准由财务部公布。" for i in range(1, 40)
)


async def _it_session():
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
                            "VALUES ('faq用户', 'faq用户', 1, NOW(6), NOW(6))"
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
    return resp.json()["data"]["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _install_fakes(monkeypatch: pytest.MonkeyPatch) -> dict:
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
        yield __import__("app.providers.generation", fromlist=["ModelDelta"]).ModelDelta(
            text="标准答案：按月发放。",
            usage={"prompt_tokens": 50, "completion_tokens": 8}, finish_reason="stop",
        )

    monkeypatch.setattr(embedding_module, "embed_batches", fake_embed)
    monkeypatch.setattr(vector_store_module, "search_similar", fake_search)
    monkeypatch.setattr(vector_store_module, "upsert_version_chunks", write_and_count)
    monkeypatch.setattr(vector_store_module, "count_version_chunks", fake_count)
    monkeypatch.setattr(generation_module, "stream_llm", fake_stream)
    monkeypatch.setattr(chat_svc, "spawn_answer", lambda request_id: None)
    monkeypatch.setattr(faq_svc, "spawn_mining", lambda run_id: None)

    engine = create_async_engine(it_dsn(), poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(chat_svc, "_engine_factory", lambda: (engine, factory))
    monkeypatch.setattr(faq_svc, "_engine_factory", lambda: (engine, factory))
    return {"engine": engine, "stored": stored}


async def _indexed_unit(client: AsyncClient, token: str, engine) -> int:
    upload = await client.post(
        "/api/uploads",
        headers=_auth(token),
        files={"file": ("薪酬制度.md", BODY.encode("utf-8"), "text/markdown")},
        data={"category": "人事制度", "client_upload_id": str(uuid4())},
    )
    unit_id, task_id = int(upload.json()["data"]["unit_id"]), int(upload.json()["data"]["task_id"])
    revision = int(
        (
            await client.get("/api/knowledge-units", headers=_auth(token))
        ).json()["data"]["items"][0]["revision"]
    )
    await client.put(
        f"/api/knowledge-units/{unit_id}/acl",
        headers=_auth(token),
        json={"global": True, "depts": [], "roles": [], "users": [],
              "expected_revision": revision},
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        lease = await claim_task(session, task_id=task_id, worker_id="t", now=utcnow())
        assert (
            await ingest_svc.run_pipeline(session, task_id=task_id, lease_token=lease.lease_token)
        )["status"] == "succeeded"
    return unit_id


async def _ask(client: AsyncClient, token: str, engine, question: str) -> int:
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
    assert result["result_type"] == "answered", result
    return request_id


def test_faq_mining_publish_match_invalidate(
    clean_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    fakes = _install_fakes(monkeypatch)

    async def _run() -> None:
        await _seed()
        async with _client() as client:
            token = await _token(client)
            unit_id = await _indexed_unit(client, token, fakes["engine"])
            # ★ 两次提问用**同一文本**：替身向量由文本哈希派生，不同文本的余弦
            #   近似为 0、永不达到聚类门槛——同文本才能验证"相似归簇"路径。
            await _ask(client, token, fakes["engine"], "薪酬怎么发放？")
            await _ask(client, token, fakes["engine"], "薪酬怎么发放？")

            # ---- F-06.01：挖掘 ----
            run_resp = await client.post(
                "/api/mining/runs", headers=_auth(token),
                json={"pipeline_version": "m06.r1"},
            )
            assert run_resp.status_code == 202, run_resp.text
            run_id = int(run_resp.json()["data"]["run_id"])
            result = await faq_svc.run_mining(run_id, engine=fakes["engine"])
            assert result["consumed"] == 2 and result["candidates"] == 1, result

            # 候选 FAQ 与来源已生成
            engine = fakes["engine"]
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as session:
                faq = (
                    await session.execute(
                        text("SELECT id, status, answer FROM faq WHERE status='candidate'")
                    )
                ).first()
                assert faq is not None and "标准答案" in faq.answer
                faq_id = int(faq.id)
                sources = (
                    await session.execute(
                        text("SELECT unit_id, version FROM faq_source WHERE faq_id=:f"),
                        {"f": faq_id},
                    )
                ).all()
                assert len(sources) == 1 and int(sources[0].unit_id) == unit_id

            # ★ 每次读取都开**新 session**：复用已 close 的 session 时 autobegin
            #   事务快照停在旧值（REPEATABLE READ），实测 revision 永远读到 1。
            async with factory() as session:
                revision = int(
                    (
                        await session.execute(
                            text("SELECT revision FROM faq WHERE id=:f"), {"f": faq_id}
                        )
                    ).scalar_one()
                )

            # ---- F-06.03：发布 ----
            published = await client.post(
                f"/api/faqs/{faq_id}/publish",
                headers=_auth(token), json={"expected_revision": revision},
            )
            assert published.status_code == 200, published.text

            # ---- F-06.05：开缓存 ----
            async with factory() as session:
                revision = int(
                    (
                        await session.execute(
                            text("SELECT revision FROM faq WHERE id=:f"), {"f": faq_id}
                        )
                    ).scalar_one()
                )
            toggled = await client.put(
                f"/api/faqs/{faq_id}/cache-enabled",
                headers=_auth(token),
                json={"enabled": True, "expected_revision": revision},
            )
            assert toggled.status_code == 200, (
                f"toggle={toggled.text!r} revision_used={revision}"
            )

            # ---- F-06.06：精确匹配直出 ----
            async with factory() as session:
                from app.engines.permission import UserCtx
                from app.models import User

                user = (
                    await session.execute(text("SELECT id, dept_id FROM `user` LIMIT 1"))
                ).first()
                ctx = UserCtx(
                    user_id=int(user.id), session_id=uuid4(), dept_id=None,
                    role_ids=frozenset(), permission_codes=frozenset({"ai:ask"}),
                    identity_revision=1,
                )
                match = await faq_svc.match_authorized(session, ctx, "薪酬怎么发放？")
                assert match["hit"] is True and "标准答案" in match["answer"]
                fakes_stored = stored_all = None

            # ---- F-06.07：来源失效 → stale，匹配不再直出 ----
            new_content = ("# 薪酬制度（修订）\n\n" + "".join(
                f"第{i}条 薪酬结构调整说明。" for i in range(1, 40)
            )).encode("utf-8")
            rev_now = int(
                (
                    await client.get("/api/knowledge-units", headers=_auth(token))
                ).json()["data"]["items"][0]["revision"]
            )
            replaced = await client.post(
                f"/api/knowledge-units/{unit_id}/versions",
                headers=_auth(token),
                files={"file": ("薪酬制度.md", new_content, "text/markdown")},
                data={"expected_revision": str(rev_now)},
            )
            assert replaced.status_code == 200
            async with factory() as session:
                # ★ 失效已由替换路由（F-06.07 接线）完成：这里的手动调用是幂等复核——
                #   published 已转 stale，故 staled=0（接线生效的证据，而非失效缺失）。
                invalidation = await faq_svc.invalidate_sources(
                    session, unit_id=unit_id, change="content"
                )
                assert invalidation["staled"] == 0
                status_now = (
                    await session.execute(
                        text("SELECT status FROM faq WHERE id=:f"), {"f": faq_id}
                    )
                ).scalar_one()
                assert status_now == "stale"
                match_after = await faq_svc.match_authorized(
                    session, ctx, "薪酬怎么发放？"
                )
                assert match_after["hit"] is False, "stale 后不再直出"

    run(_run())
    run(fakes["engine"].dispose())


def test_faq_status_flow(clean_db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    fakes = _install_fakes(monkeypatch)

    async def _run() -> None:
        await _seed()
        async with _client() as client:
            token = await _token(client)
            await _indexed_unit(client, token, fakes["engine"])
            await _ask(client, token, fakes["engine"], "薪酬怎么发放？")
            run_resp = await client.post(
                "/api/mining/runs", headers=_auth(token), json={"pipeline_version": "m06.r1"}
            )
            run_id = int(run_resp.json()["data"]["run_id"])
            await faq_svc.run_mining(run_id, engine=fakes["engine"])
            engine = fakes["engine"]
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as session:
                faq_id, revision = (
                    (
                        await session.execute(
                            text("SELECT id, revision FROM faq WHERE status='candidate' LIMIT 1")
                        )
                    ).first()
                )
                faq_id, revision = int(faq_id), int(revision)

            # 理由必填
            no_reason = await client.post(
                f"/api/faqs/{faq_id}/status", headers=_auth(token),
                json={"action": "reject", "reason": "  ", "expected_revision": revision},
            )
            assert no_reason.status_code == 422

            rejected = await client.post(
                f"/api/faqs/{faq_id}/status", headers=_auth(token),
                json={"action": "reject", "reason": "答案不完整", "expected_revision": revision},
            )
            assert rejected.status_code == 200
            assert rejected.json()["data"]["status"] == "rejected"

            # published 才能下线 → 当前 rejected，409
            offline = await client.post(
                f"/api/faqs/{faq_id}/status", headers=_auth(token),
                json={"action": "offline", "reason": "x", "expected_revision": revision + 1},
            )
            assert offline.status_code == 409

            resubmit = await client.post(
                f"/api/faqs/{faq_id}/status", headers=_auth(token),
                json={"action": "resubmit", "reason": "已补充", "expected_revision": revision + 1},
            )
            assert resubmit.status_code == 200
            assert resubmit.json()["data"]["status"] == "candidate"

    run(_run())
    run(fakes["engine"].dispose())

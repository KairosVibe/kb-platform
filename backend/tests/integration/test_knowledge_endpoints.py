"""M04 知识生命周期与四维权限端点的端到端集成测试（真实 MySQL + 真实 ASGI 应用）。

与 test_ingest_endpoints.py 同一基建。每条用例钉住一条会悄悄失守的契约：

- 防枚举：数据读权不过 → **404**（与"不存在"同响应），不是 403——403 会披露存在性；
- 默认拒绝：新建单元 ACL 全空，**任何人**（含上传者自己）都读不到正文，直到显式授权；
- 乐观锁：所有管理写操作受 `expected_revision` 保护，并发只有一个生效；
- 版本只增不改：替换/切片编辑生成新版本，旧版本切片原地不动；
- 重存源文件：切片编辑必须把编辑后的文本写回存储，否则流水线重索引会静默抹掉编辑。
"""

from __future__ import annotations

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
from app.db.base import utcnow
from app.db.session import get_session
from app.main import create_app
from app.providers import embedding as embedding_module
from app.providers import vector_store as vector_store_module
from app.services import ingest_svc
from app.services.auth_svc import _reset_login_limiter
from app.tasks.lease import claim_task
from tests.integration.conftest import it_dsn, run

PASSWORD = "integration-pass-1"
ADMIN_CODES = ("kb:view", "kb:upload", "kb:edit", "kb:delete", "kb:perm", "sys:role")

# ★ 足够长以触发默认 chunk_config（size=500）下的多窗切片——否则流水线只产出
#   1 个切片，切分/多版本用例无从谈起（实测 11 句 ≈ 230 token 不足 500）。
LONG_BODY = "# 薪酬制度\n\n" + "".join(
    f"第{i}条 员工薪酬按月发放，逢节假日提前，具体标准由财务部另行公布并解释。" for i in range(1, 40)
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
    """1 部门 + 2 角色（管理员含 kb:* / 访客仅 ai:ask）+ 3 账号 + 1 停用账号。"""
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

                async def _user(username: str, role_id: int, *, enabled: bool = True) -> int:
                    user_id = int(
                        (
                            await session.execute(
                                text(
                                    "INSERT INTO `user` (username, username_norm, password_hash, dept_id, "
                                    "enabled, identity_revision, revision, created_at, updated_at) "
                                    "VALUES (:u, :n, :h, :d, :e, 1, 1, NOW(6), NOW(6))"
                                ),
                                {
                                    "u": username,
                                    "n": username.casefold(),
                                    "h": hash_password(PASSWORD),
                                    "d": None,
                                    "e": int(enabled),
                                },
                            )
                        ).lastrowid
                    )
                    await session.execute(
                        text("INSERT INTO user_role (user_id, role_id) VALUES (:u, :r)"),
                        {"u": user_id, "r": role_id},
                    )
                    return user_id

                dept_id = int(
                    (
                        await session.execute(
                            text(
                                "INSERT INTO department (name, parent_id, revision, created_at, updated_at) "
                                "VALUES ('财务部', NULL, 1, NOW(6), NOW(6))"
                            )
                        )
                    ).lastrowid
                )
                admin_role = await _role("知识管理员", ADMIN_CODES)
                await _role("访客", ("ai:ask",))
                return {
                    "dept_id": dept_id,
                    "admin_role_id": admin_role,
                    "admin_user_id": await _user("admin", admin_role),
                    # 与 admin 同权限（含 kb:view）但默认无数据读权：防枚举 404 的正面对照。
                    "admin2_user_id": await _user("admin2", admin_role),
                    "viewer_user_id": await _user("viewer", await _role_id(session, "访客")),
                    "disabled_user_id": await _user(
                        "ghost", admin_role, enabled=False
                    ),
                }

    return await _run()


async def _role_id(session: Any, name: str) -> int:
    return int(
        (
            await session.execute(
                text("SELECT id FROM `role` WHERE name_norm=:n"), {"n": name.casefold()}
            )
        ).scalar_one()
    )


async def _token(client: AsyncClient, username: str = "admin") -> str:
    resp = await client.post(
        "/api/auth/login", json={"username": username, "password": PASSWORD}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _file_part(content: bytes = LONG_BODY.encode("utf-8"), name: str = "薪酬制度.md") -> dict:
    return {"file": (name, content, "text/markdown")}


async def _upload(
    client: AsyncClient, token: str, *, content: bytes = LONG_BODY.encode("utf-8"),
    category: str = "人事制度", name: str = "薪酬制度.md",
) -> dict:
    resp = await client.post(
        "/api/uploads",
        headers=_auth(token),
        files=_file_part(content, name=name),
        data={"category": category, "client_upload_id": str(uuid4())},
    )
    assert resp.status_code == 202, resp.text
    return resp.json()["data"]


async def _unit_revision(client: AsyncClient, token: str, unit_id: int) -> int:
    resp = await client.get(
        "/api/knowledge-units", headers=_auth(token), params={"q": ""}
    )
    assert resp.status_code == 200, resp.text
    for item in resp.json()["data"]["items"]:
        if int(item["id"]) == unit_id:
            return int(item["revision"])
    raise AssertionError(f"unit {unit_id} not in list")


async def _set_global_acl(client: AsyncClient, token: str, unit_id: int, revision: int) -> None:
    resp = await client.put(
        f"/api/knowledge-units/{unit_id}/acl",
        headers=_auth(token),
        json={"global": True, "depts": [], "roles": [], "users": [], "expected_revision": revision},
    )
    assert resp.status_code == 200, resp.text


def _install_fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    """流水线替身（与 test_pipeline 同款）：确定性向量 + 内存计数。"""
    settings = get_settings()
    dim = int(settings.embed_dim)

    async def fake_embed(texts, config):
        from app.tasks.parsing import estimate_tokens as _est
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

        return (
            [vec(t) for t in texts],
            {"prompt_tokens": sum(_est(t) for t in texts), "total_tokens": 0},
            f"{config.embed_provider}:{config.embed_model}:{config.embed_dim}",
        )

    counted: dict[tuple[int, int], int] = {}

    async def write_and_count(config, *, rows):
        counted[(rows[0]["unit_id"], rows[0]["version"])] = len(rows)
        return len(rows)

    def fake_count(config, *, unit_id, version, expected=None):
        return counted.get((unit_id, version), 0)

    monkeypatch.setattr(embedding_module, "embed_batches", fake_embed)
    monkeypatch.setattr(vector_store_module, "upsert_version_chunks", write_and_count)
    monkeypatch.setattr(vector_store_module, "count_version_chunks", fake_count)


async def _run_pipeline(task_id: int) -> dict:
    engine = create_async_engine(it_dsn(), poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            lease = await claim_task(session, task_id=task_id, worker_id="test", now=utcnow())
            assert lease is not None
            return await ingest_svc.run_pipeline(
                session, task_id=task_id, lease_token=lease.lease_token
            )
    finally:
        await engine.dispose()


# ---------------------------------------------------------------- F-04.01 / F-04.02


def test_list_units_and_update_metadata(clean_db: None) -> None:
    async def _run() -> None:
        ids = await _seed()
        async with _client() as client:
            token = await _token(client)
            unit_a = await _upload(client, token, category="人事制度")
            unit_b = await _upload(
                client, token,
                content="# 报销办法\n\n差旅报销条款。".encode("utf-8"),
                category="财务制度",
                name="报销办法.md",
            )

            resp = await client.get("/api/knowledge-units", headers=_auth(token))
            assert resp.status_code == 200
            data = resp.json()["data"]
            assert data["total"] == 2
            item = next(i for i in data["items"] if int(i["id"]) == unit_a["unit_id"])
            assert set(item) == {
                "id", "code", "title", "format", "category", "acl_tags",
                "updated_at", "enabled", "index_status", "revision",
            }
            assert item["acl_tags"] == [] and item["index_status"] == "pending"
            revision = int(item["revision"])

            filtered = (
                await client.get(
                    "/api/knowledge-units", headers=_auth(token), params={"category": "财务制度"}
                )
            ).json()["data"]
            assert filtered["total"] == 1
            assert int(filtered["items"][0]["id"]) == unit_b["unit_id"]

            by_q = (
                await client.get(
                    "/api/knowledge-units", headers=_auth(token), params={"q": "报销"}
                )
            ).json()["data"]
            assert by_q["total"] == 1 and int(by_q["items"][0]["id"]) == unit_b["unit_id"]

            # ---- F-04.02 ----
            resp = await client.patch(
                f"/api/knowledge-units/{unit_a['unit_id']}",
                headers=_auth(token),
                json={"title": "薪酬管理制度（2026 修订）", "category": "人事制度", "expected_revision": revision},
            )
            assert resp.status_code == 200, resp.text
            assert resp.json()["data"]["revision"] == revision + 1

            stale = await client.patch(
                f"/api/knowledge-units/{unit_a['unit_id']}",
                headers=_auth(token),
                json={"title": "再次修改", "category": "人事制度", "expected_revision": revision},
            )
            assert stale.status_code == 409
            assert stale.json()["code"] == "REVISION_CONFLICT"

            blank = await client.patch(
                f"/api/knowledge-units/{unit_a['unit_id']}",
                headers=_auth(token),
                json={"title": "   ", "category": "人事制度", "expected_revision": revision + 1},
            )
            assert blank.status_code == 422

            # 访客无 kb:view → 403（功能权限层）
            viewer = await _token(client, "viewer")
            forbidden = await client.get("/api/knowledge-units", headers=_auth(viewer))
            assert forbidden.status_code == 403

    run(_run())


# ---------------------------------------------------------------- F-04.03 / F-04.08 / API-S04


def test_data_read_is_404_until_authorized(clean_db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """默认拒绝 + 防枚举：无数据读权 404；授权后可读；global 对所有人生效。"""
    _install_fakes(monkeypatch)

    async def _run() -> None:
        ids = await _seed()
        async with _client() as client:
            admin = await _token(client)
            admin2 = await _token(client, "admin2")
            viewer = await _token(client, "viewer")
            unit = await _upload(client, admin)
            unit_id = int(unit["unit_id"])
            revision = await _unit_revision(client, admin, unit_id)
            assert (await _run_pipeline(unit["task_id"]))["status"] == "succeeded"

            # 默认拒绝：上传者自己也读不到正文（AC-04.08-01，无创建者旁路）
            denied = await client.get(
                f"/api/knowledge-units/{unit_id}/chunks", headers=_auth(admin)
            )
            assert denied.status_code == 404
            assert denied.json()["code"] == "NOT_FOUND"
            assert "text" not in (denied.json()["data"] or {})

            # 同权限的他人：同样 404（防枚举，不披露存在性）
            assert (
                await client.get(
                    f"/api/knowledge-units/{unit_id}/chunks", headers=_auth(admin2)
                )
            ).status_code == 404

            # 无 kb:view：403（功能层，发生在数据层之前）
            assert (
                await client.get(
                    f"/api/knowledge-units/{unit_id}/chunks", headers=_auth(viewer)
                )
            ).status_code == 403

            # 配置 global → 所有人可读
            await _set_global_acl(client, admin, unit_id, revision)
            ok_admin = await client.get(
                f"/api/knowledge-units/{unit_id}/chunks", headers=_auth(admin)
            )
            assert ok_admin.status_code == 200
            assert ok_admin.json()["data"]["total"] >= 1, "流水线替身应已产出切片"
            ok_admin2 = await client.get(
                f"/api/knowledge-units/{unit_id}/chunks", headers=_auth(admin2)
            )
            assert ok_admin2.status_code == 200
            chunk = ok_admin2.json()["data"]["items"][0]
            assert set(chunk) == {"chunk_id", "version", "seq", "text", "page_no", "offset"}

            # API-S04 回填
            acl = await client.get(f"/api/knowledge-units/{unit_id}/acl", headers=_auth(admin))
            assert acl.status_code == 200
            assert acl.json()["data"]["global"] is True

    run(_run())


def test_update_acl_entity_validation_and_user_dimension(
    clean_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F-04.08：无效实体 422；停用账号不可授权；个人维度放行；acl_version 递增。"""
    _install_fakes(monkeypatch)

    async def _run() -> None:
        ids = await _seed()
        async with _client() as client:
            admin = await _token(client)
            unit = await _upload(client, admin)
            unit_id = int(unit["unit_id"])
            revision = await _unit_revision(client, admin, unit_id)

            bad_entity = await client.put(
                f"/api/knowledge-units/{unit_id}/acl",
                headers=_auth(admin),
                json={"global": False, "depts": [99999999], "roles": [], "users": [],
                      "expected_revision": revision},
            )
            assert bad_entity.status_code == 422
            assert bad_entity.json()["code"] == "ACL_ENTITY_INVALID"

            disabled = await client.put(
                f"/api/knowledge-units/{unit_id}/acl",
                headers=_auth(admin),
                json={"global": False, "depts": [], "roles": [], "users": [ids["disabled_user_id"]],
                      "expected_revision": revision},
            )
            assert disabled.status_code == 422, "停用账号不能被授权"

            resp = await client.put(
                f"/api/knowledge-units/{unit_id}/acl",
                headers=_auth(admin),
                json={"global": False, "depts": [], "roles": [ids["admin_role_id"]],
                      "users": [ids["admin2_user_id"]], "expected_revision": revision},
            )
            assert resp.status_code == 200, resp.text
            assert resp.json()["data"]["acl_version"] == 2

            conflict = await client.put(
                f"/api/knowledge-units/{unit_id}/acl",
                headers=_auth(admin),
                json={"global": True, "depts": [], "roles": [], "users": [],
                      "expected_revision": revision},
            )
            assert conflict.status_code == 409

            # 个人维度：admin2 通过 user 维度获得读权
            ok2 = await client.get(
                f"/api/knowledge-units/{unit_id}/chunks", headers=_auth(await _token(client, "admin2"))
            )
            assert ok2.status_code == 200

            # F-04.09：实体选择（user 维度过滤停用账号）
            entities = await client.get(
                "/api/acl-entities", headers=_auth(admin), params={"kind": "user"}
            )
            assert entities.status_code == 200
            labels = [i["label"] for i in entities.json()["data"]["items"]]
            assert "ghost" not in labels, "停用账号不得出现在可选列表"
            bad_kind = await client.get(
                "/api/acl-entities", headers=_auth(admin), params={"kind": "team"}
            )
            assert bad_kind.status_code == 422

    run(_run())


# ---------------------------------------------------------------- F-04.06 / F-04.07


def test_set_enabled_and_delete_unit(clean_db: None) -> None:
    async def _run() -> None:
        await _seed()
        async with _client() as client:
            admin = await _token(client)
            unit = await _upload(client, admin)
            unit_id = int(unit["unit_id"])
            revision = await _unit_revision(client, admin, unit_id)

            disable = await client.put(
                f"/api/knowledge-units/{unit_id}/enabled",
                headers=_auth(admin),
                json={"enabled": False, "expected_revision": revision},
            )
            assert disable.status_code == 200
            new_revision = int(disable.json()["data"]["revision"])

            reenable = await client.put(
                f"/api/knowledge-units/{unit_id}/enabled",
                headers=_auth(admin),
                json={"enabled": True, "expected_revision": new_revision},
            )
            assert reenable.status_code == 409
            assert reenable.json()["code"] == "INDEX_NOT_READY", "未索引的知识不能启用"

            listed = (
                await client.get(
                    "/api/knowledge-units", headers=_auth(admin), params={"enabled": "false"}
                )
            ).json()["data"]
            assert listed["total"] == 1

            # 删除：墓碑 + 清理任务；重复删除幂等。
            # ★ httpx 的 .delete() 不接受 json body，带 body 的删除必须用 .request()。
            first = await client.request(
                "DELETE",
                f"/api/knowledge-units/{unit_id}",
                headers=_auth(admin),
                json={"expected_revision": new_revision},
            )
            assert first.status_code == 200, first.text
            deletion_id = int(first.json()["data"]["deletion_id"])
            assert first.json()["data"]["cleanup_status"] == "queued"

            again = await client.request(
                "DELETE",
                f"/api/knowledge-units/{unit_id}",
                headers=_auth(admin),
                json={"expected_revision": new_revision},
            )
            assert again.status_code == 200
            assert int(again.json()["data"]["deletion_id"]) == deletion_id, "重复删除不得新建任务"

            assert (
                await client.get("/api/knowledge-units", headers=_auth(admin))
            ).json()["data"]["total"] == 0, "墓碑单元不出现在台账"
            patched = await client.patch(
                f"/api/knowledge-units/{unit_id}",
                headers=_auth(admin),
                json={"title": "x", "category": "y", "expected_revision": new_revision},
            )
            assert patched.status_code == 404

            engine = create_async_engine(it_dsn(), poolclass=NullPool)
            try:
                async with engine.connect() as conn:
                    tomb = (
                        await conn.execute(
                            text("SELECT is_deleted FROM knowledge_unit WHERE id=:u"),
                            {"u": unit_id},
                        )
                    ).scalar_one()
                    assert int(tomb) == 1
                    task = (
                        await conn.execute(
                            text(
                                "SELECT status, unit_id FROM cleanup_task WHERE id=:d"
                            ),
                            {"d": deletion_id},
                        )
                    ).first()
                    assert task is not None and task.status == "queued"
            finally:
                await engine.dispose()

    run(_run())


# ---------------------------------------------------------------- F-04.04 / F-04.05


def test_replace_document_creates_next_version_and_keeps_acl(
    clean_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fakes(monkeypatch)

    async def _run() -> None:
        await _seed()
        async with _client() as client:
            admin = await _token(client)
            unit = await _upload(client, admin)
            unit_id = int(unit["unit_id"])
            revision = await _unit_revision(client, admin, unit_id)

            await client.put(
                f"/api/knowledge-units/{unit_id}/acl",
                headers=_auth(admin),
                json={"global": False, "depts": [], "roles": [], "users": [1],
                      "expected_revision": revision},
            )
            # ACL 更新已推进 revision，替换文档必须取**最新值**（实测踩过）
            current_revision = await _unit_revision(client, admin, unit_id)

            resp = await client.post(
                f"/api/knowledge-units/{unit_id}/versions",
                headers=_auth(admin),
                files=_file_part("# 报销制度（新版）\n\n全部重写的内容。".encode("utf-8"),
                                 name="报销制度.md"),
                data={"expected_revision": str(current_revision)},
            )
            assert resp.status_code == 200, resp.text
            data = resp.json()["data"]
            assert data["target_version"] == 2 and data["task_id"] > 0

            engine = create_async_engine(it_dsn(), poolclass=NullPool)
            try:
                async with engine.connect() as conn:
                    row = (
                        await conn.execute(
                            text(
                                "SELECT content_version, index_status, revision FROM knowledge_unit "
                                "WHERE id=:u"
                            ),
                            {"u": unit_id},
                        )
                    ).first()
                    assert (int(row.content_version), row.index_status) == (2, "pending")
                    versions = await conn.scalar(
                        text("SELECT COUNT(*) FROM knowledge_version WHERE unit_id=:u"),
                        {"u": unit_id},
                    )
                    assert int(versions) == 2
                    acl = await conn.scalar(
                        text("SELECT COUNT(*) FROM knowledge_acl_user WHERE unit_id=:u"),
                        {"u": unit_id},
                    )
                    assert int(acl) == 1, "替换文档保留 ACL"
            finally:
                await engine.dispose()

            conflict = await client.post(
                f"/api/knowledge-units/{unit_id}/versions",
                headers=_auth(admin),
                files=_file_part(name="again.md"),
                data={"expected_revision": str(current_revision)},
            )
            assert conflict.status_code == 409

    run(_run())


def test_mutate_chunks_edit_split_and_stable_old_version(
    clean_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """切片编辑：新版本 + 旧版本不动 + 编辑后重跑流水线不丢编辑（重存源文件）。"""
    _install_fakes(monkeypatch)

    async def _run() -> None:
        await _seed()
        async with _client() as client:
            admin = await _token(client)
            unit = await _upload(client, admin)
            unit_id, task_id = int(unit["unit_id"]), int(unit["task_id"])
            revision = await _unit_revision(client, admin, unit_id)
            await _set_global_acl(client, admin, unit_id, revision)
            result = await _run_pipeline(task_id)
            assert result["status"] == "succeeded"

            chunks = (
                await client.get(
                    f"/api/knowledge-units/{unit_id}/chunks", headers=_auth(admin)
                )
            ).json()["data"]
            old_rows = [(int(c["chunk_id"]), c["text"]) for c in chunks["items"]]
            assert chunks["total"] >= 2

            # ---- edit ----
            target_id, target_text = old_rows[0]
            resp = await client.post(
                f"/api/knowledge-units/{unit_id}/chunk-mutations",
                headers=_auth(admin),
                json={"chunk_id": target_id, "action": "edit",
                      "text": target_text + "【人工补充条款】", "expected_revision": revision + 1},
            )
            assert resp.status_code == 200, resp.text
            new_task = int(resp.json()["data"]["task_id"])
            assert int(resp.json()["data"]["target_version"]) == 2

            # 旧版本切片不动
            v1 = (
                await client.get(
                    f"/api/knowledge-units/{unit_id}/chunks",
                    headers=_auth(admin), params={"version": 1},
                )
            ).json()["data"]
            assert [(int(c["chunk_id"]), c["text"]) for c in v1["items"]] == old_rows

            # 流水线重跑新版本：编辑必须保留（源文件已重存），且任务成功
            pipeline_result = await _run_pipeline(new_task)
            assert pipeline_result["status"] == "succeeded", pipeline_result
            v2 = (
                await client.get(
                    f"/api/knowledge-units/{unit_id}/chunks",
                    headers=_auth(admin), params={"version": 2},
                )
            ).json()["data"]
            assert any("【人工补充条款】" in c["text"] for c in v2["items"]), (
                "重索引后人工编辑必须保留"
            )

            # ---- split：越界与空拆分 422 ----
            # ★ edit 之后 content_version=2 且切片行是新 id（版本重排 seq）——
            #   拆分请求必须基于**当前版本**的 chunk_id 与 revision。
            current_revision = await _unit_revision(client, admin, unit_id)
            v2_chunks = (
                await client.get(
                    f"/api/knowledge-units/{unit_id}/chunks",
                    headers=_auth(admin), params={"version": 2},
                )
            ).json()["data"]["items"]
            v2_target = int(v2_chunks[0]["chunk_id"])

            bad_split = await client.post(
                f"/api/knowledge-units/{unit_id}/chunk-mutations",
                headers=_auth(admin),
                json={"chunk_id": v2_target, "action": "split", "split_offset": 0,
                      "expected_revision": current_revision},
            )
            assert bad_split.status_code == 422, bad_split.text
            out_of_range = await client.post(
                f"/api/knowledge-units/{unit_id}/chunk-mutations",
                headers=_auth(admin),
                json={"chunk_id": v2_target, "action": "split",
                      "split_offset": len(str(v2_chunks[0]["text"])) + 5,
                      "expected_revision": current_revision},
            )
            assert out_of_range.status_code == 422

    run(_run())


def test_mutate_delete_last_chunk_is_422(clean_db: None, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fakes(monkeypatch)

    async def _run() -> None:
        await _seed()
        async with _client() as client:
            admin = await _token(client)
            unit = await _upload(
                client, admin,
                content="只有一段话的内容，没有标题，因此整体是一个切片。".encode("utf-8"),
                name="单片.txt",
            )
            unit_id, task_id = int(unit["unit_id"]), int(unit["task_id"])
            revision = await _unit_revision(client, admin, unit_id)
            await _set_global_acl(client, admin, unit_id, revision)
            assert (await _run_pipeline(task_id))["status"] == "succeeded"

            chunks = (
                await client.get(
                    f"/api/knowledge-units/{unit_id}/chunks", headers=_auth(admin)
                )
            ).json()["data"]
            assert chunks["total"] == 1
            only_id = int(chunks["items"][0]["chunk_id"])

            resp = await client.post(
                f"/api/knowledge-units/{unit_id}/chunk-mutations",
                headers=_auth(admin),
                json={"chunk_id": only_id, "action": "delete", "expected_revision": revision + 1},
            )
            assert resp.status_code == 422, "删除唯一切片会在流水线制造永久失败，必须入口拦截"
            assert "唯一切片" in resp.json()["message"]

    run(_run())


# ---------------------------------------------------------------- M02 遗留闭环


def test_acl_reference_blocks_role_deletion(clean_db: None) -> None:
    """删除被知识授权引用的角色 → 409 ROLE_IN_USE（M02 遗留的 ACL 引用检查）。"""

    async def _run() -> None:
        ids = await _seed()
        async with _client() as client:
            admin = await _token(client)
            unit = await _upload(client, admin)
            unit_id = int(unit["unit_id"])
            revision = await _unit_revision(client, admin, unit_id)
            resp = await client.put(
                f"/api/knowledge-units/{unit_id}/acl",
                headers=_auth(admin),
                json={"global": False, "depts": [], "roles": [ids["admin_role_id"]],
                      "users": [], "expected_revision": revision},
            )
            assert resp.status_code == 200

            engine = create_async_engine(it_dsn(), poolclass=NullPool)
            try:
                async with engine.connect() as conn:
                    role_rev = int(
                        (
                            await conn.execute(
                                text("SELECT revision FROM `role` WHERE id=:r"),
                                {"r": ids["admin_role_id"]},
                            )
                        ).scalar_one()
                    )
            finally:
                await engine.dispose()
            blocked = await client.request(
                "DELETE",
                f"/api/roles/{ids['admin_role_id']}",
                headers=_auth(admin),
                json={"expected_revision": role_rev},
            )
            assert blocked.status_code == 409
            assert blocked.json()["code"] == "ROLE_IN_USE"

    run(_run())

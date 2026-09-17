"""M03 导入受理端点的端到端集成测试（真实 MySQL + 真实 ASGI 应用）。

与 test_org_endpoints.py 同一基建（依赖覆盖指向测试库、真实 ASGI、逐用例清表）。

几条**不能省**的用例，各自对应一条会在重构中悄悄失守的契约：

- `test_accept_upload_idempotent_same_payload_reuses_ids`：幂等靠 `upload_item`
  的唯一键 + 载荷指纹，重试**不得**创建第二个任务（API-CONTRACTS §3）。
- `test_accept_upload_conflicting_payload_is_409`：同键**不同载荷**是键复用，
  不是重放——静默吞掉会让"改一次分类"消失。
- `test_task_progress_null_means_unknown`：流水线未落地时 progress 为 null，
  契约规定"未知进度显示进行中"；不为它编造 0 或 100。
- `test_retry_preserves_attempts_and_rejects_stale_revision`：重试保留 attempts
  历史（诊断依据）且受 revision 保护（并发重试只有一个生效）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.core.security import hash_password
from app.db.base import utcnow
from app.db.session import get_session
from app.main import create_app
from app.services import ingest_svc
from app.services.auth_svc import _reset_login_limiter
from tests.integration.conftest import it_dsn, run

PASSWORD = "integration-pass-1"
ADMIN_CODES = ("kb:upload", "kb:edit", "kb:view")


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
    """建 1 部门 + 2 角色（管理员含 kb:* / 普通用户仅 ai:ask）+ 2 账号。"""
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
                                {"u": username, "n": username.casefold(), "h": hash_password(PASSWORD)},
                            )
                        ).lastrowid
                    )
                    await session.execute(
                        text("INSERT INTO user_role (user_id, role_id) VALUES (:u, :r)"),
                        {"u": user_id, "r": role_id},
                    )
                    return user_id

                admin_role = await _role("导入管理员", ADMIN_CODES)
                viewer_role = await _role("访客", ("ai:ask",))
                return {
                    "admin_role_id": admin_role,
                    "viewer_role_id": viewer_role,
                    "admin_user_id": await _user("admin", admin_role),
                    # 有**相同权限**但不是创建者的账号：用于验证"防枚举 404"
                    # （403 只该出现在无功能码的人身上）。
                    "admin2_user_id": await _user("admin2", admin_role),
                    "viewer_user_id": await _user("viewer", viewer_role),
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


def _upload_files(
    filename: str = "制度.md", content: bytes = "# 制度\n正文内容".encode("utf-8")
) -> dict:
    """multipart 的 file 字段。默认值在 def 时求值——bytes 字面量不能写非 ASCII。"""
    return {"file": (filename, content, "text/markdown")}


# ---------------------------------------------------------------- 受理


def test_accept_upload_returns_persistent_task_and_empty_acl(clean_db: None) -> None:
    """AC-03.01-01：返回持久任务；**ACL 全空**；上传后可查询。"""

    async def _run() -> None:
        ids = await _seed()
        async with _client() as client:
            token = await _token(client)
            resp = await client.post(
                "/api/uploads",
                headers=_auth(token),
                files=_upload_files(),
                data={"category": "人事制度", "client_upload_id": str(uuid4())},
            )
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["code"] == "OK" and body["request_id"]
        data = body["data"]
        assert set(data) == {"unit_id", "task_id", "status"}
        assert data["status"] == "queued"

        engine = create_async_engine(it_dsn(), poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                acl = await conn.scalar(
                    text(
                        "SELECT (SELECT COUNT(*) FROM knowledge_acl_department WHERE unit_id=:u)"
                        " + (SELECT COUNT(*) FROM knowledge_acl_role WHERE unit_id=:u)"
                        " + (SELECT COUNT(*) FROM knowledge_acl_user WHERE unit_id=:u)"
                    ),
                    {"u": data["unit_id"]},
                )
                assert int(acl) == 0, "新上传的单元 ACL 必须全空（无人可读，直到显式授权）"
                ver = (
                    await conn.execute(
                        text(
                            "SELECT version, sha256, file_key, chunk_config FROM knowledge_version "
                            "WHERE unit_id=:u"
                        ),
                        {"u": data["unit_id"]},
                    )
                ).first()
                assert ver is not None and int(ver.version) == 1
                task = (
                    await conn.execute(
                        text(
                            "SELECT status, attempts, target_version FROM index_task WHERE id=:t"
                        ),
                        {"t": data["task_id"]},
                    )
                ).first()
                assert task is not None
                assert (task.status, int(task.attempts), int(task.target_version)) == ("queued", 0, 1)
                # file_key 指向真实存在的文件（先落盘后登记）
                stored = Path(get_settings().upload_dir) / ver.file_key
                assert stored.exists(), "登记的 file_key 必须指向真实文件"
                stored.unlink()
        finally:
            await engine.dispose()

    run(_run())


def test_accept_upload_idempotent_same_payload_reuses_ids(clean_db: None) -> None:
    """同键同载荷 → 200 + 原 ID，且**只有一个任务**（API-CONTRACTS §3）。"""

    async def _run() -> None:
        await _seed()
        client_upload_id = str(uuid4())
        async with _client() as client:
            token = await _token(client)
            first = await client.post(
                "/api/uploads",
                headers=_auth(token),
                files=_upload_files(),
                data={"category": "人事制度", "client_upload_id": client_upload_id},
            )
            assert first.status_code == 202, first.text
            second = await client.post(
                "/api/uploads",
                headers=_auth(token),
                files=_upload_files(),
                data={"category": "人事制度", "client_upload_id": client_upload_id},
            )
        assert second.status_code == 200, second.text
        assert second.json()["data"] == first.json()["data"]

        engine = create_async_engine(it_dsn(), poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                tasks = await conn.scalar(text("SELECT COUNT(*) FROM index_task"))
                assert int(tasks) == 1, "幂等重放不得创建第二个任务"
        finally:
            await engine.dispose()

    run(_run())


def test_accept_upload_conflicting_payload_is_409(clean_db: None) -> None:
    """同键**不同载荷** → 409 IDEMPOTENCY_CONFLICT（不是重放，是键复用）。"""

    async def _run() -> None:
        await _seed()
        client_upload_id = str(uuid4())
        async with _client() as client:
            token = await _token(client)
            await client.post(
                "/api/uploads",
                headers=_auth(token),
                files=_upload_files(),
                data={"category": "人事制度", "client_upload_id": client_upload_id},
            )
            conflict = await client.post(
                "/api/uploads",
                headers=_auth(token),
                files=_upload_files(content="# 完全不同的内容".encode("utf-8")),
                data={"category": "人事制度", "client_upload_id": client_upload_id},
            )
        assert conflict.status_code == 409, conflict.text
        assert conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"

    run(_run())


def test_accept_upload_rejects_unsupported_format_and_empty(clean_db: None) -> None:
    """415 不支持格式；422 空文件。两者都必须在**落盘与登记之前**拒绝。"""

    async def _run() -> None:
        await _seed()
        async with _client() as client:
            token = await _token(client)
            bad_ext = await client.post(
                "/api/uploads",
                headers=_auth(token),
                files={"file": ("旧文档.doc", b"x", "application/msword")},
                data={"category": "制度", "client_upload_id": str(uuid4())},
            )
            empty = await client.post(
                "/api/uploads",
                headers=_auth(token),
                files=_upload_files(content=b""),
                data={"category": "制度", "client_upload_id": str(uuid4())},
            )
        assert bad_ext.status_code == 415 and bad_ext.json()["code"] == "UNSUPPORTED_FORMAT"
        assert empty.status_code == 422 and empty.json()["code"] == "EMPTY_FILE"

        engine = create_async_engine(it_dsn(), poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                count = await conn.scalar(text("SELECT COUNT(*) FROM knowledge_unit"))
                assert int(count) == 0, "被拒文件不得留下任何登记"
        finally:
            await engine.dispose()

    run(_run())


def test_accept_upload_requires_permission(clean_db: None) -> None:
    """只有 `ai:ask` 的账号上传 → 403 功能码拒绝。"""

    async def _run() -> None:
        await _seed()
        async with _client() as client:
            token = await _token(client, "viewer")
            resp = await client.post(
                "/api/uploads",
                headers=_auth(token),
                files=_upload_files(),
                data={"category": "制度", "client_upload_id": str(uuid4())},
            )
        assert resp.status_code == 403
        assert resp.json()["code"] == "PERM_DENIED"

    run(_run())


# ---------------------------------------------------------------- 查询与重试


def test_task_progress_null_means_unknown_and_owner_scoped(clean_db: None) -> None:
    """AC-03.03-01/02：上传后可查询；progress=null；他人/未知任务 → 404 防枚举。"""

    async def _run() -> None:
        ids = await _seed()
        async with _client() as client:
            admin = await _token(client)
            created = await client.post(
                "/api/uploads",
                headers=_auth(admin),
                files=_upload_files(),
                data={"category": "制度", "client_upload_id": str(uuid4())},
            )
            task_id = created.json()["data"]["task_id"]

            viewed = await client.get(f"/api/index-tasks/{task_id}", headers=_auth(admin))
            # 同权限、非创建者 → 防枚举 404；完全无功能码 → 403（功能层先拒绝）。
            other_admin = await _token(client, "admin2")
            foreign = await client.get(f"/api/index-tasks/{task_id}", headers=_auth(other_admin))
            viewer = await _token(client, "viewer")
            forbidden = await client.get(f"/api/index-tasks/{task_id}", headers=_auth(viewer))
            unknown = await client.get("/api/index-tasks/999999", headers=_auth(admin))

        assert viewed.status_code == 200, viewed.text
        data = viewed.json()["data"]
        assert set(data) == {"status", "stage", "progress", "error_code", "attempts", "revision"}
        assert data["status"] == "queued" and data["progress"] is None
        assert data["revision"] == 1
        assert foreign.status_code == 404, "有权限但非创建者 → 按防枚举口径返回 404"
        assert forbidden.status_code == 403, "无 kb:view/kb:upload → 功能层 403"
        assert unknown.status_code == 404
        _ = ids

    run(_run())


def test_retry_preserves_attempts_and_rejects_stale_revision(clean_db: None) -> None:
    """AC-03.05：仅 failed 可重试；不新建版本；attempts 保留；revision 防并发。"""

    async def _run() -> None:
        ids = await _seed()
        engine = create_async_engine(it_dsn(), poolclass=NullPool)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with _client() as client:
                admin = await _token(client)
                created = await client.post(
                    "/api/uploads",
                    headers=_auth(admin),
                    files=_upload_files(),
                    data={"category": "制度", "client_upload_id": str(uuid4())},
                )
                task_id = created.json()["data"]["task_id"]

                queued_retry = await client.post(
                    f"/api/index-tasks/{task_id}/retry",
                    headers=_auth(admin),
                    json={"expected_revision": 1},
                )
                assert queued_retry.status_code == 409
                assert queued_retry.json()["code"] == "TASK_STATE_CONFLICT"

                async with factory() as session:
                    async with session.begin():
                        await session.execute(
                            text(
                                "UPDATE index_task SET status='failed', error_code='PARSE_FAILED', "
                                "attempts=2 WHERE id=:t"
                            ),
                            {"t": task_id},
                        )

                retried = await client.post(
                    f"/api/index-tasks/{task_id}/retry",
                    headers=_auth(admin),
                    json={"expected_revision": 1},
                )
                assert retried.status_code == 200, retried.text
                assert retried.json()["data"] == {"task_id": task_id, "status": "queued"}

                stale = await client.post(
                    f"/api/index-tasks/{task_id}/retry",
                    headers=_auth(admin),
                    json={"expected_revision": 1},
                )
                assert stale.status_code == 409
                assert stale.json()["code"] == "REVISION_CONFLICT"

            async with engine.connect() as conn:
                row = (
                    await conn.execute(
                        text("SELECT status, attempts, target_version FROM index_task WHERE id=:t"),
                        {"t": task_id},
                    )
                ).first()
                assert row is not None
                assert (row.status, int(row.attempts), int(row.target_version)) == ("queued", 2, 1)
        finally:
            await engine.dispose()
        _ = ids

    run(_run())


# ---------------------------------------------------------------- 恢复与租约


def test_recover_tasks_converges_expired_and_stale(clean_db: None) -> None:
    """AC-03.06：过期租约按尝试次数收敛；旧版本任务 superseded（不计入失败）。"""

    async def _run() -> None:
        ids = await _seed()
        engine = create_async_engine(it_dsn(), poolclass=NullPool)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as session:
                async with session.begin():
                    unit_id = int(
                        (
                            await session.execute(
                                text(
                                    "INSERT INTO knowledge_unit (code, title, format, category, creator_id, "
                                    "enabled, is_deleted, is_global, content_version, indexed_version, "
                                    "index_status, acl_version, revision, created_at, updated_at) "
                                    "VALUES ('KB-R1', '恢复用例', 'md', '制度', :u, 1, 0, 0, 1, NULL, "
                                    "'pending', 1, 1, NOW(6), NOW(6))"
                                ),
                                {"u": ids["admin_user_id"]},
                            )
                        ).lastrowid
                    )
                    # ★ 三行必须是**不同的 target_version**：(unit_id, target_version) 唯一。
                    #   且过期租约两行的 target 必须等于 content_version——否则会先被
                    #   "superseded" 规则命中，测不到租约收敛分支。
                    rows = [
                        ("running", 2, 4),   # 过期租约 + 未到上限 → retry_wait（target=当前版本）
                        ("running", 5, 3),   # 过期租约 + 已到上限 → failed
                        ("queued", 0, 1),    # 版本已落后 → superseded
                    ]
                    task_ids: list[int] = []
                    for status, attempts, target in rows:
                        task_ids.append(
                            int(
                                (
                                    await session.execute(
                                        text(
                                            "INSERT INTO index_task (unit_id, target_version, status, "
                                            "attempts, lease_until, lease_token, revision, created_at, updated_at) "
                                            "VALUES (:u, :v, :s, :a, :lu, :lt, 1, NOW(6), NOW(6))"
                                        ),
                                        {
                                            # ★ 列是 CHAR(32)（SQLAlchemy Uuid 的非原生存储 = 无连字符 hex）。
                                            #   裸 SQL 传 UUID 对象会被 pymysql 转成 36 位带连字符字符串而超长。
                                            "u": unit_id, "v": target, "s": status, "a": attempts,
                                            "lu": utcnow(), "lt": uuid4().hex,
                                        },
                                    )
                                ).lastrowid
                            )
                        )
                    await session.execute(
                        text("UPDATE knowledge_unit SET content_version=4 WHERE id=:u"),
                        {"u": unit_id},
                    )

                result = await ingest_svc.recover_tasks(session, now=utcnow())
                assert result == {"requeued": 1, "superseded": 1, "failed": 1}, result

                states = (
                    await session.execute(
                        text("SELECT id, status FROM index_task WHERE unit_id=:u ORDER BY id"),
                        {"u": unit_id},
                    )
                ).all()
                by_id = {int(r.id): r.status for r in states}
                assert by_id[task_ids[0]] == "retry_wait"
                assert by_id[task_ids[1]] == "failed"
                assert by_id[task_ids[2]] == "superseded"
        finally:
            await engine.dispose()

    run(_run())


def test_claim_task_grants_lease_once(clean_db: None) -> None:
    """H14/H15：领取置 running + token，同任务第二次领取必须失败；续租校验 token。"""

    async def _run() -> None:
        from app.tasks import claim_task, renew_lease

        ids = await _seed()
        engine = create_async_engine(it_dsn(), poolclass=NullPool)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as session:
                async with session.begin():
                    unit_id = int(
                        (
                            await session.execute(
                                text(
                                    "INSERT INTO knowledge_unit (code, title, format, category, creator_id, "
                                    "enabled, is_deleted, is_global, content_version, indexed_version, "
                                    "index_status, acl_version, revision, created_at, updated_at) "
                                    "VALUES ('KB-C1', '租约用例', 'md', '制度', :u, 1, 0, 0, 1, NULL, "
                                    "'pending', 1, 1, NOW(6), NOW(6))"
                                ),
                                {"u": ids["admin_user_id"]},
                            )
                        ).lastrowid
                    )
                    task_id = int(
                        (
                            await session.execute(
                                text(
                                    "INSERT INTO index_task (unit_id, target_version, status, attempts, "
                                    "revision, created_at, updated_at) "
                                    "VALUES (:u, 1, 'queued', 0, 1, NOW(6), NOW(6))"
                                ),
                                {"u": unit_id},
                            )
                        ).lastrowid
                    )

                now = utcnow()
                lease = await claim_task(session, task_id=task_id, worker_id="worker-1", now=now)
                assert lease is not None, "queued 任务应可领取"
                assert lease.task_id == task_id
                assert lease.target_version == 1
                assert lease.lease_until > now

                second = await claim_task(session, task_id=task_id, worker_id="worker-2", now=now)
                assert second is None, "同一任务不能被领取两次（条件更新保证只有一个成功）"

                renewed = await renew_lease(
                    session, task_id=task_id, lease_token=lease.lease_token, now=now
                )
                assert renewed is True

                wrong_token = await renew_lease(
                    session, task_id=task_id, lease_token=uuid4(), now=now
                )
                assert wrong_token is False, "token 不匹配不得续租"

                released = await claim_task(
                    session, task_id=task_id, worker_id="worker-3", now=now + timedelta(hours=1)
                )
                assert released is None, "running 状态不在可领取集合内"
        finally:
            await engine.dispose()

    run(_run())

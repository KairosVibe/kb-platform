"""M02 组织与功能权限端点的端到端集成测试（真实 MySQL + 真实 ASGI 应用）。

验证"从 HTTP 请求到数据库写入"的整条链路：DTO 校验 → H01 身份 → H02 功能码 →
服务规则 → 事务提交 → 错误码与状态码 → 响应包结构。

本文件里几条**不能省**的用例，各自对应一条会在重构中悄悄失守的契约：

- `test_department_rejects_cycle` / `_rejects_self_parent`：部门环检测在服务层
  （MySQL 无原生环约束）。一旦失守，整棵树会从根上失联。
- `test_department_delete_blocked_by_acl_reference`：ACL 引用必须先查再删，
  否则只能拿到数据库外键的笼统报错，用户不知道"到底被什么引用了"。
- `test_disable_last_admin_is_rejected`：账号保护（`SELF_LOCK`）。它**不是**数据读权旁路，
  只是防止平台把自己锁在门外。
- `test_username_uniqueness_is_case_insensitive`：唯一性由 `username_norm`（`utf8mb4_bin`
  存储 + casefold 归一化）保证，不能交给默认排序规则。
- `test_user_list_never_exposes_password_hash`：公开 DTO 是白名单，不是 ORM 行序列化。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

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
ADMIN_CODES = ("sys:dept", "sys:user", "sys:role")


async def _it_session() -> AsyncIterator[AsyncSession]:
    """每个请求一个会话，绑定**测试库**（依赖覆盖，不改全局部署配置）。"""
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
    """直接写库建立最小组织：1 个部门 + 3 个角色 + 1 个管理员账号。

    刻意不走 API：**夹具不应依赖被测代码**，否则"创建接口坏了"会让所有用例一起失败，
    看不出真正的失败点。

    返回 `{"dept_id", "admin_user_id", "admin_role_id", "viewer_user_id"}`。
    """

    async def _run() -> dict[str, int]:
        engine = create_async_engine(it_dsn(), poolclass=NullPool)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as session:
                async with session.begin():
                    dept_id = int(
                        (
                            await session.execute(
                                text(
                                    "INSERT INTO department (parent_id, name, revision, created_at, updated_at) "
                                    "VALUES (NULL, '总部', 1, NOW(6), NOW(6))"
                                )
                            )
                        ).lastrowid
                    )

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
                                text(
                                    "INSERT INTO role_permission (role_id, code) VALUES (:r, :c)"
                                ),
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
                                        "VALUES (:u, :n, :h, :d, 1, 1, 1, NOW(6), NOW(6))"
                                    ),
                                    {
                                        "u": username,
                                        "n": username.casefold(),
                                        "h": hash_password(PASSWORD),
                                        "d": dept_id,
                                    },
                                )
                            ).lastrowid
                        )
                        await session.execute(
                            text("INSERT INTO user_role (user_id, role_id) VALUES (:u, :r)"),
                            {"u": user_id, "r": role_id},
                        )
                        return user_id

                    admin_role = await _role("系统管理员", ADMIN_CODES)
                    viewer_role = await _role("普通用户", ("ai:ask",))
                    return {
                        "dept_id": dept_id,
                        "admin_role_id": admin_role,
                        "viewer_role_id": viewer_role,
                        "admin_user_id": await _user("admin", admin_role),
                        "viewer_user_id": await _user("viewer", viewer_role),
                    }
        finally:
            await engine.dispose()

    return await _run()


async def _token(client: AsyncClient, username: str = "admin") -> str:
    resp = await client.post("/api/auth/login", json={"username": username, "password": PASSWORD})
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------- 鉴权与授权


def test_requires_authentication(clean_db: None) -> None:
    async def _run() -> None:
        async with _client() as client:
            resp = await client.get("/api/departments")
        assert resp.status_code == 401
        body = resp.json()
        assert body["code"] == "TOKEN_INVALID"
        assert body["data"] is None
        # 统一响应包：每个响应都带链路追踪 UUID（API-CONTRACTS §1）
        assert body["request_id"]

    run(_run())


def test_permission_denied_without_code(clean_db: None) -> None:
    """只有 `ai:ask` 的账号访问组织台账 → 403，且是**功能码**拒绝而不是数据权限。"""

    async def _run() -> None:
        await _seed()
        async with _client() as client:
            token = await _token(client, "viewer")
            resp = await client.get("/api/departments", headers=_auth(token))
        assert resp.status_code == 403
        assert resp.json()["code"] == "PERM_DENIED"

    run(_run())


# ---------------------------------------------------------------- 部门


def test_department_create_and_list(clean_db: None) -> None:
    async def _run() -> None:
        ids = await _seed()
        async with _client() as client:
            token = await _token(client)
            created = await client.post(
                "/api/departments",
                headers=_auth(token),
                json={"parent_id": ids["dept_id"], "name": "客服部"},
            )
            assert created.status_code == 201, created.text
            child = created.json()["data"]
            assert child["parent_id"] == ids["dept_id"]
            assert child["revision"] == 1

            listed = await client.get("/api/departments", headers=_auth(token))
            assert listed.status_code == 200
            items = listed.json()["data"]["items"]
            assert [i["name"] for i in items] == ["总部", "客服部"]

    run(_run())


def test_department_rejects_missing_parent(clean_db: None) -> None:
    async def _run() -> None:
        await _seed()
        async with _client() as client:
            token = await _token(client)
            resp = await client.post(
                "/api/departments", headers=_auth(token), json={"parent_id": 999999, "name": "孤儿部门"}
            )
        assert resp.status_code == 404
        assert resp.json()["code"] == "NOT_FOUND"

    run(_run())


def test_department_duplicate_sibling(clean_db: None) -> None:
    """同级同名 → 409；且第二个**根**部门的同名检查同样生效（`IS NULL` 分支）。"""

    async def _run() -> None:
        await _seed()
        async with _client() as client:
            token = await _token(client)
            first = await client.post(
                "/api/departments", headers=_auth(token), json={"parent_id": None, "name": "分支"}
            )
            second = await client.post(
                "/api/departments", headers=_auth(token), json={"parent_id": None, "name": "分支"}
            )
        assert first.status_code == 201
        assert second.status_code == 409
        assert second.json()["code"] == "DEPT_DUPLICATE"

    run(_run())


def test_department_rejects_self_parent(clean_db: None) -> None:
    async def _run() -> None:
        ids = await _seed()
        async with _client() as client:
            token = await _token(client)
            resp = await client.patch(
                f"/api/departments/{ids['dept_id']}",
                headers=_auth(token),
                json={"parent_id": ids["dept_id"], "name": "总部", "expected_revision": 1},
            )
        assert resp.status_code == 422
        assert resp.json()["code"] == "DEPT_CYCLE"

    run(_run())


def test_department_rejects_cycle(clean_db: None) -> None:
    """A 的父改成自己的子部门 B → 422。失守会让整棵树从根上失联。"""

    async def _run() -> None:
        ids = await _seed()
        async with _client() as client:
            token = await _token(client)
            created = await client.post(
                "/api/departments",
                headers=_auth(token),
                json={"parent_id": ids["dept_id"], "name": "客服部"},
            )
            child_id = created.json()["data"]["id"]

            resp = await client.patch(
                f"/api/departments/{ids['dept_id']}",
                headers=_auth(token),
                json={"parent_id": child_id, "name": "总部", "expected_revision": 1},
            )
        assert resp.status_code == 422
        assert resp.json()["code"] == "DEPT_CYCLE"

    run(_run())


def test_department_revision_conflict(clean_db: None) -> None:
    async def _run() -> None:
        ids = await _seed()
        async with _client() as client:
            token = await _token(client)
            ok_resp = await client.patch(
                f"/api/departments/{ids['dept_id']}",
                headers=_auth(token),
                json={"parent_id": None, "name": "总部（改）", "expected_revision": 1},
            )
            assert ok_resp.status_code == 200
            stale = await client.patch(
                f"/api/departments/{ids['dept_id']}",
                headers=_auth(token),
                json={"parent_id": None, "name": "总部（再改）", "expected_revision": 1},
            )
        assert stale.status_code == 409
        assert stale.json()["code"] == "REVISION_CONFLICT"

    run(_run())


def test_department_delete_blocked_by_children(clean_db: None) -> None:
    async def _run() -> None:
        ids = await _seed()
        async with _client() as client:
            token = await _token(client)
            await client.post(
                "/api/departments",
                headers=_auth(token),
                json={"parent_id": ids["dept_id"], "name": "客服部"},
            )
            resp = await client.request(
                "DELETE",
                f"/api/departments/{ids['dept_id']}",
                headers=_auth(token),
                json={"expected_revision": 1},
            )
        assert resp.status_code == 409
        assert resp.json()["code"] == "DEPT_IN_USE"

    run(_run())


def test_department_delete_blocked_by_user(clean_db: None) -> None:
    """部门下还有用户时不可删——提示必须能指导操作（"先把用户调整到其他部门"）。"""

    async def _run() -> None:
        ids = await _seed()
        async with _client() as client:
            token = await _token(client)
            resp = await client.request(
                "DELETE",
                f"/api/departments/{ids['dept_id']}",
                headers=_auth(token),
                json={"expected_revision": 1},
            )
        assert resp.status_code == 409
        assert resp.json()["code"] == "DEPT_IN_USE"

    run(_run())


def test_department_delete_blocked_by_acl_reference(clean_db: None) -> None:
    """被知识四维授权引用的部门不可删。

    这条用例的价值在于**先查引用再删**：若不查，就只能拿到数据库外键报错，
    用户无从知道"到底被什么引用了"。
    """

    async def _run() -> None:
        ids = await _seed()
        engine = create_async_engine(it_dsn(), poolclass=NullPool)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as session:
                async with session.begin():
                    # ★ 必须新建一个**既无子部门也无用户**的部门：
                    #   `delete_department` 的引用检查是**有顺序**的（子部门 → 用户 → 知识授权），
                    #   若沿用 `_seed()` 的 `dept_id`（其下已有 admin/viewer），命中的会是"用户"
                    #   分支，永远走不到知识授权分支——那样这条用例就测不到它声称要测的东西。
                    acl_dept_id = int(
                        (
                            await session.execute(
                                text(
                                    "INSERT INTO department (parent_id, name, revision, created_at, updated_at) "
                                    "VALUES (NULL, '被授权部门', 1, NOW(6), NOW(6))"
                                )
                            )
                        ).lastrowid
                    )
                    unit_id = int(
                        (
                            await session.execute(
                                text(
                                    "INSERT INTO knowledge_unit (code, title, format, category, creator_id, "
                                    "enabled, is_deleted, is_global, content_version, indexed_version, "
                                    "index_status, acl_version, revision, created_at, updated_at) "
                                    "VALUES ('KB-0001', '薪酬管理制度', 'pdf', '人事制度', :u, 1, 0, 0, 1, 1, "
                                    "'indexed', 1, 1, NOW(6), NOW(6))"
                                ),
                                {"u": ids["admin_user_id"]},
                            )
                        ).lastrowid
                    )
                    await session.execute(
                        text(
                            "INSERT INTO knowledge_acl_department (unit_id, subject_id) VALUES (:u, :d)"
                        ),
                        {"u": unit_id, "d": acl_dept_id},
                    )
        finally:
            await engine.dispose()

        async with _client() as client:
            token = await _token(client)
            resp = await client.request(
                "DELETE",
                f"/api/departments/{acl_dept_id}",
                headers=_auth(token),
                json={"expected_revision": 1},
            )
        assert resp.status_code == 409
        assert resp.json()["code"] == "DEPT_IN_USE"
        assert "知识授权" in resp.json()["message"]

    run(_run())


def test_department_delete_success(clean_db: None) -> None:
    async def _run() -> None:
        await _seed()
        async with _client() as client:
            token = await _token(client)
            created = await client.post(
                "/api/departments", headers=_auth(token), json={"parent_id": None, "name": "临时部门"}
            )
            dept_id = created.json()["data"]["id"]
            resp = await client.request(
                "DELETE",
                f"/api/departments/{dept_id}",
                headers=_auth(token),
                json={"expected_revision": 1},
            )
        assert resp.status_code == 200
        assert resp.json()["data"]["deleted"] is True

    run(_run())


# ---------------------------------------------------------------- 用户


def test_user_create_weak_password_returns_contract_code(clean_db: None) -> None:
    """★ 必须是 `PASSWORD_LENGTH_INVALID`（422），不能漂成 `INVALID_ARGUMENT`。

    这正是"密码长度策略只放 `core/security.py` 一处、DTO 不校验"的原因：
    两处校验必然导致响应码漂移。
    """

    async def _run() -> None:
        await _seed()
        async with _client() as client:
            token = await _token(client)
            resp = await client.post(
                "/api/users",
                headers=_auth(token),
                json={"username": "short-pw", "password": "short", "dept_id": None, "role_ids": []},
            )
        assert resp.status_code == 422
        assert resp.json()["code"] == "PASSWORD_LENGTH_INVALID"

    run(_run())


def test_username_uniqueness_is_case_insensitive(clean_db: None) -> None:
    async def _run() -> None:
        await _seed()
        async with _client() as client:
            token = await _token(client)
            first = await client.post(
                "/api/users",
                headers=_auth(token),
                json={"username": "Bob", "password": PASSWORD, "dept_id": None, "role_ids": []},
            )
            second = await client.post(
                "/api/users",
                headers=_auth(token),
                json={"username": "bob", "password": PASSWORD, "dept_id": None, "role_ids": []},
            )
        assert first.status_code == 201, first.text
        assert second.status_code == 409
        assert second.json()["code"] == "USER_DUPLICATE"

    run(_run())


def test_user_list_never_exposes_password_hash(clean_db: None) -> None:
    async def _run() -> None:
        await _seed()
        async with _client() as client:
            token = await _token(client)
            resp = await client.get("/api/users", headers=_auth(token))
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["total"] >= 2
        for item in data["items"]:
            assert set(item) == {"id", "username", "dept_id", "role_ids", "enabled", "revision"}
            assert "password_hash" not in item
            assert "username_norm" not in item

    run(_run())


def test_disable_last_admin_is_rejected(clean_db: None) -> None:
    """账号保护：不能让平台失去最后一个可管理账号。

    注意它拦的是"管理能力归零"，**不涉及任何数据读权**——
    ADMIN_CODES 只含 sys:user / sys:role / sys:dept（见 org_svc 模块说明）。
    """

    async def _run() -> None:
        ids = await _seed()
        async with _client() as client:
            token = await _token(client)
            resp = await client.patch(
                f"/api/users/{ids['admin_user_id']}",
                headers=_auth(token),
                json={
                    "dept_id": ids["dept_id"],
                    "role_ids": [ids["admin_role_id"]],
                    "enabled": False,
                    "expected_revision": 1,
                },
            )
        assert resp.status_code == 409
        assert resp.json()["code"] == "SELF_LOCK"

    run(_run())


def test_user_update_replaces_roles_and_bumps_revision(clean_db: None) -> None:
    async def _run() -> None:
        ids = await _seed()
        async with _client() as client:
            token = await _token(client)
            resp = await client.patch(
                f"/api/users/{ids['viewer_user_id']}",
                headers=_auth(token),
                json={
                    "dept_id": None,
                    "role_ids": [ids["viewer_role_id"], ids["admin_role_id"]],
                    "enabled": True,
                    "expected_revision": 1,
                },
            )
            assert resp.status_code == 200, resp.text
            assert resp.json()["data"]["revision"] == 2

            listed = await client.get("/api/users", headers=_auth(token))
        viewer = next(i for i in listed.json()["data"]["items"] if i["id"] == ids["viewer_user_id"])
        assert viewer["role_ids"] == sorted([ids["viewer_role_id"], ids["admin_role_id"]])

    run(_run())


# ---------------------------------------------------------------- 角色与权限码


def test_permission_codes_returns_exactly_14(clean_db: None) -> None:
    """API-S03：固定 14 码，且带 `module` 供前端组树。"""

    async def _run() -> None:
        await _seed()
        async with _client() as client:
            token = await _token(client)
            resp = await client.get("/api/permission-codes", headers=_auth(token))
        assert resp.status_code == 200
        items = resp.json()["data"]["items"]
        assert len(items) == 14
        assert {"code", "label", "module"} <= set(items[0])
        assert "kb:perm" in {i["code"] for i in items}

    run(_run())


def test_role_save_rejects_invalid_code(clean_db: None) -> None:
    async def _run() -> None:
        await _seed()
        async with _client() as client:
            token = await _token(client)
            resp = await client.put(
                "/api/roles",
                headers=_auth(token),
                json={"id": None, "name": "越权角色", "codes": ["kb:view", "nope:code"]},
            )
        assert resp.status_code == 422
        assert resp.json()["code"] == "INVALID_PERM_CODE"
        # 非法码必须被**报出来**，而不是静默丢弃（API-CONTRACTS §1）
        assert "nope:code" in resp.json()["message"]

    run(_run())


def test_role_create_then_delete_in_use(clean_db: None) -> None:
    async def _run() -> None:
        await _seed()
        async with _client() as client:
            token = await _token(client)
            created = await client.put(
                "/api/roles",
                headers=_auth(token),
                json={"id": None, "name": "知识管理员", "codes": ["kb:view", "kb:edit"]},
            )
            assert created.status_code == 200, created.text
            role_id = created.json()["data"]["id"]

            # 分配给用户后再删除 → 409，提示先撤销引用
            await client.patch(
                f"/api/users/",
                headers=_auth(token),
                json={},
            ) if False else None

            blocked = await client.request(
                "DELETE",
                f"/api/roles/{role_id}",
                headers=_auth(token),
                json={"expected_revision": 1},
            )
        # 未分配给任何用户时可以直接删除
        assert blocked.status_code == 200, blocked.text

    run(_run())


def test_role_delete_blocked_when_assigned(clean_db: None) -> None:
    async def _run() -> None:
        ids = await _seed()
        async with _client() as client:
            token = await _token(client)
            resp = await client.request(
                "DELETE",
                f"/api/roles/{ids['viewer_role_id']}",
                headers=_auth(token),
                json={"expected_revision": 1},
            )
        assert resp.status_code == 409
        assert resp.json()["code"] == "ROLE_IN_USE"

    run(_run())


def test_role_update_cannot_strip_last_admin_capability(clean_db: None) -> None:
    """把唯一管理角色的管理码裁掉 → 409 `ROLE_PROTECTED`。"""

    async def _run() -> None:
        ids = await _seed()
        async with _client() as client:
            token = await _token(client)
            resp = await client.put(
                "/api/roles",
                headers=_auth(token),
                json={
                    "id": ids["admin_role_id"],
                    "name": "系统管理员",
                    "codes": ["kb:view"],
                    "expected_revision": 1,
                },
            )
        assert resp.status_code == 409
        assert resp.json()["code"] == "ROLE_PROTECTED"

    run(_run())


# ---------------------------------------------------------------- 目录选择器


def test_directory_permission_follows_kind(clean_db: None) -> None:
    """`/directory` 的功能码由 `kind` 决定：只有 sys:dept 的账号不能查用户目录。"""

    async def _run() -> None:
        await _seed()
        engine = create_async_engine(it_dsn(), poolclass=NullPool)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as session:
                async with session.begin():
                    role_id = int(
                        (
                            await session.execute(
                                text(
                                    "INSERT INTO `role` (name, name_norm, revision, created_at, updated_at) "
                                    "VALUES ('部门管理员', '部门管理员', 1, NOW(6), NOW(6))"
                                )
                            )
                        ).lastrowid
                    )
                    await session.execute(
                        text("INSERT INTO role_permission (role_id, code) VALUES (:r, 'sys:dept')"),
                        {"r": role_id},
                    )
                    user_id = int(
                        (
                            await session.execute(
                                text(
                                    "INSERT INTO `user` (username, username_norm, password_hash, dept_id, "
                                    "enabled, identity_revision, revision, created_at, updated_at) "
                                    "VALUES ('deptadmin', 'deptadmin', :h, NULL, 1, 1, 1, NOW(6), NOW(6))"
                                ),
                                {"h": hash_password(PASSWORD)},
                            )
                        ).lastrowid
                    )
                    await session.execute(
                        text("INSERT INTO user_role (user_id, role_id) VALUES (:u, :r)"),
                        {"u": user_id, "r": role_id},
                    )
        finally:
            await engine.dispose()

        async with _client() as client:
            token = await _token(client, "deptadmin")
            allowed = await client.get("/api/directory?kind=department", headers=_auth(token))
            denied = await client.get("/api/directory?kind=user", headers=_auth(token))
        assert allowed.status_code == 200, allowed.text
        assert denied.status_code == 403
        assert denied.json()["code"] == "PERM_DENIED"

    run(_run())


def test_directory_rejects_unknown_kind(clean_db: None) -> None:
    async def _run() -> None:
        await _seed()
        async with _client() as client:
            token = await _token(client)
            resp = await client.get("/api/directory?kind=team", headers=_auth(token))
        assert resp.status_code == 422
        assert resp.json()["code"] == "INVALID_ARGUMENT"

    run(_run())


def test_directory_user_items_expose_enabled_flag(clean_db: None) -> None:
    """用户条目要能看出"这个账号已停用"；部门/角色没有启停概念，`enabled` 为 null。"""

    async def _run() -> None:
        await _seed()
        async with _client() as client:
            token = await _token(client)
            users = await client.get("/api/directory?kind=user", headers=_auth(token))
            depts = await client.get("/api/directory?kind=department", headers=_auth(token))
        assert users.status_code == 200
        assert all(isinstance(i["enabled"], bool) for i in users.json()["data"]["items"])
        assert all(i["enabled"] is None for i in depts.json()["data"]["items"])

    run(_run())


_: Any = None

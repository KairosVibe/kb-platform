"""H04 `authorize_units` 在真实 MySQL 上的端到端验证（**审计 PA-06 的关闭证据**）。

与 `tests/test_authz_svc.py` 的分工必须说清：
- 那边验证**分桶规则**（纯函数、假行对象、覆盖 16 种组合）——那是规则正确性；
- 这边验证**读库装配**（真实行 → `UnitState` → 三个桶）——那是 SQL、连接与字段映射的正确性。

两者都不可替代：只做前者会漏掉"SQL 写错导致读到空集合"，只做后者会漏掉
"某个组合分类错误"。本用例覆盖四个维度 + 墓碑 + 停用 + 索引未就绪 + 不存在的 ID。
"""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.engines.permission import UserCtx
from app.services.authz_svc import authorize_units
from tests.integration.conftest import it_dsn, run

_INSERT_UNIT = text(
    """
    INSERT INTO knowledge_unit (code, title, format, category, creator_id, enabled, is_deleted,
                                is_global, content_version, indexed_version, index_status,
                                acl_version, revision, created_at, updated_at)
    VALUES (:code, :title, 'md', 'cat', :creator, :enabled, :deleted,
            :is_global, :content_version, :indexed_version, :index_status,
            1, 1, NOW(6), NOW(6))
    """
)


async def _scalar(sql: str, params: dict[str, object] | None = None) -> object:
    engine = create_async_engine(it_dsn(), poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            return await conn.scalar(sql, params or {})  # type: ignore[arg-type]
    finally:
        await engine.dispose()


async def _execute(sql: object, params: dict[str, object] | None = None) -> None:
    """执行一条语句。

    ★ 入参可能是 `str` 也可能是已经构造好的 `TextClause`——两种都要接受。
      首轮就是在这里把 `TextClause` 又包了一次 `text()`，报
      `TypeError: expected string or bytes-like object, got 'TextClause'`。
    """
    stmt = text(sql) if isinstance(sql, str) else sql
    engine = create_async_engine(it_dsn(), poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(stmt, params or {})  # type: ignore[arg-type]
    finally:
        await engine.dispose()


async def _insert_unit(**kwargs: object) -> int:
    await _execute(_INSERT_UNIT, kwargs)  # type: ignore[arg-type]
    return int(await _scalar(text("SELECT id FROM knowledge_unit WHERE code = :c"), {"c": kwargs["code"]}))


async def _build_fixture() -> tuple[int, int, int, int]:
    """建部门/用户/角色，返回 (dept_id, user_id, role_id, other_dept_id)。"""
    await _execute(
        "INSERT INTO department (name, revision, created_at, updated_at) "
        "VALUES ('H04 部门', 1, NOW(6), NOW(6))"
    )
    dept = int(await _scalar(text("SELECT id FROM department WHERE name='H04 部门'")))
    await _execute(
        "INSERT INTO department (name, revision, created_at, updated_at) "
        "VALUES ('H04 他部', 1, NOW(6), NOW(6))"
    )
    other_dept = int(await _scalar(text("SELECT id FROM department WHERE name='H04 他部'")))
    await _execute(
        "INSERT INTO user (username, username_norm, password_hash, dept_id, enabled, "
        "identity_revision, revision, created_at, updated_at) "
        "VALUES ('h04-user', 'h04-user', 'x', :d, 1, 1, 1, NOW(6), NOW(6))",
        {"d": dept},
    )
    user = int(await _scalar(text("SELECT id FROM user WHERE username_norm='h04-user'")))
    await _execute(
        "INSERT INTO role (name, name_norm, revision, created_at, updated_at) "
        "VALUES ('H04 角色', 'h04 角色', 1, NOW(6), NOW(6))"
    )
    role = int(await _scalar(text("SELECT id FROM role WHERE name_norm='h04 角色'")))
    return dept, user, role, other_dept


def test_authorize_units_end_to_end(clean_db: None) -> None:
    dept, user, role, other_dept = run(_build_fixture())

    async def _scenario() -> dict[str, object]:
        # 各维度命中 / 未命中 / 墓碑 / 停用 / 索引未就绪
        global_unit = await _insert_unit(
            code="h04-global", title="全局", creator=user, enabled=1, deleted=0,
            is_global=1, content_version=1, indexed_version=1, index_status="indexed",
        )
        dept_unit = await _insert_unit(
            code="h04-dept", title="部门", creator=user, enabled=1, deleted=0,
            is_global=0, content_version=1, indexed_version=1, index_status="indexed",
        )
        role_unit = await _insert_unit(
            code="h04-role", title="角色", creator=user, enabled=1, deleted=0,
            is_global=0, content_version=1, indexed_version=1, index_status="indexed",
        )
        user_unit = await _insert_unit(
            code="h04-user-unit", title="个人", creator=user, enabled=1, deleted=0,
            is_global=0, content_version=1, indexed_version=1, index_status="indexed",
        )
        denied_unit = await _insert_unit(
            code="h04-denied", title="无权", creator=user, enabled=1, deleted=0,
            is_global=0, content_version=1, indexed_version=1, index_status="indexed",
        )
        deleted_unit = await _insert_unit(
            code="h04-deleted", title="已删除", creator=user, enabled=1, deleted=1,
            is_global=1, content_version=1, indexed_version=1, index_status="indexed",
        )
        disabled_unit = await _insert_unit(
            code="h04-disabled", title="已停用", creator=user, enabled=0, deleted=0,
            is_global=1, content_version=1, indexed_version=1, index_status="indexed",
        )
        pending_unit = await _insert_unit(
            code="h04-pending", title="未索引", creator=user, enabled=1, deleted=0,
            is_global=1, content_version=1, indexed_version=None, index_status="pending",
        )
        stale_unit = await _insert_unit(
            code="h04-stale", title="版本过期", creator=user, enabled=1, deleted=0,
            is_global=1, content_version=2, indexed_version=1, index_status="stale",
        )
        other_dept_unit = await _insert_unit(
            code="h04-otherdept", title="他部", creator=user, enabled=1, deleted=0,
            is_global=0, content_version=1, indexed_version=1, index_status="indexed",
        )

        # 四维授权：全局 / 本部门 / 角色 / 个人 / 他部门
        await _execute(
            "INSERT INTO knowledge_acl_department (unit_id, subject_id) VALUES (:u, :d)",
            {"u": dept_unit, "d": dept},
        )
        await _execute(
            "INSERT INTO knowledge_acl_role (unit_id, subject_id) VALUES (:u, :r)",
            {"u": role_unit, "r": role},
        )
        await _execute(
            "INSERT INTO knowledge_acl_user (unit_id, subject_id) VALUES (:u, :x)",
            {"u": user_unit, "x": user},
        )
        await _execute(
            "INSERT INTO knowledge_acl_department (unit_id, subject_id) VALUES (:u, :d)",
            {"u": other_dept_unit, "d": other_dept},
        )

        ctx = UserCtx(
            user_id=user,
            session_id=uuid4(),
            dept_id=dept,
            role_ids=frozenset({role}),
            # 故意给全功能权限码：功能权限**不产生**正文读权
            permission_codes=frozenset({"ai:ask", "kb:view", "kb:perm"}),
            identity_revision=1,
        )

        engine = create_async_engine(it_dsn(), poolclass=NullPool)
        try:
            from sqlalchemy.ext.asyncio import async_sessionmaker

            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as session:
                result = await authorize_units(
                    session,
                    ctx,
                    [
                        global_unit, dept_unit, role_unit, user_unit, denied_unit,
                        deleted_unit, disabled_unit, pending_unit, stale_unit,
                        other_dept_unit, 999_999_999,
                    ],
                )
        finally:
            await engine.dispose()

        return {
            "allowed": result.allowed,
            "denied": result.denied,
            "unavailable": result.unavailable,
            "versions": result.versions,
            "expect": {
                "allowed": sorted([global_unit, dept_unit, role_unit, user_unit]),
                "unavailable": sorted([pending_unit, stale_unit]),
                "denied": sorted([denied_unit, deleted_unit, disabled_unit, other_dept_unit, 999_999_999]),
            },
        }

    outcome = run(_scenario())
    assert outcome["allowed"] == outcome["expect"]["allowed"], outcome
    assert outcome["unavailable"] == outcome["expect"]["unavailable"], outcome
    assert outcome["denied"] == outcome["expect"]["denied"], outcome
    # 只有放行单元才有切片版本；`unavailable` 不得出现在 versions 里
    assert outcome["versions"] == {u: 1 for u in outcome["expect"]["allowed"]}


def test_authorize_units_empty_input(clean_db: None) -> None:
    """空输入 → 三个桶全空（不能"没有输入等于没有限制"）。"""

    async def _scenario() -> tuple[list[int], list[int], list[int], dict[int, int]]:
        engine = create_async_engine(it_dsn(), poolclass=NullPool)
        try:
            from sqlalchemy.ext.asyncio import async_sessionmaker

            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as session:
                ctx = UserCtx(
                    user_id=1,
                    session_id=uuid4(),
                    dept_id=None,
                    role_ids=frozenset(),
                    permission_codes=frozenset(),
                    identity_revision=1,
                )
                result = await authorize_units(session, ctx, [])
                return result.allowed, result.denied, result.unavailable, result.versions
        finally:
            await engine.dispose()

    assert run(_scenario()) == ([], [], [], {})

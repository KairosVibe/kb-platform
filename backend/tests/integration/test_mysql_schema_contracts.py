"""真实 MySQL 上的结构契约验证（CHECK / FK / 排序规则 / 时间精度）。

这些是**只有真实后端才能证明**的性质（DATA-CONTRACTS §5 明确禁止用 SQLite 或元数据
体检替代）。元数据体检只能证明"我们声明了什么"，这里证明"MySQL 8.0.26 真的照做了"：

- `CHECK` 是否真的拦住非法枚举；
- `FOREIGN KEY ... ON DELETE RESTRICT` 是否真的阻止删除仍被引用的部门；
- `utf8mb4_bin` 是否真的让大小写敏感（默认排序规则会把 `Admin` 与 `admin` 判为同一身份）；
- **`DATETIME(6)` 是否真的保留微秒**——这是"泛型 `DateTime(6)` 退化成 `DATETIME`"
  那个缺陷的端到端证据（WORKLOG 问题 #38）。

两条实测得到的编写约定（首轮踩过）：

1. **一个用例只用一个连接 + 一个事务**。跨连接取 `SELECT LAST_INSERT_ID()`
   会拿到别的连接的值（甚至 0），因为 `NullPool` 下每次 `connect()` 都是新连接。
   这里改为读 `result.lastrowid`。
2. **约束违规的异常类型要按 `DatabaseError` 判**。MySQL 对 CHECK 违规报
   `OperationalError 3819`，对唯一键/外键报 `IntegrityError`（1062/1451）——
   只断言 `IntegrityError` 会漏掉 CHECK 那一类。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Coroutine

from sqlalchemy import text
from sqlalchemy.exc import DatabaseError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from sqlalchemy.pool import NullPool

from tests.integration.conftest import it_dsn, run

_Scenario = Callable[[AsyncConnection], Coroutine[Any, Any, Any]]


async def _with_conn(scenario: _Scenario) -> Any:
    engine = create_async_engine(it_dsn(), poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            return await scenario(conn)
    finally:
        await engine.dispose()


async def _capture(conn: AsyncConnection, stmt: str, params: dict[str, Any] | None = None) -> Exception | None:
    """在 **SAVEPOINT** 内执行语句；返回异常而不污染外层事务。

    用 SAVEPOINT 而不是"另开一个引擎"是有意的：这样既能断言"这条语句会失败"，
    又能继续在同一事务里验证"其他数据仍在"。同时它也演示了 `PersistentRefreshStore.consume`
    处理重放的同一种手法（`begin_nested`）。
    """
    savepoint = await conn.begin_nested()
    try:
        await conn.execute(text(stmt), params or {})
    except DatabaseError as exc:
        await savepoint.rollback()
        return exc
    await savepoint.commit()
    return None


async def _add_department(conn: AsyncConnection, name: str) -> int:
    result = await conn.execute(
        text(
            "INSERT INTO department (name, revision, created_at, updated_at) "
            "VALUES (:n, 1, NOW(6), NOW(6))"
        ),
        {"n": name},
    )
    return int(result.lastrowid or 0)


async def _add_user(conn: AsyncConnection, norm: str, *, dept_id: int | None, created: datetime | None = None) -> int:
    """插入账号。`created_at` 由应用提供（DDL 刻意不加 `CURRENT_TIMESTAMP`，见 db/base.py）。"""
    result = await conn.execute(
        text(
            "INSERT INTO user (username, username_norm, password_hash, dept_id, enabled, "
            "identity_revision, revision, created_at, updated_at) "
            "VALUES (:u, :n, '!it-no-login', :d, 1, 1, 1, :ts, :ts)"
        ),
        {"u": norm, "n": norm, "d": dept_id, "ts": created or datetime(2026, 9, 16, 0, 0, 0)},
    )
    return int(result.lastrowid or 0)


async def _add_unit(conn: AsyncConnection, code: str, creator_id: int, *, index_status: str = "indexed") -> int:
    result = await conn.execute(
        text(
            "INSERT INTO knowledge_unit (code, title, format, category, creator_id, enabled, "
            "is_deleted, is_global, content_version, indexed_version, index_status, acl_version, "
            "revision, created_at, updated_at) "
            "VALUES (:c, 't', 'md', 'c', :u, 1, 0, 0, 1, 1, :s, 1, 1, NOW(6), NOW(6))"
        ),
        {"c": code, "u": creator_id, "s": index_status},
    )
    return int(result.lastrowid or 0)


# ---------------------------------------------------------------- CHECK 约束

def test_check_rejects_invalid_index_status(clean_db: None) -> None:
    """`index_status='failed'` 必须被拒绝——`failed` 是**任务**结局，不是单元状态。"""

    async def _scenario(conn: AsyncConnection) -> Exception | None:
        dept = await _add_department(conn, "检查用部门")
        owner = await _add_user(conn, "chk-owner", dept_id=dept)
        return await _capture(
            conn,
            "INSERT INTO knowledge_unit (code, title, format, category, creator_id, enabled, "
            "is_deleted, is_global, content_version, index_status, acl_version, revision, "
            "created_at, updated_at) "
            "VALUES ('chk-bad', 't', 'md', 'c', :u, 1, 0, 0, 1, 'failed', 1, 1, NOW(6), NOW(6))",
            {"u": owner},
        )

    exc = run(_with_conn(_scenario))
    assert exc is not None, "非法 index_status 竟然写入成功"
    assert "ck_knowledge_unit_index_status_values" in str(exc)


def test_check_accepts_prd_values(clean_db: None) -> None:
    """PRD §1.2 第 1 条的三态必须都能写入（CHECK 写窄了会拦住合法数据）。"""

    async def _scenario(conn: AsyncConnection) -> list[int]:
        dept = await _add_department(conn, "检查用部门2")
        owner = await _add_user(conn, "chk-owner2", dept_id=dept)
        ids = [
            await _add_unit(conn, f"ok-{status}", owner, index_status=status)
            for status in ("pending", "indexed", "stale")
        ]
        return ids

    ids = run(_with_conn(_scenario))
    assert len(ids) == 3
    assert all(i > 0 for i in ids)


def test_chat_event_seq_positive_is_enforced(clean_db: None) -> None:
    """`ck_chat_event_seq_positive`：`seq <= 0` 必须被拒绝（游标从 1 开始）。"""

    async def _scenario(conn: AsyncConnection) -> Exception | None:
        return await _capture(
            conn,
            "INSERT INTO chat_event (request_id, seq, event, payload, created_at) "
            "VALUES (1, 0, 'meta', '{}', NOW(6))",
        )

    exc = run(_with_conn(_scenario))
    assert exc is not None
    assert "ck_chat_event_seq_positive" in str(exc)


# ---------------------------------------------------------------- 外键限制

def test_fk_restrict_blocks_department_deletion(clean_db: None) -> None:
    """部门仍被用户引用时不允许删除（DATA-CONTRACTS §2：存在引用则拒绝删除）。"""

    async def _scenario(conn: AsyncConnection) -> Exception | None:
        dept = await _add_department(conn, "被引用部门")
        await _add_user(conn, "fk-user", dept_id=dept)
        return await _capture(conn, "DELETE FROM department WHERE id = :d", {"d": dept})

    exc = run(_with_conn(_scenario))
    assert exc is not None, "仍被引用的部门竟然被删掉了"
    assert "fk_user_dept_id_department" in str(exc) or "1451" in str(exc)


def test_fk_cascade_removes_dependent_acl_rows(clean_db: None) -> None:
    """删除知识单元时其 ACL 行随之清理（CASCADE），不留下悬空授权。"""

    async def _scenario(conn: AsyncConnection) -> tuple[dict[str, int], dict[str, int]]:
        dept = await _add_department(conn, "级联部门")
        owner = await _add_user(conn, "cas-owner", dept_id=dept)
        unit = await _add_unit(conn, "cas-1", owner)
        await conn.execute(
            text("INSERT INTO knowledge_acl_department (unit_id, subject_id) VALUES (:u, :d)"),
            {"u": unit, "d": dept},
        )

        async def _count() -> int:
            return int(
                (await conn.execute(text("SELECT COUNT(*) FROM knowledge_acl_department"))).scalar() or 0
            )

        before = {"acl": await _count()}
        await conn.execute(text("DELETE FROM knowledge_unit WHERE id = :u"), {"u": unit})
        after = {"acl": await _count()}
        return before, after

    before, after = run(_with_conn(_scenario))
    assert before["acl"] == 1
    assert after["acl"] == 0


# ---------------------------------------------------------------- 身份排序规则

def test_binary_collation_distinguishes_case(clean_db: None) -> None:
    """`utf8mb4_bin` 让 `binx` 与 `BINX` 是**两个不同值**。

    这正是选二进制排序规则的用意：默认排序规则（`utf8mb4_0900_ai_ci`）会把它们判为相等，
    于是"大小写不同"被数据库悄悄合并，身份边界变得不可推理。应用层用
    `normalize_username`（strip + casefold）**显式**决定"什么算同一个身份"。
    """

    async def _scenario(conn: AsyncConnection) -> tuple[int, Exception | None]:
        await _add_user(conn, "binx", dept_id=None)
        await _add_user(conn, "BINX", dept_id=None)
        count = int(
            (
                await conn.execute(
                    text("SELECT COUNT(*) FROM user WHERE username_norm IN ('binx','BINX')")
                )
            ).scalar()
            or 0
        )
        # 同一规范化值（应用层会把两者都归一成 'binx'）再插入 → 唯一键必须拦住
        dup = await _capture(
            conn,
            "INSERT INTO user (username, username_norm, password_hash, dept_id, enabled, "
            "identity_revision, revision, created_at, updated_at) "
            "VALUES ('binx2', 'binx', 'x', NULL, 1, 1, 1, NOW(6), NOW(6))",
        )
        return count, dup

    count, dup = run(_with_conn(_scenario))
    assert count == 2, "二进制排序规则未生效：大小写不同的两个身份被当成同一个"
    assert dup is not None, "规范化后的重复身份竟然插入成功"
    assert "uq_user_username_norm" in str(dup)


# ---------------------------------------------------------------- 时间精度（端到端）

def test_datetime6_preserves_microseconds(clean_db: None) -> None:
    """★ `DATETIME(6)` 必须真的保留微秒。

    泛型 `DateTime(6)` 在 MySQL 方言下会退化成 `DATETIME`（秒精度），微秒被静默丢弃。
    后果不是"少几位小数"：同秒内的 `consumed_at`、`last_seq` 排序会不确定，
    `lease_until` 与令牌到期比较也会失真——这些正是并发正确性的依据。
    """
    moment = datetime(2026, 9, 16, 12, 34, 56, 789123)

    async def _scenario(conn: AsyncConnection) -> tuple[object, int]:
        await _add_user(conn, "usec", dept_id=None, created=moment)
        read_back = (
            await conn.execute(text("SELECT created_at FROM user WHERE username_norm = 'usec'"))
        ).scalar()
        micro = (
            await conn.execute(
                text("SELECT MICROSECOND(created_at) FROM user WHERE username_norm = 'usec'")
            )
        ).scalar()
        return read_back, int(micro or 0)

    read_back, micro = run(_with_conn(_scenario))
    assert read_back == moment, f"微秒丢失：写入 {moment} 读回 {read_back}"
    assert micro == 789123

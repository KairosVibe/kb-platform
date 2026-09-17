"""集成测试夹具：**真实 MySQL 8.0.26**（DATA-CONTRACTS §5 要求的真实后端验证）。

为什么必须单独有一套集成测试：唯一键竞争、外键删除限制、`CHECK` 是否真的拦脏数据、
`DATETIME(6)` 是否真的保留微秒、行锁与租约——这些**全都无法用内存库或元数据体检证明**。
DATA-CONTRACTS §5 原文："MySQL事务、唯一约束、锁、迁移及Milvus行为必须真实后端集成测试，
SQLite/内存不能替代"。

三条环境纪律：

1. **只连 `kb_platform_it`**（由 `kb_platform` 派生加 `_it`），测试数据不污染演示库；
2. **不可用时 clean skip**，而不是失败——本机没有 MySQL 时不该让整条流水线红掉，
   但 skip 原因必须写清"连不上哪个库""表数不对请先迁移"；
3. **任何输出都不打印口令**（`_masked_dsn()`）。
"""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any, TypeVar

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings

T = TypeVar("T")

BACKEND_DIR = Path(__file__).resolve().parents[2]
#: 期望表数 = 38 张业务表 + alembic_version。
#: ★ 只增不减地改这个数**不算通过**：表数对上只说明"迁移跑过了"，
#:   表集是否与契约一致由 `tests/test_models_metadata.py` 的集合比对负责。
EXPECTED_TABLES = 39


def it_dsn() -> str:
    """集成测试库 DSN。

    优先 `KB_IT_DATABASE_URL`；否则**从部署配置派生**（把库名换成 `<库名>_it`）——
    这样不需要把口令复制到第二个地方，也不会因为口令不同步导致测试连错库。
    """
    override = os.environ.get("KB_IT_DATABASE_URL")
    if override:
        return override
    base = get_settings().database_url
    return re.sub(r"/([^/?]+)(\?|$)", lambda m: f"/{m.group(1)}_it{m.group(2) or ''}", base, count=1)


def masked_dsn() -> str:
    """脱敏 DSN，用于把"连不上哪个库"写进 skip 原因而**不泄漏口令**。"""
    return re.sub(r"://([^:/@]+):[^@]*@", r"://\1:***@", it_dsn())


def run(coro: "Awaitable[T]") -> T:
    """在测试内执行协程。

    ★ 刻意不使用 pytest-asyncio：本机未安装该插件（`pytest.ini` 的 `asyncio_mode`
      目前会被忽略）。用 `asyncio.run` 每次开新事件循环，因此**每个测试必须自建引擎
      并在结束时 dispose**——不要复用进程级单例引擎，否则会撞上
      "attached to a different loop"。
    """
    return asyncio.run(coro)


def make_engine() -> AsyncEngine:
    """每个测试自用的引擎（`NullPool`：测试结束后连接立刻释放，避免残留连接干扰 TRUNCATE）。"""
    return create_async_engine(it_dsn(), poolclass=NullPool, future=True)


async def _probe() -> tuple[bool, str]:
    engine = make_engine()
    try:
        async with engine.connect() as conn:
            count = await conn.scalar(
                text(
                    "SELECT COUNT(*) FROM information_schema.tables "
                    "WHERE table_schema = DATABASE()"
                )
            )
        return True, str(int(count or 0))
    except Exception as exc:  # noqa: BLE001 - 任何连接失败都归为"不可用"
        return False, f"{type(exc).__name__}"
    finally:
        await engine.dispose()


@pytest.fixture(scope="session")
def it_database() -> Iterator[str]:
    """确认集成库可用且已迁移；否则跳过整个目录（原因明确、非静默）。"""
    ok, info = run(_probe())
    if not ok:
        pytest.skip(f"集成测试跳过：无法连接 {masked_dsn()}（{info}）")
    if info != str(EXPECTED_TABLES):
        pytest.skip(
            f"集成测试跳过：{masked_dsn()} 表数 {info} != {EXPECTED_TABLES}；"
            "请先执行 alembic -x db_url=... upgrade head"
        )
    # 供子进程探针使用（探针是"全新解释器"，必须显式获得同一个 DSN）。
    os.environ.setdefault("KB_IT_DATABASE_URL", it_dsn())
    return it_dsn()


async def _truncate_all(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
        names = (
            (
                await conn.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = DATABASE() AND table_name <> 'alembic_version'"
                    )
                )
            )
            .scalars()
            .all()
        )
        for name in names:
            await conn.execute(text(f"TRUNCATE TABLE `{name}`"))
        await conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))


@pytest.fixture
def clean_db(it_database: str) -> Iterator[None]:
    """每个用例前清空业务表（保留 `alembic_version`，即保留迁移状态）。"""

    async def _run() -> None:
        engine = make_engine()
        try:
            await _truncate_all(engine)
        finally:
            await engine.dispose()

    run(_run())
    yield


@pytest.fixture
def probe_env(it_database: str) -> dict[str, str]:
    """子进程环境：继承当前环境并显式带上集成库 DSN。"""
    env: dict[str, Any] = dict(os.environ)
    env["KB_IT_DATABASE_URL"] = it_dsn()
    env["PYTHONPATH"] = str(BACKEND_DIR)
    return env


PROBE_SCRIPT = Path(__file__).with_name("_restart_probe.py")


def run_probe(env: dict[str, str], *args: str) -> dict[str, Any]:
    """以**独立解释器进程**执行探针并解析其 JSON 输出。

    这是"注销不因重启失效"（PA-07）的关键：同一进程内的断言无法排除"状态还留在内存里"，
    因此每次操作都必须是一个全新的进程。
    """
    import json
    import subprocess
    import sys

    completed = subprocess.run(  # noqa: S603 - 参数为常量 + 生成的 UUID，无 shell
        [sys.executable, str(PROBE_SCRIPT), *args],
        cwd=str(BACKEND_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"探针失败（exit={completed.returncode}）args={args}\n"
            f"stdout={completed.stdout}\nstderr={completed.stderr}"
        )
    return json.loads(completed.stdout.strip().splitlines()[-1])


_ = Callable  # 保持导入被使用（类型注解用字符串形式时某些检查器会误报未使用）

"""Alembic 运行环境。

依据：DATA-CONTRACTS.md §5「生成工具链」、FUNCTION-MAP.md §2.1 第 10 条。

三个有意为之的设计：

1. **连接串不写进 `alembic.ini`**（那里留空），而是从 `app.core.config` 读取；临时覆盖用
   `alembic -x db_url=...`。理由：DSN 含口令，进版本库就等于提交凭据
   （DEPLOYMENT §2 第 4 条）。
2. **同时支持同步与异步 URL**。自动生成（autogenerate）需要真实连接，而开发机上
   MySQL 未必在跑；此时可临时指向一个空的 SQLite 文件——生成的**迁移操作是与方言无关的**
   （`op.create_table(...)`），真正渲染成 MySQL DDL 发生在执行 `upgrade` 时。
   ★ 但这只用于"生成迁移脚本"，**不能作为 MySQL 约束行为的验证**（§5 明确禁止）。
3. **离线模式（`alembic upgrade head --sql`）是本项目验证 DDL 的主要手段**：不需要连接
   也能按 MySQL 方言渲染出完整 DDL，便于在启动 MySQL 服务之前就审阅表结构。
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.core.config import get_settings
from app.db.base import Base

# ★ 副作用导入：只有 import app.models 才会把 19 张表注册进 Base.metadata。
#   漏掉它不会报错，只会生成一个"少表"的空迁移——见 app/models/__init__.py 的说明。
import app.models  # noqa: F401

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

_ASYNC_PREFIXES = ("mysql+aiomysql", "sqlite+aiosqlite")


def _database_url() -> str:
    """优先级：`-x db_url=` > `alembic.ini` 的 `sqlalchemy.url` > 部署配置。"""
    override = context.get_x_argument(as_dictionary=True).get("db_url")
    if override:
        return override
    configured = config.get_main_option("sqlalchemy.url")
    if configured:
        return configured
    return get_settings().database_url


def run_migrations_offline() -> None:
    """离线渲染 DDL：`alembic upgrade head --sql`。

    `literal_binds=True` 让参数直接内联，产出可人工审阅、可归档的 SQL 文本。
    """
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_async_migrations(url: str) -> None:
    engine = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        url=url,
    )
    async with engine.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await engine.dispose()


def run_migrations_online() -> None:
    url = _database_url()
    if url.startswith(_ASYNC_PREFIXES):
        asyncio.run(_run_async_migrations(url))
        return
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        url=url,
    )
    with connectable.connect() as connection:
        _do_run_migrations(connection)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

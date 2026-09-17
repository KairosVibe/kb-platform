"""异步引擎、会话工厂与请求作用域 session。

对应 FUNCTION-MAP.md §0 第 5 条（"DB session/文件存储/Provider 依赖由构造或请求作用域注入，
不在每条业务签名重复列出"）与 §2（UnitOfWork 事务契约）。

生命周期约定：

- 引擎与工厂**进程内单例**（`get_engine` / `get_sessionmaker`），由 `lru_cache` 保证；
  测试覆盖配置时调用 `reset_engine()` 清空。
- `get_session()` 是 FastAPI 依赖：一次请求一个 `AsyncSession`，请求结束自动关闭；
  异常时回滚后继续抛出，**不吞异常**（吞掉会让"写失败但接口 200"成为可能）。
- 事务边界不由本模块决定，而由 `UnitOfWork.transaction()` 显式划定（FUNCTION-MAP §2）：
  "写入与 operation_log 短事务一致；网络调用事务外"。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings


@lru_cache(maxsize=1)
def get_engine() -> AsyncEngine:
    """进程内单例异步引擎。

    `pool_pre_ping=True`：MySQL 8 默认 `wait_timeout` 会回收空闲连接，
    没有 pre-ping 时第一次查询会拿到已断开的连接并抛 `OperationalError`，
    在维护窗口后表现为"重启就报错"。代价是一次轻量 PING，可接受。
    """
    settings = get_settings()
    return create_async_engine(
        settings.database_url,
        echo=settings.db_echo,
        pool_pre_ping=True,
        future=True,
    )


@lru_cache(maxsize=1)
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """会话工厂。`expire_on_commit=False` 让提交后仍可读取已加载属性。"""
    return async_sessionmaker(
        bind=get_engine(),
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖：请求作用域 session。

    ★ 这里**只负责生命周期**，不自动提交。业务是否需要提交由服务层的
      `UnitOfWork.transaction()` 决定——隐式自动提交会让"多次写操作部分成功"
      变得难以发现。
    """
    session_factory = get_sessionmaker()
    async with session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    """释放连接池。应用关闭事件与测试收尾调用。"""
    if get_engine.cache_info().currsize:
        await get_engine().dispose()


def reset_engine() -> None:
    """清空单例缓存。**测试专用**：切换 `database_url` 后必须调用，
    否则会继续复用旧引擎（连到旧库）。"""
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()

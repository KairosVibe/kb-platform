"""刷新令牌持久仓储的真实 MySQL 集成验证（**审计 PA-07 的关闭证据**）。

契约要求（ARCHITECTURE §3 第 6 条、PRD AC-01.02-01）：
"注销不因重启失效"、"旧令牌不可重放"、"并发刷新只有一个成功"。

三条证据链分别是：
1. **跨进程重启**（`test_revocation_survives_process_restart`）：每个动作都是全新解释器，
   进程内实现必然失败、DB 实现必然通过——这是 PA-07 的判定性证据；
2. **重放被唯一键挡住**（进程内重复消费与并发消费）；
3. **会话撤销后新令牌也不能用**（撤销是会话级的，不是单令牌级的）。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.models import AuthSession, User
from app.services.auth_svc import PersistentRefreshStore
from tests.integration.conftest import it_dsn, run, run_probe

TOKEN_TTL = timedelta(days=7)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _make_fixture(session_id: UUID) -> int:
    """建一个账号与一个登录会话，返回 user_id。"""
    engine = create_async_engine(it_dsn(), poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            async with session.begin():
                user = User(
                    username=f"it-{uuid4().hex[:12]}",
                    username_norm=f"it-{uuid4().hex[:12]}",
                    password_hash="!integration-no-login",
                    enabled=True,
                )
                session.add(user)
                await session.flush()
                session.add(
                    AuthSession(
                        id=session_id, user_id=user.id, expires_at=_utcnow() + TOKEN_TTL
                    )
                )
                return int(user.id)
    finally:
        await engine.dispose()


# ---------------------------------------------------------------- 1. 跨进程重启

def test_revocation_survives_process_restart(clean_db: None, probe_env: dict[str, str]) -> None:
    """★ PA-07 判定性证据：注销在**新进程**里依然有效。

    序列（每一步都是独立解释器进程）：
      1. seed   —— 创建会话并消费一个刷新令牌
      2. verify —— 新进程读回：令牌已消费、会话未撤销
      3. revoke —— 新进程注销该会话
      4. verify —— 新进程读回：会话已撤销（**重启后仍为已撤销**）
      5. consume—— 撤销后即便是一个全新令牌也不允许消费
    """
    session_id = uuid4()
    jti = uuid4().hex

    seeded = run_probe(probe_env, "seed", "--session", str(session_id), "--jti", jti)
    assert seeded["consumed"] is True

    after_restart = run_probe(probe_env, "verify", "--session", str(session_id), "--jti", jti)
    assert after_restart["is_consumed"] is True, "重启后令牌消费状态丢失 → 与进程内实现无异"
    assert after_restart["session_revoked"] is False

    revoked = run_probe(probe_env, "revoke", "--session", str(session_id))
    assert revoked["revoked"] is True

    after_revoke_restart = run_probe(
        probe_env, "verify", "--session", str(session_id), "--jti", jti
    )
    assert after_revoke_restart["session_revoked"] is True, "重启后撤销失效 → 已注销令牌可重放"

    fresh_token = run_probe(
        probe_env, "consume", "--session", str(session_id), "--jti", uuid4().hex
    )
    assert fresh_token["consumed"] is False, "会话已撤销时任何令牌都不应能再次消费"


# ---------------------------------------------------------------- 2. 重放与并发

def test_replay_is_rejected_by_unique_key(clean_db: None) -> None:
    """同一 `jti` 第二次消费必须失败（靠 `refresh_token.token_hash` 唯一键，不靠应用判断）。"""

    async def _scenario() -> tuple[bool, bool]:
        session_id = uuid4()
        await _make_fixture(session_id)
        jti = uuid4().hex
        engine = create_async_engine(it_dsn(), poolclass=NullPool)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as session:
                async with session.begin():
                    store = PersistentRefreshStore(session)
                    first = await store.consume(
                        jti, auth_session_id=session_id, expires_at=_utcnow() + TOKEN_TTL
                    )
                    # 同一个事务内再消费一次：唯一键冲突必须被翻译成 False 且**不污染事务**
                    # （SAVEPOINT 的作用；否则后续语句会报 "transaction aborted"）。
                    second = await store.consume(
                        jti, auth_session_id=session_id, expires_at=_utcnow() + TOKEN_TTL
                    )
                    still_usable = await store.is_consumed(jti)
                    assert still_usable is True
                    return bool(first), bool(second)
        finally:
            await engine.dispose()

    first, second = run(_scenario())
    assert (first, second) == (True, False)


def test_concurrent_refresh_only_one_wins(clean_db: None) -> None:
    """AC-01.02-01：两个并发刷新，只有一个成功。

    用**两条独立连接**同时消费同一个 `jti`——这正是"用户双击刷新按钮"或"多标签页同时刷新"
    的真实形态。唯一键让其中一方必然失败（重复键或死锁），业务层只需把失败翻译成 `False`。
    """

    async def _attempt(session_id: UUID, jti: str) -> bool:
        engine = create_async_engine(it_dsn(), poolclass=NullPool)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as session:
                async with session.begin():
                    store = PersistentRefreshStore(session)
                    return bool(
                        await store.consume(
                            jti, auth_session_id=session_id, expires_at=_utcnow() + TOKEN_TTL
                        )
                    )
        finally:
            await engine.dispose()

    async def _scenario() -> list[bool]:
        session_id = uuid4()
        await _make_fixture(session_id)
        jti = uuid4().hex
        return list(await asyncio.gather(_attempt(session_id, jti), _attempt(session_id, jti)))

    results = run(_scenario())
    assert results.count(True) == 1, f"并发刷新应恰好一个成功，实际 {results}"


# ---------------------------------------------------------------- 3. 幂等与 fail-closed

def test_revoke_is_idempotent(clean_db: None) -> None:
    """重复注销返回 False 但不报错（前端重试退出不应看到 5xx，F-01.03 边界）。"""
    session_id = uuid4()
    run(_make_fixture(session_id))

    async def _scenario() -> tuple[bool, bool]:
        engine = create_async_engine(it_dsn(), poolclass=NullPool)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as session:
                store = PersistentRefreshStore(session)
                async with session.begin():
                    first = await store.revoke_session(session_id)
                async with session.begin():
                    second = await store.revoke_session(session_id)
                return bool(first), bool(second)
        finally:
            await engine.dispose()

    first, second = run(_scenario())
    assert first is True
    assert second is False


def test_unknown_session_is_fail_closed(clean_db: None) -> None:
    """会话行不存在时消费必须失败（fail-closed），不能因为"查不到"就放行。"""
    session_id = uuid4()
    run(_make_fixture(session_id))
    unknown = uuid4()

    async def _scenario() -> bool:
        engine = create_async_engine(it_dsn(), poolclass=NullPool)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as session:
                async with session.begin():
                    store = PersistentRefreshStore(session)
                    return bool(
                        await store.consume(
                            uuid4().hex,
                            auth_session_id=unknown,
                            expires_at=_utcnow() + TOKEN_TTL,
                        )
                    )
        finally:
            await engine.dispose()

    assert run(_scenario()) is False


def test_revoked_session_marks_its_tokens_revoked(clean_db: None) -> None:
    """注销时同事务把该会话**全部未撤销令牌**一并作废（否则旧令牌仍能刷新）。"""
    session_id = uuid4()
    run(_make_fixture(session_id))
    jti = uuid4().hex

    async def _scenario() -> tuple[bool, bool]:
        engine = create_async_engine(it_dsn(), poolclass=NullPool)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as session:
                async with session.begin():
                    store = PersistentRefreshStore(session)
                    consumed = await store.consume(
                        jti, auth_session_id=session_id, expires_at=_utcnow() + TOKEN_TTL
                    )
                    revoked = await store.revoke_session(session_id)
                    assert consumed and revoked
                # 读回：该令牌的 revoked_at 应已写入
                from app.models import RefreshToken

                row = (
                    await session.execute(
                        select(RefreshToken).where(
                            RefreshToken.token_hash == PersistentRefreshStore.hash_token_id(jti)
                        )
                    )
                ).scalars().one()
                return row.revoked_at is not None, row.consumed_at is not None
        finally:
            await engine.dispose()

    has_revoked_at, has_consumed_at = run(_scenario())
    assert has_revoked_at is True
    assert has_consumed_at is True


@pytest.mark.parametrize("jti_len", [32, 64])
def test_token_hash_is_deterministic_and_not_raw(clean_db: None, jti_len: int) -> None:
    """落库的是 `jti` 的 SHA-256，不是原文（DATA-CONTRACTS §2"令牌原文不落库"）。"""
    jti = "a" * jti_len
    digest = PersistentRefreshStore.hash_token_id(jti)
    assert len(digest) == 64
    assert digest != jti
    assert digest == PersistentRefreshStore.hash_token_id(jti)

"""跨进程持久化探针：生成"注销不因重启失效"的证据（审计 PA-07）。

为什么必须是**独立进程**：同一进程内的断言无法排除"状态还留在内存里"这一可能——
而进程内实现（`InMemoryRefreshRevocationStore`）恰恰就是这么失败的。每次动作开一个
全新解释器，就等价于"服务重启后再操作"。

用法（由 `tests/integration/conftest.py::run_probe` 以子进程调用）：

    python tests/integration/_restart_probe.py seed    --session <uuid> --jti <hex>
    python tests/integration/_restart_probe.py consume --session <uuid> --jti <hex>
    python tests/integration/_restart_probe.py revoke  --session <uuid>
    python tests/integration/_restart_probe.py verify  --session <uuid> --jti <hex>

每个动作向 stdout 打印**一行** JSON，供父进程解析。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.models import AuthSession, User
from app.services.auth_svc import PersistentRefreshStore

#: 探针自己那行账号（每个新进程都复用同一行，避免累积垃圾数据）。
PROBE_USERNAME = "__probe__"
TOKEN_TTL = timedelta(days=7)


def _dsn() -> str:
    value = os.environ.get("KB_IT_DATABASE_URL")
    if not value:
        raise SystemExit(
            "缺少 KB_IT_DATABASE_URL：探针必须由集成测试夹具以子进程方式调用。"
        )
    return value


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _ensure_user(session) -> int:
    row = (
        (await session.execute(select(User).where(User.username_norm == PROBE_USERNAME)))
        .scalars()
        .first()
    )
    if row is None:
        row = User(
            username=PROBE_USERNAME,
            username_norm=PROBE_USERNAME,
            # 探针不需要登录，密码哈希只需满足 NOT NULL；**不写入任何可用口令**。
            password_hash="!probe-no-login",
            enabled=True,
        )
        session.add(row)
        await session.flush()
    return int(row.id)


async def _ensure_session(session, session_id: UUID) -> int:
    user_id = await _ensure_user(session)
    row = await session.get(AuthSession, session_id)
    if row is None:
        session.add(
            AuthSession(id=session_id, user_id=user_id, expires_at=_utcnow() + TOKEN_TTL)
        )
        await session.flush()
    return user_id


async def _act(args: argparse.Namespace) -> dict[str, object]:
    engine = create_async_engine(_dsn(), poolclass=NullPool, future=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            async with session.begin():
                session_id = UUID(args.session)
                await _ensure_session(session, session_id)
                store = PersistentRefreshStore(session)
                expires_at = _utcnow() + TOKEN_TTL

                if args.action in {"seed", "consume"}:
                    ok = await store.consume(
                        args.jti, auth_session_id=session_id, expires_at=expires_at
                    )
                    return {"action": args.action, "consumed": bool(ok)}

                if args.action == "revoke":
                    return {
                        "action": "revoke",
                        "revoked": bool(await store.revoke_session(session_id)),
                    }

                # verify
                return {
                    "action": "verify",
                    "is_consumed": bool(await store.is_consumed(args.jti)),
                    "session_revoked": bool(await store.is_session_revoked(session_id)),
                }
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["seed", "consume", "revoke", "verify"])
    parser.add_argument("--session", required=True)
    parser.add_argument("--jti", default="")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(_act(args)), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

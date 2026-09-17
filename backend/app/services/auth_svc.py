"""F-01.02 刷新令牌的**持久**撤销/消费仓储（M1-6，关闭审计 PA-07 的实现部分）。

契约要求（ARCHITECTURE §3 第 6 条、PRD AC-01.02-01、DATA-CONTRACTS §2）：
**注销不因重启失效**；旧令牌不可重放；并发刷新只有一个成功。

进程内实现在原理上不可能满足第一条，因此 `app/core/security.py` 的
`InMemoryRefreshRevocationStore` 已被限定为"仅 dev 的测试替身"（`app_env != "dev"`
构造即失败）。本模块提供 DB 版本，状态落在 `auth_session` / `refresh_token` 两表。

三条与进程内实现**逐条对齐**的语义（替换实现时不得放宽）：

1. `consume` 同一 `jti` 只能成功一次——靠 `refresh_token.token_hash` 的**唯一键**保证，
   而不是靠"先查再插"（后者在并发下会双双成功，正是 AC-01.02-01 要防的场景）。
2. 会话已撤销时 `consume` 一律返回 False，无论该令牌是否被消费过。
3. `revoke_session` 幂等：重复撤销返回 False 但不报错（前端重试退出不应看到 5xx）。

事务边界：本类的写方法**假定调用方已经开启事务**（DATA-CONTRACTS §3："变更与审计
同事务"）。这里不自己 `begin()`——如果它在内部提交，审计记录就会被拆到另一个事务里，
两者不一致时无法回滚。
"""

from __future__ import annotations

import hashlib
import time
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from threading import Lock
from uuid import UUID, uuid4

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.permissions import AI_ASK
from app.core.response import BizError
from app.core.security import (
    PasswordPolicyError,
    TokenError,
    TokenPayload,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.db.base import utcnow
from app.engines.permission import UserCtx
from app.models import AuthSession, RefreshToken, RolePermission, User, UserRole, normalize_username


def _to_utc(value: int | datetime | None) -> datetime:
    """归一化时间为朴素 UTC。

    JWT 的 `exp` 是 epoch 秒（`create_refresh_token` 返回 int），而 DB 列是
    `DATETIME(6)` UTC。两者混用会出现"看起来差 8 小时"的时区错误，所以在这里
    统一转换，业务代码不需要知道这层差异。
    """
    if value is None:
        return utcnow()
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo is not None else value
    return datetime.fromtimestamp(value, tz=timezone.utc).replace(tzinfo=None)


class PersistentRefreshStore:
    """DB 持久化的刷新令牌撤销/消费表。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ---------------------------------------------------------------- 内部

    @staticmethod
    def hash_token_id(jti: str) -> str:
        """`jti` → `token_hash`。

        ★ 存哈希而不是 `jti` 原文：`jti` 是令牌的可复现组成部分，落库后一旦库被读走，
          就能配合日志/抓包定位到具体令牌。哈希后仍可"用同一个 `jti` 再算一次"做查找，
          但库里的值本身不再是有用的令牌片段（DATA-CONTRACTS §2："令牌原文不落库"）。
        """
        return hashlib.sha256(jti.encode("utf-8")).hexdigest()

    # ---------------------------------------------------------------- 读

    async def is_consumed(self, jti: str) -> bool:
        token_hash = self.hash_token_id(jti)
        row = await self._session.scalar(
            select(RefreshToken.id).where(
                RefreshToken.token_hash == token_hash,
                RefreshToken.consumed_at.is_not(None),
            )
        )
        return row is not None

    async def is_session_revoked(self, auth_session_id: object) -> bool:
        """会话是否已撤销。**会话不存在时返回 True**（fail-closed，理由见 `consume`）。

        注意这里为什么要查两次：`select(revoked_at)` 在"行不存在"与"revoked_at 为 NULL"
        两种情况下都返回 None。把两者混为一谈会得到一个反向错误——已撤销的会话被当成
        未撤销，注销就形同虚设。因此必须再查一次"行是否存在"来区分。
        """
        revoked_at = await self._session.scalar(
            select(AuthSession.revoked_at).where(AuthSession.id == auth_session_id)
        )
        if revoked_at is not None:
            return True
        return not await self._session_exists(auth_session_id)

    async def _session_exists(self, auth_session_id: object) -> bool:
        found = await self._session.scalar(
            select(AuthSession.id).where(AuthSession.id == auth_session_id)
        )
        return found is not None

    # ---------------------------------------------------------------- 写

    async def consume(
        self,
        jti: str,
        *,
        auth_session_id: object,
        expires_at: int | datetime,
        now: int | datetime | None = None,
    ) -> bool:
        """一次性消费刷新令牌。首次返回 True；重放或会话已撤销返回 False。

        ★ 不存在的会话按"已撤销"处理（fail-closed）：正常情况下令牌一定属于某个
          已登记的会话；若会话查不到，要么数据被清理、要么调用方传错了 ID——
          两种情况都不应该允许继续签发新令牌。
        """
        if not await self._session_exists(auth_session_id):
            return False
        if await self.is_session_revoked(auth_session_id):
            return False

        record = RefreshToken(
            auth_session_id=auth_session_id,
            token_hash=self.hash_token_id(jti),
            expires_at=_to_utc(expires_at),
            consumed_at=_to_utc(now),
        )
        try:
            # ★ 必须包在 SAVEPOINT（begin_nested）里：直接 flush 抛 IntegrityError 会让
            #   整个事务进入失效状态，调用方随后的语句全部报 "transaction aborted"。
            #   重放是**预期路径**（AC-01.02-01 专门测它），不能因为一次重放就毁掉事务。
            async with self._session.begin_nested():
                self._session.add(record)
                await self._session.flush()
        except IntegrityError:
            # 唯一键把重放挡在数据库层：并发下的两个请求只有一个能插入成功。
            return False
        except OperationalError as exc:
            # 两个并发插入同一唯一键时，MySQL 除了报重复键（1062）还可能**报死锁**
            # （1213）：InnoDB 检测到互相等待后回滚其中一方。对业务而言这与"另一个刷新
            # 赢了"是同一种结果——AC-01.02-01 要求"并发刷新仅一个成功"，败者应当收到
            # 明确的"不可用"，而不是一个 500。其余 OperationalError（连接断开等）
            # 仍必须上抛：那是依赖故障，不能伪装成"令牌已被用过"。
            if "1213" in str(getattr(exc, "orig", "")):
                return False
            raise
        return True

    async def revoke_session(
        self, auth_session_id: object, *, now: int | datetime | None = None
    ) -> bool:
        """撤销整个会话（注销）。返回是否**本次**撤销生效；已撤销过返回 False（幂等）。"""
        ts = _to_utc(now)
        result = await self._session.execute(
            update(AuthSession)
            .where(AuthSession.id == auth_session_id, AuthSession.revoked_at.is_(None))
            .values(revoked_at=ts)
        )
        if result.rowcount != 1:
            return False
        # 同一事务内把该会话的全部未撤销令牌一并作废：否则"已注销"的会话仍能靠
        # 未消费的旧令牌刷新（这正是"退出后该会话两类令牌失效"要防的）。
        await self._session.execute(
            update(RefreshToken)
            .where(RefreshToken.auth_session_id == auth_session_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=ts)
        )
        return True

    async def purge_expired(self, *, now: int | datetime | None = None) -> int:
        """清理已过期条目（过期后令牌本身已失效，无需再记）。返回删除行数。"""
        ts = _to_utc(now)
        tokens = await self._session.execute(
            delete(RefreshToken).where(RefreshToken.expires_at <= ts)
        )
        sessions = await self._session.execute(
            delete(AuthSession).where(
                AuthSession.expires_at <= ts, AuthSession.revoked_at.is_not(None)
            )
        )
        return int(tokens.rowcount or 0) + int(sessions.rowcount or 0)


# ---------------------------------------------------------------- 登录限流（单实例）

class _LoginRateLimiter:
    """登录限流：进程内滑窗，按 IP。

    ★ 只在**单实例、`workers=1`** 的当前目标部署下成立（DEPLOYMENT §1）。改成多实例后
      必须换成共享存储，否则每个实例各记一份，实际上限被实例数放大。
      这是有意的取舍：契约要求"同 IP 每分钟上限"，单实例下进程内计数就是**准确**实现；
      为此引入 Redis 会新增一个当前部署里不存在的依赖。
    """

    def __init__(self, *, per_minute: int, window_seconds: float = 60.0) -> None:
        self._per_minute = per_minute
        self._window = window_seconds
        self._hits: dict[str, list[float]] = {}
        self._lock = Lock()

    def allow(self, ip: str, *, now: float | None = None) -> bool:
        """是否放行这次尝试。超限返回 False（调用方映射为 429）。"""
        ts = now if now is not None else time.monotonic()
        with self._lock:
            self._prune(ts)
            window = [t for t in self._hits.get(ip, ()) if ts - t < self._window]
            if len(window) >= self._per_minute:
                self._hits[ip] = window
                return False
            window.append(ts)
            self._hits[ip] = window
            return True

    def _prune(self, ts: float) -> None:
        """丢弃窗口外的键。字典键随来源 IP 增长，长期运行必须有上限，否则是内存泄漏。"""
        stale = [k for k, v in self._hits.items() if not v or ts - v[-1] >= self._window]
        for key in stale:
            self._hits.pop(key, None)


@lru_cache(maxsize=1)
def _login_limiter() -> _LoginRateLimiter:
    return _LoginRateLimiter(per_minute=get_settings().login_rate_per_min)


def _reset_login_limiter() -> None:
    """清空限流计数。**测试专用**：用例之间不能互相污染窗口。

    刻意用私有名：它没有业务契约，不应出现在 FUNCTION-MAP 的符号登记里
    （门禁会跳过下划线开头的符号，见 scripts/check_doc_code_sync.py）。
    """
    _login_limiter.cache_clear()


# ---------------------------------------------------------------- 内部工具

def _payload_or_401(token: str, *, expect_refresh: bool) -> TokenPayload:
    """解码令牌并把 `TokenError` 映射为契约里的 401 错误码。

    区分"仅过期"与"签名/结构非法"：前者前端可以尝试刷新，后者必须直接回到登录页
    （TOKEN_EXPIRED / TOKEN_INVALID）。
    """
    try:
        return decode_token(token, expect_refresh=expect_refresh)
    except TokenError as exc:
        raise BizError("TOKEN_EXPIRED" if exc.expired else "TOKEN_INVALID", str(exc)) from exc


@lru_cache(maxsize=1)
def _timing_equalizer_hash() -> str:
    """登录失败时用来"白跑一次哈希校验"的替身。

    账号不存在时若直接返回错误，响应时间会明显短于"账号存在但密码错"，
    等于把用户名枚举做成计时侧信道。这里对不存在的账号走一次真实的 bcrypt 校验，
    让两条路径耗时同阶。惰性计算（首次用到才算），避免拖慢进程启动。
    """
    return hash_password("timing-equalizer-not-a-real-credential")


async def _find_user_by_username(session: AsyncSession, username: str) -> User | None:
    """按**规范化**登录名查账号（唯一性只由 `username_norm` 决定，见 models/identity.py）。"""
    norm = normalize_username(username)
    return (
        (await session.execute(select(User).where(User.username_norm == norm))).scalars().first()
    )


async def _load_identity(session: AsyncSession, user_id: int) -> tuple[frozenset[int], frozenset[str]]:
    """装配角色的功能权限码。

    ★ 每次请求都从库里读，**不缓存、不写进令牌**（ARCHITECTURE §3 第 6 条）：
      权限收紧后必须立刻生效，若放进令牌就会有最长一个 access token 有效期的越权窗口。
    """
    role_ids = frozenset(
        int(r)
        for r in (
            (await session.execute(select(UserRole.role_id).where(UserRole.user_id == user_id)))
            .scalars()
            .all()
        )
    )
    if not role_ids:
        return role_ids, frozenset()
    codes = frozenset(
        str(c)
        for c in (
            await session.execute(
                select(RolePermission.code).where(RolePermission.role_id.in_(role_ids))
            )
        )
        .scalars()
        .all()
    )
    return role_ids, codes


# ---------------------------------------------------------------- H01 / H02

async def authenticate(session: AsyncSession, access_token: str) -> UserCtx:
    """H01：把 access token 换成 `UserCtx`（读库部分）。

    契约逻辑三步：① 验签/用途/过期；② **核对持久会话撤销**及用户状态；③ 加载当前
    直属部门与角色功能码。第 ② 步依赖 payload 里的 `sid`（见 FUNCTION-MAP H01 说明）。

    失败一律 401，且**不区分**"令牌无效"与"账号不存在"——两者都对客户端是同一种处置
    （回登录页），分别披露只会变成账号枚举的接口。
    """
    payload = _payload_or_401(access_token, expect_refresh=False)

    # ② 会话撤销核对：注销后 access token 必须**立即**失效，而不是等它自然过期。
    revoked = await session.scalar(
        select(AuthSession.revoked_at).where(AuthSession.id == payload.session_id)
    )
    session_row = await session.scalar(
        select(AuthSession.id).where(AuthSession.id == payload.session_id)
    )
    if session_row is None:
        raise BizError("TOKEN_INVALID")
    if revoked is not None:
        raise BizError("TOKEN_INVALID")

    # ② 用户状态
    user = await session.get(User, payload.user_id)
    if user is None:
        raise BizError("TOKEN_INVALID")
    if not user.enabled:
        raise BizError("USER_DISABLED")

    # ③ 实时身份
    role_ids, codes = await _load_identity(session, user.id)

    return UserCtx(
        user_id=int(user.id),
        session_id=payload.session_id,
        dept_id=int(user.dept_id) if user.dept_id is not None else None,
        role_ids=role_ids,
        permission_codes=codes,
        identity_revision=int(user.identity_revision),
    )


def require_permission(ctx: UserCtx, code: str) -> None:
    """H02：功能权限检查。缺失抛 403。

    ★ 功能权限**不代替**数据权限（H02 边界原文）：通过本函数只说明"能执行这类操作"，
      能不能看到某条正文仍由 H03/H04 判定。系统管理员同样按普通规则逐项检查。
    """
    if code not in ctx.permission_codes:
        # `ai:ask` 有独立错误码，便于前端提示"未开通 AI 问答"而非泛化的"无权限"
        raise BizError("PERM_AI_DENIED" if code == AI_ASK else "PERM_DENIED")


# ---------------------------------------------------------------- F-01.01—F-01.04

def _issue_tokens(user_id: int, session_id: UUID) -> tuple[str, str, int]:
    """签发一对令牌，返回 `(access_token, refresh_token, expires_in)`。"""
    settings = get_settings()
    access = create_access_token(user_id, session_id)
    refresh, _jti, _exp = create_refresh_token(user_id, session_id)
    return access, refresh, settings.access_ttl_min * 60


async def login(
    session: AsyncSession, *, username: str, password: str, ip: str
) -> tuple[str, str, int]:
    """F-01.01：登录。返回 `(access_token, refresh_token, expires_in)`。

    顺序按契约：① 限流 → ② 查账号验密码与启用状态 → ③ 建立持久刷新会话 → ④ 签发令牌。

    ★ 两处顺序是有意的：**先校验凭据再判断启用**（否则用错误密码就能探出"该账号存在
      但被停用"）；**超长密码先于凭据判定抛 422**（F-01.01 要求显式拒绝，不能退化成
      "用户名或密码错误"）。
    """
    if not _login_limiter().allow(ip):
        raise BizError("RATE_LIMITED")

    user = await _find_user_by_username(session, username)
    target_hash = user.password_hash if user is not None else _timing_equalizer_hash()
    try:
        matched = verify_password(password, target_hash)
    except PasswordPolicyError as exc:
        raise BizError("PASSWORD_LENGTH_INVALID", str(exc)) from exc

    if user is None or not matched:
        raise BizError("BAD_CREDENTIALS")
    if not user.enabled:
        raise BizError("USER_DISABLED")

    settings = get_settings()
    session_id = uuid4()
    session.add(
        AuthSession(
            id=session_id,
            user_id=int(user.id),
            expires_at=utcnow() + timedelta(days=settings.refresh_ttl_days),
        )
    )
    await session.flush()
    return _issue_tokens(int(user.id), session_id)


async def rotate_refresh(session: AsyncSession, *, refresh_token: str) -> tuple[str, str, int]:
    """F-01.02：刷新。返回新的 `(access_token, refresh_token, expires_in)`。

    契约逻辑：验签与用途 → **锁定持久刷新会话** → 检查账号 → 一次性消费旧令牌并签发新令牌。
    行锁（`with_for_update`）用于串行化同一会话的并发刷新；跨进程的重复消费由
    `refresh_token.token_hash` 唯一键兜底（见 `PersistentRefreshStore.consume`）。
    """
    payload = _payload_or_401(refresh_token, expect_refresh=True)

    session_row = (
        (
            await session.execute(
                select(AuthSession)
                .where(AuthSession.id == payload.session_id)
                .with_for_update()
            )
        )
        .scalars()
        .first()
    )
    if session_row is None or session_row.revoked_at is not None:
        raise BizError("TOKEN_INVALID")
    if int(session_row.user_id) != payload.user_id:
        # 令牌与会话归属不一致：结构被篡改或数据被错配，一律拒绝
        raise BizError("TOKEN_INVALID")

    user = await session.get(User, payload.user_id)
    if user is None:
        raise BizError("TOKEN_INVALID")
    if not user.enabled:
        raise BizError("USER_DISABLED")

    consumed = await PersistentRefreshStore(session).consume(
        payload.jti, auth_session_id=payload.session_id, expires_at=payload.exp
    )
    if not consumed:
        # 重放：同一刷新令牌被用过第二次（AC-01.02-01）
        raise BizError("TOKEN_INVALID")

    return _issue_tokens(payload.user_id, payload.session_id)


async def logout(session: AsyncSession, *, ctx: UserCtx, refresh_token: str) -> bool:
    """F-01.03：退出。返回是否**本次**撤销生效（重复退出返回 False，幂等）。

    契约处理逻辑 3 是"停止该会话活动请求"——那需要 `chat_request` 表参与（M05 范围），
    当前尚未实现，故这里**只做会话撤销**；不要在文档或简历中把"退出即取消在途生成"
    说成已完成。
    """
    payload = _payload_or_401(refresh_token, expect_refresh=True)
    if payload.user_id != ctx.user_id:
        # 不能拿别人的刷新令牌去注销别人的会话（F-01.03 处理逻辑 1"验证归属"）
        raise BizError("FORBIDDEN_OWNER")
    return await PersistentRefreshStore(session).revoke_session(payload.session_id)


async def get_me(session: AsyncSession, *, ctx: UserCtx) -> dict[str, object]:
    """F-01.04：当前身份最小视图（六字段，对齐 `app/schemas/auth.py::MeResponse`）。"""
    user = await session.get(User, ctx.user_id)
    if user is None:
        raise BizError("NOT_FOUND")
    return {
        "user_id": ctx.user_id,
        "username": user.username,
        "dept_id": ctx.dept_id,
        # 排序输出：审计与用例比对需要稳定顺序（集合迭代顺序不可依赖）
        "role_ids": sorted(ctx.role_ids),
        "permission_codes": sorted(ctx.permission_codes),
        "revision": int(user.revision),
    }


__all__ = [
    "PersistentRefreshStore",
    "authenticate",
    "get_me",
    "login",
    "logout",
    "require_permission",
    "rotate_refresh",
]

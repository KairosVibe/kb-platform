"""安全原语：密码哈希、JWT 令牌与刷新令牌撤销表。

对应 FUNCTION-MAP.md §4 H01（auth_core.authenticate）、§3 F-01.01 login /
F-01.02 rotate_refresh / F-01.03 logout、DESIGN_REVISION.md §2.3、
ARCHITECTURE.md §3（第 6 条）、DATA-CONTRACTS.md §2（auth_session / refresh_token）。

关键设计：

1. **JWT payload 最小化**：只放 sub(user_id)/exp/iat/jti/typ/**sid(auth_session_id)**，
   **不放** role/dept/perms。原因：若把权限声明写进 token，权限收紧后旧 token 在有效期内
   仍携带过期声明，形成越权窗口。权限一律服务端实时解析（ARCHITECTURE §3 第 6 条）。
   ★ `sid` 是**唯一**额外声明，理由是"注销必须立即生效"：H01 要"核对持久会话撤销"，
     令牌不带会话标识就无处可核，access token 将在注销后继续有效至多 2 小时。
     `sid` 不是权限快照（不含角色/部门/权限码），不违反最小化原则。
2. **密码长度契约（审计 PA-09）**：创建时 12—72 UTF-8 字节；任何场景 >72 字节
   **显式拒绝**，绝不静默截断——截断会让"前 72 字节相同的两个不同密码"互相通过。
   见 FUNCTION-MAP F-01.01 边界及错误。
3. **刷新令牌撤销/消费表（审计 PA-07）**：契约要求"注销不因重启失效"
   （ARCHITECTURE §3 第 6 条），而进程内实现天然不满足；因此当前实现
   `InMemoryRefreshRevocationStore` **仅限 dev**——`app_env` 非 "dev" 时构造即失败，
   防止它被带上生产。DB 持久仓储（`auth_session`/`refresh_token`）待 M1-6。
4. 依赖选用 PyJWT + bcrypt 原生库，而非 python-jose + passlib：
   后两者维护停滞，且 passlib 与 bcrypt 4.x/5.x 存在已知兼容问题。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from functools import lru_cache
from threading import Lock
from uuid import UUID

import bcrypt
import jwt

from app.core.config import get_settings

# ---------------------------------------------------------------- 密码长度契约

# FUNCTION-MAP F-01.01：创建 12—72 UTF-8 字节；登录允许既有短密码，但同样拒绝 >72 字节。
PASSWORD_MIN_BYTES = 12
PASSWORD_MAX_BYTES = 72
# bcrypt 自身的硬上限（超过会抛异常或截断，故在此之前显式拒绝）
_BCRYPT_MAX_BYTES = 72


class PasswordPolicyError(ValueError):
    """密码不满足长度契约。

    ★ 与"凭据错误"是两回事：由 API 层映射为**参数错误**，不得退化为统一的
    "用户名或密码错误"——否则用户无法得知自己输入过长（F-01.01 要求"显式拒绝"）。
    """


def _as_bytes(plain: str) -> bytes:
    return plain.encode("utf-8")


def hash_password(plain: str) -> str:
    """bcrypt 哈希，cost=12。**创建/改密路径**使用，执行完整长度策略。

    cost=12 是安全与延迟的平衡点（约 0.2—0.3s/次）。配合登录限流（同 IP 每分钟上限），
    既抗暴力破解，又不至于被单连接拖垮 2C 机器。
    """
    if not plain:
        raise PasswordPolicyError("密码不能为空")
    raw = _as_bytes(plain)
    if len(raw) < PASSWORD_MIN_BYTES:
        raise PasswordPolicyError(f"密码至少 {PASSWORD_MIN_BYTES} 个 UTF-8 字节")
    if len(raw) > PASSWORD_MAX_BYTES:
        raise PasswordPolicyError(f"密码不得超过 {PASSWORD_MAX_BYTES} 个 UTF-8 字节")
    return bcrypt.hashpw(raw, bcrypt.gensalt(rounds=12)).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    """校验密码。**登录路径**使用：不校验最小长度（允许既有短密码），但拒绝超长。

    ★ PA-09：超长必须**抛异常**而不是返回 False——返回 False 会与"密码错误"混为一谈，
      调用方无法区分"输入非法"与"凭据不对"；截断则更糟（安全缺陷）。
    哈希串损坏时不抛异常、按校验失败处理，避免暴露内部状态。
    """
    if not plain or not hashed:
        return False
    raw = _as_bytes(plain)
    # >72 字节显式拒绝（该上限同时是 bcrypt 的硬上限，见 PASSWORD_MAX_BYTES 注释）
    if len(raw) > PASSWORD_MAX_BYTES:
        raise PasswordPolicyError(f"密码不得超过 {PASSWORD_MAX_BYTES} 个 UTF-8 字节")
    try:
        return bcrypt.checkpw(raw, hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------- JWT


@dataclass(frozen=True, slots=True)
class TokenPayload:
    user_id: int
    jti: str
    exp: int
    is_refresh: bool
    #: 归属的登录会话。H01 据此核对会话是否已撤销（F-01.03 注销立即生效）。
    session_id: UUID


class TokenError(Exception):
    """令牌异常。

    expired=True 表示仅过期（前端可尝试刷新）；False 表示签名/结构非法（直接跳登录）。
    区分二者可避免"凭证被篡改还去尝试刷新"的无用请求。
    """

    def __init__(self, message: str, *, expired: bool = False) -> None:
        self.expired = expired
        super().__init__(message)


def _encode(
    user_id: int, *, session_id: UUID, ttl_seconds: int, is_refresh: bool
) -> tuple[str, str, int]:
    """内部：生成 token，返回 (token, jti, exp_ts)。"""
    settings = get_settings()
    now = int(time.time())
    exp = now + ttl_seconds
    jti = uuid.uuid4().hex
    payload: dict[str, object] = {
        "sub": str(user_id),
        "exp": exp,
        "iat": now,
        "jti": jti,
        "typ": "refresh" if is_refresh else "access",
        # sid：登录会话标识，供 H01 核对撤销（见模块 docstring 第 1 条 ★）
        "sid": str(session_id),
    }
    token = jwt.encode(
        payload, settings.jwt_secret.get_secret_value(), algorithm=settings.jwt_algorithm
    )
    return token, jti, exp


def create_access_token(user_id: int, session_id: UUID) -> str:
    settings = get_settings()
    token, _, _ = _encode(
        user_id,
        session_id=session_id,
        ttl_seconds=settings.access_ttl_min * 60,
        is_refresh=False,
    )
    return token


def create_refresh_token(user_id: int, session_id: UUID) -> tuple[str, str, int]:
    """返回 (token, jti, exp_ts)。jti/exp 供撤销/消费表登记使用。"""
    settings = get_settings()
    return _encode(
        user_id,
        session_id=session_id,
        ttl_seconds=settings.refresh_ttl_days * 86400,
        is_refresh=True,
    )


def decode_token(token: str, *, expect_refresh: bool | None = None) -> TokenPayload:
    """解析并校验 token。

    expect_refresh=None 时不做类型校验（仅解码）；True/False 时校验 typ 字段，
    防止用 access token 冒充 refresh token 换取新令牌。
    """
    settings = get_settings()
    try:
        data = jwt.decode(
            token, settings.jwt_secret.get_secret_value(), algorithms=[settings.jwt_algorithm]
        )
    except jwt.ExpiredSignatureError as e:
        raise TokenError("令牌已过期", expired=True) from e
    except jwt.PyJWTError as e:
        raise TokenError("令牌无效", expired=False) from e

    is_refresh = data.get("typ") == "refresh"
    if expect_refresh is not None and is_refresh != expect_refresh:
        raise TokenError("令牌类型不匹配", expired=False)

    try:
        return TokenPayload(
            user_id=int(data["sub"]),
            jti=str(data["jti"]),
            exp=int(data["exp"]),
            is_refresh=is_refresh,
            # 缺失或非法 sid 一律按"结构不完整"拒绝：**不能退化为"没有会话约束"**，
            # 否则删掉一个 claim 就能绕过注销检查（fail-closed）。
            session_id=UUID(str(data["sid"])),
        )
    except (KeyError, TypeError, ValueError) as e:
        raise TokenError("令牌结构不完整", expired=False) from e


# ---------------------------------------------------------------- 刷新令牌撤销/消费表


@dataclass(frozen=True, slots=True)
class RefreshTokenRecord:
    """已消费的刷新令牌记录。

    字段与 DATA-CONTRACTS §2 `refresh_token` 对齐：`jti` 充当 `token_hash` 位
    （不可逆的随机标识，**令牌原文不落库**）、`consumed_at` 一次性消费、
    `auth_session_id` 关联会话、`expires_at` 用于清理。
    本类型是为"换成 DB 仓储时字段可直接映射"而存在，不是对外契约。
    """

    jti: str
    auth_session_id: UUID
    expires_at: int
    consumed_at: int


class InMemoryRefreshRevocationStore:
    """进程内刷新令牌撤销/消费表。

    ★ 审计 PA-07：契约要求"注销不因重启失效"（ARCHITECTURE §3 第 6 条），
      进程内实现天然不满足该要求，因此：
        - `app_env` 非 "dev" 时**构造即失败**——宁可启动失败，也不要把
          "重启即遗忘撤销"的实现带上生产；
        - DB 持久仓储（`auth_session`/`refresh_token`，DATA-CONTRACTS §2）待 M1-6，
          届时本类应退化为测试替身。

    语义（换实现时必须保持）：

    - `consume`：同一 `jti` **只能成功一次**，重复调用返回 False（重放）；
      会话已撤销时一律拒绝。对应 F-01.02"一次性消费旧令牌"、AC-01.02-01
      "并发刷新仅一个成功，旧令牌不可重放"。
    - `revoke_session`：会话级撤销，对应 F-01.03"退出后该会话两类令牌失效"；
      幂等——重复撤销返回 False 但不报错。

    容量与"单位时间内刷新/注销次数"同阶，很小；过期项由 `purge_expired` 清理。
    """

    def __init__(self, *, app_env: str) -> None:
        if app_env != "dev":
            raise RuntimeError(
                "进程内刷新令牌撤销表仅限 dev（app_env='dev'）；"
                f"当前 app_env={app_env!r}。生产必须使用持久化仓储——"
                "进程内实现会使已注销令牌在重启后重新可用（审计 PA-07）。"
            )
        self._records: dict[str, RefreshTokenRecord] = {}
        self._revoked_sessions: dict[UUID, int] = {}
        self._lock = Lock()

    def consume(
        self, jti: str, *, auth_session_id: UUID, expires_at: int, now: int | None = None
    ) -> bool:
        """一次性消费刷新令牌。首次返回 True；重放或会话已撤销返回 False。"""
        ts = now if now is not None else int(time.time())
        with self._lock:
            if auth_session_id in self._revoked_sessions:
                return False
            if jti in self._records:
                return False
            self._records[jti] = RefreshTokenRecord(
                jti=jti, auth_session_id=auth_session_id, expires_at=expires_at, consumed_at=ts
            )
            return True

    def revoke_session(
        self, auth_session_id: UUID, *, expires_at: int, now: int | None = None
    ) -> bool:
        """撤销整个会话（注销）。已撤销过则返回 False（幂等，不报错）。"""
        _ = now  # 当前实现不记录撤销时间；DB 仓储需要 revoked_at 时启用
        with self._lock:
            if auth_session_id in self._revoked_sessions:
                return False
            self._revoked_sessions[auth_session_id] = expires_at
            return True

    def is_consumed(self, jti: str) -> bool:
        with self._lock:
            return jti in self._records

    def is_session_revoked(self, auth_session_id: UUID) -> bool:
        with self._lock:
            return auth_session_id in self._revoked_sessions

    def purge_expired(self, *, now: int | None = None) -> int:
        """清理已过期条目（过期后令牌本身已失效，无需再记）。"""
        ts = now if now is not None else int(time.time())
        with self._lock:
            stale = [j for j, r in self._records.items() if r.expires_at <= ts]
            for j in stale:
                self._records.pop(j, None)
            stale_sessions = [s for s, e in self._revoked_sessions.items() if e <= ts]
            for s in stale_sessions:
                self._revoked_sessions.pop(s, None)
        return len(stale) + len(stale_sessions)

    def __len__(self) -> int:
        with self._lock:
            return len(self._records) + len(self._revoked_sessions)


@lru_cache(maxsize=1)
def get_refresh_store() -> InMemoryRefreshRevocationStore:
    """按部署配置返回刷新令牌撤销表。

    ★ 非 dev 环境会直接抛错（见 `InMemoryRefreshRevocationStore`）——
    这是有意为之的 fail-fast，而不是缺省值。
    测试改配置时用 `get_refresh_store.cache_clear()`。
    """
    return InMemoryRefreshRevocationStore(app_env=get_settings().app_env)

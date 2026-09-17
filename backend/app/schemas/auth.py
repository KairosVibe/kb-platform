"""认证相关 DTO（M01，F-01.01—F-01.04）。

对应 FUNCTION-MAP.md §0 第 4 条（"路由仅 DTO 校验 → H01 当前身份 → H02 功能检查 →
服务 → 安全响应"）与 §2.2（"每个表单 DTO 字段与对应 F 签名一致；路径参数不在 body
重复定义；ctx、now、lease_token 及内部任务标识由受信上下文提供"）。

三条**故意为之**的设计，都是为了防止重复事实源：

1. **DTO 不校验密码长度**（只声明为 `SecretStr`）。长度策略唯一来源是
   `app/core/security.py` 的 `PASSWORD_MIN_BYTES` / `PASSWORD_MAX_BYTES`：
   登录必须允许**既有短密码**（创建时的 12 字节下限只约束创建/改密），
   若 DTO 也加 `min_length`，老账号会在登录前就被拦下。
   而且 DTO 校验失败统一映射为 `INVALID_ARGUMENT`，与契约要求的
   `PASSWORD_LENGTH_INVALID` 不是同一个码——两处校验必然导致响应码漂移。
2. **DTO 不含 `ip` / `ctx` / `now` / `lease_token`**：这些来自受信上下文
   （请求对象、H01 解析结果、服务端时钟），由客户端提交即可被伪造——
   登录限流按客户端自报 IP 统计等于没有限流。
3. **DTO 不做 `casefold` 归一化**：身份相等性只由 `models.identity.normalize_username`
   决定。DTO 只做 `strip`（去首尾空白），因为"首尾空格"属于输入格式问题，
   而"大小写是否等价"属于身份语义——两者混在一处会让登录与唯一索引的判断不一致。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from app.db.types import LEN_USERNAME

# ---------------------------------------------------------------- 请求


class LoginRequest(BaseModel):
    """POST /api/auth/login（F-01.01 的 DTO 侧）。

    F-01.01 签名是 `login(username, password, ip)`：**`ip` 不在本 DTO 中**，
    由路由从请求对象取（见模块 docstring 第 2 条）。
    """

    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=LEN_USERNAME)
    #: `SecretStr` 保证密码不会出现在 `repr`/日志里（FUNCTION-MAP §1 对 SecretStr 的要求）。
    password: SecretStr

    @field_validator("username")
    @classmethod
    def _strip_username(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("用户名不能为空")
        return trimmed


class RefreshRequest(BaseModel):
    """POST /api/auth/refresh（F-01.02：`rotate_refresh(refresh_token)`）。"""

    model_config = ConfigDict(extra="forbid")

    refresh_token: SecretStr


class LogoutRequest(BaseModel):
    """POST /api/auth/logout（F-01.03 的 DTO 侧）。

    F-01.03 签名是 `logout(ctx, refresh_token)`：`ctx` 由 H01 提供，**不在 body 中**。
    """

    model_config = ConfigDict(extra="forbid")

    refresh_token: SecretStr


# ---------------------------------------------------------------- 响应


class TokenPair(BaseModel):
    """F-01.01 / F-01.02 的输出：`{access_token, refresh_token, expires_in}`。"""

    model_config = ConfigDict(extra="forbid")

    access_token: str
    refresh_token: str
    #: access token 剩余有效期（秒）。客户端据此提前刷新，避免"刚好过期"的请求失败。
    expires_in: int = Field(ge=1)


class MeResponse(BaseModel):
    """F-01.04 的输出：当前身份最小视图。

    ★ 只有这六个字段。**不含 `password_hash`、不含权限快照之外的东西**——
      DATA-CONTRACTS §1 要求"公开 DTO 从白名单组装，数据库行不能直接序列化给前端"，
      所以这里是显式白名单，而不是 `model_config` 里开 `from_attributes` 直接把 ORM 行喂进来。
    """

    model_config = ConfigDict(extra="forbid")

    user_id: int = Field(gt=0)
    username: str
    dept_id: int | None = None
    role_ids: list[int] = Field(default_factory=list)
    permission_codes: list[str] = Field(default_factory=list)
    revision: int = Field(ge=1)


class LogoutResponse(BaseModel):
    """F-01.03 的输出：`{revoked}`。重复退出返回 `revoked=false` 而非报错（幂等）。"""

    model_config = ConfigDict(extra="forbid")

    revoked: bool

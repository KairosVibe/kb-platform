"""请求/响应 DTO 层（Pydantic）。

定位（FUNCTION-MAP §0 第 4 条）：路由做"DTO 校验 → H01 当前身份 → H02 功能检查 →
服务 → 安全响应"，本层只负责**形状与格式**校验，不承担业务规则。

两条纪律：

1. **不把 ORM 行直接喂进响应模型**（DATA-CONTRACTS §1："公开 DTO 从白名单组装，
   数据库行不能直接序列化给前端"）。因此响应模型一律 `extra="forbid"` 且字段显式列出，
   不开 `from_attributes` 的"整行映射"用法——那是 `password_hash` 泄漏的经典路径。
2. **请求模型 `extra="forbid"`**：客户端多传字段（如自报 `user_id`、`ctx`、`revision`）
   应当**明确报错**，而不是被静默忽略。静默忽略会让"这个字段生效了吗"永远说不清。
"""

from app.schemas.auth import (
    LoginRequest,
    LogoutRequest,
    LogoutResponse,
    MeResponse,
    RefreshRequest,
    TokenPair,
)

__all__ = [
    "LoginRequest",
    "LogoutRequest",
    "LogoutResponse",
    "MeResponse",
    "RefreshRequest",
    "TokenPair",
]

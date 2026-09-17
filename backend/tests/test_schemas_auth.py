"""认证 DTO 的形状与边界测试（**不需要数据库**）。

这些用例在守护三件"看起来只是风格、实际是安全或契约"的事：

1. **密码长度策略只有一个来源**：DTO 不校验长度，否则既有短密码无法登录，
   且错误码会漂移（DTO 校验失败统一是 `INVALID_ARGUMENT`，而契约要求
   `PASSWORD_LENGTH_INVALID`）。
2. **受信字段不得出现在 body**：`ip` / `ctx` 由服务端提供；客户端能提交就能伪造，
   登录限流按自报 IP 统计等于没有限流。
3. **响应字段逐一对齐 F 签名**：多一个字段（如 `password_hash`）就是泄漏，
   少一个字段就是前端拿不到数据；两者都靠显式白名单 + `extra="forbid"` 拦住。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.security import PASSWORD_MAX_BYTES, PASSWORD_MIN_BYTES
from app.schemas.auth import (
    LoginRequest,
    LogoutRequest,
    LogoutResponse,
    MeResponse,
    RefreshRequest,
    TokenPair,
)

# ---------------------------------------------------------------- 用户名


def test_username_is_stripped() -> None:
    assert LoginRequest(username="  admin  ", password="whatever").username == "admin"


def test_username_whitespace_only_rejected() -> None:
    with pytest.raises(ValidationError):
        LoginRequest(username="   ", password="whatever")


def test_username_too_long_rejected() -> None:
    with pytest.raises(ValidationError):
        LoginRequest(username="a" * 65, password="whatever")


def test_username_is_not_casefolded_in_dto() -> None:
    """DTO 不改大小写：身份等价性只由 `normalize_username` 决定（避免两个事实源）。"""
    assert LoginRequest(username="Admin", password="whatever").username == "Admin"


# ---------------------------------------------------------------- 密码策略边界


def test_dto_does_not_enforce_min_password_length() -> None:
    """★ DTO **不得**强制最小长度：创建时的 12 字节下限不约束登录。

    若 DTO 加 `min_length=12`，所有既有短密码账号将永远无法登录——改动密码策略
    不能追溯地废掉已存在的凭据。
    """
    assert PASSWORD_MIN_BYTES == 12
    request = LoginRequest(username="admin", password="short")
    assert request.password.get_secret_value() == "short"


def test_dto_does_not_truncate_over_long_password() -> None:
    """★ DTO **不得**截断超长密码，必须原样传递。

    截断会让"前 72 字节相同的两个不同密码"互相通过（审计 PA-09）；
    真正的拒绝发生在 `core/security.py`，由服务层映射为
    `PASSWORD_LENGTH_INVALID`（422）。
    """
    raw = "p" * (PASSWORD_MAX_BYTES + 10)
    request = LoginRequest(username="admin", password=raw)
    assert len(request.password.get_secret_value().encode("utf-8")) == PASSWORD_MAX_BYTES + 10


def test_dto_does_not_strip_password() -> None:
    """密码不 trim：首尾空格是密码的一部分（F-01.01 边界："密码不trim不截断"）。"""
    request = LoginRequest(username="admin", password="  spaced  ")
    assert request.password.get_secret_value() == "  spaced  "


def test_password_never_in_repr_or_str() -> None:
    """`SecretStr` 的用途：密码不得出现在 repr/log 中。"""
    request = LoginRequest(username="admin", password="super-secret-value")
    assert "super-secret-value" not in repr(request)
    assert "super-secret-value" not in str(request)
    assert "super-secret-value" not in request.model_dump_json()


# ---------------------------------------------------------------- 受信字段不得来自 body


@pytest.mark.parametrize("field", ["ip", "ctx", "now", "lease_token", "user_id", "revision"])
def test_trusted_fields_rejected_in_login_body(field: str) -> None:
    """`extra="forbid"`：客户端多传受信字段必须**明确报错**，不被静默忽略。"""
    with pytest.raises(ValidationError):
        LoginRequest.model_validate({"username": "admin", "password": "x", field: "forged"})


def test_logout_body_has_no_ctx() -> None:
    """F-01.03 的 `ctx` 由 H01 解析，不在 body 中。"""
    assert "ctx" not in LogoutRequest.model_fields
    with pytest.raises(ValidationError):
        LogoutRequest.model_validate({"refresh_token": "t", "ctx": {"user_id": 1}})


# ---------------------------------------------------------------- 响应字段对齐 F 签名

def test_login_output_fields_match_contract() -> None:
    """F-01.01 输出必须恰为 `{access_token, refresh_token, expires_in}`。"""
    assert set(TokenPair.model_fields) == {"access_token", "refresh_token", "expires_in"}


def test_me_response_matches_contract_and_has_no_secrets() -> None:
    """F-01.04 输出六字段；且**不含任何口令字段**（白名单组装）。"""
    assert set(MeResponse.model_fields) == {
        "user_id",
        "username",
        "dept_id",
        "role_ids",
        "permission_codes",
        "revision",
    }
    assert not {
        f for f in MeResponse.model_fields if "password" in f or "secret" in f or "token" in f
    }


def test_me_response_rejects_unexpected_field() -> None:
    """多一个字段（例如顺手带上 `password_hash`）必须被拒绝。"""
    with pytest.raises(ValidationError):
        MeResponse.model_validate(
            {
                "user_id": 1,
                "username": "admin",
                "dept_id": None,
                "role_ids": [3],
                "permission_codes": ["ai:ask"],
                "revision": 1,
                "password_hash": "$2b$12$leak",
            }
        )


def test_me_response_dept_optional_and_revision_positive() -> None:
    me = MeResponse.model_validate(
        {
            "user_id": 7,
            "username": "u",
            "dept_id": None,
            "role_ids": [],
            "permission_codes": [],
            "revision": 1,
        }
    )
    assert me.dept_id is None
    with pytest.raises(ValidationError):
        MeResponse.model_validate(
            {
                "user_id": 7,
                "username": "u",
                "dept_id": None,
                "role_ids": [],
                "permission_codes": [],
                "revision": 0,
            }
        )


def test_token_pair_expires_in_positive() -> None:
    with pytest.raises(ValidationError):
        TokenPair(access_token="a", refresh_token="r", expires_in=0)


def test_logout_response_is_idempotent_shape() -> None:
    """重复退出返回 `revoked=false`（幂等），而不是报错。"""
    assert LogoutResponse(revoked=False).revoked is False


def test_refresh_request_requires_token() -> None:
    with pytest.raises(ValidationError):
        RefreshRequest.model_validate({})

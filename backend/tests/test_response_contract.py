"""统一响应包与错误码契约测试（审计步骤 5：PA-11 / PA-12）。

对应 FUNCTION-MAP.md §2（普通响应 {code,message,data,request_id}；错误码分两层）、
API-CONTRACTS.md §1（401/403/404/409/410/413/415/422/429/503）、PRD.md §1（错误码约定）、
PRD.md AC-02.06-02 / AC-02.07-02。

覆盖的两个历史缺陷：

- PA-11：`UNSUPPORTED_FORMAT` 用 400（应 415）、`INVALID_PERM_CODE` 用 400（应 422）、
  `SELF_LOCK` 用 400（应 409），且缺 410/415/422/503 与 `SESSION_BUSY`/`REINDEX_REQUIRED`；
  另外解析类错误码被放在 HTTP 表里以 200 返回，造成"HTTP 层与任务层混用"。
- PA-12：响应体缺 `request_id`，异常处理器同样不带。
"""

from __future__ import annotations

import re

import pytest

from app.core.logging import ensure_request_id, request_id_var
from app.core.response import (
    ERROR_CODES,
    TASK_ERROR_CODES,
    BizError,
    error_body,
    ok,
)

# 契约声明的 HTTP 状态码全集（API-CONTRACTS §1 与 PRD §1）
CONTRACT_STATUSES = frozenset({401, 403, 404, 409, 410, 413, 415, 422, 429, 503})
ALLOWED_STATUSES = CONTRACT_STATUSES | {500}
_HEX32 = re.compile(r"^[0-9a-f]{32}$")


@pytest.fixture(autouse=True)
def _reset_request_id() -> None:
    """每个用例从"未设置"开始，保证 request_id 生成逻辑可被断言。"""
    request_id_var.set("-")


# ---------------------------------------------------------------- PA-12 响应包


def test_ok_response_carries_request_id():
    """★ PA-12 核心断言：成功响应必须带链路追踪 ID。"""
    body = ok({"foo": "bar"})
    assert set(body) == {"code", "message", "data", "request_id"}
    assert body["code"] == "OK"
    assert body["data"] == {"foo": "bar"}
    assert _HEX32.match(body["request_id"])


def test_error_body_carries_request_id():
    """失败响应同样必须带 request_id（含未知码降级路径）。"""
    for body in (
        error_body("NOT_FOUND"),
        error_body("NOT_FOUND", "自定义文案"),
        error_body("PARSE_CORRUPTED"),      # 未知码 → 降级
    ):
        assert set(body) == {"code", "message", "data", "request_id"}
        assert _HEX32.match(body["request_id"])


def test_request_id_is_stable_within_one_request_scope():
    """同一请求范围内多次构造响应必须复用同一个 ID（否则无法串联日志与响应）。"""
    first = ok()["request_id"]
    assert error_body("NOT_FOUND")["request_id"] == first
    assert ensure_request_id() == first


def test_request_id_regenerated_after_scope_reset():
    """新的请求范围（上下文被重置）应产生新的 ID，而不是复用上一条。"""
    first = ok()["request_id"]
    request_id_var.set("-")
    assert ok()["request_id"] != first


def test_ok_allows_custom_message_but_code_stays_ok():
    body = ok([1, 2], message="已完成")
    assert body["code"] == "OK"
    assert body["message"] == "已完成"


# ---------------------------------------------------------------- PA-11 状态码


def test_every_http_error_code_uses_an_allowed_status():
    """HTTP 层不得出现契约之外的状态码，尤其**不得出现 200**。

    200 表明该码其实是任务层错误码——两层混用会让"任务失败"被当成"请求成功"。
    """
    for code, (http_status, _) in ERROR_CODES.items():
        assert http_status in ALLOWED_STATUSES, f"{code} 使用了非契约状态码 {http_status}"


def test_contract_statuses_are_all_covered():
    """★ PA-11 核心断言：契约声明的每类状态码都必须真实存在，不能只写在文档里。"""
    used = {http_status for http_status, _ in ERROR_CODES.values()}
    missing = CONTRACT_STATUSES - used
    assert not missing, f"契约声明但未实现的状态码: {sorted(missing)}"


@pytest.mark.parametrize(
    ("code", "expected_status"),
    [
        # 415 格式不支持（原 400）
        ("UNSUPPORTED_FORMAT", 415),
        # 422 参数（原 400）
        ("INVALID_ARGUMENT", 422),
        ("INVALID_PERM_CODE", 422),
        ("PASSWORD_LENGTH_INVALID", 422),
        # 409 状态/版本冲突（原 400）
        ("SELF_LOCK", 409),
        ("SESSION_BUSY", 409),
        ("REINDEX_REQUIRED", 409),
        ("REVISION_CONFLICT", 409),
        # 410 事件游标过期（原缺失）
        ("EVENT_CURSOR_EXPIRED", 410),
        # 503 依赖不可用（原缺失）
        ("DEPENDENCY_UNAVAILABLE", 503),
        # 既有正确项，防止回归
        ("FILE_TOO_LARGE", 413),
        ("RATE_LIMITED", 429),
        ("NOT_FOUND", 404),
        ("PERM_DENIED", 403),
        ("BAD_CREDENTIALS", 401),
    ],
)
def test_status_mappings_match_contract(code, expected_status):
    assert ERROR_CODES[code][0] == expected_status


def test_session_busy_and_reindex_required_exist():
    """API-CONTRACTS §4/§6 点名的两个具名错误码必须存在（原缺失）。"""
    assert "SESSION_BUSY" in ERROR_CODES
    assert "REINDEX_REQUIRED" in ERROR_CODES


def test_all_messages_are_non_empty_and_user_readable():
    for code, (_, message) in ERROR_CODES.items():
        assert message.strip(), f"{code} 缺少用户可读文案"
        assert "Traceback" not in message and "Exception" not in message


# ---------------------------------------------------------------- 两层错误码


def test_task_and_http_layers_are_disjoint():
    """★ 两层键集不得重叠：同名会让人分不清"请求失败"还是"任务失败"。"""
    assert not (set(ERROR_CODES) & set(TASK_ERROR_CODES))


def test_task_layer_codes_are_not_http_errors():
    """任务层码不得出现在 HTTP 表里（原实现把它们以 200 放在同一张表）。"""
    assert not (set(TASK_ERROR_CODES) & set(ERROR_CODES))
    for code in TASK_ERROR_CODES:
        assert not code.startswith(("PERM_", "TOKEN_"))


def test_biz_error_accepts_http_layer_code():
    exc = BizError("INVALID_PERM_CODE")
    assert exc.http_status == 422
    assert exc.message == ERROR_CODES["INVALID_PERM_CODE"][1]


def test_biz_error_accepts_message_override():
    exc = BizError("DEPT_IN_USE", "该部门下存在 3 个用户")
    assert exc.message == "该部门下存在 3 个用户"
    assert exc.http_status == 409


def test_biz_error_rejects_task_layer_code():
    """★ 关键断言：把任务层码抛成 HTTP 错误必须在构造时立刻失败。"""
    for code in TASK_ERROR_CODES:
        with pytest.raises(ValueError, match="任务层错误码"):
            BizError(code)


def test_biz_error_rejects_unknown_code():
    with pytest.raises(ValueError, match="未知的 HTTP 错误码"):
        BizError("NO_SUCH_CODE")


def test_error_body_downgrades_unknown_code_to_internal_error():
    """未知码不得原样透出（可能泄漏内部码），统一降级为 500。"""
    body = error_body("PARSE_CORRUPTED", "内部细节不应外泄")
    assert body["code"] == "INTERNAL_ERROR"
    assert body["message"] == ERROR_CODES["INTERNAL_ERROR"][1]
    assert body["data"] is None


def test_error_body_keeps_contract_message_by_default():
    body = error_body("UNSUPPORTED_FORMAT")
    assert body["message"] == ERROR_CODES["UNSUPPORTED_FORMAT"][1]

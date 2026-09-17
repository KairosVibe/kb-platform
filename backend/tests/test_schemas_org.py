"""M02 DTO 单元测试（不连数据库）。

验的是**契约边界**而不是"能不能构造"：长度上限、去重语义、`extra="forbid"`，
以及三条最容易在重构中被抹掉的约定：

1. **密码长度不在这里校验**（`UserCreateRequest.password` 只声明 `SecretStr`）。
   若有人顺手加上 `min_length=12`，错误码会从契约要求的 `PASSWORD_LENGTH_INVALID`
   漂成 `INVALID_ARGUMENT`——这两条断言会立刻失败。
2. **非法权限码不被静默丢弃**：DTO 只去重，合法性留给 `org_svc`（要能给出 422）。
3. **`ctx` / `actor_id` 不出现在任何请求 DTO 里**：客户端能提交就能伪造。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.db.types import LEN_NAME, LEN_USERNAME
from app.schemas.org import (
    DepartmentCreateRequest,
    DepartmentUpdateRequest,
    PermissionCodeOut,
    RoleSaveRequest,
    UserCreateRequest,
    UserUpdateRequest,
)


class TestUserCreateRequest:
    def test_accepts_minimal_payload(self) -> None:
        req = UserCreateRequest(username="alice", password="a-very-long-secret")
        assert req.username == "alice"
        assert req.dept_id is None
        assert req.role_ids == []

    def test_strips_username_but_keeps_case(self) -> None:
        """只 strip、不 casefold：大小写等价性由 `normalize_username` 决定（唯一来源）。"""
        req = UserCreateRequest(username="  Alice  ", password="a-very-long-secret")
        assert req.username == "Alice"

    def test_rejects_blank_username(self) -> None:
        with pytest.raises(ValidationError):
            UserCreateRequest(username="   ", password="a-very-long-secret")

    def test_does_not_enforce_min_password_length(self) -> None:
        """★ 短密码必须能通过 DTO——否则老账号（创建策略变更前）连创建都被挡在 DTO 层。

        真正的策略在 `core/security.hash_password`，错误码是 `PASSWORD_LENGTH_INVALID`。
        """
        req = UserCreateRequest(username="alice", password="short")
        assert req.password.get_secret_value() == "short"

    def test_does_not_truncate_over_long_password(self) -> None:
        """超过 72 字节也必须原样传到服务层，由 `verify/hash` 显式拒绝而不是静默截断。"""
        raw = "x" * 200
        req = UserCreateRequest(username="alice", password=raw)
        assert req.password.get_secret_value() == raw

    def test_dedupes_role_ids_preserving_order(self) -> None:
        req = UserCreateRequest(username="alice", password="a-very-long-secret", role_ids=[3, 1, 3, 2, 1])
        assert req.role_ids == [3, 1, 2]

    def test_rejects_unknown_field(self) -> None:
        """`extra="forbid"`：多传字段必须报错，不能静默忽略。"""
        with pytest.raises(ValidationError):
            UserCreateRequest(username="alice", password="a-very-long-secret", enabled=False)

    def test_rejects_trusted_context_fields(self) -> None:
        """`ctx` / `actor_id` / `revision` 一律不可由客户端提交。"""
        for field in ("ctx", "actor_id", "revision", "user_id"):
            with pytest.raises(ValidationError):
                UserCreateRequest.model_validate(
                    {"username": "alice", "password": "a-very-long-secret", field: 1}
                )

    def test_rejects_over_long_username(self) -> None:
        with pytest.raises(ValidationError):
            UserCreateRequest(username="u" * (LEN_USERNAME + 1), password="a-very-long-secret")


class TestUserUpdateRequest:
    def test_requires_enabled_explicitly(self) -> None:
        """`enabled` 必填：省略它不应被解释成"保持不变"。"""
        with pytest.raises(ValidationError):
            UserUpdateRequest.model_validate({"dept_id": None, "role_ids": [], "expected_revision": 1})

    def test_requires_expected_revision(self) -> None:
        with pytest.raises(ValidationError):
            UserUpdateRequest.model_validate({"dept_id": None, "role_ids": [], "enabled": True})

    def test_accepts_full_payload(self) -> None:
        req = UserUpdateRequest(dept_id=2, role_ids=[1, 1, 2], enabled=False, expected_revision=3)
        assert req.role_ids == [1, 2]
        assert req.enabled is False


class TestDepartmentRequests:
    def test_create_allows_root(self) -> None:
        assert DepartmentCreateRequest(parent_id=None, name="总部").parent_id is None

    def test_create_rejects_non_positive_parent(self) -> None:
        with pytest.raises(ValidationError):
            DepartmentCreateRequest(parent_id=0, name="总部")

    def test_name_length_boundary(self) -> None:
        DepartmentCreateRequest(parent_id=None, name="n" * LEN_NAME)
        with pytest.raises(ValidationError):
            DepartmentCreateRequest(parent_id=None, name="n" * (LEN_NAME + 1))

    def test_update_requires_revision(self) -> None:
        with pytest.raises(ValidationError):
            DepartmentUpdateRequest.model_validate({"parent_id": None, "name": "x"})


class TestRoleSaveRequest:
    def test_dedupes_codes_and_keeps_unknown_ones(self) -> None:
        """★ 未注册的码**必须在服务层报错**，因此 DTO 不能把它过滤掉。"""
        req = RoleSaveRequest(name="知识管理员", codes=["kb:view", "kb:view", "nope:code"])
        assert req.codes == ["kb:view", "nope:code"]

    def test_new_role_has_null_revision(self) -> None:
        req = RoleSaveRequest(name="新角色", codes=[])
        assert req.id is None
        assert req.expected_revision is None

    def test_rejects_blank_name(self) -> None:
        with pytest.raises(ValidationError):
            RoleSaveRequest(name="  ", codes=[])


class TestPermissionCodeOut:
    def test_shape_matches_frontend_tree_contract(self) -> None:
        item = PermissionCodeOut(code="kb:view", label="知识查看", module="知识")
        assert set(item.model_dump()) == {"code", "label", "module"}

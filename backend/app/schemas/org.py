"""组织、用户与功能权限 DTO（M02，F-02.01—F-02.09 + API-S01/S02/S03）。

对应 FUNCTION-MAP.md §0 第 4 条与 §2.2：**路径参数不在 body 重复定义；`ctx` 由 H01 提供**。
本模块因此只出现"客户端确实可以决定"的字段，`ctx` / `now` / `actor_id` 一律不出现。

三条刻意保留的设计：

1. **响应 DTO 是显式白名单，不 `from_attributes` 直接喂 ORM 行**（同 `schemas/auth.py` 第 1 条）。
   `User.password_hash` 泄露的经典路径就是"把行序列化出去"，这里从类型上切断。
2. **`codes` 去重但不静默丢弃非法码**：API-CONTRACTS §1 要求"source_ids/角色/ACL ID 数组去重后
   最多 1000 项，**非法 ID 不静默丢弃**"。所以 DTO 只做去重与上限，合法性由
   `org_svc` 用 `core/permissions.is_valid_code` 判定并抛 `INVALID_PERM_CODE`（422）。
3. **`name` / `username` 只 `strip`，不做大小写归一化**：唯一性语义唯一来源是
   `models.identity.normalize_username`（同 `schemas/auth.py` 第 3 条）。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from app.db.types import LEN_NAME, LEN_USERNAME

#: 数组类字段的统一上限（API-CONTRACTS §1：去重后最多 1000 项）。
MAX_ID_LIST = 1000


def _unique_preserving_order(values: list[int]) -> list[int]:
    """去重且保持输入顺序。不使用 `set` 因为集合迭代顺序不稳定，会让返回值难以复现。"""
    seen: set[int] = set()
    out: list[int] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


# ---------------------------------------------------------------- 部门


class DepartmentCreateRequest(BaseModel):
    """F-02.02 POST /api/departments。`parent_id=None` 表示根部门。"""

    model_config = ConfigDict(extra="forbid")

    parent_id: int | None = Field(default=None, gt=0)
    name: str = Field(min_length=1, max_length=LEN_NAME)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("部门名称不能为空")
        return trimmed


class DepartmentUpdateRequest(BaseModel):
    """F-02.03 PATCH /api/departments/{id}。同时承担"改名"与"移动"。"""

    model_config = ConfigDict(extra="forbid")

    parent_id: int | None = Field(default=None, gt=0)
    name: str = Field(min_length=1, max_length=LEN_NAME)
    expected_revision: int = Field(ge=1)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("部门名称不能为空")
        return trimmed


class DepartmentDeleteRequest(BaseModel):
    """F-02.04 DELETE /api/departments/{id}。

    契约允许 `If-Match: "<revision>"` 或 body revision 二选一，首版统一 body
    （FUNCTION-MAP §2.2），故这里只接受 body 一种写法，**不同时接受冲突值**。
    """

    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)


class DepartmentOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int = Field(gt=0)
    parent_id: int | None = None
    name: str
    revision: int = Field(ge=1)


class DepartmentListOut(BaseModel):
    """F-02.01 的输出。部门树**不分页**（API-CONTRACTS §1 的明确例外）。"""

    model_config = ConfigDict(extra="forbid")

    items: list[DepartmentOut] = Field(default_factory=list)


class DepartmentDeletedOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deleted: bool


# ---------------------------------------------------------------- 用户


class UserCreateRequest(BaseModel):
    """F-02.05 POST /api/users。

    ★ **密码长度不在这里校验**：策略唯一来源是 `core/security.py`
      （`PASSWORD_MIN_BYTES`/`PASSWORD_MAX_BYTES`），由创建路径调用
      `hash_password` 时抛 `PASSWORD_LENGTH_INVALID`。若 DTO 也加 `min_length`，
      错误码会漂成 `INVALID_ARGUMENT`（同 `schemas/auth.py` 第 1 条的理由）。
    """

    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=LEN_USERNAME)
    password: SecretStr
    dept_id: int | None = Field(default=None, gt=0)
    role_ids: list[int] = Field(default_factory=list, max_length=MAX_ID_LIST)

    @field_validator("username")
    @classmethod
    def _strip_username(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("用户名不能为空")
        return trimmed

    @field_validator("role_ids")
    @classmethod
    def _dedupe_roles(cls, value: list[int]) -> list[int]:
        return _unique_preserving_order(value)


class UserUpdateRequest(BaseModel):
    """F-02.06 PATCH /api/users/{id}。承担"改部门/改角色/启停"三件事。

    `enabled` 必填而非可空：停用是本模块最敏感的一步（停用后对所有人不可检索），
    省略它不应该被解释成"保持不变"从而让人以为"只改了部门"。
    """

    model_config = ConfigDict(extra="forbid")

    dept_id: int | None = Field(default=None, gt=0)
    role_ids: list[int] = Field(default_factory=list, max_length=MAX_ID_LIST)
    enabled: bool
    expected_revision: int = Field(ge=1)

    @field_validator("role_ids")
    @classmethod
    def _dedupe_roles(cls, value: list[int]) -> list[int]:
        return _unique_preserving_order(value)


class UserOut(BaseModel):
    """API-S01 的输出项。**不含 `password_hash`**。"""

    model_config = ConfigDict(extra="forbid")

    id: int = Field(gt=0)
    username: str
    dept_id: int | None = None
    role_ids: list[int] = Field(default_factory=list)
    enabled: bool
    revision: int = Field(ge=1)


class UserCreatedOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int = Field(gt=0)
    username: str
    revision: int = Field(ge=1)


class UserUpdatedOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int = Field(gt=0)
    enabled: bool
    revision: int = Field(ge=1)


# ---------------------------------------------------------------- 角色


class RoleSaveRequest(BaseModel):
    """F-02.07 PUT /api/roles。`id=None` 表示新建。"""

    model_config = ConfigDict(extra="forbid")

    id: int | None = Field(default=None, gt=0)
    name: str = Field(min_length=1, max_length=LEN_NAME)
    codes: list[str] = Field(default_factory=list, max_length=MAX_ID_LIST)
    #: 新建时为 None；修改时必填。契约要求两者同时出现时报 422，由服务层判定后抛
    #: `INVALID_ARGUMENT`（DTO 无法表达"仅当 id 非空时必填"而不引入重复规则）。
    expected_revision: int | None = Field(default=None, ge=1)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("角色名称不能为空")
        return trimmed

    @field_validator("codes")
    @classmethod
    def _dedupe_codes(cls, value: list[str]) -> list[str]:
        """去重但**不丢弃非法码**：合法性由 `org_svc` 判定并抛 `INVALID_PERM_CODE`。"""
        seen: set[str] = set()
        out: list[str] = []
        for code in value:
            if code in seen:
                continue
            seen.add(code)
            out.append(code)
        return out


class RoleDeleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)


class RoleOut(BaseModel):
    """API-S02 的输出项。"""

    model_config = ConfigDict(extra="forbid")

    id: int = Field(gt=0)
    name: str
    codes: list[str] = Field(default_factory=list)
    revision: int = Field(ge=1)


class RoleSavedOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int = Field(gt=0)
    codes: list[str] = Field(default_factory=list)
    revision: int = Field(ge=1)


class RoleDeletedOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deleted: bool


class PermissionCodeOut(BaseModel):
    """API-S03 的输出项。

    `module` 取自 `core.permissions.PermGroup`，与前端"按 module 组树"的用法一致；
    上游文档未枚举取值，因此这里**不硬编码映射表**，直接透出枚举值。
    """

    model_config = ConfigDict(extra="forbid")

    code: str
    label: str
    module: str


class PermissionCodeListOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[PermissionCodeOut] = Field(default_factory=list)


# ---------------------------------------------------------------- 目录选择器


class DirectoryItemOut(BaseModel):
    """F-02.09 GET /api/directory 的输出项。

    `enabled` 可空：部门/角色没有启停概念（部门靠删除、角色靠撤引用），
    只有用户有 `enabled`。用 `None` 表达"该 kind 不适用"，而不是伪造 `True`。
    """

    model_config = ConfigDict(extra="forbid")

    id: int = Field(gt=0)
    label: str
    enabled: bool | None = None


class DirectoryListOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[DirectoryItemOut] = Field(default_factory=list)
    total: int = Field(ge=0)

"""身份与会话实体。

对应 DATA-CONTRACTS.md §2 实体字典中的 department / user / role / role_permission /
user_role / auth_session / refresh_token，以及 FUNCTION-MAP.md §2.1 第 1 条。

本模块是**安全事实源**（REUSE-MATRIX R11：原工程信任客户端 `X-Role` 请求头，
必须整体替换）。三条不可放宽的约定：

1. `user.password_hash` **永不进入 DTO**（DATA-CONTRACTS §1："公开DTO从白名单组装，
   数据库行不能直接序列化给前端"）。
2. `username_norm` 使用 `utf8mb4_bin` 唯一索引。理由：默认排序规则大小写不敏感，
   会把 `Admin` 与 `admin` 判为同一身份；**安全身份的唯一性不能由排序规则决定**。
3. `auth_session` 与 `chat_session` 是两件事（DATA-CONTRACTS §2 明确"不是auth_session"）：
   前者是登录会话（注销按它撤销令牌），后者是聊天会话（侧栏列表）。混用会导致
   "退出登录后侧栏历史消失"或"换设备登录后旧设备仍可刷新"。
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, Boolean, ForeignKey, Index, String, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, PKMixin, RevisionMixin, TimestampMixin
from app.db.types import (
    BINARY_COLLATION,
    LEN_CODE,
    LEN_HASH,
    LEN_NAME,
    LEN_TOKEN_HASH,
    LEN_USERNAME,
    UTCDateTime,
)


def normalize_username(raw: str) -> str:
    """用户名规范化。**入库前与查询时必须调用同一个函数**。

    `strip` + `casefold`：避免"末尾多一个空格"或"首字母大写"绕过唯一约束，
    从而产生两个看起来相同的账号。用 `casefold` 而非 `lower`，因为 `lower` 对
    部分 Unicode 字符不彻底（如德语 ß）。
    """
    return raw.strip().casefold()


class Department(PKMixin, TimestampMixin, RevisionMixin, Base):
    """部门（树形）。四维权限中的"直属部门精确匹配"基于本表。

    ★ `parent_id` 表达**组织结构**，不是权限继承：判定时不向上递归
      （DESIGN_REVISION §2.1"部门仅直属部门精确匹配，不隐式继承祖先或子孙"）。
      环检测由服务层负责——MySQL 无原生环约束。
    """

    __tablename__ = "department"

    parent_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("department.id", ondelete="RESTRICT"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(LEN_NAME), nullable=False)


class User(PKMixin, TimestampMixin, RevisionMixin, Base):
    """账号。停用不删历史归属（DATA-CONTRACTS §2）。"""

    __tablename__ = "user"

    username: Mapped[str] = mapped_column(String(LEN_USERNAME), nullable=False)
    #: 规范化登录名；唯一性判定**只认这一列**，且用二进制排序规则（模块 docstring 第 2 条）。
    username_norm: Mapped[str] = mapped_column(
        String(LEN_USERNAME, collation=BINARY_COLLATION), nullable=False, unique=True
    )
    password_hash: Mapped[str] = mapped_column(String(LEN_HASH), nullable=False)
    dept_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("department.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: 身份修订号：部门/角色/权限码/启停变化时递增，用于让既有会话的实时身份失效。
    identity_revision: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=1, server_default=text("1")
    )


class Role(PKMixin, TimestampMixin, RevisionMixin, Base):
    """角色。被用户或 ACL 引用时拒绝删除（DATA-CONTRACTS §2）。"""

    __tablename__ = "role"

    name: Mapped[str] = mapped_column(String(LEN_NAME), nullable=False)
    #: 角色名的唯一性同样不能交给默认排序规则（与 username_norm 同理）。
    name_norm: Mapped[str] = mapped_column(
        String(LEN_NAME, collation=BINARY_COLLATION), nullable=False, unique=True
    )


class RolePermission(Base):
    """角色→权限码（关联表，故无公共时间列，DATA-CONTRACTS §1）。

    `code` 必须是 `app.core.permissions.PERMISSIONS` 的 14 项白名单成员。数据库层
    **不加 CHECK 约束**：白名单会随契约演进（例如新增权限码），把集合固化进 DDL 会让
    每次加码都变成一次迁移；由服务层与种子数据保证一致性，这正是
    `core/permissions.py`"唯一来源、由列表派生"的用途。
    """

    __tablename__ = "role_permission"

    role_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("role.id", ondelete="RESTRICT"), primary_key=True
    )
    code: Mapped[str] = mapped_column(String(LEN_CODE), primary_key=True)


class UserRole(Base):
    """用户→角色（关联表）。双 FK，联合主键。"""

    __tablename__ = "user_role"

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("user.id", ondelete="CASCADE"), primary_key=True
    )
    role_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("role.id", ondelete="RESTRICT"), primary_key=True
    )


class AuthSession(TimestampMixin, Base):
    """登录会话：稳定标识，与聊天会话分离。

    ★ 主键用 UUID 而非自增：`auth_session_id` 会进入令牌与审计记录，自增 ID 可被枚举
      （"猜下一个会话号"），也容易与 `chat_session.id` 混用出权限错误。
    ★ UUID 由**应用生成**（`uuid4`）而不是数据库：避免依赖具体库的 `UUID()` 函数，
      也让对象在 flush 之前就拥有可用标识（审计需要）。
    """

    __tablename__ = "auth_session"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("user.id", ondelete="CASCADE"), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    #: 撤销时间（注销）。NULL 表示仍有效——**这一列落库，"注销不因重启失效"才成立**（PA-07）。
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    __table_args__ = (
        # 刷新/校验路径按 (user_id, revoked_at) 取会话；注销时也要按 user_id 批量撤销。
        Index("ix_auth_session_user_revoked", "user_id", "revoked_at"),
    )


class RefreshToken(TimestampMixin, Base):
    """刷新令牌的持久消费/撤销登记。**令牌原文不落库**（DATA-CONTRACTS §2）。

    语义必须与 `app/core/security.py` 的进程内实现**逐条一致**（替换实现时不得放宽）：

    - `token_hash` 唯一 → 同一令牌只能成功消费一次（重放被唯一键挡住，而不是靠应用判断）；
    - `consumed_at`：一次性消费时间；
    - `revoked_at`：会话级撤销（注销）；
    - `replaced_by`：轮换链，便于排查"令牌被重复使用"的时序。
    """

    __tablename__ = "refresh_token"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    auth_session_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("auth_session.id", ondelete="CASCADE"), nullable=False
    )
    #: 令牌的不可逆标识（jti 的 SHA-256）；**不是令牌原文**。
    token_hash: Mapped[str] = mapped_column(
        String(LEN_TOKEN_HASH), nullable=False, unique=True
    )
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    replaced_by: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)

    __table_args__ = (
        # 按会话查其全部令牌（注销/轮换链审计）。
        Index("ix_refresh_token_session", "auth_session_id"),
    )


__all__ = [
    "AuthSession",
    "Department",
    "RefreshToken",
    "Role",
    "RolePermission",
    "User",
    "UserRole",
    "normalize_username",
]

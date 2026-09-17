"""SQLAlchemy 声明式基类、命名约定与公共列。

对应 FUNCTION-MAP.md §2.1 第 10 条（持久层实现约定）、DATA-CONTRACTS.md §1（公共规则）。

三条硬约定（改这里等于改全部表，必须与文档同步）：

1. **命名约定统一由 `MetaData` 生成**（`pk_`/`fk_`/`ix_`/`uq_`/`ck_`）。理由：Alembic 的
   downgrade 要按名字删约束，用 MySQL 默认名（`tbl_ibfk_1` 这类）会让迁移不可逆，
   且不同环境名字不一致。
2. **时间由应用以 UTC 写入**，DDL 层不加 `CURRENT_TIMESTAMP` 默认值。理由：`DATETIME`
   不带时区，若由数据库生成就把"服务器时区"混进了业务时间；让时区语义只在一处定义
   （`utcnow`），单元测试与集成测试行为一致。代价：绕过应用直接 INSERT 需自带时间。
3. **主键 BIGINT 自增**，对外不超过 JS 安全整数（DATA-CONTRACTS §1）。

字符集在**建库时**设定为 utf8mb4（见 `backend/seeds/README.md`），不在每张表上重复声明——
避免子类定义元组形式的 `__table_args__` 时静默丢掉 dialect 参数。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import BigInteger, MetaData, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.db.types import UTCDateTime

# 与 DATA-CONTRACTS §5「生成工具链」一致：由 metadata 命名约定生成，不用数据库默认名。
NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def utcnow() -> datetime:
    """朴素 UTC 时间。

    `DATETIME(6)` 无时区信息，全库统一存 UTC，避免"同一列里混着两种时区"。
    返回 naive 值是有意为之：带 tzinfo 的对象写入 MySQL 会被静默降级，
    不如在类型层面就统一。
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    """全部 ORM 实体的基类（DATA-CONTRACTS §2 实体字典的一一对应）。"""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class PKMixin:
    """BIGINT 自增主键。关联表不用它——关联表用联合主键（DATA-CONTRACTS §2）。"""

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)


class TimestampMixin:
    """`created_at`/`updated_at` DATETIME(6) UTC。

    DATA-CONTRACTS §1：除关联表外都要有这两列。关联表一律不加（本文件不引入
    该 mixin 即可），避免把"关系行"当业务实体记账。
    """

    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, onupdate=utcnow, nullable=False
    )


class RevisionMixin:
    """可编辑实体的 `revision`，初值 1（DATA-CONTRACTS §1）。

    ★ 乐观并发控制的唯一依据：`Repository.update_if(expected={"revision": n})`
      不匹配就不覆盖。默认值既给 Python 侧（`default`）也给 DDL 侧（`server_default`），
      使绕过 ORM 的插入也不会得到 NULL。
    """

    revision: Mapped[int] = mapped_column(
        BigInteger, default=1, server_default=text("1"), nullable=False
    )

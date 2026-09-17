"""通用仓储与工作单元。

对应 FUNCTION-MAP.md §2「通用仓储与错误契约」。契约里的六个方法在这里一一落地：
`get` / `page` / `insert` / `update_if` / `exists` / `aggregate`，外加 `UnitOfWork.transaction()`。

下面四条是**安全相关**的取舍，不是风格偏好：

1. **过滤键与排序字段都必须过白名单**（取自 `model.__table__.columns`）。§2 原文要求
   "参数绑定、排序字段白名单、权限/归属条件在分页前"。若把客户端传入的字段名直接拼进
   `ORDER BY` / `WHERE`，注入面就从"值"扩大到"标识符"，而参数绑定挡不住标识符。
2. **`update_if` 是单条条件 UPDATE**，`expected` 不匹配返回 `False` 而非覆盖。revision、
   租约、状态的并发正确性全部依赖它；"后写覆盖前写"是这里最容易出的缺陷。
3. **写入冲突上抛类型化异常，不替调用方猜**。§2 写的是"唯一约束冲突由服务决定幂等复用或
   409"——决定权在服务，仓储只把 `IntegrityError` 翻成 `UniqueViolationError` 并附约束名。
4. **仓储只返回 ORM 行**（§2："Row 为对应实体列映射，不直接当 API DTO 返回"）。序列化由
   schemas 层按白名单组装，避免把 `password_hash` 之类顺手带出去。

`for_update`（行锁）只在短事务内使用：与网络调用混用会长时间持锁（§0.5）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from sqlalchemy import ColumnElement, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import Base

ModelT = TypeVar("ModelT", bound=Base)


class RepositoryError(Exception):
    """仓储层异常基类。业务层应只捕获本类及其子类，不透传驱动异常。"""


class UnknownFieldError(RepositoryError):
    """过滤/排序/写入用到了实体上不存在的字段。

    ★ 之所以是异常而非忽略：静默忽略一个错别字字段名，会让"带了归属条件的分页"
      退化成"全表分页"——越权就是这样产生的。
    """


class UniqueViolationError(RepositoryError):
    """唯一约束冲突（由 `IntegrityError` 翻译而来）。`constraint` 便于服务判断是哪个键。"""

    def __init__(self, constraint: str | None, message: str) -> None:
        self.constraint = constraint
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class Page:
    """分页结果（FUNCTION-MAP §2：`{items, total}`）。"""

    items: list[Any]
    total: int

    def __len__(self) -> int:
        return len(self.items)


class Repository(Generic[ModelT]):
    """单实体仓储。`session` 由请求作用域注入（§0.5），不在方法签名里重复出现。"""

    def __init__(self, session: AsyncSession, model: type[ModelT]) -> None:
        self._session = session
        self._model = model

    # ---------------------------------------------------------------- 白名单

    @property
    def _column_names(self) -> set[str]:
        return set(self._model.__table__.columns.keys())

    def _check_fields(self, names: Iterable[str]) -> None:
        unknown = sorted(set(names) - self._column_names)
        if unknown:
            raise UnknownFieldError(
                f"{self._model.__tablename__} 上不存在字段 {unknown}；"
                f"可选字段：{sorted(self._column_names)}"
            )

    def _conditions(self, filters: Mapping[str, Any] | None) -> list[ColumnElement[bool]]:
        """把 `filters` 翻成 WHERE 条件。

        约定（刻意保持极小，避免"半个查询语言"）：
        - 值为 `None` → `IS NULL`；
        - 值为 `list`/`tuple`/`set` → `IN`（空集合 → 恒假，不是恒真）；
        - 其余 → 相等。

        `IN` 传空集合返回"恒假"而不是"忽略该条件"：忽略会把调用方本意是
        "没有任何可读单元"的过滤变成"不限制"，属于越权类缺陷。
        """
        if not filters:
            return []
        self._check_fields(filters.keys())
        conds: list[ColumnElement[bool]] = []
        for name, value in filters.items():
            column = self._model.__table__.columns[name]
            if value is None:
                conds.append(column.is_(None))
            elif isinstance(value, (list, tuple, set, frozenset)):
                values = list(value)
                if not values:
                    conds.append(column.in_(values))
                    continue
                conds.append(column.in_(values))
            else:
                conds.append(column == value)
        return conds

    def _order_by(self, order: Sequence[str]) -> list[ColumnElement[Any]]:
        """排序表达式。`"-revision"` 表示降序。"""
        self._check_fields(o.lstrip("-") for o in order)
        exprs: list[ColumnElement[Any]] = []
        for token in order:
            desc = token.startswith("-")
            column = self._model.__table__.columns[token.lstrip("-")]
            exprs.append(column.desc() if desc else column.asc())
        return exprs

    # ---------------------------------------------------------------- 读取

    async def get(self, id: int, *, for_update: bool = False) -> ModelT | None:
        """主键读取。`for_update=True` 施加行锁——仅限短事务（§2）。"""
        stmt = select(self._model).where(self._model.__table__.columns["id"] == id)
        if for_update:
            stmt = stmt.with_for_update()
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def find_one(
        self, filters: Mapping[str, Any], *, for_update: bool = False
    ) -> ModelT | None:
        """按条件取一行。供服务实现"幂等复用已有记录"（§2 `insert` 的配套）。"""
        stmt = select(self._model).where(*self._conditions(filters))
        if for_update:
            stmt = stmt.with_for_update()
        return (await self._session.execute(stmt)).scalars().first()

    async def page(
        self,
        filters: Mapping[str, Any] | None = None,
        *,
        page: int = 1,
        size: int = 20,
        order: Sequence[str] = ("-id",),
    ) -> Page:
        """分页查询。页码从 1 起，`size` 上限取部署配置（默认 100）。

        ★ 调用方必须把权限/归属条件放进 `filters`，**分页在过滤之后**（§2）。
        """
        from app.core.config import get_settings

        settings = get_settings()
        if page < 1:
            raise ValueError("page 必须 >= 1")
        if not 1 <= size <= settings.page_size_max:
            raise ValueError(f"size 必须在 1..{settings.page_size_max} 之间")

        where = self._conditions(filters)
        total = await self._session.scalar(
            select(func.count()).select_from(self._model).where(*where)
        )
        stmt = (
            select(self._model)
            .where(*where)
            .order_by(*self._order_by(order))
            .offset((page - 1) * size)
            .limit(size)
        )
        items = list((await self._session.execute(stmt)).scalars())
        return Page(items=items, total=int(total or 0))

    async def exists(self, filters: Mapping[str, Any]) -> bool:
        """存在性检查（§2：部门/角色 ACL 引用检查用）。"""
        stmt = select(func.count()).select_from(self._model).where(*self._conditions(filters))
        return bool(await self._session.scalar(stmt))

    # ---------------------------------------------------------------- 写入

    async def insert(self, values: Mapping[str, Any]) -> ModelT:
        """插入一行并 flush（拿到自增主键）。

        唯一约束冲突抛 `UniqueViolationError`——是否幂等复用或返回 409 由服务决定（§2）。
        本方法不吞冲突、也不自动"查到旧行返回"，因为那会把"并发重复提交"伪装成成功。
        """
        self._check_fields(values.keys())
        row = self._model(**dict(values))
        self._session.add(row)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            raise UniqueViolationError(self._constraint_of(exc), str(exc.orig)) from exc
        return row

    async def update_if(
        self, id: int, *, expected: Mapping[str, Any], patch: Mapping[str, Any]
    ) -> bool:
        """单条条件 UPDATE：全部 `expected` 命中才写 `patch`，返回是否命中。

        `expected` 为空直接报错——无条件覆盖不是本方法的语义，用 `patch` 覆盖整行是缺陷来源。
        需要"读改写 revision"时把自增表达式放进 `patch`，例如
        `patch={"revision": Model.revision + 1}`，让并发安全由数据库保证而不是先读后写。
        """
        if not expected:
            raise ValueError("update_if 的 expected 不能为空；无条件覆盖请用显式 update 路径")
        self._check_fields(expected.keys())
        self._check_fields(patch.keys())
        stmt = (
            update(self._model)
            .where(self._model.__table__.columns["id"] == id, *self._conditions(expected))
            .values(**dict(patch))
        )
        result = await self._session.execute(stmt)
        return result.rowcount == 1

    # ---------------------------------------------------------------- 聚合

    async def aggregate(
        self,
        filters: Mapping[str, Any] | None,
        *,
        groups: Sequence[str],
        measures: Sequence[str],
    ) -> list[dict[str, Any]]:
        """白名单聚合。`measures` 形如 `count` / `sum:input_tokens` / `max:latency_ms`。

        §2 要求"指标口径只由 metrics 定义"——所以这里只提供最小原语，口径（例如
        "unknown 用量是否计入")留在 M08 的 metrics 层，不在这里夹带业务判断。
        """
        self._check_fields(groups)
        group_cols = [self._model.__table__.columns[g] for g in groups]

        measure_exprs: list[ColumnElement[Any]] = []
        labels: list[str] = []
        for spec in measures:
            if spec == "count":
                measure_exprs.append(func.count().label("count"))
                labels.append("count")
                continue
            if ":" not in spec:
                raise UnknownFieldError(f"聚合表达式非法：{spec!r}（应为 count 或 fn:field）")
            fn_name, field = spec.split(":", 1)
            if fn_name not in {"sum", "min", "max", "avg"}:
                raise UnknownFieldError(f"不支持的聚合函数 {fn_name!r}")
            self._check_fields([field])
            column = self._model.__table__.columns[field]
            measure_exprs.append(getattr(func, fn_name)(column).label(f"{fn_name}_{field}"))
            labels.append(f"{fn_name}_{field}")

        stmt = select(*group_cols, *measure_exprs).where(*self._conditions(filters))
        if group_cols:
            stmt = stmt.group_by(*group_cols)
        rows = (await self._session.execute(stmt)).all()
        group_names = [g for g in groups]
        return [
            {**dict(zip(group_names, row[: len(group_names)], strict=True)),
             **dict(zip(labels, row[len(group_names):], strict=True))}
            for row in rows
        ]

    # ---------------------------------------------------------------- 内部

    @staticmethod
    def _constraint_of(exc: IntegrityError) -> str | None:
        """从驱动异常里提取约束名（失败时返回 None，不因为解析不出就改变异常语义）。"""
        orig = getattr(exc, "orig", None)
        for attr in ("constraint_name",):
            value = getattr(orig, attr, None)
            if value:
                return str(value)
        args = getattr(orig, "args", ())
        for item in args:
            if isinstance(item, str) and "u'" in item:
                return item.split("u'", 1)[1].split("'", 1)[0]
        return None


class UnitOfWork:
    """事务边界（FUNCTION-MAP §2：`UnitOfWork.transaction() -> AsyncContextManager`）。

    成功提交、异常回滚。已在事务中时降级为 SAVEPOINT，避免嵌套 `begin()` 直接抛错——
    服务之间互相调用时不应要求调用方知道自己是否已在事务里。
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @property
    def session(self) -> AsyncSession:
        return self._session

    def repo(self, model: type[ModelT]) -> Repository[ModelT]:
        """取一个绑定当前 session 的仓储。"""
        return Repository(self._session, model)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        """提交最外层事务；异常回滚。

        ★★ 这里不能拿"是否已在事务中"当作"有没有未提交写入"的依据——SQLAlchemy 有
        **autobegin**：一条普通的 `SELECT` 就会开启事务。若因此走 SAVEPOINT 分支，
        退出时只释放保存点，外层事务仍未提交，而 `AsyncSession.close()` 会**回滚**它。
        后果是"接口返回 200、数据却没落库"——最难排查的一类缺陷。

        因此语义定为：**进到这里就负责把最外层事务提交掉**。
        - 已在事务中（autobegin 或上游开启）→ 正常退出时 `commit()`，异常 `rollback()`；
        - 未在事务中 → 用 `session.begin()` 开一个再提交。

        代价：若上游已开了事务并期望自己提交，本类会替它提交。这是有意的——一个请求
        一个事务边界（FUNCTION-MAP §0 第 5 条"写入与 operation_log 短事务一致"），
        需要更细粒度边界时应显式传入独立 session。
        """
        if self._session.in_transaction():
            try:
                yield self._session
            except Exception:
                await self._session.rollback()
                raise
            await self._session.commit()
            return
        async with self._session.begin():
            yield self._session

"""持久层公共出口。

对应 FUNCTION-MAP.md §0 第 5 条（依赖注入）与 §2（仓储/工作单元契约）。
业务代码从 `app.db` 导入即可，不需要知道内部文件划分。
"""

from app.db.base import Base, PKMixin, RevisionMixin, TimestampMixin, utcnow
from app.db.repository import (
    Page,
    Repository,
    RepositoryError,
    UniqueViolationError,
    UnknownFieldError,
    UnitOfWork,
)
from app.db.session import dispose_engine, get_engine, get_session, get_sessionmaker, reset_engine

__all__ = [
    "Base",
    "PKMixin",
    "RevisionMixin",
    "TimestampMixin",
    "utcnow",
    "Page",
    "Repository",
    "RepositoryError",
    "UniqueViolationError",
    "UnknownFieldError",
    "UnitOfWork",
    "dispose_engine",
    "get_engine",
    "get_session",
    "get_sessionmaker",
    "reset_engine",
]

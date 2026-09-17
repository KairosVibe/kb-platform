"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

迁移由 `alembic revision --autogenerate` 产出，**需人工审阅**后再提交：
- 确认没有把 `op.drop_*` 混进"新增表"的迁移；
- 确认索引名、约束名符合 metadata 命名约定（DATA-CONTRACTS §5）；
- DDL 必须在 MySQL 8.0.26 上真实执行验证后方可声明通过。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
${imports if imports else ""}

# revision identifiers, used by Alembic.
revision: str = ${repr(up_revision)}
down_revision: str | None = ${repr(down_revision)}
branch_labels: str | Sequence[str] | None = ${repr(branch_labels)}
depends_on: str | Sequence[str] | None = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}

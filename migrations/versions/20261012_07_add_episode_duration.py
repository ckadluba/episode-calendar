"""Store provider episode durations in minutes."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261012_07"
down_revision: str | None = "20261010_06"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("episodes", sa.Column("duration_minutes", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("episodes", "duration_minutes")

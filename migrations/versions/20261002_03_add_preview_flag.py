"""Track provider-marked preview releases."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261002_03"
down_revision: str | None = "20261001_02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "episode_releases",
        sa.Column("preview", sa.Boolean(), server_default=sa.false(), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("episode_releases", "preview")

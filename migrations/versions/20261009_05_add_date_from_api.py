"""Track whether a release date came from a provider API or is a first-seen guess."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261009_05"
down_revision: str | None = "20261007_04"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "episode_releases",
        sa.Column("date_from_api", sa.Boolean(), server_default=sa.true(), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("episode_releases", "date_from_api")

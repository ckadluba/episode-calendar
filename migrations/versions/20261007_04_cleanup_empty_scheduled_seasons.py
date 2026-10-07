"""Remove scheduled-only episodes that never gained a release."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261007_04"
down_revision: str | None = "20261002_03"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            DELETE FROM episodes
            WHERE id IN (
                SELECT e.id
                FROM episodes e
                JOIN seasons s ON s.id = e.season_id
                WHERE s.external_id LIKE concat('%', :suffix)
                  AND NOT EXISTS (
                      SELECT 1 FROM episode_releases r WHERE r.episode_id = e.id
                  )
            )
            """
        ).bindparams(suffix=":scheduled")
    )
    op.execute(
        sa.text(
            """
            DELETE FROM seasons
            WHERE external_id LIKE concat('%', :suffix)
              AND NOT EXISTS (
                  SELECT 1 FROM episodes e WHERE e.season_id = seasons.id
              )
            """
        ).bindparams(suffix=":scheduled")
    )


def downgrade() -> None:
    pass

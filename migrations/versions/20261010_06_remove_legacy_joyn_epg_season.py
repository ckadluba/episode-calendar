"""Remove the legacy synthetic Joyn EPG season.

The Joyn provider used to store upcoming linear broadcasts in a synthetic
``joyn-epg`` season. Broadcasts are now folded into the real season/episode tree, so the
synthetic season is no longer produced and only lingers as an empty shell (its releases are
re-assigned to the matching catalogue episodes on the next import). Drop it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261010_06"
down_revision: str | None = "20261009_05"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            DELETE FROM episodes
            WHERE season_id IN (
                SELECT id FROM seasons WHERE external_id = :external_id
            )
            """
        ).bindparams(external_id="joyn-epg")
    )
    op.execute(
        sa.text("DELETE FROM seasons WHERE external_id = :external_id").bindparams(
            external_id="joyn-epg"
        )
    )


def downgrade() -> None:
    pass

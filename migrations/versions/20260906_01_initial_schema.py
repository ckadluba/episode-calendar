"""Create the initial episode calendar schema."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260906_01"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "providers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("slug", sa.String(length=100), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_providers")),
        sa.UniqueConstraint("slug", name=op.f("uq_providers_slug")),
    )
    op.create_table(
        "series",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("provider_id", sa.Uuid(), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["provider_id"],
            ["providers.id"],
            name=op.f("fk_series_provider_id_providers"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_series")),
        sa.UniqueConstraint("provider_id", "external_id", name=op.f("uq_series_provider_id")),
    )
    op.create_index(op.f("ix_series_provider_id"), "series", ["provider_id"])
    op.create_index(op.f("ix_series_title"), "series", ["title"])
    op.create_table(
        "seasons",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("series_id", sa.Uuid(), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=False),
        sa.Column("number", sa.Integer(), nullable=True),
        sa.Column("title", sa.String(length=500), nullable=True),
        sa.ForeignKeyConstraint(
            ["series_id"],
            ["series.id"],
            name=op.f("fk_seasons_series_id_series"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_seasons")),
        sa.UniqueConstraint("series_id", "external_id", name=op.f("uq_seasons_series_id")),
    )
    op.create_index(op.f("ix_seasons_series_id"), "seasons", ["series_id"])
    op.create_table(
        "episodes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("season_id", sa.Uuid(), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=False),
        sa.Column("number", sa.Integer(), nullable=True),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["season_id"],
            ["seasons.id"],
            name=op.f("fk_episodes_season_id_seasons"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_episodes")),
        sa.UniqueConstraint("season_id", "external_id", name=op.f("uq_episodes_season_id")),
    )
    op.create_index(op.f("ix_episodes_season_id"), "episodes", ["season_id"])
    op.create_table(
        "episode_releases",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("episode_id", sa.Uuid(), nullable=False),
        sa.Column("provider_id", sa.Uuid(), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=True),
        sa.Column(
            "release_type",
            sa.Enum("STREAMING", "TV_BROADCAST", name="releasetype", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("release_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("url", sa.String(length=2000), nullable=True),
        sa.ForeignKeyConstraint(
            ["episode_id"],
            ["episodes.id"],
            name=op.f("fk_episode_releases_episode_id_episodes"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["provider_id"],
            ["providers.id"],
            name=op.f("fk_episode_releases_provider_id_providers"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_episode_releases")),
        sa.UniqueConstraint(
            "episode_id",
            "provider_id",
            "release_type",
            "release_at",
            name=op.f("uq_episode_releases_episode_id"),
        ),
        sa.UniqueConstraint(
            "provider_id", "external_id", name=op.f("uq_episode_releases_provider_id")
        ),
    )
    op.create_index(op.f("ix_episode_releases_episode_id"), "episode_releases", ["episode_id"])
    op.create_index(op.f("ix_episode_releases_provider_id"), "episode_releases", ["provider_id"])
    op.create_index(op.f("ix_episode_releases_release_at"), "episode_releases", ["release_at"])


def downgrade() -> None:
    op.drop_table("episode_releases")
    op.drop_table("episodes")
    op.drop_table("seasons")
    op.drop_table("series")
    op.drop_table("providers")

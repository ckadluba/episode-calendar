from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.ext.asyncio import AsyncSession

from episode_calendar.db.models import (
    Episode,
    EpisodeRelease,
    Provider,
    Season,
    Series,
)
from episode_calendar.domain import ReleaseType


async def test_domain_tree_and_release_are_persisted(db_session: AsyncSession) -> None:
    release_at = datetime(2026, 9, 6, 18, 30, tzinfo=UTC)
    provider = Provider(slug="example", name="Example")
    series = Series(provider=provider, external_id="series-1", title="A Series")
    season = Season(series=series, external_id="season-1", number=1)
    episode = Episode(season=season, external_id="episode-1", number=1, title="Pilot")
    release = EpisodeRelease(
        episode=episode,
        provider=provider,
        external_id="release-1",
        release_type=ReleaseType.STREAMING,
        release_at=release_at,
        url="https://example.test/watch/episode-1",
    )
    db_session.add(release)
    await db_session.commit()

    persisted = await db_session.scalar(select(EpisodeRelease))

    assert persisted is not None
    assert persisted.episode.title == "Pilot"
    assert persisted.episode.season.series.external_id == "series-1"
    assert persisted.release_at == release_at
    assert persisted.release_at.tzinfo is not None


async def test_naive_release_datetime_is_rejected(db_session: AsyncSession) -> None:
    provider = Provider(slug="example", name="Example")
    series = Series(provider=provider, external_id="series-1", title="A Series")
    season = Season(series=series, external_id="season-1")
    episode = Episode(season=season, external_id="episode-1", title="Pilot")
    db_session.add(
        EpisodeRelease(
            episode=episode,
            provider=provider,
            release_type=ReleaseType.STREAMING,
            release_at=datetime(2026, 9, 6, 18, 30),
        )
    )

    with pytest.raises(StatementError, match="timezone-aware"):
        await db_session.commit()


async def test_external_ids_are_unique_only_within_provider(db_session: AsyncSession) -> None:
    first_provider = Provider(slug="first", name="First")
    second_provider = Provider(slug="second", name="Second")
    db_session.add_all(
        [
            Series(provider=first_provider, external_id="shared-id", title="First title"),
            Series(provider=second_provider, external_id="shared-id", title="Second title"),
        ]
    )
    await db_session.commit()

    db_session.add(Series(provider=first_provider, external_id="shared-id", title="Duplicate"))

    with pytest.raises(IntegrityError):
        await db_session.commit()

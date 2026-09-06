from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from episode_calendar.db.models import Episode, EpisodeRelease, Provider, Season, Series
from episode_calendar.domain import ReleaseType
from episode_calendar.importer import import_series
from episode_calendar.providers.base import (
    NormalizedEpisode,
    NormalizedEpisodeRelease,
    NormalizedSeason,
    NormalizedSeries,
)


class FakeProvider:
    slug = "joyn"

    async def fetch_series(self, external_id: str) -> NormalizedSeries:
        return NormalizedSeries(
            external_id="joyn-series",
            title="Demo",
            seasons=(
                NormalizedSeason(
                    external_id="joyn-season",
                    number=1,
                    episodes=(
                        NormalizedEpisode(
                            external_id="joyn-episode",
                            number=1,
                            title="Pilot",
                            releases=(
                                NormalizedEpisodeRelease(
                                    release_type=ReleaseType.STREAMING,
                                    release_at=datetime(2026, 9, 6, 18, 30, tzinfo=UTC),
                                    url="https://www.joyn.at/episode",
                                ),
                            ),
                        ),
                    ),
                ),
            ),
        )


async def test_import_is_idempotent(db_session: AsyncSession) -> None:
    provider = FakeProvider()
    first = await import_series(db_session, provider, "ignored")
    second = await import_series(db_session, provider, "ignored")

    assert first.series.id == second.series.id
    assert (await db_session.scalar(select(func.count()).select_from(Provider))) == 1
    assert (await db_session.scalar(select(func.count()).select_from(Series))) == 1
    assert (await db_session.scalar(select(func.count()).select_from(Season))) == 1
    assert (await db_session.scalar(select(func.count()).select_from(Episode))) == 1
    assert (await db_session.scalar(select(func.count()).select_from(EpisodeRelease))) == 1


async def test_import_updates_existing_metadata(db_session: AsyncSession) -> None:
    result = await import_series(db_session, FakeProvider(), "ignored")
    persisted = await db_session.get(Series, result.series.id)
    assert persisted is not None
    assert persisted.title == "Demo"
    assert persisted.provider.slug == "joyn"

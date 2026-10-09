import asyncio
import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from episode_calendar.db.models import Episode, EpisodeRelease, Provider, Season, Series
from episode_calendar.domain import ReleaseType
from episode_calendar.importer import (
    ImportResult,
    configured_series,
    import_configured,
    import_series,
)
from episode_calendar.providers.base import (
    NormalizedEpisode,
    NormalizedEpisodeRelease,
    NormalizedSeason,
    NormalizedSeries,
)
from episode_calendar.series_config import displayable_series_by_platform


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
    assert first.new_episodes == 1
    assert second.new_episodes == 0
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


async def test_catalog_preview_and_tv_release_are_not_reruns(
    db_session: AsyncSession,
) -> None:
    class JoynCatalogProvider:
        slug = "joyn"

        async def fetch_series(self, external_id: str) -> NormalizedSeries:
            return NormalizedSeries(
                external_id="joyn-series",
                title="Forsthaus Rampensau",
                seasons=(
                    NormalizedSeason(
                        external_id="joyn-season",
                        number=5,
                        episodes=(
                            NormalizedEpisode(
                                external_id="joyn-episode",
                                number=1,
                                title="Folge 1",
                                releases=(
                                    NormalizedEpisodeRelease(
                                        release_type=ReleaseType.STREAMING,
                                        release_at=datetime(2026, 10, 10, 0, 15, tzinfo=UTC),
                                        preview=True,
                                    ),
                                    NormalizedEpisodeRelease(
                                        release_type=ReleaseType.TV_BROADCAST,
                                        release_at=datetime(2026, 10, 12, 20, 20, tzinfo=UTC),
                                    ),
                                ),
                            ),
                        ),
                    ),
                ),
            )

    await import_series(db_session, JoynCatalogProvider(), "ignored")

    releases = list(
        await db_session.scalars(select(EpisodeRelease).order_by(EpisodeRelease.release_at))
    )
    assert len(releases) == 2
    assert [release.preview for release in releases] == [True, False]
    assert [release.rerun for release in releases] == [False, False]


async def test_old_on_demand_release_is_not_marked_as_rerun(
    db_session: AsyncSession,
) -> None:
    class ChangingCatalogProvider:
        slug = "joyn"
        calls = 0

        async def fetch_series(self, external_id: str) -> NormalizedSeries:
            self.calls += 1
            release_at = datetime(
                2025 if self.calls == 1 else 2026,
                1 if self.calls == 1 else 10,
                1 if self.calls == 1 else 6,
                tzinfo=UTC,
            )
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
                                        release_at=release_at,
                                    ),
                                ),
                            ),
                        ),
                    ),
                ),
            )

    provider = ChangingCatalogProvider()
    await import_series(
        db_session,
        provider,
        "ignored",
        reference_time=datetime(2026, 10, 6, tzinfo=UTC),
    )
    await import_series(
        db_session,
        provider,
        "ignored",
        reference_time=datetime(2026, 10, 6, tzinfo=UTC),
    )

    releases = list(await db_session.scalars(select(EpisodeRelease)))
    assert releases
    assert all(release.rerun is False for release in releases)


async def test_only_tv_release_persists_normalized_rerun_flag(
    db_session: AsyncSession,
) -> None:
    class EpgProvider:
        slug = "joyn"

        async def fetch_series(self, external_id: str) -> NormalizedSeries:
            return NormalizedSeries(
                external_id="joyn-series",
                title="Demo",
                seasons=(
                    NormalizedSeason(
                        external_id="joyn-epg",
                        episodes=(
                            NormalizedEpisode(
                                external_id="epg-episode",
                                title="Demo",
                                releases=(
                                    NormalizedEpisodeRelease(
                                        release_type=ReleaseType.STREAMING,
                                        release_at=datetime(2026, 10, 10, tzinfo=UTC),
                                        preview=True,
                                        rerun=True,
                                    ),
                                    NormalizedEpisodeRelease(
                                        release_type=ReleaseType.TV_BROADCAST,
                                        release_at=datetime(2026, 10, 11, tzinfo=UTC),
                                        rerun=True,
                                    ),
                                ),
                            ),
                        ),
                    ),
                ),
            )

    await import_series(db_session, EpgProvider(), "ignored")

    releases = list(
        await db_session.scalars(select(EpisodeRelease).order_by(EpisodeRelease.release_at))
    )
    assert [release.rerun for release in releases] == [False, True]


async def test_later_broadcast_of_same_episode_is_marked_as_rerun(
    db_session: AsyncSession,
) -> None:
    class RepeatBroadcastProvider:
        slug = "ardmediathek"

        async def fetch_series(self, external_id: str) -> NormalizedSeries:
            return NormalizedSeries(
                external_id="ard-series",
                title="Demo",
                seasons=(
                    NormalizedSeason(
                        external_id="ard-season",
                        number=1,
                        episodes=(
                            NormalizedEpisode(
                                external_id="ard-episode",
                                number=1,
                                title="Ich bin ein Dorfbewohner",
                                releases=(
                                    NormalizedEpisodeRelease(
                                        external_id="premiere-id",
                                        release_type=ReleaseType.TV_BROADCAST,
                                        release_at=datetime(2026, 9, 24, 16, 50, tzinfo=UTC),
                                    ),
                                    NormalizedEpisodeRelease(
                                        external_id="repeat-id",
                                        release_type=ReleaseType.TV_BROADCAST,
                                        release_at=datetime(2026, 10, 8, 21, 15, tzinfo=UTC),
                                    ),
                                ),
                            ),
                        ),
                    ),
                ),
            )

    await import_series(
        db_session,
        RepeatBroadcastProvider(),
        "ignored",
        reference_time=datetime(2026, 10, 7, tzinfo=UTC),
    )

    releases = list(
        await db_session.scalars(select(EpisodeRelease).order_by(EpisodeRelease.release_at))
    )
    assert [(release.external_id, release.rerun) for release in releases] == [
        ("premiere-id", False),
        ("repeat-id", True),
    ]


async def test_later_broadcast_of_earlier_episode_is_marked_as_rerun(
    db_session: AsyncSession,
) -> None:
    class MarathonProvider:
        slug = "ardmediathek"

        async def fetch_series(self, external_id: str) -> NormalizedSeries:
            return NormalizedSeries(
                external_id="ard-series",
                title="Demo",
                seasons=(
                    NormalizedSeason(
                        external_id="ard-season",
                        number=1,
                        episodes=(
                            NormalizedEpisode(
                                external_id="ep-5",
                                number=5,
                                title="Die Nachricht",
                                releases=(
                                    NormalizedEpisodeRelease(
                                        external_id="ep-5-id",
                                        release_type=ReleaseType.TV_BROADCAST,
                                        release_at=datetime(2026, 10, 1, 16, 50, tzinfo=UTC),
                                    ),
                                ),
                            ),
                            NormalizedEpisode(
                                external_id="ep-3",
                                number=3,
                                title="Was für ein Theater",
                                releases=(
                                    NormalizedEpisodeRelease(
                                        external_id="ep-3-id",
                                        release_type=ReleaseType.TV_BROADCAST,
                                        release_at=datetime(2026, 10, 8, 22, 5, tzinfo=UTC),
                                    ),
                                ),
                            ),
                        ),
                    ),
                ),
            )

    await import_series(
        db_session,
        MarathonProvider(),
        "ignored",
        reference_time=datetime(2026, 10, 7, tzinfo=UTC),
    )

    releases = dict(
        (release.external_id, release.rerun)
        for release in await db_session.scalars(select(EpisodeRelease))
    )
    assert releases == {"ep-5-id": False, "ep-3-id": True}


async def test_catalog_release_replaces_epg_release_at_same_time(
    db_session: AsyncSession,
) -> None:
    release_at = datetime(2026, 10, 6, 18, 15, tzinfo=UTC)

    class EpgThenCatalogProvider:
        slug = "joyn"
        calls = 0

        async def fetch_series(self, external_id: str) -> NormalizedSeries:
            self.calls += 1
            if self.calls == 1:
                return NormalizedSeries(
                    external_id="joyn-series",
                    title="Demo",
                    seasons=(
                        NormalizedSeason(
                            external_id="joyn-epg",
                            episodes=(
                                NormalizedEpisode(
                                    external_id="epg-release",
                                    title="Demo",
                                    releases=(
                                        NormalizedEpisodeRelease(
                                            external_id="epg-release",
                                            release_type=ReleaseType.TV_BROADCAST,
                                            release_at=release_at,
                                        ),
                                    ),
                                ),
                            ),
                        ),
                    ),
                )
            return NormalizedSeries(
                external_id="joyn-series",
                title="Demo",
                seasons=(
                    NormalizedSeason(
                        external_id="catalog-season",
                        number=14,
                        episodes=(
                            NormalizedEpisode(
                                external_id="catalog-episode",
                                number=1,
                                title="The real episode title",
                                releases=(
                                    NormalizedEpisodeRelease(
                                        release_type=ReleaseType.STREAMING,
                                        release_at=release_at,
                                        url="https://joyn.example/episode",
                                    ),
                                ),
                            ),
                        ),
                    ),
                ),
            )

    provider = EpgThenCatalogProvider()
    await import_series(db_session, provider, "ignored")
    await import_series(db_session, provider, "ignored")

    releases = list(await db_session.scalars(select(EpisodeRelease)))
    assert len(releases) == 1
    assert releases[0].release_type is ReleaseType.STREAMING
    assert releases[0].url == "https://joyn.example/episode"
    episode = await db_session.get(Episode, releases[0].episode_id)
    assert episode is not None
    assert episode.title == "The real episode title"
    season = await db_session.get(Season, episode.season_id)
    assert season is not None
    assert season.number == 14


async def test_catalog_tv_release_replaces_numberless_epg_episode(
    db_session: AsyncSession,
) -> None:
    release_at = datetime(2026, 10, 6, 18, 15, tzinfo=UTC)

    class EpgThenCatalogProvider:
        slug = "joyn"
        calls = 0

        async def fetch_series(self, external_id: str) -> NormalizedSeries:
            self.calls += 1
            if self.calls == 1:
                return NormalizedSeries(
                    external_id="joyn-series",
                    title="Demo",
                    seasons=(
                        NormalizedSeason(
                            external_id="joyn-epg",
                            episodes=(
                                NormalizedEpisode(
                                    external_id="epg-release",
                                    title="Demo",
                                    releases=(
                                        NormalizedEpisodeRelease(
                                            external_id="epg-release",
                                            release_type=ReleaseType.TV_BROADCAST,
                                            release_at=release_at,
                                        ),
                                    ),
                                ),
                            ),
                        ),
                    ),
                )
            return NormalizedSeries(
                external_id="joyn-series",
                title="Demo",
                seasons=(
                    NormalizedSeason(
                        external_id="catalog-season",
                        number=1,
                        episodes=(
                            NormalizedEpisode(
                                external_id="catalog-episode",
                                number=2,
                                title="Catalog episode",
                                releases=(
                                    NormalizedEpisodeRelease(
                                        release_type=ReleaseType.STREAMING,
                                        release_at=release_at - timedelta(days=7),
                                        url="https://joyn.example/episode-2",
                                    ),
                                    NormalizedEpisodeRelease(
                                        external_id="epg-release",
                                        release_type=ReleaseType.TV_BROADCAST,
                                        release_at=release_at,
                                    ),
                                ),
                            ),
                        ),
                    ),
                ),
            )

    provider = EpgThenCatalogProvider()
    await import_series(db_session, provider, "ignored")
    await import_series(db_session, provider, "ignored")

    releases = list(await db_session.scalars(select(EpisodeRelease)))
    assert len(releases) == 2
    tv_release = next(item for item in releases if item.release_type is ReleaseType.TV_BROADCAST)
    episode = await db_session.get(Episode, tv_release.episode_id)
    assert episode is not None
    assert episode.number == 2
    assert episode.title == "Catalog episode"
    assert tv_release.url == "https://joyn.example/episode-2"


async def test_stale_catalog_marks_epg_broadcast_as_rerun(db_session: AsyncSession) -> None:
    # MOST WANTED had its latest catalog release on 28 April, while the EPG exposes
    # repeats for 14 October. This is intentionally a fixed-date regression test so
    # the rerun policy does not depend on the wall clock when the suite runs.
    old_release_at = datetime(2026, 4, 28, 17, 1, tzinfo=UTC)
    epg_release_at = datetime(2026, 10, 14, 18, 15, tzinfo=UTC)

    class StaleCatalogProvider:
        slug = "joyn"
        calls = 0

        async def fetch_series(self, external_id: str) -> NormalizedSeries:
            self.calls += 1
            episodes = [
                NormalizedEpisode(
                    external_id="catalog-episode",
                    number=1,
                    title="Old episode",
                    releases=(
                        NormalizedEpisodeRelease(
                            release_type=ReleaseType.STREAMING,
                            release_at=old_release_at,
                        ),
                    ),
                )
            ]
            if self.calls > 1:
                episodes.append(
                    NormalizedEpisode(
                        external_id="epg-release",
                        title="MOST WANTED",
                        releases=(
                            NormalizedEpisodeRelease(
                                external_id="epg-release",
                                release_type=ReleaseType.TV_BROADCAST,
                                release_at=epg_release_at,
                            ),
                        ),
                    )
                )
            return NormalizedSeries(
                external_id="joyn-series",
                title="MOST WANTED",
                seasons=(
                    NormalizedSeason(
                        external_id="catalog-season",
                        number=1,
                        episodes=tuple(episodes[:1]),
                    ),
                    *(
                        (
                            NormalizedSeason(
                                external_id="joyn-epg",
                                number=None,
                                episodes=(episodes[1],),
                            ),
                        )
                        if self.calls > 1
                        else ()
                    ),
                ),
            )

    provider = StaleCatalogProvider()
    reference_time = datetime(2026, 10, 5, tzinfo=UTC)
    await import_series(db_session, provider, "ignored", reference_time=reference_time)
    await import_series(db_session, provider, "ignored", reference_time=reference_time)

    release = await db_session.scalar(
        select(EpisodeRelease).where(EpisodeRelease.external_id == "epg-release")
    )
    assert release is not None
    assert release.rerun is True


async def test_current_epg_slot_is_not_marked_stale_when_catalog_has_tv_schedule(
    db_session: AsyncSession,
) -> None:
    class CurrentScheduleProvider:
        slug = "joyn"

        async def fetch_series(self, external_id: str) -> NormalizedSeries:
            return NormalizedSeries(
                external_id="joyn-series",
                title="Demo",
                seasons=(
                    NormalizedSeason(
                        external_id="catalog-season",
                        number=1,
                        episodes=(
                            NormalizedEpisode(
                                external_id="catalog-episode",
                                number=1,
                                title="Old episode",
                                releases=(
                                    NormalizedEpisodeRelease(
                                        release_type=ReleaseType.TV_BROADCAST,
                                        release_at=datetime(2026, 4, 28, 18, 15, tzinfo=UTC),
                                    ),
                                ),
                            ),
                        ),
                    ),
                    NormalizedSeason(
                        external_id="joyn-epg",
                        episodes=(
                            NormalizedEpisode(
                                external_id="epg-release",
                                title="Demo",
                                releases=(
                                    NormalizedEpisodeRelease(
                                        external_id="epg-release",
                                        release_type=ReleaseType.TV_BROADCAST,
                                        release_at=datetime(2026, 10, 5, 18, 15, tzinfo=UTC),
                                    ),
                                ),
                            ),
                        ),
                    ),
                ),
            )

    await import_series(
        db_session,
        CurrentScheduleProvider(),
        "ignored",
        reference_time=datetime(2026, 10, 5, tzinfo=UTC),
    )

    release = await db_session.scalar(
        select(EpisodeRelease).where(EpisodeRelease.external_id == "epg-release")
    )
    assert release is not None
    assert release.rerun is False


async def test_late_epg_broadcasts_are_marked_as_reruns_from_schedule(
    db_session: AsyncSession,
) -> None:
    class ScheduleProvider:
        slug = "joyn"

        async def fetch_series(self, external_id: str) -> NormalizedSeries:
            catalog_episodes = tuple(
                NormalizedEpisode(
                    external_id=f"catalog-{number}",
                    number=number,
                    title=f"Episode {number}",
                    releases=(
                        NormalizedEpisodeRelease(
                            release_type=ReleaseType.TV_BROADCAST,
                            release_at=datetime(2026, 10, 6, 20 + number, 30, tzinfo=UTC),
                        ),
                    ),
                )
                for number in (1, 2)
            )
            epg_episodes = tuple(
                NormalizedEpisode(
                    external_id=f"epg-{number}",
                    title="Demo",
                    releases=(
                        NormalizedEpisodeRelease(
                            release_type=ReleaseType.TV_BROADCAST,
                            release_at=release_at,
                        ),
                    ),
                )
                for number, release_at in enumerate(
                    (
                        datetime(2026, 10, 7, 2, 40, tzinfo=UTC),
                        datetime(2026, 10, 11, 3, 55, tzinfo=UTC),
                    ),
                    start=1,
                )
            )
            return NormalizedSeries(
                external_id="joyn-series",
                title="Demo",
                seasons=(
                    NormalizedSeason(
                        external_id="catalog-season",
                        number=1,
                        episodes=catalog_episodes,
                    ),
                    NormalizedSeason(
                        external_id="joyn-epg",
                        episodes=epg_episodes,
                    ),
                ),
            )

    await import_series(
        db_session,
        ScheduleProvider(),
        "ignored",
        reference_time=datetime(2026, 10, 5, tzinfo=UTC),
    )

    releases = list(await db_session.scalars(select(EpisodeRelease)))
    assert [release.rerun for release in sorted(releases, key=lambda item: item.release_at)] == [
        False,
        False,
        True,
        True,
    ]


async def test_reimport_preserves_known_episode_releases(db_session: AsyncSession) -> None:
    class ChangingProvider(FakeProvider):
        calls = 0

        async def fetch_series(self, external_id: str) -> NormalizedSeries:
            normalized = await super().fetch_series(external_id)
            self.calls += 1
            if self.calls > 1:
                season = normalized.seasons[0]
                normalized = normalized.model_copy(
                    update={
                        "seasons": (
                            season.model_copy(
                                update={
                                    "episodes": (
                                        season.episodes[0].model_copy(update={"releases": ()}),
                                    )
                                }
                            ),
                        )
                    }
                )
            return normalized

    provider = ChangingProvider()
    reference_time = datetime(2027, 5, 1, tzinfo=UTC)
    await import_series(db_session, provider, "ignored", reference_time=reference_time)
    await import_series(db_session, provider, "ignored", reference_time=reference_time)

    assert (await db_session.scalar(select(func.count()).select_from(EpisodeRelease))) == 1


async def test_import_adds_new_episode_without_replacing_known_episode(
    db_session: AsyncSession,
) -> None:
    class ChangingProvider(FakeProvider):
        calls = 0

        async def fetch_series(self, external_id: str) -> NormalizedSeries:
            self.calls += 1
            normalized = await super().fetch_series(external_id)
            if self.calls == 1:
                return normalized
            season = normalized.seasons[0]
            return normalized.model_copy(
                update={
                    "seasons": (
                        season.model_copy(
                            update={
                                "episodes": (
                                    *season.episodes,
                                    NormalizedEpisode(
                                        external_id="joyn-episode-2",
                                        number=2,
                                        title="Second episode",
                                        releases=(
                                            NormalizedEpisodeRelease(
                                                release_type=ReleaseType.STREAMING,
                                                release_at=datetime(2026, 9, 7, 18, 30, tzinfo=UTC),
                                            ),
                                        ),
                                    ),
                                )
                            }
                        ),
                    )
                }
            )

    provider = ChangingProvider()
    reference_time = datetime(2027, 5, 1, tzinfo=UTC)
    await import_series(db_session, provider, "ignored", reference_time=reference_time)
    result = await import_series(db_session, provider, "ignored", reference_time=reference_time)

    assert result.episodes == 2
    assert (await db_session.scalar(select(func.count()).select_from(Episode))) == 2
    assert (await db_session.scalar(select(func.count()).select_from(EpisodeRelease))) == 2


async def test_import_updates_known_recent_episode(db_session: AsyncSession) -> None:
    class CorrectingProvider(FakeProvider):
        calls = 0

        async def fetch_series(self, external_id: str) -> NormalizedSeries:
            self.calls += 1
            normalized = await super().fetch_series(external_id)
            if self.calls > 1:
                episode = normalized.seasons[0].episodes[0]
                normalized = normalized.model_copy(
                    update={
                        "seasons": (
                            normalized.seasons[0].model_copy(
                                update={
                                    "episodes": (
                                        episode.model_copy(
                                            update={
                                                "title": "Corrected pilot",
                                                "releases": (
                                                    episode.releases[0].model_copy(
                                                        update={
                                                            "release_at": datetime(
                                                                2026, 9, 7, 18, 30, tzinfo=UTC
                                                            )
                                                        }
                                                    ),
                                                ),
                                            }
                                        ),
                                    )
                                }
                            ),
                        )
                    }
                )
            return normalized

    provider = CorrectingProvider()
    reference_time = datetime(2026, 9, 10, tzinfo=UTC)
    await import_series(db_session, provider, "ignored", reference_time=reference_time)
    await import_series(db_session, provider, "ignored", reference_time=reference_time)

    episode = await db_session.scalar(select(Episode))
    release = await db_session.scalar(select(EpisodeRelease))
    assert episode is not None
    assert episode.title == "Corrected pilot"
    assert release is not None
    assert release.release_at == datetime(2026, 9, 7, 18, 30, tzinfo=UTC)


async def test_three_week_comparison_with_and_without_rerun_filter(
    db_session: AsyncSession,
) -> None:
    class ComparisonProvider:
        calls = 0

        def __init__(self, slug: str) -> None:
            self.slug = slug

        async def fetch_series(self, external_id: str) -> NormalizedSeries:
            self.calls += 1
            if self.calls == 1:
                episodes = (
                    NormalizedEpisode(
                        external_id="episode-1",
                        number=1,
                        title="Episode 1",
                        releases=(
                            NormalizedEpisodeRelease(
                                release_type=ReleaseType.TV_BROADCAST,
                                release_at=datetime(2026, 1, 8, tzinfo=UTC),
                            ),
                        ),
                    ),
                )
            else:
                episodes = (
                    NormalizedEpisode(
                        external_id="episode-1",
                        number=1,
                        title="Episode 1",
                        releases=(
                            NormalizedEpisodeRelease(
                                release_type=ReleaseType.TV_BROADCAST,
                                release_at=datetime(2026, 10, 8, tzinfo=UTC),
                            ),
                        ),
                    ),
                    NormalizedEpisode(
                        external_id="episode-2",
                        number=2,
                        title="Episode 2",
                        releases=(
                            NormalizedEpisodeRelease(
                                release_type=ReleaseType.TV_BROADCAST,
                                release_at=datetime(2026, 10, 15, tzinfo=UTC),
                            ),
                        ),
                    ),
                )
            return NormalizedSeries(
                external_id=external_id,
                title="Comparison series",
                seasons=(
                    NormalizedSeason(
                        external_id="season-1",
                        number=1,
                        episodes=episodes,
                    ),
                ),
            )

    async def imported_episode_numbers(
        provider: ComparisonProvider,
        external_id: str,
        reference_time: datetime,
        *,
        include_reruns: bool,
    ) -> set[int | None]:
        await import_series(db_session, provider, external_id, reference_time=reference_time)
        await import_series(db_session, provider, external_id, reference_time=reference_time)
        rows = await db_session.execute(
            select(Episode.number)
            .join(Season)
            .join(Series)
            .join(EpisodeRelease)
            .where(
                Series.external_id == external_id,
                EpisodeRelease.release_at >= datetime(2026, 10, 1, tzinfo=UTC),
                EpisodeRelease.release_at < datetime(2026, 10, 22, tzinfo=UTC),
                EpisodeRelease.rerun.is_(False) if not include_reruns else True,
            )
        )
        return {number for (number,) in rows}

    with_filter = await imported_episode_numbers(
        ComparisonProvider("comparison-with-filter"),
        "comparison-with-filter",
        datetime(2026, 10, 1, tzinfo=UTC),
        include_reruns=False,
    )
    without_filter = await imported_episode_numbers(
        ComparisonProvider("comparison-without-filter"),
        "comparison-without-filter",
        datetime(2025, 1, 1, tzinfo=UTC),
        include_reruns=True,
    )

    assert with_filter == {2}
    assert without_filter == {1, 2}


async def test_import_marks_older_seasons_as_reruns(
    db_session: AsyncSession,
) -> None:
    def normalized(seasons: tuple[NormalizedSeason, ...]) -> NormalizedSeries:
        return NormalizedSeries(external_id="ard-series", title="Demo", seasons=seasons)

    def season(number: int, release_at: datetime) -> NormalizedSeason:
        return NormalizedSeason(
            external_id=f"ard-season-{number}",
            number=number,
            episodes=(
                NormalizedEpisode(
                    external_id=f"ard-episode-{number}",
                    number=1,
                    title=f"Episode {number}",
                    releases=(
                        NormalizedEpisodeRelease(
                            release_type=ReleaseType.TV_BROADCAST,
                            release_at=release_at,
                        ),
                    ),
                ),
            ),
        )

    class ChangingProvider:
        slug = "ardmediathek"
        calls = 0

        async def fetch_series(self, external_id: str) -> NormalizedSeries:
            self.calls += 1
            if self.calls == 1:
                return normalized((season(2, datetime(2026, 9, 24, tzinfo=UTC)),))
            return normalized(
                (
                    season(1, datetime(2026, 10, 8, tzinfo=UTC)),
                    season(2, datetime(2026, 10, 1, tzinfo=UTC)),
                )
            )

    provider = ChangingProvider()
    await import_series(db_session, provider, "configured-id")
    result = await import_series(db_session, provider, "configured-id")

    assert result.seasons == 2
    seasons = list(await db_session.scalars(select(Season).order_by(Season.number)))
    assert [item.number for item in seasons] == [1, 2]
    releases = list(
        await db_session.scalars(select(EpisodeRelease).order_by(EpisodeRelease.release_at))
    )
    assert [release.release_at for release in releases] == [
        datetime(2026, 10, 1, tzinfo=UTC),
        datetime(2026, 10, 8, tzinfo=UTC),
    ]
    assert [release.rerun for release in releases] == [False, True]


async def test_import_preserves_newer_season_when_only_rerun_is_returned(
    db_session: AsyncSession,
) -> None:
    class RerunProvider:
        slug = "ardmediathek"
        calls = 0

        async def fetch_series(self, external_id: str) -> NormalizedSeries:
            self.calls += 1
            number = 2 if self.calls == 1 else 1
            return NormalizedSeries(
                external_id="ard-series",
                title="Demo",
                seasons=(
                    NormalizedSeason(
                        external_id=f"ard-season-{number}",
                        number=number,
                        episodes=(
                            NormalizedEpisode(
                                external_id=f"ard-episode-{number}",
                                number=1,
                                title=f"Episode {number}",
                                releases=(
                                    NormalizedEpisodeRelease(
                                        release_type=ReleaseType.TV_BROADCAST,
                                        release_at=datetime(2026, 10, number, tzinfo=UTC),
                                    ),
                                ),
                            ),
                        ),
                    ),
                ),
            )

    provider = RerunProvider()
    await import_series(db_session, provider, "configured-id")
    await import_series(db_session, provider, "configured-id")

    releases = list(await db_session.scalars(select(EpisodeRelease)))
    assert len(releases) == 2
    assert {release.release_at for release in releases} == {
        datetime(2026, 10, 1, tzinfo=UTC),
        datetime(2026, 10, 2, tzinfo=UTC),
    }
    assert sum(release.rerun for release in releases) == 1


async def test_import_removes_releases_of_obsolete_season_that_disappears(
    db_session: AsyncSession,
) -> None:
    # A season transition can leave a stale release on the previous season (for example
    # the finale stamped with the upcoming season's diffusion date). Once the provider
    # only returns the new season, the obsolete season's stale release must be removed.
    class TransitionProvider:
        slug = "rtlplus"
        calls = 0

        async def fetch_series(self, external_id: str) -> NormalizedSeries:
            self.calls += 1
            number = 5 if self.calls == 1 else 6
            return NormalizedSeries(
                external_id="rtl-series",
                title="Demo",
                seasons=(
                    NormalizedSeason(
                        external_id=f"rtl-season-{number}",
                        number=number,
                        episodes=(
                            NormalizedEpisode(
                                external_id=f"rtl-episode-{number}-20",
                                number=20,
                                title="Folge 20",
                                releases=(
                                    NormalizedEpisodeRelease(
                                        release_type=ReleaseType.STREAMING,
                                        release_at=datetime(2026, 10, 5, 10, 0, tzinfo=UTC),
                                    ),
                                ),
                            ),
                        ),
                    ),
                ),
            )

    provider = TransitionProvider()
    await import_series(db_session, provider, "ignored")
    await import_series(db_session, provider, "ignored")

    season_five = await db_session.scalar(select(Season).where(Season.number == 5))
    assert season_five is not None
    stale_releases = await db_session.scalar(
        select(func.count())
        .select_from(EpisodeRelease)
        .join(Episode, Episode.id == EpisodeRelease.episode_id)
        .where(Episode.season_id == season_five.id)
    )
    assert stale_releases == 0
    # The new season's release is still imported.
    assert (await db_session.scalar(select(func.count()).select_from(EpisodeRelease))) == 1


def test_configured_series_reads_provider_lists(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "series.json"
    config_path.write_text(
        '{"platforms": [{"id": "joyn", "name": "Joyn.at", "series": '
        '[{"id": " demo "}, {"id": "hidden", "run_import": false}, '
        '{"id": ""}, {"id": "second"}]}]}',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "episode_calendar.series_config.get_settings",
        lambda: SimpleNamespace(series_config_path=str(config_path)),
    )

    assert configured_series("joyn") == ("demo", "second")


def test_configured_series_display_flags(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "series.json"
    config_path.write_text(
        '{"platforms": [{"id": "joyn", "name": "Joyn.at", "display": false, '
        '"series": [{"id": "hidden-platform"}]}, '
        '{"id": "rtlplus", "name": "RTL+", "series": '
        '[{"id": "shown"}, {"id": "hidden", "display": false}]}]}',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "episode_calendar.series_config.get_settings",
        lambda: SimpleNamespace(series_config_path=str(config_path)),
    )

    assert displayable_series_by_platform() == {"rtlplus": {"shown"}}


def test_displayable_series_keeps_legacy_ids_when_all_series_are_visible(
    tmp_path, monkeypatch
) -> None:
    config_path = tmp_path / "series.json"
    config_path.write_text(
        '{"platforms": [{"id": "joyn", "name": "Joyn.at", "series": [{"id": "configured-slug"}]}]}',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "episode_calendar.series_config.get_settings",
        lambda: SimpleNamespace(series_config_path=str(config_path)),
    )

    assert displayable_series_by_platform() == {"joyn": None}


def test_configured_bbc_series_accepts_labeled_ids(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "series.json"
    config_path.write_text(
        '{"platforms": [{"id": "bbc_iplayer", "name": "BBC iPlayer", "series": '
        '[{"id": " m1234567 ", "comment": "Demo"}]}]}',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "episode_calendar.series_config.get_settings",
        lambda: SimpleNamespace(series_config_path=str(config_path)),
    )

    assert configured_series("bbc_iplayer") == ("m1234567",)


@pytest.mark.asyncio
async def test_configured_imports_run_in_parallel_and_isolate_failures(monkeypatch, caplog) -> None:
    class Session:
        provider = None

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return None

        async def rollback(self) -> None:
            pass

        async def scalar(self, statement):
            return self.provider

        def add(self, provider) -> None:
            self.provider = provider

        async def commit(self) -> None:
            pass

    class SessionFactory:
        def __call__(self):
            return Session()

    active = 0
    maximum_active = 0

    async def fake_import_series(session, adapter, external_id, *, provider_name):
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        if external_id == "broken":
            raise RuntimeError("provider unavailable")
        return ImportResult(
            series=SimpleNamespace(title="Working"),
            seasons=1,
            episodes=2,
            new_episodes=1,
            releases=2,
        )

    monkeypatch.setattr(
        "episode_calendar.importer.configured_platform",
        lambda provider: SimpleNamespace(
            series=(("working", True, True), ("broken", True, True)),
            name="Test Provider",
            run_import=True,
        ),
    )
    monkeypatch.setattr("episode_calendar.importer.get_session_factory", lambda: SessionFactory())
    monkeypatch.setattr("episode_calendar.importer.import_series", fake_import_series)
    caplog.set_level(logging.INFO, logger="episode_calendar.importer")

    with pytest.raises(RuntimeError, match="1 of 2 Test Provider imports failed"):
        await import_configured("demo", lambda: SimpleNamespace(slug="demo"), "Test Provider")

    assert maximum_active == 2
    messages = [record.getMessage() for record in caplog.records]
    assert "Imported Test Provider/Working: 1 seasons, 2 episodes (1 new), 2 releases" in messages
    assert "Import failed for Test Provider/broken: provider unavailable" in messages


@pytest.mark.asyncio
async def test_import_skips_inactive_platform(monkeypatch, caplog) -> None:
    monkeypatch.setattr(
        "episode_calendar.importer.configured_platform",
        lambda provider: SimpleNamespace(
            name="Test Provider", run_import=False, series=(("ignored", True, True),)
        ),
    )
    caplog.set_level(logging.INFO, logger="episode_calendar.importer")

    await import_configured("demo", lambda: SimpleNamespace(slug="demo"), "Unused Name")

    assert "Skipped provider Test Provider/demo: run_import=false" in [
        record.getMessage() for record in caplog.records
    ]

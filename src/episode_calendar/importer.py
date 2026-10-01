"""Import normalized provider data into the database."""

from __future__ import annotations

import argparse
import asyncio
import logging
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from episode_calendar.db.models import Episode, EpisodeRelease, Provider, Season, Series
from episode_calendar.db.session import get_session_factory
from episode_calendar.providers.amazon_prime_de import AmazonPrimeDEProvider
from episode_calendar.providers.amazon_prime_uk import AmazonPrimeUKProvider
from episode_calendar.providers.ardmediathek import ARDMediathekProvider
from episode_calendar.providers.base import NormalizedSeries, ProviderAdapter
from episode_calendar.providers.bbc_iplayer import BBCIPlayerProvider
from episode_calendar.providers.channel4 import Channel4Provider
from episode_calendar.providers.joyn import JoynProvider
from episode_calendar.providers.rtlplus import RTLPlusProvider
from episode_calendar.providers.stv import STVProvider
from episode_calendar.series_config import configured_platform
from episode_calendar.series_config import configured_series as _configured_series

logger = logging.getLogger(__name__)

# Linear TV providers often publish the same episode again with a new broadcast date.
# Keep accepting corrections for currently running episodes, but do not replace old
# first-release data with a rerun. The fixed window also makes the policy predictable
# across all providers instead of duplicating provider-specific date heuristics.
KNOWN_EPISODE_RELEASE_MAX_AGE = timedelta(days=180)


def configured_series(provider: str) -> tuple[str, ...]:
    """Compatibility wrapper for callers importing this helper from the importer."""

    return _configured_series(provider)


@dataclass(frozen=True)
class ImportResult:
    series: Series
    seasons: int
    episodes: int
    new_episodes: int
    releases: int


@dataclass(frozen=True)
class RerunDetection:
    """Episodes identified as repeat broadcasts by the central import policy."""

    episode_keys: frozenset[tuple[str, str, int | None]]
    existing_episode_ids: frozenset[UUID]


def detect_reruns(
    normalized: NormalizedSeries,
    *,
    newest_existing_season: int | None,
    highest_existing_episode: Mapping[str, int],
    existing_episode_ids: Mapping[str, Mapping[tuple[str, int | None], UUID]],
    existing_release_dates: Mapping[UUID, datetime],
    reference_time: datetime,
) -> RerunDetection:
    """Classify provider releases that should be stored as reruns.

    This policy is deliberately centralized because catalog providers can expose old
    seasons and episodes again as upcoming TV broadcasts. Existing recent episodes
    remain updateable for provider corrections; old or superseded episodes get a new
    ``rerun`` release instead of replacing their first-release data.
    """

    rerun_episode_keys: set[tuple[str, str, int | None]] = set()
    rerun_episode_ids: set[UUID] = set()
    for normalized_season in normalized.seasons:
        season_is_obsolete = (
            newest_existing_season is not None
            and normalized_season.number is not None
            and normalized_season.number < newest_existing_season
        )
        for normalized_episode in normalized_season.episodes:
            episode_key = (
                normalized_season.external_id,
                normalized_episode.external_id,
                normalized_episode.number,
            )
            episode_id = existing_episode_ids.get(normalized_season.external_id, {}).get(
                episode_key[1:]
            )
            release_at = existing_release_dates.get(episode_id) if episode_id else None
            higher_episode_exists = (
                normalized_episode.number is not None
                and highest_existing_episode.get(normalized_season.external_id, 0)
                > normalized_episode.number
            )
            old_known_episode = (
                release_at is not None
                and reference_time - release_at > KNOWN_EPISODE_RELEASE_MAX_AGE
            )
            if (
                season_is_obsolete
                or (episode_id is None and higher_episode_exists)
                or old_known_episode
            ):
                rerun_episode_keys.add(episode_key)
                if episode_id:
                    rerun_episode_ids.add(episode_id)
    return RerunDetection(
        episode_keys=frozenset(rerun_episode_keys),
        existing_episode_ids=frozenset(rerun_episode_ids),
    )


async def import_series(
    session: AsyncSession,
    adapter: ProviderAdapter,
    external_id: str,
    *,
    provider_name: str | None = None,
    reference_time: datetime | None = None,
) -> ImportResult:
    """Fetch one complete series and idempotently upsert its normalized tree.

    ``reference_time`` is injectable for deterministic tests and diagnostics; normal
    imports use the current UTC time.
    """

    normalized = await adapter.fetch_series(external_id)
    provider = await session.scalar(select(Provider).where(Provider.slug == adapter.slug))
    if provider is None:
        provider = Provider(slug=adapter.slug, name=provider_name or adapter.slug.title())
        session.add(provider)
        await session.flush()
    elif provider_name:
        provider.name = provider_name

    series = await session.scalar(
        select(Series).where(
            Series.provider_id == provider.id,
            Series.external_id.in_((external_id, normalized.external_id)),
        )
    )
    if series is None:
        series = Series(
            provider=provider,
            external_id=external_id,
            title=normalized.title,
            description=normalized.description,
        )
        session.add(series)
        await session.flush()
    else:
        series.external_id = external_id
        series.title = normalized.title
        series.description = normalized.description

    existing_seasons = list(
        await session.scalars(select(Season).where(Season.series_id == series.id))
    )
    newest_existing_season = max(
        (season.number for season in existing_seasons if season.number is not None),
        default=None,
    )
    existing_episode_ids: dict[str, dict[tuple[str, int | None], object]] = defaultdict(dict)
    highest_existing_episode: dict[str, int] = {}
    episode_rows = await session.execute(
        select(Season.external_id, Episode.external_id, Episode.number, Episode.id)
        .join(Episode, Episode.season_id == Season.id)
        .where(Season.series_id == series.id)
    )
    for season_external_id, episode_external_id, episode_number, episode_id in episode_rows:
        existing_episode_ids[season_external_id][(episode_external_id, episode_number)] = episode_id
        if episode_number is not None:
            highest_existing_episode[season_external_id] = max(
                highest_existing_episode.get(season_external_id, episode_number),
                episode_number,
            )

    existing_release_dates: dict[UUID, datetime] = {}
    release_rows = await session.execute(
        select(Episode.id, func.max(EpisodeRelease.release_at))
        .join(EpisodeRelease, EpisodeRelease.episode_id == Episode.id)
        .where(EpisodeRelease.provider_id == provider.id)
        .group_by(Episode.id)
    )
    for episode_id, release_at in release_rows:
        if release_at is not None:
            existing_release_dates[episode_id] = release_at
    import_time = (reference_time or datetime.now(UTC)).astimezone(UTC)

    imported_seasons = normalized.seasons
    obsolete_seasons = tuple(
        season
        for season in normalized.seasons
        if newest_existing_season is not None
        and season.number is not None
        and season.number < newest_existing_season
    )
    if obsolete_seasons:
        logger.info(
            "Marked %s rerun season(s) for %s/%s; newer season %s already exists",
            len(obsolete_seasons),
            provider_name or adapter.slug,
            external_id,
            newest_existing_season,
        )

    reruns = detect_reruns(
        normalized,
        newest_existing_season=newest_existing_season,
        highest_existing_episode=highest_existing_episode,
        existing_episode_ids=existing_episode_ids,
        existing_release_dates=existing_release_dates,
        reference_time=import_time,
    )

    # Preserve first-release records while allowing the provider to add a separate rerun
    # release. Non-rerun episodes still replace their previous releases so removed provider
    # data does not remain in the calendar.
    imported_season_ids = select(Season.id).where(
        Season.series_id == series.id,
        Season.external_id.in_(season.external_id for season in normalized.seasons),
    )
    episode_ids = select(Episode.id).where(
        Episode.season_id.in_(imported_season_ids),
        Episode.id.not_in(reruns.existing_episode_ids),
    )
    await session.execute(
        delete(EpisodeRelease).where(
            EpisodeRelease.provider_id == provider.id,
            EpisodeRelease.episode_id.in_(episode_ids),
        )
    )

    season_count = episode_count = new_episode_count = release_count = 0
    for normalized_season in imported_seasons:
        season = await session.scalar(
            select(Season).where(
                Season.series_id == series.id,
                Season.external_id == normalized_season.external_id,
            )
        )
        if season is None:
            season = Season(
                series_id=series.id,
                external_id=normalized_season.external_id,
                number=normalized_season.number,
                title=normalized_season.title,
            )
            session.add(season)
            await session.flush()
        else:
            season.number = normalized_season.number
            season.title = normalized_season.title
        season_count += 1

        for normalized_episode in normalized_season.episodes:
            episode_key = (
                normalized_season.external_id,
                normalized_episode.external_id,
                normalized_episode.number,
            )
            is_rerun = episode_key in reruns.episode_keys
            episode = await session.scalar(
                select(Episode).where(
                    Episode.season_id == season.id,
                    Episode.external_id == normalized_episode.external_id,
                )
            )
            if episode is None:
                episode = Episode(
                    season_id=season.id,
                    external_id=normalized_episode.external_id,
                    number=normalized_episode.number,
                    title=normalized_episode.title,
                    description=normalized_episode.description,
                )
                session.add(episode)
                await session.flush()
                new_episode_count += 1
            else:
                episode.number = normalized_episode.number
                episode.title = normalized_episode.title
                episode.description = normalized_episode.description
            episode_count += 1

            for normalized_release in normalized_episode.releases:
                release = None
                if normalized_release.external_id:
                    release = await session.scalar(
                        select(EpisodeRelease).where(
                            EpisodeRelease.provider_id == provider.id,
                            EpisodeRelease.external_id == normalized_release.external_id,
                        )
                    )
                if release is None:
                    release = await session.scalar(
                        select(EpisodeRelease).where(
                            EpisodeRelease.episode_id == episode.id,
                            EpisodeRelease.provider_id == provider.id,
                            EpisodeRelease.release_type == normalized_release.release_type,
                            EpisodeRelease.release_at == normalized_release.release_at,
                        )
                    )
                if release is None:
                    release = EpisodeRelease(
                        episode_id=episode.id,
                        provider_id=provider.id,
                        external_id=normalized_release.external_id,
                        release_type=normalized_release.release_type,
                        release_at=normalized_release.release_at,
                        available_until=normalized_release.available_until,
                        url=str(normalized_release.url) if normalized_release.url else None,
                        rerun=is_rerun,
                    )
                    session.add(release)
                else:
                    release.available_until = normalized_release.available_until
                    release.url = str(normalized_release.url) if normalized_release.url else None
                release_count += 1

    await session.commit()
    return ImportResult(
        series=series,
        seasons=season_count,
        episodes=episode_count,
        new_episodes=new_episode_count,
        releases=release_count,
    )


async def import_configured_joyn() -> None:
    await import_configured("joyn", JoynProvider)


async def import_configured_rtlplus() -> None:
    await import_configured("rtlplus", RTLPlusProvider)


async def import_configured_bbc_iplayer() -> None:
    await import_configured("bbc_iplayer", BBCIPlayerProvider)


async def import_configured_channel4() -> None:
    await import_configured("channel4", Channel4Provider)


async def import_configured_ardmediathek() -> None:
    await import_configured("ardmediathek", ARDMediathekProvider)


async def import_configured_stv() -> None:
    await import_configured("stv", STVProvider)


async def import_configured_amazon_prime_de() -> None:
    await import_configured("amazon_prime_de", AmazonPrimeDEProvider)


async def import_configured_amazon_prime_uk() -> None:
    await import_configured("amazon_prime_uk", AmazonPrimeUKProvider)


async def import_configured(
    provider_slug: str,
    adapter_factory: type[ProviderAdapter],
    provider_name: str | None = None,
) -> None:
    """Import every configured series, reporting failures without aborting the batch."""
    platform = configured_platform(provider_slug)
    if not platform.run_import:
        logger.info("Skipped provider %s/%s: run_import=false", platform.name, provider_slug)
        return
    identifiers = tuple(identifier for identifier, run_import, _ in platform.series if run_import)
    for identifier, run_import, _ in platform.series:
        if not run_import:
            logger.info("Skipped series %s/%s: run_import=false", platform.name, identifier)
    if not identifiers:
        logger.info(
            "Skipped provider %s/%s: no active series configured", platform.name, provider_slug
        )
        return
    provider_name = provider_name or platform.name
    await _ensure_provider(provider_slug, provider_name)
    results = await asyncio.gather(
        *(
            _import_identifier(provider_name, adapter_factory, identifier)
            for identifier in identifiers
        )
    )
    failed = len(identifiers) - sum(results)
    if failed:
        raise RuntimeError(f"{failed} of {len(identifiers)} {provider_name} imports failed")


async def _ensure_provider(provider_slug: str, provider_name: str) -> None:
    """Create the shared provider row before series imports run concurrently."""

    async with get_session_factory()() as session:
        provider = await session.scalar(select(Provider).where(Provider.slug == provider_slug))
        if provider is None:
            session.add(Provider(slug=provider_slug, name=provider_name))
        else:
            provider.name = provider_name
        await session.commit()


async def _import_identifier(
    provider_name: str,
    adapter_factory: type[ProviderAdapter],
    identifier: str,
) -> bool:
    """Import one series in an isolated session so sibling imports can continue."""

    try:
        adapter = adapter_factory()
        async with get_session_factory()() as session:
            try:
                result = await import_series(
                    session, adapter, identifier, provider_name=provider_name
                )
            except Exception as exc:  # one series must not hide the other imports
                await session.rollback()
                logger.error("Import failed for %s/%s: %s", provider_name, identifier, exc)
                return False
    except Exception as exc:  # setup failures should be reported like provider failures
        logger.error("Import failed for %s/%s: %s", provider_name, identifier, exc)
        return False

    logger.info(
        "Imported %s/%s: %s seasons, %s episodes (%s new), %s releases",
        provider_name,
        result.series.title,
        result.seasons,
        result.episodes,
        result.new_episodes,
        result.releases,
    )
    return True


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Import configured provider catalogs")
    parser.add_argument(
        "provider",
        choices=(
            "joyn",
            "rtlplus",
            "bbc_iplayer",
            "channel4",
            "ardmediathek",
            "stv",
            "amazon_prime_de",
            "amazon_prime_uk",
            "all",
        ),
    )
    args = parser.parse_args()
    if args.provider == "joyn":
        asyncio.run(import_configured_joyn())
    elif args.provider == "rtlplus":
        asyncio.run(import_configured_rtlplus())
    elif args.provider == "bbc_iplayer":
        asyncio.run(import_configured_bbc_iplayer())
    elif args.provider == "channel4":
        asyncio.run(import_configured_channel4())
    elif args.provider == "ardmediathek":
        asyncio.run(import_configured_ardmediathek())
    elif args.provider == "stv":
        asyncio.run(import_configured_stv())
    elif args.provider == "amazon_prime_de":
        asyncio.run(import_configured_amazon_prime_de())
    elif args.provider == "amazon_prime_uk":
        asyncio.run(import_configured_amazon_prime_uk())
    else:

        async def run_all() -> None:
            providers = (
                ("joyn", JoynProvider),
                ("rtlplus", RTLPlusProvider),
                ("bbc_iplayer", BBCIPlayerProvider),
                ("channel4", Channel4Provider),
                ("ardmediathek", ARDMediathekProvider),
                ("stv", STVProvider),
                ("amazon_prime_de", AmazonPrimeDEProvider),
                ("amazon_prime_uk", AmazonPrimeUKProvider),
            )
            results = await asyncio.gather(
                *(
                    import_configured(provider_slug, factory)
                    for provider_slug, factory in providers
                ),
                return_exceptions=True,
            )
            failures = [result for result in results if isinstance(result, Exception)]
            for (provider_slug, _), result in zip(providers, results, strict=True):
                if isinstance(result, Exception):
                    provider_name = provider_slug
                    try:
                        provider_name = configured_platform(provider_slug).name
                    except RuntimeError:
                        pass
                    logger.error("Provider import failed for %s: %s", provider_name, result)
            if failures:
                raise RuntimeError("one or more provider imports failed") from failures[0]

        asyncio.run(run_all())


if __name__ == "__main__":
    main()

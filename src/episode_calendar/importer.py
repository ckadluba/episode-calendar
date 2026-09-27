"""Import normalized provider data into the database."""

from __future__ import annotations

import argparse
import asyncio
import logging
from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from episode_calendar.db.models import Episode, EpisodeRelease, Provider, Season, Series
from episode_calendar.db.session import get_session_factory
from episode_calendar.providers.base import ProviderAdapter
from episode_calendar.providers.bbc_iplayer import BBCIPlayerProvider
from episode_calendar.providers.channel4 import Channel4Provider
from episode_calendar.providers.itvx import ITVXProvider
from episode_calendar.providers.joyn import JoynProvider
from episode_calendar.providers.rtlplus import RTLPlusProvider
from episode_calendar.series_config import configured_platform
from episode_calendar.series_config import configured_series as _configured_series

logger = logging.getLogger(__name__)


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


async def import_series(
    session: AsyncSession,
    adapter: ProviderAdapter,
    external_id: str,
    *,
    provider_name: str | None = None,
) -> ImportResult:
    """Fetch one complete series and idempotently upsert its normalized tree."""

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

    # A provider response is the source of truth for releases. Remove releases from
    # the previous import first so obsolete inferred dates cannot remain in the calendar.
    episode_ids = select(Episode.id).join(Season).where(Season.series_id == series.id)
    await session.execute(
        delete(EpisodeRelease).where(
            EpisodeRelease.provider_id == provider.id,
            EpisodeRelease.episode_id.in_(episode_ids),
        )
    )

    season_count = episode_count = new_episode_count = release_count = 0
    for normalized_season in normalized.seasons:
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


async def import_configured_itvx() -> None:
    await import_configured("itvx", ITVXProvider)


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
    results = await asyncio.gather(
        *(
            _import_identifier(provider_name, adapter_factory, identifier)
            for identifier in identifiers
        )
    )
    failed = len(identifiers) - sum(results)
    if failed:
        raise RuntimeError(f"{failed} of {len(identifiers)} {provider_name} imports failed")


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
        "provider", choices=("joyn", "rtlplus", "bbc_iplayer", "channel4", "itvx", "all")
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
    elif args.provider == "itvx":
        asyncio.run(import_configured_itvx())
    else:

        async def run_all() -> None:
            providers = (
                ("joyn", JoynProvider),
                ("rtlplus", RTLPlusProvider),
                ("bbc_iplayer", BBCIPlayerProvider),
                ("channel4", Channel4Provider),
                ("itvx", ITVXProvider),
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

"""Import normalized provider data into the database."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from episode_calendar.config import get_settings
from episode_calendar.db.models import Episode, EpisodeRelease, Provider, Season, Series
from episode_calendar.db.session import get_session_factory
from episode_calendar.providers.base import ProviderAdapter
from episode_calendar.providers.bbc_iplayer import BBCIPlayerProvider
from episode_calendar.providers.channel4 import Channel4Provider
from episode_calendar.providers.itvx import ITVXProvider
from episode_calendar.providers.joyn import JoynProvider
from episode_calendar.providers.rtlplus import RTLPlusProvider

logger = logging.getLogger(__name__)


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

    series = await session.scalar(
        select(Series).where(
            Series.provider_id == provider.id,
            Series.external_id == normalized.external_id,
        )
    )
    if series is None:
        series = Series(
            provider=provider,
            external_id=normalized.external_id,
            title=normalized.title,
            description=normalized.description,
        )
        session.add(series)
        await session.flush()
    else:
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


def configured_series(provider: str) -> tuple[str, ...]:
    """Load non-empty series identifiers for a provider from the JSON config file."""

    path = Path(get_settings().series_config_path)
    try:
        with path.open(encoding="utf-8") as config_file:
            config = json.load(config_file)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not read series configuration {path}") from exc
    if not isinstance(config, dict):
        raise RuntimeError(f"Series configuration {path} must contain a JSON object")
    identifiers = config.get(provider, [])
    if not isinstance(identifiers, list):
        raise RuntimeError(f"Series configuration entry {provider!r} must be a list")
    normalized: list[str] = []
    for identifier in identifiers:
        if isinstance(identifier, str):
            value = identifier.strip()
        elif isinstance(identifier, dict):
            value = identifier.get("id", "")
            if not isinstance(value, str):
                raise RuntimeError(
                    f"Series configuration entry {provider!r} objects must contain a string id"
                )
            value = value.strip()
        else:
            raise RuntimeError(
                f"Series configuration entry {provider!r} must contain objects with string ids"
            )
        if value:
            normalized.append(value)
    return tuple(normalized)


async def import_configured_joyn() -> None:
    await import_configured("joyn", JoynProvider, "Joyn Austria")


async def import_configured_rtlplus() -> None:
    await import_configured("rtlplus", RTLPlusProvider, "RTL+")


async def import_configured_bbc_iplayer() -> None:
    await import_configured("bbc_iplayer", BBCIPlayerProvider, "BBC iPlayer")


async def import_configured_channel4() -> None:
    await import_configured("channel4", Channel4Provider, "Channel 4")


async def import_configured_itvx() -> None:
    await import_configured("itvx", ITVXProvider, "ITVX")


async def import_configured(
    provider_slug: str, adapter_factory: type[ProviderAdapter], provider_name: str
) -> None:
    """Import every configured series, reporting failures without aborting the batch."""
    identifiers = configured_series(provider_slug)
    if not identifiers:
        raise RuntimeError(f"No {provider_slug} series configured in the series JSON file")
    failed = 0
    adapter = adapter_factory()
    settings = get_settings()
    async with get_session_factory()() as session:
        for identifier in identifiers:
            if identifier != identifiers[0] and settings.import_delay_seconds > 0:
                await asyncio.sleep(settings.import_delay_seconds)
            try:
                result = await import_series(
                    session, adapter, identifier, provider_name=provider_name
                )
            except Exception as exc:  # provider failures must not hide later series
                await session.rollback()
                failed += 1
                logger.error("Import failed for %s/%s: %s", provider_slug, identifier, exc)
                print(f"Failed {provider_slug}/{identifier}: {exc}")
                continue
            print(
                f"Imported {result.series.title}: {result.seasons} seasons, "
                f"{result.episodes} episodes ({result.new_episodes} new), "
                f"{result.releases} releases"
            )
    if failed:
        raise RuntimeError(f"{failed} of {len(identifiers)} {provider_slug} imports failed")


def main() -> None:
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
            failures: list[Exception] = []
            for provider_slug, factory, name in (
                ("joyn", JoynProvider, "Joyn Austria"),
                ("rtlplus", RTLPlusProvider, "RTL+"),
                ("bbc_iplayer", BBCIPlayerProvider, "BBC iPlayer"),
                ("channel4", Channel4Provider, "Channel 4"),
                ("itvx", ITVXProvider, "ITVX"),
            ):
                try:
                    await import_configured(provider_slug, factory, name)
                except Exception as exc:
                    failures.append(exc)
            if failures:
                raise RuntimeError("one or more provider imports failed") from failures[0]

        asyncio.run(run_all())


if __name__ == "__main__":
    main()

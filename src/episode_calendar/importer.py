"""Import normalized provider data into the database."""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from episode_calendar.config import get_settings
from episode_calendar.db.models import Episode, EpisodeRelease, Provider, Season, Series
from episode_calendar.db.session import get_session_factory
from episode_calendar.providers.base import ProviderAdapter
from episode_calendar.providers.joyn import JoynProvider
from episode_calendar.providers.rtlplus import RTLPlusProvider


@dataclass(frozen=True)
class ImportResult:
    series: Series
    seasons: int
    episodes: int
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

    season_count = episode_count = release_count = 0
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
            else:
                episode.number = normalized_episode.number
                episode.title = normalized_episode.title
                episode.description = normalized_episode.description
            episode_count += 1

            for normalized_release in normalized_episode.releases:
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
        series=series, seasons=season_count, episodes=episode_count, releases=release_count
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
    if not isinstance(identifiers, list) or not all(
        isinstance(identifier, str) for identifier in identifiers
    ):
        raise RuntimeError(f"Series configuration entry {provider!r} must be a list of strings")
    return tuple(identifier.strip() for identifier in identifiers if identifier.strip())


async def import_configured_joyn() -> None:
    identifiers = configured_series("joyn")
    if not identifiers:
        raise RuntimeError("No Joyn series configured in the series JSON file")
    async with get_session_factory()() as session:
        for identifier in identifiers:
            result = await import_series(
                session, JoynProvider(), identifier, provider_name="Joyn Austria"
            )
            print(
                f"Imported {result.series.title}: {result.seasons} seasons, "
                f"{result.episodes} episodes, {result.releases} releases"
            )


async def import_configured_rtlplus() -> None:
    identifiers = configured_series("rtlplus")
    if not identifiers:
        raise RuntimeError("No RTL+ series configured in the series JSON file")
    async with get_session_factory()() as session:
        for identifier in identifiers:
            result = await import_series(
                session, RTLPlusProvider(), identifier, provider_name="RTL+"
            )
            print(
                f"Imported {result.series.title}: {result.seasons} seasons, "
                f"{result.episodes} episodes, {result.releases} releases"
            )


def main() -> None:
    parser = argparse.ArgumentParser(description="Import configured provider catalogs")
    parser.add_argument("provider", choices=("joyn", "rtlplus"))
    args = parser.parse_args()
    if args.provider == "joyn":
        asyncio.run(import_configured_joyn())
    elif args.provider == "rtlplus":
        asyncio.run(import_configured_rtlplus())


if __name__ == "__main__":
    main()

"""Import normalized provider data into the database."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from episode_calendar.importer.providers.base import (
    NormalizedEpisode,
    NormalizedEpisodeRelease,
    NormalizedSeason,
    NormalizedSeries,
    ProviderAdapter,
)
from episode_calendar.importer.providers.http import is_resource_gone
from episode_calendar.shared.db.models import Episode, EpisodeRelease, Provider, Season, Series
from episode_calendar.shared.db.session import get_session_factory
from episode_calendar.shared.domain import TV_BROADCAST_MATCH_TOLERANCE, ReleaseType
from episode_calendar.shared.series_config import configured_platform, platform_has_prereleases
from episode_calendar.shared.series_config import configured_series as _configured_series

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


@dataclass(frozen=True)
class RerunDetection:
    """Broadcast releases identified as repeat airings by the central import policy."""

    rerun_release_keys: frozenset[tuple[str, str, str | None]]


ExistingRelease = tuple[ReleaseType, datetime, str | None, bool, bool]


# A catalogue drop that precedes the episode's own broadcast by at least a day is an
# early on-demand release; a same-day drop that only leads the broadcast by minutes is
# the regular release cadence, never a preview. The upper bound keeps a platform whose
# regular model is streaming long before TV from being reclassified wholesale.
PREVIEW_MIN_LEAD = timedelta(days=1)
PREVIEW_MAX_LEAD = timedelta(days=14)


def _leads_broadcast(drop: datetime, broadcast: datetime) -> bool:
    """Whether an on-demand drop is the early release of that broadcast.

    Such a drop announces the airing instead of making the episode available long before
    it, so the broadcast is that episode's premiere and not a repeat. Both the preview
    marking and the rerun classification rely on this window.
    """

    return PREVIEW_MIN_LEAD <= broadcast - drop <= PREVIEW_MAX_LEAD


def detect_reruns(
    normalized: NormalizedSeries,
    *,
    existing_episode_ids: Mapping[str, Mapping[tuple[str, int | None], UUID]],
    existing_releases: Mapping[UUID, tuple[ExistingRelease, ...]],
) -> RerunDetection:
    """Classify linear broadcasts that repeat an earlier airing of the same episode.

    Only the schedule can make a broadcast a rerun: it repeats an episode that already
    has an earlier release, either in this payload or stored from an earlier import.
    Catalogue structure (season or episode numbering, or a re-listed old season) never
    classifies anything as a rerun. A catalogue correction (the provider republishes the
    episode's own release with a new date, keeping its identifier) is an update, not a
    repeat; only a genuinely additional airing is classified here. An on-demand drop that
    leads the broadcast within the preview window announces that premiere instead of
    making the episode available earlier, so the broadcast stays the regular airing.
    Releases that are themselves already reruns are ignored, so a repeat never turns an
    episode's regular airing into another rerun.
    """

    rerun_release_keys: set[tuple[str, str, str | None]] = set()
    for normalized_season in normalized.seasons:
        for normalized_episode in normalized_season.episodes:
            episode_id = existing_episode_ids.get(normalized_season.external_id, {}).get(
                (normalized_episode.external_id, normalized_episode.number)
            )
            stored = existing_releases.get(episode_id, ()) if episode_id else ()
            for release in normalized_episode.releases:
                if release.release_type is not ReleaseType.TV_BROADCAST:
                    continue
                earlier_releases = [
                    (release_type, release_at, external_id)
                    for release_type, release_at, external_id, preview, rerun in stored
                    if not preview and not rerun
                ]
                earlier_releases.extend(
                    (other.release_type, other.release_at, other.external_id)
                    for other in normalized_episode.releases
                    if other is not release and not other.preview and not other.rerun
                )
                earlier_catalog = any(
                    release_type is not ReleaseType.TV_BROADCAST
                    and release_at < release.release_at
                    and not _leads_broadcast(release_at, release.release_at)
                    for release_type, release_at, _ in earlier_releases
                )
                earlier_other_airing = any(
                    release_type is ReleaseType.TV_BROADCAST
                    and release_at < release.release_at
                    and external_id != release.external_id
                    for release_type, release_at, external_id in earlier_releases
                )
                if earlier_catalog or earlier_other_airing:
                    rerun_release_keys.add(
                        (
                            normalized_season.external_id,
                            normalized_episode.external_id,
                            release.external_id,
                        )
                    )
    return RerunDetection(rerun_release_keys=frozenset(rerun_release_keys))


def detect_previews(
    normalized: NormalizedSeries, *, now: datetime | None = None
) -> NormalizedSeries:
    """Flag catalogue drops that precede the episode's own linear broadcast.

    Some platforms (Joyn, RTL+) publish an episode on demand before its linear airing
    without leaving a preview marking in the catalogue. The broadcast merged into the
    episode anchors its regular date: a streaming release between ``PREVIEW_MIN_LEAD``
    and ``PREVIEW_MAX_LEAD`` ahead of the episode's earliest broadcast is an early
    release, a preview. Providers that carry their own marking already set ``preview``;
    this provider-independent policy only fills that flag in. Callers gate on the
    platform's prerelease flag and run this before rerun detection, so a fresh preview
    never turns its own broadcast into a rerun.

    A preview announces an episode that has not aired yet, so a broadcast that is no
    longer in the future never flags its earlier drop: the release has already happened
    and is a regular (early) release, not an upcoming preview.

    The same reasoning clears a provider rerun flag: an airing cannot repeat an episode
    that has never aired, so when the tree knows no regular airing of the episode, the
    earliest broadcast after an early drop is its regular premiere even when the provider
    attached it as an off-slot rerun (for example a numberless EPG event). That clearing
    stays independent of the preview window and therefore also applies once the premiere
    has aired; otherwise an already-aired premiere would stay flagged as a rerun. A
    broadcast the provider already left unflagged is the known premiere and nothing is
    cleared; later airings keep their rerun flag.
    """

    seasons: list[NormalizedSeason] = []
    changed = False
    for season in normalized.seasons:
        episodes: list[NormalizedEpisode] = []
        for episode in season.episodes:
            broadcasts = [
                release.release_at
                for release in episode.releases
                if release.release_type is ReleaseType.TV_BROADCAST and not release.placeholder
            ]
            if not broadcasts:
                episodes.append(episode)
                continue
            first_broadcast = min(broadcasts)
            releases = tuple(
                release.model_copy(update={"preview": True})
                if (
                    release.release_type is ReleaseType.STREAMING
                    and not release.preview
                    and not release.placeholder
                    and _leads_broadcast(release.release_at, first_broadcast)
                    and (now is None or first_broadcast > now)
                )
                else release
                for release in episode.releases
            )
            # The rerun policy stays independent of the preview window: a drop that leads
            # its episode's premiere marks that first airing as the premiere even after the
            # premiere has aired. Gating this on ``now`` would leave already-aired premieres
            # flagged as reruns of an episode that never had a regular airing.
            preview_drops = [
                release.release_at
                for release in episode.releases
                if release.release_type is ReleaseType.STREAMING
                and not release.placeholder
                and (release.preview or _leads_broadcast(release.release_at, first_broadcast))
            ]
            has_regular_airing = any(
                release.release_type is ReleaseType.TV_BROADCAST and not release.rerun
                for release in episode.releases
            )
            if preview_drops and not has_regular_airing:
                premiere = min(
                    (broadcast for broadcast in broadcasts if broadcast > min(preview_drops)),
                    default=None,
                )
                if premiere is not None:
                    releases = tuple(
                        release.model_copy(update={"rerun": False})
                        if (
                            release.release_type is ReleaseType.TV_BROADCAST
                            and release.rerun
                            and release.release_at == premiere
                        )
                        else release
                        for release in releases
                    )
            if releases != episode.releases:
                changed = True
                episode = episode.model_copy(update={"releases": releases})
            episodes.append(episode)
        seasons.append(season.model_copy(update={"episodes": tuple(episodes)}))
    if not changed:
        return normalized
    return normalized.model_copy(update={"seasons": tuple(seasons)})


def _effective_releases(
    releases: tuple[NormalizedEpisodeRelease, ...],
) -> tuple[NormalizedEpisodeRelease, ...]:
    """Let a known broadcast date replace a first-seen catalogue placeholder.

    A catalogue episode that has no provider date is stored at its discovery time with
    ``date_from_api`` cleared. When the linear programme later supplies the real airing
    date it replaces that placeholder instead of appearing as a second release.
    """

    catalog = [release for release in releases if release.release_type is ReleaseType.STREAMING]
    broadcasts = sorted(
        (release for release in releases if release.release_type is ReleaseType.TV_BROADCAST),
        key=lambda release: release.release_at,
    )
    placeholder = next((release for release in catalog if release.placeholder), None)
    if placeholder is None or not broadcasts:
        return releases
    broadcast = broadcasts.pop(0)
    catalog = [
        release.model_copy(
            update={
                "release_at": broadcast.release_at,
                "available_until": release.available_until or broadcast.available_until,
                "url": release.url or broadcast.url,
                "date_from_api": True,
                "placeholder": False,
            }
        )
        if release is placeholder
        else release
        for release in catalog
    ]
    return (*catalog, *broadcasts)


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
    # Prerelease handling only applies to platforms that publish prereleases (for example
    # RTL+ or Joyn streaming before the linear airing). For every other platform the
    # preview flag is forced off so no stale prerelease detection can leak into the data.
    allow_previews = platform_has_prereleases(adapter.slug)
    if allow_previews:
        normalized = detect_previews(normalized, now=reference_time or datetime.now(UTC))
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
    existing_episode_ids: dict[str, dict[tuple[str, int | None], object]] = defaultdict(dict)
    # A numberless EPG broadcast that later becomes a real catalogue episode is first stored
    # under a synthetic identifier. Matching by season and episode number promotes that row
    # to the real identifier instead of creating a duplicate episode.
    existing_episode_by_number: dict[tuple[str, int], object] = {}
    episode_rows = await session.execute(
        select(Season.external_id, Episode.external_id, Episode.number, Episode.id)
        .join(Episode, Episode.season_id == Season.id)
        .where(Season.series_id == series.id)
    )
    for season_external_id, episode_external_id, episode_number, episode_id in episode_rows:
        existing_episode_ids[season_external_id][(episode_external_id, episode_number)] = episode_id
        if episode_number is not None:
            existing_episode_by_number.setdefault((season_external_id, episode_number), episode_id)

    existing_releases: dict[UUID, list[ExistingRelease]] = defaultdict(list)
    release_rows = await session.execute(
        select(
            EpisodeRelease.episode_id,
            EpisodeRelease.release_type,
            EpisodeRelease.release_at,
            EpisodeRelease.external_id,
            EpisodeRelease.preview,
            EpisodeRelease.rerun,
        )
        .join(Episode, Episode.id == EpisodeRelease.episode_id)
        .join(Season, Season.id == Episode.season_id)
        .where(
            EpisodeRelease.provider_id == provider.id,
            Season.series_id == series.id,
        )
    )
    for episode_id, release_type, release_at, external_id, preview, rerun in release_rows:
        existing_releases[episode_id].append(
            (release_type, release_at, external_id, preview, rerun)
        )
    # First-seen placeholder dates (a catalogue episode without a provider date) must
    # survive a reimport. Capture them before the releases are replaced below so the same
    # discovery timestamp is reused instead of being reset to the current time.
    first_seen_release_dates: dict[tuple[UUID, ReleaseType], datetime] = {}
    first_seen_rows = await session.execute(
        select(
            EpisodeRelease.episode_id,
            EpisodeRelease.release_type,
            EpisodeRelease.release_at,
        )
        .join(Episode, Episode.id == EpisodeRelease.episode_id)
        .join(Season, Season.id == Episode.season_id)
        .where(
            EpisodeRelease.provider_id == provider.id,
            EpisodeRelease.date_from_api.is_(False),
            Season.series_id == series.id,
        )
    )
    for episode_id, release_type, release_at in first_seen_rows:
        first_seen_release_dates[(episode_id, release_type)] = release_at

    imported_seasons = normalized.seasons

    reruns = detect_reruns(
        normalized,
        existing_episode_ids=existing_episode_ids,
        existing_releases=existing_releases,
    )

    # Preserve first-release records while allowing the provider to add a separate rerun
    # release. Non-rerun episodes still replace their previous releases so removed provider
    # data does not remain in the calendar.
    imported_season_ids = select(Season.id).where(
        Season.series_id == series.id,
        Season.external_id.in_(season.external_id for season in normalized.seasons),
    )
    # Keep stored releases for episodes the provider still lists but currently reports
    # without any release, so a sparse payload cannot wipe data.
    preserved_episode_ids: set[UUID] = set()
    for normalized_season in normalized.seasons:
        for normalized_episode in normalized_season.episodes:
            if normalized_episode.releases:
                continue
            episode_id = existing_episode_ids.get(normalized_season.external_id, {}).get(
                (normalized_episode.external_id, normalized_episode.number)
            )
            if episode_id is not None:
                preserved_episode_ids.add(episode_id)
    episode_ids = select(Episode.id).where(
        Episode.season_id.in_(imported_season_ids),
        Episode.id.not_in(preserved_episode_ids),
    )
    await session.execute(
        delete(EpisodeRelease).where(
            EpisodeRelease.provider_id == provider.id,
            EpisodeRelease.episode_id.in_(episode_ids),
        )
    )

    # Seasons that disappear from the provider payload are obsolete once a newer season
    # is present. Remove their releases so a stale release produced during a season
    # transition (for example a completed season stamped with the upcoming season's
    # diffusion date) does not linger in the calendar. Seasons that are still returned,
    # and a newer season temporarily reported alone, are left untouched.
    newest_imported_season = max(
        (season.number for season in normalized.seasons if season.number is not None),
        default=None,
    )
    if newest_imported_season is not None:
        returned_season_external_ids = {season.external_id for season in normalized.seasons}
        stale_season_ids = [
            season.id
            for season in existing_seasons
            if season.number is not None
            and season.number < newest_imported_season
            and season.external_id not in returned_season_external_ids
        ]
        if stale_season_ids:
            stale_episode_ids = select(Episode.id).where(Episode.season_id.in_(stale_season_ids))
            await session.execute(
                delete(EpisodeRelease).where(
                    EpisodeRelease.provider_id == provider.id,
                    EpisodeRelease.episode_id.in_(stale_episode_ids),
                )
            )
            logger.info(
                "Removed provider releases for %s obsolete season(s) of %s/%s",
                len(stale_season_ids),
                provider_name or adapter.slug,
                external_id,
            )

    # A completed season the provider can no longer date is stamped with its discovery
    # time, which would surface it in today's calendar. Once a higher season exists such a
    # season is obsolete: episodes that still have no provider date are skipped entirely
    # instead of being imported at the import-run time. A date that was merely derived
    # rather than read from the API (for example an RTL+ preview anchor) is a real date and
    # is kept, so only ``placeholder`` releases count as "no date found".
    existing_season_numbers = {
        season.number for season in existing_seasons if season.number is not None
    }
    imported_season_numbers = {
        season.number for season in normalized.seasons if season.number is not None
    }
    highest_season_number = max(existing_season_numbers | imported_season_numbers, default=None)
    skipped_episode_keys: set[tuple[str, str, int | None]] = set()
    if highest_season_number is not None:
        for normalized_season in normalized.seasons:
            if (
                normalized_season.number is None
                or normalized_season.number >= highest_season_number
            ):
                continue
            for normalized_episode in normalized_season.episodes:
                if normalized_episode.releases and all(
                    release.placeholder for release in normalized_episode.releases
                ):
                    skipped_episode_keys.add(
                        (
                            normalized_season.external_id,
                            normalized_episode.external_id,
                            normalized_episode.number,
                        )
                    )
    if skipped_episode_keys:
        logger.info(
            "Skipped %s undated episode(s) in obsolete season(s) of %s/%s",
            len(skipped_episode_keys),
            provider_name or adapter.slug,
            external_id,
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
            if (
                normalized_season.external_id,
                normalized_episode.external_id,
                normalized_episode.number,
            ) in skipped_episode_keys:
                continue
            episode = await session.scalar(
                select(Episode).where(
                    Episode.season_id == season.id,
                    Episode.external_id == normalized_episode.external_id,
                )
            )
            reconciled = False
            if episode is None and normalized_episode.number is not None:
                episode_id = existing_episode_by_number.get(
                    (normalized_season.external_id, normalized_episode.number)
                )
                if episode_id is not None:
                    episode = await session.get(Episode, episode_id)
                    reconciled = True
            if episode is None:
                episode = Episode(
                    season_id=season.id,
                    external_id=normalized_episode.external_id,
                    number=normalized_episode.number,
                    title=normalized_episode.title,
                    description=normalized_episode.description,
                    duration_minutes=normalized_episode.duration_minutes,
                )
                session.add(episode)
                await session.flush()
                new_episode_count += 1
            else:
                episode.number = normalized_episode.number
                episode.title = normalized_episode.title
                episode.description = normalized_episode.description
                episode.duration_minutes = normalized_episode.duration_minutes
                if reconciled:
                    episode.external_id = normalized_episode.external_id
            episode_count += 1
            effective_releases = _effective_releases(normalized_episode.releases)
            catalog_url = next(
                (
                    str(release.url)
                    for release in effective_releases
                    if release.release_type is ReleaseType.STREAMING and release.url
                ),
                None,
            )

            for normalized_release in effective_releases:
                # Reruns are a linear-TV concept. On-demand releases, including previews,
                # are never reruns.
                release_key = (
                    normalized_season.external_id,
                    normalized_episode.external_id,
                    normalized_release.external_id,
                )
                release_is_rerun = (
                    normalized_release.rerun or release_key in reruns.rerun_release_keys
                ) and normalized_release.release_type is ReleaseType.TV_BROADCAST
                release_at = normalized_release.release_at
                if normalized_release.placeholder:
                    stored_first_seen = first_seen_release_dates.get(
                        (episode.id, normalized_release.release_type)
                    )
                    if stored_first_seen is not None:
                        release_at = stored_first_seen
                release_url = str(normalized_release.url) if normalized_release.url else catalog_url
                if (
                    release_url is None
                    and normalized_release.release_type is ReleaseType.TV_BROADCAST
                ):
                    release_url = await session.scalar(
                        select(EpisodeRelease.url).where(
                            EpisodeRelease.episode_id == episode.id,
                            EpisodeRelease.provider_id == provider.id,
                            EpisodeRelease.release_type == ReleaseType.STREAMING,
                            EpisodeRelease.url.is_not(None),
                        )
                    )
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
                            EpisodeRelease.release_at == release_at,
                        )
                    )
                if release is None and normalized_release.release_type in {
                    ReleaseType.STREAMING,
                    ReleaseType.TV_BROADCAST,
                }:
                    release = await _promote_tv_broadcast_release(
                        session,
                        provider.id,
                        series.id,
                        episode.id,
                        normalized_release,
                    )
                if release is None:
                    release = EpisodeRelease(
                        episode_id=episode.id,
                        provider_id=provider.id,
                        external_id=normalized_release.external_id,
                        release_type=normalized_release.release_type,
                        release_at=release_at,
                        available_until=normalized_release.available_until,
                        url=release_url,
                        rerun=release_is_rerun,
                        preview=normalized_release.preview and allow_previews,
                        date_from_api=normalized_release.date_from_api,
                    )
                    session.add(release)
                else:
                    release.episode_id = episode.id
                    release.external_id = normalized_release.external_id
                    release.release_type = normalized_release.release_type
                    # A first-seen placeholder never overwrites an already known date;
                    # an API date always wins and also clears the placeholder marker.
                    if normalized_release.date_from_api:
                        release.release_at = normalized_release.release_at
                    release.available_until = normalized_release.available_until
                    if release_url is not None:
                        release.url = release_url
                    release.rerun = release_is_rerun
                    release.preview = normalized_release.preview and allow_previews
                    release.date_from_api = (
                        release.date_from_api or normalized_release.date_from_api
                    )
                release_count += 1

    await session.commit()
    return ImportResult(
        series=series,
        seasons=season_count,
        episodes=episode_count,
        new_episodes=new_episode_count,
        releases=release_count,
    )


async def _promote_tv_broadcast_release(
    session: AsyncSession,
    provider_id: UUID,
    series_id: UUID,
    episode_id: UUID,
    normalized_release: NormalizedEpisodeRelease,
) -> EpisodeRelease | None:
    """Replace an EPG broadcast with a catalog release at the same instant."""

    candidates = list(
        await session.scalars(
            select(EpisodeRelease)
            .join(Episode, Episode.id == EpisodeRelease.episode_id)
            .join(Season, Season.id == Episode.season_id)
            .where(
                EpisodeRelease.provider_id == provider_id,
                EpisodeRelease.release_type == ReleaseType.TV_BROADCAST,
                EpisodeRelease.release_at
                >= normalized_release.release_at - TV_BROADCAST_MATCH_TOLERANCE,
                EpisodeRelease.release_at
                <= normalized_release.release_at + TV_BROADCAST_MATCH_TOLERANCE,
                Season.series_id == series_id,
            )
        )
    )
    candidates = [
        candidate
        for candidate in candidates
        if candidate.release_at.date() == normalized_release.release_at.date()
    ]
    release = min(
        candidates,
        key=lambda candidate: abs(candidate.release_at - normalized_release.release_at),
        default=None,
    )
    if release is None or release.episode_id == episode_id:
        return release

    if normalized_release.release_type is ReleaseType.STREAMING:
        existing_catalog_release = await session.scalar(
            select(EpisodeRelease).where(
                EpisodeRelease.episode_id == episode_id,
                EpisodeRelease.provider_id == provider_id,
                EpisodeRelease.release_type == ReleaseType.STREAMING,
                EpisodeRelease.release_at == normalized_release.release_at,
            )
        )
        if existing_catalog_release is not None:
            await session.delete(release)
            return existing_catalog_release
    return release


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
    """Import one series in an isolated session so sibling imports can continue.

    Returns ``False`` only for a real failure; a series the provider removed (HTTP 410) is
    reported as a warning and counts as handled so it cannot fail the provider batch.
    """

    try:
        adapter = adapter_factory()
        async with get_session_factory()() as session:
            try:
                result = await import_series(
                    session, adapter, identifier, provider_name=provider_name
                )
            except Exception as exc:  # one series must not hide the other imports
                await session.rollback()
                return _report_series_failure(provider_name, identifier, exc)
    except Exception as exc:  # setup failures should be reported like provider failures
        return _report_series_failure(provider_name, identifier, exc)

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


def _report_series_failure(provider_name: str, identifier: str, exc: Exception) -> bool:
    """Log a failed series and report whether it counts against the provider batch.

    An HTTP 410 Gone is not a transient failure: the provider removed the series permanently,
    so it is only warned about and skipped — the configuration should drop the entry — instead
    of failing the whole provider import.
    """

    if is_resource_gone(exc):
        logger.warning(
            "Skipped %s/%s: the provider removed the series (HTTP 410): %s",
            provider_name,
            identifier,
            exc,
        )
        return True
    logger.error("Import failed for %s/%s: %s", provider_name, identifier, exc)
    return False

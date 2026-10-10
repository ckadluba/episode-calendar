"""Provider-independent catalogue policy and linear-broadcast merging.

The catalogue is always the base of a series tree: episodes carry the release
date/time the provider can read from the catalogue, and undated episodes are
stamped with a ``dateFromApi=false`` placeholder. The EPG is only supplementary:
it fills missing dates and announces episodes of running seasons, but never
overrides a real catalogue date. Providers differ in where they obtain the
recurring broadcast slots of a running season (Joyn reads them from the catalogue
releases, RTL+ from its own linear EPG premieres), yet all of them resolve the
linear airings with the same shared policy.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from pydantic import HttpUrl

from episode_calendar.domain import ReleaseType
from episode_calendar.providers.base import (
    NormalizedEpisode,
    NormalizedEpisodeRelease,
    NormalizedSeason,
    NormalizedSeries,
    first_seen_release,
)

# An airing slot is a local weekday (0-6, or None for a daily slot) plus a local time.
Slot = tuple[int | None, time]

# A broadcast time recurring on at least this many distinct weekdays is treated as a
# daily slot that also matches weekdays not yet present in the catalogue (a daily show
# like "Promi Big Brother" only starts with weekday entries).
DEFAULT_DAILY_SLOT_MIN_DAYS = 5

# A season counts as running while one of its releases is no older than this.
DEFAULT_RUNNING_WINDOW = timedelta(days=14)


def derive_slots_from_moments(
    moments: Iterable[datetime],
    *,
    timezone: ZoneInfo,
    daily_slot_min_days: int = DEFAULT_DAILY_SLOT_MIN_DAYS,
) -> tuple[Slot, ...]:
    """Turn concrete airing times into recurring ``(weekday, time)`` slots."""

    weekdays_by_time: dict[time, set[int]] = defaultdict(set)
    for moment in moments:
        local = moment.astimezone(timezone)
        weekdays_by_time[local.time()].add(local.weekday())
    slots: set[Slot] = set()
    for slot_time, weekdays in weekdays_by_time.items():
        daily = len(weekdays) >= daily_slot_min_days
        for weekday in range(7) if daily else weekdays:
            slots.add((None if daily else weekday, slot_time))
    return tuple(slots)


def derive_broadcast_slots(
    season: NormalizedSeason | None,
    *,
    timezone: ZoneInfo,
    daily_slot_min_days: int = DEFAULT_DAILY_SLOT_MIN_DAYS,
) -> tuple[Slot, ...]:
    """Derive slots from a season's releases.

    Real linear broadcasts take precedence; a season catalogued only with on-demand
    releases (for example an RTL+ style drop) falls back to those non-preview times.
    """

    if season is None:
        return ()
    releases = [release for episode in season.episodes for release in episode.releases]
    broadcasts = [
        release.release_at
        for release in releases
        if release.release_type is ReleaseType.TV_BROADCAST
    ]
    moments = broadcasts or [release.release_at for release in releases if not release.preview]
    return derive_slots_from_moments(
        moments, timezone=timezone, daily_slot_min_days=daily_slot_min_days
    )


def fits_slot(
    start: datetime,
    slots: Iterable[Slot],
    *,
    timezone: ZoneInfo,
    tolerance: timedelta,
) -> bool:
    """Whether an airing matches a recurring slot (same weekday, within ``tolerance``)."""

    local = start.astimezone(timezone)
    local_minutes = local.hour * 60 + local.minute
    for weekday, slot_time in slots:
        if weekday is not None and weekday != local.weekday():
            continue
        slot_minutes = slot_time.hour * 60 + slot_time.minute
        difference = min(
            abs(local_minutes - slot_minutes), 24 * 60 - abs(local_minutes - slot_minutes)
        )
        if timedelta(minutes=difference) <= tolerance:
            return True
    return False


def running_season(
    series: NormalizedSeries, now: datetime, *, window: timedelta = DEFAULT_RUNNING_WINDOW
) -> NormalizedSeason | None:
    """Return the highest-numbered season with a release no older than ``window``."""

    cutoff = now - window
    candidates = [
        season
        for season in series.seasons
        if season.number is not None
        and any(
            release.release_at >= cutoff
            for episode in season.episodes
            for release in episode.releases
        )
    ]
    return max(candidates, key=lambda season: season.number, default=None)


def latest_episode(
    series: NormalizedSeries,
) -> tuple[NormalizedSeason | None, NormalizedEpisode | None]:
    """Return the highest-numbered season's highest-numbered episode."""

    numbered_seasons = [season for season in series.seasons if season.number is not None]
    if not numbered_seasons:
        return None, None
    season = max(numbered_seasons, key=lambda item: item.number)
    numbered_episodes = [episode for episode in season.episodes if episode.number is not None]
    if numbered_episodes:
        episode = max(numbered_episodes, key=lambda item: item.number)
    else:
        episode = season.episodes[-1] if season.episodes else None
    return season, episode


def latest_episode_of(season: NormalizedSeason | None) -> NormalizedEpisode | None:
    """Return the highest-numbered episode of ``season``, else its last episode."""

    if season is None:
        return None
    numbered = [episode for episode in season.episodes if episode.number is not None]
    if numbered:
        return max(numbered, key=lambda item: item.number)
    return season.episodes[-1] if season.episodes else None


@dataclass(frozen=True)
class EpgBroadcast:
    """One linear airing from a provider's EPG, mapped to the shared shape."""

    external_id: str
    title: str
    start: datetime
    end: datetime | None = None
    season_number: int | None = None
    episode_number: int | None = None
    linked_episode_external_id: str | None = None
    url: HttpUrl | None = None


def _undated(episode: NormalizedEpisode) -> bool:
    """Whether the catalogue has no real date for ``episode``.

    Derived dates (``date_from_api=false`` without the placeholder marker) count as
    real dates; only a ``first_seen`` placeholder means "no date found".
    """

    return not any(not release.placeholder for release in episode.releases)


def filter_obsolete_seasons(series: NormalizedSeries) -> NormalizedSeries:
    """Drop lower-numbered catalogue seasons whose episodes carry no real date.

    A season below the highest-numbered catalogue season whose episodes only have
    ``dateFromApi=false`` placeholders is obsolete: while a newer season is listed
    (for example during the transition to the upcoming season) the older one must
    neither surface in today's calendar nor look like the running season. When no
    higher season is known the season is kept, since it is the only reference the
    catalogue provides right now.
    """

    highest = max(
        (season.number for season in series.seasons if season.number is not None), default=None
    )
    if highest is None:
        return series
    seasons = tuple(
        season
        for season in series.seasons
        if not (
            season.number is not None
            and season.number < highest
            and season.episodes
            and all(_undated(episode) for episode in season.episodes)
        )
    )
    return series.model_copy(update={"seasons": seasons})


def ensure_catalog_dates(series: NormalizedSeries, *, now: datetime) -> NormalizedSeries:
    """Give every catalogue episode a dating release.

    Episodes without any release get the shared ``first_seen`` placeholder
    (``dateFromApi=false``); providers that already emit one are unaffected.
    """

    def with_date(episode: NormalizedEpisode) -> NormalizedEpisode:
        if episode.releases:
            return episode
        return episode.model_copy(update={"releases": (first_seen_release(now),)})

    return series.model_copy(
        update={
            "seasons": tuple(
                season.model_copy(
                    update={"episodes": tuple(with_date(episode) for episode in season.episodes)}
                )
                for season in series.seasons
            )
        }
    )


def apply_catalog_policy(series: NormalizedSeries, *, now: datetime) -> NormalizedSeries:
    """Apply the shared catalogue base policy before any EPG merging."""

    return ensure_catalog_dates(filter_obsolete_seasons(series), now=now)


def merge_epg(
    series: NormalizedSeries,
    broadcasts: Sequence[EpgBroadcast],
    *,
    now: datetime | None = None,
    timezone: ZoneInfo,
    slots: Iterable[Slot] | None = None,
    tolerance: timedelta,
    next_episode_external_id: Callable[[str, int], str],
    logger: logging.Logger | None = None,
) -> NormalizedSeries:
    """Merge linear airings into the catalogue, which always stays authoritative.

    For every broadcast the policy first tries to read the season/episode from the
    airing itself:

    * An explicit catalogue link (``linked_episode_external_id``) or a readable
      episode number resolved against the given or running season locates a
      catalogue episode:
        - an episode the catalogue already dates is left alone and the airing is
          dropped (the catalogue wins, the airing is only its own broadcast);
        - an undated catalogue episode gets the airing as a ``dateFromApi=true``
          broadcast release so the importer replaces the placeholder.
      A numbered airing without a matching catalogue episode becomes a new episode
      (same number) carrying the airing.

    * An airing whose season/episode cannot be read uses the slot logic: an airing
      matching a recurring slot of the running season extends that season with the
      next episode number (filling an already announced undated next episode if one
      exists); every other airing is a rerun of the running season's latest episode.
    """

    now = now or datetime.now(UTC)
    broadcasts = sorted(broadcasts, key=lambda broadcast: broadcast.start)
    if not broadcasts:
        return series

    current_season = running_season(series, now)
    resolved_slots = (
        tuple(slots)
        if slots is not None
        else (derive_broadcast_slots(current_season, timezone=timezone) if current_season else ())
    )
    latest = latest_episode_of(current_season) or latest_episode(series)[1]

    episodes_by_external_id: dict[str, tuple[NormalizedSeason, NormalizedEpisode]] = {
        episode.external_id: (season, episode)
        for season in series.seasons
        for episode in season.episodes
    }
    episodes_by_number: dict[tuple[int, int], tuple[NormalizedSeason, NormalizedEpisode]] = {
        (season.number, episode.number): (season, episode)
        for season in series.seasons
        for episode in season.episodes
        if season.number is not None and episode.number is not None
    }
    seasons_by_number = {
        season.number: season for season in series.seasons if season.number is not None
    }
    used_numbers: dict[str, set[int]] = defaultdict(set)

    extra_releases: dict[str, list[NormalizedEpisodeRelease]] = defaultdict(list)
    created: dict[str, list[NormalizedEpisode]] = defaultdict(list)

    def announced_next(season: NormalizedSeason) -> int:
        dated = [
            episode.number
            for episode in season.episodes
            if episode.number is not None and not _undated(episode)
        ]
        if dated:
            return max(dated) + 1
        numbered = [episode.number for episode in season.episodes if episode.number is not None]
        return max(numbered) + 1 if numbered else 1

    def airing(broadcast: EpgBroadcast, *, rerun: bool = False) -> NormalizedEpisodeRelease:
        return NormalizedEpisodeRelease(
            external_id=broadcast.external_id,
            release_type=ReleaseType.TV_BROADCAST,
            release_at=broadcast.start,
            available_until=broadcast.end,
            url=broadcast.url,
            rerun=rerun,
            date_from_api=True,
        )

    for broadcast in broadcasts:
        target: tuple[NormalizedSeason, NormalizedEpisode] | None = None
        create_season: NormalizedSeason | None = None
        if (
            broadcast.linked_episode_external_id is not None
            and broadcast.linked_episode_external_id in episodes_by_external_id
        ):
            target = episodes_by_external_id[broadcast.linked_episode_external_id]
        elif broadcast.episode_number is not None:
            number_season: NormalizedSeason | None = None
            if broadcast.season_number is not None:
                number_season = seasons_by_number.get(broadcast.season_number)
            elif current_season is not None:
                number_season = current_season
            if number_season is not None:
                target = episodes_by_number.get((number_season.number, broadcast.episode_number))
                if target is None:
                    create_season = number_season
            else:
                candidates = [
                    (season, episode)
                    for (season_number, episode_number), (
                        season,
                        episode,
                    ) in episodes_by_number.items()
                    if episode_number == broadcast.episode_number
                ]
                if len(candidates) == 1:
                    target = candidates[0]

        if create_season is not None:
            episode_number = broadcast.episode_number
            assert episode_number is not None
            used_numbers[create_season.external_id].add(episode_number)
            created[create_season.external_id].append(
                NormalizedEpisode(
                    external_id=next_episode_external_id(create_season.external_id, episode_number),
                    number=episode_number,
                    title=broadcast.title,
                    releases=(airing(broadcast),),
                )
            )
            continue

        if target is not None:
            _, episode = target
            if not _undated(episode):
                # The catalogue already dates the episode; its own broadcast does not
                # add information and must not create a second date.
                continue
            extra_releases[episode.external_id].append(airing(broadcast))
            continue

        if current_season is not None and fits_slot(
            broadcast.start, resolved_slots, timezone=timezone, tolerance=tolerance
        ):
            number = announced_next(current_season)
            while number in used_numbers[current_season.external_id]:
                number += 1
            announced = episodes_by_number.get((current_season.number, number))
            if announced is not None and _undated(announced[1]):
                # The running season already announces the next episode without a date;
                # the airing supplies it instead of duplicating the episode.
                used_numbers[current_season.external_id].add(number)
                extra_releases[announced[1].external_id].append(airing(broadcast))
            else:
                used_numbers[current_season.external_id].add(number)
                created[current_season.external_id].append(
                    NormalizedEpisode(
                        external_id=next_episode_external_id(current_season.external_id, number),
                        number=number,
                        title=broadcast.title,
                        releases=(airing(broadcast),),
                    )
                )
            continue

        if latest is None:
            if logger is not None:
                logger.info(
                    "Dropping unlinked EPG broadcast without a target episode: %s",
                    broadcast.start.isoformat(),
                )
            continue
        extra_releases[latest.external_id].append(airing(broadcast, rerun=True))

    seasons: list[NormalizedSeason] = []
    for season in series.seasons:
        episodes: list[NormalizedEpisode] = []
        for episode in season.episodes:
            additions = extra_releases.get(episode.external_id)
            if additions:
                episode = episode.model_copy(update={"releases": (*episode.releases, *additions)})
            episodes.append(episode)
        episodes.extend(created[season.external_id])
        seasons.append(season.model_copy(update={"episodes": tuple(episodes)}))
    return series.model_copy(update={"seasons": tuple(seasons)})

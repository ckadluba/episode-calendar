"""Provider-independent broadcast-slot derivation and EPG folding.

Providers differ in where they obtain the recurring broadcast slots of a running
season (Joyn reads them from the catalogue releases, RTL+ from its own linear EPG
premieres), but the mechanism that turns those slots into a rerun decision and
folds the linear airings back into the real season tree is shared here.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from episode_calendar.domain import ReleaseType
from episode_calendar.providers.base import (
    NormalizedEpisode,
    NormalizedEpisodeRelease,
    NormalizedSeason,
    NormalizedSeries,
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


@dataclass(frozen=True)
class EpgBroadcast:
    """One linear airing from a provider's EPG, mapped to the shared shape."""

    external_id: str
    title: str
    start: datetime
    end: datetime | None = None
    linked_episode_external_id: str | None = None


def fold_broadcasts(
    series: NormalizedSeries,
    broadcasts: Sequence[EpgBroadcast],
    *,
    running_season: NormalizedSeason | None,
    slots: Iterable[Slot],
    timezone: ZoneInfo,
    tolerance: timedelta,
    next_episode_external_id: Callable[[str, int], str],
    logger: logging.Logger | None = None,
) -> NormalizedSeries:
    """Fold linear airings into the real season/episode tree.

    An airing linked to a catalogue episode is attached there. An unlinked airing that
    matches a recurring slot extends the running season with the next episode number;
    any other airing (off-slot, or without a running season) is a rerun attached to the
    latest known episode.
    """

    slots = tuple(slots)
    catalog_episodes = {
        episode.external_id: season.external_id
        for season in series.seasons
        for episode in season.episodes
    }
    latest_season, latest = latest_episode(series)
    extra: dict[tuple[str, str], list[NormalizedEpisodeRelease]] = defaultdict(list)
    created: dict[str, list[NormalizedEpisode]] = defaultdict(list)
    next_number = {
        season.external_id: max(
            (episode.number for episode in season.episodes if episode.number is not None),
            default=0,
        )
        + 1
        for season in series.seasons
        if season.number is not None
    }
    for broadcast in broadcasts:
        release = NormalizedEpisodeRelease(
            external_id=broadcast.external_id,
            release_type=ReleaseType.TV_BROADCAST,
            release_at=broadcast.start,
            available_until=broadcast.end,
            rerun=not fits_slot(broadcast.start, slots, timezone=timezone, tolerance=tolerance),
        )
        linked = broadcast.linked_episode_external_id
        if linked is not None and linked in catalog_episodes:
            extra[(catalog_episodes[linked], linked)].append(release)
            continue
        if not release.rerun and running_season is not None:
            number = next_number[running_season.external_id]
            next_number[running_season.external_id] = number + 1
            created[running_season.external_id].append(
                NormalizedEpisode(
                    external_id=next_episode_external_id(running_season.external_id, number),
                    number=number,
                    title=broadcast.title,
                    releases=(release,),
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
        extra[(latest_season.external_id, latest.external_id)].append(
            release.model_copy(update={"rerun": True})
        )

    seasons: list[NormalizedSeason] = []
    for season in series.seasons:
        episodes: list[NormalizedEpisode] = []
        for episode in season.episodes:
            additions = extra.get((season.external_id, episode.external_id))
            if additions:
                episode = episode.model_copy(update={"releases": (*episode.releases, *additions)})
            episodes.append(episode)
        episodes.extend(created.get(season.external_id, ()))
        seasons.append(season.model_copy(update={"episodes": tuple(episodes)}))
    return series.model_copy(update={"seasons": tuple(seasons)})

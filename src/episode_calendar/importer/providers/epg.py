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

from episode_calendar.importer.providers.base import (
    NormalizedEpisode,
    NormalizedEpisodeRelease,
    NormalizedSeason,
    NormalizedSeries,
    first_seen_release,
)
from episode_calendar.shared.domain import ReleaseType

# An airing slot is a local weekday (0-6, or None for a daily slot) plus a local time.
Slot = tuple[int | None, time]

# A broadcast time recurring on at least this many distinct weekdays is treated as a
# daily slot that also matches weekdays not yet present in the catalogue (a daily show
# like "Promi Big Brother" only starts with weekday entries).
DEFAULT_DAILY_SLOT_MIN_DAYS = 5

# A season counts as running while one of its releases is no older than this.
DEFAULT_RUNNING_WINDOW = timedelta(days=14)

# An unattributable airing within this distance of the month/day on which earlier seasons
# premiered is that series' next season, not an off-season rerun.
DEFAULT_SEASON_WINDOW_TOLERANCE = timedelta(days=28)

# A new season is only plausible once the catalogue's latest release is at least this old;
# closer airings belong to the running season's own cadence.
DEFAULT_NEW_SEASON_MIN_GAP = timedelta(days=90)

# Earlier premieres required before the annual window is trusted (one-off seasons prove
# no cadence).
DEFAULT_MIN_SEASON_PREMIERES = 2

# Synthetic identifier prefix for a season the EPG announced before the catalogue listed it.
# Shared by every provider that merges its guide through ``merge_epg()``; the importer uses
# it to recognise such a forecast season and drop its releases once the airings disappear
# from the guide again.
EPG_SEASON_EXTERNAL_ID_PREFIX = "epg:season:"


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


def season_premieres(series: NormalizedSeries) -> tuple[datetime, ...]:
    """Return the earliest real release of every numbered season, one entry per season."""

    premieres: list[datetime] = []
    for season in series.seasons:
        if season.number is None:
            continue
        moments = [
            release.release_at
            for episode in season.episodes
            for release in episode.releases
            if not release.placeholder
        ]
        if moments:
            premieres.append(min(moments))
    return tuple(sorted(premieres))


def latest_release_moment(series: NormalizedSeries) -> datetime | None:
    """Return the newest real (non-placeholder) release of the whole series."""

    return max(
        (
            release.release_at
            for season in series.seasons
            for episode in season.episodes
            for release in episode.releases
            if not release.placeholder
        ),
        default=None,
    )


def annual_window_distance(
    premieres: Iterable[datetime],
    start: datetime,
    *,
    timezone: ZoneInfo,
) -> timedelta | None:
    """Distance from ``start`` to the closest yearly recurrence of a premiere.

    A premiere is projected onto the airing's year plus its neighbours, so a season that
    starts around New Year still matches an airing on either side of the year boundary.
    """

    local_start = start.astimezone(timezone)
    best: timedelta | None = None
    for premiere in premieres:
        local = premiere.astimezone(timezone)
        for year in (local_start.year - 1, local_start.year, local_start.year + 1):
            try:
                projected = local.replace(year=year)
            except ValueError:  # 29 February in a non-leap year
                projected = local.replace(year=year, day=28)
            distance = abs(local_start - projected)
            if best is None or distance < best:
                best = distance
    return best


def next_season_number(series: NormalizedSeries) -> int | None:
    """Return the season number an upcoming premiere would belong to.

    ``None`` when the catalogue has no numbered season, or when its highest season still
    carries no real release: such an announced season is already listed undated and is
    the providers' own concern rather than an EPG inference.
    """

    numbered = [season for season in series.seasons if season.number is not None]
    if not numbered:
        return None
    highest = max(numbered, key=lambda season: season.number)
    assert highest.number is not None
    if not any(
        not release.placeholder for episode in highest.episodes for release in episode.releases
    ):
        return None
    return highest.number + 1


def new_season_premiere(
    series: NormalizedSeries,
    start: datetime,
    *,
    timezone: ZoneInfo,
    tolerance: timedelta = DEFAULT_SEASON_WINDOW_TOLERANCE,
    min_gap: timedelta = DEFAULT_NEW_SEASON_MIN_GAP,
    min_premieres: int = DEFAULT_MIN_SEASON_PREMIERES,
) -> int | None:
    """Season number an unattributable airing most likely premieres, else ``None``.

    Linear guides repeat old episodes all year, so one airing proves nothing. The rule uses
    the series' own annual cadence instead: once at least ``min_premieres`` earlier seasons
    premiered around the same month and day (within ``tolerance``) and the catalogue's
    latest release is older than ``min_gap``, the airing starts the next season instead of
    repeating the finished one.
    """

    number = next_season_number(series)
    if number is None:
        return None
    premieres = season_premieres(series)
    if len(premieres) < min_premieres:
        return None
    latest = latest_release_moment(series)
    if latest is not None and start - latest < min_gap:
        return None
    distance = annual_window_distance(premieres, start, timezone=timezone)
    if distance is None or distance > tolerance:
        return None
    return number


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
    announces_season: int | None = None
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
        - an episode the catalogue already dates keeps its date: the airing is
          dropped only when it falls within ``tolerance`` of a catalogue streaming
          release (same event); otherwise it is kept as its own ``tv_broadcast``
          release;
        - an undated catalogue episode gets the airing as a ``dateFromApi=true``
          broadcast release.
      A numbered airing without a matching catalogue episode becomes a new episode
      (same number) carrying the airing.

    * An airing whose season/episode cannot be read is first checked against the
      series' own annual cadence (see ``new_season_premiere()``): an off-season
      airing that lands on the month/day earlier seasons premiered starts the next
      season as a new ``NormalizedSeason``. Its further airings extend that season in
      airing order, because the catalogue cannot attribute them yet.

    * A provider can announce the season an airing starts explicitly
      (``announces_season``). That starts the season the same way, but without needing a
      catalogue cadence: the announcing airing and the following ones build the season in
      airing order. This is how a provider whose catalogue still lists only the previous
      season contributes an upcoming one.

    * Otherwise the slot logic applies: an airing matching a recurring slot of the
      running season extends that season with the next episode number (filling an
      already announced undated next episode if one exists). Every other airing
      premieres the earliest episode the catalogue still lists as subscriber-only,
      since an airing cannot repeat an episode that has no regular date yet; once
      those are used up it is a rerun of the running season's latest episode.
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
    pending_premiere: NormalizedSeason | None = None

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

    def earliest_subscriber_only(season: NormalizedSeason | None) -> NormalizedEpisode | None:
        """First numbered episode whose catalogue dates are all previews.

        Such an episode is not regularly available yet, so an unattributable airing
        of the running season belongs to it rather than to the latest episode.
        """

        if season is None:
            return None
        numbered = sorted(
            (item for item in season.episodes if item.number is not None),
            key=lambda item: item.number or 0,
        )
        for episode in numbered:
            releases = (*episode.releases, *extra_releases.get(episode.external_id, ()))
            dated = [release for release in releases if not release.placeholder]
            if dated and all(release.preview for release in dated):
                return episode
        return None

    def add_premiere_airing(broadcast: EpgBroadcast) -> None:
        """Add an airing to the season the annual-window rule has just announced.

        The catalogue cannot attribute these airings yet, so they are numbered in airing
        order; an airing that names its own episode number keeps it.
        """

        assert pending_premiere is not None
        used = [
            episode.number
            for episode in created[pending_premiere.external_id]
            if episode.number is not None
        ]
        number = broadcast.episode_number
        if number is None or number in used:
            number = max(used, default=0) + 1
        created[pending_premiere.external_id].append(
            NormalizedEpisode(
                external_id=next_episode_external_id(pending_premiere.external_id, number),
                number=number,
                title=broadcast.title,
                releases=(airing(broadcast),),
            )
        )

    for broadcast in broadcasts:
        target: tuple[NormalizedSeason, NormalizedEpisode] | None = None
        create_season: NormalizedSeason | None = None
        announced_season = broadcast.announces_season
        premiere_number = (
            new_season_premiere(series, broadcast.start, timezone=timezone)
            if (
                announced_season is None
                and broadcast.linked_episode_external_id is None
                and broadcast.season_number is None
                and pending_premiere is None
            )
            else None
        )
        if (
            broadcast.linked_episode_external_id is not None
            and broadcast.linked_episode_external_id in episodes_by_external_id
        ):
            target = episodes_by_external_id[broadcast.linked_episode_external_id]
        elif announced_season is not None and pending_premiere is None:
            pending_premiere = NormalizedSeason(
                external_id=f"{EPG_SEASON_EXTERNAL_ID_PREFIX}{series.external_id}:{announced_season}",
                number=announced_season,
            )
            add_premiere_airing(broadcast)
            continue
        elif premiere_number is not None:
            pending_premiere = NormalizedSeason(
                external_id=f"{EPG_SEASON_EXTERNAL_ID_PREFIX}{series.external_id}:{premiere_number}",
                number=premiere_number,
            )
            add_premiere_airing(broadcast)
            continue
        elif pending_premiere is not None:
            add_premiere_airing(broadcast)
            continue
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
            # A TV airing is dropped only when it matches a catalogue streaming
            # release within the tolerance: then it is the same event, not a
            # second date. A catalogue date alone never discards the broadcast.
            catalogue_streaming = [
                release
                for release in (*episode.releases, *extra_releases.get(episode.external_id, ()))
                if release.release_type is ReleaseType.STREAMING and not release.placeholder
            ]
            if any(
                abs(release.release_at - broadcast.start) <= tolerance
                for release in catalogue_streaming
            ):
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

        if current_season is not None:
            premiere = earliest_subscriber_only(current_season)
            if premiere is not None:
                # The catalogue still lists the episode as subscriber-only, so the
                # unattributable airing is its regular premiere, not a rerun.
                extra_releases[premiere.external_id].append(airing(broadcast))
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
    if pending_premiere is not None:
        seasons.append(
            pending_premiere.model_copy(
                update={"episodes": tuple(created[pending_premiere.external_id])}
            )
        )
    return series.model_copy(update={"seasons": tuple(seasons)})

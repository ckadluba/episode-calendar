"""ARD Mediathek programme catalogue adapter."""

from __future__ import annotations

import asyncio
import logging
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from episode_calendar.importer.config import get_importer_settings
from episode_calendar.importer.providers.base import (
    NormalizedEpisode,
    NormalizedEpisodeRelease,
    NormalizedSeason,
    NormalizedSeries,
    duration_minutes_from_seconds,
)
from episode_calendar.importer.providers.epg import (
    EpgBroadcast,
    filter_obsolete_seasons,
    merge_epg,
)
from episode_calendar.shared.domain import TV_BROADCAST_MATCH_TOLERANCE, ReleaseType

logger = logging.getLogger(__name__)

_EPISODE_RE = re.compile(r"\(S(?P<season>\d+)/E(?P<episode>\d+)\)\s*$")
_TITLE_RE = re.compile(r"^(?:Folge\s+\d+\s*:\s*)?(?P<title>.*?)\s*\(S\d+/E\d+\)\s*$")

EPG_TIMEZONE = ZoneInfo("Europe/Berlin")


class ARDMediathekProviderError(RuntimeError):
    """Base class for ARD Mediathek catalogue failures."""


class ARDMediathekHTTPError(ARDMediathekProviderError):
    pass


class ARDMediathekMalformedResponseError(ARDMediathekProviderError):
    pass


class ARDMediathekProvider:
    """Fetch public ARD Mediathek show metadata by its stable asset ID."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        endpoint: str | None = None,
        timeout: float | None = None,
        page_size: int = 100,
        program_url: str | None = None,
        schedule_days: int | None = None,
        schedule_lookback_days: int | None = None,
    ) -> None:
        settings = get_importer_settings()
        self._client = client
        self._endpoint = endpoint or settings.ardmediathek_api_url
        self._timeout = timeout if timeout is not None else settings.ardmediathek_timeout_seconds
        self._page_size = page_size
        self._program_url = program_url or settings.ardmediathek_program_url
        self._program_detail_url = f"{self._program_url.rsplit('/', 1)[0]}/detail"
        self._schedule_days = (
            schedule_days if schedule_days is not None else settings.ardmediathek_schedule_days
        )
        self._schedule_lookback_days = (
            schedule_lookback_days
            if schedule_lookback_days is not None
            else settings.ardmediathek_schedule_lookback_days
        )

    @property
    def slug(self) -> str:
        return "ardmediathek"

    async def fetch_series(self, external_id: str) -> NormalizedSeries:
        return await self.get_series(external_id)

    async def get_series(self, asset_id: str) -> NormalizedSeries:
        asset_id = asset_id.strip()
        if not asset_id:
            raise ValueError("ARD Mediathek identifier must not be empty")
        if self._client is not None:
            return await self._fetch(self._client, asset_id)
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            return await self._fetch(client, asset_id)

    async def _fetch(self, client: httpx.AsyncClient, asset_id: str) -> NormalizedSeries:
        teasers: list[Mapping[str, Any]] = []
        page = 0
        total: int | None = None
        while True:
            try:
                response = await client.get(
                    f"{self._endpoint.rstrip('/')}/{asset_id}",
                    params={"pageNumber": page, "pageSize": self._page_size},
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise ARDMediathekHTTPError(
                    f"ARD Mediathek HTTP {exc.response.status_code}"
                ) from exc
            except httpx.RequestError as exc:
                raise ARDMediathekHTTPError(str(exc)) from exc
            try:
                payload = response.json()
            except ValueError as exc:
                raise ARDMediathekMalformedResponseError(
                    "ARD Mediathek response was not JSON"
                ) from exc
            if not isinstance(payload, Mapping):
                raise ARDMediathekMalformedResponseError("ARD Mediathek response is not an object")
            if total is None:
                pagination = payload.get("pagination")
                total = self._integer(
                    pagination.get("totalElements") if isinstance(pagination, Mapping) else None,
                    "pagination.totalElements",
                )
            raw_teasers = payload.get("teasers")
            if not isinstance(raw_teasers, list):
                raise ARDMediathekMalformedResponseError("ARD Mediathek teasers must be a list")
            teasers.extend(self._mapping(item, "teasers[]") for item in raw_teasers)
            if len(teasers) >= total:
                break
            if not raw_teasers:
                raise ARDMediathekMalformedResponseError(
                    "ARD Mediathek pagination returned no teasers"
                )
            page += 1

        # The catalogue already announces the whole season, including episodes that the
        # linear programme feed does not publish until roughly a week before their
        # broadcast. The catalogue is the base: an obsolete lower season is dropped, and
        # the teaser's ``broadcastedOn`` is only the upcoming linear airing, so the
        # catalogue release date prefers the on-demand publication date from the episode
        # detail payload (``_with_streaming_availability``). Scheduled broadcasts are
        # merged supplementally on top in ``_fold_scheduled_releases``.
        normalized = self._normalize(asset_id, teasers)
        normalized = filter_obsolete_seasons(normalized)
        (
            scheduled_releases,
            scheduled_descriptions,
            broadcast_targets,
        ) = await self._fetch_scheduled_releases(client, normalized.title)
        catalog_descriptions, available_from = await self._fetch_catalog_descriptions(
            client, teasers
        )
        normalized = self._with_descriptions(normalized, catalog_descriptions)
        normalized = self._with_streaming_availability(normalized, available_from)
        return self._fold_scheduled_releases(
            normalized, scheduled_releases, scheduled_descriptions, broadcast_targets
        )

    async def _fetch_scheduled_releases(
        self, client: httpx.AsyncClient, series_title: str
    ) -> tuple[
        dict[str, tuple[NormalizedEpisodeRelease, ...]],
        dict[str, str],
        dict[str, str],
    ]:
        scheduled: dict[str, list[NormalizedEpisodeRelease]] = defaultdict(list)
        descriptions: dict[str, str] = {}
        broadcast_targets: dict[str, str] = {}
        seen_programme_ids: set[str] = set()
        lookback = max(0, self._schedule_lookback_days)
        # The programme API answers per day and only publishes a limited window, so past
        # broadcasts would be skipped without looking back before today.
        start = datetime.now(UTC).date() - timedelta(days=lookback)
        for offset in range(lookback + max(0, self._schedule_days)):
            schedule_date = start + timedelta(days=offset)
            try:
                response = await client.get(
                    self._program_url,
                    params={"day": schedule_date.isoformat()},
                )
                if response.status_code == 404:
                    continue
                response.raise_for_status()
                payload = response.json()
            except httpx.HTTPStatusError as exc:
                raise ARDMediathekHTTPError(
                    f"ARD programme API HTTP {exc.response.status_code}"
                ) from exc
            except httpx.RequestError as exc:
                raise ARDMediathekHTTPError(str(exc)) from exc
            except ValueError as exc:
                raise ARDMediathekMalformedResponseError(
                    "ARD programme response was not valid JSON"
                ) from exc
            for programme in self._programmes(payload):
                if not self._same_title(
                    programme.get("title"), series_title
                ) and not self._same_title(programme.get("coreTitle"), series_title):
                    continue
                episode_title = programme.get("coreSubline") or programme.get("subline")
                programme_id = programme.get("id")
                broadcasted_on = programme.get("broadcastedOn")
                if not isinstance(episode_title, str) or not episode_title:
                    continue
                if not isinstance(programme_id, str) or not programme_id:
                    continue
                if programme_id in seen_programme_ids:
                    continue
                seen_programme_ids.add(programme_id)
                target_episode_id = self._target_episode_id(programme)
                if target_episode_id:
                    broadcast_targets[programme_id] = target_episode_id
                description = programme.get("synopsis")
                if not isinstance(description, str) or not description.strip():
                    description = await self._fetch_programme_description(client, programme_id)
                scheduled[episode_title].append(
                    release := NormalizedEpisodeRelease(
                        external_id=programme_id,
                        release_type=ReleaseType.TV_BROADCAST,
                        release_at=self._datetime(broadcasted_on, "programme.broadcastedOn"),
                        url=f"https://www.ardmediathek.de/tv-programm/{programme_id}",
                    )
                )
                if description:
                    descriptions[release.external_id or programme_id] = description
        return (
            {title: tuple(releases) for title, releases in scheduled.items()},
            descriptions,
            broadcast_targets,
        )

    @staticmethod
    def _target_episode_id(programme: Mapping[str, Any]) -> str | None:
        links = programme.get("links")
        target = links.get("target") if isinstance(links, Mapping) else None
        url_id = target.get("urlId") if isinstance(target, Mapping) else None
        return url_id if isinstance(url_id, str) and url_id else None

    async def _fetch_programme_description(
        self, client: httpx.AsyncClient, programme_id: str
    ) -> str | None:
        try:
            response = await client.get(
                self._program_detail_url,
                params={"teaserId": programme_id},
            )
            if response.status_code == 404:
                return None
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ARDMediathekHTTPError("ARD programme detail request failed") from exc
        teaser = payload.get("teaser") if isinstance(payload, Mapping) else None
        description = teaser.get("synopsis") if isinstance(teaser, Mapping) else None
        return description if isinstance(description, str) and description.strip() else None

    async def _fetch_catalog_descriptions(
        self, client: httpx.AsyncClient, teasers: list[Mapping[str, Any]]
    ) -> tuple[dict[str, str], dict[str, datetime]]:
        requests = [
            self._fetch_catalog_episode(client, teaser)
            for teaser in teasers
            if self._episode_id(teaser) and self._target_url(teaser)
        ]
        results = await asyncio.gather(*requests)
        descriptions = {
            episode_id: description for episode_id, description, _ in results if description
        }
        available_from = {
            episode_id: published for episode_id, _, published in results if published is not None
        }
        return descriptions, available_from

    async def _fetch_catalog_episode(
        self, client: httpx.AsyncClient, teaser: Mapping[str, Any]
    ) -> tuple[str, str | None, datetime | None]:
        episode_id = self._episode_id(teaser)
        target_url = self._target_url(teaser)
        if episode_id is None or target_url is None:
            return "", None, None
        try:
            response = await client.get(target_url)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ARDMediathekHTTPError("ARD episode detail request failed") from exc
        widgets = payload.get("widgets") if isinstance(payload, Mapping) else None
        if not isinstance(widgets, list):
            return episode_id, None, None
        description: str | None = None
        published: datetime | None = None
        for widget in widgets:
            if not isinstance(widget, Mapping):
                continue
            embedded = widget.get("mediaCollection", {}).get("embedded")
            meta = embedded.get("meta") if isinstance(embedded, Mapping) else None
            if not isinstance(meta, Mapping):
                continue
            if description is None:
                synopsis = meta.get("synopsis")
                if isinstance(synopsis, str) and synopsis.strip():
                    description = synopsis
            if published is None:
                raw = meta.get("broadcastedOnDateTime")
                if isinstance(raw, str):
                    try:
                        published = self._datetime(raw, "episode.broadcastedOnDateTime")
                    except ARDMediathekMalformedResponseError:
                        continue
        return episode_id, description, published

    @staticmethod
    def _episode_id(teaser: Mapping[str, Any]) -> str | None:
        value = teaser.get("id")
        return value if isinstance(value, str) and value else None

    @staticmethod
    def _target_url(teaser: Mapping[str, Any]) -> str | None:
        links = teaser.get("links")
        target = links.get("target") if isinstance(links, Mapping) else None
        href = target.get("href") if isinstance(target, Mapping) else None
        return href if isinstance(href, str) and href else None

    @staticmethod
    def _with_descriptions(
        series: NormalizedSeries, descriptions: Mapping[str, str]
    ) -> NormalizedSeries:
        return series.model_copy(
            update={
                "seasons": tuple(
                    season.model_copy(
                        update={
                            "episodes": tuple(
                                episode.model_copy(
                                    update={"description": descriptions.get(episode.external_id)}
                                )
                                for episode in season.episodes
                            )
                        }
                    )
                    for season in series.seasons
                )
            }
        )

    @staticmethod
    def _with_streaming_availability(
        series: NormalizedSeries, available_from: Mapping[str, datetime]
    ) -> NormalizedSeries:
        """Prefer the on-demand publication date over the upcoming linear broadcast.

        The teaser ``broadcastedOn`` is the linear airing time, which for pre-published
        shows lies in the future even though the episode is already streamable. The detail
        payload's ``broadcastedOnDateTime`` is the date the stream actually went online.
        """

        if not available_from:
            return series

        def dated(
            releases: tuple[NormalizedEpisodeRelease, ...],
        ) -> tuple[NormalizedEpisodeRelease, ...]:
            return tuple(
                release.model_copy(update={"release_at": available_from[release.external_id]})
                if (
                    release.release_type is ReleaseType.STREAMING
                    and release.external_id in available_from
                )
                else release
                for release in releases
            )

        return series.model_copy(
            update={
                "seasons": tuple(
                    season.model_copy(
                        update={
                            "episodes": tuple(
                                episode.model_copy(update={"releases": dated(episode.releases)})
                                for episode in season.episodes
                            )
                        }
                    )
                    for season in series.seasons
                )
            }
        )

    @staticmethod
    def _same_title(left: Any, right: str) -> bool:
        if not isinstance(left, str):
            return False

        def normalize(value: str) -> str:
            return re.sub(r"\s*[-·–—]\s*", " ", value).casefold().strip()

        return normalize(left) == normalize(right)

    @classmethod
    def _fold_scheduled_releases(
        cls,
        series: NormalizedSeries,
        scheduled_releases: Mapping[str, tuple[NormalizedEpisodeRelease, ...]],
        scheduled_descriptions: Mapping[str, str] | None = None,
        broadcast_targets: Mapping[str, str] | None = None,
        *,
        now: datetime | None = None,
    ) -> NormalizedSeries:
        """Merge planned linear broadcasts through the shared EPG policy.

        Broadcasts that resolve to a catalogue episode (target ``urlId``, else title,
        else synopsis) are handed to the shared merger, which drops them when the
        catalogue already dates the episode; only broadcasts without a catalogue home
        use the slot logic. The episode detail dates (``_with_streaming_availability``)
        were already applied, so the merged tree keeps the catalogue dates.
        """

        episodes_by_external_id = {
            episode.external_id: episode for season in series.seasons for episode in season.episodes
        }
        episodes_by_title: dict[str, list[NormalizedEpisode]] = defaultdict(list)
        description_keys: dict[str, NormalizedEpisode] = {}
        for season in series.seasons:
            for episode in season.episodes:
                if episode.title:
                    episodes_by_title[episode.title].append(episode)
                if episode.description:
                    description_keys[cls._description_key(episode.description)] = episode
        targets = broadcast_targets or {}
        descriptions = scheduled_descriptions or {}
        broadcasts: list[EpgBroadcast] = []
        for title, title_releases in scheduled_releases.items():
            for release in title_releases:
                external_id = release.external_id or ""
                linked_ids = cls._linked_episode_ids(
                    external_id,
                    title,
                    target=targets.get(external_id),
                    description=descriptions.get(external_id),
                    episodes_by_external_id=episodes_by_external_id,
                    episodes_by_title=episodes_by_title,
                    description_keys=description_keys,
                )
                if not linked_ids:
                    broadcasts.append(
                        EpgBroadcast(
                            external_id=external_id,
                            title=title,
                            start=release.release_at,
                            url=release.url,
                        )
                    )
                    continue
                for episode_id in linked_ids:
                    broadcasts.append(
                        EpgBroadcast(
                            external_id=external_id,
                            title=title,
                            start=release.release_at,
                            url=release.url,
                            linked_episode_external_id=episode_id,
                        )
                    )
        return merge_epg(
            series,
            broadcasts,
            now=now or datetime.now(UTC),
            timezone=EPG_TIMEZONE,
            tolerance=TV_BROADCAST_MATCH_TOLERANCE,
            next_episode_external_id=lambda season_id, number: f"scheduled:{season_id}:{number}",
            logger=logger,
        )

    @classmethod
    def _linked_episode_ids(
        cls,
        external_id: str,
        title: str,
        *,
        target: str | None,
        description: str | None,
        episodes_by_external_id: Mapping[str, NormalizedEpisode],
        episodes_by_title: Mapping[str, Sequence[NormalizedEpisode]],
        description_keys: Mapping[str, NormalizedEpisode],
    ) -> tuple[str, ...]:
        if target and target in episodes_by_external_id:
            return (target,)
        title_matches = episodes_by_title.get(title)
        if title_matches:
            return tuple(episode.external_id for episode in title_matches)
        if description:
            for _key, episode in description_keys.items():
                if cls._descriptions_match(description, episode.description):
                    return (episode.external_id,)
        return ()

    @staticmethod
    def _description_key(value: str | None) -> str:
        if not value:
            return ""
        normalized = value.replace("…", ".").replace("...", ".")
        normalized = re.sub(r"\s+([,.!?])", r"\1", normalized)
        return " ".join(normalized.split()).casefold()

    @classmethod
    def _descriptions_match(cls, left: str | None, right: str | None) -> bool:
        left_key = cls._description_key(left)
        right_key = cls._description_key(right)
        return bool(left_key and right_key) and (
            left_key == right_key
            or left_key.startswith(right_key)
            or right_key.startswith(left_key)
        )

    @staticmethod
    def _programmes(payload: Any) -> list[Mapping[str, Any]]:
        if not isinstance(payload, Mapping) or not isinstance(payload.get("channels"), list):
            raise ARDMediathekMalformedResponseError("ARD programme response lacks channels")
        programmes = []
        for channel in payload["channels"]:
            if not isinstance(channel, Mapping) or not isinstance(channel.get("timeSlots"), list):
                continue
            for time_slot_group in channel["timeSlots"]:
                if not isinstance(time_slot_group, list):
                    continue
                programmes.extend(item for item in time_slot_group if isinstance(item, Mapping))
        return programmes

    @classmethod
    def _normalize(cls, asset_id: str, teasers: list[Mapping[str, Any]]) -> NormalizedSeries:
        episodes_by_season: dict[int, list[NormalizedEpisode]] = defaultdict(list)
        series_title: str | None = None
        for teaser in teasers:
            show = teaser.get("show")
            if isinstance(show, Mapping):
                show_title = show.get("title")
                if isinstance(show_title, str) and show_title:
                    series_title = show_title
            title = teaser.get("longTitle") or teaser.get("mediumTitle")
            match = _EPISODE_RE.search(title) if isinstance(title, str) else None
            if not match:
                continue
            episode_id = teaser.get("id")
            if not isinstance(episode_id, str) or not episode_id:
                raise ARDMediathekMalformedResponseError("ARD episode lacks an ID")
            release_at = cls._datetime(teaser.get("broadcastedOn"), "teaser.broadcastedOn")
            available_until = cls._datetime(
                teaser.get("availableTo"), "teaser.availableTo", optional=True
            )
            episode_number = int(match.group("episode"))
            episode_title_match = _TITLE_RE.match(title)
            episode_title = (
                episode_title_match.group("title").strip().strip('"')
                if episode_title_match
                else title
            )
            episodes_by_season[int(match.group("season"))].append(
                NormalizedEpisode(
                    external_id=episode_id,
                    number=episode_number,
                    title=episode_title or f"Episode {episode_number}",
                    duration_minutes=duration_minutes_from_seconds(teaser.get("duration")),
                    releases=(
                        NormalizedEpisodeRelease(
                            external_id=episode_id,
                            release_type=ReleaseType.STREAMING,
                            release_at=release_at,
                            available_until=available_until,
                            url=f"https://www.ardmediathek.de/video/{episode_id}",
                        ),
                    ),
                )
            )

        if not series_title:
            raise ARDMediathekMalformedResponseError("ARD response lacks the series title")
        if not episodes_by_season:
            raise ARDMediathekMalformedResponseError("ARD response contains no numbered episodes")
        return NormalizedSeries(
            external_id=asset_id,
            title=series_title,
            seasons=tuple(
                NormalizedSeason(
                    external_id=f"{asset_id}:season:{season_number}",
                    number=season_number,
                    title=f"Season {season_number}",
                    episodes=tuple(sorted(episodes, key=lambda item: item.number or 0)),
                )
                for season_number, episodes in sorted(episodes_by_season.items())
            ),
        )

    @staticmethod
    def _mapping(value: Any, location: str) -> Mapping[str, Any]:
        if not isinstance(value, Mapping):
            raise ARDMediathekMalformedResponseError(f"{location} must be an object")
        return value

    @staticmethod
    def _integer(value: Any, location: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ARDMediathekMalformedResponseError(f"{location} must be a non-negative integer")
        return value

    @staticmethod
    def _datetime(value: Any, location: str, *, optional: bool = False) -> datetime | None:
        if value is None and optional:
            return None
        if not isinstance(value, str):
            raise ARDMediathekMalformedResponseError(f"{location} must be an ISO timestamp")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ARDMediathekMalformedResponseError(f"{location} is not an ISO timestamp") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ARDMediathekMalformedResponseError(f"{location} must be timezone-aware")
        return parsed.astimezone(UTC)

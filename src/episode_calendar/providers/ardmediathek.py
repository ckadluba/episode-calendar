"""ARD Mediathek programme catalogue adapter."""

from __future__ import annotations

import asyncio
import re
from collections import defaultdict
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from episode_calendar.config import get_settings
from episode_calendar.domain import ReleaseType
from episode_calendar.providers.base import (
    NormalizedEpisode,
    NormalizedEpisodeRelease,
    NormalizedSeason,
    NormalizedSeries,
)

_EPISODE_RE = re.compile(r"\(S(?P<season>\d+)/E(?P<episode>\d+)\)\s*$")
_TITLE_RE = re.compile(r"^(?:Folge\s+\d+\s*:\s*)?(?P<title>.*?)\s*\(S\d+/E\d+\)\s*$")


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
        settings = get_settings()
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
        # broadcast. Keep those catalogue releases so upcoming episodes are not missing
        # from the calendar; scheduled broadcasts are merged on top in
        # ``_merge_scheduled_releases``.
        normalized = self._normalize(asset_id, teasers)
        (
            scheduled_releases,
            scheduled_descriptions,
            broadcast_targets,
        ) = await self._fetch_scheduled_releases(client, normalized.title)
        catalog_descriptions = await self._fetch_catalog_descriptions(client, teasers)
        normalized = self._with_descriptions(normalized, catalog_descriptions)
        return self._merge_scheduled_releases(
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
    ) -> dict[str, str]:
        requests = [
            self._fetch_catalog_description(client, teaser)
            for teaser in teasers
            if self._episode_id(teaser) and self._target_url(teaser)
        ]
        results = await asyncio.gather(*requests)
        return {episode_id: description for episode_id, description in results if description}

    async def _fetch_catalog_description(
        self, client: httpx.AsyncClient, teaser: Mapping[str, Any]
    ) -> tuple[str, str | None]:
        episode_id = self._episode_id(teaser)
        target_url = self._target_url(teaser)
        if episode_id is None or target_url is None:
            return "", None
        try:
            response = await client.get(target_url)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ARDMediathekHTTPError("ARD episode detail request failed") from exc
        widgets = payload.get("widgets") if isinstance(payload, Mapping) else None
        if not isinstance(widgets, list):
            return episode_id, None
        for widget in widgets:
            if not isinstance(widget, Mapping):
                continue
            embedded = widget.get("mediaCollection", {}).get("embedded")
            meta = embedded.get("meta") if isinstance(embedded, Mapping) else None
            description = meta.get("synopsis") if isinstance(meta, Mapping) else None
            if isinstance(description, str) and description.strip():
                return episode_id, description
        return episode_id, None

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
    def _same_title(left: Any, right: str) -> bool:
        if not isinstance(left, str):
            return False

        def normalize(value: str) -> str:
            return re.sub(r"\s*[-·–—]\s*", " ", value).casefold().strip()

        return normalize(left) == normalize(right)

    @classmethod
    def _merge_scheduled_releases(
        cls,
        series: NormalizedSeries,
        scheduled_releases: Mapping[str, tuple[NormalizedEpisodeRelease, ...]],
        scheduled_descriptions: Mapping[str, str] | None = None,
        broadcast_targets: Mapping[str, str] | None = None,
    ) -> NormalizedSeries:
        merged_series = series
        known_titles = {
            episode.title for season in merged_series.seasons for episode in season.episodes
        }
        known_external_ids = {
            episode.external_id for season in merged_series.seasons for episode in season.episodes
        }
        descriptions = scheduled_descriptions or {}
        descriptions_by_episode = {
            cls._description_key(episode.description): episode
            for season in merged_series.seasons
            for episode in season.episodes
            if episode.description
        }
        targets = broadcast_targets or {}
        target_matches: dict[str, list[NormalizedEpisodeRelease]] = defaultdict(list)
        title_matches: dict[str, tuple[NormalizedEpisodeRelease, ...]] = {}
        for title, releases in scheduled_releases.items():
            untargeted: list[NormalizedEpisodeRelease] = []
            for release in releases:
                target = targets.get(release.external_id or "")
                if target and target in known_external_ids:
                    target_matches[target].append(release)
                else:
                    untargeted.append(release)
            if untargeted:
                title_matches[title] = tuple(untargeted)
        for title, releases in list(title_matches.items()):
            if title in known_titles:
                continue
            matching_releases = tuple(
                release
                for release in releases
                if release.external_id
                and any(
                    cls._descriptions_match(
                        descriptions.get(release.external_id), episode.description
                    )
                    for episode in descriptions_by_episode.values()
                )
            )
            if matching_releases:
                scheduled_description = descriptions[matching_releases[0].external_id or ""]
                matched_episode = next(
                    episode
                    for episode in descriptions_by_episode.values()
                    if cls._descriptions_match(scheduled_description, episode.description)
                )
                title_matches[matched_episode.title] = matching_releases
                title_matches.pop(title, None)

        merged_series = series.model_copy(
            update={
                "seasons": tuple(
                    season.model_copy(
                        update={
                            "episodes": tuple(
                                episode.model_copy(
                                    update={
                                        "releases": episode.releases
                                        + tuple(
                                            release
                                            for release in (
                                                *target_matches.get(episode.external_id, ()),
                                                *title_matches.get(episode.title, ()),
                                            )
                                            if release.external_id
                                            not in {
                                                existing.external_id
                                                for existing in episode.releases
                                            }
                                        )
                                    }
                                )
                                for episode in season.episodes
                            )
                        }
                    )
                    for season in series.seasons
                )
            }
        )

        scheduled_only = [
            (title, releases)
            for title, releases in title_matches.items()
            if title not in known_titles
        ]
        if not scheduled_only:
            return merged_series

        scheduled_season = next(
            (season for season in merged_series.seasons if season.number is None), None
        )
        if scheduled_season is None:
            scheduled_season = NormalizedSeason(
                external_id=f"{series.external_id}:scheduled",
                number=None,
                title="Scheduled",
            )
            seasons = (*merged_series.seasons, scheduled_season)
        else:
            seasons = merged_series.seasons
        scheduled_episodes = tuple(
            NormalizedEpisode(
                external_id=releases[0].external_id or f"{series.external_id}:{title}",
                number=None,
                title=title,
                releases=releases,
            )
            for title, releases in scheduled_only
        )
        seasons = tuple(
            season.model_copy(update={"episodes": season.episodes + scheduled_episodes})
            if season.external_id == scheduled_season.external_id
            else season
            for season in seasons
        )
        return merged_series.model_copy(update={"seasons": seasons})

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

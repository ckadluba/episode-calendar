"""BBC iPlayer programme metadata provider."""

from __future__ import annotations

import asyncio
import random
import re
from collections import defaultdict
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
from pydantic import ValidationError

from episode_calendar.config import get_settings
from episode_calendar.domain import ReleaseType
from episode_calendar.providers.base import (
    NormalizedEpisode,
    NormalizedEpisodeRelease,
    NormalizedSeason,
    NormalizedSeries,
)

_SEASON_RE = re.compile(r"(?:series|cyfres)\s+(\d+)", re.IGNORECASE)
_EPISODE_RE = re.compile(
    r"(?:series|cyfres)\s+\d+\s*:\s*(?:(?:episode|folge|week)\s+)?(\d+)",
    re.IGNORECASE,
)


class BBCIPlayerProviderError(RuntimeError):
    """Base class for BBC iPlayer catalogue failures."""


class BBCIPlayerHTTPError(BBCIPlayerProviderError):
    pass


class BBCIPlayerMalformedResponseError(BBCIPlayerProviderError):
    pass


class BBCIPlayerProvider:
    """Fetch public BBC iPlayer Business Layer metadata by programme PID.

    PIDs are configured rather than discovered at import time. The endpoint is public web
    metadata, but its contract is not a versioned partner API, so response validation stays
    deliberately strict and all BBC-specific parsing remains in this adapter.
    """

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        endpoint: str | None = None,
        timeout: float | None = None,
        schedule_channels: str | None = None,
        schedule_days: int | None = None,
    ) -> None:
        settings = get_settings()
        self._client = client
        self._endpoint = endpoint or settings.bbc_iplayer_api_url
        self._timeout = timeout if timeout is not None else settings.bbc_iplayer_timeout_seconds
        self._max_retries = settings.import_max_retries
        self._backoff = settings.import_backoff_seconds
        self._schedule_channels = tuple(
            channel.strip()
            for channel in (schedule_channels or settings.bbc_iplayer_schedule_channels).split(",")
            if channel.strip()
        )
        self._schedule_days = (
            schedule_days if schedule_days is not None else settings.bbc_iplayer_schedule_days
        )
        self._schedule_cache: dict[tuple[str, date], Mapping[str, Any]] = {}

    @property
    def slug(self) -> str:
        return "bbc_iplayer"

    async def fetch_series(self, external_id: str) -> NormalizedSeries:
        return await self.get_series(external_id)

    async def get_series(self, pid: str) -> NormalizedSeries:
        pid = pid.strip()
        if not pid or not re.fullmatch(r"[a-z0-9]{8,}", pid):
            raise ValueError("BBC iPlayer identifier must be an alphanumeric programme PID")
        if self._client is not None:
            return await self._fetch(self._client, pid)
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            return await self._fetch(client, pid)

    async def _fetch(self, client: httpx.AsyncClient, pid: str) -> NormalizedSeries:
        programme_payload = await self._request(
            client,
            f"/programmes/{pid}",
            {"rights": "web", "availability": "all", "initial_child_count": 1},
        )
        programme = self._one_programme(programme_payload, "programmes")
        episodes: list[Mapping[str, Any]] = []
        page = 1
        total: int | None = None
        while True:
            payload = await self._request(
                client,
                f"/programmes/{pid}/episodes",
                {
                    "rights": "web",
                    "availability": "all",
                    "page": page,
                    "per_page": 100,
                    "initial_child_count": 1,
                },
            )
            listing = self._mapping(payload.get("programme_episodes"), "programme_episodes")
            current_page = self._integer(listing.get("page"), "programme_episodes.page")
            if current_page != page:
                raise BBCIPlayerMalformedResponseError("BBC pagination returned an unexpected page")
            if total is None:
                total = self._integer(listing.get("count"), "programme_episodes.count")
            page_items = self._list(listing.get("elements"), "programme_episodes.elements")
            episodes.extend(
                self._mapping(item, "programme_episodes.elements[]") for item in page_items
            )
            if len(episodes) >= total:
                break
            if not page_items:
                raise BBCIPlayerMalformedResponseError("BBC pagination returned no episodes")
            page += 1

        known_episode_ids = {
            episode.get("id") for episode in episodes if isinstance(episode.get("id"), str)
        }
        for scheduled in await self._scheduled_episodes(client, pid):
            if scheduled.get("id") not in known_episode_ids:
                episodes.append(scheduled)

        return self._normalize(pid, programme, episodes)

    async def _scheduled_episodes(
        self, client: httpx.AsyncClient, pid: str
    ) -> list[Mapping[str, Any]]:
        """Return future broadcast episodes announced in the BBC channel schedules."""
        scheduled: list[Mapping[str, Any]] = []
        start = datetime.now(UTC).date()
        for offset in range(max(0, self._schedule_days)):
            schedule_date = start + timedelta(days=offset)
            for channel in self._schedule_channels:
                cache_key = (channel, schedule_date)
                payload = self._schedule_cache.get(cache_key)
                if payload is None:
                    payload = await self._request(
                        client,
                        f"/channels/{channel}/schedule/{schedule_date.isoformat()}",
                        {"lang": "en", "rights": "web", "availability": "all"},
                        allow_not_found=True,
                    )
                    self._schedule_cache[cache_key] = payload
                schedule = self._mapping(payload.get("schedule"), "schedule", optional=True)
                for element in self._list(schedule.get("elements", []), "schedule.elements"):
                    item = self._mapping(element, "schedule.elements[]")
                    raw_episode = self._mapping(item.get("episode"), "schedule.elements[].episode")
                    if raw_episode.get("tleo_id") != pid:
                        continue
                    planned_at = item.get("scheduled_start") or item.get("transmission_start")
                    if not planned_at:
                        continue
                    planned_episode = dict(raw_episode)
                    planned_episode["_planned_release_at"] = planned_at
                    planned_episode["versions"] = []
                    scheduled.append(planned_episode)
        return scheduled

    async def _request(
        self,
        client: httpx.AsyncClient,
        path: str,
        params: Mapping[str, Any],
        *,
        allow_not_found: bool = False,
    ) -> Mapping[str, Any]:
        for attempt in range(self._max_retries + 1):
            try:
                response = await client.get(f"{self._endpoint.rstrip('/')}{path}", params=params)
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < self._max_retries:
                        retry_after = response.headers.get("Retry-After")
                        delay = (
                            float(retry_after)
                            if retry_after and retry_after.replace(".", "", 1).isdigit()
                            else self._backoff * (2**attempt) + random.random() * 0.25
                        )
                        await asyncio.sleep(delay)
                        continue
                if response.status_code == 404 and allow_not_found:
                    return {}
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise BBCIPlayerMalformedResponseError("BBC response is not an object")
                if "error" in payload:
                    raise BBCIPlayerMalformedResponseError("BBC response contains an error")
                return payload
            except BBCIPlayerMalformedResponseError:
                raise
            except httpx.HTTPStatusError as exc:
                raise BBCIPlayerHTTPError(f"BBC iPlayer HTTP {exc.response.status_code}") from exc
            except httpx.RequestError as exc:
                if attempt >= self._max_retries:
                    raise BBCIPlayerHTTPError(str(exc)) from exc
                await asyncio.sleep(self._backoff * (2**attempt) + random.random() * 0.25)
            except ValueError as exc:
                raise BBCIPlayerMalformedResponseError("BBC response was not valid JSON") from exc
        raise BBCIPlayerHTTPError("BBC iPlayer request failed after retries")

    @classmethod
    def _normalize(
        cls, pid: str, programme: Mapping[str, Any], raw_episodes: list[Mapping[str, Any]]
    ) -> NormalizedSeries:
        seasons: dict[int | None, list[NormalizedEpisode]] = defaultdict(list)
        for raw in raw_episodes:
            episode = cls._episode(raw)
            if episode is None:
                continue
            season_number = cls._season_number(raw.get("subtitle"))
            seasons[season_number].append(episode)

        try:
            return NormalizedSeries(
                external_id=pid,
                title=cls._required_string(programme.get("title"), "programme.title"),
                description=cls._description(programme),
                seasons=tuple(
                    NormalizedSeason(
                        external_id=f"{pid}:season:{number if number is not None else 'specials'}",
                        number=number,
                        title=f"Series {number}" if number is not None else "Specials",
                        episodes=tuple(items),
                    )
                    for number, items in sorted(
                        seasons.items(), key=lambda item: (item[0] is None, item[0] or 0)
                    )
                ),
            )
        except ValidationError as exc:
            raise BBCIPlayerMalformedResponseError(
                "BBC programme did not match normalized model"
            ) from exc

    @classmethod
    def _episode(cls, raw: Mapping[str, Any]) -> NormalizedEpisode | None:
        episode_id = cls._required_string(raw.get("id"), "episode.id")
        title = cls._required_string(raw.get("title"), "episode.title")
        release = cls._release(raw)
        if release is None:
            return None
        subtitle = raw.get("subtitle")
        return NormalizedEpisode(
            external_id=episode_id,
            number=cls._episode_number(subtitle),
            title=cls._episode_title(raw, title),
            description=cls._description(raw),
            releases=(
                NormalizedEpisodeRelease(
                    external_id=episode_id,
                    release_type=ReleaseType.STREAMING,
                    release_at=release[0],
                    available_until=release[1],
                    url=f"https://www.bbc.co.uk/iplayer/episode/{episode_id}",
                ),
            ),
        )

    @classmethod
    def _release(cls, raw: Mapping[str, Any]) -> tuple[datetime, datetime | None] | None:
        if raw.get("_planned_release_at"):
            return cls._datetime(raw["_planned_release_at"], "schedule.scheduled_start"), None
        versions = cls._list(raw.get("versions", []), "episode.versions")
        candidates = [item for item in versions if isinstance(item, dict)]
        candidates.sort(key=lambda item: 0 if item.get("kind") == "original" else 1)
        for version in candidates:
            availability = version.get("availability")
            if not isinstance(availability, dict) or availability.get("start") is None:
                continue
            start = cls._datetime(availability["start"], "episode.version.availability.start")
            end = (
                cls._datetime(availability["end"], "episode.version.availability.end")
                if availability.get("end")
                else None
            )
            return start, end
        if raw.get("release_date_time"):
            return cls._datetime(raw["release_date_time"], "episode.release_date_time"), None
        return None

    @staticmethod
    def _description(raw: Mapping[str, Any]) -> str | None:
        synopses = raw.get("synopses")
        if not isinstance(synopses, dict):
            return None
        return next(
            (
                synopses[key]
                for key in ("medium", "small", "large")
                if isinstance(synopses.get(key), str)
            ),
            None,
        )

    @staticmethod
    def _season_number(value: Any) -> int | None:
        match = _SEASON_RE.search(value) if isinstance(value, str) else None
        return int(match.group(1)) if match else None

    @staticmethod
    def _episode_number(value: Any) -> int | None:
        match = _EPISODE_RE.search(value) if isinstance(value, str) else None
        return int(match.group(1)) if match else None

    @staticmethod
    def _episode_title(raw: Mapping[str, Any], fallback: str) -> str:
        subtitle = raw.get("subtitle")
        if not isinstance(subtitle, str) or not subtitle.strip():
            return fallback
        title = re.sub(r"^(?:series|cyfres)\s+\d+\s*:\s*", "", subtitle, flags=re.IGNORECASE)
        title = re.sub(r"^\d+\.\s*", "", title)
        return title.strip() or fallback

    @staticmethod
    def _datetime(value: Any, location: str) -> datetime:
        if not isinstance(value, str):
            raise BBCIPlayerMalformedResponseError(f"{location} must be an ISO timestamp")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise BBCIPlayerMalformedResponseError(f"{location} is not an ISO timestamp") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise BBCIPlayerMalformedResponseError(f"{location} must be timezone-aware")
        return parsed.astimezone(UTC)

    @staticmethod
    def _one_programme(payload: Mapping[str, Any], location: str) -> Mapping[str, Any]:
        items = BBCIPlayerProvider._list(payload.get(location), location)
        if len(items) != 1:
            raise BBCIPlayerMalformedResponseError(f"{location} must contain one programme")
        return BBCIPlayerProvider._mapping(items[0], f"{location}[]")

    @staticmethod
    def _mapping(value: Any, location: str, *, optional: bool = False) -> Mapping[str, Any]:
        if optional and value is None:
            return {}
        if not isinstance(value, Mapping):
            raise BBCIPlayerMalformedResponseError(f"{location} must be an object")
        return value

    @staticmethod
    def _list(value: Any, location: str) -> list[Any]:
        if not isinstance(value, list):
            raise BBCIPlayerMalformedResponseError(f"{location} must be a list")
        return value

    @staticmethod
    def _integer(value: Any, location: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise BBCIPlayerMalformedResponseError(f"{location} must be a non-negative integer")
        return value

    @staticmethod
    def _required_string(value: Any, location: str) -> str:
        if not isinstance(value, str) or not value:
            raise BBCIPlayerMalformedResponseError(f"{location} must be a non-empty string")
        return value

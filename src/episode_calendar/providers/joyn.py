from __future__ import annotations

import asyncio
import logging
import random
import re
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from json import JSONDecodeError
from typing import Any, ClassVar

import httpx
from pydantic import ValidationError

from episode_calendar.config import get_settings
from episode_calendar.domain import TV_BROADCAST_MATCH_TOLERANCE, ReleaseType
from episode_calendar.providers.base import (
    NormalizedEpisode,
    NormalizedEpisodeRelease,
    NormalizedSeason,
    NormalizedSeries,
)

SERIES_QUERY = """
query EpisodeCalendarSeries($path: String!) {
    page(path: $path) {
        __typename
        ... on SeriesPage {
            path
            series {
                id
                title
                description
                seasons {
                    id
                    number
                    numberOfEpisodes
                    episodes(first: 20, offset: 0) {
                        id
                        number
                        startsAt
                        airdate
                        endsAt
                        title
                        path
                        markings
                    }
                }
            }
        }
    }
}
"""

SEASON_QUERY = """
query EpisodeCalendarSeason($id: ID!, $first: Int!, $offset: Int!) {
    season(id: $id) {
        id
        number
        numberOfEpisodes
        episodes(first: $first, offset: $offset) {
            id
            number
            startsAt
            airdate
            endsAt
            title
            path
            markings
        }
    }
}
"""

EPG_QUERY = """
query EpisodeCalendarJoynEpg($from: Timestamp!, $to: Timestamp!) {
    liveStreams(first: 5000, offset: 0) {
        id
        title
        epgEvents(from: $from, to: $to) {
            startDate
            endDate
            program {
                ... on EpgEntry { __typename id title }
                ... on Episode { __typename id title }
                ... on Movie { __typename id title }
            }
        }
    }
}
"""

EPG_LOOKAHEAD = timedelta(days=28)
MAX_EPG_EVENT_DURATION = timedelta(hours=4)
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _EpgEvent:
    stream_id: str
    stream_title: str
    external_id: str
    title: str
    start: datetime
    end: datetime | None


class JoynProviderError(RuntimeError):
    """Base class for failures while retrieving or normalizing Joyn metadata."""


class JoynHTTPError(JoynProviderError):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(f"Joyn HTTP {status_code}: {message}")
        self.status_code = status_code


class JoynGraphQLError(JoynProviderError):
    """Joyn returned a GraphQL response containing one or more errors."""


class JoynMalformedResponseError(JoynProviderError):
    """Joyn returned JSON that does not match the catalog contract."""


class JoynProvider:
    """Fetch and normalize catalog metadata from Joyn Austria.

    The website currently accepts full GraphQL POST documents using the same public API key
    and headers as its persisted-query client. The API key is public web-client configuration,
    supplied at runtime rather than stored in this repository.
    """

    _epg_tasks: ClassVar[dict[tuple[str, str, date], asyncio.Task[tuple[_EpgEvent, ...]]]] = {}

    def __init__(
        self,
        api_key: str | None = None,
        *,
        endpoint: str | None = None,
        timeout: float | None = None,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        settings = get_settings() if api_key is None else None
        self._api_key = (
            api_key if api_key is not None else (settings.joyn_api_key if settings else None)
        )
        self._endpoint = (
            endpoint
            if endpoint is not None
            else (settings.joyn_graphql_url if settings else "https://api.joyn.de/graphql")
        )
        self._timeout = (
            timeout
            if timeout is not None
            else (settings.joyn_timeout_seconds if settings else 10.0)
        )
        self._client = client
        self._clock = clock or (lambda: datetime.now(UTC))
        self._max_retries = settings.import_max_retries if settings else 3
        self._backoff = settings.import_backoff_seconds if settings else 1.0
        if not self._api_key:
            raise ValueError("Joyn API key is required (set JOYN_API_KEY)")

    @property
    def slug(self) -> str:
        return "joyn"

    async def fetch_series(self, external_id: str) -> NormalizedSeries:
        return await self.get_series(external_id)

    async def get_series(self, identifier: str) -> NormalizedSeries:
        """Retrieve a complete series by its Joyn URL slug or `/serien/...` path.

        Joyn's current detail operation is path-based. The response's `series.id` is retained
        as the normalized provider external ID.
        """

        path = self._series_path(identifier)
        if self._client is not None:
            return await self._get_series(self._client, path)
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            return await self._get_series(client, path)

    async def _get_series(self, client: httpx.AsyncClient, path: str) -> NormalizedSeries:
        payload = await self._request(client, "EpisodeCalendarSeries", SERIES_QUERY, {"path": path})
        page = self._mapping(payload.get("page"), "data.page")
        if page.get("__typename") != "SeriesPage":
            raise JoynMalformedResponseError("data.page is not a SeriesPage")
        raw_series = self._mapping(page.get("series"), "data.page.series")
        raw_seasons = self._list(raw_series.get("seasons"), "data.page.series.seasons")
        seasons: list[NormalizedSeason] = []
        for raw_season in raw_seasons:
            season = self._mapping(raw_season, "season")
            episodes = self._list(season.get("episodes"), "season.episodes")
            total = self._episode_total(season.get("numberOfEpisodes"), len(episodes))
            while len(episodes) < total:
                offset = len(episodes)
                next_page = await self._request(
                    client,
                    "EpisodeCalendarSeason",
                    SEASON_QUERY,
                    {
                        "id": self._required_string(season.get("id"), "season.id"),
                        "first": 20,
                        "offset": offset,
                    },
                )
                page_season = self._mapping(next_page.get("season"), "data.season")
                if page_season.get("id") != season.get("id"):
                    raise JoynMalformedResponseError("paginated season ID changed")
                page_episodes = self._list(page_season.get("episodes"), "data.season.episodes")
                if not page_episodes:
                    raise JoynMalformedResponseError(
                        f"pagination returned no episodes at offset {offset}"
                    )
                episodes.extend(page_episodes)
                if len(episodes) > total:
                    raise JoynMalformedResponseError(
                        "pagination returned more episodes than advertised"
                    )
            try:
                seasons.append(
                    NormalizedSeason(
                        external_id=self._required_string(season.get("id"), "season.id"),
                        number=self._optional_int(season.get("number"), "season.number"),
                        episodes=tuple(self._episode(item) for item in episodes),
                    )
                )
            except ValidationError as exc:
                raise JoynMalformedResponseError(
                    "season did not match the normalized model"
                ) from exc
        try:
            normalized = NormalizedSeries(
                external_id=self._required_string(raw_series.get("id"), "series.id"),
                title=self._required_string(raw_series.get("title"), "series.title"),
                description=raw_series.get("description"),
                seasons=tuple(seasons),
            )
        except ValidationError as exc:
            raise JoynMalformedResponseError("series did not match the normalized model") from exc
        epg_season = await self._epg_season(client, normalized)
        return normalized.model_copy(
            update={
                "seasons": normalized.seasons + ((epg_season,) if epg_season is not None else ())
            }
        )

    async def _epg_season(
        self, client: httpx.AsyncClient, normalized: NormalizedSeries
    ) -> NormalizedSeason | None:
        """Add upcoming linear broadcasts not in Joyn's VOD catalogue yet.

        The EPG has no reliable season/episode relation, so these entries are kept in a
        synthetic season and deliberately have no episode number. Exact title matching avoids
        confusing similarly named programmes such as ``FBI: Most Wanted`` with ``MOST WANTED``.
        """

        try:
            events = await self._epg_events(client)
        except Exception as exc:  # EPG is supplementary; the catalog remains usable without it.
            logger.warning("Joyn EPG unavailable for %s: %s", normalized.title, exc)
            return None
        catalog_release_times = {
            release.release_at
            for season in normalized.seasons
            for episode in season.episodes
            for release in episode.releases
        }
        matching = [
            event
            for event in events
            if self._same_title(event.title, normalized.title)
            and not any(
                release_at.date() == event.start.date()
                and abs(release_at - event.start) <= TV_BROADCAST_MATCH_TOLERANCE
                for release_at in catalog_release_times
            )
        ]
        if not matching:
            return None
        episodes = tuple(
            NormalizedEpisode(
                external_id=event.external_id,
                number=None,
                title=event.title,
                releases=(
                    NormalizedEpisodeRelease(
                        external_id=event.external_id,
                        release_type=ReleaseType.TV_BROADCAST,
                        release_at=event.start,
                        available_until=event.end,
                    ),
                ),
            )
            for event in matching
        )
        return NormalizedSeason(external_id="joyn-epg", number=None, title="EPG", episodes=episodes)

    async def _epg_events(self, client: httpx.AsyncClient) -> tuple[_EpgEvent, ...]:
        now = self._clock().astimezone(UTC)
        key = (self._endpoint, self._api_key or "", now.date())
        task = self._epg_tasks.get(key)
        if task is None:
            task = asyncio.create_task(self._fetch_epg(client, now))
            self._epg_tasks[key] = task
        try:
            return await task
        except Exception:
            if self._epg_tasks.get(key) is task:
                del self._epg_tasks[key]
            raise

    async def _fetch_epg(self, client: httpx.AsyncClient, now: datetime) -> tuple[_EpgEvent, ...]:
        events: dict[str, _EpgEvent] = {}
        day = now
        end = now + EPG_LOOKAHEAD
        while day < end:
            next_day = min(day + timedelta(days=1), end)
            payload = await self._request(
                client,
                "EpisodeCalendarJoynEpg",
                EPG_QUERY,
                {"from": int(day.timestamp()), "to": int(next_day.timestamp())},
            )
            streams = self._list(payload.get("liveStreams"), "data.liveStreams")
            for raw_stream in streams:
                stream = self._mapping(raw_stream, "live stream")
                stream_id = self._required_string(stream.get("id"), "liveStream.id")
                stream_title = self._required_string(stream.get("title"), "liveStream.title")
                raw_events = self._list(stream.get("epgEvents"), "liveStream.epgEvents")
                for raw_event in raw_events:
                    event = self._mapping(raw_event, "epg event")
                    program = self._mapping(event.get("program"), "epg event.program")
                    title = self._required_string(program.get("title"), "epg program.title")
                    start = self._timestamp(event.get("startDate"), "epg event.startDate")
                    if start is None:
                        raise JoynMalformedResponseError("epg event.startDate was missing")
                    event_end = self._timestamp(event.get("endDate"), "epg event.endDate")
                    if event_end is not None and event_end - start > MAX_EPG_EVENT_DURATION:
                        # Long entries are usually continuous live/program blocks, not episodes.
                        continue
                    program_id = self._required_string(program.get("id"), "epg program.id")
                    external_id = f"epg:{stream_id}:{program_id}:{int(start.timestamp())}"
                    events[external_id] = _EpgEvent(
                        stream_id=stream_id,
                        stream_title=stream_title,
                        external_id=external_id,
                        title=title,
                        start=start,
                        end=event_end,
                    )
            day = next_day
        return tuple(sorted(events.values(), key=lambda event: event.start))

    @staticmethod
    def _same_title(left: str, right: str) -> bool:
        def normalize(value: str) -> str:
            value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
            return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()

        return normalize(left) == normalize(right)

    async def _request(
        self,
        client: httpx.AsyncClient,
        operation_name: str,
        query: str,
        variables: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        headers = {
            "Content-Type": "application/json",
            "Joyn-Platform": "web",
            "Joyn-Client-Version": "5.1579.1",
            "Joyn-Distribution-Tenant": "JOYN_AT",
            "Joyn-Country": "AT",
            "x-api-key": self._api_key,
        }
        for attempt in range(self._max_retries + 1):
            try:
                response = await client.post(
                    self._endpoint,
                    headers=headers,
                    json={"operationName": operation_name, "variables": variables, "query": query},
                )
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < self._max_retries:
                        retry_after = response.headers.get("Retry-After")
                        delay = (
                            float(retry_after)
                            if retry_after and retry_after.isdigit()
                            else self._backoff * (2**attempt) + random.random() * 0.25
                        )
                        await asyncio.sleep(delay)
                        continue
                response.raise_for_status()
                break
            except httpx.RequestError as exc:
                if attempt >= self._max_retries:
                    raise JoynHTTPError(0, str(exc)) from exc
                await asyncio.sleep(self._backoff * (2**attempt) + random.random() * 0.25)
            except httpx.HTTPStatusError as exc:
                raise JoynHTTPError(exc.response.status_code, exc.response.text[:500]) from exc
        try:
            payload = response.json()
        except (JSONDecodeError, ValueError) as exc:
            raise JoynMalformedResponseError("response was not valid JSON") from exc
        if not isinstance(payload, dict):
            raise JoynMalformedResponseError("response root was not an object")
        errors = payload.get("errors")
        if errors is not None and not isinstance(errors, list):
            raise JoynMalformedResponseError("response errors was not an array")
        if errors:
            messages = ", ".join(
                str(error.get("message", "unknown GraphQL error"))
                for error in errors
                if isinstance(error, dict)
            )
            raise JoynGraphQLError(messages or "unknown GraphQL error")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise JoynMalformedResponseError("response did not contain an object in data")
        return data

    def _episode(self, raw: Any) -> NormalizedEpisode:
        episode = self._mapping(raw, "episode")
        episode_id = self._required_string(episode.get("id"), "episode.id")
        starts_at = self._timestamp(episode.get("startsAt"), "episode.startsAt")
        airdate = self._timestamp(episode.get("airdate"), "episode.airdate")
        available_until = self._timestamp(episode.get("endsAt"), "episode.endsAt")
        markings = episode.get("markings")
        preview = isinstance(markings, list) and "PREVIEW" in markings
        path = episode.get("path")
        url = self._episode_url(path)
        releases: list[NormalizedEpisodeRelease] = []
        if starts_at is not None:
            releases.append(
                NormalizedEpisodeRelease(
                    release_type=ReleaseType.STREAMING,
                    release_at=starts_at,
                    available_until=available_until,
                    url=url,
                    preview=preview,
                )
            )
            if airdate is not None and airdate != starts_at:
                releases.append(
                    NormalizedEpisodeRelease(
                        release_type=ReleaseType.TV_BROADCAST,
                        release_at=airdate,
                    )
                )
        elif airdate is not None:
            # Older catalog responses sometimes expose only the linear airdate. Keep
            # those episodes usable as streaming releases, preserving the historical
            # fallback while treating an explicit startsAt as authoritative.
            path = episode.get("path")
            releases.append(
                NormalizedEpisodeRelease(
                    release_type=ReleaseType.STREAMING,
                    release_at=airdate,
                    available_until=available_until,
                    url=url,
                    preview=preview,
                )
            )
        try:
            return NormalizedEpisode(
                external_id=episode_id,
                number=self._optional_int(episode.get("number"), "episode.number"),
                title=self._required_string(episode.get("title"), "episode.title"),
                releases=tuple(releases),
            )
        except ValidationError as exc:
            raise JoynMalformedResponseError("episode did not match the normalized model") from exc

    @staticmethod
    def _series_path(identifier: str) -> str:
        if not identifier:
            raise ValueError("Joyn series identifier must not be empty")
        if identifier.startswith("http://") or identifier.startswith("https://"):
            identifier = httpx.URL(identifier).path
        if identifier.startswith("/"):
            return identifier
        return f"/serien/{identifier}"

    @staticmethod
    def _mapping(value: Any, location: str) -> Mapping[str, Any]:
        if not isinstance(value, dict):
            raise JoynMalformedResponseError(f"{location} was not an object")
        return value

    @staticmethod
    def _list(value: Any, location: str) -> list[Any]:
        if not isinstance(value, list):
            raise JoynMalformedResponseError(f"{location} was not an array")
        return value

    @staticmethod
    def _required_string(value: Any, location: str) -> str:
        if not isinstance(value, str) or not value:
            raise JoynMalformedResponseError(f"{location} was missing or not a string")
        return value

    @staticmethod
    def _optional_int(value: Any, location: str) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            raise JoynMalformedResponseError(f"{location} was not an integer")
        return value

    @classmethod
    def _episode_total(cls, value: Any, loaded: int) -> int:
        if value is None:
            if loaded >= 20:
                raise JoynMalformedResponseError(
                    "season.numberOfEpisodes is required for pagination"
                )
            return loaded
        if isinstance(value, bool) or not isinstance(value, int) or value < loaded:
            raise JoynMalformedResponseError("season.numberOfEpisodes was invalid")
        return value

    @staticmethod
    def _timestamp(value: Any, location: str) -> datetime | None:
        if value is None:
            return None
        try:
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return datetime.fromtimestamp(value, tz=UTC)
            if isinstance(value, str):
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if parsed.tzinfo is None or parsed.utcoffset() is None:
                    raise ValueError("timestamp was naive")
                return parsed.astimezone(UTC)
        except (OverflowError, OSError, ValueError) as exc:
            raise JoynMalformedResponseError(f"{location} was not a valid timestamp") from exc
        raise JoynMalformedResponseError(f"{location} had an unexpected type")

    @staticmethod
    def _episode_url(path: Any) -> str | None:
        if path is None:
            return None
        if not isinstance(path, str) or not path:
            raise JoynMalformedResponseError("episode.path was not a string")
        return path if path.startswith("http") else f"https://www.joyn.at{path}"

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from json import JSONDecodeError
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
                        airdate
                        endsAt
                        title
                        path
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
            airdate
            endsAt
            title
            path
        }
    }
}
"""


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

    def __init__(
        self,
        api_key: str | None = None,
        *,
        endpoint: str | None = None,
        timeout: float | None = None,
        client: httpx.AsyncClient | None = None,
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
            return NormalizedSeries(
                external_id=self._required_string(raw_series.get("id"), "series.id"),
                title=self._required_string(raw_series.get("title"), "series.title"),
                description=raw_series.get("description"),
                seasons=tuple(seasons),
            )
        except ValidationError as exc:
            raise JoynMalformedResponseError("series did not match the normalized model") from exc

    async def _request(
        self,
        client: httpx.AsyncClient,
        operation_name: str,
        query: str,
        variables: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        try:
            response = await client.post(
                self._endpoint,
                headers={
                    "Content-Type": "application/json",
                    "Joyn-Platform": "web",
                    "Joyn-Client-Version": "5.1579.1",
                    "Joyn-Distribution-Tenant": "JOYN_AT",
                    "Joyn-Country": "AT",
                    "x-api-key": self._api_key,
                },
                json={"operationName": operation_name, "variables": variables, "query": query},
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise JoynHTTPError(exc.response.status_code, exc.response.text[:500]) from exc
        except httpx.RequestError as exc:
            raise JoynHTTPError(0, str(exc)) from exc
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
        release_at = self._timestamp(episode.get("airdate"), "episode.airdate")
        available_until = self._timestamp(episode.get("endsAt"), "episode.endsAt")
        releases = ()
        if release_at is not None:
            path = episode.get("path")
            url = self._episode_url(path)
            releases = (
                NormalizedEpisodeRelease(
                    release_type=ReleaseType.STREAMING,
                    release_at=release_at,
                    available_until=available_until,
                    url=url,
                ),
            )
        try:
            return NormalizedEpisode(
                external_id=episode_id,
                number=self._optional_int(episode.get("number"), "episode.number"),
                title=self._required_string(episode.get("title"), "episode.title"),
                releases=releases,
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

"""Amazon Prime Video Germany detail-page adapter."""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx

from episode_calendar.config import get_settings
from episode_calendar.domain import ReleaseType
from episode_calendar.providers.base import (
    NormalizedEpisode,
    NormalizedEpisodeRelease,
    NormalizedSeason,
    NormalizedSeries,
)
from episode_calendar.providers.http import request_with_retries

_GERMAN_TIMEZONE = ZoneInfo("Europe/Berlin")
_GERMAN_MONTHS = {
    "Januar": 1,
    "Jan.": 1,
    "Februar": 2,
    "Feb.": 2,
    "März": 3,
    "Mrz.": 3,
    "April": 4,
    "Apr.": 4,
    "Mai": 5,
    "Juni": 6,
    "Jun.": 6,
    "Juli": 7,
    "Jul.": 7,
    "August": 8,
    "Aug.": 8,
    "September": 9,
    "Sept.": 9,
    "Oktober": 10,
    "Okt.": 10,
    "November": 11,
    "Nov.": 11,
    "Dezember": 12,
    "Dez.": 12,
}


class AmazonPrimeProviderError(RuntimeError):
    """Base class for Amazon Prime Video failures."""


class AmazonPrimeHTTPError(AmazonPrimeProviderError):
    pass


class AmazonPrimeMalformedResponseError(AmazonPrimeProviderError):
    pass


class AmazonPrimeProvider:
    """Read episode metadata from Prime Video's public detail-page data endpoint.

    Identifiers are Prime Video title IDs, such as an ASIN or ``amzn1.dv.gti.*``.
    The endpoint is used by the public Prime Video web application, but is not a
    documented consumer API and may change without notice.
    """

    provider_slug = "amazon_prime_de"
    provider_label = "Amazon Prime DE"
    base_url_setting = "amazon_prime_de_base_url"
    timeout_setting = "amazon_prime_de_timeout_seconds"
    marketplace = "de"

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float | None = None,
        base_url: str | None = None,
        max_retries: int | None = None,
        backoff: float | None = None,
    ) -> None:
        settings = get_settings()
        self._client = client
        self._timeout = timeout if timeout is not None else getattr(settings, self.timeout_setting)
        self._base_url = (base_url or getattr(settings, self.base_url_setting)).rstrip("/")
        self._max_retries = max_retries if max_retries is not None else settings.import_max_retries
        self._backoff = backoff if backoff is not None else settings.import_backoff_seconds

    @property
    def slug(self) -> str:
        return self.provider_slug

    async def fetch_series(self, external_id: str) -> NormalizedSeries:
        return await self.get_series(external_id)

    async def get_series(self, external_id: str) -> NormalizedSeries:
        title_id = external_id.strip()
        if not title_id or "/" in title_id or "?" in title_id:
            raise ValueError(f"{self.provider_label} identifier must be a title ID")
        if self._client is not None:
            return await self._fetch(self._client, title_id)
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            return await self._fetch(client, title_id)

    async def _fetch(self, client: httpx.AsyncClient, title_id: str) -> NormalizedSeries:
        try:
            response = await request_with_retries(
                lambda: client.get(
                    f"{self._base_url}/gp/video/api/getDetailPage",
                    params=[
                        ("titleID", title_id),
                        ("isElcano", "1"),
                        ("sections", "Atf"),
                        ("sections", "Btf"),
                    ],
                    headers={
                        "Accept": "application/json",
                        "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
                        "User-Agent": "Mozilla/5.0 (compatible; episode-calendar/1.0)",
                        "X-Requested-With": "XMLHttpRequest",
                    },
                ),
                max_retries=self._max_retries,
                backoff=self._backoff,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise AmazonPrimeHTTPError(
                f"{self.provider_label} HTTP {exc.response.status_code}"
            ) from exc
        except httpx.RequestError as exc:
            detail = str(exc) or type(exc).__name__
            raise AmazonPrimeHTTPError(f"{self.provider_label} request failed: {detail}") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise AmazonPrimeMalformedResponseError(
                f"{self.provider_label} response was not JSON"
            ) from exc
        return self._normalize(title_id, payload)

    @classmethod
    def _normalize(cls, title_id: str, payload: Any) -> NormalizedSeries:
        if not isinstance(payload, Mapping):
            raise AmazonPrimeMalformedResponseError(
                f"{cls.provider_label} response is not an object"
            )

        series_title = cls._find_string(payload, {"seriesTitle", "showTitle", "parentTitle"})
        if series_title is None:
            series_title = cls._find_string(payload, {"title", "name", "titleName"})
        if series_title is None:
            raise AmazonPrimeMalformedResponseError(
                f"{cls.provider_label} response lacks a series title"
            )

        episodes: dict[str, tuple[int | None, int, str, str | None, datetime]] = {}
        cls._collect_episodes(payload, episodes, cls._find_positive_int(payload, "seasonNumber"))
        if not episodes:
            raise AmazonPrimeMalformedResponseError(
                f"{cls.provider_label} response contains no dated episodes"
            )

        seasons: dict[int | None, list[NormalizedEpisode]] = {}
        for external_id, (
            season_number,
            episode_number,
            title,
            description,
            release_at,
        ) in episodes.items():
            url = (
                f"https://www.primevideo.com/-/{cls.marketplace}/detail/"
                f"{quote(external_id, safe='.-_')}"
            )
            seasons.setdefault(season_number, []).append(
                NormalizedEpisode(
                    external_id=external_id,
                    number=episode_number,
                    title=title,
                    description=description,
                    releases=(
                        NormalizedEpisodeRelease(
                            external_id=external_id,
                            release_type=ReleaseType.STREAMING,
                            release_at=release_at,
                            url=url,
                        ),
                    ),
                )
            )

        return NormalizedSeries(
            external_id=title_id,
            title=series_title,
            seasons=tuple(
                NormalizedSeason(
                    external_id=f"{title_id}:season:{season_number or 'specials'}",
                    number=season_number,
                    title=f"Season {season_number}" if season_number is not None else "Specials",
                    episodes=tuple(sorted(items, key=lambda item: item.number or 0)),
                )
                for season_number, items in sorted(
                    seasons.items(), key=lambda item: (item[0] is None, item[0] or 0)
                )
            ),
        )

    @classmethod
    def _collect_episodes(
        cls,
        value: Any,
        episodes: dict[str, tuple[int | None, int, str, str | None, datetime]],
        season_hint: int | None = None,
    ) -> None:
        if isinstance(value, list):
            for item in value:
                cls._collect_episodes(item, episodes, season_hint)
            return
        if not isinstance(value, Mapping):
            return

        season_number = cls._positive_int(value, "seasonNumber", "seasonIndex", "season")
        current_season = season_number or season_hint
        episode_number = cls._positive_int(
            value, "episodeNumber", "episodeIndex", "episodeSequence", "sequenceNumber"
        )
        external_id = cls._first_string(value, "titleId", "titleID", "gti", "GTI", "catalogId")
        release_at = cls._release_at(value)
        if episode_number is not None and external_id is not None and release_at is not None:
            title = (
                cls._first_string(
                    value,
                    "episodeTitle",
                    "episodeName",
                    "displayTitle",
                    "titleName",
                    "title",
                    "name",
                )
                or f"Episode {episode_number}"
            )
            description = cls._first_string(value, "synopsis", "description", "summary")
            if external_id not in episodes:
                episodes[external_id] = (
                    current_season,
                    episode_number,
                    title,
                    description,
                    release_at,
                )

        for key, child in value.items():
            child_season = current_season
            if key in {"season", "seasonInfo", "seasonMetadata"} and isinstance(child, Mapping):
                child_season = (
                    cls._positive_int(child, "seasonNumber", "seasonIndex", "number")
                    or current_season
                )
            cls._collect_episodes(child, episodes, child_season)

    @classmethod
    def _release_at(cls, value: Mapping[str, Any]) -> datetime | None:
        for key in (
            "releaseDateTime",
            "releaseDate",
            "availabilityStartDate",
            "originalReleaseDate",
            "startDate",
        ):
            raw = value.get(key)
            if isinstance(raw, str):
                try:
                    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                except ValueError:
                    parsed = cls._parse_german_date(raw)
                    if parsed is None:
                        continue
                if parsed.tzinfo is None or parsed.utcoffset() is None:
                    parsed = parsed.replace(tzinfo=_GERMAN_TIMEZONE)
                return parsed.astimezone(UTC)
            if isinstance(raw, (int, float)) and math.isfinite(raw):
                seconds = raw / 1000 if raw > 10_000_000_000 else raw
                return datetime.fromtimestamp(seconds, UTC)
        return None

    @staticmethod
    def _parse_german_date(value: str) -> datetime | None:
        parts = value.strip().split()
        if len(parts) != 3 or not parts[0].rstrip(".").isdigit():
            return None
        month = _GERMAN_MONTHS.get(parts[1])
        if month is None or not parts[2].isdigit():
            return None
        return datetime(
            year=int(parts[2]),
            month=month,
            day=int(parts[0].rstrip(".")),
            tzinfo=_GERMAN_TIMEZONE,
        )

    @staticmethod
    def _first_string(value: Mapping[str, Any], *keys: str) -> str | None:
        for key in keys:
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        return None

    @staticmethod
    def _find_string(value: Any, keys: set[str]) -> str | None:
        if isinstance(value, Mapping):
            for key, candidate in value.items():
                if key in keys and isinstance(candidate, str) and candidate.strip():
                    return candidate.strip()
                found = AmazonPrimeProvider._find_string(candidate, keys)
                if found:
                    return found
        elif isinstance(value, list):
            for candidate in value:
                found = AmazonPrimeProvider._find_string(candidate, keys)
                if found:
                    return found
        return None

    @staticmethod
    def _find_positive_int(value: Any, key: str) -> int | None:
        if isinstance(value, Mapping):
            candidate = value.get(key)
            if isinstance(candidate, int) and candidate > 0:
                return candidate
            for child in value.values():
                found = AmazonPrimeProvider._find_positive_int(child, key)
                if found is not None:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = AmazonPrimeProvider._find_positive_int(child, key)
                if found is not None:
                    return found
        return None

    @staticmethod
    def _positive_int(value: Mapping[str, Any], *keys: str) -> int | None:
        for key in keys:
            candidate = value.get(key)
            if isinstance(candidate, bool):
                continue
            try:
                number = int(candidate)
            except (TypeError, ValueError):
                continue
            if number > 0:
                return number
        return None


class AmazonPrimeDEProvider(AmazonPrimeProvider):
    """Amazon Prime Video Germany adapter."""


AmazonPrimeDEProviderError = AmazonPrimeProviderError
AmazonPrimeDEHTTPError = AmazonPrimeHTTPError
AmazonPrimeDEMalformedResponseError = AmazonPrimeMalformedResponseError

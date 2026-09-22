"""Channel 4 programme catalogue adapter."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any
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

_DATE_RE = re.compile(
    r"First shown:\s*(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+"
    r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})",
    re.IGNORECASE,
)
_MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}


class Channel4ProviderError(RuntimeError):
    """Base class for Channel 4 catalogue failures."""


class Channel4HTTPError(Channel4ProviderError):
    pass


class Channel4MalformedResponseError(Channel4ProviderError):
    pass


class Channel4Provider:
    """Read the public Channel 4 brand JSON endpoint.

    Channel 4 exposes the same catalogue data used by its programme pages through
    ``?json=true``. Its dates are first-broadcast dates and are therefore imported as
    streaming releases at midnight UK time.
    """

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float | None = None,
        base_url: str | None = None,
    ) -> None:
        settings = get_settings()
        self._client = client
        self._timeout = timeout if timeout is not None else settings.channel4_timeout_seconds
        self._base_url = (base_url or settings.channel4_base_url).rstrip("/")

    @property
    def slug(self) -> str:
        return "channel4"

    async def fetch_series(self, external_id: str) -> NormalizedSeries:
        return await self.get_series(external_id)

    async def get_series(self, external_id: str) -> NormalizedSeries:
        if self._client is not None:
            return await self._fetch(self._client, external_id)
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            return await self._fetch(client, external_id)

    async def _fetch(self, client: httpx.AsyncClient, slug: str) -> NormalizedSeries:
        try:
            response = await client.get(f"{self._base_url}/{slug}", params={"json": "true"})
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise Channel4HTTPError(f"Channel 4 HTTP {exc.response.status_code}") from exc
        except httpx.RequestError as exc:
            raise Channel4HTTPError(str(exc)) from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise Channel4MalformedResponseError("Channel 4 response was not JSON") from exc
        return self._normalize(slug, payload)

    @classmethod
    def _normalize(cls, slug: str, payload: Any) -> NormalizedSeries:
        brand_data = payload.get("brandData") if isinstance(payload, dict) else None
        brand = brand_data.get("brand") if isinstance(brand_data, dict) else None
        if not isinstance(brand, dict):
            raise Channel4MalformedResponseError("Channel 4 response lacks brand data")
        title = brand.get("title")
        episodes = brand.get("episodes")
        if not isinstance(title, str) or not title:
            raise Channel4MalformedResponseError("Channel 4 response lacks programme title")
        if not isinstance(episodes, list):
            raise Channel4MalformedResponseError("Channel 4 response lacks episodes")

        seasons: dict[int, list[NormalizedEpisode]] = {}
        for raw in episodes:
            if not isinstance(raw, dict):
                raise Channel4MalformedResponseError("Channel 4 episode is malformed")
            season_number = raw.get("seriesNumber")
            episode_number = raw.get("episodeNumber")
            episode_id = raw.get("programmeId")
            if not isinstance(season_number, int) or not isinstance(episode_number, int):
                continue
            if not isinstance(episode_id, str) or not episode_id:
                raise Channel4MalformedResponseError("Channel 4 episode lacks programmeId")
            release = cls._release(raw)
            releases = () if release is None else (release,)
            episode = NormalizedEpisode(
                external_id=episode_id,
                number=episode_number,
                title=str(raw.get("fullTitle") or raw.get("title") or f"Episode {episode_number}"),
                description=raw.get("description") or raw.get("summary"),
                releases=releases,
            )
            seasons.setdefault(season_number, []).append(episode)

        return NormalizedSeries(
            external_id=slug,
            title=title,
            description=brand.get("summary") or brand.get("shortSummary"),
            seasons=tuple(
                NormalizedSeason(
                    external_id=f"{slug}:series:{number}",
                    number=number,
                    title=f"Series {number}",
                    episodes=tuple(episodes_for_season),
                )
                for number, episodes_for_season in sorted(seasons.items())
            ),
        )

    @staticmethod
    def _release(raw: dict[str, Any]) -> NormalizedEpisodeRelease | None:
        label = raw.get("dateLabel")
        if not isinstance(label, str):
            return None
        match = _DATE_RE.search(label)
        if not match:
            return None
        day, month_name, year = match.groups()
        month = _MONTHS.get(month_name.lower())
        if month is None:
            return None
        release_at = datetime(int(year), month, int(day), tzinfo=ZoneInfo("Europe/London"))
        href = raw.get("hrefLink")
        url = f"https://www.channel4.com{href}" if isinstance(href, str) else None
        return NormalizedEpisodeRelease(
            external_id=str(raw.get("programmeId")) if raw.get("programmeId") else None,
            release_type=ReleaseType.STREAMING,
            release_at=release_at,
            url=url,
        )

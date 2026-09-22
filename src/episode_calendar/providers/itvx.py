"""ITVX programme page catalogue adapter."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from html import unescape
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

_NEXT_DATA_RE = re.compile(
    r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', re.IGNORECASE | re.DOTALL
)
_SERIES_EPISODE_RE = re.compile(
    r"(?:series|season)\s*(\d+)\D+(?:episode|ep)\s*(\d+)", re.IGNORECASE
)
_EPISODE_RE = re.compile(r"(?:episode|ep)\s*(\d+)", re.IGNORECASE)
_SERIES_RE = re.compile(r"(?:series|season)\s*(\d+)", re.IGNORECASE)
_LONDON = ZoneInfo("Europe/London")


class ITVXProviderError(RuntimeError):
    """Base class for ITVX catalogue failures."""


class ITVXHTTPError(ITVXProviderError):
    pass


class ITVXMalformedResponseError(ITVXProviderError):
    pass


class ITVXProvider:
    """Read catalogue metadata embedded in public ITVX programme pages.

    ITVX does not publish a stable catalogue API. Its programme pages contain the same
    normalized data used by the web client in ``__NEXT_DATA__``; parsing that document
    keeps the provider isolated from the page UI and avoids scraping rendered markup.
    Configure the programme path, for example ``big-brother/10a4928``.
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
        self._timeout = timeout if timeout is not None else settings.itvx_timeout_seconds
        self._base_url = (base_url or settings.itvx_base_url).rstrip("/")

    @property
    def slug(self) -> str:
        return "itvx"

    async def fetch_series(self, external_id: str) -> NormalizedSeries:
        return await self.get_series(external_id)

    async def get_series(self, external_id: str) -> NormalizedSeries:
        path = external_id.strip().strip("/")
        if not path or "://" in path:
            raise ValueError("ITVX identifier must be a programme path such as big-brother/10a4928")
        if self._client is not None:
            return await self._fetch(self._client, path)
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            return await self._fetch(client, path)

    async def _fetch(self, client: httpx.AsyncClient, path: str) -> NormalizedSeries:
        try:
            response = await client.get(
                f"{self._base_url}/{quote(path, safe='/')}",
                follow_redirects=True,
                headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-GB,en;q=0.9",
                    "Referer": "https://www.itv.com/",
                    "User-Agent": "Mozilla/5.0 (compatible; episode-calendar/1.0)",
                },
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ITVXHTTPError(f"ITVX HTTP {exc.response.status_code}") from exc
        except httpx.RequestError as exc:
            detail = str(exc) or type(exc).__name__
            raise ITVXHTTPError(f"ITVX request failed: {detail}") from exc
        return self._normalize(path, response.text)

    @classmethod
    def _normalize(cls, path: str, html: str) -> NormalizedSeries:
        match = _NEXT_DATA_RE.search(html)
        if match is None:
            raise ITVXMalformedResponseError("ITVX page lacks __NEXT_DATA__")
        try:
            data = json.loads(unescape(match.group(1)))
        except json.JSONDecodeError as exc:
            raise ITVXMalformedResponseError("ITVX __NEXT_DATA__ is not valid JSON") from exc
        if not isinstance(data, Mapping):
            raise ITVXMalformedResponseError("ITVX __NEXT_DATA__ is not an object")
        page_props = cls._mapping(cls._mapping(data.get("props")).get("pageProps"))
        series_list = cls._list(page_props.get("seriesList"))
        if not series_list:
            raise ITVXMalformedResponseError("ITVX page contains no series list")

        programme = cls._mapping(page_props.get("programme"))
        series_title = cls._first_string(
            page_props,
            ("programmeTitle", "showTitle", "title", "name"),
        ) or cls._first_string(programme, ("title", "name"))
        if not series_title:
            raise ITVXMalformedResponseError("ITVX page lacks a programme title")
        seasons: dict[int | None, list[NormalizedEpisode]] = {}
        for raw_series in series_list:
            series = cls._mapping(raw_series)
            season_hint = cls._season_number(series)
            for raw_title in cls._list(series.get("titles")):
                episode = cls._episode(path, raw_title, season_hint)
                if episode is None:
                    continue
                number = cls._season_number(raw_title) or season_hint
                seasons.setdefault(number, []).append(episode)
        if not seasons:
            raise ITVXMalformedResponseError("ITVX page contains no dated episodes")

        return NormalizedSeries(
            external_id=path,
            title=series_title,
            description=cls._first_string(page_props, ("synopsis", "description"))
            or cls._first_string(programme, ("synopsis", "description")),
            seasons=tuple(
                NormalizedSeason(
                    external_id=f"{path}:season:{number if number is not None else 'specials'}",
                    number=number,
                    title=f"Series {number}" if number is not None else "Specials",
                    episodes=tuple(sorted(episodes, key=lambda item: item.number or 0)),
                )
                for number, episodes in sorted(
                    seasons.items(), key=lambda item: (item[0] is None, item[0] or 0)
                )
            ),
        )

    @classmethod
    def _episode(
        cls, path: str, raw_title: Any, season_hint: int | None
    ) -> NormalizedEpisode | None:
        title = cls._mapping(raw_title)
        external_id = cls._external_id(title)
        if not external_id:
            return None
        release_at = cls._release_at(title)
        if release_at is None:
            return None
        numbered_title = cls._first_string(title, ("episodeTitle", "title", "numberedEpisodeTitle"))
        if not numbered_title:
            numbered_title = f"Episode {cls._episode_number(title) or 0}"
        _, episode_number = cls._numbers(numbered_title, title, season_hint)
        url = cls._first_string(title, ("url", "href"))
        if not url:
            url = f"https://www.itv.com/watch/{path}/{external_id}"
        return NormalizedEpisode(
            external_id=external_id,
            number=episode_number,
            title=numbered_title,
            description=cls._first_string(title, ("synopsis", "description", "summary")),
            releases=(
                NormalizedEpisodeRelease(
                    external_id=external_id,
                    release_type=ReleaseType.STREAMING,
                    release_at=release_at,
                    url=url,
                ),
            ),
        )

    @classmethod
    def _numbers(
        cls, text: str, title: Mapping[str, Any], season_hint: int | None
    ) -> tuple[int | None, int | None]:
        combined = _SERIES_EPISODE_RE.search(text)
        season = combined and int(combined.group(1))
        episode = combined and int(combined.group(2))
        if episode is None:
            episode = cls._episode_number(title)
        if season is None:
            season = season_hint
        return season, episode

    @staticmethod
    def _external_id(title: Mapping[str, Any]) -> str | None:
        for key in ("id", "episodeId", "contentId"):
            value = title.get(key)
            if isinstance(value, str) and value:
                return value
        encoded = title.get("encodedEpisodeId")
        if isinstance(encoded, Mapping):
            for value in encoded.values():
                if isinstance(value, str) and value:
                    return value
        return None

    @classmethod
    def _release_at(cls, title: Mapping[str, Any]) -> datetime | None:
        for key in (
            "broadcastDateTime",
            "transmissionDateTime",
            "releaseDateTime",
            "startDateTime",
            "dateTime",
        ):
            value = title.get(key)
            if isinstance(value, str):
                try:
                    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if parsed.tzinfo is None or parsed.utcoffset() is None:
                    parsed = parsed.replace(tzinfo=_LONDON)
                return parsed.astimezone(UTC)
        return None

    @staticmethod
    def _mapping(value: Any) -> Mapping[str, Any]:
        return value if isinstance(value, Mapping) else {}

    @staticmethod
    def _list(value: Any) -> list[Any]:
        return value if isinstance(value, list) else []

    @staticmethod
    def _first_string(mapping: Mapping[str, Any], keys: tuple[str, ...]) -> str | None:
        return next(
            (
                mapping[key].strip()
                for key in keys
                if isinstance(mapping.get(key), str) and mapping[key].strip()
            ),
            None,
        )

    @staticmethod
    def _season_number(value: Mapping[str, Any]) -> int | None:
        for key in ("seriesNumber", "seasonNumber", "series", "season", "number"):
            raw = value.get(key)
            if isinstance(raw, int) and raw > 0:
                return raw
            if isinstance(raw, str) and raw.isdigit() and int(raw) > 0:
                return int(raw)
        text = ITVXProvider._first_string(value, ("numberedEpisodeTitle", "title", "name"))
        match = _SERIES_RE.search(text or "")
        return int(match.group(1)) if match else None

    @staticmethod
    def _episode_number(value: Mapping[str, Any]) -> int | None:
        for key in ("episodeNumber", "number"):
            raw = value.get(key)
            if isinstance(raw, int) and raw > 0:
                return raw
            if isinstance(raw, str) and raw.isdigit() and int(raw) > 0:
                return int(raw)
        text = ITVXProvider._first_string(value, ("numberedEpisodeTitle", "episodeTitle", "title"))
        match = _EPISODE_RE.search(text or "")
        return int(match.group(1)) if match else None

"""Channel 4 programme catalogue adapter."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
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
_SERIES_EPISODE_RE = re.compile(r"\(S(\d+)\s+Ep(\d+)", re.IGNORECASE)
_INITIAL_DATA_RE = re.compile(
    r'<script[^>]+id="initialData"[^>]*>var initialData = (.*?);?</script>', re.DOTALL
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


@dataclass(frozen=True)
class _ScheduledProgramme:
    external_id: str
    season_number: int
    episode_number: int
    title: str
    description: str | None
    release: NormalizedEpisodeRelease


class Channel4Provider:
    """Read the public Channel 4 brand JSON endpoint.

    Channel 4 exposes the same catalogue data used by its programme pages through
    ``?json=true``. First-broadcast dates are imported as streaming releases at midnight
    UK time; planned C4 transmissions are merged from the TV guide with their exact times.
    """

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float | None = None,
        base_url: str | None = None,
        tv_guide_url: str | None = None,
        schedule_days: int | None = None,
    ) -> None:
        settings = get_settings()
        self._client = client
        self._timeout = timeout if timeout is not None else settings.channel4_timeout_seconds
        self._base_url = (base_url or settings.channel4_base_url).rstrip("/")
        self._tv_guide_url = (tv_guide_url or settings.channel4_tv_guide_url).rstrip("/")
        self._schedule_days = (
            schedule_days if schedule_days is not None else settings.channel4_schedule_days
        )
        self._tv_guide_cache: dict[date, tuple[dict[str, Any], ...]] = {}

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
        scheduled_releases = await self._fetch_scheduled_releases(client, slug)
        return self._normalize(slug, payload, scheduled_releases=scheduled_releases)

    @classmethod
    def _normalize(
        cls,
        slug: str,
        payload: Any,
        *,
        scheduled_releases: dict[str, tuple[_ScheduledProgramme, ...]] | None = None,
    ) -> NormalizedSeries:
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
            releases_by_id = {}
            if release is not None:
                releases_by_id[release.external_id] = release
            for scheduled_programme in (scheduled_releases or {}).get(episode_id, ()):
                releases_by_id[None] = scheduled_programme.release
            episode = NormalizedEpisode(
                external_id=episode_id,
                number=episode_number,
                title=str(raw.get("fullTitle") or raw.get("title") or f"Episode {episode_number}"),
                description=raw.get("description") or raw.get("summary"),
                releases=tuple(releases_by_id.values()),
            )
            seasons.setdefault(season_number, []).append(episode)

        existing_episode_ids = {
            episode.external_id for episodes in seasons.values() for episode in episodes
        }
        for episode_id, programmes in (scheduled_releases or {}).items():
            if episode_id in existing_episode_ids:
                continue
            for programme in programmes:
                seasons.setdefault(programme.season_number, []).append(
                    NormalizedEpisode(
                        external_id=programme.external_id,
                        number=programme.episode_number,
                        title=programme.title,
                        description=programme.description,
                        releases=(programme.release,),
                    )
                )

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

    async def _fetch_scheduled_releases(
        self, client: httpx.AsyncClient, slug: str
    ) -> dict[str, tuple[_ScheduledProgramme, ...]]:
        programmes_by_id: dict[str, list[_ScheduledProgramme]] = {}
        today = datetime.now(ZoneInfo("Europe/London")).date()
        for offset in range(max(self._schedule_days, 0)):
            guide_date = today + timedelta(days=offset)
            programmes = self._tv_guide_cache.get(guide_date)
            if programmes is None:
                response = await self._get_tv_guide(client, guide_date)
                if response is None:
                    continue
                programmes = self._parse_tv_guide(response.text)
                self._tv_guide_cache[guide_date] = programmes
            for programme in programmes:
                if programme.get("brandWebSafeTitle") != slug:
                    continue
                episode_id = programme.get("programmeId")
                start_date = programme.get("startDate")
                if not isinstance(episode_id, str) or not episode_id:
                    continue
                if not isinstance(start_date, str) or not start_date:
                    continue
                try:
                    release_at = datetime.fromisoformat(start_date.replace("Z", "+00:00"))
                except ValueError as exc:
                    raise Channel4MalformedResponseError(
                        f"Channel 4 schedule has an invalid startDate for {episode_id}"
                    ) from exc
                if release_at.tzinfo is None or release_at.utcoffset() is None:
                    raise Channel4MalformedResponseError(
                        f"Channel 4 schedule has a timezone-naive startDate for {episode_id}"
                    )
                url = programme.get("url")
                summary = programme.get("summary")
                match = _SERIES_EPISODE_RE.search(summary) if isinstance(summary, str) else None
                if match is None:
                    continue
                season_number, episode_number = (int(value) for value in match.groups())
                title = str(programme.get("title") or f"Episode {episode_number}")
                title = title.removeprefix("New: ").strip()
                scheduled_programme = _ScheduledProgramme(
                    external_id=episode_id,
                    season_number=season_number,
                    episode_number=episode_number,
                    title=title,
                    description=summary,
                    release=NormalizedEpisodeRelease(
                        external_id=None,
                        release_type=ReleaseType.TV_BROADCAST,
                        release_at=release_at,
                        url=f"https://www.channel4.com{url}" if isinstance(url, str) else None,
                    ),
                )
                programmes_by_id.setdefault(episode_id, []).append(scheduled_programme)
        return {episode_id: tuple(items) for episode_id, items in programmes_by_id.items()}

    async def _get_tv_guide(
        self, client: httpx.AsyncClient, guide_date: date
    ) -> httpx.Response | None:
        try:
            response = await client.get(f"{self._tv_guide_url}/{guide_date.isoformat()}")
            response.raise_for_status()
            return response
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise Channel4HTTPError(f"Channel 4 TV guide HTTP {exc.response.status_code}") from exc
        except httpx.RequestError as exc:
            raise Channel4HTTPError(str(exc)) from exc

    @classmethod
    def _parse_tv_guide(cls, html: str) -> tuple[dict[str, Any], ...]:
        match = _INITIAL_DATA_RE.search(html)
        if match is None:
            raise Channel4MalformedResponseError("Channel 4 TV guide lacks initial data")
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError as exc:
            raise Channel4MalformedResponseError(
                "Channel 4 TV guide initial data is invalid"
            ) from exc
        channels = data.get("channels") if isinstance(data, dict) else None
        channel = channels.get("C4") if isinstance(channels, dict) else None
        programmes = channel.get("programmes") if isinstance(channel, dict) else None
        if not isinstance(programmes, list):
            raise Channel4MalformedResponseError("Channel 4 TV guide lacks C4 programmes")
        return tuple(
            programme
            for programme in programmes
            if isinstance(programme, dict) and programme.get("isRepeat") is False
        )

"""STV Player catalogue adapter."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import quote

import httpx

from episode_calendar.importer.config import get_importer_settings
from episode_calendar.importer.providers.base import (
    NormalizedEpisode,
    NormalizedEpisodeRelease,
    NormalizedSeason,
    NormalizedSeries,
    duration_minutes_from_text,
    first_seen_release,
)
from episode_calendar.importer.providers.epg import (
    filter_obsolete_seasons,
    latest_release_moment,
    next_season_number,
)
from episode_calendar.importer.providers.http import request_with_retries
from episode_calendar.shared.domain import ReleaseType

_EPISODE_LINK_RE = re.compile(
    r"(?:https?://player\.stv\.tv)?/episode/([a-z0-9]{4})(?:/([^\"'?#<\s]+))?",
    re.IGNORECASE,
)
_SERIES_NUMBER_RE = re.compile(r"(?:series|season)\s+(\d+)", re.IGNORECASE)
_NEXT_DATA_RE = re.compile(
    r"<script[^>]+id=[\"']__NEXT_DATA__[\"'][^>]*>(.*?)</script>", re.IGNORECASE | re.DOTALL
)
_BRAND_NEW_SERIES_RE = re.compile(r"new series", re.IGNORECASE)


@dataclass(frozen=True)
class _AnnouncedPremiere:
    """The programme's next linear airing as announced on its summary page."""

    title: str
    start: datetime
    end: datetime | None = None


class STVProviderError(RuntimeError):
    """Base class for STV Player catalogue failures."""


class STVHTTPError(STVProviderError):
    """The STV Player or API request failed."""


class STVMalformedResponseError(STVProviderError):
    """The STV Player response did not contain catalogue data."""


class STVProvider:
    """Read episode metadata from the public STV Player API.

    The configured identifier keeps the historic programme-id shape, for example
    ``im-a-celebrity-get-me-out-of-here/L2649``.  The first path component is
    the STV Player programme slug; the second component is retained so that
    configured series remain stable across the provider migration.
    """

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float | None = None,
        player_url: str | None = None,
        api_url: str | None = None,
        max_retries: int | None = None,
        backoff: float | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        settings = get_importer_settings()
        self._client = client
        self._timeout = timeout if timeout is not None else settings.stv_timeout_seconds
        self._player_url = (player_url or settings.stv_player_url).rstrip("/")
        self._api_url = (api_url or settings.stv_api_url).rstrip("/")
        self._max_retries = max_retries if max_retries is not None else settings.import_max_retries
        self._backoff = backoff if backoff is not None else settings.import_backoff_seconds
        self._clock = clock or (lambda: datetime.now(UTC))

    @property
    def slug(self) -> str:
        return "stv"

    async def fetch_series(self, external_id: str) -> NormalizedSeries:
        return await self.get_series(external_id)

    async def get_series(self, external_id: str) -> NormalizedSeries:
        programme_slug = self._programme_slug(external_id)
        if self._client is not None:
            return await self._fetch(self._client, external_id, programme_slug)
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            return await self._fetch(client, external_id, programme_slug)

    async def _fetch(
        self, client: httpx.AsyncClient, external_id: str, programme_slug: str
    ) -> NormalizedSeries:
        page = await self._request_text(
            client, f"{self._player_url}/summary/{quote(programme_slug)}"
        )
        episode_ids = tuple(dict.fromkeys(_EPISODE_LINK_RE.findall(page.replace("\\/", "/"))))
        if not episode_ids:
            raise STVMalformedResponseError(
                f"STV Player page contains no episodes for {programme_slug}"
            )

        responses = await asyncio.gather(
            *(
                self._request_json(client, f"{self._api_url}/episodes/{episode_id[0]}")
                for episode_id in episode_ids
            )
        )
        series = filter_obsolete_seasons(
            self._normalize(external_id, programme_slug, episode_ids, responses)
        )
        announced = self._announced_premiere(page)
        if announced is not None:
            series = self._with_announced_premiere(series, programme_slug, announced)
        return series

    async def _request_text(self, client: httpx.AsyncClient, url: str) -> str:
        try:
            response = await request_with_retries(
                lambda: client.get(
                    url,
                    follow_redirects=True,
                    headers={
                        "Accept": "text/html,application/xhtml+xml",
                        "Accept-Language": "en-GB,en;q=0.9",
                        "User-Agent": "episode-calendar/1.0",
                    },
                ),
                max_retries=self._max_retries,
                backoff=self._backoff,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise STVHTTPError(f"STV HTTP {exc.response.status_code}") from exc
        except httpx.RequestError as exc:
            detail = str(exc) or type(exc).__name__
            raise STVHTTPError(f"STV request failed: {detail}") from exc
        return response.text

    async def _request_json(self, client: httpx.AsyncClient, url: str) -> Mapping[str, object]:
        try:
            response = await request_with_retries(
                lambda: client.get(
                    url,
                    headers={
                        "Accept": "application/json",
                        "Accept-Language": "en-GB,en;q=0.9",
                        "User-Agent": "episode-calendar/1.0",
                    },
                ),
                max_retries=self._max_retries,
                backoff=self._backoff,
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as exc:
            raise STVHTTPError(f"STV API HTTP {exc.response.status_code}") from exc
        except (httpx.RequestError, ValueError) as exc:
            detail = str(exc) or type(exc).__name__
            raise STVHTTPError(f"STV API request failed: {detail}") from exc
        if not isinstance(payload, Mapping):
            raise STVMalformedResponseError("STV API response is not an object")
        return payload

    @classmethod
    def _normalize(
        cls,
        external_id: str,
        programme_slug: str,
        episode_ids: tuple[tuple[str, str], ...],
        responses: tuple[Mapping[str, object], ...],
    ) -> NormalizedSeries:
        episodes: dict[tuple[int | None, int | None], NormalizedEpisode] = {}
        series_title: str | None = None
        for (short_id, slug), payload in zip(episode_ids, responses, strict=True):
            result = cls._mapping(payload.get("results"))
            if not result:
                continue
            programme = cls._mapping(result.get("programme"))
            player_series = cls._mapping(result.get("playerSeries"))
            schedule = cls._mapping(result.get("schedule"))
            availability = cls._mapping(result.get("availability"))
            title = cls._string(result.get("title")) or (
                f"Episode {result.get('number', '')}".strip()
            )
            number = cls._integer(result.get("number"))
            season_number = cls._series_number(cls._string(player_series.get("name")))
            start_time = cls._datetime(
                cls._string(availability.get("from")) or cls._string(schedule.get("startTime"))
            )
            if number is None:
                continue
            series_title = series_title or cls._string(programme.get("name"))
            episode_key = (season_number, number)
            video = cls._mapping(result.get("video"))
            episode = NormalizedEpisode(
                external_id=cls._string(result.get("guid")) or short_id,
                number=number,
                title=title,
                description=cls._string(result.get("summary")),
                duration_minutes=duration_minutes_from_text(cls._string(video.get("_duration"))),
                releases=(
                    NormalizedEpisodeRelease(
                        external_id=cls._string(result.get("guid")) or short_id,
                        release_type=ReleaseType.STREAMING,
                        release_at=start_time,
                        available_until=cls._datetime(cls._string(availability.get("until"))),
                        url=f"https://player.stv.tv/episode/{short_id}/{slug or programme_slug}",
                    )
                    if start_time is not None
                    else first_seen_release(
                        datetime.now(UTC),
                        url=f"https://player.stv.tv/episode/{short_id}/{slug or programme_slug}",
                    ),
                ),
            )
            existing = episodes.get(episode_key)
            if existing is None or episode.releases[0].release_at < existing.releases[0].release_at:
                episodes[episode_key] = episode

        if not episodes or not series_title:
            raise STVMalformedResponseError(f"STV API contains no dated episodes for {external_id}")

        seasons: dict[int | None, list[NormalizedEpisode]] = {}
        for (season_number, _), episode in episodes.items():
            seasons.setdefault(season_number, []).append(episode)
        return NormalizedSeries(
            external_id=external_id,
            title=series_title,
            seasons=tuple(
                NormalizedSeason(
                    external_id=(
                        f"{external_id}:season:{number if number is not None else 'specials'}"
                    ),
                    number=number,
                    title=f"Series {number}" if number is not None else "Specials",
                    episodes=tuple(sorted(items, key=lambda item: item.number or 0)),
                )
                for number, items in sorted(
                    seasons.items(), key=lambda item: (item[0] is None, item[0] or 0)
                )
            ),
        )

    @classmethod
    def _announced_premiere(cls, page: str) -> _AnnouncedPremiere | None:
        """Read the programme's announced next series premiere from the summary page.

        STV announces an upcoming season on the programme page (a "new series" promo
        plus ``scheduleNext``/``nextEpisode``) before any of its episodes are in the
        on-demand catalogue. That announcement is the provider's own signal that the
        next airing starts a new season, so the adapter turns it into a dated premiere.
        """

        data = cls._next_data(page)
        programme = cls._mapping(data.get("programmeData"))
        header = cls._mapping(data.get("programmeHeader"))
        subtitle = cls._string(programme.get("promotionalSubtitle")) or cls._string(
            header.get("promotionalSubtitle")
        )
        if subtitle is None or _BRAND_NEW_SERIES_RE.search(subtitle) is None:
            return None
        schedule_next = cls._mapping(programme.get("scheduleNext"))
        next_episode = cls._mapping(data.get("nextEpisode"))
        start = cls._datetime(
            cls._string(schedule_next.get("startTime"))
            or cls._string(next_episode.get("startTime"))
        )
        if start is None:
            return None
        return _AnnouncedPremiere(
            title=cls._string(schedule_next.get("title"))
            or cls._string(next_episode.get("title"))
            or "",
            start=start,
            end=cls._datetime(cls._string(schedule_next.get("endTime"))),
        )

    def _with_announced_premiere(
        self,
        series: NormalizedSeries,
        programme_slug: str,
        announced: _AnnouncedPremiere,
    ) -> NormalizedSeries:
        """Add the announced next-season premiere when the catalogue cannot know it yet.

        The premiere is only synthesized when the catalogue's newest season is fully
        dated, the announcement lies beyond every catalogued release, and it is still in
        the future; otherwise the catalogue already carries the airing. The episode is
        emitted with the same identifiers the real catalogue entry will use, so a later
        import promotes it in place instead of duplicating it.
        """

        number = next_season_number(series)
        latest = latest_release_moment(series)
        if number is None or latest is None:
            return series
        if announced.start <= latest or announced.start <= self._clock():
            return series
        season_id = f"{series.external_id}:season:{number}"
        premiere = NormalizedEpisode(
            external_id=f"{season_id}:episode:1",
            number=1,
            title="Episode 1",
            releases=(
                NormalizedEpisodeRelease(
                    external_id=f"stv:{series.external_id}:{announced.start.isoformat()}",
                    release_type=ReleaseType.TV_BROADCAST,
                    release_at=announced.start,
                    available_until=announced.end,
                    url=f"{self._player_url}/summary/{quote(programme_slug)}",
                ),
            ),
        )
        season = NormalizedSeason(
            external_id=season_id,
            number=number,
            title=f"Series {number}",
            episodes=(premiere,),
        )
        return series.model_copy(update={"seasons": (*series.seasons, season)})

    @classmethod
    def _next_data(cls, page: str) -> Mapping[str, object]:
        """Return the ``pageProps.data`` object embedded in the summary page, if any."""

        match = _NEXT_DATA_RE.search(page)
        if match is None:
            return {}
        try:
            payload = json.loads(match.group(1))
        except ValueError:
            return {}
        if not isinstance(payload, Mapping):
            return {}
        props = cls._mapping(payload.get("props"))
        page_props = cls._mapping(props.get("pageProps"))
        return cls._mapping(page_props.get("data"))

    @staticmethod
    def _programme_slug(external_id: str) -> str:
        path = external_id.strip().strip("/")
        if not path or "/" not in path or "//" in path or "\\" in path:
            raise ValueError(
                "STV identifier must be a programme slug and legacy id, such as big-brother/10a4928"
            )
        return path.split("/", 1)[0]

    @staticmethod
    def _mapping(value: object) -> Mapping[str, object]:
        return value if isinstance(value, Mapping) else {}

    @staticmethod
    def _string(value: object) -> str | None:
        return value if isinstance(value, str) and value else None

    @staticmethod
    def _integer(value: object) -> int | None:
        return value if isinstance(value, int) else None

    @staticmethod
    def _series_number(value: str | None) -> int | None:
        match = _SERIES_NUMBER_RE.search(value or "")
        return int(match.group(1)) if match else None

    @staticmethod
    def _datetime(value: str | None) -> datetime | None:
        if not value:
            return None
        return datetime.fromisoformat(value.replace("+0000", "+00:00"))

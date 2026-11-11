"""STV Player catalogue adapter."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo

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
    DEFAULT_NEW_SEASON_MIN_GAP,
    EpgBroadcast,
    filter_obsolete_seasons,
    latest_release_moment,
    merge_epg,
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

# STV's linear schedule is the only source that knows an announced season before its
# episodes reach the on-demand catalogue. Only the main channel carries those premieres.
_STV_EPG_CHANNEL = "stv"
_STV_EPG_LIMIT = 100
STV_EPG_TIMEZONE = ZoneInfo("Europe/London")
STV_EPG_TOLERANCE = timedelta(hours=2)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _ScheduledAiring:
    """One linear airing from the STV schedule."""

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
        if self._announces_new_series(page):
            series = await self._merge_announced_schedule(client, series, programme_slug)
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
    def _announces_new_series(cls, page: str) -> bool:
        """Whether the programme page promotes a season the catalogue cannot list yet.

        STV announces an upcoming season on the programme page (a "new series" promo)
        before any of its episodes reach the on-demand catalogue. The catalogue alone
        cannot date that season, so the promo switches the adapter to the linear schedule.
        """

        data = cls._next_data(page)
        programme = cls._mapping(data.get("programmeData"))
        header = cls._mapping(data.get("programmeHeader"))
        subtitle = cls._string(programme.get("promotionalSubtitle")) or cls._string(
            header.get("promotionalSubtitle")
        )
        return subtitle is not None and _BRAND_NEW_SERIES_RE.search(subtitle) is not None

    async def _merge_announced_schedule(
        self, client: httpx.AsyncClient, series: NormalizedSeries, programme_slug: str
    ) -> NormalizedSeries:
        """Merge the linear airings of an announced season the catalogue does not list yet.

        STV's on-demand catalogue carries a season only once it is published; the upcoming
        season first appears in the linear schedule (``/v1/schedule``) with an empty
        ``episode``. Those unattributable airings are mapped to ``EpgBroadcast`` and merged
        with the shared policy: the earliest one declares the season the catalogue still
        lacks (``announces_season``), so the merger numbers the whole run in airing order.
        Only airings newer than the newest catalogued release are considered, and the
        announced season must be far enough from that release to be a new one rather than
        the running season's own cadence. The EPG is supplementary: a failing schedule
        leaves the catalogue import intact.
        """

        number = next_season_number(series)
        latest = latest_release_moment(series)
        if number is None or latest is None:
            return series
        try:
            payload = await self._request_json(
                client,
                f"{self._api_url}/schedule"
                f"?channel={_STV_EPG_CHANNEL}"
                f"&programme.guid={quote(programme_slug)}"
                f"&limit={_STV_EPG_LIMIT}",
            )
            airings = self._schedule_airings(payload, latest=latest)
        except STVProviderError as exc:  # the EPG is supplementary; keep the catalogue.
            logger.warning("STV schedule unavailable for %s: %s", series.title, exc)
            return series
        if not airings or airings[0].start - latest < DEFAULT_NEW_SEASON_MIN_GAP:
            return series
        broadcasts = tuple(
            EpgBroadcast(
                external_id=f"stv:{series.external_id}:{airing.start.isoformat()}",
                title=airing.title,
                start=airing.start,
                end=airing.end,
                url=f"{self._player_url}/summary/{quote(programme_slug)}",
                announces_season=number if index == 0 else None,
            )
            for index, airing in enumerate(airings)
        )
        return merge_epg(
            series,
            broadcasts,
            now=self._clock(),
            timezone=STV_EPG_TIMEZONE,
            tolerance=STV_EPG_TOLERANCE,
            next_episode_external_id=lambda season_id, number: f"epg:next:{season_id}:{number}",
        )

    @classmethod
    def _schedule_airings(
        cls, payload: Mapping[str, object], *, latest: datetime
    ) -> tuple[_ScheduledAiring, ...]:
        """The unattributable airings of the programme, in airing order.

        A schedule entry the catalogue already knows carries its ``episode`` object; only
        entries without one are the announced season. Airings at or before the newest
        catalogued release belong to an older, already-imported season and are skipped.
        """

        results = payload.get("results")
        if not isinstance(results, list):
            raise STVMalformedResponseError("STV schedule response lacks results")
        airings: list[_ScheduledAiring] = []
        for raw in results:
            entry = cls._mapping(raw)
            if entry.get("episode") is not None:
                continue
            if cls._string(entry.get("channel")) != _STV_EPG_CHANNEL:
                continue
            start = cls._datetime(cls._string(entry.get("startTime")))
            if start is None or start <= latest:
                continue
            airings.append(
                _ScheduledAiring(
                    title=cls._string(entry.get("title")) or "",
                    start=start,
                    end=cls._datetime(cls._string(entry.get("endTime"))),
                )
            )
        return tuple(sorted(airings, key=lambda airing: airing.start))

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

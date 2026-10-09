from __future__ import annotations

import asyncio
import logging
import random

# The provider mirrors a verbose external contract; long endpoint/field expressions are kept
# readable alongside the response structure.
# ruff: noqa: E501
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, ClassVar
from unicodedata import normalize as unicode_normalize
from uuid import uuid4
from zoneinfo import ZoneInfo

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

EPG_LOOKAHEAD = timedelta(days=14)
MAX_EPG_EVENT_DURATION = timedelta(hours=4)
EPG_MATCH_TOLERANCE = timedelta(minutes=15)
EPG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
EPG_TIMEZONE = ZoneInfo("Europe/Vienna")
logger = logging.getLogger(__name__)

_SEASON_RE = re.compile(r"Staffel\s+(\d+)", re.IGNORECASE)
_EPISODE_RE = re.compile(r"Folge\s+(\d+)", re.IGNORECASE)
_DATE_RE = re.compile(
    r"\*\*Folge\s+(\d+)\*\*\s*\|[^|]*\|[^|]*?\s*(?:Di\.|Do\.|Mi\.|Mo\.|Sa\.|So\.)?\s*(\d{1,2})\.(\d{1,2})\.,?\s*(\d{1,2}):(\d{2})\s*Uhr",
    re.IGNORECASE,
)
_TABLE_DATE_RE = re.compile(
    r"(?:[A-Za-zÄÖÜäöü]+\\?\.?\s*,?\s*)?(\d{1,2})\\?\.(\d{1,2})\\?\.?(?:\s*(?:ab|um)?\s*)?"
    r"(\d{1,2})(?::(\d{2}))?\s*(?:Uhr)?",
    re.IGNORECASE,
)
_START_DATE_RE = re.compile(
    r"(?:ab(?:\s+dem)?|start(?:et)?(?:\s+am)?)\s+(\d{1,2})\.\s*"
    r"(Januar|Februar|März|April|Mai|Juni|Juli|August|September|Oktober|November|Dezember)",
    re.IGNORECASE,
)
_WEEKDAY_RE = re.compile(
    r"\b(Montags?|Dienstags?|Mittwochs?|Donnerstags?|Freitags?|Samstags?|Sonntags?)\b",
    re.IGNORECASE,
)
_MONTHS = {
    "januar": 1,
    "februar": 2,
    "märz": 3,
    "april": 4,
    "mai": 5,
    "juni": 6,
    "juli": 7,
    "august": 8,
    "september": 9,
    "oktober": 10,
    "november": 11,
    "dezember": 12,
}


class RTLPlusProviderError(RuntimeError):
    """Base class for RTL+ catalog failures."""


class RTLPlusHTTPError(RTLPlusProviderError):
    pass


class RTLPlusMalformedResponseError(RTLPlusProviderError):
    pass


@dataclass(frozen=True)
class _EpgEvent:
    external_id: str
    title: str
    subtitle: str | None
    description: str | None
    start: datetime
    end: datetime | None


class RTLPlusProvider:
    """Read RTL+ Bedrock layout metadata for the German RTL+ catalogue.

    The layout endpoint and item fields are observed current web behaviour. Release times are
    parsed from the SEO markdown schedule table or the newer weekly cadence format.
    """

    _epg_tasks: ClassVar[dict[tuple[str, str, date], asyncio.Task[tuple[_EpgEvent, ...]]]] = {}

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float | None = None,
        endpoint_template: str | None = None,
        bedrock_token: str | None = None,
        authorization: str | None = None,
        oidc_client_secret: str | None = None,
        epg_endpoint: str | None = None,
        epg_channels: str | None = None,
        epg_lookback_days: int | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        settings = get_settings()
        self._client = client
        self._timeout = timeout if timeout is not None else settings.rtlplus_timeout_seconds
        self._endpoint_template = endpoint_template or settings.rtlplus_layout_url
        self._epg_endpoint = epg_endpoint or settings.rtlplus_epg_url
        self._epg_channels = tuple(
            channel.strip()
            for channel in (epg_channels or settings.rtlplus_epg_channels).split(",")
            if channel.strip()
        )
        self._epg_lookback_days = (
            epg_lookback_days
            if epg_lookback_days is not None
            else settings.rtlplus_epg_lookback_days
        )
        self._bedrock_token = bedrock_token or settings.rtlplus_bedrock_token
        self._authorization = authorization or settings.rtlplus_authorization
        self._oidc_url = settings.rtlplus_oidc_token_url
        self._oidc_client_id = settings.rtlplus_oidc_client_id
        self._oidc_client_secret = oidc_client_secret or settings.rtlplus_oidc_client_secret
        self._auth_url = settings.rtlplus_auth_url
        self._max_retries = settings.import_max_retries
        self._backoff = settings.import_backoff_seconds
        self._clock = clock or (lambda: datetime.now(UTC))

    @property
    def slug(self) -> str:
        return "rtlplus"

    async def fetch_series(self, external_id: str) -> NormalizedSeries:
        return await self.get_series(external_id)

    async def get_series(self, external_id: str) -> NormalizedSeries:
        match = re.search(r"(?:^|[_-])p_(\d+)$", str(external_id))
        program_id = match.group(1) if match else str(external_id)
        if not program_id.isdigit():
            raise ValueError("RTL+ program identifier must be numeric or end in _p_<id>")
        series_url = self._series_url(external_id)
        if self._client is not None:
            if not self._bedrock_token or not self._authorization:
                self._authorization, self._bedrock_token = await self._authenticate(self._client)
            normalized = await self._fetch(self._client, program_id, series_url)
            return await self._add_epg(self._client, normalized)
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            if not self._bedrock_token or not self._authorization:
                self._authorization, self._bedrock_token = await self._authenticate(client)
            normalized = await self._fetch(client, program_id, series_url)
            return await self._add_epg(client, normalized)

    async def _add_epg(
        self, client: httpx.AsyncClient, normalized: NormalizedSeries
    ) -> NormalizedSeries:
        """Merge RTL+'s linear TV guide into the catalog result.

        Catalog episodes remain authoritative for season/episode metadata and links. The EPG
        only supplies missing TV dates or creates numberless future entries, matching Joyn's
        complementary-source semantics.
        """

        try:
            events = await self._epg_events(client)
        except Exception as exc:  # EPG is supplementary; the catalog remains usable.
            logger.warning("RTL+ EPG unavailable for %s: %s", normalized.title, exc)
            return normalized

        matching = [event for event in events if self._same_title(event.title, normalized.title)]
        if not matching:
            return normalized

        seasons = [season.model_copy(deep=True) for season in normalized.seasons]
        catalog_episodes = [episode for season in seasons for episode in season.episodes]
        synthetic: list[NormalizedEpisode] = []
        for event in matching:
            release = NormalizedEpisodeRelease(
                external_id=event.external_id,
                release_type=ReleaseType.TV_BROADCAST,
                release_at=event.start,
                available_until=event.end,
            )
            # A catalog release earlier on the same day is the known RTL+ preview
            # pattern, even when the EPG labels another episode number. An episode
            # whose own premiere already aired cannot gain a preview: its later
            # same-day drop is a cadence artifact, not an early release.
            for candidate in tuple(episode for season in seasons for episode in season.episodes):
                updated_releases = tuple(
                    existing.model_copy(update={"preview": True})
                    if (
                        existing.release_type is ReleaseType.STREAMING
                        and not existing.preview
                        and existing.release_at.date() == event.start.date()
                        and existing.release_at < event.start
                        and not any(
                            other.release_type is ReleaseType.TV_BROADCAST
                            and other.release_at < existing.release_at
                            for other in candidate.releases
                        )
                    )
                    else existing
                    for existing in candidate.releases
                )
                if updated_releases != candidate.releases:
                    seasons = self._replace_episode_releases(
                        seasons, candidate.external_id, updated_releases
                    )
            episode_number = self._epg_episode_number(event)
            target = self._catalog_episode_for_epg(seasons, episode_number, event.start)
            if target is not None:
                # A matching catalog release shortly before the linear broadcast is a
                # preview, even when the two dates are several days apart. This is how
                # RTL+ publishes episodes such as Sommerhaus S11E7 (catalog: 6 Oct,
                # TV: 13 Oct). Limit the inference to the EPG look-ahead window so an
                # unrelated old release is not reclassified when an episode is rerun,
                # and keep an already-aired episode from gaining a preview.
                updated_releases = tuple(
                    existing.model_copy(update={"preview": True})
                    if (
                        existing.release_type is ReleaseType.STREAMING
                        and not existing.preview
                        and timedelta(0) < event.start - existing.release_at <= EPG_LOOKAHEAD
                        and not any(
                            other.release_type is ReleaseType.TV_BROADCAST
                            and other.release_at < existing.release_at
                            for other in target.releases
                        )
                    )
                    else existing
                    for existing in target.releases
                )
                if updated_releases != target.releases:
                    seasons = self._replace_episode_releases(
                        seasons, target.external_id, updated_releases
                    )
                    target = target.model_copy(update={"releases": updated_releases})
                if not any(
                    existing.release_at.date() == event.start.date()
                    and abs(existing.release_at - event.start) <= EPG_MATCH_TOLERANCE
                    for existing in target.releases
                ):
                    updated_releases += (release,)
                if updated_releases != target.releases:
                    seasons = self._replace_episode_releases(
                        seasons, target.external_id, updated_releases
                    )
                continue
            if any(
                existing.release_at.date() == event.start.date()
                and abs(existing.release_at - event.start) <= EPG_MATCH_TOLERANCE
                for episode in catalog_episodes
                for existing in episode.releases
            ):
                continue
            synthetic.append(
                NormalizedEpisode(
                    external_id=event.external_id,
                    number=None,
                    title=event.title,
                    description=event.description,
                    releases=(release,),
                )
            )

        if synthetic:
            seasons.append(
                NormalizedSeason(
                    external_id="rtlplus-epg",
                    number=None,
                    title="EPG",
                    episodes=tuple(synthetic),
                )
            )
        return normalized.model_copy(update={"seasons": tuple(seasons)})

    async def _epg_events(self, client: httpx.AsyncClient) -> tuple[_EpgEvent, ...]:
        now = self._clock().astimezone(UTC)
        key = (self._epg_endpoint, ",".join(self._epg_channels), now.date())
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
        # Include the recent past so premieres that aired shortly before the import (for
        # example a Wednesday-night lead-in already broadcast today) are not lost to a
        # from-now-only window; the central rerun policy keeps their night replays marked
        # as reruns.
        start = now - timedelta(days=self._epg_lookback_days)
        end = now + EPG_LOOKAHEAD
        events: dict[str, _EpgEvent] = {}
        offset = 0
        while True:
            response = await client.get(
                self._epg_endpoint,
                params={
                    "channel": ",".join(self._epg_channels),
                    "from": start.astimezone(EPG_TIMEZONE).strftime(EPG_DATE_FORMAT),
                    "to": end.astimezone(EPG_TIMEZONE).strftime(EPG_DATE_FORMAT),
                    "limit": 99,
                    "offset": offset,
                    "with": "realdiffusiondates",
                },
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise RTLPlusMalformedResponseError("RTL+ EPG response was not an object")
            page_count = 0
            for channel_events in payload.values():
                if not isinstance(channel_events, list):
                    raise RTLPlusMalformedResponseError("RTL+ EPG channel data was not an array")
                page_count += len(channel_events)
                for raw in channel_events:
                    if not isinstance(raw, dict):
                        raise RTLPlusMalformedResponseError("RTL+ EPG event was not an object")
                    event = self._epg_event(raw)
                    if event.end is not None and event.end - event.start > MAX_EPG_EVENT_DURATION:
                        continue
                    events[event.external_id] = event
            if page_count < 99:
                break
            offset += 99
        return tuple(sorted(events.values(), key=lambda event: event.start))

    @staticmethod
    def _epg_event(raw: dict[str, Any]) -> _EpgEvent:
        identifier = raw.get("code") or raw.get("id")
        if isinstance(identifier, bool) or not isinstance(identifier, (int, str)):
            raise RTLPlusMalformedResponseError("EPG.code or EPG.id was missing or invalid")
        identifier = str(identifier)
        start = RTLPlusProvider._epg_timestamp(raw.get("diffusion_start_date"), "EPG.start")
        if start is None:
            raise RTLPlusMalformedResponseError("EPG.start was missing")
        end = RTLPlusProvider._epg_timestamp(raw.get("diffusion_end_date"), "EPG.end")
        title = RTLPlusProvider._required_string(raw.get("title"), "EPG.title")
        subtitle = raw.get("subtitle") if isinstance(raw.get("subtitle"), str) else None
        description = raw.get("description") if isinstance(raw.get("description"), str) else None
        return _EpgEvent(
            external_id=f"epg:{identifier}:{int(start.timestamp())}",
            title=title,
            subtitle=subtitle,
            description=description,
            start=start,
            end=end,
        )

    @staticmethod
    def _epg_timestamp(value: Any, location: str) -> datetime | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise RTLPlusMalformedResponseError(f"{location} was not a string")
        try:
            return (
                datetime.strptime(value, EPG_DATE_FORMAT)
                .replace(tzinfo=EPG_TIMEZONE)
                .astimezone(UTC)
            )
        except ValueError as exc:
            raise RTLPlusMalformedResponseError(f"{location} was not a valid timestamp") from exc

    @staticmethod
    def _same_title(left: str, right: str) -> bool:
        def clean(value: str) -> str:
            value = unicode_normalize("NFKD", value).encode("ascii", "ignore").decode()
            return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()

        return clean(left) == clean(right)

    @staticmethod
    def _epg_episode_number(event: _EpgEvent) -> int | None:
        for value in (event.subtitle, event.title, event.external_id):
            match = _EPISODE_RE.search(value or "")
            if match:
                return int(match.group(1))
        return None

    @staticmethod
    def _catalog_episode_for_epg(
        seasons: list[NormalizedSeason], episode_number: int | None, release_at: datetime
    ) -> NormalizedEpisode | None:
        if episode_number is not None:
            for season in sorted(
                (season for season in seasons if season.number is not None),
                key=lambda season: season.number or 0,
                reverse=True,
            ):
                for episode in season.episodes:
                    if episode.number == episode_number:
                        return episode

        same_day = [
            episode
            for season in seasons
            if season.number is not None
            for episode in season.episodes
            if any(
                release.release_type is ReleaseType.STREAMING
                and release.release_at.date() == release_at.date()
                for release in episode.releases
            )
        ]
        if len(same_day) == 1:
            # RTL+ can publish a different episode number in the EPG than in the catalog.
            # The catalog's season/episode metadata remains authoritative in that case.
            return same_day[0]
        if episode_number is None:
            return None
        return None

    @staticmethod
    def _replace_episode_releases(
        seasons: list[NormalizedSeason],
        episode_external_id: str,
        releases: tuple[NormalizedEpisodeRelease, ...],
    ) -> list[NormalizedSeason]:
        return [
            season.model_copy(
                update={
                    "episodes": tuple(
                        episode.model_copy(update={"releases": releases})
                        if episode.external_id == episode_external_id
                        else episode
                        for episode in season.episodes
                    )
                }
            )
            if any(episode.external_id == episode_external_id for episode in season.episodes)
            else season
            for season in seasons
        ]

    @staticmethod
    def _required_string(value: Any, location: str) -> str:
        if not isinstance(value, str) or not value:
            raise RTLPlusMalformedResponseError(f"{location} was missing or not a string")
        return value

    async def _authenticate(self, client: httpx.AsyncClient) -> tuple[str, str]:
        if not self._oidc_client_secret:
            raise ValueError("RTLPLUS_OIDC_CLIENT_SECRET is required for automatic authentication")
        try:
            oidc = await client.post(
                self._oidc_url,
                data={
                    "client_id": self._oidc_client_id,
                    "client_secret": self._oidc_client_secret,
                    "grant_type": "client_credentials",
                },
            )
            oidc.raise_for_status()
            oidc_payload = oidc.json()
            access_token = (
                oidc_payload.get("access_token") if isinstance(oidc_payload, dict) else None
            )
            if not isinstance(access_token, str) or not access_token:
                raise RTLPlusMalformedResponseError("OIDC response lacks access_token")
            auth = await client.get(
                self._auth_url,
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {access_token}",
                    "X-Customer-Name": "rtlde",
                    "X-Client-Release": "6.49.0",
                    "x-auth-device-name": "episode-calendar",
                    "x-auth-device-player-size-width": "0",
                    "x-auth-device-player-size-height": "0",
                    "x-auth-device-id": f"m6group_web|_luid_{uuid4()}",
                    "X-Auth-Token": access_token,
                    "X-Auth-Token-Timestamp": str(int(time.time())),
                },
            )
            auth.raise_for_status()
            auth_payload = auth.json()
            bedrock_token = auth_payload.get("token") if isinstance(auth_payload, dict) else None
            if not isinstance(bedrock_token, str) or not bedrock_token:
                raise RTLPlusMalformedResponseError("Bedrock response lacks token")
            return f"Bearer {access_token}", bedrock_token
        except httpx.HTTPStatusError as exc:
            raise RTLPlusHTTPError(f"RTL+ authentication HTTP {exc.response.status_code}") from exc
        except httpx.RequestError as exc:
            raise RTLPlusHTTPError(str(exc)) from exc
        except ValueError as exc:
            if isinstance(exc, RTLPlusMalformedResponseError):
                raise
            raise RTLPlusMalformedResponseError(
                "RTL+ authentication response was not JSON"
            ) from exc

    async def _fetch(
        self, client: httpx.AsyncClient, program_id: str, series_url: str | None
    ) -> NormalizedSeries:
        pages: list[dict[str, Any]] = []
        page = 1
        seen_pages: set[int] = set()
        while page not in seen_pages:
            seen_pages.add(page)
            payload = await self._request(client, program_id, page)
            pages.append(payload)
            next_page = payload.get("pagination", {}).get("nextPage")
            if next_page is None:
                break
            if not isinstance(next_page, int) or next_page <= page:
                raise RTLPlusMalformedResponseError("invalid RTL+ pagination")
            page = next_page
        return self._normalize(program_id, pages, series_url=series_url)

    async def _request(
        self, client: httpx.AsyncClient, program_id: str, page: int
    ) -> dict[str, Any]:
        headers = {
            "Accept": "application/json",
            "X-Customer-Name": "rtlde",
            "X-Client-Release": "6.49.0",
            "X-Bedrock-Token": self._bedrock_token or "",
            "Authorization": self._authorization or "",
        }
        for attempt in range(self._max_retries + 1):
            try:
                response = await client.get(
                    self._endpoint_template.format(program_id=program_id),
                    params={"blockPage": page, "nbPages": 2},
                    headers=headers,
                )
                if response.status_code == 401 and attempt == 0 and self._oidc_client_secret:
                    self._authorization, self._bedrock_token = await self._authenticate(client)
                    headers["Authorization"] = self._authorization
                    headers["X-Bedrock-Token"] = self._bedrock_token
                    continue
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
                    raise RTLPlusHTTPError(str(exc)) from exc
                await asyncio.sleep(self._backoff * (2**attempt) + random.random() * 0.25)
            except httpx.HTTPStatusError as exc:
                raise RTLPlusHTTPError(f"RTL+ HTTP {exc.response.status_code}") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise RTLPlusMalformedResponseError("RTL+ response was not JSON") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("blocks"), list):
            raise RTLPlusMalformedResponseError("RTL+ response lacks blocks")
        return payload

    def _normalize(
        self,
        program_id: str,
        pages: list[dict[str, Any]],
        *,
        series_url: str | None = None,
    ) -> NormalizedSeries:
        first = pages[0]
        entity = first.get("entity")
        if not isinstance(entity, dict) or entity.get("id") is None:
            raise RTLPlusMalformedResponseError("RTL+ response lacks entity")
        metadata = entity.get("metadata") if isinstance(entity.get("metadata"), dict) else {}
        title = metadata.get("title")
        if not isinstance(title, str) or not title:
            raise RTLPlusMalformedResponseError("RTL+ response lacks series title")
        schedule_labels: list[str] = []
        for payload in pages:
            for block in payload.get("blocks", []):
                if (
                    not isinstance(block, dict)
                    or block.get("analytics", {}).get("tealium", {}).get("from")
                    != "feature.videos_by_season_by_program"
                ):
                    continue
                content = block.get("content")
                block_title = (
                    (content.get("title") or {}).get("short")
                    if isinstance(content, dict) and isinstance(content.get("title"), dict)
                    else None
                )
                if isinstance(block_title, str):
                    schedule_labels.append(block_title)
        releases = self._schedule(first.get("seo"), "\n".join(schedule_labels))
        diffusion_release = self._diffusion_release(first.get("seo"))
        season_numbers: set[int] = set()
        for payload in pages:
            for block in payload.get("blocks", []):
                if (
                    not isinstance(block, dict)
                    or block.get("analytics", {}).get("tealium", {}).get("from")
                    != "feature.videos_by_season_by_program"
                ):
                    continue
                content = block.get("content")
                season_title = (
                    (content.get("title") or {}).get("short")
                    if isinstance(content, dict) and isinstance(content.get("title"), dict)
                    else None
                )
                season_match = _SEASON_RE.search(str(season_title or ""))
                if season_match:
                    season_numbers.add(int(season_match.group(1)))
                items = content.get("items", []) if isinstance(content, dict) else []
                if isinstance(items, list):
                    for item in items:
                        raw = item.get("itemContent") if isinstance(item, dict) else None
                        if not isinstance(raw, dict):
                            continue
                        season_match = _SEASON_RE.search(str(raw.get("highlight") or ""))
                        if season_match:
                            season_numbers.add(int(season_match.group(1)))
        # RTL+'s weekday/programmation block identifies the season that is currently
        # airing even when its layout block uses a different source than the season
        # blocks (for example while a new season is being announced before its own
        # season block exists). Without it the previous season would be mistaken for
        # the current one and inherit the page-level diffusion date of the upcoming
        # episode.
        for payload in pages:
            for block in payload.get("blocks", []):
                if not isinstance(block, dict):
                    continue
                content = block.get("content")
                if not isinstance(content, dict):
                    continue
                content_title = content.get("title")
                block_title = (
                    content_title.get("short") if isinstance(content_title, dict) else None
                )
                if not isinstance(block_title, str) or not _WEEKDAY_RE.search(block_title):
                    continue
                items = content.get("items")
                if not isinstance(items, list):
                    continue
                for item in items:
                    raw = item.get("itemContent") if isinstance(item, dict) else None
                    if not isinstance(raw, dict):
                        continue
                    season_match = _SEASON_RE.search(str(raw.get("highlight") or ""))
                    if season_match:
                        season_numbers.add(int(season_match.group(1)))
        current_season_number = max(season_numbers, default=None)
        latest_catalog_episode_number = self._latest_catalog_episode_number(
            pages, current_season_number
        )
        seo_text = "\n".join(
            str(value)
            for value in (first.get("seo") or {}).get("metadata", {}).values()
            if isinstance(value, str)
        )
        has_explicit_schedule = bool(
            re.search(r"^\s*\|\s*\*{0,2}Folge\s+\d+", seo_text, re.IGNORECASE | re.MULTILINE)
        )
        diffusion_episode_number = None
        if (
            diffusion_release is not None
            and releases
            and not has_explicit_schedule
            and current_season_number is not None
        ):
            diffusion_episode_number = self._diffusion_episode_number(pages, current_season_number)
        seasons: dict[int, list[NormalizedEpisode]] = {}
        for payload in pages:
            for block in payload.get("blocks", []):
                if (
                    not isinstance(block, dict)
                    or block.get("analytics", {}).get("tealium", {}).get("from")
                    != "feature.videos_by_season_by_program"
                ):
                    continue
                content = block.get("content")
                if not isinstance(content, dict):
                    raise RTLPlusMalformedResponseError("season block lacks content")
                season_title = (
                    (content.get("title") or {}).get("short")
                    if isinstance(content.get("title"), dict)
                    else None
                )
                items = content.get("items")
                if not isinstance(items, list):
                    raise RTLPlusMalformedResponseError("season block lacks items")
                for item in items:
                    raw = item.get("itemContent") if isinstance(item, dict) else None
                    if not isinstance(raw, dict):
                        raise RTLPlusMalformedResponseError("episode item malformed")
                    analytics = raw.get("action", {}).get("analytics", {}).get("tealium", {})
                    if analytics.get("clip_type") not in (None, "vi"):
                        continue
                    highlight = str(raw.get("highlight") or "")
                    season_match = _SEASON_RE.search(highlight) or _SEASON_RE.search(
                        str(season_title or "")
                    )
                    episode_match = _EPISODE_RE.search(highlight)
                    if not season_match or not episode_match or not raw.get("id"):
                        continue
                    season_number, episode_number = (
                        int(season_match.group(1)),
                        int(episode_match.group(1)),
                    )
                    release_at = None
                    if season_number == current_season_number:
                        if diffusion_episode_number is not None:
                            release_at = diffusion_release + timedelta(
                                weeks=episode_number - diffusion_episode_number
                            )
                        else:
                            release_at = releases.get(episode_number)
                    if (
                        release_at is None
                        and season_number == current_season_number
                        and episode_number == latest_catalog_episode_number
                    ):
                        release_at = diffusion_release
                    release_tuple = (
                        ()
                        if release_at is None
                        else (
                            NormalizedEpisodeRelease(
                                release_type=ReleaseType.STREAMING,
                                release_at=release_at,
                                url=series_url,
                            ),
                        )
                    )
                    try:
                        episode = NormalizedEpisode(
                            external_id=str(raw["id"]),
                            number=episode_number,
                            title=str(raw.get("title") or f"Folge {episode_number}"),
                            releases=release_tuple,
                        )
                    except ValidationError as exc:
                        raise RTLPlusMalformedResponseError(
                            "episode did not match normalized model"
                        ) from exc
                    seasons.setdefault(season_number, []).append(episode)
        if current_season_number is not None and has_explicit_schedule:
            current_episodes = seasons.get(current_season_number, [])
            next_episode_number = (
                max((episode.number for episode in current_episodes), default=0) + 1
            )
            release_at = releases.get(next_episode_number)
            if (
                release_at is not None
                and release_at.date() >= datetime.now(ZoneInfo("Europe/Vienna")).date()
            ):
                current_episodes.append(
                    NormalizedEpisode(
                        external_id=(
                            f"{program_id}:season:{current_season_number}:episode:"
                            f"{next_episode_number}"
                        ),
                        number=next_episode_number,
                        title=f"Folge {next_episode_number}",
                        releases=(
                            NormalizedEpisodeRelease(
                                release_type=ReleaseType.STREAMING,
                                release_at=release_at,
                                url=series_url,
                            ),
                        ),
                    )
                )
        normalized_seasons = tuple(
            NormalizedSeason(
                external_id=f"{program_id}:season:{number}",
                number=number,
                title=f"Staffel {number}",
                episodes=tuple(episodes),
            )
            for number, episodes in sorted(seasons.items())
        )
        return NormalizedSeries(external_id=program_id, title=title, seasons=normalized_seasons)

    @staticmethod
    def _latest_catalog_episode_number(
        pages: list[dict[str, Any]], current_season_number: int | None
    ) -> int | None:
        if current_season_number is None:
            return None
        episode_numbers: list[int] = []
        for payload in pages:
            for block in payload.get("blocks", []):
                if (
                    not isinstance(block, dict)
                    or block.get("analytics", {}).get("tealium", {}).get("from")
                    != "feature.videos_by_season_by_program"
                ):
                    continue
                content = block.get("content")
                items = content.get("items", []) if isinstance(content, dict) else []
                for item in items if isinstance(items, list) else []:
                    raw = item.get("itemContent") if isinstance(item, dict) else None
                    highlight = str(raw.get("highlight") or "") if isinstance(raw, dict) else ""
                    season_match = _SEASON_RE.search(highlight)
                    episode_match = _EPISODE_RE.search(highlight)
                    if (
                        season_match
                        and episode_match
                        and int(season_match.group(1)) == current_season_number
                    ):
                        episode_numbers.append(int(episode_match.group(1)))
        return max(episode_numbers, default=None)

    @staticmethod
    def _series_url(external_id: str) -> str | None:
        """Return the public RTL+ series page for a configured slug identifier."""

        if re.fullmatch(r"[a-z0-9][a-z0-9-]*(?:-p|_p)_\d+", external_id):
            return f"https://plus.rtl.de/{external_id}"
        return None

    @staticmethod
    def _diffusion_release(seo: Any) -> datetime | None:
        if not isinstance(seo, dict):
            return None
        value = seo.get("diffusionDate")
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, UTC).astimezone(ZoneInfo("Europe/Vienna"))
        return None

    @staticmethod
    def _diffusion_episode_number(
        pages: list[dict[str, Any]], current_season_number: int
    ) -> int | None:
        """Return the first episode from RTL+'s current weekday block.

        RTL+ exposes the episode order in the layout response. The first item in the
        weekday block is the episode represented by the page-level diffusion date.
        Other blocks, such as "Kostenlose Folgen", are unrelated to that date.
        """

        for payload in pages:
            for block in payload.get("blocks", []):
                if (
                    not isinstance(block, dict)
                    or block.get("analytics", {}).get("tealium", {}).get("from")
                    != "feature.videos_by_season_by_program"
                ):
                    continue
                content = block.get("content")
                if not isinstance(content, dict):
                    continue
                title = content.get("title")
                block_title = title.get("short") if isinstance(title, dict) else None
                if not isinstance(block_title, str) or not _WEEKDAY_RE.search(block_title):
                    continue
                items = content.get("items")
                if not isinstance(items, list):
                    continue
                for item in items:
                    raw = item.get("itemContent") if isinstance(item, dict) else None
                    if not isinstance(raw, dict):
                        continue
                    highlight = str(raw.get("highlight") or "")
                    season_match = _SEASON_RE.search(highlight)
                    episode_match = _EPISODE_RE.search(highlight)
                    if (
                        season_match
                        and episode_match
                        and int(season_match.group(1)) == current_season_number
                    ):
                        episode_number = int(episode_match.group(1))
                        return episode_number if episode_number > 0 else None
        return None

    @staticmethod
    def _schedule(seo: Any, extra_text: str = "") -> dict[int, datetime]:
        metadata = seo.get("metadata", {}) if isinstance(seo, dict) else {}
        if not isinstance(metadata, dict):
            return {}
        text = "\n".join(
            [*(str(value) for value in metadata.values() if isinstance(value, str)), extra_text]
        )
        if not isinstance(text, str):
            return {}
        title = metadata.get("title", "")
        title_year = re.search(r"\b(20\d{2})\b", title) if isinstance(title, str) else None
        # Schedule dates usually omit the year; use the page title when it identifies
        # the current season and otherwise use the current year.
        year = int(title_year.group(1)) if title_year else datetime.now().year
        result: dict[int, datetime] = {}
        for match in _DATE_RE.finditer(text):
            episode, day, month, hour, minute = map(int, match.groups())
            result[episode] = datetime(
                year, month, day, hour, minute, tzinfo=ZoneInfo("Europe/Vienna")
            )
        if result:
            return result

        # Current RTL+ pages often publish a Markdown table instead of the former SEO
        # schedule syntax. The streaming date is the rightmost date in each episode row.
        for line in text.splitlines():
            episode_match = _EPISODE_RE.search(line)
            dates = list(_TABLE_DATE_RE.finditer(line))
            if not episode_match or not dates:
                continue
            match = dates[-1]
            day, month, hour, minute = match.groups()
            result[int(episode_match.group(1))] = datetime(
                year,
                int(month),
                int(day),
                int(hour),
                int(minute or 0),
                tzinfo=ZoneInfo("Europe/Vienna"),
            )
        if result:
            return result

        timezone = ZoneInfo("Europe/Vienna")
        now = datetime.now(timezone)
        start_match = _START_DATE_RE.search(text)
        if not start_match:
            # A cadence without a start date is not enough to date an episode. In
            # particular, do not turn completed seasons into current releases.
            return result
        day = int(start_match.group(1))
        month = _MONTHS[start_match.group(2).lower()]
        start = datetime(year, month, day, tzinfo=timezone)
        if start > now + timedelta(days=31):
            start = start.replace(year=year - 1)

        return {episode: start + timedelta(weeks=episode - 1) for episode in range(1, 53)}

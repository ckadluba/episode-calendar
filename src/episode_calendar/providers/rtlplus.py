from __future__ import annotations

# The provider mirrors a verbose external contract; long endpoint/field expressions are kept
# readable alongside the response structure.
# ruff: noqa: E501
import re
from datetime import datetime
from typing import Any
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

_SEASON_RE = re.compile(r"Staffel\s+(\d+)", re.IGNORECASE)
_EPISODE_RE = re.compile(r"Folge\s+(\d+)", re.IGNORECASE)
_DATE_RE = re.compile(
    r"\*\*Folge\s+(\d+)\*\*\s*\|[^|]*\|[^|]*?\s*(?:Di\.|Do\.|Mi\.|Mo\.|Sa\.|So\.)?\s*(\d{1,2})\.(\d{1,2})\.,?\s*(\d{1,2}):(\d{2})\s*Uhr",
    re.IGNORECASE,
)


class RTLPlusProviderError(RuntimeError):
    """Base class for RTL+ catalog failures."""


class RTLPlusHTTPError(RTLPlusProviderError):
    pass


class RTLPlusMalformedResponseError(RTLPlusProviderError):
    pass


class RTLPlusProvider:
    """Read RTL+ Bedrock layout metadata for the German RTL+ catalogue.

    The layout endpoint and item fields are observed current web behaviour. Release times
    are currently only present in the SEO markdown schedule table, so parsing that table is
    intentionally isolated and may need adjustment when RTL+ changes its editorial content.
    """

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float | None = None,
        endpoint_template: str | None = None,
        bedrock_token: str | None = None,
        authorization: str | None = None,
    ) -> None:
        settings = get_settings()
        self._client = client
        self._timeout = timeout if timeout is not None else settings.rtlplus_timeout_seconds
        self._endpoint_template = endpoint_template or settings.rtlplus_layout_url
        self._bedrock_token = bedrock_token or settings.rtlplus_bedrock_token
        self._authorization = authorization or settings.rtlplus_authorization

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
        if not self._bedrock_token or not self._authorization:
            raise ValueError("RTL+ Bedrock token and Authorization are required")
        if self._client is not None:
            return await self._fetch(self._client, program_id)
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            return await self._fetch(client, program_id)

    async def _fetch(self, client: httpx.AsyncClient, program_id: str) -> NormalizedSeries:
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
        return self._normalize(program_id, pages)

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
        try:
            response = await client.get(
                self._endpoint_template.format(program_id=program_id),
                params={"blockPage": page, "nbPages": 2},
                headers=headers,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise RTLPlusHTTPError(f"RTL+ HTTP {exc.response.status_code}") from exc
        except httpx.RequestError as exc:
            raise RTLPlusHTTPError(str(exc)) from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise RTLPlusMalformedResponseError("RTL+ response was not JSON") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("blocks"), list):
            raise RTLPlusMalformedResponseError("RTL+ response lacks blocks")
        return payload

    def _normalize(self, program_id: str, pages: list[dict[str, Any]]) -> NormalizedSeries:
        first = pages[0]
        entity = first.get("entity")
        if not isinstance(entity, dict) or entity.get("id") is None:
            raise RTLPlusMalformedResponseError("RTL+ response lacks entity")
        metadata = entity.get("metadata") if isinstance(entity.get("metadata"), dict) else {}
        title = metadata.get("title")
        if not isinstance(title, str) or not title:
            raise RTLPlusMalformedResponseError("RTL+ response lacks series title")
        releases = self._schedule(first.get("seo"))
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
                    release_at = releases.get(episode_number)
                    release_tuple = (
                        ()
                        if release_at is None
                        else (
                            NormalizedEpisodeRelease(
                                release_type=ReleaseType.STREAMING, release_at=release_at
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
    def _schedule(seo: Any) -> dict[int, datetime]:
        text = seo.get("metadata", {}).get("text", "") if isinstance(seo, dict) else ""
        year_match = re.search(r"\b(20\d{2})\b", text)
        year = int(year_match.group(1)) if year_match else datetime.now().year
        result: dict[int, datetime] = {}
        for match in _DATE_RE.finditer(text):
            episode, day, month, hour, minute = map(int, match.groups())
            result[episode] = datetime(
                year, month, day, hour, minute, tzinfo=ZoneInfo("Europe/Vienna")
            )
        return result

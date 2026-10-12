import math
import re
from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict, HttpUrl, field_validator

from episode_calendar.shared.domain import ReleaseType

_ISO_DURATION_RE = re.compile(
    r"^P(?:(?P<days>\d+(?:[.,]\d+)?)D)?"
    r"(?:T(?:(?P<hours>\d+(?:[.,]\d+)?)H)?"
    r"(?:(?P<minutes>\d+(?:[.,]\d+)?)M)?"
    r"(?:(?P<seconds>\d+(?:[.,]\d+)?)S)?)?$",
    re.IGNORECASE,
)
_DURATION_TEXT_RE = re.compile(
    r"(?P<number>\d+(?:[.,]\d+)?)\s*"
    r"(?P<unit>h\b|hrs?\b|hours?\b|hour\b|std\.?\b|stunden\b|stunde\b|"
    r"minuten\b|minute\b|mins?\b|min\.?\b|m\b)",
    re.IGNORECASE,
)
_HOUR_UNITS = frozenset({"h", "hr", "hrs", "hour", "hours", "std", "std.", "stunde", "stunden"})


def duration_minutes_from_seconds(seconds: int | float | None) -> int | None:
    """Round a non-negative second count to whole minutes, half-up."""
    if seconds is None or isinstance(seconds, bool):
        return None
    if not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or seconds < 0:
        return None
    return int(seconds / 60 + 0.5)


def duration_minutes_from_iso8601(value: str | None) -> int | None:
    """Convert an ISO 8601 duration such as ``PT1H4M59.136S`` to whole minutes."""
    if not isinstance(value, str) or not value.strip():
        return None
    match = _ISO_DURATION_RE.fullmatch(value.strip())
    if match is None:
        return None
    days, hours, minutes, seconds = (
        float(part.replace(",", ".")) if part else 0.0 for part in match.groups()
    )
    return int(days * 1440 + hours * 60 + minutes + seconds / 60 + 0.5)


def duration_minutes_from_text(value: str | None) -> int | None:
    """Parse a human-readable duration such as ``1 hr 16 mins`` or ``1 Std. 26 Min.``."""
    if not isinstance(value, str) or not value.strip():
        return None
    total = 0.0
    for match in _DURATION_TEXT_RE.finditer(value):
        number = float(match.group("number").replace(",", "."))
        unit = match.group("unit").casefold()
        total += number * 60 if unit in _HOUR_UNITS else number
    return int(total + 0.5) if total > 0 else None


def episode_title_from_description(description: str | None, fallback: str) -> str:
    """Use a short description as the visible episode title when no title exists."""

    if not description or not description.strip():
        return fallback
    text = description.strip()
    return f"{text[:100]}..." if len(text) > 100 else text


class NormalizedEpisodeRelease(BaseModel):
    model_config = ConfigDict(frozen=True)

    external_id: str | None = None
    release_type: ReleaseType
    release_at: datetime
    available_until: datetime | None = None
    url: HttpUrl | None = None
    preview: bool = False
    rerun: bool = False
    date_from_api: bool = True
    placeholder: bool = False

    @field_validator("release_at", "available_until")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("release timestamps must be timezone-aware")
        return value


def first_seen_release(
    now: datetime,
    *,
    url: HttpUrl | None = None,
    available_until: datetime | None = None,
) -> NormalizedEpisodeRelease:
    """Placeholder streaming release for a catalogue episode without its own date.

    The provider stamps the discovery time and clears ``date_from_api`` so the importer
    keeps the earliest value across re-imports until the linear programme supplies a real
    broadcast date. ``placeholder`` distinguishes this "no date found" case from a date
    that was merely derived rather than read from the API (for example an RTL+ preview
    anchor), which also clears ``date_from_api`` but is a real date.
    """

    return NormalizedEpisodeRelease(
        release_type=ReleaseType.STREAMING,
        release_at=now,
        available_until=available_until,
        url=url,
        date_from_api=False,
        placeholder=True,
    )


class NormalizedEpisode(BaseModel):
    model_config = ConfigDict(frozen=True)

    external_id: str
    number: int | None = None
    title: str
    description: str | None = None
    duration_minutes: int | None = None
    releases: tuple[NormalizedEpisodeRelease, ...] = ()


class NormalizedSeason(BaseModel):
    model_config = ConfigDict(frozen=True)

    external_id: str
    number: int | None = None
    title: str | None = None
    episodes: tuple[NormalizedEpisode, ...] = ()


class NormalizedSeries(BaseModel):
    model_config = ConfigDict(frozen=True)

    external_id: str
    title: str
    description: str | None = None
    seasons: tuple[NormalizedSeason, ...] = ()


class ProviderAdapter(Protocol):
    """Boundary implemented by each provider-specific API adapter."""

    @property
    def slug(self) -> str: ...

    async def fetch_series(self, external_id: str) -> NormalizedSeries:
        """Fetch one complete series tree and normalize it for import."""
        ...

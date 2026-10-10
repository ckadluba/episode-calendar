from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict, HttpUrl, field_validator

from episode_calendar.domain import ReleaseType


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

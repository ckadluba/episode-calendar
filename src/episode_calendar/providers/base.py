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

    @field_validator("release_at", "available_until")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("release timestamps must be timezone-aware")
        return value


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

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, Enum, ForeignKey, Integer, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from episode_calendar.shared.db.base import Base
from episode_calendar.shared.db.types import UTCDateTime
from episode_calendar.shared.domain import ReleaseType


class Provider(Base):
    __tablename__ = "providers"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    slug: Mapped[str] = mapped_column(String(100), unique=True)
    name: Mapped[str] = mapped_column(String(200))

    series: Mapped[list[Series]] = relationship(back_populates="provider")
    releases: Mapped[list[EpisodeRelease]] = relationship(back_populates="provider")


class Series(Base):
    __tablename__ = "series"
    __table_args__ = (UniqueConstraint("provider_id", "external_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    provider_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("providers.id", ondelete="RESTRICT"), index=True
    )
    external_id: Mapped[str] = mapped_column(String(255))
    title: Mapped[str] = mapped_column(String(500), index=True)
    description: Mapped[str | None] = mapped_column(Text)

    provider: Mapped[Provider] = relationship(back_populates="series")
    seasons: Mapped[list[Season]] = relationship(
        back_populates="series", cascade="all, delete-orphan"
    )

    @property
    def platform(self) -> str:
        """Provider display name exposed by the public API."""
        return self.provider.name

    @property
    def platform_id(self) -> str:
        """Provider identifier exposed for filtering and stable CSS classes."""
        return self.provider.slug


class Season(Base):
    __tablename__ = "seasons"
    __table_args__ = (UniqueConstraint("series_id", "external_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    series_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("series.id", ondelete="CASCADE"), index=True
    )
    external_id: Mapped[str] = mapped_column(String(255))
    number: Mapped[int | None]
    title: Mapped[str | None] = mapped_column(String(500))

    series: Mapped[Series] = relationship(back_populates="seasons")
    episodes: Mapped[list[Episode]] = relationship(
        back_populates="season", cascade="all, delete-orphan"
    )


class Episode(Base):
    __tablename__ = "episodes"
    __table_args__ = (UniqueConstraint("season_id", "external_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    season_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("seasons.id", ondelete="CASCADE"), index=True
    )
    external_id: Mapped[str] = mapped_column(String(255))
    number: Mapped[int | None]
    title: Mapped[str] = mapped_column(String(500))
    description: Mapped[str | None] = mapped_column(Text)
    duration_minutes: Mapped[int | None] = mapped_column(Integer)

    season: Mapped[Season] = relationship(back_populates="episodes")
    releases: Mapped[list[EpisodeRelease]] = relationship(
        back_populates="episode", cascade="all, delete-orphan"
    )


def _utcnow() -> datetime:
    return datetime.now(UTC)


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    email_verified_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow)

    settings: Mapped[UserSettings | None] = relationship(
        back_populates="user", cascade="all, delete-orphan", uselist=False
    )
    sessions: Mapped[list[UserSession]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class LoginToken(Base):
    """One pending passwordless login, usable as a code or as a link.

    A single row backs both variants so a request sends exactly one email and
    consuming either variant invalidates the other.
    """

    __tablename__ = "login_tokens"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), index=True)
    code_hash: Mapped[str] = mapped_column(String(64))
    code_salt: Mapped[str] = mapped_column(String(32))
    link_token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    attempts_left: Mapped[int] = mapped_column(Integer, default=5)
    request_ip: Mapped[str | None] = mapped_column(String(64))
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow)


class UserSession(Base):
    __tablename__ = "user_sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    user_agent: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)

    user: Mapped[User] = relationship(back_populates="sessions")


class UserSettings(Base):
    __tablename__ = "user_settings"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow, onupdate=_utcnow)

    user: Mapped[User] = relationship(back_populates="settings")


class EpisodeRelease(Base):
    __tablename__ = "episode_releases"
    __table_args__ = (
        UniqueConstraint("provider_id", "external_id"),
        UniqueConstraint("episode_id", "provider_id", "release_type", "release_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    episode_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("episodes.id", ondelete="CASCADE"), index=True
    )
    provider_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("providers.id", ondelete="RESTRICT"), index=True
    )
    external_id: Mapped[str | None] = mapped_column(String(255))
    release_type: Mapped[ReleaseType] = mapped_column(
        Enum(ReleaseType, native_enum=False, length=32)
    )
    release_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    available_until: Mapped[datetime | None] = mapped_column(UTCDateTime)
    url: Mapped[str | None] = mapped_column(String(2000))
    rerun: Mapped[bool] = mapped_column(default=False, server_default="false", nullable=False)
    preview: Mapped[bool] = mapped_column(default=False, server_default="false", nullable=False)
    date_from_api: Mapped[bool] = mapped_column(default=True, server_default="true", nullable=False)
    episode: Mapped[Episode] = relationship(back_populates="releases")
    provider: Mapped[Provider] = relationship(back_populates="releases")

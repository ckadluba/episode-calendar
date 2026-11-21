from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Enum,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
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
    """An account identified only by an opaque UUID and its credentials."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow)

    settings: Mapped[UserSettings | None] = relationship(
        back_populates="user", cascade="all, delete-orphan", uselist=False
    )
    sessions: Mapped[list[UserSession]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    passkeys: Mapped[list[PasskeyCredential]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    recovery_codes: Mapped[list[RecoveryCode]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class PasskeyCredential(Base):
    """A WebAuthn credential: technical identifiers and a public key only."""

    __tablename__ = "passkey_credentials"

    credential_id: Mapped[bytes] = mapped_column(LargeBinary, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    public_key: Mapped[bytes] = mapped_column(LargeBinary)
    sign_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    user: Mapped[User] = relationship(back_populates="passkeys")


class AuthChallenge(Base):
    """A short-lived, single-use WebAuthn challenge."""

    __tablename__ = "auth_challenges"

    challenge: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))
    # No FK: a registration challenge can carry a not-yet-created user id.
    user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)


class RecoveryCode(Base):
    """A single-use fallback code, stored only as a salted scrypt digest."""

    __tablename__ = "recovery_codes"

    lookup: Mapped[str] = mapped_column(String(16), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    code_hash: Mapped[str] = mapped_column(String(64))
    salt: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_utcnow)

    user: Mapped[User] = relationship(back_populates="recovery_codes")


class UserSession(Base):
    __tablename__ = "user_sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
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

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Enum, ForeignKey, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from episode_calendar.db.base import Base
from episode_calendar.db.types import UTCDateTime
from episode_calendar.domain import ReleaseType


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

    season: Mapped[Season] = relationship(back_populates="episodes")
    releases: Mapped[list[EpisodeRelease]] = relationship(
        back_populates="episode", cascade="all, delete-orphan"
    )


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

    episode: Mapped[Episode] = relationship(back_populates="releases")
    provider: Mapped[Provider] = relationship(back_populates="releases")

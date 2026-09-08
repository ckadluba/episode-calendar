from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from episode_calendar.db.models import Episode, EpisodeRelease, Season, Series
from episode_calendar.db.session import get_session

router = APIRouter(prefix="/api/v1")
LOCAL_TIMEZONE = ZoneInfo("Europe/Vienna")


class SeriesResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    external_id: str
    title: str
    description: str | None


class EpisodeReleaseResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    release_type: str
    release_at: datetime
    available_until: datetime | None
    url: str | None


class EpisodeResponse(BaseModel):
    id: uuid.UUID
    external_id: str
    series_id: uuid.UUID
    season_id: uuid.UUID
    season_number: int | None
    number: int | None
    title: str
    description: str | None
    releases: list[EpisodeReleaseResponse]


@router.get("/series", response_model=list[SeriesResponse])
async def list_series(session: AsyncSession = Depends(get_session)) -> list[Series]:  # noqa: B008
    result = await session.scalars(select(Series).order_by(Series.title, Series.id))
    return list(result)


@router.get("/series/{series_id}", response_model=SeriesResponse)
async def get_series(  # noqa: B008
    series_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> Series:
    series = await session.get(Series, series_id)
    if series is None:
        raise HTTPException(status_code=404, detail="Series not found")
    return series


@router.get("/episodes", response_model=list[EpisodeResponse])
async def list_episodes(
    from_: datetime | None = Query(default=None, alias="from"),  # noqa: B008
    to: datetime | None = None,
    series: uuid.UUID | None = None,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> list[EpisodeResponse]:
    for value, name in ((from_, "from"), (to, "to")):
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise HTTPException(status_code=422, detail=f"{name} must be timezone-aware")
    statement = (
        select(Episode, Season.series_id, Season.number)
        .join(Episode.season)
        .join(EpisodeRelease, EpisodeRelease.episode_id == Episode.id)
        .options(selectinload(Episode.releases))
    )
    if series is not None:
        statement = statement.where(Season.series_id == series)
    if from_ is not None:
        statement = statement.where(Episode.releases.any(EpisodeRelease.release_at >= from_))
    if to is not None:
        statement = statement.where(Episode.releases.any(EpisodeRelease.release_at <= to))
    statement = statement.order_by(EpisodeRelease.release_at, Episode.id)
    rows = (await session.execute(statement)).all()
    unique_rows: dict[uuid.UUID, tuple[Episode, uuid.UUID, int | None]] = {}
    for episode, series_id, season_number in rows:
        unique_rows.setdefault(episode.id, (episode, series_id, season_number))
    return [
        EpisodeResponse(
            id=episode.id,
            external_id=episode.external_id,
            series_id=series_id,
            season_id=episode.season_id,
            season_number=season_number,
            number=episode.number,
            title=episode.title,
            description=episode.description,
            releases=[EpisodeReleaseResponse.model_validate(item) for item in episode.releases],
        )
        for episode, series_id, season_number in unique_rows.values()
    ]


def _calendar_week_window(offset: int = 0) -> tuple[datetime, datetime]:
    """Return the local Monday-start calendar week as timezone-aware bounds."""
    today = datetime.now(LOCAL_TIMEZONE).date()
    start_date = today - timedelta(days=today.weekday()) + timedelta(days=7 * offset)
    start = datetime.combine(start_date, datetime.min.time(), tzinfo=LOCAL_TIMEZONE)
    end = start + timedelta(days=7)
    return start, end


@router.get("/episodes/current-week", response_model=list[EpisodeResponse])
async def list_current_week_episodes(
    series: uuid.UUID | None = None,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> list[EpisodeResponse]:
    start, end = _calendar_week_window()
    return await list_episodes(
        from_=start,
        to=end - timedelta(microseconds=1),
        series=series,
        session=session,
    )


@router.get("/episodes/next-week", response_model=list[EpisodeResponse])
async def list_next_week_episodes(
    series: uuid.UUID | None = None,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> list[EpisodeResponse]:
    start, end = _calendar_week_window(offset=1)
    return await list_episodes(
        from_=start,
        to=end - timedelta(microseconds=1),
        series=series,
        session=session,
    )

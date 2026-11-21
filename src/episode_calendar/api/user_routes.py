"""Account and cross-device settings endpoints."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from episode_calendar.api.deps import get_current_user
from episode_calendar.shared.db.models import AuthChallenge, User, UserSettings
from episode_calendar.shared.db.session import get_session

router = APIRouter(prefix="/api/v1", tags=["account"])

MAX_SERIES_IDS = 5000
MAX_SEEN_EPISODES = 20000
MAX_PLATFORMS = 100
MAX_ID_LENGTH = 100


def _clean_string_ids(values: list[str], *, limit: int) -> list[str]:
    if len(values) > limit:
        raise ValueError(f"At most {limit} entries are allowed")
    cleaned: list[str] = []
    for value in values:
        normalized = value.strip()
        if not normalized or len(normalized) > MAX_ID_LENGTH:
            raise ValueError(f"Entries must be between 1 and {MAX_ID_LENGTH} characters")
        if normalized not in cleaned:
            cleaned.append(normalized)
    return cleaned


class SeriesSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selected: list[str] = Field(default_factory=list)
    known: list[str] = Field(default_factory=list)

    @field_validator("selected", "known")
    @classmethod
    def _validate_ids(cls, value: list[str]) -> list[str]:
        return _clean_string_ids(value, limit=MAX_SERIES_IDS)


class UserSettingsData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    series_selection: SeriesSelection = Field(default_factory=SeriesSelection)
    preview_preferences: dict[str, bool] = Field(default_factory=dict)
    seen_episode_ids: list[str] = Field(default_factory=list)

    @field_validator("preview_preferences")
    @classmethod
    def _validate_preferences(cls, value: dict[str, bool]) -> dict[str, bool]:
        if len(value) > MAX_PLATFORMS:
            raise ValueError(f"At most {MAX_PLATFORMS} platform preferences are allowed")
        for key in value:
            if not key.strip() or len(key) > MAX_ID_LENGTH:
                raise ValueError(f"Platform IDs must be between 1 and {MAX_ID_LENGTH} characters")
        return value

    @field_validator("seen_episode_ids")
    @classmethod
    def _validate_seen_episodes(cls, value: list[str]) -> list[str]:
        return _clean_string_ids(value, limit=MAX_SEEN_EPISODES)


class SettingsPayload(BaseModel):
    data: UserSettingsData
    version: int | None = Field(default=None, ge=0)


class SettingsResponse(BaseModel):
    data: UserSettingsData
    version: int
    updated_at: datetime | None


class AccountResponse(BaseModel):
    id: uuid.UUID


@router.get("/me", response_model=AccountResponse)
async def read_account(user: User = Depends(get_current_user)) -> AccountResponse:  # noqa: B008
    return AccountResponse(id=user.id)


@router.get("/me/settings", response_model=SettingsResponse)
async def read_settings(
    user: User = Depends(get_current_user),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> SettingsResponse:
    row = await session.get(UserSettings, user.id)
    if row is None:
        return SettingsResponse(data=UserSettingsData(), version=0, updated_at=None)
    return SettingsResponse(
        data=UserSettingsData.model_validate(row.data),
        version=row.version,
        updated_at=row.updated_at,
    )


@router.put("/me/settings", response_model=SettingsResponse)
async def write_settings(
    payload: SettingsPayload,
    user: User = Depends(get_current_user),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> SettingsResponse:
    row = await session.get(UserSettings, user.id)
    current_version = row.version if row is not None else 0

    if payload.version is not None and payload.version != current_version:
        current = (
            UserSettingsData.model_validate(row.data) if row is not None else UserSettingsData()
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "Settings were changed on another device",
                "current": SettingsResponse(
                    data=current,
                    version=current_version,
                    updated_at=row.updated_at if row is not None else None,
                ).model_dump(mode="json"),
            },
        )

    data = payload.data.model_dump(mode="json")
    now = datetime.now(UTC)
    if row is None:
        row = UserSettings(user_id=user.id, data=data, version=1, updated_at=now)
        session.add(row)
    else:
        row.data = data
        row.version = current_version + 1
        row.updated_at = now

    await session.commit()
    return SettingsResponse(data=payload.data, version=row.version, updated_at=row.updated_at)


@router.delete("/me", status_code=status.HTTP_204_NO_CONTENT)
async def delete_account(
    user: User = Depends(get_current_user),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> Response:
    """Delete the account, its credentials, sessions, recovery code, and settings."""

    await session.execute(delete(AuthChallenge).where(AuthChallenge.user_id == user.id))
    await session.delete(user)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)

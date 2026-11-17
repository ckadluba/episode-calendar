"""Request dependencies for authenticated endpoints."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from episode_calendar.api.security import MAX_TOKEN_LENGTH, hash_token
from episode_calendar.shared.db.models import User, UserSession
from episode_calendar.shared.db.session import get_session

LAST_SEEN_UPDATE_INTERVAL = timedelta(hours=1)


def bearer_token(authorization: str | None) -> str | None:
    """Extract the token from an ``Authorization: Bearer`` header."""

    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer":
        return None
    token = token.strip()
    if not token or len(token) > MAX_TOKEN_LENGTH:
        return None
    return token


def unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired session",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_current_session(
    session: AsyncSession = Depends(get_session),  # noqa: B008
    authorization: Annotated[str | None, Header()] = None,
) -> UserSession:
    token = bearer_token(authorization)
    if token is None:
        raise unauthorized()
    login_session = await session.scalar(
        select(UserSession)
        .options(selectinload(UserSession.user))
        .where(UserSession.token_hash == hash_token(token))
    )
    now = datetime.now(UTC)
    if login_session is None or login_session.expires_at <= now:
        raise unauthorized()
    if now - login_session.last_seen_at >= LAST_SEEN_UPDATE_INTERVAL:
        login_session.last_seen_at = now
        await session.commit()
    return login_session


async def get_current_user(
    login_session: UserSession = Depends(get_current_session),  # noqa: B008
) -> User:
    return login_session.user

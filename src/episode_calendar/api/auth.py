"""Passwordless email login: one-time code and magic link in a single email."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, field_validator
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from episode_calendar.api.config import get_api_settings
from episode_calendar.api.deps import bearer_token
from episode_calendar.api.email import LoginEmailError, send_login_email
from episode_calendar.api.security import (
    OTP_ATTEMPTS,
    generate_login_code,
    generate_salt,
    generate_token,
    hash_login_code,
    hash_token,
    is_valid_email,
    normalize_email,
    verify_login_code,
)
from episode_calendar.shared.db.models import LoginToken, User, UserSession
from episode_calendar.shared.db.session import get_session

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

GENERIC_CODE_ERROR = "Invalid or expired login code"
GENERIC_LINK_ERROR = "Invalid or expired login link"
RATE_LIMIT_WINDOW = timedelta(hours=1)
EXPIRED_TOKEN_RETENTION = timedelta(days=1)


class EmailRequest(BaseModel):
    email: str

    @field_validator("email")
    @classmethod
    def _validate_email(cls, value: str) -> str:
        normalized = normalize_email(value)
        if not is_valid_email(normalized):
            raise ValueError("Enter a valid email address")
        return normalized


class CodeVerifyRequest(BaseModel):
    email: str
    code: str

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, value: str) -> str:
        return normalize_email(value)

    @field_validator("code")
    @classmethod
    def _validate_code(cls, value: str) -> str:
        stripped = "".join(character for character in value if character.isdigit())
        if len(stripped) != 6:
            raise ValueError("The login code has six digits")
        return stripped


class LinkVerifyRequest(BaseModel):
    token: str


class LoginAccepted(BaseModel):
    status: Literal["sent"] = "sent"


class AuthUser(BaseModel):
    id: uuid.UUID
    email: str


class AuthSession(BaseModel):
    token: str
    expires_at: datetime
    user: AuthUser


def _client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",", 1)[0].strip()[:64] or None
    return request.client.host[:64] if request.client else None


async def _within_limits(session: AsyncSession, *, email: str, ip: str | None) -> bool:
    settings = get_api_settings()
    now = datetime.now(UTC)
    window_start = now - RATE_LIMIT_WINDOW

    latest = await session.scalar(
        select(LoginToken.created_at)
        .where(LoginToken.email == email)
        .order_by(LoginToken.created_at.desc())
        .limit(1)
    )
    if latest is not None and latest > now - timedelta(
        seconds=settings.auth_email_cooldown_seconds
    ):
        return False

    email_count = await session.scalar(
        select(func.count())
        .select_from(LoginToken)
        .where(LoginToken.email == email, LoginToken.created_at >= window_start)
    )
    if (email_count or 0) >= settings.auth_email_hourly_limit:
        return False

    if ip is not None:
        ip_count = await session.scalar(
            select(func.count())
            .select_from(LoginToken)
            .where(LoginToken.request_ip == ip, LoginToken.created_at >= window_start)
        )
        if (ip_count or 0) >= settings.auth_ip_hourly_limit:
            return False
    return True


async def _create_user_session(
    session: AsyncSession, *, user: User, request: Request
) -> AuthSession:
    settings = get_api_settings()
    now = datetime.now(UTC)
    raw_token = generate_token()
    session.add(
        UserSession(
            token_hash=hash_token(raw_token),
            user_id=user.id,
            user_agent=(request.headers.get("user-agent") or "")[:500] or None,
            created_at=now,
            last_seen_at=now,
            expires_at=now + timedelta(days=settings.auth_session_ttl_days),
        )
    )
    await session.commit()
    return AuthSession(
        token=raw_token,
        expires_at=now + timedelta(days=settings.auth_session_ttl_days),
        user=AuthUser(id=user.id, email=user.email),
    )


async def _find_or_create_user(session: AsyncSession, email: str) -> User:
    user = await session.scalar(select(User).where(User.email == email))
    if user is not None:
        return user
    try:
        async with session.begin_nested():
            user = User(email=email, email_verified_at=datetime.now(UTC))
            session.add(user)
    except IntegrityError:
        # A concurrent request created the same account.
        user = await session.scalar(select(User).where(User.email == email))
        if user is None:
            raise
    return user


@router.post("/email/start", response_model=LoginAccepted, status_code=status.HTTP_202_ACCEPTED)
async def start_email_login(
    payload: EmailRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> LoginAccepted:
    """Send a login code and link.

    The response is identical for known and unknown addresses so the endpoint
    does not reveal whether an account exists.
    """

    settings = get_api_settings()
    now = datetime.now(UTC)
    ip = _client_ip(request)

    await session.execute(
        delete(LoginToken).where(LoginToken.expires_at < now - EXPIRED_TOKEN_RETENTION)
    )
    await session.commit()

    if not await _within_limits(session, email=payload.email, ip=ip):
        return LoginAccepted()

    # Requesting a new login invalidates every previous pending login.
    await session.execute(
        update(LoginToken)
        .where(LoginToken.email == payload.email, LoginToken.used_at.is_(None))
        .values(used_at=now)
    )

    code = generate_login_code()
    salt = generate_salt()
    link_token = generate_token()
    ttl = timedelta(seconds=settings.auth_code_ttl_seconds)
    login_token = LoginToken(
        email=payload.email,
        code_hash=await asyncio.to_thread(hash_login_code, code, salt),
        code_salt=salt,
        link_token_hash=hash_token(link_token),
        attempts_left=OTP_ATTEMPTS,
        request_ip=ip,
        created_at=now,
        expires_at=now + ttl,
    )
    session.add(login_token)
    await session.commit()

    link = f"{settings.auth_link_base_url.rstrip('/')}/?email_token={link_token}"
    try:
        await send_login_email(
            recipient=payload.email,
            code=code,
            link=link,
            ttl_minutes=max(1, settings.auth_code_ttl_seconds // 60),
        )
    except LoginEmailError:
        await session.delete(login_token)
        await session.commit()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="The login email could not be sent. Please try again later.",
        ) from None

    return LoginAccepted()


async def _pending_token(session: AsyncSession, email: str) -> LoginToken | None:
    now = datetime.now(UTC)
    return await session.scalar(
        select(LoginToken)
        .where(
            LoginToken.email == email,
            LoginToken.used_at.is_(None),
            LoginToken.expires_at > now,
        )
        .order_by(LoginToken.created_at.desc())
        .limit(1)
        .with_for_update()
    )


@router.post("/email/verify", response_model=AuthSession)
async def verify_email_code(
    payload: CodeVerifyRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> AuthSession:
    login_token = await _pending_token(session, payload.email)
    if login_token is None or login_token.attempts_left <= 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=GENERIC_CODE_ERROR)

    matches = await asyncio.to_thread(
        verify_login_code, payload.code, login_token.code_salt, login_token.code_hash
    )
    if not matches:
        login_token.attempts_left -= 1
        if login_token.attempts_left <= 0:
            login_token.used_at = datetime.now(UTC)
        await session.commit()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=GENERIC_CODE_ERROR)

    login_token.used_at = datetime.now(UTC)
    user = await _find_or_create_user(session, payload.email)
    return await _create_user_session(session, user=user, request=request)


@router.post("/link/verify", response_model=AuthSession)
async def verify_email_link(
    payload: LinkVerifyRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> AuthSession:
    now = datetime.now(UTC)
    login_token = await session.scalar(
        select(LoginToken)
        .where(
            LoginToken.link_token_hash == hash_token(payload.token),
            LoginToken.used_at.is_(None),
            LoginToken.expires_at > now,
        )
        .with_for_update()
    )
    if login_token is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=GENERIC_LINK_ERROR)

    login_token.used_at = now
    user = await _find_or_create_user(session, login_token.email)
    return await _create_user_session(session, user=user, request=request)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> Response:
    token = bearer_token(request.headers.get("authorization"))
    if token is not None:
        await session.execute(
            delete(UserSession).where(UserSession.token_hash == hash_token(token))
        )
        await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)

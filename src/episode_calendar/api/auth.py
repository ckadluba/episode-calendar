"""Passkey authentication and account/session lifecycle."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url

from episode_calendar.api import webauthn
from episode_calendar.api.config import get_api_settings
from episode_calendar.api.deps import bearer_token, get_current_user
from episode_calendar.api.security import (
    generate_recovery_code,
    generate_salt,
    generate_token,
    hash_recovery_code,
    hash_token,
    recovery_lookup,
    verify_recovery_code,
)
from episode_calendar.shared.db.models import (
    AuthChallenge,
    PasskeyCredential,
    RecoveryCode,
    User,
    UserSession,
)
from episode_calendar.shared.db.session import get_session

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

GENERIC_CHALLENGE_ERROR = "Invalid or expired challenge"
GENERIC_CREDENTIAL_ERROR = "Invalid credential or signature"
GENERIC_RECOVERY_ERROR = "Invalid or expired recovery code"
CHALLENGE_RETENTION = timedelta(days=1)


class AuthUser(BaseModel):
    id: uuid.UUID


class AuthSession(BaseModel):
    token: str
    expires_at: datetime
    user: AuthUser


class AuthSessionWithRecovery(AuthSession):
    recovery_code: str


class PasskeyMetadata(BaseModel):
    credential_id: str
    created_at: datetime
    last_used_at: datetime | None


class WebAuthnVerification(BaseModel):
    challenge: str
    credential: dict[str, Any]


class RecoveryVerification(BaseModel):
    code: str


class RecoveryResult(BaseModel):
    recovery_code: str


def _bad_challenge() -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=GENERIC_CHALLENGE_ERROR)


def _bad_credential() -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=GENERIC_CREDENTIAL_ERROR)


def _bad_recovery() -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=GENERIC_RECOVERY_ERROR)


def _client_origin(request: Request) -> str | None:
    return request.headers.get("origin")


def _credential_dict(payload: WebAuthnVerification) -> dict[str, Any]:
    credential = payload.credential
    if not isinstance(credential, dict) or not all(
        key in credential for key in ("id", "rawId", "type", "response")
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Incomplete WebAuthn credential",
        )
    return credential


async def _cleanup_challenges(session: AsyncSession, now: datetime) -> None:
    await session.execute(
        delete(AuthChallenge).where(AuthChallenge.expires_at < now - CHALLENGE_RETENTION)
    )
    await session.commit()


async def _store_challenge(
    session: AsyncSession, *, kind: str, user_id: uuid.UUID | None
) -> tuple[str, bytes]:
    now = datetime.now(UTC)
    settings = get_api_settings()
    await _cleanup_challenges(session, now)
    value, raw = webauthn.new_challenge()
    session.add(
        AuthChallenge(
            challenge=value,
            kind=kind,
            user_id=user_id,
            created_at=now,
            expires_at=now + timedelta(seconds=settings.auth_challenge_ttl_seconds),
        )
    )
    await session.commit()
    return value, raw


async def _take_challenge(session: AsyncSession, *, value: str, kind: str) -> AuthChallenge | None:
    now = datetime.now(UTC)
    challenge = await session.scalar(
        select(AuthChallenge)
        .where(
            AuthChallenge.challenge == value,
            AuthChallenge.kind == kind,
            AuthChallenge.expires_at > now,
        )
        .with_for_update()
    )
    if challenge is not None:
        # Single use: consume it even if the following verification fails.
        await session.delete(challenge)
        await session.commit()
    return challenge


def _challenge_bytes(challenge: AuthChallenge) -> bytes:
    try:
        return webauthn.decode_challenge(challenge.challenge)
    except Exception:
        raise _bad_challenge() from None


async def _create_user_session(session: AsyncSession, *, user: User) -> AuthSession:
    settings = get_api_settings()
    now = datetime.now(UTC)
    raw_token = generate_token()
    expires_at = now + timedelta(days=settings.auth_session_ttl_days)
    session.add(
        UserSession(
            token_hash=hash_token(raw_token),
            user_id=user.id,
            created_at=now,
            last_seen_at=now,
            expires_at=expires_at,
        )
    )
    await session.commit()
    return AuthSession(token=raw_token, expires_at=expires_at, user=AuthUser(id=user.id))


async def _issue_recovery_code(session: AsyncSession, user: User) -> str:
    """Rotate the account's fallback code and return the plaintext exactly once."""

    await session.execute(delete(RecoveryCode).where(RecoveryCode.user_id == user.id))
    code = generate_recovery_code()
    salt = generate_salt()
    digest = await asyncio.to_thread(hash_recovery_code, code, salt)
    session.add(
        RecoveryCode(lookup=recovery_lookup(code), user_id=user.id, code_hash=digest, salt=salt)
    )
    await session.commit()
    return code


def _passkey_metadata(credential: PasskeyCredential) -> PasskeyMetadata:
    return PasskeyMetadata(
        credential_id=bytes_to_base64url(credential.credential_id),
        created_at=credential.created_at,
        last_used_at=credential.last_used_at,
    )


@router.post("/registration/options")
async def start_registration(
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> dict[str, Any]:
    """Create a pending user id and options for a discoverable passkey."""

    user_id = uuid.uuid4()
    _, raw = await _store_challenge(session, kind="register", user_id=user_id)
    return webauthn.registration_options(
        get_api_settings(), user_id=user_id, challenge=raw, exclude=[]
    )


@router.post("/registration/verify", response_model=AuthSessionWithRecovery)
async def finish_registration(
    payload: WebAuthnVerification,
    request: Request,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> AuthSessionWithRecovery:
    credential = _credential_dict(payload)
    challenge = await _take_challenge(session, value=payload.challenge, kind="register")
    if challenge is None or challenge.user_id is None:
        raise _bad_challenge()

    settings = get_api_settings()
    try:
        credential_id, public_key, sign_count = await asyncio.to_thread(
            webauthn.verify_registration,
            settings,
            origins=webauthn.expected_origins(settings, _client_origin(request)),
            credential=credential,
            challenge=_challenge_bytes(challenge),
        )
    except HTTPException:
        raise
    except Exception:
        raise _bad_credential() from None

    existing = await session.scalar(
        select(PasskeyCredential).where(PasskeyCredential.credential_id == credential_id)
    )
    if existing is not None:
        raise _bad_credential()

    now = datetime.now(UTC)
    user = await session.scalar(select(User).where(User.id == challenge.user_id))
    if user is None:
        user = User(id=challenge.user_id, created_at=now)
        session.add(user)
        await session.flush()

    session.add(
        PasskeyCredential(
            credential_id=credential_id,
            user_id=user.id,
            public_key=public_key,
            sign_count=sign_count,
            created_at=now,
        )
    )
    await session.commit()
    recovery_code = await _issue_recovery_code(session, user)
    auth = await _create_user_session(session, user=user)
    return AuthSessionWithRecovery(**auth.model_dump(), recovery_code=recovery_code)


@router.post("/authentication/options")
async def start_authentication(
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> dict[str, Any]:
    """Issue a challenge for a discoverable credential, without an account name."""

    _, raw = await _store_challenge(session, kind="authenticate", user_id=None)
    return webauthn.authentication_options(get_api_settings(), challenge=raw)


@router.post("/authentication/verify", response_model=AuthSession)
async def finish_authentication(
    payload: WebAuthnVerification,
    request: Request,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> AuthSession:
    credential = _credential_dict(payload)
    challenge = await _take_challenge(session, value=payload.challenge, kind="authenticate")
    if challenge is None:
        raise _bad_challenge()

    try:
        credential_id = base64url_to_bytes(str(credential["id"]))
    except Exception:
        raise _bad_credential() from None

    passkey = await session.scalar(
        select(PasskeyCredential).where(PasskeyCredential.credential_id == credential_id)
    )
    if passkey is None:
        raise _bad_credential()

    settings = get_api_settings()
    try:
        new_count = await asyncio.to_thread(
            webauthn.verify_authentication,
            settings,
            origins=webauthn.expected_origins(settings, _client_origin(request)),
            credential=credential,
            challenge=_challenge_bytes(challenge),
            public_key=passkey.public_key,
            sign_count=passkey.sign_count,
        )
    except HTTPException:
        raise
    except Exception:
        raise _bad_credential() from None

    # Synced passkeys can report lower counters on another device; never lock a
    # legitimate user out, but keep the highest value seen.
    passkey.sign_count = max(passkey.sign_count, new_count)
    passkey.last_used_at = datetime.now(UTC)
    user = await session.scalar(select(User).where(User.id == passkey.user_id))
    if user is None:
        raise _bad_credential()
    return await _create_user_session(session, user=user)


@router.post("/passkeys/options")
async def start_passkey_addition(
    user: User = Depends(get_current_user),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> dict[str, Any]:
    """Issue registration options that exclude the user's existing passkeys."""

    _, raw = await _store_challenge(session, kind="register", user_id=user.id)
    existing = await session.scalars(
        select(PasskeyCredential.credential_id).where(PasskeyCredential.user_id == user.id)
    )
    return webauthn.registration_options(
        get_api_settings(), user_id=user.id, challenge=raw, exclude=list(existing)
    )


@router.post("/passkeys", response_model=PasskeyMetadata, status_code=status.HTTP_201_CREATED)
async def finish_passkey_addition(
    payload: WebAuthnVerification,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> PasskeyMetadata:
    credential = _credential_dict(payload)
    challenge = await _take_challenge(session, value=payload.challenge, kind="register")
    if challenge is None or challenge.user_id != user.id:
        raise _bad_challenge()

    settings = get_api_settings()
    try:
        credential_id, public_key, sign_count = await asyncio.to_thread(
            webauthn.verify_registration,
            settings,
            origins=webauthn.expected_origins(settings, _client_origin(request)),
            credential=credential,
            challenge=_challenge_bytes(challenge),
        )
    except HTTPException:
        raise
    except Exception:
        raise _bad_credential() from None

    existing = await session.scalar(
        select(PasskeyCredential).where(PasskeyCredential.credential_id == credential_id)
    )
    if existing is not None:
        raise _bad_credential()

    passkey = PasskeyCredential(
        credential_id=credential_id,
        user_id=user.id,
        public_key=public_key,
        sign_count=sign_count,
        created_at=datetime.now(UTC),
    )
    session.add(passkey)
    await session.commit()
    return _passkey_metadata(passkey)


@router.get("/passkeys", response_model=list[PasskeyMetadata])
async def list_passkeys(
    user: User = Depends(get_current_user),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> list[PasskeyMetadata]:
    credentials = await session.scalars(
        select(PasskeyCredential)
        .where(PasskeyCredential.user_id == user.id)
        .order_by(PasskeyCredential.created_at)
    )
    return [_passkey_metadata(credential) for credential in credentials]


@router.delete("/passkeys/{credential_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_passkey(
    credential_id: str,
    user: User = Depends(get_current_user),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> Response:
    try:
        raw_id = base64url_to_bytes(credential_id)
    except Exception:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None

    result = await session.execute(
        delete(PasskeyCredential).where(
            PasskeyCredential.credential_id == raw_id,
            PasskeyCredential.user_id == user.id,
        )
    )
    await session.commit()
    if result.rowcount == 0:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/recovery/verify", response_model=AuthSessionWithRecovery)
async def recover_account(
    payload: RecoveryVerification,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> AuthSessionWithRecovery:
    code = payload.code.strip()
    if not code or len(code) > 128:
        raise _bad_recovery()
    row = await session.scalar(
        select(RecoveryCode).where(RecoveryCode.lookup == recovery_lookup(code)).with_for_update()
    )
    if row is None or not await asyncio.to_thread(
        verify_recovery_code, code, row.salt, row.code_hash
    ):
        raise _bad_recovery()

    user = await session.scalar(select(User).where(User.id == row.user_id))
    if user is None:
        raise _bad_recovery()
    await session.delete(row)
    await session.commit()
    new_code = await _issue_recovery_code(session, user)
    auth = await _create_user_session(session, user=user)
    return AuthSessionWithRecovery(**auth.model_dump(), recovery_code=new_code)


@router.post("/recovery/rotate", response_model=RecoveryResult)
async def rotate_recovery_code(
    user: User = Depends(get_current_user),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> RecoveryResult:
    return RecoveryResult(recovery_code=await _issue_recovery_code(session, user))


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

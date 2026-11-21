"""Thin WebAuthn wrapper around ``py_webauthn``.

The relying party is the frontend origin, not the API host: the browser talks to
the API with fetch, but the authenticator signs over the frontend's origin.
"""

from __future__ import annotations

import secrets
import uuid
from typing import Any

from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.cose import COSEAlgorithmIdentifier
from webauthn.helpers.options_to_json import options_to_json_dict
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from episode_calendar.api.config import ApiSettings

SUPPORTED_ALGORITHMS = [
    COSEAlgorithmIdentifier.ECDSA_SHA_256,
    COSEAlgorithmIdentifier.EDDSA,
]


def configured_origins(settings: ApiSettings) -> list[str]:
    return [value.strip() for value in settings.auth_expected_origins.split(",") if value.strip()]


def expected_origins(settings: ApiSettings, request_origin: str | None) -> list[str]:
    """Use the configured allowlist, narrowed to the request's Origin header."""

    allowed = configured_origins(settings)
    if request_origin and request_origin in allowed:
        return [request_origin]
    return allowed


def new_challenge() -> tuple[str, bytes]:
    raw = secrets.token_bytes(32)
    return bytes_to_base64url(raw), raw


def decode_challenge(value: str) -> bytes:
    raw = base64url_to_bytes(value)
    if len(raw) != 32:
        raise ValueError("unexpected challenge length")
    return raw


def registration_options(
    settings: ApiSettings,
    *,
    user_id: uuid.UUID,
    challenge: bytes,
    exclude: list[bytes],
) -> dict[str, Any]:
    options = generate_registration_options(
        rp_id=settings.auth_rp_id,
        rp_name=settings.auth_rp_name,
        user_id=user_id.bytes,
        user_name=str(user_id),
        user_display_name=settings.auth_rp_name,
        challenge=challenge,
        exclude_credentials=[PublicKeyCredentialDescriptor(id=value) for value in exclude],
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
        supported_pub_key_algs=SUPPORTED_ALGORITHMS,
    )
    return options_to_json_dict(options)


def authentication_options(settings: ApiSettings, *, challenge: bytes) -> dict[str, Any]:
    options = generate_authentication_options(
        rp_id=settings.auth_rp_id,
        challenge=challenge,
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    return options_to_json_dict(options)


def verify_registration(
    settings: ApiSettings,
    *,
    origins: list[str],
    credential: dict[str, Any],
    challenge: bytes,
) -> tuple[bytes, bytes, int]:
    verified = verify_registration_response(
        credential=credential,
        expected_challenge=challenge,
        expected_origin=origins,
        expected_rp_id=settings.auth_rp_id,
        require_user_verification=True,
        supported_pub_key_algs=SUPPORTED_ALGORITHMS,
    )
    return verified.credential_id, verified.credential_public_key, verified.sign_count


def verify_authentication(
    settings: ApiSettings,
    *,
    origins: list[str],
    credential: dict[str, Any],
    challenge: bytes,
    public_key: bytes,
    sign_count: int,
) -> int:
    verified = verify_authentication_response(
        credential=credential,
        expected_challenge=challenge,
        expected_origin=origins,
        expected_rp_id=settings.auth_rp_id,
        credential_public_key=public_key,
        credential_current_sign_count=sign_count,
        require_user_verification=True,
    )
    return verified.new_sign_count

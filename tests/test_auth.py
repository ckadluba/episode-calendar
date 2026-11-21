from __future__ import annotations

import hashlib
import json
import secrets
import struct
from datetime import UTC, datetime, timedelta
from typing import Any

import cbor2
import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from webauthn.helpers import bytes_to_base64url

from episode_calendar.api.config import get_api_settings
from episode_calendar.api.main import create_app
from episode_calendar.shared.db.models import (
    AuthChallenge,
    PasskeyCredential,
    RecoveryCode,
    User,
    UserSession,
    UserSettings,
)
from episode_calendar.shared.db.session import get_session

RP_ID = "localhost"
ORIGIN = "http://test"


class VirtualPasskey:
    """A tiny software authenticator for exercising the real WebAuthn verification."""

    def __init__(self) -> None:
        self.private_key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = secrets.token_bytes(32)
        self.sign_count = 0

    @property
    def public_key(self) -> bytes:
        numbers = self.private_key.public_key().public_numbers()
        return cbor2.dumps(
            {
                1: 2,
                3: -7,
                -1: 1,
                -2: numbers.x.to_bytes(32, "big"),
                -3: numbers.y.to_bytes(32, "big"),
            }
        )

    def _client_data(self, kind: str, challenge: str) -> bytes:
        return json.dumps(
            {"type": kind, "challenge": challenge, "origin": ORIGIN},
            separators=(",", ":"),
        ).encode()

    def registration(self, challenge: str) -> dict[str, Any]:
        attested = (
            b"\x00" * 16
            + struct.pack(">H", len(self.credential_id))
            + self.credential_id
            + self.public_key
        )
        auth_data = (
            hashlib.sha256(RP_ID.encode()).digest()
            + bytes([0x45])  # UP | UV | AT
            + struct.pack(">I", self.sign_count)
            + attested
        )
        attestation = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        encoded_id = bytes_to_base64url(self.credential_id)
        return {
            "id": encoded_id,
            "rawId": encoded_id,
            "type": "public-key",
            "response": {
                "clientDataJSON": bytes_to_base64url(
                    self._client_data("webauthn.create", challenge)
                ),
                "attestationObject": bytes_to_base64url(attestation),
            },
        }

    def assertion(self, challenge: str) -> dict[str, Any]:
        self.sign_count += 1
        auth_data = (
            hashlib.sha256(RP_ID.encode()).digest()
            + bytes([0x05])  # UP | UV
            + struct.pack(">I", self.sign_count)
        )
        client_data = self._client_data("webauthn.get", challenge)
        signature = self.private_key.sign(
            auth_data + hashlib.sha256(client_data).digest(),
            ec.ECDSA(hashes.SHA256()),
        )
        encoded_id = bytes_to_base64url(self.credential_id)
        return {
            "id": encoded_id,
            "rawId": encoded_id,
            "type": "public-key",
            "response": {
                "clientDataJSON": bytes_to_base64url(client_data),
                "authenticatorData": bytes_to_base64url(auth_data),
                "signature": bytes_to_base64url(signature),
                "userHandle": bytes_to_base64url(secrets.token_bytes(16)),
            },
        }


@pytest.fixture(autouse=True)
def auth_settings(monkeypatch):
    settings = get_api_settings()
    monkeypatch.setattr(settings, "auth_rp_id", RP_ID)
    monkeypatch.setattr(settings, "auth_expected_origins", ORIGIN)
    return settings


def _client(db_session: AsyncSession) -> httpx.AsyncClient:
    app = create_app()

    async def override_session():
        yield db_session

    app.dependency_overrides[get_session] = override_session
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN)


async def _register(client: httpx.AsyncClient) -> tuple[httpx.Response, VirtualPasskey]:
    options = (await client.post("/api/v1/auth/registration/options")).json()
    passkey = VirtualPasskey()
    credential = passkey.registration(options["challenge"])
    response = await client.post(
        "/api/v1/auth/registration/verify",
        json={"challenge": options["challenge"], "credential": credential},
    )
    return response, passkey


async def _login(client: httpx.AsyncClient, passkey: VirtualPasskey) -> httpx.Response:
    options = (await client.post("/api/v1/auth/authentication/options")).json()
    credential = passkey.assertion(options["challenge"])
    return await client.post(
        "/api/v1/auth/authentication/verify",
        json={"challenge": options["challenge"], "credential": credential},
    )


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_registration_creates_account_passkey_and_recovery_code(
    db_session: AsyncSession,
) -> None:
    client = _client(db_session)
    async with client:
        response, _ = await _register(client)
        assert response.status_code == 200
        payload = response.json()
        assert set(payload["user"]) == {"id"}
        assert len(payload["recovery_code"]) >= 32

    accounts = (await db_session.scalars(select(User))).all()
    assert len(accounts) == 1
    passkeys = (await db_session.scalars(select(PasskeyCredential))).all()
    assert len(passkeys) == 1
    assert passkeys[0].user_id == accounts[0].id
    assert len((await db_session.scalars(select(RecoveryCode))).all()) == 1
    assert (await db_session.scalars(select(AuthChallenge))).all() == []


async def test_registration_challenge_and_credential_are_single_use(
    db_session: AsyncSession,
) -> None:
    client = _client(db_session)
    async with client:
        options = (await client.post("/api/v1/auth/registration/options")).json()
        passkey = VirtualPasskey()
        body = {
            "challenge": options["challenge"],
            "credential": passkey.registration(options["challenge"]),
        }
        assert (await client.post("/api/v1/auth/registration/verify", json=body)).status_code == 200
        assert (await client.post("/api/v1/auth/registration/verify", json=body)).status_code == 400

        # The same authenticator cannot register the same credential twice.
        options = (await client.post("/api/v1/auth/registration/options")).json()
        duplicate = {
            "challenge": options["challenge"],
            "credential": passkey.registration(options["challenge"]),
        }
        assert (
            await client.post("/api/v1/auth/registration/verify", json=duplicate)
        ).status_code == 400


async def test_passkey_login_works_from_another_device(db_session: AsyncSession) -> None:
    client = _client(db_session)
    async with client:
        registered, passkey = await _register(client)
        token = registered.json()["token"]

        other_device = _client(db_session)
        async with other_device:
            response = await _login(other_device, passkey)
            assert response.status_code == 200
            assert response.json()["user"]["id"] == registered.json()["user"]["id"]
            assert response.json()["token"] != token

            assert (await other_device.get("/api/v1/me", headers=_auth(token))).status_code == 200

            options = (await other_device.post("/api/v1/auth/authentication/options")).json()
            credential = passkey.assertion(options["challenge"])
            request = {
                "challenge": options["challenge"],
                "credential": credential,
            }
            assert (
                await other_device.post("/api/v1/auth/authentication/verify", json=request)
            ).status_code == 200
            assert (
                await other_device.post("/api/v1/auth/authentication/verify", json=request)
            ).status_code == 400


async def test_authentication_rejects_wrong_signature_and_unknown_credential(
    db_session: AsyncSession,
) -> None:
    client = _client(db_session)
    async with client:
        _, passkey = await _register(client)
        options = (await client.post("/api/v1/auth/authentication/options")).json()
        credential = passkey.assertion(options["challenge"])
        credential["response"]["signature"] = bytes_to_base64url(b"\x00" * 64)
        response = await client.post(
            "/api/v1/auth/authentication/verify",
            json={"challenge": options["challenge"], "credential": credential},
        )
        assert response.status_code == 400

        options = (await client.post("/api/v1/auth/authentication/options")).json()
        stranger = VirtualPasskey().assertion(options["challenge"])
        response = await client.post(
            "/api/v1/auth/authentication/verify",
            json={"challenge": options["challenge"], "credential": stranger},
        )
        assert response.status_code == 400


async def test_unexpected_origin_is_rejected(db_session: AsyncSession, monkeypatch) -> None:
    client = _client(db_session)
    async with client:
        _, passkey = await _register(client)
        monkeypatch.setattr(get_api_settings(), "auth_expected_origins", "https://other.example")
        options = (await client.post("/api/v1/auth/authentication/options")).json()
        response = await client.post(
            "/api/v1/auth/authentication/verify",
            json={
                "challenge": options["challenge"],
                "credential": passkey.assertion(options["challenge"]),
            },
        )
        assert response.status_code == 400


async def test_expired_challenge_is_rejected(db_session: AsyncSession, monkeypatch) -> None:
    client = _client(db_session)
    async with client:
        options = (await client.post("/api/v1/auth/authentication/options")).json()
        challenge = await db_session.scalar(
            select(AuthChallenge).where(AuthChallenge.challenge == options["challenge"])
        )
        assert challenge is not None
        challenge.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await db_session.commit()

        response = await client.post(
            "/api/v1/auth/authentication/verify",
            json={
                "challenge": options["challenge"],
                "credential": VirtualPasskey().assertion(options["challenge"]),
            },
        )
        assert response.status_code == 400


async def test_additional_passkeys_can_be_managed(db_session: AsyncSession) -> None:
    client = _client(db_session)
    async with client:
        registered, first = await _register(client)
        auth = _auth(registered.json()["token"])

        options = (await client.post("/api/v1/auth/passkeys/options", headers=auth)).json()
        second = VirtualPasskey()
        added = await client.post(
            "/api/v1/auth/passkeys",
            headers=auth,
            json={
                "challenge": options["challenge"],
                "credential": second.registration(options["challenge"]),
            },
        )
        assert added.status_code == 201
        assert len(added.json()["credential_id"]) > 10

        listed = await client.get("/api/v1/auth/passkeys", headers=auth)
        assert listed.status_code == 200
        assert len(listed.json()) == 2

        # Both credentials sign into the same account.
        assert (await _login(client, first)).status_code == 200
        assert (await _login(client, second)).status_code == 200

        credential_id = added.json()["credential_id"]
        assert (
            await client.delete(f"/api/v1/auth/passkeys/{credential_id}", headers=auth)
        ).status_code == 204
        assert len((await client.get("/api/v1/auth/passkeys", headers=auth)).json()) == 1
        assert (
            await client.delete(f"/api/v1/auth/passkeys/{credential_id}", headers=auth)
        ).status_code == 404

        # A passkey cannot be added with an anonymous request.
        assert (await client.post("/api/v1/auth/passkeys/options")).status_code == 401


async def test_recovery_code_signs_in_and_rotates(db_session: AsyncSession) -> None:
    client = _client(db_session)
    async with client:
        registered, _ = await _register(client)
        code = registered.json()["recovery_code"]

        other_device = _client(db_session)
        async with other_device:
            response = await other_device.post("/api/v1/auth/recovery/verify", json={"code": code})
            assert response.status_code == 200
            new_code = response.json()["recovery_code"]
            assert new_code != code
            assert response.json()["user"]["id"] == registered.json()["user"]["id"]

            assert (
                await other_device.post("/api/v1/auth/recovery/verify", json={"code": code})
            ).status_code == 400
            assert (
                await other_device.post("/api/v1/auth/recovery/verify", json={"code": new_code})
            ).status_code == 200


async def test_recovery_code_can_be_rotated_while_signed_in(db_session: AsyncSession) -> None:
    client = _client(db_session)
    async with client:
        registered, _ = await _register(client)
        auth = _auth(registered.json()["token"])
        old_code = registered.json()["recovery_code"]

        response = await client.post("/api/v1/auth/recovery/rotate", headers=auth)
        assert response.status_code == 200
        new_code = response.json()["recovery_code"]
        assert new_code != old_code
        assert (
            await client.post("/api/v1/auth/recovery/verify", json={"code": old_code})
        ).status_code == 400
        assert (
            await client.post("/api/v1/auth/recovery/verify", json={"code": new_code})
        ).status_code == 200
        assert (await client.post("/api/v1/auth/recovery/rotate")).status_code == 401


async def test_settings_are_stored_per_account_and_guarded_by_version(
    db_session: AsyncSession,
) -> None:
    client = _client(db_session)
    async with client:
        first, _ = await _register(client)
        auth = _auth(first.json()["token"])

        response = await client.get("/api/v1/me/settings", headers=auth)
        assert response.status_code == 200
        assert response.json()["version"] == 0

        payload = {
            "data": {
                "series_selection": {"selected": ["series-1"], "known": ["series-1"]},
                "preview_preferences": {"joyn": False},
                "seen_episode_ids": ["episode-1"],
            }
        }
        assert (await client.put("/api/v1/me/settings", headers=auth, json=payload)).json()[
            "version"
        ] == 1

        stale = {
            "data": {**payload["data"], "seen_episode_ids": ["episode-2"]},
            "version": 0,
        }
        response = await client.put("/api/v1/me/settings", headers=auth, json=stale)
        assert response.status_code == 409
        assert response.json()["detail"]["current"]["version"] == 1

        response = await client.put(
            "/api/v1/me/settings", headers=auth, json={**payload, "version": 1}
        )
        assert response.status_code == 200
        assert response.json()["version"] == 2

        second, _ = await _register(client)
        other_auth = _auth(second.json()["token"])
        assert (await client.get("/api/v1/me/settings", headers=other_auth)).json()["version"] == 0

        response = await client.put(
            "/api/v1/me/settings", headers=auth, json={"data": {"unknown_key": True}}
        )
        assert response.status_code == 422


async def test_logout_and_expired_sessions_are_rejected(db_session: AsyncSession) -> None:
    client = _client(db_session)
    async with client:
        registered, passkey = await _register(client)
        token = registered.json()["token"]
        assert (await client.post("/api/v1/auth/logout", headers=_auth(token))).status_code == 204
        assert (await client.get("/api/v1/me", headers=_auth(token))).status_code == 401

        second = await _login(client, passkey)
        token = second.json()["token"]
        session_row = await db_session.scalar(select(UserSession))
        assert session_row is not None
        session_row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await db_session.commit()
        assert (await client.get("/api/v1/me", headers=_auth(token))).status_code == 401


async def test_delete_account_removes_account_data(db_session: AsyncSession) -> None:
    client = _client(db_session)
    async with client:
        registered, _ = await _register(client)
        auth = _auth(registered.json()["token"])
        await client.put(
            "/api/v1/me/settings",
            headers=auth,
            json={"data": {"seen_episode_ids": ["episode-1"]}},
        )
        assert (await client.delete("/api/v1/me", headers=auth)).status_code == 204
        assert (await client.get("/api/v1/me", headers=auth)).status_code == 401

    assert (await db_session.scalars(select(User))).all() == []
    assert (await db_session.scalars(select(UserSettings))).all() == []
    assert (await db_session.scalars(select(PasskeyCredential))).all() == []
    assert (await db_session.scalars(select(RecoveryCode))).all() == []
    assert (await db_session.scalars(select(UserSession))).all() == []


async def test_invalid_payloads_are_rejected(db_session: AsyncSession) -> None:
    client = _client(db_session)
    async with client:
        assert (
            await client.post(
                "/api/v1/auth/registration/verify",
                json={"challenge": "abc", "credential": {}},
            )
        ).status_code == 422
        assert (
            await client.post(
                "/api/v1/auth/authentication/verify",
                json={"challenge": "abc", "credential": {"id": "abc"}},
            )
        ).status_code == 422
        assert (
            await client.post("/api/v1/auth/recovery/verify", json={"code": ""})
        ).status_code == 400
        response = await client.get(
            "/api/v1/me", headers={"Authorization": "Bearer not-a-real-token"}
        )
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"

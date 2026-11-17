from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from episode_calendar.api.config import get_api_settings
from episode_calendar.api.main import create_app
from episode_calendar.shared.db.models import LoginToken, User, UserSession, UserSettings
from episode_calendar.shared.db.session import get_session

EMAIL = "user@example.test"


@pytest.fixture
def sent_emails(monkeypatch) -> list[dict[str, str]]:
    emails: list[dict[str, str]] = []

    async def fake_send(*, recipient: str, code: str, link: str, ttl_minutes: int) -> None:
        emails.append({"recipient": recipient, "code": code, "link": link})

    monkeypatch.setattr("episode_calendar.api.auth.send_login_email", fake_send)
    return emails


@pytest.fixture
def auth_settings(monkeypatch):
    settings = get_api_settings()
    monkeypatch.setattr(settings, "auth_email_cooldown_seconds", 0)
    monkeypatch.setattr(settings, "auth_email_hourly_limit", 100)
    monkeypatch.setattr(settings, "auth_ip_hourly_limit", 100)
    return settings


def _client(db_session: AsyncSession) -> tuple[httpx.AsyncClient, object]:
    app = create_app()

    async def override_session():
        yield db_session

    app.dependency_overrides[get_session] = override_session
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    return client, app


async def _start(client: httpx.AsyncClient, email: str = EMAIL) -> httpx.Response:
    return await client.post("/api/v1/auth/email/start", json={"email": email})


async def _verify_code(client: httpx.AsyncClient, code: str, email: str = EMAIL) -> httpx.Response:
    return await client.post("/api/v1/auth/email/verify", json={"email": email, "code": code})


async def test_code_login_creates_account_and_session(
    db_session: AsyncSession, sent_emails: list[dict[str, str]], auth_settings
) -> None:
    client, _ = _client(db_session)
    async with client:
        response = await _start(client)
        assert response.status_code == 202
        assert response.json() == {"status": "sent"}
        assert len(sent_emails) == 1
        assert sent_emails[0]["recipient"] == EMAIL
        assert "email_token=" in sent_emails[0]["link"]

        response = await _verify_code(client, sent_emails[0]["code"])
        assert response.status_code == 200
        session_payload = response.json()
        assert session_payload["user"]["email"] == EMAIL
        token = session_payload["token"]

        response = await client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200
        assert response.json()["email"] == EMAIL

        # The code is single-use.
        response = await _verify_code(client, sent_emails[0]["code"])
        assert response.status_code == 400


async def test_magic_link_login_works_on_another_device(
    db_session: AsyncSession, sent_emails: list[dict[str, str]], auth_settings
) -> None:
    client, _ = _client(db_session)
    async with client:
        await _start(client)
        link_token = sent_emails[0]["link"].split("email_token=", 1)[1]

        response = await client.post("/api/v1/auth/link/verify", json={"token": link_token})
        assert response.status_code == 200
        assert response.json()["user"]["email"] == EMAIL

        # The link is single-use.
        response = await client.post("/api/v1/auth/link/verify", json={"token": link_token})
        assert response.status_code == 400


async def test_code_is_locked_after_five_wrong_attempts(
    db_session: AsyncSession, sent_emails: list[dict[str, str]], auth_settings
) -> None:
    client, _ = _client(db_session)
    async with client:
        await _start(client)
        real_code = sent_emails[0]["code"]
        wrong_code = "000000" if real_code != "000000" else "111111"
        for _ in range(5):
            response = await _verify_code(client, wrong_code)
            assert response.status_code == 400

        response = await _verify_code(client, real_code)
        assert response.status_code == 400


async def test_new_login_invalidates_previous_code(
    db_session: AsyncSession, sent_emails: list[dict[str, str]], auth_settings
) -> None:
    client, _ = _client(db_session)
    async with client:
        await _start(client)
        first_code = sent_emails[0]["code"]
        await _start(client)
        second_code = sent_emails[1]["code"]
        assert first_code != second_code
        assert (await _verify_code(client, first_code)).status_code == 400
        assert (await _verify_code(client, second_code)).status_code == 200


async def test_start_is_rate_limited_per_email(
    db_session: AsyncSession, sent_emails: list[dict[str, str]], monkeypatch
) -> None:
    settings = get_api_settings()
    monkeypatch.setattr(settings, "auth_email_cooldown_seconds", 60)
    monkeypatch.setattr(settings, "auth_email_hourly_limit", 100)
    monkeypatch.setattr(settings, "auth_ip_hourly_limit", 100)

    client, _ = _client(db_session)
    async with client:
        assert (await _start(client)).status_code == 202
        assert (await _start(client)).status_code == 202
    assert len(sent_emails) == 1

    tokens = (await db_session.scalars(select(LoginToken))).all()
    assert len(tokens) == 1


async def test_start_does_not_reveal_whether_an_account_exists(
    db_session: AsyncSession, sent_emails: list[dict[str, str]], auth_settings
) -> None:
    db_session.add(User(email="known@example.test", email_verified_at=datetime.now(UTC)))
    await db_session.commit()

    client, _ = _client(db_session)
    async with client:
        known = await _start(client, "known@example.test")
        unknown = await _start(client, "unknown@example.test")

    assert known.status_code == unknown.status_code == 202
    assert known.json() == unknown.json() == {"status": "sent"}


async def test_settings_are_stored_per_account_and_guarded_by_version(
    db_session: AsyncSession, sent_emails: list[dict[str, str]], auth_settings
) -> None:
    client, _ = _client(db_session)
    auth = {}
    async with client:
        await _start(client)
        login = await _verify_code(client, sent_emails[0]["code"])
        auth = {"Authorization": f"Bearer {login.json()['token']}"}

        response = await client.get("/api/v1/me/settings", headers=auth)
        assert response.status_code == 200
        assert response.json()["version"] == 0
        assert response.json()["data"]["series_selection"] == {"selected": [], "known": []}

        payload = {
            "data": {
                "series_selection": {"selected": ["series-1"], "known": ["series-1"]},
                "preview_preferences": {"joyn": False},
                "seen_episode_ids": ["episode-1"],
            }
        }
        response = await client.put("/api/v1/me/settings", headers=auth, json=payload)
        assert response.status_code == 200
        assert response.json()["version"] == 1

        response = await client.get("/api/v1/me/settings", headers=auth)
        assert response.json()["data"]["preview_preferences"] == {"joyn": False}

        stale = {
            "data": {**payload["data"], "seen_episode_ids": ["episode-2"]},
            "version": 0,
        }
        response = await client.put("/api/v1/me/settings", headers=auth, json=stale)
        assert response.status_code == 409
        assert response.json()["detail"]["current"]["version"] == 1

        response = await client.put(
            "/api/v1/me/settings",
            headers=auth,
            json={**payload, "version": 1, "data": payload["data"]},
        )
        assert response.status_code == 200
        assert response.json()["version"] == 2

        # Accounts are isolated from each other.
        await _start(client, "other@example.test")
        other = await _verify_code(client, sent_emails[1]["code"], "other@example.test")
        other_auth = {"Authorization": f"Bearer {other.json()['token']}"}
        response = await client.get("/api/v1/me/settings", headers=other_auth)
        assert response.json()["version"] == 0

        # Settings are rejected when they do not match the schema.
        response = await client.put(
            "/api/v1/me/settings",
            headers=auth,
            json={"data": {"unknown_key": True}},
        )
        assert response.status_code == 422


async def test_logout_and_expired_sessions_are_rejected(
    db_session: AsyncSession, sent_emails: list[dict[str, str]], auth_settings
) -> None:
    client, _ = _client(db_session)
    async with client:
        await _start(client)
        login = await _verify_code(client, sent_emails[0]["code"])
        token = login.json()["token"]
        auth = {"Authorization": f"Bearer {token}"}
        assert (await client.get("/api/v1/me", headers=auth)).status_code == 200

        assert (await client.post("/api/v1/auth/logout", headers=auth)).status_code == 204
        assert (await client.get("/api/v1/me", headers=auth)).status_code == 401

        await _start(client)
        login = await _verify_code(client, sent_emails[1]["code"])
        token = login.json()["token"]
        session_row = await db_session.scalar(select(UserSession))
        assert session_row is not None
        session_row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await db_session.commit()

        response = await client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 401


async def test_delete_account_removes_account_data(
    db_session: AsyncSession, sent_emails: list[dict[str, str]], auth_settings
) -> None:
    client, _ = _client(db_session)
    async with client:
        await _start(client)
        login = await _verify_code(client, sent_emails[0]["code"])
        auth = {"Authorization": f"Bearer {login.json()['token']}"}
        await client.put(
            "/api/v1/me/settings",
            headers=auth,
            json={"data": {"seen_episode_ids": ["episode-1"]}},
        )

        assert (await client.delete("/api/v1/me", headers=auth)).status_code == 204
        assert (await client.get("/api/v1/me", headers=auth)).status_code == 401

    assert await db_session.scalar(select(User).where(User.email == EMAIL)) is None
    assert await db_session.scalar(select(UserSettings)) is None
    assert (await db_session.scalars(select(LoginToken))).all() == []


async def test_invalid_email_and_code_are_rejected(
    db_session: AsyncSession, sent_emails: list[dict[str, str]], auth_settings
) -> None:
    client, _ = _client(db_session)
    async with client:
        assert (await _start(client, "not-an-email")).status_code == 422
        assert (
            await client.post("/api/v1/auth/email/verify", json={"email": EMAIL, "code": "abc"})
        ).status_code == 422
        assert (await _start(client)).status_code == 202
        assert (await _verify_code(client, "12345")).status_code == 422
        response = await client.get(
            "/api/v1/me", headers={"Authorization": "Bearer not-a-real-token"}
        )
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"

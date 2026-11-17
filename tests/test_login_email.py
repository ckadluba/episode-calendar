from __future__ import annotations

import logging

import httpx
import pytest

from episode_calendar.api import email as email_module
from episode_calendar.api.config import get_api_settings
from episode_calendar.api.email import LoginEmailError, send_login_email

RECIPIENT = "user@example.test"
CODE = "123456"
LINK = "https://example.test/?email_token=abc"


class _FakeResponse:
    def __init__(self, status_code: int = 202, text: str = "") -> None:
        self.status_code = status_code
        self.text = text

    @property
    def is_error(self) -> bool:
        return self.status_code >= 400


class _FakeClient:
    def __init__(
        self, *, response: _FakeResponse | None = None, exc: Exception | None = None
    ) -> None:
        self.response = response or _FakeResponse()
        self.exc = exc
        self.calls: list[tuple[str, dict[str, str], dict[str, object]]] = []

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def post(self, url: str, headers=None, json=None):
        self.calls.append((url, headers or {}, json or {}))
        if self.exc is not None:
            raise self.exc
        return self.response


@pytest.fixture
def mail_settings(monkeypatch):
    settings = get_api_settings()
    monkeypatch.setattr(settings, "brevo_api_key", "test-key")
    monkeypatch.setattr(settings, "auth_email_from", "Episode Calendar <login@example.test>")
    return settings


def _install_client(monkeypatch, client: _FakeClient) -> _FakeClient:
    monkeypatch.setattr(email_module.httpx, "AsyncClient", lambda **_: client)
    return client


async def test_login_email_is_sent_through_brevo(monkeypatch, mail_settings) -> None:
    client = _install_client(monkeypatch, _FakeClient())

    await send_login_email(recipient=RECIPIENT, code=CODE, link=LINK, ttl_minutes=10)

    url, headers, payload = client.calls[0]
    assert url == "https://api.brevo.com/v3/smtp/email"
    assert headers["api-key"] == "test-key"
    assert payload["sender"] == {"email": "login@example.test", "name": "Episode Calendar"}
    assert payload["to"] == [{"email": RECIPIENT}]
    assert CODE in payload["subject"]
    assert CODE in payload["textContent"]
    assert LINK in payload["textContent"]
    assert LINK in payload["htmlContent"]


async def test_login_email_is_logged_without_api_key(monkeypatch, caplog) -> None:
    monkeypatch.setattr(get_api_settings(), "brevo_api_key", "")

    def fail_client(**_):
        raise AssertionError("no HTTP client without an API key")

    monkeypatch.setattr(email_module.httpx, "AsyncClient", fail_client)

    with caplog.at_level(logging.WARNING):
        await send_login_email(recipient=RECIPIENT, code=CODE, link=LINK, ttl_minutes=10)

    assert CODE in caplog.text
    assert LINK in caplog.text


async def test_rejected_email_raises(monkeypatch, mail_settings) -> None:
    _install_client(monkeypatch, _FakeClient(response=_FakeResponse(401, "bad key")))

    with pytest.raises(LoginEmailError):
        await send_login_email(recipient=RECIPIENT, code=CODE, link=LINK, ttl_minutes=10)


async def test_unreachable_brevo_raises(monkeypatch, mail_settings) -> None:
    _install_client(monkeypatch, _FakeClient(exc=httpx.ConnectError("boom")))

    with pytest.raises(LoginEmailError):
        await send_login_email(recipient=RECIPIENT, code=CODE, link=LINK, ttl_minutes=10)

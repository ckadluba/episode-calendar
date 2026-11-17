"""Delivery of passwordless login emails."""

from __future__ import annotations

import html
import logging
from email.utils import parseaddr

import httpx

from episode_calendar.api.config import get_api_settings

logger = logging.getLogger(__name__)

BREVO_ENDPOINT = "https://api.brevo.com/v3/smtp/email"
ERROR_BODY_LIMIT = 500


class LoginEmailError(RuntimeError):
    """Raised when the login email could not be handed to the mail provider."""


def _build_message(*, code: str, link: str, ttl_minutes: int) -> tuple[str, str, str]:
    subject = f"Dein Anmeldecode für Episode Calendar: {code}"
    text = (
        "Anmelden bei Episode Calendar\n\n"
        f"Dein Code: {code}\n"
        f"Der Code ist {ttl_minutes} Minuten gültig.\n\n"
        "Du kannst den Code auf jedem Gerät eingeben. Alternativ öffnest du diesen Link:\n"
        f"{link}\n\n"
        "Wenn du diesen Login nicht angefordert hast, ignoriere diese E-Mail einfach.\n"
    )
    html_body = f"""\
<!doctype html>
<html lang="de">
  <body style="font-family: system-ui, sans-serif; color: #1d2433; line-height: 1.5;">
    <h1 style="font-size: 20px;">Anmelden bei Episode Calendar</h1>
    <p>Dein Code:</p>
    <p style="font-size: 32px; font-weight: 700; letter-spacing: 6px; margin: 12px 0;">{code}</p>
    <p>Der Code ist {ttl_minutes} Minuten gültig und kann auf jedem Gerät eingegeben werden.</p>
    <p>Alternativ kannst du <a href="{html.escape(link, quote=True)}">diesen Link öffnen</a>.</p>
    <p style="color: #6b7488; font-size: 13px;">
      Wenn du diesen Login nicht angefordert hast, ignoriere diese E-Mail einfach.
    </p>
  </body>
</html>
"""
    return subject, text, html_body


def _sender(from_header: str) -> dict[str, str]:
    """Split ``Name <address>`` into the sender object Brevo expects."""

    name, address = parseaddr(from_header)
    return {"email": address, "name": name or address}


def _build_payload(
    *, recipient: str, from_header: str, subject: str, text: str, html_body: str
) -> dict[str, object]:
    return {
        "sender": _sender(from_header),
        "to": [{"email": recipient}],
        "subject": subject,
        "textContent": text,
        "htmlContent": html_body,
    }


async def send_login_email(*, recipient: str, code: str, link: str, ttl_minutes: int) -> None:
    """Send the login email through Brevo.

    Without ``BREVO_API_KEY`` the message is only logged so local development
    works without a mail account. That fallback is intentionally noisy: a
    production deployment must configure the key.
    """

    settings = get_api_settings()
    subject, text, html_body = _build_message(code=code, link=link, ttl_minutes=ttl_minutes)

    if not settings.brevo_api_key:
        logger.warning(
            "BREVO_API_KEY is not configured; login email for %s was not sent. Code: %s Link: %s",
            recipient,
            code,
            link,
        )
        return

    payload = _build_payload(
        recipient=recipient,
        from_header=settings.auth_email_from,
        subject=subject,
        text=text,
        html_body=html_body,
    )
    headers = {"api-key": settings.brevo_api_key, "accept": "application/json"}

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(BREVO_ENDPOINT, headers=headers, json=payload)
    except httpx.HTTPError as exc:
        logger.exception("Brevo is unreachable; login email for %s was not sent", recipient)
        raise LoginEmailError("login email delivery failed") from exc

    if response.is_error:
        logger.error(
            "Brevo rejected the login email for %s (%s): %s",
            recipient,
            response.status_code,
            response.text[:ERROR_BODY_LIMIT],
        )
        raise LoginEmailError("login email delivery failed")

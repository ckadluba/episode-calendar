from functools import lru_cache

from episode_calendar.shared.config import Settings


class ApiSettings(Settings):
    """Configuration for the FastAPI read service."""

    cors_origins: str = (
        "http://localhost:5173,http://127.0.0.1:5173,http://localhost:4173,http://127.0.0.1:4173"
    )
    # Passwordless login. Without BREVO_API_KEY the login email is only logged,
    # which keeps local development free of external accounts.
    brevo_api_key: str = ""
    auth_email_from: str = "Episode Calendar <login@example.com>"
    auth_link_base_url: str = "http://localhost:5173"
    auth_code_ttl_seconds: int = 600
    auth_email_cooldown_seconds: int = 60
    auth_email_hourly_limit: int = 5
    auth_ip_hourly_limit: int = 20
    auth_session_ttl_days: int = 30


@lru_cache
def get_api_settings() -> ApiSettings:
    return ApiSettings()  # type: ignore[call-arg]

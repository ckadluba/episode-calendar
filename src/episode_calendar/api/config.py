from functools import lru_cache

from episode_calendar.shared.config import Settings


class ApiSettings(Settings):
    """Configuration for the FastAPI read service."""

    cors_origins: str = (
        "http://localhost:5173,http://127.0.0.1:5173,http://localhost:4173,http://127.0.0.1:4173"
    )
    # Passkey/WebAuthn. The RP id is the frontend host; moving the frontend to a
    # different domain invalidates every registered passkey.
    auth_rp_id: str = "localhost"
    auth_rp_name: str = "Episode Calendar"
    auth_expected_origins: str = (
        "http://localhost:5173,http://127.0.0.1:5173,http://localhost:4173,http://127.0.0.1:4173"
    )
    auth_challenge_ttl_seconds: int = 300
    auth_session_ttl_days: int = 30


@lru_cache
def get_api_settings() -> ApiSettings:
    return ApiSettings()  # type: ignore[call-arg]

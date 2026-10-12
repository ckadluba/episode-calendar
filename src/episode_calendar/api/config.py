from functools import lru_cache

from episode_calendar.shared.config import Settings


class ApiSettings(Settings):
    """Configuration for the FastAPI read service."""

    cors_origins: str = (
        "http://localhost:5173,http://127.0.0.1:5173,http://localhost:4173,http://127.0.0.1:4173"
    )


@lru_cache
def get_api_settings() -> ApiSettings:
    return ApiSettings()  # type: ignore[call-arg]

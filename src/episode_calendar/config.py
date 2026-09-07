from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration loaded from environment variables."""

    database_url: str
    app_name: str = "episode-calendar"
    debug: bool = False
    joyn_api_key: str | None = None
    joyn_graphql_url: str = "https://api.joyn.de/graphql"
    joyn_timeout_seconds: float = 10.0
    series_config_path: str = "config/series.json"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]

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
    rtlplus_layout_url: str = "https://layout.rtlde.bedrock.tech/front/v1/rtlde/m6group_web/main/token-web-31/program/{program_id}/layout"
    rtlplus_bedrock_token: str | None = None
    rtlplus_authorization: str | None = None
    rtlplus_oidc_token_url: str = (
        "https://auth.rtl.de/auth/realms/rtlplus/protocol/openid-connect/token"
    )
    rtlplus_oidc_client_id: str = "anonymous-user"
    rtlplus_oidc_client_secret: str | None = None
    rtlplus_auth_url: str = (
        "https://front-auth.rtlde.bedrock.tech/v2/rtlde/platforms/m6group_web/token"
    )
    rtlplus_timeout_seconds: float = 10.0
    series_config_path: str = "config/series.json"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]

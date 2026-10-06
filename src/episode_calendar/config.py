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
    rtlplus_epg_url: str = "https://pc.middleware.rtlde.bedrock.tech/6play/v2/platforms/m6group_web/services/rtlde_rtl/guidetv"
    rtlplus_epg_channels: str = "rtlde_rtl,rtlde_vox,rtlde_rtlzwei,rtlde_voxup"
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
    bbc_iplayer_api_url: str = "https://ibl.api.bbci.co.uk/ibl/v1"
    bbc_iplayer_timeout_seconds: float = 10.0
    bbc_iplayer_schedule_channels: str = "bbc_one_london"
    bbc_iplayer_schedule_days: int = 14
    channel4_base_url: str = "https://www.channel4.com/programmes"
    channel4_tv_guide_url: str = "https://www.channel4.com/tv-guide"
    channel4_schedule_days: int = 14
    channel4_timeout_seconds: float = 10.0
    ardmediathek_api_url: str = "https://api.ardmediathek.de/page-gateway/widgets/ard/asset"
    ardmediathek_timeout_seconds: float = 10.0
    ardmediathek_program_url: str = "https://programm-api.ard.de/program/api/program"
    ardmediathek_schedule_days: int = 14
    stv_player_url: str = "https://player.stv.tv"
    stv_api_url: str = "https://player.api.stv.tv/v1"
    stv_timeout_seconds: float = 10.0
    amazon_prime_de_base_url: str = "https://www.primevideo.com/-/de"
    amazon_prime_de_timeout_seconds: float = 10.0
    amazon_prime_uk_base_url: str = "https://www.primevideo.com/-/gb"
    amazon_prime_uk_timeout_seconds: float = 10.0
    import_delay_seconds: float = 1.0
    import_max_retries: int = 3
    import_backoff_seconds: float = 1.0
    series_config_path: str = "config/series.json"
    cors_origins: str = (
        "http://localhost:5173,http://127.0.0.1:5173,http://localhost:4173,http://127.0.0.1:4173"
    )

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]

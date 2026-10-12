from episode_calendar.api.config import ApiSettings
from episode_calendar.importer.config import ImporterSettings
from episode_calendar.shared.config import Settings, get_settings
from episode_calendar.shared.db.session import get_engine


def test_database_url_is_loaded_from_environment(monkeypatch) -> None:
    database_url = "postgresql+asyncpg://user:secret@database:5432/calendar"
    monkeypatch.setenv("DATABASE_URL", database_url)

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.database_url == database_url


def test_shared_settings_ignore_component_specific_fields(monkeypatch) -> None:
    monkeypatch.setenv("JOYN_API_KEY", "public-web-key")
    monkeypatch.setenv("CORS_ORIGINS", "https://example.test")

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert not hasattr(settings, "joyn_api_key")
    assert not hasattr(settings, "cors_origins")


def test_api_settings_read_cors_origins(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://user:secret@database:5432/calendar")
    monkeypatch.setenv("CORS_ORIGINS", "https://example.test")

    settings = ApiSettings(_env_file=None)  # type: ignore[call-arg]

    assert settings.cors_origins == "https://example.test"


def test_importer_settings_read_provider_configuration(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://user:secret@database:5432/calendar")
    monkeypatch.setenv("JOYN_API_KEY", "public-web-key")
    monkeypatch.setenv("BBC_IPLAYER_SCHEDULE_DAYS", "3")

    settings = ImporterSettings(_env_file=None)  # type: ignore[call-arg]

    assert settings.joyn_api_key == "public-web-key"
    assert settings.bbc_iplayer_schedule_days == 3


async def test_database_configuration_creates_async_postgres_engine(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://user:secret@database:5432/calendar")
    get_settings.cache_clear()
    get_engine.cache_clear()

    engine = get_engine()

    assert engine.url.drivername == "postgresql+asyncpg"
    assert engine.url.database == "calendar"
    await engine.dispose()
    get_engine.cache_clear()
    get_settings.cache_clear()

from episode_calendar.config import Settings, get_settings
from episode_calendar.db.session import get_engine


def test_database_url_is_loaded_from_environment(monkeypatch) -> None:
    database_url = "postgresql+asyncpg://user:secret@database:5432/calendar"
    monkeypatch.setenv("DATABASE_URL", database_url)

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.database_url == database_url


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

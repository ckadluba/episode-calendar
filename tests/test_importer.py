import asyncio
import logging
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from episode_calendar.db.models import Episode, EpisodeRelease, Provider, Season, Series
from episode_calendar.domain import ReleaseType
from episode_calendar.importer import (
    ImportResult,
    configured_series,
    import_configured,
    import_series,
)
from episode_calendar.providers.base import (
    NormalizedEpisode,
    NormalizedEpisodeRelease,
    NormalizedSeason,
    NormalizedSeries,
)
from episode_calendar.series_config import displayable_series_by_platform


class FakeProvider:
    slug = "joyn"

    async def fetch_series(self, external_id: str) -> NormalizedSeries:
        return NormalizedSeries(
            external_id="joyn-series",
            title="Demo",
            seasons=(
                NormalizedSeason(
                    external_id="joyn-season",
                    number=1,
                    episodes=(
                        NormalizedEpisode(
                            external_id="joyn-episode",
                            number=1,
                            title="Pilot",
                            releases=(
                                NormalizedEpisodeRelease(
                                    release_type=ReleaseType.STREAMING,
                                    release_at=datetime(2026, 9, 6, 18, 30, tzinfo=UTC),
                                    url="https://www.joyn.at/episode",
                                ),
                            ),
                        ),
                    ),
                ),
            ),
        )


async def test_import_is_idempotent(db_session: AsyncSession) -> None:
    provider = FakeProvider()
    first = await import_series(db_session, provider, "ignored")
    second = await import_series(db_session, provider, "ignored")

    assert first.series.id == second.series.id
    assert first.new_episodes == 1
    assert second.new_episodes == 0
    assert (await db_session.scalar(select(func.count()).select_from(Provider))) == 1
    assert (await db_session.scalar(select(func.count()).select_from(Series))) == 1
    assert (await db_session.scalar(select(func.count()).select_from(Season))) == 1
    assert (await db_session.scalar(select(func.count()).select_from(Episode))) == 1
    assert (await db_session.scalar(select(func.count()).select_from(EpisodeRelease))) == 1


async def test_import_updates_existing_metadata(db_session: AsyncSession) -> None:
    result = await import_series(db_session, FakeProvider(), "ignored")
    persisted = await db_session.get(Series, result.series.id)
    assert persisted is not None
    assert persisted.title == "Demo"
    assert persisted.provider.slug == "joyn"


async def test_reimport_removes_releases_missing_from_provider(db_session: AsyncSession) -> None:
    class ChangingProvider(FakeProvider):
        calls = 0

        async def fetch_series(self, external_id: str) -> NormalizedSeries:
            normalized = await super().fetch_series(external_id)
            self.calls += 1
            if self.calls > 1:
                season = normalized.seasons[0]
                normalized = normalized.model_copy(
                    update={
                        "seasons": (
                            season.model_copy(
                                update={
                                    "episodes": (
                                        season.episodes[0].model_copy(update={"releases": ()}),
                                    )
                                }
                            ),
                        )
                    }
                )
            return normalized

    provider = ChangingProvider()
    await import_series(db_session, provider, "ignored")
    await import_series(db_session, provider, "ignored")

    assert (await db_session.scalar(select(func.count()).select_from(EpisodeRelease))) == 0


def test_configured_series_reads_provider_lists(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "series.json"
    config_path.write_text(
        '{"platforms": [{"id": "joyn", "name": "Joyn.at", "series": '
        '[{"id": " demo "}, {"id": "hidden", "run_import": false}, '
        '{"id": ""}, {"id": "second"}]}]}',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "episode_calendar.series_config.get_settings",
        lambda: SimpleNamespace(series_config_path=str(config_path)),
    )

    assert configured_series("joyn") == ("demo", "second")


def test_configured_series_display_flags(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "series.json"
    config_path.write_text(
        '{"platforms": [{"id": "joyn", "name": "Joyn.at", "display": false, '
        '"series": [{"id": "hidden-platform"}]}, '
        '{"id": "rtlplus", "name": "RTL+", "series": '
        '[{"id": "shown"}, {"id": "hidden", "display": false}]}]}',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "episode_calendar.series_config.get_settings",
        lambda: SimpleNamespace(series_config_path=str(config_path)),
    )

    assert displayable_series_by_platform() == {"rtlplus": {"shown"}}


def test_displayable_series_keeps_legacy_ids_when_all_series_are_visible(
    tmp_path, monkeypatch
) -> None:
    config_path = tmp_path / "series.json"
    config_path.write_text(
        '{"platforms": [{"id": "joyn", "name": "Joyn.at", "series": [{"id": "configured-slug"}]}]}',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "episode_calendar.series_config.get_settings",
        lambda: SimpleNamespace(series_config_path=str(config_path)),
    )

    assert displayable_series_by_platform() == {"joyn": None}


def test_configured_bbc_series_accepts_labeled_ids(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "series.json"
    config_path.write_text(
        '{"platforms": [{"id": "bbc_iplayer", "name": "BBC iPlayer", "series": '
        '[{"id": " m1234567 ", "comment": "Demo"}]}]}',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "episode_calendar.series_config.get_settings",
        lambda: SimpleNamespace(series_config_path=str(config_path)),
    )

    assert configured_series("bbc_iplayer") == ("m1234567",)


@pytest.mark.asyncio
async def test_configured_imports_run_in_parallel_and_isolate_failures(monkeypatch, caplog) -> None:
    class Session:
        provider = None

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return None

        async def rollback(self) -> None:
            pass

        async def scalar(self, statement):
            return self.provider

        def add(self, provider) -> None:
            self.provider = provider

        async def commit(self) -> None:
            pass

    class SessionFactory:
        def __call__(self):
            return Session()

    active = 0
    maximum_active = 0

    async def fake_import_series(session, adapter, external_id, *, provider_name):
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        if external_id == "broken":
            raise RuntimeError("provider unavailable")
        return ImportResult(
            series=SimpleNamespace(title="Working"),
            seasons=1,
            episodes=2,
            new_episodes=1,
            releases=2,
        )

    monkeypatch.setattr(
        "episode_calendar.importer.configured_platform",
        lambda provider: SimpleNamespace(
            series=(("working", True, True), ("broken", True, True)),
            name="Test Provider",
            run_import=True,
        ),
    )
    monkeypatch.setattr("episode_calendar.importer.get_session_factory", lambda: SessionFactory())
    monkeypatch.setattr("episode_calendar.importer.import_series", fake_import_series)
    caplog.set_level(logging.INFO, logger="episode_calendar.importer")

    with pytest.raises(RuntimeError, match="1 of 2 Test Provider imports failed"):
        await import_configured("demo", lambda: SimpleNamespace(slug="demo"), "Test Provider")

    assert maximum_active == 2
    messages = [record.getMessage() for record in caplog.records]
    assert "Imported Test Provider/Working: 1 seasons, 2 episodes (1 new), 2 releases" in messages
    assert "Import failed for Test Provider/broken: provider unavailable" in messages


@pytest.mark.asyncio
async def test_import_skips_inactive_platform(monkeypatch, caplog) -> None:
    monkeypatch.setattr(
        "episode_calendar.importer.configured_platform",
        lambda provider: SimpleNamespace(
            name="Test Provider", run_import=False, series=(("ignored", True, True),)
        ),
    )
    caplog.set_level(logging.INFO, logger="episode_calendar.importer")

    await import_configured("demo", lambda: SimpleNamespace(slug="demo"), "Unused Name")

    assert "Skipped provider Test Provider/demo: run_import=false" in [
        record.getMessage() for record in caplog.records
    ]

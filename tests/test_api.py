from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from episode_calendar.api import _select_release
from episode_calendar.db.models import Episode, EpisodeRelease, Provider, Season, Series
from episode_calendar.domain import ReleaseType
from episode_calendar.main import create_app


def test_select_release_keeps_preview_before_later_tv_release() -> None:
    provider = Provider(slug="joyn", name="Joyn.at")
    preview = EpisodeRelease(
        provider=provider,
        release_type=ReleaseType.STREAMING,
        release_at=datetime(2026, 9, 29, 18, tzinfo=UTC),
        preview=True,
    )
    tv = EpisodeRelease(
        provider=provider,
        release_type=ReleaseType.TV_BROADCAST,
        release_at=datetime(2026, 10, 6, 18, tzinfo=UTC),
    )

    assert _select_release([preview, tv]) is preview


def test_select_release_prefers_tv_over_later_catalog_release() -> None:
    provider = Provider(slug="joyn", name="Joyn.at")
    streaming = EpisodeRelease(
        provider=provider,
        release_type=ReleaseType.STREAMING,
        release_at=datetime(2026, 10, 14, 0, 0, tzinfo=UTC),
    )
    tv = EpisodeRelease(
        provider=provider,
        release_type=ReleaseType.TV_BROADCAST,
        release_at=datetime(2026, 10, 7, 18, 15, tzinfo=UTC),
    )

    assert _select_release([streaming, tv]) is tv


def test_select_release_keeps_catalog_release_before_tv_premiere() -> None:
    provider = Provider(slug="joyn", name="Joyn.at")
    streaming = EpisodeRelease(
        provider=provider,
        release_type=ReleaseType.STREAMING,
        release_at=datetime(2026, 10, 7, 0, 0, tzinfo=UTC),
    )
    tv = EpisodeRelease(
        provider=provider,
        release_type=ReleaseType.TV_BROADCAST,
        release_at=datetime(2026, 10, 7, 18, 15, tzinfo=UTC),
    )

    assert _select_release([streaming, tv]) is streaming


def test_select_release_ignores_ordering_artifact_without_prereleases() -> None:
    provider = Provider(slug="ardmediathek", name="ARD Mediathek")
    streaming = EpisodeRelease(
        provider=provider,
        release_type=ReleaseType.STREAMING,
        release_at=datetime(2026, 10, 8, 22, 5, tzinfo=UTC),
    )
    tv = EpisodeRelease(
        provider=provider,
        release_type=ReleaseType.TV_BROADCAST,
        release_at=datetime(2026, 9, 24, 16, 50, tzinfo=UTC),
    )

    assert _select_release([streaming, tv], has_prereleases=False) is streaming
    assert _select_release([streaming, tv], has_prereleases=True) is tv


@pytest.mark.asyncio
async def test_platforms_endpoint_exposes_prerelease_flags() -> None:
    app = create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/v1/platforms")

    assert response.status_code == 200
    platforms = {platform["id"]: platform for platform in response.json()}
    assert platforms["joyn"]["has_prereleases"] is True
    assert platforms["ardmediathek"]["has_prereleases"] is False


@pytest.mark.asyncio
async def test_episodes_do_not_leak_into_adjacent_weeks(
    db_session: AsyncSession, monkeypatch
) -> None:
    monkeypatch.setattr(
        "episode_calendar.api.displayable_series_by_platform", lambda: {"joyn": None}
    )
    provider = Provider(slug="joyn", name="Joyn.at")
    series = Series(provider=provider, external_id="d-series", title="Demo")
    season = Season(series=series, external_id="c-season", number=1)
    episode = Episode(season=season, external_id="e-1", number=1, title="Pilot")
    episode.releases.append(
        EpisodeRelease(
            provider=provider,
            release_type=ReleaseType.STREAMING,
            release_at=datetime(2026, 9, 24, 16, 50, tzinfo=UTC),
        )
    )
    episode.releases.append(
        EpisodeRelease(
            provider=provider,
            release_type=ReleaseType.STREAMING,
            release_at=datetime(2026, 10, 8, 21, 15, tzinfo=UTC),
        )
    )
    db_session.add(episode)
    await db_session.commit()

    app = create_app()

    async def override_session():
        yield db_session

    from episode_calendar.db.session import get_session

    app.dependency_overrides[get_session] = override_session
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        # The window between the two releases contains no release of this episode,
        # so it must not be reported even though one release is before and one after.
        response = await client.get(
            "/api/v1/episodes",
            params={
                "series": str(series.id),
                "from": datetime(2026, 9, 28, tzinfo=UTC).isoformat(),
                "to": datetime(2026, 10, 5, tzinfo=UTC).isoformat(),
            },
        )
        assert response.status_code == 200
        assert response.json() == []

        # A window that contains one of the releases still reports the episode.
        response = await client.get(
            "/api/v1/episodes",
            params={
                "series": str(series.id),
                "from": datetime(2026, 10, 8, tzinfo=UTC).isoformat(),
                "to": datetime(2026, 10, 9, tzinfo=UTC).isoformat(),
            },
        )
        assert response.status_code == 200
        assert [item["external_id"] for item in response.json()] == ["e-1"]

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_series_and_episode_endpoints(db_session: AsyncSession, monkeypatch) -> None:
    monkeypatch.setattr(
        "episode_calendar.api.displayable_series_by_platform", lambda: {"joyn": None}
    )
    provider = Provider(slug="joyn", name="Joyn.at")
    series = Series(provider=provider, external_id="d-series", title="Demo")
    season = Season(series=series, external_id="c-season", number=1)
    episode = Episode(season=season, external_id="e-1", number=1, title="Pilot")
    # Anchor to the Monday of the current calendar week (Europe/Vienna, the API default)
    # so the seeded releases (which span three days) stay inside the current week
    # regardless of the weekday the suite runs on.
    today = datetime.now(ZoneInfo("Europe/Vienna"))
    release_at = (
        (today - timedelta(days=today.weekday()))
        .replace(hour=12, minute=0, second=0, microsecond=0)
        .astimezone(UTC)
    )
    episode.releases.append(
        EpisodeRelease(
            provider=provider,
            release_type=ReleaseType.STREAMING,
            release_at=release_at,
        )
    )
    episode.releases.append(
        EpisodeRelease(
            provider=provider,
            release_type=ReleaseType.STREAMING,
            release_at=release_at + timedelta(days=1),
            rerun=True,
        )
    )
    episode.releases.append(
        EpisodeRelease(
            provider=provider,
            release_type=ReleaseType.STREAMING,
            release_at=release_at + timedelta(days=2),
            preview=True,
        )
    )
    episode.releases.append(
        EpisodeRelease(
            provider=provider,
            release_type=ReleaseType.TV_BROADCAST,
            release_at=release_at + timedelta(days=3),
        )
    )
    db_session.add(episode)
    await db_session.commit()

    app = create_app()

    async def override_session():
        yield db_session

    from episode_calendar.db.session import get_session

    app.dependency_overrides[get_session] = override_session
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/v1/series")
        assert response.status_code == 200
        assert response.json()[0]["external_id"] == "d-series"
        assert response.json()[0]["platform"] == "Joyn.at"
        assert response.json()[0]["platform_id"] == "joyn"
        assert (await client.get("/api/v1/series", params={"platform": "rtlplus"})).json() == []

        response = await client.get(
            "/api/v1/episodes",
            params={
                "from": (release_at - timedelta(hours=1)).isoformat(),
                "series": str(series.id),
            },
        )
        assert response.status_code == 200
        assert response.json()[0]["title"] == "Pilot"
        assert len(response.json()[0]["releases"]) == 1
        assert response.json()[0]["releases"][0]["rerun"] is False
        assert response.json()[0]["releases"][0]["preview"] is False
        assert response.json()[0]["releases"][0]["release_type"] == "streaming"
        history_response = await client.get(
            "/api/v1/episodes",
            params={"series": str(series.id), "includeReleaseHistory": "true"},
        )
        assert len(history_response.json()[0]["releases"]) == 3
        joyn_preview_response = await client.get(
            "/api/v1/episodes",
            params={
                "series": str(series.id),
                "includePreviews": "joyn",
                "includeReleaseHistory": "true",
            },
        )
        assert len(joyn_preview_response.json()[0]["releases"]) == 3
        rtlplus_preview_response = await client.get(
            "/api/v1/episodes",
            params={
                "series": str(series.id),
                "includePreviews": "rtlplus",
                "includeReleaseHistory": "true",
            },
        )
        assert len(rtlplus_preview_response.json()[0]["releases"]) == 2
        assert rtlplus_preview_response.json()[0]["releases"][0]["preview"] is False
        assert (
            len(
                (
                    await client.get(
                        "/api/v1/episodes",
                        params={"series": str(series.id), "includePreviews": "false"},
                    )
                ).json()[0]["releases"]
            )
            == 1
        )
        preview_response = await client.get("/api/v1/episodes", params={"series": str(series.id)})
        assert preview_response.json()[0]["releases"][0]["preview"] is False
        assert preview_response.json()[0]["releases"][0]["release_type"] == "streaming"
        no_preview_response = await client.get(
            "/api/v1/episodes",
            params={"series": str(series.id), "includePreviews": "false"},
        )
        assert no_preview_response.json()[0]["releases"][0]["release_type"] == "streaming"
        rerun_response = await client.get(
            "/api/v1/episodes",
            params={
                "series": str(series.id),
                "includeReruns": "true",
                "includeReleaseHistory": "true",
            },
        )
        assert rerun_response.status_code == 200
        assert len(rerun_response.json()[0]["releases"]) == 4
        assert {release["rerun"] for release in rerun_response.json()[0]["releases"]} == {
            False,
            True,
        }
        timezone_response = await client.get(
            "/api/v1/episodes/current-week",
            params={"series": str(series.id), "timezone": "UTC"},
        )
        assert timezone_response.status_code == 200
        invalid_timezone = await client.get(
            "/api/v1/episodes/current-week", params={"timezone": "not/a-timezone"}
        )
        assert invalid_timezone.status_code == 422
        assert response.json()[0]["platform"] == "Joyn.at"
        assert response.json()[0]["platform_id"] == "joyn"
        assert (await client.get("/api/v1/episodes", params={"platform": "rtlplus"})).json() == []
        assert response.json()[0]["releases"][0]["release_at"].endswith("Z")

        response = await client.get(
            "/api/v1/episodes/current-week", params={"series": str(series.id)}
        )
        assert response.status_code == 200
        assert response.json()[0]["title"] == "Pilot"

        response = await client.get("/api/v1/episodes/next-week", params={"series": str(series.id)})
        assert response.status_code == 200
        assert response.json() == []

        last_week_window = (release_at - timedelta(hours=1), release_at + timedelta(hours=1))
        monkeypatch.setattr(
            "episode_calendar.api._calendar_week_window",
            lambda offset=0, timezone_name="Europe/Vienna": (
                last_week_window if offset == -1 else (release_at, release_at + timedelta(days=7))
            ),
        )
        response = await client.get("/api/v1/episodes/last-week", params={"series": str(series.id)})
        assert response.status_code == 200
        assert response.json()[0]["title"] == "Pilot"

    app.dependency_overrides.clear()

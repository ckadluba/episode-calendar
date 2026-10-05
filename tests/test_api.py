from datetime import UTC, datetime, timedelta

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


@pytest.mark.asyncio
async def test_series_and_episode_endpoints(db_session: AsyncSession, monkeypatch) -> None:
    monkeypatch.setattr(
        "episode_calendar.api.displayable_series_by_platform", lambda: {"joyn": None}
    )
    provider = Provider(slug="joyn", name="Joyn.at")
    series = Series(provider=provider, external_id="d-series", title="Demo")
    season = Season(series=series, external_id="c-season", number=1)
    episode = Episode(season=season, external_id="e-1", number=1, title="Pilot")
    release_at = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
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

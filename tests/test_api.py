from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from episode_calendar.db.models import Episode, EpisodeRelease, Provider, Season, Series
from episode_calendar.domain import ReleaseType
from episode_calendar.main import create_app


@pytest.mark.asyncio
async def test_series_and_episode_endpoints(db_session: AsyncSession) -> None:
    provider = Provider(slug="joyn", name="Joyn")
    series = Series(provider=provider, external_id="d-series", title="Demo")
    season = Season(series=series, external_id="c-season", number=1)
    episode = Episode(season=season, external_id="e-1", number=1, title="Pilot")
    release_at = datetime.now(UTC) + timedelta(days=2)
    episode.releases.append(
        EpisodeRelease(
            provider=provider,
            release_type=ReleaseType.STREAMING,
            release_at=release_at,
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

        response = await client.get(
            "/api/v1/episodes",
            params={
                "from": (release_at - timedelta(hours=1)).isoformat(),
                "series": str(series.id),
            },
        )
        assert response.status_code == 200
        assert response.json()[0]["title"] == "Pilot"
        assert response.json()[0]["releases"][0]["release_at"].endswith("Z")

        response = await client.get(
            "/api/v1/episodes/current-week", params={"series": str(series.id)}
        )
        assert response.status_code == 200
        assert response.json()[0]["title"] == "Pilot"

        response = await client.get("/api/v1/episodes/next-week", params={"series": str(series.id)})
        assert response.status_code == 200
        assert response.json() == []

    app.dependency_overrides.clear()

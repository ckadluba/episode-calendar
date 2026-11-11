import json
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from episode_calendar.importer.providers.stv import STVMalformedResponseError, STVProvider
from episode_calendar.shared.domain import ReleaseType

SUMMARY_HTML = """
<html>
  <a href="/episode/4u0h/im-a-celebrity-get-me-out-of-here">Episode 1</a>
  <a href="https://player.stv.tv/episode/4uap/im-a-celebrity-get-me-out-of-here">Episode 2</a>
</html>
"""


def summary_html(*, subtitle: str | None) -> str:
    """Build a summary page whose embedded ``__NEXT_DATA__`` carries the promo."""

    data = {
        "props": {
            "pageProps": {
                "data": {
                    "programmeHeader": {"promotionalSubtitle": subtitle},
                    "programmeData": {"promotionalSubtitle": subtitle},
                }
            }
        }
    }
    return SUMMARY_HTML + (
        f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script>'
    )


def api_response(*, episode_id: str, number: int, start: str) -> dict:
    return {
        "success": True,
        "results": {
            "guid": f"im-a-celebrity-get-me-out-of-here-202511{number + 15:02d}-2100-demo",
            "title": f"Episode {number}",
            "summary": f"Summary {number}",
            "number": number,
            "programme": {"name": "I'm A Celebrity... Get Me Out Of Here!"},
            "playerSeries": {"name": "Series 25", "episodeIndex": number},
            "schedule": {"startTime": start},
            "availability": {"until": "2026-12-07T23:59:00+0000"},
            "video": {"_duration": "1 hr 16 mins"},
            "id": episode_id,
        },
    }


def schedule_entry(*, start: str, end: str | None = None, episode: dict | None = None) -> dict:
    return {
        "title": "I'm a Celebrity... Get Me Out of Here!",
        "channel": "stv",
        "startTime": start,
        "endTime": end,
        "episode": episode,
    }


class _Routes:
    """A mock STV transport that records whether the linear schedule was requested."""

    def __init__(
        self,
        *,
        html: str,
        catalogue_starts: dict[str, str],
        schedule: list[dict] | None = None,
        schedule_status: int = 200,
    ) -> None:
        self.html = html
        self.catalogue_starts = catalogue_starts
        self.schedule = schedule
        self.schedule_status = schedule_status
        self.schedule_requested = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/summary/im-a-celebrity-get-me-out-of-here":
            return httpx.Response(200, text=self.html)
        if request.url.path == "/v1/schedule":
            self.schedule_requested = True
            if self.schedule is None:
                return httpx.Response(404)
            if self.schedule_status != 200:
                return httpx.Response(self.schedule_status)
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "totalResults": len(self.schedule),
                    "results": self.schedule,
                },
            )
        for short_id, start in self.catalogue_starts.items():
            if request.url.path == f"/v1/episodes/{short_id}":
                return httpx.Response(
                    200,
                    json=api_response(
                        episode_id=short_id,
                        number=list(self.catalogue_starts).index(short_id) + 1,
                        start=start,
                    ),
                )
        return httpx.Response(404)


def provider(client: httpx.AsyncClient, *, now: datetime, **kwargs: object) -> STVProvider:
    return STVProvider(
        client=client,
        player_url="https://example.test",
        api_url="https://example.test/v1",
        clock=lambda: now,
        **kwargs,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_fetch_series_uses_stv_api_metadata() -> None:
    routes = _Routes(
        html=summary_html(subtitle=None),
        catalogue_starts={"4u0h": "2025-11-16T21:00:00+0000", "4uap": "2025-11-17T21:00:00+0000"},
    )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(routes), base_url="https://example.test"
    ) as client:
        result = await provider(client, now=datetime(2026, 11, 11, tzinfo=UTC)).fetch_series(
            "im-a-celebrity-get-me-out-of-here/L2649"
        )

    assert result.title == "I'm A Celebrity... Get Me Out Of Here!"
    assert result.external_id == "im-a-celebrity-get-me-out-of-here/L2649"
    assert result.seasons[0].number == 25
    assert [episode.number for episode in result.seasons[0].episodes] == [1, 2]
    episode = result.seasons[0].episodes[0]
    assert episode.title == "Episode 1"
    assert episode.description == "Summary 1"
    assert episode.duration_minutes == 76
    assert episode.releases[0].release_type is ReleaseType.STREAMING
    assert episode.releases[0].release_at == datetime(2025, 11, 16, 21, tzinfo=ZoneInfo("UTC"))
    assert episode.releases[0].available_until == datetime(
        2026, 12, 7, 23, 59, tzinfo=ZoneInfo("UTC")
    )
    assert str(episode.releases[0].url) == (
        "https://player.stv.tv/episode/4u0h/im-a-celebrity-get-me-out-of-here"
    )
    assert routes.schedule_requested is False


@pytest.mark.asyncio
async def test_fetch_series_rejects_summary_without_episode_links() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>No episodes</html>")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(STVMalformedResponseError, match="contains no episodes"):
            await STVProvider(
                client=client,
                player_url="https://example.test",
                api_url="https://example.test/v1",
            ).fetch_series("big-brother/10a4928")


@pytest.mark.asyncio
async def test_fetch_series_merges_the_announced_season_from_the_schedule() -> None:
    """The "new series" promo switches the adapter to STV's linear schedule."""

    routes = _Routes(
        html=summary_html(subtitle="Brand New Series Coming Monday 16th November"),
        catalogue_starts={"4u0h": "2025-11-16T21:00:00+0000", "4uap": "2025-11-17T21:00:00+0000"},
        schedule=[
            # The catalogue already owns these airings; their ``episode`` is populated.
            schedule_entry(start="2025-11-16T21:00:00+0000", episode={"guid": "20251116-2100-l2v"}),
            schedule_entry(start="2025-11-17T21:00:00+0000", episode={"guid": "20251117-2100-l2v"}),
            # The announced season is not catalogued yet: ``episode`` is null.
            schedule_entry(start="2026-11-16T21:00:00+0000", end="2026-11-16T22:35:00+0000"),
            schedule_entry(start="2026-11-17T21:00:00+0000", end="2026-11-17T22:30:00+0000"),
            schedule_entry(start="2026-11-18T21:00:00+0000", end="2026-11-18T22:15:00+0000"),
            schedule_entry(start="2026-11-19T21:00:00+0000", end="2026-11-19T22:15:00+0000"),
            schedule_entry(start="2026-11-20T21:00:00+0000", end="2026-11-20T22:05:00+0000"),
        ],
    )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(routes), base_url="https://example.test"
    ) as client:
        result = await provider(client, now=datetime(2026, 11, 11, tzinfo=UTC)).fetch_series(
            "im-a-celebrity-get-me-out-of-here/L2649"
        )

    assert routes.schedule_requested is True
    assert [season.number for season in result.seasons] == [25, 26]
    premiere = result.seasons[-1]
    assert premiere.external_id == "epg:season:im-a-celebrity-get-me-out-of-here/L2649:26"
    assert [episode.number for episode in premiere.episodes] == [1, 2, 3, 4, 5]
    airings = [release for episode in premiere.episodes for release in episode.releases]
    assert [release.release_at for release in airings] == [
        datetime(2026, 11, 16, 21, tzinfo=UTC),
        datetime(2026, 11, 17, 21, tzinfo=UTC),
        datetime(2026, 11, 18, 21, tzinfo=UTC),
        datetime(2026, 11, 19, 21, tzinfo=UTC),
        datetime(2026, 11, 20, 21, tzinfo=UTC),
    ]
    assert all(release.release_type is ReleaseType.TV_BROADCAST for release in airings)
    assert all(release.rerun is False for release in airings)
    assert airings[0].available_until == datetime(2026, 11, 16, 22, 35, tzinfo=UTC)


@pytest.mark.asyncio
async def test_fetch_series_ignores_the_schedule_without_a_new_series_promo() -> None:
    routes = _Routes(
        html=summary_html(subtitle=None),
        catalogue_starts={"4u0h": "2025-11-16T21:00:00+0000", "4uap": "2025-11-17T21:00:00+0000"},
        schedule=[schedule_entry(start="2026-11-16T21:00:00+0000")],
    )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(routes), base_url="https://example.test"
    ) as client:
        result = await provider(client, now=datetime(2026, 11, 11, tzinfo=UTC)).fetch_series(
            "im-a-celebrity-get-me-out-of-here/L2649"
        )

    assert routes.schedule_requested is False
    assert [season.number for season in result.seasons] == [25]


@pytest.mark.asyncio
async def test_fetch_series_ignores_schedule_airings_the_catalogue_already_lists() -> None:
    routes = _Routes(
        html=summary_html(subtitle="Brand New Series Coming Monday 16th November"),
        catalogue_starts={"4u0h": "2025-11-16T21:00:00+0000", "4uap": "2025-11-17T21:00:00+0000"},
        schedule=[
            schedule_entry(start="2025-11-16T21:00:00+0000", episode={"guid": "20251116-2100-l2v"}),
            schedule_entry(start="2025-11-17T21:00:00+0000", episode={"guid": "20251117-2100-l2v"}),
        ],
    )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(routes), base_url="https://example.test"
    ) as client:
        result = await provider(client, now=datetime(2026, 11, 11, tzinfo=UTC)).fetch_series(
            "im-a-celebrity-get-me-out-of-here/L2649"
        )

    assert [season.number for season in result.seasons] == [25]


@pytest.mark.asyncio
async def test_fetch_series_ignores_a_new_season_too_close_to_the_catalogue() -> None:
    """A fresh catalogued release means the airing belongs to the running season."""

    routes = _Routes(
        html=summary_html(subtitle="Brand New Series Coming Monday 16th November"),
        catalogue_starts={"4u0h": "2026-10-01T21:00:00+0000", "4uap": "2026-10-02T21:00:00+0000"},
        schedule=[schedule_entry(start="2026-11-01T21:00:00+0000")],
    )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(routes), base_url="https://example.test"
    ) as client:
        result = await provider(client, now=datetime(2026, 11, 11, tzinfo=UTC)).fetch_series(
            "im-a-celebrity-get-me-out-of-here/L2649"
        )

    assert routes.schedule_requested is True
    assert [season.number for season in result.seasons] == [25]


@pytest.mark.asyncio
async def test_fetch_series_keeps_the_catalogue_when_the_schedule_fails() -> None:
    routes = _Routes(
        html=summary_html(subtitle="Brand New Series Coming Monday 16th November"),
        catalogue_starts={"4u0h": "2025-11-16T21:00:00+0000", "4uap": "2025-11-17T21:00:00+0000"},
        schedule=[],
        schedule_status=500,
    )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(routes), base_url="https://example.test"
    ) as client:
        result = await provider(
            client, now=datetime(2026, 11, 11, tzinfo=UTC), max_retries=0, backoff=0.0
        ).fetch_series("im-a-celebrity-get-me-out-of-here/L2649")

    assert [season.number for season in result.seasons] == [25]

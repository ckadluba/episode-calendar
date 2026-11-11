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


def summary_html_with_next_data(
    *,
    subtitle: str | None,
    schedule_next: dict | None,
    next_episode: dict | None,
) -> str:
    """Build a summary page whose embedded ``__NEXT_DATA__`` announces an airing."""

    data = {
        "props": {
            "pageProps": {
                "data": {
                    "programmeHeader": {"promotionalSubtitle": subtitle},
                    "programmeData": {
                        "promotionalSubtitle": subtitle,
                        "scheduleNext": schedule_next,
                    },
                    "nextEpisode": next_episode,
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


@pytest.mark.asyncio
async def test_fetch_series_uses_stv_api_metadata() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/summary/im-a-celebrity-get-me-out-of-here":
            return httpx.Response(200, text=SUMMARY_HTML)
        if request.url.path == "/v1/episodes/4u0h":
            return httpx.Response(
                200,
                json=api_response(
                    episode_id="4u0h",
                    number=1,
                    start="2025-11-16T21:00:00+0000",
                ),
            )
        if request.url.path == "/v1/episodes/4uap":
            return httpx.Response(
                200,
                json=api_response(
                    episode_id="4uap",
                    number=2,
                    start="2025-11-17T21:00:00+0000",
                ),
            )
        return httpx.Response(404)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://example.test",
    ) as client:
        result = await STVProvider(
            client=client,
            player_url="https://example.test",
            api_url="https://example.test/v1",
        ).fetch_series("im-a-celebrity-get-me-out-of-here/L2649")

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


def _summary_with_announcement(*, subtitle: str | None, start: str, end: str | None = None) -> str:
    schedule_next = {"title": "Show", "channel": "stv", "startTime": start}
    if end is not None:
        schedule_next["endTime"] = end
    return summary_html_with_next_data(
        subtitle=subtitle,
        schedule_next=schedule_next,
        next_episode={"title": "Show", "startTime": start},
    )


def _catalogue_handler(html: str, *, starts: dict[str, str]) -> object:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/summary/im-a-celebrity-get-me-out-of-here":
            return httpx.Response(200, text=html)
        for short_id, start in starts.items():
            if request.url.path == f"/v1/episodes/{short_id}":
                return httpx.Response(
                    200,
                    json=api_response(
                        episode_id=short_id,
                        number=list(starts).index(short_id) + 1,
                        start=start,
                    ),
                )
        return httpx.Response(404)

    return handler


@pytest.mark.asyncio
async def test_fetch_series_announces_the_next_season_premiere() -> None:
    html = _summary_with_announcement(
        subtitle="Brand New Series Coming Monday 16th November",
        start="2026-11-16T21:00:00+0000",
        end="2026-11-16T22:35:00+0000",
    )
    handler = _catalogue_handler(
        html, starts={"4u0h": "2025-11-16T21:00:00+0000", "4uap": "2025-11-17T21:00:00+0000"}
    )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await STVProvider(
            client=client,
            player_url="https://example.test",
            api_url="https://example.test/v1",
            clock=lambda: datetime(2026, 11, 11, tzinfo=UTC),
        ).fetch_series("im-a-celebrity-get-me-out-of-here/L2649")

    assert [season.number for season in result.seasons] == [25, 26]
    premiere = result.seasons[-1]
    assert premiere.external_id == "im-a-celebrity-get-me-out-of-here/L2649:season:26"
    assert [episode.number for episode in premiere.episodes] == [1]
    release = premiere.episodes[0].releases[0]
    assert release.release_type is ReleaseType.TV_BROADCAST
    assert release.release_at == datetime(2026, 11, 16, 21, tzinfo=UTC)
    assert release.available_until == datetime(2026, 11, 16, 22, 35, tzinfo=UTC)
    assert release.rerun is False


@pytest.mark.asyncio
async def test_fetch_series_ignores_an_announcement_without_a_new_series_promo() -> None:
    html = _summary_with_announcement(subtitle=None, start="2026-11-16T21:00:00+0000")
    handler = _catalogue_handler(
        html, starts={"4u0h": "2025-11-16T21:00:00+0000", "4uap": "2025-11-17T21:00:00+0000"}
    )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await STVProvider(
            client=client,
            player_url="https://example.test",
            api_url="https://example.test/v1",
            clock=lambda: datetime(2026, 11, 11, tzinfo=UTC),
        ).fetch_series("im-a-celebrity-get-me-out-of-here/L2649")

    assert [season.number for season in result.seasons] == [25]


@pytest.mark.asyncio
async def test_fetch_series_ignores_an_announcement_the_catalogue_already_dates() -> None:
    html = _summary_with_announcement(
        subtitle="Brand New Series Coming Monday 16th November",
        start="2026-11-16T21:00:00+0000",
    )
    handler = _catalogue_handler(
        html, starts={"4u0h": "2026-11-16T21:00:00+0000", "4uap": "2026-11-17T21:00:00+0000"}
    )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await STVProvider(
            client=client,
            player_url="https://example.test",
            api_url="https://example.test/v1",
            clock=lambda: datetime(2026, 11, 11, tzinfo=UTC),
        ).fetch_series("im-a-celebrity-get-me-out-of-here/L2649")

    assert [season.number for season in result.seasons] == [25]


@pytest.mark.asyncio
async def test_fetch_series_ignores_an_announcement_that_is_already_past() -> None:
    html = _summary_with_announcement(
        subtitle="Brand New Series Coming Monday 16th November",
        start="2026-11-16T21:00:00+0000",
    )
    handler = _catalogue_handler(
        html, starts={"4u0h": "2025-11-16T21:00:00+0000", "4uap": "2025-11-17T21:00:00+0000"}
    )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await STVProvider(
            client=client,
            player_url="https://example.test",
            api_url="https://example.test/v1",
            clock=lambda: datetime(2027, 1, 1, tzinfo=UTC),
        ).fetch_series("im-a-celebrity-get-me-out-of-here/L2649")

    assert [season.number for season in result.seasons] == [25]

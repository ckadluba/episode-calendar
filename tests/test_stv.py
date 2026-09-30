from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from episode_calendar.domain import ReleaseType
from episode_calendar.providers.stv import STVMalformedResponseError, STVProvider

SUMMARY_HTML = """
<html>
  <a href="/episode/4u0h/im-a-celebrity-get-me-out-of-here">Episode 1</a>
  <a href="https://player.stv.tv/episode/4uap/im-a-celebrity-get-me-out-of-here">Episode 2</a>
</html>
"""


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

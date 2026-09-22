import json

import httpx
import pytest

from episode_calendar.domain import ReleaseType
from episode_calendar.providers.itvx import (
    ITVXMalformedResponseError,
    ITVXProvider,
)


def page_html(page_props: dict) -> str:
    data = {"props": {"pageProps": page_props}}
    return (
        '<html><script id="__NEXT_DATA__" type="application/json">'
        f"{json.dumps(data)}</script></html>"
    )


def test_normalize_itvx_series_page() -> None:
    result = ITVXProvider._normalize(
        "big-brother/10a4928",
        page_html(
            {
                "title": "Big Brother",
                "seriesList": [
                    {
                        "seriesNumber": 4,
                        "titles": [
                            {
                                "id": "10a4928a0161",
                                "numberedEpisodeTitle": "Series 4 Episode 1",
                                "episodeTitle": "Launch",
                                "synopsis": "The housemates enter.",
                                "broadcastDateTime": "2026-09-22T21:00:00+01:00",
                            }
                        ],
                    }
                ],
            }
        ),
    )

    episode = result.seasons[0].episodes[0]
    assert result.title == "Big Brother"
    assert result.seasons[0].number == 4
    assert episode.number == 1
    assert episode.title == "Launch"
    assert episode.description == "The housemates enter."
    assert episode.releases[0].release_type is ReleaseType.STREAMING
    assert str(episode.releases[0].release_at) == "2026-09-22 20:00:00+00:00"
    assert (
        str(episode.releases[0].url) == "https://www.itv.com/watch/big-brother/10a4928/10a4928a0161"
    )


@pytest.mark.asyncio
async def test_fetch_series_requests_programme_path() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://example.test/watch/im-a-celebrity/L2649"
        assert request.headers["referer"] == "https://www.itv.com/"
        assert request.headers["accept-language"] == "en-GB,en;q=0.9"
        return httpx.Response(
            200,
            text=page_html(
                {
                    "programmeTitle": "I'm a Celebrity... Get Me Out of Here!",
                    "seriesList": [
                        {
                            "seriesNumber": 21,
                            "titles": [
                                {
                                    "encodedEpisodeId": {"letterA": "1a7103a0273"},
                                    "episodeNumber": 1,
                                    "episodeTitle": "Episode 1",
                                    "broadcastDateTime": "2026-11-15T21:00:00Z",
                                }
                            ],
                        }
                    ],
                }
            ),
        )

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await ITVXProvider(
                client=client, base_url="https://example.test/watch"
            ).get_series("im-a-celebrity/L2649")
        assert result.title.startswith("I'm a Celebrity")
        assert result.seasons[0].number == 21
        assert result.seasons[0].episodes[0].external_id == "1a7103a0273"

    await run()


def test_normalize_rejects_missing_next_data() -> None:
    with pytest.raises(ITVXMalformedResponseError, match="__NEXT_DATA__"):
        ITVXProvider._normalize("big-brother/10a4928", "<html></html>")

from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from episode_calendar.domain import ReleaseType
from episode_calendar.providers.channel4 import (
    Channel4MalformedResponseError,
    Channel4Provider,
)


def payload() -> dict:
    return {
        "brandData": {
            "brand": {
                "title": "Demo",
                "summary": "A demo programme",
                "episodes": [
                    {
                        "seriesNumber": 8,
                        "episodeNumber": 7,
                        "programmeId": "78327-007",
                        "fullTitle": "Series 8 Episode 7",
                        "description": "A description",
                        "dateLabel": "First shown: Fri 17 Jul 2026",
                        "hrefLink": "/programmes/demo/on-demand/78327-007",
                    }
                ],
            }
        }
    }


async def test_fetches_public_json_and_normalizes_episode() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/programmes/demo"
        assert request.url.params["json"] == "true"
        return httpx.Response(200, json=payload())

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        result = await Channel4Provider(client=client).fetch_series("demo")
    finally:
        await client.aclose()

    episode = result.seasons[0].episodes[0]
    assert result.title == "Demo"
    assert episode.external_id == "78327-007"
    assert episode.releases[0].release_type is ReleaseType.STREAMING
    assert episode.releases[0].release_at == datetime(2026, 7, 17, tzinfo=ZoneInfo("Europe/London"))


def test_rejects_missing_brand_data() -> None:
    try:
        Channel4Provider._normalize("demo", {})
    except Channel4MalformedResponseError:
        pass
    else:
        raise AssertionError("missing brand data must be rejected")

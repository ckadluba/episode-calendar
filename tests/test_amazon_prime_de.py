from datetime import UTC

import httpx
import pytest

from episode_calendar.providers.amazon_prime_de import (
    AmazonPrimeDEMalformedResponseError,
    AmazonPrimeDEProvider,
)
from episode_calendar.providers.amazon_prime_uk import AmazonPrimeUKProvider


def test_normalize_extracts_seasons_episodes_and_release_dates() -> None:
    result = AmazonPrimeDEProvider._normalize(
        "B0DEMO1234",
        {
            "detail": {
                "title": "Demo-Serie",
                "episodes": [
                    {
                        "titleId": "ep-2",
                        "seasonNumber": 1,
                        "episodeNumber": 2,
                        "title": "Zweiter Abend",
                        "releaseDateTime": "2026-10-02T00:00:00Z",
                    },
                    {
                        "titleId": "ep-1",
                        "seasonNumber": 1,
                        "episodeNumber": 1,
                        "title": "Erster Abend",
                        "releaseDate": "2026-09-25",
                    },
                ],
            }
        },
    )

    assert result.title == "Demo-Serie"
    assert result.seasons[0].number == 1
    assert [episode.number for episode in result.seasons[0].episodes] == [1, 2]
    assert result.seasons[0].episodes[0].releases[0].release_at.tzinfo is UTC
    assert str(result.seasons[0].episodes[0].releases[0].url) == (
        "https://www.primevideo.com/-/de/detail/ep-1"
    )


def test_normalize_rejects_responses_without_dated_episodes() -> None:
    with pytest.raises(AmazonPrimeDEMalformedResponseError, match="no dated episodes"):
        AmazonPrimeDEProvider._normalize("B0DEMO1234", {"title": "Demo-Serie", "episodes": []})


def test_normalize_handles_prime_detail_payload_and_german_dates() -> None:
    result = AmazonPrimeDEProvider._normalize(
        "0T95E4JDS01YBSJEKTAH97YIZK",
        {
            "widgets": {
                "productDetails": {
                    "detail": {
                        "parentTitle": "LOL: Last One Laughing",
                        "seasonNumber": 7,
                        "title": "LOL: Last One Laughing Germany - Staffel 7",
                    }
                },
                "episodeList": {
                    "episodes": [
                        {
                            "detail": {
                                "catalogId": "B0GY9XP3WQ",
                                "episodeNumber": 1,
                                "releaseDate": "13. Mai 2026",
                                "title": "Payback Time",
                            }
                        }
                    ]
                },
            }
        },
    )

    assert result.title == "LOL: Last One Laughing"
    assert result.seasons[0].number == 7
    assert result.seasons[0].episodes[0].number == 1
    assert result.seasons[0].episodes[0].releases[0].release_at.isoformat() == (
        "2026-05-12T22:00:00+00:00"
    )

    abbreviated = AmazonPrimeDEProvider._normalize(
        "0NC2KW152Y9W1X3TIXRYC8U1KM",
        {
            "widgets": {
                "productDetails": {"detail": {"parentTitle": "LOL NEXT", "seasonNumber": 1}},
                "episodeList": {
                    "episodes": [
                        {
                            "detail": {
                                "catalogId": "B0HFH6HHMG",
                                "episodeNumber": 1,
                                "releaseDate": "10. Sept. 2026",
                                "title": "Das crazy",
                            }
                        }
                    ]
                },
            }
        },
    )
    assert abbreviated.seasons[0].episodes[0].releases[0].release_at.isoformat() == (
        "2026-09-09T22:00:00+00:00"
    )


@pytest.mark.asyncio
async def test_fetch_series_uses_prime_detail_api() -> None:
    requested: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.update({"url": str(request.url), "headers": request.headers})
        return httpx.Response(
            200,
            json={
                "title": "Demo-Serie",
                "episodes": [
                    {
                        "titleId": "ep-1",
                        "seasonNumber": 1,
                        "episodeNumber": 1,
                        "title": "Pilot",
                        "releaseDateTime": "2026-09-25T00:00:00Z",
                    }
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await AmazonPrimeDEProvider(client=client).fetch_series("B0DEMO1234")

    assert result.title == "Demo-Serie"
    assert "getDetailPage" in str(requested["url"])
    assert "titleID=B0DEMO1234" in str(requested["url"])
    assert "isElcano=1" in str(requested["url"])
    assert "sections=Atf" in str(requested["url"])
    assert "sections=Btf" in str(requested["url"])


@pytest.mark.asyncio
async def test_prime_uk_reuses_detail_api_adapter_with_uk_marketplace() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/-/gb/gp/video/api/getDetailPage"
        return httpx.Response(
            200,
            json={
                "widgets": {
                    "productDetails": {"detail": {"parentTitle": "LOL: Last One Laughing UK"}},
                    "episodeList": {
                        "episodes": [
                            {
                                "detail": {
                                    "catalogId": "uk-episode-1",
                                    "episodeNumber": 1,
                                    "releaseDate": "18. März 2026",
                                    "title": "Breaking the Ice",
                                }
                            }
                        ]
                    },
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = AmazonPrimeUKProvider(
            client=client,
            base_url="https://www.primevideo.com/-/gb",
        )
        result = await provider.fetch_series("0JAJ59KRPSJY5QIDTK0WTBWMR6")

    assert provider.slug == "amazon_prime_uk"
    assert result.title == "LOL: Last One Laughing UK"
    assert str(result.seasons[0].episodes[0].releases[0].url) == (
        "https://www.primevideo.com/-/gb/detail/uk-episode-1"
    )

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from episode_calendar.domain import ReleaseType
from episode_calendar.providers.joyn import (
    JoynGraphQLError,
    JoynHTTPError,
    JoynMalformedResponseError,
    JoynProvider,
)


def episode(
    episode_id: str,
    number: int,
    title: str,
    *,
    airdate: int | str | None = 1_704_067_200,
    path: str | None = None,
) -> dict[str, Any]:
    return {
        "id": episode_id,
        "number": number,
        "airdate": airdate,
        "endsAt": None,
        "title": title,
        "path": path or f"/serien/demo/episode-{number}",
    }


def season(
    season_id: str,
    number: int,
    episodes: list[dict[str, Any]],
    *,
    total: int | None = None,
) -> dict[str, Any]:
    return {
        "id": season_id,
        "number": number,
        "numberOfEpisodes": len(episodes) if total is None else total,
        "episodes": episodes,
    }


def series_response(seasons: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "data": {
            "page": {
                "__typename": "SeriesPage",
                "path": "/serien/demo",
                "series": {
                    "id": "d_demo",
                    "title": "Demo series",
                    "description": "A demo",
                    "seasons": seasons,
                },
            }
        }
    }


def joyn_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_normalizes_one_season_with_multiple_episodes() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        body = {
            "data": {
                "page": {
                    "__typename": "SeriesPage",
                    "series": {
                        "id": "d_demo",
                        "title": "Demo series",
                        "description": None,
                        "seasons": [
                            season(
                                "c_1",
                                1,
                                [episode("e_1", 1, "Pilot"), episode("e_2", 2, "Second")],
                            )
                        ],
                    },
                }
            }
        }
        return httpx.Response(200, json=body)

    client = joyn_client(handler)
    try:
        result = await JoynProvider(api_key="test-key", client=client).get_series("demo")
    finally:
        await client.aclose()

    assert result.external_id == "d_demo"
    assert result.title == "Demo series"
    assert len(result.seasons) == 1
    assert [item.number for item in result.seasons[0].episodes] == [1, 2]
    assert result.seasons[0].episodes[0].releases[0].release_type is ReleaseType.STREAMING
    assert str(result.seasons[0].episodes[0].releases[0].url) == (
        "https://www.joyn.at/serien/demo/episode-1"
    )


async def test_normalizes_multiple_seasons() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=series_response(
                [season("c_2", 2, [episode("e_2", 1, "S2")]), season("c_1", 1, [])]
            ),
        )

    client = joyn_client(handler)
    try:
        result = await JoynProvider(api_key="test-key", client=client).fetch_series("/serien/demo")
    finally:
        await client.aclose()

    assert [(item.external_id, item.number) for item in result.seasons] == [("c_2", 2), ("c_1", 1)]


async def test_retrieves_all_paginated_episodes() -> None:
    offsets: list[int] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = request.read()
        request_body = json.loads(body)
        if request_body["operationName"] == "EpisodeCalendarSeries":
            return httpx.Response(
                200,
                json=series_response(
                    [
                        season(
                            "c_1",
                            1,
                            [episode("e_1", 1, "One"), episode("e_2", 2, "Two")],
                            total=3,
                        )
                    ]
                ),
            )
        offset = request_body["variables"]["offset"]
        offsets.append(offset)
        return httpx.Response(
            200,
            json={
                "data": {
                    "season": {
                        "id": "c_1",
                        "number": 1,
                        "numberOfEpisodes": 3,
                        "episodes": [episode("e_3", 3, "Three")],
                    }
                }
            },
        )

    client = joyn_client(handler)
    try:
        result = await JoynProvider(api_key="test-key", client=client).get_series("demo")
    finally:
        await client.aclose()

    assert offsets == [2]
    assert [item.external_id for item in result.seasons[0].episodes] == ["e_1", "e_2", "e_3"]


async def test_parses_epoch_release_as_utc_instant() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=series_response([season("c_1", 1, [episode("e_1", 1, "Pilot", airdate=0)])]),
        )

    client = joyn_client(handler)
    try:
        result = await JoynProvider(api_key="test-key", client=client).get_series("demo")
    finally:
        await client.aclose()

    release_at = result.seasons[0].episodes[0].releases[0].release_at
    assert release_at == datetime(1970, 1, 1, tzinfo=UTC)
    assert release_at.tzinfo is UTC


async def test_parses_optional_end_time() -> None:
    raw_episode = episode("e_1", 1, "Pilot")
    raw_episode["endsAt"] = "2024-01-01T01:00:00+01:00"

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=series_response([season("c_1", 1, [raw_episode])]))

    client = joyn_client(handler)
    try:
        result = await JoynProvider(api_key="test-key", client=client).get_series("demo")
    finally:
        await client.aclose()

    assert result.seasons[0].episodes[0].releases[0].available_until == datetime(
        2024, 1, 1, tzinfo=UTC
    )


async def test_allows_missing_optional_episode_metadata() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=series_response(
                [season("c_1", 1, [episode("e_1", 1, "No date", airdate=None, path=None)])]
            ),
        )

    client = joyn_client(handler)
    try:
        result = await JoynProvider(api_key="test-key", client=client).get_series("demo")
    finally:
        await client.aclose()

    assert result.description == "A demo"
    assert result.seasons[0].episodes[0].releases == ()


async def test_reports_graphql_errors() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": None, "errors": [{"message": "Forbidden"}]})

    client = joyn_client(handler)
    try:
        with pytest.raises(JoynGraphQLError, match="Forbidden"):
            await JoynProvider(api_key="test-key", client=client).get_series("demo")
    finally:
        await client.aclose()


async def test_reports_http_errors() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="temporarily unavailable")

    client = joyn_client(handler)
    try:
        with pytest.raises(JoynHTTPError, match="HTTP 503"):
            await JoynProvider(api_key="test-key", client=client).get_series("demo")
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    "payload",
    [
        {"data": {"page": None}},
        {"data": {"page": {"__typename": "MoviePage"}}},
        {"data": {"page": {"__typename": "SeriesPage", "series": {"seasons": "bad"}}}},
        {
            "data": {
                "page": {
                    "__typename": "SeriesPage",
                    "series": {"id": "d", "title": "T", "seasons": None},
                }
            }
        },
    ],
)
async def test_reports_malformed_responses(payload: dict[str, Any]) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    client = joyn_client(handler)
    try:
        with pytest.raises(JoynMalformedResponseError):
            await JoynProvider(api_key="test-key", client=client).get_series("demo")
    finally:
        await client.aclose()


async def test_reports_failed_pagination_instead_of_returning_partial_data() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if json.loads(request.read())["operationName"] == "EpisodeCalendarSeries":
            return httpx.Response(
                200,
                json=series_response([season("c_1", 1, [episode("e_1", 1, "One")], total=2)]),
            )
        return httpx.Response(
            200,
            json={"data": {"season": {"id": "c_1", "numberOfEpisodes": 2, "episodes": []}}},
        )

    client = joyn_client(handler)
    try:
        with pytest.raises(JoynMalformedResponseError, match="no episodes"):
            await JoynProvider(api_key="test-key", client=client).get_series("demo")
    finally:
        await client.aclose()

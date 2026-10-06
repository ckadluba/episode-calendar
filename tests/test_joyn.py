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
    starts_at: int | str | None = None,
    markings: list[str] | None = None,
    path: str | None = None,
) -> dict[str, Any]:
    return {
        "id": episode_id,
        "number": number,
        "startsAt": starts_at,
        "airdate": airdate,
        "endsAt": None,
        "title": title,
        "path": path or f"/serien/demo/episode-{number}",
        "markings": markings,
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


async def test_adds_exactly_matching_future_epg_broadcasts() -> None:
    requests: list[dict[str, Any]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read())
        requests.append(body)
        if body["operationName"] == "EpisodeCalendarJoynEpg":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "liveStreams": [
                            {
                                "id": "sat1-at",
                                "title": "SAT.1",
                                "epgEvents": [
                                    {
                                        "startDate": 1_791_400_000,
                                        "endDate": 1_791_403_600,
                                        "program": {
                                            "__typename": "EpgEntry",
                                            "id": "epg-demo",
                                            "title": "Demo series",
                                        },
                                    },
                                    {
                                        "startDate": 1_791_410_000,
                                        "endDate": None,
                                        "program": {
                                            "__typename": "EpgEntry",
                                            "id": "epg-fbi",
                                            "title": "FBI: Most Wanted",
                                        },
                                    },
                                    {
                                        "startDate": 1_791_420_000,
                                        "endDate": 1_791_441_600,
                                        "program": {
                                            "__typename": "EpgEntry",
                                            "id": "epg-live-block",
                                            "title": "Demo series",
                                        },
                                    },
                                ],
                            }
                        ]
                    }
                },
            )
        return httpx.Response(
            200,
            json=series_response([season("c_1", 1, [episode("e_1", 1, "Pilot")])]),
        )

    client = joyn_client(handler)
    JoynProvider._epg_tasks.clear()
    try:
        result = await JoynProvider(
            api_key="test-key",
            endpoint="https://joyn-test.example/graphql",
            client=client,
            clock=lambda: datetime(2026, 10, 5, tzinfo=UTC),
        ).get_series("demo")
    finally:
        await client.aclose()

    epg_season = result.seasons[-1]
    assert epg_season.external_id == "joyn-epg"
    assert len(epg_season.episodes) == 1
    assert epg_season.episodes[0].title == "Demo series"
    release = epg_season.episodes[0].releases[0]
    assert release.release_type is ReleaseType.TV_BROADCAST
    assert release.external_id == "epg:sat1-at:epg-demo:1791400000"
    epg_requests = [item for item in requests if item["operationName"] == "EpisodeCalendarJoynEpg"]
    assert len(epg_requests) == 28
    assert epg_requests[-1]["variables"]["to"] - epg_requests[0]["variables"]["from"] == 28 * 86400


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


async def test_prefers_streaming_start_over_linear_airdate() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=series_response(
                [
                    season(
                        "c_1",
                        1,
                        [episode("e_1", 1, "Streaming first", airdate=200, starts_at=100)],
                    )
                ]
            ),
        )

    client = joyn_client(handler)
    try:
        result = await JoynProvider(api_key="test-key", client=client).get_series("demo")
    finally:
        await client.aclose()

    releases = result.seasons[0].episodes[0].releases
    assert releases[0].release_type is ReleaseType.STREAMING
    assert releases[0].release_at == datetime(1970, 1, 1, 0, 1, 40, tzinfo=UTC)
    assert releases[1].release_type is ReleaseType.TV_BROADCAST
    assert releases[1].release_at == datetime(1970, 1, 1, 0, 3, 20, tzinfo=UTC)
    assert releases[0].preview is False


async def test_does_not_infer_preview_from_release_dates() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=series_response(
                [
                    season(
                        "c_1",
                        1,
                        [
                            episode(
                                "e_1",
                                1,
                                "Forsthaus Rampensau",
                                airdate="2026-10-12T22:20:00+02:00",
                                starts_at="2026-10-10T00:05:00+02:00",
                            )
                        ],
                    )
                ]
            ),
        )

    client = joyn_client(handler)
    try:
        result = await JoynProvider(api_key="test-key", client=client).get_series("demo")
    finally:
        await client.aclose()

    releases = result.seasons[0].episodes[0].releases
    assert releases[0].release_type is ReleaseType.STREAMING
    assert releases[0].preview is False
    assert releases[1].release_type is ReleaseType.TV_BROADCAST
    assert releases[1].preview is False


async def test_marks_irregular_followup_epg_events_as_reruns() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read())
        if body["operationName"] == "EpisodeCalendarJoynEpg":
            events = [
                ("first", "2026-10-09T20:15:00+02:00", "2026-10-09T22:05:00+02:00"),
                ("second", "2026-10-10T00:15:00+02:00", "2026-10-10T02:05:00+02:00"),
                ("third", "2026-10-12T22:20:00+02:00", "2026-10-13T00:10:00+02:00"),
            ]
            return httpx.Response(
                200,
                json={
                    "data": {
                        "liveStreams": [
                            {
                                "id": "atv",
                                "title": "ATV",
                                "epgEvents": [
                                    {
                                        "startDate": start,
                                        "endDate": end,
                                        "program": {
                                            "__typename": "EpgEntry",
                                            "id": event_id,
                                            "title": "Demo series",
                                        },
                                    }
                                    for event_id, start, end in events
                                ],
                            }
                        ]
                    }
                },
            )
        return httpx.Response(200, json=series_response([]))

    client = joyn_client(handler)
    JoynProvider._epg_tasks.clear()
    try:
        result = await JoynProvider(
            api_key="test-key",
            endpoint="https://joyn-irregular-test.example/graphql",
            client=client,
            clock=lambda: datetime(2026, 10, 6, tzinfo=UTC),
        ).get_series("demo")
    finally:
        await client.aclose()

    releases = [episode.releases[0] for episode in result.seasons[-1].episodes]
    assert [release.rerun for release in releases] == [False, True, True]


async def test_keeps_daily_epg_slots_as_regular_releases() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read())
        if body["operationName"] == "EpisodeCalendarJoynEpg":
            events = [
                ("first", "2026-10-08T20:15:00+02:00"),
                ("second", "2026-10-09T20:15:00+02:00"),
                ("third", "2026-10-10T20:15:00+02:00"),
            ]
            return httpx.Response(
                200,
                json={
                    "data": {
                        "liveStreams": [
                            {
                                "id": "atv",
                                "title": "ATV",
                                "epgEvents": [
                                    {
                                        "startDate": start,
                                        "endDate": None,
                                        "program": {
                                            "__typename": "EpgEntry",
                                            "id": event_id,
                                            "title": "Demo series",
                                        },
                                    }
                                    for event_id, start in events
                                ],
                            }
                        ]
                    }
                },
            )
        return httpx.Response(200, json=series_response([]))

    client = joyn_client(handler)
    JoynProvider._epg_tasks.clear()
    try:
        result = await JoynProvider(
            api_key="test-key",
            endpoint="https://joyn-daily-test.example/graphql",
            client=client,
            clock=lambda: datetime(2026, 10, 6, tzinfo=UTC),
        ).get_series("demo")
    finally:
        await client.aclose()

    releases = [episode.releases[0] for episode in result.seasons[-1].episodes]
    assert [release.rerun for release in releases] == [False, False, False]


async def test_keeps_weekly_epg_slots_as_regular_releases() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read())
        if body["operationName"] == "EpisodeCalendarJoynEpg":
            events = [
                ("first", "2026-10-09T20:15:00+02:00"),
                ("second", "2026-10-16T20:15:00+02:00"),
            ]
            return httpx.Response(
                200,
                json={
                    "data": {
                        "liveStreams": [
                            {
                                "id": "atv",
                                "title": "ATV",
                                "epgEvents": [
                                    {
                                        "startDate": start,
                                        "endDate": None,
                                        "program": {
                                            "__typename": "EpgEntry",
                                            "id": event_id,
                                            "title": "Demo series",
                                        },
                                    }
                                    for event_id, start in events
                                ],
                            }
                        ]
                    }
                },
            )
        return httpx.Response(200, json=series_response([]))

    client = joyn_client(handler)
    JoynProvider._epg_tasks.clear()
    try:
        result = await JoynProvider(
            api_key="test-key",
            endpoint="https://joyn-weekly-test.example/graphql",
            client=client,
            clock=lambda: datetime(2026, 10, 6, tzinfo=UTC),
        ).get_series("demo")
    finally:
        await client.aclose()

    releases = [episode.releases[0] for episode in result.seasons[-1].episodes]
    assert [release.rerun for release in releases] == [False, False]


async def test_catalog_airdate_prevents_duplicate_epg_episode() -> None:
    broadcast_at = 1_791_400_000

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read())
        if body["operationName"] == "EpisodeCalendarJoynEpg":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "liveStreams": [
                            {
                                "id": "sat1-at",
                                "title": "SAT.1",
                                "epgEvents": [
                                    {
                                        "startDate": broadcast_at,
                                        "endDate": broadcast_at + 3600,
                                        "program": {
                                            "__typename": "Episode",
                                            "id": "epg-demo",
                                            "title": "Demo series",
                                        },
                                    }
                                ],
                            }
                        ]
                    }
                },
            )
        return httpx.Response(
            200,
            json=series_response(
                [
                    season(
                        "c_1",
                        1,
                        [
                            episode(
                                "e_1",
                                1,
                                "Pilot",
                                airdate=broadcast_at,
                                starts_at=broadcast_at - 86400,
                            )
                        ],
                    )
                ]
            ),
        )

    client = joyn_client(handler)
    try:
        result = await JoynProvider(
            api_key="test-key",
            endpoint="https://joyn-test.example/graphql",
            client=client,
            clock=lambda: datetime(2026, 10, 5, tzinfo=UTC),
        ).get_series("demo")
    finally:
        await client.aclose()

    assert len(result.seasons) == 1
    catalog_episode = result.seasons[0].episodes[0]
    assert [release.release_type for release in catalog_episode.releases] == [
        ReleaseType.STREAMING,
        ReleaseType.TV_BROADCAST,
    ]
    assert catalog_episode.number == 1


async def test_marks_joyn_preview_episodes() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=series_response(
                [
                    season(
                        "c_1",
                        1,
                        [
                            episode(
                                "e_1",
                                1,
                                "Preview",
                                starts_at="2026-09-29T22:00:00+02:00",
                                airdate="2026-10-06T22:35:00+02:00",
                                markings=["PREVIEW"],
                            )
                        ],
                    )
                ]
            ),
        )

    client = joyn_client(handler)
    try:
        result = await JoynProvider(api_key="test-key", client=client).get_series("demo")
    finally:
        await client.aclose()

    assert result.seasons[0].episodes[0].releases[0].preview is True


async def test_does_not_keep_preview_marking_after_regular_airdate() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=series_response(
                [
                    season(
                        "c_1",
                        1,
                        [
                            episode(
                                "e_1",
                                1,
                                "Regular release",
                                starts_at="2026-10-06T22:35:00+02:00",
                                airdate="2026-10-06T22:35:00+02:00",
                                markings=["PREVIEW"],
                            )
                        ],
                    )
                ]
            ),
        )

    client = joyn_client(handler)
    try:
        result = await JoynProvider(api_key="test-key", client=client).get_series("demo")
    finally:
        await client.aclose()

    assert result.seasons[0].episodes[0].releases[0].preview is False


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

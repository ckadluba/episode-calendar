from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from episode_calendar.domain import ReleaseType
from episode_calendar.providers.bbc_iplayer import (
    BBCIPlayerMalformedResponseError,
    BBCIPlayerProvider,
)


def episode(
    episode_id: str, title: str, subtitle: str, *, start: str | None = "2026-09-21T20:00:00Z"
) -> dict[str, Any]:
    return {
        "id": episode_id,
        "title": title,
        "subtitle": subtitle,
        "synopses": {"small": "Episode description"},
        "release_date_time": "2026-09-21T00:00:00Z",
        "versions": []
        if start is None
        else [
            {"kind": "original", "availability": {"start": start, "end": "2026-10-01T20:00:00Z"}}
        ],
    }


def client_for(payloads: dict[str, dict[str, Any]]) -> httpx.AsyncClient:
    async def handler(request: httpx.Request) -> httpx.Response:
        if "/channels/" in request.url.path:
            return httpx.Response(200, json={"schedule": {"elements": []}})
        if request.url.path.endswith("/episodes") and request.url.params.get("page") == "2":
            return httpx.Response(
                200,
                json={
                    "programme_episodes": {
                        "page": 2,
                        "per_page": 100,
                        "count": 2,
                        "elements": [episode("e2", "Demo", "Series 2: 2. Two")],
                    }
                },
            )
        return httpx.Response(200, json=payloads[request.url.path])

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_normalizes_programme_and_paginates_episodes() -> None:
    payloads = {
        "/ibl/v1/programmes/m1234567": {
            "programmes": [{"title": "Demo", "synopses": {"medium": "A show"}}]
        },
        "/ibl/v1/programmes/m1234567/episodes": {
            "programme_episodes": {
                "page": 1,
                "per_page": 100,
                "count": 2,
                "elements": [episode("e1", "Demo", "Series 2: 1. One")],
            }
        },
    }
    client = client_for(payloads)
    try:
        result = await BBCIPlayerProvider(client=client).get_series("m1234567")
    finally:
        await client.aclose()

    assert result.title == "Demo"
    assert result.seasons[0].number == 2
    normalized = result.seasons[0].episodes[0]
    assert normalized.number == 1
    assert normalized.releases[0].release_type is ReleaseType.STREAMING
    assert normalized.releases[0].release_at == datetime(2026, 9, 21, 20, tzinfo=UTC)


async def test_imports_scheduled_episode_before_iplayer_availability() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if "/channels/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "schedule": {
                        "elements": [
                            {
                                "scheduled_start": "2026-09-26T19:00:00Z",
                                "episode": {
                                    **episode("planned", "Demo", "Series 3: Week 1", start=None),
                                    "tleo_id": "m1234567",
                                    "status": "unavailable",
                                },
                            }
                        ]
                    }
                },
            )
        if request.url.path.endswith("/episodes"):
            return httpx.Response(
                200,
                json={"programme_episodes": {"page": 1, "count": 0, "elements": []}},
            )
        return httpx.Response(200, json={"programmes": [{"title": "Demo"}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        result = await BBCIPlayerProvider(client=client, schedule_days=1).fetch_series("m1234567")
    finally:
        await client.aclose()

    release = result.seasons[0].episodes[0].releases[0]
    assert release.release_at == datetime(2026, 9, 26, 19, tzinfo=UTC)
    assert release.available_until is None


async def test_falls_back_to_release_date_when_no_availability() -> None:
    payloads = {
        "/ibl/v1/programmes/m1234567": {"programmes": [{"title": "Demo"}]},
        "/ibl/v1/programmes/m1234567/episodes": {
            "programme_episodes": {
                "page": 1,
                "per_page": 100,
                "count": 1,
                "elements": [episode("e1", "Demo", "Special", start=None)],
            }
        },
    }
    client = client_for(payloads)
    try:
        result = await BBCIPlayerProvider(client=client).fetch_series("m1234567")
    finally:
        await client.aclose()
    assert result.seasons[0].number is None
    assert result.seasons[0].episodes[0].releases[0].release_at == datetime(2026, 9, 21, tzinfo=UTC)


async def test_rejects_incomplete_pagination() -> None:
    payloads = {
        "/ibl/v1/programmes/m1234567": {"programmes": [{"title": "Demo"}]},
        "/ibl/v1/programmes/m1234567/episodes": {
            "programme_episodes": {"page": 1, "per_page": 100, "count": 2, "elements": []}
        },
    }
    client = client_for(payloads)
    try:
        with pytest.raises(BBCIPlayerMalformedResponseError, match="no episodes"):
            await BBCIPlayerProvider(client=client).fetch_series("m1234567")
    finally:
        await client.aclose()

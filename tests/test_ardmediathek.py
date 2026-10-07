from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from episode_calendar.domain import ReleaseType
from episode_calendar.providers.ardmediathek import ARDMediathekProvider
from episode_calendar.providers.base import NormalizedEpisodeRelease


def teaser(
    episode_id: str,
    title: str,
    broadcasted_on: str,
    *,
    available_to: str | None = "2027-09-25T03:00:00Z",
) -> dict[str, Any]:
    return {
        "id": episode_id,
        "longTitle": title,
        "broadcastedOn": broadcasted_on,
        "availableTo": available_to,
        "show": {"title": "Demo Show"},
    }


async def test_fetches_numbered_episodes_and_ignores_bonus_content() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/program"):
            return httpx.Response(
                200,
                json={
                    "channels": [
                        {
                            "timeSlots": [
                                [
                                    {
                                        "id": "schedule-2",
                                        "title": "Demo Show",
                                        "coreSubline": "The Second One",
                                        "broadcastedOn": "2026-09-22T20:00:00Z",
                                        "synopsis": "The second episode description",
                                    }
                                ]
                            ]
                        }
                    ]
                },
            )
        assert request.url.path.endswith("/asset/demo")
        assert request.url.params["pageNumber"] == "0"
        assert request.url.params["pageSize"] == "100"
        return httpx.Response(
            200,
            json={
                "pagination": {"totalElements": 3},
                "teasers": [
                    teaser(
                        "episode-2",
                        "Folge 2: The Second One (S01/E02)",
                        "2026-09-22T20:00:00Z",
                    ),
                    teaser(
                        "bonus-1",
                        "Bonus: Behind the scenes",
                        "2026-09-22T19:00:00Z",
                    ),
                    teaser(
                        "episode-1",
                        'Folge 1: "The First One" (S01/E01)',
                        "2026-09-15T20:00:00Z",
                    ),
                ],
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        result = await ARDMediathekProvider(client=client, schedule_days=1).fetch_series("demo")
    finally:
        await client.aclose()

    assert result.title == "Demo Show"
    assert len(result.seasons) == 1
    assert [episode.number for episode in result.seasons[0].episodes] == [1, 2]
    assert result.seasons[0].episodes[0].releases == ()
    episode = result.seasons[0].episodes[1]
    assert episode.title == "The Second One"
    assert len(episode.releases) == 1
    assert episode.releases[0].external_id == "schedule-2"
    assert episode.releases[0].release_type is ReleaseType.TV_BROADCAST
    assert episode.releases[0].release_at == datetime(2026, 9, 22, 20, tzinfo=UTC)
    assert str(episode.releases[0].url) == "https://www.ardmediathek.de/tv-programm/schedule-2"


async def test_schedule_window_includes_recent_past_days() -> None:
    requested_days: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/program"):
            requested_days.append(request.url.params["day"])
            return httpx.Response(200, json={"channels": []})
        assert request.url.path.endswith("/asset/demo")
        return httpx.Response(
            200,
            json={
                "pagination": {"totalElements": 1},
                "teasers": [
                    teaser("episode-1", "Folge 1: The First One (S01/E01)", "2026-09-15T20:00:00Z")
                ],
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        await ARDMediathekProvider(
            client=client, schedule_days=2, schedule_lookback_days=5
        ).fetch_series("demo")
    finally:
        await client.aclose()

    today = datetime.now(UTC).date()
    expected = [
        (today - timedelta(days=5) + timedelta(days=offset)).isoformat() for offset in range(7)
    ]
    assert requested_days == expected


async def test_deduplicates_programme_entries_returned_for_several_days() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/program"):
            return httpx.Response(
                200,
                json={
                    "channels": [
                        {
                            "timeSlots": [
                                [
                                    {
                                        "id": "schedule-1",
                                        "title": "Demo Show",
                                        "coreSubline": "The First One",
                                        "broadcastedOn": "2026-09-22T20:00:00Z",
                                        "synopsis": "The first episode description",
                                    }
                                ]
                            ]
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "pagination": {"totalElements": 1},
                "teasers": [
                    teaser(
                        "episode-1",
                        'Folge 1: "The First One" (S01/E01)',
                        "2026-09-15T20:00:00Z",
                    )
                ],
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        result = await ARDMediathekProvider(
            client=client, schedule_days=3, schedule_lookback_days=3
        ).fetch_series("demo")
    finally:
        await client.aclose()

    episode = result.seasons[0].episodes[0]
    assert episode.title == "The First One"
    assert len(episode.releases) == 1
    assert episode.releases[0].external_id == "schedule-1"
    assert episode.releases[0].release_type is ReleaseType.TV_BROADCAST


def test_merges_scheduled_broadcast_with_catalog_episode() -> None:
    assert ARDMediathekProvider._same_title(
        "Werwölfe - Das Spiel von List und Täuschung",
        "Werwölfe · Das Spiel von List und Täuschung",
    )
    series = ARDMediathekProvider._normalize(
        "demo",
        [
            teaser(
                "episode-5",
                "Folge 5: Ein Funken Hoffnung (S01/E05)",
                "2026-09-29T20:00:00Z",
            )
        ],
    )
    scheduled = {
        "Ein Funken Hoffnung": (
            NormalizedEpisodeRelease(
                external_id="schedule-2026-10-01",
                release_type=ReleaseType.TV_BROADCAST,
                release_at=datetime(2026, 10, 1, 17, 15, tzinfo=UTC),
                url="https://www.ardmediathek.de/tv-programm/schedule-2026-10-01",
            ),
        ),
        "Fassungslos": (
            NormalizedEpisodeRelease(
                external_id="schedule-2026-10-01-late",
                release_type=ReleaseType.TV_BROADCAST,
                release_at=datetime(2026, 10, 1, 17, 15, tzinfo=UTC),
                url="https://www.ardmediathek.de/tv-programm/schedule-2026-10-01-late",
            ),
        ),
    }

    result = ARDMediathekProvider._merge_scheduled_releases(series, scheduled)

    assert {release.release_type for release in result.seasons[0].episodes[0].releases} == {
        ReleaseType.STREAMING,
        ReleaseType.TV_BROADCAST,
    }
    scheduled_season = next(season for season in result.seasons if season.title == "Scheduled")
    scheduled_episode = scheduled_season.episodes[0]
    assert scheduled_episode.number is None
    assert scheduled_episode.title == "Fassungslos"


def test_assigns_broadcast_to_target_episode_instead_of_shared_title() -> None:
    series = ARDMediathekProvider._normalize(
        "demo",
        [
            teaser(
                "s1e1",
                'Folge 1: "Ich bin ein Dorfbewohner" (S01/E01)',
                "2026-09-15T20:00:00Z",
            ),
            teaser(
                "s2e1",
                'Folge 1: "Ich bin ein Dorfbewohner" (S02/E01)',
                "2026-09-15T20:00:00Z",
            ),
        ],
    )
    scheduled = {
        "Ich bin ein Dorfbewohner": (
            NormalizedEpisodeRelease(
                external_id="schedule-1",
                release_type=ReleaseType.TV_BROADCAST,
                release_at=datetime(2026, 10, 8, 21, 15, tzinfo=UTC),
                url="https://www.ardmediathek.de/tv-programm/schedule-1",
            ),
        )
    }

    result = ARDMediathekProvider._merge_scheduled_releases(
        series, scheduled, None, {"schedule-1": "s2e1"}
    )

    [first_season, second_season] = result.seasons
    assert [release.release_type for release in first_season.episodes[0].releases] == [
        ReleaseType.STREAMING
    ]
    assert [release.release_type for release in second_season.episodes[0].releases] == [
        ReleaseType.STREAMING,
        ReleaseType.TV_BROADCAST,
    ]
    assert not any(season.number is None for season in result.seasons)

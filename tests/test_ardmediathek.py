from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from episode_calendar.importer.providers.ardmediathek import ARDMediathekProvider
from episode_calendar.importer.providers.base import NormalizedEpisodeRelease
from episode_calendar.shared.domain import ReleaseType


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
    absent_episode = result.seasons[0].episodes[0]
    assert [release.release_type for release in absent_episode.releases] == [ReleaseType.STREAMING]
    assert str(absent_episode.releases[0].url) == "https://www.ardmediathek.de/video/episode-1"
    episode = result.seasons[0].episodes[1]
    assert episode.external_id == "episode-2"
    assert episode.title == "The Second One"
    # The scheduled broadcast belongs to a catalogue episode, so it is not duplicated.
    assert [release.release_type for release in episode.releases] == [ReleaseType.STREAMING]


async def test_keeps_announced_catalog_episode_when_schedule_is_not_published_yet() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/program"):
            return httpx.Response(200, json={"channels": []})
        return httpx.Response(
            200,
            json={
                "pagination": {"totalElements": 2},
                "teasers": [
                    teaser("s2e7", "Folge 7: Der Bärenführer (S02/E07)", "2026-10-22T21:15:00Z"),
                    teaser(
                        "s2e8", "Folge 8: Abschied & Neubeginn (S02/E08)", "2026-10-22T21:55:00Z"
                    ),
                ],
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        result = await ARDMediathekProvider(client=client, schedule_days=1).fetch_series("demo")
    finally:
        await client.aclose()

    [season] = result.seasons
    assert [episode.number for episode in season.episodes] == [7, 8]
    assert [release.release_type for release in season.episodes[0].releases] == [
        ReleaseType.STREAMING
    ]
    assert season.episodes[0].releases[0].release_at == datetime(2026, 10, 22, 21, 15, tzinfo=UTC)


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
    # The scheduled broadcast belongs to a catalogue episode, so it is not duplicated.
    assert [release.release_type for release in episode.releases] == [ReleaseType.STREAMING]


def test_folds_untitled_scheduled_broadcast_into_running_season() -> None:
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
                external_id="schedule-2026-10-06",
                release_type=ReleaseType.TV_BROADCAST,
                release_at=datetime(2026, 10, 6, 20, 0, tzinfo=UTC),
                url="https://www.ardmediathek.de/tv-programm/schedule-2026-10-06",
            ),
        ),
        "Fassungslos": (
            NormalizedEpisodeRelease(
                external_id="schedule-2026-10-06-late",
                release_type=ReleaseType.TV_BROADCAST,
                release_at=datetime(2026, 10, 6, 20, 0, tzinfo=UTC),
                url="https://www.ardmediathek.de/tv-programm/schedule-2026-10-06-late",
            ),
        ),
    }

    result = ARDMediathekProvider._fold_scheduled_releases(
        series, scheduled, now=datetime(2026, 10, 6, 12, tzinfo=UTC)
    )

    assert not any(season.number is None for season in result.seasons)
    [season] = result.seasons
    episode = season.episodes[0]
    assert episode.external_id == "episode-5"
    # The broadcast titled after a catalogue episode is dropped; only the untitled one
    # without a catalogue home is folded into the running season.
    assert [release.release_type for release in episode.releases] == [ReleaseType.STREAMING]
    next_numbered = [episode for episode in season.episodes if episode.number == 6]
    assert len(next_numbered) == 1
    assert next_numbered[0].title == "Fassungslos"
    assert next_numbered[0].releases[0].external_id == "schedule-2026-10-06-late"


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

    result = ARDMediathekProvider._fold_scheduled_releases(
        series, scheduled, None, {"schedule-1": "s2e1"}, now=datetime(2026, 10, 8, 12, tzinfo=UTC)
    )

    # The broadcast belongs to s2e1, which the catalogue already dates, so it is dropped
    # instead of being attached to both equally titled episodes.
    [first_season, second_season] = result.seasons
    assert [release.release_type for release in first_season.episodes[0].releases] == [
        ReleaseType.STREAMING
    ]
    assert [release.release_type for release in second_season.episodes[0].releases] == [
        ReleaseType.STREAMING
    ]
    assert not any(season.number is None for season in result.seasons)


def test_streaming_release_prefers_publication_date() -> None:
    series = ARDMediathekProvider._normalize(
        "demo",
        [
            teaser(
                "episode-4",
                "Folge 4: Das Komplott (S02/E04)",
                "2026-10-15T21:15:00Z",
            )
        ],
    )
    published = ARDMediathekProvider._with_streaming_availability(
        series, {"episode-4": datetime(2026, 9, 24, 17, 50, tzinfo=UTC)}
    )

    [release] = published.seasons[0].episodes[0].releases
    assert release.release_type is ReleaseType.STREAMING
    # The detail payload's on-demand date wins over the future linear broadcast teaser.
    assert release.release_at == datetime(2026, 9, 24, 17, 50, tzinfo=UTC)

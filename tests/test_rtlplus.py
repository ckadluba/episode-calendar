from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from episode_calendar.importer.providers.base import (
    NormalizedEpisode,
    NormalizedSeason,
    NormalizedSeries,
)
from episode_calendar.importer.providers.rtlplus import RTLPlusProvider
from episode_calendar.shared.domain import ReleaseType

VIENNA = ZoneInfo("Europe/Vienna")


def stream(release_at: datetime, *, external_id: str) -> NormalizedEpisode:
    return NormalizedEpisode(
        external_id=external_id,
        number=int(external_id.rsplit("-", 1)[1]),
        title=external_id,
        releases=(
            {
                "release_type": ReleaseType.STREAMING,
                "release_at": release_at,
            },
        ),
    )


def test_epg_does_not_mark_same_day_drop_of_premiered_episode_as_preview() -> None:
    provider = RTLPlusProvider.__new__(RTLPlusProvider)
    event = RTLPlusProvider._epg_event(
        {
            "code": "folge-1",
            "title": "Demo series",
            "subtitle": "Folge 1",
            "diffusion_start_date": "2026-09-30 20:15:00",
            "diffusion_end_date": "2026-09-30 21:25:00",
        }
    )
    seasons = [
        NormalizedSeason(
            external_id="season-1",
            number=1,
            episodes=(stream(datetime(2026, 10, 6, 0, 0, tzinfo=VIENNA), external_id="episode-1"),),
        )
    ]

    # The drop falls a week after the episode's own premiere: it is the regular drop
    # cadence, not an exclusive streaming preview.
    seasons, _ = provider._mark_previews(seasons, event, 1, premieres={1: event.start})
    assert seasons[0].episodes[0].releases[0].preview is False


def test_epg_does_not_mark_cadence_drop_as_preview_of_an_unrelated_episode() -> None:
    provider = RTLPlusProvider.__new__(RTLPlusProvider)
    event = RTLPlusProvider._epg_event(
        {
            "code": "folge-9",
            "title": "Demo series",
            "subtitle": "Folge 9",
            "diffusion_start_date": "2026-10-21 20:15:00",
            "diffusion_end_date": "2026-10-21 21:25:00",
        }
    )
    seasons = [
        NormalizedSeason(
            external_id="season-1",
            number=1,
            episodes=(
                stream(datetime(2026, 10, 21, 0, 0, tzinfo=VIENNA), external_id="episode-6"),
            ),
        )
    ]

    # Episode 6's drop shares the day with the premiere of episode 9, but its own
    # premiere already aired on 10-07: the drop is its regular cadence, not a preview.
    seasons, _ = provider._mark_previews(
        seasons,
        event,
        9,
        premieres={9: event.start, 6: datetime(2026, 10, 7, 20, 15, 0, tzinfo=VIENNA)},
    )
    assert seasons[0].episodes[0].releases[0].preview is False


def test_epg_marks_same_day_drop_as_preview_when_the_episode_has_no_airing() -> None:
    provider = RTLPlusProvider.__new__(RTLPlusProvider)
    event = RTLPlusProvider._epg_event(
        {
            "code": "folge-2",
            "title": "Demo series",
            "subtitle": "Folge 2",
            "diffusion_start_date": "2026-10-07 20:15:00",
            "diffusion_end_date": "2026-10-07 21:25:00",
        }
    )
    seasons = [
        NormalizedSeason(
            external_id="season-1",
            number=1,
            episodes=(stream(datetime(2026, 10, 7, 0, 0, tzinfo=VIENNA), external_id="episode-1"),),
        )
    ]

    # Episode 1 never airs itself; its same-day drop before another episode's premiere is
    # the known same-day preview pattern.
    seasons, _ = provider._mark_previews(seasons, event, 2, premieres={2: event.start})
    assert seasons[0].episodes[0].releases[0].preview is True


def test_normalizes_layout_items_and_schedule() -> None:
    payload = {
        "entity": {"id": "6405", "metadata": {"title": "Demo"}},
        "seo": {"metadata": {"text": "**Folge 1** | Di., 8.9., 20:15 Uhr | Di., 1.9., 0:00 Uhr"}},
        "blocks": [
            {
                "analytics": {"tealium": {"from": "feature.videos_by_season_by_program"}},
                "content": {
                    "title": {"short": "Staffel 2"},
                    "items": [
                        {
                            "itemContent": {
                                "id": "clip_1",
                                "title": "Folge 1",
                                "highlight": "Staffel 2 • Folge 1 • Folge 1",
                                "action": {
                                    "analytics": {"tealium": {"clip_type": "vi"}},
                                },
                            }
                        }
                    ],
                },
            }
        ],
    }
    result = RTLPlusProvider._normalize(RTLPlusProvider.__new__(RTLPlusProvider), "6405", [payload])
    episode = result.seasons[0].episodes[0]
    assert episode.external_id == "clip_1"
    assert episode.releases[0].release_at == datetime(2026, 9, 1, tzinfo=ZoneInfo("Europe/Vienna"))


def test_normalize_adds_public_series_url_to_releases() -> None:
    payload = {
        "entity": {"id": "6838", "metadata": {"title": "Are You The One"}},
        "seo": {"metadata": {"text": "| **Folge 1** | Mi., 23.9. um 0:00 Uhr |"}},
        "blocks": [
            {
                "analytics": {"tealium": {"from": "feature.videos_by_season_by_program"}},
                "content": {
                    "title": {"short": "Staffel 6"},
                    "items": [
                        {
                            "itemContent": {
                                "id": "clip_1",
                                "title": "Folge 1",
                                "highlight": "Staffel 6 • Folge 1",
                            }
                        }
                    ],
                },
            }
        ],
    }

    result = RTLPlusProvider._normalize(
        RTLPlusProvider.__new__(RTLPlusProvider),
        "6838",
        [payload],
        series_url="https://plus.rtl.de/are-you-the-one-reality-stars-in-love-p_6838",
    )

    assert str(result.seasons[0].episodes[0].releases[0].url) == (
        "https://plus.rtl.de/are-you-the-one-reality-stars-in-love-p_6838"
    )


def test_series_url_is_only_created_for_slug_identifiers() -> None:
    assert RTLPlusProvider._series_url("are-you-the-one-reality-stars-in-love-p_6838") == (
        "https://plus.rtl.de/are-you-the-one-reality-stars-in-love-p_6838"
    )
    assert RTLPlusProvider._series_url("6838") is None


async def test_epg_adds_broadcast_date_to_catalog_episode_without_release() -> None:
    endpoint = "https://rtlplus-test.example/guidetv"

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/guidetv"
        assert request.url.params["channel"] == "rtlde_rtl"
        assert request.url.params["from"] == "2026-09-15 02:00:00"
        return httpx.Response(
            200,
            json={
                "rtlde_rtl": [
                    {
                        "code": "demo",
                        "title": "Demo series",
                        "subtitle": "Folge 3 (2026)",
                        "description": "A planned broadcast",
                        "diffusion_start_date": "2026-10-06 20:15:00",
                        "diffusion_end_date": "2026-10-06 21:45:00",
                    }
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = RTLPlusProvider.__new__(RTLPlusProvider)
    provider._epg_endpoint = endpoint
    provider._epg_channels = ("rtlde_rtl",)
    provider._epg_lookback_days = 21
    provider._clock = lambda: datetime(2026, 10, 6, tzinfo=UTC)
    RTLPlusProvider._epg_tasks.clear()
    normalized = NormalizedSeries(
        external_id="demo",
        title="Demo series",
        seasons=(
            NormalizedSeason(
                external_id="season-1",
                number=1,
                episodes=(NormalizedEpisode(external_id="episode-3", number=3, title="Episode 3"),),
            ),
        ),
    )
    try:
        result = await provider._add_epg(client, normalized)
    finally:
        await client.aclose()

    episode = result.seasons[0].episodes[0]
    assert episode.title == "Episode 3"
    assert episode.releases[0].release_type is ReleaseType.TV_BROADCAST
    assert episode.releases[0].release_at == datetime(2026, 10, 6, 18, 15, tzinfo=UTC)
    assert len(result.seasons) == 1


async def test_epg_adds_premiere_and_night_repeat_before_now() -> None:
    endpoint = "https://rtlplus-test.example/guidetv-premiere"

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "rtlde_rtlzwei": [
                    {
                        "code": "folge-5-2026",
                        "title": "Love Island VIP",
                        "subtitle": "Folge 5 (2026)",
                        "diffusion_start_date": "2026-10-07 20:15:00",
                        "diffusion_end_date": "2026-10-07 21:25:00",
                    },
                    {
                        "code": "folge-5-2026",
                        "title": "Love Island VIP",
                        "subtitle": "Folge 5 (2026)",
                        "diffusion_start_date": "2026-10-13 00:15:00",
                        "diffusion_end_date": "2026-10-13 01:20:00",
                    },
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = RTLPlusProvider.__new__(RTLPlusProvider)
    provider._epg_endpoint = endpoint
    provider._epg_channels = ("rtlde_rtlzwei",)
    provider._epg_lookback_days = 21
    provider._clock = lambda: datetime(2026, 10, 8, tzinfo=UTC)
    RTLPlusProvider._epg_tasks.clear()
    normalized = NormalizedSeries(
        external_id="demo",
        title="Love Island VIP",
        seasons=(
            NormalizedSeason(
                external_id="season-1",
                number=1,
                episodes=(NormalizedEpisode(external_id="episode-5", number=5, title="Folge 5"),),
            ),
        ),
    )
    try:
        result = await provider._add_epg(client, normalized)
    finally:
        await client.aclose()

    releases = result.seasons[0].episodes[0].releases
    assert [release.release_at for release in releases] == [
        datetime(2026, 10, 7, 18, 15, tzinfo=UTC),
        datetime(2026, 10, 12, 22, 15, tzinfo=UTC),
    ]


async def test_epg_marks_earlier_catalog_release_as_preview() -> None:
    endpoint = "https://rtlplus-test.example/guidetv-preview"

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "rtlde_rtl": [
                    {
                        "code": "demo-preview",
                        "title": "Demo series",
                        "subtitle": "Folge 6 (2026)",
                        "diffusion_start_date": "2026-10-06 20:15:00",
                        "diffusion_end_date": "2026-10-06 21:45:00",
                    }
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = RTLPlusProvider.__new__(RTLPlusProvider)
    provider._epg_endpoint = endpoint
    provider._epg_channels = ("rtlde_rtl",)
    provider._epg_lookback_days = 21
    provider._clock = lambda: datetime(2026, 10, 6, tzinfo=UTC)
    RTLPlusProvider._epg_tasks.clear()
    normalized = NormalizedSeries(
        external_id="demo",
        title="Demo series",
        seasons=(
            NormalizedSeason(
                external_id="season-1",
                number=1,
                episodes=(
                    NormalizedEpisode(
                        external_id="episode-3",
                        number=3,
                        title="Episode 3",
                        releases=(
                            {
                                "release_type": ReleaseType.STREAMING,
                                "release_at": datetime(
                                    2026, 10, 6, tzinfo=ZoneInfo("Europe/Vienna")
                                ),
                            },
                        ),
                    ),
                ),
            ),
        ),
    )
    try:
        result = await provider._add_epg(client, normalized)
    finally:
        await client.aclose()

    # The same-day catalogue drop is flagged as a preview; the readable number (6) is
    # not in the catalogue, so the airing becomes a new dated episode of the running
    # season (dateFromApi=true) instead of being attached to episode 3.
    episodes = {episode.number: episode for episode in result.seasons[0].episodes}
    assert set(episodes) == {3, 6}
    assert episodes[3].releases[0].preview is True
    assert episodes[6].releases[0].release_type is ReleaseType.TV_BROADCAST
    assert episodes[6].releases[0].date_from_api is True


async def test_epg_marks_catalog_release_days_before_broadcast_as_preview() -> None:
    endpoint = "https://rtlplus-test.example/guidetv-preview-days-apart"

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "rtlde_rtl": [
                    {
                        "code": "demo-preview-days-apart",
                        "title": "Demo series",
                        "subtitle": "Folge 7",
                        "diffusion_start_date": "2026-10-13 20:15:00",
                        "diffusion_end_date": "2026-10-13 22:30:00",
                    }
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = RTLPlusProvider.__new__(RTLPlusProvider)
    provider._epg_endpoint = endpoint
    provider._epg_channels = ("rtlde_rtl",)
    provider._epg_lookback_days = 21
    provider._clock = lambda: datetime(2026, 10, 6, tzinfo=UTC)
    RTLPlusProvider._epg_tasks.clear()
    normalized = NormalizedSeries(
        external_id="demo",
        title="Demo series",
        seasons=(
            NormalizedSeason(
                external_id="season-11",
                number=11,
                episodes=(
                    NormalizedEpisode(
                        external_id="episode-7",
                        number=7,
                        title="Episode 7",
                        releases=(
                            {
                                "release_type": ReleaseType.STREAMING,
                                "release_at": datetime(
                                    2026, 10, 6, tzinfo=ZoneInfo("Europe/Vienna")
                                ),
                            },
                        ),
                    ),
                ),
            ),
        ),
    )
    try:
        result = await provider._add_epg(client, normalized)
    finally:
        await client.aclose()

    releases = result.seasons[0].episodes[0].releases
    # The broadcast resolves to a catalogue-dated episode and is dropped; only the
    # preview flag on the catalogue release survives.
    assert len(releases) == 1
    assert releases[0].preview is True


async def test_epg_drops_numbered_broadcast_already_dated_in_catalog() -> None:
    endpoint = "https://rtlplus-test.example/guidetv-number-priority"

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "rtlde_rtl": [
                    {
                        "code": "demo-7",
                        "title": "Demo series",
                        "subtitle": "Folge 7",
                        "diffusion_start_date": "2026-10-13 20:15:00",
                        "diffusion_end_date": "2026-10-13 22:30:00",
                    }
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = RTLPlusProvider.__new__(RTLPlusProvider)
    provider._epg_endpoint = endpoint
    provider._epg_channels = ("rtlde_rtl",)
    provider._epg_lookback_days = 21
    provider._clock = lambda: datetime(2026, 10, 6, tzinfo=UTC)
    RTLPlusProvider._epg_tasks.clear()
    normalized = NormalizedSeries(
        external_id="demo",
        title="Demo series",
        seasons=(
            NormalizedSeason(
                external_id="season-1",
                number=1,
                episodes=(
                    NormalizedEpisode(
                        external_id="episode-7",
                        number=7,
                        title="Episode 7",
                        releases=(
                            {
                                "release_type": ReleaseType.STREAMING,
                                "release_at": datetime(
                                    2026, 10, 6, tzinfo=ZoneInfo("Europe/Vienna")
                                ),
                            },
                        ),
                    ),
                    NormalizedEpisode(
                        external_id="episode-8",
                        number=8,
                        title="Episode 8",
                        releases=(
                            {
                                "release_type": ReleaseType.STREAMING,
                                "release_at": datetime(
                                    2026, 10, 13, tzinfo=ZoneInfo("Europe/Vienna")
                                ),
                            },
                        ),
                    ),
                ),
            ),
        ),
    )
    try:
        result = await provider._add_epg(client, normalized)
    finally:
        await client.aclose()

    episodes = {episode.number: episode for episode in result.seasons[0].episodes}
    # Both catalogue releases are previews (same-day/days-ahead), and the numbered
    # airing resolves to the already-dated catalogue episode 7, so it is dropped.
    assert episodes[7].releases[0].preview is True
    assert not any(
        release.release_type is ReleaseType.TV_BROADCAST for release in episodes[7].releases
    )
    assert episodes[8].releases[0].preview is True


async def test_epg_does_not_mark_already_aired_catalog_release_as_preview() -> None:
    endpoint = "https://rtlplus-test.example/guidetv-no-preview-after-premiere"

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "rtlde_rtlzwei": [
                    {
                        "code": "folge-5-2026",
                        "title": "Love Island VIP",
                        "subtitle": "Folge 5 (2026)",
                        "diffusion_start_date": "2026-10-07 20:15:00",
                        "diffusion_end_date": "2026-10-07 21:25:00",
                    }
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = RTLPlusProvider.__new__(RTLPlusProvider)
    provider._epg_endpoint = endpoint
    provider._epg_channels = ("rtlde_rtlzwei",)
    provider._epg_lookback_days = 21
    provider._clock = lambda: datetime(2026, 10, 8, tzinfo=UTC)
    RTLPlusProvider._epg_tasks.clear()
    normalized = NormalizedSeries(
        external_id="demo",
        title="Love Island VIP",
        seasons=(
            NormalizedSeason(
                external_id="season-1",
                number=1,
                episodes=(
                    NormalizedEpisode(
                        external_id="episode-4",
                        number=4,
                        title="Folge 4",
                        releases=(
                            {
                                "release_type": ReleaseType.STREAMING,
                                "release_at": datetime(
                                    2026, 10, 7, 0, 0, tzinfo=ZoneInfo("Europe/Vienna")
                                ),
                            },
                            {
                                "release_type": ReleaseType.TV_BROADCAST,
                                "release_at": datetime(2026, 9, 30, 18, 15, tzinfo=UTC),
                            },
                        ),
                    ),
                ),
            ),
        ),
    )
    try:
        result = await provider._add_epg(client, normalized)
    finally:
        await client.aclose()

    episode = result.seasons[0].episodes[0]
    streaming = next(
        release for release in episode.releases if release.release_type is ReleaseType.STREAMING
    )
    assert streaming.preview is False


async def test_epg_does_not_mark_aired_season_start_as_preview() -> None:
    # A new season that started last week published its first episodes on demand before
    # the linear premiere; once that premiere has aired, the drops are the regular
    # releases and must not stay flagged as previews.
    endpoint = "https://rtlplus-test.example/guidetv-season-start"

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "rtlde_rtl": [
                    {
                        "code": "folge-1-2026",
                        "title": "#CoupleChallenge",
                        "subtitle": "Folge 1 • Folge 1",
                        "diffusion_start_date": "2026-10-22 20:15:00",
                        "diffusion_end_date": "2026-10-22 21:25:00",
                    },
                    {
                        "code": "folge-2-2026",
                        "title": "#CoupleChallenge",
                        "subtitle": "Folge 2 • Folge 2",
                        "diffusion_start_date": "2026-10-23 20:15:00",
                        "diffusion_end_date": "2026-10-23 21:25:00",
                    },
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = RTLPlusProvider.__new__(RTLPlusProvider)
    provider._epg_endpoint = endpoint
    provider._epg_channels = ("rtlde_rtl",)
    provider._epg_lookback_days = 21
    provider._clock = lambda: datetime(2026, 10, 25, tzinfo=UTC)
    RTLPlusProvider._epg_tasks.clear()
    normalized = NormalizedSeries(
        external_id="11271",
        title="#CoupleChallenge",
        seasons=(
            NormalizedSeason(
                external_id="11271:season:6",
                number=6,
                episodes=(
                    NormalizedEpisode(
                        external_id="11271:season:6:episode:1",
                        number=1,
                        title="Folge 1",
                        releases=(
                            {
                                "release_type": ReleaseType.STREAMING,
                                "release_at": datetime(
                                    2026, 10, 22, 0, 0, tzinfo=ZoneInfo("Europe/Vienna")
                                ),
                            },
                        ),
                    ),
                    NormalizedEpisode(
                        external_id="11271:season:6:episode:2",
                        number=2,
                        title="Folge 2",
                        releases=(
                            {
                                "release_type": ReleaseType.STREAMING,
                                "release_at": datetime(
                                    2026, 10, 23, 0, 0, tzinfo=ZoneInfo("Europe/Vienna")
                                ),
                            },
                        ),
                    ),
                ),
            ),
        ),
    )
    try:
        result = await provider._add_epg(client, normalized)
    finally:
        await client.aclose()

    episodes = list(result.seasons[0].episodes)
    assert [episode.number for episode in episodes] == [1, 2]
    for episode in episodes:
        streaming = next(
            release for release in episode.releases if release.release_type is ReleaseType.STREAMING
        )
        assert streaming.preview is False
        assert not any(
            release.release_type is ReleaseType.TV_BROADCAST for release in episode.releases
        )


async def test_epg_folds_numberless_future_airing_into_running_season() -> None:
    endpoint = "https://rtlplus-test.example/guidetv-numberless"

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "rtlde_rtl": [
                    {
                        "code": "future-demo",
                        "title": "Demo series",
                        "subtitle": "Die nächste Folge",
                        "description": "Coming soon",
                        "diffusion_start_date": "2026-10-07 20:15:00",
                        "diffusion_end_date": "2026-10-07 21:15:00",
                    },
                    {
                        "code": "long-demo",
                        "title": "Demo series",
                        "subtitle": "Live",
                        "diffusion_start_date": "2026-10-08 00:00:00",
                        "diffusion_end_date": "2026-10-08 05:00:00",
                    },
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = RTLPlusProvider.__new__(RTLPlusProvider)
    provider._epg_endpoint = endpoint
    provider._epg_channels = ("rtlde_rtl",)
    provider._epg_lookback_days = 21
    provider._clock = lambda: datetime(2026, 10, 6, tzinfo=UTC)
    RTLPlusProvider._epg_tasks.clear()
    normalized = NormalizedSeries(
        external_id="demo",
        title="Demo series",
        seasons=(
            NormalizedSeason(
                external_id="season-1",
                number=1,
                episodes=(
                    NormalizedEpisode(
                        external_id="episode-1",
                        number=1,
                        title="Episode 1",
                        releases=(
                            {
                                "release_type": ReleaseType.STREAMING,
                                "release_at": datetime(
                                    2026, 9, 30, 20, 15, tzinfo=ZoneInfo("Europe/Vienna")
                                ),
                            },
                        ),
                    ),
                ),
            ),
        ),
    )
    try:
        result = await provider._add_epg(client, normalized)
    finally:
        await client.aclose()

    assert len(result.seasons) == 1
    episodes = result.seasons[0].episodes
    assert [episode.number for episode in episodes] == [1, 2]
    added = episodes[-1].releases[0]
    assert added.release_type is ReleaseType.TV_BROADCAST
    assert added.release_at == datetime(2026, 10, 7, 18, 15, tzinfo=UTC)
    assert added.rerun is False


async def test_epg_drops_reruns_of_catalog_dated_episode() -> None:
    endpoint = "https://rtlplus-test.example/guidetv-night-repeat"

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "rtlde_rtl": [
                    {
                        "code": "premiere-1",
                        "title": "Demo series",
                        "subtitle": "Folge 1",
                        "diffusion_start_date": "2026-10-07 20:15:00",
                        "diffusion_end_date": "2026-10-07 21:15:00",
                    },
                    {
                        "code": "repeat-1",
                        "title": "Demo series",
                        "subtitle": "Folge 1",
                        "diffusion_start_date": "2026-10-08 00:15:00",
                        "diffusion_end_date": "2026-10-08 01:10:00",
                    },
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = RTLPlusProvider.__new__(RTLPlusProvider)
    provider._epg_endpoint = endpoint
    provider._epg_channels = ("rtlde_rtl",)
    provider._epg_lookback_days = 21
    provider._clock = lambda: datetime(2026, 10, 6, tzinfo=UTC)
    RTLPlusProvider._epg_tasks.clear()
    normalized = NormalizedSeries(
        external_id="demo",
        title="Demo series",
        seasons=(
            NormalizedSeason(
                external_id="season-1",
                number=1,
                episodes=(
                    NormalizedEpisode(
                        external_id="episode-1",
                        number=1,
                        title="Folge 1",
                        releases=(
                            {
                                "release_type": ReleaseType.STREAMING,
                                "release_at": datetime(
                                    2026, 9, 30, 20, 15, tzinfo=ZoneInfo("Europe/Vienna")
                                ),
                            },
                        ),
                    ),
                ),
            ),
        ),
    )
    try:
        result = await provider._add_epg(client, normalized)
    finally:
        await client.aclose()

    episode = result.seasons[0].episodes[0]
    # Premiere and repeat both carry the readable number 1, which resolves to the
    # catalogue-dated episode; the catalogue date wins and both airings are dropped.
    broadcasts = [
        release for release in episode.releases if release.release_type is ReleaseType.TV_BROADCAST
    ]
    assert broadcasts == []
    assert [release.release_type for release in episode.releases] == [ReleaseType.STREAMING]


def test_epg_event_uses_id_when_code_is_missing() -> None:
    event = RTLPlusProvider._epg_event(
        {
            "id": 560809,
            "title": "Der Blaulicht Report",
            "subtitle": "Frau will vorbestraften Sohn vor Knast bewahren",
            "diffusion_start_date": "2026-10-06 05:20:00",
            "diffusion_end_date": "2026-10-06 06:00:00",
        }
    )

    assert event.external_id == "epg:560809:1791256800"


def test_schedule_supports_current_weekly_rtl_format() -> None:
    schedule = RTLPlusProvider._schedule({"metadata": {"text": "Mittwochs, ab 12. August"}})

    assert schedule[6] == datetime(2026, 9, 16, tzinfo=ZoneInfo("Europe/Vienna"))
    assert schedule[7] == datetime(2026, 9, 23, tzinfo=ZoneInfo("Europe/Vienna"))


def test_schedule_reads_date_and_cadence_from_separate_metadata_fields() -> None:
    schedule = RTLPlusProvider._schedule(
        {
            "metadata": {
                "title": "Are You The One - Realitystars in Love, ab 12. August auf RTL+",
                "text": "Erstausstrahlung 2021\nMittwochs",
            }
        }
    )

    assert schedule[7] == datetime(2026, 9, 23, tzinfo=ZoneInfo("Europe/Vienna"))


def test_schedule_uses_start_date_weekday_when_block_has_no_cadence() -> None:
    schedule = RTLPlusProvider._schedule(
        {
            "metadata": {
                "title": "Love Island VIP 2026",
                "description": "Die neue Staffel startet ab dem 16. September.",
            }
        }
    )

    assert schedule[1] == datetime(2026, 9, 16, tzinfo=ZoneInfo("Europe/Vienna"))
    assert schedule[2] == datetime(2026, 9, 23, tzinfo=ZoneInfo("Europe/Vienna"))


def test_schedule_uses_streaming_dates_from_rtl_schedule_table() -> None:
    schedule = RTLPlusProvider._schedule(
        {
            "metadata": {
                "text": (
                    "Folge | RTL | RTL+ / Streaming\n"
                    "Folge 1 | Mo., 17.8\\. um 20:15 Uhr | Mo., 3.8\\. ab 00:00 Uhr\n"
                    "Folge 2 | Di., 18.8\\. um 20:15 Uhr | Mi., 5.8\\. ab 0:00 Uhr"
                )
            }
        }
    )

    assert schedule[1] == datetime(2026, 8, 3, tzinfo=ZoneInfo("Europe/Vienna"))
    assert schedule[2] == datetime(2026, 8, 5, tzinfo=ZoneInfo("Europe/Vienna"))


def test_normalize_adds_next_planned_episode_from_schedule_table() -> None:
    result = RTLPlusProvider._normalize(
        RTLPlusProvider.__new__(RTLPlusProvider),
        "6405",
        [
            {
                "entity": {"id": "6405", "metadata": {"title": "Demo"}},
                "seo": {
                    "metadata": {
                        "title": "Demo 2099, ab 25. August auf RTL+",
                        "text": (
                            "| **Folge 4** | Di., 15.9. um 0:00 Uhr |\n"
                            "| **Folge 5** | Di., 22.9. um 0:00 Uhr |"
                        ),
                    }
                },
                "blocks": [
                    {
                        "analytics": {"tealium": {"from": "feature.videos_by_season_by_program"}},
                        "content": {
                            "title": {"short": "Dienstags"},
                            "items": [
                                {
                                    "itemContent": {
                                        "id": "clip-4",
                                        "title": "Folge 4",
                                        "highlight": "Staffel 11 • Folge 4",
                                    }
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    )

    assert result.seasons[0].episodes[-1].number == 5


def test_normalize_uses_diffusion_date_for_first_episode_without_schedule() -> None:
    result = RTLPlusProvider._normalize(
        RTLPlusProvider.__new__(RTLPlusProvider),
        "254773",
        [
            {
                "entity": {"id": "254773", "metadata": {"title": "Yeliz & Jimi"}},
                "seo": {"diffusionDate": 1790028000, "metadata": {"title": "Yeliz & Jimi"}},
                "blocks": [
                    {
                        "analytics": {"tealium": {"from": "feature.videos_by_season_by_program"}},
                        "content": {
                            "items": [
                                {
                                    "itemContent": {
                                        "id": "clip-1",
                                        "title": "New Year, Same Problems",
                                        "highlight": (
                                            "Staffel 2 • Folge 1 • New Year, Same Problems"
                                        ),
                                    }
                                }
                            ]
                        },
                    }
                ],
            }
        ],
    )

    episode = result.seasons[0].episodes[0]
    assert episode.number == 1
    assert episode.releases[0].release_at == datetime(2026, 9, 22, tzinfo=ZoneInfo("Europe/Vienna"))


def test_normalize_assigns_diffusion_date_to_all_undated_catalog_episodes() -> None:
    # A completed season no longer carries a schedule table or weekday block; the page-level
    # diffusion date identifies the last published episode. Applying it to every episode of
    # that season without a date is wrong for most of them, but keeps a finished season out
    # of today's calendar.
    payload = {
        "entity": {"id": "254773", "metadata": {"title": "Yeliz & Jimi"}},
        "seo": {"diffusionDate": 1791237600, "metadata": {"title": "Yeliz & Jimi"}},
        "blocks": [
            {
                "analytics": {"tealium": {"from": "feature.videos_by_season_by_program"}},
                "content": {
                    "title": {"short": "Staffel 2"},
                    "items": [
                        {
                            "itemContent": {
                                "id": f"clip-{episode_number}",
                                "title": f"Folge {episode_number}",
                                "highlight": f"Staffel 2 • Folge {episode_number}",
                            }
                        }
                        for episode_number in (1, 2, 3)
                    ],
                },
            }
        ],
    }

    result = RTLPlusProvider._normalize(
        RTLPlusProvider.__new__(RTLPlusProvider), "254773", [payload]
    )

    episodes = {episode.number: episode for episode in result.seasons[0].episodes}
    expected = datetime(2026, 10, 6, tzinfo=ZoneInfo("Europe/Vienna"))
    for number in (1, 2, 3):
        assert episodes[number].releases[0].release_at == expected
        assert episodes[number].releases[0].date_from_api is True


def test_normalize_anchors_weekly_schedule_to_latest_feed_episode() -> None:
    result = RTLPlusProvider._normalize(
        RTLPlusProvider.__new__(RTLPlusProvider),
        "6838",
        [
            {
                "entity": {"id": "6838", "metadata": {"title": "Demo"}},
                "seo": {
                    "diffusionDate": 1790114400,
                    "metadata": {
                        "title": "Demo Staffel 6, ab 12. August auf RTL+ streamen",
                    },
                },
                "blocks": [
                    {
                        "analytics": {"tealium": {"from": "feature.videos_by_season_by_program"}},
                        "content": {
                            "title": {"short": "Mittwochs"},
                            "items": [
                                {
                                    "itemContent": {
                                        "id": f"clip-{episode_number}",
                                        "title": f"Folge {episode_number}",
                                        "highlight": (
                                            f"Staffel 6 • Folge {episode_number} • Folge "
                                            f"{episode_number}"
                                        ),
                                    }
                                }
                                for episode_number in (14, 13, 12, 7)
                            ],
                        },
                    }
                ],
            }
        ],
    )

    episodes = {episode.number: episode for episode in result.seasons[0].episodes}
    assert episodes[12].releases[0].release_at == datetime(
        2026, 9, 9, tzinfo=ZoneInfo("Europe/Vienna")
    )
    assert episodes[13].releases[0].release_at == datetime(
        2026, 9, 16, tzinfo=ZoneInfo("Europe/Vienna")
    )
    assert episodes[14].releases[0].release_at == datetime(
        2026, 9, 23, tzinfo=ZoneInfo("Europe/Vienna")
    )


def test_normalize_uses_weekday_block_order_not_highest_episode_number() -> None:
    payload = {
        "entity": {"id": "6838", "metadata": {"title": "Demo"}},
        "seo": {
            "diffusionDate": 1790114400,
            "metadata": {"title": "Demo Staffel 6, ab 12. August auf RTL+ streamen"},
        },
        "blocks": [
            {
                "analytics": {"tealium": {"from": "feature.videos_by_season_by_program"}},
                "content": {
                    "title": {"short": "Mittwochs"},
                    "items": [
                        {
                            "itemContent": {
                                "id": f"clip-{episode_number}",
                                "title": f"Folge {episode_number}",
                                "highlight": f"Staffel 6 • Folge {episode_number}",
                            }
                        }
                        for episode_number in (12, 14)
                    ],
                },
            }
        ],
    }

    result = RTLPlusProvider._normalize(RTLPlusProvider.__new__(RTLPlusProvider), "6838", [payload])

    episodes = {episode.number: episode for episode in result.seasons[0].episodes}
    assert episodes[12].releases[0].release_at == datetime(
        2026, 9, 23, tzinfo=ZoneInfo("Europe/Vienna")
    )
    assert episodes[14].releases[0].release_at == datetime(
        2026, 10, 7, tzinfo=ZoneInfo("Europe/Vienna")
    )


def test_normalize_does_not_assume_premium_previews_for_other_programmes() -> None:
    result = RTLPlusProvider._normalize(
        RTLPlusProvider.__new__(RTLPlusProvider),
        "11169",
        [
            {
                "entity": {"id": "11169", "metadata": {"title": "Ex on the Beach"}},
                "seo": {
                    "diffusionDate": 1790287200,
                    "metadata": {"title": "Ex on the Beach Staffel 7 ab 29. Mai 2026"},
                },
                "blocks": [
                    {
                        "analytics": {"tealium": {"from": "feature.videos_by_season_by_program"}},
                        "content": {
                            "title": {"short": "Freitags"},
                            "items": [
                                {
                                    "itemContent": {
                                        "id": "clip-18",
                                        "title": "Folge 18",
                                        "highlight": "Staffel 7 • Folge 18 • Folge 18",
                                    }
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    )

    episode = result.seasons[0].episodes[0]
    assert episode.number == 18
    assert episode.releases[0].release_at == datetime(2026, 9, 25, tzinfo=ZoneInfo("Europe/Vienna"))


def test_schedule_does_not_invent_dates_for_cadence_without_start_date() -> None:
    assert RTLPlusProvider._schedule({"metadata": {"text": "Montags"}}) == {}


def test_schedule_is_only_applied_to_current_season() -> None:
    blocks = []
    for season in (10, 11):
        blocks.append(
            {
                "analytics": {"tealium": {"from": "feature.videos_by_season_by_program"}},
                "content": {
                    "title": {"short": f"Staffel {season}"},
                    "items": [
                        {
                            "itemContent": {
                                "id": f"clip-{season}",
                                "title": "Folge 1",
                                "highlight": f"Staffel {season} • Folge 1",
                            }
                        }
                    ],
                },
            }
        )
    result = RTLPlusProvider._normalize(
        RTLPlusProvider.__new__(RTLPlusProvider),
        "6405",
        [
            {
                "entity": {"id": "6405", "metadata": {"title": "Demo"}},
                "seo": {"metadata": {"text": "Mittwochs, ab 12. August"}},
                "blocks": blocks,
            }
        ],
    )

    off_season_releases = result.seasons[0].episodes[0].releases
    assert len(off_season_releases) == 1
    assert off_season_releases[0].date_from_api is False
    current_releases = result.seasons[1].episodes[0].releases
    assert len(current_releases) == 1
    assert current_releases[0].date_from_api is True


def test_current_season_can_be_read_from_episode_highlight() -> None:
    payload = {
        "entity": {"id": "6838", "metadata": {"title": "Demo"}},
        "seo": {"metadata": {"title": "Demo, ab 12. August"}},
        "blocks": [
            {
                "analytics": {"tealium": {"from": "feature.videos_by_season_by_program"}},
                "content": {
                    "title": {"short": "Mittwochs"},
                    "items": [
                        {
                            "itemContent": {
                                "id": "clip-7",
                                "title": "Folge 7",
                                "highlight": "Staffel 6 • Folge 7 • Folge 7",
                            }
                        }
                    ],
                },
            }
        ],
    }

    result = RTLPlusProvider._normalize(RTLPlusProvider.__new__(RTLPlusProvider), "6838", [payload])

    assert len(result.seasons[0].episodes[0].releases) == 1


def test_normalize_ignores_diffusion_date_for_previous_season_during_transition() -> None:
    # During a season transition RTL+ keeps the completed season's block on the page
    # while the page-level diffusion date already points at the upcoming season, which
    # is announced in the weekday block before it gets a season block of its own.
    diffusion = int(datetime(2026, 10, 5, 12, 0, tzinfo=ZoneInfo("Europe/Vienna")).timestamp())
    payload = {
        "entity": {"id": "11271", "metadata": {"title": "#CoupleChallenge"}},
        "seo": {
            "diffusionDate": diffusion,
            "metadata": {"title": "CoupleChallenge Staffel 5"},
        },
        "blocks": [
            {
                "analytics": {"tealium": {"from": "feature.videos_by_season_by_program"}},
                "content": {
                    "title": {"short": "Staffel 5"},
                    "items": [
                        {
                            "itemContent": {
                                "id": f"clip-{episode_number}",
                                "title": f"Folge {episode_number}",
                                "highlight": f"Staffel 5 • Folge {episode_number}",
                            }
                        }
                        for episode_number in range(9, 21)
                    ],
                },
            },
            {
                "analytics": {"tealium": {"from": "feature.programmation_by_program"}},
                "content": {
                    "title": {"short": "Freitags"},
                    "items": [
                        {
                            "itemContent": {
                                "id": "clip-new",
                                "title": "#CoupleChallenge",
                                "highlight": "Staffel 6 • Folge 1 • Folge 1",
                            }
                        }
                    ],
                },
            },
        ],
    }

    result = RTLPlusProvider._normalize(
        RTLPlusProvider.__new__(RTLPlusProvider), "11271", [payload]
    )

    season_five = next(season for season in result.seasons if season.number == 5)
    assert [episode.number for episode in season_five.episodes] == list(range(9, 21))
    assert all(
        len(episode.releases) == 1
        and episode.releases[0].date_from_api is False
        and episode.releases[0].placeholder is True
        for episode in season_five.episodes
    )


def test_normalize_derives_completed_season_dates_from_premiere_anchor() -> None:
    payload = {
        "entity": {"id": "4267", "metadata": {"title": "Temptation Island VIP"}},
        "seo": {"metadata": {"title": "Temptation Island VIP"}},
        "blocks": [
            {
                "analytics": {"tealium": {"from": "feature.videos_by_season_by_program"}},
                "content": {
                    "title": {"short": "Staffel 7 ab 12. Oktober"},
                    "items": [
                        {
                            "itemContent": {
                                "id": "clip-7",
                                "title": "Folge 1",
                                "highlight": "Staffel 7 • Folge 1 • Folge 1",
                            }
                        }
                    ],
                },
            },
            {
                "analytics": {"tealium": {"from": "feature.videos_by_season_by_program"}},
                "content": {
                    "title": {"short": "Staffel 6"},
                    "items": [
                        {
                            "itemContent": {
                                "id": f"clip-{episode_number}",
                                "title": f"Folge {episode_number}",
                                "highlight": (
                                    f"Staffel 6 • Folge {episode_number} • Folge {episode_number}"
                                ),
                            }
                        }
                        for episode_number in (4, 15)
                    ],
                },
            },
        ],
    }

    result = RTLPlusProvider._normalize(
        RTLPlusProvider.__new__(RTLPlusProvider),
        "4267",
        [payload],
        series_url="https://plus.rtl.de/temptation-island-vip-p_4267",
        season_anchors={6: datetime(2025, 9, 29, tzinfo=ZoneInfo("Europe/Vienna"))},
    )

    season_six = next(season for season in result.seasons if season.number == 6)
    episodes = {episode.number: episode for episode in season_six.episodes}
    assert episodes[4].releases[0].release_at == datetime(
        2025, 10, 27, tzinfo=ZoneInfo("Europe/Vienna")
    )
    assert episodes[15].releases[0].release_at == datetime(
        2026, 1, 12, tzinfo=ZoneInfo("Europe/Vienna")
    )
    assert episodes[4].releases[0].date_from_api is False
    assert episodes[15].releases[0].date_from_api is False
    assert episodes[4].releases[0].placeholder is False
    assert episodes[15].releases[0].placeholder is False


def test_episode_public_url_unwraps_locked_target() -> None:
    raw = {
        "action": {
            "target": {
                "type": "lock",
                "value_lock": {
                    "originalTarget": {
                        "type": "layout",
                        "value_layout": {
                            "id": "clip_2131490",
                            "seo": "vorab-der-auftakt-von-staffel-6",
                        },
                    }
                },
            }
        }
    }

    assert RTLPlusProvider._episode_public_url(
        raw, "https://plus.rtl.de/temptation-island-vip-p_4267"
    ) == (
        "https://plus.rtl.de/temptation-island-vip-p_4267/video/"
        "vorab-der-auftakt-von-staffel-6-c_2131490"
    )


async def test_season_anchors_read_upload_date_from_public_preview_page() -> None:
    pages = [
        {
            "entity": {"id": "4267", "metadata": {"title": "Temptation Island VIP"}},
            "blocks": [
                {
                    "analytics": {"tealium": {"from": "feature.videos_by_season_by_program"}},
                    "content": {
                        "title": {"short": "Sneak Peeks"},
                        "items": [
                            {
                                "itemContent": {
                                    "id": "hash-6",
                                    "highlight": (
                                        "Sneak Peeks • Folge 6 • Der Auftakt von Staffel 6"
                                    ),
                                    "action": {
                                        "target": {
                                            "type": "layout",
                                            "value_layout": {
                                                "id": "clip_2131490",
                                                "seo": "vorab-der-auftakt-von-staffel-6",
                                            },
                                        }
                                    },
                                }
                            }
                        ],
                    },
                }
            ],
        }
    ]
    requested: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(
            200,
            text=(
                '<script type="application/ld+json">'
                '{"@type": "TVEpisode","uploadDate": "2025-09-29T00:00:00+02:00"}'
                "</script>"
            ),
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = RTLPlusProvider.__new__(RTLPlusProvider)
    try:
        anchors = await provider._season_anchors(
            client, pages, "https://plus.rtl.de/temptation-island-vip-p_4267"
        )
    finally:
        await client.aclose()

    assert requested == [
        "https://plus.rtl.de/temptation-island-vip-p_4267/video/"
        "vorab-der-auftakt-von-staffel-6-c_2131490"
    ]
    assert anchors == {6: datetime(2025, 9, 29, tzinfo=ZoneInfo("Europe/Vienna"))}


def test_normalize_sets_episode_duration_from_mapping() -> None:
    payload = {
        "entity": {"id": "4267", "metadata": {"title": "Temptation Island VIP"}},
        "seo": {"metadata": {"title": "Temptation Island VIP"}},
        "blocks": [
            {
                "analytics": {"tealium": {"from": "feature.videos_by_season_by_program"}},
                "content": {
                    "title": {"short": "Staffel 7"},
                    "items": [
                        {
                            "itemContent": {
                                "id": "clip-1",
                                "title": "Folge 1",
                                "highlight": "Staffel 7 • Folge 1 • Folge 1",
                                "action": {"analytics": {"tealium": {"clip_type": "vi"}}},
                            }
                        }
                    ],
                },
            }
        ],
    }

    result = RTLPlusProvider._normalize(
        RTLPlusProvider.__new__(RTLPlusProvider),
        "4267",
        [payload],
        durations={"clip-1": 52},
    )

    episode = result.seasons[0].episodes[0]
    assert episode.external_id == "clip-1"
    assert episode.duration_minutes == 52


async def test_episode_page_metadata_reads_duration_and_upload_date() -> None:
    pages = [
        {
            "entity": {"id": "4267", "metadata": {"title": "Temptation Island VIP"}},
            "blocks": [
                {
                    "analytics": {"tealium": {"from": "feature.videos_by_season_by_program"}},
                    "content": {
                        "title": {"short": "Staffel 7"},
                        "items": [
                            {
                                "itemContent": {
                                    "id": "clip-1",
                                    "title": "Folge 1",
                                    "highlight": "Staffel 7 • Folge 1 • Folge 1",
                                    "action": {
                                        "analytics": {"tealium": {"clip_type": "vi"}},
                                        "target": {
                                            "type": "layout",
                                            "value_layout": {
                                                "id": "clip_2150934",
                                                "seo": "folge-1",
                                            },
                                        },
                                    },
                                }
                            }
                        ],
                    },
                }
            ],
        }
    ]
    requested: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(
            200,
            text=(
                '<script type="application/ld+json">'
                '{"@type": "TVEpisode","duration": "PT51M56S",'
                ' "uploadDate": "2025-09-29T00:00:00+02:00"}'
                "</script>"
            ),
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = RTLPlusProvider.__new__(RTLPlusProvider)
    try:
        durations, upload_dates = await provider._episode_page_metadata(
            client, pages, "https://plus.rtl.de/temptation-island-vip-p_4267"
        )
    finally:
        await client.aclose()

    assert requested == ["https://plus.rtl.de/temptation-island-vip-p_4267/video/folge-1-c_2150934"]
    assert durations == {"clip-1": 52}
    assert upload_dates == {"clip-1": datetime(2025, 9, 29, tzinfo=ZoneInfo("Europe/Vienna"))}


async def test_episode_page_metadata_skips_unavailable_and_unrelated_pages() -> None:
    pages = [
        {
            "entity": {"id": "4267", "metadata": {"title": "Temptation Island VIP"}},
            "blocks": [
                {
                    "analytics": {"tealium": {"from": "feature.videos_by_season_by_program"}},
                    "content": {
                        "title": {"short": "Staffel 7"},
                        "items": [
                            {
                                "itemContent": {
                                    "id": f"clip-{number}",
                                    "title": f"Folge {number}",
                                    "highlight": f"Staffel 7 • Folge {number} • Folge {number}",
                                    "action": {
                                        "analytics": {"tealium": {"clip_type": "vi"}},
                                        "target": {
                                            "type": "layout",
                                            "value_layout": {
                                                "id": f"clip_{number}",
                                                "seo": f"folge-{number}",
                                            },
                                        },
                                    },
                                }
                            }
                            for number in (1, 2, 3)
                        ],
                    },
                }
            ],
        }
    ]

    async def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("folge-2-c_2"):
            return httpx.Response(503, text="temporarily unavailable")
        if "/video/folge-3-" in url:
            return httpx.Response(200, text="<html><body>no structured data</body></html>")
        return httpx.Response(
            200,
            text=(
                '<script type="application/ld+json">'
                '{"@type": "TVEpisode","duration": "PT52M",'
                ' "uploadDate": "2025-10-08T00:01:00+02:00"}'
                "</script>"
            ),
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = RTLPlusProvider.__new__(RTLPlusProvider)
    try:
        durations, upload_dates = await provider._episode_page_metadata(
            client, pages, "https://plus.rtl.de/der-promihof-p_68990"
        )
    finally:
        await client.aclose()

    assert durations == {"clip-1": 52}
    assert upload_dates == {"clip-1": datetime(2025, 10, 8, 0, 1, tzinfo=VIENNA)}


_PROMIHOF_S1_EPISODES: dict[int, tuple[str, str]] = {
    1: ("PT50M", "2025-10-08T00:01:00+02:00"),
    2: ("PT48M", "2025-10-08T00:01:00+02:00"),
    3: ("PT48M", "2025-10-15T00:01:00+02:00"),
    4: ("PT47M", "2025-10-15T00:01:00+02:00"),
    5: ("PT47M", "2025-10-22T00:01:00+02:00"),
    6: ("PT49M", "2025-10-22T00:01:00+02:00"),
    7: ("PT45M", "2025-10-29T00:01:00+01:00"),
    8: ("PT49M", "2025-10-29T00:01:00+01:00"),
    9: ("PT47M", "2025-11-05T00:01:00+01:00"),
    10: ("PT49M", "2025-11-05T00:01:00+01:00"),
    11: ("PT46M", "2025-11-26T20:14:00+01:00"),
    12: ("PT47M", "2025-11-26T21:16:00+01:00"),
}


def _promihof_layout_page() -> dict:
    items = [
        {
            "itemContent": {
                "id": f"promihof-{number}",
                "title": f"Folge {number}",
                "highlight": f"Staffel 1 • Folge {number} • Folge {number}",
                "action": {
                    "analytics": {"tealium": {"clip_type": "vi"}},
                    "target": {
                        "type": "layout",
                        "value_layout": {"id": f"clip_{1000 + number}", "seo": f"folge-{number}"},
                    },
                },
            }
        }
        for number in _PROMIHOF_S1_EPISODES
    ]
    return {
        "entity": {
            "type": "program",
            "id": "68990",
            "metadata": {"code": "der-promihof", "title": "Der Promihof"},
        },
        "seo": {"metadata": {"title": "Der Promihof 2099"}},
        "blocks": [
            {
                "analytics": {"tealium": {"from": "feature.videos_by_season_by_program"}},
                "content": {
                    "title": {"short": "Neue Staffel ab 22. Oktober"},
                    "items": [
                        {
                            "itemContent": {
                                "id": "bonus-1",
                                "highlight": (
                                    "Bonus-Inhalte 2026 • Folge 1 • "
                                    "Diese Promis ziehen auf den Hof!"
                                ),
                            }
                        },
                        {
                            "itemContent": {
                                "id": "bonus-3",
                                "highlight": (
                                    "Bonus-Inhalte 2026 • Folge 3 • Die ersten Minuten vorab"
                                ),
                            }
                        },
                    ],
                },
            },
            {
                "analytics": {"tealium": {"from": "feature.videos_by_season_by_program"}},
                "content": {"title": None, "items": items},
            },
        ],
    }


async def test_fetch_reads_episode_upload_dates_for_completed_season_from_api() -> None:
    # End-to-end over the mocked RTL+ API: the layout page only announces the next
    # season ("Neue Staffel ab 22. Oktober") and lists Staffel 1 without any dates,
    # while each season-1 episode page still publishes its runtime and original
    # streaming date. The finished season must keep those real 2025 dates instead of
    # being dated with the upcoming cadence, and S2 E1 must be synthesized.
    layout_page = _promihof_layout_page()
    requested: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        requested.append(request.url.path)
        if "/video/" in url:
            number = int(url.rsplit("/", 1)[-1].split("-")[1])
            duration, upload = _PROMIHOF_S1_EPISODES[number]
            return httpx.Response(
                200,
                text=(
                    '<script type="application/ld+json">'
                    f'{{"@type": "TVEpisode","duration": "{duration}",'
                    f' "uploadDate": "{upload}"}}'
                    "</script>"
                ),
            )
        return httpx.Response(200, json=layout_page)

    provider = RTLPlusProvider.__new__(RTLPlusProvider)
    provider._endpoint_template = "https://api.test/layout/{program_id}"
    provider._max_retries = 0
    provider._backoff = 0.0
    provider._bedrock_token = "token"
    provider._authorization = "auth"
    provider._oidc_client_secret = None

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        series = await provider._fetch(client, "68990", "https://plus.rtl.de/der-promihof-p_68990")
    finally:
        await client.aclose()

    assert set(requested) == {
        "/layout/68990",
        *(
            f"/der-promihof-p_68990/video/folge-{number}-c_{1000 + number}"
            for number in _PROMIHOF_S1_EPISODES
        ),
    }

    assert [season.number for season in series.seasons] == [1, 2]
    season_one = next(season for season in series.seasons if season.number == 1)
    assert [episode.number for episode in season_one.episodes] == list(range(1, 13))
    for episode in season_one.episodes:
        expected_duration = int(_PROMIHOF_S1_EPISODES[episode.number][0][2:-1])
        expected_upload = datetime.fromisoformat(_PROMIHOF_S1_EPISODES[episode.number][1])
        assert episode.duration_minutes == expected_duration
        assert len(episode.releases) == 1
        assert episode.releases[0].release_at == expected_upload
        assert episode.releases[0].date_from_api is False
        assert episode.releases[0].placeholder is False

    season_two = next(season for season in series.seasons if season.number == 2)
    assert [episode.number for episode in season_two.episodes] == [1]
    premiere = season_two.episodes[0]
    assert premiere.external_id == "68990:season:2:episode:1"
    assert premiere.releases[0].release_at == datetime(2098, 10, 22, tzinfo=VIENNA)
    assert premiere.releases[0].date_from_api is True


def test_normalize_prefers_catalogue_schedule_over_episode_upload_date() -> None:
    # A dated catalogue schedule always wins over the episode-page uploadDate fallback,
    # so a live season is never re-dated from the public page.
    payload = {
        "entity": {"id": "68990", "metadata": {"title": "Der Promihof"}},
        "seo": {
            "metadata": {
                "title": "Der Promihof 2099",
                "text": "**Folge 1** | Der Promihof | 22.10., 20:15 Uhr",
            }
        },
        "blocks": [
            {
                "analytics": {"tealium": {"from": "feature.videos_by_season_by_program"}},
                "content": {
                    "title": {"short": "Staffel 1"},
                    "items": [
                        {
                            "itemContent": {
                                "id": f"promihof-{number}",
                                "title": f"Folge {number}",
                                "highlight": f"Staffel 1 • Folge {number} • Folge {number}",
                            }
                        }
                        for number in (1, 2)
                    ],
                },
            },
        ],
    }

    result = RTLPlusProvider._normalize(
        RTLPlusProvider.__new__(RTLPlusProvider),
        "68990",
        [payload],
        upload_dates={
            "promihof-1": datetime(2025, 10, 8, tzinfo=VIENNA),
            "promihof-2": datetime(2025, 10, 8, tzinfo=VIENNA),
        },
    )

    episodes = {episode.number: episode for episode in result.seasons[0].episodes}
    assert episodes[1].releases[0].release_at == datetime(2099, 10, 22, 20, 15, tzinfo=VIENNA)
    assert episodes[1].releases[0].date_from_api is True
    assert episodes[2].releases[0].release_at == datetime(2025, 10, 8, tzinfo=VIENNA)
    assert episodes[2].releases[0].date_from_api is False


def _new_season_block(*, title: str, highlights: list[str]):
    return {
        "analytics": {"tealium": {"from": "feature.videos_by_season_by_program"}},
        "content": {
            "title": {"short": title},
            "items": [
                {"itemContent": {"id": f"clip-{index}", "title": "Demo", "highlight": highlight}}
                for index, highlight in enumerate(highlights)
            ],
        },
    }


def test_announces_unnumbered_new_season_only_without_item_season_labels() -> None:
    assert RTLPlusProvider._announces_unnumbered_new_season(
        _new_season_block(
            title="Neue Staffel ab 22. Oktober",
            highlights=[
                "Bonus-Inhalte 2026 • Folge 1 • Diese Promis ziehen auf den Hof!",
                "Bonus-Inhalte 2026 • Folge 3 • Die ersten Minuten vorab",
            ],
        )
    )
    assert not RTLPlusProvider._announces_unnumbered_new_season(
        _new_season_block(title="Neue Staffel", highlights=["Staffel 6 • Folge 1"])
    )
    assert not RTLPlusProvider._announces_unnumbered_new_season(
        _new_season_block(title="Staffel 7 ab 12. Oktober", highlights=["Folge 1"])
    )
    assert not RTLPlusProvider._announces_unnumbered_new_season(
        {
            "analytics": {"tealium": {"from": "feature.list"}},
            "content": {"title": {"short": "Neue Staffel"}, "items": [{}]},
        }
    )


def test_normalize_derives_higher_season_from_unnumbered_new_season_block() -> None:
    # RTL+ introduces the upcoming season in a block titled only "Neue Staffel ab
    # 22. Oktober" whose items carry no "Staffel N" label. The number is derived from the
    # previous season (1 → 2). The finished season keeps its real dates, read from each
    # episode page's uploadDate, instead of being dated with the upcoming cadence.
    payload = {
        "entity": {"id": "68990", "metadata": {"title": "Der Promihof"}},
        "seo": {"metadata": {"title": "Der Promihof 2099"}},
        "blocks": [
            _new_season_block(
                title="Neue Staffel ab 22. Oktober",
                highlights=[
                    "Bonus-Inhalte 2026 • Folge 1 • Diese Promis ziehen auf den Hof!",
                    "Bonus-Inhalte 2026 • Folge 3 • Die ersten Minuten vorab",
                ],
            ),
            {
                "analytics": {"tealium": {"from": "feature.videos_by_season_by_program"}},
                "content": {
                    "title": None,
                    "items": [
                        {
                            "itemContent": {
                                "id": f"clip-{episode_number}",
                                "title": f"Folge {episode_number}",
                                "highlight": (
                                    f"Staffel 1 • Folge {episode_number} • Folge {episode_number}"
                                ),
                            }
                        }
                        for episode_number in range(1, 13)
                    ],
                },
            },
        ],
    }
    upload_dates = {
        f"clip-{episode_number}": datetime(2025, 10, 8, tzinfo=VIENNA)
        + timedelta(weeks=(episode_number - 1) // 2)
        for episode_number in range(1, 13)
    }

    result = RTLPlusProvider._normalize(
        RTLPlusProvider.__new__(RTLPlusProvider),
        "68990",
        [payload],
        series_url="https://plus.rtl.de/der-promihof-p_68990",
        upload_dates=upload_dates,
    )

    assert [season.number for season in result.seasons] == [1, 2]
    season_one = result.seasons[0]
    assert all(
        release.release_at == datetime(2025, 10, 8, tzinfo=VIENNA)
        if episode.number <= 2
        else release.release_at > datetime(2025, 10, 8, tzinfo=VIENNA)
        for episode in season_one.episodes
        for release in episode.releases
    )
    assert all(
        not release.placeholder for episode in season_one.episodes for release in episode.releases
    )
    season_two = result.seasons[1]
    episode = season_two.episodes[0]
    assert episode.number == 1
    assert episode.external_id == "68990:season:2:episode:1"
    # Title year 2099 → start 22. Oktober rolled back one year to stay future-dated.
    assert episode.releases[0].release_at == datetime(2098, 10, 22, tzinfo=VIENNA)
    assert episode.releases[0].date_from_api is True
    assert episode.releases[0].placeholder is False


def test_normalize_keeps_explicit_season_in_neue_staffel_block() -> None:
    payload = {
        "entity": {"id": "11271", "metadata": {"title": "Demo"}},
        "seo": {"metadata": {"title": "Demo"}},
        "blocks": [
            _new_season_block(title="Neue Staffel", highlights=["Staffel 6 • Folge 1 • Folge 1"]),
        ],
    }

    result = RTLPlusProvider._normalize(
        RTLPlusProvider.__new__(RTLPlusProvider), "11271", [payload]
    )

    assert [season.number for season in result.seasons] == [6]

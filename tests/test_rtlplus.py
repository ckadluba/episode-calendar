from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import httpx

from episode_calendar.domain import ReleaseType
from episode_calendar.providers.base import NormalizedEpisode, NormalizedSeason, NormalizedSeries
from episode_calendar.providers.rtlplus import RTLPlusProvider


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
        assert request.url.params["from"] == "2026-10-06 02:00:00"
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


async def test_epg_adds_numberless_future_episode_and_ignores_long_entries() -> None:
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
    provider._clock = lambda: datetime(2026, 10, 6, tzinfo=UTC)
    RTLPlusProvider._epg_tasks.clear()
    normalized = NormalizedSeries(
        external_id="demo",
        title="Demo series",
        seasons=(
            NormalizedSeason(
                external_id="season-1",
                number=1,
                episodes=(NormalizedEpisode(external_id="episode-1", number=1, title="Episode 1"),),
            ),
        ),
    )
    try:
        result = await provider._add_epg(client, normalized)
    finally:
        await client.aclose()

    assert len(result.seasons) == 2
    assert result.seasons[-1].external_id == "rtlplus-epg"
    assert len(result.seasons[-1].episodes) == 1
    assert result.seasons[-1].episodes[0].number is None


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

    assert result.seasons[0].episodes[0].releases == ()
    assert len(result.seasons[1].episodes[0].releases) == 1


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

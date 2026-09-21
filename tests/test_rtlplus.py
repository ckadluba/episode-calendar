from datetime import datetime
from zoneinfo import ZoneInfo

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
                        "title": "Demo 2026, ab 25. August auf RTL+",
                        "text": (
                            "| **Folge 4** | Di., 15.9. um 0:00 Uhr |\n"
                            "| **Folge 5** | Di., 22.9. um 0:00 Uhr |"
                        ),
                    }
                },
                "blocks": [
                    {
                        "analytics": {
                            "tealium": {"from": "feature.videos_by_season_by_program"}
                        },
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

    result = RTLPlusProvider._normalize(
        RTLPlusProvider.__new__(RTLPlusProvider), "6838", [payload]
    )

    assert len(result.seasons[0].episodes[0].releases) == 1

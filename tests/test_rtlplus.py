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


def test_schedule_uses_streaming_dates_from_rtl_schedule_table() -> None:
    schedule = RTLPlusProvider._schedule(
        {
            "metadata": {
                "text": (
                    "Folge | RTL | RTL+ / Streaming\n"
                    "Folge 1 | Mo., 17.8. um 20:15 Uhr | Mo., 3.8. ab 00:00 Uhr\n"
                    "Folge 2 | Di., 18.8. um 20:15 Uhr | Mi., 5.8. ab 0:00 Uhr"
                )
            }
        }
    )

    assert schedule[1] == datetime(2026, 8, 3, tzinfo=ZoneInfo("Europe/Vienna"))
    assert schedule[2] == datetime(2026, 8, 5, tzinfo=ZoneInfo("Europe/Vienna"))


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

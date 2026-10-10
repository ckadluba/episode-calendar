from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from episode_calendar.domain import ReleaseType
from episode_calendar.providers.base import (
    NormalizedEpisode,
    NormalizedEpisodeRelease,
    NormalizedSeason,
    NormalizedSeries,
)
from episode_calendar.providers.epg import (
    EpgBroadcast,
    derive_broadcast_slots,
    derive_slots_from_moments,
    fits_slot,
    fold_broadcasts,
)

VIENNA = ZoneInfo("Europe/Vienna")


def test_derive_slots_marks_a_daily_time_as_weekday_independent() -> None:
    moments = [datetime(2026, 10, day, 20, 15, tzinfo=VIENNA) for day in range(5, 10)]

    assert derive_slots_from_moments(moments, timezone=VIENNA) == ((None, time(20, 15)),)


def test_derive_slots_keeps_weekdays_for_a_weekly_cadence() -> None:
    season = NormalizedSeason(
        external_id="s",
        number=1,
        episodes=(
            NormalizedEpisode(
                external_id="e",
                number=1,
                title="e",
                releases=(
                    NormalizedEpisodeRelease(
                        release_type=ReleaseType.TV_BROADCAST,
                        release_at=datetime(2026, 10, 7, 20, 15, tzinfo=VIENNA),
                    ),
                    NormalizedEpisodeRelease(
                        release_type=ReleaseType.TV_BROADCAST,
                        release_at=datetime(2026, 10, 14, 20, 15, tzinfo=VIENNA),
                    ),
                ),
            ),
        ),
    )

    assert derive_broadcast_slots(season, timezone=VIENNA) == ((2, time(20, 15)),)


def test_derive_broadcast_slots_prefers_broadcasts_over_previews() -> None:
    season = NormalizedSeason(
        external_id="s",
        number=1,
        episodes=(
            NormalizedEpisode(
                external_id="e",
                number=1,
                title="e",
                releases=(
                    NormalizedEpisodeRelease(
                        release_type=ReleaseType.TV_BROADCAST,
                        release_at=datetime(2026, 10, 7, 20, 15, tzinfo=VIENNA),
                    ),
                    NormalizedEpisodeRelease(
                        release_type=ReleaseType.STREAMING,
                        release_at=datetime(2026, 10, 1, tzinfo=VIENNA),
                        preview=True,
                    ),
                ),
            ),
        ),
    )

    assert derive_broadcast_slots(season, timezone=VIENNA) == ((2, time(20, 15)),)


def test_derive_broadcast_slots_falls_back_to_non_preview_streaming() -> None:
    season = NormalizedSeason(
        external_id="s",
        number=1,
        episodes=(
            NormalizedEpisode(
                external_id="e",
                number=1,
                title="e",
                releases=(
                    NormalizedEpisodeRelease(
                        release_type=ReleaseType.STREAMING,
                        release_at=datetime(2026, 10, 7, 20, 15, tzinfo=VIENNA),
                    ),
                ),
            ),
        ),
    )

    assert derive_broadcast_slots(season, timezone=VIENNA) == ((2, time(20, 15)),)


def test_fits_slot_matches_the_same_weekday_within_tolerance() -> None:
    slots = ((2, time(20, 15)),)

    assert fits_slot(
        datetime(2026, 10, 7, 21, 30, tzinfo=VIENNA),
        slots,
        timezone=VIENNA,
        tolerance=timedelta(hours=2),
    )
    assert not fits_slot(
        datetime(2026, 10, 8, 20, 15, tzinfo=VIENNA),
        slots,
        timezone=VIENNA,
        tolerance=timedelta(hours=2),
    )


def test_fold_broadcasts_extends_running_season_and_flags_reruns() -> None:
    series = NormalizedSeries(
        external_id="s",
        title="S",
        seasons=(
            NormalizedSeason(
                external_id="s1",
                number=1,
                episodes=(
                    NormalizedEpisode(
                        external_id="e1",
                        number=1,
                        title="e1",
                        releases=(
                            NormalizedEpisodeRelease(
                                release_type=ReleaseType.TV_BROADCAST,
                                release_at=datetime(2026, 10, 7, 20, 15, tzinfo=VIENNA),
                            ),
                        ),
                    ),
                ),
            ),
        ),
    )
    running = series.seasons[0]
    slots = derive_broadcast_slots(running, timezone=VIENNA)
    broadcasts = (
        EpgBroadcast(
            external_id="b1",
            title="e2",
            start=datetime(2026, 10, 14, 20, 15, tzinfo=VIENNA),
        ),
        EpgBroadcast(
            external_id="b2",
            title="night repeat",
            start=datetime(2026, 10, 15, 0, 15, tzinfo=VIENNA),
        ),
    )

    result = fold_broadcasts(
        series,
        broadcasts,
        running_season=running,
        slots=slots,
        timezone=VIENNA,
        tolerance=timedelta(hours=2),
        next_episode_external_id=lambda season_id, number: f"next:{season_id}:{number}",
    )

    episodes = result.seasons[0].episodes
    assert [episode.number for episode in episodes] == [1, 2]
    assert episodes[-1].external_id == "next:s1:2"
    assert episodes[-1].releases[0].rerun is False
    reruns = [release for release in episodes[0].releases if release.rerun]
    assert len(reruns) == 1
    assert reruns[0].release_at == datetime(2026, 10, 14, 22, 15, tzinfo=UTC)

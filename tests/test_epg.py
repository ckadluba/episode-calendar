from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from episode_calendar.importer.providers.base import (
    NormalizedEpisode,
    NormalizedEpisodeRelease,
    NormalizedSeason,
    NormalizedSeries,
    first_seen_release,
)
from episode_calendar.importer.providers.epg import (
    EpgBroadcast,
    annual_window_distance,
    derive_broadcast_slots,
    derive_slots_from_moments,
    filter_obsolete_seasons,
    fits_slot,
    merge_epg,
    new_season_premiere,
    next_season_number,
    season_premieres,
)
from episode_calendar.shared.domain import ReleaseType

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


def test_merge_epg_extends_running_season_and_flags_reruns() -> None:
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
    slots = derive_broadcast_slots(series.seasons[0], timezone=VIENNA)
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

    result = merge_epg(
        series,
        broadcasts,
        now=datetime(2026, 10, 10, tzinfo=UTC),
        timezone=VIENNA,
        slots=slots,
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


def test_merge_epg_keeps_airing_for_dated_episode_unless_within_tolerance() -> None:
    now = datetime(2026, 10, 10, tzinfo=UTC)
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
                        releases=(first_seen_release(now),),
                    ),
                    NormalizedEpisode(
                        external_id="e2",
                        number=2,
                        title="e2",
                        releases=(
                            NormalizedEpisodeRelease(
                                external_id="e2-stream",
                                release_type=ReleaseType.STREAMING,
                                release_at=datetime(2026, 10, 7, tzinfo=VIENNA),
                            ),
                        ),
                    ),
                ),
            ),
        ),
    )

    result = merge_epg(
        series,
        (
            EpgBroadcast(
                external_id="b1",
                title="e1",
                start=datetime(2026, 9, 30, 20, 15, tzinfo=VIENNA),
                episode_number=1,
            ),
            EpgBroadcast(
                external_id="b2",
                title="e2",
                start=datetime(2026, 10, 7, 20, 15, tzinfo=VIENNA),
                episode_number=2,
            ),
        ),
        now=now,
        timezone=VIENNA,
        tolerance=timedelta(hours=2),
        next_episode_external_id=lambda season_id, number: f"next:{season_id}:{number}",
    )

    announced, dated = result.seasons[0].episodes
    assert [release.release_type for release in announced.releases if not release.placeholder] == [
        ReleaseType.TV_BROADCAST,
    ]
    assert announced.releases[-1].date_from_api is True
    assert [release.external_id for release in dated.releases] == ["e2-stream", "b2"]


def test_merge_epg_creates_numbered_episode_not_in_catalog() -> None:
    now = datetime(2026, 10, 10, tzinfo=UTC)
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
                        releases=(first_seen_release(now),),
                    ),
                ),
            ),
        ),
    )

    result = merge_epg(
        series,
        (
            EpgBroadcast(
                external_id="b3",
                title="e3",
                start=datetime(2026, 10, 21, 20, 15, tzinfo=VIENNA),
                episode_number=3,
            ),
        ),
        now=now,
        timezone=VIENNA,
        tolerance=timedelta(hours=2),
        next_episode_external_id=lambda season_id, number: f"scheduled:{season_id}:{number}",
    )

    episodes = result.seasons[0].episodes
    assert [episode.number for episode in episodes] == [1, 3]
    assert episodes[-1].external_id == "scheduled:s1:3"
    assert episodes[-1].releases[0].date_from_api is True


def test_merge_epg_fills_announced_undated_next_episode() -> None:
    now = datetime(2026, 10, 10, tzinfo=UTC)
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
                    NormalizedEpisode(
                        external_id="e2",
                        number=2,
                        title="e2",
                        releases=(first_seen_release(now),),
                    ),
                ),
            ),
        ),
    )

    result = merge_epg(
        series,
        (
            EpgBroadcast(
                external_id="b2",
                title="e2",
                start=datetime(2026, 10, 14, 20, 15, tzinfo=VIENNA),
            ),
        ),
        now=now,
        timezone=VIENNA,
        tolerance=timedelta(hours=2),
        next_episode_external_id=lambda season_id, number: f"next:{season_id}:{number}",
    )

    episodes = result.seasons[0].episodes
    assert [episode.number for episode in episodes] == [1, 2]
    assert episodes[1].external_id == "e2"
    assert [
        release.release_type for release in episodes[1].releases if not release.placeholder
    ] == [
        ReleaseType.TV_BROADCAST,
    ]


def test_merge_epg_premieres_subscriber_only_episodes_in_order() -> None:
    """An unattributable airing cannot repeat an episode without a regular date."""

    now = datetime(2026, 10, 19, tzinfo=UTC)
    preview = NormalizedEpisodeRelease(
        release_type=ReleaseType.STREAMING,
        release_at=datetime(2026, 10, 17, tzinfo=VIENNA),
        preview=True,
    )
    series = NormalizedSeries(
        external_id="s",
        title="S",
        seasons=(
            NormalizedSeason(
                external_id="s1",
                number=1,
                episodes=(
                    NormalizedEpisode(external_id="e3", number=3, title="e3", releases=(preview,)),
                    NormalizedEpisode(external_id="e4", number=4, title="e4", releases=(preview,)),
                ),
            ),
        ),
    )

    result = merge_epg(
        series,
        (
            EpgBroadcast(
                external_id="b1",
                title="S",
                start=datetime(2026, 10, 25, 17, 10, tzinfo=VIENNA),
            ),
            EpgBroadcast(
                external_id="b2",
                title="S",
                start=datetime(2026, 10, 25, 18, 10, tzinfo=VIENNA),
            ),
            EpgBroadcast(
                external_id="b3",
                title="S",
                start=datetime(2026, 10, 25, 22, 10, tzinfo=VIENNA),
            ),
        ),
        now=now,
        timezone=VIENNA,
        slots=(),
        tolerance=timedelta(hours=2),
        next_episode_external_id=lambda season_id, number: f"next:{season_id}:{number}",
    )

    episodes = {episode.number: episode for episode in result.seasons[0].episodes}
    airings = {
        number: [
            release
            for release in episode.releases
            if release.release_type is ReleaseType.TV_BROADCAST
        ]
        for number, episode in episodes.items()
    }
    assert [(release.release_at, release.rerun) for release in airings[3]] == [
        (datetime(2026, 10, 25, 17, 10, tzinfo=VIENNA), False)
    ]
    assert [(release.release_at, release.rerun) for release in airings[4]] == [
        (datetime(2026, 10, 25, 18, 10, tzinfo=VIENNA), False),
        (datetime(2026, 10, 25, 22, 10, tzinfo=VIENNA), True),
    ]


def test_filter_obsolete_seasons_drops_undated_seasons_below_the_highest() -> None:
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
                        releases=(first_seen_release(datetime(2026, 10, 1, tzinfo=UTC)),),
                    ),
                ),
            ),
            NormalizedSeason(
                external_id="s2",
                number=2,
                episodes=(
                    NormalizedEpisode(
                        external_id="e2",
                        number=1,
                        title="e2",
                        releases=(
                            NormalizedEpisodeRelease(
                                release_type=ReleaseType.STREAMING,
                                release_at=datetime(2026, 10, 5, tzinfo=VIENNA),
                            ),
                        ),
                    ),
                ),
            ),
        ),
    )

    result = filter_obsolete_seasons(series)

    assert [season.number for season in result.seasons] == [2]


def test_filter_obsolete_seasons_keeps_undated_season_without_higher_season() -> None:
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
                        releases=(first_seen_release(datetime(2026, 10, 1, tzinfo=UTC)),),
                    ),
                ),
            ),
        ),
    )

    result = filter_obsolete_seasons(series)

    assert [season.number for season in result.seasons] == [1]


def annual_season(number: int, premiere: datetime) -> NormalizedSeason:
    return NormalizedSeason(
        external_id=f"s{number}",
        number=number,
        episodes=tuple(
            NormalizedEpisode(
                external_id=f"s{number}e{index}",
                number=index,
                title=f"S{number}E{index}",
                releases=(
                    NormalizedEpisodeRelease(
                        release_type=ReleaseType.STREAMING,
                        release_at=premiere + timedelta(weeks=index - 1),
                    ),
                ),
            )
            for index in range(1, 3)
        ),
    )


def annual_series() -> NormalizedSeries:
    """A series whose seasons always premiere in mid-November."""

    return NormalizedSeries(
        external_id="s",
        title="S",
        seasons=(
            annual_season(1, datetime(2023, 11, 10, 0, 1, tzinfo=VIENNA)),
            annual_season(2, datetime(2024, 11, 15, 0, 1, tzinfo=VIENNA)),
            annual_season(3, datetime(2025, 11, 12, 0, 1, tzinfo=VIENNA)),
        ),
    )


def merge_annual(
    series: NormalizedSeries, broadcasts: tuple[EpgBroadcast, ...], *, now: datetime
) -> NormalizedSeries:
    return merge_epg(
        series,
        broadcasts,
        now=now,
        timezone=VIENNA,
        tolerance=timedelta(hours=2),
        next_episode_external_id=lambda season_id, number: f"next:{season_id}:{number}",
    )


def test_annual_window_matches_a_premiere_across_the_new_year() -> None:
    premieres = [
        datetime(2023, 12, 28, 20, 15, tzinfo=VIENNA),
        datetime(2024, 12, 30, 20, 15, tzinfo=VIENNA),
    ]

    assert annual_window_distance(
        premieres, datetime(2026, 1, 4, 20, 15, tzinfo=VIENNA), timezone=VIENNA
    ) == timedelta(days=5)


def test_season_premieres_uses_the_earliest_release_per_season() -> None:
    premieres = season_premieres(annual_series())

    assert premieres == (
        datetime(2023, 11, 10, 0, 1, tzinfo=VIENNA),
        datetime(2024, 11, 15, 0, 1, tzinfo=VIENNA),
        datetime(2025, 11, 12, 0, 1, tzinfo=VIENNA),
    )


def test_next_season_number_ignores_a_listed_undated_season() -> None:
    series = annual_series()
    listed = NormalizedSeason(
        external_id="s4",
        number=4,
        episodes=(
            NormalizedEpisode(
                external_id="s4e1",
                number=1,
                title="S4E1",
                releases=(first_seen_release(datetime(2026, 10, 1, tzinfo=UTC)),),
            ),
        ),
    )

    assert next_season_number(series) == 4
    assert (
        next_season_number(series.model_copy(update={"seasons": (*series.seasons, listed)})) is None
    )


def test_new_season_needs_more_than_one_earlier_premiere() -> None:
    series = NormalizedSeries(
        external_id="s",
        title="S",
        seasons=(annual_season(1, datetime(2025, 11, 12, 0, 1, tzinfo=VIENNA)),),
    )

    assert (
        new_season_premiere(series, datetime(2026, 11, 6, 20, 15, tzinfo=VIENNA), timezone=VIENNA)
        is None
    )


def test_merge_epg_starts_next_season_inside_the_annual_window() -> None:
    result = merge_annual(
        annual_series(),
        (
            EpgBroadcast(
                external_id="b1", title="S", start=datetime(2026, 11, 6, 20, 15, tzinfo=VIENNA)
            ),
            EpgBroadcast(
                external_id="b2", title="S", start=datetime(2026, 11, 6, 21, 15, tzinfo=VIENNA)
            ),
        ),
        now=datetime(2026, 10, 20, tzinfo=UTC),
    )

    assert [season.number for season in result.seasons] == [1, 2, 3, 4]
    premiere = result.seasons[-1]
    assert [episode.number for episode in premiere.episodes] == [1, 2]
    airings = [release for episode in premiere.episodes for release in episode.releases]
    assert [release.release_at for release in airings] == [
        datetime(2026, 11, 6, 20, 15, tzinfo=VIENNA),
        datetime(2026, 11, 6, 21, 15, tzinfo=VIENNA),
    ]
    assert all(release.release_type is ReleaseType.TV_BROADCAST for release in airings)
    assert all(release.rerun is False for release in airings)
    previous = result.seasons[2].episodes[-1]
    assert not any(release.rerun for release in previous.releases)


def test_merge_epg_keeps_airing_outside_the_annual_window_as_rerun() -> None:
    result = merge_annual(
        annual_series(),
        (
            EpgBroadcast(
                external_id="b1", title="S", start=datetime(2026, 6, 5, 20, 15, tzinfo=VIENNA)
            ),
        ),
        now=datetime(2026, 6, 1, tzinfo=UTC),
    )

    assert [season.number for season in result.seasons] == [1, 2, 3]
    reruns = [
        release
        for episode in result.seasons[-1].episodes
        for release in episode.releases
        if release.rerun
    ]
    assert len(reruns) == 1
    assert reruns[0].release_at == datetime(2026, 6, 5, 20, 15, tzinfo=VIENNA)


def test_merge_epg_tolerates_a_few_weeks_of_drift_in_the_annual_window() -> None:
    inside = merge_annual(
        annual_series(),
        (
            EpgBroadcast(
                external_id="b1", title="S", start=datetime(2026, 10, 16, 20, 15, tzinfo=VIENNA)
            ),
        ),
        now=datetime(2026, 10, 1, tzinfo=UTC),
    )
    outside = merge_annual(
        annual_series(),
        (
            EpgBroadcast(
                external_id="b1", title="S", start=datetime(2026, 10, 10, 20, 15, tzinfo=VIENNA)
            ),
        ),
        now=datetime(2026, 10, 1, tzinfo=UTC),
    )

    assert [season.number for season in inside.seasons] == [1, 2, 3, 4]
    assert [season.number for season in outside.seasons] == [1, 2, 3]


def test_merge_epg_keeps_airing_of_the_running_season() -> None:
    """A fresh release keeps the airing in the season even if it lands in the window."""

    result = merge_annual(
        annual_series(),
        (
            EpgBroadcast(
                external_id="b1", title="S", start=datetime(2025, 11, 26, 20, 15, tzinfo=VIENNA)
            ),
        ),
        now=datetime(2025, 11, 20, tzinfo=UTC),
    )

    assert [season.number for season in result.seasons] == [1, 2, 3]
    reruns = [
        release
        for episode in result.seasons[-1].episodes
        for release in episode.releases
        if release.rerun
    ]
    assert len(reruns) == 1


def test_merge_epg_keeps_provider_episode_number_for_a_new_season() -> None:
    result = merge_annual(
        annual_series(),
        (
            EpgBroadcast(
                external_id="b1",
                title="S",
                start=datetime(2026, 11, 6, 20, 15, tzinfo=VIENNA),
                episode_number=5,
            ),
        ),
        now=datetime(2026, 10, 20, tzinfo=UTC),
    )

    premiere = result.seasons[-1]
    assert [episode.number for episode in premiere.episodes] == [5]


def test_merge_epg_does_not_invent_a_season_without_a_cadence() -> None:
    series = NormalizedSeries(
        external_id="s",
        title="S",
        seasons=(annual_season(1, datetime(2025, 11, 12, 0, 1, tzinfo=VIENNA)),),
    )

    result = merge_annual(
        series,
        (
            EpgBroadcast(
                external_id="b1", title="S", start=datetime(2026, 11, 6, 20, 15, tzinfo=VIENNA)
            ),
        ),
        now=datetime(2026, 10, 20, tzinfo=UTC),
    )

    assert [season.number for season in result.seasons] == [1]
    assert any(
        release.rerun for episode in result.seasons[0].episodes for release in episode.releases
    )


def test_merge_epg_starts_a_provider_announced_season_without_a_cadence() -> None:
    """A provider can name the season an airing starts even without a catalogue cadence."""

    series = NormalizedSeries(
        external_id="s",
        title="S",
        seasons=(annual_season(1, datetime(2025, 11, 16, 21, 0, tzinfo=VIENNA)),),
    )

    result = merge_epg(
        series,
        (
            EpgBroadcast(
                external_id="b1",
                title="S",
                start=datetime(2026, 11, 16, 21, 0, tzinfo=VIENNA),
                announces_season=2,
            ),
            EpgBroadcast(
                external_id="b2", title="S", start=datetime(2026, 11, 17, 21, 0, tzinfo=VIENNA)
            ),
        ),
        now=datetime(2026, 11, 11, tzinfo=UTC),
        timezone=VIENNA,
        tolerance=timedelta(hours=2),
        next_episode_external_id=lambda season_id, number: f"epg:next:{season_id}:{number}",
    )

    assert [season.number for season in result.seasons] == [1, 2]
    premiere = result.seasons[-1]
    assert premiere.external_id == "epg:season:s:2"
    assert [episode.number for episode in premiere.episodes] == [1, 2]
    airings = [release for episode in premiere.episodes for release in episode.releases]
    assert [release.release_at for release in airings] == [
        datetime(2026, 11, 16, 21, 0, tzinfo=VIENNA),
        datetime(2026, 11, 17, 21, 0, tzinfo=VIENNA),
    ]
    assert all(release.release_type is ReleaseType.TV_BROADCAST for release in airings)
    assert all(release.rerun is False for release in airings)

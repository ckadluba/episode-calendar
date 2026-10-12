from episode_calendar.importer.providers.base import (
    duration_minutes_from_iso8601,
    duration_minutes_from_seconds,
    duration_minutes_from_text,
)


def test_seconds_round_half_up_to_minutes() -> None:
    assert duration_minutes_from_seconds(1621) == 27
    assert duration_minutes_from_seconds(3088) == 51
    assert duration_minutes_from_seconds(90) == 2
    assert duration_minutes_from_seconds(0) == 0


def test_seconds_ignore_invalid_values() -> None:
    assert duration_minutes_from_seconds(None) is None
    assert duration_minutes_from_seconds(-1) is None
    assert duration_minutes_from_seconds(True) is None
    assert duration_minutes_from_seconds("1621") is None


def test_iso8601_duration_to_minutes() -> None:
    assert duration_minutes_from_iso8601("PT1H4M59.136S") == 65
    assert duration_minutes_from_iso8601("PT45M") == 45
    assert duration_minutes_from_iso8601("PT30S") == 1
    assert duration_minutes_from_iso8601("not-a-duration") is None
    assert duration_minutes_from_iso8601(None) is None


def test_text_duration_to_minutes() -> None:
    assert duration_minutes_from_text("47 mins") == 47
    assert duration_minutes_from_text("1h 5m") == 65
    assert duration_minutes_from_text("1 hr 16 mins") == 76
    assert duration_minutes_from_text("1 Std. 26 Min.") == 86
    assert duration_minutes_from_text("27 Min.") == 27


def test_text_duration_ignores_values_without_units() -> None:
    assert duration_minutes_from_text("") is None
    assert duration_minutes_from_text("no duration") is None
    assert duration_minutes_from_text(None) is None

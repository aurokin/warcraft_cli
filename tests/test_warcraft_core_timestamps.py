from __future__ import annotations

from datetime import UTC, datetime

from warcraft_core.shapes import unique_strings
from warcraft_core.timestamps import iso_now_utc, parse_iso8601_utc


def test_parse_iso8601_utc_reads_z_offsets_and_naive_times_as_utc() -> None:
    expected = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert parse_iso8601_utc("2026-01-02T03:04:05Z") == expected
    assert parse_iso8601_utc("2026-01-02T05:04:05+02:00") == expected
    assert parse_iso8601_utc("2026-01-02T03:04:05") == expected
    assert parse_iso8601_utc("yesterday") is None
    assert parse_iso8601_utc(None) is None


def test_iso_now_utc_round_trips_through_the_parser() -> None:
    stamp = iso_now_utc()
    assert stamp.endswith("Z")
    assert parse_iso8601_utc(stamp) is not None


def test_unique_strings_strips_and_keeps_first_seen_order() -> None:
    assert unique_strings([" b", "a", "b", "", "  ", None, 3, "a"]) == ["b", "a"]

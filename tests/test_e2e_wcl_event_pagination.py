"""Synthetic pagination regressions for the live filter-comparison oracle."""

from types import SimpleNamespace

import pytest

from tests.e2e import test_warcraftlogs as journeys


def test_filter_oracle_reads_later_pages_and_keeps_the_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, ...]] = []
    first = {"timestamp": 10, "targetID": 7}
    second = {"timestamp": 20, "targetID": 7}

    def run(binary: str, *args: str) -> SimpleNamespace:
        assert binary == "warcraftlogs"
        calls.append(args)
        page = {"events": [first], "next_page_timestamp": 20} if len(calls) == 1 else {
            "events": [second], "next_page_timestamp": None,
        }
        return SimpleNamespace(data=page, describe=lambda: "synthetic event page")

    monkeypatch.setattr(journeys, "run", run)
    scope = ("report-events", "Synthetic", "--fight-id", "1", "--target-id", "7")
    assert journeys._complete_event_rows(*scope) == [first, second]
    assert calls == [scope, (*scope, "--start-time", "20")]


def test_filter_oracle_rejects_a_stalled_continuation(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def run(binary: str, *args: str) -> SimpleNamespace:
        nonlocal calls
        calls += 1
        return SimpleNamespace(data={"events": [], "next_page_timestamp": 20}, describe=lambda: "synthetic stalled page")

    monkeypatch.setattr(journeys, "run", run)
    with pytest.raises(AssertionError, match="pagination stalled"):
        journeys._complete_event_rows("report-events", "Synthetic", "--fight-id", "1")
    assert calls == 2

"""Malformed cursors and interrupted writes never become complete evidence artifacts."""

from __future__ import annotations

import json

import pytest
from warcraft_core.provider import ProviderError
from warcraftlogs_cli.event_export import write_event_artifact

from tests.test_warcraftlogs_reliability import EventClient, export, page


@pytest.mark.parametrize("cursor", [True, False, float("nan"), float("inf"), -float("inf")])
def test_event_export_rejects_nonfinite_or_boolean_cursor(cursor):
    with pytest.raises(ProviderError, match="did not advance"):
        export(EventClient([page([], cursor)]))


@pytest.mark.parametrize("cursor", [True, False, float("nan"), float("inf"), -float("inf")])
def test_event_bound_does_not_bypass_cursor_validation(cursor):
    response = page([{"timestamp": 1}, {"timestamp": 2}], cursor)
    with pytest.raises(ProviderError, match="did not advance"):
        export(EventClient([response]), max_events=1)


def test_missing_cursor_cannot_establish_pagination_exhaustion():
    response = page([{"timestamp": 1}], None)
    response["events"].pop("nextPageTimestamp")
    with pytest.raises(ProviderError, match="exhaustion is unknown"):
        export(EventClient([response]))


def test_missing_revision_is_explicitly_distinct_from_pagination_completeness():
    response = page([{"timestamp": 1}], None)
    response.pop("revision")
    payload = export(EventClient([response]))
    assert payload["export"]["complete"] is True
    freshness = payload["export"]["freshness"]
    assert freshness["revision_consistent"] is False
    assert "pagination completeness does not establish a consistent report snapshot" in freshness["revision_warning"]


@pytest.mark.parametrize("format", ["json", "jsonl"])
def test_failed_artifact_write_leaves_destination_available_for_retry(tmp_path, monkeypatch, format):
    payload = export(EventClient([page([{"timestamp": 1}], None)]))
    destination = tmp_path / f"evidence.{format}"
    original = json.dump if format == "json" else json.dumps

    def fail_after_write(*args, **kwargs):
        if format == "json":
            args[1].write('{"partial":')
        raise OSError("Synthetic disk write failure")

    target = f"warcraftlogs_cli.event_export.json.{'dump' if format == 'json' else 'dumps'}"
    monkeypatch.setattr(target, fail_after_write)
    with pytest.raises(ProviderError, match="Synthetic disk write failure"):
        write_event_artifact(str(destination), payload, format=format)
    assert not destination.exists()
    assert list(tmp_path.iterdir()) == []
    monkeypatch.setattr(target, original)
    assert write_event_artifact(str(destination), payload, format=format) == str(destination)
    assert destination.exists()


def test_publication_failure_does_not_replace_existing_artifact(tmp_path, monkeypatch):
    payload = export(EventClient([page([], None)]))
    destination = tmp_path / "capture.json"
    destination.write_text("Existing evidence")
    with pytest.raises(ProviderError):
        write_event_artifact(str(destination), payload)
    assert destination.read_text() == "Existing evidence"
    assert list(tmp_path.iterdir()) == [destination]

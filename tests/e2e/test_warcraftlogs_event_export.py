"""Public event evidence can be collected and replayed without an authenticated user session."""

from __future__ import annotations

import json

from tests.e2e.harness import run
from tests.e2e.test_warcraftlogs import anchor


def test_public_default_endpoint_matches_doctor_readiness(require):
    require("warcraftlogs")
    doctor = run("warcraftlogs", "doctor")
    assert doctor.data["auth"]["command_endpoint_policy"] == "client", doctor.describe()
    assert doctor.data["auth"]["command_endpoint"] == "client", doctor.describe()
    assert doctor.data["auth"]["command_access"]["ready"] is True, doctor.describe()
    zones = run("warcraftlogs", "zones")
    assert zones.data["zones"], zones.describe()


def test_complete_event_artifact_equals_independent_large_page(require, tmp_path):
    require("warcraftlogs")
    found = anchor()
    actor = found.players[0]
    scope = [found.code, "--fight-id", str(found.fight_id), "--source-id", str(actor["id"]), "--data-type", "casts"]
    baseline = run("warcraftlogs", "report-events", *scope, "--limit", "10000")
    assert baseline.data["next_page_timestamp"] is None, baseline.describe()
    assert baseline.data["events"], baseline.describe()
    page_size = max(1, len(baseline.data["events"]) // 3)
    path = tmp_path / "events.jsonl"
    collected = run(
        "warcraftlogs",
        "report-events",
        *scope,
        "--all-pages",
        "--limit",
        str(page_size),
        "--max-pages",
        "10",
        "--max-events",
        "10000",
        "--out",
        str(path),
        "--artifact-format",
        "jsonl",
    )
    assert collected.data["export"]["complete"] is True, collected.describe()
    assert collected.data["events"] == baseline.data["events"], collected.describe()
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert lines[0]["artifact"]["export"] == collected.data["export"]
    assert [line["event"] for line in lines[1:]] == baseline.data["events"]
    bounded = run("warcraftlogs", "report-events", *scope, "--all-pages", "--limit", "1", "--max-pages", "1")
    assert bounded.data["events"] == baseline.data["events"][:1], bounded.describe()
    if len(baseline.data["events"]) > 1:
        assert bounded.data["export"]["complete"] is False, bounded.describe()
        assert bounded.data["export"]["stop_reason"] == "max_pages", bounded.describe()
        assert bounded.data["export"]["continuation"]["start_time"] is not None, bounded.describe()

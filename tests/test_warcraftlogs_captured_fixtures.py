"""Parser tests against a captured Warcraft Logs GraphQL response.

The fixture in ``tests/fixtures/warcraftlogs/`` is a real (trimmed) ``ReportEvents`` +
``ReportMasterData`` pair from a public guild report, so these assertions pin the cast parser to
data Warcraft Logs actually returns rather than to hand-written shapes. See
``docs/architecture/FIXTURE_MAINTENANCE.md``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from warcraftlogs_cli.main import _encounter_cast_rows_payload

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "warcraftlogs" / "report_encounter_casts_capture.json"


@pytest.fixture(scope="module")
def captured_casts() -> dict[str, Any]:
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    return _encounter_cast_rows_payload(
        report=fixture["report"],
        fight=fixture["fight"],
        events_report=fixture["events_report"],
        master_report=fixture["master_report"],
        preview_limit=3,
    )


def test_captured_cast_page_reports_its_own_truncation(captured_casts: dict[str, Any]) -> None:
    casts = captured_casts["casts"]
    # The real response carried a nextPageTimestamp: these aggregates are one page, not the fight.
    assert casts["event_count"] == 40
    assert casts["truncated"] is True
    assert casts["next_page_timestamp"] == 213337
    assert any("next_page_timestamp" in note for note in captured_casts["notes"])


def test_captured_casts_name_every_real_target(captured_casts: dict[str, Any]) -> None:
    by_target = captured_casts["casts"]["by_target"]
    # Environment (-1) and the boss NPC are named from master data, not left as `actor:<id>`.
    assert [(row["count"], row["target"]["name"], row["target"]["type"]) for row in by_target] == [
        (23, "Environment", "NPC"),
        (16, "Rotmire", "NPC"),
        (1, "Nicksdruid", "Player"),
    ]


def test_captured_casts_tally_real_sources_and_abilities(captured_casts: dict[str, Any]) -> None:
    casts = captured_casts["casts"]
    top_source = casts["by_source"][0]
    assert (top_source["count"], top_source["source"]["name"], top_source["source"]["sub_type"]) == (
        6,
        "Peepiceek",
        "DemonHunter",
    )
    top_ability = casts["by_ability"][0]
    assert (top_ability["count"], top_ability["ability"]["name"]) == (7, "Soul Fragment")
    assert sum(row["count"] for row in casts["by_ability"]) == casts["event_count"]

    first_preview = casts["preview"][0]
    assert first_preview["relative_time_ms"] == 31.0
    assert first_preview["source"]["identity_contract"]["identity"]["local_key"] == "DZzR9jwYmQA6tbV7:4:15"

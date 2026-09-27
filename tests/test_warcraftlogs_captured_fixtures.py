"""Parser tests against captured Warcraft Logs GraphQL responses.

The fixtures in ``tests/fixtures/warcraftlogs/`` are real (trimmed) GraphQL responses (each file's
``_capture`` block names its source), so these assertions pin the cast and aura parsers to data
Warcraft Logs actually returns rather than to hand-written shapes. See
``docs/architecture/FIXTURE_MAINTENANCE.md``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from warcraftlogs_cli.main import (
    _ability_cast_summary,
    _aura_compare_rows,
    _aura_summary_rows,
    _encounter_cast_rows_payload,
    _report_encounter_aura_summary_payload,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "warcraftlogs"
FIXTURE_PATH = FIXTURE_DIR / "report_encounter_casts_capture.json"


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
    assert casts["cast_count"] == 36
    assert casts["truncated"] is True
    assert casts["next_page_timestamp"] == 213337
    # The note names the counted casts, not the raw page size that includes begincast events.
    assert any("only the 36 casts in the first 40 events" in note for note in captured_casts["notes"])


def test_captured_casts_name_every_real_target(captured_casts: dict[str, Any]) -> None:
    by_target = captured_casts["casts"]["by_target"]
    # Environment (-1) and the boss NPC are named from master data, not left as `actor:<id>`.
    assert [(row["count"], row["target"]["name"], row["target"]["type"]) for row in by_target] == [
        (19, "Environment", "NPC"),
        (16, "Rotmire", "NPC"),
        (1, "Nicksdruid", "Player"),
    ]


def test_captured_casts_tally_real_sources_and_abilities(captured_casts: dict[str, Any]) -> None:
    casts = captured_casts["casts"]
    top_source = casts["by_source"][0]
    # Peepiceek and Infiammato both cast 5 times; the tie sorts by name.
    assert (top_source["count"], top_source["source"]["name"], top_source["source"]["sub_type"]) == (
        5,
        "Infiammato",
        "Mage",
    )
    top_ability = casts["by_ability"][0]
    assert (top_ability["count"], top_ability["ability"]["name"]) == (7, "Soul Fragment")
    assert sum(row["count"] for row in casts["by_ability"]) == casts["cast_count"]

    first_preview = casts["preview"][0]
    assert first_preview["relative_time_ms"] == 31.0
    assert first_preview["source"]["identity_contract"]["identity"]["local_key"] == "DZzR9jwYmQA6tbV7:4:15"


def test_captured_begincast_events_are_not_counted_as_casts(captured_casts: dict[str, Any]) -> None:
    casts = captured_casts["casts"]
    # The page holds 4 begincast events, each paired with the `cast` event of the same use
    # (abilities 473662 and 104316, two uses each). Counting both would double every use.
    by_ability = {row["ability"]["game_id"]: row["count"] for row in casts["by_ability"]}
    assert (by_ability[473662], by_ability[104316]) == (2, 2)
    assert {row["type"] for row in casts["preview"]} == {"cast"}


def test_captured_empowered_spell_counts_one_cast_per_press() -> None:
    fixture = json.loads((FIXTURE_DIR / "ability_events_empowered_capture.json").read_text(encoding="utf-8"))
    summary = _ability_cast_summary(
        fixture["events_report"], actor_index={}, report_code="JVFTxcKCqrvpaAzD", fight_id=4
    )
    # 12 events are 4 presses of Dream Breath, each an empowerstart + cast + empowerend triple.
    assert summary["count"] == 4
    assert [(row["source"]["id"], row["count"]) for row in summary["sources"]] == [(14, 2), (20, 2)]


def test_captured_buffs_tables_give_real_aura_compare_deltas() -> None:
    fixture = json.loads((FIXTURE_DIR / "report_encounter_aura_buffs_capture.json").read_text(encoding="utf-8"))

    def window_rows(table_key: str) -> list[dict[str, Any]]:
        summary = _report_encounter_aura_summary_payload(
            report=fixture["left_table_report"],
            fight=fixture["fight"],
            table_report=fixture[table_key],
            master_report=fixture["master_report"],
            ability_id=390386,
            include_raw=False,
        )
        return _aura_summary_rows(summary)

    rows = _aura_compare_rows(left_rows=window_rows("left_table_report"), right_rows=window_rows("right_table_report"))

    # The real Buffs table carries totalUptime/totalUses per source, not total/activeTime.
    assert [
        (row["source"]["name"], row["left_reported_total_uptime"], row["right_reported_total_uptime"],
         row["reported_total_uptime_delta"], row["reported_total_uses_delta"])
        for row in rows
    ] == [("Augvoker", 8054, 59976, 51922, 8)]

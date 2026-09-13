from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner
from warcraft_cli.main import app
from warcraftlogs_cli.client import load_warcraftlogs_auth_config

runner = CliRunner()

pytestmark = pytest.mark.live

EXAMPLE_REPORT_URL = "https://www.warcraftlogs.com/reports/bG3xDYPqKjLm8XaR?fight=22&type=damage-done"
EXAMPLE_REPORT_CODE = "bG3xDYPqKjLm8XaR"


def _require_warcraftlogs_client_auth() -> None:
    if not load_warcraftlogs_auth_config().configured:
        pytest.skip("Warcraft Logs client credentials are not configured.")


def _assert_casts_in_phase(casts: list[dict], phase: dict) -> None:
    """Every returned cast names a real spell and falls inside the phase window it was selected for."""
    assert casts, "expected at least one cast row"
    for cast in casts:
        spell_id = cast["spell"]["spell_id"]
        assert isinstance(spell_id, int), f"spell_id {spell_id!r} is not an integer"
        assert cast["spell"]["name"], f"spell {spell_id} has no name"
        assert phase["start_ms"] <= cast["timestamp_ms"] < phase["end_ms"]


def test_live_cooldown_packet_example_report_phase_two_e2e() -> None:
    _require_warcraftlogs_client_auth()

    result = runner.invoke(
        app,
        [
            "cooldown-packet",
            EXAMPLE_REPORT_URL,
            "--actor-id",
            "89",
            "--phase",
            "2",
            "--sample-limit",
            "1",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)

    assert payload["ok"] is True
    assert payload["provider"] == "warcraft"
    assert payload["kind"] == "cooldown_packet"
    assert payload["query"]["report_code"] == EXAMPLE_REPORT_CODE
    assert payload["query"]["fight_id"] == 22
    assert payload["query"]["report_type"] == "damage-done"
    assert payload["query"]["actor_id"] == 89
    assert payload["query"]["actor_name"] == "Buikia"
    assert payload["query"]["spec_slug"] == "warrior-protection"
    assert payload["query"]["boss_slug"] == "lura"

    # The report's own numbers (phase boundaries, which cooldowns the player pressed) are Warcraft
    # Logs data, not a contract: assert the invariants the packet has to satisfy instead of pinning
    # one raid night's milliseconds and spell order.
    selected_phase = payload["phase"]["selected"]
    assert selected_phase["phase"] == 2
    assert selected_phase["label"] == "P2"
    assert isinstance(selected_phase["start_ms"], int)
    assert isinstance(selected_phase["end_ms"], int)
    assert selected_phase["start_ms"] < selected_phase["end_ms"]

    player = payload["player"]
    assert player["name"] == "Buikia"
    assert player["source_id"] == 89
    assert player["spec_slug"] == "warrior-protection"
    assert player["class_slug"] == "warrior"

    player_casts = payload["cooldowns"]["player_casts"]
    selected_casts = player_casts["selected_phase_casts"]
    assert player_casts["raw_event_count"] >= player_casts["tracked_cast_count"]
    assert player_casts["tracked_cast_count"] >= player_casts["selected_phase_cast_count"]
    assert player_casts["selected_phase_cast_count"] == len(selected_casts)
    assert selected_casts, "expected at least one tracked cooldown inside the selected phase"
    _assert_casts_in_phase(selected_casts, selected_phase)

    comparison = payload["comparison"]
    assert comparison["status"] == "ready"
    assert comparison["sample_count"] == 1
    sample = comparison["samples"][0]
    assert isinstance(sample["phase_available"], bool)
    if sample["phase_available"]:
        _assert_casts_in_phase(sample["selected_phase_casts"], sample["phase_window"])

    source_expectations = {
        "lorrgs_user_report_fights": "lorrgs",
        "lorrgs_spec_spells": "lorrgs",
        "lorrgs_boss_spells": "lorrgs",
        "lorrgs_spec_ranking": "lorrgs",
        "warcraftlogs_report_fights": "warcraftlogs",
        "warcraftlogs_report_events": "warcraftlogs",
    }
    for source_key, provider in source_expectations.items():
        source = payload["sources"][source_key]
        assert source["status"] == "ok"
        assert source["provider"] == provider
        assert source["command"].startswith(f"warcraft {provider}")

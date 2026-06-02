from __future__ import annotations

from pathlib import Path

from warcraftlogs_cli.payload_envelope import apply_payload_envelope, documented_payload_keys
from warcraftlogs_cli.payload_keys_registry import ALL_COMMANDS


def test_documented_payload_keys_cover_all_registry_commands() -> None:
    documented = {row[0] for row in documented_payload_keys()}
    assert documented == set(ALL_COMMANDS)


def test_apply_payload_envelope_dual_emits_boss_kills() -> None:
    payload = {
        "ok": True,
        "provider": "warcraftlogs",
        "kind": "boss_kills",
        "kills": [{"id": 1}],
    }
    out = apply_payload_envelope("boss-kills", payload)
    assert out["command"] == "boss-kills"
    assert out["boss_kills"] == [{"id": 1}]
    assert out["kills"] == [{"id": 1}]
    assert "kills" in out["deprecated_keys"]


def test_apply_payload_envelope_dual_emits_spec_kill_samples() -> None:
    payload = {
        "ok": True,
        "provider": "warcraftlogs",
        "kind": "spec_filtered_kill_samples",
        "kills": [{"id": 1}],
    }
    out = apply_payload_envelope("spec-kill-samples", payload)
    assert out["command"] == "spec-kill-samples"
    assert out["spec_kill_samples"] == [{"id": 1}]
    assert out["kills"] == [{"id": 1}]
    assert "kills" in out["deprecated_keys"]


def test_apply_payload_envelope_encounter_rankings() -> None:
    payload = {
        "ok": True,
        "provider": "warcraftlogs",
        "rankings": {"rows": [], "count": 0},
    }
    out = apply_payload_envelope("encounter-rankings", payload)
    assert out["encounter_rankings"] == {"rows": [], "count": 0}
    assert out["rankings"]["rows"] == []
    assert "rankings" in out["deprecated_keys"]


def test_apply_payload_envelope_report_encounter_buffs_includes_scope() -> None:
    payload = {
        "ok": True,
        "provider": "warcraftlogs",
        "kind": "report_encounter_buffs",
        "encounter": {"id": 1},
        "buffs": {"total": 2, "preview": []},
    }
    out = apply_payload_envelope("report-encounter-buffs", payload)
    assert out["report_encounter_buffs"]["buffs"]["total"] == 2
    assert out["report_encounter_buffs"]["encounter"]["id"] == 1
    assert out["buffs"]["total"] == 2
    assert "buffs" in out["deprecated_keys"]


def test_payload_keys_doc_exists_and_matches_registry() -> None:
    doc_path = Path(__file__).resolve().parents[1] / "docs" / "warcraftlogs" / "PAYLOAD_KEYS.md"
    assert doc_path.exists()
    text = doc_path.read_text(encoding="utf-8")
    for command in ALL_COMMANDS:
        assert f"`{command}`" in text

from __future__ import annotations

import json
from typing import Any

from typer.testing import CliRunner
from warcraft_cli.crosswalk import (
    actor_lookup_identity,
    actor_spec_ambiguous,
    distinct_actor_targets,
    find_report_actors,
    reconcile_class_spec,
    report_actor_names,
)
from warcraft_cli.main import app as warcraft_app
from warcraft_core.identity import class_spec_identity_payload, report_actor_identity_payload

runner = CliRunner()


def _wcl_actor(name: str, server: str | None, region: str | None, actor_class: str, spec: str) -> dict[str, Any]:
    # Mirrors the shape emitted by warcraftlogs `report-player-details` (_player_detail_actor_payload),
    # building the identity blocks with the real shared helpers so reconciliation sees authentic data.
    return {
        "name": name,
        "id": 1,
        "server": server,
        "region": region,
        "specs": [{"spec": spec, "count": 1}],
        "class_spec_identity": class_spec_identity_payload(
            actor_class=actor_class,
            spec=spec,
            provider="warcraftlogs",
            source="report_player_details",
        ),
        "identity_contract": report_actor_identity_payload(
            report_code="ABC123",
            fight_id=1,
            actor_id=1,
            name=name,
            actor_class=actor_class,
            spec=spec,
            provider="warcraftlogs",
            source="report_player_details",
        ),
    }


def _wcl_actor_multispec(name: str, server: str | None, region: str | None, actor_class: str, specs: list[str]) -> dict[str, Any]:
    # A single aggregated row for a player who swapped specs -> WCL marks class_spec_identity ambiguous.
    return {
        "name": name,
        "id": 1,
        "server": server,
        "region": region,
        "specs": [{"spec": spec, "count": 1} for spec in specs],
        "class_spec_identity": class_spec_identity_payload(
            actor_class=actor_class,
            spec=None,
            provider="warcraftlogs",
            source="report_player_details",
            candidates=[(actor_class, spec) for spec in specs],
        ),
        "identity_contract": report_actor_identity_payload(
            report_code="ABC123",
            fight_id=None,
            actor_id=1,
            name=name,
            actor_class=actor_class,
            spec=None,
            provider="warcraftlogs",
            source="report_player_details",
        ),
    }


def _wcl_payload(roles_in: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    roles: dict[str, list[dict[str, Any]]] = {"tanks": [], "healers": [], "dps": []}
    roles.update(roles_in)
    counts = {role: len(rows) for role, rows in roles.items()}
    counts["total"] = sum(len(rows) for rows in roles.values())
    return {"report": {"code": "ABC123"}, "player_details": {"counts": counts, "roles": roles}}


def _raiderio_payload(name: str, actor_class: str, spec: str, *, region: str = "us", realm: str = "illidan") -> dict[str, Any]:
    return {
        "character": {
            "name": name,
            "region": region,
            "realm": realm,
            "class_name": actor_class,
            "active_spec_name": spec,
            "class_spec_identity": class_spec_identity_payload(
                actor_class=actor_class,
                spec=spec,
                provider="raiderio",
                source="character_profile",
                confidence="high",
            ),
            "profile_url": f"https://raider.io/characters/{region}/{realm}/{name}",
        }
    }


def _invoke(wcl_payload: dict[str, Any] | None, raiderio_payload: dict[str, Any] | None, *, raiderio_exit: int = 0):
    def fake(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, Any]:
        if args[0] == "report-player-details":
            return {"provider": "warcraftlogs", "exit_code": 0, "payload": wcl_payload, "stdout": ""}
        if args[0] == "character":
            return {"provider": "raiderio", "exit_code": raiderio_exit, "payload": raiderio_payload, "stdout": ""}
        raise AssertionError(f"unexpected provider invocation: {provider} {args}")

    return fake


# --- pure-helper unit tests -------------------------------------------------


def test_reconcile_class_spec_agreement_and_mismatch() -> None:
    log = class_spec_identity_payload(actor_class="Rogue", spec="Subtlety", provider="warcraftlogs")
    same = class_spec_identity_payload(actor_class="Rogue", spec="Subtlety", provider="raiderio")
    assert reconcile_class_spec(log, same) == {
        "comparable": True,
        "agree": True,
        "class_agree": True,
        "spec_agree": True,
        "reasons": [],
    }
    diff_spec = class_spec_identity_payload(actor_class="Rogue", spec="Assassination", provider="raiderio")
    rec = reconcile_class_spec(log, diff_spec)
    assert rec["class_agree"] is True
    assert rec["spec_agree"] is False
    assert rec["agree"] is False
    assert rec["reasons"] == ["spec_mismatch"]


def test_reconcile_class_spec_not_comparable_when_a_side_missing() -> None:
    log = class_spec_identity_payload(actor_class="Rogue", spec="Subtlety", provider="warcraftlogs")
    none_side = reconcile_class_spec(log, None)
    assert none_side["comparable"] is False
    assert none_side["agree"] is None
    assert none_side["class_agree"] is None and none_side["spec_agree"] is None
    # Profile gives a spec but no class -> only the spec field is comparable, so no full-match claim.
    spec_only = class_spec_identity_payload(actor_class=None, spec="Subtlety", provider="raiderio")
    partial = reconcile_class_spec(log, spec_only)
    assert partial["class_agree"] is None
    assert partial["spec_agree"] is True
    assert partial["comparable"] is True
    assert partial["agree"] is None


def test_find_report_actors_matches_case_insensitively_across_roles() -> None:
    payload = _wcl_payload(
        {
            "dps": [_wcl_actor("Roguecane", "Illidan", "us", "Rogue", "Subtlety")],
            "healers": [_wcl_actor("Healz", "Illidan", "us", "Priest", "Holy")],
        }
    )
    found = find_report_actors(payload, "healz")
    assert len(found) == 1
    assert found[0]["name"] == "Healz"
    assert found[0]["role"] == "healers"
    assert find_report_actors(payload, "nobody") == []
    assert sorted(report_actor_names(payload)) == ["Healz", "Roguecane"]


def test_distinct_actor_targets_collapses_same_realm_and_flags_different_realms() -> None:
    same_character = [
        _wcl_actor("Dup", "Illidan", "us", "Rogue", "Subtlety"),
        _wcl_actor("Dup", "Illidan", "us", "Rogue", "Assassination"),
    ]
    assert len(distinct_actor_targets(same_character)) == 1
    different_realms = [
        _wcl_actor("Dup", "Illidan", "us", "Rogue", "Subtlety"),
        _wcl_actor("Dup", "Area-52", "us", "Mage", "Frost"),
    ]
    targets = distinct_actor_targets(different_realms)
    assert len(targets) == 2
    assert {t["server"] for t in targets} == {"Illidan", "Area-52"}


def test_actor_spec_ambiguous_detects_multi_row_and_single_row_cases() -> None:
    single = [_wcl_actor("Solo", "Illidan", "us", "Rogue", "Subtlety")]
    assert actor_spec_ambiguous(single) is False
    multi_row = [
        _wcl_actor("Flex", "Illidan", "us", "Druid", "Guardian"),
        _wcl_actor("Flex", "Illidan", "us", "Druid", "Balance"),
    ]
    assert actor_spec_ambiguous(multi_row) is True
    single_row_multi_spec = [_wcl_actor_multispec("Flex", "Illidan", "us", "Druid", ["Guardian", "Balance"])]
    assert actor_spec_ambiguous(single_row_multi_spec) is True


def test_actor_lookup_identity_reports_missing_field_and_honors_override() -> None:
    actor = {"name": "Roguecane", "server": "Illidan", "region": None}
    assert actor_lookup_identity(actor) == {"ok": False, "missing": "region"}
    assert actor_lookup_identity(actor, region_override="us") == {
        "ok": True,
        "identity": {"region": "us", "realm": "illidan", "name": "Roguecane"},
    }
    assert actor_lookup_identity({"name": "X", "server": None, "region": "us"}) == {"ok": False, "missing": "realm"}
    assert actor_lookup_identity({"name": None, "server": "Illidan", "region": "us"}) == {"ok": False, "missing": "name"}
    # An explicit empty override must be honored (and rejected), not silently replaced by the actor region.
    assert actor_lookup_identity({"name": "X", "server": "Illidan", "region": "us"}, region_override="") == {"ok": False, "missing": "region"}


# --- command integration tests ----------------------------------------------


def test_actor_profile_reconciles_matching_log_and_profile(monkeypatch) -> None:
    wcl = _wcl_payload({"dps": [_wcl_actor("Roguecane", "Illidan", "us", "Rogue", "Subtlety")]})
    rio = _raiderio_payload("Roguecane", "Rogue", "Subtlety")
    monkeypatch.setattr("warcraft_cli.main.provider_invoke", _invoke(wcl, rio))

    result = runner.invoke(warcraft_app, ["actor-profile", "ABC123", "Roguecane"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["kind"] == "actor_profile_crosswalk"
    assert "not a canonical" in payload["join_rule"]
    assert payload["query"] == {
        "report_code": "ABC123",
        "actor_name": "Roguecane",
        "fight_id": None,
        "region": "us",
        "realm": "illidan",
        "name": "Roguecane",
    }
    wcl_side = payload["sources"]["warcraftlogs"]
    assert wcl_side["role"] == "dps"
    assert wcl_side["class_spec_identity"]["identity"] == {"actor_class": "rogue", "spec": "subtlety"}
    assert payload["sources"]["raiderio"]["status"] == "ok"
    assert payload["reconciliation"] == {
        "comparable": True,
        "agree": True,
        "class_agree": True,
        "spec_agree": True,
        "reasons": [],
    }


def test_actor_profile_flags_class_and_spec_mismatch(monkeypatch) -> None:
    wcl = _wcl_payload({"dps": [_wcl_actor("Roguecane", "Illidan", "us", "Rogue", "Subtlety")]})
    rio = _raiderio_payload("Roguecane", "Mage", "Frost")
    monkeypatch.setattr("warcraft_cli.main.provider_invoke", _invoke(wcl, rio))

    result = runner.invoke(warcraft_app, ["actor-profile", "ABC123", "Roguecane"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    rec = payload["reconciliation"]
    assert rec["agree"] is False
    assert set(rec["reasons"]) == {"class_mismatch", "spec_mismatch"}


def test_actor_profile_region_override_drives_lookup(monkeypatch) -> None:
    wcl = _wcl_payload({"healers": [_wcl_actor("Healz", "Tarren Mill", None, "Priest", "Holy")]})
    rio = _raiderio_payload("Healz", "Priest", "Holy", region="eu", realm="tarren-mill")
    seen: dict[str, Any] = {}

    def fake(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, Any]:
        if args[0] == "report-player-details":
            return {"provider": "warcraftlogs", "exit_code": 0, "payload": wcl, "stdout": ""}
        seen["character_args"] = args
        return {"provider": "raiderio", "exit_code": 0, "payload": rio, "stdout": ""}

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake)
    result = runner.invoke(warcraft_app, ["actor-profile", "ABC123", "Healz", "--region", "eu"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert seen["character_args"] == ["character", "eu", "tarren-mill", "Healz"]
    assert payload["query"]["region"] == "eu"
    assert payload["reconciliation"]["agree"] is True


def test_actor_profile_rejects_ambiguous_actor(monkeypatch) -> None:
    wcl = _wcl_payload(
        {
            "dps": [_wcl_actor("Dup", "Illidan", "us", "Rogue", "Subtlety")],
            "healers": [_wcl_actor("Dup", "Area-52", "us", "Priest", "Holy")],
        }
    )
    monkeypatch.setattr("warcraft_cli.main.provider_invoke", _invoke(wcl, None))

    result = runner.invoke(warcraft_app, ["actor-profile", "ABC123", "Dup"])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "ambiguous_actor"
    assert len(payload["error"]["candidates"]) == 2


def test_actor_profile_rejects_multi_spec_actor(monkeypatch) -> None:
    # Same character (same realm) listed in two role buckets with different specs across fights.
    wcl = _wcl_payload(
        {
            "tanks": [_wcl_actor("Flex", "Illidan", "us", "Druid", "Guardian")],
            "dps": [_wcl_actor("Flex", "Illidan", "us", "Druid", "Balance")],
        }
    )
    monkeypatch.setattr("warcraft_cli.main.provider_invoke", _invoke(wcl, None))

    result = runner.invoke(warcraft_app, ["actor-profile", "ABC123", "Flex"])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "ambiguous_actor_spec"


def test_actor_profile_forwards_allow_unlisted(monkeypatch) -> None:
    wcl = _wcl_payload({"dps": [_wcl_actor("Roguecane", "Illidan", "us", "Rogue", "Subtlety")]})
    rio = _raiderio_payload("Roguecane", "Rogue", "Subtlety")
    seen: dict[str, Any] = {}

    def fake(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, Any]:
        if args[0] == "report-player-details":
            seen["wcl_args"] = args
            return {"provider": "warcraftlogs", "exit_code": 0, "payload": wcl, "stdout": ""}
        return {"provider": "raiderio", "exit_code": 0, "payload": rio, "stdout": ""}

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake)
    result = runner.invoke(warcraft_app, ["actor-profile", "ABC123", "Roguecane", "--allow-unlisted"])
    assert result.exit_code == 0
    assert "--allow-unlisted" in seen["wcl_args"]


def test_actor_profile_rejects_single_row_multi_spec_actor(monkeypatch) -> None:
    wcl = _wcl_payload({"dps": [_wcl_actor_multispec("Flex", "Illidan", "us", "Druid", ["Guardian", "Balance"])]})
    monkeypatch.setattr("warcraft_cli.main.provider_invoke", _invoke(wcl, None))

    result = runner.invoke(warcraft_app, ["actor-profile", "ABC123", "Flex"])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "ambiguous_actor_spec"


def test_actor_profile_errors_when_region_unknown(monkeypatch) -> None:
    wcl = _wcl_payload({"dps": [_wcl_actor("Roguecane", "Illidan", None, "Rogue", "Subtlety")]})
    monkeypatch.setattr("warcraft_cli.main.provider_invoke", _invoke(wcl, None))

    result = runner.invoke(warcraft_app, ["actor-profile", "ABC123", "Roguecane"])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "actor_region_unknown"
    assert payload["error"]["missing_field"] == "region"
    # The resolved log side is still surfaced for context even though the lookup could not run.
    assert payload["sources"]["warcraftlogs"]["class_spec_identity"]["identity"]["actor_class"] == "rogue"


def test_actor_profile_errors_when_profile_lookup_fails(monkeypatch) -> None:
    wcl = _wcl_payload({"dps": [_wcl_actor("Roguecane", "Illidan", "us", "Rogue", "Subtlety")]})

    def fake(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, Any]:
        if args[0] == "report-player-details":
            return {"provider": "warcraftlogs", "exit_code": 0, "payload": wcl, "stdout": ""}
        return {
            "provider": "raiderio",
            "exit_code": 1,
            "payload": {"ok": False, "error": {"code": "character_not_found"}},
            "stdout": "",
        }

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake)
    result = runner.invoke(warcraft_app, ["actor-profile", "ABC123", "Roguecane"])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "profile_lookup_failed"
    assert payload["error"]["source"]["code"] == "character_not_found"


def test_actor_profile_errors_when_actor_absent(monkeypatch) -> None:
    wcl = _wcl_payload({"dps": [_wcl_actor("Someoneelse", "Illidan", "us", "Rogue", "Subtlety")]})
    monkeypatch.setattr("warcraft_cli.main.provider_invoke", _invoke(wcl, None))

    result = runner.invoke(warcraft_app, ["actor-profile", "ABC123", "Roguecane"])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "actor_not_found"
    assert payload["error"]["available_actors"] == ["Someoneelse"]


def test_actor_profile_errors_when_warcraftlogs_lookup_fails(monkeypatch) -> None:
    def fake(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, Any]:
        return {
            "provider": "warcraftlogs",
            "exit_code": 1,
            "payload": {"ok": False, "error": {"code": "auth_required", "message": "credentials missing"}},
            "stdout": "",
        }

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake)
    result = runner.invoke(warcraft_app, ["actor-profile", "ABC123", "Roguecane"])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "warcraftlogs_lookup_failed"
    assert payload["error"]["source"]["code"] == "auth_required"

"""End-to-end journeys for the ``warcraft`` wrapper's log-based composites.

These are the cross-provider handoffs that only exist in the wrapper: a Warcraft Logs fight plus
Lorrgs top parses (``cooldown-packet``), a Warcraft Logs report actor plus SimulationCraft
(``talent-packet`` / ``talent-describe``), a Warcraft Logs report actor plus Raider.IO
(``actor-profile``), and the ``warcraft warcraftlogs`` passthrough.

Inputs are discovered at run time and reuse the Warcraft Logs discovery chain in
``tests/e2e/test_warcraftlogs.py``: the current raid tier, the pinned guild's most recent kill in
it, and that kill's roster. Lorrgs only serves fights from reports it has already cached, so the
cooldown journey walks the Lorrgs spec ranking for the discovered boss until it finds one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from tests.e2e.harness import EXIT_GENERIC, JourneyFailure, Result, payload_or_legacy, run
from tests.e2e.test_warcraftlogs import anchor, current_raid_zone

# How far discovery walks the Lorrgs ranking before giving up on a cached report.
LORRGS_SPEC_ATTEMPTS = 4
LORRGS_REPORT_ATTEMPTS = 5


def _report_url(code: str, fight_id: int) -> str:
    return f"https://www.warcraftlogs.com/reports/{code}#fight={fight_id}"


def _lorrgs_spec_slug(player: dict[str, Any]) -> str | None:
    specs = player.get("specs") or []
    if not specs or not isinstance(specs[0].get("spec"), str):
        return None
    return f"{str(player['type']).lower()}-{str(specs[0]['spec']).lower()}"


@lru_cache(maxsize=1)
def lorrgs_spec_slugs() -> frozenset[str]:
    result = run("lorrgs", "specs")
    return frozenset(str(row["full_name_slug"]) for row in payload_or_legacy(result, "specs"))


@lru_cache(maxsize=1)
def lorrgs_boss_slugs() -> dict[int, str]:
    result = run("lorrgs", "bosses")
    return {int(row["id"]): str(row["full_name_slug"]) for row in payload_or_legacy(result, "bosses")}


@dataclass(frozen=True)
class LorrgsTarget:
    """A report fight Lorrgs has already cached, plus one player inside it."""

    boss_slug: str
    code: str
    fight_id: int
    actor_id: int
    actor_name: str
    spec_slug: str

    @property
    def url(self) -> str:
        return _report_url(self.code, self.fight_id)


@lru_cache(maxsize=1)
def lorrgs_target() -> LorrgsTarget:
    """A Lorrgs-cached fight on a boss from the discovered raid tier."""
    boss_slugs = lorrgs_boss_slugs()
    anchor_boss = int(anchor().fight["encounter_id"])
    boss_ids = [anchor_boss] + [
        int(row["id"]) for row in current_raid_zone()["encounters"] if int(row["id"]) != anchor_boss
    ]
    candidate_specs = [
        slug
        for slug in dict.fromkeys(filter(None, (_lorrgs_spec_slug(player) for player in anchor().players)))
        if slug in lorrgs_spec_slugs()
    ]
    if not candidate_specs:
        raise JourneyFailure(f"no roster spec maps to a Lorrgs spec slug: {[p.get('type') for p in anchor().players]}")

    for boss_id in boss_ids:
        boss_slug = boss_slugs.get(boss_id)
        if boss_slug is None:
            continue
        for spec_slug in candidate_specs[:LORRGS_SPEC_ATTEMPTS]:
            ranking = run("lorrgs", "spec-ranking", spec_slug, boss_slug)
            for report in (payload_or_legacy(ranking, "reports") or [])[:LORRGS_REPORT_ATTEMPTS]:
                fights = report.get("fights") or []
                if not fights:
                    continue
                fight_id = int(fights[0]["fight_id"])
                cached = run("lorrgs", "user-report-fights", str(report["report_id"]), "--fight", str(fight_id))
                players = ((payload_or_legacy(cached, "fights") or [{}])[0]).get("players") or []
                if not players:
                    continue
                player = players[0]
                return LorrgsTarget(
                    boss_slug=boss_slug,
                    code=str(report["report_id"]),
                    fight_id=fight_id,
                    actor_id=int(player["source_id"]),
                    actor_name=str(player["name"]),
                    spec_slug=str(player["spec_slug"]),
                )
    raise JourneyFailure(
        f"Lorrgs has no cached fight for {current_raid_zone()['name']!r} across specs {candidate_specs[:LORRGS_SPEC_ATTEMPTS]}"
    )


@lru_cache(maxsize=1)
def simc_apl_root() -> Path:
    """The default action-priority-list directory of the local SimulationCraft checkout."""
    result = run("simc", "repo")
    root = Path(str(result.data["resolution"]["root"]))
    apl_root = root / "ActionPriorityLists" / "default"
    if not apl_root.is_dir():
        raise JourneyFailure(f"{apl_root} is missing; the local SimulationCraft checkout has no default APLs")
    return apl_root


@lru_cache(maxsize=1)
def talent_actor() -> tuple[dict[str, Any], Path]:
    """A roster actor from the anchor kill whose class/spec has a SimulationCraft APL."""
    apls = {path.stem.replace("_", ""): path for path in simc_apl_root().glob("*.simc")}
    for player in anchor().players:
        slug = _lorrgs_spec_slug(player)
        if slug is None:
            continue
        path = apls.get(slug.replace("-", ""))
        if path is not None:
            return player, path
    raise JourneyFailure(f"no roster actor has a SimulationCraft APL: {sorted(apls)}")


@lru_cache(maxsize=1)
def talent_packet() -> Result:
    actor, _apl = talent_actor()
    return run(
        "warcraft",
        "talent-packet",
        anchor().url,
        "--actor-id",
        str(actor["id"]),
        "--fight-id",
        str(anchor().fight_id),
    )


def test_cooldown_packet_joins_a_report_fight_to_lorrgs_top_parses(require):
    require("warcraftlogs", "lorrgs")
    target = lorrgs_target()

    result = run(
        "warcraft",
        "cooldown-packet",
        target.url,
        "--actor-id",
        str(target.actor_id),
        "--phase",
        "1",
        "--sample-limit",
        "2",
    )
    assert result.payload["kind"] == "cooldown_packet", result.describe()
    query = result.payload["query"]
    assert query["report_code"] == target.code, result.describe()
    assert query["fight_id"] == target.fight_id, result.describe()
    assert query["actor_id"] == target.actor_id, result.describe()
    assert query["actor_name"] == target.actor_name, result.describe()
    assert query["spec_slug"] == target.spec_slug, result.describe()
    assert query["boss_slug"] == target.boss_slug, result.describe()

    data = result.data
    # Phase bounds come from Lorrgs/Warcraft Logs transition markers, so assert the invariants
    # rather than one raid night's milliseconds.
    selected = data["phase"]["selected"]
    assert selected["phase"] == 1, result.describe()
    assert selected["label"] == "P1", result.describe()
    assert selected["start_ms"] < selected["end_ms"], result.describe()
    assert selected["duration_ms"] == selected["end_ms"] - selected["start_ms"], result.describe()
    assert data["phase"]["windows"][0] == selected, result.describe()

    player = data["player"]
    assert player["name"] == target.actor_name, result.describe()
    assert player["source_id"] == target.actor_id, result.describe()
    assert player["spec_slug"] == target.spec_slug, result.describe()
    assert target.spec_slug.startswith(player["class_slug"]), result.describe()

    cooldowns = data["cooldowns"]
    assert cooldowns["tracked_spell_count"] == len(cooldowns["tracked_spells"]), result.describe()
    assert cooldowns["tracked_spells"], result.describe()
    casts = cooldowns["player_casts"]
    assert casts["raw_event_count"] >= casts["tracked_cast_count"] >= casts["selected_phase_cast_count"], result.describe()
    assert casts["selected_phase_cast_count"] == len(casts["selected_phase_casts"]), result.describe()
    assert casts["selected_phase_casts"], "the ranked player pressed no tracked cooldown in P1"
    for cast in casts["selected_phase_casts"]:
        assert isinstance(cast["spell"]["spell_id"], int), result.describe()
        assert cast["spell"]["name"], result.describe()
        assert selected["start_ms"] <= cast["timestamp_ms"] < selected["end_ms"], result.describe()

    assert data["boss"]["boss_slug"] == target.boss_slug, result.describe()

    expected_sources = {
        "lorrgs_user_report_fights": "lorrgs",
        "lorrgs_spec_spells": "lorrgs",
        "lorrgs_boss_spells": "lorrgs",
        "lorrgs_spec_ranking": "lorrgs",
        "warcraftlogs_report_fights": "warcraftlogs",
        "warcraftlogs_report_events": "warcraftlogs",
    }
    assert set(data["sources"]) == set(expected_sources), result.describe()
    for key, provider in expected_sources.items():
        source = data["sources"][key]
        assert source["status"] == "ok", result.describe()
        assert source["provider"] == provider, result.describe()
        assert source["command"].startswith(f"warcraft {provider} "), result.describe()

    comparison = data["comparison"]
    assert comparison["status"] == "ready", result.describe()
    assert comparison["sample_count"] <= 2, result.describe()
    assert comparison["selected_phase_spell_frequency"], result.describe()
    assert data["notes"], "the packet must say where its phase windows and samples come from"


def test_talent_packet_routes_a_report_actor_through_simc(require):
    require("warcraftlogs", "simc")
    actor, _apl = talent_actor()
    result = talent_packet()

    assert result.payload["kind"] == "talent_transport", result.describe()
    assert result.data["route"] == {
        "kind": "warcraftlogs_report_actor",
        "provider": "warcraftlogs",
        "actor_id": actor["id"],
        "fight_id": anchor().fight_id,
        "allow_unlisted": False,
    }, result.describe()
    assert result.data["source_packet_status"] == "raw_only", result.describe()
    assert result.data["upgrade_attempted"] is True, result.describe()

    packet = result.data["talent_transport_packet"]
    assert packet["scope"] == {
        "type": "report_fight_actor",
        "report_code": anchor().code,
        "fight_id": anchor().fight_id,
        "actor_id": actor["id"],
    }, result.describe()
    identity = packet["build_identity"]["class_spec_identity"]["identity"]
    assert identity["actor_class"] == str(actor["type"]).lower(), result.describe()
    assert identity["spec"] == str(actor["specs"][0]["spec"]).lower(), result.describe()

    validation = packet["validation"]
    # simc resolved the raw combatant-info entries against its own trait data. When every entry
    # resolves the packet is upgraded to a validated transport form; when the local SimulationCraft
    # checkout predates a talent the packet stays raw_only and says which entries it could not map.
    assert validation["status"] in {"validated", "not_validated"}, result.describe()
    assert validation["resolved_entries"], result.describe()
    assert all(entry["token"] and entry["tree"] for entry in validation["resolved_entries"]), result.describe()
    if packet["transport_status"] == "validated":
        assert result.data["upgraded"] is True, result.describe()
        assert packet["transport_forms"], result.describe()
    else:
        assert validation["reason"] == "simc_trait_resolution_incomplete", result.describe()
        assert validation["unresolved_entries"], result.describe()


def test_talent_describe_adds_simc_priority_output_for_the_report_build(require, out_dir):
    require("warcraftlogs", "simc")
    actor, apl_path = talent_actor()
    packet_out = out_dir / "talent-packet.json"
    args = (
        anchor().url,
        "--actor-id",
        str(actor["id"]),
        "--fight-id",
        str(anchor().fight_id),
        "--apl-path",
        str(apl_path),
        "--packet-out",
        str(packet_out),
    )

    if talent_packet().data["talent_transport_packet"]["transport_status"] != "validated":
        # simc could not resolve every trait, so describe-build has nothing to decode. The
        # documented handoff is an error envelope that names the missing step, not a partial
        # analysis. See tmp/handoffs/e2e-warcraftlogs.md.
        failure = run("warcraft", "talent-describe", *args, expect=EXIT_GENERIC, error_code="invalid_build_packet")
        assert "validate-talent-transport" in failure.payload["error"]["message"], failure.describe()
        assert failure.stdout == ""
        return

    result = run("warcraft", "talent-describe", *args)
    assert result.payload["kind"] == "talent_describe", result.describe()
    packet = result.data["talent_transport_packet"]
    assert packet["transport_status"] == "validated", result.describe()
    assert result.data["packet_path"] == str(packet_out) or packet_out.exists(), result.describe()
    assert json.loads(packet_out.read_text(encoding="utf-8"))["scope"]["actor_id"] == actor["id"]
    assert result.data["describe_result"], result.describe()


def test_actor_profile_crosswalks_a_report_actor_to_raider_io(require):
    require("warcraftlogs", "raiderio")
    found = anchor()
    actor = found.players[0]

    result = run("warcraft", "actor-profile", found.code, str(actor["name"]), "--fight-id", str(found.fight_id))
    assert result.payload["kind"] == "actor_profile_crosswalk", result.describe()
    query = result.payload["query"]
    assert query["report_code"] == found.code, result.describe()
    assert query["actor_name"] == actor["name"], result.describe()
    assert query["fight_id"] == found.fight_id, result.describe()

    sources = result.data["sources"]
    assert sources["warcraftlogs"]["status"] == "ok", result.describe()
    assert sources["raiderio"]["status"] == "ok", result.describe()
    log_identity = sources["warcraftlogs"]["report_actor_identity"]
    assert log_identity["status"] == "canonical", result.describe()
    assert log_identity["identity"]["name"] == actor["name"], result.describe()
    assert log_identity["identity"]["actor_id"] == actor["id"], result.describe()
    assert sources["raiderio"]["profile_url"].endswith(str(actor["name"])), result.describe()

    reconciliation = result.data["reconciliation"]
    assert reconciliation["comparable"] is True, result.describe()
    # The spec can legitimately differ: Raider.IO reports the character's current spec, the log
    # reports the spec played in that fight. The class must still agree.
    assert reconciliation["class_agree"] is True, result.describe()
    assert result.data["join_rule"], result.describe()


def test_warcraftlogs_passthrough_matches_the_direct_binary(require):
    require("warcraftlogs")
    args = ("report-fights", anchor().code)
    through_wrapper = run("warcraft", "warcraftlogs", *args)
    direct = run("warcraftlogs", *args)
    assert through_wrapper.payload == direct.payload, through_wrapper.describe()
    assert through_wrapper.payload["provider"] == "warcraftlogs", through_wrapper.describe()
    assert through_wrapper.data["fights"], through_wrapper.describe()

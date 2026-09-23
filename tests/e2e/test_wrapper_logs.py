"""End-to-end journeys for the ``warcraft`` wrapper's log-based composites.

These are the cross-provider handoffs that only exist in the wrapper: a Warcraft Logs fight plus
Lorrgs top parses (``cooldown-packet``), a Warcraft Logs report actor plus SimulationCraft
(``talent-packet`` / ``talent-describe``), a Warcraft Logs report actor plus Raider.IO
(``actor-profile``), and the ``warcraft warcraftlogs`` passthrough.

Inputs are discovered at run time and reuse the Warcraft Logs discovery chain in
``tests/e2e/test_warcraftlogs.py``: the current raid tier, the most recent kill in it, and that
kill's roster. Lorrgs only serves fights from reports it has already cached, so ``cooldown-packet``
is covered twice: once against a report walked out of the Lorrgs spec ranking (the full packet with
phase windows, for the first and the second phase), and once against the pinned guild's private
report, which Lorrgs has never cached and which is what a caller's own log looks like (the degraded
packet that keeps the Warcraft Logs half). A Heroic kill off the public leaderboard proves the
top parses are ranked at the fight's own difficulty.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from tests.e2e.harness import EXIT_NOT_FOUND, JourneyFailure, Result, run
from tests.e2e.test_warcraftlogs import _fight_roster, anchor, current_raid_zone, guild_anchor

# How far discovery walks the Lorrgs ranking before giving up on a cached report.
LORRGS_SPEC_ATTEMPTS = 4
LORRGS_REPORT_ATTEMPTS = 5

# Warcraft Logs' id for Heroic, and how many Heroic leaderboard rows discovery walks.
HEROIC_DIFFICULTY_ID = 4
HEROIC_ROW_ATTEMPTS = 5


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
    return frozenset(str(row["full_name_slug"]) for row in result.data["specs"])


@lru_cache(maxsize=1)
def lorrgs_boss_slugs() -> dict[int, str]:
    result = run("lorrgs", "bosses")
    return {int(row["id"]): str(row["full_name_slug"]) for row in result.data["bosses"]}


@dataclass(frozen=True)
class LorrgsTarget:
    """A report fight plus one player inside it."""

    boss_slug: str
    code: str
    fight_id: int
    actor_id: int
    actor_name: str
    spec_slug: str

    @property
    def url(self) -> str:
        return _report_url(self.code, self.fight_id)

    @property
    def query_url(self) -> str:
        """The ``?fight=N&type=...`` form Warcraft Logs links use; the packet must carry both through."""
        return f"https://www.warcraftlogs.com/reports/{self.code}?fight={self.fight_id}&type=damage-done"


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
            for report in ranking.data["reports"][:LORRGS_REPORT_ATTEMPTS]:
                fights = report.get("fights") or []
                if not fights:
                    continue
                fight_id = int(fights[0]["fight_id"])
                # Ranked reports are not always loaded on Lorrgs; a 404 here means "try the next one".
                cached = run("lorrgs", "user-report-fights", str(report["report_id"]), "--fight", str(fight_id), expect=None)
                if not cached.ok:
                    if cached.error_code != "not_found":
                        raise JourneyFailure(f"unexpected Lorrgs failure\n{cached.describe()}")
                    continue
                players = cached.data["fights"][0]["players"]
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
def _lorrgs_capable_actor() -> tuple[dict[str, Any], str]:
    """A pinned-guild kill actor whose spec Lorrgs publishes cooldown metadata for."""
    players = guild_anchor().players
    for player in players:
        slug = _lorrgs_spec_slug(player)
        if slug in lorrgs_spec_slugs():
            return player, str(slug)
    raise JourneyFailure(f"no guild roster spec maps to a Lorrgs spec slug: {[p.get('type') for p in players]}")


@lru_cache(maxsize=1)
def _guild_boss_slug() -> str:
    fight = guild_anchor().fight
    slug = lorrgs_boss_slugs().get(int(fight["encounter_id"]))
    if slug is None:
        raise JourneyFailure(f"Lorrgs does not know encounter {fight['encounter_id']}: {fight['name']}")
    return slug


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
        target.query_url,
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
    assert query["report_type"] == "damage-done", result.describe()

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
    assert data["lorrgs"]["status"] == "ok", result.describe()
    assert data["lorrgs"]["missing"] == [], result.describe()
    assert data["phase"]["status"] == "ready", result.describe()
    assert data["phase"]["requested"] == 1, result.describe()

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

    _assert_samples_sit_in_their_own_phase(data["comparison"], phase=1, result=result)
    assert data["notes"], "the packet must say where its phase windows and samples come from"


def _assert_samples_sit_in_their_own_phase(comparison: dict[str, Any], *, phase: int, result: Result) -> None:
    """Top-parse samples exist, and every cast they quote sits inside that sample's own phase window.

    Every sample is a top parse of the same boss, so the phase exists for it too; its window is its
    own, not the target report's.
    """
    assert comparison["status"] == "ready", result.describe()
    samples = comparison["samples"]
    assert 1 <= comparison["sample_count"] == len(samples) <= 2, result.describe()
    assert comparison["selected_phase_spell_frequency"], result.describe()
    assert any(sample["selected_phase_casts"] for sample in samples), f"no sample pressed a cooldown in P{phase}"
    for sample in samples:
        assert sample["phase_available"] is True, result.describe()
        window = sample["phase_window"]
        assert window["phase"] == phase, result.describe()
        for cast in sample["selected_phase_casts"]:
            assert window["start_ms"] <= cast["timestamp_ms"] < window["end_ms"], result.describe()


def test_cooldown_packet_selects_the_requested_phase_not_the_first(require):
    """``--phase 2`` must select the second window; a packet that always used P1 would pass every P1 journey."""
    require("warcraftlogs", "lorrgs")
    target = lorrgs_target()
    args = ("cooldown-packet", target.url, "--actor-id", str(target.actor_id), "--sample-limit", "2")

    first = run("warcraft", *args, "--phase", "1")
    windows = first.data["phase"]["windows"]
    if len(windows) < 2:
        raise JourneyFailure(f"{target.boss_slug} reports one phase window, so --phase 2 cannot be proved\n{first.describe()}")

    result = run("warcraft", *args, "--phase", "2")
    phase = result.data["phase"]
    assert phase["requested"] == 2 and phase["status"] == "ready", result.describe()
    assert phase["windows"] == windows, result.describe()
    selected = phase["selected"]
    assert selected == next(window for window in windows if window["phase"] == 2), result.describe()
    assert selected["label"] == "P2", result.describe()
    assert selected != first.data["phase"]["selected"], result.describe()

    casts = result.data["cooldowns"]["player_casts"]
    assert casts["selected_phase_cast_count"] == len(casts["selected_phase_casts"]), result.describe()
    for cast in casts["selected_phase_casts"]:
        assert selected["start_ms"] <= cast["timestamp_ms"] < selected["end_ms"], result.describe()
    _assert_samples_sit_in_their_own_phase(result.data["comparison"], phase=2, result=result)


def _hero_selection(packet: dict[str, Any]) -> dict[str, Any]:
    """The `selection` row SimC resolved: which hero tree this actor actually picked.

    A hero tree is chosen by a selection node, not by the talents underneath it, so this row is the
    packet's own statement of the choice and is what every downstream describe has to agree with.
    """
    selections: list[dict[str, Any]] = [row for row in packet["validation"]["resolved_entries"] if row["tree"] == "selection"]
    assert len(selections) == 1, f"expected exactly one hero-tree selection row, got {selections}"
    return selections[0]


def test_cooldown_packet_degrades_when_lorrgs_has_not_cached_the_report(require):
    """The common case: an ordinary report Lorrgs has never loaded.

    Lorrgs only serves reports someone has opened on lorrgs.io, so most guild and private reports
    are not there. The command must still return the Warcraft Logs half rather than failing, and it
    must say exactly what is missing instead of reporting an empty phase as if it were a real one.
    """
    require("warcraftlogs", "lorrgs")
    found = guild_anchor()
    actor, spec_slug = _lorrgs_capable_actor()
    boss_slug = _guild_boss_slug()

    result = run(
        "warcraft",
        "cooldown-packet",
        found.url,
        "--actor-id",
        str(actor["id"]),
        "--actor-name",
        str(actor["name"]),
        "--spec-slug",
        spec_slug,
        "--boss-slug",
        boss_slug,
        "--phase",
        "1",
        "--sample-limit",
        "1",
    )
    data = result.data
    lorrgs = data["lorrgs"]
    assert lorrgs["status"] == "unavailable", result.describe()
    assert lorrgs["reason"] == "lorrgs_fight_lookup_failed", result.describe()
    assert lorrgs["source"]["code"] == "not_found", result.describe()
    assert "phase_windows" in lorrgs["missing"], result.describe()

    # The phase the caller asked for was not applied, and the packet says so rather than showing
    # an empty P1 window.
    phase = data["phase"]
    assert phase["status"] == "unavailable", result.describe()
    assert phase["requested"] == 1, result.describe()
    assert phase["selected"] is None, result.describe()
    assert phase["windows"] == [], result.describe()
    assert any("no phase windows" in note for note in data["notes"]), result.describe()

    # The Warcraft Logs half is intact: the flags supplied what Lorrgs would have.
    assert result.payload["query"]["report_code"] == found.code, result.describe()
    # Without the Lorrgs roster the player is named by the flags, and the packet says so.
    identity = {key: data["player"][key] for key in ("name", "source_id", "spec_slug", "class_slug")}
    assert identity == {
        "name": actor["name"],
        "source_id": actor["id"],
        "spec_slug": spec_slug,
        "class_slug": str(actor["type"]).lower(),
    }, result.describe()
    assert any("--actor-name" in note for note in data["notes"]), result.describe()
    assert data["boss"]["boss_slug"] == boss_slug, result.describe()
    casts = data["cooldowns"]["player_casts"]
    assert casts["tracked_cast_count"] > 0, "the degraded packet returned no Warcraft Logs casts"
    assert casts["tracked_cast_count"] == len(casts["tracked_casts"]), result.describe()
    assert casts["selected_phase_casts"] == [], result.describe()

    sources = data["sources"]
    assert sources["lorrgs_user_report_fights"]["status"] == "error", result.describe()
    assert sources["warcraftlogs_report_events"]["status"] == "ok", result.describe()
    assert sources["lorrgs_spec_spells"]["status"] == "ok", result.describe()

    # --spell-id must narrow the tracked set, and the casts with it.
    pressed = max(casts["tracked_casts_by_spell"], key=lambda row: row["count"])
    spell_id = int(pressed["spell"]["spell_id"])
    narrowed = run(
        "warcraft",
        "cooldown-packet",
        found.url,
        "--actor-id",
        str(actor["id"]),
        "--spec-slug",
        spec_slug,
        "--spell-id",
        str(spell_id),
        "--phase",
        "1",
        "--sample-limit",
        "0",
    )
    tracked = narrowed.data["cooldowns"]
    assert [spell["spell_id"] for spell in tracked["tracked_spells"]] == [spell_id], narrowed.describe()
    assert tracked["tracked_spell_count"] == 1, narrowed.describe()
    narrowed_casts = tracked["player_casts"]
    assert narrowed_casts["tracked_cast_count"] == pressed["count"], narrowed.describe()
    assert all(cast["spell"]["spell_id"] == spell_id for cast in narrowed_casts["tracked_casts"]), narrowed.describe()


@lru_cache(maxsize=1)
def heroic_target() -> LorrgsTarget:
    """A ranked player in a Heroic kill of the anchor boss, from that boss's Heroic leaderboard.

    The leaderboard does not depend on any one guild's progress, and ``report-fights`` confirms the
    fight is a Heroic kill independently of the leaderboard's own filter.
    """
    boss_id = int(anchor().fight["encounter_id"])
    ranked = run(
        "warcraftlogs", "encounter-rankings", "--zone-id", str(current_raid_zone()["id"]), "--boss-id", str(boss_id),
        "--difficulty", str(HEROIC_DIFFICULTY_ID), "--top", str(HEROIC_ROW_ATTEMPTS),
    )
    for row in ranked.data["rankings"]["rows"]:
        code, fight_id = str(row["report_code"]), int(row["fight_id"])
        fights = run("warcraftlogs", "report-fights", code).data["fights"]
        fight = next(fight for fight in fights if fight["id"] == fight_id)
        assert (fight["difficulty"], fight["kill"]) == (HEROIC_DIFFICULTY_ID, True), f"{code}#{fight_id}: {fight}"
        player = next(player for player in _fight_roster(code, fight_id) if player["name"] == row["name"])
        spec_slug = _lorrgs_spec_slug(player)
        if spec_slug in lorrgs_spec_slugs():
            return LorrgsTarget(
                boss_slug=lorrgs_boss_slugs()[boss_id],
                code=code,
                fight_id=fight_id,
                actor_id=int(player["id"]),
                actor_name=str(player["name"]),
                spec_slug=str(spec_slug),
            )
    raise JourneyFailure(f"no Heroic leaderboard row names a spec Lorrgs ranks\n{ranked.describe()}")


def test_cooldown_packet_compares_a_heroic_fight_with_heroic_top_parses(require):
    """Top parses are ranked at the analyzed fight's own difficulty; a Mythic default would pass on any Mythic fight."""
    require("warcraftlogs", "lorrgs")
    target = heroic_target()
    result = run(
        "warcraft", "cooldown-packet", target.url, "--actor-id", str(target.actor_id), "--spec-slug", target.spec_slug,
        "--boss-slug", target.boss_slug, "--phase", "1", "--sample-limit", "1",
    )
    assert result.payload["query"]["difficulty"] == "heroic", result.describe()
    ranking = result.data["sources"]["lorrgs_spec_ranking"]
    assert ranking["status"] == "ok", result.describe()
    # The URL Lorrgs was actually asked, not just the command the packet prints.
    assert ranking["source_url"].endswith("?difficulty=heroic"), result.describe()
    assert result.data["comparison"]["sample_count"] == 1, result.describe()
    # A tracked cast is a `cast` event; the Casts data type also returns cast-bar and empower rows.
    tracked = result.data["cooldowns"]["player_casts"]["tracked_casts"]
    assert tracked and {cast["type"] for cast in tracked} == {"cast"}, result.describe()


def test_cooldown_packet_without_the_fallback_flags_names_the_flags_it_needs(require):
    """Without ``--actor-id``/``--spec-slug`` there is nothing left to build, so it must fail loudly."""
    require("warcraftlogs", "lorrgs")
    actor, _spec_slug = _lorrgs_capable_actor()
    result = run(
        "warcraft",
        "cooldown-packet",
        guild_anchor().url,
        "--actor-id",
        str(actor["id"]),
        "--phase",
        "1",
        expect=EXIT_NOT_FOUND,
        error_code="lorrgs_fight_lookup_failed",
    )
    assert result.payload["error"]["details"]["required_flags"] == ["--actor-id", "--spec-slug"], result.describe()
    assert "--spec-slug" in result.payload["error"]["message"], result.describe()
    assert result.stdout == ""


def test_talent_packet_routes_a_report_actor_through_simc(require):
    """The composite's whole promise: a log actor becomes a SimC-validated transport packet.

    ``not_validated`` is a real outcome of the producer, but it is a failure of this journey: the
    checkout is current and built (the simc journeys assert that), so an actor SimulationCraft
    cannot resolve means the log-to-SimC handoff is broken for every user, not just this actor.
    """
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
    assert result.data["upgraded"] is True, result.describe()

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

    assert packet["transport_status"] == "validated", result.describe()
    validation = packet["validation"]
    assert validation["status"] == "validated", result.describe()
    assert validation["actor_class"] == identity["actor_class"], result.describe()
    assert validation["spec"] == identity["spec"], result.describe()
    # Every raw combatant-info row resolved against SimC's own trait data, and nothing was dropped.
    raw_rows = packet["raw_evidence"]["talent_tree_entries"]
    assert raw_rows, result.describe()
    assert len(validation["resolved_entries"]) == len(raw_rows), result.describe()
    assert all(entry["token"] and entry["tree"] for entry in validation["resolved_entries"]), result.describe()
    assert {entry["entry"] for entry in validation["resolved_entries"]} == {row["entry"] for row in raw_rows}

    split = packet["transport_forms"]["simc_split_talents"]
    assert set(split) == {"class_talents", "spec_talents", "hero_talents"}, result.describe()
    for tree, option in split.items():
        assert option, f"{tree} came back empty for a validated packet\n{result.describe()}"
        rows = option.split("/")
        assert len(rows) == sum(1 for entry in validation["resolved_entries"] if entry["tree"] == tree.removesuffix("_talents"))
        assert all(row.count(":") == 1 for row in rows), result.describe()

    selection = _hero_selection(packet)
    assert selection["hero_tree"] and isinstance(selection["hero_tree_id"], int), result.describe()


def test_talent_describe_adds_simc_priority_output_for_the_report_build(require, out_dir):
    """talent-describe writes the packet and describes the build the packet actually names.

    The packet it wrote is then handed straight back to ``simc`` on its own, which is the handoff
    an agent performs: if the file on disk cannot drive ``validate-talent-transport`` and
    ``describe-build``, the composite produced a packet nobody else can read.
    """
    require("warcraftlogs", "simc")
    actor, apl_path = talent_actor()
    packet_out = out_dir / "talent-packet.json"

    result = run(
        "warcraft",
        "talent-describe",
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
    assert result.payload["kind"] == "talent_describe", result.describe()
    packet = result.data["talent_transport_packet"]
    assert packet["transport_status"] == "validated", result.describe()
    assert result.data["written_packet_path"] == str(packet_out.resolve()), result.describe()
    on_disk = json.loads(packet_out.read_text(encoding="utf-8"))
    assert on_disk == packet, result.describe()

    describe_payload = result.data["describe_result"]["payload"]
    assert describe_payload["ok"] is True, result.describe()
    build = describe_payload["data"]["build"]
    selection = _hero_selection(packet)
    # The APL is pruned against the hero tree the packet says the actor selected.
    assert build["hero_tree"] == {"name": selection["hero_tree"], "id": selection["hero_tree_id"]}, result.describe()
    assert describe_payload["data"]["identity"]["actor_class"] == packet["validation"]["actor_class"], result.describe()
    assert describe_payload["data"]["apl"]["path"] == str(apl_path), result.describe()
    assert describe_payload["data"]["single_target"]["active_priority"], result.describe()

    # The written packet is a complete input on its own.
    revalidated = run("simc", "validate-talent-transport", "--build-packet", str(packet_out))
    assert revalidated.data["input"]["source"] == "build_packet", revalidated.describe()
    assert revalidated.data["transport_status"] == "validated", revalidated.describe()
    assert revalidated.data["transport_forms"]["simc_split_talents"] == packet["transport_forms"]["simc_split_talents"]

    redescribed = run("simc", "describe-build", "--build-packet", str(packet_out), "--apl-path", str(apl_path))
    assert redescribed.data["build"]["hero_tree"] == build["hero_tree"], redescribed.describe()
    assert redescribed.data["build"]["enabled_talents"] == build["enabled_talents"], redescribed.describe()


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

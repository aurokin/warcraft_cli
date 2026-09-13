"""End-to-end journeys for the ``simc`` binary against the local SimulationCraft checkout.

Every command in docs/reference/simc.md is exercised here. The journeys mirror how an agent
actually works: inspect the checkout, describe a build, read an APL, run a short sim and analyse
it, validate talent transport, and fail cleanly when the checkout or an input path is wrong.

Nothing here mutates the real checkout. ``build`` is only reached through its missing-build-dir
guard, ``sync`` only through its dirty-worktree and missing-repo guards, and ``checkout`` only
through a temporary ``XDG_DATA_HOME`` whose managed root is not a git repo, so no clone, pull, or
compile ever runs.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from tests.e2e.harness import EXIT_GENERIC, EXIT_NOT_FOUND, EXIT_USAGE, dead_proxy_env, payload_or_legacy, run, run_raw

# Windwalker is the monk spec SimulationCraft ships a default APL and a profile for; mistweaver is
# a healer and has neither. The APL itself is discovered through `simc spec-files`.
APL_STEM = "monk_windwalker"
ACTOR_CLASS = "monk"
SPEC = "windwalker"

# `{ tree_index, class_id, entry_id, node_id, max_rank, ..., "Talent Name" ...}` in the SimC
# generated trait table. Only the leading ids and the name are needed to turn a decoded build back
# into the raw (entry, node, rank) rows a transport packet carries.
TRAIT_ROW_RE = re.compile(r'\{\s*(\d+),\s*(\d+),\s*(\d+),\s*(\d+),\s*(\d+),.*?"([^"]+)"')
TREE_INDEX = {"class": 1, "spec": 2, "hero": 3}
CLASS_ID = {"monk": 10}


@dataclass(frozen=True)
class Checkout:
    """The pieces of the local SimulationCraft checkout the journeys below act on."""

    root: Path
    apl: Path
    assisted_apl: Path
    profile: Path
    talents: str


def _first_item(result_data: dict[str, Any], category: str) -> dict[str, Any]:
    items = result_data["categories"][category]["items"]
    assert items, f"spec-files returned no {category} rows: {json.dumps(result_data)[:400]}"
    return items[0]


@pytest.fixture(scope="module")
def checkout() -> Checkout:
    """Discover the checkout, its windwalker APLs, and a real talent string, once per module."""
    doctor = run("simc", "doctor")
    repo = payload_or_legacy(doctor, "repo")
    assert repo["repo_ready"] is True, doctor.describe()
    assert repo["build_ready"] is True, doctor.describe()
    root = Path(repo["root"])

    spec_files = run("simc", "spec-files", APL_STEM)
    apl = Path(_first_item(spec_files.data, "default_apl")["path"])
    assisted_apl = Path(_first_item(spec_files.data, "assisted_apl")["path"])
    assert apl.name == f"{APL_STEM}.simc", spec_files.describe()

    profiles = sorted((root / "profiles").rglob("*Monk_Windwalker.simc"))
    assert profiles, f"no windwalker profile under {root / 'profiles'}"
    profile = profiles[0]

    inspected = run("simc", "inspect", str(profile))
    talents = payload_or_legacy(inspected, "target")["build_spec"]["talents"]
    assert isinstance(talents, str) and len(talents) > 40, inspected.describe()

    return Checkout(root=root, apl=apl, assisted_apl=assisted_apl, profile=profile, talents=talents)


def _talent_rows_from_build(checkout: Checkout, decoded: dict[str, Any]) -> list[str]:
    """Turn a decoded build into the raw ``entry:node:rank`` rows a log-sourced packet carries.

    Only SimC's generated trait table maps a talent to its node id, so the ids come from there.
    Names that appear more than once in a tree (choice nodes) are dropped: the row would be
    ambiguous, and validate-talent-transport is expected to accept a partial selection.
    """
    trait_file = checkout.root / "engine" / "dbc" / "generated" / "trait_data.inc"
    ids_by_name: dict[tuple[int, str], list[tuple[int, int]]] = {}
    for line in trait_file.read_text().splitlines():
        match = TRAIT_ROW_RE.search(line)
        if match is None:
            continue
        tree_index, class_id, entry_id, node_id, _max_rank, name = match.groups()
        if int(class_id) != CLASS_ID[ACTOR_CLASS]:
            continue
        ids_by_name.setdefault((int(tree_index), name), []).append((int(entry_id), int(node_id)))

    rows: list[str] = []
    for tree, talents in decoded["talents_by_tree"].items():
        if tree not in TREE_INDEX:
            continue
        for talent in talents:
            if talent["rank"] <= 0:
                continue
            candidates = ids_by_name.get((TREE_INDEX[tree], talent["name"]), [])
            if len(candidates) != 1:
                continue
            entry_id, node_id = candidates[0]
            rows.append(f"{entry_id}:{node_id}:{talent['rank']}")
    return rows


# --- repo inspection ---


def test_doctor_reports_a_ready_checkout_and_needs_no_network(require, checkout: Checkout) -> None:
    require("simc")
    # simc has no network surface, so a dead proxy must not change the answer.
    result = run("simc", "doctor", env=dead_proxy_env())
    data = result.data
    assert data == {key: payload_or_legacy(result, key) for key in data}, result.describe()
    assert data["status"] == "ready"
    assert data["auth"] == {"required": False, "deferred": False}
    assert data["capabilities"]["decode_build"] == "ready"
    assert data["capabilities"]["search"] == "coming_soon"
    assert data["repo"]["root"] == str(checkout.root)
    assert data["repo"]["binary"]["available"] is True
    assert "SimulationCraft" in data["repo"]["binary"]["version_line"]
    assert data["repo_resolution"]["configured_root"] == str(checkout.root)
    assert data["repo_resolution"]["source"] in {"cli", "env", "config", "managed", "unset"}


def test_version_reports_the_built_binary(require, checkout: Checkout) -> None:
    require("simc")
    result = run("simc", "version")
    assert result.data["binary"]["path"] == str(checkout.root / "build" / "simc")
    assert result.data["binary"]["available"] is True
    assert result.data["version"].startswith("SimulationCraft")


def test_repo_reports_the_active_resolution(require, checkout: Checkout) -> None:
    require("simc")
    result = run("simc", "repo")
    assert result.data["action"] == "inspect"
    assert result.data["changed"] is False
    assert result.data["resolution"]["root"] == str(checkout.root)
    assert result.data["resolution"]["source"] in {"config", "env", "cli", "unset", "managed"}


def test_repo_set_root_and_clear_root_round_trip(require, checkout: Checkout, out_dir: Path) -> None:
    """The persisted root is written and cleared inside a throwaway config home, never the real one."""
    require("simc")
    config_home = out_dir / "config"
    config_home.mkdir()
    env = {"XDG_CONFIG_HOME": str(config_home)}

    stored = run("simc", "repo", "--set-root", str(checkout.root), env=env)
    assert stored.data["action"] == "set_root"
    assert stored.data["changed"] is True
    assert stored.data["stored_root"] == str(checkout.root)
    assert stored.data["resolution"]["configured_root"] == str(checkout.root)
    config_path = Path(stored.data["resolution"]["config_path"])
    assert config_path.is_relative_to(config_home) and config_path.is_file()

    reread = run("simc", "repo", env=env)
    assert reread.data["resolution"]["configured_root"] == str(checkout.root)
    assert reread.data["resolution"]["source"] == "config"

    cleared = run("simc", "repo", "--clear-root", env=env)
    assert cleared.data["action"] == "clear_root"
    assert cleared.data["changed"] is True
    assert cleared.data["resolution"]["configured_root"] is None
    assert not config_path.exists()


def test_repo_set_root_rejects_a_missing_directory(require, out_dir: Path) -> None:
    require("simc")
    missing = out_dir / "not-a-checkout"
    result = run("simc", "repo", "--set-root", str(missing), expect=EXIT_NOT_FOUND, error_code="not_found")
    assert str(missing) in result.payload["error"]["message"]


def test_verify_clean_reports_the_checkout_and_binary(require, checkout: Checkout) -> None:
    require("simc")
    result = run("simc", "verify-clean", "--hash-binary", timeout=300)
    assert result.data["repo_root"] == str(checkout.root)
    assert result.data["git"]["git"] is True
    assert result.data["binary"]["exists"] is True
    assert len(result.data["binary"]["sha256"]) == 64


def test_inspect_describes_the_repo_and_a_profile_inside_it(require, checkout: Checkout) -> None:
    require("simc")
    repo_view = run("simc", "inspect")
    assert repo_view.data["inspect"] == "repo"
    assert repo_view.data["repo"]["root"] == str(checkout.root)

    file_view = run("simc", "inspect", str(checkout.profile))
    target = file_view.data["target"]
    assert target["kind"] == "file"
    assert target["relative_to_repo"] == str(checkout.profile.relative_to(checkout.root))
    assert target["line_count"] > 0
    assert target["build_spec"]["actor_class"] == ACTOR_CLASS
    assert target["build_spec"]["spec"] == SPEC
    assert target["build_spec"]["talents"] == checkout.talents


def test_spec_files_lists_class_files_for_the_queried_spec(require, checkout: Checkout) -> None:
    require("simc")
    result = run("simc", "spec-files", ACTOR_CLASS, "--limit", "50")
    assert result.payload["query"] == ACTOR_CLASS
    assert result.data["count"] > 0
    categories = result.data["categories"]
    default_paths = [item["relative_path"] for item in categories["default_apl"]["items"]]
    assert default_paths, result.describe()
    assert all(ACTOR_CLASS in path for path in default_paths)
    assert f"ActionPriorityLists/default/{APL_STEM}.simc" in default_paths
    cpp_paths = [item["relative_path"] for item in categories["cpp"]["items"]]
    assert any(ACTOR_CLASS in path for path in cpp_paths), result.describe()


def test_search_and_resolve_are_structured_coming_soon_stubs(require) -> None:
    require("simc")
    for command in ("search", "resolve"):
        result = run("simc", command, "mistweaver monk")
        assert result.data["coming_soon"] is True
        assert result.data["resolved"] is False
        assert result.data["results"] == []
        assert result.data["suggested_command"].startswith("simc ")


# --- build description ---


def test_decode_and_identify_a_build_from_a_repo_profile(require, checkout: Checkout) -> None:
    require("simc")
    identified = run("simc", "identify-build", "--profile-path", str(checkout.profile))
    assert identified.data["identity"]["actor_class"] == ACTOR_CLASS
    assert identified.data["identity"]["spec"] == SPEC
    assert identified.data["identity"]["confidence"] == "high"
    assert identified.data["build_spec"]["talents"] == checkout.talents

    decoded_result = run("simc", "decode-build", "--talents", checkout.talents, "--actor-class", ACTOR_CLASS, "--spec", SPEC)
    assert decoded_result.data["build_spec"]["talents"] == checkout.talents
    decoded = decoded_result.data["decoded"]
    assert decoded["actor_class"] == ACTOR_CLASS
    assert decoded["spec"] == SPEC
    assert len(decoded["enabled_talents"]) > 20
    for tree in ("class", "spec", "hero"):
        selected = [talent for talent in decoded["talents_by_tree"][tree] if talent["rank"] > 0]
        assert selected, f"{tree} tree decoded empty: {json.dumps(decoded['talents_by_tree'][tree])[:300]}"
        assert all(talent["rank"] <= talent["max_rank"] for talent in selected)
    assert f"decoded via {checkout.root}" in " ".join(decoded["source_notes"])


def test_describe_build_covers_talents_priority_and_the_aoe_delta(require, checkout: Checkout) -> None:
    require("simc")
    result = run(
        "simc",
        "describe-build",
        "--profile-path",
        str(checkout.profile),
        "--apl-path",
        str(checkout.apl),
        "--targets",
        "1",
        "--aoe-targets",
        "5",
        "--priority-limit",
        "6",
    )
    data = result.data
    assert data["build_spec"]["talents"] == checkout.talents
    assert data["identity"]["actor_class"] == ACTOR_CLASS
    assert data["apl"]["path"] == str(checkout.apl)
    for tree in ("class", "spec", "hero"):
        assert data["build"]["talents_by_tree"][tree]["selected"], result.describe()

    single, multi = data["single_target"], data["multi_target"]
    assert single["targets"] == 1
    assert multi["targets"] == 5
    for view in (single, multi):
        assert view["active_priority"], result.describe()
        assert len(view["active_priority"]) <= 6
        assert view["active_action_names"]
        assert all(row["action"] and row["status"] for row in view["active_priority"])
    comparison = data["comparison"]
    assert comparison["primary_targets"] == 1
    assert comparison["aoe_targets"] == 5
    # The windwalker APL splits single-target and multitarget lists, so the views must differ.
    assert set(comparison["new_active_actions_in_aoe"]) | set(comparison["missing_active_actions_in_aoe"]), result.describe()


def test_compare_builds_diffs_two_real_talent_strings(require, checkout: Checkout) -> None:
    require("simc")
    other_profiles = sorted(path for path in (checkout.root / "profiles").rglob("*Monk_Windwalker*.simc") if path != checkout.profile)
    assert other_profiles, "expected a second windwalker profile to diff against"
    other = run("simc", "inspect", str(other_profiles[0]))
    other_talents = payload_or_legacy(other, "target")["build_spec"]["talents"]
    assert other_talents and other_talents != checkout.talents

    result = run(
        "simc",
        "compare-builds",
        "--base",
        checkout.talents,
        "--other",
        other_talents,
        "--actor-class",
        ACTOR_CLASS,
        "--spec",
        SPEC,
    )
    assert result.data["base"]["actor_class"] == ACTOR_CLASS
    assert result.data["base"]["enabled_talents"]
    assert result.data["trees_compared"] == ["class", "spec", "hero"]
    comparison = result.data["comparisons"][0]
    assert comparison["input"] == other_talents
    assert comparison["has_differences"] is True
    changed_trees = [tree for tree, diff in comparison["trees"].items() if diff["has_differences"]]
    assert changed_trees, result.describe()
    for tree in changed_trees:
        diff = comparison["trees"][tree]
        assert diff["added"] or diff["removed"] or diff["changed"]
        for row in [*diff["added"], *diff["removed"]]:
            assert row["entry"] > 0 and row["name"]


def test_modify_build_removes_a_talent_and_re_encodes_it(require, checkout: Checkout) -> None:
    """Round trip: decode the base build, drop one talent, and confirm only that talent moved."""
    require("simc")
    decoded = run("simc", "decode-build", "--talents", checkout.talents, "--actor-class", ACTOR_CLASS, "--spec", SPEC)
    class_talents = [talent for talent in decoded.data["decoded"]["talents_by_tree"]["class"] if talent["rank"] > 0]
    removable = class_talents[-1]["name"]

    result = run(
        "simc",
        "modify-build",
        "--talents",
        checkout.talents,
        "--remove",
        removable,
        "--actor-class",
        ACTOR_CLASS,
        "--spec",
        SPEC,
    )
    assert result.data["base"]["input"] == checkout.talents
    assert result.data["modifications"] == [f"remove:{removable}"]
    encoded = result.data["result"]["talents_export"]
    assert encoded and encoded != checkout.talents
    assert result.data["result"]["wowhead_url"].endswith(encoded)
    diff = result.data["result"]["diff_from_base"]
    assert [row["name"] for row in diff["class"]["removed"]] == [removable]
    assert diff["class"]["added"] == []
    assert diff["spec"]["has_differences"] is False
    assert diff["hero"]["has_differences"] is False

    # The re-encoded string must decode back to the same build minus the removed talent.
    redecoded = run("simc", "decode-build", "--talents", encoded, "--actor-class", ACTOR_CLASS, "--spec", SPEC)
    base_tokens = set(decoded.data["decoded"]["enabled_talents"])
    assert set(redecoded.data["decoded"]["enabled_talents"]) == base_tokens - {class_talents[-1]["token"]}


# --- APL analysis ---


def test_apl_structure_journey(require, checkout: Checkout) -> None:
    require("simc")
    lists = run("simc", "apl-lists", str(checkout.apl))
    assert lists.data["apl"]["path"] == str(checkout.apl)
    assert lists.data["apl"]["list_count"] > 1
    assert lists.data["apl"]["entry_count"] > 10
    list_names = {entry["list_name"] for entry in lists.data["lists"]}
    assert "default" in list_names
    default_list = next(entry for entry in lists.data["lists"] if entry["list_name"] == "default")
    assert default_list["count"] == len(default_list["entries"])
    assert all(entry["action"] and entry["raw"] for entry in default_list["entries"])

    single = run("simc", "apl-lists", str(checkout.apl), "--list", "default")
    assert [entry["list_name"] for entry in single.data["lists"]] == ["default"]

    graph = run("simc", "apl-graph", str(checkout.apl))
    assert graph.data["graph"]["format"] == "mermaid"
    text = graph.data["graph"]["text"]
    assert text.startswith("flowchart TD")
    assert "default" in text and "-->" in text
    assert graph.data["apl"]["list_count"] == lists.data["apl"]["list_count"]

    talents = run("simc", "apl-talents", str(checkout.apl))
    assert talents.data["count"] == len(talents.data["talents"])
    assert talents.data["count"] > 0
    assert all(row["token"] and row["lines"] for row in talents.data["talents"])


def test_find_and_trace_an_action_across_the_checkout(require, checkout: Checkout) -> None:
    require("simc")
    action = "rising_sun_kick"
    found = run("simc", "find-action", action, "--class", ACTOR_CLASS, "--limit", "5")
    assert found.data["action"] == action
    assert found.data["class_filter"] == ACTOR_CLASS
    assert found.data["count"] > 0
    populated = {name: bucket for name, bucket in found.data["buckets"].items() if bucket["count"]}
    assert populated, found.describe()
    for bucket in populated.values():
        assert bucket["items"]
        assert len(bucket["items"]) <= 5
        assert all(item["path"] and item["line_no"] > 0 for item in bucket["items"])

    traced = run("simc", "trace-action", str(checkout.apl), action, "--class", ACTOR_CLASS, "--limit", "3")
    assert traced.data["action"] == action
    assert traced.data["apl"]["path"] == str(checkout.apl)
    apl_hits = traced.data["apl_hits"]
    assert apl_hits["count"] == len(apl_hits["items"])
    assert apl_hits["count"] > 0
    assert all(hit["action"] == action and hit["list_name"] for hit in apl_hits["items"])


def test_exact_build_priority_journey(require, checkout: Checkout) -> None:
    """priority / opener / inactive-actions / apl-prune all describe the same exact build."""
    require("simc")
    build_args = ("--profile-path", str(checkout.profile))

    priority = run("simc", "priority", str(checkout.apl), *build_args, "--limit", "5")
    assert priority.data["build"]["actor_class"] == ACTOR_CLASS
    assert priority.data["build"]["enabled_talent_count"] > 20
    rows = priority.data["priority"]["items"]
    assert rows and len(rows) <= 5
    assert priority.data["priority"]["count"] == len(rows)
    assert all(row["status"] in {"guaranteed", "possible"} for row in rows)
    assert all(row["action"] and row["text"] for row in rows)
    assert priority.data["priority"]["focus_list"] == "default"

    opener = run("simc", "opener", str(checkout.apl), *build_args, "--limit", "5")
    assert opener.data["opener"]["kind"] == "static_priority_preview"
    assert [row["action"] for row in opener.data["opener"]["items"]] == [row["action"] for row in rows]
    assert opener.data["opener"]["caveat"]

    inactive = run("simc", "inactive-actions", str(checkout.apl), *build_args, "--limit", "10")
    assert inactive.data["inactive_actions"]["talent_only"] is True
    assert inactive.data["inactive_actions"]["count"] == len(inactive.data["inactive_actions"]["items"])
    assert all("talent." in row["reason"] for row in inactive.data["inactive_actions"]["items"])
    # Everything the priority view returned is active, so it cannot also be inactive.
    assert not {row["line_no"] for row in rows} & {row["line_no"] for row in inactive.data["inactive_actions"]["items"]}

    all_dead = run("simc", "inactive-actions", str(checkout.apl), *build_args, "--all-dead", "--limit", "20")
    assert all_dead.data["inactive_actions"]["talent_only"] is False
    assert all_dead.data["inactive_actions"]["count"] >= inactive.data["inactive_actions"]["count"]

    pruned = run("simc", "apl-prune", str(checkout.apl), *build_args, "--list", "default")
    assert pruned.data["show"] == "all"
    default_list = next(entry for entry in pruned.data["lists"] if entry["list_name"] == "default")
    states = {entry["state"] for entry in default_list["items"]}
    assert states <= {"eligible", "dead", "unknown"}
    assert "eligible" in states
    assert default_list["count"] == len(default_list["items"])

    eligible_only = run("simc", "apl-prune", str(checkout.apl), *build_args, "--list", "default", "--show", "eligible")
    eligible_items = next(entry for entry in eligible_only.data["lists"] if entry["list_name"] == "default")["items"]
    assert eligible_items
    assert {entry["state"] for entry in eligible_items} == {"eligible"}


def test_branch_and_intent_journey(require, checkout: Checkout) -> None:
    require("simc")
    build_args = ("--profile-path", str(checkout.profile))

    trace = run("simc", "apl-branch-trace", str(checkout.apl), *build_args, "--max-depth", "3")
    assert trace.data["summary"]["start_list"] == "default"
    assert trace.data["trace"], trace.describe()

    intent = run("simc", "apl-intent", str(checkout.apl), *build_args, "--limit", "4")
    assert intent.data["focus_list"] == "default"
    assert intent.data["intent"] and len(intent.data["intent"]) <= 4

    explained = run("simc", "apl-intent-explain", str(checkout.apl), *build_args, "--limit", "4")
    buckets = explained.data["explained_intent"]
    assert isinstance(buckets, dict) and buckets
    assert any(buckets[name] for name in buckets)

    compared = run("simc", "apl-branch-compare", str(checkout.apl), *build_args, "--left-targets", "1", "--right-targets", "5")
    comparison = compared.data["comparison"]
    assert comparison["start_list"] == "default"
    assert comparison["left_focus_preview"] and comparison["right_focus_preview"]
    # Single-target versus five targets must flip at least one call_action_list decision.
    assert comparison["focus_changes"] or comparison["decision_changes"], compared.describe()


# --- simulation run and the analysis chain that reads its output ---


def test_sim_run_and_log_analysis_chain(require, checkout: Checkout, out_dir: Path) -> None:
    """A short sim, then the commands that read its JSON report and combat log."""
    require("simc")
    json_out = out_dir / "sim.json"
    simmed = run(
        "simc",
        "sim",
        str(checkout.profile),
        "--iterations",
        "50",
        "--threads",
        "1",
        "--max-time",
        "60",
        "--json-out",
        str(json_out),
        timeout=300,
    )
    data = simmed.data
    assert data["status"] == "completed"
    assert data["input_source"] == "file"
    assert data["profile_path"] == str(checkout.profile)
    assert data["json_report_path"] == str(json_out)
    assert json_out.is_file() and json_out.stat().st_size > 0
    assert data["player"]["spec"].lower().startswith(SPEC)
    assert data["run_settings"]["iterations_requested"] == 50
    assert data["run_settings"]["threads"] == 1
    assert data["run_settings"]["max_time"] == 60
    assert data["metrics"]["dps"] > 0
    assert data["runtime"]["elapsed_time_seconds"] >= 0
    assert data["command"][0] == str(checkout.root / "build" / "simc")
    assert str(checkout.profile) in data["command"]
    assert "iterations=50" in data["command"]

    inline = run(
        "simc",
        "sim",
        "--profile-text",
        checkout.profile.read_text(),
        "--iterations",
        "20",
        "--threads",
        "1",
        "--max-time",
        "30",
        timeout=300,
    )
    assert inline.data["input_source"] == "profile_text"
    assert inline.data["metrics"]["dps"] > 0

    combat_log = out_dir / "combat.txt"
    ran = run(
        "simc",
        "run",
        str(checkout.profile),
        "--arg",
        "iterations=1",
        "--arg",
        "threads=1",
        "--arg",
        "max_time=60",
        "--arg",
        "log=1",
        "--arg",
        f"output={combat_log}",
        timeout=300,
    )
    assert ran.data["status"] == "completed"
    assert ran.data["profile_path"] == str(checkout.profile)
    assert ran.data["command"][0] == str(checkout.root / "build" / "simc")
    assert ran.data["command"][1] == str(checkout.profile)
    assert "log=1" in ran.data["command"]
    assert combat_log.is_file()

    logged = run("simc", "log-actions", str(combat_log), "tiger_palm", "rising_sun_kick")
    assert logged.data["log_path"] == str(combat_log)
    assert logged.data["actions"] == ["tiger_palm", "rising_sun_kick"]
    assert logged.data["count"] == 2
    hits = {hit["action"]: hit for hit in logged.data["hits"]}
    assert hits["tiger_palm"]["performed_at"] is not None
    assert hits["tiger_palm"]["scheduled_at"] is not None
    assert all(hit["performed_at"] >= hit["scheduled_at"] for hit in logged.data["hits"] if hit["performed_at"] is not None)

    first_cast = run("simc", "first-cast", str(checkout.profile), "tiger_palm", "--seeds", "2", "--max-time", "30", timeout=300)
    assert first_cast.data["action"] == "tiger_palm"
    assert first_cast.data["seeds"] == 2
    assert first_cast.data["summary"]["samples"] == 2
    assert first_cast.data["summary"]["found"] == 2
    assert first_cast.data["summary"]["min"] <= first_cast.data["summary"]["avg"] <= first_cast.data["summary"]["max"]
    assert [row["seed"] for row in first_cast.data["results"]] == [1, 2]


def test_analysis_packet_bundles_branch_intent_and_sampled_timing(require, checkout: Checkout) -> None:
    require("simc")
    result = run(
        "simc",
        "analysis-packet",
        str(checkout.apl),
        "--profile-path",
        str(checkout.profile),
        "--sim-profile",
        str(checkout.profile),
        "--first-cast-action",
        "tiger_palm",
        "--seeds",
        "2",
        "--max-time",
        "30",
        "--intent-limit",
        "4",
        timeout=300,
    )
    packet = result.data["packet"]
    assert result.data["build"]["actor_class"] == ACTOR_CLASS
    assert packet["start_list"] == "default"
    assert packet["dispatch_certainty"] in {"guaranteed", "unresolved", "possible"}
    assert packet["intent_lines"] and len(packet["intent_lines"]) <= 4
    assert packet["next_steps"], result.describe()
    assert isinstance(packet["explained_intent"], dict)
    assert isinstance(packet["branch_summary"], dict)
    first_cast = packet["first_casts"]
    assert len(first_cast) == 1
    assert first_cast[0]["action"] == "tiger_palm"
    assert first_cast[0]["samples"] == 2
    assert first_cast[0]["found"] == 2
    assert first_cast[0]["min_time"] <= first_cast[0]["max_time"]


def test_harness_validate_compare_and_report_workflow(require, checkout: Checkout, out_dir: Path) -> None:
    """The documented comparison workflow, end to end, without touching the checkout."""
    require("simc")
    harness_path = out_dir / "harness.simc"
    harness = run("simc", "build-harness", "--profile-path", str(checkout.profile), "--out", str(harness_path), "--line", "role=attack")
    assert harness.data["path"] == str(harness_path)
    assert harness.data["build_spec"]["talents"] == checkout.talents
    assert harness.data["extra_lines"] == ["role=attack"]
    harness_text = harness_path.read_text()
    assert f"talents={checkout.talents}" in harness_text
    assert "load_default_gear=1" in harness_text
    assert "role=attack" in harness_text
    assert "actions" not in harness_text

    validated = run("simc", "validate-apl", str(harness_path), str(checkout.apl), "--label", "base", "--out-dir", str(out_dir), timeout=300)
    assert validated.data["valid"] is True
    assert validated.data["returncode"] == 0
    assert validated.data["label"] == "base"
    generated = Path(validated.data["profile_path"])
    assert generated.is_file()
    assert "actions" in generated.read_text()

    report_path = out_dir / "report.json"
    compared = run(
        "simc",
        "compare-apls",
        str(harness_path),
        "--base-apl",
        str(checkout.apl),
        "--variant",
        f"assisted={checkout.assisted_apl}",
        "--iterations",
        "50",
        "--threads",
        "1",
        "--out-dir",
        str(out_dir),
        "--report-out",
        str(report_path),
        timeout=300,
    )
    assert compared.data["iterations"] == 50
    assert compared.data["threads"] == 1
    assert {row["label"] for row in compared.data["validations"]} == {"base", "assisted"}
    assert all(row["valid"] is True for row in compared.data["validations"])
    assert compared.data["base"]["label"] == "base"
    assert compared.data["base"]["dps"] > 0
    assert compared.data["base"]["action_counts"]
    labels = [row["label"] for row in compared.data["ranking"]]
    assert set(labels) == {"base", "assisted"}
    dps_values = [row["dps"] for row in compared.data["ranking"]]
    assert dps_values == sorted(dps_values, reverse=True)
    assert compared.data["report_path"] == str(report_path)
    assert report_path.is_file()

    summary = run("simc", "variant-report", str(report_path))
    assert summary.data["report_path"] == str(report_path)
    assert summary.data["base_label"] == "base"
    assert summary.data["best_label"] == labels[0]
    assert summary.data["best_dps"] == dps_values[0]
    assert {row["label"] for row in summary.data["ranking"]} == {"base", "assisted"}
    base_row = next(row for row in summary.data["ranking"] if row["label"] == "base")
    assert base_row["delta_vs_base"] == 0.0
    assert summary.data["comparisons"][0]["top_action_deltas"]


# --- talent transport ---


def test_validate_talent_transport_round_trips_raw_rows(require, checkout: Checkout) -> None:
    """Raw ``entry:node:rank`` rows (the shape a log packet carries) become validated SimC forms."""
    require("simc")
    decoded = run("simc", "decode-build", "--talents", checkout.talents, "--actor-class", ACTOR_CLASS, "--spec", SPEC)
    rows = _talent_rows_from_build(checkout, decoded.data["decoded"])
    assert len(rows) > 30, "expected the decoded build to yield raw transport rows"

    args = ["validate-talent-transport", "--actor-class", ACTOR_CLASS, "--spec", SPEC]
    for row in rows:
        args += ["--talent-row", row]
    result = run("simc", *args, timeout=300)

    assert result.data["input"]["source"] == "talent_rows"
    assert result.data["input"]["talent_row_count"] == len(rows)
    assert result.data["transport_status"] == "validated"
    split = result.data["transport_forms"]["simc_split_talents"]
    assert split["class_talents"] and split["spec_talents"] and split["hero_talents"]
    validation = result.data["validation"]
    assert validation["status"] == "validated", json.dumps(validation)[:600]
    assert validation["source"] == "simc_trait_data_round_trip"
    assert validation["actor_class"] == ACTOR_CLASS
    assert validation["spec"] == SPEC
    assert len(validation["resolved_entries"]) == len(rows)
    assert all(entry["tree"] in {"class", "spec", "hero"} and entry["name"] for entry in validation["resolved_entries"])

    # The validated split talents must decode back into the same talents the rows described.
    redecoded = run(
        "simc",
        "decode-build",
        "--class-talents",
        split["class_talents"],
        "--spec-talents",
        split["spec_talents"],
        "--hero-talents",
        split["hero_talents"],
        "--actor-class",
        ACTOR_CLASS,
        "--spec",
        SPEC,
    )
    expected_tokens = {entry["token"] for entry in validation["resolved_entries"]}
    assert expected_tokens <= set(redecoded.data["decoded"]["enabled_talents"])


def test_validate_talent_transport_rejects_a_malformed_row(require) -> None:
    require("simc")
    result = run(
        "simc",
        "validate-talent-transport",
        "--actor-class",
        ACTOR_CLASS,
        "--spec",
        SPEC,
        "--talent-row",
        "not-a-row",
        expect=EXIT_GENERIC,
        error_code="invalid_talent_row",
    )
    assert "entry_id:node_id:rank" in result.payload["error"]["message"]


def test_validate_talent_transport_rejects_a_malformed_packet(require, out_dir: Path) -> None:
    require("simc")
    packet_path = out_dir / "packet.json"
    packet_path.write_text('{"kind": "talent_transport_packet"}')
    result = run(
        "simc",
        "validate-talent-transport",
        "--build-packet",
        str(packet_path),
        expect=EXIT_GENERIC,
        error_code="invalid_build_packet",
    )
    assert "transport status" in result.payload["error"]["message"]


# --- global flags ---


def test_fields_and_compact_shape_the_payload(require, checkout: Checkout) -> None:
    require("simc")
    # --fields prunes the envelope itself on success, so this journey reads the raw stdout.
    filtered = run_raw("simc", "--fields", "data.repo.root,data.status", "doctor")
    assert filtered.exit_code == 0, filtered.describe()
    assert json.loads(filtered.stdout) == {"data": {"repo": {"root": str(checkout.root)}, "status": "ready"}}

    strict = run("simc", "--fields", "data.not_a_field", "--fields-strict", "doctor", expect=EXIT_USAGE, error_code="missing_fields")
    assert strict.payload["error"]["details"]["missing_fields"] == ["data.not_a_field"]

    full = run("simc", "apl-lists", str(checkout.apl), "--list", "default")
    compact = run("simc", "--compact", "--compact-max-chars", "40", "apl-lists", str(checkout.apl), "--list", "default")
    full_entries = full.data["lists"][0]["entries"]
    compact_entries = compact.data["lists"][0]["entries"]
    assert len(compact_entries) == len(full_entries)
    assert all(len(entry["raw"]) <= 43 for entry in compact_entries)
    truncated = [entry for entry in compact_entries if entry["raw"].endswith("...")]
    assert truncated, compact.describe()
    assert any(len(entry["raw"]) > 43 for entry in full_entries)


def test_pretty_and_profile_presets_still_emit_one_envelope(require) -> None:
    require("simc")
    agent = run("simc", "--profile", "agent", "doctor")
    assert "\n" not in agent.stdout.strip()
    for args in (("--pretty",), ("--profile", "human"), ("--profile", "debug")):
        pretty = run("simc", *args, "doctor")
        assert "\n  " in pretty.stdout, pretty.describe()
        assert pretty.payload["kind"] == agent.payload["kind"]
    # simc issues no HTTP requests and records no timings, so the debug preset has no diagnostics
    # block to attach; it only changes the formatting.
    assert "diagnostics" not in run("simc", "--profile", "debug", "doctor").payload


# --- error journeys ---


def test_a_missing_repo_root_fails_with_the_documented_codes(require, out_dir: Path) -> None:
    require("simc")
    missing = out_dir / "missing-checkout"

    version = run("simc", "--repo-root", str(missing), "version", expect=EXIT_GENERIC, error_code="missing_binary")
    assert str(missing / "build" / "simc") in version.payload["error"]["message"]

    synced = run("simc", "--repo-root", str(missing), "sync", expect=EXIT_GENERIC, error_code="missing_repo")
    assert str(missing) in synced.payload["error"]["message"]

    decoded = run(
        "simc",
        "--repo-root",
        str(missing),
        "decode-build",
        "--talents",
        "CEQ",
        "--actor-class",
        ACTOR_CLASS,
        "--spec",
        SPEC,
        expect=EXIT_GENERIC,
        error_code="decode_failed",
    )
    assert "SimC binary not found" in decoded.payload["error"]["message"]

    run("simc", "--repo-root", str(missing), "build", expect=EXIT_GENERIC, error_code="missing_build_dir")


def test_bad_input_paths_exit_4(require, checkout: Checkout, out_dir: Path) -> None:
    require("simc")
    missing = out_dir / "nope.simc"
    for args in (
        ("sim", str(missing)),
        ("run", str(missing)),
        ("apl-lists", str(missing)),
        ("apl-graph", str(missing)),
        ("priority", str(missing)),
        ("log-actions", str(missing), "tiger_palm"),
        ("first-cast", str(missing), "tiger_palm"),
        ("variant-report", str(missing)),
        ("inspect", str(missing)),
    ):
        result = run("simc", *args, expect=EXIT_NOT_FOUND, error_code="not_found")
        assert str(missing) in result.payload["error"]["message"], result.describe()

    invalid_harness = run(
        "simc",
        "validate-apl",
        str(missing),
        str(checkout.apl),
        expect=EXIT_GENERIC,
        error_code="validate_apl_failed",
    )
    assert str(missing) in invalid_harness.payload["error"]["message"]


def test_usage_errors_exit_2_without_a_traceback(require) -> None:
    require("simc")
    missing_argument = run_raw("simc", "apl-lists")
    assert missing_argument.exit_code == EXIT_USAGE, missing_argument.describe()
    assert "Traceback" not in missing_argument.stderr

    run("simc", "validate-talent-transport", expect=EXIT_USAGE, error_code="invalid_query")
    run("simc", "repo", "--set-root", "/tmp", "--clear-root", expect=EXIT_USAGE, error_code="invalid_query")


def test_sync_refuses_a_dirty_worktree_instead_of_pulling(require, out_dir: Path) -> None:
    """The guard is exercised on a throwaway git repo; the real checkout is never pulled."""
    require("simc")
    fake_repo = out_dir / "dirty-checkout"
    fake_repo.mkdir()
    (fake_repo / ".git").mkdir()
    (fake_repo / "untracked.txt").write_text("local change\n")
    # A bare .git directory is enough for `git status --short` to report the untracked file.
    (fake_repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (fake_repo / ".git" / "config").write_text("[core]\n\trepositoryformatversion = 0\n")
    (fake_repo / ".git" / "objects").mkdir()
    (fake_repo / ".git" / "refs").mkdir()

    result = run("simc", "--repo-root", str(fake_repo), "sync")
    assert result.data["status"] == "skipped"
    assert result.data["reason"] == "dirty_worktree"
    assert result.data["git"]["dirty"] is True
    assert result.data["git"]["dirty_entries"]


def test_checkout_reports_a_failed_managed_update(require, out_dir: Path) -> None:
    """Point the managed data root at a non-git directory so checkout fails instead of cloning."""
    require("simc")
    data_home = out_dir / "data"
    (data_home / "warcraft" / "simc" / "repo").mkdir(parents=True)
    result = run(
        "simc",
        "checkout",
        env={"XDG_DATA_HOME": str(data_home)},
        expect=EXIT_GENERIC,
        error_code="checkout_failed",
    )
    assert "not a git repository" in result.payload["error"]["message"]

"""Drive the real SimC binary in the discovered checkout over its own stock MID1 profiles.

The bugs this file guards against were invisible to mocked tests: SimC rejected a talent hash, printed
the handful of freely granted talents first, and the CLI reported that stub as a successful decode; and
a re-encoded build silently carried the keystone of a hero tree the build never selected.

Opt in by pointing ``WARCRAFT_SIMC_TESTS_REPO`` at a SimulationCraft checkout with a built binary; the
configured or managed checkout is never read. Without the variable the file is skipped, loudly, so it
proves nothing on CI - ``docs/simc/README.md`` says so under "Tests that need the binary". The
captured-output tests in ``test_simc_build_input.py`` and ``test_simc_cli.py`` cover the same logic
everywhere else.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any

import pytest
from simc_cli.build_input import BuildResolution, BuildSpec, SimcBuildError, decode_build, encode_build, tree_entries_string
from simc_cli.main import app as simc_app
from simc_cli.repo import RepoPaths, discover_repo
from simc_cli.trait_data import TraitTable, load_trait_table
from typer.testing import CliRunner
from warcraft_core.talent_transport import CLASS_ID_BY_ACTOR_CLASS, specialization_ids

ACTOR_CLASS_LINE = re.compile(
    r'^(deathknight|demonhunter|druid|evoker|hunter|mage|monk|paladin|priest|rogue|shaman|warlock|warrior)='
)
# A max-level stock profile fills its trees. A decode that returns a handful of talents is a stub of
# freely granted ones, which is exactly the failure mode this file exists for.
MINIMUM_TALENTS_IN_A_STOCK_BUILD = 30
REPO_ENV = "WARCRAFT_SIMC_TESTS_REPO"


@dataclass(frozen=True, slots=True)
class _Decoded:
    """One stock profile and what the binary made of it; ``resolution`` is None when it was rejected."""

    name: str
    build_spec: BuildSpec
    resolution: BuildResolution | None


def _stock_profiles(repo: RepoPaths) -> list[tuple[str, BuildSpec]]:
    specs: list[tuple[str, BuildSpec]] = []
    for path in sorted((repo.root / "profiles" / "MID1").glob("*.simc")):
        lines = path.read_text().splitlines()
        actor_class = next((m.group(1) for line in lines if (m := ACTOR_CLASS_LINE.match(line))), None)
        spec = next((line.split("=", 1)[1] for line in lines if line.startswith("spec=")), None)
        talents = next((line.split("=", 1)[1] for line in lines if line.startswith("talents=")), None)
        if actor_class and spec and talents:
            specs.append((path.name, BuildSpec(actor_class=actor_class, spec=spec, talents=talents)))
    return specs


@pytest.fixture(scope="module")
def repo() -> RepoPaths:
    root = os.environ.get(REPO_ENV, "").strip()
    if not root:
        pytest.skip(f"REAL-BINARY TEST SKIPPED: set {REPO_ENV} to a SimulationCraft checkout with a built binary")
    discovered = discover_repo(root)
    if not discovered.build_simc.exists():
        pytest.fail(f"{REPO_ENV}={root} has no built SimC binary at {discovered.build_simc} (run `simc build`)")
    return discovered


@pytest.fixture(scope="module")
def decoded_profiles(repo: RepoPaths) -> list[_Decoded]:
    """Decode every stock profile once; the whole file reads this one pass over the real binary."""
    profiles = _stock_profiles(repo)
    if not profiles:
        pytest.skip(f"REAL-BINARY TEST SKIPPED: no stock MID1 profiles under {repo.root / 'profiles' / 'MID1'}")
    decoded: list[_Decoded] = []
    for name, build_spec in profiles:
        try:
            resolution = decode_build(repo, build_spec)
        except SimcBuildError:
            # An older binary genuinely cannot read a newer hash; `simc doctor` reports the mismatch.
            resolution = None
        decoded.append(_Decoded(name=name, build_spec=build_spec, resolution=resolution))
    return decoded


def test_every_stock_profile_decodes_whole_or_fails_loudly(decoded_profiles: list[_Decoded]) -> None:
    """No stock profile may come back as a successful decode holding a fraction of its talents."""
    truncated = [
        f"{item.name}: {len(item.resolution.enabled_talents)} talents, hero tree {item.resolution.hero_tree}"
        for item in decoded_profiles
        if item.resolution is not None
        and (len(item.resolution.enabled_talents) < MINIMUM_TALENTS_IN_A_STOCK_BUILD or item.resolution.hero_tree is None)
    ]
    rejected = [item.name for item in decoded_profiles if item.resolution is None]

    assert truncated == []
    assert len(rejected) < len(decoded_profiles), "every stock profile was rejected; the binary is unusable"


def test_a_decoded_stock_build_keeps_only_the_activated_hero_tree(repo: RepoPaths, decoded_profiles: list[_Decoded]) -> None:
    """A hash grants both hero keystones; the sim only ever runs the activated tree's talents."""
    sub_tree_by_entry = load_trait_table(repo.root).hero_sub_tree_by_entry
    leaked: list[str] = []
    carrying_an_unselected_tree = 0
    for item in decoded_profiles:
        resolution = item.resolution
        if resolution is None or resolution.hero_tree is None:
            continue
        if resolution.inactive_hero_talents:
            carrying_an_unselected_tree += 1
        foreign = sorted(
            talent.name
            for talent in resolution.talents_by_tree["hero"]
            if sub_tree_by_entry.get(talent.entry, resolution.hero_tree.id) != resolution.hero_tree.id
        )
        if foreign:
            leaked.append(f"{item.name}: {foreign}")

    assert leaked == []
    assert carrying_an_unselected_tree > 0, "no stock hash granted an unselected hero tree, so this check proved nothing"


def test_a_no_op_modify_build_discloses_the_hero_talents_the_reencode_adds(
    repo: RepoPaths, decoded_profiles: list[_Decoded]
) -> None:
    """Re-encoding regrants every hero keystone; what the export gained has to be in the payload."""
    subject = next(
        (
            item
            for item in decoded_profiles
            if item.resolution is not None
            and not item.resolution.inactive_hero_talents
            and any(talent.rank_known and talent.rank > 0 for talent in item.resolution.talents_by_tree["spec"])
        ),
        None,
    )
    assert subject is not None, "no stock profile was usable as a no-op modify-build subject"
    assert subject.resolution is not None
    talent = next(t for t in subject.resolution.talents_by_tree["spec"] if t.rank_known and t.rank > 0)

    result = CliRunner().invoke(
        simc_app,
        [
            "--repo-root", str(repo.root), "modify-build",
            "--talents", str(subject.build_spec.talents),
            "--actor-class", str(subject.build_spec.actor_class),
            "--spec", str(subject.build_spec.spec),
            "--add", f"{talent.entry}:{talent.rank}",
        ],
    )

    assert result.exit_code == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)["data"]["result"]
    export_resolution = decode_build(
        repo,
        BuildSpec(
            actor_class=subject.build_spec.actor_class,
            spec=subject.build_spec.spec,
            talents=payload["talents_export"],
        ),
    )
    gained = {t.entry for t in export_resolution.inactive_hero_talents}
    assert {row["entry"] for row in payload["diff_from_base"]["inactive_hero"]["added"]} == gained
    assert bool(payload["disclosures"]) is bool(gained)


def test_a_tree_swap_round_trips_a_build_that_holds_a_tiered_node(repo: RepoPaths, decoded_profiles: list[_Decoded]) -> None:
    """SimC prints a tiered node's leftover rank, so re-serializing one used to drop the whole node.

    A tree swap rebuilds every tree from the decode instead of from the base hash, so a tiered node
    anywhere in the build made `modify-build` fail with `encode_mismatch` and emit no export.
    """
    tiered_entries = load_trait_table(repo.root).tiered_siblings_by_entry
    subject = next(
        (
            item
            for item in decoded_profiles
            if item.resolution is not None
            and any(
                talent.entry in tiered_entries
                for tree in ("class", "spec", "hero")
                for talent in item.resolution.talents_by_tree[tree]
            )
        ),
        None,
    )
    assert subject is not None, "no stock profile held a tiered node, so this check proved nothing"
    assert subject.resolution is not None

    result = CliRunner().invoke(
        simc_app,
        [
            "--repo-root", str(repo.root), "modify-build",
            "--talents", str(subject.build_spec.talents),
            "--actor-class", str(subject.build_spec.actor_class),
            "--spec", str(subject.build_spec.spec),
            "--swap-hero-tree-from", str(subject.build_spec.talents),
        ],
    )

    assert result.exit_code == 0, f"{subject.name}: {result.stdout}{result.stderr}"
    export = json.loads(result.stdout)["data"]["result"]["talents_export"]
    reencoded = decode_build(
        repo,
        BuildSpec(actor_class=subject.build_spec.actor_class, spec=subject.build_spec.spec, talents=export),
    )
    assert reencoded.enabled_talents == subject.resolution.enabled_talents


@pytest.mark.parametrize("swap", ["--swap-spec-tree-from", "--swap-hero-tree-from"])
def test_a_tree_swap_round_trips_a_build_with_no_hero_tree_selected(
    repo: RepoPaths, decoded_profiles: list[_Decoded], swap: str
) -> None:
    """SimC's own default talents select no hero tree; such a hash holds only the freely granted keystones.

    A tree swap spelled those keystones out, which made SimC select a hero tree and disable the other
    keystone, so every swap on such a build failed with `encode_mismatch`.
    """
    subject = next(item for item in decoded_profiles if item.resolution is not None)
    assert subject.resolution is not None
    actor_class, spec = subject.build_spec.actor_class, subject.build_spec.spec
    trees = subject.resolution.talents_by_tree
    no_hero_tree = encode_build(
        repo,
        BuildSpec(
            actor_class=actor_class,
            spec=spec,
            class_talents=tree_entries_string(trees["class"]),
            spec_talents=tree_entries_string(trees["spec"]),
        ),
    )
    base = decode_build(repo, BuildSpec(actor_class=actor_class, spec=spec, talents=no_hero_tree))
    assert base.hero_tree is None, f"{subject.name} without hero talents still selects a hero tree; this proves nothing"

    result = CliRunner().invoke(
        simc_app,
        [
            "--repo-root", str(repo.root), "modify-build", "--talents", no_hero_tree,
            "--actor-class", str(actor_class), "--spec", str(spec), swap, no_hero_tree,
        ],
    )

    assert result.exit_code == 0, f"{subject.name}: {result.stdout}{result.stderr}"
    export = json.loads(result.stdout)["data"]["result"]["talents_export"]
    reencoded = decode_build(repo, BuildSpec(actor_class=actor_class, spec=spec, talents=export))
    assert reencoded.enabled_talents == base.enabled_talents


def test_removing_a_tiered_talent_by_name_removes_every_entry_of_its_node(
    repo: RepoPaths, decoded_profiles: list[_Decoded]
) -> None:
    """A tiered node decodes as one row per entry, all sharing the talent's name.

    Only the entry the name resolved to used to count as requested, so the node's other entries were
    reported as unrequested changes and `modify-build --remove <name>` emitted no export.
    """
    tiered_entries = load_trait_table(repo.root).tiered_siblings_by_entry
    subject, talent = next(
        (
            (item, talent)
            for item in decoded_profiles
            if item.resolution is not None
            for talent in item.resolution.talents_by_tree["spec"]
            if talent.entry in tiered_entries and talent.rank_known
        ),
        (None, None),
    )
    assert subject is not None and talent is not None, "no stock profile held a tiered spec talent, so this check proved nothing"
    node_entries = {sibling.entry for sibling in tiered_entries[talent.entry]}

    result = CliRunner().invoke(
        simc_app,
        [
            "--repo-root", str(repo.root), "modify-build",
            "--talents", str(subject.build_spec.talents),
            "--actor-class", str(subject.build_spec.actor_class),
            "--spec", str(subject.build_spec.spec),
            "--remove", talent.name,
        ],
    )

    assert result.exit_code == 0, f"{subject.name} --remove {talent.name!r}: {result.stdout}{result.stderr}"
    export = json.loads(result.stdout)["data"]["result"]["talents_export"]
    reencoded = decode_build(
        repo,
        BuildSpec(actor_class=subject.build_spec.actor_class, spec=subject.build_spec.spec, talents=export),
    )
    assert not node_entries & {t.entry for t in reencoded.talents_by_tree["spec"] if t.taken}


# Healer builds, which no stock profile covers: a Mistweaver export captured from Method's talent page
# and the Holy Priest talents this checkout's SimC loads by default (`load_default_talents=1`).
HEALER_BUILDS = [
    ("monk", "mistweaver", "C4QAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAM2mB2sYGzMbzYDzMDzsstMzYhZ0MmBMYwYWmZmZY2GmhZZmAAAAAz20ysNzysBAAAAwMzAADwiMAA"),
    ("priest", "holy", "CEQAAAAAAAAAAAAAAAAAAAAAAwYAAAAAAAgZmlxMjZGDzMzYZGmBAAAwMmlZwMzMWmxMDgZKAAQAAAgZmZBQzgxYYmBAzAD"),
]


@pytest.mark.parametrize(("actor_class", "spec", "talents"), HEALER_BUILDS, ids=["mistweaver", "holy-priest"])
def test_a_healer_build_identifies_and_can_be_modified(repo: RepoPaths, actor_class: str, spec: str, talents: str) -> None:
    """SimC refuses to simulate some healers and used to reject every encode of them.

    Mistweaver and Holy Paladin are always silenced, which aborted profile generation with "No active
    players in sim!"; Holy Priest's stale default APL failed on divine_star. Both were reported as
    `invalid_build`, blaming a valid build for the harness.
    """
    base = decode_build(repo, BuildSpec(actor_class=actor_class, spec=spec, talents=talents))
    talent = next(t for t in base.talents_by_tree["spec"] if t.rank_known and t.rank > 0)

    result = CliRunner().invoke(
        simc_app, ["--repo-root", str(repo.root), "modify-build", "--talents", talents, "--remove", talent.name]
    )

    assert result.exit_code == 0, result.stdout + result.stderr
    data = json.loads(result.stdout)["data"]
    assert (data["base"]["actor_class"], data["base"]["spec"]) == (actor_class, spec)
    reencoded = decode_build(repo, BuildSpec(actor_class=actor_class, spec=spec, talents=data["result"]["talents_export"]))
    assert talent.entry not in {t.entry for t in reencoded.talents_by_tree["spec"] if t.taken}


def _modify(repo: RepoPaths, item: _Decoded, *edits: str) -> tuple[int, dict[str, Any]]:
    result = CliRunner().invoke(
        simc_app,
        [
            "--repo-root", str(repo.root), "modify-build",
            "--talents", str(item.build_spec.talents),
            "--actor-class", str(item.build_spec.actor_class),
            "--spec", str(item.build_spec.spec),
            *edits,
        ],
    )
    return result.exit_code, json.loads(result.stdout or result.stderr)


def _taken_entries(repo: RepoPaths, item: _Decoded, export: str) -> set[int]:
    decoded = decode_build(
        repo, BuildSpec(actor_class=item.build_spec.actor_class, spec=item.build_spec.spec, talents=export)
    )
    return {t.entry for tree in ("class", "spec") for t in decoded.talents_by_tree[tree] if t.taken}


def _spec_ids(repo: RepoPaths, item: _Decoded) -> tuple[int, int]:
    actor_class, spec = str(item.build_spec.actor_class), str(item.build_spec.spec)
    return CLASS_ID_BY_ACTOR_CLASS[actor_class], specialization_ids(repo.root)[(actor_class, spec)]


def _choice_partner(table: TraitTable, repo: RepoPaths, item: _Decoded) -> tuple[str, int] | None:
    """A spec-tree choice talent the build takes, and the other entry of its node, which it does not."""
    assert item.resolution is not None
    class_id, spec_id = _spec_ids(repo, item)
    for talent in item.resolution.talents_by_tree["spec"]:
        node = table.choice_node_by_entry.get(talent.entry)
        partners = [
            entry for entry, other in table.choice_node_by_entry.items()
            if other == node and entry != talent.entry and table.tree_for_entry(entry, class_id=class_id, spec_id=spec_id) == "spec"
        ]
        if node is not None and talent.taken and len(partners) == 1:
            return talent.name, partners[0]
    return None


def test_adding_the_other_choice_of_a_taken_choice_node_needs_the_taken_one_removed(
    repo: RepoPaths, decoded_profiles: list[_Decoded]
) -> None:
    """The hash holds one entry per choice node; the add alone used to come back unchanged and verified."""
    table = load_trait_table(repo.root)
    subject, pair = next(
        ((item, pair) for item in decoded_profiles if item.resolution is not None and (pair := _choice_partner(table, repo, item))),
        (None, None),
    )
    assert subject is not None and pair is not None, "no stock profile took a spec choice node, so this check proved nothing"
    taken_name, partner = pair

    refused_code, refused = _modify(repo, subject, "--add", f"{partner}:1")
    swapped_code, swapped = _modify(repo, subject, "--add", f"{partner}:1", "--remove", taken_name)

    assert refused_code == 2, refused
    assert refused["error"]["code"] == "invalid_argument"
    assert swapped_code == 0, f"{subject.name}: {swapped}"
    taken = _taken_entries(repo, subject, swapped["data"]["result"]["talents_export"])
    assert partner in taken


def test_an_added_talent_the_base_lacks_is_in_the_export(repo: RepoPaths, decoded_profiles: list[_Decoded]) -> None:
    """Every earlier real-binary add re-added a talent the build already had, so a dropped add passed."""
    table = load_trait_table(repo.root)
    subject = next(item for item in decoded_profiles if item.resolution is not None)
    assert subject.resolution is not None
    class_id, spec_id = _spec_ids(repo, subject)
    present = {t.entry for tree in ("class", "spec", "hero") for t in subject.resolution.talents_by_tree[tree]}
    entry = next(
        entry for entry, tree in sorted(table.tree_by_entry.items())
        if tree == "spec" and entry not in present and entry not in table.choice_node_by_entry
        and entry not in table.tiered_siblings_by_entry and table.tree_for_entry(entry, class_id=class_id, spec_id=spec_id)
    )

    exit_code, payload = _modify(repo, subject, "--add", f"{entry}:1")

    assert exit_code == 0, f"{subject.name} --add {entry}:1: {payload}"
    assert entry in _taken_entries(repo, subject, payload["data"]["result"]["talents_export"])

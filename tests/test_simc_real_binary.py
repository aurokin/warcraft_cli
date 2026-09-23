"""Drive the real SimC binary in the discovered checkout over its own stock MID1 profiles.

The bugs this file guards against were invisible to mocked tests: SimC rejected a talent hash, printed
the handful of freely granted talents first, and the CLI reported that stub as a successful decode; and
a re-encoded build silently carried the keystone of a hero tree the build never selected.

Needs the local SimulationCraft checkout and a built binary. It is skipped, loudly, without them, so
it proves nothing on CI - ``docs/simc/README.md`` says so under "Tests that need the binary". The
captured-output tests in ``test_simc_build_input.py`` and ``test_simc_cli.py`` cover the same logic
everywhere else.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

import pytest
from simc_cli.build_input import BuildResolution, BuildSpec, SimcBuildError, decode_build
from simc_cli.main import app as simc_app
from simc_cli.repo import RepoPaths, discover_repo
from simc_cli.trait_data import load_trait_table
from typer.testing import CliRunner

ACTOR_CLASS_LINE = re.compile(
    r'^(deathknight|demonhunter|druid|evoker|hunter|mage|monk|paladin|priest|rogue|shaman|warlock|warrior)='
)
# A max-level stock profile fills its trees. A decode that returns a handful of talents is a stub of
# freely granted ones, which is exactly the failure mode this file exists for.
MINIMUM_TALENTS_IN_A_STOCK_BUILD = 30


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
    discovered = discover_repo()
    if not discovered.build_simc.exists():
        pytest.skip(f"REAL-BINARY TEST SKIPPED: no built SimC binary at {discovered.build_simc} (run `simc build`)")
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

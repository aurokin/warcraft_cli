"""End-to-end journeys for the wrapper's guide and SimC composites.

This is the headline flow in skills/warcraft/SKILL.md: resolve one guide query across the guide
providers, export the bundles, compare them, and hand their explicit build references to simc.
Everything here talks to the real guide sites and the real local SimulationCraft checkout.

What a green run proves:

- every guide provider resolves the pinned query to its own main guide for the spec and exports it,
  and Icy Veins resolves a damage-spec query too;
- Method and Icy Veins each contribute at least one explicit build reference (Wowhead's guide export
  carries none), and the packet hands over exactly the unique references on disk (in order,
  truncation reported);
- simc identifies and decodes every handed-off build, with no class or spec supplied, as the class
  and spec the guide is for: the healer guide the pins name and a damage guide alike;
- a leg simc cannot run is reported with its own error code per build, and the summary status
  names the empty leg instead of reading as ``ok``;
- describe-build succeeds against the spec's own APL.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from tests.e2e.harness import (
    EXIT_GENERIC,
    EXIT_NETWORK,
    EXIT_NOT_FOUND,
    EXIT_USAGE,
    REPO_ROOT,
    Result,
    dead_proxy_env,
    no_cache_env,
    run,
)
from tests.e2e.pins import GUIDE_CLASS, GUIDE_QUERY, GUIDE_SPEC

GUIDE_PROVIDERS = ("wowhead", "method", "icy-veins")
# Each provider's main guide for the pinned query: the one guide-compare-query has to select.
GUIDE_REFS = {"wowhead": "3295", "method": "mistweaver-monk", "icy-veins": "mistweaver-monk-pve-healing-guide"}
# Method and Icy Veins export article bundles with build references; a Wowhead guide export has none.
BUILD_REFERENCE_PROVIDERS = {"method", "icy-veins"}
BUNDLE_FILES = ("manifest.json", "guide.json", "pages.jsonl", "sections.jsonl", "build-references.jsonl")
# Every page a bundle cites must come from that provider's own site.
PROVIDER_HOSTS = {"wowhead": "wowhead.com", "method": "method.gg", "icy-veins": "icy-veins.com"}
# Method and Icy Veins publish their builds as in-game loadout export strings.
GUIDE_REFERENCE_TYPE = "wow_talent_export"
SIMC_LEGS = ("identify", "decode", "describe")
# A damage spec SimC ships an APL for, so every leg of its handoff, describe included, must succeed.
DPS_GUIDE_QUERY = "fury warrior guide"
DPS_CLASS = "warrior"
DPS_SPEC = "fury"
# The mistweaver handoff with the pinned monk APL: identify and decode succeed, describe cannot.
HEALER_LEGS = {"identify": True, "decode": True, "describe": False}
# Every monk hero tree, so a label naming a tree none of the codes decode to is still caught.
MONK_HERO_TREES = ("Master of Harmony", "Conduit of the Celestials", "Shado-Pan")


@dataclass(frozen=True)
class Orchestration:
    """One real ``guide-compare-query`` run, shared by the journeys that read its output."""

    out_root: Path
    payload: dict[str, Any]
    bundle_paths: tuple[Path, ...]
    providers: tuple[str, ...]

    def bundle(self, provider: str) -> Path:
        return self.bundle_paths[self.providers.index(provider)]


def _default_apls(actor_class: str) -> list[Path]:
    spec_files = run("simc", "spec-files", actor_class)
    paths = [Path(item["path"]) for item in spec_files.data["categories"]["default_apl"]["items"]]
    assert paths, spec_files.describe()
    return paths


@pytest.fixture(scope="module")
def monk_apl() -> Path:
    """A default monk APL for a spec other than the guide's.

    SimulationCraft ships no mistweaver APL (it does not sim healers). Describing a mistweaver build
    against another spec's APL is the leg that must come back as a per-build failure.
    """
    paths = [path for path in _default_apls(GUIDE_CLASS) if GUIDE_SPEC not in path.name]
    assert paths, f"no non-{GUIDE_SPEC} {GUIDE_CLASS} APL in the checkout"
    return paths[0]


@pytest.fixture(scope="module")
def orchestration(tmp_path_factory: pytest.TempPathFactory) -> Orchestration:
    out_root = tmp_path_factory.mktemp("guide-compare-query")
    result = run("warcraft", "guide-compare-query", GUIDE_QUERY, "--out-root", str(out_root), timeout=300)
    payload = result.data
    assert payload["exported_bundle_count"] >= 2, result.describe()
    rows = payload["manifest"]["providers"]
    return Orchestration(
        out_root=out_root,
        payload=payload,
        bundle_paths=tuple(Path(row["bundle_path"]) for row in rows),
        providers=tuple(row["provider"] for row in rows),
    )


@pytest.fixture(scope="module")
def handoff(orchestration: Orchestration, monk_apl: Path) -> Result:
    """One real ``guide-builds-simc`` run over the orchestration root, with decode and describe on."""
    return run("warcraft", "guide-builds-simc", str(orchestration.out_root), "--apl-path", str(monk_apl), timeout=300)


def _bundle_rows(bundle_path: Path, name: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (bundle_path / name).read_text().splitlines() if line.strip()]


def _manifest(bundle_path: Path) -> dict[str, Any]:
    manifest: dict[str, Any] = json.loads((bundle_path / "manifest.json").read_text())
    return manifest


def _assert_bundle_on_disk(bundle_path: Path) -> None:
    """The exported bundle is complete, self-describing, and cited from its own provider's site."""
    manifest = _manifest(bundle_path)
    provider = manifest["provider"]
    host = PROVIDER_HOSTS[provider]
    assert manifest["exported_at"], bundle_path
    assert manifest["counts"]["sections"] == len(_bundle_rows(bundle_path, "sections.jsonl")) > 0, bundle_path
    if provider == "wowhead":
        # A Wowhead guide export is one guide page and its sections, with no build references.
        assert host in manifest["page"]["canonical_url"], json.dumps(manifest["page"])
        return
    for name in BUNDLE_FILES:
        assert (bundle_path / name).is_file(), f"{bundle_path} is missing {name}"

    pages = _bundle_rows(bundle_path, "pages.jsonl")
    assert pages, f"{bundle_path} exported no pages"
    assert all(host in page["page_url"] for page in pages), json.dumps(pages[:2])[:400]
    assert manifest["counts"]["pages"] == len(pages), f"{bundle_path} manifest miscounts pages"
    assert len(list((bundle_path / "pages").glob("*.html"))) == len(pages), "one raw page per row"

    references = _bundle_rows(bundle_path, "build-references.jsonl")
    assert references, f"{provider} exported a guide with no explicit build reference"
    assert manifest["counts"]["build_references"] == len(references)
    for reference in references:
        assert reference["reference_type"] == GUIDE_REFERENCE_TYPE, json.dumps(reference)[:200]
        assert reference["build_code"] and reference["url"] and reference["label"]
        assert reference["source_urls"] and all(host in url for url in reference["source_urls"])


def _disk_reference_urls(bundle_paths: tuple[Path, ...]) -> list[str]:
    """The unique build-reference URLs across the bundles on disk, in URL order (Wowhead exports have none)."""
    return sorted(
        {
            row["url"]
            for path in bundle_paths
            if _manifest(path)["provider"] in BUILD_REFERENCE_PROVIDERS
            for row in _bundle_rows(path, "build-references.jsonl")
        }
    )


def _assert_build_row(build: dict[str, Any], *, providers: set[str], bundle_paths: set[str]) -> None:
    """One handed-off build: an explicit reference and the citations it came from."""
    assert build["status"] == "handed_off", json.dumps(build)[:400]
    reference = build["reference"]
    assert reference["reference_type"] == GUIDE_REFERENCE_TYPE
    assert reference["build_code"] and reference["url"] and reference["label"]

    sources = build["sources"]
    assert sources, json.dumps(reference)[:200]
    for source in sources:
        assert source["provider"] in providers, source
        assert source["bundle_path"] in bundle_paths, source
        assert source["source_urls"], source
        assert all(PROVIDER_HOSTS[source["provider"]] in url for url in source["source_urls"]), source

    evidence = build["evidence"]
    assert evidence["explicit_build_reference_only"] is True
    assert evidence["source_count"] == len(sources)
    assert set(evidence["providers"]) == {source["provider"] for source in sources}
    assert set(evidence["bundle_paths"]) == {source["bundle_path"] for source in sources}


def _assert_handoff_packet(
    packet: dict[str, Any],
    provenance: dict[str, Any],
    *,
    bundle_paths: tuple[Path, ...],
    apl_path: Path | None,
    decode: bool,
    limit: int = 20,
) -> None:
    """The guide-build-to-simc packet hands over exactly the references on disk, fully cited.

    ``provenance`` is the envelope slot for the standalone command and a nested key when the packet
    is embedded in a guide-compare-query payload; both carry the same selection contract. The
    reference list is checked against the bundles on disk, not only against the packet's own
    counters: reading only part of a bundle, or merging distinct references, keeps a packet
    self-consistent.
    """
    assert provenance["explicit_build_reference_only"] is True
    assert provenance["selection_contract"] == "embedded_build_references_only"
    assert provenance["source_providers"]
    assert packet["freshness"]["status"] == "known"
    assert packet["freshness"]["sampled_at"]
    assert packet["bundle_count"] == len(bundle_paths)
    assert {Path(path) for path in packet["citations"]["bundle_paths"]} == set(bundle_paths)
    assert packet["decode_enabled"] is decode
    assert packet["apl_path"] == (str(apl_path) if apl_path else None)
    health = packet["bundle_health"]
    assert health["bundle_count"] == len(bundle_paths)
    assert health["failed_page_count"] == packet["summary"]["failed_page_count"] == 0, json.dumps(health)

    urls = _disk_reference_urls(bundle_paths)
    selected = urls[:limit]
    builds = packet["builds"]
    summary = packet["summary"]
    assert packet["build_reference_count"] == len(urls) >= 1, json.dumps(packet["citations"])[:400]
    assert packet["truncated"] is (len(urls) > limit)
    assert packet["citations"]["build_reference_urls"] == selected
    assert packet["excluded_builds"] == [], json.dumps(packet["excluded_builds"])[:400]
    assert [build["reference"]["url"] for build in builds] == selected
    assert summary["returned_build_count"] == len(builds)
    assert summary["excluded_build_count"] == len(urls) - len(builds)

    providers = set(provenance["source_providers"])
    paths = {str(path) for path in bundle_paths}
    for build in builds:
        _assert_build_row(build, providers=providers, bundle_paths=paths)


def _assert_leg(build: dict[str, Any], leg: str, *, succeeds: bool, actor_class: str, spec: str) -> dict[str, Any] | None:
    """One requested simc leg has its pinned outcome; returns the failure row it must produce.

    The only failure pinned is describe-build against another spec's APL (``invalid_query``, "the spec
    of the APL it is read against"), so any new failure mode turns the journey red.
    """
    section = build["simc"][leg]
    assert section["ok"] is succeeds, f"{leg}: {json.dumps(section)[:600]}"
    assert section["payload"]["ok"] is succeeds
    assert (section["exit_code"] == 0) is succeeds
    if succeeds:
        identity = section["payload"]["data"]["identity"]
        assert (identity["actor_class"], identity["spec"]) == (actor_class, spec), f"{leg}: {identity}"
        return None
    error = section["error"]
    assert error["code"] == section["payload"]["error"]["code"] == "invalid_query", f"{leg}: {error}"
    assert "the spec of the APL it is read against" in error["message"], f"{leg}: {error}"
    return {"leg": leg, **error}


def _assert_simc_legs(packet: dict[str, Any], *, actor_class: str, spec: str, expected: dict[str, bool]) -> None:
    """Every build gets the pinned outcome on every requested leg, and the summary agrees.

    ``expected`` maps each requested leg to whether it must succeed; a leg not in it was not
    requested and must be absent.
    """
    builds = packet["builds"]
    for build in builds:
        failures = []
        for leg in SIMC_LEGS:
            if leg not in expected:
                assert build["simc"][leg] is None, leg
                continue
            failure = _assert_leg(build, leg, succeeds=expected[leg], actor_class=actor_class, spec=spec)
            failures.extend([failure] if failure else [])
        assert build["failures"] == failures, json.dumps(build["failures"])[:400]
        # simc read the code it was handed, as the reference type the guide published.
        identified = build["simc"]["identify"]["payload"]["data"]["build_spec"]
        assert identified["talents"] == build["reference"]["build_code"]
        assert identified["source_kind"] == build["reference"]["reference_type"]
        if expected.get("decode"):
            assert build["simc"]["decode"]["payload"]["data"]["decoded"]["enabled_talents"]

    summary = packet["summary"]
    for leg in SIMC_LEGS:
        assert summary[f"{leg}_success_count"] == (len(builds) if expected.get(leg) else 0), json.dumps(summary)
    empty = [leg for leg in SIMC_LEGS if expected.get(leg) is False]
    assert summary["empty_requested_legs"] == empty, json.dumps(summary)
    assert summary["partial_requested_legs"] == [], json.dumps(summary)
    assert summary["simc_handoff_status"] == ("failed" if empty else "ok"), json.dumps(summary)


def test_guide_compare_query_exports_bundles_and_compares_them(require, orchestration: Orchestration) -> None:
    require("wowhead", "method", "icy-veins")
    payload = orchestration.payload
    assert payload["output_root"] == str(orchestration.out_root.resolve())
    assert payload["selected_providers"] == list(GUIDE_PROVIDERS)
    assert payload["force_refresh"] is False
    assert payload["max_age_hours"] == 24

    results = {row["provider"]: row for row in payload["provider_results"]}
    assert list(results) == list(GUIDE_PROVIDERS) == list(orchestration.providers)
    assert payload["exported_bundle_count"] == len(GUIDE_PROVIDERS)
    for provider, row in results.items():
        assert row["status"] == "exported", json.dumps(row)[:600]
        candidate = row["candidate"]
        assert (candidate["ref"], candidate["selection_source"]) == (GUIDE_REFS[provider], "resolve"), json.dumps(candidate)[:400]
        assert PROVIDER_HOSTS[provider] in candidate["url"], candidate
        assert row["freshness"]["status"] == "fresh", row["freshness"]
        assert row["export"]["ok"] is True

    manifest = payload["manifest"]
    assert manifest["kind"] == "guide_compare_orchestration_manifest"
    assert manifest["query"] == GUIDE_QUERY
    assert len(manifest["providers"]) == payload["exported_bundle_count"]
    manifest_path = orchestration.out_root / "manifest.json"
    assert manifest_path.is_file()
    assert json.loads(manifest_path.read_text())["providers"] == manifest["providers"]

    for bundle_path in orchestration.bundle_paths:
        _assert_bundle_on_disk(bundle_path)

    comparison = payload["comparison"]
    assert comparison["compared_bundle_count"] == payload["exported_bundle_count"]
    assert [(row["provider"], Path(row["path"])) for row in comparison["bundles"]] == list(
        zip(orchestration.providers, orchestration.bundle_paths, strict=True)
    )
    for row in comparison["bundles"]:
        assert row["title"] and row["counts"]["sections"] > 0, json.dumps(row)[:400]
    assert comparison["section_evidence"]["count"] > 0
    assert payload["simc_build_handoff"] is None


def test_guide_compare_query_reuses_fresh_bundles_until_force_refresh(require, orchestration: Orchestration) -> None:
    """A second run reuses fresh bundles; ``--force-refresh`` re-exports the same paths anyway."""
    require("wowhead", "method", "icy-veins")
    result = run("warcraft", "guide-compare-query", GUIDE_QUERY, "--out-root", str(orchestration.out_root), timeout=300)
    reused = [row for row in result.data["provider_results"] if row["status"] == "reused"]
    previous = {row["provider"]: row["exported_at"] for row in orchestration.payload["provider_results"] if row["status"] == "exported"}
    assert {row["provider"] for row in reused} == set(previous), result.describe()
    assert result.data["exported_bundle_count"] == orchestration.payload["exported_bundle_count"]
    for row in reused:
        assert row["freshness"]["status"] == "fresh"
        assert row["freshness"]["reason"] == "within_max_age"
        assert row["freshness"]["age_hours"] <= row["freshness"]["max_age_hours"]
        assert row["exported_at"] == previous[row["provider"]], result.describe()

    refreshed = run(
        "warcraft", "guide-compare-query", GUIDE_QUERY,
        "--out-root", str(orchestration.out_root), "--force-refresh", timeout=300,
    )
    assert refreshed.data["force_refresh"] is True
    rows = {row["provider"]: row for row in refreshed.data["provider_results"] if row["status"] == "exported"}
    assert set(rows) == set(previous), refreshed.describe()
    for provider, row in rows.items():
        assert row["exported_at"] > previous[provider], f"{provider} was not re-exported"
    assert [row["bundle_path"] for row in refreshed.data["manifest"]["providers"]] == [
        str(path) for path in orchestration.bundle_paths
    ]


def test_guide_compare_reads_two_exported_bundles(require, orchestration: Orchestration) -> None:
    require("wowhead", "method", "icy-veins")
    left, right = orchestration.bundle("method"), orchestration.bundle("icy-veins")
    result = run("warcraft", "guide-compare", str(left), str(right), timeout=300)
    data = result.data
    assert data["compared_bundle_count"] == 2
    assert {Path(row["path"]) for row in data["bundles"]} == {left, right}
    for row in data["bundles"]:
        assert row["provider"] in GUIDE_PROVIDERS
        assert row["title"] and row["exported_at"]
        assert row["counts"]["pages"] > 0
        assert row["counts"]["sections"] > 0

    evidence = data["section_evidence"]
    assert evidence["matching_rule"] == "exact_normalized_section_title"
    assert evidence["count"] == len(evidence["items"])
    assert evidence["count"] > 0
    assert {row["path"] for row in evidence["unique_by_bundle"]} == {str(left), str(right)}
    shared = [item for item in evidence["items"] if item["shared_across_all_bundles"]]
    assert shared, "two guides for the same spec must share at least one section title"
    for item in evidence["items"]:
        assert item["section_title_key"]
        assert 1 <= item["bundle_count"] <= 2
        assert item["shared_across_all_bundles"] is (item["bundle_count"] == 2)
        assert len(item["bundles"]) == item["bundle_count"]
        for bundle in item["bundles"]:
            # Provenance stays attached to every compared row.
            assert bundle["citations"], json.dumps(item)[:400]
            assert all(citation["page_url"] for citation in bundle["citations"])
    assert data["freshness"]
    assert data["comparison_evidence"]


@pytest.mark.parametrize(
    ("bundles", "exit_code", "error_code"),
    [
        (("empty", "empty"), EXIT_GENERIC, "invalid_bundle"),
        (("missing", "missing"), EXIT_NOT_FOUND, "not_found"),
        (("empty",), EXIT_USAGE, "invalid_argument"),
    ],
)
def test_guide_compare_rejects_what_it_cannot_compare(out_dir: Path, bundles: tuple[str, ...], exit_code: int, error_code: str) -> None:
    """A directory with no manifest is not a bundle, a missing path is not found, one bundle is a usage error."""
    (out_dir / "empty").mkdir()
    result = run("warcraft", "guide-compare", *(str(out_dir / name) for name in bundles), expect=exit_code, error_code=error_code)
    assert result.payload["kind"] == "error"
    assert result.payload["data"] == {}
    if len(bundles) > 1:
        assert result.payload["error"]["details"]["bundle"] == str(out_dir / bundles[0]), result.describe()


def test_guide_builds_simc_hands_the_orchestration_root_to_simc(
    require, orchestration: Orchestration, monk_apl: Path, handoff: Result
) -> None:
    """Healer guide builds decode with no class or spec supplied; describing them against another
    spec's APL fails per build, and the summary calls that leg ``failed`` rather than ``ok``.
    """
    require("wowhead", "method", "icy-veins", "simc")
    result = handoff
    packet = result.data
    assert result.payload["kind"] == "guide_builds_simc_handoff"
    assert packet["source"]["kind"] == "orchestration_root"
    assert packet["source"]["manifest_kind"] == "guide_compare_orchestration_manifest"
    assert packet["source"]["query"] == GUIDE_QUERY
    assert packet["freshness"]["reason"] == "orchestration_manifest_updated_at"
    _assert_handoff_packet(
        packet,
        result.payload["provenance"],
        bundle_paths=orchestration.bundle_paths,
        apl_path=monk_apl,
        decode=True,
    )
    _assert_simc_legs(packet, actor_class=GUIDE_CLASS, spec=GUIDE_SPEC, expected=HEALER_LEGS)
    # Every guide that publishes build references must reach the packet: one provider's extraction
    # rotting away is the regression that used to hide behind an empty build list.
    contributing = {source["provider"] for build in packet["builds"] for source in build["sources"]}
    assert contributing == BUILD_REFERENCE_PROVIDERS, json.dumps(packet["summary"])


def test_guide_builds_simc_reports_the_references_it_truncates(require, orchestration: Orchestration) -> None:
    """``--limit`` cuts the reference list, and the packet says so instead of shrinking silently."""
    require("wowhead", "method", "icy-veins", "simc")
    bundle_path = orchestration.bundle("icy-veins")
    assert len(_disk_reference_urls((bundle_path,))) >= 2, "the limit must provably cut something"
    result = run("warcraft", "guide-builds-simc", str(bundle_path), "--no-decode", "--limit", "1", timeout=300)
    packet = result.data
    assert result.payload["kind"] == "guide_builds_simc_handoff"
    assert packet["source"]["kind"] == "bundle"
    assert packet["freshness"]["reason"] == "bundle_manifest_exported_at"
    _assert_handoff_packet(
        packet,
        result.payload["provenance"],
        bundle_paths=(bundle_path,),
        apl_path=None,
        decode=False,
        limit=1,
    )
    _assert_simc_legs(packet, actor_class=GUIDE_CLASS, spec=GUIDE_SPEC, expected={"identify": True})


def test_guide_build_labels_name_the_hero_tree_their_code_decodes_to(require, handoff: Result) -> None:
    """A label that names a hero tree must name the tree its own code decodes to: pairing label i
    with code i+1 is the extraction bug this catches.
    """
    require("wowhead", "method", "icy-veins", "simc")
    trees = [
        (build["simc"]["decode"]["payload"]["data"]["decoded"]["hero_tree"]["name"], build["reference"]["label"])
        for build in handoff.data["builds"]
    ]
    assert trees and all(name in MONK_HERO_TREES for name, _label in trees), json.dumps(trees)
    assert any(name.lower() in label.lower() for name, label in trees), json.dumps(trees)
    for name, label in trees:
        mislabelled = [other for other in MONK_HERO_TREES if other != name and other.lower() in label.lower()]
        assert not mislabelled, f"{label!r} decodes to {name!r} but names {mislabelled}"


def test_a_damage_guide_query_resolves_and_hands_every_build_to_simc(require, out_dir: Path) -> None:
    """``fury warrior guide`` end to end: Icy Veins resolves its damage guide, and every build it
    publishes passes every simc leg, describe included, against the spec's own APL.

    Icy Veins once resolved no damage spec at all. SimC identifying each exported code as Fury is
    the oracle for which guide was selected.
    """
    require("wowhead", "icy-veins", "simc")
    apl_path = next(path for path in _default_apls(DPS_CLASS) if path.name == f"{DPS_CLASS}_{DPS_SPEC}.simc")
    out_root = out_dir / "damage"
    out_root.mkdir()
    result = run(
        "warcraft", "guide-compare-query", DPS_GUIDE_QUERY, "--out-root", str(out_root),
        "--provider", "wowhead", "--provider", "icy-veins", "--simc-build-handoff", "--simc-apl-path", str(apl_path),
        timeout=300,
    )
    rows = {row["provider"]: row for row in result.data["provider_results"]}
    assert rows["icy-veins"]["candidate"]["selection_source"] == "resolve", json.dumps(rows["icy-veins"])[:600]
    bundle_paths = tuple(Path(row["bundle_path"]) for row in result.data["manifest"]["providers"])
    for bundle_path in bundle_paths:
        _assert_bundle_on_disk(bundle_path)
    packet = result.data["simc_build_handoff"]
    _assert_handoff_packet(packet, packet["provenance"], bundle_paths=bundle_paths, apl_path=apl_path, decode=True)
    _assert_simc_legs(
        packet, actor_class=DPS_CLASS, spec=DPS_SPEC, expected={"identify": True, "decode": True, "describe": True}
    )


def test_guide_compare_query_can_emit_the_simc_build_handoff(require, monk_apl: Path, out_dir: Path) -> None:
    """``--simc-build-handoff`` folds the same guide-builds-simc packet into the orchestration."""
    require("wowhead", "method", "icy-veins", "simc")
    out_root = out_dir / "handoff"
    out_root.mkdir()
    result = run(
        "warcraft",
        "guide-compare-query",
        GUIDE_QUERY,
        "--out-root",
        str(out_root),
        "--provider",
        "method",
        "--provider",
        "icy-veins",
        "--simc-build-handoff",
        "--simc-apl-path",
        str(monk_apl),
        "--simc-build-limit",
        "5",
        timeout=300,
    )
    payload = result.data
    assert payload["selected_providers"] == ["method", "icy-veins"]
    assert payload["exported_bundle_count"] == 2
    bundle_paths = tuple(Path(row["bundle_path"]) for row in payload["manifest"]["providers"])
    assert len(_disk_reference_urls(bundle_paths)) > 5, "the build limit must provably cut something"
    packet = payload["simc_build_handoff"]
    assert packet is not None, result.describe()
    assert packet["kind"] == "guide_builds_simc_handoff"
    assert packet["source"]["path"] == str(out_root.resolve())
    _assert_handoff_packet(
        packet, packet["provenance"], bundle_paths=bundle_paths, apl_path=monk_apl, decode=True, limit=5
    )
    _assert_simc_legs(packet, actor_class=GUIDE_CLASS, spec=GUIDE_SPEC, expected=HEALER_LEGS)


def test_guide_compare_query_refuses_to_compare_fewer_than_two_guides(require, out_dir: Path) -> None:
    require("wowhead", "method", "icy-veins")
    out_root = out_dir / "nonsense"
    out_root.mkdir()
    result = run(
        "warcraft",
        "guide-compare-query",
        "zzqqxx nonsense guide query 12345",
        "--out-root",
        str(out_root),
        expect=EXIT_GENERIC,
        error_code="insufficient_guides",
        timeout=300,
    )
    assert result.payload["kind"] == "error"
    assert result.payload["data"] == {}
    # An agent has to be able to see which provider declined and why.
    details = result.payload["error"]["details"]
    assert details["exported_bundle_count"] == 0
    assert details["required_bundle_count"] == 2
    assert details["selected_providers"] == list(GUIDE_PROVIDERS)
    assert [row["provider"] for row in details["provider_results"]] == list(GUIDE_PROVIDERS)
    for row in details["provider_results"]:
        assert row["status"] == "skipped" and row["reason"] and row["error"] is None, row
    assert list(out_root.iterdir()) == []


def test_guide_compare_query_reports_an_outage_as_the_network_error(out_dir: Path) -> None:
    """An outage is exit 5 with every provider's own error, never "no guide found" (exit 1)."""
    out_root = out_dir / "outage"
    out_root.mkdir()
    result = run(
        "warcraft", "guide-compare-query", GUIDE_QUERY, "--out-root", str(out_root),
        env={**dead_proxy_env(), **no_cache_env()}, expect=EXIT_NETWORK, error_code="network_error",
    )
    rows = result.payload["error"]["details"]["provider_results"]
    assert [row["provider"] for row in rows] == list(GUIDE_PROVIDERS), result.describe()
    for row in rows:
        assert (row["status"], row["reason"], row["resolve_reason"]) == ("error", "provider_failed", "provider_failed"), row
        assert row["error"]["code"] == "network_error", row
    assert list(out_root.iterdir()) == []


def test_guide_compare_refuses_one_bundle_named_twice(require, orchestration: Orchestration) -> None:
    """The same bundle once absolute and once relative is one bundle, so there is nothing to compare."""
    require("wowhead", "method", "icy-veins")
    bundle = orchestration.bundle("method")
    run("warcraft", "guide-compare", str(bundle), os.path.relpath(bundle, REPO_ROOT), expect=EXIT_USAGE, error_code="invalid_argument")

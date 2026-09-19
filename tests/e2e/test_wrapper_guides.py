"""End-to-end journeys for the wrapper's guide and SimC composites.

This is the headline flow in skills/warcraft/SKILL.md: resolve one guide query across the guide
providers, export the bundles, compare them, and hand their explicit build references to simc.
Everything here talks to the real guide sites and the real local SimulationCraft checkout.

What a green run proves:

- every provider that exported a bundle contributes at least one explicit build reference, so the
  empty handoff this suite used to accept (every count satisfied by ``0 == 0``) now fails;
- the packet's counts, citations and per-build rows agree with the bundles it read, and the
  truncation it applies is reported rather than silently dropping references;
- simc identifies every handed-off build, and each leg simc could not run is reported as a failure
  inside the packet instead of riding along as ``ok: true``;
- the guide-published codes really are builds for the queried spec: decoding them names the class,
  the spec and the same hero tree the guide's own label advertises.

A provider that does not resolve decisively is a real outcome — wowhead's guide candidate for the
pinned query scores below the wrapper's threshold and the orchestration records why — so the
journeys assert against the providers that actually exported rather than a fixed list.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from tests.e2e.harness import EXIT_GENERIC, Result, run
from tests.e2e.pins import GUIDE_CLASS, GUIDE_QUERY, GUIDE_SPEC

GUIDE_PROVIDERS = ("wowhead", "method", "icy-veins")
BUNDLE_FILES = ("manifest.json", "guide.json", "pages.jsonl", "sections.jsonl", "build-references.jsonl")
# Every page a bundle cites must come from that provider's own site.
PROVIDER_HOSTS = {"wowhead": "wowhead.com", "method": "method.gg", "icy-veins": "icy-veins.com"}
# The two reference shapes the wrapper accepts as an explicit build: a Wowhead talent-calc link and
# an in-game loadout export string published in the guide body.
BUILD_REFERENCE_TYPES = {"wowhead_talent_calc_url", "wow_talent_export"}
SIMC_LEGS = ("identify", "decode", "describe")


@dataclass(frozen=True)
class Orchestration:
    """One real ``guide-compare-query`` run, shared by the journeys that read its output."""

    out_root: Path
    payload: dict[str, Any]
    bundle_paths: tuple[Path, ...]
    providers: tuple[str, ...]


@pytest.fixture(scope="module")
def monk_apl() -> Path:
    """A default SimC APL for the guide's class.

    SimulationCraft ships no mistweaver APL (it does not sim healers), so this is whichever default
    APL the checkout has for the class. The wrapper only forwards the path to `simc describe-build`,
    and describing a mistweaver build against a non-mistweaver APL is exactly the leg that must come
    back as a reported failure rather than a silent success.
    """
    spec_files = run("simc", "spec-files", GUIDE_CLASS)
    items = spec_files.data["categories"]["default_apl"]["items"]
    assert items, spec_files.describe()
    return Path(items[0]["path"])


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


def _assert_bundle_on_disk(bundle_path: Path) -> None:
    """The exported bundle is complete, self-describing, and cited from its own provider's site."""
    assert bundle_path.is_dir(), f"{bundle_path} is not a bundle directory"
    for name in BUNDLE_FILES:
        assert (bundle_path / name).is_file(), f"{bundle_path} is missing {name}"
    manifest = json.loads((bundle_path / "manifest.json").read_text())
    provider = manifest["provider"]
    host = PROVIDER_HOSTS[provider]

    pages = _bundle_rows(bundle_path, "pages.jsonl")
    assert pages, f"{bundle_path} exported no pages"
    assert all(host in page["page_url"] for page in pages), json.dumps(pages[:2])[:400]
    assert manifest["counts"]["pages"] == len(pages), f"{bundle_path} manifest miscounts pages"
    assert len(list((bundle_path / "pages").glob("*.html"))) == len(pages), "one raw page per row"
    assert manifest["counts"]["sections"] == len(_bundle_rows(bundle_path, "sections.jsonl"))

    references = _bundle_rows(bundle_path, "build-references.jsonl")
    assert references, f"{provider} exported a guide with no explicit build reference"
    assert manifest["counts"]["build_references"] == len(references)
    for reference in references:
        assert reference["reference_type"] in BUILD_REFERENCE_TYPES, json.dumps(reference)[:200]
        assert reference["build_code"] and reference["url"] and reference["label"]
        assert reference["source_urls"] and all(host in url for url in reference["source_urls"])


def _leg_succeeded(build: dict[str, Any], leg: str) -> bool:
    """Whether one simc leg of a handoff row succeeded, holding both outcomes to the contract.

    A leg simc could not run must carry its own failure envelope inside the packet; the
    wrong-answer-with-``ok: true`` shape this suite exists to catch would be an exit code of 0 over
    an error payload, or an error the packet never mentions.
    """
    result = build["simc"][leg]
    assert result is not None, f"{leg} leg missing: {json.dumps(build['reference'])[:200]}"
    payload = result["payload"]
    if result["exit_code"] == 0:
        assert payload["ok"] is True, json.dumps(payload)[:400]
        return True
    assert payload["ok"] is False, json.dumps(payload)[:400]
    assert payload["error"]["code"] and payload["error"]["message"], json.dumps(payload)[:400]
    return False


def _assert_build_row(build: dict[str, Any], *, providers: set[str], bundle_paths: set[str]) -> None:
    """One handed-off build: an explicit reference, its citations, and simc's reading of it."""
    reference = build["reference"]
    assert reference["reference_type"] in BUILD_REFERENCE_TYPES
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

    # simc must at least read every build the packet returns, and read the code it was handed.
    assert _leg_succeeded(build, "identify"), json.dumps(build["simc"]["identify"])[:400]
    identified = build["simc"]["identify"]["payload"]["data"]["build_spec"]
    assert identified["talents"] == reference["build_code"]
    assert identified["source_kind"] == reference["reference_type"]


def _assert_leg_counts(packet: dict[str, Any], leg: str, *, requested: bool) -> None:
    """The summary's per-leg counters match the rows, and an empty requested leg is declared."""
    summary = packet["summary"]
    if not requested:
        assert all(build["simc"][leg] is None for build in packet["builds"])
        assert summary[f"{leg}_success_count"] == 0
        assert leg not in summary["empty_requested_legs"]
        return
    succeeded = [build for build in packet["builds"] if _leg_succeeded(build, leg)]
    assert summary[f"{leg}_success_count"] == len(succeeded), json.dumps(summary)
    assert (leg in summary["empty_requested_legs"]) == (not succeeded), json.dumps(summary)
    for build in succeeded:
        decoded = build["simc"][leg]["payload"]["data"]
        assert decoded, json.dumps(build["reference"])[:200]


def _assert_handoff_packet(
    packet: dict[str, Any],
    provenance: dict[str, Any],
    *,
    bundle_paths: tuple[Path, ...],
    apl_path: Path | None,
    decode: bool,
) -> None:
    """The guide-build-to-simc packet stays internally consistent, fully cited, and non-empty.

    ``provenance`` is the envelope slot for the standalone command and a nested key when the packet
    is embedded in a guide-compare-query payload; both carry the same selection contract.
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

    summary = packet["summary"]
    builds = packet["builds"]
    # The bundles carry explicit builds, so an empty packet is a broken handoff, not a quiet pass.
    assert packet["build_reference_count"] >= 1, json.dumps(packet["citations"])[:400]
    assert builds, json.dumps(summary)
    assert summary["returned_build_count"] == len(builds)
    assert summary["returned_build_count"] + summary["excluded_build_count"] == packet["build_reference_count"]
    assert packet["truncated"] == (summary["excluded_build_count"] > 0)
    assert len(packet["citations"]["build_reference_urls"]) >= len(builds)
    assert len(packet["citations"]["build_reference_urls"]) <= packet["build_reference_count"]

    providers = set(provenance["source_providers"])
    paths = {str(path) for path in bundle_paths}
    for build in builds:
        _assert_build_row(build, providers=providers, bundle_paths=paths)
    _assert_leg_counts(packet, "decode", requested=decode)
    _assert_leg_counts(packet, "describe", requested=apl_path is not None)
    assert (summary["simc_handoff_status"] == "ok") == (summary["empty_requested_legs"] == [])


def test_guide_compare_query_exports_bundles_and_compares_them(require, orchestration: Orchestration) -> None:
    require("wowhead", "method", "icy-veins")
    payload = orchestration.payload
    assert payload["output_root"] == str(orchestration.out_root.resolve())
    assert payload["selected_providers"] == list(GUIDE_PROVIDERS)
    assert payload["force_refresh"] is False
    assert payload["max_age_hours"] == 24

    results = {row["provider"]: row for row in payload["provider_results"]}
    assert set(results) == set(GUIDE_PROVIDERS)
    exported = [row for row in results.values() if row["status"] == "exported"]
    assert len(exported) == payload["exported_bundle_count"]
    assert {row["provider"] for row in exported} == set(orchestration.providers)
    for provider, row in results.items():
        if row["status"] == "exported":
            assert PROVIDER_HOSTS[provider] in row["candidate"]["url"], row["candidate"]
            assert row["candidate"]["selection_source"] in {"resolve", "search"}
            assert row["exported_at"]
            assert row["freshness"]["status"] in {"fresh", "stale", "missing"}
            assert row["export"]["ok"] is True
        else:
            # A provider that did not resolve decisively must name the rule it failed, not guess.
            assert row["reason"], json.dumps(row)[:400]

    manifest = payload["manifest"]
    assert manifest["kind"] == "guide_compare_orchestration_manifest"
    assert manifest["query"] == GUIDE_QUERY
    assert len(manifest["providers"]) == payload["exported_bundle_count"]
    manifest_path = orchestration.out_root / "manifest.json"
    assert manifest_path.is_file()
    assert json.loads(manifest_path.read_text())["providers"] == manifest["providers"]

    assert len(orchestration.bundle_paths) >= 2
    for bundle_path in orchestration.bundle_paths:
        _assert_bundle_on_disk(bundle_path)

    comparison = payload["comparison"]
    assert comparison["compared_bundle_count"] == payload["exported_bundle_count"]
    assert {Path(row["path"]) for row in comparison["bundles"]} == set(orchestration.bundle_paths)
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
    left, right = orchestration.bundle_paths[0], orchestration.bundle_paths[1]
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


def test_guide_compare_rejects_a_directory_that_is_not_a_bundle(require, orchestration: Orchestration, out_dir: Path) -> None:
    require("wowhead", "method", "icy-veins")
    result = run(
        "warcraft",
        "guide-compare",
        str(out_dir),
        str(orchestration.bundle_paths[0]),
        expect=EXIT_GENERIC,
        error_code="invalid_bundle",
    )
    assert "manifest.json" in result.payload["error"]["message"]


def test_guide_builds_simc_hands_the_orchestration_root_to_simc(
    require, orchestration: Orchestration, monk_apl: Path, handoff: Result
) -> None:
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
    # Every exported guide must reach the packet: one provider's extraction rotting away is the
    # regression that used to hide behind an empty build list.
    contributing = {source["provider"] for build in packet["builds"] for source in build["sources"]}
    assert contributing == set(orchestration.providers), json.dumps(packet["summary"])


def test_guide_builds_simc_reports_the_references_it_truncates(require, orchestration: Orchestration) -> None:
    """``--limit`` cuts the reference list, and the packet says so instead of shrinking silently."""
    require("wowhead", "method", "icy-veins", "simc")
    bundle_path = orchestration.bundle_paths[0]
    result = run("warcraft", "guide-builds-simc", str(bundle_path), "--no-decode", "--limit", "1", timeout=300)
    packet = result.data
    assert result.payload["kind"] == "guide_builds_simc_handoff"
    assert packet["source"]["kind"] == "bundle"
    assert packet["freshness"]["reason"] == "bundle_manifest_exported_at"
    assert packet["build_reference_count"] >= 2, "a guide bundle publishes more than one build"
    assert packet["truncated"] is True
    assert packet["summary"]["returned_build_count"] == 1
    assert packet["summary"]["excluded_build_count"] == packet["build_reference_count"] - 1
    _assert_handoff_packet(
        packet,
        result.payload["provenance"],
        bundle_paths=(bundle_path,),
        apl_path=None,
        decode=False,
    )


def test_guide_build_references_decode_into_the_queried_spec(require, handoff: Result) -> None:
    """The handed-off codes are real builds for the queried spec, and match their own labels.

    `guide-builds-simc` cannot name the spec itself: simc only identifies specs the checkout ships
    an APL for, and it ships none for healers, so the packet reports the decode leg as empty. Decode
    the same codes with the spec supplied and the chain has to hold end to end — the class and spec
    the guide query asked for, a non-empty talent set, and the hero tree the guide's label names.
    """
    require("wowhead", "method", "icy-veins", "simc")
    builds = handoff.data["builds"]
    assert builds, handoff.describe()

    trees: list[tuple[str, str]] = []
    for build in builds:
        reference = build["reference"]
        decoded = run(
            "simc", "decode-build",
            "--build-text", reference["build_code"],
            "--actor-class", GUIDE_CLASS,
            "--spec", GUIDE_SPEC,
        ).data
        assert decoded["identity"]["actor_class"] == GUIDE_CLASS
        assert decoded["identity"]["spec"] == GUIDE_SPEC
        assert decoded["decoded"]["enabled_talents"], json.dumps(reference)[:200]
        assert decoded["decoded"]["hero_tree"]["name"], json.dumps(reference)[:200]
        trees.append((decoded["decoded"]["hero_tree"]["name"], reference["label"]))

    # A label that names a hero tree must name the tree its own code decodes to: pairing label i
    # with code i+1 is the extraction bug this catches.
    known = {name for name, _label in trees}
    assert any(name.lower() in label.lower() for name, label in trees), json.dumps(trees)
    for name, label in trees:
        mislabelled = [other for other in known - {name} if other.lower() in label.lower()]
        assert not mislabelled, f"{label!r} decodes to {name!r} but names {mislabelled}"


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
    packet = payload["simc_build_handoff"]
    assert packet is not None, result.describe()
    assert packet["kind"] == "guide_builds_simc_handoff"
    assert packet["source"]["path"] == str(out_root.resolve())
    assert len(packet["builds"]) <= 5, "--simc-build-limit must cap the handed-off builds"
    _assert_handoff_packet(packet, packet["provenance"], bundle_paths=bundle_paths, apl_path=monk_apl, decode=True)


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
    assert result.payload["kind"] == "guide_bundle_comparison_orchestration"
    assert result.payload["data"] == {}
    # A failure envelope carries no `data`, so the orchestration detail rides at the top level:
    # an agent still has to be able to see which provider declined and why.
    assert result.payload["exported_bundle_count"] == 0
    assert result.payload["manifest"] is None
    assert {row["provider"] for row in result.payload["provider_results"]} == set(GUIDE_PROVIDERS)
    assert all(row["status"] != "exported" for row in result.payload["provider_results"])
    assert all(row["reason"] for row in result.payload["provider_results"])
    assert not (out_root / "manifest.json").exists()
    assert list(out_root.iterdir()) == []

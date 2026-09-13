"""End-to-end journeys for the wrapper's guide and SimC composites.

This is the headline flow in skills/warcraft/SKILL.md: resolve one guide query across the guide
providers, export the bundles, compare them, and hand their explicit build references to simc.
Everything here talks to the real guide sites and the real local SimulationCraft checkout.

A guide provider that resolves nothing is a real outcome, not a test failure: the wrapper is
documented to skip a provider whose top guide candidate is not decisive, and it records why in
``provider_results``. What must always hold is the contract: at least two bundles on disk, a
manifest that names them, a comparison over exactly those bundles, and a SimC handoff whose
counts, citations, and per-build rows agree with the bundles it read.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from tests.e2e.harness import EXIT_GENERIC, payload_or_legacy, run
from tests.e2e.pins import GUIDE_CLASS, GUIDE_QUERY

GUIDE_PROVIDERS = ("wowhead", "method", "icy-veins")
BUNDLE_FILES = ("manifest.json", "guide.json", "pages.jsonl", "sections.jsonl", "build-references.jsonl")


@dataclass(frozen=True)
class Orchestration:
    """One real ``guide-compare-query`` run, shared by the journeys that read its output."""

    out_root: Path
    payload: dict[str, Any]
    bundle_paths: tuple[Path, ...]


@pytest.fixture(scope="module")
def monk_apl() -> Path:
    """A default SimC APL for the guide's class.

    SimulationCraft ships no mistweaver APL (it does not sim healers), so the handoff journeys use
    whichever default APL the checkout has for the class; the wrapper only forwards the path to
    ``simc describe-build``.
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
    bundle_paths = tuple(Path(row["bundle_path"]) for row in payload["manifest"]["providers"])
    return Orchestration(out_root=out_root, payload=payload, bundle_paths=bundle_paths)


def _assert_bundle_on_disk(bundle_path: Path) -> None:
    assert bundle_path.is_dir(), f"{bundle_path} is not a bundle directory"
    for name in BUNDLE_FILES:
        assert (bundle_path / name).is_file(), f"{bundle_path} is missing {name}"
    pages = [json.loads(line) for line in (bundle_path / "pages.jsonl").read_text().splitlines() if line.strip()]
    assert pages, f"{bundle_path} exported no pages"
    assert all(page.get("page_url") for page in pages), json.dumps(pages[:2])[:400]
    page_files = list((bundle_path / "pages").glob("*.html"))
    assert page_files, f"{bundle_path} kept no raw page HTML alongside the normalized rows"


def _assert_handoff_packet(
    packet: dict[str, Any],
    provenance: dict[str, Any],
    *,
    bundle_count: int,
    apl_path: Path | None,
    decode: bool,
) -> None:
    """The guide-build-to-simc packet must stay internally consistent and fully cited.

    ``provenance`` is the envelope slot for the standalone command and a nested key when the
    packet is embedded in a guide-compare-query payload; both carry the same selection contract.
    """
    assert provenance["explicit_build_reference_only"] is True
    assert provenance["selection_contract"] == "embedded_build_references_only"
    assert provenance["source_providers"]
    assert packet["freshness"]["status"] == "known"
    assert packet["freshness"]["sampled_at"]
    assert packet["bundle_count"] == bundle_count
    assert len(packet["citations"]["bundle_paths"]) == bundle_count
    assert packet["decode_enabled"] is decode
    assert packet["apl_path"] == (str(apl_path) if apl_path else None)

    summary = packet["summary"]
    builds = packet["builds"]
    assert summary["returned_build_count"] == len(builds)
    assert summary["returned_build_count"] + summary["excluded_build_count"] <= packet["build_reference_count"] or packet["truncated"]
    assert (packet["build_reference_count"] == 0) == (builds == [])
    assert len(packet["citations"]["build_reference_urls"]) == packet["build_reference_count"] or packet["truncated"]

    for build in builds:
        # Every returned build is an explicit reference from a bundle, never inferred from prose.
        assert build["build_reference"]["reference_type"] == "wowhead_talent_calc_url"
        assert build["build_reference"]["build_code"]
        assert build["build_reference"]["source_url"]
        identity = build["build_identity"]["class_spec_identity"]["identity"]
        assert identity["actor_class"] and identity["spec"]
        if decode:
            decoded = build.get("decode")
            assert decoded is not None, json.dumps(build)[:400]
            if decoded.get("ok"):
                assert decoded["data"]["decoded"]["enabled_talents"], json.dumps(decoded)[:400]


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
    for row in results.values():
        if row["status"] == "exported":
            assert row["candidate"]["url"]
            assert row["candidate"]["selection_source"] in {"resolve", "search"}
            assert row["exported_at"]
            assert row["freshness"]["status"] in {"fresh", "stale", "missing"}
            assert row["export"]["ok"] is True
        else:
            # A provider that did not resolve decisively must say why instead of guessing.
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


def test_guide_compare_query_reuses_fresh_bundles(require, orchestration: Orchestration) -> None:
    """A second run against the same root must reuse the bundles instead of re-exporting them."""
    require("wowhead", "method", "icy-veins")
    result = run("warcraft", "guide-compare-query", GUIDE_QUERY, "--out-root", str(orchestration.out_root), timeout=300)
    reused = [row for row in result.data["provider_results"] if row["status"] == "reused"]
    assert reused, result.describe()
    assert result.data["exported_bundle_count"] >= 2
    for row in reused:
        assert row["freshness"]["status"] == "fresh"
        assert row["freshness"]["reason"] == "within_max_age"
        assert row["freshness"]["age_hours"] <= row["freshness"]["max_age_hours"]
    previous = {row["provider"]: row["exported_at"] for row in orchestration.payload["provider_results"] if row["status"] == "exported"}
    assert {row["provider"] for row in reused} >= set(previous), result.describe()
    for row in reused:
        if row["provider"] in previous:
            assert row["exported_at"] == previous[row["provider"]], result.describe()


def test_guide_compare_reads_two_exported_bundles(require, orchestration: Orchestration) -> None:
    require("wowhead", "method", "icy-veins")
    left, right = orchestration.bundle_paths[0], orchestration.bundle_paths[1]
    result = run("warcraft", "guide-compare", str(left), str(right), timeout=300)
    data = result.data
    assert data == {key: payload_or_legacy(result, key) for key in data}, result.describe()
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
    for item in evidence["items"]:
        assert item["section_title_key"]
        assert 1 <= item["bundle_count"] <= 2
        assert item["shared_across_all_bundles"] is (item["bundle_count"] == 2)
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


def test_guide_builds_simc_hands_the_orchestration_root_to_simc(require, orchestration: Orchestration, monk_apl: Path) -> None:
    require("wowhead", "method", "icy-veins", "simc")
    result = run("warcraft", "guide-builds-simc", str(orchestration.out_root), "--apl-path", str(monk_apl), timeout=300)
    packet = result.data
    assert packet["source"]["kind"] == "orchestration_root"
    assert packet["source"]["manifest_kind"] == "guide_compare_orchestration_manifest"
    assert packet["source"]["query"] == GUIDE_QUERY
    assert packet["freshness"]["reason"] == "orchestration_manifest_updated_at"
    assert {Path(path) for path in packet["citations"]["bundle_paths"]} == set(orchestration.bundle_paths)
    assert result.payload["kind"] == "guide_builds_simc_handoff"
    _assert_handoff_packet(
        packet,
        result.payload["provenance"],
        bundle_count=len(orchestration.bundle_paths),
        apl_path=monk_apl,
        decode=True,
    )
    for build in packet["builds"]:
        assert build.get("describe") is not None, json.dumps(build)[:400]


def test_guide_builds_simc_reads_a_single_bundle_without_decoding(require, orchestration: Orchestration) -> None:
    require("wowhead", "method", "icy-veins", "simc")
    bundle_path = orchestration.bundle_paths[0]
    result = run("warcraft", "guide-builds-simc", str(bundle_path), "--no-decode", "--limit", "5", timeout=300)
    packet = result.data
    assert packet["source"]["kind"] == "bundle"
    assert packet["freshness"]["reason"] == "bundle_manifest_exported_at"
    assert packet["citations"]["bundle_paths"] == [str(bundle_path)]
    assert result.payload["kind"] == "guide_builds_simc_handoff"
    _assert_handoff_packet(packet, result.payload["provenance"], bundle_count=1, apl_path=None, decode=False)


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
    packet = payload["simc_build_handoff"]
    assert packet is not None, result.describe()
    assert packet["kind"] == "guide_builds_simc_handoff"
    assert packet["source"]["path"] == str(out_root.resolve())
    _assert_handoff_packet(packet, packet["provenance"], bundle_count=2, apl_path=monk_apl, decode=True)
    assert {Path(path) for path in packet["citations"]["bundle_paths"]} == {
        Path(row["bundle_path"]) for row in payload["manifest"]["providers"]
    }


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
    # The orchestration detail still travels on the failure envelope so an agent can see why.
    assert result.payload["exported_bundle_count"] == 0
    assert result.payload["manifest"] is None
    assert {row["provider"] for row in result.payload["provider_results"]} == set(GUIDE_PROVIDERS)
    assert all(row["status"] != "exported" for row in result.payload["provider_results"])
    assert not (out_root / "manifest.json").exists()
    assert list(out_root.iterdir()) == []

"""Guide comparisons must leave their last completed bundles readable after failed refreshes."""
from __future__ import annotations

import json
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from warcraft_cli.guide_compare import GuideCompareQueryOptions, guide_compare_query_payload, manifest_bundle_path
from warcraft_cli.providers import ProviderCalls, provider_invoke
from warcraft_content.article_bundle import load_article_bundle, write_article_bundle
from warcraft_core.envelope import envelope_violations, error_envelope, success_envelope
from warcraft_core.provider import ProviderError

from tests.test_warcraft_wrapper import _comparison_payload


class SyntheticGuides:
    def __init__(self) -> None:
        self.exports: list[tuple[str, str, Path]] = []
        self.failed: set[tuple[str, str]] = set()
        self.revision = 1

    def lookup(self, provider, query, **options):
        payload = success_envelope(provider=provider, command="resolve", kind="resolve", data={
            "resolved": True, "confidence": "high",
            "match": {"id": query, "kind": "guide", "name": query, "url": f"https://example.test/{provider}/{query}"},
        })
        return {"provider": provider, "exit_code": 0, "payload": payload}

    def invoke(self, provider, args, **options):
        assert args[0] == "guide-export"
        ref, output = args[1], Path(args[3])
        self.exports.append((provider, ref, output))
        if (provider, ref) in self.failed:
            return {"provider": provider, "exit_code": 5, "payload": error_envelope(
                provider=provider, command="guide-export", code="network_error", message="Synthetic outage",
            )}
        manifest = write_article_bundle(_comparison_payload(
            provider=provider, slug=ref, page_url=f"https://example.test/{provider}/{ref}",
            page_title=f"{ref} revision {self.revision}", analysis_tags=["overview"],
        ), provider=provider, export_dir=output)
        return {"provider": provider, "exit_code": 0, "payload": success_envelope(
            provider=provider, command="guide-export", kind="guide_export", data=manifest,
        )}

    def unexpected(self, *args, **options):
        raise AssertionError("This fixture resolves every guide and does not request SimC")

    def calls(self) -> ProviderCalls:
        return ProviderCalls(invoke=self.invoke, resolve=self.lookup, search=self.unexpected, simc=self.unexpected)


@pytest.fixture
def options(tmp_path: Path) -> GuideCompareQueryOptions:
    return GuideCompareQueryOptions(
        query="mistweaver-monk", providers=("method", "icy-veins"), orchestration_root=tmp_path / "comparison",
        requested_expansion=None, max_age_hours=24, force_refresh=False, simc_build_handoff=False,
        simc_apl_path=None, simc_decode=False, simc_build_limit=3,
    )


def _bundle_snapshots(root: Path, manifest: dict) -> dict[Path, dict[str, bytes]]:
    snapshots = {}
    for row in manifest["providers"]:
        path = manifest_bundle_path(root, row["bundle_path"])
        snapshots[path] = {str(file.relative_to(path)): file.read_bytes() for file in path.rglob("*") if file.is_file()}
    return snapshots


def test_failed_other_query_preserves_completed_manifest_and_bundles(options: GuideCompareQueryOptions) -> None:
    guides = SyntheticGuides()
    original, code = guide_compare_query_payload(options, guides.calls())
    assert code == 0
    manifest_path = options.orchestration_root / "manifest.json"
    published = manifest_path.read_bytes()
    snapshots = _bundle_snapshots(options.orchestration_root, original["manifest"])
    guides.failed.add(("icy-veins", "frost-mage"))
    failed, code = guide_compare_query_payload(replace(options, query="frost-mage"), guides.calls())
    assert code == 5
    assert failed["ok"] is False
    assert manifest_path.read_bytes() == published
    assert _bundle_snapshots(options.orchestration_root, original["manifest"]) == snapshots
    method_b = next(path for provider, ref, path in guides.exports if provider == "method" and ref == "frost-mage")
    assert method_b not in snapshots
    assert json.loads((method_b / "manifest.json").read_text())["guide"]["slug"] == "frost-mage"
    exports_before = len(guides.exports)
    recovered, code = guide_compare_query_payload(options, guides.calls())
    assert code == 0
    assert len(guides.exports) == exports_before
    assert {row["status"] for row in recovered["provider_results"]} == {"reused"}
    assert _bundle_snapshots(options.orchestration_root, recovered["manifest"]) == snapshots


def test_forced_refresh_publishes_new_paths_without_changing_old_raw_bundles(options: GuideCompareQueryOptions) -> None:
    guides = SyntheticGuides()
    original, code = guide_compare_query_payload(options, guides.calls())
    assert code == 0
    snapshots = _bundle_snapshots(options.orchestration_root, original["manifest"])
    guides.revision = 2
    refreshed, code = guide_compare_query_payload(replace(options, force_refresh=True), guides.calls())
    assert code == 0
    assert {row["status"] for row in refreshed["provider_results"]} == {"exported"}
    refreshed_snapshots = _bundle_snapshots(options.orchestration_root, refreshed["manifest"])
    assert set(snapshots).isdisjoint(refreshed_snapshots)
    assert _bundle_snapshots(options.orchestration_root, original["manifest"]) == snapshots
    assert all("revision 2" in load_article_bundle(path)["pages"][0]["title"] for path in refreshed_snapshots)
    assert json.loads((options.orchestration_root / "manifest.json").read_text()) == refreshed["manifest"]


@pytest.mark.parametrize("corruption", ["legacy_identity", "saved_identity", "content"])
def test_reusable_bundle_with_wrong_identity_or_corrupt_content_is_refreshed(
    options: GuideCompareQueryOptions, corruption: str,
) -> None:
    guides = SyntheticGuides()
    original, code = guide_compare_query_payload(options, guides.calls())
    assert code == 0
    published = original["manifest"]
    method_row = next(row for row in published["providers"] if row["provider"] == "method")
    old_method = manifest_bundle_path(options.orchestration_root, method_row["bundle_path"])
    if corruption == "content":
        manifest = json.loads((old_method / "manifest.json").read_text())
        (old_method / manifest["files"]["pages_jsonl"]).write_text("not JSON\n")
    else:
        if corruption == "legacy_identity":
            method_row.pop("bundle_identity")
            (options.orchestration_root / "manifest.json").write_text(json.dumps(published))
        manifest = json.loads((old_method / "manifest.json").read_text())
        manifest["guide"]["slug"] = "frost-mage"
        (old_method / "manifest.json").write_text(json.dumps(manifest))
    refreshed, code = guide_compare_query_payload(options, guides.calls())
    assert code == 0
    rows = {row["provider"]: row for row in refreshed["provider_results"]}
    assert rows["method"]["status"] == "exported"
    assert Path(rows["method"]["bundle_path"]) != old_method
    assert rows["icy-veins"]["status"] == "reused"
    assert guides.exports[-1][:2] == ("method", "mistweaver-monk")


def test_failed_atomic_publication_preserves_previous_manifest_and_bundles(
    options: GuideCompareQueryOptions, monkeypatch: pytest.MonkeyPatch,
) -> None:
    guides = SyntheticGuides()
    original, code = guide_compare_query_payload(options, guides.calls())
    assert code == 0
    target = options.orchestration_root / "manifest.json"
    old_manifest = target.read_bytes()
    snapshots = _bundle_snapshots(options.orchestration_root, original["manifest"])
    original_replace = Path.replace

    def failed_replace(path: Path, destination: Path):
        if destination == target:
            raise OSError("Synthetic publication failure")
        return original_replace(path, destination)

    monkeypatch.setattr(Path, "replace", failed_replace)
    with pytest.raises(OSError, match="Synthetic publication failure"):
        guide_compare_query_payload(replace(options, force_refresh=True), guides.calls())
    assert target.read_bytes() == old_manifest
    assert _bundle_snapshots(options.orchestration_root, original["manifest"]) == snapshots
    assert {file.name for file in options.orchestration_root.iterdir()} == {"manifest.json", "bundles"}


@pytest.mark.parametrize(("provider", "operation"), [
    ("method", "method_operations"), ("icy-veins", "icy_veins_operations"), ("wowhead", "wowhead_operations"),
])
def test_composite_guide_export_calls_pure_operation_without_output_capture(
    provider: str, operation: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    calls = []

    def export(guide_ref, **kwargs):
        calls.append((guide_ref, kwargs))
        return success_envelope(provider=provider, command="guide-export", kind="guide_export", data={"path": str(tmp_path)})

    monkeypatch.setattr(f"warcraft_cli.providers.{operation}.guide_export", export)
    result = provider_invoke(provider, ["guide-export", "mistweaver-monk", "--out", str(tmp_path)], expansion="retail")
    assert result["exit_code"] == 0
    assert result["payload"]["data"] == {"path": str(tmp_path)}
    assert envelope_violations(result["payload"]) == []
    assert calls == [("mistweaver-monk", {"out": tmp_path, **({"expansion": "retail"} if provider == "wowhead" else {})})]


@pytest.mark.parametrize("provider", ["method", "icy-veins", "wowhead"])
@pytest.mark.parametrize(("error_code", "exit_code"), [("network_error", 5), ("invalid_argument", 2), ("not_found", 4)])
def test_pure_export_failure_maps_to_provider_envelope_and_exit_code(
    provider: str, error_code: str, exit_code: int, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    operation = {"method": "method_operations", "icy-veins": "icy_veins_operations", "wowhead": "wowhead_operations"}[provider]

    def export(*args, **kwargs):
        raise ProviderError(error_code, "Synthetic export failure")

    monkeypatch.setattr(f"warcraft_cli.providers.{operation}.guide_export", export)
    result = provider_invoke(provider, ["guide-export", "mistweaver-monk", "--out", str(tmp_path)])
    assert result["exit_code"] == exit_code
    payload = result["payload"]
    assert payload["error"]["code"] == error_code
    assert payload["provider"] == provider
    assert payload["command"] == "guide-export"
    assert envelope_violations(payload) == []


@pytest.mark.parametrize("operation", ["guide_full", "guide_export"])
@pytest.mark.parametrize(("explicit", "expected"), [(None, "classic"), ("retail", "retail")])
def test_pure_wowhead_guide_operations_adopt_path_expansion_unless_explicitly_overridden(
    operation: str, explicit: str | None, expected: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from wowhead_cli import provider

    monkeypatch.setattr(provider, "open_client", lambda profile: nullcontext(SimpleNamespace(expansion=profile)))

    def full(client, **kwargs):
        return {"expansion": client.expansion.key}, ""

    def export(client, **kwargs):
        return {"expansion": client.expansion.key}

    monkeypatch.setattr("wowhead_cli.guide_services.build_guide_full_payload", full)
    monkeypatch.setattr("wowhead_cli.guide_services.export_guide_bundle", export)
    kwargs = {"out": tmp_path} if operation == "guide_export" else {}
    result = getattr(provider, operation)("classic/guide=123", expansion=explicit, **kwargs)
    assert result["data"]["expansion"] == expected

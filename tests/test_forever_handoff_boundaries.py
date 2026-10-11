"""Forever source evidence must not be interpreted through realm-based or retail-only consumers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner
from warcraft_cli.guide_compare import guide_builds_simc_payload
from warcraft_cli.main import app
from warcraft_core.identity import talent_transport_packet_payload

runner = CliRunner()


@pytest.mark.parametrize("args", [
    ["--expansion", "forever", "actor-profile", "ABC123", "Asha Brightvale"],
    ["--expansion", "forever", "talent-packet", "warrior/fury/ABC123"],
    ["talent-packet", "https://www.wowhead.com/forever/talent-calc/warrior/3030-05"],
    ["talent-describe", "https://www.wowhead.com/forever/talent-calc/warrior/3030-05"],
])
def test_forever_handoffs_fail_before_provider_or_engine_calls(monkeypatch, args: list[str]) -> None:
    def unexpected(*_args, **_kwargs):
        raise AssertionError("An unverified Forever handoff must not call a provider or engine")

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", unexpected)
    monkeypatch.setattr("warcraft_cli.main.simc_call", unexpected)
    result = runner.invoke(app, args)
    assert result.exit_code == 2, result.output
    assert json.loads(result.stderr)["error"]["code"] == "unsupported_game_variant"


def test_forever_raw_packet_file_cannot_be_upgraded_or_described_with_retail_simc(monkeypatch, tmp_path: Path) -> None:
    packet = talent_transport_packet_payload(
        actor_class="warrior", spec="fury", confidence="none", source="synthetic_forever_source",
        raw_evidence={"talent_tree_entries": [{"entry": 1, "node_id": 2, "rank": 1}]},
        scope={"expansion": "forever"},
    )
    path = tmp_path / "packet.json"
    path.write_text(json.dumps(packet), encoding="utf-8")

    def unexpected(*_args, **_kwargs):
        raise AssertionError("A Forever packet must not be sent to the retail engine")

    monkeypatch.setattr("warcraft_cli.main.simc_call", unexpected)
    for command in ("talent-packet", "talent-describe"):
        result = runner.invoke(app, [command, str(path)])
        assert result.exit_code == 2, result.output
        assert json.loads(result.stderr)["error"]["code"] == "unsupported_game_variant"


@pytest.mark.parametrize(("manifest", "reference"), [
    ({"expansion": "forever"}, {
        "reference_type": "wow_talent_export", "url": "wow-talent-export:ABC123", "build_code": "ABC123",
    }),
    ({"requested_expansion": "forever"}, {
        "reference_type": "wow_talent_export", "url": "wow-talent-export:ABC123", "build_code": "ABC123",
    }),
    ({}, {
        "reference_type": "wowhead_talent_calc_url",
        "url": "https://www.wowhead.com/forever/talent-calc/warrior/fury/ABC123", "build_code": "ABC123",
    }),
    ({}, {
        "reference_type": "wow_talent_export", "url": "wow-talent-export:ABC123", "build_code": "ABC123",
        "source_url": "https://www.wowhead.com/forever/guide/warrior",
    }),
])
def test_exported_forever_evidence_is_excluded_from_retail_simc_handoff(
    tmp_path: Path, manifest: dict[str, str], reference: dict[str, str],
) -> None:
    def unexpected(*_args, **_kwargs):
        raise AssertionError("Retail-looking Forever evidence must not invoke SimC")

    bundle = {"manifest": {"provider": "wowhead", **manifest}, "build_references": [reference]}
    payload = guide_builds_simc_payload(
        source_path=tmp_path, source_kind="bundle", source_manifest=bundle["manifest"],
        bundle_inputs=[(tmp_path, bundle)], decode=True, apl_path=None, limit=10, simc=unexpected,
    )
    assert payload["summary"]["simc_handoff_status"] == "all_references_excluded"
    assert payload["excluded_builds"][0]["reason"] == "unsupported_game_variant:forever"
    excluded = payload["excluded_builds"][0]
    assert (excluded["reference"]["url"], excluded["reference"]["build_code"]) == (reference["url"], reference["build_code"])
    if "source_url" in reference:
        assert excluded["sources"][0]["source_urls"] == [reference["source_url"]]

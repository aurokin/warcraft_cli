"""Keep the experimental Forever engine separate from retail build contracts."""

import json
from pathlib import Path

import pytest
from simc_cli.build_input import BuildSpec, PacketInput, decode_build, encode_build, identify_build
from simc_cli.build_services import validate_transport_packet_payload
from simc_cli.main import app
from simc_cli.provider import FOREVER_UNSUPPORTED_COMMANDS, doctor
from simc_cli.repo import discover_repo
from simc_cli.run import binary_provenance, binary_version, require_retail_engine
from typer.testing import CliRunner
from warcraft_core.provider import ProviderError

FOREVER_BANNER = (
    "SimulationCraft 1210-01 for World of Warcraft 1.60.1.70334 Live "
    "(hotfix 2026-10-10/70334, git build forever fcfe72f)"
)


def _checkout(tmp_path: Path, banner: str = FOREVER_BANNER) -> Path:
    for relative in (
        "ActionPriorityLists/default", "ActionPriorityLists/assisted_combat",
        "engine/class_modules", "SpellDataDump", "build",
    ):
        (tmp_path / relative).mkdir(parents=True, exist_ok=True)
    binary = tmp_path / "build/simc"
    binary.write_text(
        "#!/usr/bin/env python3\nimport sys\nfrom pathlib import Path\n"
        f"Path({str(tmp_path / 'calls.jsonl')!r}).open('a').write(repr(sys.argv[1:]) + '\\n')\n"
        f"print({banner!r})\n"
    )
    binary.chmod(0o755)
    return tmp_path


@pytest.mark.parametrize(("banner", "version", "variant"), [
    (FOREVER_BANNER, "1.60.1.70334", "forever"),
    ("SimulationCraft 1210-01 for World of Warcraft 12.1.0.69933 Live (git build forever fcfe72f)",
     "12.1.0.69933", "retail"),
    ("SimulationCraft 1210-01 (git build forever fcfe72f)", None, "unknown"),
    ("SimulationCraft 1210-01 for World of Warcraft 1.15.7.60000 Live", "1.15.7.60000", "unknown"),
])
def test_game_family_uses_client_data_not_engine_version_or_branch(tmp_path: Path, banner: str,
                                                                  version: str | None, variant: str) -> None:
    paths = discover_repo(_checkout(tmp_path, banner))
    parsed = binary_version(paths)
    assert parsed.wow_version == version
    assert parsed.game_variant == variant
    if variant != "forever":
        require_retail_engine(paths)


def test_missing_binary_remains_unknown(tmp_path: Path) -> None:
    version = binary_version(discover_repo(tmp_path))
    assert version.available is False
    assert version.wow_version is None
    assert version.game_variant == "unknown"
    require_retail_engine(discover_repo(tmp_path))


def test_doctor_exposes_actual_game_and_experimental_capabilities(tmp_path: Path, monkeypatch) -> None:
    root = _checkout(tmp_path)
    monkeypatch.setattr("simc_cli.provider.ripgrep_available", lambda: True)
    monkeypatch.setattr("simc_cli.provider.repo_git_status", lambda _paths: {"head": "fcfe72f", "branch": "forever"})
    data = doctor(repo_root=root)["data"]
    assert data["status"] == "degraded"
    binary = data["repo"]["binary"]
    assert (binary["wow_version"], binary["game_variant"]) == ("1.60.1.70334", "forever")
    assert binary["matches_checkout"] is True
    assert {data["capabilities"][name] for name in FOREVER_UNSUPPORTED_COMMANDS} == {"not_supported"}
    assert data["capabilities"]["run"] == data["capabilities"]["sim"] == "experimental"
    assert data["capabilities"]["version"] == data["capabilities"]["inspect"] == "ready"


def test_build_error_provenance_keeps_client_family(tmp_path: Path, monkeypatch) -> None:
    paths = discover_repo(_checkout(tmp_path))
    monkeypatch.setattr("simc_cli.run.repo_git_status", lambda _paths: {"head": "fcfe72f"})
    provenance = binary_provenance(paths)
    assert provenance.wow_version == "1.60.1.70334"
    assert provenance.game_variant == "forever"
    assert provenance.matches_checkout is True


@pytest.mark.parametrize("operation", [identify_build, decode_build, encode_build])
def test_build_transports_reject_forever_before_generating_a_profile(tmp_path: Path, operation) -> None:
    paths = discover_repo(_checkout(tmp_path))
    build = BuildSpec(actor_class="shaman", spec="enhancement", talents="retail-hash")
    with pytest.raises(ProviderError) as caught:
        operation(paths, build)
    assert caught.value.code == "unsupported_game_variant"
    assert caught.value.exit_code == 2
    assert (tmp_path / "calls.jsonl").read_text().splitlines() == ["['spell_query=spell.id=133']"]


@pytest.mark.parametrize("args", [
    ["decode-build", "--talents", "retail-hash"],
    ["priority", "missing.simc"],
    ["build-harness", "--actor-class", "shaman", "--spec", "enhancement"],
])
def test_cli_rejects_unsupported_forever_workflows_with_usage_envelope(tmp_path: Path, args: list[str]) -> None:
    root = _checkout(tmp_path)
    result = CliRunner().invoke(app, ["--repo-root", str(root), *args])
    assert result.exit_code == 2
    error = json.loads(result.stderr)["error"]
    assert error["code"] == "unsupported_game_variant"
    assert error["details"]["wow_version"] == "1.60.1.70334"
    assert (tmp_path / "calls.jsonl").read_text().splitlines() == ["['spell_query=spell.id=133']"]


@pytest.mark.parametrize("entry_rank", [0, 1])
def test_transport_validation_rejects_forever_before_raw_only_fallback_or_packet_write(
    tmp_path: Path, monkeypatch, entry_rank: int,
) -> None:
    # Zero ranks and missing trait data both yield raw_only before the normal encode/decode guard.
    root = _checkout(tmp_path / "engine")
    packet = {
        "kind": "talent_transport_packet",
        "transport_status": "raw_only",
        "build_identity": {"class_spec_identity": {"identity": {"actor_class": "druid", "spec": "balance"}}},
        "transport_forms": {},
        "validation": {},
        "scope": {},
        "raw_evidence": {"talent_tree_entries": [{"entry": 103324, "node_id": 82244, "rank": entry_rank}]},
    }
    packet_path = tmp_path / "packet.json"
    packet_path.write_text(json.dumps(packet))
    destination = tmp_path / "updated.json"
    result = CliRunner().invoke(app, [
        "--repo-root", str(root), "validate-talent-transport", "--build-packet", str(packet_path),
        "--out", str(destination),
    ])
    assert result.exit_code == 2
    assert json.loads(result.stderr)["error"]["code"] == "unsupported_game_variant"
    assert not destination.exists()
    assert json.loads(packet_path.read_text()) == packet

    monkeypatch.setenv("SIMC_REPO_ROOT", str(root))
    with pytest.raises(ProviderError) as caught:
        validate_transport_packet_payload(PacketInput(packet))
    assert caught.value.code == "unsupported_game_variant"

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import simc_cli.compare as simc_compare
import simc_cli.main as simc_main
from simc_cli.build_input import BuildIdentity, BuildResolution, BuildSpec, DecodedTalent, HeroTree, SimcBuildError
from simc_cli.main import app as simc_app
from simc_cli.repo import RepoPaths
from simc_cli.search import word_bounded_pattern
from simc_cli.trait_data import parse_trait_table
from typer.testing import CliRunner
from warcraft_core.envelope import ENVELOPE_KEYS
from warcraft_core.talent_transport import CLASS_ID_BY_ACTOR_CLASS, tokenize_talent_name

runner = CliRunner()

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "simc"
# Real `simc ... debug=1` output for a Sunfury Arcane Mage, plus the checkout trait rows it needs.
CAPTURED_ARCANE_MAGE = (FIXTURES / "captured_mage_arcane_sunfury_debug.txt").read_text()
CAPTURED_TRAIT_DATA = (FIXTURES / "captured_trait_data.inc").read_text()
CAPTURED_SPECIALIZATION_DATA = (FIXTURES / "captured_sc_specialization_data.inc").read_text()


def _captured_without(*talent_names: str) -> str:
    """The captured decode with talent lines dropped, i.e. what SimC prints after a removal."""
    dropped = tuple(f"talent {name} (" for name in talent_names)
    return "\n".join(line for line in CAPTURED_ARCANE_MAGE.splitlines() if not any(d in line for d in dropped))


class _FakeSimcBinary:
    """Stands in for the SimC binary so the real decode/encode pipeline runs over captured output.

    Decode invocations answer with the captured debug text registered for the profile's ``talents=``
    value; encode invocations write the save file the encoder reads back.
    """

    def __init__(self, decodes: dict[str, str], *, encoded: str = "MODIFIED_EXPORT") -> None:
        self.decodes = decodes
        self.encoded = encoded
        self.profiles: list[str] = []
        self.encode_args: list[str] = []

    def __call__(self, cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        profile_path = Path(str(cmd[1]))
        text = profile_path.read_text()
        self.profiles.append(text)
        if "save=" in text:
            self.encode_args = cmd[2:]
            save = next(line.split("=", 1)[1] for line in text.splitlines() if line.startswith("save="))
            Path(save).write_text(f"talents={self.encoded}\n")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        talents = next((line.split("=", 1)[1] for line in text.splitlines() if line.startswith("talents=")), "")
        return subprocess.CompletedProcess(cmd, 0, stdout=self.decodes[talents], stderr="")

    @property
    def encode_profile(self) -> str:
        return next(text for text in self.profiles if "save=" in text)


def _checkout(tmp_path: Path) -> Path:
    """A checkout stub: every directory `validate_repo` requires, the trait table, the spec table, and a binary."""
    generated = tmp_path / "engine" / "dbc" / "generated"
    generated.mkdir(parents=True, exist_ok=True)
    (generated / "trait_data.inc").write_text(CAPTURED_TRAIT_DATA)
    (generated / "sc_specialization_data.inc").write_text(CAPTURED_SPECIALIZATION_DATA)
    for relative in (
        "ActionPriorityLists/default",
        "ActionPriorityLists/assisted_combat",
        "engine/class_modules",
        "SpellDataDump",
        "build",
    ):
        (tmp_path / relative).mkdir(parents=True, exist_ok=True)
    (tmp_path / "build" / "simc").write_text("")
    return tmp_path


def _stub_binary_banner(monkeypatch, banner: str = "SimulationCraft 1201 (git build midnight 0908ace08c)") -> None:
    """Answer the version probe without executing the stub binary."""
    monkeypatch.setattr(
        "simc_cli.run.subprocess.run",
        lambda cmd, **_kwargs: subprocess.CompletedProcess(cmd, 0, stdout=banner, stderr=""),
    )


def _resolution(
    *,
    actor_class: str = "druid",
    spec: str = "balance",
    enabled: set[str] | None = None,
    talents_by_tree: dict[str, list[DecodedTalent]] | None = None,
    generated_profile_text: str | None = None,
    source_kind: str = "wow_talent_export",
    source_notes: list[str] | None = None,
    hero_tree: HeroTree | None = None,
    inactive_hero_talents: list[DecodedTalent] | None = None,
) -> BuildResolution:
    """A real BuildResolution, so payload builders are exercised instead of a duck-typed stand-in."""
    trees = talents_by_tree or {"class": [], "spec": [], "hero": [], "selection": []}
    return BuildResolution(
        actor_class=actor_class,
        spec=spec,
        enabled_talents=enabled if enabled is not None else {t.token for row in trees.values() for t in row if t.taken},
        talents_by_tree=trees,
        source_kind=source_kind,
        generated_profile_text=generated_profile_text,
        source_notes=source_notes if source_notes is not None else ["decoded via /tmp/simc"],
        hero_tree=hero_tree,
        inactive_hero_talents=inactive_hero_talents or [],
    )


def _talent(tree: str, name: str, entry: int, rank: int = 1, max_rank: int = 1) -> DecodedTalent:
    return DecodedTalent(
        tree=tree,
        name=name,
        token=tokenize_talent_name(name),
        rank=rank,
        max_rank=max_rank,
        entry=entry,
    )



def test_simc_doctor_reports_phase_one_capabilities(monkeypatch, tmp_path: Path) -> None:
    repo_root = tmp_path / "simc"
    repo_root.mkdir()

    def fake_repo_payload(paths):  # noqa: ANN001
        return {
            "root": str(paths.root),
            "exists": True,
            "repo_ready": True,
            "build_ready": True,
            "repo_issues": [],
            "build_issues": [],
            "git": {"git": True, "dirty": False, "branch": "main", "head": "abc", "dirty_entries": []},
            "binary": {"path": str(paths.build_simc), "exists": True, "version_line": "SimulationCraft 1201", "available": True},
        }

    monkeypatch.setattr("simc_cli.provider.repo_payload", fake_repo_payload)
    # Whether the host has ripgrep decides `status`, so it is pinned rather than inherited.
    monkeypatch.setattr("simc_cli.provider.ripgrep_available", lambda: True)
    result = runner.invoke(simc_app, ["--repo-root", str(repo_root), "doctor"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["provider"] == "simc"
    assert payload["data"]["status"] == "ready"
    assert payload["data"]["capabilities"]["search"] == "coming_soon"
    assert {payload["data"]["capabilities"][name] for name in ("version", "repo", "priority", "modify_build")} == {"ready"}
    assert payload["data"]["dependencies"]["ripgrep"]["available"] is True


def test_simc_doctor_does_not_call_binary_commands_ready_without_a_usable_binary(monkeypatch, tmp_path: Path) -> None:
    """doctor used to advertise decode_build as ready while `repo.build_ready` was false."""
    repo_root = _checkout(tmp_path)
    (repo_root / "build" / "simc").unlink()
    monkeypatch.setattr("simc_cli.provider.ripgrep_available", lambda: True)

    result = runner.invoke(simc_app, ["--repo-root", str(repo_root), "doctor"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)["data"]
    assert payload["status"] == "degraded"
    assert payload["repo"]["build_ready"] is False
    assert payload["dependencies"]["simc_binary"]["available"] is False
    assert {payload["capabilities"][name] for name in ("decode_build", "describe_build", "modify_build", "sim")} == {
        "unavailable"
    }
    # A command that only reads files stays usable without the binary.
    assert payload["capabilities"]["spec_files"] == "ready"


def test_simc_doctor_reports_ripgrep_as_the_dependency_it_is(monkeypatch, tmp_path: Path) -> None:
    """Without ripgrep three commands cannot work; doctor used to call all three ready."""
    _stub_binary_banner(monkeypatch)
    monkeypatch.setattr("simc_cli.provider.ripgrep_available", lambda: False)

    result = runner.invoke(simc_app, ["--repo-root", str(_checkout(tmp_path)), "doctor"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)["data"]
    assert payload["status"] == "degraded"
    assert payload["dependencies"]["ripgrep"] == {
        "required_by": ["find_action", "spec_files", "trace_action"],
        "available": False,
    }
    assert {payload["capabilities"][name] for name in ("find_action", "spec_files", "trace_action")} == {"unavailable"}


def test_simc_repo_reports_a_binary_built_from_an_older_commit_than_the_checkout(monkeypatch, tmp_path: Path) -> None:
    """A stale binary decodes hashes against older trait data, which is why the checkout's own
    profiles get rejected; `repo` used to call that build ready."""
    repo_root = _checkout(tmp_path)
    _stub_binary_banner(monkeypatch, "SimulationCraft 1210-01 for WoW 12.1.0 Live (git build midnight 3377576e3b)")
    monkeypatch.setattr(
        "simc_cli.provider.repo_git_status",
        lambda _paths: {"git": True, "dirty": False, "branch": "midnight", "head": "0908ace08c9b", "dirty_entries": []},
    )

    result = runner.invoke(simc_app, ["--repo-root", str(repo_root), "doctor"])

    assert result.exit_code == 0
    repo = json.loads(result.stdout)["data"]["repo"]
    assert repo["binary"]["git_revision"] == "3377576e3b"
    assert repo["binary"]["matches_checkout"] is False
    assert repo["build_ready"] is False
    assert any("3377576e3b" in issue and "0908ace08c9b" in issue for issue in repo["build_issues"])


def test_simc_search_is_structured_coming_soon() -> None:
    result = runner.invoke(simc_app, ["search", "mistweaver"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["coming_soon"] is True
    assert payload["data"]["count"] == 0


def test_simc_envelopes_carry_only_the_envelope_keys(tmp_path: Path) -> None:
    """The payload lives in data; the deprecated top-level copies of it are gone."""
    success = runner.invoke(simc_app, ["search", "mistweaver"])
    failure = runner.invoke(simc_app, ["--repo-root", str(_checkout(tmp_path)), "decode-build"])

    assert set(json.loads(success.stdout)) == ENVELOPE_KEYS - {"error"}
    assert set(json.loads(failure.stderr)) == ENVELOPE_KEYS


def test_simc_repo_reports_and_updates_resolution(monkeypatch, tmp_path: Path) -> None:
    target = tmp_path / "simc"
    target.mkdir()

    monkeypatch.setattr(
        "simc_cli.main._repo_resolution",
        lambda ctx: type(
            "Resolution",
            (),
            {
                "root": target.resolve(),
                "source": "config",
                "config_path": tmp_path / "repo.json",
                "configured_root": target.resolve(),
                "managed_root": tmp_path / "managed",
                "managed_exists": False,
            },
        )(),
    )
    monkeypatch.setattr("simc_cli.main.save_configured_repo_root", lambda root: target.resolve())

    result = runner.invoke(simc_app, ["repo", "--set-root", str(target)])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["action"] == "set_root"
    assert payload["data"]["changed"] is True
    assert payload["data"]["resolution"]["source"] == "config"


def test_simc_repo_reports_unset_resolution(monkeypatch, tmp_path: Path) -> None:
    config_home = tmp_path / "config"
    data_home = tmp_path / "data"
    managed = data_home / "warcraft" / "simc" / "repo"

    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    monkeypatch.delenv("SIMC_REPO_ROOT", raising=False)

    result = runner.invoke(simc_app, ["repo"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["resolution"]["root"] == str(managed.resolve())
    assert payload["data"]["resolution"]["source"] == "unset"
    assert payload["data"]["resolution"]["configured_root"] is None
    assert payload["data"]["resolution"]["managed_root"] == str(managed.resolve())
    assert payload["data"]["resolution"]["managed_exists"] is False


def test_simc_checkout_reports_managed_checkout(monkeypatch, tmp_path: Path) -> None:
    managed = tmp_path / "managed"

    monkeypatch.setattr(
        "simc_cli.main.checkout_managed_repo",
        lambda: type("Checkout", (), {"status": "cloned", "root": managed,
                     "repo_url": "https://github.com/simulationcraft/simc.git", "commands": [["git", "clone"]]})(),
    )
    monkeypatch.setattr(
        "simc_cli.main._repo_resolution",
        lambda ctx: type(
            "Resolution",
            (),
            {
                "root": managed,
                "source": "managed",
                "config_path": tmp_path / "repo.json",
                "configured_root": None,
                "managed_root": managed,
                "managed_exists": True,
            },
        )(),
    )

    result = runner.invoke(simc_app, ["checkout"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["status"] == "cloned"
    assert payload["data"]["active_resolution"]["source"] == "managed"


def test_simc_version_uses_binary_probe(monkeypatch) -> None:
    monkeypatch.setattr(
        "simc_cli.main.binary_version",
        lambda paths: type("VersionInfo", (), {"binary_path": Path("/tmp/simc"), "available": True,
                           "version_line": "SimulationCraft 1201", "returncode": 1})(),
    )
    result = runner.invoke(simc_app, ["version"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["version"] == "SimulationCraft 1201"


def test_simc_spec_files_returns_grouped_results(monkeypatch, tmp_path: Path) -> None:
    repo_root = _checkout(tmp_path)
    monkeypatch.setattr(
        "simc_cli.main.spec_file_search",
        lambda paths, query: {
            "default_apl": [Path("/tmp/simc/ActionPriorityLists/default/monk_mistweaver.simc")],
            "assisted_apl": [],
            "cpp": [],
            "hpp": [],
            "spell_dump": [],
        },
    )
    result = runner.invoke(simc_app, ["--repo-root", str(repo_root), "spec-files", "mistweaver"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["count"] == 1
    assert payload["data"]["categories"]["default_apl"]["items"][0]["stem"] == "monk_mistweaver"


def test_simc_decode_build_outputs_decoded_talents(monkeypatch) -> None:
    monkeypatch.setattr(
        "simc_cli.main.load_build_spec",
        lambda **kwargs: BuildSpec(actor_class="monk",
            spec="mistweaver",
            talents="ABC123",
            class_talents=None,
            spec_talents=None,
            hero_talents=None,
            source_kind="wow_talent_export",
            source_notes=["command-line build options"]),
    )
    monkeypatch.setattr(
        "simc_cli.main.decode_build",
        lambda paths, build_spec: _resolution(
            actor_class="monk",
            spec="mistweaver",
            generated_profile_text='monk="simc_decode"\nlevel=90\nrace=pandaren\nspec=mistweaver\ntalents=ABC123\n',
            talents_by_tree={
                "class": [],
                "spec": [_talent("spec", "Ancient Teachings", 1)],
                "hero": [_talent("hero", "Jadefire Stomp", 2)],
                "selection": [],
            },
        ),
    )
    result = runner.invoke(simc_app, ["decode-build", "--actor-class", "monk", "--spec", "mistweaver", "--talents", "ABC123"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["build_spec"]["source_kind"] == "wow_talent_export"
    assert payload["data"]["decoded"]["actor_class"] == "monk"
    assert payload["data"]["decoded"]["source_kind"] == "wow_talent_export"
    assert 'talents=ABC123' in payload["data"]["decoded"]["generated_profile"]
    assert payload["data"]["decoded"]["enabled_talents"] == ["ancient_teachings", "jadefire_stomp"]


def test_simc_identify_build_reports_probe_result(monkeypatch) -> None:
    monkeypatch.setattr(
        "simc_cli.main._load_identified_build_spec",
        lambda *args, **kwargs: (
            BuildSpec(actor_class="demonhunter",
                    spec="devourer",
                    talents="ABC123",
                    class_talents=None,
                    spec_talents=None,
                    hero_talents=None,
                    source_kind="wow_talent_export",
                    source_notes=["single-line talent export", "identified by SimC probe"]),
            BuildIdentity(actor_class="demonhunter",
                    spec="devourer",
                    confidence="high",
                    source="simc_probe",
                    candidate_count=1,
                    candidates=[("demonhunter", "devourer")],
                    source_notes=["single-line talent export", "identified by SimC probe"]),
        ),
    )
    result = runner.invoke(simc_app, ["identify-build", "--build-text", "ABC123"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["kind"] == "identify_build"
    assert payload["data"]["identity"]["source"] == "simc_probe"
    assert payload["data"]["identity"]["candidates"] == [{"actor_class": "demonhunter", "spec": "devourer"}]
    assert payload["data"]["identity"]["identity_contract"]["kind"] == "build_identity"
    assert payload["data"]["identity"]["identity_contract"]["class_spec_identity"]["status"] == "inferred"


def test_simc_identify_build_accepts_build_packet(monkeypatch, tmp_path: Path) -> None:
    packet_path = tmp_path / "build-packet.json"
    packet_path.write_text('{"kind":"talent_transport_packet"}')

    def fake_loader(_paths, **kwargs):  # noqa: ANN001
        assert kwargs["build_packet"] == str(packet_path)
        return (
            BuildSpec(actor_class="druid",
                    spec="balance",
                    talents="ABC123",
                    class_talents=None,
                    spec_talents=None,
                    hero_talents=None,
                    source_kind="wowhead_talent_calc_url",
                    source_notes=["talent transport packet"],
                    transport_form="wowhead_talent_calc_url",
                    transport_status="exact",
                    transport_source=str(packet_path)),
            BuildIdentity(actor_class="druid",
                    spec="balance",
                    confidence="high",
                    source="wowhead_talent_calc_url",
                    candidate_count=1,
                    candidates=[("druid", "balance")],
                    source_notes=["talent transport packet"]),
        )

    monkeypatch.setattr("simc_cli.main._load_identified_build_spec", fake_loader)

    result = runner.invoke(simc_app, ["identify-build", "--build-packet", str(packet_path)])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["build_spec"]["transport_packet"]["path"] == str(packet_path)
    assert payload["data"]["build_spec"]["transport_packet"]["transport_form"] == "wowhead_talent_calc_url"
    assert payload["data"]["build_spec"]["transport_packet"]["transport_status"] == "exact"


def test_simc_identify_build_accepts_wow_export_transport_form_from_build_packet(monkeypatch, tmp_path: Path) -> None:
    packet_path = tmp_path / "build-packet.json"
    packet_path.write_text(
        json.dumps(
            {
                "kind": "talent_transport_packet",
                "transport_status": "exact",
                "build_identity": {
                    "class_spec_identity": {
                        "identity": {"actor_class": "druid", "spec": "balance"},
                    }
                },
                "transport_forms": {"wow_talent_export": "ABC123"},
                "raw_evidence": {"reference_type": "wow_talent_export"},
                "validation": {},
                "scope": {},
            }
        )
    )

    monkeypatch.setattr(
        "simc_cli.main.identify_build",
        lambda _paths, build_spec: (
            build_spec,
            BuildIdentity(actor_class=build_spec.actor_class,
                    spec=build_spec.spec,
                    confidence="high",
                    source=build_spec.source_kind,
                    candidate_count=1,
                    candidates=[(build_spec.actor_class, build_spec.spec)],
                    source_notes=build_spec.source_notes),
        ),
    )

    result = runner.invoke(simc_app, ["identify-build", "--build-packet", str(packet_path)])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["build_spec"]["source_kind"] == "wow_talent_export"
    assert payload["data"]["build_spec"]["talents"] == "ABC123"
    assert payload["data"]["build_spec"]["transport_packet"]["transport_form"] == "wow_talent_export"
    assert payload["data"]["identity"]["source"] == "wow_talent_export"


def test_simc_identify_build_probes_wow_export_packet_instead_of_trusting_packet_identity(monkeypatch, tmp_path: Path) -> None:
    packet_path = tmp_path / "build-packet.json"
    packet_path.write_text(
        json.dumps(
            {
                "kind": "talent_transport_packet",
                "transport_status": "exact",
                "build_identity": {
                    "class_spec_identity": {
                        "identity": {"actor_class": "priest", "spec": "shadow"},
                    }
                },
                "transport_forms": {"wow_talent_export": "ABC123"},
                "raw_evidence": {"reference_type": "wow_talent_export"},
                "validation": {},
                "scope": {},
            }
        )
    )

    repo = RepoPaths(
        root=tmp_path,
        apl_default=tmp_path,
        apl_assisted=tmp_path,
        class_modules=tmp_path,
        spell_dump=tmp_path,
        build_dir=tmp_path,
        build_simc=tmp_path / "simc",
    )
    monkeypatch.setattr("simc_cli.main._repo_paths", lambda _ctx: repo)
    monkeypatch.setattr(
        "simc_cli.build_input.specialization_ids",
        lambda _root: {("druid", "balance"): 102, ("priest", "shadow"): 258},
    )
    monkeypatch.setattr(
        "simc_cli.build_input.decode_build",
        lambda _repo, build_spec: (
            (_ for _ in ()).throw(SimcBuildError("wrong spec", output_preview=[], returncode=1))
            if (build_spec.actor_class, build_spec.spec) == ("priest", "shadow")
            else _resolution(enabled={"moonkin_form"})
        ),
    )

    result = runner.invoke(simc_app, ["identify-build", "--build-packet", str(packet_path)])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["build_spec"]["actor_class"] == "druid"
    assert payload["data"]["build_spec"]["spec"] == "balance"
    assert payload["data"]["build_spec"]["source_kind"] == "wow_talent_export"
    assert payload["data"]["identity"]["source"] == "simc_probe"
    assert payload["data"]["identity"]["candidates"] == [{"actor_class": "druid", "spec": "balance"}]


def test_simc_identify_build_trusts_validated_split_packet_identity(monkeypatch, tmp_path: Path) -> None:
    packet_path = tmp_path / "build-packet.json"
    packet_path.write_text(
        json.dumps(
            {
                "kind": "talent_transport_packet",
                "transport_status": "validated",
                "build_identity": {
                    "class_spec_identity": {
                        "identity": {"actor_class": "priest", "spec": "shadow"},
                    }
                },
                "transport_forms": {
                    "simc_split_talents": {
                        "class_talents": "103324:1",
                        "spec_talents": "109839:1",
                        "hero_talents": "117176:1",
                    }
                },
                "raw_evidence": {
                    "talent_tree_entries": [{"entry": 103324, "node_id": 82244, "rank": 1}],
                },
                "validation": {"status": "validated", "actor_class": "priest", "spec": "shadow"},
                "scope": {},
            }
        )
    )

    repo = RepoPaths(
        root=_checkout(tmp_path),
        apl_default=tmp_path,
        apl_assisted=tmp_path,
        class_modules=tmp_path,
        spell_dump=tmp_path,
        build_dir=tmp_path,
        build_simc=tmp_path / "simc",
    )
    monkeypatch.setattr("simc_cli.main._repo_paths", lambda _ctx: repo)
    monkeypatch.setattr(
        "simc_cli.build_input.decode_build",
        lambda _repo, build_spec: (_ for _ in ()).throw(AssertionError("validated split packets should not reprobe")),
    )

    result = runner.invoke(simc_app, ["identify-build", "--build-packet", str(packet_path)])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["build_spec"]["actor_class"] == "priest"
    assert payload["data"]["build_spec"]["spec"] == "shadow"
    assert payload["data"]["build_spec"]["source_kind"] == "simc_split_talents"
    assert payload["data"]["identity"]["source"] == "simc_split_talents"
    assert payload["data"]["identity"]["candidates"] == [{"actor_class": "priest", "spec": "shadow"}]


def test_simc_identify_build_rejects_unvalidated_split_packet(monkeypatch, tmp_path: Path) -> None:
    packet_path = tmp_path / "build-packet.json"
    packet_path.write_text(
        json.dumps(
            {
                "kind": "talent_transport_packet",
                "transport_status": "raw_only",
                "build_identity": {
                    "class_spec_identity": {
                        "identity": {"actor_class": "priest", "spec": "shadow"},
                    }
                },
                "transport_forms": {
                    "simc_split_talents": {
                        "class_talents": "103324:1",
                        "spec_talents": "109839:1",
                    }
                },
                "raw_evidence": {
                    "talent_tree_entries": [{"entry": 103324, "node_id": 82244, "rank": 1}],
                },
                "validation": {"status": "not_validated"},
                "scope": {},
            }
        )
    )

    result = runner.invoke(simc_app, ["identify-build", "--build-packet", str(packet_path)])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_build_packet"
    assert "simc_split_talents transport form requires a validated packet identity" in payload["error"]["message"]


def test_simc_identify_build_does_not_let_apl_override_validated_split_packet_identity(monkeypatch, tmp_path: Path) -> None:
    packet_path = tmp_path / "build-packet.json"
    apl_path = tmp_path / "priest_shadow.simc"
    apl_path.write_text("actions=mind_blast\n")
    packet_path.write_text(
        json.dumps(
            {
                "kind": "talent_transport_packet",
                "transport_status": "validated",
                "build_identity": {
                    "class_spec_identity": {
                        "identity": {"actor_class": "priest", "spec": "shadow"},
                    }
                },
                "transport_forms": {
                    "simc_split_talents": {
                        "class_talents": "103324:1",
                        "spec_talents": "109839:1",
                        "hero_talents": "117176:1",
                    }
                },
                "raw_evidence": {
                    "talent_tree_entries": [{"entry": 103324, "node_id": 82244, "rank": 1}],
                },
                "validation": {"status": "validated", "actor_class": "priest", "spec": "shadow"},
                "scope": {},
            }
        )
    )

    repo = RepoPaths(
        root=_checkout(tmp_path),
        apl_default=tmp_path,
        apl_assisted=tmp_path,
        class_modules=tmp_path,
        spell_dump=tmp_path,
        build_dir=tmp_path,
        build_simc=tmp_path / "simc",
    )
    monkeypatch.setattr("simc_cli.main._repo_paths", lambda _ctx: repo)
    monkeypatch.setattr(
        "simc_cli.build_input.decode_build",
        lambda _repo, build_spec: (_ for _ in ()).throw(AssertionError("validated split packets should not reprobe")),
    )

    result = runner.invoke(simc_app, ["identify-build", "--apl-path", str(apl_path), "--build-packet", str(packet_path)])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["build_spec"]["actor_class"] == "priest"
    assert payload["data"]["build_spec"]["spec"] == "shadow"
    assert payload["data"]["identity"]["source"] == "simc_split_talents"
    assert payload["data"]["identity"]["candidates"] == [{"actor_class": "priest", "spec": "shadow"}]


def test_simc_identify_build_rejects_malformed_build_packet(tmp_path: Path) -> None:
    packet_path = tmp_path / "bad-packet.json"
    packet_path.write_text(
        json.dumps(
            {
                "kind": "talent_transport_packet",
                "transport_status": "validated",
                "build_identity": {},
                "transport_forms": {"wowhead_talent_calc_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123"},
                "raw_evidence": {"reference_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123"},
                "validation": {},
                "scope": {},
            }
        )
    )

    result = runner.invoke(simc_app, ["identify-build", "--build-packet", str(packet_path)])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_build_packet"
    assert "does not match packet contents" in payload["error"]["message"]


def test_simc_identify_build_rejects_exact_packet_identity_mismatch(tmp_path: Path) -> None:
    packet_path = tmp_path / "exact-packet.json"
    packet_path.write_text(
        json.dumps(
            {
                "kind": "talent_transport_packet",
                "transport_status": "exact",
                "build_identity": {
                    "class_spec_identity": {
                        "identity": {"actor_class": "hunter", "spec": "beast_mastery"},
                    }
                },
                "transport_forms": {
                    "wowhead_talent_calc_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123"
                },
                "raw_evidence": {"reference_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123"},
                "validation": {},
                "scope": {},
            }
        )
    )

    result = runner.invoke(simc_app, ["identify-build", "--build-packet", str(packet_path)])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_build_packet"
    assert "must match build_identity.class_spec_identity.identity" in payload["error"]["message"]


def test_simc_identify_build_rejects_raw_only_build_packet_without_transport_form(tmp_path: Path) -> None:
    packet_path = tmp_path / "raw-only-packet.json"
    packet_path.write_text(
        json.dumps(
            {
                "kind": "talent_transport_packet",
                "transport_status": "raw_only",
                "build_identity": {
                    "class_spec_identity": {
                        "identity": {"actor_class": "druid", "spec": "balance"},
                    }
                },
                "transport_forms": {},
                "raw_evidence": {
                    "talent_tree_entries": [{"entry": 103324, "node_id": 82244, "rank": 1}],
                },
                "validation": {"status": "not_validated"},
                "scope": {},
            }
        )
    )

    result = runner.invoke(simc_app, ["identify-build", "--build-packet", str(packet_path)])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_build_packet"
    assert "validate-talent-transport first" in payload["error"]["message"]


def test_simc_identify_build_rejects_build_packet_with_override_inputs(tmp_path: Path) -> None:
    packet_path = tmp_path / "exact-packet.json"
    packet_path.write_text(
        json.dumps(
            {
                "kind": "talent_transport_packet",
                "transport_status": "exact",
                "build_identity": {
                    "class_spec_identity": {
                        "identity": {"actor_class": "druid", "spec": "balance"},
                    }
                },
                "transport_forms": {
                    "wowhead_talent_calc_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123",
                },
                "raw_evidence": {"reference_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123"},
                "validation": {},
                "scope": {},
            }
        )
    )

    result = runner.invoke(
        simc_app,
        ["identify-build", "--build-packet", str(packet_path), "--talents", "XYZ987"],
    )
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_build_packet"
    assert payload["error"]["message"] == "Cannot combine --build-packet with other explicit build input options."


def test_simc_identify_build_rejects_buildless_wowhead_talent_calc_url() -> None:
    result = runner.invoke(
        simc_app,
        ["identify-build", "--build-text", "https://www.wowhead.com/talent-calc/druid/balance"],
    )
    assert result.exit_code == 2
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "unsupported_build_reference"
    assert payload["error"]["details"]["reference_type"] == "wowhead_talent_calc_url"
    assert "no build code" in payload["error"]["message"]


def test_simc_validate_talent_transport_accepts_build_packet(monkeypatch, tmp_path: Path) -> None:
    packet_path = tmp_path / "build-packet.json"
    packet_path.write_text(
        json.dumps(
            {
                "kind": "talent_transport_packet",
                "transport_status": "raw_only",
                "build_identity": {
                    "class_spec_identity": {
                        "identity": {"actor_class": "druid", "spec": "balance"},
                    }
                },
                "transport_forms": {},
                "validation": {},
                "scope": {},
                "raw_evidence": {
                    "talent_tree_entries": [
                        {"entry": 103324, "node_id": 82244, "rank": 1},
                        {"entry": 109839, "node_id": 88206, "rank": 1},
                    ]
                },
            }
        )
    )

    def fake_validate(**kwargs):  # noqa: ANN001
        assert kwargs["actor_class"] == "druid"
        assert kwargs["spec"] == "balance"
        assert kwargs["talent_tree_rows"] == [
            {"entry": 103324, "node_id": 82244, "rank": 1},
            {"entry": 109839, "node_id": 88206, "rank": 1},
        ]
        return {
            "transport_forms": {
                "simc_split_talents": {
                    "class_talents": "103324:1",
                    "spec_talents": "109839:1",
                    "hero_talents": None,
                }
            },
            "validation": {
                "status": "validated",
                "source": "simc_trait_data_round_trip",
                "actor_class": "druid",
                "spec": "balance",
            },
        }

    monkeypatch.setattr("simc_cli.main.validate_talent_tree_transport", fake_validate)

    result = runner.invoke(simc_app, ["validate-talent-transport", "--build-packet", str(packet_path)])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["kind"] == "validate_talent_transport"
    assert payload["data"]["input"]["source"] == "build_packet"
    assert payload["data"]["input"]["packet_transport_status"] == "raw_only"
    assert payload["data"]["transport_status"] == "validated"
    assert payload["data"]["transport_forms"]["simc_split_talents"]["spec_talents"] == "109839:1"
    assert payload["data"]["updated_packet"]["transport_status"] == "validated"
    assert payload["data"]["updated_packet"]["build_identity"]["class_spec_identity"]["identity"] == {
        "actor_class": "druid",
        "spec": "balance",
    }
    assert payload["data"]["updated_packet"]["validation"]["actor_class"] == "druid"
    assert payload["data"]["updated_packet"]["validation"]["spec"] == "balance"
    packet_payload = json.loads(packet_path.read_text())
    assert payload["data"]["updated_packet"].get("source") == packet_payload.get("source")


def test_simc_validate_talent_transport_refreshes_packet_identity_from_cli_override(monkeypatch, tmp_path: Path) -> None:
    packet_path = tmp_path / "build-packet.json"
    packet_path.write_text(
        json.dumps(
            {
                "kind": "talent_transport_packet",
                "transport_status": "raw_only",
                "build_identity": {
                    "class_spec_identity": {
                        "identity": {"actor_class": "druid", "spec": "balance"},
                    }
                },
                "transport_forms": {},
                "validation": {},
                "scope": {},
                "raw_evidence": {
                    "talent_tree_entries": [
                        {"entry": 103324, "node_id": 82244, "rank": 1},
                    ]
                },
            }
        )
    )

    monkeypatch.setattr(
        "simc_cli.main.validate_talent_tree_transport",
        lambda **kwargs: {
            "transport_forms": {
                "simc_split_talents": {
                    "class_talents": "103324:1",
                }
            },
            "validation": {
                "status": "validated",
                "source": "simc_trait_data_round_trip",
                "actor_class": kwargs["actor_class"],
                "spec": kwargs["spec"],
            },
        },
    )

    result = runner.invoke(
        simc_app,
        [
            "validate-talent-transport",
            "--build-packet",
            str(packet_path),
            "--actor-class",
            "priest",
            "--spec",
            "shadow",
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["updated_packet"]["build_identity"]["class_spec_identity"]["identity"] == {
        "actor_class": "priest",
        "spec": "shadow",
    }
    assert payload["data"]["updated_packet"]["validation"]["actor_class"] == "priest"
    assert payload["data"]["updated_packet"]["validation"]["spec"] == "shadow"


def test_simc_validate_talent_transport_rejects_build_packet_with_talent_rows(tmp_path: Path) -> None:
    packet_path = tmp_path / "build-packet.json"
    packet_path.write_text('{"kind":"talent_transport_packet"}')

    result = runner.invoke(
        simc_app,
        ["validate-talent-transport", "--build-packet", str(packet_path), "--talent-row", "103324:82244:1"],
    )
    assert result.exit_code == 2
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_query"
    assert payload["error"]["message"] == "Use either --build-packet or --talent-row, not both."


def test_simc_validate_talent_transport_rejects_out_without_build_packet() -> None:
    result = runner.invoke(
        simc_app,
        ["validate-talent-transport", "--actor-class", "druid", "--spec", "balance",
            "--talent-row", "103324:82244:1", "--out", "./tmp/validated-packet.json"],
    )
    assert result.exit_code == 2
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_query"
    assert payload["error"]["message"] == "--out requires --build-packet."


def test_simc_validate_talent_transport_rejects_malformed_talent_row() -> None:
    result = runner.invoke(
        simc_app,
        ["validate-talent-transport", "--actor-class", "druid", "--spec", "balance", "--talent-row", "1:2"],
    )
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_talent_row"
    assert "entry_id:node_id:rank" in payload["error"]["message"]


def test_simc_validate_talent_transport_rejects_malformed_build_packet(tmp_path: Path) -> None:
    packet_path = tmp_path / "bad-packet.json"
    packet_path.write_text(
        json.dumps(
            {
                "kind": "talent_transport_packet",
                "transport_status": "validated",
                "build_identity": {},
                "transport_forms": {"wowhead_talent_calc_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123"},
                "raw_evidence": {"reference_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123"},
                "validation": {},
                "scope": {},
            }
        )
    )

    result = runner.invoke(simc_app, ["validate-talent-transport", "--build-packet", str(packet_path)])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_build_packet"
    assert "does not match packet contents" in payload["error"]["message"]


def test_simc_validate_talent_transport_rejects_incomplete_raw_only_packet_rows(tmp_path: Path) -> None:
    packet_path = tmp_path / "bad-rows-packet.json"
    packet_path.write_text(
        json.dumps(
            {
                "kind": "talent_transport_packet",
                "transport_status": "raw_only",
                "build_identity": {
                    "class_spec_identity": {
                        "identity": {"actor_class": "druid", "spec": "balance"},
                    }
                },
                "transport_forms": {},
                "raw_evidence": {
                    "talent_tree_entries": [{"entry": 103324, "rank": 1}],
                },
                "validation": {"status": "not_validated"},
                "scope": {},
            }
        )
    )

    result = runner.invoke(simc_app, ["validate-talent-transport", "--build-packet", str(packet_path)])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_build_packet"
    assert "raw_only status requires usable raw talent_tree_entries evidence" in payload["error"]["message"]


def test_simc_validate_talent_transport_rejects_null_only_packet_rows(tmp_path: Path) -> None:
    packet_path = tmp_path / "bad-rows-packet.json"
    packet_path.write_text(
        json.dumps(
            {
                "kind": "talent_transport_packet",
                "transport_status": "unknown",
                "build_identity": {
                    "class_spec_identity": {
                        "identity": {"actor_class": "druid", "spec": "balance"},
                    }
                },
                "transport_forms": {},
                "raw_evidence": {
                    "talent_tree_entries": [{"entry": None, "node_id": None, "rank": None}],
                },
                "validation": {"status": "not_validated"},
                "scope": {},
            }
        )
    )

    result = runner.invoke(simc_app, ["validate-talent-transport", "--build-packet", str(packet_path)])
    assert result.exit_code == 2
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_query"
    assert payload["error"]["message"] == "No raw talent rows were available to validate."


def test_simc_validate_talent_transport_rejects_boolean_packet_rows(tmp_path: Path) -> None:
    packet_path = tmp_path / "bool-rows-packet.json"
    packet_path.write_text(
        json.dumps(
            {
                "kind": "talent_transport_packet",
                "transport_status": "unknown",
                "build_identity": {
                    "class_spec_identity": {
                        "identity": {"actor_class": "druid", "spec": "balance"},
                    }
                },
                "transport_forms": {},
                "raw_evidence": {
                    "talent_tree_entries": [{"entry": True, "node_id": 82244, "rank": 1}],
                },
                "validation": {"status": "not_validated"},
                "scope": {},
            }
        )
    )

    result = runner.invoke(simc_app, ["validate-talent-transport", "--build-packet", str(packet_path)])
    assert result.exit_code == 2
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_query"
    assert payload["error"]["message"] == "No raw talent rows were available to validate."


def test_simc_validate_talent_transport_rejects_talent_rows_without_class_spec_identity() -> None:
    result = runner.invoke(
        simc_app,
        ["validate-talent-transport", "--talent-row", "103324:82244:1"],
    )
    assert result.exit_code == 2
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_query"
    assert "requires class/spec identity" in payload["error"]["message"]


def test_simc_validate_talent_transport_normalizes_packet_refresh_failures(monkeypatch, tmp_path: Path) -> None:
    packet_path = tmp_path / "build-packet.json"
    packet_path.write_text(
        json.dumps(
            {
                "kind": "talent_transport_packet",
                "transport_status": "raw_only",
                "build_identity": {
                    "class_spec_identity": {
                        "identity": {"actor_class": "druid", "spec": "balance"},
                    }
                },
                "transport_forms": {},
                "validation": {"status": "not_validated"},
                "scope": {},
                "raw_evidence": {
                    "talent_tree_entries": [
                        {"entry": 103324, "node_id": 82244, "rank": 1},
                    ]
                },
            }
        )
    )

    monkeypatch.setattr(
        "simc_cli.main.validate_talent_tree_transport",
        lambda **kwargs: {
            "transport_forms": {"simc_split_talents": {"class_talents": "103324:1"}},
            "validation": {"status": "validated", "actor_class": "druid", "spec": "balance"},
        },
    )
    monkeypatch.setattr(
        "simc_cli.main.refresh_talent_transport_packet",
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("bad refresh")),
    )

    result = runner.invoke(simc_app, ["validate-talent-transport", "--build-packet", str(packet_path)])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_build_packet"
    assert payload["error"]["message"] == "bad refresh"


def test_simc_validate_talent_transport_can_write_upgraded_packet(monkeypatch, tmp_path: Path) -> None:
    packet_path = tmp_path / "build-packet.json"
    out_path = tmp_path / "validated-packet.json"
    packet_path.write_text(
        json.dumps(
            {
                "kind": "talent_transport_packet",
                "transport_status": "raw_only",
                "build_identity": {
                    "class_spec_identity": {
                        "identity": {"actor_class": "druid", "spec": "balance"},
                    }
                },
                "raw_evidence": {
                    "talent_tree_entries": [
                        {"entry": 103324, "node_id": 82244, "rank": 1},
                    ]
                },
                "transport_forms": {},
                "validation": {"status": "not_validated"},
                "scope": {},
                "source": {"provider": "warcraftlogs", "source": "warcraftlogs_talent_tree"},
            }
        )
    )
    monkeypatch.setattr(
        "simc_cli.main.validate_talent_tree_transport",
        lambda **kwargs: {
            "transport_forms": {
                "simc_split_talents": {
                    "class_talents": "103324:1",
                }
            },
            "validation": {
                "status": "validated",
                "source": "simc_trait_data_round_trip",
                "actor_class": "druid",
                "spec": "balance",
            },
        },
    )

    result = runner.invoke(
        simc_app,
        [
            "validate-talent-transport",
            "--build-packet",
            str(packet_path),
            "--out",
            str(out_path),
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["written_packet_path"] == str(out_path.resolve())

    written = json.loads(out_path.read_text())
    assert written["transport_status"] == "validated"
    assert written["source"] == {"provider": "warcraftlogs", "source": "warcraftlogs_talent_tree"}
    assert written["transport_forms"]["simc_split_talents"]["class_talents"] == "103324:1"


def test_simc_validate_talent_transport_normalizes_write_failure(monkeypatch, tmp_path: Path) -> None:
    packet_path = tmp_path / "build-packet.json"
    out_dir = tmp_path / "out-dir"
    out_dir.mkdir()
    packet_path.write_text(
        json.dumps(
            {
                "kind": "talent_transport_packet",
                "transport_status": "raw_only",
                "build_identity": {
                    "class_spec_identity": {
                        "identity": {"actor_class": "druid", "spec": "balance"},
                    }
                },
                "raw_evidence": {
                    "talent_tree_entries": [
                        {"entry": 103324, "node_id": 82244, "rank": 1},
                    ]
                },
                "transport_forms": {},
                "validation": {"status": "not_validated"},
                "scope": {},
            }
        )
    )
    monkeypatch.setattr(
        "simc_cli.main.validate_talent_tree_transport",
        lambda **kwargs: {
            "transport_forms": {
                "simc_split_talents": {
                    "class_talents": "103324:1",
                }
            },
            "validation": {
                "status": "validated",
                "source": "simc_trait_data_round_trip",
                "actor_class": "druid",
                "spec": "balance",
            },
        },
    )

    result = runner.invoke(
        simc_app,
        ["validate-talent-transport", "--build-packet", str(packet_path), "--out", str(out_dir)],
    )
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "transport_packet_write_failed"


def test_simc_validate_talent_transport_accepts_inline_rows(monkeypatch) -> None:
    def fake_validate(**kwargs):  # noqa: ANN001
        assert kwargs["actor_class"] == "druid"
        assert kwargs["spec"] == "balance"
        assert kwargs["talent_tree_rows"] == [
            {"entry": 103324, "node_id": 82244, "rank": 1},
            {"entry": 109839, "node_id": 88206, "rank": 1},
        ]
        return {
            "transport_forms": {},
            "validation": {
                "status": "not_validated",
                "reason": "simc_trait_resolution_incomplete",
            },
        }

    monkeypatch.setattr("simc_cli.main.validate_talent_tree_transport", fake_validate)

    result = runner.invoke(
        simc_app,
        [
            "validate-talent-transport",
            "--actor-class",
            "druid",
            "--spec",
            "balance",
            "--talent-row",
            "103324:82244:1",
            "--talent-row",
            "109839:88206:1",
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["input"]["source"] == "talent_rows"
    assert payload["data"]["transport_status"] == "raw_only"
    assert payload["data"]["validation"]["reason"] == "simc_trait_resolution_incomplete"


def test_simc_validate_talent_transport_keeps_zero_rank_packets_raw_only(tmp_path: Path) -> None:
    generated = tmp_path / "engine" / "dbc" / "generated"
    generated.mkdir(parents=True)
    (generated / "sc_specialization_data.inc").write_text(
        """enum specialization_e {
  SPEC_NONE              = 0,
  DRUID_BALANCE          = 102,
};
"""
    )
    (generated / "trait_data.inc").write_text(
        "static constexpr std::array<trait_data_t, 1> __trait_data_data { {\n"
        '  { 1, 11, 103324, 82244, 1, 23, 108329, 29166, 0, 0, 10, 8, 100, "Innervate", '
        "{ 0, 0, 0, 0 }, { 0, 0, 0, 0 }, 0, 0 },\n"
        "} };\n"
    )
    packet_path = tmp_path / "build-packet.json"
    packet_path.write_text(
        json.dumps(
            {
                "kind": "talent_transport_packet",
                "transport_status": "raw_only",
                "build_identity": {
                    "class_spec_identity": {
                        "identity": {"actor_class": "druid", "spec": "balance"},
                    }
                },
                "transport_forms": {},
                "raw_evidence": {
                    "talent_tree_entries": [
                        {"entry": 103324, "node_id": 82244, "rank": 0},
                    ]
                },
                "validation": {"status": "not_validated"},
                "scope": {},
            }
        )
    )

    result = runner.invoke(
        simc_app,
        ["--repo-root", str(tmp_path), "validate-talent-transport", "--build-packet", str(packet_path)],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["transport_status"] == "raw_only"
    assert payload["data"]["transport_forms"] == {}
    assert payload["data"]["validation"]["status"] == "not_validated"
    assert payload["data"]["validation"]["reason"] == "no_ranked_talent_entries"
    assert payload["data"]["updated_packet"]["transport_status"] == "raw_only"
    assert payload["data"]["updated_packet"]["transport_forms"] == {}
    assert payload["data"]["updated_packet"]["validation"]["reason"] == "no_ranked_talent_entries"


def test_simc_validate_talent_transport_requires_one_input_mode() -> None:
    result = runner.invoke(simc_app, ["validate-talent-transport"])
    assert result.exit_code == 2
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_query"


def test_simc_decode_build_auto_identifies_missing_class_and_spec(monkeypatch) -> None:
    monkeypatch.setattr(
        "simc_cli.main._load_identified_build_spec",
        lambda *args, **kwargs: (
            BuildSpec(actor_class="demonhunter",
                    spec="devourer",
                    talents="ABC123",
                    class_talents=None,
                    spec_talents=None,
                    hero_talents=None,
                    source_kind="wow_talent_export",
                    source_notes=["single-line talent export", "identified by SimC probe"]),
            BuildIdentity(actor_class="demonhunter",
                    spec="devourer",
                    confidence="high",
                    source="simc_probe",
                    candidate_count=1,
                    candidates=[("demonhunter", "devourer")],
                    source_notes=["single-line talent export", "identified by SimC probe"]),
        ),
    )
    monkeypatch.setattr(
        "simc_cli.main.decode_build",
        lambda paths, build_spec: _resolution(
                actor_class='demonhunter',
                spec='devourer',
                enabled={'void_ray'},
                source_kind='wow_talent_export',
                generated_profile_text='demonhunter="simc_decode"\nlevel=90\nrace=night_elf\nspec=devourer\ntalents=ABC123\n',
                source_notes=['decoded via /tmp/simc'],
            ),
    )
    result = runner.invoke(simc_app, ["decode-build", "--build-text", "ABC123"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["build_spec"]["actor_class"] == "demonhunter"
    assert payload["data"]["identity"]["source"] == "simc_probe"
    assert payload["data"]["decoded"]["spec"] == "devourer"


@pytest.mark.parametrize(
    "command",
    [
        ["decode-build", "--talents", "HOLY_PALADIN_EXPORT"],
        ["describe-build", "--talents", "HOLY_PALADIN_EXPORT"],
        ["build-harness", "--talents", "HOLY_PALADIN_EXPORT"],
        ["compare-builds", "--base", "HOLY_PALADIN_EXPORT", "--other", "HOLY_PALADIN_EXPORT"],
        ["modify-build", "--talents", "HOLY_PALADIN_EXPORT", "--remove", "anything"],
    ],
)
def test_simc_asks_for_class_and_spec_when_no_spec_decodes_the_build(tmp_path: Path, command: list[str]) -> None:
    """Identification decodes the build once per spec SimC knows; when none takes it, say so."""
    repo_root = _checkout(tmp_path)
    (repo_root / "engine" / "dbc" / "generated" / "sc_specialization_data.inc").write_text(
        "  MAGE_ARCANE = 62,\n  PALADIN_RETRIBUTION = 70,\n"
    )
    no_talents = "0.000 Player 'simc_decode' generic base stats\n"
    fake = _FakeSimcBinary({"HOLY_PALADIN_EXPORT": no_talents})

    with patch("simc_cli.build_input.subprocess.run", side_effect=fake):
        result = runner.invoke(simc_app, ["--repo-root", str(repo_root), *command])

    assert result.exit_code == 2
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_query"
    assert "decodes as none of the 2 specs SimulationCraft knows" in payload["error"]["message"]
    assert payload["error"]["details"]["identity"]["candidate_count"] == 0
    # Both specs were tried, one decode each.
    assert sorted(text.splitlines()[0] for text in fake.profiles) == ['mage="simc_decode"', 'paladin="simc_decode"']


def test_simc_unidentified_build_message_names_the_specs_a_class_hint_narrowed_the_probe_to(tmp_path: Path) -> None:
    """With --actor-class only that class's specs are probed, so the message may not claim every spec failed."""
    repo_root = _checkout(tmp_path)
    (repo_root / "engine" / "dbc" / "generated" / "sc_specialization_data.inc").write_text(
        "  MAGE_ARCANE = 62,\n  PALADIN_HOLY = 65,\n  PALADIN_RETRIBUTION = 70,\n"
    )
    fake = _FakeSimcBinary({"ARCANE_EXPORT": "0.000 Player 'simc_decode' generic base stats\n"})

    with patch("simc_cli.build_input.subprocess.run", side_effect=fake):
        result = runner.invoke(
            simc_app, ["--repo-root", str(repo_root), "decode-build", "--talents", "ARCANE_EXPORT", "--actor-class", "paladin"]
        )

    assert result.exit_code == 2
    message = json.loads(result.stderr)["error"]["message"]
    assert "decodes as none of the 2 paladin specs." in message
    assert [text.splitlines()[0] for text in fake.profiles] == ['paladin="simc_decode"', 'paladin="simc_decode"']


@pytest.mark.parametrize(
    ("command", "valid"),
    [
        # An unknown class narrowed the probe to nothing: "decodes as none of the 0 death_night specs".
        (["decode-build", "--talents", "BASE", "--actor-class", "death_night"], "Valid classes: deathknight, demonhunter,"),
        # An impossible pair reached SimC, which blamed the build with invalid_build.
        (["decode-build", "--talents", "BASE", "--actor-class", "mage", "--spec", "holy"], "Valid mage specs: arcane, fire, frost."),
        # The APL views read the hint on their own path, which reported prune_context_failed (exit 1).
        (["apl-prune", "mage_arcane.simc", "--talents", "BASE", "--spec", "holyy"], "Valid mage specs: arcane, fire, frost."),
    ],
    ids=["unknown-class", "impossible-pair", "apl-view"],
)
def test_simc_rejects_a_class_or_spec_hint_simc_has_no_spec_for(tmp_path: Path, command: list[str], valid: str) -> None:
    repo_root = _checkout(tmp_path)
    (repo_root / "mage_arcane.simc").write_text("actions=arcane_blast\n")
    fake = _FakeSimcBinary({"BASE": CAPTURED_ARCANE_MAGE})

    with patch("simc_cli.build_input.subprocess.run", side_effect=fake):
        result = runner.invoke(simc_app, ["--repo-root", str(repo_root), *command])

    assert result.exit_code == 2, result.stdout + result.stderr
    error = json.loads(result.stderr)["error"]
    assert error["code"] == "invalid_query"
    assert valid in error["message"]
    assert fake.profiles == [], "SimC ran for a hint no spec matches"


def test_simc_modify_build_reads_a_class_and_spec_hint_in_any_spelling(tmp_path: Path) -> None:
    """`--spec Arcane` used to crash modify-build with an uncaught KeyError and no envelope."""
    fake = _FakeSimcBinary({"BASE": CAPTURED_ARCANE_MAGE, "MODIFIED_EXPORT": _captured_without("Arcane Tempo")})

    with patch("simc_cli.build_input.subprocess.run", side_effect=fake):
        result = runner.invoke(
            simc_app,
            ["--repo-root", str(_checkout(tmp_path)), "modify-build", "--talents", "BASE",
             "--actor-class", "Mage", "--spec", "Arcane", "--remove", "Arcane Tempo"],
        )

    assert result.exit_code == 0, result.stdout + result.stderr
    base = json.loads(result.stdout)["data"]["base"]
    assert (base["actor_class"], base["spec"]) == ("mage", "arcane")
    assert all("spec=arcane" in text for text in fake.profiles)


@pytest.mark.parametrize(
    ("missing", "reason"),
    [
        ("engine/dbc/generated/sc_specialization_data.inc", "specialization data"),
        ("build/simc", "SimC binary not found"),
        # Needed once a decode succeeds; its absence was reported as the caller's invalid_query (exit 2).
        ("engine/dbc/generated/trait_data.inc", "trait data not found"),
    ],
)
def test_simc_identification_blames_the_checkout_when_it_cannot_probe(tmp_path: Path, missing: str, reason: str) -> None:
    """Without spec data, trait data or a binary nothing is decoded, which used to read as 'decodes as none of the specs'."""
    repo_root = _checkout(tmp_path)
    (repo_root / missing).unlink()

    with patch("simc_cli.build_input.subprocess.run", side_effect=_FakeSimcBinary({"ARCANE_EXPORT": CAPTURED_ARCANE_MAGE})):
        result = runner.invoke(simc_app, ["--repo-root", str(repo_root), "decode-build", "--talents", "ARCANE_EXPORT"])

    assert result.exit_code == 1
    error = json.loads(result.stderr)["error"]
    assert error["code"] == "identify_failed"
    assert reason in error["message"]


def test_simc_identify_build_says_no_build_was_supplied(tmp_path: Path) -> None:
    """It used to answer ok: true with an all-null build and identity source missing_build_data."""
    result = runner.invoke(simc_app, ["--repo-root", str(_checkout(tmp_path)), "identify-build"])

    assert result.exit_code == 2
    error = json.loads(result.stderr)["error"]
    assert error["code"] == "invalid_query"
    assert error["message"].startswith("No build was supplied to identify")


def test_simc_decode_build_rejects_an_empty_talents_option(tmp_path: Path) -> None:
    """An empty `--talents` used to decode to an empty build with ok: true."""
    result = runner.invoke(
        simc_app,
        ["--repo-root", str(_checkout(tmp_path)), "decode-build", "--talents", "", "--actor-class", "monk", "--spec", "mistweaver"],
    )

    assert result.exit_code == 2
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_query"
    assert "--talents" in payload["error"]["message"]


@pytest.mark.parametrize("identity", [[], ["--actor-class", "monk", "--spec", "mistweaver"]])
def test_simc_decode_build_says_no_build_was_supplied(tmp_path: Path, identity: list[str]) -> None:
    """With no talents there is nothing to decode; a class and spec alone used to decode to an empty build."""
    result = runner.invoke(simc_app, ["--repo-root", str(_checkout(tmp_path)), "decode-build", *identity])

    assert result.exit_code == 2
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_query"
    assert payload["error"]["message"].startswith("No talent build was supplied")


@pytest.mark.parametrize("command", ["describe-build", "build-harness"])
def test_simc_build_commands_say_no_build_was_supplied_rather_than_blaming_the_probe(tmp_path: Path, command: str) -> None:
    result = runner.invoke(simc_app, ["--repo-root", str(_checkout(tmp_path)), command])

    assert result.exit_code == 2
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_query"
    assert "no talent build was supplied" in payload["error"]["message"]


def test_simc_decode_build_rejects_a_page_url_as_an_unsupported_build_reference(tmp_path: Path) -> None:
    """A guide URL used to reach SimC as if it were a talent hash, which blamed the build."""
    result = runner.invoke(
        simc_app,
        [
            "--repo-root", str(_checkout(tmp_path)), "decode-build",
            "--build-text", "https://www.icy-veins.com/wow/mistweaver-monk-pve-healing-guide",
        ],
    )

    assert result.exit_code == 2
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "unsupported_build_reference"
    assert payload["error"]["details"]["reference_type"] == "url"


def test_simc_decode_build_reads_back_the_talent_calc_url_modify_build_publishes(tmp_path: Path) -> None:
    """`modify-build` publishes /talent-calc/blizzard/<hash>, which names no class or spec."""
    repo_root = _checkout(tmp_path)
    fake = _FakeSimcBinary({"ARCANE_EXPORT": CAPTURED_ARCANE_MAGE})

    with patch("simc_cli.build_input.subprocess.run", side_effect=fake):
        result = runner.invoke(
            simc_app,
            [
                "--repo-root", str(repo_root), "decode-build",
                "--actor-class", "mage", "--spec", "arcane",
                "--build-text", "https://www.wowhead.com/talent-calc/blizzard/ARCANE_EXPORT",
            ],
        )

    assert result.exit_code == 0, result.stderr
    data = json.loads(result.stdout)["data"]
    assert data["build_spec"]["talents"] == "ARCANE_EXPORT"
    assert data["decoded"]["hero_tree"] == {"name": "Sunfury", "id": 39}


def test_simc_decode_build_accepts_build_packet(monkeypatch, tmp_path: Path) -> None:
    packet_path = tmp_path / "build-packet.json"
    packet_path.write_text('{"kind":"talent_transport_packet"}')

    def fake_loader(_paths, **kwargs):  # noqa: ANN001
        assert kwargs["build_packet"] == str(packet_path)
        return (
            BuildSpec(actor_class="druid",
                    spec="balance",
                    talents=None,
                    class_talents="103324:1",
                    spec_talents="109839:1",
                    hero_talents="117176:1",
                    source_kind="simc_split_talents",
                    source_notes=["talent transport packet"],
                    transport_form="simc_split_talents",
                    transport_status="validated",
                    transport_source=str(packet_path)),
            BuildIdentity(actor_class="druid",
                    spec="balance",
                    confidence="high",
                    source="warcraftlogs_talent_tree",
                    candidate_count=1,
                    candidates=[("druid", "balance")],
                    source_notes=["talent transport packet"]),
        )

    monkeypatch.setattr("simc_cli.main._load_identified_build_spec", fake_loader)
    monkeypatch.setattr(
        "simc_cli.main.decode_build",
        lambda paths, build_spec: _resolution(
                actor_class='druid',
                spec='balance',
                enabled={'innervate', 'incarnation_chosen_of_elune'},
                source_kind='simc_split_talents',
                generated_profile_text='druid="simc_decode"\nclass_talents=103324:1\nspec_talents=109839:1\nhero_talents=117176:1\n',
                source_notes=['talent transport packet', 'decoded via /tmp/simc'],
            ),
    )

    result = runner.invoke(simc_app, ["decode-build", "--build-packet", str(packet_path)])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["build_spec"]["transport_packet"]["transport_form"] == "simc_split_talents"
    assert payload["data"]["decoded"]["source_kind"] == "simc_split_talents"


def test_simc_decode_build_uses_validated_split_packet_identity(monkeypatch, tmp_path: Path) -> None:
    packet_path = tmp_path / "build-packet.json"
    packet_path.write_text(
        json.dumps(
            {
                "kind": "talent_transport_packet",
                "transport_status": "validated",
                "build_identity": {
                    "class_spec_identity": {
                        "identity": {"actor_class": "priest", "spec": "shadow"},
                    }
                },
                "transport_forms": {
                    "simc_split_talents": {
                        "class_talents": "103324:1",
                        "spec_talents": "109839:1",
                    }
                },
                "raw_evidence": {"talent_tree_entries": [{"entry": 103324, "node_id": 82244, "rank": 1}]},
                "validation": {"status": "validated", "actor_class": "priest", "spec": "shadow"},
                "scope": {},
            }
        )
    )

    repo = RepoPaths(
        root=_checkout(tmp_path),
        apl_default=tmp_path,
        apl_assisted=tmp_path,
        class_modules=tmp_path,
        spell_dump=tmp_path,
        build_dir=tmp_path,
        build_simc=tmp_path / "simc",
    )
    monkeypatch.setattr("simc_cli.main._repo_paths", lambda _ctx: repo)

    def fake_decode_build(_paths, build_spec):  # noqa: ANN001
        assert build_spec.actor_class == "priest"
        assert build_spec.spec == "shadow"
        return _resolution(
                actor_class='priest',
                spec='shadow',
                enabled={'mind_blast'},
                source_kind='simc_split_talents',
                generated_profile_text='priest="simc_decode"\nclass_talents=103324:1\nspec_talents=109839:1\n',
                source_notes=['talent transport packet', 'decoded via /tmp/simc'],
            )

    monkeypatch.setattr("simc_cli.main.decode_build", fake_decode_build)

    result = runner.invoke(simc_app, ["decode-build", "--build-packet", str(packet_path)])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["identity"]["source"] == "simc_split_talents"
    assert payload["data"]["build_spec"]["actor_class"] == "priest"
    assert payload["data"]["build_spec"]["spec"] == "shadow"


def test_simc_decode_build_accepts_wowhead_transport_form_from_build_packet(monkeypatch, tmp_path: Path) -> None:
    packet_path = tmp_path / "exact-packet.json"
    packet_path.write_text(
        json.dumps(
            {
                "kind": "talent_transport_packet",
                "transport_status": "exact",
                "build_identity": {
                    "class_spec_identity": {
                        "identity": {"actor_class": "druid", "spec": "balance"},
                    }
                },
                "transport_forms": {
                    "wowhead_talent_calc_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123"
                },
                "raw_evidence": {
                    "reference_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123"
                },
                "validation": {},
                "scope": {},
            }
        )
    )

    def fake_decode_build(_paths, build_spec):  # noqa: ANN001
        assert build_spec.actor_class == "druid"
        assert build_spec.spec == "balance"
        assert build_spec.talents == "ABC123"
        assert build_spec.source_kind == "wowhead_talent_calc_url"
        assert build_spec.transport_form == "wowhead_talent_calc_url"
        return _resolution(
                actor_class='druid',
                spec='balance',
                enabled={'moonkin_form'},
                source_kind='wowhead_talent_calc_url',
                generated_profile_text='druid="simc_decode"\ntalents=ABC123\n',
                source_notes=['talent transport packet'],
            )

    monkeypatch.setattr("simc_cli.main.decode_build", fake_decode_build)

    result = runner.invoke(simc_app, ["decode-build", "--build-packet", str(packet_path)])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["build_spec"]["source_kind"] == "wowhead_talent_calc_url"
    assert payload["data"]["build_spec"]["talents"] == "ABC123"
    assert payload["data"]["build_spec"]["transport_packet"]["transport_form"] == "wowhead_talent_calc_url"
    assert payload["data"]["decoded"]["source_kind"] == "wowhead_talent_calc_url"


def test_simc_decode_build_probes_wow_export_packet_instead_of_trusting_packet_identity(monkeypatch, tmp_path: Path) -> None:
    packet_path = tmp_path / "exact-packet.json"
    packet_path.write_text(
        json.dumps(
            {
                "kind": "talent_transport_packet",
                "transport_status": "exact",
                "build_identity": {
                    "class_spec_identity": {
                        "identity": {"actor_class": "priest", "spec": "shadow"},
                    }
                },
                "transport_forms": {"wow_talent_export": "ABC123"},
                "raw_evidence": {"reference_type": "wow_talent_export"},
                "validation": {},
                "scope": {},
            }
        )
    )

    repo = RepoPaths(
        root=tmp_path,
        apl_default=tmp_path,
        apl_assisted=tmp_path,
        class_modules=tmp_path,
        spell_dump=tmp_path,
        build_dir=tmp_path,
        build_simc=tmp_path / "simc",
    )
    monkeypatch.setattr("simc_cli.main._repo_paths", lambda _ctx: repo)
    monkeypatch.setattr(
        "simc_cli.build_input.specialization_ids",
        lambda _root: {("druid", "balance"): 102, ("priest", "shadow"): 258},
    )
    monkeypatch.setattr(
        "simc_cli.build_input.decode_build",
        lambda _repo, build_spec: (
            (_ for _ in ()).throw(SimcBuildError("wrong spec", output_preview=[], returncode=1))
            if (build_spec.actor_class, build_spec.spec) == ("priest", "shadow")
            else _resolution(enabled={"moonkin_form"})
        ),
    )

    def fake_decode_build(_paths, build_spec):  # noqa: ANN001
        assert build_spec.actor_class == "druid"
        assert build_spec.spec == "balance"
        return _resolution(
                actor_class='druid',
                spec='balance',
                enabled={'moonkin_form'},
                source_kind='wow_talent_export',
                generated_profile_text='druid="simc_decode"\ntalents=ABC123\n',
                source_notes=['decoded via /tmp/simc'],
            )

    monkeypatch.setattr("simc_cli.main.decode_build", fake_decode_build)

    result = runner.invoke(simc_app, ["decode-build", "--build-packet", str(packet_path)])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["build_spec"]["actor_class"] == "druid"
    assert payload["data"]["build_spec"]["spec"] == "balance"
    assert payload["data"]["identity"]["source"] == "simc_probe"
    assert payload["data"]["decoded"]["actor_class"] == "druid"
    assert payload["data"]["decoded"]["spec"] == "balance"


def test_simc_decode_build_rejects_malformed_build_packet(tmp_path: Path) -> None:
    packet_path = tmp_path / "bad-packet.json"
    packet_path.write_text(
        json.dumps(
            {
                "kind": "talent_transport_packet",
                "transport_status": "validated",
                "build_identity": {},
                "transport_forms": {"wowhead_talent_calc_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123"},
                "raw_evidence": {"reference_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123"},
                "validation": {},
                "scope": {},
            }
        )
    )

    result = runner.invoke(simc_app, ["decode-build", "--build-packet", str(packet_path)])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_build_packet"
    assert "does not match packet contents" in payload["error"]["message"]


def test_simc_decode_build_rejects_raw_only_build_packet_without_transport_form(tmp_path: Path) -> None:
    packet_path = tmp_path / "raw-only-packet.json"
    packet_path.write_text(
        json.dumps(
            {
                "kind": "talent_transport_packet",
                "transport_status": "raw_only",
                "build_identity": {
                    "class_spec_identity": {
                        "identity": {"actor_class": "druid", "spec": "balance"},
                    }
                },
                "transport_forms": {},
                "raw_evidence": {
                    "talent_tree_entries": [{"entry": 103324, "node_id": 82244, "rank": 1}],
                },
                "validation": {"status": "not_validated"},
                "scope": {},
            }
        )
    )

    result = runner.invoke(simc_app, ["decode-build", "--build-packet", str(packet_path)])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_build_packet"
    assert "validate-talent-transport first" in payload["error"]["message"]


def test_simc_decode_build_rejects_buildless_wowhead_talent_calc_url() -> None:
    result = runner.invoke(
        simc_app,
        ["decode-build", "--build-text", "https://www.wowhead.com/talent-calc/druid/balance"],
    )
    assert result.exit_code == 2
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "unsupported_build_reference"
    assert payload["error"]["details"]["reference_type"] == "wowhead_talent_calc_url"
    assert "no build code" in payload["error"]["message"]


def test_simc_describe_build_summarizes_st_and_aoe(monkeypatch, tmp_path: Path) -> None:
    apl_path = tmp_path / "demonhunter_devourer.simc"
    apl_path.write_text("actions=void_ray\n")

    monkeypatch.setattr(
        "simc_cli.main._load_identified_build_spec",
        lambda *args, **kwargs: (
            BuildSpec(actor_class="demonhunter",
                    spec="devourer",
                    talents="ABC123",
                    class_talents=None,
                    spec_talents=None,
                    hero_talents=None,
                    source_kind="wow_talent_export",
                    source_notes=["single-line talent export"]),
            BuildIdentity(actor_class="demonhunter",
                    spec="devourer",
                    confidence="high",
                    source="simc_probe",
                    candidate_count=1,
                    candidates=[("demonhunter", "devourer")],
                    source_notes=["single-line talent export", "identified by SimC probe"]),
        ),
    )

    resolution = _resolution(
        actor_class="demonhunter",
        spec="devourer",
        enabled={"void_ray", "world_killer", "soul_immolation"},
        talents_by_tree={
            "class": [_talent("class", "Voidblade", 1)],
            "spec": [
                _talent("spec", "Void Ray", 2),
                _talent("spec", "Midnight", 3, rank=0),
                _talent("spec", "Soul Immolation", 4),
            ],
            "hero": [_talent("hero", "World Killer", 5)],
            "selection": [],
        },
        hero_tree=HeroTree(name="Annihilator", id=124),
        inactive_hero_talents=[_talent("hero", "Void Reaver", 6)],
    )

    def _resolve_prune_context(_paths, _apl, _values, targets):
        context = type("Context", (), {"targets": targets, "enabled_talents": {"void_ray", "world_killer"},
                       "disabled_talents": set(), "talent_sources": {"void_ray": "spec"}})()
        return context, resolution

    monkeypatch.setattr("simc_cli.main._resolve_prune_context", _resolve_prune_context)

    def _describe_target_payload(_resolved, context, *, start_list, priority_limit, inactive_limit):
        if context.targets == 1:
            return {
                "targets": 1,
                "focus_list": "melee_combo",
                "dispatch_certainty": "guaranteed",
                "branch_summary": {"start_list": start_list, "guaranteed_dispatch": "melee_combo", "guaranteed_dispatch_line": 4, "guaranteed_dispatch_reason": "no condition", "dead_branches": [], "unresolved_branches": [], "shadowed_lines": []},
                "active_priority": [
                    {"action": "metamorphosis", "line_no": 1, "target_list": None,
                        "status": "guaranteed", "reason": "no condition", "text": "metamorphosis"},
                    {"action": "void_ray", "line_no": 2, "target_list": None, "status": "possible",
                        "reason": "depends on runtime-only state", "text": "void_ray"},
                    {"action": "collapsing_star", "line_no": 3, "target_list": None,
                        "status": "guaranteed", "reason": "no condition", "text": "collapsing_star"},
                ],
                "inactive_talent_branches": [
                    {"action": "the_hunt", "line_no": 7, "target_list": None, "status": "dead",
                        "reason": "talent.the_hunt.enabled is false", "text": "the_hunt"}
                ],
                "explained_intent": {"setup": ["setup"], "helpers": [], "burst": ["burst"], "priorities": ["priority"]},
                "runtime_sensitive": [{"action": "void_ray", "line_no": 2, "target_list": None, "status": "possible", "reason": "depends on runtime-only state", "text": "void_ray"}],
            }
        return {
            "targets": context.targets,
            "focus_list": "aoe",
            "dispatch_certainty": "guaranteed",
            "branch_summary": {"start_list": start_list, "guaranteed_dispatch": "aoe", "guaranteed_dispatch_line": 8, "guaranteed_dispatch_reason": "active_enemies>1", "dead_branches": [], "unresolved_branches": [], "shadowed_lines": []},
            "active_priority": [
                {"action": "metamorphosis", "line_no": 1, "target_list": None,
                    "status": "guaranteed", "reason": "no condition", "text": "metamorphosis"},
                {"action": "soul_immolation", "line_no": 5, "target_list": None,
                    "status": "guaranteed", "reason": "no condition", "text": "soul_immolation"},
                {"action": "collapsing_star", "line_no": 6, "target_list": None,
                    "status": "guaranteed", "reason": "no condition", "text": "collapsing_star"},
            ],
            "inactive_talent_branches": [
                {"action": "devourers_bite", "line_no": 9, "target_list": None, "status": "dead",
                    "reason": "talent.devourers_bite.enabled is false", "text": "devourers_bite"}
            ],
            "explained_intent": {"setup": ["setup"], "helpers": [], "burst": ["burst"], "priorities": ["priority"]},
            "runtime_sensitive": [],
        }

    monkeypatch.setattr("simc_cli.main._describe_target_payload", _describe_target_payload)

    result = runner.invoke(simc_app, ["describe-build", "--apl-path", str(apl_path), "--build-text", "ABC123", "--aoe-targets", "5"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["kind"] == "describe_build"
    assert payload["data"]["identity"]["source"] == "simc_probe"
    assert payload["data"]["build"]["talents_by_tree"]["spec"]["selected"][0]["token"] == "void_ray"
    assert payload["data"]["build"]["talents_by_tree"]["spec"]["skipped"][0]["token"] == "midnight"
    assert payload["data"]["build"]["hero_tree"] == {"name": "Annihilator", "id": 124}
    assert [row["name"] for row in payload["data"]["build"]["inactive_hero_talents"]] == ["Void Reaver"]
    assert payload["data"]["single_target"]["focus_list"] == "melee_combo"
    assert payload["data"]["multi_target"]["focus_list"] == "aoe"
    assert payload["data"]["comparison"]["new_active_actions_in_aoe"] == ["soul_immolation"]
    assert payload["data"]["single_target"]["inactive_talent_branches"][0]["action"] == "the_hunt"


def test_simc_action_names_keep_the_dispatch_target() -> None:
    """Two call_action_list rows must not collapse to one name, or describe-build's

    single-target versus AoE comparison would report no difference when the build dispatches to a
    different action list at another target count.
    """
    names = simc_main._action_names(
        [
            {"action": "tiger_palm", "target_list": None},
            {"action": "call_action_list", "target_list": "default_st"},
            {"action": "call_action_list", "target_list": "multitarget"},
            {"action": None, "target_list": "ignored"},
        ]
    )
    assert names == ["tiger_palm", "call_action_list -> default_st", "call_action_list -> multitarget"]


def test_simc_describe_build_accepts_build_packet(monkeypatch, tmp_path: Path) -> None:
    apl_path = tmp_path / "druid_balance.simc"
    apl_path.write_text("actions=wrath\n")
    packet_path = tmp_path / "build-packet.json"
    packet_path.write_text('{"kind":"talent_transport_packet"}')

    def fake_loader(_paths, **kwargs):  # noqa: ANN001
        assert kwargs["build_packet"] == str(packet_path)
        return (
            BuildSpec(actor_class="druid",
                    spec="balance",
                    talents=None,
                    class_talents="103324:1",
                    spec_talents="109839:1",
                    hero_talents="117176:1",
                    source_kind="simc_split_talents",
                    source_notes=["talent transport packet"],
                    transport_form="simc_split_talents",
                    transport_status="validated",
                    transport_source=str(packet_path)),
            BuildIdentity(actor_class="druid",
                    spec="balance",
                    confidence="high",
                    source="warcraftlogs_talent_tree",
                    candidate_count=1,
                    candidates=[("druid", "balance")],
                    source_notes=["talent transport packet"]),
        )

    monkeypatch.setattr("simc_cli.main._load_identified_build_spec", fake_loader)

    resolution = _resolution(
            actor_class='druid',
            spec='balance',
            source_kind='simc_split_talents',
            enabled={'wrath'},
            source_notes=['talent transport packet', 'decoded via /tmp/simc'],
        )

    def fake_resolve_prune_context(_paths, _apl, option_values, targets):  # noqa: ANN001
        assert option_values["build_packet"] == str(packet_path)
        context = type("Context", (), {"targets": targets, "enabled_talents": {"wrath"}, "disabled_talents": set(), "talent_sources": {}})()
        return context, resolution

    monkeypatch.setattr("simc_cli.main._resolve_prune_context", fake_resolve_prune_context)
    monkeypatch.setattr(
        "simc_cli.main._describe_target_payload",
        lambda _resolved, context, *, start_list, priority_limit, inactive_limit: {
            "targets": context.targets,
            "focus_list": "default",
            "focus_path": ["default"],
            "focus_resolution": "direct",
            "active_priority": [],
            "inactive_priority": [],
            "active_action_names": ["wrath"],
            "inactive_action_names": [],
            "talent_tree": {"class": {"selected": [], "skipped": []}, "spec": {"selected": [], "skipped": []}, "hero": {"selected": [], "skipped": []}},
            "inactive_talents": [],
            "active_talents": [],
            "explained_intent": {"setup": [], "helpers": [], "burst": [], "priorities": []},
            "runtime_sensitive": [],
        },
    )

    result = runner.invoke(simc_app, ["describe-build", "--apl-path", str(apl_path), "--build-packet", str(packet_path)])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["build_spec"]["transport_packet"]["path"] == str(packet_path)
    assert payload["data"]["build_spec"]["transport_packet"]["transport_form"] == "simc_split_talents"


def test_simc_describe_build_uses_validated_split_packet_identity(monkeypatch, tmp_path: Path) -> None:
    apl_path = tmp_path / "shadow_priest.simc"
    apl_path.write_text("actions=mind_blast\n")
    packet_path = tmp_path / "build-packet.json"
    packet_path.write_text(
        json.dumps(
            {
                "kind": "talent_transport_packet",
                "transport_status": "validated",
                "build_identity": {
                    "class_spec_identity": {
                        "identity": {"actor_class": "priest", "spec": "shadow"},
                    }
                },
                "transport_forms": {
                    "simc_split_talents": {
                        "class_talents": "103324:1",
                        "spec_talents": "109839:1",
                    }
                },
                "raw_evidence": {"talent_tree_entries": [{"entry": 103324, "node_id": 82244, "rank": 1}]},
                "validation": {"status": "validated", "actor_class": "priest", "spec": "shadow"},
                "scope": {},
            }
        )
    )

    repo = RepoPaths(
        root=_checkout(tmp_path),
        apl_default=tmp_path,
        apl_assisted=tmp_path,
        class_modules=tmp_path,
        spell_dump=tmp_path,
        build_dir=tmp_path,
        build_simc=tmp_path / "simc",
    )
    monkeypatch.setattr("simc_cli.main._repo_paths", lambda _ctx: repo)

    resolution = _resolution(
            actor_class='priest',
            spec='shadow',
            source_kind='simc_split_talents',
            enabled={'mind_blast'},
            source_notes=['talent transport packet', 'decoded via /tmp/simc'],
        )

    def fake_resolve_prune_context(_paths, _apl, option_values, targets):  # noqa: ANN001
        assert option_values["build_packet"] == str(packet_path)
        context = type("Context", (), {"targets": targets, "enabled_talents": {
                       "mind_blast"}, "disabled_talents": set(), "talent_sources": {}})()
        return context, resolution

    monkeypatch.setattr("simc_cli.main._resolve_prune_context", fake_resolve_prune_context)
    monkeypatch.setattr(
        "simc_cli.main._describe_target_payload",
        lambda _resolved, context, *, start_list, priority_limit, inactive_limit: {
            "targets": context.targets,
            "focus_list": "default",
            "focus_path": ["default"],
            "focus_resolution": "direct",
            "active_priority": [],
            "inactive_priority": [],
            "active_action_names": ["mind_blast"],
            "inactive_action_names": [],
            "talent_tree": {"class": {"selected": [], "skipped": []}, "spec": {"selected": [], "skipped": []}, "hero": {"selected": [], "skipped": []}},
            "inactive_talents": [],
            "active_talents": [],
            "explained_intent": {"setup": [], "helpers": [], "burst": [], "priorities": []},
            "runtime_sensitive": [],
        },
    )

    result = runner.invoke(simc_app, ["describe-build", "--apl-path", str(apl_path), "--build-packet", str(packet_path)])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["identity"]["source"] == "simc_split_talents"
    assert payload["data"]["build_spec"]["actor_class"] == "priest"
    assert payload["data"]["build_spec"]["spec"] == "shadow"


def test_simc_describe_build_accepts_wow_export_transport_form_from_build_packet(monkeypatch, tmp_path: Path) -> None:
    apl_path = tmp_path / "druid_balance.simc"
    apl_path.write_text("actions=wrath\n")
    packet_path = tmp_path / "exact-packet.json"
    packet_path.write_text(
        json.dumps(
            {
                "kind": "talent_transport_packet",
                "transport_status": "exact",
                "build_identity": {
                    "class_spec_identity": {
                        "identity": {"actor_class": "druid", "spec": "balance"},
                    }
                },
                "transport_forms": {"wow_talent_export": "ABC123"},
                "raw_evidence": {"reference_type": "wow_talent_export"},
                "validation": {},
                "scope": {},
            }
        )
    )

    repo = RepoPaths(
        root=tmp_path,
        apl_default=tmp_path,
        apl_assisted=tmp_path,
        class_modules=tmp_path,
        spell_dump=tmp_path,
        build_dir=tmp_path,
        build_simc=tmp_path / "simc",
    )
    monkeypatch.setattr("simc_cli.main._repo_paths", lambda _ctx: repo)
    monkeypatch.setattr(
        "simc_cli.build_input.specialization_ids",
        lambda _root: {("druid", "balance"): 102},
    )
    monkeypatch.setattr(
        "simc_cli.build_input.decode_build",
        lambda _repo, build_spec: _resolution(enabled={"wrath"}),
    )

    resolution = _resolution(
            actor_class='druid',
            spec='balance',
            source_kind='wow_talent_export',
            enabled={'wrath'},
            source_notes=['talent transport packet'],
        )

    def fake_resolve_prune_context(_paths, _apl, option_values, targets):  # noqa: ANN001
        assert option_values["build_packet"] == str(packet_path)
        context = type("Context", (), {"targets": targets, "enabled_talents": {"wrath"}, "disabled_talents": set(), "talent_sources": {}})()
        return context, resolution

    monkeypatch.setattr("simc_cli.main._resolve_prune_context", fake_resolve_prune_context)
    monkeypatch.setattr(
        "simc_cli.main._describe_target_payload",
        lambda _resolved, context, *, start_list, priority_limit, inactive_limit: {
            "targets": context.targets,
            "focus_list": "default",
            "focus_path": ["default"],
            "focus_resolution": "direct",
            "active_priority": [],
            "inactive_priority": [],
            "active_action_names": ["wrath"],
            "inactive_action_names": [],
            "talent_tree": {"class": {"selected": [], "skipped": []}, "spec": {"selected": [], "skipped": []}, "hero": {"selected": [], "skipped": []}},
            "inactive_talents": [],
            "active_talents": [],
            "explained_intent": {"setup": [], "helpers": [], "burst": [], "priorities": []},
            "runtime_sensitive": [],
        },
    )

    result = runner.invoke(simc_app, ["describe-build", "--apl-path", str(apl_path), "--build-packet", str(packet_path)])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["build_spec"]["source_kind"] == "wow_talent_export"
    assert payload["data"]["build_spec"]["talents"] == "ABC123"
    assert payload["data"]["build_spec"]["transport_packet"]["transport_form"] == "wow_talent_export"


def test_simc_describe_build_probes_wow_export_packet_instead_of_trusting_packet_identity(monkeypatch, tmp_path: Path) -> None:
    apl_path = tmp_path / "druid_balance.simc"
    apl_path.write_text("actions=wrath\n")
    packet_path = tmp_path / "exact-packet.json"
    packet_path.write_text(
        json.dumps(
            {
                "kind": "talent_transport_packet",
                "transport_status": "exact",
                "build_identity": {
                    "class_spec_identity": {
                        "identity": {"actor_class": "priest", "spec": "shadow"},
                    }
                },
                "transport_forms": {"wow_talent_export": "ABC123"},
                "raw_evidence": {"reference_type": "wow_talent_export"},
                "validation": {},
                "scope": {},
            }
        )
    )

    repo = RepoPaths(
        root=tmp_path,
        apl_default=tmp_path,
        apl_assisted=tmp_path,
        class_modules=tmp_path,
        spell_dump=tmp_path,
        build_dir=tmp_path,
        build_simc=tmp_path / "simc",
    )
    monkeypatch.setattr("simc_cli.main._repo_paths", lambda _ctx: repo)
    monkeypatch.setattr(
        "simc_cli.build_input.specialization_ids",
        lambda _root: {("druid", "balance"): 102, ("priest", "shadow"): 258},
    )
    monkeypatch.setattr(
        "simc_cli.build_input.decode_build",
        lambda _repo, build_spec: (
            (_ for _ in ()).throw(SimcBuildError("wrong spec", output_preview=[], returncode=1))
            if (build_spec.actor_class, build_spec.spec) == ("priest", "shadow")
            else _resolution(enabled={"moonkin_form"})
        ),
    )

    resolution = _resolution(
            actor_class='druid',
            spec='balance',
            source_kind='wow_talent_export',
            enabled={'wrath'},
            source_notes=['talent transport packet'],
        )

    def fake_resolve_prune_context(_paths, _apl, option_values, targets):  # noqa: ANN001
        assert option_values["build_packet"] == str(packet_path)
        context = type("Context", (), {"targets": targets, "enabled_talents": {"wrath"}, "disabled_talents": set(), "talent_sources": {}})()
        return context, resolution

    monkeypatch.setattr("simc_cli.main._resolve_prune_context", fake_resolve_prune_context)
    monkeypatch.setattr(
        "simc_cli.main._describe_target_payload",
        lambda _resolved, context, *, start_list, priority_limit, inactive_limit: {
            "targets": context.targets,
            "focus_list": "default",
            "focus_path": ["default"],
            "focus_resolution": "direct",
            "active_priority": [],
            "inactive_priority": [],
            "active_action_names": ["wrath"],
            "inactive_action_names": [],
            "talent_tree": {"class": {"selected": [], "skipped": []}, "spec": {"selected": [], "skipped": []}, "hero": {"selected": [], "skipped": []}},
            "inactive_talents": [],
            "active_talents": [],
            "explained_intent": {"setup": [], "helpers": [], "burst": [], "priorities": []},
            "runtime_sensitive": [],
        },
    )

    result = runner.invoke(simc_app, ["describe-build", "--apl-path", str(apl_path), "--build-packet", str(packet_path)])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["build_spec"]["actor_class"] == "druid"
    assert payload["data"]["build_spec"]["spec"] == "balance"
    assert payload["data"]["identity"]["source"] == "simc_probe"
    assert payload["data"]["build"]["actor_class"] == "druid"
    assert payload["data"]["build"]["spec"] == "balance"


def test_simc_describe_build_rejects_malformed_build_packet(tmp_path: Path) -> None:
    packet_path = tmp_path / "bad-packet.json"
    packet_path.write_text(
        json.dumps(
            {
                "kind": "talent_transport_packet",
                "transport_status": "validated",
                "build_identity": {},
                "transport_forms": {"wowhead_talent_calc_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123"},
                "raw_evidence": {"reference_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123"},
                "validation": {},
                "scope": {},
            }
        )
    )

    result = runner.invoke(simc_app, ["describe-build", "--build-packet", str(packet_path)])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_build_packet"
    assert "does not match packet contents" in payload["error"]["message"]


def test_simc_describe_build_rejects_raw_only_build_packet_without_transport_form(tmp_path: Path) -> None:
    packet_path = tmp_path / "raw-only-packet.json"
    packet_path.write_text(
        json.dumps(
            {
                "kind": "talent_transport_packet",
                "transport_status": "raw_only",
                "build_identity": {
                    "class_spec_identity": {
                        "identity": {"actor_class": "druid", "spec": "balance"},
                    }
                },
                "transport_forms": {},
                "raw_evidence": {
                    "talent_tree_entries": [{"entry": 103324, "node_id": 82244, "rank": 1}],
                },
                "validation": {"status": "not_validated"},
                "scope": {},
            }
        )
    )

    result = runner.invoke(simc_app, ["describe-build", "--build-packet", str(packet_path)])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_build_packet"
    assert "validate-talent-transport first" in payload["error"]["message"]


def test_simc_describe_build_rejects_buildless_wowhead_talent_calc_url(tmp_path: Path) -> None:
    apl_path = tmp_path / "druid_balance.simc"
    apl_path.write_text("actions=wrath\n")

    result = runner.invoke(
        simc_app,
        ["describe-build", "--apl-path", str(apl_path), "--build-text", "https://www.wowhead.com/talent-calc/druid/balance"],
    )
    assert result.exit_code == 2
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "unsupported_build_reference"
    assert payload["error"]["details"]["reference_type"] == "wowhead_talent_calc_url"
    assert "no build code" in payload["error"]["message"]


def test_simc_describe_build_uses_leaf_focus_and_full_action_diff(monkeypatch, tmp_path: Path) -> None:
    apl_path = tmp_path / "demonhunter_devourer.simc"
    apl_path.write_text(
        "\n".join(
            [
                "actions=call_action_list,name=cooldowns",
                "actions+=call_action_list,name=leaf",
                "actions.cooldowns=metamorphosis",
                "actions.leaf=void_ray",
                "actions.leaf+=collapsing_star,if=active_enemies>1",
            ]
        )
        + "\n"
    )

    monkeypatch.setattr(
        "simc_cli.main._load_identified_build_spec",
        lambda *args, **kwargs: (
            BuildSpec(actor_class="demonhunter",
                    spec="devourer",
                    talents="ABC123",
                    class_talents=None,
                    spec_talents=None,
                    hero_talents=None,
                    source_kind="wow_talent_export",
                    source_notes=["single-line talent export"]),
            BuildIdentity(actor_class="demonhunter",
                    spec="devourer",
                    confidence="high",
                    source="simc_probe",
                    candidate_count=1,
                    candidates=[("demonhunter", "devourer")],
                    source_notes=["single-line talent export", "identified by SimC probe"]),
        ),
    )

    resolution = _resolution(
            actor_class='demonhunter',
            spec='devourer',
            source_kind='wow_talent_export',
            enabled={'void_ray'},
            source_notes=['decoded via /tmp/simc'],
        )

    def _resolve_prune_context(_paths, _apl, _values, targets):
        context = type(
            "Context",
            (),
            {
                "targets": targets,
                "enabled_talents": {"void_ray"},
                "disabled_talents": set(),
                "talent_sources": {"void_ray": "spec"},
            },
        )()
        return context, resolution

    monkeypatch.setattr("simc_cli.main._resolve_prune_context", _resolve_prune_context)

    result = runner.invoke(
        simc_app,
        ["describe-build", "--apl-path", str(apl_path), "--build-text", "ABC123", "--priority-limit", "1", "--aoe-targets", "5"],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["single_target"]["focus_list"] == "leaf"
    assert payload["data"]["single_target"]["focus_path"] == ["default", "leaf"]
    assert payload["data"]["single_target"]["focus_resolution"] == "guaranteed_call_leaf"
    assert payload["data"]["comparison"]["new_active_actions_in_aoe"] == ["collapsing_star"]


def test_simc_decode_build_failure_includes_source_metadata(monkeypatch) -> None:
    monkeypatch.setattr(
        "simc_cli.main.load_build_spec",
        lambda **kwargs: BuildSpec(actor_class="demonhunter",
            spec="devourer",
            talents="CgcBG5bbocFKcv+yIq8fPd6ORBA2MmZmxMzMGzMAAAAAAAegxsNYGAAAAAAAAmxMMmZmZmZmZGzsYGjFtsxMzMzWbzMzAYYAIwMGMmB",
            class_talents=None,
            spec_talents=None,
            hero_talents=None,
            source_kind="wow_talent_export",
            source_notes=["single-line talent export", "inline build text"]),
    )

    def _raise_decode(_paths, _build_spec):
        raise RuntimeError("Nothing to sim!")

    monkeypatch.setattr("simc_cli.main.decode_build", _raise_decode)
    result = runner.invoke(
        simc_app,
        [
            "decode-build",
            "--actor-class",
            "demonhunter",
            "--spec",
            "devourer",
            "--build-text",
            "CgcBG5bbocFKcv+yIq8fPd6ORBA2MmZmxMzMGzMAAAAAAAegxsNYGAAAAAAAAmxMMmZmZmZmZGzsYGjFtsxMzMzWbzMzAYYAIwMGMmB",
        ],
    )
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "decode_failed"
    details = payload["error"]["details"]
    assert details["build_spec"]["source_kind"] == "wow_talent_export"
    assert 'demonhunter="simc_decode"' in details["generated_profile"]


def test_simc_decode_build_reports_a_rejected_hash_instead_of_the_truncated_build(tmp_path: Path) -> None:
    """SimC prints the free spec grants before rejecting a hash; those two talents are not a build."""
    rejected = (FIXTURES / "captured_paladin_retribution_hash_error_debug.txt").read_text()
    option_dump = "World of Warcraft Raid Simulator Options:\n" + "".join(f"option_{i}=0\n" for i in range(700))
    repo_root = _checkout(tmp_path)

    def fake_run(cmd, **_kwargs):  # noqa: ANN001, ANN003
        return subprocess.CompletedProcess(cmd, 81, stdout=option_dump + rejected, stderr="")

    with patch("simc_cli.build_input.subprocess.run", side_effect=fake_run):
        result = runner.invoke(
            simc_app,
            ["--repo-root", str(repo_root), "decode-build", "--actor-class", "paladin", "--spec", "retribution",
             "--talents", "CYEAAA"],
        )

    assert result.exit_code == 1
    assert result.stdout == ""
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_build"
    assert "Node 81527 is not a choice node" in payload["error"]["message"]
    assert "World of Warcraft Raid Simulator Options" not in payload["error"]["message"]
    assert len(payload["error"]["message"]) < 500
    assert payload["error"]["details"]["simc_returncode"] == 81
    assert len(payload["error"]["details"]["simc_output_preview"]) == 20


CHECKOUT_HEAD = "0908ace08c9b22638fcdac80a710bc7b6928d3d8"


def _decode_build_against_a_binary_built_from(monkeypatch, tmp_path: Path, revision: str) -> dict[str, Any]:
    """Reject a hash with a binary whose build revision is `revision`, and return the error envelope.

    ``simc_cli.run`` and ``simc_cli.build_input`` share the ``subprocess`` module, so one dispatching
    fake answers the decode, the binary's version banner and git.
    """
    rejected = (FIXTURES / "captured_paladin_retribution_hash_error_debug.txt").read_text()
    repo_root = _checkout(tmp_path)
    (repo_root / ".git").mkdir()

    def fake_run(cmd, **_kwargs):  # noqa: ANN001, ANN003
        argv = [str(part) for part in cmd]
        if argv[0] == "git":
            return subprocess.CompletedProcess(cmd, 0, stdout=CHECKOUT_HEAD, stderr="")
        if any(part.startswith("spell_query=") for part in argv):
            banner = f"SimulationCraft 1210-01 for WoW 12.1.0 Live (git build midnight {revision})"
            return subprocess.CompletedProcess(cmd, 0, stdout=banner, stderr="")
        return subprocess.CompletedProcess(cmd, 81, stdout=rejected, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = runner.invoke(
        simc_app,
        ["--repo-root", str(repo_root), "decode-build", "--actor-class", "paladin", "--spec", "retribution",
         "--talents", "CYEAAA"],
    )
    assert result.exit_code == 1
    return json.loads(result.stderr)


def test_simc_decode_build_names_a_stale_binary_when_simc_rejects_the_hash(monkeypatch, tmp_path: Path) -> None:
    """A binary older than its checkout rejects valid hashes; the envelope has to point at it."""
    payload = _decode_build_against_a_binary_built_from(monkeypatch, tmp_path, "3377576e3b")

    assert payload["error"]["details"]["simc_binary"] == {
        "git_revision": "3377576e3b",
        "checkout_head": CHECKOUT_HEAD,
        "matches_checkout": False,
    }
    assert "built from 3377576e3b" in payload["error"]["message"]
    assert "simc build" in payload["error"]["message"]


def test_simc_decode_build_error_stays_quiet_about_a_binary_that_matches_the_checkout(monkeypatch, tmp_path: Path) -> None:
    """The rebuild hint is only true for a stale binary; a matching one must not be blamed."""
    payload = _decode_build_against_a_binary_built_from(monkeypatch, tmp_path, CHECKOUT_HEAD[:10])

    assert payload["error"]["details"]["simc_binary"]["matches_checkout"] is True
    assert "simc build" not in payload["error"]["message"]


def test_simc_decode_build_rejects_an_apl_path_that_does_not_exist(tmp_path: Path) -> None:
    """The class and spec come from the file stem, so a missing file invents a confident identity."""
    result = runner.invoke(
        simc_app, ["decode-build", "--apl-path", str(tmp_path / "warlock_nonsensespec.simc")]
    )

    assert result.exit_code == 4
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "not_found"
    assert "warlock_nonsensespec.simc" in payload["error"]["message"]


def test_simc_identify_build_from_an_apl_file_name_is_not_high_confidence(tmp_path: Path) -> None:
    """A class and spec read off a file stem is a guess, not a verified identity."""
    repo_root = _checkout(tmp_path)
    apl = repo_root / "ActionPriorityLists" / "default" / "mage_arcane.simc"
    apl.write_text("actions=arcane_blast\n")

    result = runner.invoke(simc_app, ["--repo-root", str(repo_root), "identify-build", "--apl-path", str(apl)])

    assert result.exit_code == 0
    identity = json.loads(result.stdout)["data"]["identity"]
    assert (identity["source"], identity["actor_class"], identity["spec"]) == ("apl_path", "mage", "arcane")
    assert identity["confidence"] == "medium"


def test_simc_decode_build_payload_separates_the_inactive_hero_tree_and_unreadable_ranks(tmp_path: Path) -> None:
    repo_root = _checkout(tmp_path)

    with patch(
        "simc_cli.build_input.subprocess.run",
        side_effect=lambda cmd, **_k: subprocess.CompletedProcess(cmd, 0, stdout=CAPTURED_ARCANE_MAGE, stderr=""),
    ):
        result = runner.invoke(
            simc_app,
            ["--repo-root", str(repo_root), "decode-build", "--actor-class", "mage", "--spec", "arcane",
             "--talents", "C4DAAA"],
        )

    assert result.exit_code == 0
    decoded = json.loads(result.stdout)["data"]["decoded"]
    assert decoded["hero_tree"] == {"name": "Sunfury", "id": 39}
    assert [row["name"] for row in decoded["inactive_hero_talents"]] == ["Splintering Sorcery"]
    assert "splintering_sorcery" not in decoded["enabled_talents"]
    assert "prismatic_bolt" in decoded["enabled_talents"]
    tiered = next(row for row in decoded["talents_by_tree"]["spec"] if row["name"] == "Prismatic Bolt")
    assert (tiered["rank"], tiered["rank_known"]) == (None, False)


def _fake_build_spec(*, actor_class="druid", spec="balance", talents="ABC123"):  # noqa: ANN001
    return BuildSpec(actor_class=actor_class,
        spec=spec,
        talents=talents,
        class_talents=None,
        spec_talents=None,
        hero_talents=None,
        source_kind="wow_talent_export",
        source_notes=["command-line build options"])


def _fake_identity(*, actor_class="druid", spec="balance"):  # noqa: ANN001
    return BuildIdentity(actor_class=actor_class,
        spec=spec,
        confidence="high",
        source="direct",
        candidate_count=1,
        candidates=[(actor_class, spec)],
        source_notes=["command-line build options"])


def _fake_resolution(
    *,
    actor_class="druid",
    spec="balance",
    class_talents=None,
    spec_talents=None,
    hero_talents=None,
):  # noqa: ANN001
    return _resolution(
        actor_class=actor_class,
        spec=spec,
        talents_by_tree={
            "class": class_talents or [
                _talent("class", "Thick Hide", 100),
                _talent("class", "Innervate", 200),
            ],
            "spec": spec_talents or [_talent("spec", "Starlord", 300, rank=2, max_rank=2)],
            "hero": hero_talents or [_talent("hero", "Dream Surge", 400)],
            "selection": [],
        },
    )


# --- compare-builds ---


def test_simc_compare_builds_shows_tree_diffs(monkeypatch) -> None:
    from simc_cli.build_input import DecodedTalent

    def _t(tree, name, entry, rank=1, max_rank=1):  # noqa: ANN001
        token = name.lower().replace(" ", "_")
        return DecodedTalent(tree=tree, name=name, token=token, rank=rank, max_rank=max_rank, entry=entry)

    base_res = _fake_resolution(
        class_talents=[_t("class", "Thick Hide", 100), _t("class", "Innervate", 200)],
    )
    other_res = _fake_resolution(
        class_talents=[_t("class", "Thick Hide", 100), _t("class", "Forestwalk", 300, rank=2, max_rank=2)],
    )

    call_count = {"n": 0}

    def fake_decode(paths, build_spec):  # noqa: ANN001
        call_count["n"] += 1
        return base_res if call_count["n"] == 1 else other_res

    monkeypatch.setattr(
        "simc_cli.main._load_identified_build_spec",
        lambda *a, **kw: (_fake_build_spec(), _fake_identity()),
    )
    monkeypatch.setattr("simc_cli.main.decode_build", fake_decode)

    result = runner.invoke(simc_app, [
        "compare-builds", "--base", "ABC123", "--other", "DEF456", "--tree", "class",
    ])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["kind"] == "compare_builds"
    assert payload["data"]["base"]["actor_class"] == "druid"
    assert payload["data"]["trees_compared"] == ["class"]
    comp = payload["data"]["comparisons"][0]
    assert comp["has_differences"] is True
    class_diff = comp["trees"]["class"]
    assert len(class_diff["added"]) == 1
    assert class_diff["added"][0]["name"] == "Forestwalk"
    assert len(class_diff["removed"]) == 1
    assert class_diff["removed"][0]["name"] == "Innervate"


def test_simc_compare_builds_reports_no_differences(monkeypatch) -> None:
    res = _fake_resolution()

    monkeypatch.setattr(
        "simc_cli.main._load_identified_build_spec",
        lambda *a, **kw: (_fake_build_spec(), _fake_identity()),
    )
    monkeypatch.setattr("simc_cli.main.decode_build", lambda paths, spec: res)

    result = runner.invoke(simc_app, ["compare-builds", "--base", "ABC", "--other", "ABC"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["comparisons"][0]["has_differences"] is False


def test_simc_compare_builds_multiple_others(monkeypatch) -> None:
    from simc_cli.build_input import DecodedTalent

    def _t(tree, name, entry, rank=1, max_rank=1):  # noqa: ANN001
        token = name.lower().replace(" ", "_")
        return DecodedTalent(tree=tree, name=name, token=token, rank=rank, max_rank=max_rank, entry=entry)

    base_res = _fake_resolution(class_talents=[_t("class", "Thick Hide", 100)])
    other_a = _fake_resolution(class_talents=[_t("class", "Thick Hide", 100)])
    other_b = _fake_resolution(class_talents=[_t("class", "Forestwalk", 300)])

    decode_results = iter([base_res, other_a, other_b])

    monkeypatch.setattr(
        "simc_cli.main._load_identified_build_spec",
        lambda *a, **kw: (_fake_build_spec(), _fake_identity()),
    )
    monkeypatch.setattr("simc_cli.main.decode_build", lambda paths, spec: next(decode_results))

    result = runner.invoke(simc_app, [
        "compare-builds", "--base", "A", "--other", "B", "--other", "C", "--tree", "class",
    ])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert len(payload["data"]["comparisons"]) == 2
    assert payload["data"]["comparisons"][0]["has_differences"] is False
    assert payload["data"]["comparisons"][1]["has_differences"] is True


def _decode_failing_on(bad: str) -> Any:
    """A decode stub: the base and every other build decode, except the ones whose talents are ``bad``."""

    def fake_decode(paths, spec):  # noqa: ANN001
        if spec.talents == bad:
            raise RuntimeError("bad build")
        return _fake_resolution()

    return fake_decode


def test_simc_compare_builds_fails_when_no_other_build_decodes(monkeypatch) -> None:
    """A comparison with nothing to compare against is a failure, not an empty success."""
    monkeypatch.setattr("simc_cli.main.decode_build", _decode_failing_on("BAD"))

    result = runner.invoke(
        simc_app, ["compare-builds", "--base", "A", "--other", "BAD", "--actor-class", "druid", "--spec", "balance"]
    )

    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "decode_failed"
    assert "bad build" in payload["error"]["message"]
    assert payload["error"]["details"]["comparisons"] == [{"input": "BAD", "error": "bad build"}]


def test_simc_compare_builds_counts_the_other_builds_that_failed(monkeypatch) -> None:
    monkeypatch.setattr("simc_cli.main.decode_build", _decode_failing_on("BAD"))

    result = runner.invoke(
        simc_app,
        ["compare-builds", "--base", "A", "--other", "A", "--other", "BAD", "--actor-class", "druid", "--spec", "balance"],
    )

    assert result.exit_code == 0
    data = json.loads(result.stdout)["data"]
    assert data["summary"] == {"succeeded": 1, "failed": 1}
    assert data["comparisons"][1] == {"input": "BAD", "error": "bad build"}


def test_simc_compare_builds_rejects_an_unknown_tree(monkeypatch) -> None:
    """An unknown tree used to compare nothing and report no differences."""
    monkeypatch.setattr("simc_cli.main.decode_build", lambda paths, spec: _fake_resolution())

    result = runner.invoke(
        simc_app,
        ["compare-builds", "--base", "A", "--other", "B", "--tree", "heroo", "--actor-class", "druid", "--spec", "balance"],
    )

    assert result.exit_code == 2
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_argument"
    assert "heroo" in payload["error"]["message"]


@pytest.mark.parametrize(
    ("flag", "args"),
    [("--base", ["--base", "", "--other", "B"]), ("--other", ["--base", "A", "--other", "B", "--other", " "])],
)
def test_simc_compare_builds_names_the_option_given_an_empty_build(monkeypatch, flag: str, args: list[str]) -> None:
    monkeypatch.setattr("simc_cli.main.decode_build", lambda paths, spec: _fake_resolution())

    result = runner.invoke(simc_app, ["compare-builds", *args, "--actor-class", "druid", "--spec", "balance"])

    assert result.exit_code == 2
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_query"
    assert payload["error"]["message"] == f"{flag} was given an empty value."


def test_simc_compare_builds_rejects_buildless_wowhead_talent_calc_url() -> None:
    result = runner.invoke(
        simc_app,
        [
            "compare-builds",
            "--base",
            "https://www.wowhead.com/talent-calc/druid/balance",
            "--other",
            "DEF456",
        ],
    )
    assert result.exit_code == 2
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "unsupported_build_reference"
    assert payload["error"]["details"]["reference_type"] == "wowhead_talent_calc_url"
    assert "no build code" in payload["error"]["message"]


def test_simc_compare_builds_rejects_buildless_wowhead_other(monkeypatch) -> None:
    """An --other that is no build is a usage error, even when the base decodes."""
    monkeypatch.setattr("simc_cli.main.decode_build", lambda paths, spec: _fake_resolution())

    result = runner.invoke(
        simc_app,
        [
            "compare-builds",
            "--actor-class",
            "druid",
            "--spec",
            "balance",
            "--base",
            "BASE",
            "--other",
            "https://www.wowhead.com/talent-calc/druid/balance",
        ],
    )
    assert result.exit_code == 2
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "unsupported_build_reference"
    assert "no build code" in payload["error"]["message"]


# --- modify-build ---
#
# These drive the real edit pipeline (tree routing, encoding, re-decode, verification) and stub only
# the SimC binary, answering it with captured `debug=1` output for an Arcane Mage.


def _modify(tmp_path: Path, fake: _FakeSimcBinary, *args: str) -> tuple[int, dict[str, Any]]:
    repo_root = _checkout(tmp_path)
    with patch("simc_cli.build_input.subprocess.run", side_effect=fake):
        result = runner.invoke(
            simc_app,
            ["--repo-root", str(repo_root), "modify-build", "--talents", "BASE",
             "--actor-class", "mage", "--spec", "arcane", *args],
        )
    return result.exit_code, json.loads(result.stdout or result.stderr)


def test_simc_modify_build_routes_a_spec_talent_name_into_the_spec_option(tmp_path: Path) -> None:
    """SimC resolves talent names per tree, so a spec talent sent as `class_talents` is rejected."""
    fake = _FakeSimcBinary({"BASE": CAPTURED_ARCANE_MAGE, "MODIFIED_EXPORT": _captured_without("Arcane Tempo")})

    exit_code, payload = _modify(tmp_path, fake, "--remove", "Arcane Tempo")

    assert exit_code == 0
    assert "spec_talents=arcane_tempo:0" in fake.encode_profile
    assert "class_talents=" not in fake.encode_profile
    assert [row["name"] for row in payload["data"]["result"]["diff_from_base"]["spec"]["removed"]] == ["Arcane Tempo"]
    assert payload["data"]["result"]["verified"] is True


def test_simc_modify_build_adds_a_talent_the_base_build_does_not_have_by_name(tmp_path: Path) -> None:
    """`--add` by name has to work for a talent absent from the build; only trait data knows its tree."""
    added = CAPTURED_ARCANE_MAGE + "0.000 Player 'simc_decode' adding spec talent Presence of Mind (node=1 entry=126530 rank=1/1)\n"
    fake = _FakeSimcBinary({"BASE": CAPTURED_ARCANE_MAGE, "MODIFIED_EXPORT": added})

    exit_code, payload = _modify(tmp_path, fake, "--add", "presence_of_mind:1")

    assert exit_code == 0
    assert "spec_talents=presence_of_mind:1" in fake.encode_profile
    assert [row["name"] for row in payload["data"]["result"]["diff_from_base"]["spec"]["added"]] == ["Presence of Mind"]


def test_simc_modify_build_discloses_hero_talents_the_reencode_grants_outside_the_selected_tree(tmp_path: Path) -> None:
    """SimC regrants every hero keystone when it regenerates a hash; the payload has to say so.

    Splinterstorm belongs to Spellslinger (sub tree 40) while this build activated Sunfury (39), so
    the export carries a talent the input hash did not and the sim will never use.
    """
    phantom = CAPTURED_ARCANE_MAGE + "0.000 Player 'simc_decode' adding hero talent Splinterstorm (node=94657 entry=117257 rank=1/1)\n"
    fake = _FakeSimcBinary({"BASE": CAPTURED_ARCANE_MAGE, "MODIFIED_EXPORT": phantom})

    exit_code, payload = _modify(tmp_path, fake, "--add", "137084:1")

    assert exit_code == 0
    result = payload["data"]["result"]
    assert [tree for tree in ("class", "spec", "hero") if result["diff_from_base"][tree]["has_differences"]] == []
    assert [row["name"] for row in result["diff_from_base"]["inactive_hero"]["added"]] == ["Splinterstorm"]
    assert result["disclosures"] != []


def test_simc_modify_build_reports_nothing_to_disclose_when_the_export_matches_the_base(tmp_path: Path) -> None:
    fake = _FakeSimcBinary({"BASE": CAPTURED_ARCANE_MAGE, "MODIFIED_EXPORT": CAPTURED_ARCANE_MAGE})

    exit_code, payload = _modify(tmp_path, fake, "--add", "137084:1")

    assert exit_code == 0
    diff = payload["data"]["result"]["diff_from_base"]
    assert [tree for tree, rows in diff.items() if rows["has_differences"]] == []
    assert payload["data"]["result"]["disclosures"] == []


def test_simc_modify_build_refuses_to_emit_an_export_carrying_unrequested_changes(tmp_path: Path) -> None:
    """SimC re-serializes the whole build; an active-tree change nobody asked for is never shipped."""
    collateral = _captured_without("Arcane Tempo", "Ice Cold")
    fake = _FakeSimcBinary({"BASE": CAPTURED_ARCANE_MAGE, "MODIFIED_EXPORT": collateral})

    exit_code, payload = _modify(tmp_path, fake, "--remove", "Arcane Tempo")

    assert exit_code == 1
    assert payload["error"]["code"] == "encode_mismatch"
    assert [
        (row["tree"], row["change"], row["name"]) for row in payload["error"]["details"]["unrequested_changes"]
    ] == [("class", "removed", "Ice Cold")]
    assert "MODIFIED_EXPORT" not in json.dumps(payload)


class _TieredReadBackSimcBinary(_FakeSimcBinary):
    """Also answers the ``log=1`` read-back run with the ranks SimC spread over Prismatic Bolt's entries."""

    def __call__(self, cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if "log=1" in cmd:
            self.profiles.append(Path(str(cmd[1])).read_text())
            log = "\n".join(
                f"0.000 Overwriting talent Prismatic Bolt ({entry}), rank {rank} -> 0"
                for entry, rank in ((137028, 1), (137027, 2), (137026, 1))
            )
            return subprocess.CompletedProcess(cmd, 0, stdout=log, stderr="")
        return super().__call__(cmd, **kwargs)


def test_simc_modify_build_removes_every_entry_of_a_tiered_talent_by_name(tmp_path: Path) -> None:
    """A tiered node decodes as one row per entry, all sharing the talent's name.

    Removing it by name removes all of them; only matching the one entry the name resolved to reported
    the node's other entries as unrequested changes and refused the export.
    """
    fake = _TieredReadBackSimcBinary({"BASE": CAPTURED_ARCANE_MAGE, "MODIFIED_EXPORT": _captured_without("Prismatic Bolt")})

    exit_code, payload = _modify(tmp_path, fake, "--remove", "Prismatic Bolt")

    assert exit_code == 0, payload
    removed = payload["data"]["result"]["diff_from_base"]["spec"]["removed"]
    assert [(row["entry"], row["rank"]) for row in removed] == [(137026, 1), (137027, 2), (137028, 1)]
    assert "spec_talents=prismatic_bolt:0" in fake.encode_profile


def test_simc_modify_build_name_edit_does_not_cover_a_same_named_talent_in_another_tree() -> None:
    """A name edit covers its own tiered node, not a talent that happens to share the name elsewhere."""
    from simc_cli.main import _TalentEdit, _unrequested_changes

    def diff(*removed: int) -> dict[str, Any]:
        rows = [{"name": "Arcane Tempo", "token": "arcane_tempo", "rank": 1, "max_rank": 1, "entry": entry} for entry in removed]
        return {"added": [], "removed": rows, "changed": [], "has_differences": bool(rows)}

    edits = [_TalentEdit(tree="spec", value="arcane_tempo", rank=0, entry=1)]

    unrequested = _unrequested_changes({"class": diff(9), "spec": diff(1, 2), "hero": diff()}, edits)

    assert [(row["tree"], row["entry"]) for row in unrequested] == [("class", 9)]


def test_simc_modify_build_rejects_a_talent_name_that_belongs_to_no_tree(tmp_path: Path) -> None:
    fake = _FakeSimcBinary({"BASE": CAPTURED_ARCANE_MAGE})

    exit_code, payload = _modify(tmp_path, fake, "--remove", "nonexistent_talent")

    assert exit_code == 2
    assert payload["error"]["code"] == "unknown_talent"


def test_simc_modify_build_rejects_an_entry_id_the_checkout_does_not_know(tmp_path: Path) -> None:
    fake = _FakeSimcBinary({"BASE": CAPTURED_ARCANE_MAGE})

    exit_code, payload = _modify(tmp_path, fake, "--add", "999999:1")

    assert exit_code == 2
    assert payload["error"]["code"] == "unknown_talent"


@pytest.mark.parametrize(
    "edit",
    [
        ["--remove", "Blazing Barrier"],  # a mage class-tree talent only Fire can take
        ["--add", "80178:1"],  # the same talent by entry id
        ["--add", "96172:1"],  # Blinding Sleet, a Death Knight class talent
    ],
)
def test_simc_modify_build_rejects_a_talent_this_spec_cannot_take(tmp_path: Path, edit: list[str]) -> None:
    """SimC rejects such an edit as if the build were invalid; it is the caller's edit that names no talent."""
    fake = _FakeSimcBinary({"BASE": CAPTURED_ARCANE_MAGE})

    exit_code, payload = _modify(tmp_path, fake, *edit)

    assert exit_code == 2
    assert payload["error"]["code"] == "unknown_talent"
    assert not any("save=" in text for text in fake.profiles), "the edit reached the encoder"


def test_simc_hero_talents_follow_the_specs_their_hero_tree_is_offered_to() -> None:
    """A hero entry is takeable by the specs its hero tree's selection rows name, not by its own spec tags.

    That is SimC's trait_data_t::is_hero_trait_available. Augmentation's Chronowarden entries carry only
    Preservation's tag, and applying the tags rejected every Augmentation hero edit as `unknown_talent`.
    Ignoring the restriction altogether let Augmentation add Flameshaper's Consume Flame and Arcane add
    Frostfire's Isothermic Core, both trees their specs cannot select.
    """
    table = parse_trait_table(CAPTURED_TRAIT_DATA)
    evoker, augmentation, preservation = CLASS_ID_BY_ACTOR_CLASS["evoker"], 1473, 1468
    mage, arcane, frost = CLASS_ID_BY_ACTOR_CLASS["mage"], 62, 64

    assert table.tree_for_entry(117522, class_id=evoker, spec_id=augmentation) == "hero"
    assert table.tree_for_name("Chronoboon", class_id=evoker, spec_id=augmentation) == "hero"
    assert table.tree_for_name("Consume Flame", class_id=evoker, spec_id=preservation) == "hero"
    assert table.tree_for_name("Consume Flame", class_id=evoker, spec_id=augmentation) is None
    assert table.tree_for_name("Isothermic Core", class_id=mage, spec_id=frost) == "hero"
    assert table.tree_for_name("Isothermic Core", class_id=mage, spec_id=arcane) is None


def test_simc_encode_runs_simc_the_way_a_healer_build_needs(tmp_path: Path) -> None:
    """Without `debug=1` SimC silences a healer it will not simulate (Mistweaver, Holy Paladin) and then
    saves no profile ("No active players in sim!"); `allow_experimental_specializations` makes it build
    Holy Priest's stale default APL, which fails on divine_star. Either way a valid healer build came
    back as `invalid_build`.
    """
    fake = _FakeSimcBinary({"BASE": CAPTURED_ARCANE_MAGE, "MODIFIED_EXPORT": _captured_without("Arcane Tempo")})

    exit_code, _payload = _modify(tmp_path, fake, "--remove", "Arcane Tempo")

    assert exit_code == 0
    assert "debug=1" in fake.encode_args
    assert not [arg for arg in fake.encode_args if arg.startswith("allow_experimental_specializations")]


def test_simc_modify_build_fails_on_bad_add_format(tmp_path: Path) -> None:
    fake = _FakeSimcBinary({"BASE": CAPTURED_ARCANE_MAGE})

    exit_code, payload = _modify(tmp_path, fake, "--add", "no_rank")

    assert exit_code == 2
    assert payload["error"]["code"] == "invalid_argument"


def test_simc_modify_build_fails_without_modifications(tmp_path: Path) -> None:
    fake = _FakeSimcBinary({"BASE": CAPTURED_ARCANE_MAGE})

    exit_code, payload = _modify(tmp_path, fake)

    assert exit_code == 1
    assert payload["error"]["code"] == "no_modifications"


def test_simc_modify_build_swap_class_tree_rebuilds_from_split_trees(tmp_path: Path) -> None:
    """A tree swap drops the base hash, so every tree must be written out as entry:rank strings."""
    swapped = _captured_without("Ice Cold")
    fake = _FakeSimcBinary({"BASE": CAPTURED_ARCANE_MAGE, "REF": swapped, "MODIFIED_EXPORT": swapped})
    repo_root = _checkout(tmp_path)

    with patch("simc_cli.build_input.subprocess.run", side_effect=fake):
        result = runner.invoke(
            simc_app,
            ["--repo-root", str(repo_root), "modify-build", "--talents", "BASE",
             "--actor-class", "mage", "--spec", "arcane", "--swap-class-tree-from", "REF"],
        )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["modifications"] == ["swap_class_tree"]
    assert "talents=BASE" not in fake.encode_profile
    assert "class_talents=" in fake.encode_profile and "spec_talents=" in fake.encode_profile
    assert [row["name"] for row in payload["data"]["result"]["diff_from_base"]["class"]["removed"]] == ["Ice Cold"]


def test_simc_modify_build_refuses_a_swap_whose_export_dropped_a_tiered_talent(tmp_path: Path) -> None:
    """A swapped tree is verified against the build it came from, not against the base.

    SimC prints a tiered node's leftover rank (always 0), so re-serializing a decoded tree as
    `entry:rank` pairs loses it. Here Prismatic Bolt is in the swap source only: measured against the
    base the swapped tree looks untouched, which is how the loss used to ship as a verified export.
    """
    without_tiered = _captured_without("Prismatic Bolt")
    fake = _FakeSimcBinary({"BASE": without_tiered, "REF": CAPTURED_ARCANE_MAGE, "MODIFIED_EXPORT": without_tiered})
    repo_root = _checkout(tmp_path)

    with patch("simc_cli.build_input.subprocess.run", side_effect=fake):
        result = runner.invoke(
            simc_app,
            ["--repo-root", str(repo_root), "modify-build", "--talents", "BASE",
             "--actor-class", "mage", "--spec", "arcane", "--swap-spec-tree-from", "REF"],
        )

    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "encode_mismatch"
    assert [
        (row["tree"], row["change"], row["name"]) for row in payload["error"]["details"]["unrequested_changes"]
    ] == [("spec", "removed", "Prismatic Bolt")]
    assert "MODIFIED_EXPORT" not in json.dumps(payload)


def test_simc_modify_build_swap_leaves_out_the_hero_tree_of_a_build_that_selected_none(tmp_path: Path) -> None:
    """Spelling out the freely granted keystones of such a build makes SimC select a hero tree.

    SimC then disables the other keystone, so every tree swap on a build with no hero tree selected
    (SimC's own default talents, for one) failed with `encode_mismatch`.
    """
    no_hero_tree = "\n".join(line for line in CAPTURED_ARCANE_MAGE.splitlines() if "activating sub tree" not in line)
    fake = _FakeSimcBinary({"BASE": no_hero_tree, "MODIFIED_EXPORT": no_hero_tree})

    with patch("simc_cli.build_input.subprocess.run", side_effect=fake):
        result = runner.invoke(
            simc_app,
            ["--repo-root", str(_checkout(tmp_path)), "modify-build", "--talents", "BASE",
             "--actor-class", "mage", "--spec", "arcane", "--swap-spec-tree-from", "BASE"],
        )

    assert result.exit_code == 0, result.stdout + result.stderr
    assert "spec_talents=" in fake.encode_profile
    assert "hero_talents=" not in fake.encode_profile


def test_simc_modify_build_aborts_when_swap_tree_decode_fails(tmp_path: Path) -> None:
    """A failing --swap-*-tree-from source must abort before anything is encoded or emitted."""
    rejected = "0.000 Player 'p' generic base statsError: Initialization error: Player 'p': Hash 'REF': bad node.\n"
    fake = _FakeSimcBinary({"BASE": CAPTURED_ARCANE_MAGE, "REF": rejected})
    repo_root = _checkout(tmp_path)

    with patch("simc_cli.build_input.subprocess.run", side_effect=fake):
        result = runner.invoke(
            simc_app,
            ["--repo-root", str(repo_root), "modify-build", "--talents", "BASE",
             "--actor-class", "mage", "--spec", "arcane", "--swap-class-tree-from", "REF",
             "--add", "137084:1"],
        )

    assert result.exit_code == 1
    assert result.stdout.strip() == ""
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_build"
    assert "Failed to decode class tree source" in payload["error"]["message"]
    assert "bad node" in payload["error"]["message"]


def test_simc_modify_build_rejects_buildless_wowhead_talent_calc_url() -> None:
    result = runner.invoke(
        simc_app,
        [
            "modify-build",
            "--talents",
            "https://www.wowhead.com/talent-calc/druid/balance",
            "--remove",
            "innervate",
        ],
    )
    assert result.exit_code == 2
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "unsupported_build_reference"
    assert payload["error"]["details"]["reference_type"] == "wowhead_talent_calc_url"
    assert "no build code" in payload["error"]["message"]


def test_simc_modify_build_rejects_buildless_wowhead_swap_source(monkeypatch) -> None:
    """The swap source goes through the same reference parsing as the base build."""
    monkeypatch.setattr("simc_cli.main.decode_build", lambda paths, spec: _fake_resolution())

    result = runner.invoke(
        simc_app,
        [
            "modify-build",
            "--actor-class",
            "druid",
            "--spec",
            "balance",
            "--talents",
            "BASE",
            "--swap-class-tree-from",
            "https://www.wowhead.com/talent-calc/druid/balance",
        ],
    )
    assert result.exit_code == 2
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "unsupported_build_reference"
    assert payload["error"]["details"]["reference_type"] == "wowhead_talent_calc_url"
    assert "no build code" in payload["error"]["message"]


def test_simc_modify_build_reports_the_simc_error_when_encoding_fails(tmp_path: Path) -> None:
    class _FailingEncoder(_FakeSimcBinary):
        def __call__(self, cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            if "save=" in Path(str(cmd[1])).read_text():
                self.profiles.append(Path(str(cmd[1])).read_text())
                return subprocess.CompletedProcess(
                    cmd, 1, stdout="banner\n" * 900,
                    stderr="Error: Generating profiles: Player 'simc_decode': Invalid 'spec_talents'.\n",
                )
            return super().__call__(cmd, **kwargs)

    exit_code, payload = _modify(tmp_path, _FailingEncoder({"BASE": CAPTURED_ARCANE_MAGE}), "--remove", "Arcane Tempo")

    assert exit_code == 1
    assert payload["error"]["code"] == "invalid_build"
    assert "Invalid 'spec_talents'" in payload["error"]["message"]
    assert "banner" not in payload["error"]["message"]


def test_simc_build_harness_compare_report_and_verify_clean(monkeypatch, tmp_path: Path) -> None:
    harness_path = tmp_path / "demo_harness.simc"

    monkeypatch.setattr(
        "simc_cli.main._load_identified_build_spec_or_fail",
        lambda *args, **kwargs: (
            BuildSpec(actor_class="warlock",
                    spec="demonology",
                    talents="ABC123",
                    class_talents=None,
                    spec_talents=None,
                    hero_talents=None,
                    source_notes=["command-line build options"]),
            BuildIdentity(actor_class="warlock",
                    spec="demonology",
                    confidence="high",
                    source="direct",
                    candidate_count=1,
                    candidates=[("warlock", "demonology")],
                    source_notes=["command-line build options"]),
        ),
    )
    build_result = runner.invoke(
        simc_app,
        ["build-harness", "--actor-class", "warlock", "--spec", "demonology", "--talents",
            "ABC123", "--out", str(harness_path), "--line", "hero_talents=2"],
    )
    assert build_result.exit_code == 0
    build_payload = json.loads(build_result.stdout)
    assert build_payload["kind"] == "build_harness"
    assert build_payload["data"]["path"] == str(harness_path)
    assert harness_path.exists()

    apl = tmp_path / "variant.simc"
    apl.write_text("actions=shadow_bolt\n")
    profile = tmp_path / "variant_profile.simc"
    profile.write_text("warlock=\"probe\"\nactions=shadow_bolt\n")
    monkeypatch.setattr("simc_cli.main.build_variant_profile", lambda harness_path, apl_path, label, out_dir=None: profile)
    monkeypatch.setattr(
        "simc_cli.main.validate_profile_file",
        lambda paths, profile_path: type(
            "Validation",
            (),
            {
                "result": type("Result", (), {"returncode": 0, "stdout": "ok\n", "stderr": ""})(),
            },
        )(),
    )
    validate_result = runner.invoke(simc_app, ["validate-apl", str(harness_path), str(apl), "--label", "wowhead"])
    assert validate_result.exit_code == 0
    validate_payload = json.loads(validate_result.stdout)
    assert validate_payload["data"]["valid"] is True
    assert validate_payload["data"]["label"] == "wowhead"

    # Only the SimC run is stubbed, so the ranking, deltas and sampling disclosure are computed for real.
    dps_by_label = {"base": 100.0, "wowhead": 99.0}

    def fake_simulate(paths, *, label, apl_path, profile_path, iterations, threads, out_dir):  # noqa: ANN001, ANN003
        return simc_compare.VariantSummary(
            label=label, apl_path=apl_path, profile_path=profile_path, json_path=out_dir / f"{label}.json",
            dps=dps_by_label[label], dps_error=1.0, fight_length=60.0,
            action_counts={"shadow_bolt": 10}, action_cpm={"shadow_bolt": 10.0},
        )

    monkeypatch.setattr("simc_cli.compare._simulate_variant", fake_simulate)
    compare_result = runner.invoke(
        simc_app,
        ["compare-apls", str(harness_path), "--base-apl", str(apl), "--variant",
         f"wowhead={apl}", "--skip-validate", "--out-dir", str(tmp_path / "compare"),
         "--report-out", str(tmp_path / "report.json")],
    )
    assert compare_result.exit_code == 0
    compare_envelope = json.loads(compare_result.stdout)
    assert compare_envelope["kind"] == "apl_comparison"
    compare_stdout = compare_envelope["data"]
    assert compare_stdout["report_path"] == str((tmp_path / "report.json").resolve())
    assert [row["label"] for row in compare_stdout["ranking"]] == ["base", "wowhead"]
    assert compare_stdout["comparisons"][0]["dps_delta"] == -1.0
    assert compare_stdout["sampling"]["action_sequence_iterations"] == 1

    report_result = runner.invoke(simc_app, ["variant-report", str(tmp_path / "report.json")])
    assert report_result.exit_code == 0
    report_envelope = json.loads(report_result.stdout)
    assert report_envelope["kind"] == "apl_variant_report"
    report_payload = report_envelope["data"]
    assert report_payload["best_label"] == "base"
    assert {row["label"]: row["delta_vs_base"] for row in report_payload["ranking"]} == {"base": 0.0, "wowhead": -1.0}

    monkeypatch.setattr(
        "simc_cli.main.verify_clean_payload",
        lambda paths, hash_binary: {"kind": "verify_clean", "repo_root": "/tmp/simc", "git": {"dirty": False}, "binary": {"exists": True}},
    )
    clean_result = runner.invoke(simc_app, ["verify-clean"])
    assert clean_result.exit_code == 0
    clean_payload = json.loads(clean_result.stdout)
    assert clean_payload["kind"] == "verify_clean"
    assert clean_payload["data"]["git"]["dirty"] is False


def test_simc_build_harness_rejects_buildless_wowhead_talent_calc_url() -> None:
    result = runner.invoke(
        simc_app,
        [
            "build-harness",
            "--actor-class",
            "druid",
            "--spec",
            "balance",
            "--talents",
            "https://www.wowhead.com/talent-calc/druid/balance",
        ],
    )
    assert result.exit_code == 2
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "unsupported_build_reference"
    assert payload["error"]["details"]["reference_type"] == "wowhead_talent_calc_url"
    assert "no build code" in payload["error"]["message"]


def test_simc_apl_lists_graph_talents_and_trace(monkeypatch, tmp_path: Path) -> None:
    apl = tmp_path / "monk_mistweaver.simc"
    apl.write_text(
        "\n".join(
            [
                "actions=auto_attack",
                "actions+=/run_action_list,name=aoe,if=active_enemies>2",
                "actions+=/rising_sun_kick,if=talent.rising_mist.enabled",
                "actions.aoe=spinning_crane_kick",
            ]
        )
        + "\n"
    )

    result_lists = runner.invoke(simc_app, ["apl-lists", str(apl)])
    assert result_lists.exit_code == 0
    payload_lists = json.loads(result_lists.stdout)
    assert payload_lists["data"]["apl"]["list_count"] == 2
    assert payload_lists["data"]["lists"][0]["count"] >= 1

    result_graph = runner.invoke(simc_app, ["apl-graph", str(apl)])
    assert result_graph.exit_code == 0
    payload_graph = json.loads(result_graph.stdout)
    assert payload_graph["data"]["graph"]["format"] == "mermaid"
    assert "flowchart TD" in payload_graph["data"]["graph"]["text"]

    result_talents = runner.invoke(simc_app, ["apl-talents", str(apl)])
    assert result_talents.exit_code == 0
    payload_talents = json.loads(result_talents.stdout)
    assert payload_talents["data"]["count"] == 1
    assert payload_talents["data"]["talents"][0]["token"] == "rising_mist"

    monkeypatch.setattr(
        "simc_cli.main.find_action",
        lambda paths, action, wow_class: {"apl_default": [], "apl_assisted": [], "class_modules": [], "spell_dump": []},
    )
    result_trace = runner.invoke(simc_app, ["trace-action", str(apl), "rising_sun_kick"])
    assert result_trace.exit_code == 0
    payload_trace = json.loads(result_trace.stdout)
    assert payload_trace["data"]["apl_hits"]["count"] == 1
    assert payload_trace["data"]["apl_hits"]["items"][0]["list_name"] == "default"


def test_simc_find_action_groups_hits(monkeypatch, tmp_path: Path) -> None:
    repo_root = _checkout(tmp_path)
    monkeypatch.setattr(
        "simc_cli.main.find_action",
        lambda paths, action, wow_class: {
            "apl_default": [],
            "apl_assisted": [],
            "class_modules": [type("Hit", (), {"path": Path("/tmp/sc_monk.cpp"), "line_no": 42, "text": "if ( action == rising_sun_kick )"})()],
            "spell_dump": [],
        },
    )
    result = runner.invoke(simc_app, ["--repo-root", str(repo_root), "find-action", "rising_sun_kick"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["count"] == 1
    assert payload["data"]["buckets"]["class_modules"]["items"][0]["line_no"] == 42


@pytest.mark.parametrize(("args", "command"), [(["find-action", "mind_blast"], "find-action"), (["spec-files", "monk"], "spec-files")])
def test_simc_content_search_names_ripgrep_when_it_is_not_installed(
    monkeypatch, tmp_path: Path, args: list[str], command: str
) -> None:
    """Without ripgrep the commands used to die with a leaked FileNotFoundError and internal_error."""
    repo_root = _checkout(tmp_path)
    monkeypatch.setattr("simc_cli.search.shutil.which", lambda _name: None)

    result = runner.invoke(simc_app, ["--repo-root", str(repo_root), *args])

    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["command"] == command
    assert payload["error"]["code"] == "missing_dependency"
    assert "ripgrep" in payload["error"]["message"]


def test_simc_find_action_sends_ripgrep_a_pattern_that_matches_the_query(monkeypatch, tmp_path: Path) -> None:
    """The pattern has to match the text the query came from.

    Escaping alone was not enough: the old ``\\b{query}\\b`` cannot match a query ending in
    punctuation, because no word boundary exists after ``)``. ripgrep's ``\\b`` is Python's, so the
    compiled pattern is checked here against text with and without a surrounding word.
    """
    repo_root = _checkout(tmp_path)
    patterns: list[str] = []

    def fake_run(cmd, **_kwargs):  # noqa: ANN001, ANN003
        argv = [str(part) for part in cmd]
        patterns.append(argv[argv.index("--no-heading") + 1])
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")

    monkeypatch.setattr("simc_cli.search.shutil.which", lambda _name: "/usr/bin/rg")
    monkeypatch.setattr("simc_cli.search.subprocess.run", fake_run)

    result = runner.invoke(simc_app, ["--repo-root", str(repo_root), "find-action", "cast(x)+"])

    assert result.exit_code == 0
    assert patterns
    for pattern in patterns:
        assert re.search(pattern, "actions+=/cast(x)+,if=1") is not None
        assert re.search(pattern, "precast(x)+,if=1") is None


@pytest.mark.parametrize(
    ("query", "text", "matches"),
    [
        ("mind_blast", "actions+=/mind_blast,if=1", True),
        ("mind_blast", "actions+=/mind_blaster", False),
        ("cast(x)", "a cast(x) b", True),
        ("+mind_blast", "buff+mind_blast,if=1", True),
    ],
)
def test_word_bounded_pattern_anchors_only_where_a_boundary_can_exist(query: str, text: str, matches: bool) -> None:
    assert (re.search(word_bounded_pattern(query), text) is not None) is matches


def test_simc_apl_prune_branch_trace_and_intent(monkeypatch, tmp_path: Path) -> None:
    apl = tmp_path / "evoker_devastation.simc"
    apl.write_text(
        "\n".join(
            [
                "actions+=/run_action_list,name=aoe,if=active_enemies>=3",
                "actions+=/run_action_list,name=st,if=active_enemies<3",
                "actions.aoe+=/fire_breath",
                "actions.st+=/disintegrate,if=talent.mass_disintegrate",
            ]
        )
        + "\n"
    )
    monkeypatch.setattr(
        "simc_cli.main._resolve_prune_context",
        lambda paths, apl_path, option_values, targets: (
            type("Context", (), {"enabled_talents": {"mass_disintegrate"}, "disabled_talents": set(),
                 "targets": targets, "talent_sources": {"mass_disintegrate": "spec"}})(),
            _resolution(actor_class="evoker", spec="devastation"),
        ),
    )

    prune_result = runner.invoke(simc_app, ["apl-prune", str(apl), "--targets", "3"])
    assert prune_result.exit_code == 0
    prune_payload = json.loads(prune_result.stdout)
    assert prune_payload["data"]["lists"][0]["items"][0]["state"] == "eligible"

    trace_result = runner.invoke(simc_app, ["apl-branch-trace", str(apl), "--targets", "3"])
    assert trace_result.exit_code == 0
    trace_payload = json.loads(trace_result.stdout)
    assert trace_payload["data"]["summary"]["guaranteed_dispatch"] == "aoe"
    assert trace_payload["data"]["trace"][0]["text"] == "[default]"

    intent_result = runner.invoke(simc_app, ["apl-intent", str(apl), "--targets", "1"])
    assert intent_result.exit_code == 0
    intent_payload = json.loads(intent_result.stdout)
    assert intent_payload["data"]["focus_list"] == "st"
    assert intent_payload["data"]["intent"]


def test_simc_priority_inactive_actions_and_opener(monkeypatch, tmp_path: Path) -> None:
    apl = tmp_path / "demonhunter_devourer.simc"
    apl.write_text(
        "\n".join(
            [
                "actions+=/run_action_list,name=aoe,if=active_enemies>=5",
                "actions+=/reapers_toll",
                "actions.aoe+=/void_ray,if=talent.void_ray",
                "actions.aoe+=/collapsing_star,if=talent.collapsing_star",
                "actions.aoe+=/reapers_toll",
                "actions.aoe+=/predators_wake",
            ]
        )
        + "\n"
    )
    monkeypatch.setattr(
        "simc_cli.main._resolve_prune_context",
        lambda paths, apl_path, option_values, targets: (
            type(
                "Context",
                (),
                {
                    "enabled_talents": {"void_ray", "predators_wake"},
                    "disabled_talents": set(),
                    "targets": targets,
                    "talent_sources": {"void_ray": "spec", "predators_wake": "spec"},
                },
            )(),
            _resolution(actor_class="demonhunter", spec="devourer"),
        ),
    )

    priority_result = runner.invoke(simc_app, ["priority", str(apl), "--targets", "5"])
    assert priority_result.exit_code == 0
    priority_payload = json.loads(priority_result.stdout)
    assert priority_payload["data"]["priority"]["focus_list"] == "aoe"
    assert [row["action"] for row in priority_payload["data"]["priority"]["items"][:2]] == ["void_ray", "reapers_toll"]
    assert priority_payload["data"]["priority"]["inactive_talent_branches"][0]["action"] == "collapsing_star"

    inactive_result = runner.invoke(simc_app, ["inactive-actions", str(apl), "--targets", "5"])
    assert inactive_result.exit_code == 0
    inactive_payload = json.loads(inactive_result.stdout)
    assert inactive_payload["data"]["inactive_actions"]["count"] == 1
    assert inactive_payload["data"]["inactive_actions"]["items"][0]["action"] == "collapsing_star"

    opener_result = runner.invoke(simc_app, ["opener", str(apl), "--targets", "5", "--limit", "3"])
    assert opener_result.exit_code == 0
    opener_payload = json.loads(opener_result.stdout)
    assert opener_payload["data"]["opener"]["kind"] == "static_priority_preview"
    assert opener_payload["data"]["opener"]["items"][0]["action"] == "void_ray"
    assert "static exact-build opener preview" in opener_payload["data"]["opener"]["caveat"]


def test_simc_intent_explain_branch_compare_and_analysis_packet(monkeypatch, tmp_path: Path) -> None:
    apl = tmp_path / "evoker_devastation.simc"
    apl.write_text(
        "\n".join(
            [
                "actions+=/run_action_list,name=aoe,if=active_enemies>=3",
                "actions+=/run_action_list,name=st,if=active_enemies<3",
                "actions.aoe+=/fire_breath",
                "actions.st+=/disintegrate,if=talent.mass_disintegrate",
            ]
        )
        + "\n"
    )

    def fake_context(paths, apl_path, option_values, targets):  # noqa: ANN001
        return (
            type(
                "Context",
                (),
                {
                    "enabled_talents": {"mass_disintegrate"} if targets == 1 else set(),
                    "disabled_talents": set(),
                    "targets": targets,
                    "talent_sources": {"mass_disintegrate": "spec"},
                },
            )(),
            _resolution(actor_class="evoker", spec="devastation"),
        )

    monkeypatch.setattr("simc_cli.main._resolve_prune_context", fake_context)

    explain_result = runner.invoke(simc_app, ["apl-intent-explain", str(apl), "--targets", "1"])
    assert explain_result.exit_code == 0
    explain_payload = json.loads(explain_result.stdout)
    assert explain_payload["data"]["explained_intent"]["priorities"]

    compare_result = runner.invoke(simc_app, ["apl-branch-compare", str(apl), "--left-targets", "3", "--right-targets", "1"])
    assert compare_result.exit_code == 0
    compare_payload = json.loads(compare_result.stdout)
    assert compare_payload["data"]["comparison"]["dispatch_changed"] is True
    assert compare_payload["data"]["comparison"]["left_focus_intent"]

    packet_result = runner.invoke(simc_app, ["analysis-packet", str(apl), "--targets", "1"])
    assert packet_result.exit_code == 0
    packet_payload = json.loads(packet_result.stdout)
    assert packet_payload["data"]["packet"]["focus_list"] == "st"
    assert packet_payload["data"]["packet"]["explained_intent"]["priorities"]


def test_simc_first_cast_and_log_actions(monkeypatch, tmp_path: Path) -> None:
    profile = tmp_path / "example.simc"
    profile.write_text('monk="example"\n')
    log_path = tmp_path / "combat.log"
    log_path.write_text(
        "\n".join(
            [
                "0.100 schedules execute for Action 'rising_sun_kick'",
                "0.250 performs Action 'rising_sun_kick'",
            ]
        )
        + "\n"
    )

    monkeypatch.setattr(
        "simc_cli.main.run_first_casts",
        lambda paths, profile_path, action, seeds, max_time, targets, fight_style: [
            type("Result", (), {"seed": 1, "time": 0.2, "log_path": tmp_path / "seed_1.log"})(),
            type("Result", (), {"seed": 2, "time": 0.3, "log_path": tmp_path / "seed_2.log"})(),
        ],
    )
    monkeypatch.setattr(
        "simc_cli.main.summarize_first_casts",
        lambda results: {"samples": 2, "found": 2, "min": 0.2, "avg": 0.25, "max": 0.3},
    )

    first_cast_result = runner.invoke(simc_app, ["first-cast", str(profile), "rising_sun_kick"])
    assert first_cast_result.exit_code == 0
    first_cast_payload = json.loads(first_cast_result.stdout)
    assert first_cast_payload["data"]["summary"]["avg"] == 0.25
    assert first_cast_payload["data"]["results"][0]["seed"] == 1

    log_result = runner.invoke(simc_app, ["log-actions", str(log_path), "rising_sun_kick"])
    assert log_result.exit_code == 0
    log_payload = json.loads(log_result.stdout)
    assert log_payload["data"]["count"] == 1
    assert log_payload["data"]["hits"][0]["performed_at"] == 0.25

    directory_result = runner.invoke(simc_app, ["log-actions", str(tmp_path), "rising_sun_kick"])
    assert directory_result.exit_code == 4
    assert json.loads(directory_result.stderr)["error"]["code"] == "not_found"


def test_simc_analysis_packet_surfaces_runtime_timing_failures(monkeypatch, tmp_path: Path) -> None:
    apl = tmp_path / "evoker_devastation.simc"
    apl.write_text("actions.st+=/disintegrate\n")

    monkeypatch.setattr(
        "simc_cli.main._resolve_prune_context",
        lambda paths, apl_path, option_values, targets: (
            type("Context", (), {"enabled_talents": set(), "disabled_talents": set(), "targets": targets, "talent_sources": {}})(),
            _resolution(actor_class="evoker", spec="devastation"),
        ),
    )
    monkeypatch.setattr("simc_cli.main.build_analysis_packet", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("timing failed")))

    result = runner.invoke(
        simc_app,
        [
            "analysis-packet",
            str(apl),
            "--targets",
            "1",
            "--sim-profile",
            str(tmp_path / "profile.simc"),
            "--first-cast-action",
            "disintegrate",
        ],
    )
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "analysis_packet_failed"
    assert payload["error"]["message"] == "timing failed"


def test_simc_sync_skips_dirty_repo(monkeypatch, tmp_path: Path) -> None:
    repo_root = tmp_path / "simc"
    repo_root.mkdir()
    build_dir = repo_root / "build"
    monkeypatch.setattr(
        "simc_cli.main._repo_paths",
        lambda ctx: RepoPaths(
            root=repo_root,
            apl_default=repo_root / "ActionPriorityLists" / "default",
            apl_assisted=repo_root / "ActionPriorityLists" / "assisted_combat",
            class_modules=repo_root / "engine" / "class_modules",
            spell_dump=repo_root / "SpellDataDump",
            build_dir=build_dir,
            build_simc=build_dir / "simc",
        ),
    )
    monkeypatch.setattr(
        "simc_cli.main.repo_git_status",
        lambda paths: {
            "git": True,
            "dirty": True,
            "branch": "main",
            "head": "abc",
            "dirty_entries": [" M engine/file.cpp"],
        },
    )
    monkeypatch.setattr("simc_cli.main.sync_repo", lambda paths, allow_dirty: None)
    result = runner.invoke(simc_app, ["sync"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["status"] == "skipped"
    assert payload["data"]["reason"] == "dirty_worktree"


def test_simc_build_surfaces_success(monkeypatch, tmp_path: Path) -> None:
    repo_root = tmp_path / "simc"
    build_dir = repo_root / "build"
    build_dir.mkdir(parents=True)
    monkeypatch.setattr(
        "simc_cli.main._repo_paths",
        lambda ctx: RepoPaths(
            root=repo_root,
            apl_default=repo_root / "ActionPriorityLists" / "default",
            apl_assisted=repo_root / "ActionPriorityLists" / "assisted_combat",
            class_modules=repo_root / "engine" / "class_modules",
            spell_dump=repo_root / "SpellDataDump",
            build_dir=build_dir,
            build_simc=build_dir / "simc",
        ),
    )
    monkeypatch.setattr(
        "simc_cli.main.build_repo",
        lambda paths, target: type(
            "Result",
            (),
            {
                "command": ["cmake", "--build", str(paths.build_dir)],
                "returncode": 0,
                "stdout": "Built target simc\n",
                "stderr": "",
            },
        )(),
    )
    result = runner.invoke(simc_app, ["build"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["status"] == "built"


def test_simc_run_surfaces_failure_with_preview(monkeypatch, tmp_path: Path) -> None:
    profile = tmp_path / "example.simc"
    profile.write_text('monk="example"\n')
    monkeypatch.setattr(
        "simc_cli.main.run_profile",
        lambda paths, profile_path, simc_args: type("Result", (), {"command": [str(paths.build_simc), str(
            profile_path)], "returncode": 1, "stdout": "", "stderr": "bad profile\n"})(),
    )
    monkeypatch.setattr(
        "simc_cli.main.binary_version",
        lambda paths: type("VersionInfo", (), {"binary_path": paths.build_simc, "available": True,
                           "version_line": "SimulationCraft 1201", "returncode": 1})(),
    )
    result = runner.invoke(simc_app, ["run", str(profile)])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "run_failed"


def test_simc_sim_uses_quick_preset_and_surfaces_run_metadata(monkeypatch, tmp_path: Path) -> None:
    profile = tmp_path / "example.simc"
    profile.write_text('paladin="example"\n')

    def _run(paths, profile_path, simc_args):
        args = {item.split("=", 1)[0]: item.split("=", 1)[1] for item in simc_args if "=" in item}
        json_path = Path(args["json2"])
        json_path.write_text(
            json.dumps(
                {
                    "version": "SimulationCraft 1201-01",
                    "sim": {
                        "options": {
                            "iterations": 1000,
                            "target_error": 0,
                            "threads": 12,
                            "fight_style": "Patchwerk",
                            "desired_targets": 1,
                            "max_time": 300,
                            "vary_combat_length": 0.2,
                            "seed": 12345,
                            "dbc": {
                                "version_used": "Live",
                                "Live": {"wow_version": "12.0.1.66263"},
                            },
                        },
                        "statistics": {
                            "elapsed_time_seconds": 4.33,
                            "elapsed_cpu_seconds": 100.4,
                            "init_time_seconds": 0.12,
                            "merge_time_seconds": 0.03,
                            "analyze_time_seconds": 0.01,
                            "simulation_length": {"count": 1003},
                        },
                        "players": [
                            {
                                "name": "example",
                                "specialization": "protection",
                                "role": "tank",
                                "collected_data": {
                                    "fight_length": {"mean": 299.37, "count": 1003},
                                    "dps": {"mean": 18834.4},
                                    "dpse": {"mean": 37.2},
                                    "dtps": {"mean": 75769.2},
                                    "hps": {"mean": 2210.9},
                                    "deaths": {"mean": 0.0},
                                    "absorb": {"mean": 795348.3},
                                    "heal": {"mean": 661816.8},
                                },
                            }
                        ],
                    },
                }
            )
        )
        return type(
            "Result",
            (),
            {"command": [str(paths.build_simc), str(profile_path), *simc_args], "returncode": 0, "stdout": "ok\n", "stderr": ""},
        )()

    monkeypatch.setattr("simc_cli.main.run_profile", _run)
    result = runner.invoke(simc_app, ["sim", str(profile)])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["preset"] == "quick"
    assert payload["data"]["input_source"] == "file"
    assert payload["data"]["run_settings"]["iterations_requested"] == 1000
    assert payload["data"]["run_settings"]["iterations_completed"] == 1003
    assert payload["data"]["run_settings"]["stop_reason"] == "fixed_iterations_completed"
    assert payload["data"]["runtime"]["elapsed_time_seconds"] == 4.33
    assert payload["data"]["metrics"]["dps"] == 18834.4
    assert payload["data"]["metrics"]["dtps"] == 75769.2
    assert payload["data"]["metrics"]["fight_length"] == 299.37
    assert payload["data"]["simc_version"] == "SimulationCraft 1201-01"
    assert payload["data"]["game_version"] == "12.0.1.66263"
    assert payload["data"]["json_report_path"] is None


def test_simc_sim_reads_stdin_and_respects_overrides(monkeypatch, tmp_path: Path) -> None:
    def _run(paths, profile_path, simc_args):
        args = {item.split("=", 1)[0]: item.split("=", 1)[1] for item in simc_args if "=" in item}
        json_path = Path(args["json2"])
        json_path.write_text(
            json.dumps(
                {
                    "version": "SimulationCraft 1201-01",
                    "sim": {
                        "options": {
                            "iterations": 6000,
                            "target_error": 0,
                            "threads": 4,
                            "fight_style": "HecticAddCleave",
                            "desired_targets": 5,
                            "max_time": 180,
                            "vary_combat_length": 0.1,
                            "seed": 222,
                            "dbc": {"version_used": "Live", "Live": {"wow_version": "12.0.1.66263"}},
                        },
                        "statistics": {"elapsed_time_seconds": 8.6, "simulation_length": {"count": 6003}},
                        "players": [
                            {
                                "name": "stdin-example",
                                "specialization": "protection",
                                "role": "tank",
                                "collected_data": {
                                    "fight_length": {"mean": 180.0, "count": 6003},
                                    "dps": {"mean": 21000.0},
                                    "dpse": {"mean": 50.0},
                                    "dtps": {"mean": 80000.0},
                                    "hps": {"mean": 2500.0},
                                    "deaths": {"mean": 0.0},
                                    "absorb": {"mean": 1.0},
                                    "heal": {"mean": 2.0},
                                },
                            }
                        ],
                    },
                }
            )
        )
        return type(
            "Result",
            (),
            {"command": [str(paths.build_simc), str(profile_path), *simc_args], "returncode": 0, "stdout": "", "stderr": ""},
        )()

    monkeypatch.setattr("simc_cli.main.run_profile", _run)
    result = runner.invoke(
        simc_app,
        ["sim", "-", "--preset", "high-accuracy", "--iterations", "6000", "--max-time", "180", "--fight-style",
            "HecticAddCleave", "--targets", "5", "--threads", "4", "--vary-combat-length", "0.1"],
        input='paladin="stdin-example"\n',
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["preset"] == "high-accuracy"
    assert payload["data"]["input_source"] == "stdin"
    assert payload["data"]["profile_path"] is None
    assert payload["data"]["run_settings"]["iterations_requested"] == 6000
    assert payload["data"]["run_settings"]["threads"] == 4
    assert payload["data"]["run_settings"]["fight_style"] == "HecticAddCleave"
    assert payload["data"]["run_settings"]["desired_targets"] == 5
    assert payload["data"]["run_settings"]["max_time"] == 180


def test_simc_version_reads_explicit_repo_binary(tmp_path: Path) -> None:
    binary = tmp_path / "build" / "simc"
    binary.parent.mkdir()
    binary.write_text("#!/bin/sh\necho 'SimulationCraft 1201-test'\n")
    binary.chmod(0o755)

    result = runner.invoke(simc_app, ["--repo-root", str(tmp_path), "version"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert str(payload["data"]["version"]).startswith("SimulationCraft")


def test_simc_run_turns_uncaught_exception_into_error_envelope(monkeypatch, capsys) -> None:
    def explode(ctx):  # noqa: ANN001
        raise RuntimeError("boom")

    monkeypatch.setattr("simc_cli.main._repo_paths", explode)
    monkeypatch.setattr("sys.argv", ["simc", "spec-files", "monk"])
    with pytest.raises(SystemExit) as raised:
        simc_main.run()
    assert raised.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    payload = json.loads(captured.err)
    assert payload["ok"] is False
    assert payload["provider"] == "simc"
    assert payload["command"] == "spec-files"
    assert payload["error"]["code"] == "internal_error"
    assert "boom" in payload["error"]["message"]


def _describe_with_disable(repo_root: Path, *disable: str) -> tuple[int, dict[str, Any]]:
    fake = _FakeSimcBinary({"ARCANE_EXPORT": CAPTURED_ARCANE_MAGE})
    args = ["--repo-root", str(repo_root), "describe-build",
            "--actor-class", "mage", "--spec", "arcane", "--talents", "ARCANE_EXPORT"]
    for value in disable:
        args += ["--disable", value]
    with patch("simc_cli.build_input.subprocess.run", side_effect=fake):
        result = runner.invoke(simc_app, args)
    return result.exit_code, json.loads(result.stdout or result.stderr)


def _prismatic_states(payload: dict[str, Any]) -> list[str]:
    return payload["data"]["single_target"]["active_action_names"]


def test_simc_describe_build_disables_a_talent_named_the_way_the_help_says(tmp_path: Path) -> None:
    """--disable advertises talent names, but only the SimC token used to match anything."""
    repo_root = _checkout(tmp_path)
    (repo_root / "ActionPriorityLists" / "default" / "mage_arcane.simc").write_text(
        "actions=arcane_blast\nactions+=/arcane_barrage,if=talent.prismatic_bolt\n"
    )

    baseline_code, baseline = _describe_with_disable(repo_root)
    display_code, display_name = _describe_with_disable(repo_root, "Prismatic Bolt")
    token_code, token = _describe_with_disable(repo_root, "prismatic_bolt")

    assert (baseline_code, display_code, token_code) == (0, 0, 0)
    assert _prismatic_states(display_name) == _prismatic_states(token)
    assert _prismatic_states(display_name) != _prismatic_states(baseline)


def test_simc_describe_build_rejects_a_disable_value_that_names_no_talent(tmp_path: Path) -> None:
    """An unresolvable value used to be dropped in silence, so the answer ignored the flag."""
    repo_root = _checkout(tmp_path)
    (repo_root / "ActionPriorityLists" / "default" / "mage_arcane.simc").write_text("actions=arcane_blast\n")

    exit_code, payload = _describe_with_disable(repo_root, "Spear Hand Strike")

    assert exit_code == 2
    assert payload["error"]["code"] == "unknown_talent"
    assert payload["error"]["details"]["unknown_talents"] == ["Spear Hand Strike"]

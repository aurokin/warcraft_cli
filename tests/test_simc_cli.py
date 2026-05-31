from __future__ import annotations

import json
from pathlib import Path

from simc_cli.main import app as simc_app
from simc_cli.repo import RepoPaths
from typer.testing import CliRunner

runner = CliRunner()


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

    monkeypatch.setattr("simc_cli.main._repo_payload", fake_repo_payload)
    result = runner.invoke(simc_app, ["--repo-root", str(repo_root), "doctor"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["provider"] == "simc"
    assert payload["status"] == "ready"
    assert payload["capabilities"]["version"] == "ready"
    assert payload["capabilities"]["search"] == "coming_soon"
    assert payload["capabilities"]["repo"] == "ready"
    assert payload["capabilities"]["checkout"] == "ready"
    assert payload["capabilities"]["apl_lists"] == "ready"
    assert payload["capabilities"]["apl_prune"] == "ready"
    assert payload["capabilities"]["priority"] == "ready"
    assert payload["capabilities"]["inactive_actions"] == "ready"
    assert payload["capabilities"]["opener"] == "ready"
    assert payload["capabilities"]["analysis_packet"] == "ready"
    assert payload["capabilities"]["first_cast"] == "ready"
    assert payload["capabilities"]["log_actions"] == "ready"
    assert payload["capabilities"]["compare_builds"] == "ready"
    assert payload["capabilities"]["modify_build"] == "ready"
    assert payload["capabilities"]["validate_talent_transport"] == "ready"


def test_simc_search_is_structured_coming_soon() -> None:
    result = runner.invoke(simc_app, ["search", "mistweaver"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["coming_soon"] is True
    assert payload["count"] == 0


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
    assert payload["action"] == "set_root"
    assert payload["changed"] is True
    assert payload["resolution"]["source"] == "config"


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
    assert payload["resolution"]["root"] == str(managed.resolve())
    assert payload["resolution"]["source"] == "unset"
    assert payload["resolution"]["configured_root"] is None
    assert payload["resolution"]["managed_root"] == str(managed.resolve())
    assert payload["resolution"]["managed_exists"] is False


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
    assert payload["status"] == "cloned"
    assert payload["active_resolution"]["source"] == "managed"


def test_simc_version_uses_binary_probe(monkeypatch) -> None:
    monkeypatch.setattr(
        "simc_cli.main.binary_version",
        lambda paths: type("VersionInfo", (), {"binary_path": Path("/tmp/simc"), "available": True,
                           "version_line": "SimulationCraft 1201", "returncode": 1})(),
    )
    result = runner.invoke(simc_app, ["version"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["version"] == "SimulationCraft 1201"


def test_simc_spec_files_returns_grouped_results(monkeypatch) -> None:
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
    result = runner.invoke(simc_app, ["spec-files", "mistweaver"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["count"] == 1
    assert payload["categories"]["default_apl"]["items"][0]["stem"] == "monk_mistweaver"


def test_simc_decode_build_outputs_decoded_talents(monkeypatch) -> None:
    monkeypatch.setattr(
        "simc_cli.main.load_build_spec",
        lambda **kwargs: type("BuildSpec", (), {
            "actor_class": "monk",
            "spec": "mistweaver",
            "talents": "ABC123",
            "class_talents": None,
            "spec_talents": None,
            "hero_talents": None,
            "source_kind": "wow_talent_export",
            "source_notes": ["command-line build options"],
        })(),
    )
    monkeypatch.setattr(
        "simc_cli.main.decode_build",
        lambda paths, build_spec: type("Resolution", (), {
            "actor_class": "monk",
            "spec": "mistweaver",
            "enabled_talents": {"ancient_teachings", "jadefire_stomp"},
            "source_kind": "wow_talent_export",
            "generated_profile_text": 'monk="simc_decode"\nlevel=90\nrace=pandaren\nspec=mistweaver\ntalents=ABC123\n',
            "talents_by_tree": {
                "class": [],
                "spec": [type("Talent", (), {"name": "Ancient Teachings", "token": "ancient_teachings", "rank": 1, "max_rank": 1})()],
                "hero": [type("Talent", (), {"name": "Jadefire Stomp", "token": "jadefire_stomp", "rank": 1, "max_rank": 1})()],
                "selection": [],
            },
            "source_notes": ["decoded via /tmp/simc"],
        })(),
    )
    result = runner.invoke(simc_app, ["decode-build", "--actor-class", "monk", "--spec", "mistweaver", "--talents", "ABC123"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["build_spec"]["source_kind"] == "wow_talent_export"
    assert payload["decoded"]["actor_class"] == "monk"
    assert payload["decoded"]["source_kind"] == "wow_talent_export"
    assert 'talents=ABC123' in payload["decoded"]["generated_profile"]
    assert payload["decoded"]["enabled_talents"] == ["ancient_teachings", "jadefire_stomp"]


def test_simc_identify_build_reports_probe_result(monkeypatch) -> None:
    monkeypatch.setattr(
        "simc_cli.main._load_identified_build_spec",
        lambda *args, **kwargs: (
            type(
                "BuildSpec",
                (),
                {
                    "actor_class": "demonhunter",
                    "spec": "devourer",
                    "talents": "ABC123",
                    "class_talents": None,
                    "spec_talents": None,
                    "hero_talents": None,
                    "source_kind": "wow_talent_export",
                    "source_notes": ["single-line talent export", "identified by SimC probe"],
                },
            )(),
            type(
                "BuildIdentity",
                (),
                {
                    "actor_class": "demonhunter",
                    "spec": "devourer",
                    "confidence": "high",
                    "source": "simc_probe",
                    "candidate_count": 1,
                    "candidates": [("demonhunter", "devourer")],
                    "source_notes": ["single-line talent export", "identified by SimC probe"],
                },
            )(),
        ),
    )
    result = runner.invoke(simc_app, ["identify-build", "--build-text", "ABC123"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["kind"] == "identify_build"
    assert payload["identity"]["source"] == "simc_probe"
    assert payload["identity"]["candidates"] == [{"actor_class": "demonhunter", "spec": "devourer"}]
    assert payload["identity"]["identity_contract"]["kind"] == "build_identity"
    assert payload["identity"]["identity_contract"]["class_spec_identity"]["status"] == "inferred"


def test_simc_identify_build_accepts_build_packet(monkeypatch, tmp_path: Path) -> None:
    packet_path = tmp_path / "build-packet.json"
    packet_path.write_text('{"kind":"talent_transport_packet"}')

    def fake_loader(_paths, **kwargs):  # noqa: ANN001
        assert kwargs["build_packet"] == str(packet_path)
        return (
            type(
                "BuildSpec",
                (),
                {
                    "actor_class": "druid",
                    "spec": "balance",
                    "talents": "ABC123",
                    "class_talents": None,
                    "spec_talents": None,
                    "hero_talents": None,
                    "source_kind": "wowhead_talent_calc_url",
                    "source_notes": ["talent transport packet"],
                    "transport_form": "wowhead_talent_calc_url",
                    "transport_status": "exact",
                    "transport_source": str(packet_path),
                },
            )(),
            type(
                "BuildIdentity",
                (),
                {
                    "actor_class": "druid",
                    "spec": "balance",
                    "confidence": "high",
                    "source": "wowhead_talent_calc_url",
                    "candidate_count": 1,
                    "candidates": [("druid", "balance")],
                    "source_notes": ["talent transport packet"],
                },
            )(),
        )

    monkeypatch.setattr("simc_cli.main._load_identified_build_spec", fake_loader)

    result = runner.invoke(simc_app, ["identify-build", "--build-packet", str(packet_path)])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["build_spec"]["transport_packet"]["path"] == str(packet_path)
    assert payload["build_spec"]["transport_packet"]["transport_form"] == "wowhead_talent_calc_url"
    assert payload["build_spec"]["transport_packet"]["transport_status"] == "exact"


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
            type(
                "BuildIdentity",
                (),
                {
                    "actor_class": build_spec.actor_class,
                    "spec": build_spec.spec,
                    "confidence": "high",
                    "source": build_spec.source_kind,
                    "candidate_count": 1,
                    "candidates": [(build_spec.actor_class, build_spec.spec)],
                    "source_notes": build_spec.source_notes,
                },
            )(),
        ),
    )

    result = runner.invoke(simc_app, ["identify-build", "--build-packet", str(packet_path)])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["build_spec"]["source_kind"] == "wow_talent_export"
    assert payload["build_spec"]["talents"] == "ABC123"
    assert payload["build_spec"]["transport_packet"]["transport_form"] == "wow_talent_export"
    assert payload["identity"]["source"] == "wow_talent_export"


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
        "simc_cli.build_input.supported_specs",
        lambda _repo: [("priest", "shadow"), ("druid", "balance")],
    )
    monkeypatch.setattr(
        "simc_cli.build_input.decode_build",
        lambda _repo, build_spec: (
            (_ for _ in ()).throw(RuntimeError("wrong spec"))
            if (build_spec.actor_class, build_spec.spec) == ("priest", "shadow")
            else type("Resolution", (), {"enabled_talents": {"moonkin_form"}})()
        ),
    )

    result = runner.invoke(simc_app, ["identify-build", "--build-packet", str(packet_path)])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["build_spec"]["actor_class"] == "druid"
    assert payload["build_spec"]["spec"] == "balance"
    assert payload["build_spec"]["source_kind"] == "wow_talent_export"
    assert payload["identity"]["source"] == "simc_probe"
    assert payload["identity"]["candidates"] == [{"actor_class": "druid", "spec": "balance"}]


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
        "simc_cli.build_input.decode_build",
        lambda _repo, build_spec: (_ for _ in ()).throw(AssertionError("validated split packets should not reprobe")),
    )

    result = runner.invoke(simc_app, ["identify-build", "--build-packet", str(packet_path)])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["build_spec"]["actor_class"] == "priest"
    assert payload["build_spec"]["spec"] == "shadow"
    assert payload["build_spec"]["source_kind"] == "simc_split_talents"
    assert payload["identity"]["source"] == "simc_split_talents"
    assert payload["identity"]["candidates"] == [{"actor_class": "priest", "spec": "shadow"}]


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
        "simc_cli.build_input.decode_build",
        lambda _repo, build_spec: (_ for _ in ()).throw(AssertionError("validated split packets should not reprobe")),
    )

    result = runner.invoke(simc_app, ["identify-build", "--apl-path", str(apl_path), "--build-packet", str(packet_path)])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["build_spec"]["actor_class"] == "priest"
    assert payload["build_spec"]["spec"] == "shadow"
    assert payload["identity"]["source"] == "simc_split_talents"
    assert payload["identity"]["candidates"] == [{"actor_class": "priest", "spec": "shadow"}]


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
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_query"
    assert "must include a build code" in payload["error"]["message"]


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
    assert payload["input"]["source"] == "build_packet"
    assert payload["input"]["packet_transport_status"] == "raw_only"
    assert payload["transport_status"] == "validated"
    assert payload["transport_forms"]["simc_split_talents"]["spec_talents"] == "109839:1"
    assert payload["updated_packet"]["transport_status"] == "validated"
    assert payload["updated_packet"]["build_identity"]["class_spec_identity"]["identity"] == {
        "actor_class": "druid",
        "spec": "balance",
    }
    assert payload["updated_packet"]["validation"]["actor_class"] == "druid"
    assert payload["updated_packet"]["validation"]["spec"] == "balance"
    packet_payload = json.loads(packet_path.read_text())
    assert payload["updated_packet"].get("source") == packet_payload.get("source")


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
    assert payload["updated_packet"]["build_identity"]["class_spec_identity"]["identity"] == {
        "actor_class": "priest",
        "spec": "shadow",
    }
    assert payload["updated_packet"]["validation"]["actor_class"] == "priest"
    assert payload["updated_packet"]["validation"]["spec"] == "shadow"


def test_simc_validate_talent_transport_rejects_build_packet_with_talent_rows(tmp_path: Path) -> None:
    packet_path = tmp_path / "build-packet.json"
    packet_path.write_text('{"kind":"talent_transport_packet"}')

    result = runner.invoke(
        simc_app,
        ["validate-talent-transport", "--build-packet", str(packet_path), "--talent-row", "103324:82244:1"],
    )
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_query"
    assert payload["error"]["message"] == "Use either --build-packet or --talent-row, not both."


def test_simc_validate_talent_transport_rejects_out_without_build_packet() -> None:
    result = runner.invoke(
        simc_app,
        ["validate-talent-transport", "--actor-class", "druid", "--spec", "balance",
            "--talent-row", "103324:82244:1", "--out", "./tmp/validated-packet.json"],
    )
    assert result.exit_code == 1
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
    assert result.exit_code == 1
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
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_query"
    assert payload["error"]["message"] == "No raw talent rows were available to validate."


def test_simc_validate_talent_transport_rejects_talent_rows_without_class_spec_identity() -> None:
    result = runner.invoke(
        simc_app,
        ["validate-talent-transport", "--talent-row", "103324:82244:1"],
    )
    assert result.exit_code == 1
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
    assert payload["written_packet_path"] == str(out_path.resolve())

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
    assert payload["input"]["source"] == "talent_rows"
    assert payload["transport_status"] == "raw_only"
    assert payload["validation"]["reason"] == "simc_trait_resolution_incomplete"


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
    assert payload["transport_status"] == "raw_only"
    assert payload["transport_forms"] == {}
    assert payload["validation"]["status"] == "not_validated"
    assert payload["validation"]["reason"] == "no_ranked_talent_entries"
    assert payload["updated_packet"]["transport_status"] == "raw_only"
    assert payload["updated_packet"]["transport_forms"] == {}
    assert payload["updated_packet"]["validation"]["reason"] == "no_ranked_talent_entries"


def test_simc_validate_talent_transport_requires_one_input_mode() -> None:
    result = runner.invoke(simc_app, ["validate-talent-transport"])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_query"


def test_simc_decode_build_auto_identifies_missing_class_and_spec(monkeypatch) -> None:
    monkeypatch.setattr(
        "simc_cli.main._load_identified_build_spec",
        lambda *args, **kwargs: (
            type(
                "BuildSpec",
                (),
                {
                    "actor_class": "demonhunter",
                    "spec": "devourer",
                    "talents": "ABC123",
                    "class_talents": None,
                    "spec_talents": None,
                    "hero_talents": None,
                    "source_kind": "wow_talent_export",
                    "source_notes": ["single-line talent export", "identified by SimC probe"],
                },
            )(),
            type(
                "BuildIdentity",
                (),
                {
                    "actor_class": "demonhunter",
                    "spec": "devourer",
                    "confidence": "high",
                    "source": "simc_probe",
                    "candidate_count": 1,
                    "candidates": [("demonhunter", "devourer")],
                    "source_notes": ["single-line talent export", "identified by SimC probe"],
                },
            )(),
        ),
    )
    monkeypatch.setattr(
        "simc_cli.main.decode_build",
        lambda paths, build_spec: type(
            "Resolution",
            (),
            {
                "actor_class": "demonhunter",
                "spec": "devourer",
                "enabled_talents": {"void_ray"},
                "source_kind": "wow_talent_export",
                "generated_profile_text": 'demonhunter="simc_decode"\nlevel=90\nrace=night_elf\nspec=devourer\ntalents=ABC123\n',
                "talents_by_tree": {"class": [], "spec": [], "hero": [], "selection": []},
                "source_notes": ["decoded via /tmp/simc"],
            },
        )(),
    )
    result = runner.invoke(simc_app, ["decode-build", "--build-text", "ABC123"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["build_spec"]["actor_class"] == "demonhunter"
    assert payload["identity"]["source"] == "simc_probe"
    assert payload["decoded"]["spec"] == "devourer"


def test_simc_decode_build_accepts_build_packet(monkeypatch, tmp_path: Path) -> None:
    packet_path = tmp_path / "build-packet.json"
    packet_path.write_text('{"kind":"talent_transport_packet"}')

    def fake_loader(_paths, **kwargs):  # noqa: ANN001
        assert kwargs["build_packet"] == str(packet_path)
        return (
            type(
                "BuildSpec",
                (),
                {
                    "actor_class": "druid",
                    "spec": "balance",
                    "talents": None,
                    "class_talents": "103324:1",
                    "spec_talents": "109839:1",
                    "hero_talents": "117176:1",
                    "source_kind": "simc_split_talents",
                    "source_notes": ["talent transport packet"],
                    "transport_form": "simc_split_talents",
                    "transport_status": "validated",
                    "transport_source": str(packet_path),
                },
            )(),
            type(
                "BuildIdentity",
                (),
                {
                    "actor_class": "druid",
                    "spec": "balance",
                    "confidence": "high",
                    "source": "warcraftlogs_talent_tree",
                    "candidate_count": 1,
                    "candidates": [("druid", "balance")],
                    "source_notes": ["talent transport packet"],
                },
            )(),
        )

    monkeypatch.setattr("simc_cli.main._load_identified_build_spec", fake_loader)
    monkeypatch.setattr(
        "simc_cli.main.decode_build",
        lambda paths, build_spec: type(
            "Resolution",
            (),
            {
                "actor_class": "druid",
                "spec": "balance",
                "enabled_talents": {"innervate", "incarnation_chosen_of_elune"},
                "source_kind": "simc_split_talents",
                "generated_profile_text": 'druid="simc_decode"\nclass_talents=103324:1\nspec_talents=109839:1\nhero_talents=117176:1\n',
                "talents_by_tree": {"class": [], "spec": [], "hero": [], "selection": []},
                "source_notes": ["talent transport packet", "decoded via /tmp/simc"],
            },
        )(),
    )

    result = runner.invoke(simc_app, ["decode-build", "--build-packet", str(packet_path)])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["build_spec"]["transport_packet"]["transport_form"] == "simc_split_talents"
    assert payload["decoded"]["source_kind"] == "simc_split_talents"


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
        root=tmp_path,
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
        return type(
            "Resolution",
            (),
            {
                "actor_class": "priest",
                "spec": "shadow",
                "enabled_talents": {"mind_blast"},
                "source_kind": "simc_split_talents",
                "generated_profile_text": 'priest="simc_decode"\nclass_talents=103324:1\nspec_talents=109839:1\n',
                "talents_by_tree": {"class": [], "spec": [], "hero": [], "selection": []},
                "source_notes": ["talent transport packet", "decoded via /tmp/simc"],
            },
        )()

    monkeypatch.setattr("simc_cli.main.decode_build", fake_decode_build)

    result = runner.invoke(simc_app, ["decode-build", "--build-packet", str(packet_path)])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["identity"]["source"] == "simc_split_talents"
    assert payload["build_spec"]["actor_class"] == "priest"
    assert payload["build_spec"]["spec"] == "shadow"


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
        return type(
            "Resolution",
            (),
            {
                "actor_class": "druid",
                "spec": "balance",
                "enabled_talents": {"moonkin_form"},
                "source_kind": "wowhead_talent_calc_url",
                "generated_profile_text": 'druid="simc_decode"\ntalents=ABC123\n',
                "talents_by_tree": {"class": [], "spec": [], "hero": [], "selection": []},
                "source_notes": ["talent transport packet"],
            },
        )()

    monkeypatch.setattr("simc_cli.main.decode_build", fake_decode_build)

    result = runner.invoke(simc_app, ["decode-build", "--build-packet", str(packet_path)])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["build_spec"]["source_kind"] == "wowhead_talent_calc_url"
    assert payload["build_spec"]["talents"] == "ABC123"
    assert payload["build_spec"]["transport_packet"]["transport_form"] == "wowhead_talent_calc_url"
    assert payload["decoded"]["source_kind"] == "wowhead_talent_calc_url"


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
        "simc_cli.build_input.supported_specs",
        lambda _repo: [("priest", "shadow"), ("druid", "balance")],
    )
    monkeypatch.setattr(
        "simc_cli.build_input.decode_build",
        lambda _repo, build_spec: (
            (_ for _ in ()).throw(RuntimeError("wrong spec"))
            if (build_spec.actor_class, build_spec.spec) == ("priest", "shadow")
            else type("Resolution", (), {"enabled_talents": {"moonkin_form"}})()
        ),
    )

    def fake_decode_build(_paths, build_spec):  # noqa: ANN001
        assert build_spec.actor_class == "druid"
        assert build_spec.spec == "balance"
        return type(
            "Resolution",
            (),
            {
                "actor_class": "druid",
                "spec": "balance",
                "enabled_talents": {"moonkin_form"},
                "source_kind": "wow_talent_export",
                "generated_profile_text": 'druid="simc_decode"\ntalents=ABC123\n',
                "talents_by_tree": {"class": [], "spec": [], "hero": [], "selection": []},
                "source_notes": ["decoded via /tmp/simc"],
            },
        )()

    monkeypatch.setattr("simc_cli.main.decode_build", fake_decode_build)

    result = runner.invoke(simc_app, ["decode-build", "--build-packet", str(packet_path)])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["build_spec"]["actor_class"] == "druid"
    assert payload["build_spec"]["spec"] == "balance"
    assert payload["identity"]["source"] == "simc_probe"
    assert payload["decoded"]["actor_class"] == "druid"
    assert payload["decoded"]["spec"] == "balance"


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
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_query"
    assert "must include a build code" in payload["error"]["message"]


def test_simc_describe_build_summarizes_st_and_aoe(monkeypatch, tmp_path: Path) -> None:
    apl_path = tmp_path / "demonhunter_devourer.simc"
    apl_path.write_text("actions=void_ray\n")

    monkeypatch.setattr(
        "simc_cli.main._load_identified_build_spec",
        lambda *args, **kwargs: (
            type(
                "BuildSpec",
                (),
                {
                    "actor_class": "demonhunter",
                    "spec": "devourer",
                    "talents": "ABC123",
                    "class_talents": None,
                    "spec_talents": None,
                    "hero_talents": None,
                    "source_kind": "wow_talent_export",
                    "source_notes": ["single-line talent export"],
                },
            )(),
            type(
                "BuildIdentity",
                (),
                {
                    "actor_class": "demonhunter",
                    "spec": "devourer",
                    "confidence": "high",
                    "source": "simc_probe",
                    "candidate_count": 1,
                    "candidates": [("demonhunter", "devourer")],
                    "source_notes": ["single-line talent export", "identified by SimC probe"],
                },
            )(),
        ),
    )

    resolution = type(
        "Resolution",
        (),
        {
            "actor_class": "demonhunter",
            "spec": "devourer",
            "source_kind": "wow_talent_export",
            "enabled_talents": {"void_ray", "world_killer", "soul_immolation"},
            "talents_by_tree": {
                "class": [type("Talent", (), {"name": "Voidblade", "token": "voidblade", "rank": 1, "max_rank": 1})()],
                "spec": [
                    type("Talent", (), {"name": "Void Ray", "token": "void_ray", "rank": 1, "max_rank": 1})(),
                    type("Talent", (), {"name": "Midnight", "token": "midnight", "rank": 0, "max_rank": 1})(),
                    type("Talent", (), {"name": "Soul Immolation", "token": "soul_immolation", "rank": 1, "max_rank": 1})(),
                ],
                "hero": [type("Talent", (), {"name": "World Killer", "token": "world_killer", "rank": 1, "max_rank": 1})()],
                "selection": [],
            },
            "source_notes": ["decoded via /tmp/simc"],
        },
    )()

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
    assert payload["identity"]["source"] == "simc_probe"
    assert payload["build"]["talents_by_tree"]["spec"]["selected"][0]["token"] == "void_ray"
    assert payload["build"]["talents_by_tree"]["spec"]["skipped"][0]["token"] == "midnight"
    assert payload["single_target"]["focus_list"] == "melee_combo"
    assert payload["multi_target"]["focus_list"] == "aoe"
    assert payload["comparison"]["new_active_actions_in_aoe"] == ["soul_immolation"]
    assert payload["single_target"]["inactive_talent_branches"][0]["action"] == "the_hunt"


def test_simc_describe_build_accepts_build_packet(monkeypatch, tmp_path: Path) -> None:
    apl_path = tmp_path / "druid_balance.simc"
    apl_path.write_text("actions=wrath\n")
    packet_path = tmp_path / "build-packet.json"
    packet_path.write_text('{"kind":"talent_transport_packet"}')

    def fake_loader(_paths, **kwargs):  # noqa: ANN001
        assert kwargs["build_packet"] == str(packet_path)
        return (
            type(
                "BuildSpec",
                (),
                {
                    "actor_class": "druid",
                    "spec": "balance",
                    "talents": None,
                    "class_talents": "103324:1",
                    "spec_talents": "109839:1",
                    "hero_talents": "117176:1",
                    "source_kind": "simc_split_talents",
                    "source_notes": ["talent transport packet"],
                    "transport_form": "simc_split_talents",
                    "transport_status": "validated",
                    "transport_source": str(packet_path),
                },
            )(),
            type(
                "BuildIdentity",
                (),
                {
                    "actor_class": "druid",
                    "spec": "balance",
                    "confidence": "high",
                    "source": "warcraftlogs_talent_tree",
                    "candidate_count": 1,
                    "candidates": [("druid", "balance")],
                    "source_notes": ["talent transport packet"],
                },
            )(),
        )

    monkeypatch.setattr("simc_cli.main._load_identified_build_spec", fake_loader)

    resolution = type(
        "Resolution",
        (),
        {
            "actor_class": "druid",
            "spec": "balance",
            "source_kind": "simc_split_talents",
            "enabled_talents": {"wrath"},
            "talents_by_tree": {"class": [], "spec": [], "hero": [], "selection": []},
            "source_notes": ["talent transport packet", "decoded via /tmp/simc"],
        },
    )()

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
    assert payload["build_spec"]["transport_packet"]["path"] == str(packet_path)
    assert payload["build_spec"]["transport_packet"]["transport_form"] == "simc_split_talents"


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
        root=tmp_path,
        apl_default=tmp_path,
        apl_assisted=tmp_path,
        class_modules=tmp_path,
        spell_dump=tmp_path,
        build_dir=tmp_path,
        build_simc=tmp_path / "simc",
    )
    monkeypatch.setattr("simc_cli.main._repo_paths", lambda _ctx: repo)

    resolution = type(
        "Resolution",
        (),
        {
            "actor_class": "priest",
            "spec": "shadow",
            "source_kind": "simc_split_talents",
            "enabled_talents": {"mind_blast"},
            "talents_by_tree": {"class": [], "spec": [], "hero": [], "selection": []},
            "source_notes": ["talent transport packet", "decoded via /tmp/simc"],
        },
    )()

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
    assert payload["identity"]["source"] == "simc_split_talents"
    assert payload["build_spec"]["actor_class"] == "priest"
    assert payload["build_spec"]["spec"] == "shadow"


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
        "simc_cli.build_input.supported_specs",
        lambda _repo: [("druid", "balance")],
    )
    monkeypatch.setattr(
        "simc_cli.build_input.decode_build",
        lambda _repo, build_spec: type("Resolution", (), {"enabled_talents": {"wrath"}})(),
    )

    resolution = type(
        "Resolution",
        (),
        {
            "actor_class": "druid",
            "spec": "balance",
            "source_kind": "wow_talent_export",
            "enabled_talents": {"wrath"},
            "talents_by_tree": {"class": [], "spec": [], "hero": [], "selection": []},
            "source_notes": ["talent transport packet"],
        },
    )()

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
    assert payload["build_spec"]["source_kind"] == "wow_talent_export"
    assert payload["build_spec"]["talents"] == "ABC123"
    assert payload["build_spec"]["transport_packet"]["transport_form"] == "wow_talent_export"


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
        "simc_cli.build_input.supported_specs",
        lambda _repo: [("priest", "shadow"), ("druid", "balance")],
    )
    monkeypatch.setattr(
        "simc_cli.build_input.decode_build",
        lambda _repo, build_spec: (
            (_ for _ in ()).throw(RuntimeError("wrong spec"))
            if (build_spec.actor_class, build_spec.spec) == ("priest", "shadow")
            else type("Resolution", (), {"enabled_talents": {"moonkin_form"}})()
        ),
    )

    resolution = type(
        "Resolution",
        (),
        {
            "actor_class": "druid",
            "spec": "balance",
            "source_kind": "wow_talent_export",
            "enabled_talents": {"wrath"},
            "talents_by_tree": {"class": [], "spec": [], "hero": [], "selection": []},
            "source_notes": ["talent transport packet"],
        },
    )()

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
    assert payload["build_spec"]["actor_class"] == "druid"
    assert payload["build_spec"]["spec"] == "balance"
    assert payload["identity"]["source"] == "simc_probe"
    assert payload["build"]["actor_class"] == "druid"
    assert payload["build"]["spec"] == "balance"


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
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_query"
    assert "must include a build code" in payload["error"]["message"]


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
            type(
                "BuildSpec",
                (),
                {
                    "actor_class": "demonhunter",
                    "spec": "devourer",
                    "talents": "ABC123",
                    "class_talents": None,
                    "spec_talents": None,
                    "hero_talents": None,
                    "source_kind": "wow_talent_export",
                    "source_notes": ["single-line talent export"],
                },
            )(),
            type(
                "BuildIdentity",
                (),
                {
                    "actor_class": "demonhunter",
                    "spec": "devourer",
                    "confidence": "high",
                    "source": "simc_probe",
                    "candidate_count": 1,
                    "candidates": [("demonhunter", "devourer")],
                    "source_notes": ["single-line talent export", "identified by SimC probe"],
                },
            )(),
        ),
    )

    resolution = type(
        "Resolution",
        (),
        {
            "actor_class": "demonhunter",
            "spec": "devourer",
            "source_kind": "wow_talent_export",
            "enabled_talents": {"void_ray"},
            "talents_by_tree": {"class": [], "spec": [], "hero": [], "selection": []},
            "source_notes": ["decoded via /tmp/simc"],
        },
    )()

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
    assert payload["single_target"]["focus_list"] == "leaf"
    assert payload["single_target"]["focus_path"] == ["default", "leaf"]
    assert payload["single_target"]["focus_resolution"] == "guaranteed_call_leaf"
    assert payload["comparison"]["new_active_actions_in_aoe"] == ["collapsing_star"]


def test_simc_decode_build_failure_includes_source_metadata(monkeypatch) -> None:
    monkeypatch.setattr(
        "simc_cli.main.load_build_spec",
        lambda **kwargs: type("BuildSpec", (), {
            "actor_class": "demonhunter",
            "spec": "devourer",
            "talents": "CgcBG5bbocFKcv+yIq8fPd6ORBA2MmZmxMzMGzMAAAAAAAegxsNYGAAAAAAAAmxMMmZmZmZmZGzsYGjFtsxMzMzWbzMzAYYAIwMGMmB",
            "class_talents": None,
            "spec_talents": None,
            "hero_talents": None,
            "source_kind": "wow_talent_export",
            "source_notes": ["single-line talent export", "inline build text"],
        })(),
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
    assert payload["build_spec"]["source_kind"] == "wow_talent_export"
    assert 'demonhunter="simc_decode"' in payload["generated_profile"]


def _fake_build_spec(*, actor_class="druid", spec="balance", talents="ABC123"):  # noqa: ANN001
    return type("BuildSpec", (), {
        "actor_class": actor_class,
        "spec": spec,
        "talents": talents,
        "class_talents": None,
        "spec_talents": None,
        "hero_talents": None,
        "source_kind": "wow_talent_export",
        "source_notes": ["command-line build options"],
    })()


def _fake_identity(*, actor_class="druid", spec="balance"):  # noqa: ANN001
    return type("BuildIdentity", (), {
        "actor_class": actor_class,
        "spec": spec,
        "confidence": "high",
        "source": "direct",
        "candidate_count": 1,
        "candidates": [(actor_class, spec)],
        "source_notes": ["command-line build options"],
    })()


def _fake_resolution(
    *,
    actor_class="druid",
    spec="balance",
    class_talents=None,
    spec_talents=None,
    hero_talents=None,
):  # noqa: ANN001
    from simc_cli.build_input import DecodedTalent

    def _t(tree, name, entry, rank=1, max_rank=1):  # noqa: ANN001
        token = name.lower().replace("'", "").replace(" ", "_").replace("-", "_")
        return DecodedTalent(tree=tree, name=name, token=token, rank=rank, max_rank=max_rank, entry=entry)

    return type("Resolution", (), {
        "actor_class": actor_class,
        "spec": spec,
        "enabled_talents": {"thick_hide", "innervate", "starlord", "dream_surge"},
        "source_kind": "wow_talent_export",
        "generated_profile_text": None,
        "talents_by_tree": {
            "class": class_talents or [
                _t("class", "Thick Hide", 100),
                _t("class", "Innervate", 200),
            ],
            "spec": spec_talents or [
                _t("spec", "Starlord", 300, rank=2, max_rank=2),
            ],
            "hero": hero_talents or [
                _t("hero", "Dream Surge", 400),
            ],
            "selection": [],
        },
        "source_notes": ["decoded via /tmp/simc"],
    })()


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
    monkeypatch.setattr("simc_cli.main.load_build_spec", lambda **kw: _fake_build_spec())
    monkeypatch.setattr("simc_cli.main.decode_build", fake_decode)

    result = runner.invoke(simc_app, [
        "compare-builds", "--base", "ABC123", "--other", "DEF456", "--tree", "class",
    ])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["kind"] == "compare_builds"
    assert payload["base"]["actor_class"] == "druid"
    assert payload["trees_compared"] == ["class"]
    comp = payload["comparisons"][0]
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
    monkeypatch.setattr("simc_cli.main.load_build_spec", lambda **kw: _fake_build_spec())
    monkeypatch.setattr("simc_cli.main.decode_build", lambda paths, spec: res)

    result = runner.invoke(simc_app, ["compare-builds", "--base", "ABC", "--other", "ABC"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["comparisons"][0]["has_differences"] is False


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
    monkeypatch.setattr("simc_cli.main.load_build_spec", lambda **kw: _fake_build_spec())
    monkeypatch.setattr("simc_cli.main.decode_build", lambda paths, spec: next(decode_results))

    result = runner.invoke(simc_app, [
        "compare-builds", "--base", "A", "--other", "B", "--other", "C", "--tree", "class",
    ])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert len(payload["comparisons"]) == 2
    assert payload["comparisons"][0]["has_differences"] is False
    assert payload["comparisons"][1]["has_differences"] is True


def test_simc_compare_builds_decode_failure_reports_error(monkeypatch) -> None:
    call_count = {"n": 0}

    def fake_decode(paths, spec):  # noqa: ANN001
        call_count["n"] += 1
        if call_count["n"] == 1:
            return _fake_resolution()
        raise RuntimeError("bad build")

    monkeypatch.setattr(
        "simc_cli.main._load_identified_build_spec",
        lambda *a, **kw: (_fake_build_spec(), _fake_identity()),
    )
    monkeypatch.setattr("simc_cli.main.load_build_spec", lambda **kw: _fake_build_spec())
    monkeypatch.setattr("simc_cli.main.decode_build", fake_decode)

    result = runner.invoke(simc_app, ["compare-builds", "--base", "A", "--other", "BAD"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert "error" in payload["comparisons"][0]


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
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_query"
    assert "must include a build code" in payload["error"]["message"]


def test_simc_compare_builds_reports_buildless_wowhead_other_as_structured_error(monkeypatch) -> None:
    monkeypatch.setattr(
        "simc_cli.main._load_identified_build_spec_or_fail",
        lambda *args, **kwargs: (_fake_build_spec(), _fake_identity()),
    )
    monkeypatch.setattr("simc_cli.main.decode_build", lambda paths, spec: _fake_resolution())

    result = runner.invoke(
        simc_app,
        [
            "compare-builds",
            "--base",
            "BASE",
            "--other",
            "https://www.wowhead.com/talent-calc/druid/balance",
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["comparisons"][0]["input"] == "https://www.wowhead.com/talent-calc/druid/balance"
    assert "must include a build code" in payload["comparisons"][0]["error"]


# --- modify-build ---


def test_simc_modify_build_swap_class_tree(monkeypatch) -> None:
    from simc_cli.build_input import DecodedTalent

    def _t(tree, name, entry, rank=1, max_rank=1):  # noqa: ANN001
        token = name.lower().replace(" ", "_")
        return DecodedTalent(tree=tree, name=name, token=token, rank=rank, max_rank=max_rank, entry=entry)

    base_res = _fake_resolution(
        class_talents=[_t("class", "Innervate", 200)],
        spec_talents=[_t("spec", "Starlord", 300)],
        hero_talents=[_t("hero", "Dream Surge", 400)],
    )
    swap_res = _fake_resolution(
        class_talents=[_t("class", "Thick Hide", 100)],
    )
    verify_res = _fake_resolution(
        class_talents=[_t("class", "Thick Hide", 100)],
        spec_talents=[_t("spec", "Starlord", 300)],
        hero_talents=[_t("hero", "Dream Surge", 400)],
    )

    decode_results = iter([base_res, swap_res, verify_res])

    monkeypatch.setattr(
        "simc_cli.main._load_identified_build_spec",
        lambda *a, **kw: (_fake_build_spec(), _fake_identity()),
    )
    monkeypatch.setattr("simc_cli.main.load_build_spec", lambda **kw: _fake_build_spec())
    monkeypatch.setattr("simc_cli.main.decode_build", lambda p, s: next(decode_results))
    monkeypatch.setattr("simc_cli.main.encode_build", lambda p, s: "SPLICED_EXPORT")

    result = runner.invoke(simc_app, [
        "modify-build", "--talents", "BASE", "--swap-class-tree-from", "REF",
    ])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["kind"] == "modify_build"
    assert payload["modifications"] == ["swap_class_tree"]
    assert payload["result"]["talents_export"] == "SPLICED_EXPORT"
    assert "wowhead.com/talent-calc/blizzard/SPLICED_EXPORT" in payload["result"]["wowhead_url"]
    assert payload["result"]["diff_from_base"]["class"]["has_differences"] is True


def test_simc_modify_build_add_and_remove(monkeypatch) -> None:
    base_res = _fake_resolution()

    monkeypatch.setattr(
        "simc_cli.main._load_identified_build_spec",
        lambda *a, **kw: (_fake_build_spec(), _fake_identity()),
    )
    monkeypatch.setattr("simc_cli.main.decode_build", lambda p, s: base_res)
    monkeypatch.setattr("simc_cli.main.encode_build", lambda p, s: "MODIFIED_EXPORT")

    result = runner.invoke(simc_app, [
        "modify-build", "--talents", "BASE",
        "--remove", "innervate", "--add", "forestwalk:2",
    ])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["kind"] == "modify_build"
    assert "remove:innervate" in payload["modifications"]
    assert "add:forestwalk:2" in payload["modifications"]
    assert payload["result"]["talents_export"] == "MODIFIED_EXPORT"


def test_simc_modify_build_remove_by_entry_id(monkeypatch) -> None:
    base_res = _fake_resolution()

    monkeypatch.setattr(
        "simc_cli.main._load_identified_build_spec",
        lambda *a, **kw: (_fake_build_spec(), _fake_identity()),
    )
    monkeypatch.setattr("simc_cli.main.decode_build", lambda p, s: base_res)
    monkeypatch.setattr("simc_cli.main.encode_build", lambda p, s: "MODIFIED")

    result = runner.invoke(simc_app, [
        "modify-build", "--talents", "BASE", "--remove", "200",
    ])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert "remove:200" in payload["modifications"]


def test_simc_modify_build_fails_without_modifications(monkeypatch) -> None:
    monkeypatch.setattr(
        "simc_cli.main._load_identified_build_spec",
        lambda *a, **kw: (_fake_build_spec(), _fake_identity()),
    )
    monkeypatch.setattr("simc_cli.main.decode_build", lambda p, s: _fake_resolution())

    result = runner.invoke(simc_app, ["modify-build", "--talents", "BASE"])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "no_modifications"


def test_simc_modify_build_fails_on_unknown_remove_name(monkeypatch) -> None:
    monkeypatch.setattr(
        "simc_cli.main._load_identified_build_spec",
        lambda *a, **kw: (_fake_build_spec(), _fake_identity()),
    )
    monkeypatch.setattr("simc_cli.main.decode_build", lambda p, s: _fake_resolution())

    result = runner.invoke(simc_app, [
        "modify-build", "--talents", "BASE", "--remove", "nonexistent_talent",
    ])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "unknown_talent"


def test_simc_modify_build_fails_on_bad_add_format(monkeypatch) -> None:
    monkeypatch.setattr(
        "simc_cli.main._load_identified_build_spec",
        lambda *a, **kw: (_fake_build_spec(), _fake_identity()),
    )
    monkeypatch.setattr("simc_cli.main.decode_build", lambda p, s: _fake_resolution())

    result = runner.invoke(simc_app, [
        "modify-build", "--talents", "BASE", "--add", "no_rank",
    ])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_add"


def test_simc_modify_build_aborts_when_swap_tree_decode_fails(monkeypatch) -> None:
    # Regression: a failing --swap-*-tree-from source must abort the command via
    # _fail (typer.Exit) even when --add/--remove are also present — it must NOT
    # fall through to encode/emit a modified build.
    base_res = _fake_resolution()
    decode_calls = {"n": 0}

    def fake_decode(p, s):  # noqa: ANN001
        decode_calls["n"] += 1
        if decode_calls["n"] == 1:
            return base_res
        raise ValueError("swap source is undecodable")

    monkeypatch.setattr(
        "simc_cli.main._load_identified_build_spec",
        lambda *a, **kw: (_fake_build_spec(), _fake_identity()),
    )
    monkeypatch.setattr("simc_cli.main.load_build_spec", lambda **kw: _fake_build_spec())
    monkeypatch.setattr("simc_cli.main.decode_build", fake_decode)
    monkeypatch.setattr("simc_cli.main.encode_build", lambda p, s: "LEAKED_EXPORT")

    result = runner.invoke(simc_app, [
        "modify-build", "--talents", "BASE",
        "--swap-class-tree-from", "REF", "--add", "forestwalk:2",
    ])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "decode_failed"
    # No modified build may be encoded or emitted after the swap decode failure.
    assert "LEAKED_EXPORT" not in result.stdout
    assert result.stdout.strip() == ""


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
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_query"
    assert "must include a build code" in payload["error"]["message"]


def test_simc_modify_build_rejects_buildless_wowhead_swap_source(monkeypatch) -> None:
    def fake_load_identified_build_spec(paths, **kwargs):  # noqa: ANN001
        if kwargs["talents"] == "BASE":
            return _fake_build_spec(), _fake_identity()
        raise ValueError("Wowhead talent-calc URLs must include a build code.")

    monkeypatch.setattr("simc_cli.main._load_identified_build_spec", fake_load_identified_build_spec)
    monkeypatch.setattr("simc_cli.main.decode_build", lambda paths, spec: _fake_resolution())

    result = runner.invoke(
        simc_app,
        [
            "modify-build",
            "--talents",
            "BASE",
            "--swap-class-tree-from",
            "https://www.wowhead.com/talent-calc/druid/balance",
        ],
    )
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_query"
    assert "must include a build code" in payload["error"]["message"]


def test_simc_modify_build_encode_failure_reports_error(monkeypatch) -> None:
    monkeypatch.setattr(
        "simc_cli.main._load_identified_build_spec",
        lambda *a, **kw: (_fake_build_spec(), _fake_identity()),
    )
    monkeypatch.setattr("simc_cli.main.decode_build", lambda p, s: _fake_resolution())
    monkeypatch.setattr("simc_cli.main.encode_build", lambda p, s: (_ for _ in ()).throw(RuntimeError("encode fail")))

    result = runner.invoke(simc_app, [
        "modify-build", "--talents", "BASE", "--add", "forestwalk:2",
    ])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "encode_failed"


def test_simc_build_harness_compare_report_and_verify_clean(monkeypatch, tmp_path: Path) -> None:
    harness_path = tmp_path / "demo_harness.simc"

    monkeypatch.setattr(
        "simc_cli.main._load_identified_build_spec_or_fail",
        lambda *args, **kwargs: (
            type(
                "BuildSpec",
                (),
                {
                    "actor_class": "warlock",
                    "spec": "demonology",
                    "talents": "ABC123",
                    "class_talents": None,
                    "spec_talents": None,
                    "hero_talents": None,
                    "source_notes": ["command-line build options"],
                },
            )(),
            type(
                "BuildIdentity",
                (),
                {
                    "actor_class": "warlock",
                    "spec": "demonology",
                    "confidence": "high",
                    "source": "direct",
                    "candidate_count": 1,
                    "candidates": [("warlock", "demonology")],
                    "source_notes": ["command-line build options"],
                },
            )(),
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
    assert build_payload["path"] == str(harness_path)
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
    assert validate_payload["valid"] is True
    assert validate_payload["label"] == "wowhead"

    compare_payload = {
        "kind": "apl_comparison",
        "compare_dir": str(tmp_path),
        "harness_path": str(harness_path),
        "iterations": 250,
        "threads": 1,
        "validations": [],
        "base": {"label": "base", "dps": 100.0},
        "ranking": [{"label": "base", "dps": 100.0}, {"label": "wowhead", "dps": 99.0}],
        "comparisons": [{"label": "wowhead", "base_label": "base", "dps_delta": -1.0, "percent_delta": -1.0, "top_action_deltas": []}],
    }
    monkeypatch.setattr("simc_cli.main.compare_apl_variants", lambda *args, **kwargs: compare_payload)
    compare_result = runner.invoke(
        simc_app,
        ["compare-apls", str(harness_path), "--base-apl", str(apl), "--variant",
         f"wowhead={apl}", "--report-out", str(tmp_path / "report.json")],
    )
    assert compare_result.exit_code == 0
    compare_stdout = json.loads(compare_result.stdout)
    assert compare_stdout["kind"] == "apl_comparison"
    assert compare_stdout["report_path"] == str((tmp_path / "report.json").resolve())

    report_result = runner.invoke(simc_app, ["variant-report", str(tmp_path / "report.json")])
    assert report_result.exit_code == 0
    report_payload = json.loads(report_result.stdout)
    assert report_payload["kind"] == "apl_variant_report"
    assert report_payload["best_label"] == "base"

    monkeypatch.setattr(
        "simc_cli.main.verify_clean_payload",
        lambda paths, hash_binary: {"kind": "verify_clean", "repo_root": "/tmp/simc", "git": {"dirty": False}, "binary": {"exists": True}},
    )
    clean_result = runner.invoke(simc_app, ["verify-clean"])
    assert clean_result.exit_code == 0
    clean_payload = json.loads(clean_result.stdout)
    assert clean_payload["kind"] == "verify_clean"
    assert clean_payload["git"]["dirty"] is False


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
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_query"
    assert "must include a build code" in payload["error"]["message"]


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
    assert payload_lists["apl"]["list_count"] == 2
    assert payload_lists["lists"][0]["count"] >= 1

    result_graph = runner.invoke(simc_app, ["apl-graph", str(apl)])
    assert result_graph.exit_code == 0
    payload_graph = json.loads(result_graph.stdout)
    assert payload_graph["graph"]["format"] == "mermaid"
    assert "flowchart TD" in payload_graph["graph"]["text"]

    result_talents = runner.invoke(simc_app, ["apl-talents", str(apl)])
    assert result_talents.exit_code == 0
    payload_talents = json.loads(result_talents.stdout)
    assert payload_talents["count"] == 1
    assert payload_talents["talents"][0]["token"] == "rising_mist"

    monkeypatch.setattr(
        "simc_cli.main.find_action",
        lambda paths, action, wow_class: {"apl_default": [], "apl_assisted": [], "class_modules": [], "spell_dump": []},
    )
    result_trace = runner.invoke(simc_app, ["trace-action", str(apl), "rising_sun_kick"])
    assert result_trace.exit_code == 0
    payload_trace = json.loads(result_trace.stdout)
    assert payload_trace["apl_hits"]["count"] == 1
    assert payload_trace["apl_hits"]["items"][0]["list_name"] == "default"


def test_simc_find_action_groups_hits(monkeypatch) -> None:
    monkeypatch.setattr(
        "simc_cli.main.find_action",
        lambda paths, action, wow_class: {
            "apl_default": [],
            "apl_assisted": [],
            "class_modules": [type("Hit", (), {"path": Path("/tmp/sc_monk.cpp"), "line_no": 42, "text": "if ( action == rising_sun_kick )"})()],
            "spell_dump": [],
        },
    )
    result = runner.invoke(simc_app, ["find-action", "rising_sun_kick"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["count"] == 1
    assert payload["buckets"]["class_modules"]["items"][0]["line_no"] == 42


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
            type("Resolution", (), {"actor_class": "evoker", "spec": "devastation", "source_notes": ["decoded via /tmp/simc"]})(),
        ),
    )

    prune_result = runner.invoke(simc_app, ["apl-prune", str(apl), "--targets", "3"])
    assert prune_result.exit_code == 0
    prune_payload = json.loads(prune_result.stdout)
    assert prune_payload["lists"][0]["items"][0]["state"] == "eligible"

    trace_result = runner.invoke(simc_app, ["apl-branch-trace", str(apl), "--targets", "3"])
    assert trace_result.exit_code == 0
    trace_payload = json.loads(trace_result.stdout)
    assert trace_payload["summary"]["guaranteed_dispatch"] == "aoe"
    assert trace_payload["trace"][0]["text"] == "[default]"

    intent_result = runner.invoke(simc_app, ["apl-intent", str(apl), "--targets", "1"])
    assert intent_result.exit_code == 0
    intent_payload = json.loads(intent_result.stdout)
    assert intent_payload["focus_list"] == "st"
    assert intent_payload["intent"]


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
            type("Resolution", (), {"actor_class": "demonhunter", "spec": "devourer", "source_notes": ["decoded via /tmp/simc"]})(),
        ),
    )

    priority_result = runner.invoke(simc_app, ["priority", str(apl), "--targets", "5"])
    assert priority_result.exit_code == 0
    priority_payload = json.loads(priority_result.stdout)
    assert priority_payload["priority"]["focus_list"] == "aoe"
    assert [row["action"] for row in priority_payload["priority"]["items"][:2]] == ["void_ray", "reapers_toll"]
    assert priority_payload["priority"]["inactive_talent_branches"][0]["action"] == "collapsing_star"

    inactive_result = runner.invoke(simc_app, ["inactive-actions", str(apl), "--targets", "5"])
    assert inactive_result.exit_code == 0
    inactive_payload = json.loads(inactive_result.stdout)
    assert inactive_payload["inactive_actions"]["count"] == 1
    assert inactive_payload["inactive_actions"]["items"][0]["action"] == "collapsing_star"

    opener_result = runner.invoke(simc_app, ["opener", str(apl), "--targets", "5", "--limit", "3"])
    assert opener_result.exit_code == 0
    opener_payload = json.loads(opener_result.stdout)
    assert opener_payload["opener"]["kind"] == "static_priority_preview"
    assert opener_payload["opener"]["items"][0]["action"] == "void_ray"
    assert "static exact-build opener preview" in opener_payload["opener"]["caveat"]


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
            type("Resolution", (), {"actor_class": "evoker", "spec": "devastation", "source_notes": ["decoded via /tmp/simc"]})(),
        )

    monkeypatch.setattr("simc_cli.main._resolve_prune_context", fake_context)

    explain_result = runner.invoke(simc_app, ["apl-intent-explain", str(apl), "--targets", "1"])
    assert explain_result.exit_code == 0
    explain_payload = json.loads(explain_result.stdout)
    assert explain_payload["explained_intent"]["priorities"]

    compare_result = runner.invoke(simc_app, ["apl-branch-compare", str(apl), "--left-targets", "3", "--right-targets", "1"])
    assert compare_result.exit_code == 0
    compare_payload = json.loads(compare_result.stdout)
    assert compare_payload["comparison"]["dispatch_changed"] is True
    assert compare_payload["comparison"]["left_focus_intent"]

    packet_result = runner.invoke(simc_app, ["analysis-packet", str(apl), "--targets", "1"])
    assert packet_result.exit_code == 0
    packet_payload = json.loads(packet_result.stdout)
    assert packet_payload["packet"]["focus_list"] == "st"
    assert packet_payload["packet"]["explained_intent"]["priorities"]


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
    assert first_cast_payload["summary"]["avg"] == 0.25
    assert first_cast_payload["results"][0]["seed"] == 1

    log_result = runner.invoke(simc_app, ["log-actions", str(log_path), "rising_sun_kick"])
    assert log_result.exit_code == 0
    log_payload = json.loads(log_result.stdout)
    assert log_payload["count"] == 1
    assert log_payload["hits"][0]["performed_at"] == 0.25


def test_simc_analysis_packet_surfaces_runtime_timing_failures(monkeypatch, tmp_path: Path) -> None:
    apl = tmp_path / "evoker_devastation.simc"
    apl.write_text("actions.st+=/disintegrate\n")

    monkeypatch.setattr(
        "simc_cli.main._resolve_prune_context",
        lambda paths, apl_path, option_values, targets: (
            type("Context", (), {"enabled_talents": set(), "disabled_talents": set(), "targets": targets, "talent_sources": {}})(),
            type("Resolution", (), {"actor_class": "evoker", "spec": "devastation", "source_notes": ["decoded via /tmp/simc"]})(),
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
    assert payload["status"] == "skipped"
    assert payload["reason"] == "dirty_worktree"


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
    assert payload["status"] == "built"


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
    assert payload["preset"] == "quick"
    assert payload["input_source"] == "file"
    assert payload["run_settings"]["iterations_requested"] == 1000
    assert payload["run_settings"]["iterations_completed"] == 1003
    assert payload["run_settings"]["stop_reason"] == "fixed_iterations_completed"
    assert payload["runtime"]["elapsed_time_seconds"] == 4.33
    assert payload["metrics"]["dps"] == 18834.4
    assert payload["metrics"]["dtps"] == 75769.2
    assert payload["metrics"]["fight_length"] == 299.37
    assert payload["simc_version"] == "SimulationCraft 1201-01"
    assert payload["game_version"] == "12.0.1.66263"
    assert payload["json_report_path"] is None


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
    assert payload["preset"] == "high-accuracy"
    assert payload["input_source"] == "stdin"
    assert payload["profile_path"] is None
    assert payload["run_settings"]["iterations_requested"] == 6000
    assert payload["run_settings"]["threads"] == 4
    assert payload["run_settings"]["fight_style"] == "HecticAddCleave"
    assert payload["run_settings"]["desired_targets"] == 5
    assert payload["run_settings"]["max_time"] == 180


def test_simc_version_reads_explicit_repo_binary(tmp_path: Path) -> None:
    binary = tmp_path / "build" / "simc"
    binary.parent.mkdir()
    binary.write_text("#!/bin/sh\necho 'SimulationCraft 1201-test'\n")
    binary.chmod(0o755)

    result = runner.invoke(simc_app, ["--repo-root", str(tmp_path), "version"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert str(payload["version"]).startswith("SimulationCraft")

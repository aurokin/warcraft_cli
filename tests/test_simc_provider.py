"""Envelope, exit-code, and provider-surface contract for the simc CLI."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from simc_cli.main import app as simc_app
from simc_cli.provider import PROVIDER, simc_envelope
from typer.testing import CliRunner
from warcraft_core.envelope import SCHEMA_VERSION, envelope_violations
from warcraft_core.provider import ProviderSurface

runner = CliRunner()


@pytest.mark.parametrize(
    ("args", "kind"),
    [
        (["doctor"], "doctor"),
        (["search", "mistweaver"], "search"),
        (["resolve", "mistweaver"], "resolve"),
        (["spec-files", "monk"], "spec_files"),
        (["repo"], "repo"),
    ],
)
def test_commands_emit_a_conforming_envelope(tmp_path: Path, args: list[str], kind: str) -> None:
    result = runner.invoke(simc_app, ["--repo-root", str(tmp_path / "missing-repo"), *args])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert envelope_violations(payload) == []
    assert payload["provider"] == "simc"
    assert payload["command"] == args[0]
    assert payload["kind"] == kind
    assert payload["schema_version"] == SCHEMA_VERSION


def test_missing_repo_failure_is_an_error_envelope_on_stderr(tmp_path: Path) -> None:
    """A missing checkout is simc's transport failure: structured error, no traceback."""
    result = runner.invoke(simc_app, ["--repo-root", str(tmp_path / "missing-repo"), "sync"])
    assert result.exit_code == 1
    assert result.stdout == ""
    payload = json.loads(result.stderr)
    assert envelope_violations(payload) == []
    assert payload["error"]["code"] == "missing_repo"


def test_missing_target_exits_with_the_not_found_code(tmp_path: Path) -> None:
    result = runner.invoke(simc_app, ["--repo-root", str(tmp_path / "missing-repo"), "run", str(tmp_path / "absent.simc")])
    assert result.exit_code == 4
    assert json.loads(result.stderr)["error"]["code"] == "not_found"


def test_flat_payload_keys_are_mirrored_inside_data(tmp_path: Path) -> None:
    """Legacy top-level keys stay for existing agents while `data` carries the same payload."""
    result = runner.invoke(simc_app, ["--repo-root", str(tmp_path / "missing-repo"), "doctor"])
    payload = json.loads(result.stdout)
    assert payload["capabilities"] == payload["data"]["capabilities"]
    assert payload["status"] == payload["data"]["status"]


def test_provider_surface_is_pure_and_conforms(tmp_path: Path) -> None:
    assert isinstance(PROVIDER, ProviderSurface)
    for envelope in (
        PROVIDER.search("mistweaver", limit=3, repo_root=str(tmp_path)),
        PROVIDER.resolve("mistweaver", repo_root=str(tmp_path)),
        PROVIDER.doctor(repo_root=str(tmp_path)),
    ):
        assert envelope_violations(envelope) == []
        assert envelope["ok"] is True
    assert PROVIDER.search("mistweaver", repo_root=str(tmp_path))["data"]["coming_soon"] is True



def test_simc_envelope_keeps_the_executed_argv_in_data() -> None:
    """``run``/``sim``/``build``/``sync`` report the argv they executed; the envelope must not drop it."""
    payload = simc_envelope("run", {"command": ["simc", "profile.simc", "iterations=1"], "returncode": 0})
    assert payload["command"] == "run"
    assert payload["data"]["command"] == ["simc", "profile.simc", "iterations=1"]
    assert payload["data"]["returncode"] == 0
    assert envelope_violations(payload) == []

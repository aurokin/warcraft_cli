"""Envelope, exit-code, and provider-surface contract for the simc CLI."""

from __future__ import annotations

import json
import shlex
from pathlib import Path

import pytest
from simc_cli.main import app as simc_app
from simc_cli.provider import BINARY_COMMANDS, CAPABILITIES, PROVIDER, simc_envelope
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


def _checkout_with_monk_files(tmp_path: Path) -> Path:
    """A checkout whose file names already answer a `monk` query, so no ripgrep fallback is needed."""
    root = tmp_path / "checkout"
    for relative in (
        "ActionPriorityLists/default/monk_mistweaver.simc",
        "ActionPriorityLists/assisted_combat/monk_windwalker.simc",
        "engine/class_modules/sc_monk.cpp",
        "engine/class_modules/sc_monk.hpp",
        "SpellDataDump/monk.txt",
    ):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")
    return root


def test_spec_files_emits_a_conforming_envelope_without_ripgrep(monkeypatch, tmp_path: Path) -> None:
    """CI has no ripgrep; a file-name query must still answer from the checkout alone."""
    monkeypatch.setattr("simc_cli.search.shutil.which", lambda _name: None)

    result = runner.invoke(simc_app, ["--repo-root", str(_checkout_with_monk_files(tmp_path)), "spec-files", "monk"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert envelope_violations(payload) == []
    assert payload["kind"] == "spec_files"
    assert payload["data"]["count"] == 5


@pytest.mark.parametrize(
    "command",
    [["spec-files", "monk"], ["find-action", "arcane_blast"], ["trace-action", "monk_mistweaver.simc", "vivify"]],
)
def test_searching_without_a_checkout_is_not_found(tmp_path: Path, command: list[str]) -> None:
    """Searching a checkout that is not there used to report zero hits as a successful search."""
    result = runner.invoke(simc_app, ["--repo-root", str(tmp_path / "missing-repo"), *command])

    assert result.exit_code == 4
    payload = json.loads(result.stderr)
    assert envelope_violations(payload) == []
    assert payload["error"]["code"] == "not_found"
    assert str(tmp_path / "missing-repo") in payload["error"]["message"]


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



def test_resolve_suggests_a_command_that_runs(tmp_path: Path) -> None:
    """It used to suggest `decode-build --apl-path <apl>`, which always fails: an APL carries no build."""
    root = tmp_path / "simc checkout"
    (root / "ActionPriorityLists" / "default").mkdir(parents=True)
    (root / "ActionPriorityLists" / "default" / "monk_windwalker.simc").write_text("actions=tiger_palm\n")

    suggested = PROVIDER.resolve("windwalker", repo_root=str(root))["data"]["suggested_command"]
    program, *args = shlex.split(suggested)
    result = runner.invoke(simc_app, ["--repo-root", str(root), *args])

    assert program == "simc"
    assert result.exit_code == 0, result.stdout + result.stderr


def test_the_capability_map_covers_every_command_the_cli_exposes() -> None:
    """doctor's capability map is the contract agents read; a command missing from it is invisible."""
    commands = {command.name.replace("-", "_") for command in simc_app.registered_commands if command.name}
    assert commands == set(CAPABILITIES)
    assert commands >= BINARY_COMMANDS


def test_simc_envelope_keeps_the_executed_argv_in_data() -> None:
    """``run``/``sim``/``build``/``sync`` report the argv they executed; the envelope must not drop it."""
    payload = simc_envelope("run", {"command": ["simc", "profile.simc", "iterations=1"], "returncode": 0})
    assert payload["command"] == "run"
    assert payload["data"]["command"] == ["simc", "profile.simc", "iterations=1"]
    assert payload["data"]["returncode"] == 0
    assert envelope_violations(payload) == []

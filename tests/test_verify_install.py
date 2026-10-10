"""Guard the release verifier against false passes and inherited developer configuration."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest
from warcraft_core.envelope import error_envelope, success_envelope


def _script(name: str):
    path = Path(__file__).resolve().parents[1] / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_isolated_install_clears_credentials_checkout_and_python_injection(monkeypatch, tmp_path: Path) -> None:
    verifier = _script("verify_wheel")
    for name in ("BLIZZARD_CLIENT_SECRET", "WARCRAFTLOGS_CLIENT_ID", "SIMC_REPO_ROOT", "WARCRAFT_WORKTREE_ROOT", "PYTHONPATH"):
        monkeypatch.setenv(name, "synthetic-inherited-setting")
    env = verifier.isolated_env(tmp_path)
    assert not any(name in env for name in ("BLIZZARD_CLIENT_SECRET", "WARCRAFTLOGS_CLIENT_ID", "SIMC_REPO_ROOT",
                                           "WARCRAFT_WORKTREE_ROOT", "PYTHONPATH"))
    assert env["HOME"] == str(tmp_path / "home")
    assert all(Path(env[name]).is_relative_to(tmp_path) for name in
               ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"))


def test_installed_child_records_a_swallowed_network_attempt(tmp_path: Path) -> None:
    verifier = _script("verify_wheel")
    env = verifier.isolated_env(tmp_path)
    verifier.install_network_guard(tmp_path, env)
    source = "import socket\ntry:\n socket.getaddrinfo('example.invalid',443)\nexcept Exception:\n pass"
    child = subprocess.run([sys.executable, "-c", source], cwd=tmp_path, env=env, capture_output=True, text=True, check=True)
    assert child.stdout == "" and child.stderr == ""
    assert "example.invalid" in Path(env["VERIFY_NETWORK_LOG"]).read_text()


@pytest.mark.parametrize("stdout,stderr,exit_code", [("{}", "extra output", 0), ("extra output", "{}", 3)])
def test_installed_envelope_rejects_output_on_the_other_stream(stdout: str, stderr: str, exit_code: int) -> None:
    verifier = _script("verify_installed")
    result = subprocess.CompletedProcess(["probe"], exit_code, stdout, stderr)
    with pytest.raises(AssertionError):
        verifier.checked_envelope(result, provider="probe", exit_code=exit_code)


def test_installed_envelope_rejects_a_valid_shape_with_the_wrong_provider() -> None:
    import json

    verifier = _script("verify_installed")
    payload = success_envelope(provider="other", command="doctor", kind="doctor", data={})
    result = subprocess.CompletedProcess(["probe"], 0, json.dumps(payload), "")
    with pytest.raises(AssertionError):
        verifier.checked_envelope(result, provider="probe", exit_code=0)


def test_installed_envelope_rejects_wrong_auth_error_code() -> None:
    import json

    verifier = _script("verify_installed")
    payload = error_envelope(provider="probe", command="item", code="network_error", message="synthetic")
    result = subprocess.CompletedProcess(["probe"], 3, "", json.dumps(payload))
    with pytest.raises(AssertionError):
        verifier.checked_envelope(result, provider="probe", exit_code=3, error_codes={"missing_client_credentials"})

from __future__ import annotations

import json
import os

import pytest
from curseforge_cli.auth import load_curseforge_auth_config
from curseforge_cli.main import app
from typer.testing import CliRunner
from warcraft_cli.main import app as warcraft_app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolate_curseforge_env(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    # Isolate provider config discovery so doctor tests never read a real
    # ~/.config/warcraft/providers/curseforge.env or a repo-level .env.local on the dev machine.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CURSEFORGE_API_KEY", raising=False)


def _write_provider_env(tmp_path, contents: str) -> str:
    provider_env = tmp_path / "config" / "warcraft" / "providers" / "curseforge.env"
    provider_env.parent.mkdir(parents=True, exist_ok=True)
    provider_env.write_text(contents)
    return str(provider_env)


def test_doctor_reports_auth_and_capabilities() -> None:
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["provider"] == "curseforge"
    assert payload["status"] == "partial"
    assert payload["installed"] is True
    auth = payload["auth"]
    assert auth["required"] is True
    assert auth["flow"] == "api_key"
    assert auth["key_env"] == "CURSEFORGE_API_KEY"
    assert isinstance(auth["configured"], bool)
    assert auth["configured"] is False
    assert auth["lookup_order"][0] == ".env.local"
    assert auth["lookup_order"][-1] == "environment"
    capabilities = payload["capabilities"]
    assert capabilities["doctor"] == "ready"
    assert capabilities["search"] == "coming_soon"
    assert capabilities["resolve"] == "coming_soon"
    assert capabilities["addon"] == "ready"
    assert payload["notes"]


def test_doctor_reports_configured_when_key_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CURSEFORGE_API_KEY", "abc123")
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["auth"]["configured"] is True
    assert payload["auth"]["credential_source"] == "environment"


def test_load_curseforge_auth_config_reads_env_local(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    env_local = tmp_path / ".env.local"
    env_local.write_text("CURSEFORGE_API_KEY=from-local\n")

    config = load_curseforge_auth_config(start_dir=str(tmp_path))

    assert config.api_key == "from-local"
    assert config.configured is True
    assert config.credential_source == str(env_local)
    # Reading the env file must not leak the key into the process environment.
    assert "CURSEFORGE_API_KEY" not in os.environ


def test_load_curseforge_auth_config_unconfigured_without_key(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    config = load_curseforge_auth_config(start_dir=str(tmp_path))

    assert config.api_key is None
    assert config.configured is False
    assert config.credential_source is None


def test_provider_env_file_overrides_process_key(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Documented precedence: provider env file beats a stale process-environment key.
    monkeypatch.setenv("CURSEFORGE_API_KEY", "from-process")
    provider_env = _write_provider_env(tmp_path, "CURSEFORGE_API_KEY=from-provider\n")

    config = load_curseforge_auth_config(start_dir=str(tmp_path))

    assert config.api_key == "from-provider"
    assert config.credential_source == provider_env


def test_warcraft_curseforge_doctor_routes_through_wrapper() -> None:
    result = runner.invoke(warcraft_app, ["curseforge", "doctor"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["provider"] == "curseforge"
    assert payload["capabilities"]["addon"] == "ready"

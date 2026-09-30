from __future__ import annotations

import json
import os

import pytest
from blizzard_api_cli.auth import load_blizzard_auth_config
from blizzard_api_cli.main import app
from typer.testing import CliRunner

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolate_blizzard_env(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    # Isolate provider config discovery so doctor tests never read a real
    # ~/.config/warcraft/providers/blizzard-api.env or a repo-level .env.local on the dev machine.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    # The client-credentials token cache lives under the state root; isolate it too so a token
    # cached by a live run on the dev machine never leaks into offline expectations.
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BLIZZARD_CLIENT_ID", raising=False)
    monkeypatch.delenv("BLIZZARD_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("BLIZZARD_REGION", raising=False)


def _write_provider_env(tmp_path, contents: str) -> str:
    provider_env = tmp_path / "config" / "warcraft" / "providers" / "blizzard-api.env"
    provider_env.parent.mkdir(parents=True, exist_ok=True)
    provider_env.write_text(contents)
    return str(provider_env)


def test_doctor_reports_scaffold_auth_and_capabilities() -> None:
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["provider"] == "blizzard-api"
    assert payload["data"]["status"] == "partial"
    assert payload["data"]["installed"] is True
    auth = payload["data"]["auth"]
    assert auth["required"] is True
    assert auth["flow"] == "oauth_client_credentials"
    assert auth["active_mode"] == "client_credentials"
    assert auth["endpoint_family"] == "client"
    assert isinstance(auth["configured"], bool)
    assert auth["lookup_order"][0] == ".env.local"
    assert auth["lookup_order"][-1] == "environment"
    # Nothing writes a user-auth state for this provider, so doctor reports only the token cache.
    assert "state" not in auth and "state_path" not in auth
    assert auth["token_cache_path"].endswith("providers/blizzard-api-client-credentials.json")
    assert auth["token_cache"]["has_access_token"] is False
    capabilities = payload["data"]["capabilities"]
    assert capabilities["doctor"] == "ready"
    assert capabilities["search"] == "coming_soon"
    assert capabilities["resolve"] == "coming_soon"
    assert capabilities["game_data"] == "ready"
    assert capabilities["profile"] == "ready"
    region = payload["data"]["region"]
    assert region["routing"] == "ready"
    assert region["configured"] is None
    assert region["default"] == "us"
    assert region["supported_regions"] == ["us", "eu", "kr", "tw", "cn"]
    # Routing is live-confirmed everywhere except CN, whose hosts are unreachable from outside China.
    assert region["verification"]["retail"] == "live_confirmed"
    assert region["verification"]["classic"] == "live_confirmed"
    assert region["verification"]["verified_regions"] == ["eu", "kr", "tw", "us"]
    assert region["verification"]["unverified_regions"] == ["cn"]
    assert "CN routing" in region["verification"]["note"]
    assert payload["data"]["notes"]


def test_doctor_surfaces_configured_region(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BLIZZARD_REGION", "eu")
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["region"]["configured"] == "eu"


def test_load_blizzard_auth_config_reads_env_local(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    env_local = tmp_path / ".env.local"
    env_local.write_text("BLIZZARD_CLIENT_ID=abc123\nBLIZZARD_CLIENT_SECRET=shhh\n")

    config = load_blizzard_auth_config(start_dir=str(tmp_path))

    assert config.client_id == "abc123"
    assert config.client_secret == "shhh"
    assert config.configured is True
    assert config.credential_source == str(env_local)
    # Reading the env file must not leak the credentials into the process environment.
    assert "BLIZZARD_CLIENT_ID" not in os.environ


def test_load_blizzard_auth_config_unconfigured_without_credentials(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    config = load_blizzard_auth_config(start_dir=str(tmp_path))

    assert config.client_id is None
    assert config.client_secret is None
    assert config.configured is False


def test_provider_env_file_overrides_process_credentials(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Documented precedence: provider env file beats stale process-environment credentials.
    monkeypatch.setenv("BLIZZARD_CLIENT_ID", "from-process")
    monkeypatch.setenv("BLIZZARD_CLIENT_SECRET", "from-process")
    provider_env = _write_provider_env(tmp_path, "BLIZZARD_CLIENT_ID=from-provider\nBLIZZARD_CLIENT_SECRET=provider-secret\n")

    config = load_blizzard_auth_config(start_dir=str(tmp_path))

    assert config.client_id == "from-provider"
    assert config.client_secret == "provider-secret"
    assert config.credential_source == provider_env


def test_credentials_come_from_the_first_layer_that_holds_both_halves(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A stale lone ID in .env.local used to be paired with the provider file's secret, so every
    # request authenticated as a client that does not exist.
    (tmp_path / ".env.local").write_text("BLIZZARD_CLIENT_ID=stale-id\n")
    provider_env = _write_provider_env(tmp_path, "BLIZZARD_CLIENT_ID=good-id\nBLIZZARD_CLIENT_SECRET=good-secret\n")

    config = load_blizzard_auth_config(start_dir=str(tmp_path))

    assert (config.client_id, config.client_secret, config.credential_source) == ("good-id", "good-secret", provider_env)


def test_halves_split_across_layers_are_not_a_credential_pair(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".env.local").write_text("BLIZZARD_CLIENT_ID=local-id\n")
    _write_provider_env(tmp_path, "BLIZZARD_CLIENT_SECRET=provider-secret\n")

    config = load_blizzard_auth_config(start_dir=str(tmp_path))

    assert config.configured is False
    assert config.credential_source is None


def test_doctor_reads_region_from_provider_env_file(tmp_path) -> None:
    _write_provider_env(tmp_path, "BLIZZARD_REGION=kr\n")

    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["region"]["configured"] == "kr"


def test_doctor_credential_source_is_environment_when_only_process_has_creds(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Credentials come from the process environment; a provider env file exists but only sets region.
    # credential_source must name the process environment, not the unrelated file.
    monkeypatch.setenv("BLIZZARD_CLIENT_ID", "proc-id")
    monkeypatch.setenv("BLIZZARD_CLIENT_SECRET", "proc-secret")
    _write_provider_env(tmp_path, "BLIZZARD_REGION=tw\n")

    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["auth"]["configured"] is True
    assert payload["data"]["auth"]["credential_source"] == "environment"
    assert payload["data"]["region"]["configured"] == "tw"

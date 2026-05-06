from __future__ import annotations

import json
from pathlib import Path

from warcraft_core.auth import delete_provider_auth_state, load_provider_auth_state, provider_auth_status, save_provider_auth_state
from warcraft_core.paths import provider_env_path, provider_state_path


def test_provider_env_and_state_paths_follow_xdg(monkeypatch, tmp_path: Path) -> None:
    config_home = tmp_path / "config"
    state_home = tmp_path / "state"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))

    assert provider_env_path("warcraftlogs") == config_home / "warcraft" / "providers" / "warcraftlogs.env"
    assert provider_state_path("warcraftlogs") == state_home / "warcraft" / "providers" / "warcraftlogs.json"


def test_provider_auth_status_reports_missing_state(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

    payload = provider_auth_status("warcraftlogs", now=1000.0)

    assert payload["exists"] is False
    assert payload["readable"] is False
    assert payload["valid_json"] is False
    assert payload["auth_mode"] is None
    assert payload["expired"] is None


def test_provider_auth_status_reports_token_state(monkeypatch, tmp_path: Path) -> None:
    state_file = tmp_path / "warcraftlogs.json"
    state_file.write_text(
        json.dumps(
            {
                "auth_mode": "authorization_code",
                "access_token": "abc123",
                "refresh_token": "refresh123",
                "expires_at": 1500.0,
            }
        )
    )

    payload = provider_auth_status("warcraftlogs", path=state_file, now=1000.0)

    assert payload["exists"] is True
    assert payload["readable"] is True
    assert payload["valid_json"] is True
    assert payload["auth_mode"] == "authorization_code"
    assert payload["has_access_token"] is True
    assert payload["has_refresh_token"] is True
    assert payload["expires_at"] == 1500.0
    assert payload["expired"] is False


def test_load_provider_auth_state_returns_dict_payload(tmp_path: Path) -> None:
    state_file = tmp_path / "warcraftlogs.json"
    state_file.write_text(json.dumps({"auth_mode": "pkce", "access_token": "token"}))

    payload = load_provider_auth_state("warcraftlogs", path=state_file)

    assert payload == {"auth_mode": "pkce", "access_token": "token"}


def test_load_provider_auth_state_returns_none_for_malformed_json(tmp_path: Path) -> None:
    state_file = tmp_path / "warcraftlogs.json"
    state_file.write_text("{not-valid-json")

    assert load_provider_auth_state("warcraftlogs", path=state_file) is None


def test_load_provider_auth_state_returns_none_for_unreadable_file(tmp_path: Path) -> None:
    state_file = tmp_path / "warcraftlogs.json"
    state_file.write_text(json.dumps({"auth_mode": "pkce"}))
    state_file.chmod(0o000)
    try:
        assert load_provider_auth_state("warcraftlogs", path=state_file) is None
    finally:
        state_file.chmod(0o600)


def test_save_and_delete_provider_auth_state_round_trip(tmp_path: Path) -> None:
    state_file = tmp_path / "warcraftlogs.json"

    saved = save_provider_auth_state("warcraftlogs", {"auth_mode": "authorization_code"}, path=state_file)
    deleted = delete_provider_auth_state("warcraftlogs", path=state_file)

    assert saved == state_file
    assert deleted is True
    assert not state_file.exists()

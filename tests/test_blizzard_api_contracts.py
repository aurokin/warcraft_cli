from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import blizzard_api_cli.client as client_module
import httpx
import pytest
import typer
from blizzard_api_cli.client import SUPPORTED_REGIONS, BlizzardClient, verification_note
from blizzard_api_cli.main import app
from blizzard_api_cli.provider import PROVIDER
from typer.testing import CliRunner
from warcraft_core.envelope import ENVELOPE_KEYS, REQUIRED_KEYS, SCHEMA_VERSION, envelope_violations
from warcraft_core.provider import ProviderSurface

runner = CliRunner()

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "blizzard"


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURE_DIR / f"{name}.json").read_text())


class _FakeResponse:
    def __init__(self, payload: Any, url: str) -> None:
        self._payload = payload
        self.request = SimpleNamespace(url=url)
        self.status_code = 200

    def json(self) -> Any:
        return self._payload


def _fixture_for_url(url: str) -> dict[str, Any]:
    if "/data/wow/realm/" in url:
        return _fixture("realm")
    if "/data/wow/item/" in url:
        return _fixture("item")
    if "/profile/wow/character/" in url:
        return _fixture("character")
    raise AssertionError(f"unexpected API url {url}")


@pytest.fixture(autouse=True)
def _isolate_blizzard_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Isolate credential discovery AND shared-state token caching to tmp, so contract tests never
    # read real creds or pollute ~/.local/state, and the cross-instance cache test is deterministic.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BLIZZARD_CLIENT_ID", "test-id")
    monkeypatch.setenv("BLIZZARD_CLIENT_SECRET", "test-secret")
    monkeypatch.delenv("BLIZZARD_REGION", raising=False)


def _install_recorder(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Patch request_with_retries to serve a token for /token POSTs and fixtures for API GETs."""
    token_calls: list[str] = []

    def _fake(client: Any, url: str, *, method: str = "GET", **kwargs: Any) -> _FakeResponse:
        if url.endswith("/token"):
            token_calls.append(url)
            return _FakeResponse({"access_token": "fake-token", "expires_in": 3600, "token_type": "Bearer"}, url)
        return _FakeResponse(_fixture_for_url(url), url)

    monkeypatch.setattr(client_module, "request_with_retries", _fake)
    return token_calls


def test_realm_command_envelope_and_provenance(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_recorder(monkeypatch)
    result = runner.invoke(app, ["realm", "illidan"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["provider"] == "blizzard-api"
    assert payload["command"] == "realm"
    assert payload["kind"] == "realm"
    prov = payload["provenance"]
    assert prov["region"] == "us"
    assert prov["namespace"] == "dynamic-us"
    assert prov["namespace_class"] == "dynamic"
    assert prov["game_version"] == "retail"
    assert prov["verified"] is True
    assert "confirmed against live Blizzard endpoints" in prov["verification_note"]
    assert payload["data"]["slug"] == "illidan"


def test_item_command_uses_static_namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_recorder(monkeypatch)
    result = runner.invoke(app, ["item", "19019"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["provenance"]["namespace"] == "static-us"
    assert payload["data"]["id"] == 19019


def test_character_command_uses_profile_namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_recorder(monkeypatch)
    result = runner.invoke(app, ["character", "illidan", "Imonthegcd"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["provenance"]["namespace"] == "profile-us"
    assert payload["data"]["name"] == "Imonthegcd"


def test_region_option_routes_namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_recorder(monkeypatch)
    result = runner.invoke(app, ["realm", "draenor", "--region", "eu"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["provenance"]["region"] == "eu"
    assert payload["provenance"]["namespace"] == "dynamic-eu"


def test_region_resolves_from_env_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BLIZZARD_REGION", "kr")
    _install_recorder(monkeypatch)
    result = runner.invoke(app, ["realm", "azshara"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["provenance"]["region"] == "kr"
    assert payload["provenance"]["namespace"] == "dynamic-kr"


def test_region_alias_normalizes(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_recorder(monkeypatch)
    result = runner.invoke(app, ["realm", "illidan", "--region", "na"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["provenance"]["region"] == "us"


def test_classic_flag_selects_classic_category(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_recorder(monkeypatch)
    result = runner.invoke(app, ["realm", "faerlina", "--classic"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["provenance"]["namespace"] == "dynamic-classic-us"
    assert payload["provenance"]["game_version"] == "classic"


def test_classic_profile_unsupported(monkeypatch: pytest.MonkeyPatch) -> None:
    # --classic on a profile read is a bad flag combination, not an internal failure: usage exit code.
    _install_recorder(monkeypatch)
    result = runner.invoke(app, ["character", "faerlina", "Someone", "--classic"])
    assert result.exit_code == 2
    payload = json.loads(result.stderr)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "classic_profile_unsupported"
    assert payload["error"]["message"] == (
        "The Blizzard Profile API has no classic namespace; character lookups are retail-only."
    )


def test_help_doctor_and_payloads_state_one_verification_posture(monkeypatch: pytest.MonkeyPatch) -> None:
    """`blizzard --help` (and docs/reference/blizzard.md, generated from it), `doctor`, and every
    payload must state the same verification posture, so the help can never call the endpoints
    unverified while the payloads report provenance.verified=true."""
    _install_recorder(monkeypatch)
    help_text = typer.main.get_command(app).help or ""
    doctor = runner.invoke(app, ["doctor"])
    assert doctor.exit_code == 0
    assert any(verification_note() in note for note in json.loads(doctor.stdout)["data"]["notes"])

    for region in SUPPORTED_REGIONS:
        result = runner.invoke(app, ["realm", "illidan", "--region", region])
        assert result.exit_code == 0
        prov = json.loads(result.stdout)["provenance"]
        assert prov["verification_note"] in help_text
        # The boolean and the prose have to agree region by region, so no payload can claim
        # verified=true under the note that says the region is unconfirmed.
        assert prov["verified"] is (prov["verification_note"] == verification_note())


def test_unsupported_region_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    # A mistyped --region is a usage error (exit 2) like everywhere else in the repo, and the message
    # lists the accepted values as plain text, not a Python tuple repr.
    _install_recorder(monkeypatch)
    result = runner.invoke(app, ["realm", "illidan", "--region", "oc"])
    assert result.exit_code == 2
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "unsupported_region"
    assert payload["error"]["message"] == "--region must be one of: us, eu, kr, tw, cn; got 'oc'."


def test_unsupported_game_version_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_recorder(monkeypatch)
    result = runner.invoke(app, ["item", "19019", "--game-version", "classic-era"])
    assert result.exit_code == 2
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "unsupported_game_version"
    assert payload["error"]["message"].startswith("--game-version must be one of: retail, classic; got 'classic-era'.")


@pytest.mark.parametrize("command", ["search", "resolve"])
def test_coming_soon_commands_emit_structured_stub(command: str) -> None:
    # doctor advertises search/resolve as coming_soon, so the commands must exist and emit a
    # structured coming_soon envelope (not Click's "No such command") when a caller probes them.
    result = runner.invoke(app, [command, "illidan"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["provider"] == "blizzard-api"
    assert payload["command"] == command
    assert payload["kind"] == "coming_soon"
    assert payload["data"]["coming_soon"] is True


def test_classic_conflicts_with_explicit_retail(monkeypatch: pytest.MonkeyPatch) -> None:
    # Contradictory flags must error, not silently route to classic: --classic and an explicit
    # --game-version retail cannot both be honored.
    _install_recorder(monkeypatch)
    result = runner.invoke(app, ["realm", "illidan", "--game-version", "retail", "--classic"])
    assert result.exit_code == 2
    payload = json.loads(result.stderr)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "unsupported_game_version"
    assert payload["error"]["message"] == (
        "--classic conflicts with --game-version 'retail'; pass only one "
        "(--classic is shorthand for --game-version classic)."
    )


def test_missing_credentials_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BLIZZARD_CLIENT_ID", raising=False)
    monkeypatch.delenv("BLIZZARD_CLIENT_SECRET", raising=False)
    _install_recorder(monkeypatch)
    result = runner.invoke(app, ["realm", "illidan"])
    assert result.exit_code == 3
    payload = json.loads(result.stderr)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "missing_client_credentials"


def test_http_status_error_no_traceback(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake(client: Any, url: str, *, method: str = "GET", **kwargs: Any) -> _FakeResponse:
        if url.endswith("/token"):
            return _FakeResponse({"access_token": "fake-token", "expires_in": 3600}, url)
        request = httpx.Request("GET", url)
        response = httpx.Response(404, request=request)
        raise httpx.HTTPStatusError("Not Found", request=request, response=response)

    monkeypatch.setattr(client_module, "request_with_retries", _fake)
    result = runner.invoke(app, ["realm", "does-not-exist"])
    assert result.exit_code == 4
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "not_found"
    assert "404" in payload["error"]["message"]


@pytest.mark.parametrize(
    "args",
    [
        ["realm", "illidan"],
        ["item", "19019"],
        ["character", "illidan", "Imonthegcd"],
    ],
)
def test_network_error_no_traceback(monkeypatch: pytest.MonkeyPatch, args: list[str]) -> None:
    def _fake(client: Any, url: str, *, method: str = "GET", **kwargs: Any) -> _FakeResponse:
        if url.endswith("/token"):
            return _FakeResponse({"access_token": "fake-token", "expires_in": 3600}, url)
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(client_module, "request_with_retries", _fake)
    result = runner.invoke(app, args)
    # Transport failures must produce the error envelope on stderr and exit 5, never a traceback
    # and never a partial payload on stdout.
    assert result.exit_code == 5
    assert result.stdout == ""
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "network_error"
    assert envelope_violations(payload) == []


def test_invalid_json_response_no_traceback(monkeypatch: pytest.MonkeyPatch) -> None:
    class _BadResponse(_FakeResponse):
        def json(self) -> Any:
            raise json.JSONDecodeError("no json", "", 0)

    def _fake(client: Any, url: str, *, method: str = "GET", **kwargs: Any) -> _FakeResponse:
        if url.endswith("/token"):
            return _FakeResponse({"access_token": "fake-token", "expires_in": 3600}, url)
        return _BadResponse(None, url)

    monkeypatch.setattr(client_module, "request_with_retries", _fake)
    result = runner.invoke(app, ["realm", "illidan"])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_response"


def test_in_memory_token_reuse_single_post(monkeypatch: pytest.MonkeyPatch) -> None:
    token_calls = _install_recorder(monkeypatch)
    client = BlizzardClient()
    try:
        client.fetch_realm("illidan")
        client.fetch_item(19019)
    finally:
        client.close()
    assert len(token_calls) == 1


def test_shared_state_token_reuse_across_instances(monkeypatch: pytest.MonkeyPatch) -> None:
    # AC4: a fresh client (empty in-memory cache) reuses the persisted token with no second POST.
    token_calls = _install_recorder(monkeypatch)
    runner.invoke(app, ["realm", "illidan"])
    runner.invoke(app, ["item", "19019"])
    assert len(token_calls) == 1
    # A different region key must NOT reuse the cached token (key = sha256(region, id, secret)).
    runner.invoke(app, ["realm", "draenor", "--region", "eu"])
    assert len(token_calls) == 2


def test_locale_option_forwarded(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def _fake(client: Any, url: str, *, method: str = "GET", **kwargs: Any) -> _FakeResponse:
        if url.endswith("/token"):
            return _FakeResponse({"access_token": "fake-token", "expires_in": 3600}, url)
        captured["params"] = kwargs.get("params")
        return _FakeResponse(_fixture_for_url(url), url)

    monkeypatch.setattr(client_module, "request_with_retries", _fake)
    result = runner.invoke(app, ["realm", "illidan", "--locale", "de_DE"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["provenance"]["locale"] == "de_DE"
    assert captured["params"]["locale"] == "de_DE"


def test_token_without_access_token_no_traceback(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake(client: Any, url: str, *, method: str = "GET", **kwargs: Any) -> _FakeResponse:
        if url.endswith("/token"):
            return _FakeResponse({"token_type": "Bearer"}, url)  # 200 JSON, but no access_token
        return _FakeResponse(_fixture_for_url(url), url)

    monkeypatch.setattr(client_module, "request_with_retries", _fake)
    result = runner.invoke(app, ["realm", "illidan"])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_response"


def test_realm_slug_lowercased(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_recorder(monkeypatch)
    result = runner.invoke(app, ["realm", "Illidan"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["provenance"]["source_url"].endswith("/data/wow/realm/illidan")


@pytest.mark.parametrize(
    ("args", "path"),
    [
        (["realm", "Mal'Ganis"], "/data/wow/realm/{}"),
        (["realm", "mal-ganis"], "/data/wow/realm/{}"),
        (["character", "Mal'Ganis", "Aurow"], "/profile/wow/character/{}/aurow"),
    ],
)
def test_realm_display_name_finds_the_blizzard_slug(monkeypatch: pytest.MonkeyPatch, args: list[str], path: str) -> None:
    # Blizzard drops apostrophes ("malganis") but keeps word breaks ("tarren-mill"), so each slug
    # spelling is tried in turn and only a 404 moves on to the next.
    requested: list[str] = []

    def _fake(client: Any, url: str, *, method: str = "GET", **kwargs: Any) -> _FakeResponse:
        if url.endswith("/token"):
            return _FakeResponse({"access_token": "fake-token", "expires_in": 3600}, url)
        requested.append(url)
        if path.format("malganis") not in url:
            request = httpx.Request("GET", url)
            raise httpx.HTTPStatusError("Not Found", request=request, response=httpx.Response(404, request=request))
        return _FakeResponse(_fixture_for_url(url), url)

    monkeypatch.setattr(client_module, "request_with_retries", _fake)
    result = runner.invoke(app, args)

    assert result.exit_code == 0, result.output
    assert [url.split(".api.blizzard.com")[1] for url in requested] == [path.format("mal-ganis"), path.format("malganis")]
    assert json.loads(result.stdout)["provenance"]["source_url"].endswith(path.format("malganis"))


def test_malformed_expires_in_no_traceback(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake(client: Any, url: str, *, method: str = "GET", **kwargs: Any) -> _FakeResponse:
        if url.endswith("/token"):
            return _FakeResponse({"access_token": "fake-token", "expires_in": "soon"}, url)
        return _FakeResponse(_fixture_for_url(url), url)

    monkeypatch.setattr(client_module, "request_with_retries", _fake)
    result = runner.invoke(app, ["realm", "illidan"])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_response"


def test_in_memory_token_keyed_by_region(monkeypatch: pytest.MonkeyPatch) -> None:
    # A single client reused across regions must re-fetch the token: CN uses a different OAuth host
    # from other regions, so reusing the first region's bearer token would be wrong.
    token_urls: list[str] = []

    def _fake(client: Any, url: str, *, method: str = "GET", **kwargs: Any) -> _FakeResponse:
        if url.endswith("/token"):
            token_urls.append(url)
            return _FakeResponse({"access_token": "fake-token", "expires_in": 3600}, url)
        return _FakeResponse(_fixture_for_url(url), url)

    monkeypatch.setattr(client_module, "request_with_retries", _fake)
    client = BlizzardClient()
    try:
        client.fetch_realm("illidan", region="us")
        client.fetch_realm("antonidas", region="cn")
    finally:
        client.close()
    assert len(token_urls) == 2
    assert any("oauth.battle.net" in url for url in token_urls)
    assert any("battlenet.com.cn" in url for url in token_urls)


@pytest.mark.parametrize(
    ("args", "stream"),
    [
        (["doctor"], "stdout"),
        (["search", "illidan"], "stdout"),
        (["resolve", "illidan"], "stdout"),
        (["realm", "illidan"], "stdout"),
        (["item", "19019"], "stdout"),
        (["character", "illidan", "Imonthegcd"], "stdout"),
        (["realm", "illidan", "--region", "oc"], "stderr"),
    ],
)
def test_every_command_emits_a_conforming_envelope(monkeypatch: pytest.MonkeyPatch, args: list[str], stream: str) -> None:
    _install_recorder(monkeypatch)
    result = runner.invoke(app, args)
    payload = json.loads(result.stdout if stream == "stdout" else result.stderr)
    assert envelope_violations(payload) == []
    assert set(payload) == (REQUIRED_KEYS if stream == "stdout" else ENVELOPE_KEYS)
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["provider"] == "blizzard-api"


def test_provider_surface_is_pure_and_experimental() -> None:
    # The wrapper calls PROVIDER in process, so the surface must return envelopes without printing.
    assert isinstance(PROVIDER, ProviderSurface)
    doctor = PROVIDER.doctor()
    assert envelope_violations(doctor) == []
    assert doctor["data"]["tier"] == "experimental"
    assert doctor["data"]["capabilities"]["search"] == "coming_soon"
    for envelope in (PROVIDER.search("illidan", limit=3), PROVIDER.resolve("illidan")):
        assert envelope_violations(envelope) == []
        assert envelope["data"]["coming_soon"] is True


def test_timeout_maps_to_network_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake(client: Any, url: str, *, method: str = "GET", **kwargs: Any) -> _FakeResponse:
        raise httpx.ReadTimeout("timed out")

    monkeypatch.setattr(client_module, "request_with_retries", _fake)
    result = runner.invoke(app, ["item", "19019"])
    assert result.exit_code == 5
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "timeout"


def test_unauthorized_maps_to_auth_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake(client: Any, url: str, *, method: str = "GET", **kwargs: Any) -> _FakeResponse:
        if url.endswith("/token"):
            return _FakeResponse({"access_token": "fake-token", "expires_in": 3600}, url)
        request = httpx.Request("GET", url)
        raise httpx.HTTPStatusError("Forbidden", request=request, response=httpx.Response(403, request=request))

    monkeypatch.setattr(client_module, "request_with_retries", _fake)
    result = runner.invoke(app, ["realm", "illidan"])
    assert result.exit_code == 3
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "auth_failed"
    assert payload["error"]["details"]["status_code"] == 403


def test_global_output_flags_apply(monkeypatch: pytest.MonkeyPatch) -> None:
    # The shared callback gives every binary --fields/--pretty, not just wowhead.
    _install_recorder(monkeypatch)
    result = runner.invoke(app, ["--fields", "data.slug", "realm", "illidan"])
    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"data": {"slug": "illidan"}}


def test_cn_routing_stays_unverified(monkeypatch: pytest.MonkeyPatch) -> None:
    # CN routes through gateway.battlenet.com.cn and a separate OAuth host that cannot be reached
    # from outside China, so it is the one region whose payloads must not claim live confirmation.
    _install_recorder(monkeypatch)
    result = runner.invoke(app, ["item", "19019", "--region", "cn"])
    assert result.exit_code == 0
    prov = json.loads(result.stdout)["provenance"]
    assert prov["region"] == "cn"
    assert prov["verified"] is False
    assert "CN routing" in prov["verification_note"]
    assert "confirmed against live" not in prov["verification_note"]


def test_classic_game_data_routing_is_verified(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_recorder(monkeypatch)
    result = runner.invoke(app, ["item", "19019", "--classic"])
    assert result.exit_code == 0
    prov = json.loads(result.stdout)["provenance"]
    assert prov["namespace"] == "static-classic-us"
    assert prov["verified"] is True

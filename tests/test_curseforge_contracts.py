from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import curseforge_cli.client as client_module
import httpx
import pytest
from curseforge_cli.main import app
from typer.testing import CliRunner

runner = CliRunner()

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "curseforge"


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
    # Order matters: the changelog URL also contains "/v1/mods/", so match it first.
    if "/changelog" in url:
        return _fixture("changelog")
    if "/v1/mods/search" in url:
        return _fixture("search")
    if "/v1/mods/" in url:
        return _fixture("mod")
    raise AssertionError(f"unexpected API url {url}")


@pytest.fixture(autouse=True)
def _isolate_curseforge_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Isolate credential discovery to tmp so contract tests never read a real key or a repo .env.local.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CURSEFORGE_API_KEY", "test-key")


def _install_recorder(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Patch request_with_retries to serve fixtures for every CurseForge GET; record called URLs."""
    calls: list[str] = []

    def _fake(client: Any, url: str, *, method: str = "GET", **kwargs: Any) -> _FakeResponse:
        calls.append(url)
        return _FakeResponse(_fixture_for_url(url), url)

    monkeypatch.setattr(client_module, "request_with_retries", _fake)
    return calls


def test_addon_by_slug_envelope_and_provenance(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_recorder(monkeypatch)
    result = runner.invoke(app, ["addon", "deadly-boss-mods"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["provider"] == "curseforge"
    assert payload["command"] == "addon"
    assert payload["kind"] == "addon"
    prov = payload["provenance"]
    assert prov["game_id"] == 1
    assert prov["mod_id"] == 3358
    assert prov["slug"] == "deadly-boss-mods"
    assert prov["resolved_by"] == "slug_search"
    assert prov["verified"] is False
    assert "pending one-time live confirmation" in prov["verification_note"]
    assert set(prov["source_urls"]) == {"mod", "search", "changelog"}
    data = payload["data"]
    assert data["metadata"]["id"] == 3358
    assert len(data["latest_files"]) == 2
    assert data["changelog"]["file_id"] == 5001
    assert "11.1.0" in data["changelog"]["body"]


def test_addon_by_id_skips_search(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_recorder(monkeypatch)
    result = runner.invoke(app, ["addon", "3358"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["provenance"]["resolved_by"] == "id"
    assert "search" not in payload["provenance"]["source_urls"]
    assert not any("/v1/mods/search" in url for url in calls)


def test_changelog_targets_newest_file(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_recorder(monkeypatch)
    result = runner.invoke(app, ["addon", "3358"])
    assert result.exit_code == 0
    # Newest file is id 5001 (later fileDate), not 4900.
    assert any(url.endswith("/files/5001/changelog") for url in calls)
    assert not any(url.endswith("/files/4900/changelog") for url in calls)


def test_missing_api_key_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CURSEFORGE_API_KEY", raising=False)
    _install_recorder(monkeypatch)
    result = runner.invoke(app, ["addon", "deadly-boss-mods"])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "missing_api_key"


def test_addon_not_found_for_empty_search(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake(client: Any, url: str, *, method: str = "GET", **kwargs: Any) -> _FakeResponse:
        if "/v1/mods/search" in url:
            return _FakeResponse({"data": [], "pagination": {"resultCount": 0}}, url)
        return _FakeResponse(_fixture_for_url(url), url)

    monkeypatch.setattr(client_module, "request_with_retries", _fake)
    result = runner.invoke(app, ["addon", "no-such-addon"])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "addon_not_found"


def test_addon_not_found_for_mod_404(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake(client: Any, url: str, *, method: str = "GET", **kwargs: Any) -> _FakeResponse:
        request = httpx.Request("GET", url)
        response = httpx.Response(404, request=request)
        raise httpx.HTTPStatusError("Not Found", request=request, response=response)

    monkeypatch.setattr(client_module, "request_with_retries", _fake)
    result = runner.invoke(app, ["addon", "999999"])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "addon_not_found"


def test_http_status_error_no_traceback(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake(client: Any, url: str, *, method: str = "GET", **kwargs: Any) -> _FakeResponse:
        request = httpx.Request("GET", url)
        response = httpx.Response(403, request=request)
        raise httpx.HTTPStatusError("Forbidden", request=request, response=response)

    monkeypatch.setattr(client_module, "request_with_retries", _fake)
    result = runner.invoke(app, ["addon", "3358"])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "http_error"
    assert "403" in payload["error"]["message"]


def test_network_error_no_traceback(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake(client: Any, url: str, *, method: str = "GET", **kwargs: Any) -> _FakeResponse:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(client_module, "request_with_retries", _fake)
    result = runner.invoke(app, ["addon", "3358"])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "network_error"


def test_invalid_json_response_no_traceback(monkeypatch: pytest.MonkeyPatch) -> None:
    class _BadResponse(_FakeResponse):
        def json(self) -> Any:
            raise json.JSONDecodeError("no json", "", 0)

    def _fake(client: Any, url: str, *, method: str = "GET", **kwargs: Any) -> _FakeResponse:
        return _BadResponse(None, url)

    monkeypatch.setattr(client_module, "request_with_retries", _fake)
    result = runner.invoke(app, ["addon", "3358"])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_response"


def test_changelog_best_effort_error_marker_on_http_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    # A changelog HTTP failure must NOT fail the whole addon lookup, and must NOT be masked as a silent
    # null: metadata + files still return, with an explicit changelog error marker and no source url.
    def _fake(client: Any, url: str, *, method: str = "GET", **kwargs: Any) -> _FakeResponse:
        if "/changelog" in url:
            request = httpx.Request("GET", url)
            response = httpx.Response(503, request=request)
            raise httpx.HTTPStatusError("Service Unavailable", request=request, response=response)
        return _FakeResponse(_fixture_for_url(url), url)

    monkeypatch.setattr(client_module, "request_with_retries", _fake)
    result = runner.invoke(app, ["addon", "3358"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    changelog = payload["data"]["changelog"]
    assert changelog["file_id"] == 5001
    assert changelog["error"]["code"] == "http_error"
    assert "changelog" not in payload["provenance"]["source_urls"]
    assert payload["data"]["metadata"]["id"] == 3358


def test_changelog_best_effort_marker_on_network_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    # A post-retry network failure on the changelog endpoint also degrades to a marker, not a crash.
    def _fake(client: Any, url: str, *, method: str = "GET", **kwargs: Any) -> _FakeResponse:
        if "/changelog" in url:
            raise httpx.ConnectError("connection refused")
        return _FakeResponse(_fixture_for_url(url), url)

    monkeypatch.setattr(client_module, "request_with_retries", _fake)
    result = runner.invoke(app, ["addon", "3358"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["changelog"]["error"]["code"] == "network_error"


def test_changelog_best_effort_marker_on_malformed_body(monkeypatch: pytest.MonkeyPatch) -> None:
    # A 200 changelog with a non-JSON / non-object body raises invalid_response inside the changelog
    # fetch; that must degrade to a marker, not fail the whole addon lookup.
    class _BadResponse(_FakeResponse):
        def json(self) -> Any:
            raise json.JSONDecodeError("no json", "", 0)

    def _fake(client: Any, url: str, *, method: str = "GET", **kwargs: Any) -> _FakeResponse:
        if "/changelog" in url:
            return _BadResponse(None, url)
        return _FakeResponse(_fixture_for_url(url), url)

    monkeypatch.setattr(client_module, "request_with_retries", _fake)
    result = runner.invoke(app, ["addon", "3358"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["changelog"]["error"]["code"] == "invalid_response"
    assert payload["data"]["metadata"]["id"] == 3358


def test_slug_search_skips_cross_game_duplicate(monkeypatch: pytest.MonkeyPatch) -> None:
    # Same slug across games: a non-WoW row must be skipped in favor of the WoW (gameId==1) row.
    def _fake(client: Any, url: str, *, method: str = "GET", **kwargs: Any) -> _FakeResponse:
        if "/v1/mods/search" in url:
            return _FakeResponse(
                {"data": [
                    {"id": 777, "slug": "deadly-boss-mods", "gameId": 432},
                    {"id": 3358, "slug": "deadly-boss-mods", "gameId": 1},
                ]},
                url,
            )
        return _FakeResponse(_fixture_for_url(url), url)

    monkeypatch.setattr(client_module, "request_with_retries", _fake)
    result = runner.invoke(app, ["addon", "deadly-boss-mods"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["provenance"]["mod_id"] == 3358


def test_slug_search_requires_exact_match(monkeypatch: pytest.MonkeyPatch) -> None:
    # If the search returns rows that do not exactly match the requested slug (e.g. the server ignored
    # the filter), bind nothing rather than the wrong mod.
    def _fake(client: Any, url: str, *, method: str = "GET", **kwargs: Any) -> _FakeResponse:
        if "/v1/mods/search" in url:
            return _FakeResponse({"data": [{"id": 9999, "slug": "some-other-addon", "gameId": 1}]}, url)
        return _FakeResponse(_fixture_for_url(url), url)

    monkeypatch.setattr(client_module, "request_with_retries", _fake)
    result = runner.invoke(app, ["addon", "deadly-boss-mods"])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "addon_not_found"


def test_malformed_search_payload_is_invalid_response(monkeypatch: pytest.MonkeyPatch) -> None:
    # A non-list `data` (schema drift / error wrapped in data) is invalid_response, NOT addon_not_found.
    def _fake(client: Any, url: str, *, method: str = "GET", **kwargs: Any) -> _FakeResponse:
        if "/v1/mods/search" in url:
            return _FakeResponse({"data": {"error": "unexpected"}}, url)
        return _FakeResponse(_fixture_for_url(url), url)

    monkeypatch.setattr(client_module, "request_with_retries", _fake)
    result = runner.invoke(app, ["addon", "deadly-boss-mods"])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_response"


def test_changelog_non_string_body_is_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    # A present-but-non-string changelog `data` is schema drift → explicit marker, not a silent null.
    def _fake(client: Any, url: str, *, method: str = "GET", **kwargs: Any) -> _FakeResponse:
        if "/changelog" in url:
            return _FakeResponse({"data": {"unexpected": "object"}}, url)
        return _FakeResponse(_fixture_for_url(url), url)

    monkeypatch.setattr(client_module, "request_with_retries", _fake)
    result = runner.invoke(app, ["addon", "3358"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["changelog"]["error"]["code"] == "invalid_response"


def test_numeric_id_rejects_non_wow_mod(monkeypatch: pytest.MonkeyPatch) -> None:
    # Numeric ids are global across CurseForge games; a mod from another game must not be returned as a
    # WoW addon (which would still claim provenance.game_id == 1).
    def _fake(client: Any, url: str, *, method: str = "GET", **kwargs: Any) -> _FakeResponse:
        return _FakeResponse({"data": {"id": 12345, "slug": "some-minecraft-mod", "gameId": 432}}, url)

    monkeypatch.setattr(client_module, "request_with_retries", _fake)
    result = runner.invoke(app, ["addon", "12345"])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "addon_not_found"

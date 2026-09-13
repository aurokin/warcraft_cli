"""Every binary answers a transport failure with a JSON error envelope, never a traceback.

The contract lives in docs/foundation/ERROR_CONTRACT.md: ``ok: false`` with an error code and
message, and an exit code from the shared vocabulary (5 for transport/upstream, 1 generic).
These tests force the failure at the HTTP seam so no request ever leaves the process.
"""

from __future__ import annotations

import json
import os
from typing import Any

import curl_cffi.requests
import httpx
import pytest
import typer
import warcraft_api.http
import wowprogress_cli.client
from cli_testkit import all_cli_apps, run_binary, walk_commands
from warcraft_core.envelope import envelope_violations
from warcraft_core.exit_codes import EXIT_NETWORK, exit_code_for

CLI_APPS = all_cli_apps()

# (binary, args, extra env). Credentials are dummies: the request never leaves the process, but the
# command has to get past its own auth precondition to reach the transport layer.
NETWORK_CASES: list[tuple[str, list[str], dict[str, str]]] = [
    ("wowhead", ["search", "thunderfury"], {}),
    ("method", ["search", "mistweaver monk"], {}),
    ("icy-veins", ["search", "mistweaver monk"], {}),
    ("raiderio", ["search", "liquid"], {}),
    ("warcraft-wiki", ["search", "api"], {}),
    ("lorrgs", ["specs"], {}),
    ("raidbots", ["inspect-report", "abcdefghijkl"], {}),
    ("warcraftlogs", ["zones"], {"WARCRAFTLOGS_CLIENT_ID": "x", "WARCRAFTLOGS_CLIENT_SECRET": "y"}),
    ("blizzard", ["realm", "illidan"], {"BLIZZARD_CLIENT_ID": "x", "BLIZZARD_CLIENT_SECRET": "y"}),
    ("curseforge", ["addon", "123"], {"CURSEFORGE_API_KEY": "x"}),
    ("wowprogress", ["guild", "us", "illidan", "Liquid"], {}),
]
CASE_IDS = [f"{binary}-{args[0]}" for binary, args, _ in NETWORK_CASES]

# wowprogress reaches the network through curl_cffi (libcurl), not httpx, so it needs its own seam.
CURL_TRANSPORT_BINARIES = frozenset({"wowprogress"})
# These read sitemaps and article pages, so a non-JSON body is a normal response for them: it
# yields zero results, not an error. "The body is not JSON" is only a failure mode for JSON APIs.
MARKUP_RESPONSE_BINARIES = frozenset({"method", "icy-veins", "wowprogress"})


def _no_sleep(*args: Any, **kwargs: Any) -> None:
    """Retry backoff must not slow the suite down; the failure is deterministic anyway."""


def _install_httpx_failure(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    def send(self: httpx.Client, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        if mode == "connect_error":
            raise httpx.ConnectError("offline", request=request)
        if mode == "http_status_error":
            # 400 is not in warcraft_api.http.RETRYABLE_STATUS_CODES, so it surfaces immediately.
            return httpx.Response(400, request=request, content=b"{}")
        return httpx.Response(
            200, request=request, content=b"<html>not json", headers={"content-type": "application/json"}
        )

    monkeypatch.setattr(httpx.Client, "send", send)


def _install_curl_failure(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    def get(self: Any, url: str, *args: Any, **kwargs: Any) -> Any:
        if mode == "connect_error":
            raise curl_cffi.requests.errors.RequestsError("offline")
        return curl_cffi.requests.Response()

    if mode == "http_status_error":
        def get(self: Any, url: str, *args: Any, **kwargs: Any) -> Any:  # noqa: F811
            response = curl_cffi.requests.Response()
            response.status_code = 400
            response.url = url
            response.content = b"nope"
            return response

    monkeypatch.setattr(curl_cffi.requests.Session, "get", get)
    monkeypatch.setattr(wowprogress_cli.client.time, "sleep", _no_sleep)


def _first_json_object(text: str) -> dict[str, Any] | None:
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def test_every_error_contract_case_names_a_real_command() -> None:
    """Fail loudly here rather than silently stop testing a renamed command."""
    for binary, args, _ in NETWORK_CASES:
        paths = {path[0] for path, _ in walk_commands(typer.main.get_command(CLI_APPS[binary]))}
        assert args[0] in paths, f"{binary} has no {args[0]!r} command; update NETWORK_CASES."


@pytest.mark.parametrize(("binary", "args", "env"), NETWORK_CASES, ids=CASE_IDS)
@pytest.mark.parametrize("mode", ["connect_error", "http_status_error", "invalid_json"])
def test_transport_failure_emits_an_error_envelope(
    binary: str, args: list[str], env: dict[str, str], mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    if mode == "invalid_json" and binary in MARKUP_RESPONSE_BINARIES:
        pytest.skip(f"{binary} reads markup, so a non-JSON body is not a failure")
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(warcraft_api.http.time, "sleep", _no_sleep)
    if binary in CURL_TRANSPORT_BINARIES:
        _install_curl_failure(monkeypatch, mode)
    else:
        _install_httpx_failure(monkeypatch, mode)

    result = run_binary(binary, args)

    assert "Traceback" not in result.stdout + result.stderr
    payload = _first_json_object(result.stderr) or _first_json_object(result.stdout)
    assert payload is not None, f"{binary}: no JSON envelope in output\nstdout={result.stdout}\nstderr={result.stderr}"
    assert not envelope_violations(payload), f"{binary} {mode}: {envelope_violations(payload)}"
    assert payload["ok"] is False
    code = payload["error"]["code"]
    assert code and payload["error"]["message"]
    # The exit code an agent branches on must agree with the error code it reads.
    assert result.exit_code == exit_code_for(code), f"{binary} {mode}: exit {result.exit_code} for code {code!r}"
    if mode == "connect_error":
        assert result.exit_code == EXIT_NETWORK, f"{binary}: an unreachable host must exit {EXIT_NETWORK}, got {payload}"


def test_wrapper_search_reports_every_provider_failure_as_an_error_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """A crashing provider must appear as ``ok: false`` with an error, never as ``payload: null``."""
    monkeypatch.setattr(warcraft_api.http.time, "sleep", _no_sleep)
    _install_httpx_failure(monkeypatch, "connect_error")
    _install_curl_failure(monkeypatch, "connect_error")

    result = run_binary("warcraft", ["search", "thunderfury"])

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    rows = payload["providers"]
    assert rows, "the wrapper fanned out to no provider at all"
    # Providers that answer free text without a request (warcraftlogs, wowprogress) stay ok; if none
    # of the rest failed, the transport was not actually broken and this test proves nothing.
    assert any(not row["ok"] for row in rows), "no provider reached the broken transport"
    for row in rows:
        assert isinstance(row["payload"], dict), f"{row['provider']}: payload must be an envelope, not {row['payload']!r}"
        if row["ok"]:
            continue
        assert row["error"]["code"], f"{row['provider']}: a failed row must carry an error code"


def test_wrapper_doctor_is_offline() -> None:
    """``warcraft doctor`` is discovery, so it must answer without touching any provider endpoint."""
    result = run_binary("warcraft", ["doctor"])
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout)["wrapper"]["provider_count"] == len(CLI_APPS) - 1


def test_forced_failures_leave_no_cache_files(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed fetch must never be written to the shared cache root.

    The cache is switched on for this test (conftest disables it everywhere else), so an empty
    cache root is evidence that nothing was persisted, not just that caching was off.
    """
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setenv("WOWHEAD_CACHE_BACKEND", "file")
    monkeypatch.setattr(warcraft_api.http.time, "sleep", _no_sleep)
    _install_httpx_failure(monkeypatch, "connect_error")
    run_binary("wowhead", ["search", "thunderfury"])
    assert not [path for path in tmp_path.rglob("*") if path.is_file()], sorted(os.listdir(tmp_path))

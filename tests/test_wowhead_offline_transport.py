"""Every network-touching wowhead command must answer transport failures with an error envelope.

The transport seam is ``WowheadClient._request_with_retries``: every live call funnels through it,
so patching it proves the command wraps its client calls instead of letting httpx escape as a
traceback. Commands are invoked through ``CliRunner``, which bypasses ``run()``/``guarded_run``,
so these tests also prove the in-command handling is what produces the envelope.
"""

from __future__ import annotations

import json

import httpx
import pytest
from typer.testing import CliRunner
from warcraft_core.provider import ProviderError
from wowhead_cli.main import app
from wowhead_cli.provider import transport_errors
from wowhead_cli.wowhead_client import WowheadClient

runner = CliRunner()

NETWORK_COMMANDS: tuple[tuple[str, list[str]], ...] = (
    ("search", ["search", "thunderfury"]),
    ("resolve", ["resolve", "thunderfury"]),
    ("entity", ["entity", "item", "19019"]),
    ("entity-page", ["entity-page", "item", "19019"]),
    ("comments", ["comments", "item", "19019"]),
    ("compare", ["compare", "item:19019", "item:19020"]),
    ("linked-graph", ["linked-graph", "item", "19019"]),
    ("guide", ["guide", "1"]),
    ("guide-full", ["guide-full", "1"]),
    ("news", ["news"]),
    ("blue-tracker", ["blue-tracker"]),
    ("news-post", ["news-post", "https://www.wowhead.com/news/example-1"]),
    ("blue-topic", ["blue-topic", "https://www.wowhead.com/blue-tracker/topic/us/1"]),
    ("guides", ["guides", "classes"]),
    ("talent-calc", ["talent-calc", "https://www.wowhead.com/talent-calc/warrior/arms"]),
    ("profession-tree", ["profession-tree", "alchemy"]),
    ("dressing-room", ["dressing-room", "https://www.wowhead.com/dressing-room#x"]),
)


def _raise_connect_error(self: WowheadClient, url: str, *, params: dict[str, object] | None = None) -> httpx.Response:
    raise httpx.ConnectError("connection refused", request=httpx.Request("GET", url))


def _raise_not_found(self: WowheadClient, url: str, *, params: dict[str, object] | None = None) -> httpx.Response:
    request = httpx.Request("GET", url)
    raise httpx.HTTPStatusError("404 Not Found", request=request, response=httpx.Response(404, request=request))


def _raise_unavailable(self: WowheadClient, url: str, *, params: dict[str, object] | None = None) -> httpx.Response:
    request = httpx.Request("GET", url)
    raise httpx.HTTPStatusError("503", request=request, response=httpx.Response(503, request=request))


def _raise_timeout(self: WowheadClient, url: str, *, params: dict[str, object] | None = None) -> httpx.Response:
    raise httpx.ReadTimeout("timed out", request=httpx.Request("GET", url))


@pytest.mark.parametrize(("name", "argv"), NETWORK_COMMANDS, ids=[row[0] for row in NETWORK_COMMANDS])
def test_connect_error_returns_network_envelope(name: str, argv: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(WowheadClient, "_request_with_retries", _raise_connect_error)
    result = runner.invoke(app, argv)

    assert result.exit_code == 5, result.output
    assert result.stdout == ""
    payload = json.loads(result.stderr)
    assert payload["ok"] is False
    assert payload["provider"] == "wowhead"
    assert payload["schema_version"] == "1"
    assert payload["error"]["code"] == "network_error"


@pytest.mark.parametrize(("name", "argv"), NETWORK_COMMANDS, ids=[row[0] for row in NETWORK_COMMANDS])
def test_upstream_404_returns_not_found_envelope(name: str, argv: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(WowheadClient, "_request_with_retries", _raise_not_found)
    result = runner.invoke(app, argv)

    assert result.exit_code == 4, result.output
    assert result.stdout == ""
    payload = json.loads(result.stderr)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "not_found"
    assert payload["error"]["details"]["status_code"] == 404


@pytest.mark.parametrize(("name", "argv"), NETWORK_COMMANDS, ids=[row[0] for row in NETWORK_COMMANDS])
def test_every_command_reports_an_upstream_failure_the_same_way(
    name: str, argv: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """One mapping for the whole binary: a 5xx is `upstream_error` naming the URL, a timeout is `timeout`."""
    monkeypatch.setattr(WowheadClient, "_request_with_retries", _raise_unavailable)
    unavailable = runner.invoke(app, argv)
    assert unavailable.exit_code == 5, unavailable.output
    error = json.loads(unavailable.stderr)["error"]
    assert error["code"] == "upstream_error"
    assert error["details"]["status_code"] == 503
    assert error["details"]["url"].startswith("https://")

    monkeypatch.setattr(WowheadClient, "_request_with_retries", _raise_timeout)
    timed_out = runner.invoke(app, argv)
    assert timed_out.exit_code == 5, timed_out.output
    assert json.loads(timed_out.stderr)["error"]["code"] == "timeout"


def test_a_429_is_rate_limited() -> None:
    request = httpx.Request("GET", "https://www.wowhead.com/item=19019")
    with pytest.raises(ProviderError) as caught, transport_errors():
        raise httpx.HTTPStatusError("429", request=request, response=httpx.Response(429, request=request))
    assert (caught.value.code, caught.value.exit_code) == ("rate_limited", 5)

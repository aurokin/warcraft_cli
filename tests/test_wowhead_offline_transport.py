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
from wowhead_cli.main import app
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
    assert payload["error"]["code"] in {"network_error", "http_error"}


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

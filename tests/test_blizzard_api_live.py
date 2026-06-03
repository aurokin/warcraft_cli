from __future__ import annotations

import json

import pytest
from article_provider_testkit import require_live
from blizzard_api_cli.client import BlizzardClient
from blizzard_api_cli.main import app
from typer.testing import CliRunner

pytestmark = pytest.mark.live

runner = CliRunner()


def _require_configured() -> None:
    # require_live gates on BLIZZARD_LIVE_TESTS; this additionally skips (never fails) when the
    # OAuth client credentials are not present, so the suite is safe to run without secrets.
    require_live("Blizzard")
    if not BlizzardClient().configured:
        pytest.skip("Set BLIZZARD_CLIENT_ID and BLIZZARD_CLIENT_SECRET to run live Blizzard tests.")


def _payload(args: list[str]) -> dict:
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.stderr or result.stdout
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    return payload


def test_live_blizzard_realm_contract() -> None:
    # This live run is the one-time spike that confirms the retail dynamic host + namespace.
    _require_configured()
    payload = _payload(["realm", "illidan"])
    assert payload["kind"] == "realm"
    assert payload["provenance"]["namespace"] == "dynamic-us"
    assert payload["data"]["slug"] == "illidan"
    assert payload["data"]["id"]


def test_live_blizzard_item_contract() -> None:
    _require_configured()
    payload = _payload(["item", "19019"])
    assert payload["kind"] == "item"
    assert payload["provenance"]["namespace"] == "static-us"
    assert payload["data"]["id"] == 19019
    assert payload["data"]["name"]

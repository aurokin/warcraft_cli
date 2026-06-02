from __future__ import annotations

import json

import pytest
from article_provider_testkit import require_live
from curseforge_cli.client import CurseForgeClient
from curseforge_cli.main import app
from typer.testing import CliRunner

pytestmark = pytest.mark.live

runner = CliRunner()


def _require_configured() -> None:
    # require_live gates on CURSEFORGE_LIVE_TESTS; this additionally skips (never fails) when the API
    # key is not present, so the suite is safe to run without secrets.
    require_live("CurseForge")
    if not CurseForgeClient().configured:
        pytest.skip("Set CURSEFORGE_API_KEY to run live CurseForge tests.")


def test_live_curseforge_addon_contract() -> None:
    # One-time spike: confirms the host, x-api-key auth, slug search, mod, and changelog shapes.
    _require_configured()
    result = runner.invoke(app, ["addon", "deadly-boss-mods"])
    assert result.exit_code == 0, result.stderr or result.stdout
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["kind"] == "addon"
    assert payload["provenance"]["resolved_by"] == "slug_search"
    assert payload["data"]["metadata"]["id"]
    assert isinstance(payload["data"]["latest_files"], list)

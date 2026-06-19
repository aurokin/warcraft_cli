from __future__ import annotations

import json
import os

import pytest
from lorrgs_cli.main import app
from typer.testing import CliRunner

runner = CliRunner()


pytestmark = pytest.mark.live


def _require_live() -> None:
    if os.getenv("LORRGS_LIVE_TESTS") != "1":
        pytest.skip("set LORRGS_LIVE_TESTS=1 to run Lorrgs live contract tests")


def test_lorrgs_live_specs_and_bosses() -> None:
    _require_live()
    specs_result = runner.invoke(app, ["specs"])
    assert specs_result.exit_code == 0
    specs_payload = json.loads(specs_result.stdout)
    assert specs_payload["ok"] is True
    assert any(row.get("full_name_slug") == "mage-frost" for row in specs_payload["data"]["specs"])

    bosses_result = runner.invoke(app, ["bosses"])
    assert bosses_result.exit_code == 0
    bosses_payload = json.loads(bosses_result.stdout)
    assert bosses_payload["ok"] is True
    assert any(row.get("full_name_slug") == "chimaerus-the-undreamt-god" for row in bosses_payload["data"]["bosses"])


def test_lorrgs_live_spec_ranking_info_shape() -> None:
    _require_live()
    result = runner.invoke(app, ["spec-ranking-info", "mage-frost", "chimaerus-the-undreamt-god"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["data"]["spec_slug"] == "mage-frost"
    assert payload["data"]["boss_slug"] == "chimaerus-the-undreamt-god"
    assert isinstance(payload["data"]["updated"], str)


def test_lorrgs_live_current_season_shape() -> None:
    _require_live()
    result = runner.invoke(app, ["current-season"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["query"] == {"season_slug": "current"}
    assert isinstance(payload["data"]["name"], str)
    assert isinstance(payload["data"]["raids"], list)
    assert payload["data"]["raids"]


def test_lorrgs_live_report_overview_accepts_warcraftlogs_url() -> None:
    _require_live()
    url = "https://www.warcraftlogs.com/reports/bG3xDYPqKjLm8XaR?fight=22&type=damage-done"
    result = runner.invoke(app, ["report-overview", url])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["query"]["report_id"] == "bG3xDYPqKjLm8XaR"
    assert payload["query"]["fight_id"] == 22
    assert payload["query"]["report_type"] == "damage-done"
    assert any(row.get("fight_id") == 22 for row in payload["data"]["fights"])

from __future__ import annotations

import json
from unittest.mock import MagicMock

from typer.testing import CliRunner

from wowhead_cli.expansion_profiles import detect_expansion_from_url, parse_entity_from_wowhead_url
from wowhead_cli.main import app

runner = CliRunner()


def test_detect_expansion_from_url_path_and_legacy_subdomain() -> None:
    assert detect_expansion_from_url("https://www.wowhead.com/wotlk/item=19019").key == "wotlk"
    assert detect_expansion_from_url("https://classic.wowhead.com/item=19019").key == "classic"
    assert detect_expansion_from_url("https://www.wowhead.com/mop-classic/item=19019").key == "mop-classic"
    assert detect_expansion_from_url("https://www.wowhead.com/item=19019").key == "retail"


def test_parse_entity_from_wowhead_url() -> None:
    assert parse_entity_from_wowhead_url("https://www.wowhead.com/wotlk/item=19019/thunderfury") == ("item", 19019)


def test_search_auto_detects_expansion_from_entity_url(monkeypatch) -> None:
    monkeypatch.setattr(
        "wowhead_cli.wowhead_client.WowheadClient.search_suggestions",
        lambda self, query: {"search": query, "results": [{"id": 19019, "name": "Thunderfury", "type": "item"}]},
    )
    monkeypatch.setattr(
        "wowhead_cli.main._normalize_search_results",
        lambda results, *, query, expansion: [
            {"id": 19019, "name": "Thunderfury", "entity_type": "item", "url": "https://www.wowhead.com/wotlk/item=19019"}
        ],
    )

    result = runner.invoke(app, ["search", "https://www.wowhead.com/wotlk/item=19019", "--limit", "1"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["expansion"] == "wotlk"
    assert payload["expansion_source"] == "url"
    assert payload["search_url"].startswith("https://www.wowhead.com/wotlk/")


def test_search_keeps_explicit_expansion_flag_over_url(monkeypatch) -> None:
    monkeypatch.setattr(
        "wowhead_cli.wowhead_client.WowheadClient.search_suggestions",
        lambda self, query: {"search": query, "results": []},
    )
    monkeypatch.setattr("wowhead_cli.main._normalize_search_results", lambda results, *, query, expansion: [])

    result = runner.invoke(
        app,
        ["--expansion", "classic", "search", "https://www.wowhead.com/wotlk/item=19019", "--limit", "1"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["expansion"] == "classic"
    assert payload["expansion_source"] == "flag"


def test_entity_url_flag_overrides_type_and_id(monkeypatch) -> None:
    monkeypatch.setattr(
        "wowhead_cli.main.WowheadClient.tooltip",
        lambda self, entity_type, entity_id, data_env=None: {"name": "Thunderfury", "quality": 5},
    )
    monkeypatch.setattr(
        "wowhead_cli.main._entity_page_needs_fetch",
        lambda **kwargs: False,
    )

    result = runner.invoke(
        app,
        [
            "entity",
            "item",
            "1",
            "--url",
            "https://www.wowhead.com/wotlk/item=19019",
            "--no-include-comments",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["expansion"] == "wotlk"
    assert payload["entity"]["id"] == 19019


def test_doctor_reports_expansion_url_policy(monkeypatch) -> None:
    monkeypatch.setattr("wowhead_cli.doctor._probe_search_suggestions", lambda *args, **kwargs: {"ok": True, "skipped": False})
    monkeypatch.setattr("wowhead_cli.doctor._probe_tooltip", lambda *args, **kwargs: {"ok": True, "skipped": False})
    monkeypatch.setattr("wowhead_cli.doctor._probe_entity_page", lambda *args, **kwargs: {"ok": True, "skipped": False})

    result = runner.invoke(app, ["--expansion", "wotlk", "doctor", "--no-live"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    policy = payload["expansion_url_policy"]
    assert policy["ok"] is True
    assert policy["checks"]

from __future__ import annotations

import json

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
    assert parse_entity_from_wowhead_url("https://www.wowhead.com/wotlk/fr/item=19019/thunderfury") == ("item", 19019)


def test_detect_expansion_rejects_non_wowhead_hosts() -> None:
    assert detect_expansion_from_url("https://evilwowhead.com/item=19019") is None


def test_compare_rejects_mixed_expansion_urls(monkeypatch) -> None:
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.tooltip", lambda *args, **kwargs: {"name": "A"})
    monkeypatch.setattr(
        "wowhead_cli.main._fetch_entity_page",
        lambda *args, **kwargs: ("<html></html>", {"canonical_url": "https://example.test", "title": "T"}),
    )
    result = runner.invoke(
        app,
        [
            "compare",
            "https://www.wowhead.com/wotlk/item=1",
            "https://www.wowhead.com/classic/item=2",
            "--comment-sample",
            "0",
            "--max-links-per-entity",
            "1",
        ],
    )
    assert result.exit_code != 0


def test_compare_routes_off_a_url_in_any_argument_position(monkeypatch) -> None:
    """A bare `<type>:<id>` ref first must not strip the expansion a later URL names."""
    calls: list[str] = []

    def fake_tooltip(self, entity_type: str, entity_id: int, data_env=None):  # noqa: ANN001, ANN202
        calls.append(f"{self.expansion.key}:{entity_type}:{entity_id}")
        return {"name": f"{entity_type} {entity_id}"}

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.tooltip", fake_tooltip)
    monkeypatch.setattr(
        "wowhead_cli.main._fetch_entity_page",
        lambda *args, **kwargs: ("<html></html>", {"canonical_url": "https://example.test", "title": "T"}),
    )
    result = runner.invoke(
        app,
        [
            "compare",
            "item:19019",
            "https://www.wowhead.com/classic/item=19351",
            "--comment-sample",
            "0",
            "--max-links-per-entity",
            "1",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["data"]["expansion"] == "classic"
    assert calls == ["classic:item:19019", "classic:item:19351"]



def test_search_auto_detects_expansion_from_entity_url(monkeypatch) -> None:
    monkeypatch.setattr(
        "wowhead_cli.wowhead_client.WowheadClient.search_suggestions",
        lambda self, query: {"search": query, "results": [{"id": 19019, "name": "Thunderfury", "type": "item"}]},
    )
    monkeypatch.setattr(
        "wowhead_cli.provider.normalize_search_results",
        lambda results, *, query, expansion, entity_types=(), rank_bonuses=None: (
            [{"id": 19019, "name": "Thunderfury", "entity_type": "item", "url": "https://www.wowhead.com/wotlk/item=19019"}],
            0,
        ),
    )

    result = runner.invoke(app, ["search", "https://www.wowhead.com/wotlk/item=19019", "--limit", "1"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["data"]["expansion"] == "wotlk"
    assert payload["data"]["expansion_source"] == "url"
    assert payload["data"]["search_url"].startswith("https://www.wowhead.com/wotlk/")


def test_search_keeps_explicit_expansion_flag_over_url(monkeypatch) -> None:
    monkeypatch.setattr(
        "wowhead_cli.wowhead_client.WowheadClient.search_suggestions",
        lambda self, query: {"search": query, "results": []},
    )

    result = runner.invoke(
        app,
        ["--expansion", "classic", "search", "https://www.wowhead.com/wotlk/item=19019", "--limit", "1"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["data"]["expansion"] == "classic"
    assert payload["data"]["expansion_source"] == "flag"


def test_entity_url_flag_overrides_type_and_id(monkeypatch) -> None:
    monkeypatch.setattr(
        "wowhead_cli.main.WowheadClient.tooltip",
        lambda self, entity_type, entity_id, data_env=None: {"name": "Thunderfury", "quality": 5},
    )
    monkeypatch.setattr(
        "wowhead_cli.main.entity_page_needs_fetch",
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
    assert payload["data"]["expansion"] == "wotlk"
    assert payload["data"]["entity"]["id"] == 19019


def test_doctor_reports_expansion_url_policy(monkeypatch) -> None:
    monkeypatch.setattr("wowhead_cli.doctor._probe_search_suggestions", lambda *args, **kwargs: {"ok": True, "skipped": False})
    monkeypatch.setattr("wowhead_cli.doctor._probe_tooltip", lambda *args, **kwargs: {"ok": True, "skipped": False})
    monkeypatch.setattr("wowhead_cli.doctor._probe_entity_page", lambda *args, **kwargs: {"ok": True, "skipped": False})

    result = runner.invoke(app, ["--expansion", "wotlk", "doctor", "--no-live"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    policy = payload["data"]["expansion_url_policy"]
    assert policy["ok"] is True
    assert policy["checks"]

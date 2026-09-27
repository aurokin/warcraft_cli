from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner
from wowhead_cli.expansion_profiles import detect_expansion_from_url, parse_entity_from_wowhead_url
from wowhead_cli.main import app
from wowhead_cli.wowhead_client import WowheadClient

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



def test_search_answers_the_entity_a_url_names_without_searching_upstream(monkeypatch) -> None:
    """Wowhead's suggestions endpoint matches names, so a URL (or "item 19019") finds nothing there."""

    def no_upstream_search(self: WowheadClient, query: str) -> dict[str, object]:
        raise AssertionError(f"searched upstream for {query!r}")

    monkeypatch.setattr("wowhead_cli.wowhead_client.WowheadClient.search_suggestions", no_upstream_search)

    result = runner.invoke(app, ["search", "https://www.wowhead.com/wotlk/item=19019/thunderfury"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)["data"]
    assert data["expansion"] == "wotlk"
    assert data["expansion_source"] == "url"
    assert data["count"] == 1
    row = data["results"][0]
    assert (row["entity_type"], row["id"], row["url"]) == ("item", 19019, "https://www.wowhead.com/wotlk/item=19019")
    assert row["follow_up"]["command"] == "wowhead --expansion wotlk entity item 19019"

    mount = runner.invoke(app, ["search", "https://www.wowhead.com/mount=2"])
    assert json.loads(mount.stdout)["data"]["results"][0]["follow_up"]["command"] == "wowhead entity mount 2"


@pytest.mark.parametrize(
    ("url", "command"),
    [
        (
            "https://www.wowhead.com/guide/classes/warrior/fury/overview-pve-dps",
            "wowhead guide https://www.wowhead.com/guide/classes/warrior/fury/overview-pve-dps",
        ),
        (
            "https://www.wowhead.com/de/news/patch-notes-379201",
            "wowhead news-post https://www.wowhead.com/de/news/patch-notes-379201",
        ),
        (
            "https://www.wowhead.com/blue-tracker/topic/eu/class-tuning-123#post-4",
            "wowhead blue-topic 'https://www.wowhead.com/blue-tracker/topic/eu/class-tuning-123#post-4'",
        ),
        ("https://www.wowhead.com/wotlk/guides/classes", "wowhead --expansion wotlk guides classes"),
        ("https://www.wowhead.com/news?page=2", "wowhead news"),
    ],
)
def test_search_and_resolve_answer_a_page_url_with_the_command_that_reads_it(monkeypatch, url: str, command: str) -> None:
    def no_upstream_search(self: WowheadClient, query: str) -> dict[str, object]:
        raise AssertionError(f"searched upstream for {query!r}")

    monkeypatch.setattr("wowhead_cli.wowhead_client.WowheadClient.search_suggestions", no_upstream_search)

    searched = runner.invoke(app, ["search", url])
    assert searched.exit_code == 0, searched.output
    data = json.loads(searched.stdout)["data"]
    assert data["count"] == 1
    assert data["results"][0]["follow_up"]["command"] == command

    resolved = runner.invoke(app, ["resolve", url])
    assert resolved.exit_code == 0, resolved.output
    assert json.loads(resolved.stdout)["data"]["next_command"] == command


def test_resolve_does_not_answer_a_url_outside_the_entity_type_filter(monkeypatch) -> None:
    def no_upstream_search(self: WowheadClient, query: str) -> dict[str, object]:
        raise AssertionError(f"searched upstream for {query!r}")

    monkeypatch.setattr("wowhead_cli.wowhead_client.WowheadClient.search_suggestions", no_upstream_search)

    result = runner.invoke(app, ["resolve", "--entity-type", "npc", "https://www.wowhead.com/item=19019"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)["data"]
    assert (data["resolved"], data["match"], data["count"]) == (False, None, 0)
    assert data["filters"]["entity_types"] == ["npc"]

    matching = runner.invoke(app, ["resolve", "--entity-type", "item", "https://www.wowhead.com/item=19019"])
    assert json.loads(matching.stdout)["data"]["next_command"] == "wowhead entity item 19019"


def test_search_rejects_a_wowhead_url_no_command_reads(monkeypatch) -> None:
    def no_upstream_search(self: WowheadClient, query: str) -> dict[str, object]:
        raise AssertionError(f"searched upstream for {query!r}")

    monkeypatch.setattr("wowhead_cli.wowhead_client.WowheadClient.search_suggestions", no_upstream_search)

    result = runner.invoke(app, ["search", "https://www.wowhead.com/search?q=thunderfury"])
    assert result.exit_code == 2, result.output
    error = json.loads(result.stderr)["error"]
    assert error["code"] == "invalid_query"
    assert "news-post" in error["message"]


def test_search_sends_free_text_that_mentions_wowhead_upstream(monkeypatch) -> None:
    searched: list[str] = []

    def upstream_search(self: WowheadClient, query: str) -> dict[str, object]:
        searched.append(query)
        return {"search": query, "results": []}

    monkeypatch.setattr("wowhead_cli.wowhead_client.WowheadClient.search_suggestions", upstream_search)

    result = runner.invoke(app, ["search", "items on www.wowhead.com/classic"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["data"]["expansion"] == "retail"
    assert searched


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

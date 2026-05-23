from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner
from wowhead_cli.expansion_profiles import (
    build_comment_replies_url,
    build_entity_url,
    build_search_suggestions_url,
    build_tooltip_url,
    list_profiles,
    resolve_expansion,
)
from wowhead_cli.main import app

runner = CliRunner()

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "expansion_recorded.json"
FIXTURE = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
PROFILE_KEYS = tuple(FIXTURE["profiles"].keys())
EXPANSION_PREFIXES = frozenset(
    profile.path_prefix for profile in list_profiles() if profile.path_prefix
)
ENTITY_REF_RE = re.compile(r"^/(?:([^/]+)/)?([a-z-]+)=(\d+)")


def _make_entity_html(canonical_url: str, link_href: str, comment: dict[str, Any]) -> str:
    comments_payload = json.dumps([comment], separators=(",", ":"))
    return (
        "<html><head>"
        '<meta property="og:title" content="Thunderfury, Blessed Blade of the Windseeker">'
        '<meta name="description" content="Recorded fixture page">'
        f'<link rel="canonical" href="{canonical_url}">'
        "</head><body>"
        f'<a href="{link_href}">Baron Geddon</a>'
        f"<script>var lv_comments0 = {comments_payload};</script>"
        "</body></html>"
    )


def _expected_link_url(link_href: str) -> str:
    match = ENTITY_REF_RE.match(link_href)
    if match is None:
        raise AssertionError(f"Unexpected fixture link href: {link_href}")
    prefix, entity_type, entity_id = match.groups()
    if prefix and prefix in EXPANSION_PREFIXES:
        return f"https://www.wowhead.com/{prefix}/{entity_type}={entity_id}"
    return f"https://www.wowhead.com/{entity_type}={entity_id}"


def _install_recorded_transport(monkeypatch: pytest.MonkeyPatch, expansion_key: str) -> None:
    profile = resolve_expansion(expansion_key)
    profile_data = FIXTURE["profiles"][expansion_key]

    search_url = build_search_suggestions_url(profile)
    tooltip_url = build_tooltip_url(profile, "item", 19019)
    replies_url = build_comment_replies_url(profile)
    page_url = build_entity_url(profile, "item", 19019)

    search_payload = {
        "search": FIXTURE["query"],
        "results": [FIXTURE["search_result"]],
    }
    tooltip_payload = FIXTURE["tooltip"]
    replies_payload = FIXTURE["reply_thread"]
    page_html = _make_entity_html(
        canonical_url=profile_data["canonical_url"],
        link_href=profile_data["link_href"],
        comment=FIXTURE["comment"],
    )

    def fake_get_json(self, url: str, params: dict[str, Any] | None = None, **kwargs):  # noqa: ANN001
        params = params or {}
        if url == search_url:
            assert params == {"q": FIXTURE["query"]}
            return deepcopy(search_payload)
        if url == tooltip_url:
            assert params == {"dataEnv": profile.data_env}
            return deepcopy(tooltip_payload)
        if url == replies_url:
            assert params == {"id": FIXTURE["comment"]["id"]}
            return deepcopy(replies_payload)
        raise AssertionError(f"Unexpected JSON request for {expansion_key}: url={url} params={params}")

    def fake_get_text(self, url: str, params: dict[str, Any] | None = None, **kwargs):  # noqa: ANN001
        assert params in ({}, None)
        if url == page_url:
            return page_html
        raise AssertionError(f"Unexpected text request for {expansion_key}: url={url} params={params}")

    monkeypatch.setattr("wowhead_cli.wowhead_client.WowheadClient._get_json", fake_get_json)
    monkeypatch.setattr("wowhead_cli.wowhead_client.WowheadClient._get_text", fake_get_text)


@pytest.mark.parametrize("expansion_key", PROFILE_KEYS)
def test_recorded_fixture_search_entity_entity_page_comments(
    monkeypatch: pytest.MonkeyPatch,
    expansion_key: str,
) -> None:
    _install_recorded_transport(monkeypatch, expansion_key)
    profile = resolve_expansion(expansion_key)
    profile_data = FIXTURE["profiles"][expansion_key]

    search_result = runner.invoke(
        app,
        ["--expansion", expansion_key, "search", FIXTURE["query"], "--limit", "1"],
    )
    assert search_result.exit_code == 0
    search_payload = json.loads(search_result.stdout)
    assert search_payload["expansion"] == expansion_key
    assert search_payload["results"][0]["url"] == build_entity_url(profile, "item", 19019)

    entity_result = runner.invoke(app, ["--expansion", expansion_key, "entity", "item", "19019"])
    assert entity_result.exit_code == 0
    entity_payload = json.loads(entity_result.stdout)
    assert entity_payload["expansion"] == expansion_key
    assert entity_payload["entity"]["name"] == FIXTURE["tooltip"]["name"]
    assert entity_payload["entity"]["page_url"] == profile_data["canonical_url"]
    assert entity_payload["linked_entities"]["count"] == 1
    assert entity_payload["linked_entities"]["items"][0]["type"] == "npc"

    page_result = runner.invoke(app, ["--expansion", expansion_key, "entity-page", "item", "19019", "--max-links", "5"])
    assert page_result.exit_code == 0
    page_payload = json.loads(page_result.stdout)
    assert page_payload["expansion"] == expansion_key
    assert page_payload["entity"]["page_url"] == profile_data["canonical_url"]
    assert page_payload["linked_entities"]["count"] == 1
    assert page_payload["linked_entities"]["items"][0]["url"] == _expected_link_url(profile_data["link_href"])

    comments_result = runner.invoke(
        app,
        [
            "--expansion",
            expansion_key,
            "comments",
            "item",
            "19019",
            "--limit",
            "1",
            "--hydrate-missing-replies",
        ],
    )
    assert comments_result.exit_code == 0
    comments_payload = json.loads(comments_result.stdout)
    assert comments_payload["expansion"] == expansion_key
    assert comments_payload["entity"]["page_url"] == profile_data["canonical_url"]
    assert comments_payload["counts"]["hydrated_reply_threads"] == 1
    assert comments_payload["comments"][0]["citation_url"] == f'{profile_data["canonical_url"]}#comments:id=342'
    assert comments_payload["comments"][0]["replies"][0]["id"] == 267532

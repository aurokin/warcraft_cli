"""Wowhead expansion routing checks against synthetic (hand-written) fixtures."""

from __future__ import annotations

import json
import re

import pytest
from typer.testing import CliRunner
from wowhead_cli.expansion_profiles import build_entity_url, list_profiles, resolve_expansion
from wowhead_cli.main import app

from tests.article_provider_testkit import WOWHEAD_SYNTHETIC_FIXTURE, install_wowhead_synthetic_transport

runner = CliRunner()

PROFILE_KEYS = tuple(WOWHEAD_SYNTHETIC_FIXTURE["profiles"].keys())
EXPANSION_PREFIXES = frozenset(
    profile.path_prefix for profile in list_profiles() if profile.path_prefix
)
ENTITY_REF_RE = re.compile(r"^/(?:([^/]+)/)?([a-z-]+)=(\d+)")


def _expected_link_url(link_href: str) -> str:
    match = ENTITY_REF_RE.match(link_href)
    if match is None:
        raise AssertionError(f"Unexpected fixture link href: {link_href}")
    prefix, entity_type, entity_id = match.groups()
    if prefix and prefix in EXPANSION_PREFIXES:
        return f"https://www.wowhead.com/{prefix}/{entity_type}={entity_id}"
    return f"https://www.wowhead.com/{entity_type}={entity_id}"


@pytest.mark.parametrize("expansion_key", PROFILE_KEYS)
def test_synthetic_fixture_search_entity_entity_page_comments(
    monkeypatch: pytest.MonkeyPatch,
    expansion_key: str,
) -> None:
    install_wowhead_synthetic_transport(monkeypatch, expansion_key)
    profile = resolve_expansion(expansion_key)
    profile_data = WOWHEAD_SYNTHETIC_FIXTURE["profiles"][expansion_key]

    search_result = runner.invoke(
        app,
        ["--expansion", expansion_key, "search", WOWHEAD_SYNTHETIC_FIXTURE["query"], "--limit", "1"],
    )
    assert search_result.exit_code == 0
    search_payload = json.loads(search_result.stdout)
    assert search_payload["expansion"] == expansion_key
    assert search_payload["results"][0]["url"] == build_entity_url(profile, "item", 19019)

    entity_result = runner.invoke(app, ["--expansion", expansion_key, "entity", "item", "19019"])
    assert entity_result.exit_code == 0
    entity_payload = json.loads(entity_result.stdout)
    assert entity_payload["expansion"] == expansion_key
    assert entity_payload["entity"]["name"] == WOWHEAD_SYNTHETIC_FIXTURE["tooltip"]["name"]
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

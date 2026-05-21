"""Schema snapshot checks for Wowhead CLI outputs (recorded fixtures)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.fixtures.wowhead_output_schemas import (
    COMMENTS_KEYS,
    COMPARE_KEYS,
    ENTITY_KEYS,
    ENTITY_PAGE_KEYS,
    SEARCH_KEYS,
)
from wowhead_cli.main import app

runner = CliRunner()
FIXTURE_PATH = Path(__file__).parent / "fixtures" / "expansion_recorded.json"
FIXTURE = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _assert_schema(payload: dict[str, object], *, required: frozenset[str], label: str) -> None:
    missing = sorted(required - set(payload))
    assert not missing, f"{label} missing keys: {missing}"


def _install_recorded_transport(monkeypatch: pytest.MonkeyPatch, expansion_key: str) -> None:
    from copy import deepcopy

    from wowhead_cli.expansion_profiles import (
        build_comment_replies_url,
        build_entity_url,
        build_search_suggestions_url,
        build_tooltip_url,
        resolve_expansion,
    )

    from tests.test_expansion_recorded_fixtures import _make_entity_html

    profile = resolve_expansion(expansion_key)
    profile_data = FIXTURE["profiles"][expansion_key]
    search_url = build_search_suggestions_url(profile)
    tooltip_url = build_tooltip_url(profile, "item", 19019)
    replies_url = build_comment_replies_url(profile)
    page_url = build_entity_url(profile, "item", 19019)

    search_payload = {"search": FIXTURE["query"], "results": [FIXTURE["search_result"]]}
    tooltip_payload = FIXTURE["tooltip"]
    replies_payload = FIXTURE["reply_thread"]
    page_html = _make_entity_html(
        canonical_url=profile_data["canonical_url"],
        link_href=profile_data["link_href"],
        comment=FIXTURE["comment"],
    )

    def fake_get_json(self, url: str, params: dict | None = None, **kwargs):  # noqa: ANN001
        params = params or {}
        if url == search_url:
            return deepcopy(search_payload)
        if url == tooltip_url:
            return deepcopy(tooltip_payload)
        if url == replies_url:
            return deepcopy(replies_payload)
        raise AssertionError(f"unexpected json url={url}")

    def fake_get_text(self, url: str, params: dict | None = None, **kwargs):  # noqa: ANN001
        if url == page_url:
            return page_html
        raise AssertionError(f"unexpected text url={url}")

    monkeypatch.setattr("wowhead_cli.wowhead_client.WowheadClient._get_json", fake_get_json)
    monkeypatch.setattr("wowhead_cli.wowhead_client.WowheadClient._get_text", fake_get_text)


@pytest.mark.parametrize("expansion_key", tuple(FIXTURE["profiles"].keys()))
def test_wowhead_schema_snapshots_for_recorded_expansion(monkeypatch: pytest.MonkeyPatch, expansion_key: str) -> None:
    _install_recorded_transport(monkeypatch, expansion_key)

    search = json.loads(
        runner.invoke(app, ["--expansion", expansion_key, "search", FIXTURE["query"], "--limit", "1"]).stdout
    )
    _assert_schema(search, required=SEARCH_KEYS, label="search")

    entity = json.loads(runner.invoke(app, ["--expansion", expansion_key, "entity", "item", "19019"]).stdout)
    _assert_schema(entity, required=ENTITY_KEYS, label="entity")

    page = json.loads(
        runner.invoke(app, ["--expansion", expansion_key, "entity-page", "item", "19019", "--max-links", "3"]).stdout
    )
    _assert_schema(page, required=ENTITY_PAGE_KEYS, label="entity-page")

    comments = json.loads(
        runner.invoke(
            app,
            ["--expansion", expansion_key, "comments", "item", "19019", "--limit", "1", "--hydrate-missing-replies"],
        ).stdout
    )
    _assert_schema(comments, required=COMMENTS_KEYS, label="comments")

    compare = json.loads(
        runner.invoke(
            app,
            [
                "--expansion",
                expansion_key,
                "compare",
                "item:19019",
                "item:19019",
                "--comment-sample",
                "0",
                "--max-links-per-entity",
                "3",
            ],
        ).stdout
    )
    _assert_schema(compare, required=COMPARE_KEYS, label="compare")

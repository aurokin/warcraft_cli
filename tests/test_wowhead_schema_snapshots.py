"""Schema snapshot checks for Wowhead CLI outputs (synthetic fixtures)."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner
from wowhead_cli.main import app

from tests.article_provider_testkit import WOWHEAD_SYNTHETIC_FIXTURE, install_wowhead_synthetic_transport
from tests.fixtures.wowhead_output_schemas import (
    COMMENTS_KEYS,
    COMPARE_KEYS,
    ENTITY_ITEM_KEYS,
    ENTITY_PAGE_ITEM_KEYS,
    SEARCH_KEYS,
)

runner = CliRunner()
FIXTURE = WOWHEAD_SYNTHETIC_FIXTURE


def _assert_schema(payload: dict[str, dict[str, object]], *, required: frozenset[str], label: str) -> None:
    missing = sorted(required - set(payload["data"]))
    assert not missing, f"{label} missing keys: {missing}"


@pytest.mark.parametrize("expansion_key", tuple(FIXTURE["profiles"].keys()))
def test_wowhead_schema_snapshots_for_synthetic_expansion(monkeypatch: pytest.MonkeyPatch, expansion_key: str) -> None:
    install_wowhead_synthetic_transport(monkeypatch, expansion_key)

    search = json.loads(
        runner.invoke(app, ["--expansion", expansion_key, "search", FIXTURE["query"], "--limit", "1"]).stdout
    )
    _assert_schema(search, required=SEARCH_KEYS, label="search")

    entity = json.loads(runner.invoke(app, ["--expansion", expansion_key, "entity", "item", "19019"]).stdout)
    _assert_schema(entity, required=ENTITY_ITEM_KEYS, label="entity")

    page = json.loads(
        runner.invoke(app, ["--expansion", expansion_key, "entity-page", "item", "19019", "--max-links", "3"]).stdout
    )
    _assert_schema(page, required=ENTITY_PAGE_ITEM_KEYS, label="entity-page")

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

"""Scoped root discovery preserves the selected provider's actual item identity."""

from __future__ import annotations

import pytest

from tests.e2e.harness import run


@pytest.mark.parametrize("command", ["search", "resolve"])
def test_provider_and_entity_scope_returns_the_known_item(require, command):
    require("wowhead")
    result = run(
        "warcraft", command, "Thunderfury", "--provider", "wowhead",
        "--entity-type", "item", "--limit", "5",
    )
    assert result.data["included_providers"] == ["wowhead"], result.describe()
    assert result.data["filters"] == {"providers": ["wowhead"], "entity_types": ["item"]}
    rows = result.data["results"] if command == "search" else [result.data["match"]]
    assert rows, result.describe()
    assert all(row["provider"] == "wowhead" and row["entity_type"] == "item" for row in rows)
    assert any(row["id"] == 19019 for row in rows), result.describe()
    if command == "resolve":
        assert result.data["resolved"] is True
        assert result.data["match"]["id"] == 19019

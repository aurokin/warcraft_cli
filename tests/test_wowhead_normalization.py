from __future__ import annotations

import json
from unittest.mock import MagicMock

from typer.testing import CliRunner

from wowhead_cli.normalization import (
    ENTITY_PAGE_PAYLOAD_SCHEMA_VERSION,
    ENTITY_PAYLOAD_SCHEMA_VERSION,
    attach_entity_normalization,
    attach_entity_page_normalization,
    build_normalized_item,
)
from wowhead_cli.main import app

runner = CliRunner()


def test_build_normalized_item_from_tooltip() -> None:
    item = build_normalized_item(
        tooltip={
            "name": "Thunderfury, Blessed Blade of the Windseeker",
            "quality": 5,
            "icon": "inv_sword_39",
            "itemLevel": 29,
            "binding": 1,
            "inventoryType": 17,
        }
    )
    assert item is not None
    assert item["name"]["value"].startswith("Thunderfury")
    assert item["name"]["provenance"]["source"] == "tooltip"
    assert item["quality"]["value"] == 5
    assert item["binding"]["value"] == "pickup"


def test_attach_entity_normalization_skips_non_items() -> None:
    payload = {"entity": {"type": "spell", "id": 1}}
    assert "schema_version" not in attach_entity_normalization(payload, entity_type="spell")


def test_entity_command_emits_schema_version_for_items(monkeypatch) -> None:
    monkeypatch.setattr(
        "wowhead_cli.main.WowheadClient.tooltip",
        lambda self, entity_type, entity_id, data_env=None: {
            "name": "Thunderfury",
            "quality": 5,
            "icon": "inv_sword_39",
            "itemLevel": 29,
            "binding": 2,
            "inventoryType": 17,
        },
    )
    monkeypatch.setattr("wowhead_cli.main._entity_page_needs_fetch", lambda **kwargs: False)

    result = runner.invoke(app, ["entity", "item", "19019", "--no-include-comments"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == ENTITY_PAYLOAD_SCHEMA_VERSION
    assert payload["normalized"]["item"]["quality"]["value"] == 5
    assert payload["tooltip"]["quality"] == 5


def test_entity_page_emits_page_backed_normalization(monkeypatch) -> None:
    monkeypatch.setattr(
        "wowhead_cli.main.WowheadClient.entity_page_html",
        lambda self, entity_type, entity_id: (
            '<html><head><meta property="og:title" content="Thunderfury Page">'
            '<link rel="canonical" href="https://www.wowhead.com/item=19019">'
            "</head></html>"
        ),
    )
    monkeypatch.setattr(
        "wowhead_cli.main._resolve_page_fetch_target",
        lambda *args, **kwargs: MagicMock(page_entity_type="item", page_entity_id=19019),
    )
    monkeypatch.setattr(
        "wowhead_cli.main.extract_linked_entities_from_href",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr("wowhead_cli.main.parse_page_meta_json", lambda html: None)

    result = runner.invoke(app, ["entity-page", "item", "19019", "--max-links", "1", "--no-include-gatherer"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == ENTITY_PAGE_PAYLOAD_SCHEMA_VERSION
    assert payload["normalized"]["item"]["name"]["provenance"]["source"] == "page"


def test_cached_entity_payload_preserves_existing_normalization(monkeypatch) -> None:
    cached = {
        "expansion": "retail",
        "entity": {"type": "item", "id": 19019, "name": "Thunderfury"},
        "tooltip": {"quality": 5},
        "schema_version": ENTITY_PAYLOAD_SCHEMA_VERSION,
        "normalized": {
            "item": {
                "page_title": {"value": "Page Title", "provenance": {"source": "page"}},
            }
        },
    }

    class FakeClient:
        def get_cached_entity_response(self, **kwargs):  # noqa: ANN003
            return cached

        def set_cached_entity_response(self, *args, **kwargs):  # noqa: ANN003
            return None

    monkeypatch.setattr("wowhead_cli.main._client", lambda ctx: FakeClient())

    result = runner.invoke(app, ["entity", "item", "19019", "--no-include-comments"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["normalized"]["item"]["page_title"]["value"] == "Page Title"


def test_attach_entity_page_normalization() -> None:
    payload = attach_entity_page_normalization(
        {"entity": {"type": "item", "id": 1}, "page": {"title": "Foo"}},
        entity_type="item",
        page={"title": "Foo"},
    )
    assert payload["schema_version"] == ENTITY_PAGE_PAYLOAD_SCHEMA_VERSION

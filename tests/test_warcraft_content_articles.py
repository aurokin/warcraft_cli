"""Shared article helpers: merged linked entities and bundle loading.

Both surfaces used to lose information silently -- the merge dropped provider-specific identity
payloads, and a directory that is not a bundle surfaced as a raw ``FileNotFoundError``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from warcraft_content.article_bundle import InvalidArticleBundleError, load_article_bundle
from warcraft_content.article_discovery import merge_article_linked_entities
from warcraft_core.provider import ProviderError

ABILITY_IDENTITY = {"kind": "ability_identity", "spell_id": 116670, "ability": "vivify"}


def _page(page_url: str, entities: list[dict[str, Any]]) -> dict[str, Any]:
    return {"guide": {"page_url": page_url}, "linked_entities": entities}


def test_merge_keeps_the_ability_identity_a_page_attached() -> None:
    """``guide`` returns ability_identity, so ``guide-full``/``guide-export`` must too."""
    pages = [
        _page(
            "https://example.invalid/p1",
            [{"type": "spell", "id": 116670, "name": "Vivify", "url": "u", "ability_identity": ABILITY_IDENTITY}],
        )
    ]

    merged = merge_article_linked_entities(pages)

    assert merged == [
        {
            "name": "Vivify",
            "type": "spell",
            "id": 116670,
            "url": "u",
            "ability_identity": ABILITY_IDENTITY,
            "source_urls": ["https://example.invalid/p1"],
        }
    ]


def test_merge_fills_a_missing_identity_from_a_later_page_and_collects_source_urls() -> None:
    pages = [
        _page("https://example.invalid/p1", [{"type": "spell", "id": 116670, "name": None, "url": "u"}]),
        _page(
            "https://example.invalid/p2",
            [{"type": "spell", "id": 116670, "name": "Vivify", "url": "u", "ability_identity": ABILITY_IDENTITY}],
        ),
    ]

    merged = merge_article_linked_entities(pages)

    assert len(merged) == 1
    assert merged[0]["name"] == "Vivify"
    assert merged[0]["ability_identity"] == ABILITY_IDENTITY
    assert merged[0]["source_urls"] == ["https://example.invalid/p1", "https://example.invalid/p2"]


def test_load_article_bundle_rejects_a_directory_without_a_manifest(tmp_path: Path) -> None:
    with pytest.raises(InvalidArticleBundleError) as exc_info:
        load_article_bundle(tmp_path)

    assert exc_info.value.code == "invalid_bundle"
    assert "manifest.json" in exc_info.value.message
    # warcraft_cli's guide-compare catches ValueError per bundle to keep the other bundles going.
    assert isinstance(exc_info.value, ValueError)


def test_load_article_bundle_rejects_a_manifest_that_is_not_a_json_object(tmp_path: Path) -> None:
    (tmp_path / "manifest.json").write_text(json.dumps([1, 2, 3]), encoding="utf-8")

    with pytest.raises(InvalidArticleBundleError) as exc_info:
        load_article_bundle(tmp_path)

    assert exc_info.value.code == "invalid_bundle"


def test_guide_query_on_a_directory_that_is_not_a_bundle_is_not_an_internal_error(tmp_path: Path) -> None:
    """Pointing guide-query at the parent of a bundle used to exit 1 with a raw FileNotFoundError."""
    from method_cli import provider

    with pytest.raises(ProviderError) as exc_info:
        provider.guide_query(str(tmp_path), "anything")

    assert exc_info.value.code == "invalid_bundle"

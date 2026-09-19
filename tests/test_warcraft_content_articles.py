"""Shared article helpers: merged linked entities, bundle loading, and partial-export reporting.

All three surfaces used to lose information silently -- the merge dropped provider-specific identity
payloads, a directory that is not a bundle surfaced as a raw ``FileNotFoundError``, and an export
that failed to fetch some of its pages was written and read back as if it were complete.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from warcraft_content.article_bundle import (
    InvalidArticleBundleError,
    compare_article_bundles,
    load_article_bundle,
    query_article_bundle,
    write_article_bundle,
)
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


FAILED_PAGE = {
    "url": "https://example.invalid/talents",
    "section_slug": "talents",
    "error": {"code": "upstream_error", "message": "502 Bad Gateway"},
}


def _guide_payload(*, failed: bool) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "guide": {"title": "Mistweaver Monk", "page_url": "https://example.invalid/guide"},
        "pages": [
            {
                "guide": {
                    "section_slug": "overview",
                    "section_title": "Overview",
                    "page_url": "https://example.invalid/overview",
                },
                "page": {"title": "Overview", "description": None},
                "article": {
                    "html": "<p>Vivify</p>",
                    "text": "Vivify",
                    "headings": ["Overview"],
                    "sections": [{"title": "Overview", "level": 1, "ordinal": 0, "text": "Vivify", "html": "<p>Vivify</p>"}],
                },
            }
        ],
    }
    if failed:
        payload["failed_pages"] = {"count": 1, "items": [FAILED_PAGE]}
    return payload


def _export(tmp_path: Path, name: str, *, failed: bool) -> Path:
    export_dir = tmp_path / name
    write_article_bundle(_guide_payload(failed=failed), provider="method", export_dir=export_dir)
    return export_dir


def test_article_bundle_export_persists_the_pages_it_could_not_fetch(tmp_path: Path) -> None:
    """A bundle reader can only see that an export is partial if the manifest on disk says so."""
    export_dir = _export(tmp_path, "guide-partial", failed=True)

    manifest = json.loads((export_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["failed_pages"] == {"count": 1, "items": [FAILED_PAGE]}
    assert load_article_bundle(export_dir)["failed_pages"] == [FAILED_PAGE]


def test_guide_query_reports_that_it_answered_from_a_partial_bundle(tmp_path: Path) -> None:
    bundle = load_article_bundle(_export(tmp_path, "guide-partial", failed=True))

    result = query_article_bundle(bundle, query="vivify", limit=5, kinds={"sections"}, section_title_filter=None)

    assert result["failed_pages"] == {"count": 1, "items": [FAILED_PAGE]}


def test_guide_compare_marks_which_of_the_compared_bundles_is_partial(tmp_path: Path) -> None:
    paths = [_export(tmp_path, "guide-partial", failed=True), _export(tmp_path, "guide-complete", failed=False)]

    comparison = compare_article_bundles([(path, load_article_bundle(path)) for path in paths])

    assert [row["failed_page_count"] for row in comparison["bundles"]] == [1, 0]


def test_guide_query_on_a_directory_that_is_not_a_bundle_is_not_an_internal_error(tmp_path: Path) -> None:
    """Pointing guide-query at the parent of a bundle used to exit 1 with a raw FileNotFoundError."""
    from method_cli import provider

    with pytest.raises(ProviderError) as exc_info:
        provider.guide_query(str(tmp_path), "anything")

    assert exc_info.value.code == "invalid_bundle"

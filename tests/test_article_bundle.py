from __future__ import annotations

from datetime import datetime
from pathlib import Path

from warcraft_content.article_bundle import (
    compare_article_bundles,
    default_article_export_dir,
    load_article_bundle,
    query_article_bundle,
    write_article_bundle,
)


def _method_like_payload() -> dict[str, object]:
    return {
        "guide": {
            "slug": "mistweaver-monk",
            "page_url": "https://www.method.gg/guides/mistweaver-monk",
            "section_slug": "introduction",
            "section_title": "Introduction",
            "author": "Tincell",
            "last_updated": "Last Updated: 26th Feb, 2026",
            "patch": "Patch 12.0.1",
            "page_count": 2,
        },
        "page": {
            "title": "Method Mistweaver Monk Guide - Introduction - Midnight 12.0.1",
            "description": "Intro guide",
            "canonical_url": "https://www.method.gg/guides/mistweaver-monk",
        },
        "navigation": {
            "count": 2,
            "items": [
                {"title": "Introduction", "url": "https://www.method.gg/guides/mistweaver-monk",
                    "section_slug": "introduction", "active": True, "ordinal": 1},
                {"title": "Talents", "url": "https://www.method.gg/guides/mistweaver-monk/talents",
                    "section_slug": "talents", "active": False, "ordinal": 2},
            ],
        },
        "pages": [
            {
                "guide": {
                    "slug": "mistweaver-monk",
                    "page_url": "https://www.method.gg/guides/mistweaver-monk",
                    "section_slug": "introduction",
                    "section_title": "Introduction",
                },
                "page": {
                    "title": "Introduction",
                    "description": "Intro page",
                    "canonical_url": "https://www.method.gg/guides/mistweaver-monk",
                },
                "article": {
                    "html": "<h2>Introduction</h2><p>Intro copy</p>",
                    "text": "Introduction Intro copy",
                    "headings": [{"title": "Introduction", "level": 2, "ordinal": 1}],
                    "sections": [{"title": "Introduction", "level": 2, "ordinal": 1, "text": "Intro copy", "html": "<p>Intro copy</p>"}],
                },
            },
            {
                "guide": {
                    "slug": "mistweaver-monk",
                    "page_url": "https://www.method.gg/guides/mistweaver-monk/talents",
                    "section_slug": "talents",
                    "section_title": "Talents",
                },
                "page": {
                    "title": "Talents",
                    "description": "Talent page",
                    "canonical_url": "https://www.method.gg/guides/mistweaver-monk/talents",
                },
                "article": {
                    "html": "<h2>Talents</h2><p>Tea of Serenity</p>",
                    "text": "Talents Tea of Serenity",
                    "headings": [{"title": "Talents", "level": 2, "ordinal": 1}],
                    "sections": [{"title": "Talents", "level": 2, "ordinal": 1, "text": "Tea of Serenity", "html": "<p>Tea of Serenity</p>"}],
                },
            },
        ],
        "linked_entities": {
            "count": 1,
            "items": [
                {"type": "spell", "id": 388020, "name": "Tea of Serenity", "url": "https://www.wowhead.com/spell=388020/tea-of-serenity",
                    "source_urls": ["https://www.method.gg/guides/mistweaver-monk/talents"]},
            ],
        },
        "build_references": {
            "count": 1,
            "items": [
                {
                    "kind": "build_reference",
                    "reference_type": "wowhead_talent_calc_url",
                    "url": "https://www.wowhead.com/talent-calc/monk/mistweaver/ABC123",
                    "label": "Raid Build",
                    "build_code": "ABC123",
                    "build_identity": {
                        "kind": "build_identity",
                        "status": "inferred",
                        "class_spec_identity": {"identity": {"actor_class": "monk", "spec": "mistweaver"}},
                    },
                    "source_urls": ["https://www.method.gg/guides/mistweaver-monk/talents"],
                },
            ],
        },
        "analysis_surfaces": {
            "count": 1,
            "items": [
                {
                    "kind": "guide_analysis_surface",
                    "surface_tags": ["builds_talents", "talent_recommendations"],
                    "confidence": "medium",
                    "source_kind": "title_slug_heuristic",
                    "provider": "method",
                    "content_family": "class_guide",
                    "page_url": "https://www.method.gg/guides/mistweaver-monk/talents",
                    "section_slug": "talents",
                    "section_title": "Talents",
                    "page_title": "Talents",
                    "text_preview": "Talent page copy",
                    "match_reasons": ["keyword:talent"],
                    "citation": {
                        "page_url": "https://www.method.gg/guides/mistweaver-monk/talents",
                        "section_title": "Talents",
                        "page_title": "Talents",
                    },
                }
            ],
        },
    }


def _icy_like_payload() -> dict[str, object]:
    return {
        "guide": {
            "slug": "mistweaver-monk-pve-healing-guide",
            "page_url": "https://www.icy-veins.com/wow/mistweaver-monk-pve-healing-guide",
            "section_slug": "intro",
            "section_title": "General Information",
            "author": "Dhaubbs",
            "last_updated": "Mar 05, 2026",
            "patch": "Midnight (12.0.1)",
            "page_count": 1,
        },
        "page": {
            "title": "Mistweaver Monk Healing Guide - Midnight (12.0.1)",
            "description": "Icy intro guide",
            "canonical_url": "https://www.icy-veins.com/wow/mistweaver-monk-pve-healing-guide",
        },
        "navigation": {
            "count": 3,
            "items": [
                {"title": "Mistweaver Monk Guide", "url": "https://www.icy-veins.com/wow/mistweaver-monk-pve-healing-guide",
                    "section_slug": "intro", "active": True, "ordinal": 1},
                {"title": "Builds and Talents", "url": "https://www.icy-veins.com/wow/mistweaver-monk-pve-healing-spec-builds-talents",
                    "section_slug": "builds-and-talents", "active": False, "ordinal": 2},
                {"title": "Rotation, Cooldowns, and Abilities", "url": "https://www.icy-veins.com/wow/mistweaver-monk-pve-healing-rotation-cooldowns-abilities",
                    "section_slug": "rotation-cooldowns-and-abilities", "active": False, "ordinal": 3},
            ],
        },
        "pages": [
            {
                "guide": {
                    "slug": "mistweaver-monk-pve-healing-guide",
                    "page_url": "https://www.icy-veins.com/wow/mistweaver-monk-pve-healing-guide",
                    "section_slug": "intro",
                    "section_title": "General Information",
                },
                "page": {
                    "title": "Mistweaver Monk Healing Guide - Midnight (12.0.1)",
                    "description": "Icy intro guide",
                    "canonical_url": "https://www.icy-veins.com/wow/mistweaver-monk-pve-healing-guide",
                },
                "article": {
                    "html": "<h2>Mistweaver Monk Overview</h2><p>Healing intro</p>",
                    "text": "Mistweaver Monk Overview Healing intro",
                    "headings": [{"title": "Mistweaver Monk Overview", "level": 2, "ordinal": 1}],
                    "sections": [{"title": "Mistweaver Monk Overview", "level": 2, "ordinal": 1, "text": "Healing intro", "html": "<p>Healing intro</p>"}],
                },
            }
        ],
        "linked_entities": {
            "count": 1,
            "items": [
                {"type": "page", "id": 1, "name": "Builds and Talents", "url": "https://www.icy-veins.com/wow/mistweaver-monk-pve-healing-spec-builds-talents",
                    "source_urls": ["https://www.icy-veins.com/wow/mistweaver-monk-pve-healing-guide"]},
            ],
        },
        "build_references": {
            "count": 0,
            "items": [],
        },
        "analysis_surfaces": {
            "count": 1,
            "items": [
                {
                    "kind": "guide_analysis_surface",
                    "surface_tags": ["overview"],
                    "confidence": "high",
                    "source_kind": "content_family",
                    "provider": "icy-veins",
                    "content_family": "spec_guide",
                    "page_url": "https://www.icy-veins.com/wow/mistweaver-monk-pve-healing-guide",
                    "section_slug": "intro",
                    "section_title": "General Information",
                    "page_title": "Mistweaver Monk Healing Guide - Midnight (12.0.1)",
                    "text_preview": "Healing intro",
                    "match_reasons": ["content_family:spec_guide"],
                    "citation": {
                        "page_url": "https://www.icy-veins.com/wow/mistweaver-monk-pve-healing-guide",
                        "section_title": "General Information",
                        "page_title": "Mistweaver Monk Healing Guide - Midnight (12.0.1)",
                    },
                }
            ],
        },
    }


def _wowhead_like_payload() -> dict[str, object]:
    return {
        "guide": {
            "id": 3143,
            "slug": "frost-death-knight-overview-pve-dps",
            "page_url": "https://www.wowhead.com/guide/classes/death-knight/frost/overview-pve-dps",
            "section_slug": "overview",
            "section_title": "Overview",
            "page_count": 1,
        },
        "page": {
            "title": "Frost Death Knight DPS Guide - Midnight",
            "description": "Wowhead guide",
            "canonical_url": "https://www.wowhead.com/guide/classes/death-knight/frost/overview-pve-dps",
        },
        "navigation": {
            "count": 1,
            "items": [
                {
                    "title": "Overview",
                    "url": "https://www.wowhead.com/guide/classes/death-knight/frost/overview-pve-dps",
                    "section_slug": "overview",
                    "active": True,
                    "ordinal": 1,
                }
            ],
        },
        "pages": [
            {
                "guide": {
                    "slug": "frost-death-knight-overview-pve-dps",
                    "page_url": "https://www.wowhead.com/guide/classes/death-knight/frost/overview-pve-dps",
                    "section_slug": "overview",
                    "section_title": "Overview",
                },
                "page": {
                    "title": "Frost Death Knight DPS Guide - Midnight",
                    "description": "Wowhead guide",
                    "canonical_url": "https://www.wowhead.com/guide/classes/death-knight/frost/overview-pve-dps",
                },
                "article": {
                    "html": "<h2>Overview</h2><p>Welcome to the guide.</p>",
                    "text": "Overview Welcome to the guide.",
                    "headings": [{"title": "Overview", "level": 2, "ordinal": 1}],
                    "sections": [{"title": "Overview", "level": 2, "ordinal": 1, "text": "Welcome to the guide.", "html": "<p>Welcome to the guide.</p>"}],
                },
            }
        ],
        "linked_entities": {
            "count": 1,
            "items": [
                {"type": "spell", "id": 49020, "name": "Obliterate", "url": "https://www.wowhead.com/spell=49020/obliterate",
                    "source_urls": ["https://www.wowhead.com/guide/classes/death-knight/frost/overview-pve-dps"]},
            ],
        },
        "build_references": {
            "count": 1,
            "items": [
                {
                    "kind": "build_reference",
                    "reference_type": "wowhead_talent_calc_url",
                    "url": "https://www.wowhead.com/talent-calc/monk/mistweaver/ABC123",
                    "label": "Raid Build",
                    "build_code": "ABC123",
                    "build_identity": {
                        "kind": "build_identity",
                        "status": "inferred",
                        "class_spec_identity": {"identity": {"actor_class": "monk", "spec": "mistweaver"}},
                    },
                    "source_urls": ["https://www.wowhead.com/guide/classes/death-knight/frost/overview-pve-dps"],
                },
            ],
        },
        "analysis_surfaces": {
            "count": 1,
            "items": [
                {
                    "kind": "guide_analysis_surface",
                    "surface_tags": ["overview"],
                    "confidence": "high",
                    "source_kind": "section_heading",
                    "provider": "wowhead",
                    "content_family": None,
                    "page_url": "https://www.wowhead.com/guide/classes/death-knight/frost/overview-pve-dps",
                    "section_slug": "overview",
                    "section_title": "Overview",
                    "page_title": "Frost Death Knight DPS Guide - Midnight",
                    "text_preview": "Welcome to the guide.",
                    "match_reasons": ["keyword:overview"],
                    "citation": {
                        "page_url": "https://www.wowhead.com/guide/classes/death-knight/frost/overview-pve-dps",
                        "section_title": "Overview",
                        "page_title": "Frost Death Knight DPS Guide - Midnight",
                    },
                }
            ],
        },
    }


def test_default_article_export_dir_uses_provider_root() -> None:
    path = default_article_export_dir("method", "mistweaver-monk", cwd=Path("/tmp/example"))
    assert path == Path("/tmp/example/method_exports/guide-mistweaver-monk")


def test_write_and_query_article_bundle_for_method_shape(tmp_path: Path) -> None:
    export_dir = tmp_path / "method-guide"
    manifest = write_article_bundle(_method_like_payload(), provider="method", export_dir=export_dir)
    assert manifest["counts"] == {
        "pages": 2,
        "sections": 2,
        "navigation_links": 2,
        "linked_entities": 1,
        "build_references": 1,
        "analysis_surfaces": 1,
    }
    assert manifest["files"]["page_files_json"] == "page-files.json"
    # AUR-386: bundles carry a parseable ISO-8601 UTC freshness anchor (export_version unchanged).
    assert manifest["export_version"] == 1
    exported_at = datetime.fromisoformat(manifest["exported_at"].replace("Z", "+00:00"))
    assert exported_at.tzinfo is not None
    bundle = load_article_bundle(export_dir)
    assert bundle["page_files"][0]["section_slug"] == "introduction"
    result = query_article_bundle(
        bundle,
        query="tea serenity",
        limit=5,
        kinds={"sections", "navigation", "linked_entities", "build_references"},
        section_title_filter=None,
    )
    assert result["match_counts"]["linked_entities"] == 1
    assert result["top"][0]["name"] == "Tea of Serenity"
    build_result = query_article_bundle(bundle, query="abc123 mistweaver", limit=5, kinds={"build_references"}, section_title_filter=None)
    assert build_result["match_counts"]["build_references"] == 1
    assert build_result["matches"]["build_references"][0]["build_code"] == "ABC123"
    analysis_result = query_article_bundle(bundle, query="talent recommendations", limit=5,
                                           kinds={"analysis_surfaces"}, section_title_filter=None)
    assert analysis_result["match_counts"]["analysis_surfaces"] == 1
    assert analysis_result["matches"]["analysis_surfaces"][0]["surface_tags"] == ["builds_talents", "talent_recommendations"]


def test_write_and_query_article_bundle_for_icy_shape(tmp_path: Path) -> None:
    export_dir = tmp_path / "icy-guide"
    manifest = write_article_bundle(_icy_like_payload(), provider="icy-veins", export_dir=export_dir)
    assert manifest["provider"] == "icy-veins"
    assert manifest["counts"] == {
        "pages": 1,
        "sections": 1,
        "navigation_links": 3,
        "linked_entities": 1,
        "build_references": 0,
        "analysis_surfaces": 1,
    }
    bundle = load_article_bundle(export_dir)
    result = query_article_bundle(bundle, query="builds talents", limit=5, kinds={
                                  "navigation", "linked_entities"}, section_title_filter=None)
    assert result["match_counts"]["navigation"] >= 1
    assert result["matches"]["navigation"][0]["title"] == "Builds and Talents"


def test_write_article_bundle_supports_article_resource_key(tmp_path: Path) -> None:
    export_dir = tmp_path / "wiki-article"
    payload = {
        "article": {
            "title": "World of Warcraft API",
            "page_url": "https://warcraft.wiki.gg/wiki/World_of_Warcraft_API",
            "section_slug": "world-of-warcraft-api",
            "section_title": "World of Warcraft API",
            "page_count": 1,
        },
        "navigation": {
            "count": 1,
            "items": [
                {"title": "API systems", "url": "https://warcraft.wiki.gg/wiki/World_of_Warcraft_API#API_systems",
                    "section_slug": "API_systems", "active": True, "ordinal": 1},
            ],
        },
        "pages": [
            {
                "article_meta": {
                    "title": "World of Warcraft API",
                    "page_url": "https://warcraft.wiki.gg/wiki/World_of_Warcraft_API",
                    "section_slug": "world-of-warcraft-api",
                    "section_title": "World of Warcraft API",
                },
                "page": {
                    "title": "World of Warcraft API",
                    "description": "Warcraft Wiki API reference",
                    "canonical_url": "https://warcraft.wiki.gg/wiki/World_of_Warcraft_API",
                },
                "article": {
                    "html": "<h2>API systems</h2><p>FrameXML</p>",
                    "text": "API systems FrameXML",
                    "headings": [{"title": "API systems", "level": 2, "ordinal": 1}],
                    "sections": [{"title": "API systems", "level": 2, "ordinal": 1, "text": "FrameXML", "html": "<p>FrameXML</p>"}],
                },
            }
        ],
        "linked_entities": {
            "count": 1,
            "items": [
                {"type": "wiki_article", "id": "UIOBJECT_Frame", "name": "UIOBJECT Frame",
                    "url": "https://warcraft.wiki.gg/wiki/UIOBJECT_Frame", "source_urls": ["https://warcraft.wiki.gg/wiki/World_of_Warcraft_API"]},
            ],
        },
        "build_references": {
            "count": 0,
            "items": [],
        },
        "analysis_surfaces": {
            "count": 0,
            "items": [],
        },
    }
    manifest = write_article_bundle(
        payload,
        provider="warcraft-wiki",
        export_dir=export_dir,
        resource_key="article",
        page_resource_key="article_meta",
    )
    assert manifest["resource_key"] == "article"
    assert manifest["page_resource_key"] == "article_meta"
    assert manifest["article"]["title"] == "World of Warcraft API"
    bundle = load_article_bundle(export_dir)
    assert bundle["manifest"]["article"]["title"] == "World of Warcraft API"
    assert bundle["sections"][0]["title"] == "API systems"


def test_query_article_bundle_normalizes_section_title_filter(tmp_path: Path) -> None:
    export_dir = tmp_path / "method-guide"
    write_article_bundle(_method_like_payload(), provider="method", export_dir=export_dir)
    bundle = load_article_bundle(export_dir)

    result = query_article_bundle(
        bundle,
        query="tea serenity",
        limit=5,
        kinds={"sections"},
        section_title_filter="TALENTS",
    )

    assert result["match_counts"]["sections"] == 1
    assert result["top"][0]["title"] == "Talents"


def test_load_article_bundle_tolerates_missing_page_files_metadata(tmp_path: Path) -> None:
    export_dir = tmp_path / "icy-guide"
    write_article_bundle(_icy_like_payload(), provider="icy-veins", export_dir=export_dir)
    (export_dir / "page-files.json").unlink()

    bundle = load_article_bundle(export_dir)

    assert bundle["page_files"] == []


def test_compare_article_bundles_preserves_additive_surface_and_build_evidence(tmp_path: Path) -> None:
    method_dir = tmp_path / "method-guide"
    icy_dir = tmp_path / "icy-guide"
    wowhead_dir = tmp_path / "wowhead-guide"
    write_article_bundle(_method_like_payload(), provider="method", export_dir=method_dir)
    write_article_bundle(_icy_like_payload(), provider="icy-veins", export_dir=icy_dir)
    write_article_bundle(_wowhead_like_payload(), provider="wowhead", export_dir=wowhead_dir)

    comparison = compare_article_bundles(
        [
            (method_dir, load_article_bundle(method_dir)),
            (icy_dir, load_article_bundle(icy_dir)),
            (wowhead_dir, load_article_bundle(wowhead_dir)),
        ]
    )

    assert comparison["kind"] == "guide_bundle_comparison"
    assert comparison["compared_bundle_count"] == 3
    assert comparison["comparison_scope"] == ["section_evidence", "analysis_surfaces", "build_references"]
    assert comparison["section_evidence"]["matching_rule"] == "exact_normalized_section_title"
    assert comparison["section_evidence"]["shared"] == []
    assert "overview" in comparison["section_evidence"]["partial"]
    overview_section_row = next(
        row for row in comparison["section_evidence"]["items"] if row["section_title_key"] == "overview"
    )
    assert overview_section_row["bundle_count"] == 1
    assert {row["provider"] for row in overview_section_row["bundles"]} == {"wowhead"}
    assert "mistweaver monk overview" in comparison["section_evidence"]["partial"]
    assert comparison["analysis_surface_tags"]["shared"] == []
    assert "overview" in comparison["analysis_surface_tags"]["partial"]
    overview_row = next(row for row in comparison["analysis_surface_tags"]["items"] if row["tag"] == "overview")
    assert overview_row["bundle_count"] == 2
    assert {row["provider"] for row in overview_row["bundles"]} == {"icy-veins", "wowhead"}
    assert any(row["keys"] == ["builds_talents", "talent_recommendations"]
               for row in comparison["analysis_surface_tags"]["unique_by_bundle"])
    build_row = comparison["build_references"]["items"][0]
    assert build_row["build_code"] == "ABC123"
    assert build_row["bundle_count"] == 2
    assert {row["provider"] for row in build_row["bundles"]} == {"method", "wowhead"}

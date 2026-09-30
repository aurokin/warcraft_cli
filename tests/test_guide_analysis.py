from __future__ import annotations

from warcraft_content.guide_analysis import (
    extract_guide_analysis_surfaces,
    extract_section_chunk_analysis_surfaces,
    merge_guide_analysis_surfaces,
)


def test_extract_guide_analysis_surfaces_prefers_explicit_content_family() -> None:
    payload = {
        "guide": {
            "page_url": "https://www.icy-veins.com/wow/mistweaver-monk-pve-healing-stat-priority",
            "section_slug": "mistweaver-monk-pve-healing-stat-priority",
            "section_title": "Stat Priority",
            "content_family": "stat_priority",
        },
        "page": {"title": "Mistweaver Monk Stat Priority"},
        "article": {"text": "Prioritize Critical Strike and Versatility."},
    }

    rows = extract_guide_analysis_surfaces(payload, provider="icy-veins")

    assert rows == [
        {
            "kind": "guide_analysis_surface",
            "surface_tags": ["stat_priority", "stat_context"],
            "confidence": "high",
            "source_kind": "content_family",
            "provider": "icy-veins",
            "content_family": "stat_priority",
            "page_url": "https://www.icy-veins.com/wow/mistweaver-monk-pve-healing-stat-priority",
            "section_slug": "mistweaver-monk-pve-healing-stat-priority",
            "section_title": "Stat Priority",
            "page_title": "Mistweaver Monk Stat Priority",
            "text_preview": "Prioritize Critical Strike and Versatility.",
            "match_reasons": ["content_family:stat_priority", "keyword:stat priority"],
            "citation": {
                "page_url": "https://www.icy-veins.com/wow/mistweaver-monk-pve-healing-stat-priority",
                "section_title": "Stat Priority",
                "page_title": "Mistweaver Monk Stat Priority",
            },
        }
    ]


def test_extract_guide_analysis_surfaces_supports_title_slug_heuristics() -> None:
    payload = {
        "guide": {
            "page_url": "https://www.method.gg/guides/mistweaver-monk/talents",
            "section_slug": "talents",
            "section_title": "Talents",
            "content_family": "class_guide",
        },
        "page": {"title": "Method Mistweaver Monk Guide - Talents"},
        "article": {"text": "Talent page copy."},
    }

    rows = extract_guide_analysis_surfaces(payload, provider="method")

    assert rows[0]["surface_tags"] == ["builds_talents", "talent_recommendations"]
    assert "keyword:talents" in rows[0]["match_reasons"]


def test_merge_guide_analysis_surfaces_dedupes_by_page_and_tag_set() -> None:
    pages = [
        {
            "guide": {"page_url": "https://example.invalid/guide/talents"},
            "analysis_surfaces": [
                {
                    "kind": "guide_analysis_surface",
                    "surface_tags": ["builds_talents", "talent_recommendations"],
                    "match_reasons": ["keyword:talent"],
                }
            ],
        },
        {
            "guide": {"page_url": "https://example.invalid/guide/talents"},
            "analysis_surfaces": [
                {
                    "kind": "guide_analysis_surface",
                    "surface_tags": ["builds_talents", "talent_recommendations"],
                    "match_reasons": ["content_family:spec_builds_talents"],
                    "text_preview": "Raid build page.",
                }
            ],
        },
    ]

    rows = merge_guide_analysis_surfaces(pages)

    assert rows == [
        {
            "kind": "guide_analysis_surface",
            "surface_tags": ["builds_talents", "talent_recommendations"],
            "match_reasons": ["keyword:talent", "content_family:spec_builds_talents"],
            "text_preview": "Raid build page.",
        }
    ]


def test_extract_section_chunk_analysis_surfaces_uses_section_headings_as_high_confidence_evidence() -> None:
    rows = extract_section_chunk_analysis_surfaces(
        provider="wowhead",
        page_url="https://www.wowhead.com/guide/classes/death-knight/frost/overview-pve-dps",
        page_title="Frost Death Knight DPS Guide - Midnight",
        section_chunks=[
            {"ordinal": 1, "title": "Frost Death Knight Overview", "content_text": "Welcome to the guide."},
            {"ordinal": 2, "title": "Best in Slot Gear", "content_text": "Use raid gear."},
        ],
    )

    assert rows[0]["surface_tags"] == ["overview"]
    assert rows[0]["confidence"] == "high"
    assert rows[0]["source_kind"] == "section_heading"
    assert rows[1]["surface_tags"] == ["gear_best_in_slot", "gear_context"]


def test_section_heading_tags_mythic_plus_only_for_mythic_plus_not_the_mythic_raid_difficulty() -> None:
    rows = extract_section_chunk_analysis_surfaces(
        provider="wowhead",
        page_url="https://www.wowhead.com/guide/classes/death-knight/frost/overview-pve-dps",
        page_title="Frost Death Knight DPS Guide - Midnight",
        section_chunks=[
            {"ordinal": 1, "title": "Mythic Raid Tips", "content_text": ""},
            {"ordinal": 2, "title": "Mythic Queen Ansurek Strategy", "content_text": ""},
            {"ordinal": 3, "title": "Mythic+ Tips", "content_text": ""},
            {"ordinal": 4, "title": "M+ Talents", "content_text": ""},
        ],
    )

    assert {row["section_title"]: row["surface_tags"] for row in rows} == {
        "Mythic Raid Tips": ["raid_guide"],
        "Mythic+ Tips": ["mythic_plus"],
        "M+ Talents": ["builds_talents", "talent_recommendations", "mythic_plus"],
    }

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest
from article_provider_testkit import load_fixture_text
from bs4 import BeautifulSoup
from icy_veins_cli.page_parser import classify_guide_slug, parse_guide_page

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "icy_veins"

# Slug -> content family is a pure string classification, so it needs a table, not a captured page.
# The captured pages below all carry assertions about parsed markup.
SLUG_FAMILY_CASES = [
    ("monk-guide", "class_hub"),
    ("healing-guide", "role_guide"),
    ("mistweaver-monk-pve-healing-guide", "spec_guide"),
    ("fury-warrior-pve-dps-easy-mode", "easy_mode"),
    ("mistweaver-monk-leveling-guide", "leveling"),
    ("mistweaver-monk-pvp-guide", "pvp"),
    ("mistweaver-monk-pve-healing-spec-builds-talents", "spec_builds_talents"),
    ("mistweaver-monk-pve-healing-rotation-cooldowns-abilities", "rotation_guide"),
    ("mistweaver-monk-pve-healing-stat-priority", "stat_priority"),
    ("mistweaver-monk-pve-healing-gems-enchants-consumables", "gems_enchants_consumables"),
    ("mistweaver-monk-pve-healing-gear-best-in-slot", "gear_best_in_slot"),
    ("mistweaver-monk-pve-healing-spell-summary", "spell_summary"),
    ("mistweaver-monk-resources", "resources"),
    ("mistweaver-monk-pve-healing-macros-addons", "macros_addons"),
    ("mistweaver-monk-pve-healing-mythic-plus-tips", "mythic_plus_tips"),
    ("mistweaver-monk-pve-healing-simulations", "simulations"),
    ("mistweaver-monk-pve-healing-nerub-ar-palace-raid-guide", "raid_guide"),
    ("mistweaver-monk-the-war-within-pve-guide", "expansion_guide"),
    ("mistweaver-monk-mists-of-pandaria-remix-guide", "special_event_guide"),
    ("latest-class-changes", None),
    ("news-roundup", None),
    ("", None),
]


@pytest.mark.parametrize(("slug", "content_family"), SLUG_FAMILY_CASES)
def test_classify_guide_slug_maps_every_supported_family(slug: str, content_family: str | None) -> None:
    assert classify_guide_slug(slug) == content_family


def test_recorded_class_hub_fixture_navigation_contract() -> None:
    payload = parse_guide_page(
        load_fixture_text(FIXTURE_DIR, "class_hub.html"),
        source_url="https://www.icy-veins.com/wow/monk-guide",
    )

    assert payload["navigation"][0]["section_slug"] == "death-knight-guide"
    assert len(payload["navigation"]) == 13


def test_recorded_spec_guide_fixture_contract_details() -> None:
    payload = parse_guide_page(
        load_fixture_text(FIXTURE_DIR, "spec_guide.html"),
        source_url="https://www.icy-veins.com/wow/mistweaver-monk-pve-healing-guide",
    )

    assert payload["guide"]["author"] == "Dhaubbs"
    assert len(payload["article"]["headings"]) == len({row["title"] for row in payload["article"]["headings"]})
    assert payload["linked_entities"][0]["type"] in {"page", "spell"}


def test_recorded_role_guide_pins_nested_heading_and_section_levels() -> None:
    """The legacy capture is the only proof that h3/h4 sub-headings survive parsing with their level."""
    payload = parse_guide_page(
        load_fixture_text(FIXTURE_DIR, "role_guide.html"),
        source_url="https://www.icy-veins.com/wow/healing-guide",
    )

    assert Counter(row["level"] for row in payload["article"]["headings"]) == {2: 7, 3: 8, 4: 10}
    assert Counter(row["level"] for row in payload["article"]["sections"]) == {2: 7, 3: 8, 4: 10}


def test_recorded_unsupported_page_fixture_contract() -> None:
    payload = parse_guide_page(
        load_fixture_text(FIXTURE_DIR, "unsupported_page.html"),
        source_url="https://www.icy-veins.com/wow/latest-class-changes",
    )

    assert payload["guide"]["slug"] == "latest-class-changes"
    assert payload["guide"]["content_family"] is None
    assert payload["guide"]["supported_surface"] is False
    assert len(payload["article"]["sections"]) >= 1


# Icy Veins rebuilt the WoW guides on an Astro layout in 2026 (new container classes for the family
# switcher, the on-page contents, and the article body). These captures pin the new markup; the
# cases above keep pinning the pre-redesign markup the parser still falls back to.
ASTRO_CASES = [
    ("astro_spec_guide.html", "https://www.icy-veins.com/wow/mistweaver-monk-pve-healing-guide", "spec_guide"),
    ("astro_class_hub.html", "https://www.icy-veins.com/wow/monk-guide", "class_hub"),
    ("astro_role_guide.html", "https://www.icy-veins.com/wow/healing-guide", "role_guide"),
    (
        "astro_spec_builds_talents.html",
        "https://www.icy-veins.com/wow/mistweaver-monk-pve-healing-spec-builds-talents",
        "spec_builds_talents",
    ),
]


@pytest.mark.parametrize(("fixture_name", "source_url", "content_family"), ASTRO_CASES)
def test_astro_layout_fixture_yields_article_content(fixture_name: str, source_url: str, content_family: str) -> None:
    payload = parse_guide_page(load_fixture_text(FIXTURE_DIR, fixture_name), source_url=source_url)

    assert payload["guide"]["content_family"] == content_family
    assert payload["guide"]["author"]
    assert payload["page"]["page_type"] == "guides"
    assert len(payload["article"]["sections"]) >= 1
    assert payload["article"]["text"]
    assert all(section["text"] or section["html"] for section in payload["article"]["sections"])


def test_astro_spec_guide_fixture_has_family_navigation_and_page_toc() -> None:
    payload = parse_guide_page(
        load_fixture_text(FIXTURE_DIR, "astro_spec_guide.html"),
        source_url="https://www.icy-veins.com/wow/mistweaver-monk-pve-healing-guide",
    )

    navigation = payload["navigation"]
    assert len(navigation) >= 2
    assert {row["section_slug"] for row in navigation} >= {
        "mistweaver-monk-leveling-guide",
        "mistweaver-monk-pve-healing-spec-builds-talents",
    }
    active = [row for row in navigation if row["active"]]
    assert [row["section_slug"] for row in active] == ["mistweaver-monk-pve-healing-guide"]
    assert payload["guide"]["section_title"] == active[0]["title"]
    assert len(payload["page_toc"]) >= 2
    assert all(row["anchor"] for row in payload["page_toc"])


def test_astro_class_hub_fixture_keeps_the_class_switcher_as_family_navigation() -> None:
    """The Astro class hub moved the switcher to the header dropdown; it is still the family list."""
    payload = parse_guide_page(
        load_fixture_text(FIXTURE_DIR, "astro_class_hub.html"),
        source_url="https://www.icy-veins.com/wow/monk-guide",
    )

    navigation = payload["navigation"]
    assert [row["section_slug"] for row in navigation[:2]] == ["death-knight-guide", "demon-hunter-guide"]
    assert len(navigation) == 13
    assert [row["section_slug"] for row in navigation if row["active"]] == ["monk-guide"]


def test_astro_spec_guide_never_borrows_the_class_dropdown_as_its_family() -> None:
    """Spec pages carry the class dropdown too; if their switcher drifts, guide-full must not crawl every class hub."""
    soup = BeautifulSoup(load_fixture_text(FIXTURE_DIR, "astro_spec_guide.html"), "html.parser")
    for switcher in soup.select(".table-of-contents"):
        switcher.decompose()

    payload = parse_guide_page(str(soup), source_url="https://www.icy-veins.com/wow/mistweaver-monk-pve-healing-guide")

    assert payload["navigation"] == []


def test_astro_layout_fixture_drops_page_furniture_from_the_article() -> None:
    """The Astro article container wraps the family switcher and the on-page contents; neither is prose."""
    payload = parse_guide_page(
        load_fixture_text(FIXTURE_DIR, "astro_spec_guide.html"),
        source_url="https://www.icy-veins.com/wow/mistweaver-monk-pve-healing-guide",
    )

    assert "Table of Contents" not in payload["article"]["text"]
    assert payload["article"]["intro_text"].startswith("Welcome to our Mistweaver Monk guide")
    assert payload["article"]["intro_text"] not in payload["article"]["text"]


def test_astro_talents_fixture_pins_the_full_heading_outline() -> None:
    """Exact titles and levels, so a parser change that loses nested headings or flattens levels fails."""
    payload = parse_guide_page(
        load_fixture_text(FIXTURE_DIR, "astro_spec_builds_talents.html"),
        source_url="https://www.icy-veins.com/wow/mistweaver-monk-pve-healing-spec-builds-talents",
    )

    assert [(row["title"], row["level"]) for row in payload["article"]["headings"]] == [
        ("Best Midnight Talents for Mistweaver Monk", 2),
        ("Rising Mist Build", 3),
        ("DPS Mythic+ Build", 3),
        ("Delve Soloing", 3),
        ("Master of Harmony Raid Build", 3),
        ("Master of Harmony Mythic+ Build", 3),
        ("Omnium Folio", 2),
        ("Apex Talent: Spiritfont", 2),
        ("Hero Talents", 2),
        ("Conduit of the Celestials", 3),
        ("Master of Harmony", 3),
        ("PvP Talents (War Mode)", 2),
        ("Changelog", 2),
    ]
    # Every heading that has body text under it is its own section: the five build sub-sections are
    # nested two wrappers deep, so a direct-children scan of the article would merge them into the
    # section above. "Changelog" is the only heading with no prose of its own.
    assert [(row["title"], row["level"]) for row in payload["article"]["sections"]] == [
        ("Best Midnight Talents for Mistweaver Monk", 2),
        ("Rising Mist Build", 3),
        ("DPS Mythic+ Build", 3),
        ("Delve Soloing", 3),
        ("Master of Harmony Raid Build", 3),
        ("Master of Harmony Mythic+ Build", 3),
        ("Omnium Folio", 2),
        ("Apex Talent: Spiritfont", 2),
        ("Hero Talents", 2),
        ("Conduit of the Celestials", 3),
        ("Master of Harmony", 3),
        ("PvP Talents (War Mode)", 2),
    ]


def test_astro_talents_fixture_extracts_published_loadout_import_strings() -> None:
    """Icy Veins publishes builds as import strings, not talent-calc links; they are the build evidence."""
    payload = parse_guide_page(
        load_fixture_text(FIXTURE_DIR, "astro_spec_builds_talents.html"),
        source_url="https://www.icy-veins.com/wow/mistweaver-monk-pve-healing-spec-builds-talents",
    )

    builds = payload["build_references"]
    assert [row["label"] for row in builds] == [
        "Mistweaver Raid - Conduit of the Celestials",
        "Mistweaver Mythic+ - Conduit of the Celestials",
        "Mistweaver Delves - Conduit of the Celestials",
    ]
    assert {row["reference_type"] for row in builds} == {"wow_talent_export"}
    assert [row["build_code"] for row in builds] == [row["url"] for row in builds]
    assert builds[0]["build_code"].startswith("C4QAAAAAAAAAAAAAAAAAAAAAA")
    assert all(
        row["source_url"] == "https://www.icy-veins.com/wow/mistweaver-monk-pve-healing-spec-builds-talents"
        for row in builds
    )
    assert builds[0]["source"] == {"provider": "icy-veins", "source": "guide_talent_export_string"}


def test_recorded_fixtures_stay_small() -> None:
    """Captured pages are trimmed per docs/architecture/FIXTURE_MAINTENANCE.md."""
    oversized = {path.name: path.stat().st_size for path in FIXTURE_DIR.glob("*.html") if path.stat().st_size > 100_000}

    assert oversized == {}

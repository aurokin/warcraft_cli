from __future__ import annotations

from pathlib import Path

import pytest
from article_provider_testkit import load_fixture_text
from icy_veins_cli.page_parser import parse_guide_page

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "icy_veins"

RECORDED_CASES = [
    ("class_hub.html", "https://www.icy-veins.com/wow/monk-guide", "monk-guide", "class_hub", "current_page"),
    ("role_guide.html", "https://www.icy-veins.com/wow/healing-guide", "healing-guide", "role_guide", "current_page"),
    (
        "spec_guide.html",
        "https://www.icy-veins.com/wow/mistweaver-monk-pve-healing-guide",
        "mistweaver-monk-pve-healing-guide",
        "spec_guide",
        "family_navigation",
    ),
    (
        "easy_mode.html",
        "https://www.icy-veins.com/wow/fury-warrior-pve-dps-easy-mode",
        "fury-warrior-pve-dps-easy-mode",
        "easy_mode",
        "family_navigation",
    ),
    (
        "leveling.html",
        "https://www.icy-veins.com/wow/mistweaver-monk-leveling-guide",
        "mistweaver-monk-leveling-guide",
        "leveling",
        "family_navigation",
    ),
    (
        "pvp_guide.html",
        "https://www.icy-veins.com/wow/mistweaver-monk-pvp-guide",
        "mistweaver-monk-pvp-guide",
        "pvp",
        "family_navigation",
    ),
    (
        "spec_builds_talents.html",
        "https://www.icy-veins.com/wow/mistweaver-monk-pve-healing-spec-builds-talents",
        "mistweaver-monk-pve-healing-spec-builds-talents",
        "spec_builds_talents",
        "family_navigation",
    ),
    (
        "rotation_guide.html",
        "https://www.icy-veins.com/wow/mistweaver-monk-pve-healing-rotation-cooldowns-abilities",
        "mistweaver-monk-pve-healing-rotation-cooldowns-abilities",
        "rotation_guide",
        "family_navigation",
    ),
    (
        "stat_priority.html",
        "https://www.icy-veins.com/wow/mistweaver-monk-pve-healing-stat-priority",
        "mistweaver-monk-pve-healing-stat-priority",
        "stat_priority",
        "family_navigation",
    ),
    (
        "gems_enchants_consumables.html",
        "https://www.icy-veins.com/wow/mistweaver-monk-pve-healing-gems-enchants-consumables",
        "mistweaver-monk-pve-healing-gems-enchants-consumables",
        "gems_enchants_consumables",
        "family_navigation",
    ),
    (
        "spell_summary.html",
        "https://www.icy-veins.com/wow/mistweaver-monk-pve-healing-spell-summary",
        "mistweaver-monk-pve-healing-spell-summary",
        "spell_summary",
        "family_navigation",
    ),
    (
        "resources.html",
        "https://www.icy-veins.com/wow/mistweaver-monk-resources",
        "mistweaver-monk-resources",
        "resources",
        "family_navigation",
    ),
    (
        "macros_addons.html",
        "https://www.icy-veins.com/wow/mistweaver-monk-pve-healing-macros-addons",
        "mistweaver-monk-pve-healing-macros-addons",
        "macros_addons",
        "family_navigation",
    ),
    (
        "mythic_plus_tips.html",
        "https://www.icy-veins.com/wow/mistweaver-monk-pve-healing-mythic-plus-tips",
        "mistweaver-monk-pve-healing-mythic-plus-tips",
        "mythic_plus_tips",
        "family_navigation",
    ),
    (
        "simulations.html",
        "https://www.icy-veins.com/wow/mistweaver-monk-pve-healing-simulations",
        "mistweaver-monk-pve-healing-simulations",
        "simulations",
        "family_navigation",
    ),
    (
        "raid_guide.html",
        "https://www.icy-veins.com/wow/mistweaver-monk-pve-healing-nerub-ar-palace-raid-guide",
        "mistweaver-monk-pve-healing-nerub-ar-palace-raid-guide",
        "raid_guide",
        "family_navigation",
    ),
    (
        "expansion_guide.html",
        "https://www.icy-veins.com/wow/mistweaver-monk-the-war-within-pve-guide",
        "mistweaver-monk-the-war-within-pve-guide",
        "expansion_guide",
        "family_navigation",
    ),
    (
        "special_event_guide.html",
        "https://www.icy-veins.com/wow/mistweaver-monk-mists-of-pandaria-remix-guide",
        "mistweaver-monk-mists-of-pandaria-remix-guide",
        "special_event_guide",
        "family_navigation",
    ),
]


@pytest.mark.parametrize(("fixture_name", "source_url", "slug", "content_family", "traversal_scope"), RECORDED_CASES)
def test_recorded_family_fixture_contracts(
    fixture_name: str,
    source_url: str,
    slug: str,
    content_family: str,
    traversal_scope: str,
) -> None:
    payload = parse_guide_page(load_fixture_text(FIXTURE_DIR, fixture_name), source_url=source_url)

    assert payload["guide"]["slug"] == slug
    assert payload["guide"]["content_family"] == content_family
    assert payload["guide"]["supported_surface"] is True
    assert payload["guide"]["traversal_scope"] == traversal_scope


def test_recorded_class_hub_fixture_navigation_contract() -> None:
    payload = parse_guide_page(
        load_fixture_text(FIXTURE_DIR, "class_hub.html"),
        source_url="https://www.icy-veins.com/wow/monk-guide",
    )

    assert payload["navigation"][0]["section_slug"] == "death-knight-guide"


def test_recorded_spec_guide_fixture_contract_details() -> None:
    payload = parse_guide_page(
        load_fixture_text(FIXTURE_DIR, "spec_guide.html"),
        source_url="https://www.icy-veins.com/wow/mistweaver-monk-pve-healing-guide",
    )

    assert payload["guide"]["author"] == "Dhaubbs"
    assert len(payload["article"]["headings"]) == len({row["title"] for row in payload["article"]["headings"]})
    assert payload["linked_entities"][0]["type"] in {"page", "spell"}


def test_recorded_role_guide_has_sections() -> None:
    payload = parse_guide_page(
        load_fixture_text(FIXTURE_DIR, "role_guide.html"),
        source_url="https://www.icy-veins.com/wow/healing-guide",
    )

    assert len(payload["article"]["sections"]) >= 1


def test_recorded_unsupported_page_fixture_contract() -> None:
    payload = parse_guide_page(
        load_fixture_text(FIXTURE_DIR, "unsupported_page.html"),
        source_url="https://www.icy-veins.com/wow/latest-class-changes",
    )

    assert payload["guide"]["slug"] == "latest-class-changes"
    assert payload["guide"]["content_family"] is None
    assert payload["guide"]["supported_surface"] is False
    assert len(payload["article"]["sections"]) >= 1

from __future__ import annotations

from pathlib import Path

from article_provider_testkit import load_fixture_text
from method_cli.page_parser import parse_guide_page

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "method"


def test_recorded_class_guide_fixture_contract() -> None:
    payload = parse_guide_page(load_fixture_text(FIXTURE_DIR, "class_guide.html"),
                               source_url="https://www.method.gg/guides/mistweaver-monk")

    assert payload["guide"]["slug"] == "mistweaver-monk"
    assert payload["guide"]["content_family"] == "class_guide"
    assert payload["guide"]["supported_surface"] is True
    assert payload["guide"]["author"] == "Tincell"
    assert payload["guide"]["patch"] == "Patch 12.0.1"
    assert payload["navigation"][0]["active"] is True
    assert payload["linked_entities"][0]["id"] == 116670


def test_recorded_profession_guide_fixture_contract() -> None:
    payload = parse_guide_page(
        load_fixture_text(FIXTURE_DIR, "profession_guide.html"),
        source_url="https://www.method.gg/guides/midnight-alchemy-profession-guide",
    )

    assert payload["guide"]["slug"] == "midnight-alchemy-profession-guide"
    assert payload["guide"]["content_family"] == "profession_guide"
    assert payload["guide"]["supported_surface"] is True
    assert payload["guide"]["author"] == "Roguery"
    assert payload["guide"]["last_updated"] == "5th March 2026"
    assert payload["article"]["sections"][0]["title"] == "Introduction"


def test_recorded_delve_guide_fixture_contract() -> None:
    payload = parse_guide_page(
        load_fixture_text(FIXTURE_DIR, "delve_guide.html"),
        source_url="https://www.method.gg/guides/shadowguard-point-delve-guide",
    )

    assert payload["guide"]["slug"] == "shadowguard-point-delve-guide"
    assert payload["guide"]["content_family"] == "delve_guide"
    assert payload["guide"]["supported_surface"] is True
    assert payload["guide"]["author"] == "Roguery"
    assert payload["guide"]["last_updated"] == "25th February 2026"
    assert len(payload["article"]["sections"]) >= 1


def test_recorded_reputation_guide_fixture_contract() -> None:
    payload = parse_guide_page(
        load_fixture_text(FIXTURE_DIR, "reputation_guide.html"),
        source_url="https://www.method.gg/guides/harati-renown-reputation-guide",
    )

    assert payload["guide"]["slug"] == "harati-renown-reputation-guide"
    assert payload["guide"]["content_family"] == "reputation_guide"
    assert payload["guide"]["supported_surface"] is True
    assert payload["guide"]["author"] == "Roguery"
    assert payload["guide"]["last_updated"] == "26th February 2026"
    assert payload["linked_entities"][0]["id"] == 246734


def test_recorded_article_guide_fixture_contract() -> None:
    payload = parse_guide_page(
        load_fixture_text(FIXTURE_DIR, "article_guide.html"),
        source_url="https://www.method.gg/guides/world-of-warcraft-midnight-season-1-dungeon-locations",
    )

    assert payload["guide"]["slug"] == "world-of-warcraft-midnight-season-1-dungeon-locations"
    assert payload["guide"]["content_family"] == "article_guide"
    assert payload["guide"]["supported_surface"] is True
    assert payload["guide"]["author"] == "Tayder"
    assert payload["guide"]["last_updated"] == "26th February 2026"
    assert len(payload["article"]["sections"]) >= 1


def test_recorded_unsupported_index_fixture_contract() -> None:
    payload = parse_guide_page(
        load_fixture_text(FIXTURE_DIR, "unsupported_index.html"),
        source_url="https://www.method.gg/guides/tier-list",
    )

    assert payload["guide"]["slug"] == "tier-list"
    assert payload["guide"]["content_family"] == "unsupported_index"
    assert payload["guide"]["supported_surface"] is False
    assert payload["guide"]["page_url"] == "https://www.method.gg/guides/tier-list/mythic-plus"

"""Captured Method page: one real guide page, trimmed per docs/architecture/FIXTURE_MAINTENANCE.md.

The rest of the Method fixtures are hand-written and pin routing and key presence. This one pins the
parser against Method's production markup, which is where the talent builds live.
"""

from __future__ import annotations

from pathlib import Path

from article_provider_testkit import load_fixture_text
from method_cli.page_parser import parse_guide_page

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "method"
SOURCE_URL = "https://www.method.gg/guides/mistweaver-monk/talents"


def _captured_payload() -> dict:
    return parse_guide_page(load_fixture_text(FIXTURE_DIR, "captured_talents_page.html"), source_url=SOURCE_URL)


def test_captured_talents_page_reports_guide_identity_and_navigation() -> None:
    payload = _captured_payload()

    assert payload["guide"]["slug"] == "mistweaver-monk"
    assert payload["guide"]["section_slug"] == "talents"
    assert payload["guide"]["author"] == "Tincell"
    assert payload["guide"]["patch"] == "Patch 12.1"
    assert [row["section_slug"] for row in payload["navigation"]] == [
        "introduction",
        "talents",
        "gearing",
        "stats-races-and-consumables",
        "playstyle-and-rotation",
        "interface-and-macros",
    ]
    assert [row["section_slug"] for row in payload["navigation"] if row["active"]] == ["talents"]


def test_captured_talents_page_pins_the_full_heading_outline() -> None:
    """Exact titles and levels, so a parser change that loses nested headings or flattens levels fails."""
    payload = _captured_payload()

    assert [(row["title"], row["level"]) for row in payload["article"]["headings"]] == [
        ("Talent Builds", 2),
        ("Raid (Conduit of the Celestials) - Preferred", 3),
        ("Raid (Conduit of the Celestials - Sheilun's Gift", 3),
        ("Raid (Master of Harmony)", 3),
        ("Mythic+ (Conduit of the Celestials) - Preferred", 3),
        ("Mythic+ (Conduit of the Celestials) - Damage Oriented", 3),
        ("Mythic+ (Master of Harmony)", 3),
        ("Class Talents", 2),
        ("Spec Talents", 2),
        ("Apex Talents", 2),
        ("Hero Talents", 2),
        ("Conduit of the Celestials", 3),
        ("Master of Harmony", 3),
    ]
    assert [(row["title"], row["level"]) for row in payload["article"]["sections"]] == [
        ("Talents", 2),
        ("Conduit of the Celestials", 3),
        ("Master of Harmony", 3),
    ]


def test_captured_talents_page_extracts_published_loadout_import_strings() -> None:
    """Method publishes builds as import strings, not talent-calc links; they are the build evidence."""
    payload = _captured_payload()

    builds = payload["build_references"]
    assert sorted(row["label"] for row in builds) == [
        "Mythic+ (Conduit of the Celestials) - Damage Oriented",
        "Mythic+ (Conduit of the Celestials) - Preferred",
        "Mythic+ (Master of Harmony)",
        "Raid (Conduit of the Celestials - Sheilun's Gift",
        "Raid (Conduit of the Celestials) - Preferred",
        "Raid (Master of Harmony)",
    ]
    assert {row["reference_type"] for row in builds} == {"wow_talent_export"}
    assert [row["build_code"] for row in builds] == [row["url"] for row in builds]
    assert all(row["build_code"].startswith("C4QAAAAAAAAAAAAAAAAAAAAAA") for row in builds)
    assert all(row["source_url"] == SOURCE_URL for row in builds)
    assert builds[0]["source"] == {"provider": "method", "source": "guide_talent_export_string"}


def test_captured_talents_page_links_spell_entities_with_identities() -> None:
    payload = _captured_payload()

    tiger_palm = next(row for row in payload["linked_entities"] if row["id"] == 100780)
    assert tiger_palm["type"] == "spell"
    assert tiger_palm["ability_identity"]["identity"]["normalized_name"] == "tiger_palm"


def test_captured_method_fixture_stays_small() -> None:
    assert (FIXTURE_DIR / "captured_talents_page.html").stat().st_size < 100_000

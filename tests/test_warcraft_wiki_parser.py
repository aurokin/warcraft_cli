from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from warcraft_wiki_cli.page_parser import (
    classify_article_family,
    normalize_article_ref,
    parse_article_page,
    parse_search_results,
)

# Captured warcraft.wiki.gg API responses; see docs/architecture/FIXTURE_MAINTENANCE.md.
CAPTURED_DIR = Path(__file__).parent / "fixtures" / "warcraft_wiki"


def _captured(name: str) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads((CAPTURED_DIR / name).read_text())
    return payload


def test_normalize_article_ref_handles_wiki_paths() -> None:
    assert normalize_article_ref("/wiki/World_of_Warcraft_API") == "World of Warcraft API"


def test_normalize_article_ref_handles_full_wiki_urls() -> None:
    assert normalize_article_ref("https://warcraft.wiki.gg/wiki/Event:PLAYER_LOGIN") == "Event:PLAYER LOGIN"
    # A pasted section URL keeps its anchor: MediaWiki resolves "Elwynn Forest#Geography" to the
    # page itself (verified live: `article <that url>` returns the Elwynn Forest page, ok:true).
    assert normalize_article_ref("https://warcraft.wiki.gg/wiki/Elwynn_Forest%23Geography") == "Elwynn Forest#Geography"


def test_classify_article_family_handles_programming_and_system_titles() -> None:
    assert classify_article_family("API CreateFrame") == "api_function"
    assert classify_article_family("UIHANDLER OnKeyDown") == "ui_handler"
    assert classify_article_family("API change summaries") == "api_changes"
    assert classify_article_family("World of Warcraft API") == "framework_page"
    assert classify_article_family("Widget API") == "framework_page"
    assert classify_article_family("Create a WoW AddOn in 15 Minutes") == "howto_programming"
    assert classify_article_family("XML schema") == "xml_schema"
    assert classify_article_family("Patch 2.2.0/API changes") == "api_changes"
    assert classify_article_family("Renown") == "system_reference"
    assert classify_article_family("Druid") == "class_reference"
    assert classify_article_family("Profession") == "profession_reference"
    assert classify_article_family("Alchemy") == "profession_reference"
    assert classify_article_family("Zone scaling") == "zone_reference"
    assert classify_article_family("World of Warcraft: Legion") == "expansion_reference"


def test_classify_article_family_recognises_the_event_namespace() -> None:
    assert classify_article_family("Event:PLAYER LOGIN") == "event_reference"
    assert classify_article_family("Event:COMBAT_LOG_EVENT_UNFILTERED") == "event_reference"
    assert classify_article_family("Events") == "framework_page"


def test_parse_article_page_uses_mw_parser_output_root() -> None:
    payload = {
        "parse": {
            "title": "World of Warcraft API",
            "displaytitle": "<span class='mw-page-title-main'>World of Warcraft API</span>",
            "sections": [
                {"line": "API systems", "anchor": "API_systems"},
                {"line": "Object APIs", "anchor": "Object_APIs"},
            ],
            "text": {
                "*": """
                <div class="mw-parser-output">
                  <p>Intro copy.</p>
                  <h2><span class="mw-headline" id="API_systems">API systems</span></h2>
                  <p>FrameXML reference.</p>
                  <h2><span class="mw-headline" id="Object_APIs">Object APIs</span></h2>
                  <p><a href="/wiki/UIOBJECT_Frame">UIOBJECT Frame</a></p>
                </div>
                """
            },
        }
    }

    parsed = parse_article_page(payload, source_title="World of Warcraft API")

    assert parsed["article"]["title"] == "World of Warcraft API"
    assert parsed["article_content"]["headings"][0]["title"] == "API systems"
    assert len(parsed["article_content"]["sections"]) >= 2
    assert parsed["linked_entities"][0]["id"] == "UIOBJECT Frame"


def test_parse_article_page_extracts_programming_reference_and_filters_edit_links() -> None:
    payload = {
        "parse": {
            "title": "API CreateFrame",
            "displaytitle": "<span class='mw-page-title-main'>CreateFrame</span>",
            "sections": [
                {"line": "Arguments", "anchor": "Arguments"},
                {"line": "Returns", "anchor": "Returns"},
                {"line": "Example", "anchor": "Example"},
            ],
            "text": {
                "*": """
                <div class="mw-parser-output">
                  <div class="nomobile">Main Menu WoW API Lua API FrameXML API</div>
                  <div>Game Types mainline Links GitHub search Globe Wowprogramming</div>
                  <p>Creates a Frame object.</p>
                  <div class="mw-highlight">frame = CreateFrame(frameType)</div>
                  <h2><span class="mw-headline" id="Arguments">Arguments</span><span class="mw-editsection"><a href="/wiki/API_CreateFrame?action=edit&section=1">edit</a></span></h2>
                  <p>frameType string</p>
                  <h2><span class="mw-headline" id="Returns">Returns</span></h2>
                  <p>frame Frame</p>
                  <h2><span class="mw-headline" id="Example">Example</span></h2>
                  <p><a href="/wiki/API_CreateFramePool">CreateFramePool</a></p>
                </div>
                """
            },
        }
    }

    parsed = parse_article_page(payload, source_title="API CreateFrame")

    assert parsed["article"]["content_family"] == "api_function"
    assert parsed["reference"]["content_family"] == "api_function"
    assert parsed["reference"]["programming_reference"] is True
    assert parsed["reference"]["signature"] == "frame = CreateFrame(frameType)"
    assert parsed["reference"]["arguments"] == "frameType string"
    assert parsed["reference"]["returns"] == "frame Frame"
    assert "Main Menu" not in parsed["article_content"]["text"]
    assert "Wowprogramming" not in parsed["article_content"]["text"]
    assert all("action=edit" not in row["url"] for row in parsed["linked_entities"])


def test_parse_article_page_extracts_non_programming_reference_metadata() -> None:
    payload = {
        "parse": {
            "title": "Druid",
            "displaytitle": "<span class='mw-page-title-main'>Druid</span>",
            "sections": [
                {"line": "Class overview", "anchor": "Class_overview"},
                {"line": "Patch changes", "anchor": "Patch_changes"},
                {"line": "See also", "anchor": "See_also"},
                {"line": "References", "anchor": "References"},
            ],
            "text": {
                "*": """
                <div class="mw-parser-output">
                  <p>Druids are shapeshifting hybrids.</p>
                  <h2><span class="mw-headline" id="Class_overview">Class overview</span></h2>
                  <p>Versatile class overview.</p>
                  <h2><span class="mw-headline" id="Patch_changes">Patch changes</span></h2>
                  <p>Patch 10.0.0 adjusted forms.</p>
                  <h2><span class="mw-headline" id="See_also">See also</span></h2>
                  <p>Druid abilities</p>
                  <h2><span class="mw-headline" id="References">References</span></h2>
                  <p>Chronicle.</p>
                </div>
                """
            },
        }
    }

    parsed = parse_article_page(payload, source_title="Druid")

    assert parsed["article"]["content_family"] == "class_reference"
    assert parsed["reference"]["content_family"] == "class_reference"
    assert parsed["reference"]["summary"].startswith("Druids are shapeshifting hybrids.")
    assert parsed["reference"]["patch_changes"] == "Patch 10.0.0 adjusted forms."
    assert parsed["reference"]["see_also"] == "Druid abilities"
    assert parsed["reference"]["references"] == "Chronicle."


def test_parse_article_page_refines_general_family_to_faction_reference() -> None:
    payload = {
        "parse": {
            "title": "Argent Dawn",
            "displaytitle": "<span class='mw-page-title-main'>Argent Dawn</span>",
            "sections": [
                {"line": "History", "anchor": "History"},
                {"line": "Members", "anchor": "Members"},
                {"line": "Reputation", "anchor": "Reputation"},
                {"line": "Patch changes", "anchor": "Patch_changes"},
            ],
            "text": {
                "*": """
                <div class="mw-parser-output">
                  <p>The Argent Dawn is a holy order.</p>
                  <h2><span class="mw-headline" id="History">History</span></h2>
                  <p>Founded to fight the Scourge.</p>
                  <h2><span class="mw-headline" id="Members">Members</span></h2>
                  <p>Tirion Fordring.</p>
                  <h2><span class="mw-headline" id="Reputation">Reputation</span></h2>
                  <p>Faction rewards and standing.</p>
                  <h2><span class="mw-headline" id="Patch_changes">Patch changes</span></h2>
                  <p>Patch 3.0.2 updated reputation.</p>
                </div>
                """
            },
        }
    }

    parsed = parse_article_page(payload, source_title="Argent Dawn")

    assert parsed["article"]["content_family"] == "faction_reference"
    assert parsed["reference"]["content_family"] == "faction_reference"
    assert parsed["reference"]["summary"].startswith("The Argent Dawn is a holy order.")


def test_parse_article_page_refines_general_family_to_lore_reference() -> None:
    payload = {
        "parse": {
            "title": "Jaina Proudmoore",
            "displaytitle": "<span class='mw-page-title-main'>Jaina Proudmoore</span>",
            "sections": [
                {"line": "Biography", "anchor": "Biography"},
                {"line": "Patch changes", "anchor": "Patch_changes"},
            ],
            "text": {
                "*": """
                <div class="mw-parser-output">
                  <p>Jaina Proudmoore is a powerful sorceress.</p>
                  <h2><span class="mw-headline" id="Biography">Biography</span></h2>
                  <p>Leader of the Kirin Tor.</p>
                  <h2><span class="mw-headline" id="Patch_changes">Patch changes</span></h2>
                  <p>Patch 8.1.0 updated her model.</p>
                </div>
                """
            },
        }
    }

    parsed = parse_article_page(payload, source_title="Jaina Proudmoore")

    assert parsed["article"]["content_family"] == "lore_reference"
    assert parsed["reference"]["content_family"] == "lore_reference"
    assert parsed["reference"]["patch_changes"] == "Patch 8.1.0 updated her model."


def test_parse_article_page_refines_general_family_to_zone_reference_and_strips_comments() -> None:
    payload = {
        "parse": {
            "title": "Elwynn Forest",
            "displaytitle": "<span class='mw-page-title-main'>Elwynn Forest</span>",
            "sections": [
                {"line": "Geography", "anchor": "Geography"},
                {"line": "Maps and subregions", "anchor": "Maps_and_subregions"},
                {"line": "Quest and travel hubs", "anchor": "Quest_and_travel_hubs"},
                {"line": "Patch changes", "anchor": "Patch_changes"},
            ],
            "text": {
                "*": """
                <div class="mw-parser-output">
                  <table class="infobox"><tr><td>Zone infobox content</td></tr></table>
                  <p>Elwynn Forest is a human starting zone.</p>
                  <h2><span class="mw-headline" id="Geography">Geography</span></h2>
                  <p>Forests and rivers.</p>
                  <h2><span class="mw-headline" id="Maps_and_subregions">Maps and subregions</span></h2>
                  <p>Goldshire and Northshire.</p>
                  <h2><span class="mw-headline" id="Quest_and_travel_hubs">Quest and travel hubs</span></h2>
                  <p>Goldshire questing hub.</p>
                  <h2><span class="mw-headline" id="Patch_changes">Patch changes</span></h2>
                  <p>Patch 7.3.5 added scaling.</p>
                </div>
                <!-- Saved in parser cache with key something -->
                """
            },
        }
    }

    parsed = parse_article_page(payload, source_title="Elwynn Forest")

    assert parsed["article"]["content_family"] == "zone_reference"
    assert parsed["reference"]["content_family"] == "zone_reference"
    assert "Zone infobox content" not in parsed["article_content"]["text"]
    assert "Saved in parser cache" not in parsed["article_content"]["text"]


def test_parse_article_page_extracts_profession_reference_metadata() -> None:
    payload = {
        "parse": {
            "title": "Alchemy",
            "displaytitle": "<span class='mw-page-title-main'>Alchemy</span>",
            "sections": [
                {"line": "Official overview", "anchor": "Official_overview"},
                {"line": "Alchemy training", "anchor": "Alchemy_training"},
                {"line": "See also", "anchor": "See_also"},
                {"line": "Patch changes", "anchor": "Patch_changes"},
            ],
            "text": {
                "*": """
                <div class="mw-parser-output">
                  <p>Alchemy is a primary profession.</p>
                  <h2><span class="mw-headline" id="Official_overview">Official overview</span></h2>
                  <p>Mixes herbs into potions.</p>
                  <h2><span class="mw-headline" id="Alchemy_training">Alchemy training</span></h2>
                  <p>Learn from profession trainers.</p>
                  <h2><span class="mw-headline" id="See_also">See also</span></h2>
                  <p>Alchemy trainers</p>
                  <h2><span class="mw-headline" id="Patch_changes">Patch changes</span></h2>
                  <p>Patch 8.0.1 split profession skill bars.</p>
                </div>
                """
            },
        }
    }

    parsed = parse_article_page(payload, source_title="Alchemy")

    assert parsed["article"]["content_family"] == "profession_reference"
    assert parsed["reference"]["content_family"] == "profession_reference"
    assert parsed["reference"]["see_also"] == "Alchemy trainers"


def test_parse_article_page_extracts_expansion_reference_metadata() -> None:
    payload = {
        "parse": {
            "title": "World of Warcraft: Legion",
            "displaytitle": "<span class='mw-page-title-main'>World of Warcraft: Legion</span>",
            "sections": [
                {"line": "Features", "anchor": "Features"},
                {"line": "New zones", "anchor": "New_zones"},
                {"line": "Dungeons and raids", "anchor": "Dungeons_and_raids"},
                {"line": "References", "anchor": "References"},
            ],
            "text": {
                "*": """
                <div class="mw-parser-output">
                  <p>Legion is the sixth expansion.</p>
                  <h2><span class="mw-headline" id="Features">Features</span></h2>
                  <p>Artifacts and class halls.</p>
                  <h2><span class="mw-headline" id="New_zones">New zones</span></h2>
                  <p>Broken Isles.</p>
                  <h2><span class="mw-headline" id="Dungeons_and_raids">Dungeons and raids</span></h2>
                  <p>Emerald Nightmare.</p>
                  <h2><span class="mw-headline" id="References">References</span></h2>
                  <p>Official announcement.</p>
                </div>
                """
            },
        }
    }

    parsed = parse_article_page(payload, source_title="World of Warcraft: Legion")

    assert parsed["article"]["content_family"] == "expansion_reference"
    assert parsed["reference"]["content_family"] == "expansion_reference"


def test_linked_entities_key_on_the_article_title_not_the_anchor() -> None:
    # The section link comes first on purpose: the row it creates has to carry the fetchable title
    # and the fragment-free url, not "Mage#Talents" / ".../Mage#Talents" / the name "talents".
    payload = {
        "parse": {
            "title": "Mage",
            "displaytitle": "Mage",
            "sections": [],
            "text": {
                "*": """
                <div class="mw-parser-output">
                  <p>
                    <a href="/wiki/Mage#Talents">talents</a>
                    <a href="/wiki/Mage">Mage</a>
                    <a href="/wiki/Mage#Lore">lore</a>
                    <a href="/wiki/Frost_Nova">Frost Nova</a>
                    <a href="/wiki/Mage?action=edit&amp;section=1">edit</a>
                    <a href="/wiki/File:Mage.png">image</a>
                    <a href="/wiki/Category:Mage_abilities">category</a>
                    <a href="/wiki/Special:WhatLinksHere/Mage">links</a>
                    <a href="/wiki/Help:Editing">help</a>
                    <a href="/wiki/Template:Mage">template</a>
                    <a href="https://example.com/mage">offsite</a>
                  </p>
                </div>
                """
            },
        }
    }

    entities = parse_article_page(payload, source_title="Mage")["linked_entities"]

    assert [row["id"] for row in entities] == ["Frost Nova", "Mage"]
    assert [row["url"] for row in entities] == [
        "https://warcraft.wiki.gg/wiki/Frost_Nova",
        "https://warcraft.wiki.gg/wiki/Mage",
    ]
    assert [row["name"] for row in entities] == ["Frost Nova", "Mage"]


def test_parse_search_results_maps_a_captured_mediawiki_search_response() -> None:
    total_hits, rows = parse_search_results(_captured("search_player_login.json"))

    assert total_hits == 472
    assert [row["title"] for row in rows[:3]] == ["Event:PLAYER LOGIN", "UIHANDLER OnEvent", "API:Frame IsEventRegistered"]
    first = rows[0]
    assert first["pageid"] == 284516
    assert first["url"] == "https://warcraft.wiki.gg/wiki/Event:PLAYER_LOGIN"
    # The upstream snippet is HTML with <span class="searchmatch"> highlights; it is stripped to text.
    assert "<span" not in first["snippet"]
    assert "PLAYER_LOGIN" in first["snippet"]


def test_parse_search_results_skips_rows_without_a_title() -> None:
    payload = {"query": {"searchinfo": {"totalhits": 3}, "search": [{"title": " "}, "junk", {"title": "Mage"}]}}

    total_hits, rows = parse_search_results(payload)

    assert total_hits == 3
    assert [row["title"] for row in rows] == ["Mage"]


def test_parse_captured_event_page_is_an_event_reference_without_page_chrome() -> None:
    parsed = parse_article_page(_captured("parse_event_player_login.json"), source_title="Event:PLAYER_LOGIN")

    assert parsed["article"]["title"] == "Event:PLAYER LOGIN"
    assert parsed["article"]["display_title"] == "PLAYER_LOGIN"
    assert parsed["article"]["page_url"] == "https://warcraft.wiki.gg/wiki/Event:PLAYER_LOGIN"
    assert parsed["article"]["content_family"] == "event_reference"
    assert [(row["title"], row["level"]) for row in parsed["article_content"]["headings"]] == [
        ("Payload", 2),
        ("Details", 2),
        ("See also", 2),
    ]
    assert parsed["reference"]["programming_reference"] is True
    assert parsed["reference"]["summary"].startswith("Triggered immediately before PLAYER_ENTERING_WORLD on login")
    assert parsed["reference"]["details"] == "Related Events PLAYER_LOGOUT"
    # The "Game Types"/"Main Menu" navigation tables are chrome, not event documentation.
    assert "Main Menu" not in parsed["article_content"]["text"]
    assert "Wowprogramming" not in parsed["article_content"]["text"]
    assert [row["id"] for row in parsed["linked_entities"]] == [
        "AddOn loading process",
        "Event:PLAYER ENTERING WORLD",
        "Event:PLAYER LOGOUT",
    ]


def test_parse_captured_api_function_page_keeps_nested_heading_levels() -> None:
    parsed = parse_article_page(_captured("parse_api_unithealth.json"), source_title="API:UnitHealth")

    assert parsed["article"]["title"] == "API:UnitHealth"
    assert parsed["article"]["content_family"] == "api_function"
    assert [(row["title"], row["level"]) for row in parsed["article_content"]["headings"]] == [
        ("Arguments", 2),
        ("Returns", 2),
        ("Details", 2),
        ("Patch changes", 2),
        ("Retail", 4),
        ("Classic", 4),
        ("References", 2),
    ]
    assert parsed["reference"]["signature"] == "health = UnitHealth ( unit [, usePredicted ])"
    assert parsed["reference"]["arguments"].startswith("unit UnitToken : string")
    assert parsed["reference"]["returns"].startswith("health number - Returns 0 if the unit is dead")
    assert "Main Menu" not in parsed["article_content"]["text"]


def test_parse_captured_lore_page_refines_to_lore_reference_and_drops_the_infobox() -> None:
    parsed = parse_article_page(_captured("parse_mankrik.json"), source_title="Mankrik")

    assert parsed["article"]["content_family"] == "lore_reference"
    headings = [(row["title"], row["level"]) for row in parsed["article_content"]["headings"]]
    assert headings[:5] == [
        ("Biography", 2),
        ("Cataclysm", 3),
        ("Legion", 3),
        ("Dragonflight", 3),
        ("The War Within", 3),
    ]
    assert headings[-3:] == [("Gallery", 2), ("References", 2), ("External links", 2)]
    assert len(headings) == 17
    assert parsed["article_content"]["sections"][0]["title"] == "Introduction"
    assert "Mankrik is an orc quest giver" in parsed["article_content"]["text"]
    assert "programming_reference" not in parsed["reference"]

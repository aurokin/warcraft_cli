from __future__ import annotations

import pytest
from article_provider_testkit import error_payload, payload_for_live, require_live
from typer.testing import CliRunner
from warcraft_wiki_cli.main import app

pytestmark = pytest.mark.live

runner = CliRunner()


def test_live_warcraft_wiki_api_search_and_resolve_contract() -> None:
    require_live("Warcraft Wiki")
    search_payload = payload_for_live(runner, app, ["search", "CreateFrame", "--limit", "5"], provider_name="Warcraft Wiki")["data"]

    assert search_payload["count"] >= 1
    first = search_payload["results"][0]
    # The wiki moved its API reference out of the main-namespace "API Foo" titles into the real
    # "API:" namespace (id 3000) and left "API CreateFrame" behind as a redirect. Redirects are not
    # returned by list=search, so the canonical title is now the only thing search can surface.
    assert first["id"] == "API:CreateFrame"
    assert first["metadata"]["content_family"] == "api_function"

    resolve_payload = payload_for_live(runner, app, ["resolve", "CreateFrame", "--limit", "5"], provider_name="Warcraft Wiki")["data"]
    assert resolve_payload["resolved"] is True
    assert resolve_payload["next_command"] == "warcraft-wiki article API:CreateFrame"

    api_payload = payload_for_live(runner, app, ["api", "CreateFrame"], provider_name="Warcraft Wiki")["data"]
    assert api_payload["article"]["content_family"] == "api_function"
    assert api_payload["resolved_surface"] == "api"

    api_full_payload = payload_for_live(runner, app, ["api-full", "CreateFrame"], provider_name="Warcraft Wiki")["data"]
    assert api_full_payload["article"]["content_family"] == "api_function"
    assert api_full_payload["resolved_surface"] == "api"
    assert api_full_payload["reference"]["signature"] is not None


def test_live_warcraft_wiki_handler_resolve_contract() -> None:
    require_live("Warcraft Wiki")
    payload = payload_for_live(runner, app, ["resolve", "OnKeyDown", "--limit", "5"], provider_name="Warcraft Wiki")["data"]

    assert payload["resolved"] is True
    assert payload["match"]["metadata"]["content_family"] == "ui_handler"
    assert payload["match"]["id"] == "UIHANDLER OnKeyDown"

    event_payload = payload_for_live(runner, app, ["event", "OnKeyDown"], provider_name="Warcraft Wiki")["data"]
    assert event_payload["article"]["content_family"] == "ui_handler"
    assert event_payload["resolved_surface"] == "event"

    event_full_payload = payload_for_live(runner, app, ["event-full", "OnKeyDown"], provider_name="Warcraft Wiki")["data"]
    assert event_full_payload["article"]["content_family"] == "ui_handler"
    assert event_full_payload["resolved_surface"] == "event"


def test_live_warcraft_wiki_faction_query_cleanup_contract() -> None:
    require_live("Warcraft Wiki")
    payload = payload_for_live(runner, app, ["resolve", "faction argent dawn", "--limit", "5"], provider_name="Warcraft Wiki")["data"]

    assert payload["search_query"] == "argent dawn"
    assert payload["excluded_terms"] == ["faction"]
    assert payload["resolved"] is True
    assert payload["match"]["id"] == "Argent Dawn"


def test_live_warcraft_wiki_programming_article_contract() -> None:
    require_live("Warcraft Wiki")
    payload = payload_for_live(runner, app, ["article", "API_CreateFrame"], provider_name="Warcraft Wiki")["data"]

    assert payload["article"]["content_family"] == "api_function"
    assert payload["reference"]["programming_reference"] is True
    assert payload["reference"]["signature"] is not None
    assert "Main Menu" not in payload["content"]["text"]
    assert all("action=edit" not in row["url"] for row in payload["linked_entities"]["items"])


def test_live_warcraft_wiki_api_changes_article_contract() -> None:
    require_live("Warcraft Wiki")
    payload = payload_for_live(runner, app, ["article", "Patch 2.1.0/API changes"], provider_name="Warcraft Wiki")["data"]

    assert payload["article"]["content_family"] == "api_changes"
    assert payload["reference"]["programming_reference"] is True
    assert payload["reference"]["summary"] is not None
    assert payload["content"]["section_count"] >= 5


def test_live_warcraft_wiki_framework_and_xml_api_contracts() -> None:
    require_live("Warcraft Wiki")

    framework_payload = payload_for_live(runner, app, ["api", "World of Warcraft API"], provider_name="Warcraft Wiki")["data"]
    assert framework_payload["article"]["content_family"] == "framework_page"
    assert framework_payload["resolved_surface"] == "api"
    assert framework_payload["reference"]["programming_reference"] is True

    xml_payload = payload_for_live(runner, app, ["api", "XML schema"], provider_name="Warcraft Wiki")["data"]
    assert xml_payload["article"]["content_family"] == "xml_schema"
    assert xml_payload["resolved_surface"] == "api"
    assert xml_payload["reference"]["programming_reference"] is True


def test_live_warcraft_wiki_framework_event_contract() -> None:
    require_live("Warcraft Wiki")
    payload = payload_for_live(runner, app, ["event", "Events"], provider_name="Warcraft Wiki")["data"]

    assert payload["article"]["content_family"] == "framework_page"
    assert payload["resolved_surface"] == "event"
    assert payload["reference"]["programming_reference"] is True


@pytest.mark.parametrize(
    ("event_name", "expected_title"),
    [
        ("PLAYER_LOGIN", "Event:PLAYER LOGIN"),
        ("PLAYER_ENTERING_WORLD", "Event:PLAYER ENTERING WORLD"),
        # The wiki keeps this one under its pre-"UNFILTERED" title and serves the long name as a redirect.
        ("COMBAT_LOG_EVENT_UNFILTERED", "Event:COMBAT LOG EVENT"),
        ("ENCOUNTER_START", "Event:ENCOUNTER START"),
        ("UNIT_HEALTH", "Event:UNIT HEALTH"),
        ("BAG_UPDATE", "Event:BAG UPDATE"),
    ],
)
def test_live_warcraft_wiki_game_event_contract(event_name: str, expected_title: str) -> None:
    require_live("Warcraft Wiki")
    data = payload_for_live(runner, app, ["event", event_name], provider_name="Warcraft Wiki")["data"]

    assert data["article"]["title"] == expected_title
    assert data["article"]["content_family"] == "event_reference"
    assert data["resolved_from"] == "direct_fetch"
    assert data["content"]["text"]


def test_live_warcraft_wiki_unknown_event_fails_not_found() -> None:
    require_live("Warcraft Wiki")
    # invoke_live retries until exit 0, so an expected failure has to go through the runner directly.
    result = runner.invoke(app, ["event", "NOT_A_REAL_EVENT_XYZ"])

    assert result.exit_code == 4
    assert error_payload(result)["error"]["code"] == "not_found"


def test_live_warcraft_wiki_game_event_search_contract() -> None:
    require_live("Warcraft Wiki")
    data = payload_for_live(runner, app, ["search", "PLAYER_LOGIN", "--limit", "5"], provider_name="Warcraft Wiki")["data"]

    top = data["results"][0]
    assert top["id"] == "Event:PLAYER LOGIN"
    assert "exact_event_title" in top["ranking"]["match_reasons"]
    # Whatever upstream ranked below it does not match the query: it must not come close in score.
    assert all(row["ranking"]["score"] < top["ranking"]["score"] - 18 for row in data["results"][1:])


def test_live_warcraft_wiki_programming_howto_contract() -> None:
    require_live("Warcraft Wiki")
    payload = payload_for_live(runner, app, ["article", "Create a WoW AddOn in 15 Minutes"], provider_name="Warcraft Wiki")["data"]

    assert payload["article"]["content_family"] == "howto_programming"
    assert payload["reference"]["programming_reference"] is True
    assert "Main Menu" not in payload["content"]["text"]
    assert payload["content"]["section_count"] >= 5


def test_live_warcraft_wiki_system_article_contract() -> None:
    require_live("Warcraft Wiki")
    payload = payload_for_live(runner, app, ["article", "Renown"], provider_name="Warcraft Wiki")["data"]

    assert payload["article"]["content_family"] == "system_reference"
    assert payload["reference"]["content_family"] == "system_reference"
    assert payload["reference"]["summary"] is not None
    assert payload["content"]["section_count"] >= 3
    assert payload["navigation"]["count"] >= 3


def test_live_warcraft_wiki_expansion_article_contract() -> None:
    require_live("Warcraft Wiki")
    payload = payload_for_live(runner, app, ["article", "Expansion"], provider_name="Warcraft Wiki")["data"]

    assert payload["article"]["content_family"] == "expansion_reference"
    assert payload["reference"]["content_family"] == "expansion_reference"
    assert payload["reference"]["summary"] is not None
    assert payload["content"]["section_count"] >= 2


def test_live_warcraft_wiki_redirect_article_contract() -> None:
    require_live("Warcraft Wiki")
    payload = payload_for_live(runner, app, ["article", "Legion"], provider_name="Warcraft Wiki")["data"]
    assert payload["article"]["title"] == "World of Warcraft: Legion"
    assert payload["article"]["content_family"] == "expansion_reference"


def test_live_warcraft_wiki_class_article_contract() -> None:
    require_live("Warcraft Wiki")
    payload = payload_for_live(runner, app, ["article", "Druid"], provider_name="Warcraft Wiki")["data"]

    assert payload["article"]["content_family"] == "class_reference"
    assert payload["reference"]["content_family"] == "class_reference"
    assert payload["reference"]["patch_changes"] is not None
    assert payload["content"]["section_count"] >= 10


def test_live_warcraft_wiki_faction_article_contract() -> None:
    require_live("Warcraft Wiki")
    payload = payload_for_live(runner, app, ["article", "Argent Dawn"], provider_name="Warcraft Wiki")["data"]

    assert payload["article"]["content_family"] == "faction_reference"
    assert payload["reference"]["content_family"] == "faction_reference"
    assert payload["reference"]["patch_changes"] is not None
    assert payload["content"]["section_count"] >= 8


def test_live_warcraft_wiki_lore_query_cleanup_and_article_contract() -> None:
    require_live("Warcraft Wiki")
    resolve_payload = payload_for_live(runner, app, ["resolve", "lore jaina proudmoore", "--limit", "5"], provider_name="Warcraft Wiki")["data"]
    assert resolve_payload["search_query"] == "jaina proudmoore"
    assert resolve_payload["excluded_terms"] == ["lore"]
    assert resolve_payload["resolved"] is True
    assert resolve_payload["match"]["id"] == "Jaina Proudmoore"

    payload = payload_for_live(runner, app, ["article", "Jaina Proudmoore"], provider_name="Warcraft Wiki")["data"]
    assert payload["article"]["content_family"] == "lore_reference"
    assert payload["reference"]["content_family"] == "lore_reference"
    assert payload["reference"]["patch_changes"] is not None


def test_live_warcraft_wiki_zone_query_cleanup_and_article_contract() -> None:
    require_live("Warcraft Wiki")
    resolve_payload = payload_for_live(runner, app, ["resolve", "zone elwynn forest", "--limit", "10"], provider_name="Warcraft Wiki")["data"]
    assert resolve_payload["search_query"] == "elwynn forest"
    assert resolve_payload["excluded_terms"] == ["zone"]
    assert resolve_payload["resolved"] is True
    assert resolve_payload["match"]["id"] == "Elwynn Forest"

    payload = payload_for_live(runner, app, ["article", "Elwynn Forest"], provider_name="Warcraft Wiki")["data"]
    assert payload["article"]["content_family"] == "zone_reference"
    assert payload["reference"]["content_family"] == "zone_reference"
    assert payload["reference"]["patch_changes"] is not None


def test_live_warcraft_wiki_guide_query_cleanup_and_article_contract() -> None:
    require_live("Warcraft Wiki")
    resolve_payload = payload_for_live(runner, app, ["resolve", "guide interface customization",
                                       "--limit", "10"], provider_name="Warcraft Wiki")["data"]
    assert resolve_payload["search_query"] == "interface customization"
    assert resolve_payload["excluded_terms"] == ["guide"]
    assert resolve_payload["resolved"] is True
    assert resolve_payload["match"]["id"] == "User interface customization guide"
    assert resolve_payload["match"]["metadata"]["content_family"] == "howto_programming"


def test_live_warcraft_wiki_profession_query_cleanup_and_article_contract() -> None:
    require_live("Warcraft Wiki")
    resolve_payload = payload_for_live(runner, app, ["resolve", "profession alchemy", "--limit", "10"], provider_name="Warcraft Wiki")["data"]
    assert resolve_payload["search_query"] == "alchemy"
    assert resolve_payload["excluded_terms"] == ["profession"]
    assert resolve_payload["resolved"] is True
    assert resolve_payload["match"]["id"] == "Alchemy"

    payload = payload_for_live(runner, app, ["article", "Alchemy"], provider_name="Warcraft Wiki")["data"]
    assert payload["article"]["content_family"] == "profession_reference"
    assert payload["reference"]["content_family"] == "profession_reference"
    assert payload["reference"]["patch_changes"] is not None


def test_live_warcraft_wiki_class_query_cleanup_contract() -> None:
    require_live("Warcraft Wiki")
    resolve_payload = payload_for_live(runner, app, ["resolve", "class druid", "--limit", "10"], provider_name="Warcraft Wiki")["data"]
    assert resolve_payload["search_query"] == "druid"
    assert resolve_payload["excluded_terms"] == ["class"]
    assert resolve_payload["resolved"] is True
    assert resolve_payload["match"]["id"] == "Druid"
    assert resolve_payload["match"]["metadata"]["content_family"] == "class_reference"


def test_live_warcraft_wiki_expansion_query_cleanup_and_article_contract() -> None:
    require_live("Warcraft Wiki")
    resolve_payload = payload_for_live(runner, app, ["resolve", "expansion legion", "--limit", "10"], provider_name="Warcraft Wiki")["data"]
    assert resolve_payload["search_query"] == "legion"
    assert resolve_payload["excluded_terms"] == ["expansion"]
    assert resolve_payload["resolved"] is True
    assert resolve_payload["match"]["id"] == "World of Warcraft: Legion"
    assert resolve_payload["match"]["metadata"]["content_family"] == "expansion_reference"

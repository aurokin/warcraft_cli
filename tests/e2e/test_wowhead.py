"""End-to-end journeys for the ``wowhead`` binary against the live site.

Listing identifiers (news slug, guide id, comment id, npc/spell/quest id, talent build code) are
discovered at run time from an earlier command in the same journey. The pins are Thunderfury
(``tests/e2e/pins.py``), the classic-era ids of the entity types whose page lives under another
route, and regression targets that can age out and then need updating: the three opaque tool-state
refs Wowhead only mints in a browser, the Fury Warrior guide id, and achievement 18372.
"""

from __future__ import annotations

import json
import re
import shlex
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from tests.discovery_contract import resolve_data_violations, search_data_violations
from tests.e2e import pins
from tests.e2e.harness import (
    EXIT_NETWORK,
    EXIT_NOT_FOUND,
    EXIT_OK,
    EXIT_USAGE,
    Result,
    dead_proxy_env,
    no_cache_env,
    run,
    run_raw,
    stream_records,
    word_names,
)

BINARY = "wowhead"

# Class/spec and profession slugs are permanent Wowhead routes.
TALENT_CALC_SPEC = "druid/balance"
BALANCE_SPEC_ID = 102
# A Classic Era warrior build; classic calculators take any build code the tree accepts.
CLASSIC_TALENT_CALC_URL = "https://www.wowhead.com/classic/talent-calc/warrior/30305001302-05050005525010051"
PROFESSION_TREE_REF = "alchemy/BCuA"
# Opaque client-side state: Wowhead only mints these in the browser, so they cannot be discovered
# from any listing command. The profiler list has since been removed from Wowhead, which is what
# its journey pins.
DRESSING_ROOM_REF = "#fz8zz0zb89c8mM8YB8mN8X18mO8ub8mP8uD"
PROFILER_REF = "97060220/us/illidan/Roguecane"
# A WoW Forever news post, the shape `news` lists under /forever/news/; news posts stay up.
FOREVER_NEWS_URL = "https://www.wowhead.com/forever/news/ghost-wolf-appearance-temporary-until-fixes-are-in-for-wow-forever-383241"

# `wowhead expansions` lists these; every one routes real Wowhead paths for a classic-era item.
CLASSIC_EXPANSIONS = ("classic", "tbc", "wotlk", "cata", "mop-classic")

# Wowhead updates each class guide in place every expansion, so the main Fury Warrior guide keeps
# one id; the retired guides that share its words (Legion Remix, Dragonflight seasons) have their own.
FURY_GUIDE_QUERY = "fury warrior guide"
FURY_GUIDE_ID = 3087

# Entity types whose Wowhead page lives under a different `<type>=<id>` route than the type name:
# a mount is an item page, a recipe is a spell page, a battle pet is an NPC page. The ids are
# classic-era entries and as permanent as the pins in tests/e2e/pins.py.
ROUTED_ENTITIES: tuple[tuple[str, int, str], ...] = (
    ("faction", 529, "https://www.wowhead.com/faction=529"),
    ("pet", 39, "https://www.wowhead.com/pet=39"),
    ("recipe", 2549, "https://www.wowhead.com/spell=2549"),
    ("mount", 460, "https://www.wowhead.com/item=84101"),
    ("battle-pet", 39, "https://www.wowhead.com/npc=2671"),
)
# Elwynn Forest, a zone since 2004 with its own quests and NPCs.
ELWYNN_FOREST_ZONE_ID = 12
# Isle of Dorn, The War Within's first zone: its page links more than entity-page's 2000-link cap.
ISLE_OF_DORN_ZONE_ID = 14717
# Wowhead's own `typeName` for the numeric suggestion `type` this CLI maps to each entity type.
# A wrong id in that table mislabels the row and mints a follow-up command for the wrong page.
SUGGESTION_TYPE_NAMES: dict[str, str] = {
    "achievement": "achievement",
    "currency": "currency",
    "faction": "faction",
    "guide": "guide",
    "hunter pet": "pet",
    "item": "item",
    "news post": "news",
    "npc": "npc",
    "object": "object",
    "quest": "quest",
    "spell": "spell",
    "transmog set": "transmog-set",
    "world event": "event",
    "zone": "zone",
}


def assert_envelope_data_holds(result: Result, *keys: str) -> None:
    """The envelope slot ``data`` carries the payload block each journey goes on to read."""
    assert result.data, f"data slot is empty\n{result.describe()}"
    for key in keys:
        assert key in result.data, f"data is missing {key!r}\n{result.describe()}"


def run_follow_up(command: str) -> Result:
    """Run a follow-up command exactly as the CLI printed it."""
    parts = shlex.split(command)
    assert parts[0] == BINARY, f"follow-up command does not start with the binary: {command!r}"
    return run(BINARY, *parts[1:])


def entity_rows(result: Result, entity_type: str) -> list[dict[str, Any]]:
    return [row for row in result.data["results"] if row.get("entity_type") == entity_type]


@pytest.fixture(scope="module")
def thunderfury_search() -> Result:
    return run(BINARY, "search", pins.ITEM_SEARCH_QUERY, "--limit", "10")


@pytest.fixture(scope="module")
def class_guides() -> Result:
    return run(BINARY, "guides", "classes", "--sort", "updated", "--limit", "5")


@pytest.fixture(scope="module")
def class_guide_baseline() -> Result:
    """The class guides in Wowhead's default order: the unsorted, unfiltered read the sort and patch journeys compare against."""
    return run(BINARY, "guides", "classes", "--limit", "200")


@pytest.fixture(scope="module")
def guide_id(class_guides: Result) -> int:
    return int(class_guides.data["results"][0]["id"])


@pytest.fixture(scope="module")
def news_listing() -> Result:
    return run(BINARY, "news", "--limit", "5")


@pytest.fixture(scope="module")
def news_scan() -> Result:
    """Two whole pages of news: the unfiltered baseline every news filter journey compares against.

    Wowhead's listing pages land in the session cache, so the filtered calls below cost no further
    request and the comparison is exact rather than statistical.
    """
    return run(BINARY, "news", "--pages", "2", "--limit", "200")


@pytest.fixture(scope="module")
def blue_listing() -> Result:
    # The whole first page, so the --region journey can compare filtered against unfiltered exactly.
    return run(BINARY, "blue-tracker", "--limit", "200")


def test_doctor_reports_live_endpoints_and_the_isolated_cache(require, cache_root: Path) -> None:
    require("wowhead")
    live = run(BINARY, "doctor")
    assert_envelope_data_holds(live, "status", "endpoints", "cache")
    assert live.data["status"] == "ready", live.describe()
    assert live.data["failed_probes"] == [], live.describe()
    for name, probe in live.data["endpoints"].items():
        assert probe["ok"] is True, f"{name} probe failed\n{live.describe()}"
        assert probe["status_code"] == 200, f"{name} probe status\n{live.describe()}"
    assert live.data["endpoints"]["search_suggestions"]["shape"]["result_count"] > 0, live.describe()
    assert live.data["endpoints"]["tooltip"]["shape"]["has_name"] is True, live.describe()
    assert str(cache_root) in live.data["cache"]["cache_dir"], live.describe()

    offline = run(BINARY, "doctor", "--no-live")
    assert offline.data["status"] == "ready", offline.describe()
    assert all(probe["skipped"] is True for probe in offline.data["endpoints"].values()), offline.describe()


def test_a_repeated_search_says_it_was_answered_from_the_cache(require, tmp_path: Path) -> None:
    """``provenance.cache`` calls a cold search a miss and the same search a moment later a hit."""
    require("wowhead")
    # A cache root of its own, so the first search is a miss whatever the session already fetched.
    cache_env = {"XDG_CACHE_HOME": str(tmp_path / "cache")}
    first = run(BINARY, "search", pins.ITEM_SEARCH_QUERY, env=cache_env)
    second = run(BINARY, "search", pins.ITEM_SEARCH_QUERY, env={**cache_env, **dead_proxy_env()})
    assert first.payload["provenance"]["cache"]["hit"] is False, first.describe()
    cache = second.payload["provenance"]["cache"]
    assert (cache["backend"], cache["hit"], cache["all_hits"]) == ("file", True, True), second.describe()
    assert 0 <= cache["oldest_hit_age_seconds"] <= cache["oldest_hit_ttl_seconds"], second.describe()


def test_expansions_list_backs_expansion_detect_on_real_urls(require) -> None:
    require("wowhead")
    listed = run(BINARY, "expansions")
    assert_envelope_data_holds(listed, "default", "profiles")
    profiles = {row["key"]: row for row in listed.data["profiles"]}
    assert listed.data["default"] == "retail"
    assert {"retail", *CLASSIC_EXPANSIONS} <= set(profiles), listed.describe()

    for key in ("retail", *CLASSIC_EXPANSIONS):
        base = profiles[key]["wowhead_base"]
        detected = run(BINARY, "expansion-detect", f"{base}/item={pins.ITEM_ID}")
        assert_envelope_data_holds(detected, "detected_expansion", "entity")
        assert detected.data["detected_expansion"] == key, detected.describe()
        assert detected.data["entity"] == {"type": "item", "id": pins.ITEM_ID}, detected.describe()
        assert detected.data["matches_selected_expansion"] is (key == "retail"), detected.describe()


@pytest.mark.parametrize("word", ["shadow", "fire", "light"])
def test_a_one_word_resolve_is_high_only_for_a_row_the_word_names(require, word: str) -> None:
    """``resolve shadow`` answered "In the Catalyst's Shadow" at high (2026-10) on Wowhead's rank alone.

    Upstream rows drift, so the oracle is the top row itself: when the word does not name it, the
    answer is at most medium and says why.
    """
    require("wowhead")
    result = run(BINARY, "resolve", word)
    data = result.data
    assert resolve_data_violations(data, provider="wowhead") == [], result.describe()
    match = data["match"]
    if match is not None and not word_names(word, match["name"], match["metadata"].get("display_name")):
        assert data["confidence"] != "high", result.describe()
        assert data.get("confidence_cap") in (None, {"rule": "single_word_query", "from": "high"}), result.describe()


def test_a_wowhead_url_resolves_at_high_whatever_its_shape(require) -> None:
    """A URL names one entity outright, so the one-word rule never touches it."""
    require("wowhead")
    result = run(BINARY, "resolve", f"https://www.wowhead.com/item={pins.ITEM_ID}")
    assert (result.data["confidence"], result.data["next_command"]) == ("high", f"wowhead entity item {pins.ITEM_ID}"), result.describe()


def test_search_resolve_and_entity_agree_on_thunderfury(require, thunderfury_search: Result) -> None:
    require("wowhead")
    assert_envelope_data_holds(thunderfury_search, "results", "count", "search_url")
    rows = thunderfury_search.data["results"]
    assert thunderfury_search.data["count"] == len(rows) > 0, thunderfury_search.describe()
    assert search_data_violations(thunderfury_search.data, provider="wowhead") == [], thunderfury_search.describe()
    assert all(isinstance(row["id"], int) and row["name"] for row in rows)
    # Several Wowhead items are named after Thunderfury (a replica, a quest copy); the search has to
    # carry the real one, under its real name.
    assert [row["name"] for row in entity_rows(thunderfury_search, "item") if row["id"] == pins.ITEM_ID] == [
        pins.ITEM_NAME
    ], thunderfury_search.describe()

    resolved = run(BINARY, "resolve", pins.ITEM_SEARCH_QUERY, "--entity-type", "item", "--limit", "3")
    assert_envelope_data_holds(resolved, "match", "candidates")
    match = resolved.data["match"]
    assert isinstance(match, dict), f"resolve found no item\n{resolved.describe()}"
    assert match["entity_type"] == "item", resolved.describe()
    # Not "some item the search also returned": the query names one item and resolve must pick it.
    assert (match["id"], match["name"]) == (pins.ITEM_ID, pins.ITEM_NAME), resolved.describe()
    assert resolved.data["confidence"] == "high", resolved.describe()
    assert resolved.data["filters"]["entity_types"] == ["item"], resolved.describe()
    assert resolve_data_violations(resolved.data, provider="wowhead") == [], resolved.describe()

    entity = run(BINARY, "entity", "item", str(pins.ITEM_ID))
    assert_envelope_data_holds(entity, "entity", "tooltip", "normalized")
    assert entity.data["entity"] == {
        "type": "item",
        "id": pins.ITEM_ID,
        "name": pins.ITEM_NAME,
        "page_url": entity.data["entity"]["page_url"],
    }
    assert entity.data["entity"]["page_url"].startswith(f"https://www.wowhead.com/item={pins.ITEM_ID}")
    tooltip = entity.data["tooltip"]
    assert tooltip["icon"] and pins.ITEM_NAME in tooltip["text"], entity.describe()
    assert tooltip["quality"] == 5, entity.describe()
    normalized = entity.data["normalized"]
    assert normalized["schema_version"], entity.describe()
    assert normalized["item"]["name"]["value"] == pins.ITEM_NAME, entity.describe()
    assert normalized["item"]["name"]["provenance"], entity.describe()

    page = run(BINARY, "entity-page", "item", str(pins.ITEM_ID), "--max-links", "10")
    assert_envelope_data_holds(page, "entity", "page", "linked_entities", "normalized")
    assert page.data["page"]["title"] == pins.ITEM_NAME, page.describe()
    assert page.data["normalized"]["item"]["name"]["value"] == pins.ITEM_NAME, page.describe()
    assert page.data["citations"]["page"] == page.data["entity"]["page_url"], page.describe()
    links = page.data["linked_entities"]
    assert links["count"] == len(links["items"]) > 0, page.describe()


def test_search_answers_a_wowhead_url_with_the_entity_it_names(require) -> None:
    """A pasted entity URL is answered by that entity, on the URL's own dataset.

    Wowhead's suggestions match names, so forwarding the URL upstream once answered ok: true with
    no results. The entity page the follow-up opens is the oracle for the name.
    """
    require("wowhead")
    found = run(BINARY, "search", f"https://www.wowhead.com/classic/item={pins.ITEM_ID}")
    assert found.data["expansion"] == "classic", found.describe()
    assert [(row["entity_type"], row["id"]) for row in found.data["results"]] == [("item", pins.ITEM_ID)], found.describe()
    entity = run_follow_up(found.data["results"][0]["follow_up"]["command"])
    assert entity.data["expansion"] == "classic", entity.describe()
    assert (entity.data["entity"]["id"], entity.data["entity"]["name"]) == (pins.ITEM_ID, pins.ITEM_NAME), entity.describe()

    # `--url` stands in for TYPE ID, as documented; the positionals were once still required.
    by_url = run(BINARY, "entity", "--url", f"https://www.wowhead.com/classic/item={pins.ITEM_ID}", "--no-include-comments")
    assert by_url.data["expansion"] == "classic", by_url.describe()
    assert (by_url.data["entity"]["type"], by_url.data["entity"]["id"], by_url.data["entity"]["name"]) == (
        "item", pins.ITEM_ID, pins.ITEM_NAME,
    ), by_url.describe()
    assert by_url.data["entity"]["page_url"].startswith(f"https://www.wowhead.com/classic/item={pins.ITEM_ID}"), by_url.describe()


def test_search_answers_a_wowhead_guide_url_with_the_guide_command(require, class_guides: Result) -> None:
    """A pasted guide URL once went upstream as text and answered ok: true with no results.

    The URL comes from the live guide listing; the page its follow-up fetches is the oracle.
    """
    require("wowhead")
    listed = class_guides.data["results"][0]
    found = run(BINARY, "search", listed["url"])
    assert found.data["search_query"] is None, f"the URL was searched upstream\n{found.describe()}"
    assert [row["url"] for row in found.data["results"]] == [listed["url"]], found.describe()
    guide = run_follow_up(found.data["results"][0]["follow_up"]["command"])
    assert guide.data["page"]["canonical_url"] == listed["url"], guide.describe()
    assert guide.data["page"]["title"], guide.describe()


def test_resolve_answers_with_the_faction_a_query_names(require) -> None:
    """``resolve "argent dawn"`` must land on Faction 529, not an item whose name contains the query.

    Wowhead lists the faction only in its category groups, not in the dropdown rows, so this is the
    journey that fails if ``resolve`` stops ranking every row the suggestion response returned.
    """
    require("wowhead")
    resolved = run(BINARY, "resolve", "argent dawn", "--limit", "5")
    match = resolved.data["match"]
    assert (match["entity_type"], match["id"], match["name"]) == ("faction", 529, "Argent Dawn"), resolved.describe()
    assert resolved.data["confidence"] == "high", resolved.describe()
    assert resolved.data["next_command"] == f"{BINARY} entity faction 529", resolved.describe()

    entity = run_follow_up(resolved.data["next_command"])
    assert entity.data["entity"]["name"] == "Argent Dawn", entity.describe()
    assert entity.data["entity"]["page_url"].startswith("https://www.wowhead.com/faction=529"), entity.describe()


def test_a_type_word_in_the_query_still_finds_the_entity(require) -> None:
    """``search "hogger npc"`` once came back empty: Wowhead matches every word it is sent against names.

    The journey fails if Wowhead starts matching type words itself (the retry would go unused, and
    ``search_query`` would keep "npc") or if the retry without them stops finding NPC Hogger.
    """
    require("wowhead")
    resolved = run(BINARY, "resolve", "hogger npc", "--limit", "5")
    assert resolved.data["search_query"] == "hogger", resolved.describe()
    match = resolved.data["match"]
    assert (match["entity_type"], match["id"], match["name"]) == ("npc", 448, "Hogger"), resolved.describe()
    assert resolved.data["next_command"] == f"{BINARY} entity npc 448", resolved.describe()


def test_a_spec_shorthand_guide_query_resolves_to_the_guide(require) -> None:
    """``resolve "resto druid guide"`` must answer with the healer guide although no title says "resto".

    `warcraft guide-compare-query` takes Wowhead's guide only when this resolves.
    """
    require("wowhead")
    resolved = run(BINARY, "resolve", "resto druid guide", "--limit", "5")
    match = resolved.data["match"]
    assert match["entity_type"] == "guide" and "Restoration Druid" in match["name"], resolved.describe()
    assert resolved.data["confidence"] == "high", resolved.describe()
    assert resolved.data["next_command"] == f"{BINARY} guide {match['id']}", resolved.describe()
    guide = run_follow_up(resolved.data["next_command"])
    assert guide.data["guide"]["id"] == match["id"], guide.describe()


def test_a_class_guide_query_lists_current_guides_before_retired_ones(require) -> None:
    """``search "fury warrior guide"`` once led with the retired Legion Remix guide.

    Retired guides whose title contains the whole query outscore several current guides on text
    alone, so only the freshness demotion keeps them below. Wowhead's own ``updated`` dates, not the CLI's
    ``stale_guide`` flag, say which guides are the retired ones.
    """
    require("wowhead")
    found = run(BINARY, "search", FURY_GUIDE_QUERY, "--limit", "10")
    rows = found.data["results"]
    stale = [row for row in rows if "stale_guide" in row["ranking"]["match_reasons"]]
    current = [row for row in rows if row not in stale]
    assert stale and current, f"the page needs current and retired guides for their order to show\n{found.describe()}"
    assert rows == current + stale, f"a retired guide is listed above a current one\n{found.describe()}"
    assert rows[0]["id"] == FURY_GUIDE_ID, found.describe()
    current_dates = [row["metadata"]["updated"] for row in current if row["entity_type"] == "guide"]
    assert max(row["metadata"]["updated"] for row in stale) < min(current_dates), found.describe()

    resolved = run(BINARY, "resolve", FURY_GUIDE_QUERY)
    assert (resolved.data["match"]["id"], resolved.data["confidence"]) == (FURY_GUIDE_ID, "high"), resolved.describe()
    assert resolved.data["next_command"] == f"{BINARY} guide {FURY_GUIDE_ID}", resolved.describe()


def test_a_guide_query_is_never_answered_confidently_with_another_type(require) -> None:
    """``resolve "bm hunter guide"`` once answered the spell "Summon Hunter Guide" with confidence high.

    The query names a guide, and the candidate list holds one; a row of another type that shares
    only some of the words can lead the list but cannot be the confident answer.
    """
    require("wowhead")
    resolved = run(BINARY, "resolve", "bm hunter guide", "--limit", "10")
    candidates = resolved.data["candidates"]
    assert any(row["entity_type"] == "guide" for row in candidates), resolved.describe()
    match = resolved.data["match"]
    assert match["entity_type"] == "guide" or resolved.data["confidence"] != "high", resolved.describe()


def test_the_database_rank_bonus_goes_only_to_rows_that_name_the_query(require) -> None:
    """Wowhead orders database rows on text the suggestion never shows, so that order alone is no evidence.

    ``search "the argent dawn"`` once promoted achievement 18372, "Wards of the Dread Citadel", to
    third place on the word "the". A promoted row has to carry a real query word in its own name.
    A row that names only some of the words (Argent Quartermaster Hasana) still answers the query and
    has to stay on the page; an earlier fix dropped every such row.
    """
    require("wowhead")
    found = run(BINARY, "search", "the argent dawn", "--limit", "30")
    rows = found.data["results"]
    promoted = {
        (row["entity_type"], row["id"]): row["name"] for row in rows if "upstream_database_rank" in row["ranking"]["match_reasons"]
    }
    assert promoted.get(("faction", 529)) == "Argent Dawn", f"the faction lost the bonus it earns\n{found.describe()}"
    assert all(re.search(r"\b(argent|dawn)\b", name, re.IGNORECASE) for name in promoted.values()), found.describe()
    assert 18372 not in {row["id"] for row in rows if row["entity_type"] == "achievement"}, found.describe()
    partial = [row for row in rows if re.search(r"\bargent\b", row["name"], re.IGNORECASE) and not re.search(r"\bdawn\b", row["name"], re.IGNORECASE)]
    assert partial, f"no row naming only 'argent' survived\n{found.describe()}"
    assert all("some_terms_match" in row["ranking"]["match_reasons"] for row in partial), found.describe()


def test_suggestion_type_ids_label_rows_the_way_wowhead_does(require) -> None:
    """Every row's derived ``entity_type`` must agree with Wowhead's own ``typeName`` for its ``type`` id.

    The numeric suggestion ids are the only thing that tells search and resolve what a row is; a
    wrong id (zone rows once came back labelled achievement) mints a follow-up command for another
    page entirely, with ok: true. The response carries both fields, so the check is free.
    """
    require("wowhead")
    seen: dict[str, dict[str, Any]] = {}
    for query in ("elwynn forest", "valorstones"):
        found = run(BINARY, "search", query, "--limit", "10")
        for row in found.data["results"]:
            expected = SUGGESTION_TYPE_NAMES.get(str(row["type_name"]).lower())
            if expected is None:
                # A type this CLI does not map (Storyline, Trading Post Activity, ...): it must not guess one.
                assert row["entity_type"] is None, (
                    f"a {row['type_name']!r} row (type id {row['type_id']}) was labelled "
                    f"{row['entity_type']!r}\n{found.describe()}"
                )
                continue
            assert row["entity_type"] == expected, (
                f"type id {row['type_id']} labelled {row['entity_type']!r} for a {row['type_name']!r} row\n{found.describe()}"
            )
            seen.setdefault(expected, row)

    required = {"zone", "achievement", "currency"}
    assert required <= set(seen), f"searches surfaced no {sorted(required - set(seen))} row to check"
    for entity_type in sorted(required):
        row = seen[entity_type]
        entity = run_follow_up(row["follow_up"]["command"])
        assert entity.data["entity"]["type"] == entity_type, entity.describe()
        assert entity.data["entity"]["id"] == row["id"], entity.describe()
        assert entity.data["entity"]["name"] == row["name"], entity.describe()


def test_entity_routes_a_mount_recipe_and_battle_pet_to_the_page_that_holds_them(require) -> None:
    """Wowhead has no mount/recipe/battle-pet page: those ids must be routed to item/spell/npc pages."""
    require("wowhead")
    names: dict[str, str] = {}
    for entity_type, entity_id, page_prefix in ROUTED_ENTITIES:
        entity = run(
            BINARY, "entity", entity_type, str(entity_id),
            "--no-include-comments", "--linked-entity-preview-limit", "0",
        )
        assert entity.data["entity"]["type"] == entity_type, entity.describe()
        assert entity.data["entity"]["id"] == entity_id, entity.describe()
        assert entity.data["entity"]["page_url"].startswith(page_prefix), entity.describe()
        name = entity.data["entity"]["name"]
        assert name and name in entity.data["tooltip"]["text"], entity.describe()
        names[f"{entity_type}:{entity_id}"] = name

    # compare reads the same pages `entity` does: it once asked the tooltip route of a faction (which
    # has none) and the mount and recipe pages Wowhead does not have.
    compared = run(BINARY, "compare", *names, "--comment-sample", "0", "--max-links-per-entity", "5")
    by_ref = {row["ref"]: row for row in compared.data["entities"]}
    assert set(by_ref) == set(names), compared.describe()
    for entity_type, entity_id, page_prefix in ROUTED_ENTITIES:
        row = by_ref[f"{entity_type}:{entity_id}"]
        assert row["entity"]["page_url"].startswith(page_prefix), compared.describe()
        assert row["summary"]["name"] == names[row["ref"]], compared.describe()


def test_entity_page_links_what_the_page_lists_in_its_relation_tabs(require) -> None:
    """A zone's quests and NPCs live in the page's relation tabs, not in its body links.

    ``entity-page zone 12`` (Elwynn Forest) once reported hundreds of items and none of the zone's
    quests or NPCs.
    """
    require("wowhead")
    page = run(BINARY, "entity-page", "zone", str(ELWYNN_FOREST_ZONE_ID), "--max-links", "2000")
    listed = [row for row in page.data["linked_entities"]["items"] if "listview" in (row.get("sources") or [row.get("source_kind")])]
    listed_types = {row["entity_type"] for row in listed}
    assert {"quest", "npc"} <= listed_types, page.describe()
    # A tab row that merged into an earlier body or gatherer link keeps its tab id too.
    assert all(row["listview"] for row in listed), page.describe()

    # Isle of Dorn links more entities than entity-page can return, and the entity preview says so.
    dorn = run(BINARY, "entity", "zone", str(ISLE_OF_DORN_ZONE_ID), "--no-include-comments")
    preview = dorn.data["linked_entities"]
    assert preview["count"] > 2000 and preview["fetch_more_truncated"] is True, dorn.describe()


def test_the_entity_preview_leads_with_the_npc_that_drops_the_item(require) -> None:
    """``entity item 50818`` (Invincible's Reins) once previewed Onyxia, a comment link, and nothing marked
    The Lich King, whom the page's "dropped-by" tab names, as the dropper."""
    require("wowhead")
    entity = run(BINARY, "entity", "item", "50818", "--no-include-comments")
    npc = next(row for row in entity.data["linked_entities"]["items"] if row["type"] == "npc")
    assert (npc["id"], npc["listview"]) == (36597, "dropped-by"), entity.describe()
    sample = npc["listview_data"]
    assert isinstance(sample["count"], int) and sample["outof"] > sample["count"] > 0, entity.describe()


def test_a_mount_battle_pet_or_quest_word_still_resolves_to_the_entity(require) -> None:
    """Wowhead's suggestions carry no mount or battle-pet type: "Mimiron's Head mount" and "Mr. Bigglesworth
    battle pet" once answered nothing, and "Heritage of the Lightforged quest" resolved low because the type
    word kept the quest from matching its name exactly."""
    require("wowhead")
    for query, sent, expected in (
        ("Mimiron's Head mount", "mimiron's head", ("item", 45693)),
        ("Mr. Bigglesworth battle pet", "mr. bigglesworth", ("npc", 16998)),
        ("Heritage of the Lightforged quest", "heritage of the lightforged", ("quest", 49782)),
    ):
        resolved = run(BINARY, "resolve", query, "--limit", "5")
        match = resolved.data["match"]
        assert resolved.data["search_query"] == sent, resolved.describe()
        assert (match["entity_type"], match["id"]) == expected, resolved.describe()
        assert resolved.data["confidence"] == "high", resolved.describe()


def test_entity_page_facts_give_a_quest_chain_its_giver_and_an_npc_location(require) -> None:
    """Quest 24748 closes the Shadowmourne chain that Highlord Darion Mograine (NPC 37120) starts and ends;
    the infobox and map data that say so were never read."""
    require("wowhead")
    quest = run(BINARY, "entity-page", "quest", "24748").data["facts"]
    assert quest["start"]["id"] == quest["end"]["id"] == 37120, quest
    chain = quest["series"][0]
    assert [step["position"] for step in chain] == list(range(1, len(chain) + 1)), quest
    assert chain[-1] == {**chain[-1], "id": 24748, "current": True}, quest
    assert any(line.startswith("Side: ") for line in quest["quick_facts"]), quest

    # The compact summary counts the spawns; entity-page lists their coordinates.
    npc = run(BINARY, "entity", "npc", "37120", "--no-include-comments").data["facts"]
    location = npc["locations"][0]
    assert location["zone"] == "Icecrown Citadel" and location["count"] and "coords" not in location, npc
    npc_page = run(BINARY, "entity-page", "npc", "37120").data["facts"]
    assert npc_page["locations"][0]["coords"], npc_page

def test_resolve_rejects_entity_types_the_suggestion_endpoint_cannot_emit(require) -> None:
    """``resolve`` answers with a database entity, so a type no suggestion row carries is a usage error.

    Filtering on one of these silently removed every row and reported "nothing matched"; the same
    types are legitimate on ``entity``, which is what the message points at.
    """
    require("wowhead")
    for entity_type in ("mount", "recipe", "battle-pet"):
        rejected = run(
            BINARY, "resolve", pins.ITEM_SEARCH_QUERY, "--entity-type", entity_type,
            expect=EXIT_USAGE, error_code="invalid_argument",
        )
        assert entity_type in rejected.payload["error"]["message"], rejected.describe()
        assert "item" in rejected.payload["error"]["message"], "the message must list the types that do work"


def test_search_stream_emits_one_jsonl_record_per_result(require) -> None:
    require("wowhead")
    streamed = run(BINARY, "--stream", "search", pins.SPELL_SEARCH_QUERY, "--limit", "5", stream=True)
    records = stream_records(streamed)
    # The header is the envelope with the streamed rows emptied out and `data.stream` naming them.
    assert streamed.data["results"] == [], "the JSONL header must empty the streamed collection"
    assert streamed.data["stream"] == {"field": "results", "count": len(records)}, streamed.describe()
    assert 0 < len(records) <= 5, streamed.describe()
    assert all(isinstance(row["id"], int) and row["name"] for row in records), streamed.describe()
    assert any(row["entity_type"] == "spell" for row in records), streamed.describe()


def test_discovered_npc_spell_and_quest_each_fetch_as_an_entity(require) -> None:
    require("wowhead")
    discovered: dict[str, dict[str, Any]] = {}
    for entity_type, query in (
        ("npc", pins.NPC_SEARCH_QUERY),
        ("spell", pins.SPELL_SEARCH_QUERY),
        ("quest", "the deadmines"),
    ):
        found = run(BINARY, "search", query, "--limit", "10")
        rows = entity_rows(found, entity_type)
        assert rows, f"no {entity_type} in search {query!r}\n{found.describe()}"
        discovered[entity_type] = rows[0]
        assert rows[0]["url"] == f"https://www.wowhead.com/{entity_type}={rows[0]['id']}", found.describe()

    for entity_type, row in discovered.items():
        entity = run(
            BINARY, "entity", entity_type, str(row["id"]),
            "--no-include-comments", "--linked-entity-preview-limit", "0",
        )
        assert_envelope_data_holds(entity, "entity", "tooltip")
        assert entity.data["entity"]["type"] == entity_type, entity.describe()
        assert entity.data["entity"]["id"] == row["id"], entity.describe()
        # The id came off that search row, so the page it opens must be the thing the row named.
        assert entity.data["entity"]["name"] == row["name"], entity.describe()
        assert row["name"] in entity.data["tooltip"]["text"], entity.describe()


def test_comments_rank_stream_and_match_the_entity_preview(require) -> None:
    require("wowhead")
    entity = run(BINARY, "entity", "item", str(pins.ITEM_ID))
    preview = entity.data["comments"]
    assert preview["count"] > len(preview["top"]) > 0, entity.describe()
    assert preview["needs_raw_fetch"] is True, "a sampled preview must say a raw fetch is still needed"
    preview_ids = {row["id"] for row in preview["top"]}

    ranked = run(
        BINARY, "comments", "item", str(pins.ITEM_ID),
        "--limit", "5", "--sort", "rating", "--insights", "--insight-limit", "2",
    )
    assert_envelope_data_holds(ranked, "comments", "counts", "citations")
    rows = ranked.data["comments"]
    assert ranked.data["counts"]["returned_comments"] == len(rows) > 0, ranked.describe()
    ratings = [row["rating"] for row in rows]
    assert ratings == sorted(ratings, reverse=True), ranked.describe()
    assert preview_ids & {row["id"] for row in rows}, "entity preview and comments disagree on ids"
    for row in rows:
        assert row["body"], ranked.describe()
        assert row["citation_url"].endswith(f"#comments:id={row['id']}"), ranked.describe()
        assert row["source_url"] == ranked.data["entity"]["page_url"], ranked.describe()
    # The intelligence block summarises the whole filtered sample, not just the returned page.
    insights = ranked.data["intelligence"]
    counts = ranked.data["counts"]
    assert insights["sample"]["embedded_total"] == counts["embedded_comments"], ranked.describe()
    assert insights["sample"]["filtered_count"] == counts["filtered_comments"], ranked.describe()
    assert 0 < insights["freshness"]["comment_count"] <= counts["filtered_comments"], ranked.describe()
    assert insights["freshness"]["newest_at"] >= insights["freshness"]["oldest_at"], ranked.describe()
    assert 0 < len(insights["insights"]) <= 2, "--insight-limit did not cap the insight rows"
    for insight in insights["insights"]:
        assert insight["citation_url"].endswith(f"#comments:id={insight['comment_id']}"), ranked.describe()

    streamed = run(BINARY, "--stream", "comments", "item", str(pins.ITEM_ID), "--limit", "5", "--sort", "rating", stream=True)
    assert streamed.data["comments"] == [], "the JSONL header must empty the streamed collection"
    assert [row["id"] for row in stream_records(streamed)] == [row["id"] for row in rows], streamed.describe()


def test_comment_filters_keep_exactly_the_rows_that_pass_them(require) -> None:
    """``--min-rating``/``--min-replies`` must return the unfiltered sample's matching rows, no others.

    Both filters run over the comment dataset embedded in the entity page, so the baseline call and
    the two filtered calls read the same cached page: the comparison is exact, not statistical.
    """
    require("wowhead")
    baseline = run(BINARY, "comments", "item", str(pins.ITEM_ID), "--limit", "500", "--sort", "rating")
    counts = baseline.data["counts"]
    assert counts["returned_comments"] == counts["filtered_comments"], (
        f"the baseline must hold every comment; raise --limit\n{baseline.describe()}"
    )
    rows = baseline.data["comments"]
    ratings = sorted({row["rating"] for row in rows})
    assert len(ratings) > 1, f"every comment shares one rating, so --min-rating cannot be tested here\n{baseline.describe()}"
    bound = ratings[len(ratings) // 2]

    rated = run(BINARY, "comments", "item", str(pins.ITEM_ID), "--limit", "500", "--sort", "rating", "--min-rating", str(bound))
    assert [row["id"] for row in rated.data["comments"]] == [row["id"] for row in rows if row["rating"] >= bound]
    assert 0 < rated.data["counts"]["filtered_comments"] < counts["filtered_comments"], rated.describe()
    # The envelope's query echo is where the comment filters are reported back.
    assert rated.payload["query"]["min_rating"] == bound, rated.describe()

    replied = run(BINARY, "comments", "item", str(pins.ITEM_ID), "--limit", "500", "--sort", "rating", "--min-replies", "1")
    expected_replied = [row["id"] for row in rows if row["nreplies"] >= 1]
    assert expected_replied, f"no comment on the pinned item has a reply\n{baseline.describe()}"
    assert [row["id"] for row in replied.data["comments"]] == expected_replied
    assert len(expected_replied) < len(rows), "--min-replies 1 kept every comment, so it filtered nothing"

    words = sorted(set(re.findall(r"[a-z]{5,}", " ".join(row["body"].lower() for row in rows))))
    keyword = next(word for word in words if 0 < sum(word in row["body"].lower() for row in rows) < len(rows))
    matching = run(
        BINARY, "comments", "item", str(pins.ITEM_ID), "--limit", "500", "--sort", "rating",
        "--keyword", keyword.upper(),
    )
    assert [row["id"] for row in matching.data["comments"]] == [row["id"] for row in rows if keyword in row["body"].lower()]
    assert 0 < len(matching.data["comments"]) < len(rows), matching.describe()

    # A malformed date window is a usage error raised before the page fetch (behind a dead proxy),
    # not an internal error after it.
    run(
        BINARY, "comments", "item", str(pins.ITEM_ID), "--date-from", "2026-13-45",
        expect=EXIT_USAGE, error_code="invalid_argument", env={**dead_proxy_env(), **no_cache_env()},
    )


def test_compare_diffs_two_legendary_items_field_by_field(require) -> None:
    """Two Molten Core legendaries: the field diff, and every link cap cutting lists that are long enough to cut.

    The two pages have to share more than one link, or every shared-link assertion below holds at
    zero whether or not the caps are wired up.
    """
    require("wowhead")
    other = run(BINARY, "resolve", "sulfuras hand of ragnaros", "--entity-type", "item", "--limit", "3")
    assert isinstance(other.data["match"], dict), f"resolve found no item\n{other.describe()}"
    other_id = other.data["match"]["id"]
    assert other_id != pins.ITEM_ID, other.describe()

    compared = run(
        BINARY, "compare", f"item:{pins.ITEM_ID}", f"item:{other_id}",
        "--preset", "gear", "--comment-sample", "0", "--max-links-per-entity", "200",
    )
    assert_envelope_data_holds(compared, "entities", "comparison", "inputs")
    assert compared.data["inputs"] == [f"item:{pins.ITEM_ID}", f"item:{other_id}"]
    by_id = {row["entity"]["id"]: row for row in compared.data["entities"]}
    assert set(by_id) == {pins.ITEM_ID, other_id}, compared.describe()
    assert by_id[pins.ITEM_ID]["summary"]["name"] == pins.ITEM_NAME, compared.describe()
    assert by_id[other_id]["summary"]["name"] == other.data["match"]["name"], compared.describe()

    name_field = compared.data["comparison"]["fields"]["name"]
    assert name_field["all_equal"] is False, compared.describe()
    assert name_field["values"][f"item:{pins.ITEM_ID}"] == pins.ITEM_NAME, compared.describe()
    links = compared.data["comparison"]["linked_entities"]
    assert links["shared_count_returned"] == len(links["shared_items"]) == links["shared_count_total"] > 1, (
        f"the uncapped comparison must return every shared link, and there must be more than one to cap\n"
        f"{compared.describe()}"
    )
    refs = {f"item:{pins.ITEM_ID}", f"item:{other_id}"}
    unique = links["unique_by_entity"]
    assert set(unique) == refs, compared.describe()
    assert all(len(unique[ref]) == links["unique_count_total_by_entity"][ref] > 1 for ref in refs), (
        f"the uncapped comparison must return every unique link, and more than one per item to cap\n{compared.describe()}"
    )

    # The link caps must cut the same lists down, not return a different set of links.
    capped = run(
        BINARY, "compare", f"item:{pins.ITEM_ID}", f"item:{other_id}",
        "--preset", "gear", "--comment-sample", "0", "--max-links-per-entity", "200",
        "--max-shared-links", "1", "--max-unique-links", "1",
    )
    capped_links = capped.data["comparison"]["linked_entities"]
    assert capped_links["shared_count_total"] == links["shared_count_total"], "a cap changed the totals it only reports"
    assert capped_links["shared_items"] == links["shared_items"][:1], capped.describe()
    assert capped_links["shared_count_returned"] == 1, capped.describe()
    assert set(capped_links["unique_by_entity"]) == refs, capped.describe()
    for ref in refs:
        assert capped_links["unique_by_entity"][ref] == unique[ref][:1], capped.describe()
        assert capped_links["unique_count_total_by_entity"][ref] == links["unique_count_total_by_entity"][ref]

    # --max-links-per-entity cuts each entity's own link list; the shared/unique split still covers
    # every link. It once ran on the cut lists, so identical pages reported nothing shared.
    narrow = run(
        BINARY, "compare", f"item:{pins.ITEM_ID}", f"item:{other_id}",
        "--preset", "gear", "--comment-sample", "0", "--max-links-per-entity", "1",
    )
    narrow_links = narrow.data["comparison"]["linked_entities"]
    assert narrow_links["shared_count_total"] == links["shared_count_total"], narrow.describe()
    assert narrow_links["unique_count_total_by_entity"] == links["unique_count_total_by_entity"], narrow.describe()
    for row in narrow.data["entities"]:
        block = row["linked_entities"]
        assert (block["count"], block["truncated"]) == (1, True) and block["total"] > 1, narrow.describe()


def test_linked_graph_walks_out_from_thunderfury(require) -> None:
    require("wowhead")
    graph = run(
        BINARY, "linked-graph", "item", str(pins.ITEM_ID),
        "--depth", "1", "--limit", "12", "--max-fetches", "2",
    )
    assert_envelope_data_holds(graph, "root", "graph")
    assert graph.data["root"]["key"] == f"item:{pins.ITEM_ID}", graph.describe()
    nodes = graph.data["graph"]["nodes"]
    assert graph.data["graph"]["node_count"] == len(nodes) > 1, graph.describe()
    assert f"item:{pins.ITEM_ID}" in {node["key"] for node in nodes}, graph.describe()
    assert {node["entity_type"] for node in nodes} - {"item"}, "the graph never left the root type"
    assert graph.data["graph"]["edge_count"] > 0, graph.describe()
    # Thunderfury's page links to itself; that is not a relation.
    assert all(edge["from"] != edge["to"] for edge in graph.data["graph"]["edges"]), graph.describe()


def test_linked_graph_follows_a_zones_relation_tabs(require) -> None:
    """A zone lists its quests in a relation tab, not in body links; the graph once found two of Elwynn's."""
    require("wowhead")
    graph = run(
        BINARY, "linked-graph", "zone", str(ELWYNN_FOREST_ZONE_ID),
        "--relation", "quest", "--limit", "500", "--max-fetches", "1",
    )
    assert graph.data["root"]["name"] == "Elwynn Forest", graph.describe()
    quests = [node for node in graph.data["graph"]["nodes"] if node["entity_type"] == "quest"]
    assert len(quests) > 50, graph.describe()
    assert "listview" in {edge["source_kind"] for edge in graph.data["graph"]["edges"]}, graph.describe()

    # A misspelt relation used to filter every edge away and answer ok with an empty graph.
    run(
        BINARY, "linked-graph", "item", str(pins.ITEM_ID), "--relation", "npcs",
        expect=EXIT_USAGE, error_code="invalid_argument", env={**dead_proxy_env(), **no_cache_env()},
    )


def test_linked_graph_relation_keeps_exactly_the_matching_root_edges(require) -> None:
    require("wowhead")
    args = ("linked-graph", "item", str(pins.ITEM_ID), "--depth", "1", "--limit", "500", "--max-fetches", "1")
    baseline = run(BINARY, *args)
    assert baseline.data["sampling"]["truncated"] is False, baseline.describe()
    edges = baseline.data["graph"]["edges"]
    expected = [edge for edge in edges if edge["relation"] == "npc"]
    assert 0 < len(expected) < len(edges), baseline.describe()
    filtered = run(BINARY, *args, "--relation", "npc")
    assert filtered.data["graph"]["edges"] == expected, filtered.describe()
    assert {node["key"] for node in filtered.data["graph"]["nodes"]} == {
        baseline.data["root"]["key"], *(edge["to"] for edge in expected),
    }


def test_linked_graph_reports_the_pages_a_fetch_cap_left_unread(require) -> None:
    """``--depth 2`` has to read every child page the root links, so a ``--max-fetches`` that stops
    short is a sample and must say so; it once reported ``truncated: false`` after reading one child.

    The depth-1 graph is the oracle for how many child pages depth 2 asks for: every linked node with
    a page, of which two fetches read the root and one child.
    """
    require("wowhead")
    common = ("linked-graph", "item", str(pins.ITEM_ID), "--limit", "500")
    shallow = run(BINARY, *common, "--depth", "1", "--max-fetches", "1")
    assert shallow.data["sampling"]["truncated"] is False, shallow.describe()
    children = [node for node in shallow.data["graph"]["nodes"] if node["key"] != f"item:{pins.ITEM_ID}" and node.get("url")]
    assert len(children) > 2, f"the root links too few pages for a fetch cap to cut\n{shallow.describe()}"

    capped = run(BINARY, *common, "--depth", "2", "--max-fetches", "2")
    sampling = capped.data["sampling"]
    assert sampling["pages_fetched"] == 2, capped.describe()
    assert sampling["truncated"] is True, capped.describe()
    assert sampling["pages_skipped"] == len(children) - 1, capped.describe()
    assert all(edge["from"] != edge["to"] for edge in capped.data["graph"]["edges"]), capped.describe()


def test_guide_listing_leads_to_one_guide_and_its_full_hydration(
    require, class_guides: Result, class_guide_baseline: Result, guide_id: int
) -> None:
    require("wowhead")
    assert_envelope_data_holds(class_guides, "results", "count", "guides_url")
    assert class_guides.data["category"] == "classes"
    assert class_guides.data["guides_url"] == "https://www.wowhead.com/guides/classes"
    rows = class_guides.data["results"]
    # The category holds thousands of guides, so a five-row page is always full.
    assert len(rows) == 5, class_guides.describe()
    updated = [datetime.fromisoformat(row["last_updated"]) for row in rows]
    assert updated == sorted(updated, reverse=True), "--sort updated did not sort"
    # The sort has to run before the limit: re-sorting the default first page would miss the newest
    # guide further down Wowhead's own order.
    baseline_rows = class_guide_baseline.data["results"]
    unsorted = [datetime.fromisoformat(row["last_updated"]) for row in baseline_rows if isinstance(row["last_updated"], str)]
    assert max(unsorted) > max(unsorted[:5]), f"the default first page already holds the newest guide\n{class_guide_baseline.describe()}"
    assert updated[0] >= max(unsorted), class_guides.describe()
    assert all(row["url"].startswith("https://www.wowhead.com/guide/") for row in rows)
    assert class_guides.data["facets"]["authors"], class_guides.describe()

    summary = run(BINARY, "guide", str(guide_id), "--comment-sample", "1", "--linked-entity-preview-limit", "3")
    assert_envelope_data_holds(summary, "guide", "page", "linked_entities")
    assert summary.data["guide"]["id"] == guide_id, summary.describe()
    assert summary.data["page"]["title"], summary.describe()
    assert summary.data["citations"]["page"] == summary.data["guide"]["page_url"], summary.describe()
    # `count` is what the page links, `items` is the preview: the flag caps the preview alone.
    preview = summary.data["linked_entities"]
    assert len(preview["items"]) == min(3, preview["count"]) > 0, summary.describe()

    full = run(BINARY, "guide-full", str(guide_id), "--max-links", "25")
    assert_envelope_data_holds(full, "guide", "body", "linked_entities")
    assert full.data["guide"]["id"] == guide_id, full.describe()
    assert full.data["page"]["title"] == summary.data["page"]["title"], "guide and guide-full disagree on title"
    sections = full.data["body"]["sections"]
    assert sections and all(section.get("title") for section in sections), full.describe()
    assert full.data["body"]["raw_markup"], "guide-full dropped the raw guide markup"
    assert full.data["linked_entities"]["count"] == len(full.data["linked_entities"]["items"]) > 0
    assert full.data["navigation"]["links"], full.describe()


def test_guide_patch_filters_cut_the_listing_down_to_their_patch_window(require, class_guide_baseline: Result) -> None:
    """``--patch-min``/``--patch-max`` must drop guides outside the window, from the whole category.

    The class category holds thousands of guides, so the exact rows a ``--limit`` returns cannot be
    enumerated; ``total_matches`` counts every guide that passed the filters before the limit, which
    is what proves a filter removed rows rather than just reordering the page.
    """
    require("wowhead")
    baseline = class_guide_baseline
    total = baseline.data["total_matches"]
    patches = sorted({row["patch"] for row in baseline.data["results"] if isinstance(row["patch"], int)})
    assert len(patches) > 1, f"every class guide shares one patch build\n{baseline.describe()}"

    newer = run(BINARY, "guides", "classes", "--limit", "200", "--patch-min", str(patches[-1]))
    assert newer.data["filters"]["patch_min"] == patches[-1], newer.describe()
    assert all(row["patch"] >= patches[-1] for row in newer.data["results"]), newer.describe()
    assert 0 < newer.data["total_matches"] < total, "--patch-min kept every guide"

    older = run(BINARY, "guides", "classes", "--limit", "200", "--patch-max", str(patches[0]))
    assert all(row["patch"] <= patches[0] for row in older.data["results"]), older.describe()
    assert 0 < older.data["total_matches"] < total, "--patch-max kept every guide"
    # The two windows overlap on nothing and together cover every guide that carries a patch build.
    assert newer.data["total_matches"] + older.data["total_matches"] <= total, "the windows double-counted guides"


def test_guide_update_bounds_remove_guides_on_both_sides_of_a_discovered_date(require, class_guide_baseline: Result) -> None:
    require("wowhead")
    baseline = class_guide_baseline
    dates = sorted({row["last_updated"] for row in baseline.data["results"] if row.get("last_updated")})
    assert len(dates) > 2, baseline.describe()
    bound = dates[len(dates) // 2]
    timestamp = datetime.fromisoformat(bound)
    for flag, newer in (("--updated-after", True), ("--updated-before", False)):
        filtered = run(BINARY, "guides", "classes", "--limit", "200", flag, bound)
        assert 0 < filtered.data["total_matches"] < baseline.data["total_matches"], filtered.describe()
        assert filtered.data["results"], filtered.describe()
        assert all(
            row.get("last_updated") and (
                datetime.fromisoformat(row["last_updated"]) >= timestamp if newer
                else datetime.fromisoformat(row["last_updated"]) <= timestamp
            )
            for row in filtered.data["results"]
        ), filtered.describe()


def test_guide_export_writes_a_bundle_the_bundle_commands_can_query(
    require, guide_id: int, out_dir: Path
) -> None:
    require("wowhead")
    bundle_dir = out_dir / f"guide-{guide_id}"
    exported = run(BINARY, "guide-export", str(guide_id), "--out", str(bundle_dir), "--max-links", "25")
    assert_envelope_data_holds(exported, "guide", "counts", "files")
    assert exported.data["guide"]["id"] == guide_id, exported.describe()
    assert exported.data["counts"]["sections"] > 0, exported.describe()
    for name in exported.data["files"].values():
        assert (bundle_dir / name).is_file(), f"{bundle_dir / name} was not written"
    for name in ("guide.json", "page.html", "sections.jsonl", "manifest.json", "body.markup.txt"):
        assert (bundle_dir / name).stat().st_size > 0, f"{bundle_dir / name} is empty"
    manifest = json.loads((bundle_dir / "manifest.json").read_text())
    assert manifest["guide"]["id"] == guide_id
    # `warcraft guide-compare` names each bundle's provider from this field.
    assert manifest["provider"] == BINARY, manifest
    section_lines = (bundle_dir / "sections.jsonl").read_text().splitlines()
    assert len(section_lines) == exported.data["counts"]["sections"]
    # Inline [spell=ID] / [item=ID] markup reads as the entity's name, or guide-query cannot find a
    # section by the ability it discusses. The page's own gatherer names are the oracle.
    linked = json.loads((bundle_dir / "guide.json").read_text())["linked_entities"]["items"]
    names = {(row["entity_type"], row["id"]): row["name"] for row in linked}
    inline = [
        (names[(kind, int(ident))], section["content_text"])
        for section in map(json.loads, section_lines)
        for kind, ident in re.findall(r"\[(spell|item)=(\d+)", section["content_raw"])
        if (kind, int(ident)) in names
    ]
    assert inline, f"guide {guide_id} links no named spell or item inline"
    assert [name for name, text in inline if name not in text] == [], "section text dropped inline entity names"
    assert (out_dir / "index.json").is_file(), "the corpus index was not written next to the bundle"

    query = run(BINARY, "guide-query", str(bundle_dir), "guide", "--limit", "3", "--kind", "sections")
    # The match payload icy-veins and method guide-query answer with.
    assert_envelope_data_holds(query, "bundle", "guide", "count", "match_counts", "matches", "top", "failed_pages")
    assert query.data["guide"]["id"] == guide_id, query.describe()
    assert query.data["matches"]["sections"], query.describe()
    assert query.data["failed_pages"] == {"count": 0, "items": []}, query.describe()
    assert {row["kind"] for row in query.data["top"]} == {"section"}, query.describe()
    # A linked entity's own name finds it, and the source filter keeps only links the body made.
    entity = next(row for row in linked if "href" in (row.get("sources") or []))
    links = run(
        BINARY, "guide-query", str(bundle_dir), entity["name"], "--kind", "linked_entities", "--linked-source", "href"
    )
    assert entity["id"] in [row["id"] for row in links.data["matches"]["linked_entities"]], links.describe()
    assert all("href" in row["sources"] for row in links.data["matches"]["linked_entities"]), links.describe()
    assert links.data["match_counts"]["sections"] == 0, links.describe()

    listed = run(BINARY, "guide-bundle-list", "--root", str(out_dir))
    assert_envelope_data_holds(listed, "bundles", "count")
    assert [row["guide_id"] for row in listed.data["bundles"]] == [guide_id], listed.describe()
    assert listed.data["bundles"][0]["freshness"]["bundle"] == "fresh", listed.describe()

    searched = run(BINARY, "guide-bundle-search", str(guide_id), "--root", str(out_dir))
    assert [row["guide_id"] for row in searched.data["matches"]] == [guide_id], searched.describe()

    corpus = run(BINARY, "guide-bundle-query", "guide", "--root", str(out_dir), "--limit", "3")
    assert corpus.data["searched_bundle_count"] == 1, corpus.describe()
    assert [row["guide_id"] for row in corpus.data["bundles"]] == [guide_id], corpus.describe()
    # The guide-query match shape: counts per kind, and top rows that name their kind, score and bundle.
    assert corpus.data["counts"]["build_references"] == 0, corpus.describe()
    assert corpus.data["top"], corpus.describe()
    assert all(row["kind"] and row["score"] > 0 and row["bundle"]["guide_id"] == guide_id for row in corpus.data["top"])

    inspected = run(BINARY, "guide-bundle-inspect", str(guide_id), "--root", str(out_dir))
    assert inspected.data["guide"]["id"] == guide_id, inspected.describe()
    assert inspected.data["counts"]["observed"] == inspected.data["counts"]["manifest"], inspected.describe()
    assert inspected.data["issues"] == [], inspected.describe()

    rebuilt = run(BINARY, "guide-bundle-index-rebuild", "--root", str(out_dir))
    assert rebuilt.data["count"] == 1, rebuilt.describe()
    assert rebuilt.data["index"]["current"]["valid"] is True, rebuilt.describe()

    refreshed = run(BINARY, "guide-bundle-refresh", str(guide_id), "--root", str(out_dir), "--force")
    assert refreshed.data["guide"]["id"] == guide_id, refreshed.describe()
    assert refreshed.data["refresh"] == {"updated": True, "reason": "forced", "max_age_hours": 24}
    assert refreshed.data["exported_at"] >= exported.data["exported_at"], refreshed.describe()


def test_guide_export_hydrates_linked_entities_and_a_plain_reexport_drops_them(
    require, guide_id: int, out_dir: Path
) -> None:
    """A re-export without hydration once kept the old entities manifest, so inspect flagged the fresh bundle."""
    require("wowhead")
    bundle_dir = out_dir / "hydrated"
    hydrated = run(
        BINARY, "guide-export", str(guide_id), "--out", str(bundle_dir), "--max-links", "25",
        "--hydrate-linked-entities", "--hydrate-type", "spell,item", "--hydrate-limit", "2",
    )
    hydration = hydrated.data["hydration"]
    assert sum(hydration["source_counts"].values()) == hydrated.data["counts"]["hydrated_entities"] == 2, hydrated.describe()
    entities_manifest = json.loads((bundle_dir / "entities" / "manifest.json").read_text())
    assert all((bundle_dir / row["path"]).is_file() for row in entities_manifest["items"]), entities_manifest

    run(BINARY, "guide-export", str(guide_id), "--out", str(bundle_dir), "--max-links", "25")
    inspected = run(BINARY, "guide-bundle-inspect", str(bundle_dir))
    assert inspected.data["issues"] == [], inspected.describe()


def test_guide_bundle_refresh_rereads_the_dataset_the_bundle_was_exported_from(require, out_dir: Path) -> None:
    """Refreshing a classic bundle with no ``--expansion`` re-reads classic, not retail.

    Refresh once ignored the expansion the manifest records and re-exported into the bundle from
    the retail site. The guide page's own canonical link says which dataset was read.
    """
    require("wowhead")
    listed = run(BINARY, "--expansion", "classic", "guides", "classes", "--limit", "1")
    guide_id = int(listed.data["results"][0]["id"])
    bundle_dir = out_dir / f"guide-{guide_id}"
    run(BINARY, "--expansion", "classic", "guide-export", str(guide_id), "--out", str(bundle_dir))
    exported_page = json.loads((bundle_dir / "guide.json").read_text())["page"]
    assert "/classic/" in exported_page["canonical_url"], exported_page

    refreshed = run(BINARY, "guide-bundle-refresh", str(guide_id), "--root", str(out_dir), "--force")
    assert (refreshed.data["expansion"], refreshed.data["refresh"]["reason"]) == ("classic", "forced"), refreshed.describe()
    assert json.loads((bundle_dir / "guide.json").read_text())["page"] == exported_page


def test_a_classic_guide_url_is_read_and_exported_as_classic(require, out_dir: Path) -> None:
    """A classic guide URL names its dataset: ``guide`` and ``guide-export`` adopt it with no ``--expansion``.

    Both once labelled and exported the classic page as retail, and a later refresh kept that label.
    The URL comes from the classic guide listing; the page's own canonical link is the oracle.
    """
    require("wowhead")
    listed = run(BINARY, "--expansion", "classic", "guides", "classes", "--limit", "1")
    url = listed.data["results"][0]["url"]
    assert url.startswith("https://www.wowhead.com/classic/guide/"), listed.describe()

    summary = run(BINARY, "guide", url, "--comment-sample", "0", "--linked-entity-preview-limit", "0")
    assert summary.data["expansion"] == "classic", summary.describe()
    assert "/classic/" in summary.data["page"]["canonical_url"], summary.describe()

    bundle_dir = out_dir / "classic-guide"
    run(BINARY, "guide-export", url, "--out", str(bundle_dir))
    manifest = json.loads((bundle_dir / "manifest.json").read_text())
    assert manifest["expansion"] == "classic", manifest


def test_news_listing_leads_to_one_news_post(require, news_listing: Result) -> None:
    require("wowhead")
    assert_envelope_data_holds(news_listing, "results", "count", "news_url")
    assert news_listing.data["news_url"] == "https://www.wowhead.com/news"
    assert news_listing.data["scan"]["total_pages"] >= news_listing.data["scan"]["pages_scanned"] >= 1
    rows = news_listing.data["results"]
    assert 0 < len(rows) <= 5, news_listing.describe()
    # A --limit that cut the scan short has to say so instead of reading as the whole listing.
    assert news_listing.data["truncated"] is True, news_listing.describe()
    assert news_listing.data["total_matches"] > news_listing.data["count"] == len(rows), news_listing.describe()
    # Wowhead scopes some posts under an expansion path, for example /forever/news/<slug>.
    assert all("/news/" in row["url"] and row["title"] for row in rows), news_listing.describe()
    assert all(row["url"].startswith("https://www.wowhead.com/") for row in rows), news_listing.describe()
    assert news_listing.data["facets"]["authors"], news_listing.describe()

    post = run(BINARY, "news-post", rows[0]["url"], "--related-limit", "2")
    assert_envelope_data_holds(post, "post", "content", "citations")
    assert post.data["post"]["page_url"] == rows[0]["url"], post.describe()
    # The URL is an echo of the input; the title is the only field that proves which post was read.
    assert post.data["post"]["title"] == rows[0]["title"], "news-post read a different post than the listing row"
    assert post.data["content"]["text"].strip(), "news-post returned an empty body"
    assert post.data["content"]["section_count"] == len(post.data["content"]["sections"])
    assert post.data["citations"]["page"] == post.data["post"]["page_url"], post.describe()
    assert post.data["related"], f"the post links no related entity, so --related-limit caps nothing\n{post.describe()}"
    for name, bucket in post.data["related"].items():
        assert bucket["count"] == len(bucket["items"]) == min(2, bucket["total"]) > 0, f"{name}\n{post.describe()}"
        assert bucket["truncated"] is (bucket["total"] > 2), f"{name}\n{post.describe()}"


def test_news_date_window_returns_the_posts_inside_it_and_says_what_it_could_not_read(
    require, news_scan: Result
) -> None:
    """``--date-from``/``--date-to`` must select on Wowhead's rendered timestamps, not silently drop everything.

    Wowhead renders ``2026/09/18 at 6:05 PM`` rather than an ISO timestamp; when that parse broke,
    every dated query answered ``count: 0`` with ``ok: true``. The window bounds are read off the
    unfiltered scan, so this compares the same two pages against themselves.
    """
    require("wowhead")
    baseline = news_scan
    assert baseline.data["truncated"] is False, f"raise --limit; the baseline must hold every post\n{baseline.describe()}"
    assert baseline.data["scan"]["unparsed_timestamps"] == 0, (
        f"Wowhead's listing timestamps stopped parsing\n{baseline.describe()}"
    )
    rows = baseline.data["results"]
    assert rows, baseline.describe()
    days = sorted({row["posted_at"][:10] for row in rows})
    assert len(days) > 1, f"the two-page news window covers a single day\n{baseline.describe()}"

    recent = run(BINARY, "news", "--pages", "2", "--limit", "200", "--date-from", days[-1])
    assert recent.data["filters"]["date_from"] == f"{days[-1]}T00:00:00+00:00", recent.describe()
    assert {row["id"] for row in recent.data["results"]} == {row["id"] for row in rows if row["posted_at"][:10] >= days[-1]}
    assert 0 < recent.data["count"] < len(rows), "--date-from returned the whole scan"
    assert all(row["posted_at"][:10] == days[-1] for row in recent.data["results"]), recent.describe()

    oldest = run(BINARY, "news", "--pages", "2", "--limit", "200", "--date-to", days[0])
    assert {row["id"] for row in oldest.data["results"]} == {row["id"] for row in rows if row["posted_at"][:10] <= days[0]}
    assert 0 < oldest.data["count"] < len(rows), "--date-to returned the whole scan"


def test_news_page_starts_the_scan_on_the_page_it_names(require, news_scan: Result) -> None:
    """``--page 2`` returns the second page of the unfiltered two-page scan, not the first again.

    Both listing pages are in the session cache after the scan, so these reads are exact.
    """
    require("wowhead")
    first = run(BINARY, "news", "--limit", "200")
    second = run(BINARY, "news", "--page", "2", "--limit", "200")
    assert second.data["scan"]["page"] == 2, second.describe()
    first_ids = [row["id"] for row in first.data["results"]]
    second_ids = [row["id"] for row in second.data["results"]]
    assert first_ids and second_ids, second.describe()
    assert not set(first_ids) & set(second_ids), "page 2 repeated rows of page 1"
    assert first_ids + second_ids == [row["id"] for row in news_scan.data["results"]], second.describe()


# Words that longer words contain: a substring or prefix filter would keep "damage" for "mage",
# "during" for "ring", "classic" for "class" and "warcraft" for "war".
LISTING_QUERY_WORDS = ("mage", "ring", "class", "war")
# The listing fields the topic filter reads.
LISTING_TEXT_FIELDS = ("title", "preview", "body_preview", "author", "topic", "type_name", "forum_area", "forum")


def _listing_text(row: dict[str, Any]) -> str:
    """The row's filtered fields, lowercased, with apostrophes dropped so "Mage's" reads as one word."""
    return " ".join(str(row.get(name) or "") for name in LISTING_TEXT_FIELDS).lower().replace("'", "").replace("’", "")


def _carries_the_word(word: str, text: str) -> bool:
    """``word`` is a word of ``text`` up to a plural or possessive ending ("Mages", "Mage's"), not part of "Magelord".

    "-es" is a plural ending only after a sibilant ("classes"), so "wares" does not carry "war".
    """
    sibilants = ("s", "x", "z", "ch", "sh")
    return any(
        token in (word, f"{word}s")
        or word == f"{token}s"
        or (token == f"{word}es" and word.endswith(sibilants))
        or (word == f"{token}es" and token.endswith(sibilants))
        for token in re.findall(r"\w+", text)
    )


def test_news_topic_query_keeps_the_rows_that_carry_the_word(require, news_scan: Result) -> None:
    """``news mage`` once returned every post that said "damage" or "image", in page order.

    The query word has to be a word of the row, up to a plural or possessive ending; the expected
    rows are read off the unfiltered scan, and the word chosen is one that also sits inside longer
    words there, so a substring or prefix filter would return more.
    """
    require("wowhead")
    texts = [(row["id"], _listing_text(row)) for row in news_scan.data["results"]]
    for word in LISTING_QUERY_WORDS:
        expected = [row_id for row_id, text in texts if _carries_the_word(word, text)]
        inside_only = [row_id for row_id, text in texts if word in text and not _carries_the_word(word, text)]
        if expected and inside_only:
            break
    else:
        raise AssertionError(f"no word in {LISTING_QUERY_WORDS} both is and hides inside words of the scan")
    matched = run(BINARY, "news", word, "--pages", "2", "--limit", "200")
    assert [row["id"] for row in matched.data["results"]] == expected, matched.describe()


def test_listing_field_filters_keep_exactly_the_rows_that_carry_that_value(
    require, news_scan: Result, blue_listing: Result
) -> None:
    """``--type``/``--author`` select on the listing field, exactly, over the rows already scanned.

    Both filters are exact case-insensitive matches on a field the unfiltered listing reports, so
    the expected row set is computable: each filtered call must return that set and nothing else.
    The filter value is taken from the baseline's own facets, which is what makes the bound bite —
    a facet with more than one value cannot select every row.
    """
    require("wowhead")
    news_rows = news_scan.data["results"]
    news_types = news_scan.data["facets"]["types"]
    assert len(news_types) > 1, f"the scanned news window carried one type, so --type filters nothing\n{news_scan.describe()}"
    typed = run(BINARY, "news", "--pages", "2", "--limit", "200", "--type", news_types[0])
    assert typed.data["filters"]["types"] == [news_types[0].lower()], typed.describe()
    assert [row["id"] for row in typed.data["results"]] == [row["id"] for row in news_rows if row["type_name"] == news_types[0]]
    assert 0 < typed.data["count"] < len(news_rows), "--type returned the whole scan"

    news_authors = news_scan.data["facets"]["authors"]
    assert len(news_authors) > 1, f"the scanned news window has one author\n{news_scan.describe()}"
    by_author = run(BINARY, "news", "--pages", "2", "--limit", "200", "--author", news_authors[0])
    assert by_author.data["filters"]["authors"] == [news_authors[0].lower()], by_author.describe()
    assert [row["id"] for row in by_author.data["results"]] == [row["id"] for row in news_rows if row["author"] == news_authors[0]]
    assert 0 < by_author.data["count"] < len(news_rows), "--author returned the whole scan"

    blue_rows = blue_listing.data["results"]
    blue_authors = blue_listing.data["facets"]["authors"]
    assert len(blue_authors) > 1, f"the blue-tracker page has one author\n{blue_listing.describe()}"
    blue_filtered = run(BINARY, "blue-tracker", "--limit", "200", "--author", blue_authors[0])
    assert blue_filtered.data["filters"]["authors"] == [blue_authors[0].lower()], blue_filtered.describe()
    assert {row["id"] for row in blue_filtered.data["results"]} == {
        row["id"] for row in blue_rows if row["author"] == blue_authors[0]
    }
    assert 0 < blue_filtered.data["count"] < len(blue_rows), "--author returned the whole listing"

    forums = blue_listing.data["facets"]["forums"]
    assert forums, blue_listing.describe()
    forum = next(value for value in forums if 0 < sum(str(row.get("forum") or row.get("forum_area") or "").lower() == value.lower() for row in blue_rows) < len(blue_rows))
    by_forum = run(BINARY, "blue-tracker", "--limit", "200", "--forum", forum)
    assert {row["id"] for row in by_forum.data["results"]} == {
        row["id"] for row in blue_rows if str(row.get("forum") or row.get("forum_area") or "").lower() == forum.lower()
    }, by_forum.describe()


def test_blue_tracker_listing_leads_to_one_blue_topic(require, blue_listing: Result) -> None:
    require("wowhead")
    assert_envelope_data_holds(blue_listing, "results", "count", "blue_tracker_url")
    assert blue_listing.data["blue_tracker_url"] == "https://www.wowhead.com/blue-tracker"
    rows = blue_listing.data["results"]
    assert rows, blue_listing.describe()
    assert blue_listing.data["truncated"] is False, f"raise --limit; the baseline must hold the page\n{blue_listing.describe()}"
    assert all(row["url"].startswith("https://www.wowhead.com/blue-tracker/") for row in rows)
    regions = blue_listing.data["facets"]["regions"]
    assert set(regions) <= {"us", "eu"}, blue_listing.describe()
    # Every listed entry's URL carries its own region and id, so the row and its citation agree.
    for row in rows:
        assert f"/{row['region']}/" in row["url"], blue_listing.describe()
        assert row["url"].endswith(f"-{row['id']}"), blue_listing.describe()

    assert len(regions) > 1, f"the tracker page listed one region, so --region filters nothing\n{blue_listing.describe()}"
    filtered = run(BINARY, "blue-tracker", "--limit", "200", "--region", regions[0])
    assert filtered.data["filters"]["regions"] == [regions[0]], filtered.describe()
    assert {row["id"] for row in filtered.data["results"]} == {row["id"] for row in rows if row["region"] == regions[0]}
    assert 0 < filtered.data["count"] < len(rows), "--region returned the whole listing"
    # `na` is every other provider's spelling of `us`; it used to filter every post away.
    north_america = run(BINARY, "blue-tracker", "--limit", "200", "--region", "na")
    assert north_america.data["filters"]["regions"] == ["us"], north_america.describe()
    assert {row["id"] for row in north_america.data["results"]} == {row["id"] for row in rows if row["region"] == "us"}
    run(BINARY, "blue-tracker", "--region", "xx", expect=EXIT_USAGE, error_code="invalid_argument")

    topics = [row for row in rows if "/blue-tracker/topic/" in row["url"]]
    assert topics, f"no forum topic in the listing\n{blue_listing.describe()}"
    # The listing's Blizzard news rows are not forum topics; blue-topic used to fetch one and fail with
    # a parse error (exit 1), and news-post read any Wowhead page, an item's included, as an article.
    offline = {**dead_proxy_env(), **no_cache_env()}
    blizzard_news = "https://www.wowhead.com/blue-tracker/news/us/hotfixes-october-1-2026-world-of-warcraft-blizzard-news-24296142"
    run(BINARY, "blue-topic", blizzard_news, expect=EXIT_USAGE, error_code="invalid_ref", env=offline)
    item_url = f"https://www.wowhead.com/item={pins.ITEM_ID}"
    run(BINARY, "news-post", item_url, expect=EXIT_USAGE, error_code="invalid_ref", env=offline)
    topic = run(BINARY, "blue-topic", topics[0]["url"])
    assert_envelope_data_holds(topic, "topic", "posts", "summary")
    assert topic.data["topic"]["page_url"] == topics[0]["url"], topic.describe()
    posts = topic.data["posts"]
    assert posts["count"] == len(posts["items"]) >= 1, topic.describe()
    assert all(row["author"] and row["body_html"] for row in posts["items"]), topic.describe()
    assert topic.data["summary"]["participants"], topic.describe()
    assert topic.data["citations"]["page"] == topic.data["topic"]["page_url"], topic.describe()


def test_talent_calculator_build_decodes_into_a_transport_packet(require, out_dir: Path) -> None:
    require("wowhead")
    spec = run(BINARY, "talent-calc", TALENT_CALC_SPEC, "--listed-build-limit", "3")
    assert_envelope_data_holds(spec, "tool", "build_identity", "listed_builds")
    assert spec.data["tool"]["class_slug"] == "druid", spec.describe()
    assert spec.data["tool"]["spec_slug"] == "balance", spec.describe()
    assert spec.data["tool"]["has_build_code"] is False, spec.describe()
    identity = spec.data["build_identity"]["class_spec_identity"]["identity"]
    assert identity == {"actor_class": "druid", "spec": "balance"}, spec.describe()
    assert spec.data["tool"]["spec_id"] == BALANCE_SPEC_ID, spec.describe()
    builds = spec.data["listed_builds"]
    assert builds["count"] >= len(builds["items"]) > 0, "the talent-calc page listed no builds"
    # The page embeds every spec's listed builds; a Windwalker build used to come back first here.
    assert {row["spec_id"] for row in builds["items"]} == {BALANCE_SPEC_ID}, spec.describe()
    build_code = builds["items"][0]["hash"]
    assert build_code, spec.describe()

    decoded = run(BINARY, "talent-calc", f"{TALENT_CALC_SPEC}/{build_code}", "--listed-build-limit", "1")
    assert decoded.data["tool"]["build_code"] == build_code, decoded.describe()
    assert decoded.data["tool"]["has_build_code"] is True, decoded.describe()
    assert decoded.data["tool"]["state_url"].endswith(f"{TALENT_CALC_SPEC}/{build_code}"), decoded.describe()
    assert decoded.data["citations"]["page"] == decoded.data["tool"]["state_url"], decoded.describe()

    packet_path = out_dir / "talent-packet.json"
    packet = run(
        BINARY, "talent-calc-packet", f"{TALENT_CALC_SPEC}/{build_code}",
        "--out", str(packet_path), "--listed-build-limit", "1",
    )
    assert_envelope_data_holds(packet, "tool", "talent_transport_packet")
    emitted = packet.data["talent_transport_packet"]
    assert emitted["transport_status"] == "exact", packet.describe()
    assert emitted["transport_forms"]["wowhead_talent_calc_url"].endswith(build_code), packet.describe()
    assert json.loads(packet_path.read_text()) == emitted, "--out wrote something other than the packet"

    # A build code is a loadout string whose header names its spec, so it cannot be relabelled.
    run(
        BINARY, "talent-calc-packet", f"druid/feral/{build_code}",
        expect=EXIT_USAGE, error_code="invalid_tool_ref", env={**dead_proxy_env(), **no_cache_env()},
    )


def test_a_classic_talent_calculator_url_is_a_class_and_a_build_code_not_a_spec(require) -> None:
    """Classic-era calculators have no spec segment: ``/classic/talent-calc/<class>/<build-code>``.

    That shape was read as class plus a spec named after the build code, at high confidence. The
    page Wowhead serves for the URL has to be its Classic talent calculator.
    """
    require("wowhead")
    classic = run(BINARY, "talent-calc", CLASSIC_TALENT_CALC_URL, "--listed-build-limit", "1")
    tool = classic.data["tool"]
    assert classic.data["expansion"] == "classic", classic.describe()
    assert (tool["class_slug"], tool["spec_slug"], tool["build_code"]) == ("warrior", None, CLASSIC_TALENT_CALC_URL.rsplit("/", 1)[1])
    assert classic.data["build_identity"]["confidence"] != "high", classic.describe()
    assert classic.data["page"]["canonical_url"] == "https://www.wowhead.com/classic/talent-calc", classic.describe()
    assert "talent calculator" in (classic.data["page"]["title"] or "").lower(), classic.describe()

    # A path that names no WoW class, or a spec of another class, is a bad reference, refused before any fetch.
    for bad_ref in ("https://www.wowhead.com/talent-calc/notaclass/notaspec", "paladin/frost"):
        run(
            BINARY, "talent-calc", bad_ref,
            expect=EXIT_USAGE, error_code="invalid_tool_ref", env={**dead_proxy_env(), **no_cache_env()},
        )


# One build per classic calculator, each checked against what Wowhead's own calculator renders for it:
# points per tree in the calculator's order and the last talent taken in each tree. All but WoW
# Forever's come from Wowhead's class guides; the Cataclysm URL carries the selection order Wowhead
# appends after clicks, and the WoW Forever build (made in its calculator, which has no guide builds
# yet) is a regression target that ages out if Blizzard reworks its trees before launch.
CLASSIC_CALCULATOR_BUILDS: tuple[tuple[str, str, list[str | None]], ...] = (
    (CLASSIC_TALENT_CALC_URL, "17/34/0", ["Impale", "Bloodthirst", None]),
    ("https://www.wowhead.com/classic/talent-calc/hunter/A_AAzAQ40zc0AA", "20/31/0", ["Ferocity", "Trueshot Aura", None]),
    ("https://www.wowhead.com/tbc/talent-calc/mage/2-5052120123033310531251-053002001", "2/48/11", ["Arcane Subtlety", "Dragon's Breath", "Icy Veins"]),
    (
        "https://www.wowhead.com/wotlk/talent-calc/death-knight/0055101-30505050350203010300233101351-005_001xv611s8q31ts841sxd51s8g",
        "12/54/5",
        ["Rune Tap", "Howling Blast", "Anticipation"],
    ),
    (
        "https://www.wowhead.com/cata/talent-calc/death-knight/20322200112222311321-1-203003_001s8511sy821s7r31s9h41xv261ts871s9r81sxd/0ACFDeeMjkQPNtsRVwz2CAF0w1a",
        "32/1/8",
        ["Dancing Rune Weapon", "Runic Power Mastery", "Morbidity"],
    ),
    ("https://www.wowhead.com/forever/talent-calc/warrior/v2252202-5321011-2541230211002001_t0", "13/13/24", ["Improved Overpower", "Blood Craze", "Focused Rage"]),
    ("https://www.wowhead.com/classic-ptr/talent-calc/warrior/30305001302-05050005525010051", "17/34/0", ["Impale", "Bloodthirst", None]),
)
MOP_CLASSIC_BUILD_URL = "https://www.wowhead.com/mop-classic/talent-calc/druid/balance/323222/AA4FGtB4TpcC4ToOD4F3gE4TpX"


@pytest.mark.parametrize(("url", "points_by_tree", "last_talents"), CLASSIC_CALCULATOR_BUILDS, ids=lambda value: str(value)[:48])
def test_a_classic_calculator_build_decodes_to_the_trees_wowhead_shows(require, url: str, points_by_tree: str, last_talents: list) -> None:
    """The calculator page names its versioned talent data file; the build decodes against that file."""
    require("wowhead")
    result = run(BINARY, "talent-calc", url)
    talents = result.data["talents"]
    assert talents["decoded"] is True, result.describe()
    assert talents["points_by_tree"] == points_by_tree, result.describe()
    assert [tree["talents"][-1]["name"] if tree["talents"] else None for tree in talents["trees"]] == last_talents, result.describe()
    assert re.fullmatch(r"https://nether\.wowhead\.com/[a-z-]+/data/talents-classic\?dv=\d+&db=\d+", talents["data_url"]), result.describe()


def test_a_mop_classic_build_decodes_to_the_talent_chosen_in_each_tier(require) -> None:
    require("wowhead")
    result = run(BINARY, "talent-calc", MOP_CLASSIC_BUILD_URL)
    talents = result.data["talents"]
    assert talents["decoded"] is True, result.describe()
    assert [tier["talent"]["name"] for tier in talents["tiers"]] == [
        "Wild Charge", "Renewal", "Typhoon", "Incarnation", "Ursol's Vortex", "Dream of Cenarius",
    ], result.describe()
    assert talents["glyphs_code"] == MOP_CLASSIC_BUILD_URL.rsplit("/", 1)[1], result.describe()


def test_profession_dressing_room_and_profiler_refs_normalize_and_cite(require) -> None:
    """The three inspectors normalize their opaque ref and read the page that ref belongs to.

    The tool block is derived from the input, so each journey also pins what the fetched page says.
    The profession tree and dressing room: a word from the page's title, which rejects the site
    shell Wowhead serves for an unknown route, and the page's own canonical link as a literal. That
    literal drops the loadout code and the share hash, so the input-built fallback cannot match it.
    The profiler: Wowhead has removed the pinned list, and only that list's page says so. The CLI
    has to fetch it and fail not_found with Wowhead's message; it used to fetch the generic /list
    page and answer ok: true with a canonical URL built from the input.
    """
    require("wowhead")
    profession = run(BINARY, "profession-tree", PROFESSION_TREE_REF)
    assert_envelope_data_holds(profession, "tool", "page", "citations")
    assert profession.data["tool"]["profession_slug"] == "alchemy", profession.describe()
    assert profession.data["tool"]["loadout_code"] == "BCuA", profession.describe()
    assert profession.data["tool"]["state_url"].endswith(PROFESSION_TREE_REF), profession.describe()
    assert profession.data["page"]["canonical_url"] == "https://www.wowhead.com/profession-tree-calc/alchemy", profession.describe()
    profession_title = profession.data["page"]["title"].lower()
    assert "alchemy" in profession_title and "profession tree" in profession_title, profession.describe()

    dressing = run(BINARY, "dressing-room", DRESSING_ROOM_REF)
    assert_envelope_data_holds(dressing, "tool", "page")
    assert dressing.data["tool"]["has_share_hash"] is True, dressing.describe()
    assert dressing.data["tool"]["share_hash"] == DRESSING_ROOM_REF.lstrip("#"), dressing.describe()
    assert dressing.data["tool"]["state_url"] == f"https://www.wowhead.com/dressing-room{DRESSING_ROOM_REF}"
    assert dressing.data["page"]["canonical_url"] == "https://www.wowhead.com/dressing-room", dressing.describe()
    assert "dressing room" in dressing.data["page"]["title"].lower(), dressing.describe()

    profiler = run(BINARY, "profiler", PROFILER_REF, expect=EXIT_NOT_FOUND, error_code="not_found")
    error = profiler.payload["error"]
    assert error["details"]["url"] == f"https://www.wowhead.com/list?list={PROFILER_REF}", profiler.describe()
    assert "This list doesn't exist or has been removed." in error["message"], profiler.describe()

    # Wowhead's own "Default Lists", and the canonical /list=<id>/<slug> URL it reports, which once exited 2.
    default_lists = run(BINARY, "profiler", "1")
    canonical = default_lists.data["page"]["canonical_url"]
    assert canonical == "https://www.wowhead.com/list=1/default-lists", default_lists.describe()
    by_url = run(BINARY, "profiler", canonical)
    assert by_url.data["tool"]["list_id"] == "1", by_url.describe()
    assert by_url.data["page"]["canonical_url"] == canonical, by_url.describe()


def test_global_output_flags_reshape_the_same_entity_payload(require) -> None:
    require("wowhead")
    pretty = run(BINARY, "--pretty", "entity", "item", str(pins.ITEM_ID), "--no-include-comments")
    assert "\n  " in pretty.stdout, "--pretty did not indent"
    assert pretty.data["entity"]["name"] == pins.ITEM_NAME, pretty.describe()

    compact = run(
        BINARY, "--compact", "--compact-max-chars", "60",
        "entity", "item", str(pins.ITEM_ID), "--no-include-comments",
    )
    assert "\n" not in compact.stdout.strip(), "--compact output should stay on one line"
    assert len(compact.data["tooltip"]["text"]) <= 60, compact.describe()
    assert compact.data["tooltip"]["text"].endswith("..."), compact.describe()
    assert "data.tooltip.text" in compact.payload["provenance"]["compacted_paths"], compact.describe()

    # --fields projects the envelope away on purpose, so it cannot go through the envelope contract.
    projected = run_raw(BINARY, "--fields", "data.entity.name", "--fields-strict", "entity", "item", str(pins.ITEM_ID))
    assert projected.exit_code == EXIT_OK, projected.describe()
    assert json.loads(projected.stdout) == {"data": {"entity": {"name": pins.ITEM_NAME}}}, projected.describe()

    missing = run(
        BINARY, "--fields", "data.entity.not_a_field", "--fields-strict",
        "entity", "item", str(pins.ITEM_ID),
        expect=EXIT_USAGE, error_code="missing_fields",
    )
    assert missing.payload["error"]["details"]["missing_fields"] == ["data.entity.not_a_field"], missing.describe()


def test_classic_expansion_profiles_route_the_same_item(require) -> None:
    require("wowhead")
    for key in CLASSIC_EXPANSIONS:
        routed = run(
            BINARY, "--expansion", key, "entity", "item", str(pins.ITEM_ID),
            "--no-include-comments", "--linked-entity-preview-limit", "0",
        )
        assert routed.data["expansion"] == key, routed.describe()
        assert routed.data["expansion_source"] == "flag", routed.describe()
        assert routed.data["entity"]["name"] == pins.ITEM_NAME, routed.describe()
        assert routed.data["entity"]["page_url"] == f"https://www.wowhead.com/{key}/item={pins.ITEM_ID}"
        assert routed.data["tooltip"]["text"], routed.describe()


def test_a_classic_search_follow_up_keeps_the_agent_on_the_classic_dataset(require) -> None:
    """The command search prints has to carry ``--expansion``, or the next call silently reads retail."""
    require("wowhead")
    found = run(BINARY, "--expansion", "classic", "search", pins.ITEM_SEARCH_QUERY, "--limit", "10")
    assert found.data["expansion"] == "classic", found.describe()
    entities = [row for row in found.data["results"] if row["follow_up"]["surface"] == "entity"]
    assert entities, f"the classic search offered no entity to follow up on\n{found.describe()}"
    row = entities[0]
    assert row["url"].startswith("https://www.wowhead.com/classic/"), found.describe()

    command = row["follow_up"]["command"]
    assert command == f"{BINARY} --expansion classic entity {row['entity_type']} {row['id']}", found.describe()
    entity = run_follow_up(command)
    assert entity.data["expansion"] == "classic", entity.describe()
    assert entity.data["entity"]["id"] == row["id"], entity.describe()
    assert entity.data["entity"]["name"] == row["name"], entity.describe()
    # Wowhead appends a name slug to some canonical URLs, so the route is the prefix.
    assert entity.data["entity"]["page_url"].startswith(f"https://www.wowhead.com/classic/{row['entity_type']}={row['id']}")


def test_cache_inspect_counts_entries_and_cache_clear_empties_a_namespace(require, cache_root: Path) -> None:
    require("wowhead")
    namespace = "search_suggestions"
    query = "molten core"
    # Seed an unrelated namespace so the targeted clear at the end has something to leave behind.
    run(BINARY, "entity", "item", str(pins.ITEM_ID), "--no-include-comments")
    run(BINARY, "cache-clear", "--namespace", namespace)

    empty = run(BINARY, "cache-inspect")
    assert_envelope_data_holds(empty, "settings", "stats")
    assert empty.data["settings"]["enabled"] is True, empty.describe()
    assert empty.data["settings"]["backend"] == "file", empty.describe()
    assert str(cache_root) in empty.data["settings"]["cache_dir"], "the journey is not on the isolated cache"
    assert empty.data["stats"]["namespaces"].get(namespace, {}).get("active", 0) == 0, empty.describe()

    first = run(BINARY, "search", query, "--limit", "5")
    warm = run(BINARY, "cache-inspect")
    assert warm.data["stats"]["namespaces"][namespace]["active"] == 1, warm.describe()

    # No payload field reports cache state, so prove the hit structurally: the repeated call returns
    # the identical payload and adds no cache entry.
    second = run(BINARY, "search", query, "--limit", "5")
    assert second.data == first.data, "a cached search returned a different payload"
    reinspected = run(BINARY, "cache-inspect", "--summary", "--hide-zero")
    assert reinspected.data["stats"]["totals"]["active"] == warm.data["stats"]["totals"]["active"], (
        f"the second identical search added a cache entry\n{reinspected.describe()}"
    )
    assert namespace in {row["namespace"] for row in reinspected.data["stats"]["top_namespaces"]}

    # The pre-namespacing file entries are their own namespace; this session wrote none, so clearing
    # the expired ones must leave every live entry in place.
    legacy = run(BINARY, "cache-clear", "--namespace", "legacy_unscoped", "--expired-only")
    assert legacy.data["namespaces"] == ["legacy_unscoped"], legacy.describe()
    assert legacy.data["removed"]["total"] == 0, legacy.describe()
    assert legacy.data["remaining"]["totals"]["active"] == reinspected.data["stats"]["totals"]["active"], legacy.describe()

    # A misspelled namespace once cleared nothing and answered ok; the clear below proves it touched nothing.
    run(BINARY, "cache-clear", "--namespace", "search_suggestion", expect=EXIT_USAGE, error_code="invalid_argument")

    cleared = run(BINARY, "cache-clear", "--namespace", namespace)
    assert cleared.data["namespaces"] == [namespace], cleared.describe()
    assert cleared.data["removed"]["total"] == 1, cleared.describe()
    assert cleared.data["remaining"]["namespaces"].get(namespace, {}).get("active", 0) == 0, cleared.describe()
    assert cleared.data["remaining"]["totals"]["active"] > 0, "clearing one namespace emptied the cache"


def test_a_wow_forever_url_is_not_read_as_retail(require) -> None:
    """Wowhead's /forever/ section is another game; its item page was answered with retail data."""
    require("wowhead")
    forever_item = f"https://www.wowhead.com/forever/item={pins.ITEM_ID}"
    detected = run(BINARY, "expansion-detect", forever_item)
    assert detected.data["detected_expansion"] is None, detected.describe()
    run(
        BINARY, "entity", "--url", forever_item,
        expect=EXIT_USAGE, error_code="invalid_argument", env={**dead_proxy_env(), **no_cache_env()},
    )

    # The Forever news rows `news` emits route to news-post, which says it could not infer an expansion.
    searched = run(BINARY, "search", FOREVER_NEWS_URL)
    assert searched.data["results"][0]["follow_up"]["command"] == f"wowhead news-post {FOREVER_NEWS_URL}", searched.describe()
    post = run(BINARY, "news-post", FOREVER_NEWS_URL)
    assert "ghost wolf" in (post.data["page"]["title"] or "").lower(), post.describe()
    assert post.data["notes"] == [f"Could not infer expansion from URL {FOREVER_NEWS_URL!r}."], post.describe()


def test_an_unknown_guide_category_is_not_found(require) -> None:
    """Wowhead redirects /guides/class to its whole guide index, once answered ok as category `class`."""
    require("wowhead")
    run(BINARY, "guides", "class", expect=EXIT_NOT_FOUND, error_code="not_found")


def test_an_unknown_guide_id_is_not_found(require) -> None:
    """Wowhead answers an unknown /guide=<id> with HTTP 400; it exited 5, which agents retry."""
    require("wowhead")
    missing = run(BINARY, "guide", "99999999", expect=EXIT_NOT_FOUND, error_code="not_found")
    assert missing.payload["error"]["details"]["status_code"] == 400, missing.describe()


def test_resolve_does_not_answer_a_season_query_with_another_season(require) -> None:
    """Wowhead's database order puts "Keystone Legend: Season 2" first for "season 3"; it was resolved at
    high confidence, and still was once a type word ("achievement", "transmog") gave the row `type_hint`."""
    require("wowhead")
    for query, number in (
        ("keystone legend season 3", "3"),
        ("keystone legend season 3 achievement", "3"),
        ("tier 2 warrior transmog", "2"),
    ):
        resolved = run(BINARY, "resolve", query)
        match = resolved.data["match"] or {}
        assert resolved.data["resolved"] is False or number in re.findall(r"\w+", match.get("name") or ""), resolved.describe()


def test_entity_tooltip_text_reads_money_units_from_the_markup(require) -> None:
    """Linen Cloth sells for copper and the Darkmoon Dancing Bear costs prize tickets; the text called
    both gold ("13g", "Cost: 180g"), reading units by position. The tooltip's own markup is the oracle."""
    require("wowhead")
    options = ("--no-include-comments", "--linked-entity-preview-limit", "0")
    linen = run(BINARY, "entity", "item", "2589", *options)
    spans = re.findall(r'class="money(gold|silver|copper)">([^<]*)<', linen.data["tooltip"]["html"])
    assert spans, linen.describe()
    price = " ".join(f"{amount}{unit[0]}" for unit, amount in spans)
    assert f"Sell Price: {price}" in linen.data["tooltip"]["text"], linen.describe()

    bear = run(BINARY, "entity", "item", "73766", *options)
    currency = re.search(r'Cost: </span>(\d+)<a href="[^"]*/currency=\d+[^"]*" aria-label="([^"]+)"', bear.data["tooltip"]["html"])
    assert currency is not None, bear.describe()
    assert f"Cost: {currency.group(1)} {currency.group(2)}" in bear.data["tooltip"]["text"], bear.describe()


def test_search_opens_a_tier_set_with_entity_item_set(require) -> None:
    """Item sets (suggestion type 4) came back with no URL and no follow-up command."""
    require("wowhead")
    found = run(BINARY, "search", "battlegear of wrath")
    row = next((row for row in found.data["results"] if row["type_name"] == "Item Set"), None)
    assert row is not None, found.describe()
    assert row["follow_up"]["command"] == f"wowhead entity item-set {row['id']}", found.describe()
    entity = run_follow_up(row["follow_up"]["command"])
    assert entity.data["entity"]["name"] == row["name"], entity.describe()


def test_guide_refuses_a_wowhead_page_that_is_not_a_guide(require) -> None:
    """`guide` answered the home page, a listing, and an item page as kind=guide."""
    require("wowhead")
    offline = {**dead_proxy_env(), **no_cache_env()}
    for ref in ("https://www.wowhead.com/", "https://www.wowhead.com/items", f"https://www.wowhead.com/item={pins.ITEM_ID}"):
        run(BINARY, "guide", ref, expect=EXIT_USAGE, error_code="invalid_argument", env=offline)


def test_unknown_item_id_is_a_not_found_envelope(require) -> None:
    require("wowhead")
    missing = run(BINARY, "entity", "item", "999999999", expect=EXIT_NOT_FOUND, error_code="not_found")
    assert missing.stdout == "", missing.describe()
    assert missing.payload["data"] == {}, missing.describe()
    assert missing.payload["error"]["details"]["status_code"] == 404, missing.describe()


def test_a_misspelled_entity_type_is_a_usage_error_not_a_missing_entity(require) -> None:
    """`entity items` read Wowhead's tooltip 404 as "Thunderfury does not exist", and `entity-page items`
    answered with the item listing Wowhead redirects `/items=19019` to."""
    require("wowhead")
    for argv in (("entity", "items", str(pins.ITEM_ID)), ("entity-page", "items", str(pins.ITEM_ID))):
        typo = run(BINARY, *argv, expect=EXIT_USAGE, error_code="invalid_argument")
        assert "item" in typo.payload["error"]["message"].split("Known entity types: ", 1)[1], typo.describe()
    # Wowhead's tooltip endpoint has no `class` type but its pages do, so the message points there.
    no_tooltip = run(BINARY, "entity", "class", "1", expect=EXIT_USAGE, error_code="invalid_argument")
    assert "`wowhead entity-page class 1`" in no_tooltip.payload["error"]["message"], no_tooltip.describe()
    run(BINARY, "entity-page", "class", "1")


def test_network_failure_is_an_exit_5_envelope(require) -> None:
    require("wowhead")
    dead = run(BINARY, "search", pins.ITEM_SEARCH_QUERY, expect=EXIT_NETWORK, env={**dead_proxy_env(), **no_cache_env()})
    assert dead.error_code in {"network_error", "timeout"}, dead.describe()
    assert dead.stdout == "", dead.describe()

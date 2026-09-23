"""End-to-end journeys for the ``wowhead`` binary against the live site.

Every volatile identifier (news slug, guide id, comment id, npc/spell/quest id, talent build code)
is discovered at run time from an earlier command in the same journey, so the file cannot rot on a
stale pin. The only pinned entity is Thunderfury (``tests/e2e/pins.py``), plus the three opaque
tool-state refs below that Wowhead only ever mints inside a browser, and the handful of
classic-era ids that pin the entity types whose page lives under another route.
"""

from __future__ import annotations

import json
import re
import shlex
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

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
)

BINARY = "wowhead"

# Class/spec and profession slugs are permanent Wowhead routes.
TALENT_CALC_SPEC = "druid/balance"
PROFESSION_TREE_REF = "alchemy/BCuA"
# Opaque client-side state: Wowhead only mints these in the browser, so they cannot be discovered
# from any listing command. Both are inspectors that only normalize and cite the ref they are given.
DRESSING_ROOM_REF = "#fz8zz0zb89c8mM8YB8mN8X18mO8ub8mP8uD"
PROFILER_REF = "97060220/us/illidan/Roguecane"

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


def test_search_resolve_and_entity_agree_on_thunderfury(require, thunderfury_search: Result) -> None:
    require("wowhead")
    assert_envelope_data_holds(thunderfury_search, "results", "count", "search_url")
    rows = thunderfury_search.data["results"]
    assert thunderfury_search.data["count"] == len(rows) > 0, thunderfury_search.describe()
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


def test_the_database_rank_bonus_goes_only_to_rows_that_name_the_query(require) -> None:
    """Wowhead orders database rows on text the suggestion never shows, so that order alone is no evidence.

    ``search "the argent dawn"`` once promoted achievement 18372, "Wards of the Dread Citadel", to
    third place on the word "the". A promoted row has to carry a real query word in its own name.
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


def test_compare_diffs_two_legendary_items_field_by_field(require) -> None:
    """Two Molten Core legendaries: the field diff, and both link caps cutting lists that are long enough to cut.

    ``--max-links-per-entity`` has to be wide enough that the two pages actually share links, or
    every shared-link assertion below holds at zero whether or not the cap is wired up.
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
    assert (out_dir / "index.json").is_file(), "the corpus index was not written next to the bundle"

    query = run(BINARY, "guide-query", str(bundle_dir), "guide", "--limit", "3", "--kind", "sections")
    assert_envelope_data_holds(query, "guide", "matches")
    assert query.data["guide"]["id"] == guide_id, query.describe()
    assert query.data["matches"]["sections"], query.describe()

    listed = run(BINARY, "guide-bundle-list", "--root", str(out_dir))
    assert_envelope_data_holds(listed, "bundles", "count")
    assert [row["guide_id"] for row in listed.data["bundles"]] == [guide_id], listed.describe()
    assert listed.data["bundles"][0]["freshness"]["bundle"] == "fresh", listed.describe()

    searched = run(BINARY, "guide-bundle-search", str(guide_id), "--root", str(out_dir))
    assert [row["guide_id"] for row in searched.data["matches"]] == [guide_id], searched.describe()

    corpus = run(BINARY, "guide-bundle-query", "guide", "--root", str(out_dir), "--limit", "3")
    assert corpus.data["searched_bundle_count"] == 1, corpus.describe()
    assert [row["guide_id"] for row in corpus.data["bundles"]] == [guide_id], corpus.describe()

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

    topics = [row for row in rows if "/blue-tracker/topic/" in row["url"]]
    assert topics, f"no forum topic in the listing\n{blue_listing.describe()}"
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
    builds = spec.data["listed_builds"]
    assert builds["count"] >= len(builds["items"]) > 0, "the talent-calc page listed no builds"
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


def test_profession_dressing_room_and_profiler_refs_normalize_and_cite(require) -> None:
    """The three inspectors normalize their opaque ref and read the page that ref belongs to.

    The tool block is derived from the input, so each journey also pins the fetched page: a word
    from its own title, which is what rejects the site shell Wowhead serves for an unknown route,
    and its canonical URL as a known literal. The profession tree's canonical URL drops the loadout
    code, so it also proves the CLI read the page's own link rather than falling back to the input;
    the other two pages' canonical URL is the URL fetched, so there only the title can tell.
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

    profiler = run(BINARY, "profiler", PROFILER_REF)
    assert_envelope_data_holds(profiler, "tool", "page")
    assert profiler.data["tool"]["list_parts"] == PROFILER_REF.split("/"), profiler.describe()
    assert profiler.data["tool"]["region_slug"] == pins.REGION, profiler.describe()
    assert profiler.data["tool"]["realm_slug"] == pins.REALM_SLUG, profiler.describe()
    assert profiler.data["tool"]["state_url"] == f"https://www.wowhead.com/list?list={PROFILER_REF}"
    assert profiler.data["page"]["canonical_url"] == f"https://www.wowhead.com/list?list={PROFILER_REF}", profiler.describe()
    assert "profiler" in profiler.data["page"]["title"].lower(), profiler.describe()


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
    entities = [row for row in found.data["results"] if (row.get("follow_up") or {}).get("recommended_surface") == "entity"]
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

    repaired = run(BINARY, "cache-repair", "--dry-run")
    assert repaired.data["repair"]["apply"] is False, repaired.describe()
    assert repaired.data["repair"]["removed"] == 0, repaired.describe()

    cleared = run(BINARY, "cache-clear", "--namespace", namespace)
    assert cleared.data["namespaces"] == [namespace], cleared.describe()
    assert cleared.data["removed"]["total"] == 1, cleared.describe()
    assert cleared.data["remaining"]["namespaces"].get(namespace, {}).get("active", 0) == 0, cleared.describe()
    assert cleared.data["remaining"]["totals"]["active"] > 0, "clearing one namespace emptied the cache"


def test_unknown_item_id_is_a_not_found_envelope(require) -> None:
    require("wowhead")
    missing = run(BINARY, "entity", "item", "999999999", expect=EXIT_NOT_FOUND, error_code="not_found")
    assert missing.stdout == "", missing.describe()
    assert missing.payload["data"] == {}, missing.describe()
    assert missing.payload["error"]["details"]["status_code"] == 404, missing.describe()


def test_network_failure_is_an_exit_5_envelope(require) -> None:
    require("wowhead")
    dead = run(BINARY, "search", pins.ITEM_SEARCH_QUERY, expect=EXIT_NETWORK, env={**dead_proxy_env(), **no_cache_env()})
    assert dead.error_code in {"network_error", "timeout"}, dead.describe()
    assert dead.stdout == "", dead.describe()

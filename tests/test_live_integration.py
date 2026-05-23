from __future__ import annotations

import json
import os
import time
from typing import Any

import pytest
from typer.testing import CliRunner
from wowhead_cli.expansion_profiles import resolve_expansion
from wowhead_cli.main import app

pytestmark = pytest.mark.live

LIVE_ENABLED = os.getenv("WOWHEAD_LIVE_TESTS", "").strip().lower() in {"1", "true", "yes", "on"}
runner = CliRunner()
ENTITY_DISCOVERY_QUERIES: dict[str, str] = {
    "quest": "defias in dustwallow",
    "npc": "defias ringleader",
    "spell": "thunderfury",
}


def _require_live() -> None:
    if not LIVE_ENABLED:
        pytest.skip("Set WOWHEAD_LIVE_TESTS=1 to run live integration tests.")


def _invoke_live(args: list[str], *, attempts: int = 3) -> Any:
    last_result = None
    for attempt in range(1, attempts + 1):
        result = runner.invoke(app, args)
        if result.exit_code == 0:
            return result
        last_result = result
        if attempt < attempts:
            time.sleep(float(attempt))
    assert last_result is not None
    pytest.fail(
        f"Live command failed after {attempts} attempts.\n"
        f"args={args}\n"
        f"exit_code={last_result.exit_code}\n"
        f"output={last_result.output[:2000]}"
    )


def _payload_for(args: list[str]) -> dict[str, Any]:
    result = _invoke_live(args)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        pytest.fail(f"Command did not produce JSON.\nargs={args}\nstdout={result.stdout[:2000]}\n{exc}")
    assert payload.get("ok") is not False
    return payload


def _discover_entity_id(*, expansion_key: str, entity_type: str, query: str) -> int:
    payload = _payload_for(["--expansion", expansion_key, "search", query, "--limit", "10"])
    results = payload.get("results")
    assert isinstance(results, list)
    for row in results:
        if not isinstance(row, dict):
            continue
        if row.get("entity_type") != entity_type:
            continue
        candidate_id = row.get("id")
        if isinstance(candidate_id, int):
            return candidate_id
    pytest.fail(
        f"Could not discover {entity_type!r} via query {query!r} "
        f"for expansion={expansion_key}. results={results}"
    )


@pytest.mark.parametrize("expansion_key", ["retail", "classic", "wotlk", "cata", "mop-classic", "ptr"])
def test_live_search_contract(expansion_key: str) -> None:
    _require_live()
    profile = resolve_expansion(expansion_key)
    payload = _payload_for(["--expansion", expansion_key, "search", "thunderfury", "--limit", "3"])

    assert payload["expansion"] == expansion_key
    assert payload["search_url"].startswith(f"{profile.wowhead_base}/search?q=")
    assert payload["count"] >= len(payload["results"])
    assert len(payload["results"]) > 0
    for row in payload["results"]:
        assert isinstance(row.get("id"), int)
        url = row.get("url")
        if isinstance(url, str):
            assert url.startswith(f"{profile.wowhead_base}/")


@pytest.mark.parametrize("expansion_key", ["retail", "classic", "wotlk", "cata", "mop-classic", "ptr"])
def test_live_entity_contract(expansion_key: str) -> None:
    _require_live()
    profile = resolve_expansion(expansion_key)
    payload = _payload_for(["--expansion", expansion_key, "entity", "item", "19019"])

    assert payload["expansion"] == expansion_key
    expected_page_prefix = (
        "https://www.wowhead.com/item=19019"
        if expansion_key == "ptr"
        else f"{profile.wowhead_base}/item=19019"
    )
    assert payload["entity"]["page_url"].startswith(expected_page_prefix)
    assert payload["citations"]["comments"].startswith(payload["entity"]["page_url"] + "#comments")
    tooltip = payload["tooltip"]
    assert isinstance(payload["entity"].get("name"), str)
    assert isinstance(tooltip.get("icon"), str)
    assert isinstance(tooltip.get("text"), str)
    assert isinstance(tooltip.get("summary"), str)


@pytest.mark.parametrize(
    ("entity_type", "entity_id", "page_prefix"),
    [
        ("faction", 529, "https://www.wowhead.com/faction=529"),
        ("pet", 39, "https://www.wowhead.com/pet=39"),
        ("recipe", 2549, "https://www.wowhead.com/spell=2549"),
        ("mount", 460, "https://www.wowhead.com/item=84101"),
        ("battle-pet", 39, "https://www.wowhead.com/npc=2671"),
    ],
)
def test_live_special_entity_route_contract(entity_type: str, entity_id: int, page_prefix: str) -> None:
    _require_live()
    payload = _payload_for(["entity", entity_type, str(entity_id), "--no-include-comments", "--linked-entity-preview-limit", "0"])

    assert payload["entity"]["type"] == entity_type
    assert payload["entity"]["id"] == entity_id
    assert payload["entity"]["page_url"].startswith(page_prefix)
    tooltip = payload.get("tooltip")
    assert isinstance(tooltip, dict)
    assert isinstance(tooltip.get("text"), str)


@pytest.mark.parametrize("expansion_key", ["retail", "wotlk", "ptr"])
def test_live_entity_page_contract(expansion_key: str) -> None:
    _require_live()
    payload = _payload_for(["--expansion", expansion_key, "entity-page", "item", "19019", "--max-links", "10"])

    assert payload["expansion"] == expansion_key
    assert payload["normalize_canonical_to_expansion"] is False
    expected_page_prefix = (
        "https://www.wowhead.com/item=19019"
        if expansion_key == "ptr"
        else f"{resolve_expansion(expansion_key).wowhead_base}/item=19019"
    )
    assert payload["entity"]["page_url"].startswith(expected_page_prefix)
    assert payload["citations"]["page"] == payload["entity"]["page_url"]
    assert payload["citations"]["comments"] == f'{payload["entity"]["page_url"]}#comments'
    assert payload["linked_entities"]["count"] == len(payload["linked_entities"]["items"])
    assert payload["linked_entities"]["count"] > 0


def test_live_ptr_canonical_normalization_contract() -> None:
    _require_live()
    default_payload = _payload_for(["--expansion", "ptr", "entity-page", "item", "19019", "--max-links", "1"])
    normalized_payload = _payload_for(
        [
            "--expansion",
            "ptr",
            "--normalize-canonical-to-expansion",
            "entity-page",
            "item",
            "19019",
            "--max-links",
            "1",
        ]
    )

    assert default_payload["normalize_canonical_to_expansion"] is False
    assert normalized_payload["normalize_canonical_to_expansion"] is True
    assert normalized_payload["entity"]["page_url"].startswith("https://www.wowhead.com/ptr/item=19019")


@pytest.mark.parametrize("expansion_key", ["retail", "cata", "ptr"])
def test_live_comments_contract(expansion_key: str) -> None:
    _require_live()
    payload = _payload_for(
        [
            "--expansion",
            expansion_key,
            "comments",
            "item",
            "19019",
            "--limit",
            "2",
            "--sort",
            "newest",
            "--hydrate-missing-replies",
        ]
    )

    assert payload["expansion"] == expansion_key
    assert payload["counts"]["returned_comments"] == len(payload["comments"])
    assert payload["counts"]["returned_comments"] > 0
    first = payload["comments"][0]
    assert "#comments:id=" in first["citation_url"]
    assert first["source_url"] == payload["entity"]["page_url"]


@pytest.mark.parametrize("expansion_key", ["retail", "wotlk", "cata"])
def test_live_compare_contract(expansion_key: str) -> None:
    _require_live()
    profile = resolve_expansion(expansion_key)
    payload = _payload_for(
        [
            "--expansion",
            expansion_key,
            "compare",
            "item:19019",
            "item:19351",
            "--comment-sample",
            "0",
            "--max-links-per-entity",
            "10",
        ]
    )

    assert payload["expansion"] == expansion_key
    assert len(payload["entities"]) == 2
    fields = payload["comparison"]["fields"]
    for key in ["name", "quality", "icon", "title"]:
        assert key in fields
        assert isinstance(fields[key]["all_equal"], bool)
    links = payload["comparison"]["linked_entities"]
    assert links["shared_count_total"] >= links["shared_count_returned"]
    for row in payload["entities"]:
        page_url = row["entity"]["page_url"]
        assert page_url.startswith(f"{profile.wowhead_base}/")
        assert row["citations"]["comments"].startswith(page_url + "#comments")
        assert "page" not in row["citations"]
    for link_row in links["shared_items"]:
        assert "citation_url" not in link_row


@pytest.mark.parametrize(
    ("entity_type", "query"),
    [
        ("quest", ENTITY_DISCOVERY_QUERIES["quest"]),
        ("npc", ENTITY_DISCOVERY_QUERIES["npc"]),
        ("spell", ENTITY_DISCOVERY_QUERIES["spell"]),
    ],
)
def test_live_discovered_entity_type_command_flow(entity_type: str, query: str) -> None:
    _require_live()
    expansion_key = "retail"
    profile = resolve_expansion(expansion_key)
    entity_id = _discover_entity_id(expansion_key=expansion_key, entity_type=entity_type, query=query)

    entity_payload = _payload_for(["--expansion", expansion_key, "entity", entity_type, str(entity_id)])
    assert entity_payload["expansion"] == expansion_key
    assert entity_payload["entity"]["type"] == entity_type
    assert entity_payload["entity"]["id"] == entity_id
    assert entity_payload["entity"]["page_url"].startswith(f"{profile.wowhead_base}/{entity_type}={entity_id}")

    page_payload = _payload_for(
        ["--expansion", expansion_key, "entity-page", entity_type, str(entity_id), "--max-links", "10"]
    )
    assert page_payload["entity"]["type"] == entity_type
    assert page_payload["entity"]["id"] == entity_id
    assert page_payload["linked_entities"]["count"] == len(page_payload["linked_entities"]["items"])
    assert page_payload["linked_entities"]["count"] > 0
    assert page_payload["citations"]["page"] == page_payload["entity"]["page_url"]

    comments_payload = _payload_for(
        ["--expansion", expansion_key, "comments", entity_type, str(entity_id), "--limit", "2", "--sort", "newest"]
    )
    returned_comments = comments_payload["counts"]["returned_comments"]
    comments = comments_payload["comments"]
    assert returned_comments == len(comments)
    assert comments_payload["citations"]["comments"] == f'{comments_payload["entity"]["page_url"]}#comments'
    if returned_comments > 0:
        first = comments[0]
        assert "#comments:id=" in first["citation_url"]
        assert first["source_url"] == comments_payload["entity"]["page_url"]
    else:
        assert comments == []


def test_live_compare_contract_for_discovered_mixed_entity_types() -> None:
    _require_live()
    expansion_key = "retail"
    profile = resolve_expansion(expansion_key)
    quest_id = _discover_entity_id(
        expansion_key=expansion_key,
        entity_type="quest",
        query=ENTITY_DISCOVERY_QUERIES["quest"],
    )
    npc_id = _discover_entity_id(
        expansion_key=expansion_key,
        entity_type="npc",
        query=ENTITY_DISCOVERY_QUERIES["npc"],
    )
    spell_id = _discover_entity_id(
        expansion_key=expansion_key,
        entity_type="spell",
        query=ENTITY_DISCOVERY_QUERIES["spell"],
    )
    payload = _payload_for(
        [
            "--expansion",
            expansion_key,
            "compare",
            f"quest:{quest_id}",
            f"npc:{npc_id}",
            f"spell:{spell_id}",
            "--comment-sample",
            "1",
            "--max-links-per-entity",
            "10",
        ]
    )

    assert payload["expansion"] == expansion_key
    assert len(payload["entities"]) == 3
    fields = payload["comparison"]["fields"]
    for key in ["name", "quality", "icon", "title"]:
        assert key in fields
        assert isinstance(fields[key]["all_equal"], bool)
    links = payload["comparison"]["linked_entities"]
    assert links["shared_count_total"] >= links["shared_count_returned"]
    assert len(payload["entities"]) == 3
    for row in payload["entities"]:
        page_url = row["entity"]["page_url"]
        assert page_url.startswith(f"{profile.wowhead_base}/")
        assert row["citations"]["comments"].startswith(page_url + "#comments")
        assert "page" not in row["citations"]
    for link_row in links["shared_items"]:
        assert "citation_url" not in link_row


def test_live_news_contract() -> None:
    _require_live()
    payload = _payload_for(["news", "hotfixes", "--pages", "2", "--limit", "5"])

    assert payload["expansion"] == "retail"
    assert payload["news_url"] == "https://www.wowhead.com/news"
    assert payload["scan"]["pages_scanned"] >= 1
    assert payload["scan"]["total_pages"] >= payload["scan"]["pages_scanned"]
    assert payload["count"] >= len(payload["results"])
    assert len(payload["results"]) > 0
    first = payload["results"][0]
    assert isinstance(first.get("id"), int)
    assert isinstance(first.get("title"), str)
    assert isinstance(first.get("posted"), str)
    assert isinstance(first.get("url"), str)
    assert first["url"].startswith("https://www.wowhead.com/news/")
    assert isinstance(payload["facets"]["authors"], list)
    assert isinstance(payload["facets"]["types"], list)


def test_live_news_type_filter_contract() -> None:
    _require_live()
    payload = _payload_for(["news", "hotfixes", "--type", "live", "--pages", "2", "--limit", "5"])

    assert payload["filters"]["types"] == ["live"]
    assert payload["count"] >= len(payload["results"])
    for row in payload["results"]:
        assert str(row.get("type_name", "")).lower() == "live"


def test_live_blue_tracker_contract() -> None:
    _require_live()
    payload = _payload_for(["blue-tracker", "class tuning", "--pages", "2", "--limit", "5"])

    assert payload["expansion"] == "retail"
    assert payload["blue_tracker_url"] == "https://www.wowhead.com/blue-tracker"
    assert payload["scan"]["pages_scanned"] >= 1
    assert payload["scan"]["total_pages"] >= payload["scan"]["pages_scanned"]
    assert payload["count"] >= len(payload["results"])
    assert len(payload["results"]) > 0
    first = payload["results"][0]
    assert isinstance(first.get("id"), int)
    assert isinstance(first.get("title"), str)
    assert isinstance(first.get("posted"), str)
    assert isinstance(first.get("url"), str)
    assert first["url"].startswith("https://www.wowhead.com/blue-tracker/topic/")
    assert isinstance(payload["facets"]["regions"], list)
    assert isinstance(payload["facets"]["forums"], list)


def test_live_blue_tracker_region_filter_contract() -> None:
    _require_live()
    payload = _payload_for(["blue-tracker", "class tuning", "--region", "eu", "--pages", "2", "--limit", "5"])

    assert payload["filters"]["regions"] == ["eu"]
    assert payload["count"] >= len(payload["results"])
    for row in payload["results"]:
        assert str(row.get("region", "")).lower() == "eu"


def test_live_news_post_contract() -> None:
    _require_live()
    payload = _payload_for(
        [
            "news-post",
            "/news/midnight-hotfixes-for-march-13th-marl-decor-cost-reduction-class-bugfixes-and-380785",
        ]
    )

    assert payload["expansion"] == "retail"
    assert payload["post"]["page_url"].startswith("https://www.wowhead.com/news/")
    assert isinstance(payload["content"]["text"], str)
    assert payload["content"]["text"]
    assert payload["citations"]["page"] == payload["post"]["page_url"]
    assert isinstance(payload.get("related"), dict)


def test_live_blue_topic_contract() -> None:
    _require_live()
    payload = _payload_for(
        [
            "blue-topic",
            "/blue-tracker/topic/eu/class-tuning-incoming-18-march-610948",
        ]
    )

    assert payload["expansion"] == "retail"
    assert payload["topic"]["page_url"].startswith("https://www.wowhead.com/blue-tracker/topic/")
    assert payload["posts"]["count"] >= 1
    first = payload["posts"]["items"][0]
    assert isinstance(first.get("author"), str)
    assert isinstance(first.get("body_text"), str)
    assert isinstance(payload["summary"]["participants"], list)
    assert payload["citations"]["page"] == payload["topic"]["page_url"]


def test_live_guide_category_contract() -> None:
    _require_live()
    payload = _payload_for(["guides", "classes", "death knight", "--limit", "5"])

    assert payload["expansion"] == "retail"
    assert payload["category"] == "classes"
    assert payload["guides_url"] == "https://www.wowhead.com/guides/classes"
    assert payload["count"] >= len(payload["results"])
    assert len(payload["results"]) > 0
    first = payload["results"][0]
    assert isinstance(first.get("id"), int)
    assert isinstance(first.get("title"), str)
    assert isinstance(first.get("url"), str)
    assert first["url"].startswith("https://www.wowhead.com/guide/")
    assert isinstance(payload["facets"]["authors"], list)
    assert isinstance(payload["facets"]["category_paths"], list)


def test_live_guide_category_patch_filter_contract() -> None:
    _require_live()
    payload = _payload_for(["guides", "classes", "--patch-min", "120001", "--limit", "5"])

    assert payload["filters"]["patch_min"] == 120001
    assert payload["count"] >= len(payload["results"])
    for row in payload["results"]:
        patch = row.get("patch")
        assert isinstance(patch, int)
        assert patch >= 120001


def test_live_guide_category_updated_sort_contract() -> None:
    _require_live()
    payload = _payload_for(["guides", "classes", "--sort", "updated", "--limit", "5"])

    assert payload["filters"]["sort"] == "updated"
    timestamps = [row.get("last_updated") for row in payload["results"] if isinstance(row.get("last_updated"), str)]
    assert len(timestamps) >= 2
    assert timestamps == sorted(timestamps, reverse=True)


def test_live_talent_calc_contract() -> None:
    _require_live()
    payload = _payload_for(
        [
            "talent-calc",
            "druid/balance/DAQBBBBQQRUFURYVBEANVVRUVFVVVQCVQhEUEBUEBhVQ",
            "--listed-build-limit",
            "5",
        ]
    )

    assert payload["expansion"] == "retail"
    assert payload["tool"]["kind"] == "talent-calc"
    assert payload["tool"]["class_slug"] == "druid"
    assert payload["tool"]["spec_slug"] == "balance"
    assert payload["tool"]["build_code"] == "DAQBBBBQQRUFURYVBEANVVRUVFVVVQCVQhEUEBUEBhVQ"
    assert payload["tool"]["state_url"].startswith("https://www.wowhead.com/talent-calc/druid/balance/")
    assert payload["build_identity"]["status"] == "inferred"
    assert payload["build_identity"]["class_spec_identity"]["identity"]["actor_class"] == "druid"
    assert payload["citations"]["page"] == payload["tool"]["state_url"]
    assert payload["page"]["title"].startswith("Balance Druid")
    assert payload["listed_builds"]["count"] >= len(payload["listed_builds"]["items"])


def test_live_profession_tree_contract() -> None:
    _require_live()
    payload = _payload_for(["profession-tree", "alchemy/BCuA"])

    assert payload["expansion"] == "retail"
    assert payload["tool"]["kind"] == "profession-tree"
    assert payload["tool"]["profession_slug"] == "alchemy"
    assert payload["tool"]["loadout_code"] == "BCuA"
    assert payload["tool"]["state_url"].startswith("https://www.wowhead.com/profession-tree-calc/alchemy/")
    assert payload["citations"]["page"] == payload["tool"]["state_url"]
    assert payload["page"]["title"].startswith("Alchemy")


def test_live_dressing_room_contract() -> None:
    _require_live()
    payload = _payload_for(["dressing-room", "#fz8zz0zb89c8mM8YB8mN8X18mO8ub8mP8uD"])

    assert payload["expansion"] == "retail"
    assert payload["tool"]["kind"] == "dressing-room"
    assert payload["tool"]["has_share_hash"] is True
    assert payload["tool"]["share_hash"].startswith("fz8")
    assert payload["tool"]["state_url"].startswith("https://www.wowhead.com/dressing-room#")
    assert payload["page"]["title"].startswith("Dressing Room")


def test_live_profiler_contract() -> None:
    _require_live()
    payload = _payload_for(["profiler", "97060220/us/illidan/Roguecane"])

    assert payload["expansion"] == "retail"
    assert payload["tool"]["kind"] == "profiler"
    assert payload["tool"]["list_id"] == "97060220"
    assert payload["tool"]["region_slug"] == "us"
    assert payload["tool"]["realm_slug"] == "illidan"
    assert payload["tool"]["character_name"] == "Roguecane"
    assert payload["tool"]["state_url"].startswith("https://www.wowhead.com/list?list=")
    assert payload["page"]["title"].startswith("Profiler")

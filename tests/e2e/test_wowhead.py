"""End-to-end journeys for the ``wowhead`` binary against the live site.

Every volatile identifier (news slug, guide id, comment id, npc/spell/quest id, talent build code)
is discovered at run time from an earlier command in the same journey, so the file cannot rot on a
stale pin. The only pinned entity is Thunderfury (``tests/e2e/pins.py``), plus the three opaque
tool-state refs below that Wowhead only ever mints inside a browser.
"""

from __future__ import annotations

import json
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
    payload_or_legacy,
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


def assert_envelope_data_holds(result: Result, *keys: str) -> None:
    """The envelope slot ``data`` carries the payload, and legacy top-level copies agree with it."""
    assert result.data, f"data slot is empty\n{result.describe()}"
    for key in keys:
        assert key in result.data, f"data is missing {key!r}\n{result.describe()}"
        assert payload_or_legacy(result, key) == result.data[key], (
            f"legacy top-level {key!r} disagrees with data\n{result.describe()}"
        )


def entity_rows(result: Result, entity_type: str) -> list[dict[str, Any]]:
    return [row for row in result.data["results"] if row.get("entity_type") == entity_type]


@pytest.fixture(scope="module")
def thunderfury_search() -> Result:
    return run(BINARY, "search", pins.ITEM_SEARCH_QUERY, "--limit", "10")


@pytest.fixture(scope="module")
def class_guides() -> Result:
    return run(BINARY, "guides", "classes", "--sort", "updated", "--limit", "5")


@pytest.fixture(scope="module")
def guide_id(class_guides: Result) -> int:
    return int(class_guides.data["results"][0]["id"])


@pytest.fixture(scope="module")
def news_listing() -> Result:
    return run(BINARY, "news", "--limit", "5")


@pytest.fixture(scope="module")
def blue_listing() -> Result:
    return run(BINARY, "blue-tracker", "--limit", "20")


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
    items = entity_rows(thunderfury_search, "item")
    assert items, thunderfury_search.describe()
    assert all(isinstance(row["id"], int) and row["name"] for row in thunderfury_search.data["results"])

    resolved = run(BINARY, "resolve", pins.ITEM_SEARCH_QUERY, "--entity-type", "item", "--limit", "3")
    assert_envelope_data_holds(resolved, "match", "candidates")
    match = resolved.data["match"]
    assert isinstance(match, dict), f"resolve found no item\n{resolved.describe()}"
    assert match["entity_type"] == "item", resolved.describe()
    assert match["id"] in {row["id"] for row in items}, resolved.describe()
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


def test_search_stream_emits_one_jsonl_record_per_result(require) -> None:
    require("wowhead")
    streamed = run(BINARY, "--stream", "search", pins.SPELL_SEARCH_QUERY, "--limit", "5", stream=True)
    assert streamed.data["results"] == [], "the JSONL header must empty the streamed collection"
    records = stream_records(streamed)
    assert 0 < len(records) <= 5, streamed.describe()
    assert all(isinstance(row["id"], int) and row["name"] for row in records), streamed.describe()
    assert any(row["entity_type"] == "spell" for row in records), streamed.describe()


def test_discovered_npc_spell_and_quest_each_fetch_as_an_entity(require) -> None:
    require("wowhead")
    discovered: dict[str, int] = {}
    for entity_type, query in (
        ("npc", pins.NPC_SEARCH_QUERY),
        ("spell", pins.SPELL_SEARCH_QUERY),
        ("quest", "the deadmines"),
    ):
        found = run(BINARY, "search", query, "--limit", "10")
        rows = entity_rows(found, entity_type)
        assert rows, f"no {entity_type} in search {query!r}\n{found.describe()}"
        discovered[entity_type] = rows[0]["id"]
        assert rows[0]["url"] == f"https://www.wowhead.com/{entity_type}={rows[0]['id']}", found.describe()

    for entity_type, entity_id in discovered.items():
        entity = run(
            BINARY, "entity", entity_type, str(entity_id),
            "--no-include-comments", "--linked-entity-preview-limit", "0",
        )
        assert_envelope_data_holds(entity, "entity", "tooltip")
        assert entity.data["entity"]["type"] == entity_type, entity.describe()
        assert entity.data["entity"]["id"] == entity_id, entity.describe()
        assert entity.data["entity"]["name"], entity.describe()
        assert entity.data["tooltip"]["text"], entity.describe()


def test_comments_rank_stream_and_match_the_entity_preview(require) -> None:
    require("wowhead")
    entity = run(BINARY, "entity", "item", str(pins.ITEM_ID))
    preview = entity.data["comments"]
    assert preview["count"] > len(preview["top"]) > 0, entity.describe()
    assert preview["needs_raw_fetch"] is True, "a sampled preview must say a raw fetch is still needed"
    preview_ids = {row["id"] for row in preview["top"]}

    ranked = run(BINARY, "comments", "item", str(pins.ITEM_ID), "--limit", "5", "--sort", "rating", "--insights")
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

    streamed = run(BINARY, "--stream", "comments", "item", str(pins.ITEM_ID), "--limit", "5", "--sort", "rating", stream=True)
    assert streamed.data["comments"] == [], "the JSONL header must empty the streamed collection"
    assert [row["id"] for row in stream_records(streamed)] == [row["id"] for row in rows], streamed.describe()


def test_compare_diffs_two_legendary_items_field_by_field(require) -> None:
    require("wowhead")
    other = run(BINARY, "resolve", "sulfuras hand of ragnaros", "--entity-type", "item", "--limit", "3")
    assert isinstance(other.data["match"], dict), f"resolve found no item\n{other.describe()}"
    other_id = other.data["match"]["id"]
    assert other_id != pins.ITEM_ID, other.describe()

    compared = run(
        BINARY, "compare", f"item:{pins.ITEM_ID}", f"item:{other_id}",
        "--preset", "gear", "--comment-sample", "0", "--max-links-per-entity", "10",
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
    assert links["shared_count_total"] >= links["shared_count_returned"] >= 0, compared.describe()


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
    require, class_guides: Result, guide_id: int
) -> None:
    require("wowhead")
    assert_envelope_data_holds(class_guides, "results", "count", "guides_url")
    assert class_guides.data["category"] == "classes"
    assert class_guides.data["guides_url"] == "https://www.wowhead.com/guides/classes"
    rows = class_guides.data["results"]
    assert 0 < len(rows) <= 5, class_guides.describe()
    updated = [row["last_updated"] for row in rows if isinstance(row.get("last_updated"), str)]
    assert updated == sorted(updated, reverse=True), "--sort updated did not sort"
    assert all(row["url"].startswith("https://www.wowhead.com/guide/") for row in rows)
    assert class_guides.data["facets"]["authors"], class_guides.describe()

    summary = run(BINARY, "guide", str(guide_id), "--comment-sample", "1", "--linked-entity-preview-limit", "3")
    assert_envelope_data_holds(summary, "guide", "page", "linked_entities")
    assert summary.data["guide"]["id"] == guide_id, summary.describe()
    assert summary.data["page"]["title"], summary.describe()
    assert summary.data["citations"]["page"] == summary.data["guide"]["page_url"], summary.describe()
    assert len(summary.data["linked_entities"]["items"]) <= 3, summary.describe()

    full = run(BINARY, "guide-full", str(guide_id), "--max-links", "25")
    assert_envelope_data_holds(full, "guide", "body", "linked_entities")
    assert full.data["guide"]["id"] == guide_id, full.describe()
    assert full.data["page"]["title"] == summary.data["page"]["title"], "guide and guide-full disagree on title"
    sections = full.data["body"]["sections"]
    assert sections and all(section.get("title") for section in sections), full.describe()
    assert full.data["body"]["raw_markup"], "guide-full dropped the raw guide markup"
    assert full.data["linked_entities"]["count"] == len(full.data["linked_entities"]["items"]) > 0
    assert full.data["navigation"]["links"], full.describe()


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
    # Wowhead scopes some posts under an expansion path, for example /forever/news/<slug>.
    assert all("/news/" in row["url"] and row["title"] for row in rows), news_listing.describe()
    assert all(row["url"].startswith("https://www.wowhead.com/") for row in rows), news_listing.describe()
    assert news_listing.data["facets"]["authors"], news_listing.describe()

    post = run(BINARY, "news-post", rows[0]["url"], "--related-limit", "2")
    assert_envelope_data_holds(post, "post", "content", "citations")
    assert post.data["post"]["page_url"] == rows[0]["url"], post.describe()
    assert post.data["post"]["title"], post.describe()
    assert post.data["content"]["text"].strip(), "news-post returned an empty body"
    assert post.data["content"]["section_count"] == len(post.data["content"]["sections"])
    assert post.data["citations"]["page"] == post.data["post"]["page_url"], post.describe()
    for bucket in post.data["related"].values():
        assert len(bucket["items"]) <= 2, post.describe()


def test_blue_tracker_listing_leads_to_one_blue_topic(require, blue_listing: Result) -> None:
    require("wowhead")
    assert_envelope_data_holds(blue_listing, "results", "count", "blue_tracker_url")
    assert blue_listing.data["blue_tracker_url"] == "https://www.wowhead.com/blue-tracker"
    rows = blue_listing.data["results"]
    assert rows, blue_listing.describe()
    assert all(row["url"].startswith("https://www.wowhead.com/blue-tracker/") for row in rows)
    assert set(blue_listing.data["facets"]["regions"]) <= {"us", "eu"}, blue_listing.describe()

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
    require("wowhead")
    profession = run(BINARY, "profession-tree", PROFESSION_TREE_REF)
    assert_envelope_data_holds(profession, "tool", "page", "citations")
    assert profession.data["tool"]["profession_slug"] == "alchemy", profession.describe()
    assert profession.data["tool"]["loadout_code"] == "BCuA", profession.describe()
    assert profession.data["tool"]["state_url"].endswith(PROFESSION_TREE_REF), profession.describe()
    assert profession.data["page"]["title"], profession.describe()

    dressing = run(BINARY, "dressing-room", DRESSING_ROOM_REF)
    assert_envelope_data_holds(dressing, "tool", "page")
    assert dressing.data["tool"]["has_share_hash"] is True, dressing.describe()
    assert dressing.data["tool"]["share_hash"] == DRESSING_ROOM_REF.lstrip("#"), dressing.describe()
    assert dressing.data["tool"]["state_url"] == f"https://www.wowhead.com/dressing-room{DRESSING_ROOM_REF}"
    assert dressing.data["page"]["title"], dressing.describe()

    profiler = run(BINARY, "profiler", PROFILER_REF)
    assert_envelope_data_holds(profiler, "tool", "page")
    assert profiler.data["tool"]["list_parts"] == PROFILER_REF.split("/"), profiler.describe()
    assert profiler.data["tool"]["region_slug"] == pins.REGION, profiler.describe()
    assert profiler.data["tool"]["realm_slug"] == pins.REALM_SLUG, profiler.describe()
    assert profiler.data["tool"]["state_url"] == f"https://www.wowhead.com/list?list={PROFILER_REF}"
    assert profiler.data["page"]["title"], profiler.describe()


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

    debug = run(BINARY, "--profile", "debug", "entity", "item", str(pins.ITEM_ID), "--no-include-comments")
    assert "\n  " in debug.stdout, "--profile debug should be pretty"
    assert debug.data["entity"]["name"] == pins.ITEM_NAME, debug.describe()

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


def test_inactive_beta_profile_fails_cleanly_instead_of_crashing(require) -> None:
    require("wowhead")
    beta = run(
        BINARY, "--expansion", "beta", "entity", "item", str(pins.ITEM_ID),
        "--no-include-comments", expect=None,
    )
    if beta.ok:
        assert beta.data["expansion"] == "beta", beta.describe()
        assert beta.data["entity"]["page_url"].startswith("https://www.wowhead.com/beta/"), beta.describe()
        return
    # Wowhead retires the beta subtree between expansions; the CLI must report that, not crash.
    assert beta.exit_code in {EXIT_NOT_FOUND, EXIT_NETWORK}, beta.describe()
    assert beta.error_code in {"not_found", "http_error", "upstream_error"}, beta.describe()
    assert beta.stdout == "", beta.describe()


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

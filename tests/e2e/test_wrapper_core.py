"""End-to-end journeys for the ``warcraft`` wrapper's own commands.

Covers the routing and composition surface the wrapper owns: ``doctor``, ``schema``, ``search``,
``resolve``, expansion filtering, the guild snapshot, the global output flags, and provider
passthrough. Provider-specific depth lives in the per-provider journey modules; what is asserted
here is the wrapper contract in docs/foundation/WRAPPER_PROVIDER_CONTRACT.md.

A fanout journey that tolerates a provider failing cannot report the outage it exists to catch, so
every fanout here requires the included providers to answer, and the merged list is checked against
the provider payloads it was built from rather than only for shape.
"""

from __future__ import annotations

import json
import shlex
from collections import Counter
from datetime import UTC, datetime
from typing import Any

import pytest

from tests.e2e import pins
from tests.e2e.harness import EXIT_NETWORK, EXIT_USAGE, REPO_ROOT, Result, dead_proxy_env, no_cache_env, run, run_raw

REGION = pins.GUILD_REGION
REALM = pins.GUILD_REALM_DISPLAY
REALM_SLUG = "mal-ganis"
GUILD = pins.GUILD_NAME
GUILD_QUERY = f"guild {REGION} {REALM_SLUG} {GUILD}"

TIERS = {"core", "supported", "experimental"}
# warcraftlogs is the only other expansion-profiled provider, so wotlk keeps exactly these two.
WOTLK_INCLUDED = ["wowhead", "warcraftlogs"]
# Providers with a retail-capable search surface; the rest have no expansion axis at all.
RETAIL_FANOUT_PROVIDERS = {"wowhead", "method", "icy-veins", "raiderio", "warcraftlogs", "warcraft-wiki", "lorrgs"}
NO_EXPANSION_AXIS_PROVIDERS = {"simc", "raidbots", "blizzard-api", "curseforge"}
# Warcraft Logs only looks up explicit report references; free text gets a local hint, not an answer.
REPORT_ONLY_PROVIDERS = {"warcraftlogs"}
ITEM_QUERY = pins.ITEM_SEARCH_QUERY
ITEM_LIMIT = "5"


def _provider_rows(result: Result) -> dict[str, dict[str, Any]]:
    rows = result.data["providers"]
    assert isinstance(rows, list) and rows, result.describe()
    return {row["provider"]: row for row in rows}


def _assert_fanout_answered(result: Result) -> dict[str, dict[str, Any]]:
    """Every included provider that searches free text answered.

    Without this the routing assertions below are all satisfied by a completely dead fanout: the
    included/excluded sets are computed from the registry before any provider is called.
    """
    data = result.data
    assert data["failed_providers"] == [], result.describe()
    assert data["failed_provider_count"] == 0
    rows = _provider_rows(result)
    assert set(rows) == set(data["included_providers"])
    unanswered = {name for name, row in rows.items() if not row["answered"]}
    assert unanswered == REPORT_ONLY_PROVIDERS & set(rows), result.describe()
    assert data["answered_provider_count"] == data["included_provider_count"] - len(unanswered)
    for name, row in rows.items():
        assert row["ok"] is True, name
        assert row["error"] is None, name
        assert row["status"] in {"ready", "partial"}, name
    return rows


def _provider_result_rows(rows: dict[str, dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    return {name: row["payload"]["data"]["results"] for name, row in rows.items()}


def _page_ids(rows: list[dict[str, Any]]) -> str:
    return json.dumps([(row["provider"], row["id"]) for row in rows])


def _assert_merged_page(result: Result) -> list[dict[str, Any]]:
    """The page keeps every provider's own order, and its policy block adds up.

    A provider's order is its ranking (WRAPPER_PROVIDER_CONTRACT.md), so each provider's rows on the
    page must be the head of that provider's own list: the wrapper interleaves providers, but a row
    it reordered, skipped past or invented breaks the prefix. Between providers only the documented
    tiers are checked (anchored rows first, off-intent rows last); the scores that order the rest are
    the product's own arithmetic, not something this journey can know independently.
    """
    data = result.data
    rows: list[dict[str, Any]] = data["results"]
    by_provider = _provider_result_rows(_provider_rows(result))
    assert {row["provider"] for row in rows} <= set(by_provider), _page_ids(rows)
    for provider, provider_rows in by_provider.items():
        page_ids = [row["id"] for row in rows if row["provider"] == provider]
        own_head = [row["id"] for row in provider_rows[: len(page_ids)]]
        assert page_ids == own_head, f"{provider} ranked {own_head}, the page shows {page_ids}"
    tiers = [(not row["wrapper_ranking"]["anchor"], row["wrapper_ranking"]["off_intent"]) for row in rows]
    assert tiers == sorted(tiers), _page_ids(rows)
    policy = data["merge_policy"]
    assert policy["provider_row_counts"] == dict(Counter(row["provider"] for row in rows))
    assert policy["candidate_row_count"] == data["count"]
    return rows


def _row_index(rows: list[dict[str, Any]], provider: str, identifier: Any) -> int:
    matches = [index for index, row in enumerate(rows) if (row["provider"], row["id"]) == (provider, identifier)]
    assert matches, f"{provider} {identifier!r} is not on the page: {_page_ids(rows)}"
    return matches[0]


@pytest.fixture(scope="module")
def item_search() -> Result:
    """One default ``warcraft search`` over the pinned item query, shared by the merge journeys."""
    return run("warcraft", "search", ITEM_QUERY, "--limit", ITEM_LIMIT)


@pytest.fixture(scope="module")
def brief_item_search() -> Result:
    """The same query under the flags that reshape the wrapper payload instead of the candidates."""
    return run(
        "warcraft", "search", ITEM_QUERY, "--limit", ITEM_LIMIT,
        "--brief", "--ranking-debug", "--expansion-debug",
    )


def test_doctor_reports_a_tiered_readiness_row_for_every_provider(doctor_rows: dict[str, dict[str, Any]]) -> None:
    result = run("warcraft", "doctor")

    wrapper = result.data["wrapper"]
    assert set(wrapper["tiers"]) == TIERS
    tiered = {name for names in wrapper["tiers"].values() for name in names}
    assert tiered == set(doctor_rows), "wrapper.tiers must name exactly the registered providers"
    assert wrapper["provider_count"] == len(doctor_rows)
    assert wrapper["expansion_filter_active"] is False

    for name, row in doctor_rows.items():
        assert row["tier"] in TIERS, name
        assert row["status"] in {"ready", "partial"}, f"{name}: {row['status']}"
        assert row["installed"] is True, name
        assert isinstance(row["auth"]["required"], bool), name
        assert set(row["wrapper_surfaces"]) == {"doctor", "search", "resolve"}, name
        # Every row embeds the provider's own doctor envelope, so its readiness stays auditable.
        assert row["details"]["ok"] is True, name
        assert row["expansion_support"]["mode"] in {"profiled", "fixed", "none"}, name

    assert result.data["paths"], "doctor must report the resolved runtime paths"


def test_schema_matches_the_checked_in_envelope_schema() -> None:
    result = run("warcraft", "schema")

    schema = result.data["schema"]
    checked_in = json.loads((REPO_ROOT / "schemas" / "envelope.schema.json").read_text())
    assert schema == checked_in, "warcraft schema must not drift from schemas/envelope.schema.json"
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert {"ok", "provider", "command", "kind", "schema_version", "query", "provenance", "data"} <= set(schema["properties"])


def test_search_fans_out_to_every_included_provider(item_search: Result) -> None:
    rows = _assert_fanout_answered(item_search)
    data = item_search.data
    by_provider = _provider_result_rows(rows)

    # wowhead is the core provider for an item query: a silent outage there is the failure this
    # journey exists to report, so a structured error row is not an acceptable outcome.
    assert pins.ITEM_ID in [row["id"] for row in by_provider["wowhead"]], item_search.describe()

    # `count` is the merged candidate total and `truncated` reports whether --limit cut the list.
    assert data["count"] == sum(len(results) for results in by_provider.values())
    assert len(data["results"]) == int(ITEM_LIMIT)
    assert data["truncated"] is (data["count"] > len(data["results"]))
    _assert_merged_page(item_search)
    for row in data["results"]:
        assert row["name"], row
        assert row["wrapper_ranking"]["reasons"], row


def _assert_item_leads(rows: list[dict[str, Any]]) -> None:
    """Wowhead's top row for the bare item name is the item, and the page leads with it.

    The page must also carry a row titled exactly the query (Wowhead's ``Thunderfury`` proc spells,
    which it ranks below the item): that row is what an exact-title boost used to lift above the
    item, so without it on the page the first-row check could not fail for that reason.
    """
    top = rows[0]
    assert (top["provider"], top["id"], top["name"]) == ("wowhead", pins.ITEM_ID, pins.ITEM_NAME), _page_ids(rows)
    assert top["wrapper_ranking"]["anchor"] is True, json.dumps(top["wrapper_ranking"])
    assert any(row["name"].lower() == ITEM_QUERY for row in rows[1:]), _page_ids(rows)


def test_search_leads_with_the_named_item(item_search: Result) -> None:
    """A bare item name is answered by the item, not by the players, posts or pages that mention it."""
    _assert_fanout_answered(item_search)
    rows = _assert_merged_page(item_search)
    _assert_item_leads(rows)
    # Raider.IO has characters named Thunderfury; none of them may take the page from the item.
    assert "raiderio" not in {row["provider"] for row in rows}, _page_ids(rows)


def test_search_merges_candidates_from_more_than_one_provider(item_search: Result) -> None:
    """No single provider can own every slot in the merged list (WRAPPER_PROVIDER_CONTRACT.md)."""
    _assert_fanout_answered(item_search)
    policy = item_search.data["merge_policy"]
    counts = policy["provider_row_counts"]
    assert len(counts) >= 2 and "wowhead" in counts, json.dumps(policy)
    # Other providers had rows to spare, so no provider's overflow may have been promoted past its cap.
    assert policy["promoted_after_cap_count"] == 0, json.dumps(policy)
    assert max(counts.values()) <= policy["per_provider_cap"], json.dumps(policy)


def test_search_merges_guide_candidates_from_more_than_one_provider() -> None:
    """The guide sites share the page: Method's and Icy Veins' own guides for the spec are both on it."""
    result = run("warcraft", "search", pins.GUIDE_QUERY, "--limit", "6")
    _assert_fanout_answered(result)
    rows = _assert_merged_page(result)
    for provider, guide in (("method", "mistweaver-monk"), ("icy-veins", "mistweaver-monk-pve-healing-guide")):
        assert rows[_row_index(rows, provider, guide)]["kind"] == "guide", _page_ids(rows)


def test_search_routes_a_structured_guild_query_to_raiderio_first(require) -> None:
    require("raiderio")
    result = run("warcraft", "search", GUILD_QUERY, "--limit", ITEM_LIMIT)
    _assert_fanout_answered(result)
    top = _assert_merged_page(result)[0]
    assert (top["provider"], top["kind"], top["name"]) == ("raiderio", "guild", GUILD), json.dumps(top)[:600]


def test_search_routes_a_multi_word_realm_profile_query_to_raiderio_first(require) -> None:
    """``<region> <realm words> <name>`` is a profile query even when the realm is two words."""
    require("raiderio")
    result = run("warcraft", "search", f"{REGION} mal ganis {pins.CHARACTER_NAME}", "--limit", ITEM_LIMIT)
    _assert_fanout_answered(result)
    top = _assert_merged_page(result)[0]
    assert (top["provider"], top["kind"], top["name"]) == ("raiderio", "character", pins.CHARACTER_NAME), json.dumps(top)[:600]
    # The two realm words have to reach Raider.IO as one realm: a namesake on another realm is the wrong character.
    assert top["id"] == f"https://raider.io/characters/{REGION}/{pins.GUILD_REALM}/{pins.CHARACTER_NAME}", json.dumps(top)[:600]
    # Read as free text, the same row would be an off-intent profile that only fills leftover slots.
    assert top["wrapper_ranking"]["off_intent"] is False, json.dumps(top["wrapper_ranking"])


def test_search_keeps_a_raiderio_row_for_a_bare_character_name(require) -> None:
    """A bare name that is exactly a character's name keeps one profile slot on the page."""
    require("raiderio")
    result = run("warcraft", "search", pins.CHARACTER_NAME, "--limit", ITEM_LIMIT)
    _assert_fanout_answered(result)
    rows = _assert_merged_page(result)
    # _assert_merged_page already holds the reserved row to Raider.IO's own top row.
    profiles = [row for row in rows if row["provider"] == "raiderio" and row["name"] == pins.CHARACTER_NAME]
    assert profiles, json.dumps([(row["provider"], row["name"]) for row in rows])
    assert result.data["merge_policy"]["reserved_exact_profile_slot_count"] == 1, json.dumps(result.data["merge_policy"])


def test_search_brief_and_debug_flags_reshape_the_same_candidates(item_search: Result, brief_item_search: Result) -> None:
    """``--brief`` compacts the rows, ``--ranking-debug``/``--expansion-debug`` add inspection."""
    data = brief_item_search.data
    assert data["providers"] == [], "--brief must drop the per-provider payloads"
    assert data["count"] == item_search.data["count"]
    assert [(row["provider"], row["id"]) for row in data["results"]] == [
        (row["provider"], row["id"]) for row in item_search.data["results"]
    ], "--brief must reshape the same candidates in the same order"
    for brief_row, full_row in zip(data["results"], item_search.data["results"], strict=True):
        # The only key the compact row adds is the flattened follow-up command, and every row keeps it.
        assert set(brief_row) - {"follow_up_command"} < set(full_row), brief_row
        assert brief_row["name"] == full_row["name"]
        assert brief_row["kind"] == full_row["kind"]
        assert brief_row["follow_up_command"] == full_row["follow_up"]["command"], brief_row

    assert [row["id"] for row in data["ranking_debug"]] == [row["id"] for row in data["results"]]
    # The snapshot covers every registered provider, not just the ones this fanout included.
    snapshot = {row["provider"] for row in data["expansion_debug"]}
    assert snapshot >= set(data["included_providers"]) | {row["provider"] for row in data["excluded_providers"]}
    assert len(snapshot) == data["provider_count"]


def test_search_follow_up_command_returns_the_same_entity(item_search: Result) -> None:
    """Every row hands back a runnable command, and the item's command must reach that item."""
    rows = item_search.data["results"]
    assert all(row["follow_up"]["command"] for row in rows), json.dumps(rows)[:600]

    row = rows[_row_index(rows, "wowhead", pins.ITEM_ID)]
    binary, *args = shlex.split(row["follow_up"]["command"])
    follow_up = run(binary, *args)
    assert follow_up.payload["provider"] == row["provider"]
    entity = follow_up.data["entity"]
    assert (entity["type"], entity["id"], entity["name"]) == ("item", pins.ITEM_ID, pins.ITEM_NAME), follow_up.describe()


@pytest.mark.parametrize("command", ["search", "resolve"])
def test_fanout_fails_with_the_network_error_when_no_provider_can_answer(command: str) -> None:
    """A network outage must never read as a good answer: exit 5, with every outage named.

    Warcraft Logs answers free text locally without searching, so it is not an answer and not a
    failure; every provider that does search has to be in the failure list.
    """
    result = run(
        "warcraft", command, ITEM_QUERY,
        env={**dead_proxy_env(), **no_cache_env()},
        expect=EXIT_NETWORK,
        error_code="network_error",
    )
    assert (result.payload["kind"], result.payload["command"]) == ("error", command), result.describe()
    assert result.payload["data"] == {}, result.describe()
    failed = {row["provider"]: row for row in result.payload["error"]["details"]["failed_providers"]}
    assert set(failed) == RETAIL_FANOUT_PROVIDERS - REPORT_ONLY_PROVIDERS, result.describe()
    for provider, row in failed.items():
        assert row["code"] == "network_error", f"{provider}: {row}"
        assert row["message"], provider


def test_resolve_selects_raiderio_and_hands_over_a_next_command(require) -> None:
    require("raiderio")
    result = run("warcraft", "resolve", GUILD_QUERY, "--limit", "5")
    _assert_fanout_answered(result)
    _assert_guild_resolved_by_raiderio(result)


def test_resolve_with_the_retail_filter_keeps_the_retail_fanout(require) -> None:
    require("raiderio")
    result = run("warcraft", "--expansion", "retail", "resolve", GUILD_QUERY, "--limit", "3")
    _assert_fanout_answered(result)
    data = result.data
    assert data["requested_expansion"] == "retail"
    assert data["expansion_filter_active"] is True
    assert set(data["included_providers"]) == RETAIL_FANOUT_PROVIDERS
    assert {row["provider"] for row in data["excluded_providers"]} == NO_EXPANSION_AXIS_PROVIDERS
    _assert_guild_resolved_by_raiderio(result)


def _assert_guild_resolved_by_raiderio(result: Result) -> None:
    """A structured guild query resolves to Raider.IO, and its next command returns that guild."""
    data = result.data
    assert data["resolved"] is True, result.describe()
    assert data["selected_provider"] == "raiderio", result.describe()
    # The envelope names the binary that answered; the matched provider is data.selected_provider.
    assert result.payload["provider"] == "warcraft"
    assert data["match"]["provider"] == "raiderio"
    assert data["match"]["name"] == GUILD
    profile_url = data["match"]["profile_url"]
    assert profile_url.startswith(f"https://raider.io/guilds/{REGION}/"), profile_url

    binary, *args = shlex.split(data["next_command"])
    assert binary == "raiderio"
    follow_up = run(binary, *args)
    # The command it handed over returns that guild, profile URL included.
    assert follow_up.data["guild"]["name"] == GUILD, follow_up.describe()
    assert profile_url in json.dumps(follow_up.data), follow_up.describe()


def test_resolve_attributes_an_unresolved_answer_to_the_wrapper() -> None:
    result = run("warcraft", "resolve", "zzqqxx nonsense query 8471", "--limit", "3")
    _assert_fanout_answered(result)
    data = result.data

    assert data["resolved"] is False
    assert data["selected_provider"] is None
    assert result.payload["provider"] == "warcraft"
    assert data["match"] is None
    assert data["next_command"] is None
    assert data["best_unresolved_candidate"] is None
    # An unresolved resolve still hands the agent a next step rather than a dead end.
    assert data["fallback_search_commands"], result.describe()
    assert data["fallback_search_command"] == data["fallback_search_commands"][0]["command"]


def test_resolve_answers_with_the_candidate_search_ranks_first(item_search: Result) -> None:
    """A bare entity name: resolve and search name the same Wowhead item, whatever scale the wiki scores on.

    Resolve once broke ties on raw provider scores, so the wiki article outranked the item search
    leads with. The item is pinned, so the agreement is checked against a known answer.
    """
    result = run("warcraft", "resolve", ITEM_QUERY)
    _assert_fanout_answered(result)
    data = result.data
    top = item_search.data["results"][0]
    answer = data["match"] if data["resolved"] else data["best_unresolved_candidate"]
    assert (answer["provider"], answer["id"]) == (top["provider"], top["id"]) == ("wowhead", pins.ITEM_ID), result.describe()
    if data["resolved"]:
        binary, *args = shlex.split(data["next_command"])
        assert run(binary, *args).data["entity"]["name"] == pins.ITEM_NAME
    else:
        assert answer["unresolved_reason"] == "provider_did_not_resolve", result.describe()


def test_resolve_answers_a_guide_query_with_a_guide_for_that_spec() -> None:
    """``frost mage guide`` once resolved to Lorrgs spec metadata at high confidence.

    The answer must be a guide site's guide and the row search ranks first, and the command it
    hands over must open a page whose own title names the spec.
    """
    query = "frost mage guide"
    searched = run("warcraft", "search", query)
    _assert_fanout_answered(searched)
    result = run("warcraft", "resolve", query)
    _assert_fanout_answered(result)
    data = result.data
    assert data["resolved"] is True, result.describe()
    assert data["selected_provider"] in {"wowhead", "method", "icy-veins"}, result.describe()
    top = searched.data["results"][0]
    assert (data["match"]["provider"], data["match"]["id"]) == (top["provider"], top["id"]), result.describe()

    binary, *args = shlex.split(data["next_command"])
    assert binary == data["selected_provider"], data["next_command"]
    title = run(binary, *args).data["page"]["title"].lower()
    assert "frost" in title and "mage" in title, title


def test_resolve_hands_over_a_runnable_command_for_a_multi_word_guild(require) -> None:
    """A guild name with a space reaches the handed-over command quoted, so that command runs.

    The guild comes from Raider.IO's heroic leaderboard for a raid that is open now, which is also
    the oracle for its name.
    """
    require("raiderio")
    catalog = run("raiderio", "raids")
    open_slugs = sorted(_open_raid_slugs(catalog.data["rows"], region=REGION))
    assert open_slugs, catalog.describe()
    board = run("raiderio", "leaderboard", "raids", "--raid", open_slugs[0], "--difficulty", "heroic", "--region", REGION)
    guild = next((row["guild"] for row in board.data["rows"] if " " in row["guild"]["name"]), None)
    assert guild is not None, f"no multi-word guild name on the first leaderboard page\n{board.describe()}"

    result = run("warcraft", "resolve", f"guild {REGION} {guild['realm']} {guild['name']}")
    assert result.data["selected_provider"] == "raiderio", result.describe()
    binary, *args = shlex.split(result.data["next_command"])
    assert run(binary, *args).data["guild"]["name"] == guild["name"], result.data["next_command"]


def test_expansion_filter_narrows_search_and_explains_every_exclusion() -> None:
    result = run("warcraft", "--expansion", "wotlk", "search", ITEM_QUERY, "--limit", "10")
    rows = _assert_fanout_answered(result)
    data = result.data

    assert data["requested_expansion"] == "wotlk"
    assert data["expansion_filter_active"] is True
    assert data["included_providers"] == WOTLK_INCLUDED
    excluded = data["excluded_providers"]
    assert {row["provider"] for row in excluded} == (RETAIL_FANOUT_PROVIDERS | NO_EXPANSION_AXIS_PROVIDERS) - set(
        WOTLK_INCLUDED
    )
    for row in excluded:
        support = row["expansion_support"]
        assert support["requested_expansion"] == "wotlk"
        assert support["allowed"] is False
        assert support["exclusion_reason"], row["provider"]

    # The filter reached Wowhead as a wotlk lookup, not a retail one relabelled afterwards.
    wowhead = rows["wowhead"]["payload"]["data"]
    assert (wowhead["expansion"], wowhead["expansion_source"]) == ("wotlk", "flag"), json.dumps(wowhead)[:400]
    page = _assert_merged_page(result)
    assert all(row["provider"] == "wowhead" and "/wotlk/" in row["url"] for row in page), json.dumps(page)[:600]
    _assert_item_leads(page)


def test_expansion_filter_reaches_a_different_provider_profile_than_an_unfiltered_search(item_search: Result) -> None:
    """``--expansion retail`` is a different call, not a relabelled one: Wowhead is asked for retail."""
    filtered = run("warcraft", "--expansion", "retail", "search", ITEM_QUERY, "--limit", ITEM_LIMIT)
    filtered_rows = _assert_fanout_answered(filtered)

    assert filtered.data["expansion_filter_active"] is True
    assert set(filtered.data["included_providers"]) == RETAIL_FANOUT_PROVIDERS
    assert {row["provider"] for row in filtered.data["excluded_providers"]} == NO_EXPANSION_AXIS_PROVIDERS

    assert filtered.data["results"]
    assert {row["provider_expansion"]["requested_expansion"] for row in filtered.data["results"]} == {"retail"}
    assert item_search.data["expansion_filter_active"] is False
    assert {row["provider_expansion"]["requested_expansion"] for row in item_search.data["results"]} == {None}
    # The label above is stamped by the wrapper; the provider payload shows what Wowhead was asked.
    filtered_wowhead = filtered_rows["wowhead"]["payload"]["data"]
    unfiltered_wowhead = _provider_rows(item_search)["wowhead"]["payload"]["data"]
    assert (filtered_wowhead["expansion"], filtered_wowhead["expansion_source"]) == ("retail", "flag")
    assert (unfiltered_wowhead["expansion"], unfiltered_wowhead["expansion_source"]) == ("retail", "default")


def test_expansion_filter_never_resolves_to_an_excluded_provider() -> None:
    """A guild query under wotlk has no provider that can answer it: unresolved, with a wotlk
    Wowhead search handed over as the next step.
    """
    result = run("warcraft", "--expansion", "wotlk", "resolve", f"guild {REGION} {REALM} {GUILD}", "--limit", "3")
    _assert_fanout_answered(result)
    data = result.data

    assert data["requested_expansion"] == "wotlk"
    assert data["included_providers"] == WOTLK_INCLUDED
    assert {row["provider"] for row in data["excluded_providers"]} == (RETAIL_FANOUT_PROVIDERS | NO_EXPANSION_AXIS_PROVIDERS) - set(
        WOTLK_INCLUDED
    )
    assert data["resolved"] is False, result.describe()
    assert data["selected_provider"] is None
    assert result.payload["provider"] == "warcraft"

    assert [row["provider"] for row in data["fallback_search_commands"]] == ["wowhead"], result.describe()
    binary, *args = shlex.split(data["fallback_search_command"])
    assert (binary, args[:2]) == ("wowhead", ["--expansion", "wotlk"]), data["fallback_search_command"]
    fallback = run(binary, *args)
    assert fallback.data["expansion"] == "wotlk"
    assert fallback.data["query"] == f"guild {REGION} {REALM} {GUILD}"


def _assert_rank_join(raids: list[dict[str, Any]], raiding: dict[str, Any]) -> None:
    """Every raid row carries its own raid's progression and rankings, joined on ``raid_slug``."""
    progression = {row["raid_slug"]: row for row in raiding["progression"]}
    rankings = {row["raid_slug"]: row for row in raiding["rankings"]}
    assert {raid["raid_slug"] for raid in raids} == set(progression)
    for raid in raids:
        slug = raid["raid_slug"]
        assert set(raid["ranks"]) == {"normal", "heroic", "mythic"}
        for difficulty, ranks in raid["ranks"].items():
            assert ranks == rankings[slug][difficulty], f"{slug} borrowed another raid's {difficulty} ranks"
        assert raid["total_bosses"] == progression[slug]["total_bosses"]
        assert raid["mythic_bosses_killed"] <= raid["total_bosses"]


def _open_raid_slugs(raids: list[dict[str, Any]], *, region: str) -> set[str]:
    """Raid slugs whose Raider.IO window is open right now for ``region``."""
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    return {row["slug"] for row in raids if row["starts"][region] <= now <= row["ends"][region]}


def test_guild_returns_one_identity_and_the_ranks_of_every_open_raid(require) -> None:
    """Every raid Raider.IO reports survives the wrap with its own ranks, and the open raids are all there.

    Raider.IO reports progression and rankings as two lists; a wrong join hands a raid another raid's
    world rank, which reads as a perfectly plausible number. The snapshot names no active tier, so a
    guild whose ranks stop at last tier's raid would report a stale season as current.
    """
    require("raiderio")
    result = run("warcraft", "guild", REGION, REALM, GUILD)

    assert result.payload["query"] == {"region": REGION, "realm": REALM_SLUG, "name": GUILD}
    guild = result.data["guild"]
    assert guild["name"] == GUILD
    assert guild["region"] == REGION
    assert guild["realm"].lower().replace("'", "").replace("-", "") == "malganis"

    sources = result.data["sources"]
    assert set(sources) == {"raiderio"}
    source = sources["raiderio"]
    assert source["status"] == "ok", f"raiderio source failed: {source.get('error')}"
    # The source keeps its own envelope, so the citation trail survives the wrap.
    assert source["payload"]["ok"] is True
    assert source["payload"]["provenance"]["citations"]

    summary = source["summary"]
    raids = summary["raids"]
    assert summary["raid_count"] == len(raids) >= 1
    assert summary["roster"]["member_count"] >= 1
    _assert_rank_join(raids, source["payload"]["data"]["raiding"])
    mythic = [raid for raid in raids if raid["mythic_bosses_killed"]]
    assert mythic, "at least one raid must have mythic progress"
    for raid in mythic:
        assert all(isinstance(rank, int) and rank > 0 for rank in raid["ranks"]["mythic"].values()), raid

    catalog = run("raiderio", "raids")
    open_slugs = _open_raid_slugs(catalog.data["rows"], region=REGION)
    assert open_slugs, catalog.describe()
    missing = open_slugs - {raid["raid_slug"] for raid in raids}
    assert not missing, f"missing open raids {sorted(missing)}"


def test_passthrough_returns_the_provider_payload_unchanged(require) -> None:
    require("raiderio")
    args = ("search", f"guild {REGION} malganis {GUILD}", "--limit", "3")
    through_wrapper = run("warcraft", "raiderio", *args)
    direct = run("raiderio", *args)

    assert through_wrapper.payload["provider"] == "raiderio", "passthrough must not relabel the provider"
    assert through_wrapper.data == direct.data
    assert through_wrapper.payload["command"] == direct.payload["command"]
    assert through_wrapper.payload["kind"] == direct.payload["kind"]


def test_passthrough_preserves_the_provider_exit_code(require) -> None:
    require("raiderio")
    result = run("warcraft", "raiderio", "guild", REGION, "malganis", "zzzznotarealguildzzzz", expect=4, error_code="not_found")
    assert result.payload["provider"] == "raiderio"


def test_passthrough_forwards_the_global_output_flags(require) -> None:
    require("raiderio")
    result = run_raw("warcraft", "--fields", "data.status", "--fields-strict", "raiderio", "doctor")
    assert result.exit_code == 0, result.describe()
    assert result.payload == {"data": {"status": "ready"}}, result.describe()


@pytest.mark.parametrize(
    ("argv", "provider", "error_code"),
    [
        (("method", "search", pins.GUIDE_QUERY), "method", "unsupported_provider_expansion"),
        (("wowhead", "--expansion", "retail", "search", ITEM_QUERY), "wowhead", "duplicate_expansion_argument"),
    ],
)
def test_passthrough_refuses_an_expansion_it_cannot_apply(argv: tuple[str, ...], provider: str, error_code: str) -> None:
    """``--expansion wotlk`` for a retail-only provider, or twice over, is refused by the wrapper itself.

    Behind a dead proxy, so a passthrough that ran the provider anyway fails on the network or
    answers from the cache instead of producing this usage error.
    """
    result = run("warcraft", "--expansion", "wotlk", *argv, expect=EXIT_USAGE, error_code=error_code, env=dead_proxy_env())
    assert result.payload["provider"] == "warcraft", result.describe()
    assert result.payload["error"]["details"]["provider"] == provider, result.describe()
    assert result.payload["query"] == {"provider": provider, "expansion": "wotlk"}, result.describe()


def test_expansion_advisory_rides_along_when_a_provider_has_no_expansion_axis(require) -> None:
    require("blizzard-api")
    result = run("warcraft", "--expansion", "wotlk", "blizzard", "doctor")

    advisory = result.data["expansion_advisory"]
    assert advisory["expansion_filter"] == "passthrough_no_expansion_semantics"
    assert advisory["requested_expansion"] == "wotlk"
    assert advisory["provider_expansion_mode"] == "none"


def test_expansion_advisory_survives_a_strict_field_projection(require) -> None:
    require("blizzard-api")
    result = run_raw(
        "warcraft", "--fields", "data.expansion_advisory", "--fields-strict", "--expansion", "wotlk", "blizzard", "doctor"
    )

    assert result.exit_code == 0, result.describe()
    assert set(result.payload) == {"data"}
    assert set(result.payload["data"]) == {"expansion_advisory"}
    assert result.payload["data"]["expansion_advisory"]["requested_expansion"] == "wotlk"


def test_global_output_flags_shape_the_wrapper_payload() -> None:
    human = run("warcraft", "--profile", "human", "doctor")
    assert human.stdout.startswith("{\n"), "the human profile must pretty-print"

    fields = run_raw("warcraft", "--fields", "data.wrapper.tiers", "--fields-strict", "doctor")
    assert fields.exit_code == 0, fields.describe()
    assert set(fields.payload["data"]["wrapper"]["tiers"]) == TIERS


def test_fields_reports_the_paths_it_could_not_project() -> None:
    """Without --fields-strict a missing path is dropped, but never silently."""
    result = run_raw("warcraft", "--fields", "data.wrapper.tiers", "--fields", "data.no_such_key", "doctor")
    assert result.exit_code == 0, result.describe()
    assert result.payload["fields_missing"] == ["data.no_such_key"]
    assert set(result.payload["data"]["wrapper"]["tiers"]) == TIERS


def test_fields_strict_rejects_a_missing_path() -> None:
    result = run("warcraft", "--fields", "data.no_such_key", "--fields-strict", "schema", expect=EXIT_USAGE, error_code="missing_fields")
    assert result.payload["error"]["details"]["missing_fields"] == ["data.no_such_key"]


def test_a_missing_argument_is_a_usage_error() -> None:
    """Argument parsing failures are envelopes on stderr too, so an agent never has to read help."""
    result = run("warcraft", "guild", REGION, expect=EXIT_USAGE, error_code="invalid_argument")
    assert "realm" in result.payload["error"]["message"], result.describe()
    assert result.payload["command"] == "guild"


def test_an_unknown_provider_is_a_usage_error() -> None:
    result = run("warcraft", "notaprovider", "doctor", expect=EXIT_USAGE, error_code="invalid_argument")
    assert "notaprovider" in result.payload["error"]["message"], result.describe()


@pytest.mark.parametrize("command", ["search", "resolve"])
def test_an_unknown_expansion_is_rejected_rather_than_widened(command: str) -> None:
    # The wrapper must not silently widen scope when it cannot honour the requested expansion.
    result = run("warcraft", "--expansion", "not-an-expansion", command, ITEM_QUERY, expect=EXIT_USAGE, error_code="invalid_argument")
    assert "not-an-expansion" in result.payload["error"]["message"], result.describe()
    assert result.payload["command"] == command

"""End-to-end journeys for the ``warcraft`` wrapper's own commands.

Covers the routing and composition surface the wrapper owns: ``doctor``, ``schema``, ``search``,
``resolve``, expansion filtering, the guild composites, the global output flags, and provider
passthrough. Provider-specific depth lives in the per-provider journey modules; what is asserted
here is the wrapper contract in docs/foundation/WRAPPER_PROVIDER_CONTRACT.md.

A fanout journey that tolerates a provider failing cannot report the outage it exists to catch, so
every fanout here requires the included providers to answer, and the merged list is checked against
the provider payloads it was built from rather than only for shape.
"""

from __future__ import annotations

import json
import shlex
from datetime import UTC, datetime
from typing import Any

import pytest

from tests.e2e import pins
from tests.e2e.harness import EXIT_USAGE, REPO_ROOT, Result, dead_proxy_env, no_cache_env, run, run_raw

REGION = pins.GUILD_REGION
REALM = pins.GUILD_REALM_DISPLAY
REALM_SLUG = "mal-ganis"
GUILD = pins.GUILD_NAME

TIERS = {"core", "supported", "experimental"}
# warcraftlogs is the only other expansion-profiled provider, so wotlk keeps exactly these two.
WOTLK_INCLUDED = ["wowhead", "warcraftlogs"]
# Providers with a retail-capable search surface; the rest have no expansion axis at all.
RETAIL_FANOUT_PROVIDERS = {"wowhead", "method", "icy-veins", "raiderio", "warcraftlogs", "warcraft-wiki", "lorrgs"}
ITEM_QUERY = pins.ITEM_SEARCH_QUERY
ITEM_LIMIT = "5"


def _provider_rows(result: Result) -> dict[str, dict[str, Any]]:
    rows = result.data["providers"]
    assert isinstance(rows, list) and rows, result.describe()
    return {row["provider"]: row for row in rows}


def _assert_fanout_answered(result: Result) -> dict[str, dict[str, Any]]:
    """Every included provider answered.

    Without this the routing assertions below are all satisfied by a completely dead fanout: the
    included/excluded sets are computed from the registry before any provider is called.
    """
    data = result.data
    assert data["failed_providers"] == [], result.describe()
    assert data["failed_provider_count"] == 0
    assert data["answered_provider_count"] == data["included_provider_count"]
    rows = _provider_rows(result)
    assert set(rows) == set(data["included_providers"])
    for name, row in rows.items():
        assert row["ok"] is True, name
        assert row["error"] is None, name
        assert row["status"] in {"ready", "partial", "error"}, name
    return rows


def _provider_result_rows(rows: dict[str, dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    return {name: row["payload"]["data"]["results"] for name, row in rows.items()}


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
        assert row["status"] in {"ready", "partial", "error"}, name
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
    assert by_provider["wowhead"], f"wowhead answered the pinned item query with nothing: {item_search.describe()}"
    assert any(ITEM_QUERY in str(row["name"]).lower() for row in by_provider["wowhead"])

    # `count` is the merged candidate total and `truncated` reports whether --limit cut the list.
    assert data["count"] == sum(len(results) for results in by_provider.values())
    assert len(data["results"]) <= int(ITEM_LIMIT)
    assert data["truncated"] == (data["count"] > len(data["results"]))
    answering = {name for name, results in by_provider.items() if results}
    assert {row["provider"] for row in data["results"]} <= answering
    for row in data["results"]:
        assert row["name"], row
        assert row["wrapper_ranking"]["score"] is not None, row
        assert row["wrapper_ranking"]["reasons"], row


def test_search_merges_candidates_from_more_than_one_provider(item_search: Result) -> None:
    """Rescaling each provider's scores against its own best row must keep one provider from
    owning every slot in the merged list (WRAPPER_PROVIDER_CONTRACT.md, search result ordering).
    """
    rows = _assert_fanout_answered(item_search)
    answering = {name for name, results in _provider_result_rows(rows).items() if results}
    assert len(answering) >= 2, f"only {answering} returned candidates for {ITEM_QUERY!r}"

    merged = {row["provider"] for row in item_search.data["results"]}
    assert len(merged) >= 2, json.dumps(item_search.data["results"])[:600]
    assert "wowhead" in merged, json.dumps(item_search.data["results"])[:600]


def test_search_brief_and_debug_flags_reshape_the_same_candidates(item_search: Result, brief_item_search: Result) -> None:
    """``--brief`` compacts the rows, ``--ranking-debug``/``--expansion-debug`` add inspection."""
    data = brief_item_search.data
    assert data["providers"] == [], "--brief must drop the per-provider payloads"
    assert data["count"] == item_search.data["count"]
    assert [(row["provider"], row["id"]) for row in data["results"]] == [
        (row["provider"], row["id"]) for row in item_search.data["results"]
    ], "--brief must reshape the same candidates in the same order"
    for brief_row, full_row in zip(data["results"], item_search.data["results"], strict=True):
        # The only key the compact row adds is the flattened follow-up command.
        assert set(brief_row) - {"follow_up_command"} < set(full_row), brief_row
        assert brief_row["name"] == full_row["name"]
        assert brief_row["kind"] == full_row["kind"]
        assert brief_row["follow_up_command"] == full_row["follow_up"]["command"]

    assert [row["id"] for row in data["ranking_debug"]] == [row["id"] for row in data["results"]]
    # The snapshot covers every registered provider, not just the ones this fanout included.
    snapshot = {row["provider"] for row in data["expansion_debug"]}
    assert snapshot >= set(data["included_providers"]) | {row["provider"] for row in data["excluded_providers"]}
    assert len(snapshot) == data["provider_count"]


def test_search_follow_up_command_returns_the_same_entity(brief_item_search: Result) -> None:
    """The compact rows hand back a runnable command, and it must reach the row it came from."""
    rows = [row for row in brief_item_search.data["results"] if row.get("follow_up_command")]
    assert rows, brief_item_search.describe()

    row = rows[0]
    binary, *args = shlex.split(row["follow_up_command"])
    follow_up = run(binary, *args)
    assert follow_up.payload["provider"] == row["provider"]
    assert str(row["name"]).lower() in json.dumps(follow_up.data).lower(), follow_up.describe()


def test_search_reports_every_provider_that_could_not_answer() -> None:
    """A network outage must never read as a good answer: no results, and the failures are named.

    The wrapper's exit-5 path needs *every* included provider to fail, which this cannot reach:
    warcraftlogs' search surface is a documented stub that answers with zero rows without touching
    the network, so it counts as an answer even with every socket closed.
    """
    result = run(
        "warcraft", "search", ITEM_QUERY, "--limit", "3",
        env={**dead_proxy_env(), **no_cache_env()},
    )
    data = result.data
    failed = {row["provider"]: row for row in data["failed_providers"]}
    assert "wowhead" in failed, result.describe()
    for provider, row in failed.items():
        assert row["code"] == "network_error", f"{provider}: {row}"
        assert row["message"], provider
    assert data["failed_provider_count"] == len(failed)
    assert data["answered_provider_count"] == data["included_provider_count"] - len(failed)
    assert data["results"] == [], result.describe()
    assert data["count"] == 0
    assert data["truncated"] is False


def test_resolve_mirrors_the_selected_provider_and_hands_over_a_next_command(require) -> None:
    require("raiderio")
    result = run("warcraft", "resolve", f"guild {REGION} {REALM_SLUG} {GUILD}", "--limit", "5")
    _assert_fanout_answered(result)
    data = result.data

    selected = data["selected_provider"]
    assert data["resolved"] is True, result.describe()
    assert selected in data["included_providers"]
    assert result.payload["provider"] == selected, "a resolved envelope must be attributed to the matched provider"
    assert data["match"]["provider"] == selected

    binary, *args = shlex.split(data["next_command"])
    assert binary == selected
    follow_up = run(binary, *args)
    # The match's own profile URL has to come back from the command it handed over.
    assert data["match"]["profile_url"] in json.dumps(follow_up.data), follow_up.describe()


def test_resolve_attributes_an_unresolved_answer_to_the_wrapper() -> None:
    result = run("warcraft", "resolve", "zzqqxx nonsense query 8471", "--limit", "3")
    _assert_fanout_answered(result)
    data = result.data

    assert data["resolved"] is False
    assert data["selected_provider"] is None
    assert result.payload["provider"] == "warcraft", "nothing matched, so the wrapper owns the answer"
    assert data["match"] is None
    assert data["next_command"] is None
    assert data["best_unresolved_candidate"] is None


def test_expansion_filter_narrows_search_and_explains_every_exclusion() -> None:
    result = run("warcraft", "--expansion", "wotlk", "search", ITEM_QUERY, "--limit", "3")
    _assert_fanout_answered(result)
    data = result.data

    assert data["requested_expansion"] == "wotlk"
    assert data["expansion_filter_active"] is True
    assert data["included_providers"] == WOTLK_INCLUDED

    excluded = data["excluded_providers"]
    assert excluded, "the wrapper must name the providers it dropped"
    for row in excluded:
        support = row["expansion_support"]
        assert support["requested_expansion"] == "wotlk"
        assert support["allowed"] is False
        assert support["exclusion_reason"], row["provider"]
    assert set(data["included_providers"]) & {row["provider"] for row in excluded} == set()

    assert data["results"], "the wotlk search returned no candidates at all"
    assert all(row["provider"] == "wowhead" for row in data["results"])
    assert any(ITEM_QUERY in str(row["name"]).lower() for row in data["results"])


def test_expansion_filter_reaches_a_different_provider_profile_than_an_unfiltered_search(item_search: Result) -> None:
    """``--expansion retail`` is a different call, not a relabelled one: it pins every row's profile."""
    filtered = run("warcraft", "--expansion", "retail", "search", ITEM_QUERY, "--limit", ITEM_LIMIT)
    _assert_fanout_answered(filtered)

    assert filtered.data["expansion_filter_active"] is True
    assert set(filtered.data["included_providers"]) == RETAIL_FANOUT_PROVIDERS
    assert {row["provider"] for row in filtered.data["excluded_providers"]} == (
        set(item_search.data["included_providers"]) | {row["provider"] for row in item_search.data["excluded_providers"]}
    ) - RETAIL_FANOUT_PROVIDERS

    assert filtered.data["results"]
    assert {row["provider_expansion"]["requested_expansion"] for row in filtered.data["results"]} == {"retail"}
    assert item_search.data["expansion_filter_active"] is False
    assert {row["provider_expansion"]["requested_expansion"] for row in item_search.data["results"]} == {None}


def test_expansion_filter_never_resolves_to_an_excluded_provider() -> None:
    result = run("warcraft", "--expansion", "wotlk", "resolve", f"guild {REGION} {REALM} {GUILD}", "--limit", "3")
    _assert_fanout_answered(result)
    data = result.data

    assert data["requested_expansion"] == "wotlk"
    assert data["included_providers"] == WOTLK_INCLUDED
    excluded = {row["provider"] for row in data["excluded_providers"]}
    assert "raiderio" in excluded, "the retail-only guild provider must be dropped for wotlk"
    selected = data["selected_provider"]
    assert selected is None or selected in WOTLK_INCLUDED, result.describe()
    assert result.payload["provider"] == (selected or "warcraft")
    assert data["resolved"] is (selected is not None)


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


def test_guild_returns_one_identity_and_every_raid_raiderio_reports(require) -> None:
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
    assert summary["raid_count"] == len(summary["raids"]) >= 1
    assert summary["roster"]["member_count"] >= 1
    # Every raid Raider.IO reports progression for survives the wrap; none is invented.
    progression = source["payload"]["data"]["raiding"]["progression"]
    assert {raid["raid_slug"] for raid in summary["raids"]} == {row["raid_slug"] for row in progression}
    for raid in summary["raids"]:
        assert set(raid["ranks"]) == {"normal", "heroic", "mythic"}, raid["raid_slug"]


def test_guild_ranks_joins_every_raid_to_its_own_rankings_row(require) -> None:
    """Raider.IO reports progression and rankings as two lists; a wrong join hands a raid another
    raid's world rank, which reads as a perfectly plausible number.
    """
    require("raiderio")
    result = run("warcraft", "guild-ranks", REGION, REALM, GUILD)

    assert result.payload["kind"] == "guild_ranks"
    assert result.data["source"] == "raiderio"
    raids = result.data["raids"]
    assert raids and result.data["count"] == len(raids)

    raiding = result.data["provider_payload"]["data"]["raiding"]
    assert raiding["progression"] and raiding["rankings"], result.describe()
    _assert_rank_join(raids, raiding)

    mythic = [raid for raid in raids if raid["mythic_bosses_killed"]]
    assert mythic, "at least one raid must have mythic progress"
    for raid in mythic:
        assert {"world", "region", "realm"} <= set(raid["ranks"]["mythic"]), raid["raid_slug"]
    assert result.data["citations"]["profile"].startswith("https://raider.io/guilds/")


def test_guild_ranks_cover_the_raids_that_are_currently_open(require) -> None:
    """The snapshot names no active tier, so the currently open raids must all be in it: a guild
    whose ranks stop at last tier's raid is reporting a stale season as if it were current.
    """
    require("raiderio")
    catalog = run("raiderio", "raids")
    open_slugs = _open_raid_slugs(catalog.data["rows"], region=REGION)
    assert open_slugs, catalog.describe()

    result = run("warcraft", "guild-ranks", REGION, REALM, GUILD)
    reported = {raid["raid_slug"] for raid in result.data["raids"]}
    assert open_slugs <= reported, f"missing open raids {sorted(open_slugs - reported)}"


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


def test_expansion_advisory_rides_along_when_a_provider_has_no_expansion_axis(require) -> None:
    require("blizzard-api")
    result = run("warcraft", "--expansion", "wotlk", "blizzard", "doctor")

    advisory = result.data["expansion_advisory"]
    assert advisory["expansion_filter"] == "passthrough_no_expansion_semantics"
    assert advisory["requested_expansion"] == "wotlk"
    assert advisory["provider_expansion_mode"] == "none"
    # Mirrored at the top level for agents still reading the deprecated flat copies.
    assert result.payload["expansion_advisory"] == advisory


def test_expansion_advisory_survives_a_strict_field_projection(require) -> None:
    require("blizzard-api")
    result = run_raw("warcraft", "--fields", "expansion_advisory", "--fields-strict", "--expansion", "wotlk", "blizzard", "doctor")

    assert result.exit_code == 0, result.describe()
    assert set(result.payload) == {"expansion_advisory"}
    assert result.payload["expansion_advisory"]["requested_expansion"] == "wotlk"


def test_global_output_flags_shape_the_wrapper_payload() -> None:
    human = run("warcraft", "--profile", "human", "doctor")
    assert human.stdout.startswith("{\n"), "the human profile must pretty-print"

    compact = run("warcraft", "--compact", "--compact-max-chars", "40", "doctor")
    paths = compact.data["paths"]
    assert any(isinstance(value, str) and value.endswith("...") and len(value) == 40 for value in paths.values()), compact.describe()

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


def test_a_retired_output_profile_is_a_usage_error() -> None:
    """The `debug` preset is gone; asking for it must fail loudly rather than fall back to `agent`."""
    result = run("warcraft", "--profile", "debug", "schema", expect=EXIT_USAGE, error_code="invalid_argument")
    assert "agent, human" in result.payload["error"]["message"]


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

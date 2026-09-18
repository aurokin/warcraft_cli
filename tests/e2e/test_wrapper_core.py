"""End-to-end journeys for the ``warcraft`` wrapper's own commands.

Covers the routing and composition surface the wrapper owns: ``doctor``, ``schema``, ``search``,
``resolve``, expansion filtering, the guild composites, and provider passthrough. Provider-specific
depth lives in the per-provider journey modules; what is asserted here is the wrapper contract in
docs/foundation/WRAPPER_PROVIDER_CONTRACT.md.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from tests.e2e import pins
from tests.e2e.harness import EXIT_USAGE, REPO_ROOT, Result, payload_or_legacy, run, run_raw, run_text

REGION = pins.GUILD_REGION
REALM = pins.GUILD_REALM_DISPLAY
REALM_SLUG = "mal-ganis"
GUILD = pins.GUILD_NAME

TIERS = {"core", "supported", "experimental"}
# warcraftlogs is the only other expansion-profiled provider, so wotlk keeps exactly these two.
WOTLK_INCLUDED = ["wowhead", "warcraftlogs"]


def _provider_rows(result: Result) -> dict[str, dict[str, Any]]:
    rows = payload_or_legacy(result, "providers")
    assert isinstance(rows, list) and rows, result.describe()
    return {row["provider"]: row for row in rows}


def test_doctor_reports_a_tiered_readiness_row_for_every_provider(doctor_rows: dict[str, dict[str, Any]]) -> None:
    result = run("warcraft", "doctor")

    wrapper = payload_or_legacy(result, "wrapper")
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

    assert payload_or_legacy(result, "paths"), "doctor must report the resolved runtime paths"


def test_schema_matches_the_checked_in_envelope_schema() -> None:
    result = run("warcraft", "schema")

    schema = payload_or_legacy(result, "schema")
    checked_in = json.loads((REPO_ROOT / "schemas" / "envelope.schema.json").read_text())
    assert schema == checked_in, "warcraft schema must not drift from schemas/envelope.schema.json"
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert {"ok", "provider", "command", "kind", "schema_version", "query", "provenance", "data"} <= set(schema["properties"])


def test_search_fans_out_and_marks_every_provider_row_ok_or_failed() -> None:
    result = run("warcraft", "search", pins.ITEM_SEARCH_QUERY, "--limit", "5")

    included = payload_or_legacy(result, "included_providers")
    rows = _provider_rows(result)
    assert set(rows) == set(included), "every included provider must report a row"
    for name, row in rows.items():
        assert isinstance(row["ok"], bool), name
        assert (row["error"] is None) == row["ok"], f"{name} must carry an error exactly when it failed"
        assert row["status"] in {"ready", "partial", "error"}, name

    # wowhead is the core search provider for an item query: it must take part in the fanout, and
    # when it cannot answer it must say so with a structured error rather than a silent empty row.
    wowhead = rows["wowhead"]
    if wowhead["ok"]:
        assert wowhead["payload"]["data"]["results"], "wowhead must answer the pinned item query"
    else:
        assert wowhead["error"]["code"] and wowhead["error"]["message"], wowhead

    answered = [name for name, row in rows.items() if row["ok"] and row["payload"]["data"].get("results")]
    assert answered, f"no provider answered the fanout: {json.dumps({k: v['error'] for k, v in rows.items()})}"

    results = payload_or_legacy(result, "results")
    assert results and all(row["provider"] in answered for row in results)
    assert payload_or_legacy(result, "count") >= len(results)


def test_resolve_mirrors_the_selected_provider_when_it_resolves() -> None:
    result = run("warcraft", "resolve", pins.ITEM_SEARCH_QUERY, "--limit", "5")

    selected = payload_or_legacy(result, "selected_provider")
    assert payload_or_legacy(result, "resolved") is True, result.describe()
    assert selected in payload_or_legacy(result, "included_providers")
    assert result.payload["provider"] == selected, "a resolved envelope must be attributed to the matched provider"
    match = payload_or_legacy(result, "match")
    assert match["provider"] == selected
    assert payload_or_legacy(result, "next_command"), "a resolved match must hand over a follow-up command"


def test_resolve_attributes_an_unresolved_answer_to_the_wrapper() -> None:
    result = run("warcraft", "resolve", "zzqqxx nonsense query 8471", "--limit", "3")

    assert payload_or_legacy(result, "resolved") is False
    assert payload_or_legacy(result, "selected_provider") is None
    assert result.payload["provider"] == "warcraft", "nothing matched, so the wrapper owns the answer"
    assert payload_or_legacy(result, "match") is None
    assert payload_or_legacy(result, "next_command") is None


def test_expansion_filter_narrows_search_and_explains_every_exclusion() -> None:
    result = run("warcraft", "--expansion", "wotlk", "search", pins.ITEM_SEARCH_QUERY, "--limit", "3")

    assert payload_or_legacy(result, "requested_expansion") == "wotlk"
    assert payload_or_legacy(result, "expansion_filter_active") is True
    assert payload_or_legacy(result, "included_providers") == WOTLK_INCLUDED

    excluded = payload_or_legacy(result, "excluded_providers")
    assert excluded, "the wrapper must name the providers it dropped"
    for row in excluded:
        support = row["expansion_support"]
        assert support["requested_expansion"] == "wotlk"
        assert support["allowed"] is False
        assert support["exclusion_reason"], row["provider"]
    assert set(payload_or_legacy(result, "included_providers")) & {row["provider"] for row in excluded} == set()

    assert all(row["provider"] in WOTLK_INCLUDED for row in payload_or_legacy(result, "results"))


def test_expansion_filter_never_resolves_to_an_excluded_provider() -> None:
    result = run("warcraft", "--expansion", "wotlk", "resolve", f"guild {REGION} {REALM} {GUILD}", "--limit", "3")

    assert payload_or_legacy(result, "requested_expansion") == "wotlk"
    assert payload_or_legacy(result, "included_providers") == WOTLK_INCLUDED
    excluded = {row["provider"] for row in payload_or_legacy(result, "excluded_providers")}
    assert "raiderio" in excluded, "the retail-only guild provider must be dropped for wotlk"
    selected = payload_or_legacy(result, "selected_provider")
    assert selected is None or selected in WOTLK_INCLUDED, result.describe()


def test_guild_returns_one_identity_from_raiderio(require) -> None:
    require("raiderio")
    result = run("warcraft", "guild", REGION, REALM, GUILD)

    assert result.payload["query"] == {"region": REGION, "realm": REALM_SLUG, "name": GUILD}
    guild = payload_or_legacy(result, "guild")
    assert guild["name"] == GUILD
    assert guild["region"] == REGION
    assert guild["realm"].lower().replace("'", "").replace("-", "") == "malganis"

    sources = payload_or_legacy(result, "sources")
    assert set(sources) == {"raiderio"}
    source = sources["raiderio"]
    assert source["status"] == "ok", f"raiderio source failed: {source.get('error')}"
    # The source keeps its own envelope, so the citation trail survives the wrap.
    assert source["payload"]["ok"] is True
    assert source["payload"]["provenance"]["citations"]


def test_guild_ranks_reports_per_raid_ranks_with_citations(require) -> None:
    require("raiderio")
    result = run("warcraft", "guild-ranks", REGION, REALM, GUILD)

    assert result.payload["kind"] == "guild_ranks"
    assert payload_or_legacy(result, "source") == "raiderio"
    raids = payload_or_legacy(result, "raids")
    assert raids and payload_or_legacy(result, "count") == len(raids)
    for raid in raids:
        assert raid["raid_slug"]
        assert set(raid["ranks"]) == {"normal", "heroic", "mythic"}
    mythic = [raid for raid in raids if raid["mythic_bosses_killed"]]
    assert mythic, "at least one raid must have mythic progress"
    for raid in mythic:
        assert {"world", "region", "realm"} <= set(raid["ranks"]["mythic"]), raid["raid_slug"]
    assert payload_or_legacy(result, "citations")["profile"].startswith("https://raider.io/guilds/")


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


def test_fields_strict_rejects_a_missing_path() -> None:
    result = run("warcraft", "--fields", "data.no_such_key", "--fields-strict", "schema", expect=EXIT_USAGE, error_code="missing_fields")
    assert result.payload["error"]["details"]["missing_fields"] == ["data.no_such_key"]


def test_a_missing_argument_is_a_usage_error() -> None:
    result = run_text("warcraft", "guild", REGION, expect=EXIT_USAGE)
    assert "Usage:" in result.stderr or "Usage:" in result.stdout


def test_an_unknown_provider_is_a_usage_error() -> None:
    result = run_text("warcraft", "notaprovider", "doctor", expect=EXIT_USAGE)
    assert "No such command" in result.stderr or "No such command" in result.stdout


@pytest.mark.parametrize("command", ["search", "resolve"])
def test_an_unknown_expansion_is_rejected_rather_than_widened(command: str) -> None:
    # The wrapper must not silently widen scope when it cannot honour the requested expansion.
    result = run_text("warcraft", "--expansion", "not-an-expansion", command, pins.ITEM_SEARCH_QUERY, expect=EXIT_USAGE)
    assert result.stdout == "", result.describe()

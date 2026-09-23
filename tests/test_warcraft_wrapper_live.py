from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner
from warcraft_cli.main import app

runner = CliRunner()

RETAIL_FANOUT_PROVIDERS = {
    "wowhead",
    "method",
    "icy-veins",
    "raiderio",
    "warcraftlogs",
    "warcraft-wiki",
    "lorrgs",
}
NO_EXPANSION_AXIS_PROVIDERS = {"simc", "raidbots", "blizzard-api", "curseforge"}


def _envelope_for(args: list[str]) -> dict[str, object]:
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def _data_for(args: list[str]) -> dict[str, object]:
    """The payload body. Envelope-level keys (`provider`, `query`) live on the envelope, not here."""
    return _envelope_for(args)["data"]


def _assert_providers_answered(data: dict[str, object]) -> None:
    """The wrapper ran *and* providers answered.

    Without this, every routing assertion below is satisfied by a completely dead fanout: the
    included/excluded sets are computed from the registry before any provider is called.
    """
    assert data["failed_providers"] == [], f"providers failed: {data['failed_providers']}"
    assert data["answered_provider_count"] == data["included_provider_count"]


def _assert_selection_within(envelope: dict[str, object], *, included: set[str]) -> None:
    """Hold the resolve envelope identity contract for a filtered wrapper resolve.

    ``provider`` is the envelope identity and is always a string: the selected provider when one
    matched, otherwise the wrapper itself. ``selected_provider`` is the nullable selection and may
    only ever name a provider the expansion filter kept.
    """
    data = envelope["data"]
    selected = data["selected_provider"]
    assert selected is None or selected in included, f"selection {selected!r} escaped the filter"
    assert envelope["provider"] == (selected or "warcraft")
    assert data["resolved"] is (selected is not None)


@pytest.mark.live
def test_live_warcraft_search_expansion_filter_only_uses_supported_providers() -> None:
    data = _data_for(["--expansion", "wotlk", "search", "thunderfury", "--limit", "3"])

    assert data["requested_expansion"] == "wotlk"
    assert data["expansion_filter_active"] is True
    assert data["included_providers"] == ["wowhead", "warcraftlogs"]
    assert {row["provider"] for row in data["excluded_providers"]} == (
        RETAIL_FANOUT_PROVIDERS | NO_EXPANSION_AXIS_PROVIDERS
    ) - {"wowhead", "warcraftlogs"}
    _assert_providers_answered(data)
    assert data["results"], "wotlk search returned no candidates at all"
    assert all(row["provider"] == "wowhead" for row in data["results"])
    assert any("thunderfury" in str(row["name"]).lower() for row in data["results"])


@pytest.mark.live
def test_live_warcraft_resolve_expansion_filter_does_not_resolve_to_retail_only_provider() -> None:
    envelope = _envelope_for(["--expansion", "wotlk", "resolve", "guild us illidan Liquid", "--limit", "3"])
    data = envelope["data"]

    assert data["requested_expansion"] == "wotlk"
    assert data["expansion_filter_active"] is True
    assert data["included_providers"] == ["wowhead", "warcraftlogs"]
    _assert_providers_answered(data)
    _assert_selection_within(envelope, included={"wowhead", "warcraftlogs"})
    if not data["resolved"]:
        # An unresolved resolve must still hand the agent a next step rather than dead-end.
        assert data["fallback_search_command"] or data["best_unresolved_candidate"]


@pytest.mark.live
def test_live_warcraft_search_retail_filter_returns_different_rows_than_unfiltered_search() -> None:
    filtered = _data_for(["--expansion", "retail", "search", "thunderfury", "--limit", "5"])
    unfiltered = _data_for(["search", "thunderfury", "--limit", "5"])

    assert filtered["requested_expansion"] == "retail"
    assert filtered["expansion_filter_active"] is True
    assert unfiltered["expansion_filter_active"] is False
    assert set(filtered["included_providers"]) == RETAIL_FANOUT_PROVIDERS
    assert {row["provider"] for row in filtered["excluded_providers"]} == NO_EXPANSION_AXIS_PROVIDERS
    _assert_providers_answered(filtered)
    _assert_providers_answered(unfiltered)
    assert filtered["results"] and unfiltered["results"]
    # The retail filter pins wowhead to the retail profile, so the two runs are not the same call.
    filtered_expansions = {
        row.get("provider_expansion", {}).get("requested_expansion") for row in filtered["results"]
    }
    assert filtered_expansions == {"retail"}
    assert {row.get("provider_expansion", {}).get("requested_expansion") for row in unfiltered["results"]} == {None}


@pytest.mark.live
def test_live_warcraft_search_merges_more_than_one_provider() -> None:
    """Normalized scores must keep the merged list from collapsing onto a single provider."""
    data = _data_for(["search", "mistweaver monk guide", "--limit", "6"])

    _assert_providers_answered(data)
    answering = {row["provider"] for row in data["providers"] if (row["payload"].get("data") or {}).get("count")}
    assert len(answering) >= 2, f"only {answering} returned candidates"
    assert len({row["provider"] for row in data["results"]}) >= 2, data["results"]


@pytest.mark.live
def test_live_warcraft_resolve_retail_filter_can_use_fixed_retail_provider() -> None:
    envelope = _envelope_for(["--expansion", "retail", "resolve", "guild us illidan Liquid", "--limit", "3"])
    data = envelope["data"]

    assert data["requested_expansion"] == "retail"
    assert data["expansion_filter_active"] is True
    assert set(data["included_providers"]) == RETAIL_FANOUT_PROVIDERS
    assert {row["provider"] for row in data["excluded_providers"]} == NO_EXPANSION_AXIS_PROVIDERS
    _assert_providers_answered(data)
    assert data["resolved"] is True
    _assert_selection_within(envelope, included=set(data["included_providers"]))
    assert data["selected_provider"] != "warcraft-wiki"
    assert data["match"]["name"]
    assert data["next_command"]


@pytest.mark.live
def test_live_warcraft_guild_contract() -> None:
    envelope = _envelope_for(["guild", "us", "Mal'Ganis", "gn"])
    data = envelope["data"]

    assert envelope["query"] == {"region": "us", "realm": "mal-ganis", "name": "gn"}
    assert set(data["sources"]) == {"raiderio"}
    summary = data["sources"]["raiderio"]["summary"]
    assert summary["raid_count"] == len(summary["raids"]) >= 1
    # Every raid is joined to its own ranks by slug; no row borrows another raid's ranks.
    for raid in summary["raids"]:
        assert raid["raid_slug"]
        assert set(raid["ranks"]) == {"normal", "heroic", "mythic"}


@pytest.mark.live
def test_live_warcraft_guild_ranks_contract() -> None:
    data = _data_for(["guild-ranks", "us", "Mal'Ganis", "gn"])

    assert data["source"] == "raiderio"
    assert data["count"] >= 1
    assert data["count"] == len(data["raids"])
    assert data["raids"][0]["raid_slug"]
    assert set(data["raids"][0]["ranks"]) == {"normal", "heroic", "mythic"}

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner
from warcraft_cli.main import app

runner = CliRunner()


def _payload_for(args: list[str]) -> dict[str, object]:
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def _skip_if_wowprogress_blocked(error_payload: dict[str, object]) -> None:
    error = error_payload.get("error") if isinstance(error_payload.get("error"), dict) else {}
    error_code = error.get("code") if isinstance(error.get("code"), str) else None
    if error_code == "blocked":
        pytest.skip("WowProgress live requests are currently blocked by upstream bot protection.")


@pytest.mark.live
def test_live_warcraft_search_expansion_filter_only_uses_supported_providers() -> None:
    payload = _payload_for(["--expansion", "wotlk", "search", "thunderfury", "--limit", "3"])

    assert payload["requested_expansion"] == "wotlk"
    assert payload["expansion_filter_active"] is True
    assert payload["included_providers"] == ["wowhead"]
    assert {row["provider"] for row in payload["excluded_providers"]} == {
        "method",
        "icy-veins",
        "raiderio",
        "warcraftlogs",
        "warcraft-wiki",
        "wowprogress",
        "simc",
        "raidbots",
        "blizzard-api",
    }
    assert all(row["provider"] == "wowhead" for row in payload["results"])


@pytest.mark.live
def test_live_warcraft_resolve_expansion_filter_does_not_resolve_to_retail_only_provider() -> None:
    payload = _payload_for(["--expansion", "wotlk", "resolve", "guild us illidan Liquid", "--limit", "3"])

    assert payload["requested_expansion"] == "wotlk"
    assert payload["expansion_filter_active"] is True
    assert payload["included_providers"] == ["wowhead"]
    assert payload["provider"] in {None, "wowhead"}
    assert payload["provider"] != "wowprogress"
    assert payload["provider"] != "raiderio"


@pytest.mark.live
def test_live_warcraft_search_retail_filter_is_not_same_as_unfiltered_search() -> None:
    payload = _payload_for(["--expansion", "retail", "search", "mistweaver monk guide", "--limit", "5"])

    assert payload["requested_expansion"] == "retail"
    assert payload["expansion_filter_active"] is True
    assert set(payload["included_providers"]) == {
        "wowhead",
        "method",
        "icy-veins",
        "raiderio",
        "warcraftlogs",
        "warcraft-wiki",
        "wowprogress",
    }
    assert {row["provider"] for row in payload["excluded_providers"]} == {"simc", "raidbots", "blizzard-api", "curseforge"}


@pytest.mark.live
def test_live_warcraft_resolve_retail_filter_can_use_fixed_retail_provider() -> None:
    payload = _payload_for(["--expansion", "retail", "resolve", "guild us illidan Liquid", "--limit", "3"])

    assert payload["requested_expansion"] == "retail"
    assert payload["expansion_filter_active"] is True
    assert set(payload["included_providers"]) == {
        "wowhead",
        "method",
        "icy-veins",
        "raiderio",
        "warcraftlogs",
        "warcraft-wiki",
        "wowprogress",
    }
    assert {row["provider"] for row in payload["excluded_providers"]} == {"simc", "raidbots", "blizzard-api", "curseforge"}
    assert payload["provider"] in {"wowprogress", "raiderio", "wowhead", None}
    assert payload["provider"] != "warcraft-wiki"


@pytest.mark.live
def test_live_warcraft_guild_contract() -> None:
    payload = _payload_for(["guild", "us", "Mal'Ganis", "gn"])

    assert payload["ok"] is True
    assert payload["query"] == {"region": "us", "realm": "mal-ganis", "name": "gn"}
    wowprogress_source = payload["sources"]["wowprogress"]
    if wowprogress_source["status"] != "ok":
        _skip_if_wowprogress_blocked(wowprogress_source)
    assert payload["sources"]["wowprogress"]["status"] == "ok"
    assert payload["sources"]["raiderio"]["status"] == "ok"


@pytest.mark.live
def test_live_warcraft_guild_ranks_contract() -> None:
    result = runner.invoke(app, ["guild-ranks", "us", "Mal'Ganis", "gn"])
    if result.exit_code != 0:
        _skip_if_wowprogress_blocked(json.loads(result.stderr or result.output))
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)

    assert payload["ok"] is True
    assert payload["source"] == "wowprogress"
    assert payload["count"] >= 1
    assert payload["tiers"][0]["raid"]

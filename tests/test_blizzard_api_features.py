"""PvP, collections and auction reads, served from trimmed captured Battle.net bodies.

The bodies under tests/fixtures/blizzard/captured/ were captured live on 2026-10-03 (us, retail) and
trimmed to a few rows each. The realm record behind `auctions` is the synthetic realm.json.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import blizzard_api_cli.client as client_module
import httpx
import pytest
from blizzard_api_cli.main import app
from blizzard_api_cli.summaries import auctions_view
from typer.testing import CliRunner
from warcraft_core.envelope import envelope_violations

runner = CliRunner()

FIXTURES = Path(__file__).parent / "fixtures" / "blizzard"
LAST_MODIFIED = "Sun, 4 Oct 2026 04:43:38 GMT"

# API path -> fixture file, for every GET these commands make.
ROUTES = {
    "/data/wow/pvp-season/index": "captured/pvp_season_index.json",
    "/data/wow/pvp-season/42": "captured/pvp_season.json",
    "/data/wow/pvp-season/41": "captured/pvp_season.json",
    "/data/wow/pvp-season/42/pvp-leaderboard/index": "captured/pvp_leaderboard_index.json",
    "/data/wow/pvp-season/41/pvp-leaderboard/index": "captured/pvp_leaderboard_index.json",
    "/data/wow/pvp-season/42/pvp-reward/index": "captured/pvp_rewards.json",
    "/data/wow/pvp-season/41/pvp-reward/index": "captured/pvp_rewards.json",
    "/data/wow/pvp-season/42/pvp-leaderboard/3v3": "captured/pvp_leaderboard_3v3.json",
    "/data/wow/pvp-season/41/pvp-leaderboard/3v3": "captured/pvp_leaderboard_3v3.json",
    "/profile/wow/character/tichondrius/takhfiend/pvp-summary": "captured/pvp_summary.json",
    "/profile/wow/character/tichondrius/takhfiend/pvp-bracket/shuffle-priest-discipline": "captured/pvp_bracket_shuffle.json",
    "/profile/wow/character/tichondrius/takhfiend/pvp-bracket/3v3": "captured/pvp_bracket_3v3.json",
    **{
        f"/profile/wow/character/malganis/aurow/collections/{kind}": f"captured/collections_{kind}.json"
        for kind in ("mounts", "pets", "toys", "heirlooms", "transmogs")
    },
    "/data/wow/auctions/commodities": "captured/commodities.json",
    "/data/wow/realm/illidan": "realm.json",
    "/data/wow/connected-realm/57/auctions": "captured/connected_realm_auctions.json",
}


@pytest.fixture(autouse=True)
def _isolate_blizzard_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("BLIZZARD_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BLIZZARD_CLIENT_ID", "test-id")
    monkeypatch.setenv("BLIZZARD_CLIENT_SECRET", "test-secret")
    monkeypatch.delenv("BLIZZARD_REGION", raising=False)
    monkeypatch.setenv("BLIZZARD_CACHE_BACKEND", "none")


def _serve(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    """Answer the token POST and every routed GET; 404 anything else. Records (path, params) per GET."""
    requested: list[tuple[str, dict[str, Any]]] = []

    def _fake(client: Any, url: str, *, method: str = "GET", **kwargs: Any) -> httpx.Response:
        request = httpx.Request(method, url)
        if url.endswith("/token"):
            return httpx.Response(200, json={"access_token": "fake-token", "expires_in": 3600}, request=request)
        path = url.split(".api.blizzard.com")[1]
        requested.append((path, kwargs.get("params") or {}))
        if path not in ROUTES:
            raise httpx.HTTPStatusError("Not Found", request=request, response=httpx.Response(404, request=request))
        body = json.loads((FIXTURES / ROUTES[path]).read_text())
        return httpx.Response(200, json=body, headers={"Last-Modified": LAST_MODIFIED}, request=request)

    monkeypatch.setattr(client_module, "request_with_retries", _fake)
    return requested


def _ok(args: list[str]) -> dict[str, Any]:
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert envelope_violations(payload) == []
    return payload


def _usage_error(args: list[str], code: str) -> str:
    result = runner.invoke(app, args)
    assert result.exit_code == 2, result.output
    error = json.loads(result.stderr)["error"]
    assert error["code"] == code
    return str(error["message"])


def test_pvp_season_defaults_to_the_current_season_and_lists_its_cutoffs(monkeypatch: pytest.MonkeyPatch) -> None:
    requested = _serve(monkeypatch)
    payload = _ok(["pvp-season"])

    assert [path for path, _ in requested] == [
        "/data/wow/pvp-season/index",
        "/data/wow/pvp-season/42",
        "/data/wow/pvp-season/42/pvp-leaderboard/index",
        "/data/wow/pvp-season/42/pvp-reward/index",
    ]
    assert payload["kind"] == "pvp_season"
    data = payload["data"]
    assert (data["season_id"], data["current_season_id"]) == (42, 42)
    assert data["season_name"] == "Player vs. Player (Midnight Season 2)"
    assert data["season_start"] == "2026-08-18T15:00:00Z"
    assert data["seasons"] == [42, 41, 40]
    assert data["brackets"] == ["2v2", "blitz-overall", "shuffle-overall", "3v3", "rbg", "shuffle-mage-frost"]
    gladiator = next(row for row in data["rewards"] if row["bracket"] == "ARENA_3v3")
    assert gladiator == {
        "bracket": "ARENA_3v3",
        "achievement": "Venomous Gladiator: Midnight Season 2",
        "achievement_id": 62922,
        "rating_cutoff": 2761,
        "specialization": None,
        "specialization_id": None,
        "faction": None,
    }
    # Shuffle cutoffs are per spec, battleground ones per faction.
    assert {(row["specialization_id"], row["rating_cutoff"]) for row in data["rewards"] if row["bracket"] == "SHUFFLE"} == {(581, 1016), (72, 2625)}
    assert {row["faction"] for row in data["rewards"] if row["bracket"] == "BATTLEGROUNDS"} == {"ALLIANCE", "HORDE"}
    assert data["freshness"]["last_modified"] == "2026-10-04T04:43:38Z"
    assert isinstance(data["freshness"]["age_seconds"], int)


def test_pvp_season_reads_the_season_asked_for(monkeypatch: pytest.MonkeyPatch) -> None:
    requested = _serve(monkeypatch)
    data = _ok(["pvp-season", "41"])["data"]
    assert "/data/wow/pvp-season/41/pvp-reward/index" in [path for path, _ in requested]
    assert (data["season_id"], data["current_season_id"]) == (41, 42)


def test_pvp_leaderboard_returns_flat_rows_up_to_the_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    requested = _serve(monkeypatch)
    payload = _ok(["pvp-leaderboard", "3V3", "--limit", "2"])

    path, params = requested[-1]
    assert path == "/data/wow/pvp-season/42/pvp-leaderboard/3v3"
    # Nothing in a leaderboard is localized, so one cached copy serves every --locale.
    assert params == {"namespace": "dynamic-us"}
    data = payload["data"]
    assert (data["bracket"], data["bracket_type"], data["season_id"]) == ("3v3", "ARENA_3v3", 42)
    assert (data["total_entries"], data["returned"], data["truncated"]) == (3, 2, True)
    assert data["entries"][0] == {
        "rank": 1,
        "rating": 2913,
        "name": "Promisedland",
        "realm": "sargeras",
        "character_id": 203590047,
        "faction": "HORDE",
        "played": 166,
        "won": 123,
        "lost": 43,
        "tier_id": 14,
    }
    assert data["freshness"]["last_modified"] == "2026-10-04T04:43:38Z"


def test_pvp_leaderboard_reads_the_season_asked_for(monkeypatch: pytest.MonkeyPatch) -> None:
    requested = _serve(monkeypatch)
    _ok(["pvp-leaderboard", "3v3", "--season", "41"])
    assert requested[-1][0] == "/data/wow/pvp-season/41/pvp-leaderboard/3v3"


@pytest.mark.parametrize("bracket", ["../../realm/illidan", "3v3?x=1", "3v3/../2v2"])
def test_a_bracket_that_is_not_one_path_segment_is_refused_before_any_request(monkeypatch: pytest.MonkeyPatch, bracket: str) -> None:
    requested = _serve(monkeypatch)
    _usage_error(["pvp-leaderboard", bracket], "invalid_query")
    assert requested == []


def test_a_leaderboard_is_cached_whole_so_a_larger_limit_replays_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BLIZZARD_CACHE_BACKEND", "file")
    _serve(monkeypatch)
    _ok(["pvp-leaderboard", "3v3", "--limit", "1"])
    monkeypatch.setattr(client_module, "request_with_retries", lambda *args, **kwargs: pytest.fail("cache miss"))
    replay = _ok(["pvp-leaderboard", "3v3", "--limit", "3"])
    assert [row["rank"] for row in replay["data"]["entries"]] == [1, 2, 3]
    # Blizzard's Last-Modified is cached with the view, so a replay still dates the snapshot.
    assert replay["data"]["freshness"]["last_modified"] == "2026-10-04T04:43:38Z"
    assert replay["provenance"]["cache"]["all_hits"] is True


def test_pvp_character_reads_every_bracket_its_summary_links(monkeypatch: pytest.MonkeyPatch) -> None:
    requested = _serve(monkeypatch)
    payload = _ok(["pvp-character", "us", "Tichondrius", "Takhfiend"])

    assert [path for path, _ in requested] == [
        "/profile/wow/character/tichondrius/takhfiend/pvp-summary",
        "/profile/wow/character/tichondrius/takhfiend/pvp-bracket/shuffle-priest-discipline",
        "/profile/wow/character/tichondrius/takhfiend/pvp-bracket/3v3",
    ]
    data = payload["data"]
    assert payload["kind"] == "pvp_character"
    assert data["character"] == {"name": "Takhfiend", "id": 242315347, "realm": "tichondrius"}
    assert (data["honor_level"], data["honorable_kills"]) == (1573, 29992)
    shuffle, arena = data["brackets"]
    assert shuffle["bracket"] == "shuffle-priest-discipline"
    assert (shuffle["rating"], shuffle["specialization"], shuffle["season_id"]) == (3068, "Discipline", 42)
    # Solo Shuffle reports rounds as well as matches; the other brackets have no rounds.
    assert shuffle["season_rounds"] == {"played": 262, "won": 150, "lost": 112}
    assert arena == {
        "bracket": "3v3",
        "bracket_type": "ARENA_3v3",
        "season_id": 42,
        "rating": 2228,
        "tier_id": 13,
        "specialization": None,
        "season": {"played": 87, "won": 57, "lost": 30},
        "weekly": {"played": 16, "won": 13, "lost": 3},
    }
    assert data["battlegrounds"][0] == {"map": "Alterac Valley", "played": 89, "won": 35, "lost": 54}


def test_classic_pvp_character_probes_the_brackets_its_summary_leaves_out(monkeypatch: pytest.MonkeyPatch) -> None:
    # Live, Classic pvp-summary linked only 2v2 for the top 3v3 player; pvp-bracket/3v3 still answered.
    base = "/profile/wow/character/tichondrius/takhfiend"
    summary = json.loads((FIXTURES / "captured/pvp_summary.json").read_text())
    summary["brackets"] = [{"href": f"https://us.api.blizzard.com{base}/pvp-bracket/2v2?namespace=profile-classic-us"}]
    summary_file = Path("classic_pvp_summary.json").resolve()
    summary_file.write_text(json.dumps(summary))
    monkeypatch.setitem(ROUTES, f"{base}/pvp-summary", str(summary_file))
    monkeypatch.setitem(ROUTES, f"{base}/pvp-bracket/2v2", "captured/pvp_bracket_3v3.json")
    requested = _serve(monkeypatch)
    data = _ok(["pvp-character", "Tichondrius", "Takhfiend", "--classic"])["data"]

    assert [path.rsplit("/", 1)[-1] for path, _ in requested] == ["pvp-summary", "2v2", "3v3", "5v5", "rbg"]
    # 5v5 and rbg answered 404: never played, so no row and no error.
    assert [(row["bracket"], row["rating"]) for row in data["brackets"]] == [("2v2", 2228), ("3v3", 2228)]


def test_retail_pvp_character_reads_only_the_linked_brackets(monkeypatch: pytest.MonkeyPatch) -> None:
    requested = _serve(monkeypatch)
    _ok(["pvp-character", "Tichondrius", "Takhfiend"])
    assert "5v5" not in [path.rsplit("/", 1)[-1] for path, _ in requested]


def test_collections_count_everything_and_list_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    requested = _serve(monkeypatch)
    payload = _ok(["collections", "Mal'Ganis", "Aurow", "--limit", "2"])

    # The first read settles the realm's slug spelling; the other four go straight to it.
    assert [path.rsplit("/", 1)[-1] for path, _ in requested] == ["mounts", "pets", "toys", "heirlooms", "transmogs"]
    data = payload["data"]
    assert data["character"] == {"name": "Aurow", "realm": "malganis"}
    mounts = data["collections"]["mounts"]
    assert (mounts["count"], mounts["matched"], mounts["returned"], mounts["truncated"]) == (3, 3, 2, True)
    assert [row["name"] for row in mounts["items"]] == ["Brown Horse", "Raven Lord"]
    assert mounts["items"][0] == {"id": 6, "name": "Brown Horse", "is_favorite": False, "is_useable": False}
    pets = data["collections"]["pets"]
    assert (pets["count"], pets["unique_species"]) == (4, 3)
    assert pets["items"][0]["name"] == "Abyssius"
    transmogs = data["collections"]["transmogs"]
    assert (transmogs["count"], transmogs["appearance_count"]) == (2, 5)
    assert transmogs["appearances_by_slot"] == {"HEAD": 3, "SHOULDER": 2}
    assert data["collections"]["heirlooms"]["items"][0] == {"id": 706, "name": "Polished Helm of Valor", "upgrade_level": 3}


def test_collections_kind_and_match_narrow_the_read(monkeypatch: pytest.MonkeyPatch) -> None:
    requested = _serve(monkeypatch)
    data = _ok(["collections", "malganis", "aurow", "--kind", "pets", "--match", "ABYSS"])["data"]
    assert [path for path, _ in requested] == ["/profile/wow/character/malganis/aurow/collections/pets"]
    pets = data["collections"]["pets"]
    assert list(data["collections"]) == ["pets"]
    assert (pets["count"], pets["matched"], pets["truncated"]) == (4, 2, False)
    assert {row["species_id"] for row in pets["items"]} == {1624}


def test_an_unknown_collection_kind_is_refused_before_any_request(monkeypatch: pytest.MonkeyPatch) -> None:
    requested = _serve(monkeypatch)
    message = _usage_error(["collections", "malganis", "aurow", "--kind", "achievements"], "invalid_query")
    assert "mounts, pets, toys, heirlooms, transmogs" in message
    assert requested == []


def test_commodities_summarize_each_item_by_unit_weighted_median(monkeypatch: pytest.MonkeyPatch) -> None:
    requested = _serve(monkeypatch)
    payload = _ok(["commodities", "--item-id", "198161", "--item-id", "1"])

    assert requested[-1] == ("/data/wow/auctions/commodities", {"namespace": "dynamic-us"})
    data = payload["data"]
    assert (data["auction_count"], data["item_count"]) == (6, 2)
    # 134 units at 29200, 200 at 455500 and 5 at 499999400: half of the 339 units cost 455500 or less.
    assert data["items"] == [
        {"item_id": 198161, "auctions": 4, "quantity": 339, "min_unit_price": 29200, "median_unit_price": 455500}
    ]
    assert data["not_listed"] == [1]
    assert data["freshness"]["last_modified"] == "2026-10-04T04:43:38Z"


def test_commodities_without_an_item_filter_list_the_most_listed_items(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch)
    data = _ok(["commodities", "--limit", "1"])["data"]
    assert [row["item_id"] for row in data["items"]] == [198161]
    assert (data["returned"], data["truncated"]) == (1, True)


def test_classic_commodities_are_a_usage_error_saying_no_classic_prices_exist(monkeypatch: pytest.MonkeyPatch) -> None:
    requested = _serve(monkeypatch)
    message = _usage_error(["commodities", "--classic"], "unsupported_game_version")
    # Classic realm auction houses carry no trade goods, so the error must not send agents there.
    assert "no commodity" in message and "blizzard auctions" not in message
    assert requested == []


def test_realm_auctions_follow_the_realm_to_its_connected_realm(monkeypatch: pytest.MonkeyPatch) -> None:
    requested = _serve(monkeypatch)
    payload = _ok(["auctions", "Illidan", "--item-id", "232662"])

    assert [path for path, _ in requested] == ["/data/wow/realm/illidan", "/data/wow/connected-realm/57/auctions"]
    data = payload["data"]
    assert (data["realm"], data["connected_realm_id"], data["auction_count"]) == ("illidan", 57, 4)
    # Realm listings carry the whole listing's buyout; the bids are not prices anyone can pay now.
    assert data["items"] == [
        {"item_id": 232662, "auctions": 3, "quantity": 3, "min_unit_price": 8108731100, "median_unit_price": 8108766800}
    ]
    assert data["not_listed"] == []
    assert payload["provenance"]["source_url"].endswith("/data/wow/connected-realm/57/auctions")


def test_a_bid_only_listing_counts_toward_units_but_not_prices() -> None:
    # Synthetic: Blizzard omits `buyout` on a listing that can only be bid on.
    view = auctions_view(
        {
            "auctions": [
                {"item": {"id": 7}, "bid": 500, "quantity": 2},
                {"item": {"id": 8}, "bid": 100, "quantity": 1},
                {"item": {"id": 8}, "buyout": 900, "quantity": 3},
            ]
        }
    )
    assert view["items"] == [
        {"item_id": 8, "auctions": 2, "quantity": 4, "min_unit_price": 300, "median_unit_price": 300},
        {"item_id": 7, "auctions": 1, "quantity": 2, "min_unit_price": None, "median_unit_price": None},
    ]

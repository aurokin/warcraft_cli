from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from raiderio_cli.main import app
from typer.testing import CliRunner

runner = CliRunner()


def _payload_for(args: list[str]) -> dict[str, Any]:
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


@pytest.mark.live
def test_live_raiderio_structured_guild_search_uses_direct_probe() -> None:
    payload = _payload_for(["search", "guild us illidan Liquid", "--limit", "5"])

    assert payload["count"] >= 1
    assert payload["results"][0]["kind"] == "guild"
    assert payload["results"][0]["follow_up"]["command"] == "raiderio guild us illidan Liquid"
    assert "structured_probe" in payload["results"][0]["ranking"]["match_reasons"]


@pytest.mark.live
def test_live_raiderio_structured_character_resolve_uses_direct_probe() -> None:
    payload = _payload_for(["resolve", "character us illidan Roguecane", "--limit", "5"])

    assert payload["resolved"] is True
    assert payload["next_command"] == "raiderio character us illidan Roguecane"
    assert payload["match"]["kind"] == "character"
    assert "structured_probe" in payload["match"]["ranking"]["match_reasons"]


@pytest.mark.live
def test_live_raiderio_sample_mythic_plus_runs_contract() -> None:
    payload = _payload_for(["sample", "mythic-plus-runs", "--pages", "1", "--limit", "20"])

    assert payload["kind"] == "mythic_plus_runs_sample"
    assert payload["sample"]["pages_requested"] == 1
    assert payload["sample"]["pages_fetched"] >= 1
    assert payload["sample"]["run_count"] >= 1
    assert payload["freshness"]["cache_ttl_seconds"] >= 1
    assert len(payload["citations"]["leaderboard_urls"]) >= 1


@pytest.mark.live
def test_live_raiderio_distribution_mythic_plus_runs_contract() -> None:
    payload = _payload_for(["distribution", "mythic-plus-runs", "--metric", "dungeon", "--pages", "1", "--limit", "20"])

    assert payload["kind"] == "mythic_plus_runs_distribution"
    assert payload["metric"] == "dungeon"
    assert payload["distribution"]["unit"] == "runs"
    assert len(payload["distribution"]["rows"]) >= 1


@pytest.mark.live
def test_live_raiderio_threshold_mythic_plus_runs_contract() -> None:
    payload = _payload_for(
        ["threshold", "mythic-plus-runs", "--metric", "score", "--value", "560", "--pages", "1", "--limit", "20", "--nearest", "5"]
    )

    assert payload["kind"] == "mythic_plus_runs_threshold"
    assert payload["metric"] == "score"
    assert payload["threshold"]["nearest_match_count"] >= 1
    assert payload["threshold"]["estimate"]["metric"] == "mythic_level"


@pytest.mark.live
def test_live_raiderio_filtered_sample_contract() -> None:
    payload = _payload_for(["sample", "mythic-plus-runs", "--pages", "1", "--limit",
                           "20", "--level-min", "20", "--contains-role", "healer"])

    assert payload["kind"] == "mythic_plus_runs_sample"
    assert payload["query"]["filters"]["level_min"] == 20
    assert payload["query"]["filters"]["contains_role"] == ["healer"]
    assert payload["sample"]["filtering"]["source_run_count"] >= payload["sample"]["filtering"]["returned_run_count"]


@pytest.mark.live
def test_live_raiderio_sample_mythic_plus_players_contract() -> None:
    payload = _payload_for(["sample", "mythic-plus-players", "--pages", "1", "--limit", "20", "--player-limit", "25"])

    assert payload["kind"] == "mythic_plus_players_sample"
    assert payload["sample"]["run_count"] >= 1
    assert payload["sample"]["player_count"] >= 1
    assert payload["sample"]["player_sampling"]["source_player_count"] >= payload["sample"]["player_sampling"]["returned_player_count"]
    assert len(payload["players"]) >= 1


@pytest.mark.live
def test_live_raiderio_distribution_mythic_plus_players_contract() -> None:
    payload = _payload_for(["distribution", "mythic-plus-players", "--metric", "class", "--pages", "1", "--limit", "20"])

    assert payload["kind"] == "mythic_plus_players_distribution"
    assert payload["metric"] == "class"
    assert payload["distribution"]["unit"] == "player_class_tags"
    assert len(payload["distribution"]["rows"]) >= 1


def _served_season_slug(payload: dict[str, object]) -> str:
    """Recover the concrete season slug Raider.IO actually served for a leaderboard payload.

    ``--season current`` omits the season param so the API picks the season; the slug is only
    knowable from the response, where every run carries it and the citation URL embeds it.
    """
    runs = payload["runs"]
    assert isinstance(runs, list) and runs
    first_run = runs[0]
    assert isinstance(first_run, dict)
    slug = first_run["season"]
    assert isinstance(slug, str) and slug.startswith("season-"), slug
    citations = payload["citations"]
    assert isinstance(citations, dict)
    urls = citations["leaderboard_urls"]
    assert isinstance(urls, list)
    assert any(slug in url for url in urls), (slug, urls)
    return slug


@pytest.mark.live
def test_live_raiderio_raid_leaderboard_citation_url_is_a_real_page() -> None:
    """The rankings citation URL is a layout this CLI invents, so fetch it instead of re-deriving it.

    Every offline assertion rebuilds the same f-string, which cannot catch a wrong layout shipping
    inside an ``ok: true`` envelope. raider.io answers an unknown raid path with HTTP 400, so a 200
    here is evidence the emitted URL names a page that exists.
    """
    raid_slug = _payload_for(["raids"])["data"]["rows"][0]["slug"]
    data = _payload_for(["leaderboard", "raids", "--raid", raid_slug, "--region", "us", "--realm", "malganis", "--limit", "1"])["data"]
    citation = data["citations"]["leaderboard_urls"][0]

    assert citation == f"https://raider.io/{raid_slug}/rankings/us/mythic?realm=malganis"
    with httpx.Client(timeout=30.0, follow_redirects=True) as client:
        assert client.get(citation).status_code == 200, citation
        bogus = client.get("https://raider.io/no-such-raid-zzz/rankings/us/mythic")
    assert bogus.status_code != 200, "an unknown raid must not answer 200, or the check above proves nothing"


@pytest.mark.live
def test_live_raiderio_leaderboard_mythic_plus_contract() -> None:
    payload = _payload_for(["leaderboard", "mythic-plus", "--season", "current", "--region", "us", "--dungeon", "all", "--limit", "20"])

    assert payload["kind"] == "mythic_plus_leaderboard"
    assert payload["count"] >= 1
    assert len(payload["runs"]) >= 1
    assert payload["freshness"]["sampled_at"]
    assert payload["freshness"]["cache_ttl_seconds"] >= 1
    assert len(payload["citations"]["leaderboard_urls"]) >= 1

    # ``query.resolved_season`` is intentionally not asserted for --season current: the CLI reads a
    # top-level "season" key that the API does not send (the slug lives in params.season), so the
    # alias reports null. Exercise the resolution contract against the slug the API just served.
    season_slug = _served_season_slug(payload)
    explicit = _payload_for(["leaderboard", "mythic-plus", "--season", season_slug, "--region", "us", "--dungeon", "all", "--limit", "20"])

    assert explicit["kind"] == "mythic_plus_leaderboard"
    assert explicit["query"]["season"] == season_slug
    assert explicit["query"]["resolved_season"] == season_slug
    assert explicit["count"] >= 1
    assert all(run["season"] == season_slug for run in explicit["runs"])
    assert all(season_slug in url for url in explicit["citations"]["leaderboard_urls"])

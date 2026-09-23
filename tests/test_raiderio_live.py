from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from raiderio_cli.main import app
from typer.testing import CliRunner

runner = CliRunner()


def _payload_for(args: list[str]) -> dict[str, Any]:
    """The whole envelope: ``kind``, ``query`` and ``provenance`` live here, the payload in ``data``."""
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def _data_for(args: list[str]) -> dict[str, Any]:
    """The envelope's ``data`` block. The flat top-level copies of it are deprecated."""
    data = _payload_for(args)["data"]
    assert isinstance(data, dict)
    return data


@pytest.mark.live
def test_live_raiderio_structured_guild_search_uses_direct_probe() -> None:
    data = _data_for(["search", "guild us illidan Liquid", "--limit", "5"])

    assert data["count"] >= 1
    assert data["results"][0]["kind"] == "guild"
    assert data["results"][0]["follow_up"]["command"] == "raiderio guild us illidan Liquid"
    assert "structured_probe" in data["results"][0]["ranking"]["match_reasons"]


@pytest.mark.live
def test_live_raiderio_structured_character_resolve_uses_direct_probe() -> None:
    data = _data_for(["resolve", "character us illidan Roguecane", "--limit", "5"])

    assert data["resolved"] is True
    assert data["next_command"] == "raiderio character us illidan Roguecane"
    assert data["match"]["kind"] == "character"
    assert "structured_probe" in data["match"]["ranking"]["match_reasons"]


@pytest.mark.live
def test_live_raiderio_sample_mythic_plus_runs_contract() -> None:
    payload = _payload_for(["sample", "mythic-plus-runs", "--pages", "1", "--limit", "20"])
    data = payload["data"]

    assert payload["kind"] == "mythic_plus_runs_sample"
    assert data["sample"]["pages_requested"] == 1
    assert data["sample"]["pages_fetched"] >= 1
    assert data["sample"]["run_count"] >= 1
    assert data["freshness"]["cache_ttl_seconds"] >= 1
    assert len(data["citations"]["leaderboard_urls"]) >= 1


@pytest.mark.live
def test_live_raiderio_distribution_mythic_plus_runs_contract() -> None:
    payload = _payload_for(["distribution", "mythic-plus-runs", "--metric", "dungeon", "--pages", "1", "--limit", "20"])
    data = payload["data"]

    assert payload["kind"] == "mythic_plus_runs_distribution"
    assert data["metric"] == "dungeon"
    assert data["distribution"]["unit"] == "runs"
    assert len(data["distribution"]["rows"]) >= 1


@pytest.mark.live
def test_live_raiderio_threshold_mythic_plus_runs_contract() -> None:
    payload = _payload_for(
        ["threshold", "mythic-plus-runs", "--metric", "score", "--value", "560", "--pages", "1", "--limit", "20", "--nearest", "5"]
    )
    data = payload["data"]

    assert payload["kind"] == "mythic_plus_runs_threshold"
    assert data["metric"] == "score"
    assert data["threshold"]["nearest_match_count"] >= 1
    assert data["threshold"]["estimate"]["metric"] == "mythic_level"


@pytest.mark.live
def test_live_raiderio_filtered_sample_contract() -> None:
    payload = _payload_for(["sample", "mythic-plus-runs", "--pages", "1", "--limit",
                            "20", "--level-min", "20", "--contains-role", "healer"])
    data = payload["data"]

    assert payload["kind"] == "mythic_plus_runs_sample"
    assert payload["query"]["filters"]["level_min"] == 20
    assert payload["query"]["filters"]["contains_role"] == ["healer"]
    assert data["sample"]["filtering"]["source_run_count"] >= data["sample"]["filtering"]["returned_run_count"]


@pytest.mark.live
def test_live_raiderio_sample_mythic_plus_players_contract() -> None:
    payload = _payload_for(["sample", "mythic-plus-players", "--pages", "1", "--limit", "20", "--player-limit", "25"])
    data = payload["data"]

    assert payload["kind"] == "mythic_plus_players_sample"
    assert data["sample"]["run_count"] >= 1
    assert data["sample"]["player_count"] >= 1
    assert data["sample"]["player_sampling"]["source_player_count"] >= data["sample"]["player_sampling"]["returned_player_count"]
    assert len(data["players"]) >= 1


@pytest.mark.live
def test_live_raiderio_distribution_mythic_plus_players_contract() -> None:
    payload = _payload_for(["distribution", "mythic-plus-players", "--metric", "class", "--pages", "1", "--limit", "20"])
    data = payload["data"]

    assert payload["kind"] == "mythic_plus_players_distribution"
    assert data["metric"] == "class"
    assert data["distribution"]["unit"] == "player_class_tags"
    assert len(data["distribution"]["rows"]) >= 1


def _served_season_slug(data: dict[str, object]) -> str:
    """Recover the concrete season slug Raider.IO actually served for a leaderboard payload.

    ``--season current`` omits the season param so the API picks the season; the slug is only
    knowable from the response, where every run carries it and the citation URL embeds it.
    """
    runs = data["runs"]
    assert isinstance(runs, list) and runs
    first_run = runs[0]
    assert isinstance(first_run, dict)
    slug = first_run["season"]
    assert isinstance(slug, str) and slug.startswith("season-"), slug
    citations = data["citations"]
    assert isinstance(citations, dict)
    urls = citations["leaderboard_urls"]
    assert isinstance(urls, list)
    assert any(slug in url for url in urls), (slug, urls)
    return slug


@pytest.mark.live
def test_live_raiderio_raid_leaderboard_scopes_to_the_realm_and_cites_a_real_page() -> None:
    """``--realm`` must narrow the rows, and the citation URL must name a page that exists.

    The rankings URL is a layout this CLI invents, so it is fetched rather than re-derived from the
    same f-string: raider.io answers an unknown raid path with HTTP 400, so a 200 is evidence the
    path layout is real. Its ``?realm=`` query form stays unverified on purpose -- the page is
    client-rendered (the HTML holds no realm at all) and answers 200 for any realm value, so no
    fetch can tell a real query parameter from an invented one. What the realm scope does to the
    data is checked instead, against the rows the API returned.
    """
    raid_slug = _data_for(["raids"])["rows"][0]["slug"]
    data = _data_for(["leaderboard", "raids", "--raid", raid_slug, "--region", "us", "--realm", "malganis", "--limit", "5"])
    citation = data["citations"]["leaderboard_urls"][0]

    assert data["rows"], "an empty page proves nothing about the realm scope"
    assert {row["guild"]["realm"] for row in data["rows"]} == {"malganis"}
    assert citation == f"https://raider.io/{raid_slug}/rankings/us/mythic?realm=malganis"
    with httpx.Client(timeout=30.0, follow_redirects=True) as client:
        assert client.get(citation).status_code == 200, citation
        bogus = client.get("https://raider.io/no-such-raid-zzz/rankings/us/mythic")
    assert bogus.status_code != 200, "an unknown raid must not answer 200, or the check above proves nothing"


@pytest.mark.live
def test_live_raiderio_leaderboard_mythic_plus_contract() -> None:
    payload = _payload_for(["leaderboard", "mythic-plus", "--season", "current", "--region", "us", "--dungeon", "all", "--limit", "20"])
    data = payload["data"]

    assert payload["kind"] == "mythic_plus_leaderboard"
    assert data["count"] >= 1
    assert len(data["runs"]) >= 1
    assert data["freshness"]["sampled_at"]
    assert data["freshness"]["cache_ttl_seconds"] >= 1
    assert len(data["citations"]["leaderboard_urls"]) >= 1

    # ``query.resolved_season`` is intentionally not asserted for --season current: the CLI reads a
    # top-level "season" key that the API does not send (the slug lives in params.season), so the
    # alias reports null. Exercise the resolution contract against the slug the API just served.
    season_slug = _served_season_slug(data)
    explicit = _payload_for(["leaderboard", "mythic-plus", "--season", season_slug, "--region", "us", "--dungeon", "all", "--limit", "20"])
    explicit_data = explicit["data"]

    assert explicit["kind"] == "mythic_plus_leaderboard"
    assert explicit["query"]["season"] == season_slug
    assert explicit["query"]["resolved_season"] == season_slug
    assert explicit_data["count"] >= 1
    assert all(run["season"] == season_slug for run in explicit_data["runs"])
    assert all(season_slug in url for url in explicit_data["citations"]["leaderboard_urls"])


@pytest.mark.live
def test_live_raiderio_guild_reports_when_the_profile_came_off_the_wire() -> None:
    """A guild profile carries the same freshness block as every other payload with provenance.

    The live suite runs with the cache off, so this read is a real fetch: ``cache_hit`` is false and
    ``fetched_at`` is this moment. Replay behaviour is pinned offline, where the cache can be forced.
    """
    payload = _payload_for(["guild", "us", "malganis", "gn"])
    freshness = payload["provenance"]["freshness"]

    assert freshness == payload["data"]["freshness"]
    assert freshness["cache_hit"] is False
    assert freshness["cache_ttl_seconds"] >= 1
    fetched_at = datetime.fromisoformat(freshness["fetched_at"])
    assert timedelta(0) <= datetime.now(UTC) - fetched_at < timedelta(minutes=5), freshness

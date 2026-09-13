"""End-to-end journeys for the ``raiderio`` binary against the live Raider.IO API.

Every command in docs/reference/raiderio.md is exercised here. The Mythic+ season is discovered
from the leaderboard payload instead of pinned, so the suite cannot rot when Raider.IO rolls a
season; only the maintainer's guild/character identity comes from tests/e2e/pins.py.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.e2e import pins
from tests.e2e.harness import (
    EXIT_NETWORK,
    EXIT_NOT_FOUND,
    EXIT_USAGE,
    Result,
    dead_proxy_env,
    payload_or_legacy,
    run,
    run_raw,
    run_retrying,
)

REGION = pins.GUILD_REGION
REALM = pins.GUILD_REALM
GUILD = pins.GUILD_NAME
CHARACTER = pins.CHARACTER_NAME

# One live sampling scope for every analytics journey: small, US-only, one page.
SCOPE = ("--region", "us", "--pages", "1", "--limit", "20")


@pytest.fixture(autouse=True)
def _require_raiderio(require) -> None:
    require("raiderio")


@pytest.fixture(scope="module")
def current_season() -> str:
    """The season Raider.IO is serving right now, read back from the leaderboard payload."""
    # The leaderboard endpoint occasionally exceeds the client's read timeout; retry only that.
    result = run_retrying("raiderio", "leaderboard", "mythic-plus", "--season", "current", "--region", "us", "--limit", "5")
    season = result.payload["query"]["resolved_season"]
    assert isinstance(season, str) and season, f"leaderboard must resolve 'current' to a concrete season\n{result.describe()}"
    return season


def _cache_entries(cache_root: Path) -> set[Path]:
    provider_cache = cache_root / "warcraft" / "raiderio" / "http"
    return set(provider_cache.rglob("*")) if provider_cache.exists() else set()


def _rows(result: Result, key: str) -> list[dict[str, Any]]:
    rows = payload_or_legacy(result, key)
    assert isinstance(rows, list) and rows, f"{key} must be a non-empty list\n{result.describe()}"
    return rows


def _assert_sampled_provenance(result: Result) -> None:
    """Sampled payloads must carry the freshness and citations the analytics rules require."""
    freshness = payload_or_legacy(result, "freshness")
    citations = payload_or_legacy(result, "citations")
    assert isinstance(freshness, dict) and freshness.get("sampled_at"), result.describe()
    assert isinstance(freshness.get("cache_ttl_seconds"), int) and freshness["cache_ttl_seconds"] >= 1, result.describe()
    assert isinstance(citations, dict), result.describe()
    urls = citations.get("leaderboard_urls")
    assert isinstance(urls, list) and urls and all(url.startswith("https://raider.io/") for url in urls), result.describe()
    assert result.payload["provenance"]["citations"] == citations, result.describe()


def test_doctor_reports_ready_capabilities_and_the_isolated_cache(cache_root: Path) -> None:
    result = run("raiderio", "doctor")

    assert payload_or_legacy(result, "status") == "ready"
    assert payload_or_legacy(result, "installed") is True
    assert payload_or_legacy(result, "auth")["required"] is False
    capabilities = payload_or_legacy(result, "capabilities")
    assert {"search", "resolve", "character", "guild", "mythic_plus_leaderboard"} <= set(capabilities)
    assert set(capabilities.values()) == {"ready"}, result.describe()
    cache = payload_or_legacy(result, "cache")
    assert cache["enabled"] is True
    assert Path(cache["cache_dir"]) == cache_root / "warcraft" / "raiderio" / "http", "doctor must report the session cache root"


def test_search_ranks_the_pinned_guild_from_a_structured_probe() -> None:
    result = run("raiderio", "search", f"guild {REGION} {REALM} {GUILD}", "--limit", "5")

    rows = _rows(result, "results")
    top = rows[0]
    assert top["kind"] == "guild"
    assert top["name"] == GUILD
    assert top["region"] == REGION
    assert "mal" in str(top["realm"]).lower() and "ganis" in str(top["realm"]).lower()
    assert top["profile_url"].startswith("https://raider.io/guilds/")
    assert "structured_probe" in top["ranking"]["match_reasons"]
    assert top["follow_up"]["command"] == f"raiderio guild {REGION} {REALM} {GUILD}"


def test_search_kind_filter_narrows_to_characters() -> None:
    result = run("raiderio", "search", f"{REGION} {REALM} {CHARACTER}", "--kind", "character", "--limit", "5")

    rows = _rows(result, "results")
    assert all(row["kind"] == "character" for row in rows), result.describe()
    assert rows[0]["name"] == CHARACTER


def test_resolve_returns_one_follow_up_command_for_each_pinned_identity() -> None:
    for kind, name, surface in (("guild", GUILD, "guild"), ("character", CHARACTER, "character")):
        result = run("raiderio", "resolve", f"{kind} {REGION} {REALM} {name}", "--limit", "5")

        assert payload_or_legacy(result, "resolved") is True, result.describe()
        assert payload_or_legacy(result, "confidence") == "high"
        assert payload_or_legacy(result, "next_command") == f"raiderio {surface} {REGION} {REALM} {name}"
        match = payload_or_legacy(result, "match")
        assert match["kind"] == kind
        assert "structured_probe" in match["ranking"]["match_reasons"]
        assert "realm_match" in match["ranking"]["match_reasons"], "the pinned realm slug must be credited"


def test_resolve_stays_unresolved_for_a_nonsense_query() -> None:
    result = run("raiderio", "resolve", "zzqqxx nonsense query 8471")

    assert payload_or_legacy(result, "resolved") is False
    assert payload_or_legacy(result, "next_command") is None
    assert payload_or_legacy(result, "fallback_search_command").startswith("raiderio search ")


def test_character_profile_carries_identity_score_and_normalized_class_spec() -> None:
    result = run("raiderio", "character", REGION, REALM, CHARACTER)

    character = payload_or_legacy(result, "character")
    assert character["name"] == CHARACTER
    assert character["region"] == REGION
    assert character["realm"].lower().replace("'", "") == REALM
    assert character["class_name"] and character["faction"]
    identity = character["class_spec_identity"]
    assert identity["confidence"] == "high"
    assert identity["identity"]["actor_class"] == character["class_name"].lower()

    mythic_plus = payload_or_legacy(result, "mythic_plus")
    assert isinstance(mythic_plus["current_score"], (int, float))
    assert isinstance(mythic_plus["ranks"]["overall"]["world"], int)
    assert result.payload["provenance"]["citations"]["profile"].startswith("https://raider.io/characters/")


def test_guild_profile_echoes_the_guild_and_numeric_raid_rankings(cache_root: Path) -> None:
    result = run("raiderio", "guild", REGION, REALM, GUILD)

    guild = payload_or_legacy(result, "guild")
    assert guild["name"] == GUILD
    assert guild["region"] == REGION
    assert guild["realm"].lower().replace("'", "") == REALM
    assert isinstance(guild["member_count"], int) and guild["member_count"] > 0

    raiding = payload_or_legacy(result, "raiding")
    assert raiding["raid_count"] >= 1
    progression = raiding["progression"]
    assert progression and all(row["summary"] for row in progression)
    assert all(isinstance(row["total_bosses"], int) for row in progression)
    rankings = raiding["rankings"]
    assert rankings and all(isinstance(row["mythic"]["world"], int) for row in rankings)
    assert result.payload["provenance"]["citations"]["profile"].startswith("https://raider.io/guilds/")
    assert any("guild_profile" in str(path) for path in _cache_entries(cache_root)), "the session cache root must hold the fetched profile"


def test_a_repeated_guild_fetch_is_served_from_the_disk_cache(tmp_path: Path) -> None:
    # Raider.IO payloads expose no cache-hit flag, so the disk cache is the observable signal:
    # a cold private cache dir must gain entries on the first call and none on an identical second.
    private_cache = tmp_path / "raiderio-cache"
    env = {"RAIDERIO_CACHE_DIR": str(private_cache)}
    assert not private_cache.exists()

    first = run("raiderio", "guild", REGION, REALM, GUILD, env=env)
    warmed = set(private_cache.rglob("*.json"))
    assert warmed, "the first fetch must write the guild profile to the configured cache dir"

    repeat = run("raiderio", "guild", REGION, REALM, GUILD, env=env)
    assert set(private_cache.rglob("*.json")) == warmed, "a cache hit must not add cache entries"
    assert repeat.data == first.data, "a cache hit must return the same payload"


def test_mythic_plus_runs_echoes_the_resolved_season_and_full_rows(current_season: str) -> None:
    result = run("raiderio", "mythic-plus-runs", "--region", "us", "--dungeon", "all", "--page", "0")

    query = result.payload["query"]
    assert query["resolved_season"] == current_season, "the page must report the season it actually read"
    assert query["region"] == "us"
    runs = _rows(result, "runs")
    assert payload_or_legacy(result, "count") == len(runs)
    top = runs[0]
    assert top["rank"] == 1
    assert isinstance(top["score"], (int, float)) and top["score"] > 0
    assert isinstance(top["mythic_level"], int) and top["mythic_level"] > 0
    assert top["dungeon"] and top["affixes"]
    assert len(top["roster"]) == 5


def test_sample_mythic_plus_runs_reports_sampling_filtering_and_provenance(current_season: str) -> None:
    result = run("raiderio", "sample", "mythic-plus-runs", *SCOPE, "--level-min", "2", "--contains-role", "healer")

    query = result.payload["query"]
    assert query["resolved_season"] == current_season
    assert query["filters"]["level_min"] == 2
    assert query["filters"]["contains_role"] == ["healer"]

    sample = payload_or_legacy(result, "sample")
    assert sample["season"] == current_season
    assert sample["pages_requested"] == 1 and sample["pages_fetched"] >= 1
    assert sample["run_count"] >= 1
    assert sample["roster_entry_count"] == sample["run_count"] * 5
    filtering = sample["filtering"]
    assert filtering["source_run_count"] == filtering["returned_run_count"] + filtering["excluded_run_count"]
    assert sample["mythic_level"]["min"] >= 2
    assert any(row["value"] == "healer" for row in sample["role_counts"])
    assert _rows(result, "runs")
    _assert_sampled_provenance(result)


def test_sample_mythic_plus_players_dedupes_roster_entries_into_snapshots() -> None:
    result = run("raiderio", "sample", "mythic-plus-players", *SCOPE, "--player-limit", "25")

    sample = payload_or_legacy(result, "sample")
    assert sample["run_count"] >= 1
    player_sampling = sample["player_sampling"]
    assert player_sampling["source_player_count"] >= player_sampling["returned_player_count"]
    assert player_sampling["source_player_count"] == player_sampling["returned_player_count"] + player_sampling["excluded_player_count"]

    players = _rows(result, "players")
    assert len(players) == player_sampling["returned_player_count"]
    top = players[0]
    assert top["name"] and top["realm"] and top["region"]
    assert isinstance(top["appearance_count"], int) and top["appearance_count"] >= 1
    assert isinstance(top["top_mythic_level"], int)
    assert top["profile_url"].startswith("https://raider.io/characters/")
    _assert_sampled_provenance(result)


@pytest.mark.parametrize("metric", ["mythic_level", "dungeon", "role", "player_region", "class", "spec"])
def test_distribution_mythic_plus_runs_covers_every_documented_metric(metric: str) -> None:
    result = run("raiderio", "distribution", "mythic-plus-runs", "--metric", metric, *SCOPE)

    assert payload_or_legacy(result, "metric") == metric
    distribution = payload_or_legacy(result, "distribution")
    assert distribution["unit"]
    rows = distribution["rows"]
    assert rows and all(isinstance(row["count"], int) and row["count"] >= 1 for row in rows)
    assert abs(sum(row["percent"] for row in rows) - 100.0) < 1.0, result.describe()
    _assert_sampled_provenance(result)


@pytest.mark.parametrize("metric", ["appearance_count", "top_mythic_level", "class", "spec", "role", "player_region"])
def test_distribution_mythic_plus_players_covers_every_documented_metric(metric: str) -> None:
    result = run("raiderio", "distribution", "mythic-plus-players", "--metric", metric, *SCOPE, "--player-limit", "25")

    assert payload_or_legacy(result, "metric") == metric
    distribution = payload_or_legacy(result, "distribution")
    assert distribution["unit"]
    assert distribution["rows"], result.describe()
    _assert_sampled_provenance(result)


@pytest.mark.parametrize("metric", ["score", "mythic_level"])
def test_threshold_mythic_plus_runs_estimates_around_a_target(metric: str) -> None:
    sample = run("raiderio", "sample", "mythic-plus-runs", *SCOPE)
    runs = _rows(sample, "runs")
    target = runs[len(runs) // 2][metric]

    result = run("raiderio", "threshold", "mythic-plus-runs", "--metric", metric, "--value", str(target), *SCOPE, "--nearest", "5")

    assert payload_or_legacy(result, "metric") == metric
    assert payload_or_legacy(result, "target") == pytest.approx(float(target))
    threshold = payload_or_legacy(result, "threshold")
    assert 1 <= threshold["nearest_match_count"] <= 5
    assert len(threshold["nearest_matches"]) == threshold["nearest_match_count"]
    assert threshold["nearest_matches"][0]["distance"] == pytest.approx(0.0), "an observed value must have a zero-distance neighbour"
    assert threshold["estimate"] is not None
    _assert_sampled_provenance(result)


def test_leaderboard_mythic_plus_reports_returned_versus_requested(current_season: str) -> None:
    result = run("raiderio", "leaderboard", "mythic-plus", "--season", current_season, "--region", "us", "--dungeon", "all", "--limit", "25")

    query = result.payload["query"]
    assert query["resolved_season"] == current_season
    assert query["limit"] == 25
    sample = payload_or_legacy(result, "sample")
    assert sample["requested_limit"] == 25
    assert sample["returned_run_count"] == payload_or_legacy(result, "count")
    assert sample["pages_fetched"] >= 2, "25 rows needs more than one 20-row ranking page"
    assert sample["limit_reached"] is True

    runs = _rows(result, "runs")
    assert len(runs) == 25
    assert [row["rank"] for row in runs] == sorted(row["rank"] for row in runs)
    assert all(current_season in url for url in payload_or_legacy(result, "citations")["leaderboard_urls"])
    _assert_sampled_provenance(result)


def test_global_output_flags_shape_the_payload() -> None:
    fields = run_raw("raiderio", "--fields", "data.results", "--fields-strict", "search", f"guild {REGION} {REALM} {GUILD}")
    assert fields.exit_code == 0, fields.describe()
    assert set(fields.payload) == {"data"}, fields.describe()
    assert fields.payload["data"]["results"][0]["name"] == GUILD

    full = run("raiderio", "character", REGION, REALM, CHARACTER)
    compact = run("raiderio", "--compact", "--compact-max-chars", "40", "character", REGION, REALM, CHARACTER)
    thumbnail = compact.data["character"]["thumbnail_url"]
    assert len(full.data["character"]["thumbnail_url"]) > 40, "the uncompacted field must be long enough to truncate"
    assert thumbnail.endswith("...") and len(thumbnail) == 40, compact.describe()

    human = run("raiderio", "--profile", "human", "doctor")
    assert human.stdout.startswith("{\n"), "the human profile must pretty-print"

    debug = run("raiderio", "--profile", "debug", "doctor")
    assert debug.stdout.startswith("{\n")
    assert payload_or_legacy(debug, "status") == "ready"


def test_fields_strict_rejects_a_missing_path() -> None:
    result = run("raiderio", "--fields", "data.no_such_key", "--fields-strict", "doctor", expect=EXIT_USAGE, error_code="missing_fields")
    assert result.payload["error"]["details"]["missing_fields"] == ["data.no_such_key"]


def test_unknown_guild_and_character_are_not_found() -> None:
    for command, name in (("guild", "zzzznotarealguildzzzz"), ("character", "Zzzznotarealcharzzz")):
        result = run("raiderio", command, REGION, REALM, name, expect=EXIT_NOT_FOUND, error_code="not_found")
        assert result.payload["error"]["details"]["status_code"] == 400
        assert "could not find" in result.payload["error"]["message"].lower()


def test_malformed_request_is_a_usage_error() -> None:
    result = run("raiderio", "guild", "zz", REALM, GUILD, expect=EXIT_USAGE, error_code="invalid_query")
    assert result.payload["error"]["details"]["status_code"] == 400


@pytest.mark.parametrize(
    "args",
    [
        ("search", "liquid", "--kind", "bogus"),
        ("resolve", "liquid", "--kind", "bogus"),
        ("distribution", "mythic-plus-runs", "--metric", "bogus"),
        ("distribution", "mythic-plus-players", "--metric", "bogus"),
        ("threshold", "mythic-plus-runs", "--metric", "bogus", "--value", "100"),
    ],
)
def test_invalid_kind_or_metric_is_a_usage_error(args: tuple[str, ...]) -> None:
    run("raiderio", *args, expect=EXIT_USAGE, error_code="invalid_query")


def test_network_failure_is_an_exit_5_envelope(cache_root: Path) -> None:
    del cache_root  # the dead-proxy run needs a cold cache, so it uses its own root below
    env = {**dead_proxy_env(), "RAIDERIO_CACHE_BACKEND": "none"}
    result = run("raiderio", "guild", REGION, REALM, GUILD, expect=EXIT_NETWORK, env=env)
    assert result.error_code in {"network_error", "timeout"}, result.describe()
    assert result.stdout == ""

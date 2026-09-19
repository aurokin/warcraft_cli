"""End-to-end journeys for the ``raiderio`` binary against the live Raider.IO API.

Every command in docs/reference/raiderio.md is exercised here. The Mythic+ season is discovered
from the leaderboard payload and the raid slug from ``raiderio raids`` instead of pinned, so the
suite cannot rot when Raider.IO rolls a season or tier; only the maintainer's guild/character
identity comes from tests/e2e/pins.py. The global output flags are not re-checked here: the
cross-binary contract in tests/e2e/test_contract.py holds every binary to them.

Two things this file goes out of its way to make falsifiable. The rankings citation URL is a
layout this CLI invents, so it is fetched rather than compared against the f-string that built it.
And every result-narrowing flag (``--realm``, ``--page``, ``--affixes``, the sampled bounds) is
checked against the unnarrowed call, so a flag that quietly stopped being wired cannot stay green.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import pytest

from tests.e2e import pins
from tests.e2e.harness import (
    EXIT_NETWORK,
    EXIT_NOT_FOUND,
    EXIT_USAGE,
    Result,
    dead_proxy_env,
    run,
    run_retrying,
)

REGION = pins.GUILD_REGION
REALM = pins.GUILD_REALM
GUILD = pins.GUILD_NAME
CHARACTER = pins.CHARACTER_NAME

# One live sampling scope for every analytics journey: small, US-only, one page.
SCOPE = ("--region", "us", "--pages", "1", "--limit", "20")
# Raider.IO serves 20 ranking rows per page, so --page is only observable at that granularity.
RANKING_PAGE_SIZE = 20


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


@pytest.fixture(scope="module")
def current_raid() -> str:
    """The first catalogued raid of the current expansion that already has mythic rankings."""
    catalog = run("raiderio", "raids")
    slugs = [row["slug"] for row in _rows(catalog, "rows")]
    for slug in slugs:
        result = run_retrying("raiderio", "leaderboard", "raids", "--raid", slug, "--difficulty", "mythic", "--region", "us", "--limit", "1")
        if result.data["count"] >= 1:
            return slug
    raise AssertionError(f"no catalogued raid has US mythic rankings yet: {slugs}\n{catalog.describe()}")


def _cache_entries(cache_root: Path) -> set[Path]:
    provider_cache = cache_root / "warcraft" / "raiderio" / "http"
    return set(provider_cache.rglob("*")) if provider_cache.exists() else set()


def _rows(result: Result, key: str) -> list[dict[str, Any]]:
    rows = result.data.get(key)
    assert isinstance(rows, list) and rows, f"{key} must be a non-empty list\n{result.describe()}"
    return rows


def _run_keys(result: Result) -> list[tuple[Any, ...]]:
    """One identity per sampled run, so a filtered call can be compared with the unfiltered one."""
    return [(row["dungeon_slug"], row["completed_at"], row["score"]) for row in _rows(result, "runs")]


def _assert_freshness(result: Result) -> dict[str, Any]:
    """Every payload with provenance reports when its data came off the wire and whether it was replayed."""
    freshness = result.data["freshness"]
    assert isinstance(freshness["fetched_at"], str) and freshness["fetched_at"], result.describe()
    assert isinstance(freshness["cache_hit"], bool), result.describe()
    assert isinstance(freshness["cache_ttl_seconds"], int) and freshness["cache_ttl_seconds"] >= 1, result.describe()
    assert result.payload["provenance"]["freshness"] == freshness, result.describe()
    return freshness


def _assert_sampled_provenance(result: Result) -> None:
    """Sampled payloads add the assembly time on top of the fetch time, plus leaderboard citations."""
    freshness = _assert_freshness(result)
    assert freshness["sampled_at"] >= freshness["fetched_at"], "a sample cannot predate the page it read"
    citations = result.data["citations"]
    urls = citations["leaderboard_urls"]
    assert urls and all(url.startswith("https://raider.io/") for url in urls), result.describe()
    assert result.payload["provenance"]["citations"] == citations, result.describe()


def test_doctor_reports_ready_capabilities_and_the_isolated_cache(cache_root: Path) -> None:
    result = run("raiderio", "doctor")

    assert result.data["status"] == "ready"
    assert result.data["installed"] is True
    assert result.data["auth"]["required"] is False
    capabilities = result.data["capabilities"]
    assert {"search", "resolve", "character", "guild", "mythic_plus_leaderboard", "raid_leaderboard", "raid_catalog"} <= set(capabilities)
    assert set(capabilities.values()) == {"ready"}, result.describe()
    cache = result.data["cache"]
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


def test_resolve_hands_over_a_next_command_that_returns_the_same_entity() -> None:
    for kind, name, surface in (("guild", GUILD, "guild"), ("character", CHARACTER, "character")):
        result = run("raiderio", "resolve", f"{kind} {REGION} {REALM} {name}", "--limit", "5")

        assert result.data["resolved"] is True, result.describe()
        assert result.data["confidence"] == "high"
        match = result.data["match"]
        assert match["kind"] == kind
        assert "structured_probe" in match["ranking"]["match_reasons"]
        assert "realm_match" in match["ranking"]["match_reasons"], "the pinned realm slug must be credited"

        # An agent runs next_command verbatim, so it has to resolve to the entity that was matched.
        next_command = result.data["next_command"]
        assert next_command == f"raiderio {surface} {REGION} {REALM} {name}"
        binary, *args = next_command.split()
        assert binary == "raiderio"
        assert run("raiderio", *args).data[surface]["name"] == name


def test_resolve_stays_unresolved_for_a_nonsense_query() -> None:
    result = run("raiderio", "resolve", "zzqqxx nonsense query 8471")

    assert result.data["resolved"] is False
    assert result.data["next_command"] is None
    assert result.data["fallback_search_command"].startswith("raiderio search ")


def test_character_profile_carries_identity_score_and_normalized_class_spec() -> None:
    result = run("raiderio", "character", REGION, REALM, CHARACTER)

    character = result.data["character"]
    assert character["name"] == CHARACTER
    assert character["region"] == REGION
    assert character["realm"].lower().replace("'", "") == REALM
    assert character["class_name"] and character["faction"]
    identity = character["class_spec_identity"]
    assert identity["confidence"] == "high"
    assert identity["identity"]["actor_class"] == character["class_name"].lower()

    mythic_plus = result.data["mythic_plus"]
    assert isinstance(mythic_plus["current_score"], (int, float))
    assert isinstance(mythic_plus["ranks"]["overall"]["world"], int)
    assert result.payload["provenance"]["citations"]["profile"].startswith("https://raider.io/characters/")


def test_guild_profile_echoes_the_guild_and_numeric_raid_rankings(cache_root: Path) -> None:
    result = run("raiderio", "guild", REGION, REALM, GUILD)

    guild = result.data["guild"]
    assert guild["name"] == GUILD
    assert guild["region"] == REGION
    assert guild["realm"].lower().replace("'", "") == REALM
    assert isinstance(guild["member_count"], int) and guild["member_count"] > 0

    raiding = result.data["raiding"]
    assert raiding["raid_count"] >= 1
    progression = raiding["progression"]
    assert progression and all(row["summary"] for row in progression)
    assert all(isinstance(row["total_bosses"], int) for row in progression)
    rankings = raiding["rankings"]
    assert rankings and all(isinstance(row["mythic"]["world"], int) for row in rankings)
    # The rankings are keyed by the same raid slugs the progression rows use, so the two blocks can
    # be joined; a ranking for a raid the guild has no progression on would mean they cannot.
    assert {row["raid_slug"] for row in rankings} <= {row["raid_slug"] for row in progression}
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
    assert result.data["count"] == len(runs)
    top = runs[0]
    assert top["rank"] == 1
    assert isinstance(top["score"], (int, float)) and top["score"] > 0
    assert isinstance(top["mythic_level"], int) and top["mythic_level"] > 0
    assert top["dungeon"] and top["affixes"]
    assert len(top["roster"]) == 5
    _assert_freshness(result)
    assert result.data["citations"]["leaderboard_urls"], result.describe()


def test_sample_mythic_plus_runs_reports_sampling_filtering_and_provenance(current_season: str) -> None:
    result = run("raiderio", "sample", "mythic-plus-runs", *SCOPE, "--level-min", "2", "--contains-role", "healer")

    query = result.payload["query"]
    assert query["resolved_season"] == current_season
    assert query["filters"]["level_min"] == 2
    assert query["filters"]["contains_role"] == ["healer"]

    sample = result.data["sample"]
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


def test_the_sampled_bounds_return_strict_subsets_that_add_back_up() -> None:
    """``--score-min``/``--score-max``/``--level-max`` narrow the same sample, they do not re-sample."""
    unfiltered = run("raiderio", "sample", "mythic-plus-runs", *SCOPE)
    runs = _rows(unfiltered, "runs")
    everything = set(_run_keys(unfiltered))
    scores = sorted(row["score"] for row in runs)
    midpoint = scores[len(scores) // 2]

    above = run("raiderio", "sample", "mythic-plus-runs", *SCOPE, "--score-min", str(midpoint))
    below = run("raiderio", "sample", "mythic-plus-runs", *SCOPE, "--score-max", str(midpoint))
    above_keys, below_keys = set(_run_keys(above)), set(_run_keys(below))

    assert above_keys < everything, "--score-min must drop the runs below the bound"
    assert below_keys < everything, "--score-max must drop the runs above the bound"
    assert all(row["score"] >= midpoint for row in _rows(above, "runs"))
    assert all(row["score"] <= midpoint for row in _rows(below, "runs"))
    # Inclusive bounds around one observed score: every run is in one half or the other.
    assert above_keys | below_keys == everything, "the two halves must cover the whole sample"
    assert above.data["sample"]["filtering"]["source_run_count"] == len(everything)

    lowest_level = min(row["mythic_level"] for row in runs)
    capped = run("raiderio", "sample", "mythic-plus-runs", *SCOPE, "--level-max", str(lowest_level))
    assert set(_run_keys(capped)) <= everything
    assert all(row["mythic_level"] == lowest_level for row in _rows(capped, "runs"))


def test_the_affixes_scope_changes_both_the_rows_and_the_citation() -> None:
    """``--affixes`` picks a different Raider.IO leaderboard, so it must show up in every row."""
    unfiltered = run("raiderio", "sample", "mythic-plus-runs", *SCOPE)
    affix = sorted({affix for row in _rows(unfiltered, "runs") for affix in row["affixes"]})[0]

    result = run("raiderio", "sample", "mythic-plus-runs", *SCOPE, "--affixes", affix)

    assert result.payload["query"]["affixes"] == affix
    assert all(affix in row["affixes"] for row in _rows(result, "runs")), result.describe()
    urls = result.data["citations"]["leaderboard_urls"]
    assert all(url.endswith(f"/{affix}") for url in urls), urls


def test_sample_mythic_plus_players_dedupes_roster_entries_into_snapshots() -> None:
    result = run("raiderio", "sample", "mythic-plus-players", *SCOPE, "--player-limit", "25")

    sample = result.data["sample"]
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

    assert result.data["metric"] == metric
    distribution = result.data["distribution"]
    assert distribution["unit"]
    rows = distribution["rows"]
    assert rows and all(isinstance(row["count"], int) and row["count"] >= 1 for row in rows)
    assert abs(sum(row["percent"] for row in rows) - 100.0) < 1.0, result.describe()
    _assert_sampled_provenance(result)


@pytest.mark.parametrize("metric", ["appearance_count", "top_mythic_level", "class", "spec", "role", "player_region"])
def test_distribution_mythic_plus_players_covers_every_documented_metric(metric: str) -> None:
    result = run("raiderio", "distribution", "mythic-plus-players", "--metric", metric, *SCOPE, "--player-limit", "25")

    assert result.data["metric"] == metric
    distribution = result.data["distribution"]
    assert distribution["unit"]
    assert distribution["rows"], result.describe()
    _assert_sampled_provenance(result)


@pytest.mark.parametrize("metric", ["score", "mythic_level"])
def test_threshold_mythic_plus_runs_estimates_around_a_target(metric: str) -> None:
    sample = run("raiderio", "sample", "mythic-plus-runs", *SCOPE)
    runs = _rows(sample, "runs")
    target = runs[len(runs) // 2][metric]

    result = run("raiderio", "threshold", "mythic-plus-runs", "--metric", metric, "--value", str(target), *SCOPE, "--nearest", "5")

    assert result.data["metric"] == metric
    assert result.data["target"] == pytest.approx(float(target))
    threshold = result.data["threshold"]
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
    sample = result.data["sample"]
    assert sample["requested_limit"] == 25
    assert sample["returned_run_count"] == result.data["count"]
    assert sample["pages_fetched"] >= 2, "25 rows needs more than one 20-row ranking page"
    assert sample["limit_reached"] is True

    runs = _rows(result, "runs")
    assert len(runs) == 25
    assert [row["rank"] for row in runs] == sorted(row["rank"] for row in runs)
    # The rows themselves have to come from the season that was asked for, not just the citation.
    assert {row["season"] for row in runs} == {current_season}, result.describe()
    assert all(current_season in url for url in result.data["citations"]["leaderboard_urls"])
    _assert_sampled_provenance(result)


def test_raids_catalog_lists_slugs_with_encounters() -> None:
    result = run("raiderio", "raids", "--expansion-id", "11")

    assert result.payload["kind"] == "raid_catalog"
    assert result.payload["query"] == {"expansion_id": 11}
    rows = _rows(result, "rows")
    assert result.data["count"] == len(rows)
    for row in rows:
        assert row["slug"] and row["name"], result.describe()
        assert row["encounters"] and all(encounter["slug"] for encounter in row["encounters"]), result.describe()
    _assert_freshness(result)
    assert result.data["citations"]["static_data_url"].endswith("expansion_id=11")


def test_raid_leaderboard_returns_ranked_guilds_with_profile_urls(current_raid: str) -> None:
    result = run("raiderio", "leaderboard", "raids", "--raid", current_raid, "--difficulty", "mythic", "--region", "us", "--limit", "5")

    assert result.payload["kind"] == "raid_leaderboard"
    assert result.payload["command"] == "leaderboard raids", "nested commands must label themselves fully"
    query = result.payload["query"]
    assert query == {"raid": current_raid, "difficulty": "mythic", "region": "us", "realm": None, "page": 0, "limit": 5}
    sample = result.data["sample"]
    assert sample["requested_limit"] == 5
    assert sample["returned_row_count"] == result.data["count"] == 5
    assert sample["pages_fetched"] == 1 and sample["limit_reached"] is True

    rows = _rows(result, "rows")
    ranks = [row["rank"] for row in rows]
    assert ranks == list(range(1, len(rows) + 1)), "the first page of a region scope is ranks 1..n"
    for row in rows:
        assert isinstance(row["region_rank"], int)
        guild = row["guild"]
        assert guild["name"] and guild["realm"] and guild["region"] == "us"
        assert guild["profile_url"] == f"https://raider.io/guilds/us/{guild['realm']}/{quote(guild['name'])}"
        assert row["encounters_defeated_count"] == len(row["encounters_defeated"]) >= 1
        assert row["encounters_pulled_count"] == len(row["encounters_pulled"])
    _assert_sampled_provenance(result)


def test_the_raid_rankings_citation_resolves_to_a_real_page(current_raid: str) -> None:
    """Fetch the citation instead of re-deriving it: the URL layout is this CLI's own invention.

    Every other check on this URL rebuilds the same f-string, so a wrong layout would ship inside an
    ``ok: true`` envelope that tells an agent to follow it. raider.io answers an unknown raid slug or
    difficulty with HTTP 400 and a wrong path shape with 404, which is what makes a 200 here
    evidence. The page is a client-rendered shell, so its body carries no raid name to match on, and
    the ``?realm=`` query form is not validated server-side -- neither is checked below.
    """
    result = run("raiderio", "leaderboard", "raids", "--raid", current_raid, "--difficulty", "mythic", "--region", "us", "--limit", "1")
    citation = result.data["citations"]["leaderboard_urls"][0]

    with httpx.Client(timeout=30.0, follow_redirects=True) as client:
        served = client.get(citation)
        control = client.get(citation.replace(f"/{current_raid}/", "/no-such-raid-zzz/"))
    assert served.status_code == 200, f"{citation} -> {served.status_code}"
    assert control.status_code != 200, "an unknown raid must not answer 200, or the check above proves nothing"


def test_raid_leaderboard_pages_are_contiguous_and_never_overlap(current_raid: str) -> None:
    """``--page`` walks the ranking; two adjacent pages must join up without repeating a guild."""
    scope = ("leaderboard", "raids", "--raid", current_raid, "--difficulty", "mythic", "--region", "us", "--limit", str(RANKING_PAGE_SIZE))
    first = run("raiderio", *scope, "--page", "0")
    second = run("raiderio", *scope, "--page", "1")

    first_rows, second_rows = _rows(first, "rows"), _rows(second, "rows")
    assert [row["rank"] for row in first_rows] == list(range(1, RANKING_PAGE_SIZE + 1))
    assert [row["rank"] for row in second_rows] == list(range(RANKING_PAGE_SIZE + 1, 2 * RANKING_PAGE_SIZE + 1))
    first_guilds = {row["guild"]["profile_url"] for row in first_rows}
    second_guilds = {row["guild"]["profile_url"] for row in second_rows}
    assert not (first_guilds & second_guilds), "a guild must not appear on two pages"
    assert second.payload["query"]["page"] == 1


def test_raid_leaderboard_realm_narrows_rows_to_that_realm(current_raid: str) -> None:
    scope = ("leaderboard", "raids", "--raid", current_raid, "--difficulty", "mythic", "--region", REGION)
    region_wide = run("raiderio", *scope, "--limit", str(RANKING_PAGE_SIZE))
    result = run("raiderio", *scope, "--realm", REALM, "--limit", "5")

    assert result.payload["query"]["realm"] == REALM
    rows = _rows(result, "rows")
    assert all(row["guild"]["realm"] == REALM for row in rows), result.describe()
    assert [row["rank"] for row in rows] == list(range(1, len(rows) + 1)), "realm-scoped rank must restart at 1"
    assert all(row["region_rank"] >= row["rank"] for row in rows), "a realm rank can never beat the region rank"
    # The realm scope is a slice of the region scope, so a realm guild inside the region's top page
    # has to carry the same region_rank in both answers.
    region_ranks = {row["guild"]["profile_url"]: row["rank"] for row in _rows(region_wide, "rows")}
    shared = [row for row in rows if row["guild"]["profile_url"] in region_ranks]
    assert all(region_ranks[row["guild"]["profile_url"]] == row["region_rank"] for row in shared), result.describe()
    assert result.data["citations"]["leaderboard_urls"][0].endswith(f"?realm={REALM}")


def test_unknown_guild_and_character_are_not_found() -> None:
    for command, name in (("guild", "zzzznotarealguildzzzz"), ("character", "Zzzznotarealcharzzz")):
        result = run("raiderio", command, REGION, REALM, name, expect=EXIT_NOT_FOUND, error_code="not_found")
        assert result.payload["error"]["details"]["status_code"] == 400
        assert "could not find" in result.payload["error"]["message"].lower()


def test_malformed_request_is_a_usage_error() -> None:
    result = run("raiderio", "guild", "zz", REALM, GUILD, expect=EXIT_USAGE, error_code="invalid_query")
    assert result.payload["error"]["details"]["status_code"] == 400


@pytest.mark.parametrize(
    ("args", "command"),
    [
        (("search", "liquid", "--kind", "bogus"), "search"),
        (("resolve", "liquid", "--kind", "bogus"), "resolve"),
        (("distribution", "mythic-plus-runs", "--metric", "bogus"), "distribution mythic-plus-runs"),
        (("distribution", "mythic-plus-players", "--metric", "bogus"), "distribution mythic-plus-players"),
        (("threshold", "mythic-plus-runs", "--metric", "bogus", "--value", "100"), "threshold mythic-plus-runs"),
        (("leaderboard", "raids", "--raid", "sporefall", "--difficulty", "bogus"), "leaderboard raids"),
        (("leaderboard", "raids", "--raid", "sporefall", "--realm", "malganis"), "leaderboard raids"),
    ],
)
def test_invalid_kind_or_metric_is_a_usage_error(args: tuple[str, ...], command: str) -> None:
    result = run("raiderio", *args, expect=EXIT_USAGE, error_code="invalid_query")
    # A failure labels itself with the full sub-path, the same value the success envelope carries,
    # so no two commands answer to the same `command`.
    assert result.payload["command"] == command, result.describe()


def test_network_failure_is_an_exit_5_envelope(cache_root: Path) -> None:
    del cache_root  # the dead-proxy run needs a cold cache, so it uses its own root below
    env = {**dead_proxy_env(), "RAIDERIO_CACHE_BACKEND": "none"}
    result = run("raiderio", "guild", REGION, REALM, GUILD, expect=EXIT_NETWORK, env=env)
    assert result.error_code in {"network_error", "timeout"}, result.describe()
    assert result.stdout == ""

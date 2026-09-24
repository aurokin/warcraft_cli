"""End-to-end journeys for the ``raiderio`` binary against the live Raider.IO API.

Every command in docs/reference/raiderio.md is exercised here. The Mythic+ season is discovered
from the leaderboard payload and the raid slug from ``raiderio raids`` instead of pinned, so the
suite cannot rot when Raider.IO rolls a season or tier; only the maintainer's guild/character
identity comes from tests/e2e/pins.py. The global output flags are not re-checked here: the
cross-binary contract in tests/e2e/test_contract.py holds every binary to them.

Two things this file goes out of its way to make falsifiable. The rankings citation URL is a
layout this CLI invents, so it is fetched rather than compared against the f-string that built it.
And every result-narrowing flag (``--realm``, ``--page``, ``--affixes``, the sampled bounds, the
roster filters) is checked against the unnarrowed call with a bound that has to bite, so a flag
that quietly stopped being wired cannot stay green by returning everything.
"""

from __future__ import annotations

import shlex
from collections import Counter
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
# The playable classes. A class composition can only answer with these and a spec composition never
# can, which is what tells the two compositions apart.
WOW_CLASS_SLUGS = frozenset(
    {
        "death-knight", "demon-hunter", "druid", "evoker", "hunter", "mage", "monk",
        "paladin", "priest", "rogue", "shaman", "warlock", "warrior",
    }
)
# Unit per documented `distribution mythic-plus-runs --metric`: run-level metrics count runs, roster
# metrics count the five roster entries of each run. The unit alone rules out most wrong metrics.
RUN_DISTRIBUTION_UNITS = {
    "mythic_level": "runs",
    "dungeon": "runs",
    "composition": "runs",
    "class_composition": "runs",
    "role": "roster_entries",
    "class": "roster_entries",
    "spec": "roster_entries",
    "player_region": "roster_entries",
}
# Unit per documented `distribution mythic-plus-players --metric`; the tag units count one row per
# class/spec/role a sampled player was seen in, so they cannot be confused with the player metrics.
PLAYER_DISTRIBUTION_UNITS = {
    "appearance_count": "players",
    "top_mythic_level": "players",
    "class": "player_class_tags",
    "spec": "player_spec_tags",
    "role": "player_role_tags",
    "player_region": "players",
}


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
            return str(slug)
    raise AssertionError(f"no catalogued raid has US mythic rankings yet: {slugs}\n{catalog.describe()}")


@pytest.fixture(scope="module")
def baseline_sample() -> Result:
    """The unfiltered sample every narrowing journey is measured against, fetched once."""
    return run("raiderio", "sample", "mythic-plus-runs", *SCOPE)


@pytest.fixture(scope="module")
def player_sample() -> Result:
    """The player snapshots for the baseline scope; they read the same cached leaderboard page."""
    return run("raiderio", "sample", "mythic-plus-players", *SCOPE, "--player-limit", "25")


def _cache_entries(cache_root: Path) -> set[Path]:
    provider_cache = cache_root / "warcraft" / "raiderio" / "http"
    return set(provider_cache.rglob("*")) if provider_cache.exists() else set()


def _rows(result: Result, key: str) -> list[dict[str, Any]]:
    rows = result.data.get(key)
    assert isinstance(rows, list) and rows, f"{key} must be a non-empty list\n{result.describe()}"
    return rows


def _run_key(row: dict[str, Any]) -> tuple[Any, ...]:
    """One identity per sampled run, so a filtered call can be compared with the unfiltered one."""
    return (row["dungeon_slug"], row["completed_at"], row["score"])


def _run_keys(result: Result) -> set[tuple[Any, ...]]:
    return {_run_key(row) for row in _rows(result, "runs")}


def _class_spec(entry: dict[str, Any]) -> str:
    """``priest-holy``: spec slugs repeat across classes (holy, frost, protection, restoration)."""
    return f"{entry['class_slug']}-{entry['spec_slug']}"


def _roster_values(row: dict[str, Any], field: str) -> set[str]:
    return {_class_spec(entry) if field == "class_spec" else entry[field] for entry in row["roster"]}


def _a_value_only_some_runs_carry(runs: list[dict[str, Any]], field: str) -> str:
    """A roster value that is on at least one run and missing from at least one other.

    A filter on such a value has to return a strict, non-empty subset, so a flag that quietly
    stopped being wired cannot produce the expected set by returning everything.
    """
    carriers = Counter(value for row in runs for value in _roster_values(row, field))
    candidates = sorted(value for value, count in carriers.items() if 0 < count < len(runs))
    assert candidates, f"every sampled run carries the same {field}; nothing can narrow the sample"
    return str(candidates[0])


def _counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    return {row["value"]: row["count"] for row in rows}


def _roster_entries_by_player(baseline: Result) -> dict[str, list[dict[str, Any]]]:
    """The baseline roster rows grouped per character, the independent record of each player's runs."""
    entries: dict[str, list[dict[str, Any]]] = {}
    for row in _rows(baseline, "runs"):
        for entry in row["roster"]:
            entries.setdefault(entry["profile_url"], []).append(entry)
    return entries


def _assert_freshness(result: Result) -> dict[str, Any]:
    """Every payload with provenance reports when its data came off the wire and whether it was replayed."""
    freshness: dict[str, Any] = result.data["freshness"]
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
    assert top["follow_up"]["command"] == shlex.join(["raiderio", "guild", REGION, REALM, GUILD])


def test_search_kind_filter_drops_the_other_entity_type() -> None:
    """``--kind`` has to drop the other entity type, whichever way round the query reads.

    Each leg is checked against the unfiltered answer for the same query, so a ``--kind`` that
    stopped being applied cannot look correct by returning what the query would have returned.
    """
    guild_query = f"guild {REGION} {REALM} {GUILD}"
    character_query = f"{REGION} {REALM} {CHARACTER}"

    guilds = run("raiderio", "search", guild_query, "--limit", "5")
    assert any(row["kind"] == "guild" for row in _rows(guilds, "results")), guilds.describe()
    without_guilds = run("raiderio", "search", guild_query, "--kind", "character", "--limit", "5")
    assert not any(row["kind"] == "guild" for row in without_guilds.data["results"]), without_guilds.describe()

    characters = run("raiderio", "search", character_query, "--kind", "character", "--limit", "5")
    rows = _rows(characters, "results")
    assert all(row["kind"] == "character" for row in rows), characters.describe()
    assert rows[0]["name"] == CHARACTER
    # Site search sends no path for characters; the row still has to link the character's page.
    assert rows[0]["profile_url"].startswith(f"https://raider.io/characters/{REGION}/"), characters.describe()
    assert rows[0]["profile_url"].endswith(f"/{CHARACTER}"), characters.describe()
    without_characters = run("raiderio", "search", character_query, "--kind", "guild", "--limit", "5")
    assert not any(row["kind"] == "character" for row in without_characters.data["results"]), without_characters.describe()


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
        assert next_command == shlex.join(["raiderio", surface, REGION, REALM, name])
        binary, *args = shlex.split(next_command)
        assert binary == "raiderio"
        assert run("raiderio", *args).data[surface]["name"] == name


def test_a_multi_word_guild_name_hands_over_a_command_that_runs(current_raid: str) -> None:
    """Most guild names have a space, so the handed-over command has to be shell-quoted to run.

    The guild is discovered from the raid leaderboard, which is also the oracle for its name and
    realm. An unquoted ``raiderio guild us <realm> Two Words`` fails with "unexpected extra argument".
    """
    leaderboard = run(
        "raiderio", "leaderboard", "raids", "--raid", current_raid, "--difficulty", "mythic", "--region", "us",
        "--limit", str(RANKING_PAGE_SIZE),
    )
    guild = next((row["guild"] for row in _rows(leaderboard, "rows") if " " in row["guild"]["name"]), None)
    assert guild is not None, f"no multi-word guild name on the first leaderboard page\n{leaderboard.describe()}"

    result = run("raiderio", "resolve", f"guild us {guild['realm']} {guild['name']}", "--kind", "guild")
    assert result.data["resolved"] is True, result.describe()
    binary, *args = shlex.split(result.data["next_command"])
    assert (binary, len(args)) == ("raiderio", 4), result.describe()
    profile = run("raiderio", *args)
    assert (profile.data["guild"]["name"], profile.data["guild"]["region"]) == (guild["name"], "us"), profile.describe()


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
    _assert_freshness(result)
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
    _assert_freshness(result)
    assert result.payload["provenance"]["citations"]["profile"].startswith("https://raider.io/guilds/")
    assert any("guild_profile" in str(path) for path in _cache_entries(cache_root)), "the session cache root must hold the fetched profile"


@pytest.mark.parametrize(("command", "name"), [("guild", GUILD), ("character", CHARACTER)])
def test_a_repeated_profile_fetch_is_replayed_and_says_so(tmp_path: Path, command: str, name: str) -> None:
    """A replay has to report itself and quote the age of what it replayed, not the time it ran.

    ``cache_hit`` is the only way a caller can tell a fresh answer from a stale one, so it is
    observed flipping against a private, provably cold cache directory: a payload that always
    reported ``false``, or that restamped ``fetched_at`` on the replay, would be lying about age.
    """
    private_cache = tmp_path / "raiderio-cache"
    env = {"RAIDERIO_CACHE_DIR": str(private_cache)}
    assert not private_cache.exists()

    first = run("raiderio", command, REGION, REALM, name, env=env)
    warmed = set(private_cache.rglob("*.json"))
    assert warmed, f"the first fetch must write the {command} profile to the configured cache dir"
    cold = _assert_freshness(first)
    assert cold["cache_hit"] is False, "a cold cache cannot report a hit"

    repeat = run("raiderio", command, REGION, REALM, name, env=env)
    assert set(private_cache.rglob("*.json")) == warmed, "a cache hit must not add cache entries"
    replayed = _assert_freshness(repeat)
    assert replayed["cache_hit"] is True, "the second read must report that it was replayed"
    assert replayed["fetched_at"] == cold["fetched_at"], "a replay must report when the data was fetched"
    # Everything but the freshness block, which is the one part that is allowed to differ.
    assert {key: value for key, value in repeat.data.items() if key != "freshness"} == {
        key: value for key, value in first.data.items() if key != "freshness"
    }, "a cache hit must replay the same answer"


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


def test_sample_mythic_plus_runs_reports_what_it_read(current_season: str, baseline_sample: Result) -> None:
    """The summary is recomputed from the rows in the same envelope, so it cannot describe something else."""
    result = baseline_sample
    assert result.payload["query"]["resolved_season"] == current_season

    runs = _rows(result, "runs")
    roster = [entry for row in runs for entry in row["roster"]]
    sample = result.data["sample"]
    assert sample["season"] == current_season
    assert sample["pages_requested"] == 1 and sample["pages_fetched"] >= 1
    assert sample["run_count"] == len(runs)
    assert sample["roster_entry_count"] == len(roster) == sample["run_count"] * 5
    assert sample["unique_dungeons"] == sorted({row["dungeon"] for row in runs})
    levels = [row["mythic_level"] for row in runs]
    assert (sample["mythic_level"]["min"], sample["mythic_level"]["max"]) == (min(levels), max(levels))
    assert _counts(sample["role_counts"]) == dict(Counter(entry["role"] for entry in roster))
    assert _counts(sample["player_region_counts"]) == dict(Counter(entry["region"] for entry in roster))

    # Nothing was asked for, so nothing may be dropped: the filtering block has to say so.
    filtering = sample["filtering"]
    assert filtering["source_run_count"] == filtering["returned_run_count"] == len(runs)
    assert filtering["excluded_run_count"] == 0
    _assert_sampled_provenance(result)


def test_the_sampled_bounds_return_strict_subsets_that_add_back_up(baseline_sample: Result) -> None:
    """``--score-min``/``--score-max``/``--level-min``/``--level-max`` narrow the same sample.

    Each bound is placed where it has to bite whatever the live leaderboard looks like: the score
    halves split the sample around an observed score, and the level bounds are set one step outside
    the observed range, which no sample can satisfy. A flag that stopped being wired returns the
    whole sample and fails every one of them.
    """
    runs = _rows(baseline_sample, "runs")
    everything = _run_keys(baseline_sample)
    scores = sorted(row["score"] for row in runs)
    midpoint = scores[len(scores) // 2]

    above = run("raiderio", "sample", "mythic-plus-runs", *SCOPE, "--score-min", str(midpoint))
    below = run("raiderio", "sample", "mythic-plus-runs", *SCOPE, "--score-max", str(midpoint))
    above_keys, below_keys = _run_keys(above), _run_keys(below)

    assert above_keys < everything, "--score-min must drop the runs below the bound"
    assert below_keys < everything, "--score-max must drop the runs above the bound"
    assert all(row["score"] >= midpoint for row in _rows(above, "runs"))
    assert all(row["score"] <= midpoint for row in _rows(below, "runs"))
    # Inclusive bounds around one observed score: every run is in one half or the other.
    assert above_keys | below_keys == everything, "the two halves must cover the whole sample"
    assert above.data["sample"]["filtering"]["source_run_count"] == len(everything)

    levels = [row["mythic_level"] for row in runs]
    floor = run("raiderio", "sample", "mythic-plus-runs", *SCOPE, "--level-min", str(max(levels) + 1))
    assert floor.data["runs"] == [], "no run can clear a floor above the highest sampled key level"
    assert floor.data["sample"]["filtering"]["excluded_run_count"] == len(everything)

    capped = run("raiderio", "sample", "mythic-plus-runs", *SCOPE, "--level-max", str(min(levels) - 1))
    assert capped.data["runs"] == [], "no run can fit under a cap below the lowest sampled key level"
    assert capped.data["sample"]["filtering"]["excluded_run_count"] == len(everything)

    # The other direction: bounds sitting exactly on the observed range are inclusive and keep every
    # run, so a level filter that dropped everything would fail here instead of passing the two above.
    edges = run("raiderio", "sample", "mythic-plus-runs", *SCOPE, "--level-min", str(min(levels)), "--level-max", str(max(levels)))
    assert _run_keys(edges) == everything, edges.describe()

    # And a floor inside the range keeps exactly the runs at the top key level: a strict, non-empty
    # subset, so a bound that only works at the extremes (or off by one) cannot pass.
    assert min(levels) < max(levels), "the sample needs two key levels for a bound inside the range to bite"
    top = run("raiderio", "sample", "mythic-plus-runs", *SCOPE, "--level-min", str(max(levels)))
    expected = {_run_key(row) for row in runs if row["mythic_level"] == max(levels)}
    assert _run_keys(top) == expected, top.describe()
    assert expected < everything


def test_the_roster_filters_keep_exactly_the_runs_that_carry_the_value(baseline_sample: Result) -> None:
    """``--contains-class``/``--contains-spec`` select on the roster; ``--player-region``/``--contains-role`` too.

    The class and spec values are chosen from the sample so that some runs carry them and some do
    not, which makes the expected set known exactly. The other two flags are proved both ways, on a
    value every run carries (each run on a US leaderboard fields a US player and a tank) and on one
    none can (no EU player, no made-up role): a filter that dropped everything fails the first leg,
    one that did nothing fails the second.
    """
    runs = _rows(baseline_sample, "runs")
    everything = _run_keys(baseline_sample)

    # A class-qualified spec (``priest-holy``) must not also keep the runs of another class's Holy.
    for field, flag in (("class_slug", "--contains-class"), ("spec_slug", "--contains-spec"), ("class_spec", "--contains-spec")):
        value = _a_value_only_some_runs_carry(runs, field)
        expected = {_run_key(row) for row in runs if value in _roster_values(row, field)}
        narrowed = run("raiderio", "sample", "mythic-plus-runs", *SCOPE, flag, value)
        assert _run_keys(narrowed) == expected, f"{flag} {value} kept the wrong runs\n{narrowed.describe()}"
        assert expected < everything, f"{flag} {value} has to drop at least one run to prove anything"
        assert narrowed.data["sample"]["filtering"]["excluded_run_count"] == len(everything) - len(expected)

    for flag, field, everywhere, nowhere in (("--player-region", "region", "us", "eu"), ("--contains-role", "role", "tank", "healbot")):
        assert all(everywhere in _roster_values(row, field) for row in runs), f"a sampled run has no {everywhere} {field}"
        kept = run("raiderio", "sample", "mythic-plus-runs", *SCOPE, flag, everywhere)
        assert _run_keys(kept) == everything, f"{flag} {everywhere} is on every sampled roster\n{kept.describe()}"
        assert kept.data["sample"]["filtering"]["excluded_run_count"] == 0

        assert not any(nowhere in _roster_values(row, field) for row in runs), f"{nowhere} is in the sample after all"
        empty = run("raiderio", "sample", "mythic-plus-runs", *SCOPE, flag, nowhere)
        assert empty.data["runs"] == [], f"{flag} {nowhere} matches no sampled roster\n{empty.describe()}"
        assert empty.data["sample"]["filtering"]["excluded_run_count"] == len(everything)


def test_the_affixes_scope_changes_both_the_rows_and_the_citation(baseline_sample: Result) -> None:
    """``--affixes`` picks a different Raider.IO leaderboard, so it must show up in every row."""
    affix = sorted({affix for row in _rows(baseline_sample, "runs") for affix in row["affixes"]})[0]

    result = run("raiderio", "sample", "mythic-plus-runs", *SCOPE, "--affixes", affix)

    assert result.payload["query"]["affixes"] == affix
    assert all(affix in row["affixes"] for row in _rows(result, "runs")), result.describe()
    urls = result.data["citations"]["leaderboard_urls"]
    assert all(url.endswith(f"/{affix}") for url in urls), urls


def test_sample_mythic_plus_players_dedupes_roster_entries_into_snapshots(player_sample: Result, baseline_sample: Result) -> None:
    result = player_sample

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
    # One snapshot per character, counting every roster row that character has in the same page.
    roster = _roster_entries_by_player(baseline_sample)
    assert len({player["profile_url"] for player in players}) == len(players), "a character must be one snapshot"
    assert all(player["appearance_count"] == len(roster[player["profile_url"]]) for player in players), result.describe()
    _assert_sampled_provenance(result)


def _assert_distribution_shape(result: Result, *, unit: str) -> list[dict[str, Any]]:
    """The rows are a complete tally under ``unit``, ordered the way the payload promises.

    The ordering is checked against an independently sorted copy rather than trusted, because the
    rows are what an agent reads "the commonest first" off.
    """
    distribution = result.data["distribution"]
    rows: list[dict[str, Any]] = distribution["rows"]
    assert distribution["unit"] == unit, result.describe()
    assert rows and all(isinstance(row["count"], int) and row["count"] >= 1 for row in rows)
    assert rows == sorted(rows, key=lambda row: (-row["count"], row["value"])), "rows must be ordered by count"
    assert abs(sum(row["percent"] for row in rows) - 100.0) < 1.0, result.describe()
    _assert_sampled_provenance(result)
    return rows


def _assert_run_distribution_matches_the_metric(result: Result, metric: str, rows: list[dict[str, Any]], baseline: Result) -> None:
    """Tie the tally to the one thing in the sample that only this metric can produce."""
    sample = result.data["sample"]
    values = {row["value"] for row in rows}
    if metric == "mythic_level":
        assert result.data["distribution"]["statistics"] == sample["mythic_level"], result.describe()
    elif metric == "dungeon":
        assert values == set(sample["unique_dungeons"]), result.describe()
    elif metric == "role":
        assert rows == sample["role_counts"], result.describe()
    elif metric == "player_region":
        assert rows == sample["player_region_counts"], result.describe()
    elif metric in ("class", "spec"):
        # The sample block carries no class or spec counts, so the tally is held to the roster rows of
        # the baseline sample, which read the same cached page. A tally of any other roster field
        # (region, role) has the same unit and total, and only an exact count tells it apart. A spec
        # is counted with its class, so Holy Paladin and Holy Priest are two rows, not one "holy".
        roster = [entry for row in _rows(baseline, "runs") for entry in row["roster"]]
        label = (lambda entry: entry["class_slug"]) if metric == "class" else _class_spec
        assert _counts(rows) == dict(Counter(map(label, roster))), result.describe()
    else:
        # The composition keys are one "role:label" pair per roster slot, over class or class-spec labels.
        labels = {part.split(":", 1)[1] for value in values for part in value.split(" | ")}
        assert all(len(value.split(" | ")) == 5 for value in values), result.describe()
        assert (labels <= WOW_CLASS_SLUGS) is (metric == "class_composition"), result.describe()
        if metric == "composition":
            assert all(any(label.startswith(f"{slug}-") for slug in WOW_CLASS_SLUGS) for label in labels), result.describe()


@pytest.mark.parametrize("metric", sorted(RUN_DISTRIBUTION_UNITS))
def test_distribution_mythic_plus_runs_covers_every_documented_metric(metric: str, baseline_sample: Result) -> None:
    result = run("raiderio", "distribution", "mythic-plus-runs", "--metric", metric, *SCOPE)

    assert result.data["metric"] == metric
    sample = result.data["sample"]
    unit = RUN_DISTRIBUTION_UNITS[metric]
    rows = _assert_distribution_shape(result, unit=unit)
    assert sum(row["count"] for row in rows) == (sample["run_count"] if unit == "runs" else sample["roster_entry_count"])
    _assert_run_distribution_matches_the_metric(result, metric, rows, baseline_sample)


def _assert_player_distribution_matches_the_metric(
    result: Result, metric: str, rows: list[dict[str, Any]], players: Result, baseline: Result
) -> None:
    """Tie the tally to the one thing in the sample block that only this metric can produce."""
    sample = result.data["sample"]
    values = {row["value"] for row in rows}
    total = sum(row["count"] for row in rows)
    if metric in ("appearance_count", "top_mythic_level"):
        assert result.data["distribution"]["statistics"] == sample[metric], result.describe()
        assert total == sample["player_count"], result.describe()
    elif metric in ("class", "spec", "role"):
        # A tag tally counts each sampled player once per distinct value it played. The sample block's
        # own tag lists come from the same snapshot field as the tally, so the expected counts are
        # rebuilt from the baseline roster rows of the sampled players instead.
        field = {"class": "class_slug", "spec": "class_spec", "role": "role"}[metric]
        roster = _roster_entries_by_player(baseline)
        expected = Counter(
            value
            for player in _rows(players, "players")
            for value in _roster_values({"roster": roster[player["profile_url"]]}, field)
        )
        assert _counts(rows) == dict(expected), result.describe()
    else:
        assert total == sample["player_count"], result.describe()
        assert values <= {row["value"] for row in sample["player_region_counts"]}, result.describe()


@pytest.mark.parametrize("metric", sorted(PLAYER_DISTRIBUTION_UNITS))
def test_distribution_mythic_plus_players_covers_every_documented_metric(
    metric: str, player_sample: Result, baseline_sample: Result
) -> None:
    result = run("raiderio", "distribution", "mythic-plus-players", "--metric", metric, *SCOPE, "--player-limit", "25")

    assert result.data["metric"] == metric
    rows = _assert_distribution_shape(result, unit=PLAYER_DISTRIBUTION_UNITS[metric])
    _assert_player_distribution_matches_the_metric(result, metric, rows, player_sample, baseline_sample)


@pytest.mark.parametrize(("metric", "estimated"), [("score", "mythic_level"), ("mythic_level", "score")])
def test_threshold_mythic_plus_runs_estimates_around_a_target(metric: str, estimated: str, baseline_sample: Result) -> None:
    runs = _rows(baseline_sample, "runs")
    target = runs[len(runs) // 2][metric]

    result = run("raiderio", "threshold", "mythic-plus-runs", "--metric", metric, "--value", str(target), *SCOPE, "--nearest", "5")

    assert result.data["metric"] == metric
    assert result.data["target"] == pytest.approx(float(target))
    threshold = result.data["threshold"]
    assert 1 <= threshold["nearest_match_count"] <= 5
    assert len(threshold["nearest_matches"]) == threshold["nearest_match_count"]
    assert threshold["nearest_matches"][0]["distance"] == pytest.approx(0.0), "an observed value must have a zero-distance neighbour"
    # A threshold answers "what does a run near this score look like", so the estimate has to be
    # about the other metric; an estimate of the metric that was asked about says nothing.
    estimate = threshold["estimate"]
    assert estimate["metric"] == estimated, result.describe()
    neighbours = [row["run"][estimated] for row in threshold["nearest_matches"]]
    assert (estimate["count"], estimate["min"], estimate["max"]) == (len(neighbours), min(neighbours), max(neighbours))
    assert threshold["caveat"].strip(), "a derived estimate must carry its caveat"
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
    result = run("raiderio", *scope, "--realm", REALM, "--limit", "5")

    assert result.payload["query"]["realm"] == REALM
    rows = _rows(result, "rows")
    assert all(row["guild"]["realm"] == REALM for row in rows), result.describe()
    assert [row["rank"] for row in rows] == list(range(1, len(rows) + 1)), "realm-scoped rank must restart at 1"
    assert all(row["region_rank"] >= row["rank"] for row in rows), "a realm rank can never beat the region rank"
    # The realm scope is a slice of the region scope, so the realm's best guild has to sit at its
    # region_rank on the region-wide page that holds that rank.
    best = rows[0]
    page = (best["region_rank"] - 1) // RANKING_PAGE_SIZE
    region_page = run("raiderio", *scope, "--limit", str(RANKING_PAGE_SIZE), "--page", str(page))
    region_ranks = {row["guild"]["profile_url"]: row["rank"] for row in _rows(region_page, "rows")}
    assert region_ranks.get(best["guild"]["profile_url"]) == best["region_rank"], region_page.describe()
    assert result.data["citations"]["leaderboard_urls"][0].endswith(f"?realm={REALM}")


def test_unknown_guild_and_character_are_not_found() -> None:
    for command, name in (("guild", "zzzznotarealguildzzzz"), ("character", "Zzzznotarealcharzzz")):
        result = run("raiderio", command, REGION, REALM, name, expect=EXIT_NOT_FOUND, error_code="not_found")
        assert result.payload["error"]["details"]["status_code"] == 400
        assert "could not find" in result.payload["error"]["message"].lower()

    # Raider.IO answers an unknown realm with HTTP 400 "Failed to find realm", which is a miss too.
    realm = run("raiderio", "guild", REGION, "zzzznorealmzzzz", GUILD, expect=EXIT_NOT_FOUND, error_code="not_found")
    assert "failed to find realm" in realm.payload["error"]["message"].lower(), realm.describe()


def test_a_realm_display_name_finds_the_same_guild() -> None:
    """Players type ``Mal'Ganis``; Raider.IO takes either slug spelling, so the display name has to reach the guild."""
    by_slug = run("raiderio", "guild", REGION, REALM, GUILD)
    by_name = run("raiderio", "guild", REGION, pins.GUILD_REALM_DISPLAY, GUILD)
    assert by_name.data["guild"]["name"] == by_slug.data["guild"]["name"] == GUILD, by_name.describe()
    assert by_name.data["guild"]["realm"] == by_slug.data["guild"]["realm"], by_name.describe()


def test_malformed_request_is_a_usage_error() -> None:
    result = run("raiderio", "guild", "zz", REALM, GUILD, expect=EXIT_USAGE, error_code="invalid_query")
    assert result.payload["error"]["details"]["status_code"] == 400


@pytest.mark.parametrize(
    ("args", "command", "error_code"),
    [
        # An unsupported --kind is the code every provider gives that mistake (method, icy-veins too).
        (("search", "liquid", "--kind", "bogus"), "search", "invalid_argument"),
        (("resolve", "liquid", "--kind", "bogus"), "resolve", "invalid_argument"),
        (("distribution", "mythic-plus-runs", "--metric", "bogus"), "distribution mythic-plus-runs", "invalid_query"),
        (("distribution", "mythic-plus-players", "--metric", "bogus"), "distribution mythic-plus-players", "invalid_query"),
        (("threshold", "mythic-plus-runs", "--metric", "bogus", "--value", "100"), "threshold mythic-plus-runs", "invalid_query"),
        (("leaderboard", "raids", "--raid", "sporefall", "--difficulty", "bogus"), "leaderboard raids", "invalid_query"),
        (("leaderboard", "raids", "--raid", "sporefall", "--realm", "malganis"), "leaderboard raids", "invalid_query"),
    ],
)
def test_invalid_kind_or_metric_is_a_usage_error(args: tuple[str, ...], command: str, error_code: str) -> None:
    result = run("raiderio", *args, expect=EXIT_USAGE, error_code=error_code)
    # A failure labels itself with the full sub-path, the same value the success envelope carries,
    # so no two commands answer to the same `command`.
    assert result.payload["command"] == command, result.describe()


def test_network_failure_is_an_exit_5_envelope(cache_root: Path) -> None:
    del cache_root  # the dead-proxy run needs a cold cache, so it uses its own root below
    env = {**dead_proxy_env(), "RAIDERIO_CACHE_BACKEND": "none"}
    result = run("raiderio", "guild", REGION, REALM, GUILD, expect=EXIT_NETWORK, env=env)
    assert result.error_code in {"network_error", "timeout"}, result.describe()
    assert result.stdout == ""

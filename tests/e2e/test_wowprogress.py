"""End-to-end journeys for the ``wowprogress`` binary against live WowProgress pages.

Every command in docs/reference/wowprogress.md is exercised here.

WowProgress sits behind Cloudflare and the client answers it by impersonating Chrome
(``DEFAULT_IMPERSONATE`` in ``wowprogress_cli.client``); see docs/wowprogress/README.md. A
``blocked`` envelope is therefore a real journey failure, never a reason to skip: it means either
the impersonation regressed or upstream tightened. Exclude the provider explicitly with
``WARCRAFT_E2E_SKIP=wowprogress`` only after that has been investigated and recorded.

The active raid tier, leaderboard ranks, and tier history all move upstream, so nothing about them
is pinned; the journeys read the shape and the identity back out of the payload instead.
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
)

REGION = pins.GUILD_REGION
# WowProgress addresses realms by their punctuated title; the CLI normalizes it to the page slug.
REALM = pins.GUILD_REALM_DISPLAY
REALM_SLUG = "mal-ganis"
GUILD = pins.GUILD_NAME
CHARACTER = pins.CHARACTER_NAME


@pytest.fixture(autouse=True)
def _require_wowprogress(require) -> None:
    require("wowprogress")


def _cache_entries(cache_root: Path) -> set[Path]:
    provider_cache = cache_root / "warcraft" / "wowprogress" / "http"
    return set(provider_cache.rglob("*")) if provider_cache.exists() else set()


def _rows(result: Result, key: str) -> list[dict[str, Any]]:
    rows = payload_or_legacy(result, key)
    assert isinstance(rows, list) and rows, f"{key} must be a non-empty list\n{result.describe()}"
    return rows


def _assert_sampling_and_freshness(result: Result) -> None:
    """Sampled payloads must describe the slice they fetched and cite the page they read."""
    sample = payload_or_legacy(result, "sample")
    sampling = sample["sampling"]
    assert sampling["requested_limit"] >= 1, result.describe()
    freshness = payload_or_legacy(result, "freshness")
    assert freshness["sampled_at"], result.describe()
    assert isinstance(freshness["cache_ttl_seconds"], int) and freshness["cache_ttl_seconds"] >= 1, result.describe()
    citations = payload_or_legacy(result, "citations")
    assert citations["leaderboard_page"].startswith("https://www.wowprogress.com/"), result.describe()


def test_doctor_reports_the_impersonating_transport_and_isolated_cache(cache_root: Path) -> None:
    result = run("wowprogress", "doctor")

    assert payload_or_legacy(result, "status") == "ready"
    assert payload_or_legacy(result, "auth")["required"] is False
    transport = payload_or_legacy(result, "transport")
    assert transport["mode"] == "browser_fingerprint_http"
    assert transport["impersonate"].startswith("chrome"), "the sanctioned browser impersonation must be advertised"
    capabilities = payload_or_legacy(result, "capabilities")
    assert {"guild", "guild_history", "guild_ranks", "guild_snapshot", "history_trajectory", "character", "leaderboard"} <= set(capabilities)
    assert set(capabilities.values()) == {"ready"}, result.describe()
    cache = payload_or_legacy(result, "cache")
    assert Path(cache["cache_dir"]) == cache_root / "warcraft" / "wowprogress" / "http"
    assert result.payload["provenance"]["transport"] == "browser_fingerprint_http"


def test_search_probes_the_pinned_guild_route() -> None:
    result = run("wowprogress", "search", f"guild {REGION} {REALM} {GUILD}", "--limit", "5")

    rows = _rows(result, "results")
    top = rows[0]
    assert top["kind"] == "guild"
    assert top["name"] == GUILD
    assert top["follow_up"]["command"] == f"wowprogress guild {REGION} {REALM} {GUILD}"
    assert "type_hint" in top["ranking"]["match_reasons"]


def test_search_returns_a_structured_hint_instead_of_guessing() -> None:
    result = run("wowprogress", "search", "thunderfury", "--limit", "5")

    assert payload_or_legacy(result, "count") == 0
    assert payload_or_legacy(result, "results") == []
    assert "structured" in payload_or_legacy(result, "message").lower()
    assert payload_or_legacy(result, "suggested_queries"), "an unparseable query must suggest the structured form"


def test_resolve_returns_one_next_command_for_the_pinned_guild() -> None:
    result = run("wowprogress", "resolve", f"guild {REGION} {REALM} {GUILD}", "--limit", "5")

    assert payload_or_legacy(result, "resolved") is True, result.describe()
    assert payload_or_legacy(result, "next_command") == f"wowprogress guild {REGION} {REALM} {GUILD}"
    match = payload_or_legacy(result, "match")
    assert match["kind"] == "guild"
    assert "exact_target_name" in match["ranking"]["match_reasons"]


def test_resolve_stays_unresolved_for_an_unparseable_query() -> None:
    result = run("wowprogress", "resolve", "thunderfury")

    assert payload_or_legacy(result, "resolved") is False
    assert payload_or_legacy(result, "match") is None
    assert payload_or_legacy(result, "next_command") is None


def test_guild_page_carries_progress_ranks_and_item_level(cache_root: Path) -> None:
    result = run("wowprogress", "guild", REGION, REALM, GUILD)

    guild = payload_or_legacy(result, "guild")
    assert guild["name"] == GUILD
    assert guild["region"] == REGION
    assert REALM_SLUG in guild["page_url"], result.describe()
    assert guild["page_url"].startswith("https://www.wowprogress.com/guild/")

    progress = payload_or_legacy(result, "progress")
    assert progress["summary"], "a raiding guild page must report a progress summary"
    assert str(progress["ranks"]["world"]).isdigit(), result.describe()
    item_level = payload_or_legacy(result, "item_level")
    assert isinstance(item_level["average"], (int, float)) and item_level["average"] > 0
    assert payload_or_legacy(result, "citations")["page"].startswith("https://www.wowprogress.com/guild/")
    assert _cache_entries(cache_root), "the guild page fetch must populate the wowprogress cache directory"


def test_a_repeated_guild_fetch_is_served_from_the_disk_cache(tmp_path: Path) -> None:
    # WowProgress payloads expose no cache-hit flag, so the disk cache is the observable signal.
    private_cache = tmp_path / "wowprogress-cache"
    env = {"WOWPROGRESS_CACHE_DIR": str(private_cache)}
    assert not private_cache.exists()

    first = run("wowprogress", "guild", REGION, REALM, GUILD, env=env)
    warmed = set(private_cache.rglob("*.json"))
    assert warmed, "the first fetch must write the guild page to the configured cache dir"

    repeat = run("wowprogress", "guild", REGION, REALM, GUILD, env=env)
    assert set(private_cache.rglob("*.json")) == warmed, "a cache hit must not add cache entries"
    assert repeat.data == first.data, "a cache hit must return the same payload"


def test_guild_history_returns_every_archived_tier() -> None:
    result = run("wowprogress", "guild-history", REGION, REALM, GUILD)

    assert result.payload["query"] == {"region": REGION, "realm": REALM_SLUG, "name": GUILD}
    tiers = _rows(result, "tiers")
    assert payload_or_legacy(result, "count") == len(tiers)
    top = tiers[0]
    assert top["raid"], result.describe()
    assert top["tier_key"].startswith("tier")
    assert top["page_url"].startswith("https://www.wowprogress.com/guild/")
    assert top["final_ranks"] is not None


def test_guild_ranks_reports_world_region_and_realm_per_tier() -> None:
    result = run("wowprogress", "guild-ranks", REGION, REALM, GUILD)

    assert result.payload["kind"] == "guild_ranks"
    tiers = _rows(result, "tiers")
    assert payload_or_legacy(result, "count") == len(tiers)
    ranked = [tier for tier in tiers if tier["final_ranks"]]
    assert ranked, "at least one archived tier must carry final ranks"
    assert {"world", "region", "realm"} <= set(ranked[0]["final_ranks"])
    assert payload_or_legacy(result, "citations")["page"].startswith("https://www.wowprogress.com/guild/")


def test_guild_snapshot_composes_current_state_with_a_rank_series() -> None:
    result = run("wowprogress", "guild-snapshot", REGION, REALM, GUILD)

    assert result.payload["kind"] == "guild_snapshot"
    assert result.payload["query"] == {"region": REGION, "realm": REALM_SLUG, "name": GUILD}
    assert payload_or_legacy(result, "guild")["name"] == GUILD
    assert payload_or_legacy(result, "progress")["ranks"]["world"] is not None
    assert payload_or_legacy(result, "item_level")["average"] is not None
    assert isinstance(payload_or_legacy(result, "encounters")["count"], int)
    assert _rows(result, "rank_series")
    freshness = payload_or_legacy(result, "freshness")
    assert freshness["sampled_at"] and freshness["cache_ttl_seconds"] >= 1


def test_history_trajectory_orders_tiers_oldest_first_with_deltas() -> None:
    result = run("wowprogress", "history-trajectory", REGION, REALM, GUILD)

    assert result.payload["kind"] == "history_trajectory"
    tiers = _rows(result, "tiers")
    assert payload_or_legacy(result, "count") == len(tiers)
    assert tiers[0]["delta_vs_previous"] is None, "the oldest tier has nothing to compare against"
    assert all(tier["page_url"].startswith("https://www.wowprogress.com/") for tier in tiers)
    # The difficulty column is a short token ("M"), not a "8/8 (M)" summary.
    assert all(tier["difficulty"] is None or len(str(tier["difficulty"])) <= 3 for tier in tiers)
    if len(tiers) > 1:
        delta = tiers[1]["delta_vs_previous"]
        assert delta["previous_tier_key"] == tiers[0]["tier_key"]
    notes = payload_or_legacy(result, "notes")
    assert any("different raids" in note for note in notes), "tier-over-tier deltas must be caveated"


def test_character_page_carries_item_level_and_ranks() -> None:
    result = run("wowprogress", "character", REGION, REALM, CHARACTER)

    character = payload_or_legacy(result, "character")
    assert character["name"] == CHARACTER
    assert character["region"] == REGION
    assert character["page_url"].startswith("https://www.wowprogress.com/character/")
    assert payload_or_legacy(result, "item_level")["value"] is not None
    assert payload_or_legacy(result, "citations")["page"].startswith("https://www.wowprogress.com/character/")


def test_leaderboard_pve_returns_ranked_rows() -> None:
    result = run("wowprogress", "leaderboard", "pve", "us", "--limit", "10")

    leaderboard = payload_or_legacy(result, "leaderboard")
    assert leaderboard["kind"] == "pve"
    assert leaderboard["region"] == "us"
    assert leaderboard["active_raid"], "the leaderboard must name the raid it ranks"
    assert leaderboard["page_url"] == "https://www.wowprogress.com/pve/us"

    entries = _rows(result, "entries")
    assert len(entries) == 10
    assert payload_or_legacy(result, "count") == 10
    assert [row["rank"] for row in entries] == sorted(row["rank"] for row in entries)
    assert all(row["guild_name"] and row["progress"] for row in entries)


def test_sample_pve_leaderboard_reports_its_sampling_boundaries() -> None:
    result = run("wowprogress", "sample", "pve-leaderboard", "--region", "us", "--limit", "10")

    assert result.payload["kind"] == "pve_leaderboard_sample"
    sample = payload_or_legacy(result, "sample")
    assert sample["active_raid"]
    assert sample["sampling"]["requested_limit"] == 10
    entries = _rows(result, "entries")
    assert sample["entry_count"] == len(entries) == sample["sampling"]["returned_entry_count"]
    assert isinstance(entries[0]["bosses_killed"], int)
    assert entries[0]["difficulty"], "each row must carry the parsed difficulty token"
    _assert_sampling_and_freshness(result)


@pytest.mark.parametrize("metric", ["progress", "difficulty", "realm", "bosses_killed", "rank"])
def test_distribution_pve_leaderboard_covers_every_documented_metric(metric: str) -> None:
    result = run("wowprogress", "distribution", "pve-leaderboard", "--region", "us", "--metric", metric, "--limit", "25")

    assert result.payload["kind"] == "pve_leaderboard_distribution"
    assert payload_or_legacy(result, "metric") == metric
    rows = payload_or_legacy(result, "distribution")["rows"]
    assert rows and all(isinstance(row["count"], int) and row["count"] >= 1 for row in rows)
    _assert_sampling_and_freshness(result)


@pytest.mark.parametrize("metric", ["rank", "bosses_killed"])
def test_threshold_pve_leaderboard_estimates_around_a_target(metric: str) -> None:
    result = run(
        "wowprogress", "threshold", "pve-leaderboard",
        "--region", "us", "--metric", metric, "--value", "10", "--nearest", "5", "--limit", "25",
    )

    assert result.payload["kind"] == "pve_leaderboard_threshold"
    assert payload_or_legacy(result, "metric") == metric
    threshold = payload_or_legacy(result, "threshold")
    assert 1 <= threshold["nearest_match_count"] <= 5
    assert len(threshold["nearest_matches"]) == threshold["nearest_match_count"]
    assert threshold["estimate"] is not None
    _assert_sampling_and_freshness(result)


def test_sample_pve_guild_profiles_enriches_the_top_rows() -> None:
    result = run("wowprogress", "sample", "pve-guild-profiles", "--region", "us", "--limit", "3")

    assert result.payload["kind"] == "pve_guild_profiles_sample"
    sample = payload_or_legacy(result, "sample")
    sampling = sample["sampling"]
    assert sampling["source_leaderboard_entry_count"] >= sampling["returned_guild_profile_count"]
    profiles = _rows(result, "guild_profiles")
    assert sample["guild_profile_count"] == len(profiles)
    top = profiles[0]
    assert top["guild_name"] and top["item_level_average"] is not None
    assert top["progress_ranks"]["world"] is not None
    _assert_sampling_and_freshness(result)


def test_sample_pve_guild_profiles_reports_what_a_filter_excluded() -> None:
    result = run("wowprogress", "sample", "pve-guild-profiles", "--region", "us", "--limit", "3", "--difficulty", "m")

    assert result.payload["query"]["filters"]["difficulty"] == ["m"]
    filtering = payload_or_legacy(result, "sample")["filtering"]
    assert filtering["source_profile_count"] == filtering["returned_profile_count"] + filtering["excluded_profile_count"]


@pytest.mark.parametrize("metric", ["progress", "faction", "item_level_average", "world_rank", "encounter"])
def test_distribution_pve_guild_profiles_covers_every_documented_metric(metric: str) -> None:
    result = run("wowprogress", "distribution", "pve-guild-profiles", "--region", "us", "--metric", metric, "--limit", "3")

    assert result.payload["kind"] == "pve_guild_profiles_distribution"
    assert payload_or_legacy(result, "metric") == metric
    assert payload_or_legacy(result, "distribution")["rows"], result.describe()
    _assert_sampling_and_freshness(result)


@pytest.mark.parametrize("metric", ["world_rank", "item_level_average"])
def test_threshold_pve_guild_profiles_estimates_around_a_target(metric: str) -> None:
    result = run(
        "wowprogress", "threshold", "pve-guild-profiles",
        "--region", "us", "--metric", metric, "--value", "10", "--nearest", "2", "--limit", "3",
    )

    assert result.payload["kind"] == "pve_guild_profiles_threshold"
    assert payload_or_legacy(result, "metric") == metric
    threshold = payload_or_legacy(result, "threshold")
    assert threshold["nearest_matches"]
    assert threshold["estimate"] is not None
    _assert_sampling_and_freshness(result)


def test_global_output_flags_shape_the_payload() -> None:
    fields = run_raw("wowprogress", "--fields", "data.leaderboard", "--fields-strict", "leaderboard", "pve", "us", "--limit", "5")
    assert fields.exit_code == 0, fields.describe()
    assert set(fields.payload) == {"data"}, fields.describe()
    assert fields.payload["data"]["leaderboard"]["kind"] == "pve"

    compact = run("wowprogress", "--compact", "--compact-max-chars", "40", "guild", REGION, REALM, GUILD)
    page_url = compact.data["guild"]["page_url"]
    assert page_url.endswith("...") and len(page_url) == 40, compact.describe()

    human = run("wowprogress", "--profile", "human", "doctor")
    assert human.stdout.startswith("{\n"), "the human profile must pretty-print"

    debug = run("wowprogress", "--profile", "debug", "doctor")
    assert debug.stdout.startswith("{\n")
    assert payload_or_legacy(debug, "status") == "ready"


def test_fields_strict_rejects_a_missing_path() -> None:
    result = run("wowprogress", "--fields", "data.no_such_key", "--fields-strict", "doctor", expect=EXIT_USAGE, error_code="missing_fields")
    assert result.payload["error"]["details"]["missing_fields"] == ["data.no_such_key"]


def test_unknown_guild_is_not_found() -> None:
    run("wowprogress", "guild", REGION, REALM, "zzzznotarealguildzzzz", expect=EXIT_NOT_FOUND, error_code="not_found")


@pytest.mark.parametrize(
    "args",
    [
        ("leaderboard", "pvp", "us"),
        ("distribution", "pve-leaderboard", "--region", "us", "--metric", "bogus"),
        ("distribution", "pve-guild-profiles", "--region", "us", "--metric", "bogus"),
        ("threshold", "pve-leaderboard", "--region", "us", "--metric", "bogus", "--value", "10"),
        ("threshold", "pve-guild-profiles", "--region", "us", "--metric", "bogus", "--value", "10"),
    ],
)
def test_invalid_kind_or_metric_is_a_usage_error(args: tuple[str, ...]) -> None:
    run("wowprogress", *args, expect=EXIT_USAGE, error_code="invalid_query")


def test_network_failure_is_an_exit_5_envelope() -> None:
    env = {**dead_proxy_env(), "WOWPROGRESS_CACHE_BACKEND": "none"}
    result = run("wowprogress", "guild", REGION, REALM, GUILD, expect=EXIT_NETWORK, env=env)
    assert result.error_code == "network_error", result.describe()
    assert result.stdout == ""

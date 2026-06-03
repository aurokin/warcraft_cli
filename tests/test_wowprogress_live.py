from __future__ import annotations

import pytest
from article_provider_testkit import payload_for_live, require_live
from typer.testing import CliRunner
from wowprogress_cli.main import app

pytestmark = pytest.mark.live

runner = CliRunner()


def test_live_wowprogress_structured_guild_search_contract() -> None:
    require_live("WowProgress")
    payload = payload_for_live(runner, app, ["search", "guild us illidan Liquid", "--limit", "5"], provider_name="WowProgress")

    assert payload["count"] >= 1
    first = payload["results"][0]
    assert first["kind"] == "guild"
    assert first["follow_up"]["command"] == "wowprogress guild us illidan Liquid"
    assert "type_hint" in first["ranking"]["match_reasons"]


def test_live_wowprogress_structured_character_resolve_contract() -> None:
    require_live("WowProgress")
    payload = payload_for_live(
        runner,
        app,
        ["resolve", "character us illidan Imonthegcd", "--limit", "5"],
        provider_name="WowProgress",
    )

    assert payload["resolved"] is True
    assert payload["next_command"] == "wowprogress character us illidan Imonthegcd"
    assert payload["match"]["kind"] == "character"
    assert "exact_target_name" in payload["match"]["ranking"]["match_reasons"]


def test_live_wowprogress_short_exact_guild_resolve_contract() -> None:
    require_live("WowProgress")
    payload = payload_for_live(runner, app, ["resolve", "guild us area-52 xD", "--limit", "5"], provider_name="WowProgress")

    assert payload["resolved"] is True
    assert payload["next_command"] == "wowprogress guild us area-52 xD"
    assert payload["match"]["kind"] == "guild"
    assert "exact_target_name" in payload["match"]["ranking"]["match_reasons"]
    assert "exact_target_realm" in payload["match"]["ranking"]["match_reasons"]


def test_live_wowprogress_leaderboard_contract() -> None:
    require_live("WowProgress")
    payload = payload_for_live(runner, app, ["leaderboard", "pve", "us", "--limit", "5"], provider_name="WowProgress")

    assert payload["leaderboard"]["kind"] == "pve"
    assert payload["leaderboard"]["region"] == "us"
    assert payload["count"] == 5
    assert len(payload["entries"]) == 5
    assert all(entry["progress"] and "Manaforge Omega" not in entry["progress"] for entry in payload["entries"])


def test_live_wowprogress_guild_contract() -> None:
    require_live("WowProgress")
    payload = payload_for_live(runner, app, ["guild", "na", "Illidan", "Liquid"], provider_name="WowProgress")

    assert payload["guild"]["name"] == "Liquid"
    assert payload["progress"]["summary"] == "8/8 (M)"
    assert payload["progress"]["ranks"]["world"] is not None
    assert payload["item_level"]["ranks"]["region"] is not None


def test_live_wowprogress_guild_history_contract() -> None:
    require_live("WowProgress")
    payload = payload_for_live(runner, app, ["guild-history", "us", "Mal'Ganis", "gn"], provider_name="WowProgress")

    assert payload["kind"] == "guild_history"
    assert payload["count"] >= 1
    assert payload["tiers"][0]["raid"]
    assert payload["tiers"][0]["final_ranks"] is not None


def test_live_wowprogress_guild_snapshot_contract() -> None:
    require_live("WowProgress")
    payload = payload_for_live(runner, app, ["guild-snapshot", "na", "Illidan", "Liquid"], provider_name="WowProgress")

    assert payload["kind"] == "guild_snapshot"
    assert payload["guild"]["name"] == "Liquid"
    assert payload["progress"]["ranks"]["world"] is not None
    assert payload["item_level"]["average"] is not None  # guild-page item_level shape
    assert len(payload["rank_series"]) >= 1
    assert payload["freshness"]["cache_ttl_seconds"] >= 1


def test_live_wowprogress_history_trajectory_contract() -> None:
    require_live("WowProgress")
    payload = payload_for_live(runner, app, ["history-trajectory", "us", "Mal'Ganis", "gn"], provider_name="WowProgress")

    assert payload["kind"] == "history_trajectory"
    assert payload["count"] >= 1
    assert payload["tiers"][0]["page_url"]
    # The difficulty token must be a short code (e.g. "M"), not a "N/N (...)" summary string.
    assert all((t["difficulty"] is None or len(str(t["difficulty"])) <= 3) for t in payload["tiers"])
    assert any("different raids" in note for note in payload["notes"])


def test_live_wowprogress_character_contract() -> None:
    require_live("WowProgress")
    payload = payload_for_live(runner, app, ["character", "us", "illidan", "Imonthegcd"], provider_name="WowProgress")

    assert payload["character"]["name"] == "Imonthegcd"
    assert payload["item_level"]["value"] is not None
    assert payload["item_level"]["ranks"]["realm"] is not None
    assert payload["sim_dps"]["ranks"]["region"] is not None


def test_live_wowprogress_sample_pve_leaderboard_contract() -> None:
    require_live("WowProgress")
    payload = payload_for_live(
        runner,
        app,
        ["sample", "pve-leaderboard", "--region", "us", "--limit", "10"],
        provider_name="WowProgress",
    )

    assert payload["kind"] == "pve_leaderboard_sample"
    assert payload["sample"]["entry_count"] == len(payload["entries"])
    assert payload["sample"]["sampling"]["requested_limit"] == 10
    assert payload["sample"]["active_raid"]
    assert payload["freshness"]["cache_ttl_seconds"] is not None


def test_live_wowprogress_distribution_pve_leaderboard_contract() -> None:
    require_live("WowProgress")
    payload = payload_for_live(
        runner,
        app,
        ["distribution", "pve-leaderboard", "--region", "us", "--metric", "difficulty", "--limit", "10"],
        provider_name="WowProgress",
    )

    assert payload["kind"] == "pve_leaderboard_distribution"
    assert payload["metric"] == "difficulty"
    assert payload["distribution"]["rows"]
    assert payload["citations"]["leaderboard_page"]


def test_live_wowprogress_threshold_pve_leaderboard_contract() -> None:
    require_live("WowProgress")
    payload = payload_for_live(
        runner,
        app,
        ["threshold", "pve-leaderboard", "--region", "us", "--metric", "rank", "--value", "25", "--nearest", "5", "--limit", "25"],
        provider_name="WowProgress",
    )

    assert payload["kind"] == "pve_leaderboard_threshold"
    assert payload["metric"] == "rank"
    assert payload["threshold"]["nearest_matches"]
    assert payload["threshold"]["estimate"] is not None


def test_live_wowprogress_sample_pve_guild_profiles_contract() -> None:
    require_live("WowProgress")
    payload = payload_for_live(
        runner,
        app,
        ["sample", "pve-guild-profiles", "--region", "us", "--limit", "5"],
        provider_name="WowProgress",
    )

    assert payload["kind"] == "pve_guild_profiles_sample"
    assert payload["sample"]["guild_profile_count"] == len(payload["guild_profiles"])
    assert payload["sample"]["sampling"]["source_leaderboard_entry_count"] >= payload["sample"]["sampling"]["returned_guild_profile_count"]
    assert payload["guild_profiles"]
    assert payload["guild_profiles"][0]["item_level_average"] is not None


def test_live_wowprogress_filtered_guild_profiles_contract() -> None:
    require_live("WowProgress")
    payload = payload_for_live(
        runner,
        app,
        ["sample", "pve-guild-profiles", "--region", "us", "--limit", "5", "--difficulty", "m"],
        provider_name="WowProgress",
    )

    assert payload["kind"] == "pve_guild_profiles_sample"
    assert payload["query"]["filters"]["difficulty"] == ["m"]
    assert payload["sample"]["filtering"]["source_profile_count"] >= payload["sample"]["filtering"]["returned_profile_count"]


def test_live_wowprogress_distribution_pve_guild_profiles_contract() -> None:
    require_live("WowProgress")
    payload = payload_for_live(
        runner,
        app,
        ["distribution", "pve-guild-profiles", "--region", "us", "--metric", "progress", "--limit", "5"],
        provider_name="WowProgress",
    )

    assert payload["kind"] == "pve_guild_profiles_distribution"
    assert payload["distribution"]["rows"]
    assert payload["citations"]["leaderboard_page"]


def test_live_wowprogress_threshold_pve_guild_profiles_contract() -> None:
    require_live("WowProgress")
    payload = payload_for_live(
        runner,
        app,
        ["threshold", "pve-guild-profiles", "--region", "us", "--metric", "world_rank", "--value", "25", "--nearest", "5", "--limit", "5"],
        provider_name="WowProgress",
    )

    assert payload["kind"] == "pve_guild_profiles_threshold"
    assert payload["threshold"]["nearest_matches"]
    assert payload["threshold"]["estimate"] is not None

"""Raider.IO payload builders run against captured API responses, not hand-written stubs.

The JSON under ``tests/fixtures/raiderio/`` was captured from the live Raider.IO API per
``docs/architecture/FIXTURE_MAINTENANCE.md`` (the guild roster was trimmed to twelve members, the
Mythic+ leaderboard page to its first two runs, and the character's recent runs to two; nothing else
was edited). Every value asserted here
is one Raider.IO actually sent, so a normalizer that drifts away from the real response shape fails
instead of agreeing with its own fixture.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from raiderio_cli.client import FetchedJson
from raiderio_cli.main import app as raiderio_app
from typer.testing import CliRunner

runner = CliRunner()
FIXTURE_DIR = Path(__file__).parent / "fixtures" / "raiderio"


def _captured(name: str) -> dict[str, Any]:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def test_raiderio_guild_payload_parses_a_captured_guild_profile(monkeypatch) -> None:
    profile = _captured("guild_profile_us_malganis_gn.json")
    monkeypatch.setattr(
        "raiderio_cli.client.RaiderIOClient.guild_profile",
        lambda self, *, region, realm, name, fields="": FetchedJson(
            payload=profile,
            fetched_at="2026-09-19T00:00:00+00:00",
            cache_hit=False,
        ),
    )
    result = runner.invoke(raiderio_app, ["guild", "us", "malganis", "gn"])
    assert result.exit_code == 0, result.output

    data = json.loads(result.stdout)["data"]
    assert data["guild"] == {
        "name": "gn",
        "region": "us",
        "realm": "Mal'Ganis",
        "faction": "horde",
        "profile_url": "https://raider.io/guilds/us/malganis/gn",
        "member_count": 12,
    }
    progression = {row["raid_slug"]: row for row in data["raiding"]["progression"]}
    assert progression["the-venomous-abyss"] == {
        "raid_slug": "the-venomous-abyss",
        "summary": "7/8 M",
        "total_bosses": 8,
        "normal_bosses_killed": 8,
        "heroic_bosses_killed": 8,
        "mythic_bosses_killed": 7,
    }
    rankings = {row["raid_slug"]: row for row in data["raiding"]["rankings"]}
    assert rankings["the-venomous-abyss"]["mythic"] == {"world": 35, "region": 10, "realm": 2}
    first_member = data["roster_preview"][0]
    assert first_member["name"] == "Fharg"
    assert first_member["class_spec_identity"]["identity"] == {"actor_class": "shaman", "spec": "enhancement"}
    assert first_member["class_spec_identity"]["confidence"] == "high"


def test_raiderio_raid_leaderboard_parses_a_captured_rankings_response(monkeypatch) -> None:
    rankings = _captured("raid_rankings_us_malganis_the_venomous_abyss.json")
    monkeypatch.setattr(
        "raiderio_cli.client.RaiderIOClient.raid_rankings",
        lambda self, *, raid, difficulty, region, realm=None, limit, page: FetchedJson(
            payload=rankings if page == 0 else {"raidRankings": []},
            fetched_at="2026-09-19T00:00:00+00:00",
            cache_hit=False,
        ),
    )
    result = runner.invoke(
        raiderio_app,
        ["leaderboard", "raids", "--raid", "the-venomous-abyss", "--region", "us", "--realm", "malganis", "--limit", "5"],
    )
    assert result.exit_code == 0, result.output

    data = json.loads(result.stdout)["data"]
    assert [row["rank"] for row in data["rows"]] == [1, 2, 3, 4, 5]
    assert [row["guild"]["name"] for row in data["rows"]] == ["Instant Dollars", "gn", "nVus", "Pathogen", "Void"]
    second = data["rows"][1]
    # rank is the realm position under --realm while region_rank stays region-wide.
    assert second["rank"] == 2
    assert second["region_rank"] == 10
    assert second["guild"]["realm"] == "malganis"
    assert second["guild"]["realm_name"] == "Mal'Ganis"
    assert second["guild"]["profile_url"] == "https://raider.io/guilds/us/malganis/gn"
    assert second["encounters_defeated_count"] == 7
    assert second["encounters_pulled_count"] == 8
    assert second["encounters_defeated"][0] == {
        "slug": "nekzali-the-soulcoiler",
        "first_defeated": "2026-08-25T02:41:56.000Z",
        "last_defeated": "2026-09-16T00:30:53.000Z",
    }
    assert second["encounters_pulled"][0] == {
        "slug": "nekzali-the-soulcoiler",
        "num_pulls": 2,
        "best_percent": 0,
        "is_defeated": True,
        "pull_started_at": "2026-08-25T02:33:25Z",
    }
    # No realm_rank: the captured response carries no realmRank, so the row shape does not claim one.
    assert "realm_rank" not in second


def _captured_runs_page(monkeypatch) -> None:
    """Answer every ``/mythic-plus/runs`` page with the captured first page, then nothing."""
    runs_page = _captured("mythic_plus_runs_us_page0.json")
    monkeypatch.setattr(
        "raiderio_cli.client.RaiderIOClient.mythic_plus_runs",
        lambda self, *, season, region, dungeon, affixes, page: FetchedJson(
            payload=runs_page if page == 0 else {"rankings": []},
            fetched_at="2026-09-19T00:00:00+00:00",
            cache_hit=False,
        ),
    )


def test_raiderio_leaderboard_parses_a_captured_leaderboard_page(monkeypatch) -> None:
    _captured_runs_page(monkeypatch)
    result = runner.invoke(raiderio_app, ["leaderboard", "mythic-plus", "--region", "us"])
    assert result.exit_code == 0, result.output

    payload = json.loads(result.stdout)
    data = payload["data"]
    # Raider.IO echoes the season it applied under params.season only, so this is the one place the
    # resolved season can come from when --season is omitted.
    assert payload["query"]["resolved_season"] == "season-mn-2"
    assert data["count"] == 2
    top = data["runs"][0]
    assert top["rank"] == 1
    assert top["score"] == 515.4
    assert top["mythic_level"] == 22
    assert top["dungeon_slug"] == "murder-row"
    assert top["completed_at"] == "2026-09-17T21:54:20.000Z"
    assert top["affixes"] == ["fortified", "tyrannical", "xalataths-guile"]
    # Timed by 21.6 seconds; logged_run_id is Raider.IO's own id for the logged run, not a Warcraft Logs report code.
    assert (top["clear_time_ms"], top["keystone_time_ms"], top["num_chests"]) == (2019411, 2040999, 1)
    assert top["run_id"] == 13240272
    assert top["logged_run_id"] == 3904086
    assert payload["provenance"]["citations"]["leaderboard_urls"] == [
        "https://raider.io/mythic-plus-rankings/season-mn-2/all/us/leaderboards-strict"
    ]


def test_raiderio_character_recent_runs_parse_a_captured_profile(monkeypatch) -> None:
    profile = _captured("character_profile_us_stormrage_rockystorm.json")
    monkeypatch.setattr(
        "raiderio_cli.client.RaiderIOClient.character_profile",
        lambda self, *, region, realm, name, fields="": FetchedJson(
            payload=profile,
            fetched_at="2026-09-30T07:08:00+00:00",
            cache_hit=False,
        ),
    )
    result = runner.invoke(raiderio_app, ["character", "us", "stormrage", "Rockystorm"])
    assert result.exit_code == 0, result.output

    mythic_plus = json.loads(result.stdout)["data"]["mythic_plus"]
    assert mythic_plus["recent_run_count"] == 2
    # A profile run names its dungeon as a string and calls the timer par_time_ms and the chest count
    # num_keystone_upgrades; the row reports them under the leaderboard's names.
    assert mythic_plus["recent_runs"][0] == {
        "mythic_level": 10,
        "dungeon": "Murder Row",
        "short_name": "MR",
        "completed_at": "2026-09-29T21:26:56.000Z",
        "score": 335,
        "num_chests": 3,
        "clear_time_ms": 807797,
        "keystone_time_ms": 2040999,
        "run_id": 16930919,
        "url": "https://raider.io/mythic-plus-runs/season-mn-2/16930919-10-murder-row",
    }


def test_raiderio_sample_players_parses_captured_roster_entries(monkeypatch) -> None:
    _captured_runs_page(monkeypatch)
    result = runner.invoke(raiderio_app, ["sample", "mythic-plus-players", "--region", "us", "--limit", "2"])
    assert result.exit_code == 0, result.output

    data = json.loads(result.stdout)["data"]
    assert data["sample"]["run_count"] == 2
    players = {row["name"]: row for row in data["players"]}
    tank = players["Yodadkt"]
    assert tank["realm"] == "area-52"
    assert tank["region"] == "us"
    assert tank["roles"] == ["tank"]
    assert tank["class_slugs"] == ["death-knight"]
    assert tank["spec_slugs"] == ["death-knight-blood"]
    assert tank["top_mythic_level"] == 22
    # The same five players ran both captured runs, so their snapshots merge instead of duplicating.
    assert tank["appearance_count"] == 2
    assert data["sample"]["player_count"] == 5

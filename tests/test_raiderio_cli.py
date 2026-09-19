from __future__ import annotations

import json
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import typer.main
from raiderio_cli.analytics import (
    distribution_values,
    player_distribution_values,
    player_sample_summary,
    player_snapshots,
    ranking_roster_entry,
    run_matches_filters,
)
from raiderio_cli.candidates import (
    candidate_dedupe_key,
    candidate_from_character_profile,
    dedupe_search_candidates,
    match_reasons,
    resolve_candidate_is_confident,
    resolve_confidence_label,
    search_result_candidate,
)
from raiderio_cli.client import FetchedJson, RaiderIOClient
from raiderio_cli.main import (
    PLAYER_DISTRIBUTION_METRICS,
    RUN_DISTRIBUTION_METRICS,
    THRESHOLD_METRICS,
)
from raiderio_cli.main import (
    app as raiderio_app,
)
from raiderio_cli.main import (
    run as raiderio_run,
)
from raiderio_cli.provider import PROVIDER
from typer.testing import CliRunner
from warcraft_core.analytics import numeric_summary
from warcraft_core.envelope import envelope_violations
from warcraft_core.provider import ProviderSurface

runner = CliRunner()


def _option_help(command_path: list[str], flag: str) -> str:
    """The help text of one option, isolated from the rest of the rendered help screen."""
    command: Any = typer.main.get_command(raiderio_app)
    for name in command_path:
        command = command.commands[name]
    option = next(param for param in command.params if flag in param.opts)
    return option.help or ""


def _as_fetched(
    stub: Callable[..., dict[str, Any]],
    *,
    fetched_at: str | None = None,
    cache_hit: bool = False,
) -> Callable[..., FetchedJson]:
    """Return a client stub that answers the way the real client does: the body plus its fetch time."""

    def fetch(*args: Any, **kwargs: Any) -> FetchedJson:
        return FetchedJson(
            payload=stub(*args, **kwargs),
            fetched_at=fetched_at or datetime.now(UTC).isoformat(),
            cache_hit=cache_hit,
        )

    return fetch


def _assert_read_just_now(value: str) -> None:
    """A freshness timestamp must be a real UTC instant from this run, not a placeholder string."""
    read_at = datetime.fromisoformat(value)
    assert read_at.tzinfo is not None, f"freshness timestamp is not timezone-aware: {value}"
    assert timedelta(0) <= datetime.now(UTC) - read_at < timedelta(minutes=5), value


def test_raiderio_doctor_reports_phase_one_capabilities() -> None:
    result = runner.invoke(raiderio_app, ["doctor"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["provider"] == "raiderio"
    assert payload["status"] == "ready"
    assert payload["auth"]["required"] is False
    assert payload["auth"]["deferred"] is True
    assert payload["capabilities"]["character"] == "ready"
    assert payload["capabilities"]["search"] == "ready"
    assert payload["capabilities"]["sample_mythic_plus_runs"] == "ready"
    assert payload["capabilities"]["sample_mythic_plus_players"] == "ready"
    assert payload["capabilities"]["distribution_mythic_plus_runs"] == "ready"
    assert payload["capabilities"]["distribution_mythic_plus_players"] == "ready"
    assert payload["capabilities"]["threshold_mythic_plus_runs"] == "ready"
    assert payload["capabilities"]["mythic_plus_leaderboard"] == "ready"


def test_raiderio_search_returns_ranked_matches(monkeypatch) -> None:
    monkeypatch.setattr(
        "raiderio_cli.client.RaiderIOClient.search",
        lambda self, *, term, kind=None: {
            "matches": [
                {
                    "type": "guild",
                    "name": "Liquid",
                    "data": {
                        "id": 1,
                        "name": "Liquid",
                        "displayName": "Liquid",
                        "faction": "horde",
                        "region": {"slug": "us", "name": "United States & Oceania"},
                        "realm": {"slug": "illidan", "name": "Illidan"},
                        "path": "/guilds/us/illidan/Liquid",
                    },
                },
                {
                    "type": "guild",
                    "name": "Liquid",
                    "data": {
                        "id": 2,
                        "name": "Liquid",
                        "displayName": "Liquid",
                        "faction": "alliance",
                        "region": {"slug": "us", "name": "United States & Oceania"},
                        "realm": {"slug": "gnomeregan", "name": "Gnomeregan"},
                        "path": "/guilds/us/gnomeregan/Liquid",
                    },
                },
            ]
        },
    )
    result = runner.invoke(raiderio_app, ["search", "guild Liquid", "--limit", "5"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["count"] == 2
    assert payload["results"][0]["kind"] == "guild"
    assert payload["results"][0]["realm"] == "illidan"
    assert "type_hint" in payload["results"][0]["ranking"]["match_reasons"]
    assert payload["results"][0]["follow_up"]["command"] == "raiderio guild us illidan Liquid"


def test_raiderio_search_result_candidate_builds_profile_shape() -> None:
    candidate = search_result_candidate(
        {
            "type": "guild",
            "name": "Liquid",
            "data": {
                "id": 1,
                "name": "Liquid",
                "displayName": "Liquid",
                "faction": "horde",
                "region": {"slug": "us", "name": "United States & Oceania"},
                "realm": {"slug": "illidan", "name": "Illidan"},
                "path": "/guilds/us/illidan/Liquid",
            },
        },
        query="Liquid guild",
        type_hint="guild",
    )
    assert candidate is not None
    assert candidate["kind"] == "guild"
    assert candidate["profile_url"] == "https://raider.io/guilds/us/illidan/Liquid"
    assert "type_hint" in candidate["ranking"]["match_reasons"]


def test_raiderio_candidate_dedupe_prefers_higher_score() -> None:
    first = {
        "kind": "guild",
        "region": "us",
        "realm": "illidan",
        "name": "Liquid",
        "ranking": {"score": 10},
    }
    second = {
        "kind": "guild",
        "region": "us",
        "realm": "illidan",
        "name": "Liquid",
        "ranking": {"score": 20},
    }
    assert candidate_dedupe_key(first) == ("guild", "us", "illidan", "Liquid")
    deduped = dedupe_search_candidates([first, second])
    assert len(deduped) == 1
    assert deduped[0]["ranking"]["score"] == 20


def test_raiderio_match_reasons_helper_tracks_exact_and_hints() -> None:
    score, reasons = match_reasons(
        query="us illidan Liquid",
        type_hint="guild",
        kind="guild",
        name="Liquid",
        region="us",
        realm="illidan",
    )
    assert score > 0
    assert "all_terms_match" in reasons
    assert "type_hint" in reasons


def test_raiderio_resolve_confidence_helpers() -> None:
    assert resolve_candidate_is_confident([{"ranking": {"score": 45}}]) is True
    assert resolve_candidate_is_confident([{"ranking": {"score": 44}}, {"ranking": {"score": 10}}]) is False
    assert resolve_candidate_is_confident([{"ranking": {"score": 50}}, {"ranking": {"score": 40}}]) is False
    assert resolve_confidence_label(45, resolved=True) == "high"
    assert resolve_confidence_label(30, resolved=False) == "medium"
    assert resolve_confidence_label(20, resolved=False) == "low"


def test_raiderio_ranking_roster_entry_builds_profile_summary() -> None:
    entry = ranking_roster_entry(
        {
            "character": {
                "name": "Cotti",
                "realm": {"slug": "tarren-mill"},
                "region": {"slug": "eu"},
                "class": {"name": "Druid", "slug": "druid"},
                "spec": {"name": "Balance", "slug": "balance"},
                "path": "/characters/eu/tarren-mill/Cotti",
            },
            "role": "dps",
        }
    )
    assert entry["name"] == "Cotti"
    assert entry["spec_slug"] == "balance"
    assert entry["profile_url"] == "https://raider.io/characters/eu/tarren-mill/Cotti"
    # Raw class/spec preserved alongside the additive normalized identity (high when both resolve).
    assert entry["class_name"] == "Druid"
    assert entry["spec_name"] == "Balance"
    identity = entry["class_spec_identity"]
    assert identity["kind"] == "class_spec_identity"
    assert identity["status"] == "normalized"
    assert identity["confidence"] == "high"
    assert identity["identity"] == {"actor_class": "druid", "spec": "balance"}
    assert identity["source"] == {"provider": "raiderio", "source": "ranking_roster"}


def test_raiderio_ranking_roster_entry_identity_degrades_without_spec() -> None:
    entry = ranking_roster_entry(
        {
            "character": {
                "name": "Cotti",
                "realm": {"slug": "tarren-mill"},
                "region": {"slug": "eu"},
                "class": {"name": "Druid", "slug": "druid"},
                "spec": {},
                "path": "/characters/eu/tarren-mill/Cotti",
            },
            "role": "dps",
        }
    )
    identity = entry["class_spec_identity"]
    assert identity["status"] == "normalized"
    assert identity["confidence"] == "none"
    assert identity["identity"] == {"actor_class": "druid", "spec": None}


def test_raiderio_search_character_emits_identity_guild_does_not() -> None:
    character = search_result_candidate(
        {
            "type": "character",
            "name": "Imonthegcd",
            "data": {
                "id": 7,
                "name": "Imonthegcd",
                "class": {"name": "Mage", "slug": "mage"},
                "region": {"slug": "us", "name": "United States"},
                "realm": {"slug": "illidan", "name": "Illidan"},
                "path": "/characters/us/illidan/Imonthegcd",
            },
        },
        query="us illidan Imonthegcd",
        type_hint="character",
    )
    assert character is not None
    assert character["class_name"] == "Mage"
    identity = character["class_spec_identity"]
    # Search results expose class but no spec -> class-only normalized, confidence none.
    assert identity["status"] == "normalized"
    assert identity["confidence"] == "none"
    assert identity["identity"] == {"actor_class": "mage", "spec": None}
    assert identity["source"] == {"provider": "raiderio", "source": "search_result"}

    guild = search_result_candidate(
        {
            "type": "guild",
            "name": "Liquid",
            "data": {"id": 1, "name": "Liquid", "region": {"slug": "us"}, "realm": {"slug": "illidan"}},
        },
        query="Liquid guild",
        type_hint="guild",
    )
    assert guild is not None
    assert "class_spec_identity" not in guild


def test_raiderio_candidate_from_profile_emits_identity() -> None:
    candidate = candidate_from_character_profile(
        query="us illidan Roguecane",
        type_hint="character",
        payload={"name": "Roguecane", "region": "us", "realm": "illidan", "class": "Rogue", "active_spec_name": "Subtlety"},
        query_region="us",
        query_realm="illidan",
        query_name="Roguecane",
    )
    assert candidate["class_name"] == "Rogue"
    assert candidate["active_spec_name"] == "Subtlety"
    identity = candidate["class_spec_identity"]
    assert identity["status"] == "normalized"
    assert identity["confidence"] == "high"
    assert identity["identity"] == {"actor_class": "rogue", "spec": "subtlety"}
    assert identity["source"] == {"provider": "raiderio", "source": "resolve_character_profile"}


def test_raiderio_distribution_values_cover_numeric_and_composition_metrics() -> None:
    runs = [
        {
            "mythic_level": 26,
            "dungeon": "The Dawnbreaker",
            "roster": [
                {"role": "dps", "class_slug": "druid", "spec_slug": "balance", "region": "eu"},
                {"role": "healer", "class_slug": "shaman", "spec_slug": "restoration", "region": "eu"},
            ],
        }
    ]

    values, unit, numeric = distribution_values("mythic_level", runs)
    assert values == [26]
    assert unit == "runs"
    assert numeric is True

    values, unit, numeric = distribution_values("composition", runs)
    assert values == ["dps:balance | healer:restoration"]
    assert unit == "runs"
    assert numeric is False

    values, unit, numeric = distribution_values("player_region", runs)
    assert values == ["eu", "eu"]
    assert unit == "roster_entries"
    assert numeric is False


def test_raiderio_player_distribution_values_cover_numeric_and_tag_metrics() -> None:
    players = [
        {
            "appearance_count": 2,
            "top_mythic_level": 26,
            "class_slugs": ["druid"],
            "spec_slugs": ["balance"],
            "roles": ["dps"],
            "region": "eu",
        }
    ]

    values, unit, numeric = player_distribution_values("appearance_count", players)
    assert values == [2]
    assert unit == "players"
    assert numeric is True

    values, unit, numeric = player_distribution_values("class", players)
    assert values == ["druid"]
    assert unit == "player_class_tags"
    assert numeric is False

    values, unit, numeric = player_distribution_values("unknown", players)
    assert values == ["eu"]
    assert unit == "players"
    assert numeric is False


def test_raiderio_numeric_and_player_sample_helpers() -> None:
    runs = [
        {
            "dungeon": "Cinderbrew Meadery",
            "mythic_level": 17,
            "score": 321.4,
            "completed_at": "2026-03-10T00:00:00+00:00",
            "roster": [
                {"name": "Alpha", "realm": "illidan", "region": "us", "role": "tank",
                    "class_slug": "warrior", "spec_slug": "protection-warrior"},
                {"name": "Bravo", "realm": "illidan", "region": "us", "role": "healer", "class_slug": "priest", "spec_slug": "holy-priest"},
            ],
        },
        {
            "dungeon": "Priory of the Sacred Flame",
            "mythic_level": 18,
            "score": 333.0,
            "completed_at": "2026-03-11T00:00:00+00:00",
            "roster": [
                {"name": "Alpha", "realm": "illidan", "region": "us", "role": "tank",
                    "class_slug": "warrior", "spec_slug": "protection-warrior"},
                {"name": "Charlie", "realm": "tichondrius", "region": "us", "role": "dps", "class_slug": "mage", "spec_slug": "arcane-mage"},
            ],
        },
    ]
    players = player_snapshots(runs)
    summary = player_sample_summary(
        players,
        runs=runs,
        meta={"sampled_at": "2026-03-12T00:00:00+00:00", "season": "season-tww-2", "pages_requested": 1, "pages_fetched": 1},
        filtering={"contains_class": ["warrior"]},
        player_sampling={"player_limit": 10, "source_player_count": 3,
                         "returned_player_count": 3, "excluded_player_count": 0, "truncated": False},
    )
    assert numeric_summary([17, 18]) == {"min": 17, "max": 18, "average": 17.5, "median": 17.5}
    assert summary["unique_class_count"] == 3
    assert summary["appearance_count"]["max"] == 2
    assert summary["top_mythic_level"]["max"] == 18


def test_raiderio_search_uses_structured_direct_guild_probe_when_search_is_empty(monkeypatch) -> None:
    monkeypatch.setattr("raiderio_cli.client.RaiderIOClient.search", lambda self, *, term, kind=None: {"matches": []})
    monkeypatch.setattr(
        "raiderio_cli.client.RaiderIOClient.guild_profile_variants",
        lambda self, *, region, realm, name, fields="": {
            "id": 1,
            "name": "Liquid",
            "region": "us",
            "realm": "Illidan",
            "faction": "horde",
            "profile_url": "https://raider.io/guilds/us/illidan/Liquid",
        },
    )
    monkeypatch.setattr(
        "raiderio_cli.client.RaiderIOClient.character_profile_variants",
        lambda self, *, region, realm, name, fields="": (_ for _ in ()).throw(
            httpx.HTTPStatusError(
                "not found",
                request=httpx.Request("GET", "https://raider.io/api/v1/characters/profile"),
                response=httpx.Response(404, request=httpx.Request("GET", "https://raider.io/api/v1/characters/profile")),
            )
        ),
    )

    result = runner.invoke(raiderio_app, ["search", "guild us illidan Liquid", "--limit", "5"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["count"] == 1
    assert payload["results"][0]["kind"] == "guild"
    assert payload["results"][0]["name"] == "Liquid"
    assert "structured_probe" in payload["results"][0]["ranking"]["match_reasons"]
    assert payload["results"][0]["follow_up"]["command"] == "raiderio guild us illidan Liquid"


def test_raiderio_resolve_returns_conservative_next_command(monkeypatch) -> None:
    monkeypatch.setattr(
        "raiderio_cli.client.RaiderIOClient.search",
        lambda self, *, term, kind=None: {
            "matches": [
                {
                    "type": "character",
                    "name": "Roguecane",
                    "data": {
                        "id": 39943,
                        "name": "Roguecane",
                        "faction": "horde",
                        "region": {"slug": "us", "name": "United States & Oceania"},
                        "realm": {"slug": "illidan", "name": "Illidan"},
                        "class": {"name": "Rogue", "slug": "rogue"},
                    },
                }
            ]
        },
    )
    result = runner.invoke(raiderio_app, ["resolve", "Roguecane"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["resolved"] is True
    assert payload["confidence"] == "high"
    assert payload["next_command"] == "raiderio character us illidan Roguecane"


def test_raiderio_resolve_uses_structured_direct_character_probe(monkeypatch) -> None:
    monkeypatch.setattr("raiderio_cli.client.RaiderIOClient.search", lambda self, *, term, kind=None: {"matches": []})
    monkeypatch.setattr(
        "raiderio_cli.client.RaiderIOClient.character_profile_variants",
        lambda self, *, region, realm, name, fields="": {
            "id": 39943,
            "name": "Roguecane",
            "region": "us",
            "realm": "Illidan",
            "class": "Rogue",
            "active_spec_name": "Subtlety",
            "faction": "horde",
            "profile_url": "https://raider.io/characters/us/illidan/Roguecane",
        },
    )
    monkeypatch.setattr(
        "raiderio_cli.client.RaiderIOClient.guild_profile_variants",
        lambda self, *, region, realm, name, fields="": (_ for _ in ()).throw(
            httpx.HTTPStatusError(
                "not found",
                request=httpx.Request("GET", "https://raider.io/api/v1/guilds/profile"),
                response=httpx.Response(404, request=httpx.Request("GET", "https://raider.io/api/v1/guilds/profile")),
            )
        ),
    )

    result = runner.invoke(raiderio_app, ["resolve", "character us illidan Roguecane"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["resolved"] is True
    assert payload["confidence"] == "high"
    assert payload["next_command"] == "raiderio character us illidan Roguecane"
    assert "structured_probe" in payload["match"]["ranking"]["match_reasons"]


def test_raiderio_resolve_stays_unresolved_for_ambiguous_match_set(monkeypatch) -> None:
    monkeypatch.setattr(
        "raiderio_cli.client.RaiderIOClient.search",
        lambda self, *, term, kind=None: {
            "matches": [
                {
                    "type": "guild",
                    "name": "Liquid",
                    "data": {
                        "id": 1,
                        "name": "Liquid",
                        "displayName": "Liquid",
                        "faction": "horde",
                        "region": {"slug": "us", "name": "United States & Oceania"},
                        "realm": {"slug": "illidan", "name": "Illidan"},
                        "path": "/guilds/us/illidan/Liquid",
                    },
                },
                {
                    "type": "guild",
                    "name": "Liquid",
                    "data": {
                        "id": 2,
                        "name": "Liquid",
                        "displayName": "Liquid",
                        "faction": "horde",
                        "region": {"slug": "us", "name": "United States & Oceania"},
                        "realm": {"slug": "area-52", "name": "Area 52"},
                        "path": "/guilds/us/area-52/Liquid",
                    },
                },
            ]
        },
    )
    result = runner.invoke(raiderio_app, ["resolve", "guild Liquid"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["resolved"] is False
    assert payload["next_command"] is None
    assert payload["fallback_search_command"] == 'raiderio search "Liquid"'


def test_raiderio_character_summary(monkeypatch) -> None:
    def fake_profile(self, *, region: str, realm: str, name: str, fields: str = ""):  # noqa: ANN001
        assert region == "us"
        assert realm == "illidan"
        assert name == "Roguecane"
        return {
            "name": "Roguecane",
            "region": "us",
            "realm": "Illidan",
            "race": "Blood Elf",
            "class": "Rogue",
            "active_spec_name": "Subtlety",
            "faction": "horde",
            "profile_url": "https://raider.io/characters/us/illidan/Roguecane",
            "thumbnail_url": "https://example.test/thumb.jpg",
            "guild": {"name": "Liquid", "realm": "Illidan", "region": "us"},
            "raid_progression": {
                "tier-mn-1": {
                    "summary": "3/8H",
                    "total_bosses": 8,
                    "normal_bosses_killed": 8,
                    "heroic_bosses_killed": 3,
                    "mythic_bosses_killed": 0,
                }
            },
            "mythic_plus_scores_by_season": [
                {
                    "season": "season-tww-3",
                    "scores": {"all": 1234.5},
                    "segments": {"all": {"color": "#abcdef"}},
                }
            ],
            "mythic_plus_ranks": {"overall": {"world": 50, "region": 10, "realm": 1}},
            "mythic_plus_recent_runs": [
                {
                    "mythic_level": 12,
                    "completed_at": "2026-03-10T12:00:00Z",
                    "num_chests": 2,
                    "clear_time_ms": 1200000,
                    "keystone_time_ms": 1500000,
                    "dungeon": {"name": "The Dawnbreaker", "slug": "the-dawnbreaker"},
                }
            ],
        }

    monkeypatch.setattr("raiderio_cli.client.RaiderIOClient.character_profile_variants", fake_profile)
    result = runner.invoke(raiderio_app, ["character", "us", "illidan", "Roguecane"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["character"]["name"] == "Roguecane"
    assert payload["guild"]["name"] == "Liquid"
    assert payload["mythic_plus"]["current_score"] == 1234.5
    assert payload["raiding"]["progression"][0]["raid_slug"] == "tier-mn-1"
    # Raw class/spec strings stay intact alongside the additive normalized identity.
    assert payload["character"]["class_name"] == "Rogue"
    assert payload["character"]["active_spec_name"] == "Subtlety"
    identity = payload["character"]["class_spec_identity"]
    assert identity["kind"] == "class_spec_identity"
    assert identity["status"] == "normalized"
    assert identity["identity"] == {"actor_class": "rogue", "spec": "subtlety"}
    assert identity["confidence"] == "high"
    assert identity["source"] == {"provider": "raiderio", "source": "character_profile"}


def test_raiderio_character_identity_degrades_when_class_and_spec_missing(monkeypatch) -> None:
    def fake_profile(self, *, region: str, realm: str, name: str, fields: str = ""):  # noqa: ANN001
        return {
            "name": "Roguecane",
            "region": "us",
            "realm": "Illidan",
            "profile_url": "https://raider.io/characters/us/illidan/Roguecane",
        }

    monkeypatch.setattr("raiderio_cli.client.RaiderIOClient.character_profile_variants", fake_profile)
    result = runner.invoke(raiderio_app, ["character", "us", "illidan", "Roguecane"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["character"]["class_name"] is None
    assert payload["character"]["active_spec_name"] is None
    identity = payload["character"]["class_spec_identity"]
    assert identity["status"] == "unknown"
    assert identity["identity"] == {"actor_class": None, "spec": None}
    # Missing source data must not advertise high confidence.
    assert identity["confidence"] == "none"


def test_raiderio_guild_summary(monkeypatch) -> None:
    def fake_profile(self, *, region: str, realm: str, name: str, fields: str = ""):  # noqa: ANN001
        return {
            "name": "Liquid",
            "region": "us",
            "realm": "Illidan",
            "faction": "horde",
            "profile_url": "https://raider.io/guilds/us/illidan/Liquid",
            "raid_progression": {
                "tier-mn-1": {
                    "summary": "8/8M",
                    "total_bosses": 8,
                    "normal_bosses_killed": 8,
                    "heroic_bosses_killed": 8,
                    "mythic_bosses_killed": 8,
                }
            },
            "raid_rankings": {
                "tier-mn-1": {
                    "normal": {"world": 0, "region": 0, "realm": 0},
                    "heroic": {"world": 0, "region": 0, "realm": 0},
                    "mythic": {"world": 1, "region": 1, "realm": 1},
                }
            },
            "members": [
                {"character": {"name": "Roguecane", "realm": "Illidan", "class": "Rogue", "active_spec_name": "Subtlety"}},
                {"character": {"name": "Ruinmkv", "realm": "Illidan", "class": "Paladin", "active_spec_name": "Retribution"}},
            ],
        }

    monkeypatch.setattr("raiderio_cli.client.RaiderIOClient.guild_profile_variants", fake_profile)
    result = runner.invoke(raiderio_app, ["guild", "us", "illidan", "Liquid"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["guild"]["name"] == "Liquid"
    assert payload["guild"]["member_count"] == 2
    assert payload["raiding"]["rankings"][0]["raid_slug"] == "tier-mn-1"
    assert payload["roster_preview"][0]["name"] == "Roguecane"
    # Roster preview rows carry the additive normalized identity alongside raw class/spec.
    assert payload["roster_preview"][0]["class_name"] == "Rogue"
    roster_identity = payload["roster_preview"][0]["class_spec_identity"]
    assert roster_identity["status"] == "normalized"
    assert roster_identity["confidence"] == "high"
    assert roster_identity["identity"] == {"actor_class": "rogue", "spec": "subtlety"}
    assert roster_identity["source"] == {"provider": "raiderio", "source": "guild_roster_preview"}


def test_raiderio_mythic_plus_runs_summary(monkeypatch) -> None:
    def fake_runs(self, *, season: str | None, region: str, dungeon: str, affixes: str | None, page: int):  # noqa: ANN001
        assert region == "world"
        return {
            "season": "season-tww-3",
            "region": "world",
            "dungeon": "all",
            "rankings": [
                {
                    "rank": 1,
                    "score": 581.5,
                    "run": {
                        "mythic_level": 26,
                        "completed_at": "2026-01-21T18:27:09.000Z",
                        "weekly_modifiers": [{"slug": "tyrannical"}],
                        "dungeon": {"name": "The Dawnbreaker", "slug": "the-dawnbreaker"},
                        "roster": [
                            {"character": {"name": "Cotti", "realm": {"slug": "tarren-mill"}, "region": {"slug": "eu"}}, "role": "dps"}
                        ],
                    },
                }
            ],
        }

    monkeypatch.setattr("raiderio_cli.client.RaiderIOClient.mythic_plus_runs", _as_fetched(fake_runs))
    result = runner.invoke(raiderio_app, ["mythic-plus-runs"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["count"] == 1
    assert payload["runs"][0]["rank"] == 1
    assert payload["runs"][0]["roster"][0]["name"] == "Cotti"


def test_raiderio_sample_mythic_plus_runs(monkeypatch) -> None:
    def fake_runs(self, *, season: str | None, region: str, dungeon: str, affixes: str | None, page: int):  # noqa: ANN001
        rows = {
            0: [
                {
                    "rank": 1,
                    "score": 580.0,
                    "run": {
                        "keystone_run_id": 1001,
                        "season": "season-tww-3",
                        "mythic_level": 26,
                        "completed_at": "2026-01-21T18:27:09.000Z",
                        "clear_time_ms": 1200000,
                        "keystone_time_ms": 1500000,
                        "num_chests": 2,
                        "weekly_modifiers": [{"slug": "tyrannical"}],
                        "dungeon": {"name": "The Dawnbreaker", "slug": "the-dawnbreaker"},
                        "roster": [
                            {
                                "character": {
                                    "name": "Cotti",
                                    "realm": {"slug": "tarren-mill"},
                                    "region": {"slug": "eu"},
                                    "class": {"name": "Druid", "slug": "druid"},
                                    "spec": {"name": "Balance", "slug": "balance"},
                                    "path": "/characters/eu/tarren-mill/Cotti",
                                },
                                "role": "dps",
                            },
                            {
                                "character": {
                                    "name": "Meowfreak",
                                    "realm": {"slug": "tarren-mill"},
                                    "region": {"slug": "eu"},
                                    "class": {"name": "Demon Hunter", "slug": "demon-hunter"},
                                    "spec": {"name": "Vengeance", "slug": "vengeance"},
                                    "path": "/characters/eu/tarren-mill/Meowfreak",
                                },
                                "role": "tank",
                            },
                        ],
                    },
                },
                {
                    "rank": 2,
                    "score": 575.0,
                    "run": {
                        "keystone_run_id": 1002,
                        "season": "season-tww-3",
                        "mythic_level": 25,
                        "completed_at": "2026-01-21T18:30:09.000Z",
                        "clear_time_ms": 1210000,
                        "keystone_time_ms": 1500000,
                        "num_chests": 1,
                        "weekly_modifiers": [{"slug": "tyrannical"}],
                        "dungeon": {"name": "Operation: Floodgate", "slug": "operation-floodgate"},
                        "roster": [
                            {
                                "character": {
                                    "name": "Meowtide",
                                    "realm": {"slug": "sylvanas"},
                                    "region": {"slug": "eu"},
                                    "class": {"name": "Shaman", "slug": "shaman"},
                                    "spec": {"name": "Restoration", "slug": "restoration"},
                                    "path": "/characters/eu/sylvanas/Meowtide",
                                },
                                "role": "healer",
                            },
                            {
                                "character": {
                                    "name": "Solanis",
                                    "realm": {"slug": "sylvanas"},
                                    "region": {"slug": "eu"},
                                    "class": {"name": "Mage", "slug": "mage"},
                                    "spec": {"name": "Frost", "slug": "frost"},
                                    "path": "/characters/eu/sylvanas/Solanis",
                                },
                                "role": "dps",
                            },
                        ],
                    },
                },
            ],
            1: [
                {
                    "rank": 3,
                    "score": 570.0,
                    "run": {
                        "keystone_run_id": 1003,
                        "season": "season-tww-3",
                        "mythic_level": 25,
                        "completed_at": "2026-01-22T18:27:09.000Z",
                        "clear_time_ms": 1220000,
                        "keystone_time_ms": 1500000,
                        "num_chests": 1,
                        "weekly_modifiers": [{"slug": "tyrannical"}],
                        "dungeon": {"name": "The Dawnbreaker", "slug": "the-dawnbreaker"},
                        "roster": [
                            {"character": {"name": "Azunazx", "realm": {"slug": "hyjal"}, "region": {"slug": "us"}}, "role": "dps"},
                            {"character": {"name": "Yodadhz", "realm": {"slug": "zuljin"}, "region": {"slug": "us"}}, "role": "tank"},
                        ],
                    },
                }
            ],
        }
        return {
            "season": "season-tww-3",
            "leaderboard_url": f"https://raider.io/mythic-plus-runs/season-tww-3/world/all/{page}",
            "rankings": rows.get(page, []),
        }

    monkeypatch.setattr("raiderio_cli.client.RaiderIOClient.mythic_plus_runs", _as_fetched(fake_runs))
    result = runner.invoke(raiderio_app, ["sample", "mythic-plus-runs", "--pages", "2", "--limit", "3"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["kind"] == "mythic_plus_runs_sample"
    assert payload["query"]["pages"] == 2
    assert payload["sample"]["pages_fetched"] == 2
    assert payload["sample"]["run_count"] == 3
    assert payload["sample"]["unique_player_count"] == 6
    assert payload["sample"]["mythic_level"]["max"] == 26
    assert payload["citations"]["leaderboard_urls"][0].startswith("https://raider.io/mythic-plus-runs/")


def test_raiderio_sample_mythic_plus_players(monkeypatch) -> None:
    def fake_runs(self, *, season: str | None, region: str, dungeon: str, affixes: str | None, page: int):  # noqa: ANN001
        return {
            "season": "season-tww-3",
            "leaderboard_url": "https://raider.io/mythic-plus-runs/season-tww-3/world/all/0",
            "rankings": [
                {
                    "rank": 1,
                    "score": 580.0,
                    "run": {
                        "keystone_run_id": 1001,
                        "season": "season-tww-3",
                        "mythic_level": 26,
                        "completed_at": "2026-01-21T18:27:09.000Z",
                        "weekly_modifiers": [{"slug": "tyrannical"}],
                        "dungeon": {"name": "The Dawnbreaker", "slug": "the-dawnbreaker"},
                        "roster": [
                            {
                                "character": {
                                    "name": "Cotti",
                                    "realm": {"slug": "tarren-mill"},
                                    "region": {"slug": "eu"},
                                    "class": {"name": "Druid", "slug": "druid"},
                                    "spec": {"name": "Balance", "slug": "balance"},
                                    "path": "/characters/eu/tarren-mill/Cotti",
                                },
                                "role": "dps",
                            },
                            {
                                "character": {
                                    "name": "Meowtide",
                                    "realm": {"slug": "sylvanas"},
                                    "region": {"slug": "eu"},
                                    "class": {"name": "Shaman", "slug": "shaman"},
                                    "spec": {"name": "Restoration", "slug": "restoration"},
                                    "path": "/characters/eu/sylvanas/Meowtide",
                                },
                                "role": "healer",
                            },
                        ],
                    },
                },
                {
                    "rank": 2,
                    "score": 575.0,
                    "run": {
                        "keystone_run_id": 1002,
                        "season": "season-tww-3",
                        "mythic_level": 25,
                        "completed_at": "2026-01-21T18:30:09.000Z",
                        "weekly_modifiers": [{"slug": "tyrannical"}],
                        "dungeon": {"name": "Operation: Floodgate", "slug": "operation-floodgate"},
                        "roster": [
                            {
                                "character": {
                                    "name": "Cotti",
                                    "realm": {"slug": "tarren-mill"},
                                    "region": {"slug": "eu"},
                                    "class": {"name": "Druid", "slug": "druid"},
                                    "spec": {"name": "Balance", "slug": "balance"},
                                    "path": "/characters/eu/tarren-mill/Cotti",
                                },
                                "role": "dps",
                            }
                        ],
                    },
                },
            ],
        }

    monkeypatch.setattr("raiderio_cli.client.RaiderIOClient.mythic_plus_runs", _as_fetched(fake_runs))
    result = runner.invoke(raiderio_app, ["sample", "mythic-plus-players", "--player-limit", "10"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["kind"] == "mythic_plus_players_sample"
    assert payload["sample"]["player_count"] == 2
    assert payload["sample"]["appearance_count"]["max"] == 2
    assert payload["sample"]["player_sampling"]["source_player_count"] == 2
    assert payload["sample"]["player_sampling"]["truncated"] is False
    assert payload["players"][0]["name"] == "Cotti"
    assert payload["players"][0]["appearance_count"] == 2
    assert payload["players"][0]["top_mythic_level"] == 26


def test_raiderio_sample_mythic_plus_players_reports_truncation(monkeypatch) -> None:
    def fake_runs(self, *, season: str | None, region: str, dungeon: str, affixes: str | None, page: int):  # noqa: ANN001
        return {
            "season": "season-tww-3",
            "leaderboard_url": "https://raider.io/mythic-plus-runs/season-tww-3/world/all/0",
            "rankings": [
                {
                    "rank": 1,
                    "score": 580.0,
                    "run": {
                        "keystone_run_id": 1001,
                        "season": "season-tww-3",
                        "mythic_level": 26,
                        "completed_at": "2026-01-21T18:27:09.000Z",
                        "weekly_modifiers": [{"slug": "tyrannical"}],
                        "dungeon": {"name": "The Dawnbreaker", "slug": "the-dawnbreaker"},
                        "roster": [
                            {
                                "character": {
                                    "name": "Cotti",
                                    "realm": {"slug": "tarren-mill"},
                                    "region": {"slug": "eu"},
                                    "class": {"name": "Druid", "slug": "druid"},
                                    "spec": {"name": "Balance", "slug": "balance"},
                                    "path": "/characters/eu/tarren-mill/Cotti",
                                },
                                "role": "dps",
                            },
                            {
                                "character": {
                                    "name": "Meowtide",
                                    "realm": {"slug": "sylvanas"},
                                    "region": {"slug": "eu"},
                                    "class": {"name": "Shaman", "slug": "shaman"},
                                    "spec": {"name": "Restoration", "slug": "restoration"},
                                    "path": "/characters/eu/sylvanas/Meowtide",
                                },
                                "role": "healer",
                            },
                        ],
                    },
                }
            ],
        }

    monkeypatch.setattr("raiderio_cli.client.RaiderIOClient.mythic_plus_runs", _as_fetched(fake_runs))
    result = runner.invoke(raiderio_app, ["sample", "mythic-plus-players", "--player-limit", "1"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["sample"]["player_sampling"]["source_player_count"] == 2
    assert payload["sample"]["player_sampling"]["returned_player_count"] == 1
    assert payload["sample"]["player_sampling"]["truncated"] is True
    assert payload["sample"]["player_sampling"]["excluded_player_count"] == 1


def test_raiderio_run_matches_filters_with_normalized_roster_fields() -> None:
    run = {
        "mythic_level": 26,
        "score": 581.5,
        "roster": [
            {
                "role": "dps",
                "class_name": "Demon Hunter",
                "spec_name": "Havoc",
                "region": "EU",
            }
        ],
    }

    assert (
        run_matches_filters(
            run,
            level_min=25,
            level_max=27,
            score_min=580.0,
            score_max=590.0,
            contains_role=["dps"],
            contains_class=["demon-hunter"],
            contains_spec=["havoc"],
            player_region=["eu"],
        )
        is True
    )
    assert (
        run_matches_filters(
            run,
            level_min=27,
            level_max=None,
            score_min=None,
            score_max=None,
            contains_role=[],
            contains_class=[],
            contains_spec=[],
            player_region=[],
        )
        is False
    )


def test_raiderio_run_filter_bounds_are_inclusive_at_the_boundary() -> None:
    # --level-min/--score-min are documented as "at or above", so a run sitting exactly on the
    # threshold is retained; the same holds for the --max side.
    run = {"mythic_level": 25, "score": 580.0, "roster": []}
    assert (
        run_matches_filters(
            run,
            level_min=25,
            level_max=25,
            score_min=580.0,
            score_max=580.0,
            contains_role=[],
            contains_class=[],
            contains_spec=[],
            player_region=[],
        )
        is True
    )


@pytest.mark.parametrize(
    ("bounds", "expected"),
    [
        ({"level_min": 20}, False),
        ({"level_max": 30}, False),
        ({"score_min": 100.0}, False),
        ({}, True),
    ],
)
def test_raiderio_run_with_a_missing_metric_is_excluded_only_when_that_bound_is_set(
    bounds: dict[str, float], expected: bool
) -> None:
    # A run whose level/score Raider.IO left out cannot be claimed to sit inside a requested range,
    # so it is excluded (and counted in filtering.excluded_run_count) rather than silently retained.
    run = {"mythic_level": None, "score": None, "roster": []}
    defaults = {"level_min": None, "level_max": None, "score_min": None, "score_max": None}
    assert (
        run_matches_filters(
            run,
            **{**defaults, **bounds},
            contains_role=[],
            contains_class=[],
            contains_spec=[],
            player_region=[],
        )
        is expected
    )


def test_raiderio_player_snapshots_merge_repeated_roster_entries() -> None:
    runs = [
        {
            "mythic_level": 26,
            "score": 581.5,
            "completed_at": "2026-01-21T18:27:09.000Z",
            "dungeon": "The Dawnbreaker",
            "dungeon_slug": "the-dawnbreaker",
            "roster": [
                {
                    "name": "Cotti",
                    "realm": "tarren-mill",
                    "region": "eu",
                    "role": "dps",
                    "class_name": "Druid",
                    "spec_name": "Balance",
                    "profile_url": "https://raider.io/characters/eu/tarren-mill/Cotti",
                }
            ],
        },
        {
            "mythic_level": 25,
            "score": 575.0,
            "completed_at": "2026-01-21T18:30:09.000Z",
            "dungeon": "Operation: Floodgate",
            "dungeon_slug": "operation-floodgate",
            "roster": [
                {
                    "name": "Cotti",
                    "realm": "tarren-mill",
                    "region": "eu",
                    "role": "dps",
                    "class_slug": "druid",
                    "spec_slug": "balance",
                    "profile_url": "https://raider.io/characters/eu/tarren-mill/Cotti",
                }
            ],
        },
    ]

    snapshots = player_snapshots(runs)

    assert len(snapshots) == 1
    assert snapshots[0]["name"] == "Cotti"
    assert snapshots[0]["appearance_count"] == 2
    assert snapshots[0]["top_mythic_level"] == 26
    assert snapshots[0]["top_score"] == 581.5
    assert snapshots[0]["latest_completed_at"] == "2026-01-21T18:30:09.000Z"
    assert snapshots[0]["class_slugs"] == ["druid"]
    assert snapshots[0]["spec_slugs"] == ["balance"]
    assert snapshots[0]["dungeon_slugs"] == ["the-dawnbreaker", "operation-floodgate"]


def test_raiderio_distribution_mythic_plus_runs(monkeypatch) -> None:
    def fake_runs(self, *, season: str | None, region: str, dungeon: str, affixes: str | None, page: int):  # noqa: ANN001
        return {
            "season": "season-tww-3",
            "leaderboard_url": "https://raider.io/mythic-plus-runs/season-tww-3/world/all/0",
            "rankings": [
                {
                    "rank": 1,
                    "score": 580.0,
                    "run": {
                        "keystone_run_id": 1001,
                        "season": "season-tww-3",
                        "mythic_level": 26,
                        "completed_at": "2026-01-21T18:27:09.000Z",
                        "weekly_modifiers": [{"slug": "tyrannical"}],
                        "dungeon": {"name": "The Dawnbreaker", "slug": "the-dawnbreaker"},
                        "roster": [
                            {
                                "character": {
                                    "name": "Cotti",
                                    "realm": {"slug": "tarren-mill"},
                                    "region": {"slug": "eu"},
                                    "class": {"name": "Druid", "slug": "druid"},
                                    "spec": {"name": "Balance", "slug": "balance"},
                                    "path": "/characters/eu/tarren-mill/Cotti",
                                },
                                "role": "dps",
                            },
                            {
                                "character": {
                                    "name": "Meowfreak",
                                    "realm": {"slug": "tarren-mill"},
                                    "region": {"slug": "eu"},
                                    "class": {"name": "Demon Hunter", "slug": "demon-hunter"},
                                    "spec": {"name": "Vengeance", "slug": "vengeance"},
                                    "path": "/characters/eu/tarren-mill/Meowfreak",
                                },
                                "role": "tank",
                            },
                        ],
                    },
                },
                {
                    "rank": 2,
                    "score": 575.0,
                    "run": {
                        "keystone_run_id": 1002,
                        "season": "season-tww-3",
                        "mythic_level": 25,
                        "completed_at": "2026-01-21T18:30:09.000Z",
                        "weekly_modifiers": [{"slug": "tyrannical"}],
                        "dungeon": {"name": "The Dawnbreaker", "slug": "the-dawnbreaker"},
                        "roster": [
                            {
                                "character": {
                                    "name": "Meowtide",
                                    "realm": {"slug": "sylvanas"},
                                    "region": {"slug": "eu"},
                                    "class": {"name": "Shaman", "slug": "shaman"},
                                    "spec": {"name": "Restoration", "slug": "restoration"},
                                    "path": "/characters/eu/sylvanas/Meowtide",
                                },
                                "role": "healer",
                            },
                            {
                                "character": {
                                    "name": "Solanis",
                                    "realm": {"slug": "sylvanas"},
                                    "region": {"slug": "eu"},
                                    "class": {"name": "Mage", "slug": "mage"},
                                    "spec": {"name": "Frost", "slug": "frost"},
                                    "path": "/characters/eu/sylvanas/Solanis",
                                },
                                "role": "dps",
                            },
                        ],
                    },
                },
            ],
        }

    monkeypatch.setattr("raiderio_cli.client.RaiderIOClient.mythic_plus_runs", _as_fetched(fake_runs))
    level_result = runner.invoke(raiderio_app, ["distribution", "mythic-plus-runs", "--metric", "mythic_level"])
    assert level_result.exit_code == 0
    level_payload = json.loads(level_result.stdout)
    assert level_payload["distribution"]["unit"] == "runs"
    assert level_payload["distribution"]["statistics"]["max"] == 26
    assert level_payload["distribution"]["rows"][0]["value"] in {"25", "26"}

    role_result = runner.invoke(raiderio_app, ["distribution", "mythic-plus-runs", "--metric", "role"])
    assert role_result.exit_code == 0
    role_payload = json.loads(role_result.stdout)
    assert role_payload["distribution"]["unit"] == "roster_entries"
    assert role_payload["distribution"]["rows"][0]["value"] == "dps"

    spec_result = runner.invoke(raiderio_app, ["distribution", "mythic-plus-runs", "--metric", "spec"])
    assert spec_result.exit_code == 0
    spec_payload = json.loads(spec_result.stdout)
    assert spec_payload["distribution"]["unit"] == "roster_entries"
    assert spec_payload["distribution"]["rows"][0]["value"] in {"balance", "frost", "restoration", "vengeance"}

    class_result = runner.invoke(raiderio_app, ["distribution", "mythic-plus-runs", "--metric", "class"])
    assert class_result.exit_code == 0
    class_payload = json.loads(class_result.stdout)
    assert class_payload["distribution"]["unit"] == "roster_entries"
    assert class_payload["distribution"]["rows"][0]["value"] in {"druid", "demon-hunter", "shaman", "mage"}

    comp_result = runner.invoke(raiderio_app, ["distribution", "mythic-plus-runs", "--metric", "composition"])
    assert comp_result.exit_code == 0
    comp_payload = json.loads(comp_result.stdout)
    assert comp_payload["distribution"]["unit"] == "runs"
    assert len(comp_payload["distribution"]["rows"]) >= 1


def test_raiderio_distribution_mythic_plus_players(monkeypatch) -> None:
    def fake_runs(self, *, season: str | None, region: str, dungeon: str, affixes: str | None, page: int):  # noqa: ANN001
        return {
            "season": "season-tww-3",
            "leaderboard_url": "https://raider.io/mythic-plus-runs/season-tww-3/world/all/0",
            "rankings": [
                {
                    "rank": 1,
                    "score": 580.0,
                    "run": {
                        "keystone_run_id": 1001,
                        "season": "season-tww-3",
                        "mythic_level": 26,
                        "completed_at": "2026-01-21T18:27:09.000Z",
                        "weekly_modifiers": [{"slug": "tyrannical"}],
                        "dungeon": {"name": "The Dawnbreaker", "slug": "the-dawnbreaker"},
                        "roster": [
                            {
                                "character": {
                                    "name": "Cotti",
                                    "realm": {"slug": "tarren-mill"},
                                    "region": {"slug": "eu"},
                                    "class": {"name": "Druid", "slug": "druid"},
                                    "spec": {"name": "Balance", "slug": "balance"},
                                    "path": "/characters/eu/tarren-mill/Cotti",
                                },
                                "role": "dps",
                            },
                            {
                                "character": {
                                    "name": "Meowtide",
                                    "realm": {"slug": "sylvanas"},
                                    "region": {"slug": "eu"},
                                    "class": {"name": "Shaman", "slug": "shaman"},
                                    "spec": {"name": "Restoration", "slug": "restoration"},
                                    "path": "/characters/eu/sylvanas/Meowtide",
                                },
                                "role": "healer",
                            },
                        ],
                    },
                },
                {
                    "rank": 2,
                    "score": 575.0,
                    "run": {
                        "keystone_run_id": 1002,
                        "season": "season-tww-3",
                        "mythic_level": 25,
                        "completed_at": "2026-01-21T18:30:09.000Z",
                        "weekly_modifiers": [{"slug": "tyrannical"}],
                        "dungeon": {"name": "Operation: Floodgate", "slug": "operation-floodgate"},
                        "roster": [
                            {
                                "character": {
                                    "name": "Cotti",
                                    "realm": {"slug": "tarren-mill"},
                                    "region": {"slug": "eu"},
                                    "class": {"name": "Druid", "slug": "druid"},
                                    "spec": {"name": "Balance", "slug": "balance"},
                                    "path": "/characters/eu/tarren-mill/Cotti",
                                },
                                "role": "dps",
                            }
                        ],
                    },
                },
            ],
        }

    monkeypatch.setattr("raiderio_cli.client.RaiderIOClient.mythic_plus_runs", _as_fetched(fake_runs))
    result = runner.invoke(raiderio_app, ["distribution", "mythic-plus-players", "--metric", "appearance_count"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["kind"] == "mythic_plus_players_distribution"
    assert payload["distribution"]["unit"] == "players"
    assert payload["distribution"]["statistics"]["max"] == 2
    assert payload["distribution"]["rows"][0]["value"] in {"1", "2"}
    assert payload["sample"]["player_sampling"]["source_player_count"] == 2

    class_result = runner.invoke(raiderio_app, ["distribution", "mythic-plus-players", "--metric", "class"])
    assert class_result.exit_code == 0
    class_payload = json.loads(class_result.stdout)
    assert class_payload["distribution"]["unit"] == "player_class_tags"
    assert class_payload["distribution"]["rows"][0]["value"] in {"druid", "shaman"}


def test_raiderio_distribution_rejects_unknown_metric() -> None:
    """An unknown --metric is rejected before any request, with the usage exit code and an envelope."""
    result = runner.invoke(raiderio_app, ["distribution", "mythic-plus-runs", "--metric", "unknown"])
    assert result.exit_code == 2
    assert result.stdout == ""
    payload = json.loads(result.stderr)
    assert payload["ok"] is False
    assert payload["provider"] == "raiderio"
    assert payload["schema_version"] == "1"
    assert payload["error"]["code"] == "invalid_query"

    player_result = runner.invoke(raiderio_app, ["distribution", "mythic-plus-players", "--metric", "unknown"])
    assert player_result.exit_code == 2
    player_payload = json.loads(player_result.stderr)
    assert player_payload["error"]["code"] == "invalid_query"


@pytest.mark.parametrize(
    ("args", "metrics"),
    [
        (["distribution", "mythic-plus-runs"], RUN_DISTRIBUTION_METRICS),
        (["distribution", "mythic-plus-players"], PLAYER_DISTRIBUTION_METRICS),
        (["threshold", "mythic-plus-runs"], THRESHOLD_METRICS),
    ],
)
def test_raiderio_metric_help_lists_every_metric_the_command_accepts(args: list[str], metrics: tuple[str, ...]) -> None:
    # --help (and the reference generated from it) used to advertise four of the eight run metrics,
    # so class/spec/composition breakdowns looked unavailable. Only the --metric option's own help
    # counts: reading the rest of the help screen lets neighbouring flags (--dungeon, --score-min)
    # stand in for metric names.
    flag_help = _option_help(args, "--metric")
    for metric in metrics:
        assert metric in flag_help, f"{metric} missing from `{' '.join(args)} --metric` help: {flag_help!r}"


def test_raiderio_threshold_mythic_plus_runs(monkeypatch) -> None:
    def fake_runs(self, *, season: str | None, region: str, dungeon: str, affixes: str | None, page: int):  # noqa: ANN001
        return {
            "season": "season-tww-3",
            "leaderboard_url": "https://raider.io/mythic-plus-runs/season-tww-3/world/all/0",
            "rankings": [
                {
                    "rank": 1,
                    "score": 581.5,
                    "run": {
                        "keystone_run_id": 1001,
                        "season": "season-tww-3",
                        "mythic_level": 26,
                        "completed_at": "2026-01-21T18:27:09.000Z",
                        "weekly_modifiers": [{"slug": "tyrannical"}],
                        "dungeon": {"name": "The Dawnbreaker", "slug": "the-dawnbreaker"},
                        "roster": [
                            {
                                "character": {
                                    "name": "Cotti",
                                    "realm": {"slug": "tarren-mill"},
                                    "region": {"slug": "eu"},
                                    "class": {"name": "Druid", "slug": "druid"},
                                    "spec": {"name": "Balance", "slug": "balance"},
                                    "path": "/characters/eu/tarren-mill/Cotti",
                                },
                                "role": "dps",
                            }
                        ],
                    },
                },
                {
                    "rank": 2,
                    "score": 560.0,
                    "run": {
                        "keystone_run_id": 1002,
                        "season": "season-tww-3",
                        "mythic_level": 25,
                        "completed_at": "2026-01-21T18:30:09.000Z",
                        "weekly_modifiers": [{"slug": "tyrannical"}],
                        "dungeon": {"name": "Operation: Floodgate", "slug": "operation-floodgate"},
                        "roster": [
                            {
                                "character": {
                                    "name": "Meowtide",
                                    "realm": {"slug": "sylvanas"},
                                    "region": {"slug": "eu"},
                                    "class": {"name": "Shaman", "slug": "shaman"},
                                    "spec": {"name": "Restoration", "slug": "restoration"},
                                    "path": "/characters/eu/sylvanas/Meowtide",
                                },
                                "role": "healer",
                            }
                        ],
                    },
                },
            ],
        }

    monkeypatch.setattr("raiderio_cli.client.RaiderIOClient.mythic_plus_runs", _as_fetched(fake_runs))
    score_result = runner.invoke(
        raiderio_app,
        ["threshold", "mythic-plus-runs", "--metric", "score", "--value", "560", "--nearest", "2"],
    )
    assert score_result.exit_code == 0
    score_payload = json.loads(score_result.stdout)
    assert score_payload["threshold"]["nearest_match_count"] == 2
    assert score_payload["threshold"]["estimate"]["metric"] == "mythic_level"
    assert score_payload["threshold"]["nearest_matches"][0]["value"] == 560.0

    level_result = runner.invoke(
        raiderio_app,
        ["threshold", "mythic-plus-runs", "--metric", "mythic_level", "--value", "25", "--nearest", "2"],
    )
    assert level_result.exit_code == 0
    level_payload = json.loads(level_result.stdout)
    assert level_payload["threshold"]["estimate"]["metric"] == "score"


def test_raiderio_threshold_rejects_unknown_metric() -> None:
    result = runner.invoke(raiderio_app, ["threshold", "mythic-plus-runs", "--metric", "rating", "--value", "3000"])
    assert result.exit_code == 2
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_query"


def test_raiderio_sample_mythic_plus_runs_filters(monkeypatch) -> None:
    def fake_runs(self, *, season: str | None, region: str, dungeon: str, affixes: str | None, page: int):  # noqa: ANN001
        return {
            "season": "season-tww-3",
            "leaderboard_url": "https://raider.io/mythic-plus-runs/season-tww-3/world/all/0",
            "rankings": [
                {
                    "rank": 1,
                    "score": 581.5,
                    "run": {
                        "keystone_run_id": 1001,
                        "season": "season-tww-3",
                        "mythic_level": 26,
                        "completed_at": "2026-01-21T18:27:09.000Z",
                        "weekly_modifiers": [{"slug": "tyrannical"}],
                        "dungeon": {"name": "The Dawnbreaker", "slug": "the-dawnbreaker"},
                        "roster": [
                            {
                                "character": {
                                    "name": "Cotti",
                                    "realm": {"slug": "tarren-mill"},
                                    "region": {"slug": "eu"},
                                    "class": {"name": "Druid", "slug": "druid"},
                                    "spec": {"name": "Balance", "slug": "balance"},
                                    "path": "/characters/eu/tarren-mill/Cotti",
                                },
                                "role": "dps",
                            }
                        ],
                    },
                },
                {
                    "rank": 2,
                    "score": 560.0,
                    "run": {
                        "keystone_run_id": 1002,
                        "season": "season-tww-3",
                        "mythic_level": 24,
                        "completed_at": "2026-01-21T18:30:09.000Z",
                        "weekly_modifiers": [{"slug": "tyrannical"}],
                        "dungeon": {"name": "Operation: Floodgate", "slug": "operation-floodgate"},
                        "roster": [
                            {
                                "character": {
                                    "name": "Meowtide",
                                    "realm": {"slug": "sylvanas"},
                                    "region": {"slug": "eu"},
                                    "class": {"name": "Shaman", "slug": "shaman"},
                                    "spec": {"name": "Restoration", "slug": "restoration"},
                                    "path": "/characters/eu/sylvanas/Meowtide",
                                },
                                "role": "healer",
                            }
                        ],
                    },
                },
            ],
        }

    monkeypatch.setattr("raiderio_cli.client.RaiderIOClient.mythic_plus_runs", _as_fetched(fake_runs))
    result = runner.invoke(
        raiderio_app,
        [
            "sample",
            "mythic-plus-runs",
            "--limit",
            "10",
            "--level-min",
            "25",
            "--contains-spec",
            "balance",
            "--contains-role",
            "dps",
            "--player-region",
            "eu",
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["query"]["filters"]["level_min"] == 25
    assert payload["query"]["filters"]["contains_spec"] == ["balance"]
    assert payload["sample"]["filtering"]["source_run_count"] == 2
    assert payload["sample"]["filtering"]["returned_run_count"] == 1
    assert payload["sample"]["filtering"]["excluded_run_count"] == 1
    assert payload["runs"][0]["mythic_level"] == 26


def test_raiderio_distribution_mythic_plus_runs_filters(monkeypatch) -> None:
    def fake_runs(self, *, season: str | None, region: str, dungeon: str, affixes: str | None, page: int):  # noqa: ANN001
        return {
            "season": "season-tww-3",
            "leaderboard_url": "https://raider.io/mythic-plus-runs/season-tww-3/world/all/0",
            "rankings": [
                {
                    "rank": 1,
                    "score": 581.5,
                    "run": {
                        "keystone_run_id": 1001,
                        "season": "season-tww-3",
                        "mythic_level": 26,
                        "completed_at": "2026-01-21T18:27:09.000Z",
                        "weekly_modifiers": [{"slug": "tyrannical"}],
                        "dungeon": {"name": "The Dawnbreaker", "slug": "the-dawnbreaker"},
                        "roster": [
                            {
                                "character": {
                                    "name": "Cotti",
                                    "realm": {"slug": "tarren-mill"},
                                    "region": {"slug": "eu"},
                                    "class": {"name": "Druid", "slug": "druid"},
                                    "spec": {"name": "Balance", "slug": "balance"},
                                    "path": "/characters/eu/tarren-mill/Cotti",
                                },
                                "role": "dps",
                            }
                        ],
                    },
                },
                {
                    "rank": 2,
                    "score": 560.0,
                    "run": {
                        "keystone_run_id": 1002,
                        "season": "season-tww-3",
                        "mythic_level": 25,
                        "completed_at": "2026-01-21T18:30:09.000Z",
                        "weekly_modifiers": [{"slug": "tyrannical"}],
                        "dungeon": {"name": "Operation: Floodgate", "slug": "operation-floodgate"},
                        "roster": [
                            {
                                "character": {
                                    "name": "Meowtide",
                                    "realm": {"slug": "sylvanas"},
                                    "region": {"slug": "us"},
                                    "class": {"name": "Shaman", "slug": "shaman"},
                                    "spec": {"name": "Restoration", "slug": "restoration"},
                                    "path": "/characters/us/sylvanas/Meowtide",
                                },
                                "role": "healer",
                            }
                        ],
                    },
                },
            ],
        }

    monkeypatch.setattr("raiderio_cli.client.RaiderIOClient.mythic_plus_runs", _as_fetched(fake_runs))
    result = runner.invoke(
        raiderio_app,
        ["distribution", "mythic-plus-runs", "--metric", "class", "--player-region", "eu", "--contains-class", "druid"],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["query"]["filters"]["player_region"] == ["eu"]
    assert payload["query"]["filters"]["contains_class"] == ["druid"]
    assert payload["sample"]["filtering"]["returned_run_count"] == 1
    assert payload["distribution"]["rows"][0]["value"] == "druid"


def test_raiderio_threshold_mythic_plus_runs_filters_to_empty_sample(monkeypatch) -> None:
    def fake_runs(self, *, season: str | None, region: str, dungeon: str, affixes: str | None, page: int):  # noqa: ANN001
        return {
            "season": "season-tww-3",
            "leaderboard_url": "https://raider.io/mythic-plus-runs/season-tww-3/world/all/0",
            "rankings": [
                {
                    "rank": 1,
                    "score": 581.5,
                    "run": {
                        "keystone_run_id": 1001,
                        "season": "season-tww-3",
                        "mythic_level": 26,
                        "completed_at": "2026-01-21T18:27:09.000Z",
                        "weekly_modifiers": [{"slug": "tyrannical"}],
                        "dungeon": {"name": "The Dawnbreaker", "slug": "the-dawnbreaker"},
                        "roster": [
                            {
                                "character": {
                                    "name": "Cotti",
                                    "realm": {"slug": "tarren-mill"},
                                    "region": {"slug": "eu"},
                                    "class": {"name": "Druid", "slug": "druid"},
                                    "spec": {"name": "Balance", "slug": "balance"},
                                    "path": "/characters/eu/tarren-mill/Cotti",
                                },
                                "role": "dps",
                            }
                        ],
                    },
                }
            ],
        }

    monkeypatch.setattr("raiderio_cli.client.RaiderIOClient.mythic_plus_runs", _as_fetched(fake_runs))
    result = runner.invoke(
        raiderio_app,
        ["threshold", "mythic-plus-runs", "--metric", "score", "--value", "560", "--contains-spec", "restoration"],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["sample"]["filtering"]["returned_run_count"] == 0
    assert payload["threshold"]["nearest_match_count"] == 0
    assert payload["threshold"]["estimate"] is None


def test_raiderio_http_error_maps_to_structured_error(monkeypatch) -> None:
    request = httpx.Request("GET", "https://raider.io/api/v1/characters/profile")
    response = httpx.Response(404, request=request, json={"message": "Character not found"})

    def fake_profile(self, *, region: str, realm: str, name: str, fields: str = ""):  # noqa: ANN001
        raise httpx.HTTPStatusError("not found", request=request, response=response)

    monkeypatch.setattr("raiderio_cli.client.RaiderIOClient.character_profile_variants", fake_profile)
    result = runner.invoke(raiderio_app, ["character", "us", "illidan", "Missing"])
    assert result.exit_code == 4

    payload = json.loads(result.stderr)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "not_found"
    assert payload["error"]["message"] == "Character not found"


@pytest.mark.parametrize(
    ("status", "code", "exit_code"),
    [(400, "invalid_query", 2), (403, "auth_failed", 3), (429, "rate_limited", 5), (502, "upstream_error", 5)],
)
def test_raiderio_http_status_maps_to_exit_code(monkeypatch, status: int, code: str, exit_code: int) -> None:
    """Upstream HTTP statuses map to the shared error-code and exit-code vocabulary."""
    request = httpx.Request("GET", "https://raider.io/api/v1/characters/profile")
    response = httpx.Response(status, request=request, json={})

    def fake_profile(self, *, region: str, realm: str, name: str, fields: str = ""):  # noqa: ANN001
        raise httpx.HTTPStatusError("upstream", request=request, response=response)

    monkeypatch.setattr("raiderio_cli.client.RaiderIOClient.character_profile_variants", fake_profile)
    result = runner.invoke(raiderio_app, ["character", "us", "illidan", "Cotti"])
    assert result.exit_code == exit_code
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == code
    assert payload["error"]["details"]["status_code"] == status


def _leaderboard_rows(page: int) -> list[dict]:
    rows = {
        0: [
            {
                "rank": 1,
                "score": 580.0,
                "run": {
                    "keystone_run_id": 2001,
                    "season": "season-tww-3",
                    "mythic_level": 26,
                    "completed_at": "2026-01-21T18:27:09.000Z",
                    "dungeon": {"name": "The Dawnbreaker", "slug": "the-dawnbreaker"},
                    "weekly_modifiers": [{"slug": "tyrannical"}],
                    "roster": [
                        {"character": {"name": "Cotti", "realm": {"slug": "tarren-mill"}, "region": {"slug": "eu"}}, "role": "dps"},
                    ],
                },
            },
            {
                "rank": 2,
                "score": 575.0,
                "run": {
                    "keystone_run_id": 2002,
                    "season": "season-tww-3",
                    "mythic_level": 25,
                    "completed_at": "2026-01-21T18:30:09.000Z",
                    "dungeon": {"name": "Operation: Floodgate", "slug": "operation-floodgate"},
                    "weekly_modifiers": [{"slug": "tyrannical"}],
                    "roster": [
                        {"character": {"name": "Meowtide", "realm": {"slug": "sylvanas"}, "region": {"slug": "eu"}}, "role": "healer"},
                    ],
                },
            },
        ],
    }
    return rows.get(page, [])


def test_raiderio_leaderboard_mythic_plus(monkeypatch) -> None:
    def fake_runs(self, *, season: str | None, region: str, dungeon: str, affixes: str | None, page: int):  # noqa: ANN001
        return {
            "season": "season-tww-3",
            "leaderboard_url": f"https://raider.io/mythic-plus-runs/season-tww-3/{region}/{dungeon}/{page}",
            "rankings": _leaderboard_rows(page),
        }

    monkeypatch.setattr("raiderio_cli.client.RaiderIOClient.mythic_plus_runs", _as_fetched(fake_runs))
    result = runner.invoke(raiderio_app, ["leaderboard", "mythic-plus", "--season", "current", "--region", "us", "--dungeon", "all", "--limit", "20"])
    assert result.exit_code == 0, result.output

    payload = json.loads(result.stdout)
    assert payload["provider"] == "raiderio"
    assert payload["kind"] == "mythic_plus_leaderboard"
    assert payload["query"]["resolved_season"] == "season-tww-3"
    assert payload["query"]["region"] == "us"
    assert payload["query"]["limit"] == 20
    assert payload["count"] == 2
    assert len(payload["runs"]) == 2
    assert payload["runs"][0]["rank"] == 1
    assert payload["sample"]["requested_limit"] == 20
    assert payload["sample"]["returned_run_count"] == 2
    assert payload["sample"]["pages_fetched"] == 1
    assert payload["sample"]["limit_reached"] is False  # provider returned fewer than --limit
    assert payload["freshness"]["sampled_at"]
    assert payload["freshness"]["cache_ttl_seconds"] >= 1
    assert len(payload["citations"]["leaderboard_urls"]) >= 1


def test_raiderio_leaderboard_paginates_for_limit(monkeypatch) -> None:
    # --limit beyond one page must fetch more pages, not silently return one page of rows.
    def fake_runs(self, *, season: str | None, region: str, dungeon: str, affixes: str | None, page: int):  # noqa: ANN001
        # 20 unique rows per page (the real Raider.IO page size), pages 0 and 1 populated.
        rankings = []
        if page in (0, 1):
            base = page * 20
            for i in range(20):
                rank = base + i + 1
                rankings.append({
                    "rank": rank,
                    "score": 600.0 - rank,
                    "run": {
                        "keystone_run_id": 9000 + rank,
                        "season": "season-tww-3",
                        "mythic_level": 25,
                        "completed_at": "2026-01-21T18:27:09.000Z",
                        "dungeon": {"name": "The Dawnbreaker", "slug": "the-dawnbreaker"},
                        "weekly_modifiers": [{"slug": "tyrannical"}],
                        "roster": [{"character": {"name": f"P{rank}", "realm": {"slug": "r"}, "region": {"slug": "us"}}, "role": "dps"}],
                    },
                })
        return {
            "season": "season-tww-3",
            "leaderboard_url": f"https://raider.io/mythic-plus-runs/season-tww-3/us/all/{page}",
            "rankings": rankings,
        }

    monkeypatch.setattr("raiderio_cli.client.RaiderIOClient.mythic_plus_runs", _as_fetched(fake_runs))
    result = runner.invoke(raiderio_app, ["leaderboard", "mythic-plus", "--region", "us", "--limit", "40"])
    assert result.exit_code == 0, result.output

    payload = json.loads(result.stdout)
    assert payload["count"] == 40
    assert payload["sample"]["pages_fetched"] == 2
    assert payload["sample"]["limit_reached"] is True
    assert len(payload["citations"]["leaderboard_urls"]) == 2


def test_raiderio_leaderboard_season_current_omits_season_param(monkeypatch) -> None:
    # --season current must resolve to None (omit the param so the API uses its default season),
    # and the payload echoes the season recovered from the API response.
    captured: dict[str, object] = {}

    def fake_runs(self, *, season: str | None, region: str, dungeon: str, affixes: str | None, page: int):  # noqa: ANN001
        captured["season"] = season
        return {
            "season": "season-tww-3",
            "leaderboard_url": "https://raider.io/mythic-plus-runs/season-tww-3/world/all/0",
            "rankings": _leaderboard_rows(0),
        }

    monkeypatch.setattr("raiderio_cli.client.RaiderIOClient.mythic_plus_runs", _as_fetched(fake_runs))
    result = runner.invoke(raiderio_app, ["leaderboard", "mythic-plus", "--season", "current"])
    assert result.exit_code == 0, result.output

    assert captured["season"] is None  # 'current' -> None
    payload = json.loads(result.stdout)
    assert payload["query"]["resolved_season"] == "season-tww-3"


def test_raiderio_sample_surfaces_resolved_season(monkeypatch) -> None:
    # AC3: analytics commands surface resolved_season recovered from the API response.
    def fake_runs(self, *, season: str | None, region: str, dungeon: str, affixes: str | None, page: int):  # noqa: ANN001
        return {
            "season": "season-tww-3",
            "leaderboard_url": "https://raider.io/mythic-plus-runs/season-tww-3/world/all/0",
            "rankings": _leaderboard_rows(0),
        }

    monkeypatch.setattr("raiderio_cli.client.RaiderIOClient.mythic_plus_runs", _as_fetched(fake_runs))
    result = runner.invoke(raiderio_app, ["sample", "mythic-plus-runs", "--season", "current", "--limit", "2"])
    assert result.exit_code == 0, result.output

    payload = json.loads(result.stdout)
    assert payload["query"]["resolved_season"] == "season-tww-3"


def test_raiderio_leaderboard_empty_runs_degrades_cleanly(monkeypatch) -> None:
    # Empty rankings: no IndexError; citations still carry the season-scoped leaderboard URL.
    def fake_runs(self, *, season: str | None, region: str, dungeon: str, affixes: str | None, page: int):  # noqa: ANN001
        return {
            "season": "season-tww-3",
            "leaderboard_url": "https://raider.io/mythic-plus-runs/season-tww-3/world/all/0",
            "rankings": [],
        }

    monkeypatch.setattr("raiderio_cli.client.RaiderIOClient.mythic_plus_runs", _as_fetched(fake_runs))
    result = runner.invoke(raiderio_app, ["leaderboard", "mythic-plus"])
    assert result.exit_code == 0, result.output

    payload = json.loads(result.stdout)
    assert payload["count"] == 0
    assert payload["runs"] == []
    assert payload["query"]["resolved_season"] == "season-tww-3"
    assert len(payload["citations"]["leaderboard_urls"]) >= 1


def _raise_404(endpoint: str) -> None:
    """Raise the 404 Raider.IO answers a profile lookup with when the target does not exist."""
    request = httpx.Request("GET", f"https://raider.io/api/v1/{endpoint}/profile")
    raise httpx.HTTPStatusError("not found", request=request, response=httpx.Response(404, request=request))


def _raise_connect(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
    raise httpx.ConnectError("offline", request=httpx.Request("GET", "https://raider.io/api/v1/search"))


@pytest.mark.parametrize(
    "args",
    [
        ["search", "liquid"],
        ["resolve", "us illidan cotti"],
        ["character", "us", "illidan", "Cotti"],
        ["guild", "us", "illidan", "Liquid"],
        ["mythic-plus-runs", "--region", "us"],
        ["leaderboard", "mythic-plus"],
        ["sample", "mythic-plus-runs"],
        ["sample", "mythic-plus-players"],
        ["distribution", "mythic-plus-runs", "--metric", "dungeon"],
        ["distribution", "mythic-plus-players", "--metric", "class"],
        ["threshold", "mythic-plus-runs", "--value", "10"],
    ],
    ids=lambda args: " ".join(args[:2]),
)
def test_raiderio_connect_error_is_enveloped(monkeypatch, args: list[str]) -> None:
    """A transport failure returns the error envelope on stderr and the network exit code, never a traceback."""
    monkeypatch.setattr("raiderio_cli.client.request_with_retries", _raise_connect)
    result = runner.invoke(raiderio_app, args)
    assert result.exit_code == 5, result.output
    assert result.stdout == ""
    payload = json.loads(result.stderr)
    assert payload["ok"] is False
    assert payload["provider"] == "raiderio"
    # The error envelope names the same command the success envelope would: the full sub-path,
    # because the Typer leaf name is ambiguous (`raids` is both the catalog and the leaderboard).
    expected = " ".join(args[:2]) if args[0] in {"sample", "distribution", "threshold", "leaderboard"} else args[0]
    assert payload["command"] == expected
    assert payload["schema_version"] == "1"
    assert payload["error"]["code"] == "network_error"
    assert not isinstance(result.exception, httpx.HTTPError)


def test_raiderio_payloads_satisfy_the_shared_envelope(monkeypatch) -> None:
    """doctor, search, resolve, and character emit a conforming envelope with flat legacy keys."""
    monkeypatch.setattr(
        "raiderio_cli.client.RaiderIOClient.search",
        lambda self, *, term, kind=None: {"matches": []},
    )
    monkeypatch.setattr(
        "raiderio_cli.client.RaiderIOClient.character_profile_variants",
        lambda self, *, region, realm, name, fields="": {
            "name": "Cotti",
            "region": "eu",
            "realm": "Tarren Mill",
            "class": "Druid",
            "active_spec_name": "Balance",
            "profile_url": "https://raider.io/characters/eu/tarren-mill/Cotti",
        },
    )
    invocations = {
        "doctor": ["doctor"],
        "search": ["search", "liquid"],
        "resolve": ["resolve", "liquid"],
        "character": ["character", "eu", "tarren-mill", "Cotti"],
    }
    for command, args in invocations.items():
        result = runner.invoke(raiderio_app, args)
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert envelope_violations(payload) == [], command
        assert payload["command"] == command
        assert payload["provider"] == "raiderio"
        assert payload["schema_version"] == "1"

    # Deprecated flat copies agents already read stay next to the envelope keys.
    search_payload = json.loads(runner.invoke(raiderio_app, ["search", "liquid"]).stdout)
    assert search_payload["results"] == search_payload["data"]["results"]
    assert search_payload["count"] == 0


def test_raiderio_provider_object_satisfies_the_surface() -> None:
    provider: ProviderSurface = PROVIDER
    assert provider.name == "raiderio"
    assert isinstance(PROVIDER, ProviderSurface)


def _runs_response_with_params_season(season: str, *, page: int = 0) -> dict:
    """The real ``/mythic-plus/runs`` shape: the applied season is echoed under ``params``."""
    return {
        "params": {"dungeon": "all", "page": page, "region": "us", "access_key": "", "season": season},
        "leaderboard_url": f"https://raider.io/mythic-plus-rankings/{season}/all/us/leaderboards-strict",
        "rankings": _leaderboard_rows(page),
    }


def test_raiderio_leaderboard_recovers_resolved_season_from_params(monkeypatch) -> None:
    # Raider.IO echoes the applied season under params.season, never as a top-level key, so
    # --season current must still report a concrete resolved_season.
    monkeypatch.setattr(
        "raiderio_cli.client.RaiderIOClient.mythic_plus_runs",
        _as_fetched(lambda self, *, season, region, dungeon, affixes, page: _runs_response_with_params_season("season-mn-2", page=page)),
    )
    result = runner.invoke(raiderio_app, ["leaderboard", "mythic-plus", "--season", "current", "--region", "us"])
    assert result.exit_code == 0, result.output

    payload = json.loads(result.stdout)
    assert payload["query"]["resolved_season"] == "season-mn-2"


def test_raiderio_mythic_plus_runs_recovers_resolved_season_from_params(monkeypatch) -> None:
    monkeypatch.setattr(
        "raiderio_cli.client.RaiderIOClient.mythic_plus_runs",
        _as_fetched(lambda self, *, season, region, dungeon, affixes, page: _runs_response_with_params_season("season-mn-2", page=page)),
    )
    result = runner.invoke(raiderio_app, ["mythic-plus-runs", "--region", "us"])
    assert result.exit_code == 0, result.output

    payload = json.loads(result.stdout)
    assert payload["query"]["resolved_season"] == "season-mn-2"


def test_raiderio_sample_recovers_resolved_season_from_params(monkeypatch) -> None:
    monkeypatch.setattr(
        "raiderio_cli.client.RaiderIOClient.mythic_plus_runs",
        _as_fetched(lambda self, *, season, region, dungeon, affixes, page: _runs_response_with_params_season("season-mn-2", page=page)),
    )
    result = runner.invoke(raiderio_app, ["sample", "mythic-plus-runs", "--season", "current", "--limit", "2"])
    assert result.exit_code == 0, result.output

    payload = json.loads(result.stdout)
    assert payload["query"]["resolved_season"] == "season-mn-2"
    assert payload["sample"]["season"] == "season-mn-2"


@pytest.mark.parametrize(
    ("message", "code", "exit_code"),
    [
        ("Could not find requested guild", "not_found", 4),
        ("Invalid request query input", "invalid_query", 2),
    ],
)
def test_raiderio_http_400_separates_missing_target_from_bad_input(monkeypatch, message: str, code: str, exit_code: int) -> None:
    # Raider.IO returns HTTP 400 both for a guild that does not exist and for a malformed request;
    # only the message distinguishes exit 4 (not found) from exit 2 (usage).
    request = httpx.Request("GET", "https://raider.io/api/v1/guilds/profile")
    response = httpx.Response(400, request=request, json={"statusCode": 400, "error": "Bad Request", "message": message})

    def fake_profile(self, *, region: str, realm: str, name: str, fields: str = ""):  # noqa: ANN001
        raise httpx.HTTPStatusError("bad request", request=request, response=response)

    monkeypatch.setattr("raiderio_cli.client.RaiderIOClient.guild_profile_variants", fake_profile)
    result = runner.invoke(raiderio_app, ["guild", "us", "malganis", "Missing"])
    assert result.exit_code == exit_code, result.output

    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == code
    assert payload["error"]["message"] == message


@pytest.mark.parametrize("realm_spelling", ["mal'ganis", "malganis", "mal-ganis"])
def test_raiderio_resolve_scores_every_spelling_of_a_punctuated_realm_alike(monkeypatch, realm_spelling: str) -> None:
    # Raider.IO echoes the realm display name ("Mal'Ganis") while the CLI's own next_command writes
    # the slug, so a query with no "guild"/"character" hint has to resolve whichever way the realm is
    # spelled -- otherwise the follow-up command this very command emits does not resolve when fed
    # back into it.
    monkeypatch.setattr("raiderio_cli.client.RaiderIOClient.search", lambda self, *, term, kind=None: {"matches": []})
    monkeypatch.setattr(
        "raiderio_cli.client.RaiderIOClient.character_profile_variants",
        lambda self, *, region, realm, name, fields="": _raise_404("characters"),
    )
    monkeypatch.setattr(
        "raiderio_cli.client.RaiderIOClient.guild_profile_variants",
        lambda self, *, region, realm, name, fields="": {
            "name": "gn",
            "region": "us",
            "realm": "Mal'Ganis",
            "faction": "horde",
            "profile_url": "https://raider.io/guilds/us/malganis/gn",
        },
    )
    result = runner.invoke(raiderio_app, ["resolve", f"us {realm_spelling} gn"])
    assert result.exit_code == 0, result.output

    payload = json.loads(result.stdout)["data"]
    assert payload["resolved"] is True
    assert payload["confidence"] == "high"
    # next_command always spells the realm as a slug, and both slug spellings are parameters of this
    # test, so the command this command emits resolves when it is fed back in.
    assert payload["next_command"].split()[3] in {"malganis", "mal-ganis"}, payload["next_command"]
    assert payload["next_command"].startswith("raiderio guild us ") and payload["next_command"].endswith(" gn")
    assert payload["match"]["ranking"]["score"] == 61
    assert "realm_match" in payload["match"]["ranking"]["match_reasons"]
    assert "all_terms_match" in payload["match"]["ranking"]["match_reasons"]


def test_raiderio_structured_probe_tries_multi_word_realm_splits(monkeypatch) -> None:
    # "eu tarren mill Cotti" reads the realm as one token first; the direct-profile path only fires
    # if the two-token realm is tried as well.
    attempts: list[tuple[str, str]] = []

    def fake_character(self, *, region: str, realm: str, name: str, fields: str = ""):  # noqa: ANN001
        attempts.append((realm, name))
        if realm != "tarren-mill":
            _raise_404("characters")
        return {
            "id": 1,
            "name": "Cotti",
            "region": "eu",
            "realm": "Tarren Mill",
            "class": "Rogue",
            "active_spec_name": "Subtlety",
            "profile_url": "https://raider.io/characters/eu/tarren-mill/Cotti",
        }

    monkeypatch.setattr("raiderio_cli.client.RaiderIOClient.character_profile_variants", fake_character)
    monkeypatch.setattr(
        "raiderio_cli.client.RaiderIOClient.guild_profile_variants",
        lambda self, *, region, realm, name, fields="": _raise_404("guilds"),
    )
    monkeypatch.setattr(
        "raiderio_cli.client.RaiderIOClient.search",
        lambda self, *, term, kind=None: pytest.fail("a realm that exists must not fall back to site search"),
    )

    result = runner.invoke(raiderio_app, ["resolve", "eu tarren mill Cotti"])
    assert result.exit_code == 0, result.output

    assert attempts == [("tarren", "mill Cotti"), ("tarren-mill", "Cotti")]
    payload = json.loads(result.stdout)["data"]
    assert payload["resolved"] is True
    assert payload["next_command"] == "raiderio character eu tarren-mill Cotti"
    # The two words that name the realm are credited as a realm match, not just as loose terms.
    assert "realm_match" in payload["match"]["ranking"]["match_reasons"]


def test_raiderio_search_keeps_a_name_that_ends_in_a_type_word(monkeypatch) -> None:
    # Raider.IO has a guild called "Liquid Guild" on Illidan (verified live). Reading the trailing
    # word as a type hint searched for "Liquid" and answered with a different guild under ok:true.
    terms: list[str] = []

    def fake_search(self, *, term: str, kind: str | None = None):  # noqa: ANN001
        terms.append(term)
        return {
            "matches": [
                {
                    "type": "guild",
                    "name": "Liquid Guild",
                    "data": {
                        "id": 1712678,
                        "displayName": "Liquid Guild",
                        "region": {"slug": "us", "name": "United States & Oceania"},
                        "realm": {"slug": "illidan", "name": "Illidan"},
                        "path": "/guilds/us/illidan/Liquid%20Guild",
                    },
                }
            ]
        }

    monkeypatch.setattr("raiderio_cli.client.RaiderIOClient.search", fake_search)
    result = runner.invoke(raiderio_app, ["search", "Liquid Guild"])
    assert result.exit_code == 0, result.output

    assert terms == ["Liquid Guild"]
    top = json.loads(result.stdout)["data"]["results"][0]
    assert top["name"] == "Liquid Guild"
    assert top["ranking"]["match_reasons"] == ["exact_name", "all_terms_match"]


def test_raiderio_structured_probe_keeps_a_type_word_inside_the_name(monkeypatch) -> None:
    # "guild"/"character" is a type hint only as the first token. Stripping the word wherever it
    # appeared probed (and site-searched) for "Old Order", a guild nobody has.
    attempts: list[tuple[str, str]] = []

    def fake_guild(self, *, region: str, realm: str, name: str, fields: str = ""):  # noqa: ANN001
        attempts.append((realm, name))
        if name != "Old Guild Order":
            _raise_404("guilds")
        return {"id": 7, "name": name, "region": region, "realm": "Mal'Ganis", "profile_url": "https://raider.io/guilds/us/malganis/Old-Guild-Order"}

    monkeypatch.setattr("raiderio_cli.client.RaiderIOClient.guild_profile_variants", fake_guild)
    monkeypatch.setattr(
        "raiderio_cli.client.RaiderIOClient.character_profile_variants",
        lambda self, *, region, realm, name, fields="": pytest.fail("a guild-hinted query must not probe characters"),
    )
    monkeypatch.setattr(
        "raiderio_cli.client.RaiderIOClient.search",
        lambda self, *, term, kind=None: pytest.fail(f"the guild exists; no site search for {term!r}"),
    )

    result = runner.invoke(raiderio_app, ["resolve", "guild us malganis Old Guild Order"])
    assert result.exit_code == 0, result.output

    assert attempts == [("malganis", "Old Guild Order")]
    assert json.loads(result.stdout)["data"]["match"]["name"] == "Old Guild Order"


def _raid_ranking_row(rank: int, *, realm: str = "malganis", guild_id: int | None = None) -> dict:
    name = f"Guild {rank}"
    return {
        "rank": rank,
        "regionRank": rank + 1,
        "guild": {
            "id": guild_id if guild_id is not None else 1000 + rank,
            "name": name,
            "faction": "horde",
            "realm": {"name": "Mal'Ganis", "slug": realm},
            "region": {"name": "United States & Oceania", "slug": "us", "short_name": "US"},
            "path": f"/guilds/us/{realm}/Guild%20{rank}",
        },
        "encountersDefeated": [
            {"slug": "vexie-and-the-geargrinders", "firstDefeated": "2025-03-07T05:25:48.000Z", "lastDefeated": "2025-08-05T01:09:05.000Z"},
        ],
        "guildPrivacy": {"raidPulls": True},
        "encountersPulled": [
            {"id": 1, "slug": "vexie-and-the-geargrinders", "numPulls": 3, "pullStartedAt": "2025-03-07T05:19:17Z", "bestPercent": 0, "isDefeated": True},
            {"id": 2, "slug": "cauldron-of-carnage", "numPulls": 12, "pullStartedAt": "2025-03-07T05:33:27Z", "bestPercent": 41.5, "isDefeated": False},
        ],
    }


def test_raiderio_leaderboard_raids_normalizes_rows(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_rankings(self, *, raid, difficulty, region, realm=None, limit, page):  # noqa: ANN001
        captured.update(raid=raid, difficulty=difficulty, region=region, realm=realm, limit=limit, page=page)
        return {"raidRankings": [_raid_ranking_row(1), _raid_ranking_row(2)]}

    monkeypatch.setattr("raiderio_cli.client.RaiderIOClient.raid_rankings", _as_fetched(fake_rankings))
    result = runner.invoke(
        raiderio_app,
        ["leaderboard", "raids", "--raid", "liberation-of-undermine", "--region", "US", "--realm", "malganis", "--limit", "5"],
    )
    assert result.exit_code == 0, result.output

    payload = json.loads(result.stdout)
    assert not envelope_violations(payload)
    assert payload["kind"] == "raid_leaderboard"
    assert payload["query"] == {"raid": "liberation-of-undermine", "difficulty": "mythic", "region": "us", "realm": "malganis", "page": 0, "limit": 5}
    assert captured == {"raid": "liberation-of-undermine", "difficulty": "mythic", "region": "us", "realm": "malganis", "limit": 20, "page": 0}
    assert payload["count"] == 2
    assert payload["sample"] == {"requested_limit": 5, "returned_row_count": 2, "pages_requested": 1, "pages_fetched": 1, "limit_reached": False}

    top = payload["data"]["rows"][0]
    assert top["rank"] == 1
    assert top["region_rank"] == 2
    assert top["guild"] == {
        "name": "Guild 1",
        "realm": "malganis",
        "realm_name": "Mal'Ganis",
        "region": "us",
        "faction": "horde",
        "profile_url": "https://raider.io/guilds/us/malganis/Guild%201",
    }
    assert top["encounters_defeated_count"] == 1
    assert top["encounters_pulled_count"] == 2
    assert top["encounters_defeated"] == [
        {"slug": "vexie-and-the-geargrinders", "first_defeated": "2025-03-07T05:25:48.000Z", "last_defeated": "2025-08-05T01:09:05.000Z"}
    ]
    assert top["encounters_pulled"][1] == {
        "slug": "cauldron-of-carnage",
        "num_pulls": 12,
        "best_percent": 41.5,
        "is_defeated": False,
        "pull_started_at": "2025-03-07T05:33:27Z",
    }
    assert payload["citations"]["leaderboard_urls"] == ["https://raider.io/liberation-of-undermine/rankings/us/mythic?realm=malganis"]
    assert payload["freshness"]["sampled_at"] and payload["freshness"]["cache_ttl_seconds"] >= 1
    assert payload["provenance"]["citations"] == payload["citations"]


def test_raiderio_leaderboard_raids_paginates_for_limit(monkeypatch) -> None:
    # --limit beyond one 20-row page must fetch more pages and stop at the first short page.
    def fake_rankings(self, *, raid, difficulty, region, realm=None, limit, page):  # noqa: ANN001
        assert limit == 20
        if page == 0:
            return {"raidRankings": [_raid_ranking_row(rank) for rank in range(1, 21)]}
        if page == 1:
            return {"raidRankings": [_raid_ranking_row(rank) for rank in range(21, 26)]}
        raise AssertionError(f"page {page} must not be requested after a short page")

    monkeypatch.setattr("raiderio_cli.client.RaiderIOClient.raid_rankings", _as_fetched(fake_rankings))
    result = runner.invoke(raiderio_app, ["leaderboard", "raids", "--raid", "sporefall", "--limit", "50"])
    assert result.exit_code == 0, result.output

    payload = json.loads(result.stdout)
    assert payload["query"]["region"] == "world" and payload["query"]["realm"] is None
    assert payload["count"] == 25
    assert payload["sample"] == {"requested_limit": 50, "returned_row_count": 25, "pages_requested": 3, "pages_fetched": 2, "limit_reached": False}
    assert [row["rank"] for row in payload["rows"]] == list(range(1, 26))
    assert payload["citations"]["leaderboard_urls"] == ["https://raider.io/sporefall/rankings/world/mythic"]


def test_raiderio_leaderboard_raids_trims_to_limit_and_dedupes_guilds(monkeypatch) -> None:
    def fake_rankings(self, *, raid, difficulty, region, realm=None, limit, page):  # noqa: ANN001
        base = page * 20
        rows = [_raid_ranking_row(base + offset) for offset in range(1, 21)]
        if page == 1:
            rows[0] = _raid_ranking_row(21, guild_id=1001)  # already seen on page 0
        return {"raidRankings": rows}

    monkeypatch.setattr("raiderio_cli.client.RaiderIOClient.raid_rankings", _as_fetched(fake_rankings))
    result = runner.invoke(raiderio_app, ["leaderboard", "raids", "--raid", "sporefall", "--difficulty", "heroic", "--limit", "25"])
    assert result.exit_code == 0, result.output

    payload = json.loads(result.stdout)
    assert payload["query"]["difficulty"] == "heroic"
    assert payload["count"] == 25
    assert payload["sample"]["pages_fetched"] == 2
    assert payload["sample"]["limit_reached"] is True
    ranks = [row["rank"] for row in payload["rows"]]
    assert 21 not in ranks and ranks[-1] == 26


@pytest.mark.parametrize(
    ("realm_flag", "expected_realm", "expected_url_realm"),
    [
        ("Tarren Mill", "tarren-mill", "tarren-mill"),
        ("Mal'Ganis", "mal-ganis", "mal-ganis"),
        ("  malganis  ", "malganis", "malganis"),
        # Cyrillic realms have no ASCII slug, so the display name passes through to the API (which
        # accepts it, confirmed live) and the citation URL has to percent-encode it.
        (
            "Ревущий фьорд",
            "ревущий фьорд",
            "%D1%80%D0%B5%D0%B2%D1%83%D1%89%D0%B8%D0%B9%20%D1%84%D1%8C%D0%BE%D1%80%D0%B4",
        ),
    ],
)
def test_raiderio_leaderboard_raids_slugifies_and_encodes_the_realm(
    monkeypatch, realm_flag: str, expected_realm: str, expected_url_realm: str
) -> None:
    # A display-name realm must reach the API as a slug and the citation as a valid URL; sending
    # "Tarren Mill" raw emitted a URL with a literal space inside an ok:true envelope.
    captured: dict[str, object] = {}

    def fake_rankings(self, *, raid, difficulty, region, realm=None, limit, page):  # noqa: ANN001
        captured["realm"] = realm
        return {"raidRankings": [_raid_ranking_row(1)]}

    monkeypatch.setattr("raiderio_cli.client.RaiderIOClient.raid_rankings", _as_fetched(fake_rankings))
    result = runner.invoke(
        raiderio_app,
        ["leaderboard", "raids", "--raid", "sporefall", "--region", "eu", "--realm", realm_flag, "--limit", "5"],
    )
    assert result.exit_code == 0, result.output

    payload = json.loads(result.stdout)
    assert captured["realm"] == expected_realm
    # `query` is an envelope field, not a legacy top-level copy of a `data` key.
    assert payload["query"]["realm"] == expected_realm
    citation = payload["provenance"]["citations"]["leaderboard_urls"][0]
    assert citation == f"https://raider.io/sporefall/rankings/eu/mythic?realm={expected_url_realm}"
    assert " " not in citation


def test_raiderio_leaderboard_raids_accepts_the_region_aliases_its_siblings_accept(monkeypatch) -> None:
    # `raiderio character na illidan X` works, so `--region na` must not be a usage error here.
    captured: dict[str, object] = {}

    def fake_rankings(self, *, raid, difficulty, region, realm=None, limit, page):  # noqa: ANN001
        captured["region"] = region
        return {"raidRankings": [_raid_ranking_row(1)]}

    monkeypatch.setattr("raiderio_cli.client.RaiderIOClient.raid_rankings", _as_fetched(fake_rankings))
    result = runner.invoke(raiderio_app, ["leaderboard", "raids", "--raid", "sporefall", "--region", "na", "--limit", "5"])
    assert result.exit_code == 0, result.output

    assert captured["region"] == "us"
    assert json.loads(result.stdout)["query"]["region"] == "us"


@pytest.mark.parametrize(
    ("args", "fragment"),
    [
        (["--raid", "sporefall", "--difficulty", "legendary"], "--difficulty must be one of"),
        (["--raid", "sporefall", "--region", "mars"], "--region must be one of"),
        (["--raid", "sporefall", "--realm", "malganis"], "--realm requires a standard --region"),
    ],
)
def test_raiderio_leaderboard_raids_rejects_bad_scope(monkeypatch, args: list[str], fragment: str) -> None:
    def never(self, **kwargs):  # noqa: ANN001, ANN003
        raise AssertionError("an invalid scope must be rejected before any request")

    monkeypatch.setattr("raiderio_cli.client.RaiderIOClient.raid_rankings", _as_fetched(never))
    result = runner.invoke(raiderio_app, ["leaderboard", "raids", *args])
    assert result.exit_code == 2, result.output
    payload = json.loads(result.stderr)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_query"
    assert fragment in payload["error"]["message"]


def test_raiderio_leaderboard_raids_maps_unknown_raid_to_usage_error(monkeypatch) -> None:
    # Raider.IO answers an unknown raid slug with HTTP 400 "Invalid request query input".
    def fake_rankings(self, **kwargs):  # noqa: ANN001, ANN003
        request = httpx.Request("GET", "https://raider.io/api/v1/raiding/raid-rankings")
        response = httpx.Response(400, json={"statusCode": 400, "error": "Bad Request", "message": "Invalid request query input"}, request=request)
        raise httpx.HTTPStatusError("400", request=request, response=response)

    monkeypatch.setattr("raiderio_cli.client.RaiderIOClient.raid_rankings", _as_fetched(fake_rankings))
    result = runner.invoke(raiderio_app, ["leaderboard", "raids", "--raid", "nope"])
    assert result.exit_code == 2, result.output
    assert json.loads(result.stderr)["error"]["code"] == "invalid_query"


def test_raiderio_raids_catalog(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_static(self, *, expansion_id):  # noqa: ANN001
        captured["expansion_id"] = expansion_id
        return {
            "raids": [
                {
                    "id": 8062,
                    "slug": "sporefall",
                    "name": "Sporefall",
                    "short_name": "SF",
                    "icon": "inv_achievement_raid_sporefall",
                    "starts": {"us": "2026-03-17T15:00:00Z", "eu": "2026-03-18T04:00:00Z"},
                    "ends": {"us": "2026-08-18T15:00:00Z", "eu": "2026-08-19T04:00:00Z"},
                    "encounters": [{"id": 1, "slug": "first-boss", "name": "First Boss"}, "junk"],
                },
                "junk",
            ]
        }

    monkeypatch.setattr("raiderio_cli.client.RaiderIOClient.raid_static_data", _as_fetched(fake_static))
    result = runner.invoke(raiderio_app, ["raids", "--expansion-id", "11"])
    assert result.exit_code == 0, result.output

    payload = json.loads(result.stdout)
    assert not envelope_violations(payload)
    assert payload["kind"] == "raid_catalog"
    assert payload["query"] == {"expansion_id": 11}
    assert captured == {"expansion_id": 11}
    assert payload["count"] == 1
    assert payload["data"]["rows"] == [
        {
            "id": 8062,
            "slug": "sporefall",
            "name": "Sporefall",
            "short_name": "SF",
            "starts": {"us": "2026-03-17T15:00:00Z", "eu": "2026-03-18T04:00:00Z"},
            "ends": {"us": "2026-08-18T15:00:00Z", "eu": "2026-08-19T04:00:00Z"},
            "encounters": [{"id": 1, "slug": "first-boss", "name": "First Boss"}],
        }
    ]


def test_raiderio_raids_catalog_cites_its_source_and_the_catalog_ttl(monkeypatch) -> None:
    # The catalog is the command agents call first to discover raid slugs, so an empty provenance
    # leaves them with rows they cannot attribute or age. The TTL has to be the static-data one:
    # quoting a shorter sibling TTL understates how stale a cached catalog may be.
    monkeypatch.setenv("RAIDERIO_STATIC_CACHE_TTL_SECONDS", "4242")
    monkeypatch.setenv("RAIDERIO_MPLUS_RUNS_CACHE_TTL_SECONDS", "77")
    monkeypatch.setenv("RAIDERIO_RAID_RANKINGS_CACHE_TTL_SECONDS", "88")
    monkeypatch.setattr(
        "raiderio_cli.client.RaiderIOClient.raid_static_data",
        _as_fetched(lambda self, *, expansion_id: {"raids": []}),
    )
    result = runner.invoke(raiderio_app, ["raids", "--expansion-id", "10"])
    assert result.exit_code == 0, result.output

    payload = json.loads(result.stdout)
    provenance = payload["provenance"]
    assert provenance["citations"] == {
        "static_data_url": "https://raider.io/api/v1/raiding/static-data?expansion_id=10"
    }
    assert provenance["freshness"]["cache_ttl_seconds"] == 4242
    assert provenance["freshness"]["cache_hit"] is False
    _assert_read_just_now(provenance["freshness"]["fetched_at"])


def test_raiderio_mythic_plus_runs_cites_the_leaderboard_it_read(monkeypatch) -> None:
    monkeypatch.setenv("RAIDERIO_STATIC_CACHE_TTL_SECONDS", "4242")
    monkeypatch.setenv("RAIDERIO_MPLUS_RUNS_CACHE_TTL_SECONDS", "77")
    monkeypatch.setattr(
        "raiderio_cli.client.RaiderIOClient.mythic_plus_runs",
        _as_fetched(
            lambda self, *, season, region, dungeon, affixes, page: {
                "season": "season-mn-1",
                "leaderboard_url": "https://raider.io/mythic-plus-runs/season-mn-1/us/all/0",
                "rankings": [],
            }
        ),
    )
    result = runner.invoke(raiderio_app, ["mythic-plus-runs", "--region", "us"])
    assert result.exit_code == 0, result.output

    provenance = json.loads(result.stdout)["provenance"]
    assert provenance["citations"]["leaderboard_urls"] == ["https://raider.io/mythic-plus-runs/season-mn-1/us/all/0"]
    assert provenance["freshness"]["cache_ttl_seconds"] == 77
    assert provenance["freshness"]["cache_hit"] is False
    _assert_read_just_now(provenance["freshness"]["fetched_at"])


@pytest.mark.parametrize(
    ("args", "method", "stub"),
    [
        (["raids"], "raid_static_data", lambda self, *, expansion_id: {"raids": []}),
        (
            ["mythic-plus-runs"],
            "mythic_plus_runs",
            lambda self, *, season, region, dungeon, affixes, page: {"rankings": []},
        ),
        (
            ["leaderboard", "raids", "--raid", "sporefall"],
            "raid_rankings",
            lambda self, *, raid, difficulty, region, realm=None, limit, page: {"raidRankings": []},
        ),
        (
            ["sample", "mythic-plus-runs"],
            "mythic_plus_runs",
            lambda self, *, season, region, dungeon, affixes, page: {"rankings": []},
        ),
    ],
)
def test_raiderio_freshness_reports_the_cached_fetch_time_not_the_run_time(
    monkeypatch, args: list[str], method: str, stub: Callable[..., dict[str, Any]]
) -> None:
    # A cache hit used to be stamped with datetime.now() at payload-build time, so a six-hour-old
    # raid catalog claimed to be seconds old. The fetch time travels with the cached body instead.
    fetched_at = "2026-09-19T00:00:00+00:00"
    monkeypatch.setattr(
        f"raiderio_cli.client.RaiderIOClient.{method}",
        _as_fetched(stub, fetched_at=fetched_at, cache_hit=True),
    )
    result = runner.invoke(raiderio_app, args)
    assert result.exit_code == 0, result.output

    freshness = json.loads(result.stdout)["data"]["freshness"]
    assert freshness["fetched_at"] == fetched_at
    assert freshness["cache_hit"] is True


def test_raiderio_client_stores_the_fetch_time_with_the_cached_body(monkeypatch, tmp_path) -> None:
    # The client is what makes the reported fetch time truthful: the second read replays the body
    # AND the instant it was fetched, instead of re-stamping the replay with the current time.
    monkeypatch.setenv("RAIDERIO_CACHE_BACKEND", "file")  # the suite disables every provider cache
    monkeypatch.setenv("RAIDERIO_CACHE_DIR", str(tmp_path / "cache"))
    requests: list[str] = []

    def fake_request(client, url, *, params, retry_attempts):  # noqa: ANN001, ANN202
        requests.append(url)
        return httpx.Response(200, json={"raids": [{"slug": "sporefall"}]}, request=httpx.Request("GET", url))

    monkeypatch.setattr("raiderio_cli.client.request_with_retries", fake_request)
    with RaiderIOClient() as client:
        first = client.raid_static_data(expansion_id=11)
        second = client.raid_static_data(expansion_id=11)

    assert len(requests) == 1
    assert first.cache_hit is False and second.cache_hit is True
    assert second.fetched_at == first.fetched_at
    assert second.payload == first.payload


def _stub_every_read(monkeypatch) -> None:
    """Answer every upstream read with the emptiest valid response, so any command can be invoked."""
    reads = {
        "search": lambda self, *, term, kind=None: {"matches": []},
        "character_profile_variants": lambda self, *, region, realm, name, fields="": {"name": name, "realm": realm},
        "guild_profile_variants": lambda self, *, region, realm, name, fields="": {"name": name, "realm": realm},
        "mythic_plus_runs": _as_fetched(lambda self, *, season, region, dungeon, affixes, page: {"rankings": []}),
        "raid_rankings": _as_fetched(lambda self, *, raid, difficulty, region, realm=None, limit, page: {"raidRankings": []}),
        "raid_static_data": _as_fetched(lambda self, *, expansion_id: {"raids": []}),
    }
    for name, stub in reads.items():
        monkeypatch.setattr(f"raiderio_cli.client.RaiderIOClient.{name}", stub)


@pytest.mark.parametrize(
    ("args", "command"),
    [
        (["search", "gn"], "search"),
        (["resolve", "gn"], "resolve"),
        (["character", "us", "malganis", "cotti"], "character"),
        (["guild", "us", "malganis", "gn"], "guild"),
        (["mythic-plus-runs"], "mythic-plus-runs"),
        (["sample", "mythic-plus-runs"], "sample mythic-plus-runs"),
        (["sample", "mythic-plus-players"], "sample mythic-plus-players"),
        (["distribution", "mythic-plus-runs"], "distribution mythic-plus-runs"),
        (["distribution", "mythic-plus-players"], "distribution mythic-plus-players"),
        (["threshold", "mythic-plus-runs", "--value", "3000"], "threshold mythic-plus-runs"),
        (["leaderboard", "mythic-plus"], "leaderboard mythic-plus"),
        (["leaderboard", "raids", "--raid", "sporefall"], "leaderboard raids"),
        (["raids"], "raids"),
    ],
)
def test_raiderio_success_envelope_names_the_full_command_path(monkeypatch, args: list[str], command: str) -> None:
    # `command` is envelope identity, and Typer's leaf names collide: `raids` named both the catalog
    # and the guild leaderboard, and four different payload shapes all answered to
    # `mythic-plus-runs`. Each invocation path gets its own label.
    _stub_every_read(monkeypatch)
    result = runner.invoke(raiderio_app, args)
    assert result.exit_code == 0, result.output

    assert json.loads(result.stdout)["command"] == command


def test_raiderio_error_envelope_carries_the_same_command_as_the_success_envelope(monkeypatch) -> None:
    # An error envelope's `data` is empty and its `kind` is "error", so `command` is all an agent
    # has left to tell which command failed.
    _stub_every_read(monkeypatch)
    failed = runner.invoke(raiderio_app, ["leaderboard", "raids", "--raid", "sporefall", "--difficulty", "legendary"])
    assert failed.exit_code == 2
    assert json.loads(failed.stderr)["command"] == "leaderboard raids"

    bad_metric = runner.invoke(raiderio_app, ["distribution", "mythic-plus-runs", "--metric", "nope"])
    assert bad_metric.exit_code == 2
    assert json.loads(bad_metric.stderr)["command"] == "distribution mythic-plus-runs"


@pytest.mark.parametrize(
    "command",
    ["distribution mythic-plus-runs", "distribution mythic-plus-players", "leaderboard mythic-plus", "sample mythic-plus-runs"],
)
def test_raiderio_usage_error_before_the_command_body_names_the_full_path(monkeypatch, capsys, command: str) -> None:
    # `--pages abc` is rejected by Click's parser, so the command body never runs and cannot relabel
    # the envelope. Every nested command used to answer to its group name instead, which made the
    # two `distribution` commands (and the two `mythic-plus` ones) indistinguishable on failure.
    monkeypatch.setattr(sys, "argv", ["raiderio", *command.split(), "--pages", "abc"])
    with pytest.raises(SystemExit) as exit_info:
        raiderio_run()

    assert exit_info.value.code == 2
    payload = json.loads(capsys.readouterr().err)
    assert payload["command"] == command
    assert payload["error"]["code"] == "invalid_argument"


def test_raiderio_doctor_reports_raid_capabilities_and_ttl() -> None:
    result = runner.invoke(raiderio_app, ["doctor"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["capabilities"]["raid_leaderboard"] == "ready"
    assert payload["capabilities"]["raid_catalog"] == "ready"
    assert payload["cache"]["ttls"]["raid_rankings"] >= 1

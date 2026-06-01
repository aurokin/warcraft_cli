from __future__ import annotations

import json

import httpx
from raiderio_cli.main import (
    _candidate_dedupe_key,
    _dedupe_search_candidates,
    _distribution_values,
    _match_reasons,
    _numeric_summary,
    _player_distribution_values,
    _player_sample_summary,
    _player_snapshots,
    _ranking_roster_entry,
    _resolve_candidate_is_confident,
    _resolve_confidence_label,
    _run_matches_filters,
    _search_result_candidate,
)
from raiderio_cli.main import (
    app as raiderio_app,
)
from typer.testing import CliRunner

runner = CliRunner()


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


def test_raiderio_search_returns_ranked_matches(monkeypatch) -> None:
    monkeypatch.setattr(
        "raiderio_cli.main.RaiderIOClient.search",
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
    result = runner.invoke(raiderio_app, ["search", "Liquid guild", "--limit", "5"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["count"] == 2
    assert payload["results"][0]["kind"] == "guild"
    assert payload["results"][0]["realm"] == "illidan"
    assert "type_hint" in payload["results"][0]["ranking"]["match_reasons"]
    assert payload["results"][0]["follow_up"]["command"] == "raiderio guild us illidan Liquid"


def test_raiderio_search_result_candidate_builds_profile_shape() -> None:
    candidate = _search_result_candidate(
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
    assert _candidate_dedupe_key(first) == ("guild", "us", "illidan", "Liquid")
    deduped = _dedupe_search_candidates([first, second])
    assert len(deduped) == 1
    assert deduped[0]["ranking"]["score"] == 20


def test_raiderio_match_reasons_helper_tracks_exact_and_hints() -> None:
    score, reasons = _match_reasons(
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
    assert _resolve_candidate_is_confident([{"ranking": {"score": 45}}]) is True
    assert _resolve_candidate_is_confident([{"ranking": {"score": 44}}, {"ranking": {"score": 10}}]) is False
    assert _resolve_candidate_is_confident([{"ranking": {"score": 50}}, {"ranking": {"score": 40}}]) is False
    assert _resolve_confidence_label(45, resolved=True) == "high"
    assert _resolve_confidence_label(30, resolved=False) == "medium"
    assert _resolve_confidence_label(20, resolved=False) == "low"


def test_raiderio_ranking_roster_entry_builds_profile_summary() -> None:
    entry = _ranking_roster_entry(
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

    values, unit, numeric = _distribution_values("mythic_level", runs)
    assert values == [26]
    assert unit == "runs"
    assert numeric is True

    values, unit, numeric = _distribution_values("composition", runs)
    assert values == ["dps:balance | healer:restoration"]
    assert unit == "runs"
    assert numeric is False

    values, unit, numeric = _distribution_values("player_region", runs)
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

    values, unit, numeric = _player_distribution_values("appearance_count", players)
    assert values == [2]
    assert unit == "players"
    assert numeric is True

    values, unit, numeric = _player_distribution_values("class", players)
    assert values == ["druid"]
    assert unit == "player_class_tags"
    assert numeric is False

    values, unit, numeric = _player_distribution_values("unknown", players)
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
    players = _player_snapshots(runs)
    summary = _player_sample_summary(
        players,
        runs=runs,
        meta={"sampled_at": "2026-03-12T00:00:00+00:00", "season": "season-tww-2", "pages_requested": 1, "pages_fetched": 1},
        filtering={"contains_class": ["warrior"]},
        player_sampling={"player_limit": 10, "source_player_count": 3,
                         "returned_player_count": 3, "excluded_player_count": 0, "truncated": False},
    )
    assert _numeric_summary([17, 18]) == {"min": 17, "max": 18, "average": 17.5, "median": 17.5}
    assert summary["unique_class_count"] == 3
    assert summary["appearance_count"]["max"] == 2
    assert summary["top_mythic_level"]["max"] == 18


def test_raiderio_search_uses_structured_direct_guild_probe_when_search_is_empty(monkeypatch) -> None:
    monkeypatch.setattr("raiderio_cli.main.RaiderIOClient.search", lambda self, *, term, kind=None: {"matches": []})
    monkeypatch.setattr(
        "raiderio_cli.main.RaiderIOClient.guild_profile_variants",
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
        "raiderio_cli.main.RaiderIOClient.character_profile_variants",
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
        "raiderio_cli.main.RaiderIOClient.search",
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
    monkeypatch.setattr("raiderio_cli.main.RaiderIOClient.search", lambda self, *, term, kind=None: {"matches": []})
    monkeypatch.setattr(
        "raiderio_cli.main.RaiderIOClient.character_profile_variants",
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
        "raiderio_cli.main.RaiderIOClient.guild_profile_variants",
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
        "raiderio_cli.main.RaiderIOClient.search",
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
    result = runner.invoke(raiderio_app, ["resolve", "Liquid guild"])
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

    monkeypatch.setattr("raiderio_cli.main.RaiderIOClient.character_profile_variants", fake_profile)
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

    monkeypatch.setattr("raiderio_cli.main.RaiderIOClient.character_profile_variants", fake_profile)
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

    monkeypatch.setattr("raiderio_cli.main.RaiderIOClient.guild_profile_variants", fake_profile)
    result = runner.invoke(raiderio_app, ["guild", "us", "illidan", "Liquid"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["guild"]["name"] == "Liquid"
    assert payload["guild"]["member_count"] == 2
    assert payload["raiding"]["rankings"][0]["raid_slug"] == "tier-mn-1"
    assert payload["roster_preview"][0]["name"] == "Roguecane"


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

    monkeypatch.setattr("raiderio_cli.main.RaiderIOClient.mythic_plus_runs", fake_runs)
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

    monkeypatch.setattr("raiderio_cli.main.RaiderIOClient.mythic_plus_runs", fake_runs)
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

    monkeypatch.setattr("raiderio_cli.main.RaiderIOClient.mythic_plus_runs", fake_runs)
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

    monkeypatch.setattr("raiderio_cli.main.RaiderIOClient.mythic_plus_runs", fake_runs)
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
        _run_matches_filters(
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
        _run_matches_filters(
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

    snapshots = _player_snapshots(runs)

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

    monkeypatch.setattr("raiderio_cli.main.RaiderIOClient.mythic_plus_runs", fake_runs)
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

    monkeypatch.setattr("raiderio_cli.main.RaiderIOClient.mythic_plus_runs", fake_runs)
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
    result = runner.invoke(raiderio_app, ["distribution", "mythic-plus-runs", "--metric", "spec"])
    assert result.exit_code == 0


def test_raiderio_distribution_rejects_unknown_metric_name() -> None:
    result = runner.invoke(raiderio_app, ["distribution", "mythic-plus-runs", "--metric", "unknown"])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_query"

    player_result = runner.invoke(raiderio_app, ["distribution", "mythic-plus-players", "--metric", "unknown"])
    assert player_result.exit_code == 1
    player_payload = json.loads(player_result.stderr)
    assert player_payload["error"]["code"] == "invalid_query"


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

    monkeypatch.setattr("raiderio_cli.main.RaiderIOClient.mythic_plus_runs", fake_runs)
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
    assert result.exit_code == 1
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

    monkeypatch.setattr("raiderio_cli.main.RaiderIOClient.mythic_plus_runs", fake_runs)
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

    monkeypatch.setattr("raiderio_cli.main.RaiderIOClient.mythic_plus_runs", fake_runs)
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

    monkeypatch.setattr("raiderio_cli.main.RaiderIOClient.mythic_plus_runs", fake_runs)
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

    monkeypatch.setattr("raiderio_cli.main.RaiderIOClient.character_profile_variants", fake_profile)
    result = runner.invoke(raiderio_app, ["character", "us", "illidan", "Missing"])
    assert result.exit_code == 1

    payload = json.loads(result.stderr)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "not_found"

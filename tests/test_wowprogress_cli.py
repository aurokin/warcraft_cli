from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from warcraft_core.analytics import numeric_summary as _numeric_summary
from wowprogress_cli.analytics import (
    _guild_profile_distribution_values,
    _guild_profile_matches_filters,
    _guild_profile_sample_summary,
    _guild_profile_threshold_estimate,
    _normalized_encounter_values,
    _sample_summary,
)
from wowprogress_cli.client import WowProgressClient, WowProgressClientError
from wowprogress_cli.main import app as wowprogress_app
from wowprogress_cli.search import (
    _candidate_from_probe,
    _distinct_result_kinds,
    _has_follow_up_command,
    _is_ambiguous_untyped_result,
    _meets_score_confidence,
    _resolve_confidence_label,
    _resolve_is_confident,
    _score_match,
    _sorted_search_candidates,
)

runner = CliRunner()


def test_wowprogress_client_classifies_cloudflare_403_as_blocked(monkeypatch) -> None:
    class _FakeResponse:
        def __init__(self) -> None:
            self.status_code = 403
            self.url = "https://www.wowprogress.com/guild/us/mal-ganis/gn"
            self.text = "<!DOCTYPE html><html><head><title>Just a moment...</title></head></html>"

    class _FakeSession:
        def get(self, url: str, **kwargs):  # noqa: ANN001
            del url, kwargs
            return _FakeResponse()

    client = WowProgressClient(retry_attempts=1)
    monkeypatch.setattr(client, "_client", lambda: _FakeSession())

    with pytest.raises(WowProgressClientError) as exc_info:
        client.fetch_guild_page(region="us", realm="Mal'Ganis", name="gn")

    assert exc_info.value.code == "blocked"


def test_wowprogress_doctor_reports_phase_one_capabilities() -> None:
    result = runner.invoke(wowprogress_app, ["doctor"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["provider"] == "wowprogress"
    assert payload["status"] == "ready"
    assert payload["transport"]["mode"] == "browser_fingerprint_http"
    assert payload["capabilities"]["guild"] == "ready"
    assert payload["capabilities"]["guild_history"] == "ready"
    assert payload["capabilities"]["search"] == "ready"
    assert payload["capabilities"]["sample_pve_leaderboard"] == "ready"


def test_wowprogress_search_returns_structured_hint_for_unparseable_query() -> None:
    result = runner.invoke(wowprogress_app, ["search", "liquid"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["count"] == 0
    assert payload["results"] == []
    assert "structured queries" in payload["message"]
    assert payload["suggested_queries"]


def test_wowprogress_search_returns_ranked_structured_results(monkeypatch) -> None:
    def fake_probe(self, *, region: str, realm: str, name: str, obj_type: str):  # noqa: ANN001
        assert region == "us"
        assert realm == "illidan"
        assert name == "Liquid"
        if obj_type == "char":
            return None
        return {
            "_search_kind": "guild",
            "guild": {
                "name": "Liquid",
                "region": "us",
                "realm": "US-Illidan",
                "faction": "Horde",
                "page_url": "https://www.wowprogress.com/guild/us/illidan/Liquid",
            },
        }

    monkeypatch.setattr("wowprogress_cli.main.WowProgressClient.probe_search_route", fake_probe)
    result = runner.invoke(wowprogress_app, ["search", "guild us illidan Liquid"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["count"] == 1
    assert payload["results"][0]["kind"] == "guild"
    assert payload["results"][0]["follow_up"]["command"] == "wowprogress guild us illidan Liquid"
    assert "type_hint" in payload["results"][0]["ranking"]["match_reasons"]
    assert payload["normalized_candidates"][0] == {"region": "us", "realm": "illidan", "name": "Liquid"}


def test_wowprogress_search_normalizes_multiword_realms_and_excluded_terms(monkeypatch) -> None:
    def fake_probe(self, *, region: str, realm: str, name: str, obj_type: str):  # noqa: ANN001
        assert region == "us"
        assert realm == "area-52"
        assert name == "xD"
        if obj_type == "guild":
            return {
                "_search_kind": "guild",
                "guild": {
                    "name": "xD",
                    "region": "us",
                    "realm": "US-Area 52",
                    "page_url": "https://www.wowprogress.com/guild/us/area-52/xD",
                },
            }
        return None

    monkeypatch.setattr("wowprogress_cli.main.WowProgressClient.probe_search_route", fake_probe)
    result = runner.invoke(wowprogress_app, ["search", "guild na area 52 xD recruit"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["search_query"] == "guild us area-52 xD"
    assert payload["excluded_terms"] == ["recruit"]
    assert payload["normalized_candidates"][0] == {"region": "us", "realm": "area-52", "name": "xD"}


def test_wowprogress_candidate_from_probe_builds_character_shape() -> None:
    candidate = _candidate_from_probe(
        "us illidan Imonthegcd",
        kind_hint="character",
        payload={
            "_search_kind": "character",
            "character": {
                "name": "Imonthegcd",
                "region": "us",
                "realm": "illidan",
                "guild_name": "Liquid",
                "class_name": "Mage",
                "page_url": "https://www.wowprogress.com/character/us/illidan/Imonthegcd",
            },
        },
        query_region="us",
        query_realm="illidan",
        query_name="Imonthegcd",
    )
    assert candidate["kind"] == "character"
    assert candidate["profile_url"] == "https://www.wowprogress.com/character/us/illidan/Imonthegcd"
    assert "type_hint" in candidate["ranking"]["match_reasons"]


def test_wowprogress_search_and_resolve_helpers_cover_ambiguity() -> None:
    sorted_rows = _sorted_search_candidates(
        [
            {"kind": "guild", "name": "Liquid", "ranking": {"score": 40}},
            {"kind": "character", "name": "Liquid", "ranking": {"score": 55}},
        ]
    )
    assert sorted_rows[0]["kind"] == "character"
    assert _distinct_result_kinds(sorted_rows) == ["character", "guild"]

    best = {"ranking": {"score": 60}, "follow_up": {"command": "wowprogress character us illidan Liquid"}}
    second = {"ranking": {"score": 30}, "follow_up": {"command": "wowprogress guild us illidan Liquid"}}
    assert _resolve_is_confident(best=best, second=second, query_kind="character", distinct_kinds=["character"]) is True
    assert _resolve_is_confident(best=best, second=second, query_kind=None, distinct_kinds=["character", "guild"]) is False
    assert _resolve_confidence_label(best, resolved=True) == "high"
    assert _resolve_confidence_label({"ranking": {"score": 40}}, resolved=False) == "medium"
    assert _resolve_confidence_label({"ranking": {"score": 20}}, resolved=False) == "low"
    assert _has_follow_up_command(best) is True
    assert _meets_score_confidence(60, second_score=30, has_second=True) is True
    assert _meets_score_confidence(60, second_score=50, has_second=True) is False
    assert _is_ambiguous_untyped_result(None, ["character", "guild"]) is True


def test_wowprogress_score_and_summary_helpers() -> None:
    score, reasons = _score_match(
        query="guild us illidan Liquid recruit",
        kind_hint="guild",
        kind="guild",
        name="Liquid",
        region="us",
        realm="US-Illidan",
        query_name="Liquid",
        query_realm="illidan",
    )
    assert score > 0
    assert "exact_target_name" in reasons
    assert "type_hint" in reasons

    leaderboard_summary = _sample_summary(
        [
            {"rank": 1, "bosses_killed": 8, "realm": "Illidan", "difficulty": "M"},
            {"rank": 3, "bosses_killed": 7, "realm": "Tichondrius", "difficulty": "M"},
        ],
        meta={"sampled_at": "2026-03-12T00:00:00+00:00", "requested_limit": 5, "active_raid": "Manaforge Omega"},
    )
    assert leaderboard_summary["rank"]["min"] == 1
    assert leaderboard_summary["bosses_killed"]["max"] == 8

    guild_summary = _guild_profile_sample_summary(
        [
            {
                "faction": "Horde",
                "progress": "8/8 (M)",
                "difficulty": "M",
                "item_level_average": 685.2,
                "progress_ranks": {"world": "12"},
            },
            {
                "faction": "Alliance",
                "progress": "7/8 (M)",
                "difficulty": "M",
                "item_level_average": 681.0,
                "progress_ranks": {"world": "20"},
            },
        ],
        meta={
            "sampled_at": "2026-03-12T00:00:00+00:00",
            "requested_limit": 5,
            "leaderboard_entry_count": 2,
            "skipped_missing_profile_url": 0,
            "active_raid": "Manaforge Omega",
        },
        filtering={"faction": ["horde"]},
    )
    assert guild_summary["item_level_average"]["max"] == 685.2
    assert guild_summary["world_progress_rank"]["min"] == 12


def test_wowprogress_resolve_stays_conservative_when_multiple_results(monkeypatch) -> None:
    def fake_probe(self, *, region: str, realm: str, name: str, obj_type: str):  # noqa: ANN001
        if obj_type == "char":
            return {
                "_search_kind": "character",
                "character": {
                    "name": "Liquid",
                    "region": "us",
                    "realm": "illidan",
                    "guild_name": "Liquid",
                    "class_name": "Mage",
                    "page_url": "https://www.wowprogress.com/character/us/illidan/Liquid",
                },
            }
        return {
            "_search_kind": "guild",
            "guild": {
                "name": "Liquid",
                "region": "us",
                "realm": "illidan",
                "faction": "Horde",
                "page_url": "https://www.wowprogress.com/guild/us/illidan/Liquid",
            },
        }

    monkeypatch.setattr("wowprogress_cli.main.WowProgressClient.probe_search_route", fake_probe)
    result = runner.invoke(wowprogress_app, ["resolve", "us illidan Liquid"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["resolved"] is False
    assert payload["confidence"] in {"medium", "low"}
    assert payload["fallback_search_command"] == 'wowprogress search "us illidan Liquid"'


def test_wowprogress_resolve_returns_command_for_typed_query(monkeypatch) -> None:
    def fake_probe(self, *, region: str, realm: str, name: str, obj_type: str):  # noqa: ANN001
        assert obj_type == "char"
        return {
            "_search_kind": "character",
            "character": {
                "name": "Imonthegcd",
                "region": "us",
                "realm": "illidan",
                "guild_name": "Liquid",
                "class_name": "Mage",
                "page_url": "https://www.wowprogress.com/character/us/illidan/Imonthegcd",
            },
        }

    monkeypatch.setattr("wowprogress_cli.main.WowProgressClient.probe_search_route", fake_probe)
    result = runner.invoke(wowprogress_app, ["resolve", "character us illidan Imonthegcd"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["resolved"] is True
    assert payload["confidence"] == "high"
    assert payload["next_command"] == "wowprogress character us illidan Imonthegcd"


def test_wowprogress_resolve_returns_command_for_short_exact_guild_query(monkeypatch) -> None:
    def fake_probe(self, *, region: str, realm: str, name: str, obj_type: str):  # noqa: ANN001
        assert obj_type == "guild"
        return {
            "_search_kind": "guild",
            "guild": {
                "name": "xD",
                "region": "us",
                "realm": "US-Area 52",
                "faction": "Horde",
                "page_url": "https://www.wowprogress.com/guild/us/area-52/xD",
            },
        }

    monkeypatch.setattr("wowprogress_cli.main.WowProgressClient.probe_search_route", fake_probe)
    result = runner.invoke(wowprogress_app, ["resolve", "guild us area-52 xD"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["resolved"] is True
    assert payload["confidence"] == "high"
    assert payload["next_command"] == "wowprogress guild us area-52 xD"
    assert "exact_target_name" in payload["match"]["ranking"]["match_reasons"]
    assert "exact_target_realm" in payload["match"]["ranking"]["match_reasons"]


def test_wowprogress_guild_uses_variant_fetch_and_normalized_identity(monkeypatch) -> None:
    def fake_fetch(self, *, region: str, realm: str, name: str):  # noqa: ANN001
        assert region == "us"
        assert realm == "mal-ganis"
        assert name == "gn"
        return {"guild": {"name": "gn"}, "progress": {}, "item_level": {}, "encounters": {"count": 0, "items": []}}

    monkeypatch.setattr("wowprogress_cli.main.WowProgressClient.fetch_guild_page_variants", fake_fetch)
    result = runner.invoke(wowprogress_app, ["guild", "na", "Mal'Ganis", "gn"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["guild"]["name"] == "gn"


def test_wowprogress_guild_history_and_ranks_commands(monkeypatch) -> None:
    history_payload = {
        "provider": "wowprogress",
        "kind": "guild_history",
        "guild": {"name": "gn", "region": "us", "realm": "Mal'Ganis"},
        "history": [
            {
                "tier_key": "tier34",
                "raid": "Liberation of Undermine",
                "current": True,
                "progress": "8/8 (M)",
                "progress_ranks": {"world": "19", "region": "6", "realm": "2"},
                "item_level_average": 732.1,
                "item_level_ranks": {"world": "1", "region": "1", "realm": "1"},
                "first_kill_at": "Mar 10, 2025 03:35",
                "last_kill_at": "Apr 4, 2025 02:03",
                "encounter_count": 8,
                "page_url": "https://www.wowprogress.com/guild/us/mal-ganis/gn/rating.tier34",
            }
        ],
        "citations": {"page": "https://www.wowprogress.com/guild/us/mal-ganis/gn"},
    }

    monkeypatch.setattr("wowprogress_cli.main.WowProgressClient.fetch_guild_history", lambda self, **kwargs: history_payload)

    history_result = runner.invoke(wowprogress_app, ["guild-history", "us", "Mal'Ganis", "gn"])
    assert history_result.exit_code == 0
    history = json.loads(history_result.stdout)
    assert history["count"] == 1
    assert history["query"] == {"region": "us", "realm": "mal-ganis", "name": "gn"}
    assert history["tiers"][0]["raid"] == "Liberation of Undermine"
    assert history["tiers"][0]["final_ranks"]["world"] == "19"

    ranks_result = runner.invoke(wowprogress_app, ["guild-ranks", "us", "Mal'Ganis", "gn"])
    assert ranks_result.exit_code == 0
    ranks = json.loads(ranks_result.stdout)
    assert ranks["kind"] == "guild_ranks"
    assert ranks["tiers"][0]["final_ranks"]["realm"] == "2"


def test_wowprogress_search_normalizes_multi_word_realm(monkeypatch) -> None:
    calls: list[tuple[str, str, str, str]] = []

    def fake_probe(self, *, region: str, realm: str, name: str, obj_type: str):  # noqa: ANN001
        calls.append((region, realm, name, obj_type))
        if (region, realm, name, obj_type) == ("us", "area-52", "xD", "guild"):
            return {
                "_search_kind": "guild",
                "guild": {
                    "name": "xD",
                    "region": "us",
                    "realm": "US-Area 52",
                    "faction": "Horde",
                    "page_url": "https://www.wowprogress.com/guild/us/area-52/xD",
                },
            }
        return None

    monkeypatch.setattr("wowprogress_cli.main.WowProgressClient.probe_search_route", fake_probe)
    result = runner.invoke(wowprogress_app, ["search", "guild us area 52 xD"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["count"] == 1
    assert payload["results"][0]["follow_up"]["command"] == "wowprogress guild us area-52 xD"
    assert {"region": "us", "realm": "area-52", "name": "xD"} in payload["normalized_candidates"]
    assert ("us", "area-52", "xD", "guild") in calls


def test_wowprogress_search_excludes_trailing_terms_with_hint(monkeypatch) -> None:
    def fake_probe(self, *, region: str, realm: str, name: str, obj_type: str):  # noqa: ANN001
        assert region == "us"
        assert realm == "illidan"
        assert name == "Liquid"
        assert obj_type == "guild"
        return {
            "_search_kind": "guild",
            "guild": {
                "name": "Liquid",
                "region": "us",
                "realm": "US-Illidan",
                "faction": "Horde",
                "page_url": "https://www.wowprogress.com/guild/us/illidan/Liquid",
            },
        }

    monkeypatch.setattr("wowprogress_cli.main.WowProgressClient.probe_search_route", fake_probe)
    result = runner.invoke(wowprogress_app, ["search", "guild us illidan Liquid recruit"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["count"] == 1
    assert payload["excluded_terms"] == ["recruit"]
    assert payload["normalization_hint"]["code"] == "excluded_query_terms"
    assert payload["search_query"] == "guild us illidan Liquid"


def test_wowprogress_client_search_probe_cache_reads_final_url(monkeypatch) -> None:
    client = WowProgressClient()
    try:
        monkeypatch.setattr(
            client,
            "_read_cache",
            lambda key: json.dumps(
                {
                    "html": "<html><h1>Liquid Guild</h1></html>",
                    "final_url": "https://www.wowprogress.com/guild/us/illidan/Liquid",
                }
            ),
        )
        monkeypatch.setattr(
            "wowprogress_cli.client.parse_guild_page",
            lambda html, *, url, region, realm, name: {"guild": {"name": name, "region": region, "realm": realm, "page_url": url}},
        )
        payload = client.probe_search_route(region="us", realm="illidan", name="Liquid", obj_type="guild")
    finally:
        client.close()

    assert payload is not None
    assert payload["guild"]["page_url"] == "https://www.wowprogress.com/guild/us/illidan/Liquid"


def test_wowprogress_guild_summary(monkeypatch) -> None:
    def fake_fetch(self, *, region: str, realm: str, name: str):  # noqa: ANN001
        assert region == "us"
        assert realm == "illidan"
        assert name == "Liquid"
        return {
            "guild": {
                "name": "Liquid",
                "region": "us",
                "realm": "US-Illidan",
                "faction": "Horde",
                "page_url": "https://www.wowprogress.com/guild/us/illidan/Liquid",
                "armory_url": "https://worldofwarcraft.com/en-us/guild/illidan/liquid",
            },
            "progress": {
                "summary": "8/8 (M)",
                "ranks": {"world": "1", "region": "1", "realm": "1"},
            },
            "item_level": {
                "average": 724.51,
                "group_size": "20-man",
                "ranks": {"world": "9026", "region": "4149", "realm": "238"},
            },
            "encounters": {
                "count": 1,
                "items": [{"encounter": "Dimensius, the All-Devouring", "world_rank": "1"}],
            },
            "citations": {"page": "https://www.wowprogress.com/guild/us/illidan/Liquid"},
        }

    monkeypatch.setattr("wowprogress_cli.main.WowProgressClient.fetch_guild_page_variants", fake_fetch)
    result = runner.invoke(wowprogress_app, ["guild", "us", "illidan", "Liquid"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["guild"]["name"] == "Liquid"
    assert payload["progress"]["summary"] == "8/8 (M)"
    assert payload["encounters"]["count"] == 1


def test_wowprogress_character_summary(monkeypatch) -> None:
    def fake_fetch(self, *, region: str, realm: str, name: str):  # noqa: ANN001
        assert region == "us"
        assert realm == "illidan"
        assert name == "Imonthegcd"
        return {
            "character": {
                "name": "Imonthegcd",
                "region": "us",
                "realm": "US-Illidan",
                "guild_name": "Liquid",
                "guild_url": "https://www.wowprogress.com/guild/us/illidan/Liquid",
                "race": "Void Elf",
                "class_name": "Mage",
                "level": 90,
                "page_url": "https://www.wowprogress.com/character/us/illidan/Imonthegcd",
                "armory_url": "https://worldofwarcraft.com/en-us/character/illidan/Imonthegcd",
            },
            "profile": {"languages": "English"},
            "item_level": {"value": 728.81},
            "sim_dps": {"value": 6089532.23},
            "pve": {"score": 750000.0, "raids": [{"raid": "Manaforge Omega"}]},
            "citations": {"page": "https://www.wowprogress.com/character/us/illidan/Imonthegcd"},
        }

    monkeypatch.setattr("wowprogress_cli.main.WowProgressClient.fetch_character_page_variants", fake_fetch)
    result = runner.invoke(wowprogress_app, ["character", "us", "illidan", "Imonthegcd"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["character"]["name"] == "Imonthegcd"
    assert payload["item_level"]["value"] == 728.81
    assert payload["pve"]["score"] == 750000.0


def test_wowprogress_leaderboard(monkeypatch) -> None:
    def fake_fetch(self, *, region: str, realm: str | None = None, limit: int = 25):  # noqa: ANN001
        assert region == "us"
        assert realm is None
        assert limit == 10
        return {
            "leaderboard": {
                "kind": "pve",
                "title": "US Mythic Progress",
                "region": "us",
                "realm": None,
                "active_raid": "Manaforge Omega",
                "page_url": "https://www.wowprogress.com/pve/us",
            },
            "count": 2,
            "entries": [{"rank": 1, "guild_name": "Liquid"}, {"rank": 2, "guild_name": "Echo"}],
            "citations": {"page": "https://www.wowprogress.com/pve/us"},
        }

    monkeypatch.setattr("wowprogress_cli.main.WowProgressClient.fetch_pve_leaderboard", fake_fetch)
    result = runner.invoke(wowprogress_app, ["leaderboard", "pve", "us", "--limit", "10"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["leaderboard"]["active_raid"] == "Manaforge Omega"
    assert payload["count"] == 2


def test_wowprogress_error_maps_to_structured_error(monkeypatch) -> None:
    def fake_fetch(self, *, region: str, realm: str, name: str):  # noqa: ANN001
        from wowprogress_cli.client import WowProgressClientError

        raise WowProgressClientError("not_found", "WowProgress could not resolve that guild or character.")

    monkeypatch.setattr("wowprogress_cli.main.WowProgressClient.fetch_guild_page_variants", fake_fetch)
    result = runner.invoke(wowprogress_app, ["guild", "us", "illidan", "Missing"])
    assert result.exit_code == 1

    payload = json.loads(result.stderr)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "not_found"


def test_wowprogress_sample_pve_leaderboard(monkeypatch) -> None:
    def fake_fetch(self, *, region: str, realm: str | None = None, limit: int = 25):  # noqa: ANN001
        assert region == "us"
        assert realm is None
        assert limit == 10
        return {
            "leaderboard": {
                "kind": "pve",
                "title": "US Mythic Progress",
                "region": "us",
                "realm": None,
                "active_raid": "Manaforge Omega",
                "page_url": "https://www.wowprogress.com/pve/us",
            },
            "count": 2,
            "entries": [
                {
                    "rank": 1,
                    "guild_name": "Liquid",
                    "guild_url": "https://www.wowprogress.com/guild/us/illidan/Liquid",
                    "realm": "US-Illidan",
                    "realm_url": "https://www.wowprogress.com/pve/us/illidan",
                    "progress": "8/8 (M)",
                },
                {
                    "rank": 2,
                    "guild_name": "Echo",
                    "guild_url": "https://www.wowprogress.com/guild/eu/tarren-mill/Echo",
                    "realm": "EU-Tarren Mill",
                    "realm_url": "https://www.wowprogress.com/pve/eu/tarren-mill",
                    "progress": "8/8 (M)",
                },
            ],
            "citations": {"page": "https://www.wowprogress.com/pve/us"},
        }

    monkeypatch.setattr("wowprogress_cli.main.WowProgressClient.fetch_pve_leaderboard", fake_fetch)
    result = runner.invoke(wowprogress_app, ["sample", "pve-leaderboard", "--region", "us", "--limit", "10"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["kind"] == "pve_leaderboard_sample"
    assert payload["sample"]["entry_count"] == 2
    assert payload["sample"]["sampling"]["requested_limit"] == 10
    assert payload["sample"]["sampling"]["returned_entry_count"] == 2
    assert payload["sample"]["active_raid"] == "Manaforge Omega"
    assert payload["entries"][0]["bosses_killed"] == 8
    assert payload["entries"][0]["difficulty"] == "M"


def test_wowprogress_distribution_pve_leaderboard(monkeypatch) -> None:
    def fake_fetch(self, *, region: str, realm: str | None = None, limit: int = 25):  # noqa: ANN001
        return {
            "leaderboard": {
                "kind": "pve",
                "title": "US Mythic Progress",
                "region": "us",
                "realm": None,
                "active_raid": "Manaforge Omega",
                "page_url": "https://www.wowprogress.com/pve/us",
            },
            "count": 3,
            "entries": [
                {"rank": 1, "guild_name": "Liquid", "realm": "US-Illidan", "progress": "8/8 (M)"},
                {"rank": 2, "guild_name": "Echo", "realm": "EU-Tarren Mill", "progress": "8/8 (M)"},
                {"rank": 3, "guild_name": "Method", "realm": "EU-Twisting Nether", "progress": "7/8 (M)"},
            ],
            "citations": {"page": "https://www.wowprogress.com/pve/us"},
        }

    monkeypatch.setattr("wowprogress_cli.main.WowProgressClient.fetch_pve_leaderboard", fake_fetch)
    result = runner.invoke(
        wowprogress_app,
        ["distribution", "pve-leaderboard", "--region", "us", "--metric", "progress", "--limit", "10"],
    )
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["kind"] == "pve_leaderboard_distribution"
    assert payload["metric"] == "progress"
    assert payload["sample"]["sampling"]["requested_limit"] == 10
    assert payload["distribution"]["rows"][0]["value"] == "8/8 (M)"
    assert payload["distribution"]["rows"][0]["count"] == 2


def test_wowprogress_threshold_pve_leaderboard(monkeypatch) -> None:
    def fake_fetch(self, *, region: str, realm: str | None = None, limit: int = 25):  # noqa: ANN001
        return {
            "leaderboard": {
                "kind": "pve",
                "title": "US Mythic Progress",
                "region": "us",
                "realm": None,
                "active_raid": "Manaforge Omega",
                "page_url": "https://www.wowprogress.com/pve/us",
            },
            "count": 4,
            "entries": [
                {"rank": 1, "guild_name": "Liquid", "realm": "US-Illidan", "progress": "8/8 (M)"},
                {"rank": 2, "guild_name": "Echo", "realm": "EU-Tarren Mill", "progress": "8/8 (M)"},
                {"rank": 40, "guild_name": "Method", "realm": "EU-Twisting Nether", "progress": "7/8 (M)"},
                {"rank": 85, "guild_name": "Random", "realm": "US-Area 52", "progress": "6/8 (M)"},
            ],
            "citations": {"page": "https://www.wowprogress.com/pve/us"},
        }

    monkeypatch.setattr("wowprogress_cli.main.WowProgressClient.fetch_pve_leaderboard", fake_fetch)
    result = runner.invoke(
        wowprogress_app,
        ["threshold", "pve-leaderboard", "--region", "us", "--metric", "rank", "--value", "50", "--nearest", "2", "--limit", "10"],
    )
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["kind"] == "pve_leaderboard_threshold"
    assert payload["metric"] == "rank"
    assert payload["threshold"]["nearest_match_count"] == 2
    assert payload["threshold"]["estimate"]["metric"] == "bosses_killed"


def test_wowprogress_distribution_rejects_invalid_metric() -> None:
    result = runner.invoke(
        wowprogress_app,
        ["distribution", "pve-leaderboard", "--region", "us", "--metric", "invalid"],
    )
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_query"


def test_wowprogress_sample_pve_guild_profiles(monkeypatch) -> None:
    def fake_leaderboard(self, *, region: str, realm: str | None = None, limit: int = 25):  # noqa: ANN001
        assert region == "us"
        assert limit == 2
        return {
            "leaderboard": {
                "kind": "pve",
                "title": "US Mythic Progress",
                "region": "us",
                "realm": None,
                "active_raid": "Manaforge Omega",
                "page_url": "https://www.wowprogress.com/pve/us",
            },
            "count": 2,
            "entries": [
                {
                    "rank": 1,
                    "guild_name": "Liquid",
                    "guild_url": "https://www.wowprogress.com/guild/us/illidan/Liquid",
                    "realm": "US-Illidan",
                    "progress": "8/8 (M)",
                },
                {
                    "rank": 2,
                    "guild_name": "Echo",
                    "guild_url": "https://www.wowprogress.com/guild/eu/tarren-mill/Echo",
                    "realm": "EU-Tarren Mill",
                    "progress": "8/8 (M)",
                },
            ],
            "citations": {"page": "https://www.wowprogress.com/pve/us"},
        }

    def fake_guild_url(self, url: str):  # noqa: ANN001
        region, realm, name = url.rstrip("/").split("/")[-3:]
        return {
            "guild": {
                "name": name,
                "region": region,
                "realm": f"{region.upper()}-{realm.title()}",
                "faction": "Horde" if name == "Liquid" else "Alliance",
                "page_url": f"https://www.wowprogress.com/guild/{region}/{realm}/{name}",
                "armory_url": None,
            },
            "progress": {"summary": "8/8 (M)", "ranks": {"world": "1" if name == "Liquid" else "2", "region": "1", "realm": "1"}},
            "item_level": {"average": 724.51 if name == "Liquid" else 723.1, "group_size": "20-man", "ranks": {"world": "10", "region": "5", "realm": "1"}},
            "encounters": {"count": 1, "items": [{"encounter": "Dimensius, the All-Devouring", "difficulty": "M", "world_rank": "1"}]},
            "citations": {"page": f"https://www.wowprogress.com/guild/{region}/{realm}/{name}"},
        }

    monkeypatch.setattr("wowprogress_cli.main.WowProgressClient.fetch_pve_leaderboard", fake_leaderboard)
    monkeypatch.setattr("wowprogress_cli.main.WowProgressClient.fetch_guild_page_url", fake_guild_url)
    result = runner.invoke(wowprogress_app, ["sample", "pve-guild-profiles", "--region", "us", "--limit", "2"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["kind"] == "pve_guild_profiles_sample"
    assert payload["sample"]["guild_profile_count"] == 2
    assert payload["sample"]["sampling"]["source_leaderboard_entry_count"] == 2
    assert payload["sample"]["sampling"]["returned_guild_profile_count"] == 2
    assert payload["sample"]["sampling"]["skipped_missing_profile_url"] == 0
    assert payload["guild_profiles"][0]["item_level_average"] is not None
    assert payload["guild_profiles"][0]["progress_ranks"]["world"] is not None


def test_wowprogress_normalized_encounter_values() -> None:
    entry = {
        "encounters": [
            {"encounter": "Dimensius, the All-Devouring"},
            {"encounter": "Nexus-King Salhadaar"},
        ]
    }

    values = _normalized_encounter_values(entry)

    assert values == {"dimensius-the-all-devouring", "nexus-king-salhadaar"}


def test_wowprogress_guild_profile_matches_filters_uses_normalized_fields() -> None:
    entry = {
        "faction": "Horde",
        "difficulty": "M",
        "progress_ranks": {"world": "12"},
        "item_level_average": 724.5,
        "encounters": [{"encounter": "Dimensius, the All-Devouring"}],
    }

    assert (
        _guild_profile_matches_filters(
            entry,
            faction=["horde"],
            difficulty=["m"],
            world_rank_min=10,
            world_rank_max=20,
            item_level_min=720.0,
            item_level_max=730.0,
            encounter=["dimensius-the-all-devouring"],
        )
        is True
    )
    assert (
        _guild_profile_matches_filters(
            entry,
            faction=["alliance"],
            difficulty=[],
            world_rank_min=None,
            world_rank_max=None,
            item_level_min=None,
            item_level_max=None,
            encounter=[],
        )
        is False
    )


def test_wowprogress_guild_profile_distribution_values_cover_numeric_and_encounter_metrics() -> None:
    entries = [
        {
            "faction": "Horde",
            "progress": "8/8 (M)",
            "encounters": [{"encounter": "Dimensius, the All-Devouring"}],
            "progress_ranks": {"world": "12"},
            "item_level_average": 724.5,
        }
    ]

    values, unit, numeric = _guild_profile_distribution_values("encounter", entries)
    assert values == ["Dimensius, the All-Devouring"]
    assert unit == "encounters"
    assert numeric is False

    values, unit, numeric = _guild_profile_distribution_values("world_rank", entries)
    assert values == [12]
    assert unit == "guild_profiles"
    assert numeric is True


def test_wowprogress_guild_profile_threshold_estimate_switches_metric() -> None:
    nearest = [
        {
            "entry": {
                "progress_ranks": {"world": "12"},
                "item_level_average": 724.5,
            }
        }
    ]

    metric, values, caveat = _guild_profile_threshold_estimate("item_level_average", nearest)
    assert metric == "world_rank"
    assert values == [12]
    assert "world-progress ranks" in caveat

    metric, values, caveat = _guild_profile_threshold_estimate("world_rank", nearest)
    assert metric == "item_level_average"
    assert values == [724.5]
    assert "item-level averages" in caveat


def test_wowprogress_numeric_summary_handles_empty_and_populated_values() -> None:
    assert _numeric_summary([]) is None
    summary = _numeric_summary([724.5, 721.0, 724.0])
    assert summary is not None
    assert summary["min"] == 721.0
    assert summary["max"] == 724.5


def test_wowprogress_distribution_pve_guild_profiles(monkeypatch) -> None:
    def fake_leaderboard(self, *, region: str, realm: str | None = None, limit: int = 25):  # noqa: ANN001
        return {
            "leaderboard": {"kind": "pve", "title": "US Mythic Progress", "region": "us", "realm": None, "active_raid": "Manaforge Omega", "page_url": "https://www.wowprogress.com/pve/us"},
            "count": 2,
            "entries": [
                {"rank": 1, "guild_name": "Liquid", "guild_url": "https://www.wowprogress.com/guild/us/illidan/Liquid", "realm": "US-Illidan", "progress": "8/8 (M)"},
                {"rank": 2, "guild_name": "Echo", "guild_url": "https://www.wowprogress.com/guild/eu/tarren-mill/Echo", "realm": "EU-Tarren Mill", "progress": "7/8 (M)"},
            ],
            "citations": {"page": "https://www.wowprogress.com/pve/us"},
        }

    def fake_guild_url(self, url: str):  # noqa: ANN001
        region, realm, name = url.rstrip("/").split("/")[-3:]
        return {
            "guild": {"name": name, "region": region, "realm": realm, "faction": "Horde" if name == "Liquid" else "Alliance", "page_url": f"https://www.wowprogress.com/guild/{region}/{realm}/{name}"},
            "progress": {"summary": "8/8 (M)" if name == "Liquid" else "7/8 (M)", "ranks": {"world": "1" if name == "Liquid" else "25"}},
            "item_level": {"average": 724.5 if name == "Liquid" else 721.0},
            "encounters": {"count": 1, "items": [{"encounter": "Dimensius, the All-Devouring"}]},
            "citations": {"page": f"https://www.wowprogress.com/guild/{region}/{realm}/{name}"},
        }

    monkeypatch.setattr("wowprogress_cli.main.WowProgressClient.fetch_pve_leaderboard", fake_leaderboard)
    monkeypatch.setattr("wowprogress_cli.main.WowProgressClient.fetch_guild_page_url", fake_guild_url)
    result = runner.invoke(
        wowprogress_app,
        ["distribution", "pve-guild-profiles", "--region", "us", "--metric", "faction", "--limit", "2"],
    )
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["kind"] == "pve_guild_profiles_distribution"
    assert payload["metric"] == "faction"
    assert payload["sample"]["sampling"]["source_leaderboard_entry_count"] == 2
    assert payload["distribution"]["rows"][0]["count"] == 1


def test_wowprogress_threshold_pve_guild_profiles(monkeypatch) -> None:
    def fake_leaderboard(self, *, region: str, realm: str | None = None, limit: int = 25):  # noqa: ANN001
        return {
            "leaderboard": {"kind": "pve", "title": "US Mythic Progress", "region": "us", "realm": None, "active_raid": "Manaforge Omega", "page_url": "https://www.wowprogress.com/pve/us"},
            "count": 3,
            "entries": [
                {"rank": 1, "guild_name": "Liquid", "guild_url": "https://www.wowprogress.com/guild/us/illidan/Liquid", "realm": "US-Illidan", "progress": "8/8 (M)"},
                {"rank": 2, "guild_name": "Echo", "guild_url": "https://www.wowprogress.com/guild/eu/tarren-mill/Echo", "realm": "EU-Tarren Mill", "progress": "8/8 (M)"},
                {"rank": 40, "guild_name": "Method", "guild_url": "https://www.wowprogress.com/guild/eu/twisting-nether/Method", "realm": "EU-Twisting Nether", "progress": "7/8 (M)"},
            ],
            "citations": {"page": "https://www.wowprogress.com/pve/us"},
        }

    def fake_guild_url(self, url: str):  # noqa: ANN001
        region, realm, name = url.rstrip("/").split("/")[-3:]
        values = {
            "Liquid": ("1", 724.5),
            "Echo": ("2", 724.0),
            "Method": ("40", 721.0),
        }
        world_rank, item_level = values[name]
        return {
            "guild": {"name": name, "region": region, "realm": realm, "faction": "Horde", "page_url": f"https://www.wowprogress.com/guild/{region}/{realm}/{name}"},
            "progress": {"summary": "8/8 (M)" if name != "Method" else "7/8 (M)", "ranks": {"world": world_rank}},
            "item_level": {"average": item_level},
            "encounters": {"count": 0, "items": []},
            "citations": {"page": f"https://www.wowprogress.com/guild/{region}/{realm}/{name}"},
        }

    monkeypatch.setattr("wowprogress_cli.main.WowProgressClient.fetch_pve_leaderboard", fake_leaderboard)
    monkeypatch.setattr("wowprogress_cli.main.WowProgressClient.fetch_guild_page_url", fake_guild_url)
    result = runner.invoke(
        wowprogress_app,
        ["threshold", "pve-guild-profiles", "--region", "us", "--metric", "world_rank", "--value", "10", "--nearest", "2", "--limit", "3"],
    )
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["kind"] == "pve_guild_profiles_threshold"
    assert payload["metric"] == "world_rank"
    assert payload["sample"]["sampling"]["source_leaderboard_entry_count"] == 3
    assert payload["threshold"]["nearest_match_count"] == 2
    assert payload["threshold"]["estimate"]["metric"] == "item_level_average"


def test_wowprogress_sample_pve_guild_profiles_filters(monkeypatch) -> None:
    def fake_leaderboard(self, *, region: str, realm: str | None = None, limit: int = 25):  # noqa: ANN001
        return {
            "leaderboard": {"kind": "pve", "title": "US Mythic Progress", "region": "us", "realm": None, "active_raid": "Manaforge Omega", "page_url": "https://www.wowprogress.com/pve/us"},
            "count": 2,
            "entries": [
                {"rank": 1, "guild_name": "Liquid", "guild_url": "https://www.wowprogress.com/guild/us/illidan/Liquid", "realm": "US-Illidan", "progress": "8/8 (M)"},
                {"rank": 2, "guild_name": "Echo", "guild_url": "https://www.wowprogress.com/guild/eu/tarren-mill/Echo", "realm": "EU-Tarren Mill", "progress": "8/8 (M)"},
            ],
            "citations": {"page": "https://www.wowprogress.com/pve/us"},
        }

    def fake_guild_url(self, url: str):  # noqa: ANN001
        region, realm, name = url.rstrip("/").split("/")[-3:]
        if name == "Liquid":
            return {
                "guild": {"name": name, "region": region, "realm": realm, "faction": "Horde", "page_url": url},
                "progress": {"summary": "8/8 (M)", "ranks": {"world": "1"}},
                "item_level": {"average": 724.5},
                "encounters": {"count": 1, "items": [{"encounter": "Dimensius, the All-Devouring"}]},
                "citations": {"page": url},
            }
        return {
            "guild": {"name": name, "region": region, "realm": realm, "faction": "Alliance", "page_url": url},
            "progress": {"summary": "8/8 (M)", "ranks": {"world": "25"}},
            "item_level": {"average": 721.0},
            "encounters": {"count": 1, "items": [{"encounter": "Nexus-King Salhadaar"}]},
            "citations": {"page": url},
        }

    monkeypatch.setattr("wowprogress_cli.main.WowProgressClient.fetch_pve_leaderboard", fake_leaderboard)
    monkeypatch.setattr("wowprogress_cli.main.WowProgressClient.fetch_guild_page_url", fake_guild_url)
    result = runner.invoke(
        wowprogress_app,
        ["sample", "pve-guild-profiles", "--region", "us", "--limit", "10", "--faction", "horde", "--world-rank-max", "10", "--encounter", "dimensius-the-all-devouring"],
    )
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["query"]["filters"]["faction"] == ["horde"]
    assert payload["query"]["filters"]["encounter"] == ["dimensius-the-all-devouring"]
    assert payload["sample"]["filtering"]["source_profile_count"] == 2
    assert payload["sample"]["filtering"]["returned_profile_count"] == 1
    assert payload["guild_profiles"][0]["guild_name"] == "Liquid"


def test_wowprogress_distribution_pve_guild_profiles_filters(monkeypatch) -> None:
    def fake_leaderboard(self, *, region: str, realm: str | None = None, limit: int = 25):  # noqa: ANN001
        return {
            "leaderboard": {"kind": "pve", "title": "US Mythic Progress", "region": "us", "realm": None, "active_raid": "Manaforge Omega", "page_url": "https://www.wowprogress.com/pve/us"},
            "count": 2,
            "entries": [
                {"rank": 1, "guild_name": "Liquid", "guild_url": "https://www.wowprogress.com/guild/us/illidan/Liquid", "realm": "US-Illidan", "progress": "8/8 (M)"},
                {"rank": 2, "guild_name": "Echo", "guild_url": "https://www.wowprogress.com/guild/eu/tarren-mill/Echo", "realm": "EU-Tarren Mill", "progress": "7/8 (M)"},
            ],
            "citations": {"page": "https://www.wowprogress.com/pve/us"},
        }

    def fake_guild_url(self, url: str):  # noqa: ANN001
        region, realm, name = url.rstrip("/").split("/")[-3:]
        return {
            "guild": {"name": name, "region": region, "realm": realm, "faction": "Horde" if name == "Liquid" else "Alliance", "page_url": url},
            "progress": {"summary": "8/8 (M)" if name == "Liquid" else "7/8 (M)", "ranks": {"world": "1" if name == "Liquid" else "25"}},
            "item_level": {"average": 724.5 if name == "Liquid" else 721.0},
            "encounters": {"count": 1, "items": [{"encounter": "Dimensius, the All-Devouring"}]},
            "citations": {"page": url},
        }

    monkeypatch.setattr("wowprogress_cli.main.WowProgressClient.fetch_pve_leaderboard", fake_leaderboard)
    monkeypatch.setattr("wowprogress_cli.main.WowProgressClient.fetch_guild_page_url", fake_guild_url)
    result = runner.invoke(
        wowprogress_app,
        ["distribution", "pve-guild-profiles", "--region", "us", "--metric", "item_level_average", "--faction", "horde"],
    )
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["sample"]["filtering"]["returned_profile_count"] == 1
    assert payload["distribution"]["statistics"]["max"] == 724.5


def test_wowprogress_threshold_pve_guild_profiles_filters_to_empty(monkeypatch) -> None:
    def fake_leaderboard(self, *, region: str, realm: str | None = None, limit: int = 25):  # noqa: ANN001
        return {
            "leaderboard": {"kind": "pve", "title": "US Mythic Progress", "region": "us", "realm": None, "active_raid": "Manaforge Omega", "page_url": "https://www.wowprogress.com/pve/us"},
            "count": 1,
            "entries": [
                {"rank": 1, "guild_name": "Liquid", "guild_url": "https://www.wowprogress.com/guild/us/illidan/Liquid", "realm": "US-Illidan", "progress": "8/8 (M)"},
            ],
            "citations": {"page": "https://www.wowprogress.com/pve/us"},
        }

    def fake_guild_url(self, url: str):  # noqa: ANN001
        return {
            "guild": {"name": "Liquid", "region": "us", "realm": "illidan", "faction": "Horde", "page_url": url},
            "progress": {"summary": "8/8 (M)", "ranks": {"world": "1"}},
            "item_level": {"average": 724.5},
            "encounters": {"count": 1, "items": [{"encounter": "Dimensius, the All-Devouring"}]},
            "citations": {"page": url},
        }

    monkeypatch.setattr("wowprogress_cli.main.WowProgressClient.fetch_pve_leaderboard", fake_leaderboard)
    monkeypatch.setattr("wowprogress_cli.main.WowProgressClient.fetch_guild_page_url", fake_guild_url)
    result = runner.invoke(
        wowprogress_app,
        ["threshold", "pve-guild-profiles", "--region", "us", "--metric", "world_rank", "--value", "10", "--faction", "alliance"],
    )
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["sample"]["filtering"]["returned_profile_count"] == 0
    assert payload["threshold"]["nearest_match_count"] == 0
    assert payload["threshold"]["estimate"] is None


def test_wowprogress_sample_pve_guild_profiles_reports_missing_urls(monkeypatch) -> None:
    def fake_leaderboard(self, *, region: str, realm: str | None = None, limit: int = 25):  # noqa: ANN001
        return {
            "leaderboard": {"kind": "pve", "title": "US Mythic Progress", "region": "us", "realm": None, "active_raid": "Manaforge Omega", "page_url": "https://www.wowprogress.com/pve/us"},
            "count": 2,
            "entries": [
                {"rank": 1, "guild_name": "Liquid", "guild_url": "https://www.wowprogress.com/guild/us/illidan/Liquid", "realm": "US-Illidan", "progress": "8/8 (M)"},
                {"rank": 2, "guild_name": "Unknown", "guild_url": None, "realm": "US-Illidan", "progress": "7/8 (M)"},
            ],
            "citations": {"page": "https://www.wowprogress.com/pve/us"},
        }

    def fake_guild_url(self, url: str):  # noqa: ANN001
        return {
            "guild": {"name": "Liquid", "region": "us", "realm": "US-Illidan", "faction": "Horde", "page_url": url},
            "progress": {"summary": "8/8 (M)", "ranks": {"world": "1"}},
            "item_level": {"average": 724.5},
            "encounters": {"count": 0, "items": []},
            "citations": {"page": url},
        }

    monkeypatch.setattr("wowprogress_cli.main.WowProgressClient.fetch_pve_leaderboard", fake_leaderboard)
    monkeypatch.setattr("wowprogress_cli.main.WowProgressClient.fetch_guild_page_url", fake_guild_url)
    result = runner.invoke(wowprogress_app, ["sample", "pve-guild-profiles", "--region", "us", "--limit", "10"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["sample"]["sampling"]["source_leaderboard_entry_count"] == 2
    assert payload["sample"]["sampling"]["returned_guild_profile_count"] == 1
    assert payload["sample"]["sampling"]["skipped_missing_profile_url"] == 1


def test_wowprogress_sample_pve_guild_profiles_decodes_plus_names(monkeypatch) -> None:
    seen: list[tuple[str, str, str]] = []

    def fake_leaderboard(self, *, region: str, realm: str | None = None, limit: int = 25):  # noqa: ANN001
        return {
            "leaderboard": {"kind": "pve", "title": "US Mythic Progress", "region": "us", "realm": None, "active_raid": "Manaforge Omega", "page_url": "https://www.wowprogress.com/pve/us"},
            "count": 1,
            "entries": [
                {
                    "rank": 2,
                    "guild_name": "Instant Dollars",
                    "guild_url": "https://www.wowprogress.com/guild/us/mal-ganis/Instant+Dollars",
                    "realm": "US-Mal'Ganis",
                    "progress": "8/8 (M)",
                }
            ],
            "citations": {"page": "https://www.wowprogress.com/pve/us"},
        }

    def fake_guild_url(self, url: str):  # noqa: ANN001
        region, realm, name = url.rstrip("/").split("/")[-3:]
        seen.append((region, realm, name))
        return {
            "guild": {"name": name, "region": region, "realm": realm, "faction": "Horde", "page_url": f"https://www.wowprogress.com/guild/{region}/{realm}/{name}"},
            "progress": {"summary": "8/8 (M)", "ranks": {"world": "2"}},
            "item_level": {"average": 723.5},
            "encounters": {"count": 0, "items": []},
            "citations": {"page": f"https://www.wowprogress.com/guild/{region}/{realm}/{name}"},
        }

    monkeypatch.setattr("wowprogress_cli.main.WowProgressClient.fetch_pve_leaderboard", fake_leaderboard)
    monkeypatch.setattr("wowprogress_cli.main.WowProgressClient.fetch_guild_page_url", fake_guild_url)
    result = runner.invoke(wowprogress_app, ["sample", "pve-guild-profiles", "--region", "us", "--limit", "1"])
    assert result.exit_code == 0
    assert seen == [("us", "mal-ganis", "Instant+Dollars")]

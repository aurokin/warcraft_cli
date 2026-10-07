"""Synthetic cohort regressions: keep seasons and distinct pulls separate."""

from collections import Counter
from pathlib import Path
from typing import Any

import httpx
import pytest
from raiderio_cli.analytics import SampleRequest, sample_leaderboard_runs
from raiderio_cli.client import FetchedJson, RaiderIOClient
from warcraft_core.paths import provider_cache_root
from warcraft_core.provider import ProviderError
from warcraftlogs_cli.boss_kills import deduplicate_pulls
from warcraftlogs_cli.client import WarcraftLogsClient, WarcraftLogsClientError, load_warcraftlogs_cache_settings_from_env


def test_warcraftlogs_default_cache_follows_the_current_xdg_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in ("before", "after"):
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / name))
        settings, *_ = load_warcraftlogs_cache_settings_from_env()
        assert settings.cache_dir == provider_cache_root("warcraftlogs") / "http"
        assert settings.cache_dir.is_relative_to(tmp_path / name)


@pytest.mark.parametrize("realm", ["Azjol-Nerub", "Ревущий фьорд"])
def test_warcraftlogs_guild_reports_retry_canonical_and_localized_realms(monkeypatch: pytest.MonkeyPatch, realm: str) -> None:
    client = WarcraftLogsClient.__new__(WarcraftLogsClient)
    client._report_ttl = 60
    calls: list[dict[str, Any]] = []
    expected_slug = "azjolnerub" if realm.isascii() else "howling-fjord"

    def graphql(*, variables: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        calls.append(variables)
        if variables["guildServerSlug"] != expected_slug:
            raise WarcraftLogsClientError("not_found", "No guild exists for this name/server/region.")
        return {"reportData": {"reports": {"data": [{"code": "A"}], "current_page": 2}}}

    monkeypatch.setattr(client, "_graphql", graphql)
    monkeypatch.setattr(client, "_server_slug_by_name", lambda region, name: "howling-fjord")
    found = client.reports(guild_region="eu", guild_realm=realm, guild_name="My Guild", page=2, zone_id=42, limit=7)
    assert found == {"data": [{"code": "A"}], "current_page": 2}
    assert calls[-1]["guildServerSlug"] == expected_slug
    assert len(calls) == (2 if realm.isascii() else 3)
    assert all(call["page"] == 2 and call["zoneID"] == 42 and call["limit"] == 7 for call in calls)


def test_warcraftlogs_empty_guild_reports_are_valid_and_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    client = WarcraftLogsClient.__new__(WarcraftLogsClient)
    client._report_ttl = 60
    calls: list[str] = []

    def graphql(*, variables: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        calls.append(variables["guildServerSlug"])
        return {"reportData": {"reports": {"data": [], "has_more_pages": False}}}

    monkeypatch.setattr(client, "_graphql", graphql)
    assert client.reports(guild_region="us", guild_realm="Azjol-Nerub", guild_name="Empty")["data"] == []
    assert calls == ["azjol-nerub"]


def test_warcraftlogs_client_credentials_reject_a_non_object_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WARCRAFTLOGS_CLIENT_ID", "synthetic-id")
    monkeypatch.setenv("WARCRAFTLOGS_CLIENT_SECRET", "synthetic-secret")
    response = httpx.Response(200, json=[], request=httpx.Request("POST", "https://www.warcraftlogs.com/oauth/token"))
    monkeypatch.setattr("warcraftlogs_cli.client._request", lambda *args, **kwargs: response)
    client = WarcraftLogsClient()
    try:
        with pytest.raises(WarcraftLogsClientError) as caught:
            client._token()
        assert caught.value.code == "invalid_response"
    finally:
        client.close()


def _pulls(levels: tuple[int, ...] = (10, 10, 10)) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    return [
        (
            {"code": f"report{index}", "startTime": 100_000 + index * 100, "guild": {"id": 1}},
            {"id": 1, "encounterID": 123, "difficulty": 8, "size": 5, "keystoneLevel": level, "startTime": 0, "endTime": 60_000},
        )
        for index, level in enumerate(levels)
    ]


def test_warcraftlogs_dedup_keeps_different_keystone_levels_without_roster_reads() -> None:
    def roster(report: dict[str, Any], fight: dict[str, Any]) -> frozenset[str]:
        pytest.fail("different key levels are different pulls before roster lookup")

    assert len(deduplicate_pulls(_pulls((10, 12)), roster=roster)) == 2


def test_warcraftlogs_dedup_keeps_contradictory_guild_rosters_and_memoizes_reads() -> None:
    calls: Counter[str] = Counter()

    def roster(report: dict[str, Any], fight: dict[str, Any]) -> frozenset[str]:
        code = report["code"]
        calls[code] += 1
        return frozenset({"A", "B"}) if code != "report1" else frozenset({"X", "Y"})

    pulls = deduplicate_pulls(_pulls(), roster=roster)
    assert [pull.report["code"] for pull in pulls] == ["report0", "report1"]
    assert pulls[0].duplicates == [{"report_code": "report2", "fight_id": 1}]
    assert calls == {"report0": 1, "report1": 1, "report2": 1}


def test_warcraftlogs_dedup_remembers_roster_evidence_when_first_upload_has_none() -> None:
    rosters = {"report0": frozenset(), "report1": frozenset({"A"}), "report2": frozenset({"B"})}
    pulls = deduplicate_pulls(_pulls(), roster=lambda report, fight: rosters[report["code"]])
    assert len(pulls) == 2
    assert pulls[0].duplicates == [{"report_code": "report1", "fight_id": 1}]


def _sample_request(*, season: str = "current") -> SampleRequest:
    return SampleRequest(season=season, region="us", dungeon="all", affixes="", page=0, pages=2, limit=2)


def _season_page(season: str | None, page: int, *, cached: bool = False) -> FetchedJson:
    return FetchedJson(
        payload={"params": {"season": season}, "rankings": [{"rank": page + 1, "run": {"keystone_run_id": page + 1}}]},
        fetched_at="2026-10-01T00:00:00+00:00" if cached else "2026-10-07T00:00:00+00:00",
        cache_hit=cached,
    )


def test_raiderio_current_season_pagination_pins_the_cached_first_page(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str | None, int]] = []

    def fetch(self: RaiderIOClient, *, season: str | None, page: int, **kwargs: Any) -> FetchedJson:
        calls.append((season, page))
        served = "old-season" if page == 0 or season == "old-season" else "new-season"
        return _season_page(served, page, cached=page == 0)

    monkeypatch.setattr(RaiderIOClient, "mythic_plus_runs", fetch)
    runs, sample = sample_leaderboard_runs(RaiderIOClient(), _sample_request())
    assert calls == [(None, 0), ("old-season", 1)]
    assert len(runs) == 2 and sample["season"] == "old-season"
    assert sample["cache_hit"] is True and sample["fetched_at"] == "2026-10-01T00:00:00+00:00"


@pytest.mark.parametrize("season", ["current", "old-season"])
def test_raiderio_rejects_inconsistent_pagination_seasons(monkeypatch: pytest.MonkeyPatch, season: str) -> None:
    monkeypatch.setattr(RaiderIOClient, "mythic_plus_runs", lambda self, *, page, **kwargs: _season_page("old-season" if page == 0 else "new-season", page))
    with pytest.raises(ProviderError) as caught:
        sample_leaderboard_runs(RaiderIOClient(), _sample_request(season=season))
    assert caught.value.code == "invalid_response"
    assert caught.value.details == {"expected_season": "old-season", "served_season": "new-season", "page": 1}


def test_raiderio_cannot_paginate_an_unidentified_current_season(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def fetch(self: RaiderIOClient, *, page: int, **kwargs: Any) -> FetchedJson:
        calls.append(page)
        return _season_page(None, page)

    monkeypatch.setattr(RaiderIOClient, "mythic_plus_runs", fetch)
    with pytest.raises(ProviderError, match="identify the season"):
        sample_leaderboard_runs(RaiderIOClient(), _sample_request())
    assert calls == [0]


def test_warcraftlogs_spec_filter_reuses_duplicate_confirmation_details(monkeypatch: pytest.MonkeyPatch) -> None:
    from warcraftlogs_cli.boss_kills import _scan_reports_for_boss_kills

    calls: Counter[str] = Counter()
    candidates = [(report, {**fight, "kill": True}) for report, fight in _pulls((10, 10))]
    fights = {report["code"]: fight for report, fight in candidates}
    client = WarcraftLogsClient.__new__(WarcraftLogsClient)
    client._finished_report_ttl = 86400
    monkeypatch.setattr(client, "report_fights", lambda *, code, **kwargs: {"fights": [fights[code]]})

    def details(*, code: str, **kwargs: Any) -> dict[str, Any]:
        calls[code] += 1
        return {"playerDetails": {"data": {"dps": [{"id": 1, "name": "A", "server": "Realm", "type": "Mage", "specs": [{"spec": "Frost"}]}]}}}

    monkeypatch.setattr(client, "report_player_details", details)
    scanned = _scan_reports_for_boss_kills(
        client, [report for report, fight in candidates], boss_id=123, boss_name=None,
        difficulty=8, spec_name="Frost", kill_time_min=None, kill_time_max=None,
    )
    assert len(scanned.rows) == 1 and scanned.duplicates_removed == 1
    assert scanned.rows[0]["matching_players"][0]["name"] == "A"
    assert calls == {"report0": 1, "report1": 1}

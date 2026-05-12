from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner
from warcraft_core.auth import provider_auth_status
from warcraftlogs_cli.client import load_warcraftlogs_auth_config
from warcraftlogs_cli.main import app

runner = CliRunner()


def _payload_for(args: list[str]) -> dict[str, object]:
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def _require_warcraftlogs_auth() -> None:
    if not load_warcraftlogs_auth_config().configured:
        pytest.skip("Warcraft Logs credentials are not configured.")


def _require_warcraftlogs_user_auth() -> None:
    state = provider_auth_status("warcraftlogs")
    if not (state.get("has_access_token") and state.get("auth_mode") in {"authorization_code", "pkce"} and not state.get("expired")):
        pytest.skip("Warcraft Logs user auth token is not configured.")


def _public_raid_report() -> tuple[str, int]:
    for page in (1, 2):
        payload = _payload_for(["reports", "--zone-id", "38", "--limit", "5", "--page", str(page)])
        reports = payload.get("reports")
        assert isinstance(reports, list), payload
        for report in reports:
            code = report.get("code")
            if not isinstance(code, str) or not code:
                continue
            fights_payload = _payload_for(["report-fights", code, "--difficulty", "5"])
            fights = fights_payload.get("fights")
            if not isinstance(fights, list) or not fights:
                continue
            fight_id = fights[0].get("id")
            if isinstance(fight_id, int):
                return code, fight_id
    raise AssertionError("Could not find a sampled public report with at least one mythic fight.")


@pytest.mark.live
def test_live_warcraftlogs_regions_contract() -> None:
    _require_warcraftlogs_auth()
    payload = _payload_for(["regions"])

    assert payload["provider"] == "warcraftlogs"
    assert payload["count"] >= 1
    assert any(region["slug"] == "us" for region in payload["regions"])


@pytest.mark.live
def test_live_warcraftlogs_auth_metadata_contract() -> None:
    _require_warcraftlogs_auth()

    status_payload = _payload_for(["auth", "status"])
    assert status_payload["provider"] == "warcraftlogs"
    assert status_payload["auth"]["configured"] is True

    client_payload = _payload_for(["auth", "client"])
    assert client_payload["provider"] == "warcraftlogs"
    assert client_payload["client"]["configured"] is True
    assert client_payload["client"]["client_api_url"].endswith("/api/v2/client")


@pytest.mark.live
def test_live_warcraftlogs_server_contract() -> None:
    _require_warcraftlogs_auth()
    payload = _payload_for(["server", "us", "illidan"])

    assert payload["provider"] == "warcraftlogs"
    assert payload["server"]["slug"] == "illidan"
    assert payload["server"]["region"]["slug"] == "us"


@pytest.mark.live
def test_live_warcraftlogs_expansions_and_zone_contracts() -> None:
    _require_warcraftlogs_auth()
    expansions_payload = _payload_for(["expansions"])

    assert expansions_payload["provider"] == "warcraftlogs"
    assert expansions_payload["count"] >= 1
    expansion = expansions_payload["expansions"][0]
    assert "id" in expansion
    assert "name" in expansion

    zone_payload = _payload_for(["zone", "38"])
    assert zone_payload["provider"] == "warcraftlogs"
    assert zone_payload["zone"]["id"] == 38
    assert isinstance(zone_payload["zone"]["encounters"], list)
    assert isinstance(zone_payload["zone"]["partitions"], list)


@pytest.mark.live
def test_live_warcraftlogs_guild_contract() -> None:
    _require_warcraftlogs_auth()
    payload = _payload_for(["guild", "us", "illidan", "Liquid"])

    assert payload["provider"] == "warcraftlogs"
    assert payload["guild"]["name"] == "Liquid"
    assert payload["guild"]["server"]["slug"] == "illidan"


@pytest.mark.live
def test_live_warcraftlogs_guild_members_contract() -> None:
    _require_warcraftlogs_auth()
    payload = _payload_for(["guild-members", "us", "illidan", "Liquid", "--limit", "5"])

    assert payload["provider"] == "warcraftlogs"
    assert payload["guild_members"]["name"] == "Liquid"
    assert isinstance(payload["guild_members"]["pagination"], dict)
    assert isinstance(payload["guild_members"]["members"], list)


@pytest.mark.live
def test_live_warcraftlogs_guild_rankings_contract() -> None:
    _require_warcraftlogs_auth()
    payload = _payload_for(["guild-rankings", "us", "illidan", "Liquid", "--zone-id", "38", "--size", "20", "--difficulty", "5"])

    assert payload["provider"] == "warcraftlogs"
    assert payload["guild_rankings"]["name"] == "Liquid"
    assert "progress" in payload["guild_rankings"]["zone_ranking"]


@pytest.mark.live
def test_live_warcraftlogs_reports_contract() -> None:
    _require_warcraftlogs_auth()
    payload = _payload_for(["reports", "--guild-region", "us", "--guild-realm", "illidan", "--guild-name", "Liquid", "--limit", "2"])

    assert payload["provider"] == "warcraftlogs"
    assert payload["count"] >= 1
    report = payload["reports"][0]
    assert "code" in report
    assert isinstance(report["archive_status"], dict) or report["archive_status"] is None

    guild_payload = _payload_for(["guild-reports", "us", "illidan", "Liquid", "--limit", "2"])
    assert guild_payload["provider"] == "warcraftlogs"
    assert guild_payload["guild"]["name"] == "Liquid"
    assert isinstance(guild_payload["reports"], list)


@pytest.mark.live
def test_live_warcraftlogs_boss_kills_contract() -> None:
    _require_warcraftlogs_auth()
    payload = _payload_for(
        ["boss-kills", "--zone-id", "38", "--boss-id", "3012", "--difficulty", "5", "--top", "3", "--report-pages", "1", "--reports-per-page", "5"]
    )

    assert payload["provider"] == "warcraftlogs"
    assert payload["kind"] == "boss_kills"
    assert payload["ranking_basis"] == "sampled_fastest_kills"
    assert payload["sample"]["source_report_count"] >= payload["sample"]["finished_report_count"]
    assert payload["sample"]["filtered_kill_count"] >= payload["count"]
    assert isinstance(payload["kills"], list)


@pytest.mark.live
def test_live_warcraftlogs_boss_spec_usage_contract() -> None:
    _require_warcraftlogs_auth()
    payload = _payload_for(
        ["boss-spec-usage", "--zone-id", "38", "--boss-id", "3012", "--difficulty", "5", "--top", "5", "--report-pages", "1", "--reports-per-page", "5"]
    )

    assert payload["provider"] == "warcraftlogs"
    assert payload["kind"] == "boss_spec_usage"
    assert payload["ranking_basis"] == "sampled_finished_kill_cohort_spec_presence"
    assert payload["sample"]["filtered_kill_count"] >= 0
    assert payload["sample"]["distinct_spec_count"] >= payload["count"]
    assert isinstance(payload["spec_usage"], list)


@pytest.mark.live
def test_live_warcraftlogs_report_encounter_contracts() -> None:
    _require_warcraftlogs_auth()
    code, fight_id = _public_raid_report()
    report_url = f"https://www.warcraftlogs.com/reports/{code}#fight={fight_id}"

    encounter_payload = _payload_for(["report-encounter", report_url])
    assert encounter_payload["provider"] == "warcraftlogs"
    assert encounter_payload["kind"] == "report_encounter"
    assert encounter_payload["reference"]["code"] == code
    assert encounter_payload["reference"]["fight_id"] == fight_id
    assert encounter_payload["fight"]["id"] == fight_id

    players_payload = _payload_for(["report-encounter-players", report_url])
    assert players_payload["provider"] == "warcraftlogs"
    assert players_payload["kind"] == "report_encounter_players"
    assert players_payload["reference"]["fight_id"] == fight_id
    assert players_payload["player_details"]["counts"]["total"] >= 1

    damage_payload = _payload_for(["report-encounter-damage-breakdown", report_url, "--view-by", "source"])
    assert damage_payload["provider"] == "warcraftlogs"
    assert damage_payload["kind"] == "report_encounter_damage_breakdown"
    assert damage_payload["reference"]["fight_id"] == fight_id
    assert damage_payload["query"]["data_type"] == "DamageDone"
    assert "table" in damage_payload


@pytest.mark.live
def test_live_warcraftlogs_report_encounter_buffs_preview_contract() -> None:
    """Guards the typed buffs-preview shape against WCL field-name drift.

    The reported_* fields are direct passthroughs of WCL's auras row schema (totalUptime,
    totalUses, bands.startTime/endTime). If WCL renames any of them, the typed contract
    fails this test fast instead of silently emitting null fields in production.
    """
    _require_warcraftlogs_auth()
    code, fight_id = _public_raid_report()
    report_url = f"https://www.warcraftlogs.com/reports/{code}#fight={fight_id}"

    payload = _payload_for([
        "report-encounter-buffs", report_url,
        "--view-by", "source",
        "--preview-limit", "5",
    ])

    assert payload["provider"] == "warcraftlogs"
    assert payload["kind"] == "report_encounter_buffs"
    assert payload["reference"]["code"] == code
    assert payload["reference"]["fight_id"] == fight_id
    assert payload["query"]["data_type"] == "Buffs"
    assert payload["query"]["preview_limit"] == 5

    buffs = payload["buffs"]
    assert isinstance(buffs["total"], int)
    assert isinstance(buffs["preview_truncated"], bool)
    assert isinstance(buffs["preview"], list)
    assert len(buffs["preview"]) <= 5
    assert buffs["preview_truncated"] == (buffs["total"] > 5)

    if buffs["total"] > 0:
        row = buffs["preview"][0]
        assert "source" in row
        assert row["source"]["identity_contract"]["source"]["provider"] == "warcraftlogs"
        assert row["aura"]["identity_contract"]["source"]["provider"] == "warcraftlogs"
        assert isinstance(row["aura"]["game_id"], int)
        assert isinstance(row["aura"]["name"], str)
        # Direct passthroughs of the live WCL auras-row field names — these are what could drift.
        assert "reported_total_uptime" in row
        assert "reported_total_uses" in row
        assert "reported_bands" in row
        if isinstance(row["reported_bands"], list) and row["reported_bands"]:
            band = row["reported_bands"][0]
            assert "startTime" in band
            assert "endTime" in band


@pytest.mark.live
def test_live_warcraftlogs_report_detail_contracts() -> None:
    _require_warcraftlogs_auth()
    code, fight_id = _public_raid_report()

    report_payload = _payload_for(["report", code])
    assert report_payload["provider"] == "warcraftlogs"
    assert report_payload["report"]["code"] == code

    master_data_payload = _payload_for(["report-master-data", code, "--actor-type", "Player"])
    assert master_data_payload["provider"] == "warcraftlogs"
    assert master_data_payload["report"]["code"] == code
    assert isinstance(master_data_payload["master_data"]["actors"], list)

    player_details_payload = _payload_for(["report-player-details", code, "--fight-id", str(fight_id)])
    assert player_details_payload["provider"] == "warcraftlogs"
    assert player_details_payload["report"]["code"] == code
    assert player_details_payload["player_details"]["counts"]["total"] >= 1

    events_payload = _payload_for(["report-events", code, "--fight-id", str(fight_id), "--limit", "5"])
    assert events_payload["provider"] == "warcraftlogs"
    assert events_payload["report"]["code"] == code
    assert "events" in events_payload
    assert events_payload["query"]["fight_ids"] == [fight_id]

    table_payload = _payload_for(["report-table", code, "--data-type", "damage-done", "--fight-id", str(fight_id)])
    assert table_payload["provider"] == "warcraftlogs"
    assert table_payload["report"]["code"] == code
    assert table_payload["query"]["data_type"] == "DamageDone"
    assert "table" in table_payload

    graph_payload = _payload_for(["report-graph", code, "--data-type", "damage-done", "--fight-id", str(fight_id)])
    assert graph_payload["provider"] == "warcraftlogs"
    assert graph_payload["report"]["code"] == code
    assert graph_payload["query"]["data_type"] == "DamageDone"
    assert "graph" in graph_payload

    rankings_payload = _payload_for(
        ["report-rankings", code, "--fight-id", str(fight_id), "--player-metric", "dps", "--timeframe", "historical", "--compare", "rankings"]
    )
    assert rankings_payload["provider"] == "warcraftlogs"
    assert rankings_payload["report"]["code"] == code
    assert rankings_payload["query"]["compare"] == "Rankings"
    assert rankings_payload["query"]["timeframe"] == "Historical"
    assert "rankings" in rankings_payload


@pytest.mark.live
def test_live_warcraftlogs_user_whoami_contract() -> None:
    _require_warcraftlogs_auth()
    _require_warcraftlogs_user_auth()

    payload = _payload_for(["auth", "whoami"])
    assert payload["provider"] == "warcraftlogs"
    assert payload["endpoint_family"] == "user"
    assert isinstance(payload["user"]["id"], int)
    assert isinstance(payload["user"]["name"], str) and payload["user"]["name"]

"""End-to-end journeys for the ``warcraftlogs`` binary against the live Warcraft Logs API.

Nothing volatile is pinned. Every zone, encounter, report code, fight, actor, and ability used
below is discovered at run time from ``tests/e2e/pins.py`` identities (the maintainer's guild and
character), because retail tiers roll over and reports age out of Warcraft Logs retention.

The discovery chain is:

``zones`` -> newest unfrozen zone that exposes the Normal/Heroic/Mythic triple (the current raid)
-> ``guild-reports`` for that zone -> the newest report that contains a boss kill
-> ``report-encounter-players`` for that kill -> actor ids, specs, and ability ids.

Most journeys hang off that one kill, so when the pinned guild has not killed anything in the new
tier yet (the window right after a tier rollover) discovery falls back to the newest public report
of the same zone that contains a kill. The sampled cross-report analytics are scoped to the anchor
report's own guild when it has one and to a report-time window around it, so the sampled cohort
provably contains the anchor kill instead of racing the public report firehose.

Auth journeys read only. ``auth login``, ``auth pkce-login``, and ``auth logout`` rewrite the saved
user token, so exercising them would log this machine out; they are deliberately not covered here
and are the documented exception in docs/architecture/E2E_TESTING.md.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from tests.e2e import pins
from tests.e2e.harness import (
    EXIT_NETWORK,
    EXIT_NOT_FOUND,
    EXIT_USAGE,
    JourneyFailure,
    Result,
    dead_proxy_env,
    no_cache_env,
    payload_or_legacy,
    run,
    run_raw,
)

GUILD = (pins.GUILD_REGION, pins.GUILD_REALM, pins.GUILD_NAME)

# A Warcraft Logs zone is a raid when it exposes the Normal/Heroic/Mythic triple; Mythic+, Delves,
# and PTR/beta zones expose their own difficulty ids instead.
RAID_DIFFICULTY_IDS = frozenset({3, 4, 5})

# How many of the guild's most recent current-tier reports discovery scans for an anchor kill.
DISCOVERY_REPORT_LIMIT = 10

# Padding around the anchor report's start/end so the sampled report window certainly contains it.
SAMPLE_WINDOW_PADDING_MS = 60_000
SAMPLE_REPORT_PAGES = "1"
SAMPLE_REPORTS_PER_PAGE = "5"


@dataclass(frozen=True)
class Anchor:
    """One real boss kill in the current raid tier, plus its roster."""

    zone: dict[str, Any]
    report: dict[str, Any]
    fight: dict[str, Any]
    players: tuple[dict[str, Any], ...]

    @property
    def code(self) -> str:
        return str(self.report["code"])

    @property
    def fight_id(self) -> int:
        return int(self.fight["id"])

    @property
    def url(self) -> str:
        return f"https://www.warcraftlogs.com/reports/{self.code}#fight={self.fight_id}"

    @property
    def guild_scope(self) -> list[str]:
        """``--guild-*`` filter for the sampled cohort, empty when the anchor report has no guild."""
        guild = self.report.get("guild")
        if not isinstance(guild, dict):
            return []
        server = guild.get("server") or {}
        region = (server.get("region") or {}).get("slug")
        if not (guild.get("name") and server.get("slug") and region):
            return []
        return [
            "--guild-region", str(region),
            "--guild-realm", str(server["slug"]),
            "--guild-name", str(guild["name"]).lower(),
        ]


def _rows(result: Result, key: str) -> list[Any]:
    value = payload_or_legacy(result, key)
    if not isinstance(value, list) or not value:
        raise JourneyFailure(f"expected a non-empty {key!r} list\n{result.describe()}")
    return value


@lru_cache(maxsize=1)
def current_raid_zone() -> dict[str, Any]:
    """The newest unfrozen raid zone Warcraft Logs knows about."""
    result = run("warcraftlogs", "zones")
    raids = [
        zone
        for zone in _rows(result, "zones")
        if isinstance(zone, dict)
        and not zone.get("frozen")
        and zone.get("encounters")
        and {difficulty.get("id") for difficulty in zone.get("difficulties") or []} >= RAID_DIFFICULTY_IDS
    ]
    if not raids:
        raise JourneyFailure(f"no unfrozen raid zone in the zone list\n{result.describe()}")
    return max(raids, key=lambda zone: ((zone.get("expansion") or {}).get("id") or 0, zone.get("id") or 0))


def _fight_roster(code: str, fight_id: int) -> tuple[dict[str, Any], ...]:
    result = run("warcraftlogs", "report-encounter-players", code, "--fight-id", str(fight_id))
    roles = (payload_or_legacy(result, "player_details") or {}).get("roles") or {}
    roster = [{**row, "role": role} for role, rows in roles.items() for row in rows if isinstance(row, dict)]
    if not roster:
        raise JourneyFailure(f"fight {fight_id} of {code} has no players\n{result.describe()}")
    return tuple(roster)


def _guild_identity(guild: Any) -> tuple[Any, ...] | None:
    """The fields two Warcraft Logs surfaces must agree on; the rest of the block is padded nulls."""
    if not isinstance(guild, dict):
        return None
    server = guild.get("server") or {}
    return (guild.get("id"), guild.get("name"), server.get("slug"), (server.get("region") or {}).get("slug"))


def _anchor_from_reports(zone: dict[str, Any], reports: list[Any]) -> Anchor | None:
    """The newest of ``reports`` that actually contains a boss kill, with its roster attached."""
    for report in reports:
        code = str(report.get("code") or "")
        if not code:
            continue
        fights = payload_or_legacy(run("warcraftlogs", "report-fights", code), "fights") or []
        kills = [fight for fight in fights if fight.get("kill") and fight.get("encounter_id")]
        if not kills:
            continue
        # Prefer the hardest difficulty in the report; ties go to the latest pull.
        fight = max(kills, key=lambda row: (row.get("difficulty") or 0, row.get("id") or 0))
        roster = _fight_roster(code, int(fight["id"]))
        return Anchor(zone=zone, report=report, fight=fight, players=roster)
    return None


@lru_cache(maxsize=1)
def anchor() -> Anchor:
    """A current-tier kill: the pinned guild's most recent one, or the newest public one.

    The pinned guild is preferred because its reports keep the sampled cohort small and provably
    contain this kill. At a tier rollover the guild can legitimately have no kill in the new zone
    yet, and the whole log half of the suite hangs off this fixture, so discovery then falls back
    to the public report listing for the same zone rather than taking the suite offline.
    """
    zone = current_raid_zone()
    guild_reports = run(
        "warcraftlogs", "guild-reports", *GUILD, "--zone-id", str(zone["id"]), "--limit", str(DISCOVERY_REPORT_LIMIT)
    )
    found = _anchor_from_reports(zone, _rows(guild_reports, "reports"))
    if found is not None:
        return found

    public = run("warcraftlogs", "reports", "--zone-id", str(zone["id"]), "--limit", str(DISCOVERY_REPORT_LIMIT))
    found = _anchor_from_reports(zone, _rows(public, "reports"))
    if found is not None:
        return found
    raise JourneyFailure(
        f"no kill in {zone['name']!r}: neither the {DISCOVERY_REPORT_LIMIT} most recent reports for "
        f"{pins.GUILD_NAME!r} nor the {DISCOVERY_REPORT_LIMIT} most recent public reports contain one"
    )


def cohort_args() -> list[str]:
    """Sampled-analytics scope that provably contains the anchor kill."""
    found = anchor()
    start = int(found.report["start_time"]) - SAMPLE_WINDOW_PADDING_MS
    end = int(found.report["end_time"]) + SAMPLE_WINDOW_PADDING_MS
    return [
        "--zone-id",
        str(found.zone["id"]),
        "--boss-id",
        str(found.fight["encounter_id"]),
        "--difficulty",
        str(found.fight["difficulty"]),
        *found.guild_scope,
        "--start-time",
        str(start),
        "--end-time",
        str(end),
        "--report-pages",
        SAMPLE_REPORT_PAGES,
        "--reports-per-page",
        SAMPLE_REPORTS_PER_PAGE,
    ]


def assert_sampling_metadata(result: Result, *, expect_rows: bool) -> dict[str, Any]:
    """Every sampled command must describe its own cohort per SAFE_ANALYTICS_RULES.md."""
    data = result.data
    scope = data.get("sample_scope") or {}
    sample = data.get("sample") or {}
    assert data.get("ranking_basis"), result.describe()
    assert data.get("matching_rule"), result.describe()
    assert (data.get("freshness") or {}).get("sampled_at"), result.describe()
    assert (data.get("cache_provenance") or {}).get("source"), result.describe()
    assert scope.get("filters", {}).get("zone_id") == anchor().zone["id"], result.describe()
    assert isinstance(scope.get("returned"), int), result.describe()
    assert sample.get("source_report_count", 0) >= 1, result.describe()
    if expect_rows:
        assert scope["returned"] > 0, result.describe()
        codes = [row.get("report_code") for row in (data.get("citations") or {}).get("sample_reports") or []]
        assert anchor().code in codes, result.describe()
    return data


@lru_cache(maxsize=1)
def anchor_aura_id() -> int:
    """An aura game id that is actually applied during the anchor kill."""
    result = run("warcraftlogs", "report-encounter-buffs", anchor().url, "--view-by", "source", "--preview-limit", "5")
    preview = (payload_or_legacy(result, "buffs") or {}).get("preview") or []
    for row in preview:
        game_id = ((row.get("aura") or {}).get("game_id"))
        if isinstance(game_id, int):
            return game_id
    raise JourneyFailure(f"no aura game id in the anchor fight's buff preview\n{result.describe()}")


@lru_cache(maxsize=1)
def anchor_ability_id() -> int:
    """The most-cast ability game id in the anchor kill."""
    result = run("warcraftlogs", "report-encounter-casts", anchor().url, "--limit", "200", "--preview-limit", "1")
    for row in (payload_or_legacy(result, "casts") or {}).get("by_ability") or []:
        game_id = ((row.get("ability") or {}).get("game_id"))
        if isinstance(game_id, int):
            return game_id
    raise JourneyFailure(f"no ability game id in the anchor fight's cast summary\n{result.describe()}")


@lru_cache(maxsize=1)
def anchor_wipe_fight_id() -> int:
    """A wipe on the anchor kill's encounter, in the same report.

    ``--wipe-cutoff`` only has anything to cut on a pull that wiped, so the flag cannot be proved
    against the anchor kill itself.
    """
    found = anchor()
    fights = payload_or_legacy(run("warcraftlogs", "report-fights", found.code), "fights") or []
    wipes = [
        fight
        for fight in fights
        if fight.get("encounter_id") == found.fight["encounter_id"] and not fight.get("kill")
    ]
    if not wipes:
        raise JourneyFailure(
            f"report {found.code} has no wipe on encounter {found.fight['encounter_id']}, "
            "so --wipe-cutoff cannot be exercised against it"
        )
    return int(max(wipes, key=lambda row: row["end_time"] - row["start_time"])["id"])


# The saved-token state block may only describe the token; these are every key it is allowed to
# carry, so a future field that smuggles a credential value out fails the auth journeys.
TOKEN_STATE_KEYS = frozenset(
    {
        "path",
        "exists",
        "readable",
        "valid_json",
        "auth_mode",
        "pending_auth_mode",
        "has_pending_state",
        "has_access_token",
        "has_refresh_token",
        "expires_at",
        "expired",
    }
)


def assert_no_credential_values(state: dict[str, Any]) -> None:
    assert set(state) <= TOKEN_STATE_KEYS, f"unexpected token-state keys: {sorted(set(state) - TOKEN_STATE_KEYS)}"


def anchor_spec_slug() -> str:
    """A participant spec of the anchor kill, in the slug form the sampled filters accept."""
    for player in anchor().players:
        specs = player.get("specs") or []
        if specs and isinstance(specs[0].get("spec"), str):
            return str(specs[0]["spec"]).lower()
    raise JourneyFailure(f"no spec on the anchor roster: {[p.get('name') for p in anchor().players]}")


# --------------------------------------------------------------------------------------------
# World metadata and discovery
# --------------------------------------------------------------------------------------------


def test_zones_expose_the_current_raid_tier_with_named_encounters(require):
    require("warcraftlogs")
    zone = current_raid_zone()
    assert isinstance(zone["name"], str) and zone["name"].strip(), zone
    encounters = zone["encounters"]
    assert len(encounters) >= 3, zone
    assert all(isinstance(row.get("name"), str) and row["name"].strip() for row in encounters), zone
    assert (zone.get("expansion") or {}).get("name"), zone


def test_zone_and_encounter_agree_on_the_discovered_tier(require):
    require("warcraftlogs")
    zone = current_raid_zone()
    boss = zone["encounters"][0]

    zone_result = run("warcraftlogs", "zone", str(zone["id"]))
    assert zone_result.payload["kind"] == "zone", zone_result.describe()
    detail = payload_or_legacy(zone_result, "zone")
    assert detail["id"] == zone["id"], zone_result.describe()
    assert detail["name"] == zone["name"], zone_result.describe()
    assert {row["id"] for row in detail["encounters"]} == {row["id"] for row in zone["encounters"]}, zone_result.describe()

    encounter_result = run("warcraftlogs", "encounter", str(boss["id"]))
    assert encounter_result.payload["kind"] == "encounter", encounter_result.describe()
    encounter = payload_or_legacy(encounter_result, "encounter")
    assert encounter["name"] == boss["name"], encounter_result.describe()
    assert encounter["zone"]["id"] == zone["id"], encounter_result.describe()
    identity = encounter_result.data["encounter_identity"]
    assert identity["identity"]["encounter_id"] == boss["id"], encounter_result.describe()


def test_regions_expansions_and_server_resolve_real_names(require):
    require("warcraftlogs")
    regions = run("warcraftlogs", "regions")
    slugs = {row["slug"] for row in _rows(regions, "regions")}
    assert {"us", "eu"} <= slugs, regions.describe()

    expansions = run("warcraftlogs", "expansions")
    rows = _rows(expansions, "expansions")
    assert current_raid_zone()["expansion"]["id"] in {row["id"] for row in rows}, expansions.describe()

    server = run("warcraftlogs", "server", pins.GUILD_REGION, pins.GUILD_REALM)
    detail = payload_or_legacy(server, "server")
    assert detail["slug"] == pins.GUILD_REALM, server.describe()
    assert detail["name"] == pins.GUILD_REALM_DISPLAY, server.describe()


def test_rate_limit_reports_the_hourly_point_budget(require):
    require("warcraftlogs")
    result = run("warcraftlogs", "rate-limit")
    assert result.payload["kind"] == "rate_limit", result.describe()
    limit = payload_or_legacy(result, "rate_limit")
    assert limit["limit_per_hour"] > 0, result.describe()
    assert 0 <= limit["points_spent_this_hour"] <= limit["limit_per_hour"], result.describe()


# --------------------------------------------------------------------------------------------
# Auth (read-only)
# --------------------------------------------------------------------------------------------


def test_doctor_reports_a_ready_provider_on_the_retail_profile(require):
    require("warcraftlogs")
    result = run("warcraftlogs", "doctor")
    doctor = payload_or_legacy(result, "status")
    assert doctor == "ready", result.describe()
    data = payload_or_legacy(result, "auth")
    assert data["required"] is True, result.describe()
    assert data["configured"] is True, result.describe()
    assert data["public_api_access"]["ready"] is True, result.describe()
    assert payload_or_legacy(result, "site_profile")["key"] == "retail", result.describe()
    assert_no_credential_values(data["state"])


def test_auth_status_reports_client_and_user_readiness_without_leaking_the_token(require):
    require("warcraftlogs")
    result = run("warcraftlogs", "auth", "status")
    auth = payload_or_legacy(result, "auth")
    assert auth["configured"] is True, result.describe()
    assert auth["site_profile"]["key"] == "retail", result.describe()
    assert auth["credential_source"], result.describe()
    assert auth["public_api_access"]["ready"] is True, result.describe()
    state = auth["state"]
    assert state["has_access_token"] is True, result.describe()
    assert isinstance(state["expires_at"], (int, float)), result.describe()
    assert state["expired"] is False, result.describe()
    # Shape only: the token state block reports presence and expiry, never a credential value.
    assert_no_credential_values(state)


def test_auth_client_and_token_describe_the_oauth_setup(require):
    require("warcraftlogs")
    client_result = run("warcraftlogs", "auth", "client")
    client = payload_or_legacy(client_result, "client")
    assert client["configured"] is True, client_result.describe()
    assert client["client_id"].endswith("..."), "auth client must only show a truncated client id"
    assert client["token_url"].endswith("/oauth/token"), client_result.describe()
    assert client["client_api_url"].endswith("/api/v2/client"), client_result.describe()

    token_result = run("warcraftlogs", "auth", "token")
    token = payload_or_legacy(token_result, "token")
    # A saved user token is what `auth whoami` reads, so the token surface must report the same one.
    assert token["endpoint_family"] == "user", token_result.describe()
    assert token["state"]["has_access_token"] is True, token_result.describe()
    assert isinstance(token["state"]["has_refresh_token"], bool), token_result.describe()
    granted = token["scopes"]["granted"]
    assert isinstance(granted, list) and granted, token_result.describe()
    assert_no_credential_values(token["state"])


def test_auth_whoami_names_the_account_behind_the_saved_user_token(require):
    require("warcraftlogs")
    result = run("warcraftlogs", "auth", "whoami")
    assert payload_or_legacy(result, "endpoint_family") == "user", result.describe()
    user = payload_or_legacy(result, "user")
    assert isinstance(user["id"], int) and user["id"] > 0, result.describe()
    assert isinstance(user["name"], str) and user["name"].strip(), result.describe()


def test_site_profiles_route_classic_and_fresh_with_the_same_client(require):
    require("warcraftlogs")
    for site, host in (("classic", "classic.warcraftlogs.com"), ("fresh", "fresh.warcraftlogs.com")):
        status = run("warcraftlogs", "--site", site, "auth", "status", "--no-live")
        profile = payload_or_legacy(status, "auth")["site_profile"]
        assert profile["key"] == site, status.describe()
        assert host in profile["api_url"], status.describe()

    classic = run("warcraftlogs", "--site", "classic", "expansions")
    classic_names = {row["name"] for row in _rows(classic, "expansions")}
    retail_names = {row["name"] for row in _rows(run("warcraftlogs", "expansions"), "expansions")}
    assert classic_names, classic.describe()
    assert classic_names != retail_names, "classic and retail must not return the same expansion list"


# --------------------------------------------------------------------------------------------
# Discovery surfaces
# --------------------------------------------------------------------------------------------


def test_search_and_resolve_accept_a_report_url_and_a_bare_code(require):
    require("warcraftlogs")
    found = anchor()

    for query in (found.url, found.code):
        search = run("warcraftlogs", "search", query)
        assert search.payload["kind"] == "search_results", search.describe()
        results = _rows(search, "results")
        assert results[0]["report_reference"]["code"] == found.code, search.describe()

    from_url = run("warcraftlogs", "resolve", found.url)
    assert from_url.payload["kind"] == "resolution", from_url.describe()
    assert from_url.data["resolved"] is True, from_url.describe()
    match = from_url.data["match"]
    assert match["report_reference"]["code"] == found.code, from_url.describe()
    assert match["report_reference"]["fight_id"] == found.fight_id, from_url.describe()
    assert str(found.fight_id) in from_url.data["next_command"], from_url.describe()

    from_code = run("warcraftlogs", "resolve", found.code)
    assert from_code.data["match"]["report_reference"]["code"] == found.code, from_code.describe()


def test_guild_family_reports_the_pinned_guild(require):
    require("warcraftlogs")
    zone = current_raid_zone()

    guild = run("warcraftlogs", "guild", *GUILD)
    detail = payload_or_legacy(guild, "guild")
    assert detail["name"].lower() == pins.GUILD_NAME, guild.describe()
    assert detail["server"]["slug"] == pins.GUILD_REALM, guild.describe()

    members = run("warcraftlogs", "guild-members", *GUILD, "--limit", "5")
    roster = payload_or_legacy(members, "guild_members")
    assert roster["count"] > 0, members.describe()
    assert all(row["name"] for row in roster["members"]), members.describe()

    rankings = run("warcraftlogs", "guild-rankings", *GUILD, "--zone-id", str(zone["id"]))
    ranks = payload_or_legacy(rankings, "guild_rankings")
    assert ranks["name"].lower() == pins.GUILD_NAME, rankings.describe()
    assert ranks["server"]["slug"] == pins.GUILD_REALM, rankings.describe()
    assert set(ranks["zone_ranking"]) >= {"progress", "speed"}, rankings.describe()

    attendance = run("warcraftlogs", "guild-attendance", *GUILD, "--limit", "3")
    rows = payload_or_legacy(attendance, "guild_attendance")
    assert rows["count"] > 0, attendance.describe()
    night = rows["attendance"][0]
    assert night["code"], attendance.describe()
    assert night["player_count"] > 0, attendance.describe()
    assert night["players"][0]["name"], attendance.describe()


def test_character_and_character_rankings_resolve_the_pinned_character(require):
    require("warcraftlogs")
    zone = current_raid_zone()

    character = run("warcraftlogs", "character", pins.GUILD_REGION, pins.GUILD_REALM, pins.CHARACTER_NAME)
    detail = payload_or_legacy(character, "character")
    assert detail["name"] == pins.CHARACTER_NAME, character.describe()
    assert detail["server"]["slug"] == pins.GUILD_REALM, character.describe()

    rankings = run(
        "warcraftlogs",
        "character-rankings",
        pins.GUILD_REGION,
        pins.GUILD_REALM,
        pins.CHARACTER_NAME,
        "--zone-id",
        str(zone["id"]),
        "--top",
        "3",
    )
    assert rankings.payload["kind"] == "character_rankings", rankings.describe()
    payload = payload_or_legacy(rankings, "character_rankings")
    assert payload["name"] == pins.CHARACTER_NAME, rankings.describe()
    assert payload["summary"]["zone"] == zone["id"], rankings.describe()
    # The trust block is what makes a leaderboard number safe to quote (SAFE_ANALYTICS_RULES.md).
    trust = payload["trust"]
    assert trust["ranking_basis"] == "public_character_zone_rankings", rankings.describe()
    assert set(trust["scope"]) == {"zone", "difficulty", "metric", "partition", "size"}, rankings.describe()
    assert trust["freshness"]["sampled_at"], rankings.describe()
    assert trust["source_character_identity"]["kind"] == "class_spec_identity", rankings.describe()
    assert trust["source_character_identity"]["source"] == {
        "provider": "warcraftlogs",
        "source": "character_rankings",
    }, rankings.describe()
    # The raw upstream payload always stays attached next to the summary.
    assert payload["raw"], rankings.describe()
    assert payload["error"] is None, rankings.describe()


def test_encounter_rankings_leaderboard_is_scoped_by_zone_and_boss_options(require):
    require("warcraftlogs")
    found = anchor()
    result = run(
        "warcraftlogs",
        "encounter-rankings",
        "--zone-id",
        str(found.zone["id"]),
        "--boss-id",
        str(found.fight["encounter_id"]),
        "--difficulty",
        str(found.fight["difficulty"]),
        "--top",
        "3",
    )
    assert result.payload["kind"] == "encounter_rankings", result.describe()
    assert "encounter_rankings" in result.data, result.describe()
    assert result.data["ranking_basis"], result.describe()
    encounter = result.data["encounter"]
    assert encounter["id"] == found.fight["encounter_id"], result.describe()
    assert encounter["name"] == found.fight["name"], result.describe()
    rankings = result.data["rankings"]
    assert rankings["count"] == len(rankings["rows"]), result.describe()
    assert 0 < len(rankings["rows"]) <= 3, "the current tier's anchor boss must have public encounter rankings"
    assert all(row.get("name") and row.get("class_name") for row in rankings["rows"]), result.describe()


def test_reports_and_guild_reports_list_the_current_tier(require):
    require("warcraftlogs")
    zone = current_raid_zone()
    found = anchor()

    public = run("warcraftlogs", "reports", "--zone-id", str(zone["id"]), "--limit", "3")
    rows = _rows(public, "reports")
    assert len(rows) <= 3, public.describe()
    assert all(row["zone"]["id"] == zone["id"] for row in rows), public.describe()

    guild_reports = run("warcraftlogs", "guild-reports", *GUILD, "--zone-id", str(zone["id"]), "--limit", "3")
    guild_rows = _rows(guild_reports, "reports")
    assert all(row["guild"]["name"].lower() == pins.GUILD_NAME for row in guild_rows), guild_reports.describe()
    assert guild_reports.data["pagination"]["total"] > 0, guild_reports.describe()
    assert found.report["zone"]["id"] == zone["id"], found.report


# --------------------------------------------------------------------------------------------
# Report surfaces
# --------------------------------------------------------------------------------------------


def test_report_and_report_fights_echo_the_discovered_report(require):
    require("warcraftlogs")
    found = anchor()

    report = run("warcraftlogs", "report", found.code)
    detail = payload_or_legacy(report, "report")
    assert detail["code"] == found.code, report.describe()
    assert detail["zone"]["id"] == found.zone["id"], report.describe()
    # `report` and the listing that discovered it must agree on who owns the report.
    assert _guild_identity(detail["guild"]) == _guild_identity(found.report["guild"]), report.describe()

    fights = run("warcraftlogs", "report-fights", found.code)
    assert fights.payload["kind"] == "report_fights", fights.describe()
    assert "report_fights" in fights.data, fights.describe()
    rows = _rows(fights, "fights")
    assert fights.data["count"] == len(rows), fights.describe()
    assert found.fight_id in {row["id"] for row in rows}, fights.describe()
    assert any(row.get("kill") for row in rows), fights.describe()

    filtered = run("warcraftlogs", "report-fights", found.code, "--difficulty", str(found.fight["difficulty"]))
    filtered_rows = _rows(filtered, "fights")
    assert {row["difficulty"] for row in filtered_rows} == {found.fight["difficulty"]}, filtered.describe()


def test_report_encounter_family_describes_the_discovered_kill(require):
    require("warcraftlogs")
    found = anchor()

    encounter = run("warcraftlogs", "report-encounter", found.url)
    assert encounter.payload["kind"] == "report_encounter", encounter.describe()
    assert encounter.data["reference"]["code"] == found.code, encounter.describe()
    assert encounter.data["fight"]["id"] == found.fight_id, encounter.describe()
    assert encounter.data["fight"]["kill"] is True, encounter.describe()
    assert encounter.data["encounter"]["name"] == found.fight["name"], encounter.describe()
    assert encounter.data["encounter_identity"]["status"] == "canonical", encounter.describe()
    assert encounter.data["stability"]["report_finished"] is True, encounter.describe()

    players = run("warcraftlogs", "report-encounter-players", found.code, "--fight-id", str(found.fight_id))
    details = payload_or_legacy(players, "player_details")
    assert details["counts"]["total"] == len(found.players), players.describe()
    assert details["counts"]["total"] > 0, players.describe()
    names = {row["name"] for row in found.players}
    assert all(isinstance(name, str) and name.strip() for name in names), found.players
    assert {"tanks", "healers", "dps"} >= set(details["roles"]), players.describe()


def test_report_encounter_casts_and_buffs_summarize_real_events(require):
    require("warcraftlogs")
    found = anchor()

    casts = run("warcraftlogs", "report-encounter-casts", found.url, "--limit", "200", "--preview-limit", "5")
    summary = payload_or_legacy(casts, "casts")
    assert summary["event_count"] > 0, casts.describe()
    assert len(summary["preview"]) <= 5, casts.describe()
    assert summary["by_ability"], casts.describe()
    assert summary["by_source"][0]["source"]["name"], casts.describe()
    assert anchor_ability_id() > 0

    buffs = run("warcraftlogs", "report-encounter-buffs", found.url, "--view-by", "source", "--preview-limit", "5")
    buff_summary = payload_or_legacy(buffs, "buffs")
    assert buff_summary["total"] > 0, buffs.describe()
    assert buff_summary["view_by"].lower() == "source", buffs.describe()
    assert len(buff_summary["preview"]) == min(5, buff_summary["total"]), buffs.describe()
    assert buff_summary["preview_truncated"] == (buff_summary["total"] > 5), buffs.describe()
    # The reported_* fields are straight passthroughs of the upstream auras row; a rename upstream
    # would otherwise show up as silently null typed fields.
    row = buff_summary["preview"][0]
    assert row["aura"]["name"] and isinstance(row["aura"]["game_id"], int), buffs.describe()
    assert row["aura"]["identity_contract"]["source"]["provider"] == "warcraftlogs", buffs.describe()
    assert row["source"]["identity_contract"]["source"]["provider"] == "warcraftlogs", buffs.describe()
    assert {"reported_total_uptime", "reported_total_uses", "reported_bands"} <= set(row), buffs.describe()


def test_report_encounter_aura_summary_and_compare_use_explicit_windows(require):
    require("warcraftlogs")
    found = anchor()
    ability_id = anchor_aura_id()
    duration = int(found.fight["end_time"]) - int(found.fight["start_time"])
    half = max(duration // 2, 1000)

    summary = run(
        "warcraftlogs",
        "report-encounter-aura-summary",
        found.url,
        "--ability-id",
        str(ability_id),
        "--window-start-ms",
        "0",
        "--window-end-ms",
        str(half),
    )
    assert summary.payload["kind"] == "report_encounter_aura_summary", summary.describe()
    assert summary.data["aura"]["game_id"] == ability_id, summary.describe()
    assert summary.data["aura"]["name"], summary.describe()
    rows = summary.data["aura_summary"]["rows"]
    assert summary.data["aura_summary"]["entry_count"] == len(rows), summary.describe()
    assert rows, "the discovered aura must have at least one holder in its own fight"
    assert rows[0]["source"]["name"], summary.describe()

    compare = run(
        "warcraftlogs",
        "report-encounter-aura-compare",
        found.url,
        "--ability-id",
        str(ability_id),
        "--left-window-start-ms",
        "0",
        "--left-window-end-ms",
        str(half),
        "--right-window-start-ms",
        str(half),
        "--right-window-end-ms",
        str(duration),
    )
    assert compare.payload["kind"] == "report_encounter_aura_compare", compare.describe()
    assert compare.data["comparison"]["matching_rule"], compare.describe()
    windows = {window["label"]: window["query"] for window in compare.data["windows"]}
    assert windows["left"]["window_end_ms"] == windows["right"]["window_start_ms"], compare.describe()
    assert windows["left"]["ability_id"] == ability_id, compare.describe()


def test_report_encounter_damage_surfaces_return_typed_and_raw_views(require):
    require("warcraftlogs")
    found = anchor()

    by_source = run("warcraftlogs", "report-encounter-damage-source-summary", found.url)
    source_rows = by_source.data["damage_summary"]["rows"]
    assert source_rows, by_source.describe()
    assert source_rows[0]["source"]["name"], by_source.describe()

    by_target = run("warcraftlogs", "report-encounter-damage-target-summary", found.url)
    assert by_target.data["damage_summary"]["rows"], by_target.describe()

    breakdown = run("warcraftlogs", "report-encounter-damage-breakdown", found.url, "--view-by", "source")
    assert breakdown.payload["kind"] == "report_encounter_damage_breakdown", breakdown.describe()
    assert breakdown.data["table"], breakdown.describe()


def test_report_player_talents_emits_a_raw_only_packet_and_writes_it(require, out_dir):
    require("warcraftlogs")
    found = anchor()
    actor = found.players[0]
    out_path = out_dir / "actor-packet.json"

    result = run(
        "warcraftlogs",
        "report-player-talents",
        found.url,
        "--actor-id",
        str(actor["id"]),
        "--out",
        str(out_path),
    )
    assert result.payload["kind"] == "report_player_talents", result.describe()
    assert result.data["player"]["name"] == actor["name"], result.describe()

    packet = result.data["talent_transport_packet"]
    assert packet["kind"] == "talent_transport_packet", result.describe()
    # Warcraft Logs performs structural validation only; it never runs SimulationCraft.
    assert packet["transport_status"] == "raw_only", result.describe()
    assert packet["validation"] == {"status": "not_validated", "reason": "simc_backend_unavailable"}, result.describe()
    assert packet["scope"] == {
        "type": "report_fight_actor",
        "report_code": found.code,
        "fight_id": found.fight_id,
        "actor_id": actor["id"],
    }, result.describe()
    assert packet["raw_evidence"]["talent_tree_entries"], result.describe()

    assert result.data["written_packet_path"] == str(out_path), result.describe()
    assert json.loads(out_path.read_text(encoding="utf-8")) == packet


def test_raw_report_surfaces_return_scoped_slices(require):
    require("warcraftlogs")
    found = anchor()
    fight = str(found.fight_id)

    events = run("warcraftlogs", "report-events", found.code, "--fight-id", fight, "--data-type", "casts", "--limit", "5")
    assert events.payload["kind"] == "report_events", events.describe()
    assert events.data["report"]["code"] == found.code, events.describe()
    assert events.data["events"], events.describe()
    assert len(events.data["events"]) <= 5, events.describe()
    assert {row["type"] for row in events.data["events"]} <= {"cast", "begincast"}, events.describe()
    assert {row["fight"] for row in events.data["events"]} == {found.fight_id}, events.describe()

    roster = {row["name"] for row in found.players}
    table = run("warcraftlogs", "report-table", found.code, "--data-type", "damage-done", "--fight-id", fight)
    entries = table.data["table"]["data"]["entries"]
    assert entries, table.describe()
    assert {row["name"] for row in entries} <= roster, table.describe()

    graph = run("warcraftlogs", "report-graph", found.code, "--data-type", "damage-done", "--fight-id", fight)
    series = graph.data["graph"]["data"]["series"]
    assert series, graph.describe()
    # The graph adds a synthetic "Total" series on top of the roster.
    assert {row["name"] for row in series} == roster | {"Total"}, graph.describe()

    master = run("warcraftlogs", "report-master-data", found.code, "--actor-type", "Player")
    actors = master.data["master_data"]["actors"]
    assert actors, master.describe()
    assert {row["name"] for row in actors} >= {found.players[0]["name"]}, master.describe()

    details = run("warcraftlogs", "report-player-details", found.code, "--fight-id", fight)
    assert details.data["player_details"], details.describe()

    rankings = run(
        "warcraftlogs",
        "report-rankings",
        found.code,
        "--fight-id",
        fight,
        "--player-metric",
        "dps",
        "--timeframe",
        "historical",
        "--compare",
        "rankings",
    )
    assert rankings.payload["kind"] == "report_rankings", rankings.describe()
    assert rankings.data["rankings"], rankings.describe()


def test_filter_expression_narrows_the_events_a_report_slice_returns(require):
    """``--filter-expression`` is the only way to narrow events server-side; prove it lands.

    The expression is built from an ability the anchor kill actually contains, so the expected
    result is exact: the filtered page holds that ability and nothing else.
    """
    require("warcraftlogs")
    found = anchor()
    ability_id = anchor_ability_id()
    slice_args = (found.code, "--fight-id", str(found.fight_id), "--data-type", "casts", "--limit", "25")

    unfiltered = run("warcraftlogs", "report-events", *slice_args)
    all_abilities = {row.get("abilityGameID") for row in unfiltered.data["events"]}
    assert len(all_abilities) > 1, unfiltered.describe()

    filtered = run("warcraftlogs", "report-events", *slice_args, "--filter-expression", f"ability.id = {ability_id}")
    events = filtered.data["events"]
    assert events, filtered.describe()
    assert {row["abilityGameID"] for row in events} == {ability_id}, filtered.describe()
    assert {row["fight"] for row in events} == {found.fight_id}, filtered.describe()
    assert filtered.payload["query"]["filter_expression"] == f"ability.id = {ability_id}", filtered.describe()


def test_wipe_cutoff_trims_the_tail_of_a_wipe_pull(require):
    """``--wipe-cutoff`` must honour its *value*, not merely be accepted.

    Warcraft Logs cuts the table at the Nth wipe, so on a pull that wiped the reported damage has
    to grow monotonically as the cutoff moves later and never exceed the uncut total.
    """
    require("warcraftlogs")
    wipe_fight = str(anchor_wipe_fight_id())

    def damage(*extra: str) -> tuple[float, set[Any], Result]:
        result = run("warcraftlogs", "report-encounter-damage-source-summary", anchor().code, "--fight-id", wipe_fight, *extra)
        rows = result.data["damage_summary"]["rows"]
        assert rows, result.describe()
        return (
            sum(float(row["reported_total"] or 0) for row in rows),
            {(row["source"] or {}).get("id") for row in rows},
            result,
        )

    uncut_total, uncut_sources, uncut = damage()
    first_total, first_sources, first = damage("--wipe-cutoff", "1")
    later_total, _later_sources, later = damage("--wipe-cutoff", "3")

    assert uncut_total > 0, uncut.describe()
    assert first_total < later_total, f"--wipe-cutoff 1 and 3 reported the same damage\n{later.describe()}"
    assert later_total <= uncut_total, later.describe()
    assert first_sources <= uncut_sources, first.describe()
    assert first.payload["query"]["wipe_cutoff"] == 1, first.describe()


def test_include_raw_attaches_the_untyped_table_entry_on_request(require):
    """The typed rows are the contract; ``--include-raw`` adds the upstream entry without changing them."""
    require("warcraftlogs")
    found = anchor()
    args = ("report-encounter-damage-target-summary", found.code, "--fight-id", str(found.fight_id))

    typed = run("warcraftlogs", *args)
    rows = typed.data["damage_summary"]["rows"]
    assert rows, typed.describe()
    assert all("raw_entry" not in row for row in rows), typed.describe()

    with_raw = run("warcraftlogs", *args, "--include-raw")
    raw_rows = with_raw.data["damage_summary"]["rows"]
    assert all(row["raw_entry"] for row in raw_rows), with_raw.describe()
    assert [{key: value for key, value in row.items() if key != "raw_entry"} for row in raw_rows] == rows, with_raw.describe()


def test_aura_compare_labels_name_the_two_windows(require):
    """``--left-label``/``--right-label`` rename the comparison windows the payload reports."""
    require("warcraftlogs")
    found = anchor()
    duration = int(found.fight["end_time"]) - int(found.fight["start_time"])
    half = max(duration // 2, 1000)

    result = run(
        "warcraftlogs",
        "report-encounter-aura-compare",
        found.url,
        "--ability-id",
        str(anchor_aura_id()),
        "--left-window-start-ms",
        "0",
        "--left-window-end-ms",
        str(half),
        "--right-window-start-ms",
        str(half),
        "--right-window-end-ms",
        str(duration),
        "--left-label",
        "opener",
        "--right-label",
        "execute",
    )
    windows = {window["label"]: window for window in result.data["windows"]}
    assert set(windows) == {"opener", "execute"}, result.describe()
    assert windows["opener"]["query"]["window_end_ms"] == windows["execute"]["query"]["window_start_ms"], result.describe()


def test_report_encounter_casts_says_when_its_aggregates_are_truncated(require):
    """A cast page that hit ``--limit`` must say so; the aggregates below it are partial."""
    require("warcraftlogs")
    found = anchor()
    args = ("report-encounter-casts", found.url, "--preview-limit", "1")

    capped = run("warcraftlogs", *args, "--limit", "25")
    summary = capped.data["casts"]
    assert summary["truncated"] is True, capped.describe()
    assert summary["next_page_timestamp"] is not None, capped.describe()
    # The note has to name the count the aggregates below it were actually built from.
    assert any(f"first {summary['event_count']} cast events" in note for note in capped.data["notes"]), capped.describe()

    # The opening seconds of the pull fit inside one page, so the same command must stop warning.
    complete = run("warcraftlogs", *args, "--limit", "10000", "--window-start-ms", "0", "--window-end-ms", "5000")
    assert complete.data["casts"]["truncated"] is False, complete.describe()
    assert complete.data["casts"]["next_page_timestamp"] is None, complete.describe()
    assert complete.data["notes"] == [], complete.describe()
    assert complete.data["casts"]["event_count"] > summary["event_count"], complete.describe()


# --------------------------------------------------------------------------------------------
# Sampled cross-report analytics
# --------------------------------------------------------------------------------------------


def test_boss_kills_and_top_kills_return_the_anchor_kill(require):
    require("warcraftlogs")
    found = anchor()

    boss_kills = run("warcraftlogs", "boss-kills", *cohort_args(), "--top", "3")
    assert boss_kills.payload["kind"] == "boss_kills", boss_kills.describe()
    assert "boss_kills" in boss_kills.data, boss_kills.describe()
    data = assert_sampling_metadata(boss_kills, expect_rows=True)
    kills = data["kills"]
    assert any(row["report"]["code"] == found.code and row["fight"]["id"] == found.fight_id for row in kills), boss_kills.describe()
    assert all(row["fight"]["kill"] is True for row in kills), boss_kills.describe()
    assert all(row["duration_seconds"] > 0 for row in kills), boss_kills.describe()

    top_kills = run("warcraftlogs", "top-kills", *cohort_args(), "--top", "3")
    assert top_kills.payload["kind"] == "top_kills", top_kills.describe()
    top = assert_sampling_metadata(top_kills, expect_rows=True)["kills"]
    durations = [row["duration_ms"] for row in top]
    assert durations == sorted(durations), "top-kills must return the fastest kills first"


def test_spec_kill_samples_and_boss_spec_usage_describe_the_cohort(require):
    require("warcraftlogs")
    spec = anchor_spec_slug()

    samples = run("warcraftlogs", "spec-kill-samples", *cohort_args(), "--spec-name", spec, "--top", "3")
    assert samples.payload["kind"] == "spec_filtered_kill_samples", samples.describe()
    assert "spec_kill_samples" in samples.data, samples.describe()
    data = assert_sampling_metadata(samples, expect_rows=True)
    assert data["sample"]["spec_name"] == spec, samples.describe()
    assert data["cohort"] == "spec_filtered_participant_kill_cohort", samples.describe()
    assert data["sample"]["returned_kill_count"] == len(data["spec_kill_samples"]), samples.describe()
    assert data["sample"]["sample_size"] >= data["sample"]["returned_kill_count"], samples.describe()
    # The rows are a sample of a cohort, not a leaderboard, and must keep saying so.
    assert any("not a spec ranking leaderboard" in note for note in data["notes"]), samples.describe()

    # Every spec in the cohort, so the anchor spec cannot fall outside a truncated top list.
    usage = run("warcraftlogs", "boss-spec-usage", *cohort_args(), "--top", "40")
    assert usage.payload["kind"] == "boss_spec_usage", usage.describe()
    rows = assert_sampling_metadata(usage, expect_rows=True)["boss_spec_usage"]
    assert rows, usage.describe()
    assert all(row["spec_name"] and row["appearance_count"] > 0 for row in rows), usage.describe()
    assert spec in {str(row["spec_name"]).lower() for row in rows}, usage.describe()


def test_comp_samples_and_kill_time_distribution_bucket_the_cohort(require):
    require("warcraftlogs")

    comps = run("warcraftlogs", "comp-samples", *cohort_args(), "--top", "3")
    assert comps.payload["kind"] == "comp_samples", comps.describe()
    data = assert_sampling_metadata(comps, expect_rows=True)
    assert data["composition_signatures"], comps.describe()
    assert data["class_presence"], comps.describe()
    assert all(row["class_name"] for row in data["class_presence"]), comps.describe()

    distribution = run("warcraftlogs", "kill-time-distribution", *cohort_args(), "--bucket-seconds", "60")
    assert distribution.payload["kind"] == "kill_time_distribution", distribution.describe()
    histogram = assert_sampling_metadata(distribution, expect_rows=True)["distribution"]
    assert histogram["bucket_seconds"] == 60, distribution.describe()
    assert histogram["statistics"]["median"] > 0, distribution.describe()
    assert sum(bucket["count"] for bucket in histogram["rows"]) > 0, distribution.describe()


def test_ability_usage_summary_counts_a_discovered_ability(require):
    require("warcraftlogs")
    ability_id = anchor_ability_id()
    result = run(
        "warcraftlogs",
        "ability-usage-summary",
        *cohort_args(),
        "--ability-id",
        str(ability_id),
        "--preview-limit",
        "3",
        "--event-limit",
        "200",
    )
    assert result.payload["kind"] == "ability_usage_summary", result.describe()
    data = assert_sampling_metadata(result, expect_rows=True)
    assert data["ability"]["game_id"] == ability_id, result.describe()
    usage = data["usage"]
    assert usage["total_casts"] > 0, result.describe()
    assert usage["kills_with_any_usage_count"] > 0, result.describe()
    assert data["kills_preview"][0]["report"]["code"], result.describe()
    assert usage["total_casts_is_lower_bound"] is False, result.describe()
    assert data["sample"]["kills_with_truncated_events_count"] == 0, result.describe()


def test_ability_usage_summary_reports_its_own_truncation(require):
    """A cast total that hit ``--event-limit`` is a lower bound and must be labelled as one.

    SAFE_ANALYTICS_RULES.md: sampling and truncation are never silent. One event per kill is far
    below any real kill's cast count, so the cap provably bites.
    """
    require("warcraftlogs")
    result = run(
        "warcraftlogs",
        "ability-usage-summary",
        *cohort_args(),
        "--ability-id",
        str(anchor_ability_id()),
        "--preview-limit",
        "1",
        "--event-limit",
        "1",
    )
    data = assert_sampling_metadata(result, expect_rows=True)
    assert data["sample"]["kills_with_truncated_events_count"] > 0, result.describe()
    assert data["usage"]["total_casts_is_lower_bound"] is True, result.describe()
    assert any("--event-limit=1" in note for note in data["notes"]), result.describe()


def _kill_durations(result: Result) -> list[float]:
    return [row["duration_seconds"] for row in result.data["kills"]]


def test_kill_time_filters_return_a_strict_subset_of_the_cohort(require):
    """``--kill-time-min``/``--kill-time-max`` must shape the returned kills, not just echo.

    Both bounds are derived from the unfiltered cohort, so the expected outcome is exact: the
    upper-bound call keeps every kill, and a floor above the slowest kill keeps none.
    """
    require("warcraftlogs")
    unfiltered = run("warcraftlogs", "boss-kills", *cohort_args(), "--top", "10")
    durations = _kill_durations(unfiltered)
    assert durations, unfiltered.describe()
    # `duration_seconds` is rounded for display, so the bounds are widened by a second either way.
    ceiling = max(durations) + 1
    codes = {(row["report"]["code"], row["fight"]["id"]) for row in unfiltered.data["kills"]}

    kept = run("warcraftlogs", "boss-kills", *cohort_args(), "--top", "10", "--kill-time-max", str(ceiling))
    assert kept.data["sample_scope"]["filters"]["kill_time_max"] == ceiling, kept.describe()
    assert {(row["report"]["code"], row["fight"]["id"]) for row in kept.data["kills"]} == codes, kept.describe()
    assert all(duration <= ceiling for duration in _kill_durations(kept)), kept.describe()

    pruned = run("warcraftlogs", "boss-kills", *cohort_args(), "--top", "10", "--kill-time-min", str(ceiling + 1))
    assert pruned.data["kills"] == [], pruned.describe()
    assert pruned.data["sample"]["filtered_kill_count"] == 0, pruned.describe()
    # The cohort was still scanned; only the filter emptied it, and the scan still says so.
    assert pruned.data["sample"]["scanned_fight_count"] > 0, pruned.describe()
    assert pruned.data["sample"]["source_report_count"] == unfiltered.data["sample"]["source_report_count"], pruned.describe()
    assert_sampling_metadata(pruned, expect_rows=False)


def test_boss_kills_spec_filter_narrows_the_cohort_to_that_spec(require):
    """``--spec-name`` on boss-kills keeps only the kills whose roster contains that spec."""
    require("warcraftlogs")
    spec = anchor_spec_slug()
    unfiltered = run("warcraftlogs", "boss-kills", *cohort_args(), "--top", "10")
    all_kills = {(row["report"]["code"], row["fight"]["id"]) for row in unfiltered.data["kills"]}

    filtered = run("warcraftlogs", "boss-kills", *cohort_args(), "--top", "10", "--spec-name", spec)
    data = assert_sampling_metadata(filtered, expect_rows=True)
    assert data["sample_scope"]["filters"]["spec_name"] == spec, filtered.describe()
    kept = {(row["report"]["code"], row["fight"]["id"]) for row in data["kills"]}
    assert kept <= all_kills, filtered.describe()
    # The anchor kill's own roster contains the spec it was discovered from.
    assert (anchor().code, anchor().fight_id) in kept, filtered.describe()
    assert any("spec" in note.lower() for note in data["notes"]), filtered.describe()


# --------------------------------------------------------------------------------------------
# Raw GraphQL and global flags
# --------------------------------------------------------------------------------------------


def test_graphql_introspect_and_a_typed_query_reach_the_api(require):
    require("warcraftlogs")
    found = anchor()

    introspect = run("warcraftlogs", "graphql", "--introspect")
    assert introspect.payload["kind"] == "graphql", introspect.describe()
    schema = introspect.data["introspection"]
    assert schema["queryType"]["name"], introspect.describe()
    assert len(schema["types"]) > 50, introspect.describe()

    query = run(
        "warcraftlogs",
        "graphql",
        "--query",
        "query R($code: String!) { reportData { report(code: $code) { code title } } }",
        "--report-code",
        found.code,
    )
    assert query.data["reportData"]["report"]["code"] == found.code, query.describe()
    assert query.payload["query"]["variables"]["code"] == found.code, query.describe()


def test_fields_projection_and_compact_bound_the_payload(require):
    require("warcraftlogs")
    # --fields intentionally strips the envelope, so this journey reads the raw process output.
    projected = run_raw("warcraftlogs", "--fields", "data.regions", "--fields-strict", "regions")
    assert projected.exit_code == 0, projected.describe()
    payload = json.loads(projected.stdout)
    assert set(payload) == {"data"}, projected.describe()
    assert {row["slug"] for row in payload["data"]["regions"]} >= {"us", "eu"}, projected.describe()

    missing = run(
        "warcraftlogs",
        "--fields",
        "data.not_a_real_path",
        "--fields-strict",
        "regions",
        expect=EXIT_USAGE,
        error_code="missing_fields",
    )
    assert missing.payload["error"]["details"]["missing_fields"] == ["data.not_a_real_path"], missing.describe()

    # Without --fields-strict the projection succeeds but must name what it could not find, so a
    # caller never mistakes an empty projection for an empty answer.
    lenient = run_raw("warcraftlogs", "--fields", "data.regions,data.not_a_real_path", "regions")
    assert lenient.exit_code == 0, lenient.describe()
    lenient_payload = json.loads(lenient.stdout)
    assert lenient_payload["fields_missing"] == ["data.not_a_real_path"], lenient.describe()
    assert {row["slug"] for row in lenient_payload["data"]["regions"]} >= {"us", "eu"}, lenient.describe()

    compact = run("warcraftlogs", "--compact", "--compact-max-chars", "40", "report", anchor().code)
    title = payload_or_legacy(compact, "report")["title"]
    assert len(title) <= 43, compact.describe()


# --------------------------------------------------------------------------------------------
# Error journeys
# --------------------------------------------------------------------------------------------


def test_a_missing_fight_in_a_real_report_is_a_not_found_envelope(require):
    require("warcraftlogs")
    result = run(
        "warcraftlogs",
        "report-encounter",
        anchor().code,
        "--fight-id",
        "999999",
        expect=EXIT_NOT_FOUND,
        error_code="not_found",
    )
    assert anchor().code in result.payload["error"]["message"], result.describe()
    assert result.stdout == ""


def test_a_missing_zone_is_a_not_found_envelope(require):
    require("warcraftlogs")
    result = run("warcraftlogs", "zone", "999999", expect=EXIT_NOT_FOUND, error_code="not_found")
    assert "999999" in result.payload["error"]["message"], result.describe()


def test_an_unknown_report_code_is_not_found(require):
    require("warcraftlogs")
    # Warcraft Logs answers an unknown report code with report = null plus a GraphQL error whose
    # message says the report does not exist; the client classifies that as not_found (exit 4),
    # matching every sibling lookup (zone/encounter/guild/character/server).
    result = run("warcraftlogs", "report", "ZZZZZZZZZZZZZZZZ", expect=EXIT_NOT_FOUND, error_code="not_found")
    assert "report" in result.payload["error"]["message"].lower(), result.describe()
    assert result.stdout == ""


def test_a_missing_boss_argument_is_reported_before_any_sampling(require):
    require("warcraftlogs")
    result = run(
        "warcraftlogs",
        "boss-kills",
        "--zone-id",
        str(current_raid_zone()["id"]),
        expect=EXIT_USAGE,
        error_code="missing_boss",
    )
    assert "--boss-id" in result.payload["error"]["message"], result.describe()


def test_a_report_query_without_a_fight_scope_is_a_usage_error(require):
    """Warcraft Logs answers an unscoped report query with an empty payload, which reads as "no data".

    Both commands must reject it at the CLI instead, naming the two slices the API really accepts.
    """
    require("warcraftlogs")
    found = anchor()
    for command, extra in (
        ("report-player-details", ()),
        ("report-player-details", ("--encounter-id", str(found.fight["encounter_id"]))),
        ("report-events", ("--data-type", "casts")),
    ):
        result = run("warcraftlogs", command, found.code, *extra, expect=EXIT_USAGE, error_code="missing_scope")
        assert "--fight-id" in result.payload["error"]["message"], result.describe()
        assert "--start-time" in result.payload["error"]["message"], result.describe()
        assert result.stdout == ""


def test_a_fight_scope_that_matches_nothing_is_not_found_not_an_empty_roster(require):
    """An unknown fight id must not come back as a report with zero players."""
    require("warcraftlogs")
    found = anchor()
    result = run(
        "warcraftlogs",
        "report-player-details",
        found.code,
        "--fight-id",
        "999999",
        expect=EXIT_NOT_FOUND,
        error_code="not_found",
    )
    assert found.code in result.payload["error"]["message"], result.describe()
    assert "999999" in result.payload["error"]["message"], result.describe()


def test_a_dead_proxy_is_an_exit_5_envelope_on_stderr(require):
    require("warcraftlogs")
    # A target no other journey touches, so the session cache cannot mask the transport failure.
    result = run(
        "warcraftlogs",
        "server",
        "us",
        "proudmoore",
        expect=EXIT_NETWORK,
        env={**dead_proxy_env(), **no_cache_env()},
    )
    assert result.error_code in {"network_error", "timeout"}, result.describe()
    assert result.stdout == ""

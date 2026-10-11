"""End-to-end journeys for the ``warcraftlogs`` binary against the live Warcraft Logs API.

Nothing volatile is pinned. Every zone, encounter, report code, fight, actor, and ability used
below is discovered at run time from ``tests/e2e/pins.py`` identities (the maintainer's guild and
character), because retail tiers roll over and reports age out of Warcraft Logs retention.

The discovery chain is:

``zones`` -> newest unfrozen zone that exposes the Normal/Heroic/Mythic triple (the current raid)
-> ``encounter-rankings`` for its bosses -> the first ranked kill in a *public* guild report
-> ``report-encounter-players`` for that kill -> actor ids, specs, and ability ids.

Three discovery fixtures, because one cannot prove everything:

- ``anchor`` is a public, guild-owned kill off the tier leaderboard: a guaranteed kill in a report
  any token can read, which is what an agent is usually pointed at. Almost every report journey
  hangs off it. The sampled cross-report analytics are scoped to its guild and to a report-time
  window around it, so the sampled cohort provably contains it instead of racing the firehose.
- ``guild_anchor`` is the pinned guild's own newest kill in a *private* report (the guild logs
  privately, bar the odd unlisted report), so it is what proves the saved user token opens a report the client token cannot; it is also the
  roster the spec filter's negative case is derived from, and a report Lorrgs has never cached.
- ``wide_cohort`` is the guild's newest reports of one tier (the current one, or an earlier one while
  the current tier is too new) on a boss it killed more than once, because ordering and duplicate
  collapsing are claims that a single-kill cohort can never contradict.

Auth journeys read only. ``auth login``, ``auth pkce-login``, and ``auth logout`` rewrite the saved
user token, so exercising them would log this machine out; they are deliberately not covered here
and are the documented exception in docs/architecture/E2E_TESTING.md.
"""

from __future__ import annotations

import json
import shlex
import time
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from functools import cache, lru_cache
from typing import Any

from tests.e2e import pins
from tests.e2e.harness import (
    EXIT_AUTH,
    EXIT_NETWORK,
    EXIT_NOT_FOUND,
    EXIT_USAGE,
    JourneyFailure,
    Result,
    dead_proxy_env,
    no_cache_env,
    run,
    run_raw,
)

GUILD = (pins.GUILD_REGION, pins.GUILD_REALM, pins.GUILD_NAME)

# A Warcraft Logs zone is a raid when it exposes the Normal/Heroic/Mythic triple; Mythic+, Delves,
# and PTR/beta zones expose their own difficulty ids instead.
RAID_DIFFICULTY_IDS = frozenset({3, 4, 5})

# How many of the guild's most recent current-tier reports discovery scans for an anchor kill.
DISCOVERY_REPORT_LIMIT = 10

# Most-applied auras tried, in order, for one held in both halves of the anchor kill.
AURA_CANDIDATE_ATTEMPTS = 5

# Padding around the anchor report's start/end so the sampled report window certainly contains it.
SAMPLE_WINDOW_PADDING_MS = 60_000
SAMPLE_REPORT_PAGES = "1"
SAMPLE_REPORTS_PER_PAGE = "5"

# A report counts as finished once its last event is this old (ms); a raid night still being logged
# is not, and the newest ranked kill can come from one.
FINISHED_REPORT_QUIET_MS = 2 * 60 * 60 * 1000

# Mythic: the difficulty the public leaderboards rank; Heroic is killed far more often.
MYTHIC_DIFFICULTY_ID = 5
HEROIC_DIFFICULTY_ID = 4
# How far the public-report walk goes before reporting what it scanned.
PUBLIC_ANCHOR_BOSS_ATTEMPTS = 3
PUBLIC_ANCHOR_ROW_ATTEMPTS = 5
WIDE_COHORT_TOP = "20"
# How many raid zones, newest first, the wide cohort walks. The one before the current tier can be a
# one-boss raid, so two would not always reach a finished tier.
WIDE_COHORT_ZONES = 3
# The widest wall-clock drift two logs of one pull may show before they are different pulls. Stated
# here rather than imported so the journey asserts the contract instead of the implementation.
DUPLICATE_PULL_TOLERANCE_MS = 5_000
# Two logs listing the same players may drift this far and still be one pull.
ROSTER_MATCH_TOLERANCE_MS = 30_000


@dataclass(frozen=True)
class Anchor:
    """One real boss kill in the current raid tier, plus its roster."""

    zone: dict[str, Any]
    # The report code, owning guild and region exactly as the *listing* that discovered the kill gave
    # them (a leaderboard row or a guild's report list), so `report` can be held to them independently.
    code: str
    listed_guild: str
    listed_region: str
    report: dict[str, Any]
    fight: dict[str, Any]
    players: tuple[dict[str, Any], ...]

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
    value = result.data.get(key)
    if not isinstance(value, list) or not value:
        raise JourneyFailure(f"expected a non-empty {key!r} list\n{result.describe()}")
    return value


@lru_cache(maxsize=1)
def raid_zones() -> tuple[dict[str, Any], ...]:
    """Every raid zone Warcraft Logs knows about, newest first."""
    result = run("warcraftlogs", "zones")
    raids = [
        zone
        for zone in _rows(result, "zones")
        if isinstance(zone, dict)
        and zone.get("encounters")
        and {difficulty.get("id") for difficulty in zone.get("difficulties") or []} >= RAID_DIFFICULTY_IDS
    ]
    return tuple(
        sorted(raids, key=lambda zone: ((zone.get("expansion") or {}).get("id") or 0, zone.get("id") or 0), reverse=True)
    )


def current_raid_zone() -> dict[str, Any]:
    """The newest unfrozen raid zone Warcraft Logs knows about."""
    zone = next((zone for zone in raid_zones() if not zone.get("frozen")), None)
    if zone is None:
        raise JourneyFailure(f"no unfrozen raid zone among {[zone['name'] for zone in raid_zones()]}")
    return zone


def _fight_roster(code: str, fight_id: int, *, required: bool = True, endpoint: str = "client") -> tuple[dict[str, Any], ...]:
    """The fight's players by role. Warcraft Logs keeps no player details for some reports; such a
    roster fails the journey unless ``required`` is false, when it comes back empty."""
    result = run("warcraftlogs", "--endpoint", endpoint, "report-encounter-players", code, "--fight-id", str(fight_id))
    roles = (result.data["player_details"] or {}).get("roles") or {}
    roster = [{**row, "role": role} for role, rows in roles.items() for row in rows if isinstance(row, dict)]
    if not roster and required:
        raise JourneyFailure(f"fight {fight_id} of {code} has no players\n{result.describe()}")
    return tuple(roster)


@dataclass(frozen=True)
class FightWindow:
    """One fight placed on the wall clock: report start plus the report-relative fight offsets."""

    encounter_id: int
    difficulty: int
    start_ms: float
    end_ms: float


def _absolute_fight_window(code: str, fight_id: int, *, endpoint: str = "client") -> FightWindow:
    report = run("warcraftlogs", "--endpoint", endpoint, "report", code).data["report"]
    fights = run("warcraftlogs", "--endpoint", endpoint, "report-fights", code).data["fights"]
    fight = next((row for row in fights if row.get("id") == fight_id), None)
    if fight is None:
        raise JourneyFailure(f"report {code} has no fight {fight_id}")
    base = float(report["start_time"])
    return FightWindow(
        encounter_id=int(fight["encounter_id"]),
        difficulty=int(fight["difficulty"]),
        start_ms=base + float(fight["start_time"]),
        end_ms=base + float(fight["end_time"]),
    )


@cache
def guild_reports(zone_id: int) -> tuple[dict[str, Any], ...]:
    """The pinned guild's most recent reports in one zone; the guild anchor and the wide cohort share them."""
    result = run("warcraftlogs", "--endpoint", "user", "guild-reports", *GUILD, "--zone-id", str(zone_id), "--limit", str(DISCOVERY_REPORT_LIMIT))
    return tuple(result.data["reports"])


@lru_cache(maxsize=1)
def guild_anchor() -> Anchor:
    """The newest current-tier kill in the pinned guild's own private logs, with its roster."""
    zone = current_raid_zone()
    reports = guild_reports(int(zone["id"]))
    for report in reports:
        # The guild logs privately, but posts the odd report unlisted; only a private one proves the
        # user-token-only access this anchor exists for.
        if report.get("visibility") != "private":
            continue
        code = str(report["code"])
        fights = run("warcraftlogs", "--endpoint", "user", "report-fights", code).data["fights"]
        # Only the tier's own bosses: a raid-zone report can also hold a Mythic+ run (difficulty 10),
        # whose dungeon "encounter" would otherwise win the difficulty tie-break below.
        raid_bosses = {int(boss["id"]) for boss in zone["encounters"]}
        kills = [fight for fight in fights if fight.get("kill") and fight.get("encounter_id") in raid_bosses]
        if not kills:
            continue
        # Prefer the hardest difficulty in the report; ties go to the latest pull.
        fight = max(kills, key=lambda row: (row.get("difficulty") or 0, row.get("id") or 0))
        # Some of the guild's reports carry no player details at all (Lf1hGgRZcXzM9nN6); the next one does.
        roster = _fight_roster(code, int(fight["id"]), required=False, endpoint="user")
        if not roster:
            continue
        return Anchor(
            zone=zone,
            code=code,
            listed_guild=str(report["guild"]["name"]),
            listed_region=pins.GUILD_REGION,
            report=report,
            fight=fight,
            players=roster,
        )
    raise JourneyFailure(
        f"{pins.GUILD_NAME!r} has no kill in its {len(reports)} newest {zone['name']!r} reports, so "
        "nothing here exercises a private report (expected only in the days after a tier rollover)"
    )


@lru_cache(maxsize=1)
def anchor() -> Anchor:
    """A kill inside a *public*, guild-owned report, taken from the current tier's own leaderboard.

    Encounter rankings list ranked parses, so each row is a guaranteed kill; its report has to be
    public (any client token can read it) and belong to a guild (so the sampled cohort can be scoped
    to that guild and provably contain this kill). Together with :func:`guild_anchor` this is what
    :func:`test_report_visibility_decides_which_token_can_read_it` contrasts.
    """
    zone = current_raid_zone()
    scanned: list[str] = []
    for boss in zone["encounters"][:PUBLIC_ANCHOR_BOSS_ATTEMPTS]:
        ranked = run(
            "warcraftlogs",
            "encounter-rankings",
            "--zone-id", str(zone["id"]),
            "--boss-id", str(boss["id"]),
            "--difficulty", str(MYTHIC_DIFFICULTY_ID),
            "--top", str(PUBLIC_ANCHOR_ROW_ATTEMPTS),
        )
        for row in ranked.data["rankings"]["rows"]:
            code, fight_id = row.get("report_code"), row.get("fight_id")
            if not isinstance(code, str) or not isinstance(fight_id, int):
                continue
            scanned.append(f"{code}#{fight_id}")
            if not row.get("guild_name"):
                continue
            detail = run("warcraftlogs", "report", code).data["report"]
            if detail.get("visibility") != "public" or not detail.get("guild"):
                continue
            # A report that logged more Mythic+ runs than raid pulls is filed under the Mythic+ zone,
            # so the zone-scoped cohorts and listings would never see it.
            if (detail.get("zone") or {}).get("id") != zone["id"]:
                continue
            fights = run("warcraftlogs", "report-fights", code).data["fights"]
            fight = next((row for row in fights if row.get("id") == fight_id and row.get("kill")), None)
            if fight is None:
                continue
            roster = _fight_roster(code, fight_id, endpoint="client")
            return Anchor(
                zone=zone,
                code=code,
                listed_guild=str(row["guild_name"]),
                listed_region=str(row["server_region"]),
                report=detail,
                fight=fight,
                players=roster,
            )
    raise JourneyFailure(f"no ranked kill in a public guild report in {zone['name']!r}; scanned {scanned}")


def _report_window(reports: tuple[dict[str, Any], ...]) -> tuple[int, int]:
    """Report-time bounds covering every one of ``reports``."""
    start = min(int(row["start_time"]) for row in reports) - SAMPLE_WINDOW_PADDING_MS
    end = max(int(row["end_time"]) for row in reports) + SAMPLE_WINDOW_PADDING_MS
    return start, end


@dataclass(frozen=True)
class WideCohort:
    """A sampled scope holding more than one kill, so an ordering or dedupe claim can be wrong."""

    zone: dict[str, Any]
    boss: dict[str, Any]
    args: tuple[str, ...]
    kills: tuple[dict[str, Any], ...]
    sample: dict[str, Any]


def _guild_boss_cohorts(zone: dict[str, Any], scanned: list[str]) -> Iterator[WideCohort]:
    """Every boss of ``zone`` as the pinned guild's cohort over its newest reports, noting each in ``scanned``."""
    reports = guild_reports(int(zone["id"]))
    if not reports:
        scanned.append(f"{zone['name']}: no guild reports")
        return
    start, end = _report_window(reports)
    for boss in zone["encounters"]:
        args = (
            "--zone-id", str(zone["id"]),
            "--boss-id", str(boss["id"]),
            "--guild-region", pins.GUILD_REGION,
            "--guild-realm", pins.GUILD_REALM,
            "--guild-name", pins.GUILD_NAME,
            "--start-time", str(start),
            "--end-time", str(end),
            "--report-pages", SAMPLE_REPORT_PAGES,
            "--reports-per-page", str(DISCOVERY_REPORT_LIMIT),
        )
        result = run("warcraftlogs", "--endpoint", "user", "boss-kills", *args, "--top", str(WIDE_COHORT_TOP))
        kills = tuple(result.data["kills"])
        sample = result.data["sample"]
        scanned.append(
            f"{zone['name']} / {boss['name']}: {len(kills)} kill(s), {sample['duplicates_removed']} duplicate(s)"
        )
        yield WideCohort(zone=zone, boss=boss, args=args, kills=kills, sample=sample)


def _zone_wide_cohort(zone: dict[str, Any], scanned: list[str]) -> WideCohort | None:
    """The first boss of ``zone`` the pinned guild's newest reports hold a qualifying cohort for."""
    return next(
        (
            cohort
            for cohort in _guild_boss_cohorts(zone, scanned)
            if len({row["duration_ms"] for row in cohort.kills}) >= 2 and cohort.sample["duplicates_removed"] >= 1
        ),
        None,
    )


@lru_cache(maxsize=1)
def wide_cohort() -> WideCohort:
    """The pinned guild's newest reports of one tier, every difficulty, on a boss it killed more than once.

    :func:`cohort_args` is one report window, which after duplicate collapsing is a single kill: an
    ordering claim over one row is true whatever the product does, and a dedupe claim has nothing to
    collapse. This walks each tier's bosses until one yields at least two kills of different lengths
    *and* at least one collapsed duplicate report, and says exactly what it scanned when none does.
    A tier that opened days ago has no boss killed twice yet, so the walk falls back to the tiers
    before it. The guild kills a boss about once per difficulty, so the difficulty is left unfiltered.
    """
    scanned: list[str] = []
    for zone in raid_zones()[:WIDE_COHORT_ZONES]:
        cohort = _zone_wide_cohort(zone, scanned)
        if cohort is not None:
            return cohort
    raise JourneyFailure(
        f"no boss in the {WIDE_COHORT_ZONES} newest raid zones gives {pins.GUILD_NAME!r} two kills of different "
        f"lengths with a double-logged pull among them, so ordering and dedupe cannot be proved: {scanned}"
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


def assert_sampling_metadata(
    result: Result,
    *,
    expect_rows: bool,
    wide: WideCohort | None = None,
) -> dict[str, Any]:
    """Every sampled command must describe its own cohort per SAFE_ANALYTICS_RULES.md.

    The zone, boss, difficulty and a report every populated cohort has to cite come from the anchor,
    or from ``wide`` for the wide cohort, which filters no difficulty. Echoing back all three filters
    matters because a sampled answer is only quotable next to the scope it was sampled from.
    """
    if wide is None:
        found = anchor()
        zone_id, boss_id, difficulty, code = found.zone["id"], found.fight["encounter_id"], found.fight["difficulty"], found.code
    else:
        zone_id, boss_id, difficulty, code = wide.zone["id"], wide.boss["id"], None, wide.kills[0]["report"]["code"]
    data = result.data
    scope = data.get("sample_scope") or {}
    sample = data.get("sample") or {}
    assert data.get("ranking_basis"), result.describe()
    assert data.get("matching_rule"), result.describe()
    assert (data.get("freshness") or {}).get("sampled_at"), result.describe()
    assert (data.get("cache_provenance") or {}).get("source"), result.describe()
    filters = scope.get("filters") or {}
    assert filters.get("zone_id") == zone_id, result.describe()
    assert filters.get("boss_id") == boss_id, result.describe()
    assert filters.get("difficulty") == difficulty, result.describe()
    assert isinstance(scope.get("returned"), int), result.describe()
    assert sample.get("source_report_count", 0) >= 1, result.describe()
    if expect_rows:
        assert scope["returned"] > 0, result.describe()
        codes = [row.get("report_code") for row in (data.get("citations") or {}).get("sample_reports") or []]
        assert code in codes, result.describe()
    return data


@lru_cache(maxsize=1)
def anchor_aura_id() -> int:
    """An aura game id applied again and again during the anchor kill, so both halves of the fight hold it.

    The most-applied aura, not the longest-held one: a pre-pull food buff is held all fight but
    applied once, which gives the two-window comparison nothing to set side by side. The most-applied
    one can still be an encounter mechanic of one phase (Fury of the Dead on Nek'zali), so each
    candidate must have a holder in the second half too.
    """
    found = anchor()
    result = run("warcraftlogs", "report-encounter-buffs", found.url, "--view-by", "source", "--preview-limit", "25")
    preview = [row for row in (result.data["buffs"] or {}).get("preview") or [] if isinstance((row.get("aura") or {}).get("game_id"), int)]
    if not preview:
        raise JourneyFailure(f"no aura game id in the anchor fight's buff preview\n{result.describe()}")
    duration = int(found.fight["end_time"]) - int(found.fight["start_time"])
    half = max(duration // 2, 1000)
    candidates = sorted(preview, key=lambda row: row.get("reported_total_uses") or 0, reverse=True)
    for row in candidates[:AURA_CANDIDATE_ATTEMPTS]:
        game_id = int(row["aura"]["game_id"])
        second_half = run(
            "warcraftlogs", "report-encounter-aura-summary", found.url, "--ability-id", str(game_id),
            "--window-start-ms", str(half), "--window-end-ms", str(duration),
        )
        if second_half.data["aura_summary"]["rows"]:
            return game_id
    raise JourneyFailure(f"none of the {AURA_CANDIDATE_ATTEMPTS} most-applied auras is held in the anchor kill's second half")


@lru_cache(maxsize=1)
def anchor_ability_id() -> int:
    """The anchor kill's ability with the most cast-bar or empower events.

    The Casts data type also returns ``begincast``, ``empowerstart`` and ``empowerend`` rows, and a
    cast count must skip them; only an ability that has some can show a count that does not.
    """
    found = anchor()
    result = run(
        "warcraftlogs", "report-events", found.code, "--fight-id", str(found.fight_id), "--data-type", "casts",
        "--limit", "200", "--filter-expression", 'type != "cast"',
    )
    abilities = Counter(event["abilityGameID"] for event in result.data["events"])
    if not abilities:
        raise JourneyFailure(f"no cast-bar or empower event in the anchor fight\n{result.describe()}")
    return int(abilities.most_common(1)[0][0])


@lru_cache(maxsize=1)
def anchor_wipe_fight_id() -> int:
    """The longest boss wipe in the anchor report.

    ``--wipe-cutoff`` only has anything to cut on a pull that wiped, so the flag cannot be proved
    against the anchor kill itself.
    """
    found = anchor()
    fights = run("warcraftlogs", "report-fights", found.code).data["fights"]
    wipes = [fight for fight in fights if fight.get("encounter_id") and fight.get("kill") is False]
    if not wipes:
        raise JourneyFailure(f"report {found.code} has no boss wipe, so --wipe-cutoff cannot be exercised against it")
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
    detail = zone_result.data["zone"]
    assert detail["id"] == zone["id"], zone_result.describe()
    assert detail["name"] == zone["name"], zone_result.describe()
    assert {row["id"] for row in detail["encounters"]} == {row["id"] for row in zone["encounters"]}, zone_result.describe()

    encounter_result = run("warcraftlogs", "encounter", str(boss["id"]))
    assert encounter_result.payload["kind"] == "encounter", encounter_result.describe()
    encounter = encounter_result.data["encounter"]
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
    detail = server.data["server"]
    assert detail["slug"] == pins.GUILD_REALM, server.describe()
    assert detail["name"] == pins.GUILD_REALM_DISPLAY, server.describe()


def test_rate_limit_reports_the_hourly_point_budget(require):
    require("warcraftlogs")
    result = run("warcraftlogs", "rate-limit")
    assert result.payload["kind"] == "rate_limit", result.describe()
    limit = result.data["rate_limit"]
    assert limit["limit_per_hour"] > 0, result.describe()
    assert 0 <= limit["points_spent_this_hour"] <= limit["limit_per_hour"], result.describe()


# --------------------------------------------------------------------------------------------
# Auth (read-only)
# --------------------------------------------------------------------------------------------


def test_doctor_reports_a_ready_provider_on_the_retail_profile(require):
    require("warcraftlogs")
    result = run("warcraftlogs", "doctor")
    doctor = result.data["status"]
    assert doctor == "ready", result.describe()
    data = result.data["auth"]
    assert data["required"] is True, result.describe()
    assert data["configured"] is True, result.describe()
    assert data["public_api_access"]["ready"] is True, result.describe()
    assert result.data["site_profile"]["key"] == "retail", result.describe()
    assert_no_credential_values(data["state"])


def test_auth_status_reports_client_and_user_readiness_without_leaking_the_token(require):
    require("warcraftlogs")
    result = run("warcraftlogs", "auth", "status")
    auth = result.data["auth"]
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
    client = client_result.data["client"]
    assert client["configured"] is True, client_result.describe()
    assert client["client_id"].endswith("..."), "auth client must only show a truncated client id"
    assert client["token_url"].endswith("/oauth/token"), client_result.describe()
    assert client["client_api_url"].endswith("/api/v2/client"), client_result.describe()

    token_result = run("warcraftlogs", "auth", "token")
    token = token_result.data["token"]
    # A saved user token is what `auth whoami` reads, so the token surface must report the same one.
    assert token["endpoint_family"] == "user", token_result.describe()
    assert token["state"]["has_access_token"] is True, token_result.describe()
    granted = token["scopes"]["granted"]
    assert isinstance(granted, list) and granted, token_result.describe()
    assert_no_credential_values(token["state"])


def test_auth_whoami_names_the_account_behind_the_saved_user_token(require):
    require("warcraftlogs")
    result = run("warcraftlogs", "auth", "whoami")
    assert result.data["endpoint_family"] == "user", result.describe()
    user = result.data["user"]
    assert isinstance(user["id"], int) and user["id"] > 0, result.describe()
    assert isinstance(user["name"], str) and user["name"].strip(), result.describe()


def test_site_profiles_route_classic_and_fresh_with_the_same_client(require):
    require("warcraftlogs")
    for site, host in (("classic", "classic.warcraftlogs.com"), ("fresh", "fresh.warcraftlogs.com")):
        status = run("warcraftlogs", "--site", site, "auth", "status", "--no-live")
        profile = status.data["auth"]["site_profile"]
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
        assert (search.data["count"], search.data["total_matches"], search.data["truncated"]) == (1, 1, False), search.describe()

    from_url = run("warcraftlogs", "resolve", found.url)
    assert from_url.payload["kind"] == "resolve_match", from_url.describe()
    assert from_url.data["resolved"] is True, from_url.describe()
    match = from_url.data["match"]
    assert match["report_reference"]["code"] == found.code, from_url.describe()
    assert match["url"] == f"https://www.warcraftlogs.com/reports/{found.code}#fight={found.fight_id}", from_url.describe()
    assert match["report_reference"]["fight_id"] == found.fight_id, from_url.describe()
    assert str(found.fight_id) in from_url.data["next_command"], from_url.describe()
    # next_command is handed to an agent to run as written, so it has to run and read that fight.
    binary, *args = shlex.split(from_url.data["next_command"])
    handed_over = run(binary, *args)
    assert (handed_over.data["reference"]["code"], handed_over.data["fight"]["id"]) == (found.code, found.fight_id), handed_over.describe()

    # Warcraft Logs writes the fight as ``?fight=N`` as often as ``#fight=N``; both carry it.
    query_form = run("warcraftlogs", "resolve", f"https://www.warcraftlogs.com/reports/{found.code}?fight={found.fight_id}")
    assert query_form.data["match"]["report_reference"]["fight_id"] == found.fight_id, query_form.describe()

    # A bare code is matched by its shape alone: medium confidence, unresolved, its command on the match.
    from_code = run("warcraftlogs", "resolve", found.code)
    assert from_code.data["match"]["report_reference"]["code"] == found.code, from_code.describe()
    assert (from_code.data["resolved"], from_code.data["confidence"], from_code.data["next_command"]) == (False, "medium", None), from_code.describe()
    assert from_code.data["match"]["follow_up"]["command"] == f"warcraftlogs report {found.code}", from_code.describe()

    # Report codes are 16 letters and digits, and some have no digit at all; parsing one needs no
    # network. A 16-letter word is still a word, not a report.
    for query in ("https://www.warcraftlogs.com/reports/JVFTxcKCqrvpaAzD#fight=4", "JVFTxcKCqrvpaAzD"):
        lettered = run("warcraftlogs", "search", query)
        assert _rows(lettered, "results")[0]["report_reference"]["code"] == "JVFTxcKCqrvpaAzD", lettered.describe()
    for word_query in ("frostdeathknight", "FrostDeathKnight"):
        word = run("warcraftlogs", "resolve", word_query)
        assert not (word.data.get("match") or {}).get("report_reference"), word.describe()


def test_a_classic_report_url_selects_the_classic_site(require):
    """A report code only exists on its own site, so a classic URL has to carry ``--site classic``."""
    require("warcraftlogs")
    listing = run("warcraftlogs", "--site", "classic", "reports", "--limit", "1")
    code = str(_rows(listing, "reports")[0]["code"])
    url = f"https://classic.warcraftlogs.com/reports/{code}"

    resolved = run("warcraftlogs", "resolve", url)
    assert resolved.data["next_command"] == f"warcraftlogs --site classic report {code}", resolved.describe()
    assert resolved.data["match"]["url"] == url, resolved.describe()
    binary, *args = shlex.split(resolved.data["next_command"])
    assert run(binary, *args).data["report"]["code"] == code

    mismatch = run("warcraftlogs", "report-encounter", f"{url}#fight=1", expect=EXIT_USAGE, error_code="invalid_query")
    assert "--site classic" in mismatch.payload["error"]["message"], mismatch.describe()


def test_guild_family_reports_the_pinned_guild(require):
    require("warcraftlogs")
    zone = current_raid_zone()

    guild = run("warcraftlogs", "guild", *GUILD)
    detail = guild.data["guild"]
    assert detail["name"].lower() == pins.GUILD_NAME, guild.describe()
    assert detail["server"]["slug"] == pins.GUILD_REALM, guild.describe()
    # Players type the realm's display name; Warcraft Logs slugs Mal'Ganis without the apostrophe.
    by_name = run("warcraftlogs", "guild", pins.GUILD_REGION, pins.GUILD_REALM_DISPLAY, pins.GUILD_NAME)
    assert by_name.data["guild"]["id"] == detail["id"], by_name.describe()

    members = run("warcraftlogs", "guild-members", *GUILD, "--limit", "5")
    roster = members.data["guild_members"]
    assert roster["count"] > 0, members.describe()
    assert all(row["name"] for row in roster["members"]), members.describe()

    rankings = run("warcraftlogs", "guild-rankings", *GUILD, "--zone-id", str(zone["id"]))
    ranks = rankings.data["guild_rankings"]
    assert ranks["name"].lower() == pins.GUILD_NAME, rankings.describe()
    assert ranks["server"]["slug"] == pins.GUILD_REALM, rankings.describe()
    # The guild has kills this tier (guild_anchor), so it holds a progress rank at every scope, and a
    # rank can only get better as the scope narrows: a swapped or dropped scope breaks the order.
    progress = ranks["zone_ranking"]["progress"]
    world, region, server = (progress[scope]["number"] for scope in ("world", "region", "server"))
    assert all(isinstance(rank, int) for rank in (world, region, server)), rankings.describe()
    assert 0 < server <= region <= world, rankings.describe()

    attendance = run("warcraftlogs", "--endpoint", "user", "guild-attendance", *GUILD, "--limit", "3")
    rows = attendance.data["guild_attendance"]
    assert rows["count"] > 0, attendance.describe()
    night = rows["attendance"][0]
    assert night["code"], attendance.describe()
    assert night["player_count"] > 0, attendance.describe()
    assert night["players"][0]["name"], attendance.describe()


def test_character_and_character_rankings_resolve_the_pinned_character(require):
    require("warcraftlogs")
    zone = current_raid_zone()

    character = run("warcraftlogs", "character", pins.GUILD_REGION, pins.GUILD_REALM, pins.CHARACTER_NAME)
    detail = character.data["character"]
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
    payload = rankings.data["character_rankings"]
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
    assert result.data["ranking_basis"], result.describe()
    encounter = result.data["encounter"]
    assert encounter["id"] == found.fight["encounter_id"], result.describe()
    assert encounter["name"] == found.fight["name"], result.describe()
    rankings = result.data["rankings"]
    assert rankings["count"] == len(rankings["rows"]), result.describe()
    assert 0 < len(rankings["rows"]) <= 3, "the current tier's anchor boss must have public encounter rankings"
    assert all(row.get("name") and row.get("class_name") for row in rankings["rows"]), result.describe()


# Every healing spec, as Warcraft Logs names class and spec on a ranking row.
HEALER_SPECS = frozenset(
    {
        ("Priest", "Holy"), ("Priest", "Discipline"), ("Paladin", "Holy"), ("Druid", "Restoration"),
        ("Shaman", "Restoration"), ("Monk", "Mistweaver"), ("Evoker", "Preservation"),
    }
)


def test_encounter_rankings_class_spec_metric_and_page_reach_warcraft_logs(require):
    """The leaderboard filters go to Warcraft Logs, whose own row fields prove each one landed.

    Multi-word classes and specs are spelled the way players type them; Warcraft Logs rejected the
    spaced display names (``Death Knight``, ``Beast Mastery``) the CLI once sent. ``--page 2`` must
    continue the ranking where page 1 stopped, not repeat it.
    """
    require("warcraftlogs")
    found = anchor()
    scope = (
        "encounter-rankings", "--zone-id", str(found.zone["id"]), "--boss-id", str(found.fight["encounter_id"]),
        "--difficulty", str(found.fight["difficulty"]),
    )

    # Both pages skip the cache: an earlier test's page 1 can be minutes older than a fresh page 2,
    # and a live leaderboard shifts rows across the page boundary in that time.
    first = run("warcraftlogs", *scope, "--top", "100", env=no_cache_env())
    first_rows = first.data["rankings"]["rows"]
    assert len({row["class_name"] for row in first_rows}) >= 2, "an unfiltered leaderboard holds more than one class"

    for flags, expected in (
        (("--class-name", "death-knight"), {"class_name": "DeathKnight"}),
        (("--class-name", "hunter", "--spec-name", "beast-mastery"), {"class_name": "Hunter", "spec_name": "BeastMastery"}),
        # Shorthand and another provider's slug name the same filter.
        (("--class-name", "dk"), {"class_name": "DeathKnight"}),
        (("--class-name", "hunter", "--spec-name", "bm"), {"class_name": "Hunter", "spec_name": "BeastMastery"}),
        # A spec spelling that names its class needs no --class-name.
        (("--spec-name", "bm hunter"), {"class_name": "Hunter", "spec_name": "BeastMastery"}),
    ):
        narrowed = run("warcraftlogs", *scope, *flags, "--top", "10")
        rows = narrowed.data["rankings"]["rows"]
        assert rows, narrowed.describe()
        assert all({key: row[key] for key in expected} == expected for row in rows), narrowed.describe()

    healers = run("warcraftlogs", *scope, "--metric", "hps", "--top", "10")
    healer_rows = healers.data["rankings"]["rows"]
    assert healer_rows and {(row["class_name"], row["spec_name"]) for row in healer_rows} <= HEALER_SPECS, healers.describe()

    second = run("warcraftlogs", *scope, "--top", "10", "--page", "2", env=no_cache_env())
    rankings = second.data["rankings"]
    assert rankings["page"] == 2, second.describe()
    assert rankings["rows"][0]["rank"] == first.data["rankings"]["page_count"] + 1, second.describe()

    def keys(rows: list[dict[str, Any]]) -> set[tuple[Any, ...]]:
        return {(row["report_code"], row["fight_id"], row["name"]) for row in rows}

    assert not keys(rankings["rows"]) & keys(first_rows), "page 2 repeats rows from page 1"


def test_reports_and_guild_reports_list_the_current_tier(require):
    require("warcraftlogs")
    zone = current_raid_zone()
    found = anchor()

    public = run("warcraftlogs", "reports", "--zone-id", str(zone["id"]), "--limit", "3")
    rows = _rows(public, "reports")
    # The current tier has far more than three public reports, so the cap has to bite.
    assert public.data["pagination"]["has_more_pages"] is True, public.describe()
    assert len(rows) == 3, public.describe()
    assert all(row["zone"]["id"] == zone["id"] for row in rows), public.describe()

    listing = run("warcraftlogs", "--endpoint", "user", "guild-reports", *GUILD, "--zone-id", str(zone["id"]), "--limit", "3")
    guild_rows = _rows(listing, "reports")
    assert all(row["guild"]["name"].lower() == pins.GUILD_NAME for row in guild_rows), listing.describe()
    assert listing.data["pagination"]["total"] > 0, listing.describe()
    assert found.report["zone"]["id"] == zone["id"], found.report


def test_report_listings_page_forward(require):
    """``--page`` moves both report listings forward instead of answering page 1 again.

    The guild's own listing barely changes between two reads, so page 2 of two rows must be exactly
    rows three and four of a four-row first page. The public listing moves with every upload, so only
    Warcraft Logs' own pagination block can witness its page.
    """
    require("warcraftlogs")
    wide = run("warcraftlogs", "--endpoint", "user", "guild-reports", *GUILD, "--limit", "4")
    paged = run("warcraftlogs", "--endpoint", "user", "guild-reports", *GUILD, "--limit", "2", "--page", "2")
    codes = [row["code"] for row in _rows(wide, "reports")]
    assert len(codes) == 4, wide.describe()
    assert [row["code"] for row in _rows(paged, "reports")] == codes[2:], paged.describe()
    assert paged.data["pagination"]["current_page"] == 2, paged.describe()

    public = run("warcraftlogs", "reports", "--zone-id", str(current_raid_zone()["id"]), "--limit", "3", "--page", "2")
    assert len(_rows(public, "reports")) == 3, public.describe()
    assert (public.data["pagination"]["current_page"], public.data["pagination"]["from"]) == (2, 4), public.describe()


# --------------------------------------------------------------------------------------------
# Report surfaces
# --------------------------------------------------------------------------------------------

_VISIBILITY_QUERY = "query R($code: String!) { reportData { report(code: $code) { code visibility } } }"


def _client_view(code: str) -> Any:
    """What the *client* (application) token alone can see of a report: the row, or ``None``."""
    result = run("warcraftlogs", "graphql", "--endpoint", "client", "--query", _VISIBILITY_QUERY, "--report-code", code)
    assert result.payload["query"]["endpoint"] == "client", result.describe()
    return result.data["reportData"]["report"]


def test_report_visibility_decides_which_token_can_read_it(require):
    """Both visibility modes, contrasted on the same command.

    Every other report journey reads the public anchor. The pinned guild logs privately, and this is
    the one journey that proves the saved *user* token opens a report the client token cannot: the
    private report is invisible to the client token and readable through the user endpoint, while
    the public anchor is readable by the client token as well.
    """
    require("warcraftlogs")
    private, public = guild_anchor(), anchor()
    assert private.report["visibility"] == "private", (
        f"the pinned guild's report {private.code} is no longer private, so nothing in this suite "
        "exercises user-token-only report access any more"
    )
    assert public.report["visibility"] == "public", public.report

    assert _client_view(private.code) is None, f"the client token could read private report {private.code}"
    assert _client_view(public.code) == {"code": public.code, "visibility": "public"}, public.report

    # And the saved user token reaches the private one the client token just could not.
    through_user = run(
        "warcraftlogs", "graphql", "--endpoint", "user", "--query", _VISIBILITY_QUERY, "--report-code", private.code
    )
    assert through_user.data["reportData"]["report"] == {"code": private.code, "visibility": "private"}, (
        through_user.describe()
    )

    # The normal report command reads the private one too; it names the pinned guild.
    detail = run("warcraftlogs", "--endpoint", "user", "report", private.code).data["report"]
    assert detail["guild"]["name"].lower() == pins.GUILD_NAME, detail


def test_report_and_report_fights_echo_the_discovered_report(require):
    require("warcraftlogs")
    found = anchor()

    report = run("warcraftlogs", "report", found.code)
    detail = report.data["report"]
    # found.code is the leaderboard row's report code, not an earlier `report` answer.
    assert detail["code"] == found.code, report.describe()
    assert detail["zone"]["id"] == found.zone["id"], report.describe()
    # `report` and the leaderboard row that discovered it must agree on who owns the report.
    assert detail["guild"]["name"] == found.listed_guild, report.describe()
    assert detail["guild"]["server"]["region"]["slug"].lower() == found.listed_region.lower(), report.describe()

    fights = run("warcraftlogs", "report-fights", found.code)
    assert fights.payload["kind"] == "report_fights", fights.describe()
    rows = _rows(fights, "fights")
    assert fights.data["count"] == len(rows), fights.describe()
    assert found.fight_id in {row["id"] for row in rows}, fights.describe()
    assert any(row.get("kill") for row in rows), fights.describe()

    filtered = run("warcraftlogs", "report-fights", found.code, "--difficulty", str(found.fight["difficulty"]))
    filtered_rows = _rows(filtered, "fights")
    assert {row["difficulty"] for row in filtered_rows} == {found.fight["difficulty"]}, filtered.describe()

    # A report URL names the same report as its bare code.
    by_url = run("warcraftlogs", "report", found.url)
    assert by_url.data["report"]["code"] == found.code, by_url.describe()


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
    finished = time.time() * 1000 - encounter.data["report"]["end_time"] >= FINISHED_REPORT_QUIET_MS
    assert encounter.data["stability"]["report_finished"] is finished, encounter.describe()
    assert encounter.data["stability"]["live"] is not finished, encounter.describe()

    players = run("warcraftlogs", "report-encounter-players", found.code, "--fight-id", str(found.fight_id))
    details = players.data["player_details"]
    roster = [row for rows in details["roles"].values() for row in rows]
    # The roster size is cross-checked against the raid size `report-encounter` read off the fight
    # itself; comparing it to the discovery fixture would only compare this command with itself.
    assert details["counts"]["total"] == len(roster), players.describe()
    assert details["counts"]["total"] == encounter.data["fight"]["size"], players.describe()
    assert set(details["roles"]) <= {"tanks", "healers", "dps"}, players.describe()
    assert all(isinstance(row.get("name"), str) and row["name"].strip() for row in roster), players.describe()
    assert sum(details["counts"][role] for role in details["roles"]) == details["counts"]["total"], players.describe()


def test_report_encounter_casts_and_buffs_summarize_real_events(require):
    require("warcraftlogs")
    found = anchor()

    casts = run("warcraftlogs", "report-encounter-casts", found.url, "--limit", "200", "--preview-limit", "5")
    summary = casts.data["casts"]
    assert summary["event_count"] > 0, casts.describe()
    assert len(summary["preview"]) <= 5, casts.describe()
    assert summary["by_ability"], casts.describe()
    assert summary["by_source"][0]["source"]["name"], casts.describe()

    buffs = run("warcraftlogs", "report-encounter-buffs", found.url, "--view-by", "source", "--preview-limit", "5")
    buff_summary = buffs.data["buffs"]
    assert buff_summary["total"] > 0, buffs.describe()
    assert buff_summary["view_by"].lower() == "source", buffs.describe()
    assert len(buff_summary["preview"]) == min(5, buff_summary["total"]), buffs.describe()
    assert buff_summary["preview_truncated"] == (buff_summary["total"] > 5), buffs.describe()
    # The reported_* fields are straight passthroughs of the upstream auras row; a rename upstream
    # would otherwise show up as silently null typed fields.
    row = buff_summary["preview"][0]
    assert row["aura"]["name"] and isinstance(row["aura"]["game_id"], int), buffs.describe()
    assert row["aura"]["identity_contract"]["source"]["provider"] == "warcraftlogs", buffs.describe()
    assert row["aura_holder"]["identity_contract"]["source"]["provider"] == "warcraftlogs", buffs.describe()
    assert {"reported_total_uptime", "reported_total_uses", "reported_bands"} <= set(row), buffs.describe()


def test_report_encounter_aura_summary_and_compare_use_explicit_windows(require):
    require("warcraftlogs")
    found = anchor()
    ability_id = anchor_aura_id()
    duration = int(found.fight["end_time"]) - int(found.fight["start_time"])
    half = max(duration // 2, 1000)

    def window_summary(start: int, end: int) -> Result:
        return run(
            "warcraftlogs",
            "report-encounter-aura-summary",
            found.url,
            "--ability-id", str(ability_id),
            "--window-start-ms", str(start),
            "--window-end-ms", str(end),
        )

    summary, second_half = window_summary(0, half), window_summary(half, duration)
    assert summary.payload["kind"] == "report_encounter_aura_summary", summary.describe()
    assert summary.data["aura"]["game_id"] == ability_id, summary.describe()
    assert summary.data["aura"]["name"], summary.describe()
    rows = summary.data["aura_summary"]["rows"]
    assert summary.data["aura_summary"]["entry_count"] == len(rows), summary.describe()
    assert rows, "the discovered aura must have at least one holder in its own fight"
    assert summary.data["aura_summary"]["row_actor"] == "aura_holder", summary.describe()
    assert rows[0]["aura_holder"]["name"], summary.describe()
    # An ignored window would give both halves the whole fight's table, and then every comparison
    # below would agree with itself.
    assert summary.data["aura_summary"] != second_half.data["aura_summary"], second_half.describe()

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
    windows = {window["label"]: window for window in compare.data["windows"]}
    assert windows["left"]["query"]["window_end_ms"] == windows["right"]["query"]["window_start_ms"], compare.describe()
    assert windows["left"]["query"]["ability_id"] == ability_id, compare.describe()
    # Each side is the same aura table the single-window summary reads for that window.
    assert windows["left"]["aura_summary"] == summary.data["aura_summary"], compare.describe()
    assert windows["right"]["aura_summary"] == second_half.data["aura_summary"], compare.describe()

    # The deltas are right minus left of the two single-window summaries, per holder (a holder missing
    # from one window has no delta). Every delta once came back null on real data.
    def uptime_by_holder(result: Result) -> dict[Any, Any]:
        return {row["aura_holder"]["id"]: row["reported_total_uptime"] for row in result.data["aura_summary"]["rows"]}

    left_uptime, right_uptime = uptime_by_holder(summary), uptime_by_holder(second_half)
    assert set(left_uptime) & set(right_uptime), f"no holder in both windows: {left_uptime} vs {right_uptime}"
    compared = compare.data["comparison"]["rows"]
    assert {row["aura_holder"]["id"] for row in compared} == set(left_uptime) | set(right_uptime), compare.describe()
    for row in compared:
        holder = row["aura_holder"]["id"]
        both = holder in left_uptime and holder in right_uptime
        expected = right_uptime[holder] - left_uptime[holder] if both else None
        assert row["reported_total_uptime_delta"] == expected, compare.describe()
    deltas = [abs(row["reported_total_uptime_delta"]) for row in compared if row["reported_total_uptime_delta"] is not None]
    assert deltas == sorted(deltas, reverse=True), compare.describe()

    # A window that opens after the pull ended holds nothing, and zero would read as "never up".
    past_end = run(
        "warcraftlogs", "report-encounter-aura-summary", found.url, "--ability-id", str(ability_id),
        "--window-start-ms", str(duration + 1000), expect=EXIT_USAGE, error_code="invalid_query",
    )
    assert "--window-start-ms" in past_end.payload["error"]["message"], past_end.describe()

    # A window that runs past the pull is clamped to it and says so, so uptime is read per real span.
    overrun = run(
        "warcraftlogs", "report-encounter-aura-summary", found.url, "--ability-id", str(ability_id),
        "--window-start-ms", str(half), "--window-end-ms", str(duration + 60_000),
    )
    query = overrun.payload["query"]
    assert query["end_time"] == found.fight["end_time"], overrun.describe()
    assert (query["window_clamped"], query["effective_window_duration_ms"]) == (True, duration - half), overrun.describe()
    assert any("clamped" in note for note in overrun.data["notes"]), overrun.describe()
    assert overrun.data["aura_summary"] == second_half.data["aura_summary"], overrun.describe()


def test_aura_rows_name_the_holder_and_the_caster_the_buff_events_show(require):
    """An external aura's rows match its own applybuff events: holders are targets, appliers are sources.

    The labels were once swapped: `report-encounter-aura-summary` called every Power Infusion
    recipient its `source`. The aura is discovered from the anchor kill's own buff events, picking
    one applied to someone who never applied it, so swapped labels cannot pass.
    """
    require("warcraftlogs")
    found = anchor()
    page = run("warcraftlogs", "report-events", found.code, "--fight-id", str(found.fight_id), "--data-type", "buffs", "--limit", "10000")
    applies: dict[int, list[dict[str, Any]]] = {}
    for event in page.data["events"] or []:
        if event.get("type") == "applybuff" and isinstance(event.get("abilityGameID"), int):
            applies.setdefault(int(event["abilityGameID"]), []).append(event)
    external = next(
        (
            ability_id for ability_id, events in sorted(applies.items(), key=lambda item: -len(item[1]))
            if {e.get("targetID") for e in events} - {e.get("sourceID") for e in events}
        ),
        None,
    )
    if external is None:
        raise JourneyFailure(f"no aura in the anchor kill was applied to a player who never applied it\n{page.describe()}")

    events = run(
        "warcraftlogs", "report-events", found.code, "--fight-id", str(found.fight_id), "--data-type", "buffs",
        "--ability-id", str(external), "--limit", "10000",
    )
    assert events.data["next_page_timestamp"] is None, events.describe()
    applied = [event for event in events.data["events"] if event.get("type") == "applybuff"]
    casters, holders = {e["sourceID"] for e in applied}, {e["targetID"] for e in applied}

    by_holder = run("warcraftlogs", "report-encounter-aura-summary", found.url, "--ability-id", str(external))
    holder_ids = {row["aura_holder"]["id"] for row in by_holder.data["aura_summary"]["rows"]}
    assert holder_ids and holder_ids <= holders, (holder_ids, holders, by_holder.describe())
    assert holder_ids - casters, f"aura {external}: every holder row is also a caster {holder_ids} vs {casters}"

    by_caster = run("warcraftlogs", "report-encounter-buffs", found.url, "--ability-id", str(external), "--view-by", "target")
    buffs = by_caster.data["buffs"]
    assert buffs["row_actor"] == "applied_by", by_caster.describe()
    caster_ids = {row["applied_by"]["id"] for row in buffs["preview"]}
    assert caster_ids and caster_ids <= casters, (caster_ids, casters, by_caster.describe())
    assert all(row["aura"]["game_id"] == external for row in buffs["preview"]), by_caster.describe()


def test_report_encounter_damage_surfaces_return_typed_and_raw_views(require):
    require("warcraftlogs")
    found = anchor()

    # The fight-scoped raw table is the reference: every encounter-scoped damage view of the same
    # fight must report the same per-actor totals, which a wrong fight or window cannot.
    table = run("warcraftlogs", "report-table", found.code, "--data-type", "damage-done", "--fight-id", str(found.fight_id))
    expected = {row["id"]: row["total"] for row in table.data["table"]["data"]["entries"]}
    assert expected and all(total > 0 for total in expected.values()), table.describe()

    by_source = run("warcraftlogs", "report-encounter-damage-source-summary", found.url)
    source_rows = by_source.data["damage_summary"]["rows"]
    assert {row["source"]["id"]: row["reported_total"] for row in source_rows} == expected, by_source.describe()
    assert all(row["source"]["name"] for row in source_rows), by_source.describe()

    breakdown = run("warcraftlogs", "report-encounter-damage-breakdown", found.url, "--view-by", "source")
    assert breakdown.payload["kind"] == "report_encounter_damage_breakdown", breakdown.describe()
    entries = breakdown.data["table"]["data"]["entries"]
    assert {row["id"]: row["total"] for row in entries} == expected, breakdown.describe()
    assert {row["name"] for row in entries} <= {row["name"] for row in found.players}, breakdown.describe()

    # Grouped by target, the rows are what the raid hit, most damage first, so an enemy leads. (Raiders
    # can appear further down: some mechanics make them targets of their own raid's damage.)
    by_target = run("warcraftlogs", "report-encounter-damage-target-summary", found.url)
    target_rows = by_target.data["damage_summary"]["rows"]
    assert target_rows and target_rows[0]["target"]["type"] == "NPC", by_target.describe()
    totals = [row["reported_total"] for row in target_rows]
    assert totals == sorted(totals, reverse=True), by_target.describe()


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
    # --limit cuts the same time-ordered page short; a wider page proves the cap bit. Warcraft Logs
    # ends a page on a whole timestamp, so events sharing the fifth one's timestamp come along too.
    wider = run("warcraftlogs", "report-events", found.code, "--fight-id", fight, "--data-type", "casts", "--limit", "50")
    capped = events.data["events"]
    assert len(wider.data["events"]) > len(capped) >= 5, wider.describe()
    assert capped == wider.data["events"][: len(capped)], events.describe()
    assert {row["timestamp"] for row in capped[4:]} == {capped[4]["timestamp"]}, events.describe()
    assert events.data["next_page_timestamp"] == wider.data["events"][len(capped)]["timestamp"], events.describe()
    assert {row["type"] for row in events.data["events"]} <= {"cast", "begincast"}, events.describe()
    assert {row["fight"] for row in events.data["events"]} == {found.fight_id}, events.describe()
    # The next page, fetched as the note says (--start-time alone), continues the same stream.
    next_page = run(
        "warcraftlogs", "report-events", found.code, "--fight-id", fight, "--data-type", "casts", "--limit", "5",
        "--start-time", str(int(events.data["next_page_timestamp"])),
    )
    assert next_page.payload["query"]["end_time"] == found.fight["end_time"], next_page.describe()
    assert next_page.data["events"][0] == wider.data["events"][len(capped)], next_page.describe()
    # A report URL's #fight=N scopes the slice like --fight-id.
    by_url = run("warcraftlogs", "report-events", found.url, "--data-type", "casts", "--limit", "5")
    assert by_url.data["events"] == capped, by_url.describe()

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
    # The report's actor list covers every fight in it, so this fight's roster has to sit inside it.
    assert roster <= {row["name"] for row in actors}, master.describe()

    details = run("warcraftlogs", "report-player-details", found.code, "--fight-id", fight)
    detail_roles = details.data["player_details"]["roles"]
    detail_names = {row["name"] for rows in detail_roles.values() for row in rows}
    # Cross-checked against the damage graph above, which names its series independently.
    assert detail_names == {row["name"] for row in series} - {"Total"}, details.describe()
    assert details.data["player_details"]["counts"]["total"] == len(detail_names), details.describe()

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
    ranked = rankings.data["rankings"]
    assert ranked["count"] == len(ranked["rows"]) == 1, rankings.describe()
    row = ranked["rows"][0]
    # A ranking of the wrong fight is the failure worth catching: it is still a well-formed table.
    assert row["fightID"] == found.fight_id, rankings.describe()
    assert row["encounter"]["id"] == found.fight["encounter_id"], rankings.describe()
    assert row["difficulty"] == found.fight["difficulty"], rankings.describe()
    assert row["kill"] == 1, rankings.describe()
    ranked_names = {
        character["name"] for role in row["roles"].values() for character in role["characters"]
    }
    assert ranked_names, rankings.describe()
    assert ranked_names <= roster, rankings.describe()


def _complete_event_rows(*scope: str) -> list[dict[str, Any]]:
    """Read complete event streams before comparing filters, rejecting stalled pagination."""
    events: list[dict[str, Any]] = []
    start: int | None = None
    for _ in range(20):
        continuation = ("--start-time", str(start)) if start is not None else ()
        page = run("warcraftlogs", *scope, *continuation)
        events.extend(page.data["events"])
        following = page.data["next_page_timestamp"]
        if following is None:
            return events
        assert isinstance(following, (int, float)), page.describe()
        assert start is None or following > start, f"event pagination stalled: {page.describe()}"
        start = int(following)
    raise JourneyFailure("Event discovery exceeded 20 pages; no complete baseline is available for the filter check")


def test_buff_event_target_filter_keeps_exactly_the_selected_caster(require):
    require("warcraftlogs")
    found = anchor()
    # Buffs views scope target-id to the caster, which raw events identify with sourceID.
    scope = ("report-events", found.code, "--fight-id", str(found.fight_id), "--data-type", "buffs", "--limit", "10000")
    events = _complete_event_rows(*scope)
    counts = Counter(event.get("sourceID") for event in events)
    caster_id = next((actor for actor, count in counts.most_common() if isinstance(actor, int) and 0 < count < len(events)), None)
    assert caster_id is not None, "the baseline needs events for more than one caster to prove filtering"
    expected = [event for event in events if event.get("sourceID") == caster_id]
    filtered = _complete_event_rows(*scope, "--target-id", str(caster_id))
    assert filtered == expected
    assert 0 < len(expected) < len(events)


def test_event_kill_type_excludes_a_kill_from_wipe_only_reads(require):
    require("warcraftlogs")
    found = anchor()
    scope = ("report-events", found.code, "--fight-id", str(found.fight_id), "--data-type", "casts", "--limit", "10000")
    baseline = _complete_event_rows(*scope)
    assert baseline
    kills = _complete_event_rows(*scope, "--kill-type", "Kills")
    wipes = _complete_event_rows(*scope, "--kill-type", "Wipes")
    assert kills == baseline
    assert wipes == []


def test_event_hostility_filter_separates_enemy_and_friendly_cast_sources(require):
    require("warcraftlogs")
    found = anchor()
    scope = ("report-events", found.code, "--fight-id", str(found.fight_id), "--data-type", "casts", "--limit", "10000")
    friendly = _complete_event_rows(*scope, "--hostility-type", "Friendlies")
    enemy = _complete_event_rows(*scope, "--hostility-type", "Enemies")
    for result in (friendly, enemy):
        assert result
    friendly_sources = {event["sourceID"] for event in friendly if isinstance(event.get("sourceID"), int)}
    enemy_sources = {event["sourceID"] for event in enemy if isinstance(event.get("sourceID"), int)}
    assert friendly_sources and enemy_sources
    assert friendly_sources.isdisjoint(enemy_sources)
    roster = {player["id"] for player in found.players}
    assert friendly_sources & roster
    assert not enemy_sources & roster


def test_healers_are_ranked_on_healing_unless_a_metric_is_named(require):
    """In a raid Warcraft Logs ranks every role on dps when no metric is sent, so a healer's default parse was a damage parse."""
    require("warcraftlogs")
    found = anchor()
    difficulty = {1: "lfr", 3: "normal", 4: "heroic", 5: "mythic"}.get(found.fight["difficulty"], str(found.fight["difficulty"]))
    scope = (
        "encounter-rankings", "--zone-id", str(found.zone["id"]), "--boss-id", str(found.fight["encounter_id"]),
        "--difficulty", difficulty, "--spec-name", "holy priest", "--limit", "3",
    )
    healing = run("warcraftlogs", *scope)
    damage = run("warcraftlogs", *scope, "--metric", "dps")
    assert (healing.payload["query"]["metric"], damage.payload["query"]["metric"]) == ("hps", "dps"), healing.describe()
    assert healing.payload["query"]["difficulty"] == found.fight["difficulty"], healing.describe()
    top_amount = {name: result.data["rankings"]["rows"][0]["amount"] for name, result in (("hps", healing), ("dps", damage))}
    assert top_amount["hps"] > top_amount["dps"], top_amount

    def healer_amounts(*extra: str) -> list[float]:
        result = run("warcraftlogs", "report-rankings", found.code, "--fight-id", str(found.fight_id), *extra)
        return [character["amount"] for character in result.data["rankings"]["rows"][0]["roles"]["healers"]["characters"]]

    default_healers, damage_healers = healer_amounts(), healer_amounts("--player-metric", "dps")
    assert default_healers and len(default_healers) == len(damage_healers), (default_healers, damage_healers)
    assert max(default_healers) > max(damage_healers), (default_healers, damage_healers)


def test_mythic_plus_rankings_keep_warcraft_logs_score_order_by_default(require):
    """In a Mythic+ zone Warcraft Logs ranks on score when no metric is sent; the healer default must not replace it."""
    require("warcraftlogs")
    zones = [
        zone
        for zone in _rows(run("warcraftlogs", "zones"), "zones")
        if isinstance(zone, dict) and not zone.get("frozen") and zone.get("encounters")
        and 10 in {difficulty.get("id") for difficulty in zone.get("difficulties") or []}
    ]
    if not zones:
        raise JourneyFailure("no unfrozen Mythic+ zone (difficulty 10) in `warcraftlogs zones`")
    zone = max(zones, key=lambda row: row["id"])
    encounter_id = zone["encounters"][0]["id"]
    ranked = run(
        "warcraftlogs", "encounter-rankings", "--zone-id", str(zone["id"]), "--boss-id", str(encounter_id),
        "--spec-name", "holy priest", "--limit", "3",
    )
    assert ranked.payload["query"]["metric"] == "playerscore", ranked.describe()
    upstream = run(
        "warcraftlogs", "graphql", "--query",
        f'query {{ worldData {{ encounter(id: {encounter_id}) {{ characterRankings(className: "Priest", specName: "Holy") }} }} }}',
    )
    upstream_rows = upstream.data["worldData"]["encounter"]["characterRankings"]["rankings"][:3]
    names = [row["name"] for row in ranked.data["rankings"]["rows"]]
    assert names and names == [row["name"] for row in upstream_rows], (names, upstream_rows)


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


def test_source_id_keeps_only_that_actors_events(require):
    """``--source-id`` narrows events server-side to one actor; ``cooldown-packet`` trusts it to attribute casts.

    The actor is one the unfiltered page actually shows casting, and ``sourceID`` is Warcraft Logs'
    own event field, so a filter that stopped reaching the API returns the other raiders' casts too.
    """
    require("warcraftlogs")
    found = anchor()
    slice_args = (found.code, "--fight-id", str(found.fight_id), "--data-type", "casts", "--limit", "200")

    unfiltered = run("warcraftlogs", "report-events", *slice_args)
    roster_ids = {int(row["id"]) for row in found.players}
    casters = Counter(row["sourceID"] for row in unfiltered.data["events"] if row.get("sourceID") in roster_ids)
    assert len(casters) >= 2, unfiltered.describe()
    actor = casters.most_common(1)[0][0]

    filtered = run("warcraftlogs", "report-events", *slice_args, "--source-id", str(actor))
    events = filtered.data["events"]
    assert events, filtered.describe()
    assert {row["sourceID"] for row in events} == {actor}, filtered.describe()


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
    # The note has to name the count the aggregates below it were actually built from, and that
    # count is held to the same 25-event page read through the raw events surface.
    counted = f"{summary['cast_count']} casts in the first {summary['event_count']} events"
    assert any(counted in note for note in capped.data["notes"]), capped.describe()
    page = run("warcraftlogs", "report-events", found.code, "--fight-id", str(found.fight_id), "--data-type", "casts", "--limit", "25")
    page_events = page.data["events"]
    assert summary["event_count"] == len(page_events), page.describe()
    assert summary["cast_count"] == sum(1 for event in page_events if event["type"] == "cast"), page.describe()

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
    data = assert_sampling_metadata(boss_kills, expect_rows=True)
    kills = data["kills"]
    assert any((found.code, found.fight_id) in _pull_keys(row) for row in kills), boss_kills.describe()
    assert all(row["fight"]["kill"] is True for row in kills), boss_kills.describe()
    assert all(row["duration_seconds"] > 0 for row in kills), boss_kills.describe()

    top_kills = run("warcraftlogs", "top-kills", *cohort_args(), "--top", "3")
    assert top_kills.payload["kind"] == "top_kills", top_kills.describe()
    top = assert_sampling_metadata(top_kills, expect_rows=True)["kills"]
    assert {_kill_key(row) for row in top} == {_kill_key(row) for row in kills}, top_kills.describe()


def test_sampled_kills_on_the_current_raid_include_reports_still_being_logged(require):
    """An unscoped cohort on the current raid finds kills; raid nights still being logged are most of it.

    Sampling once dropped every report that was still receiving uploads, which on the current tier
    is nearly all of them, and answered ``ok: true`` with no kills. Heroic of the first boss is
    killed in far more than one of the newest reports. Each kill is held to ``report-fights``.
    """
    require("warcraftlogs")
    zone = current_raid_zone()
    boss = zone["encounters"][0]
    result = run(
        "warcraftlogs", "boss-kills", "--zone-id", str(zone["id"]), "--boss-id", str(boss["id"]),
        "--difficulty", str(HEROIC_DIFFICULTY_ID), "--top", "3",
    )
    sample = result.data["sample"]
    kills = result.data["kills"]
    assert kills and sample["filtered_kill_count"] >= len(kills), result.describe()
    # The cohort is marked live exactly when one of its kills came from a report still being logged.
    assert result.data["cache_provenance"]["live"] is True, result.describe()
    assert result.data["cache_provenance"]["source"] == "sampled_reports", result.describe()
    for row in kills:
        fights = run("warcraftlogs", "report-fights", row["report"]["code"]).data["fights"]
        fight = next(fight for fight in fights if fight["id"] == row["fight"]["id"])
        assert (fight["encounter_id"], fight["difficulty"], fight["kill"]) == (boss["id"], HEROIC_DIFFICULTY_ID, True), fight


def _kill_key(row: dict[str, Any]) -> tuple[str, int]:
    return str(row["report"]["code"]), int(row["fight"]["id"])


def _pull_keys(row: dict[str, Any]) -> set[tuple[str, int]]:
    """Every report that logged this kill: the row's own, plus the ones collapsed into it.

    Another raider's earlier-starting log of the anchor pull represents it after the collapse, so
    "the anchor kill is in the cohort" means it is one of these.
    """
    return {_kill_key(row), *((str(entry["report_code"]), int(entry["fight_id"])) for entry in row["duplicate_reports"])}


def test_top_kills_orders_a_multi_kill_cohort_by_duration(require):
    """``top-kills`` claims "fastest first"; over one row that claim cannot be wrong.

    The wide cohort holds several kills of different lengths, and the expected order is an
    independently sorted copy of the durations ``boss-kills`` reported for the same scope — so a
    product that returned them in scan order, or reversed, fails here.
    """
    require("warcraftlogs")
    cohort = wide_cohort()
    assert len({row["duration_ms"] for row in cohort.kills}) >= 2, cohort.kills

    result = run("warcraftlogs", "--endpoint", "user", "top-kills", *cohort.args, "--top", WIDE_COHORT_TOP)
    ranked = assert_sampling_metadata(result, expect_rows=True, wide=cohort)["kills"]
    assert {_kill_key(row) for row in ranked} == {_kill_key(row) for row in cohort.kills}, result.describe()
    expected = sorted(cohort.kills, key=lambda row: (row["duration_ms"], _kill_key(row)))
    assert [_kill_key(row) for row in ranked] == [_kill_key(row) for row in expected], result.describe()


def test_sampled_kills_collapse_one_pull_logged_in_two_reports(require):
    """Two raiders logging the same pull is one kill, cited from both reports and counted once.

    SAFE_ANALYTICS_RULES.md: a dedupe is never silent. The collapse is verified against the source
    reports themselves — the folded fight has to be the same encounter at the same wall-clock time —
    so this fails both when the product stops collapsing and when it collapses two different pulls.
    """
    require("warcraftlogs")
    cohort = wide_cohort()
    removed = cohort.sample["duplicates_removed"]
    assert removed >= 1, cohort.sample

    folded = [(row, entry) for row in cohort.kills for entry in row["duplicate_reports"]]
    assert len(folded) == removed, f"{removed} collapsed, {len(folded)} named on rows: {cohort.kills}"
    assert cohort.sample["matched_boss_kill_count"] == len(cohort.kills), cohort.sample

    citations = run("warcraftlogs", "--endpoint", "user", "boss-kills", *cohort.args, "--top", WIDE_COHORT_TOP)
    cited = {
        (row["report_code"], row["fight_id"]) for row in citations.data["citations"]["sample_reports"]
    }
    assert any("same pull logged in more than one report" in note for note in citations.data["notes"]), citations.describe()

    for kept, entry in folded:
        key = (entry["report_code"], entry["fight_id"])
        assert key in cited, f"collapsed report {key} is not citable: {sorted(cited)}"
        assert key != _kill_key(kept), f"a kill was folded into itself: {kept}"
        _assert_same_pull(kept, entry)


def _assert_same_pull(kept: dict[str, Any], folded: dict[str, Any]) -> None:
    """Re-derive both fights' wall-clock windows from their own reports and require them to agree."""
    kept_window = _absolute_fight_window(str(kept["report"]["code"]), int(kept["fight"]["id"]), endpoint="user")
    folded_window = _absolute_fight_window(str(folded["report_code"]), int(folded["fight_id"]), endpoint="user")
    assert kept_window.encounter_id == folded_window.encounter_id, (kept_window, folded_window)
    assert kept_window.difficulty == folded_window.difficulty, (kept_window, folded_window)
    drift = max(
        abs(kept_window.start_ms - folded_window.start_ms), abs(kept_window.end_ms - folded_window.end_ms)
    )
    assert drift <= ROSTER_MATCH_TOLERANCE_MS, (
        f"collapsed two pulls {drift} ms apart: {kept_window} vs {folded_window}"
    )
    # Past the guild timing bound only the same players make it one pull.
    if drift > DUPLICATE_PULL_TOLERANCE_MS:
        def players(code: str, fight_id: int) -> set[str]:
            return {f"{row['name']}-{row['server']}" for row in _fight_roster(code, fight_id, endpoint="user")}

        kept_roster = players(str(kept["report"]["code"]), int(kept["fight"]["id"]))
        assert kept_roster == players(str(folded["report_code"]), int(folded["fight_id"])), (
            f"collapsed two pulls {drift} ms apart with different rosters: {kept_window} vs {folded_window}"
        )


def test_spec_kill_samples_and_boss_spec_usage_describe_the_cohort(require):
    require("warcraftlogs")
    spec = anchor_spec_slug()

    samples = run("warcraftlogs", "spec-kill-samples", *cohort_args(), "--spec-name", spec, "--top", "3")
    assert samples.payload["kind"] == "spec_filtered_kill_samples", samples.describe()
    data = assert_sampling_metadata(samples, expect_rows=True)
    assert data["sample"]["spec_name"] == spec, samples.describe()
    assert data["cohort"] == "spec_filtered_participant_kill_cohort", samples.describe()
    assert data["sample"]["returned_kill_count"] == len(data["kills"]), samples.describe()
    assert data["sample"]["sample_size"] >= data["sample"]["returned_kill_count"], samples.describe()
    # The rows are a sample of a cohort, not a leaderboard, and must keep saying so.
    assert any("not a spec ranking leaderboard" in note for note in data["notes"]), samples.describe()

    # Every spec in the cohort, so the anchor spec cannot fall outside a truncated top list.
    usage = run("warcraftlogs", "boss-spec-usage", *cohort_args(), "--top", "40")
    assert usage.payload["kind"] == "boss_spec_usage", usage.describe()
    rows = assert_sampling_metadata(usage, expect_rows=True)["spec_usage"]
    assert rows, usage.describe()
    assert all(row["spec_name"] and row["appearance_count"] > 0 for row in rows), usage.describe()
    assert spec in {str(row["spec_name"]).lower() for row in rows}, usage.describe()
    keys = [(row["class_name"], row["spec_name"], row["role"]) for row in rows]
    assert len(keys) == len(set(keys)), usage.describe()


@lru_cache(maxsize=1)
def _shared_spec_name_cohort() -> tuple[WideCohort, set[tuple[str, str]]]:
    """A guild cohort plus the ``(class, spec)`` roster of a kill in it that fields one spec name under two classes.

    Only such a roster can tell a class-blind count apart. A tier that opened days ago may hold a
    kill or two without one, so the walk continues through the tiers before it.
    """
    scanned: list[str] = []
    for zone in raid_zones()[:WIDE_COHORT_ZONES]:
        for cohort in _guild_boss_cohorts(zone, scanned):
            for kill in cohort.kills:
                code, fight_id = _kill_key(kill)
                roster = _fight_roster(code, fight_id, endpoint="user")
                fielded = {(str(player["type"]), str(entry["spec"])) for player in roster for entry in player.get("specs") or []}
                if any(count > 1 for count in Counter(spec for _, spec in fielded).values()):
                    return cohort, fielded
                scanned.append(f"  {code}#{fight_id}: no spec name under two classes")
    raise JourneyFailure(f"no guild kill in the {WIDE_COHORT_ZONES} newest raid zones fields one spec name under two classes: {scanned}")


def test_boss_spec_usage_counts_a_spec_with_its_class(require):
    """Frost Mage and Frost Death Knight are two rows, never one "Frost".

    Only a roster that fields one spec name under two classes can tell a class-blind count apart, so
    the check reads such a kill's roster from its own report and requires every class and spec on it
    as a row of the cohort that holds it.
    """
    require("warcraftlogs")
    cohort, fielded = _shared_spec_name_cohort()
    usage = run("warcraftlogs", "--endpoint", "user", "boss-spec-usage", *cohort.args, "--top", "40")
    rows = assert_sampling_metadata(usage, expect_rows=True, wide=cohort)["spec_usage"]
    assert fielded <= {(row["class_name"], row["spec_name"]) for row in rows}, usage.describe()


def _anchor_pull_row(rows: list[dict[str, Any]], result: Result) -> dict[str, Any]:
    """The sampled row that represents the anchor pull (its own report, or the one it was folded into)."""
    found = anchor()
    row = next((row for row in rows if (found.code, found.fight_id) in _pull_keys(row)), None)
    if row is None:
        raise JourneyFailure(f"the anchor kill {found.code}#{found.fight_id} is not a sampled row\n{result.describe()}")
    return row


def _whole_cohort_kills() -> Result:
    """``boss-kills`` over the anchor cohort, required to be complete so it can serve as the reference."""
    result = run("warcraftlogs", "boss-kills", *cohort_args(), "--top", "10")
    assert result.data["sample"]["truncated"] is False, result.describe()
    return result


def test_comp_samples_count_the_anchor_roster(require):
    """comp-samples covers the kills ``boss-kills`` returns, and the anchor pull's classes are its roster.

    The roster comes from ``report-encounter-players``, so a composition built from the wrong fight,
    or one that drops a role, fails here.
    """
    require("warcraftlogs")
    found = anchor()
    kills = _whole_cohort_kills()
    durations = [row["duration_seconds"] for row in kills.data["kills"]]

    comps = run("warcraftlogs", "comp-samples", *cohort_args(), "--top", "10")
    assert comps.payload["kind"] == "comp_samples", comps.describe()
    data = assert_sampling_metadata(comps, expect_rows=True)
    assert {_kill_key(row) for row in data["kills"]} == {_kill_key(row) for row in kills.data["kills"]}, comps.describe()
    roster_classes = Counter(str(player["type"]) for player in found.players)
    composition = _anchor_pull_row(data["kills"], comps)["composition"]
    assert composition["player_count"] == len(found.players), comps.describe()
    assert {row["class_name"]: row["count"] for row in composition["class_counts"]} == roster_classes, comps.describe()
    # Presence counts kills and appearances count players, so the anchor pull alone accounts for its roster.
    presence = {row["class_name"]: row for row in data["class_presence"]}
    for class_name, count in roster_classes.items():
        assert presence[class_name]["appearance_count"] >= count, comps.describe()
        assert 1 <= presence[class_name]["kill_presence_count"] <= len(durations), comps.describe()
    signatures = {row["class_signature"]: row["kill_count"] for row in data["composition_signatures"]}
    assert signatures[composition["class_signature"]] >= 1, comps.describe()
    assert sum(signatures.values()) == len(durations), comps.describe()


def test_kill_time_distribution_buckets_the_cohort_durations(require):
    """The histogram describes the durations ``boss-kills`` reports, anchored to ``report-fights``."""
    require("warcraftlogs")
    found = anchor()
    kills = _whole_cohort_kills()
    durations = [row["duration_seconds"] for row in kills.data["kills"]]

    distribution = run("warcraftlogs", "kill-time-distribution", *cohort_args(), "--bucket-seconds", "60")
    assert distribution.payload["kind"] == "kill_time_distribution", distribution.describe()
    histogram = assert_sampling_metadata(distribution, expect_rows=True)["distribution"]
    assert histogram["bucket_seconds"] == 60, distribution.describe()
    statistics = histogram["statistics"]
    assert (statistics["min"], statistics["max"]) == (min(durations), max(durations)), distribution.describe()
    assert sum(bucket["count"] for bucket in histogram["rows"]) == len(durations), distribution.describe()
    # The anchor pull's length, as report-fights measured it, sits in a populated one-minute bucket.
    anchor_row = _anchor_pull_row(kills.data["kills"], kills)
    anchor_ms = int(found.fight["end_time"]) - int(found.fight["start_time"])
    assert abs(anchor_row["duration_ms"] - anchor_ms) <= DUPLICATE_PULL_TOLERANCE_MS, kills.describe()
    bucket = next(row for row in histogram["rows"] if row["start_seconds"] <= anchor_row["duration_seconds"] < row["end_seconds"])
    assert bucket["count"] >= 1 and bucket["end_seconds"] - bucket["start_seconds"] == 60, distribution.describe()


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
        "10",
        "--event-limit",
        "5000",
    )
    assert result.payload["kind"] == "ability_usage_summary", result.describe()
    data = assert_sampling_metadata(result, expect_rows=True)
    assert data["ability"]["game_id"] == ability_id, result.describe()
    usage = data["usage"]
    assert usage["total_casts_is_lower_bound"] is False, result.describe()
    assert data["sample"]["kills_with_truncated_events_count"] == 0, result.describe()
    assert data["sample"]["preview_truncated"] is False, result.describe()
    assert usage["total_casts"] == sum(row["casts"]["count"] for row in data["kills_preview"]), result.describe()

    # The anchor pull's casts, counted per caster, against that fight's own event log filtered server-side.
    # A cast is a `cast` event; the ability was picked for having cast-bar or empower events as well.
    row = _anchor_pull_row(data["kills_preview"], result)
    code, fight_id = _kill_key(row)
    events = run(
        "warcraftlogs", "report-events", code, "--fight-id", str(fight_id), "--data-type", "casts",
        "--limit", "10000", "--filter-expression", f"ability.id = {ability_id}",
    )
    assert events.data["next_page_timestamp"] is None, events.describe()
    casters = Counter(event["sourceID"] for event in events.data["events"] if event["type"] == "cast")
    assert 0 < casters.total() < len(events.data["events"]), events.describe()
    assert row["casts"]["count"] == casters.total(), result.describe()
    assert {source["source"]["id"]: source["count"] for source in row["casts"]["sources"]} == casters, result.describe()


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

    Both bounds are derived from the unfiltered cohort and both are proved by what they *remove*: a
    floor above the slowest kill and a ceiling below the fastest one each have to empty the result.
    A bound chosen to keep everything cannot tell a working filter from an ignored one.
    """
    require("warcraftlogs")
    unfiltered = run("warcraftlogs", "boss-kills", *cohort_args(), "--top", "10")
    durations = _kill_durations(unfiltered)
    assert durations, unfiltered.describe()
    # `duration_seconds` is rounded for display, so the bounds are widened by a second either way.
    ceiling = max(durations) + 1
    floor = min(durations) - 1
    codes = {(row["report"]["code"], row["fight"]["id"]) for row in unfiltered.data["kills"]}

    kept = run("warcraftlogs", "boss-kills", *cohort_args(), "--top", "10", "--kill-time-max", str(ceiling))
    assert kept.data["sample_scope"]["filters"]["kill_time_max"] == ceiling, kept.describe()
    assert {(row["report"]["code"], row["fight"]["id"]) for row in kept.data["kills"]} == codes, kept.describe()
    assert all(duration <= ceiling for duration in _kill_durations(kept)), kept.describe()

    for bound, value in (("--kill-time-min", ceiling + 1), ("--kill-time-max", floor)):
        pruned = run("warcraftlogs", "boss-kills", *cohort_args(), "--top", "10", bound, str(value))
        assert pruned.data["kills"] == [], pruned.describe()
        assert pruned.data["sample"]["filtered_kill_count"] == 0, pruned.describe()
        # The cohort was still scanned; only the filter emptied it, and the scan still says so.
        assert pruned.data["sample"]["scanned_fight_count"] > 0, pruned.describe()
        assert pruned.data["sample"]["source_report_count"] == unfiltered.data["sample"]["source_report_count"], pruned.describe()
        assert_sampling_metadata(pruned, expect_rows=False)


def _roster_specs(players: tuple[dict[str, Any], ...]) -> set[str]:
    return {
        str(spec["spec"]).lower()
        for player in players
        for spec in player.get("specs") or []
        if isinstance(spec.get("spec"), str)
    }


def test_boss_kills_spec_filter_narrows_the_cohort_to_that_spec(require):
    """``--spec-name`` on boss-kills keeps only the kills whose roster contains that spec.

    The positive half alone cannot fail while the flag is ignored, because every kill in the anchor
    cohort is the anchor kill and its roster has the spec. The discriminating half asks for a spec
    that the pinned guild fielded and no kill in the unfiltered cohort did: the cohort must come back
    empty. The positive half also checks the rows' own ``matching_players``, which an ignored filter
    would leave blank.
    """
    require("warcraftlogs")
    spec = anchor_spec_slug()
    unfiltered = _whole_cohort_kills()
    all_kills = {_kill_key(row) for row in unfiltered.data["kills"]}

    filtered = run("warcraftlogs", "boss-kills", *cohort_args(), "--top", "10", "--spec-name", spec)
    data = assert_sampling_metadata(filtered, expect_rows=True)
    assert data["sample_scope"]["filters"]["spec_name"] == spec, filtered.describe()
    kept = {(row["report"]["code"], row["fight"]["id"]) for row in data["kills"]}
    assert kept <= all_kills, filtered.describe()
    # The anchor kill's own roster contains the spec it was discovered from.
    assert any((anchor().code, anchor().fight_id) in _pull_keys(row) for row in data["kills"]), filtered.describe()
    assert any("spec" in note.lower() for note in data["notes"]), filtered.describe()
    for row in data["kills"]:
        matched = row["matching_players"]
        assert matched, f"a kept kill names no player of {spec!r}\n{filtered.describe()}"
        assert {
            str(entry["spec"]).lower() for player in matched for entry in player["matching_specs"]
        } == {spec}, filtered.describe()

    # A spec is provably absent only if no kill in the unfiltered cohort fielded it.
    fielded = set().union(*(_roster_specs(_fight_roster(code, fight_id)) for code, fight_id in all_kills))
    absent = sorted(_roster_specs(guild_anchor().players) - fielded)
    assert absent, (
        "every spec the pinned guild fielded is also on a kill in the cohort, so no spec is provably "
        f"absent from it: {sorted(fielded)}"
    )
    empty = run("warcraftlogs", "boss-kills", *cohort_args(), "--top", "10", "--spec-name", absent[0])
    assert empty.data["kills"] == [], f"no kill in the cohort fielded {absent[0]!r}\n{empty.describe()}"
    assert empty.data["sample"]["scanned_fight_count"] > 0, empty.describe()


# --------------------------------------------------------------------------------------------
# Raw GraphQL and global flags
# --------------------------------------------------------------------------------------------


def test_graphql_introspect_and_a_typed_query_reach_the_api(require):
    """Raw GraphQL reaches the API, and ``--introspect`` returns the schema.

    Warcraft Logs refused any ``__schema.types`` selection with "Internal server error" from
    2026-09-24 until 2026-09-29; if that returns, this fails on the introspection call.
    """
    require("warcraftlogs")
    found = anchor()

    introspect = run("warcraftlogs", "graphql", "--endpoint", "client", "--introspect")
    # graphql's data is the GraphQL result itself, so introspection sits under its own __schema.
    schema = introspect.data["__schema"]
    assert schema["queryType"]["name"] == "Query", introspect.describe()
    assert len(schema["types"]) > 50, introspect.describe()

    query = run(
        "warcraftlogs",
        "graphql",
        "--endpoint", "client",
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
    title = compact.data["report"]["title"]
    assert len(title) <= 43, compact.describe()


# --------------------------------------------------------------------------------------------
# Error journeys
# --------------------------------------------------------------------------------------------


def test_missing_credentials_exit_3_with_a_recovery_hint(require, tmp_path):
    """No client credentials anywhere is an auth answer (exit 3) that names the variables to set.

    The config and state roots point at empty directories and the cache is off, so neither the real
    credentials nor a saved token or cached response can answer instead. The empty cwd also keeps
    the checkout's .env.local out of credential discovery.
    """
    require("warcraftlogs")
    blank = {
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "WARCRAFTLOGS_CLIENT_ID": "",
        "WARCRAFTLOGS_CLIENT_SECRET": "",
        **no_cache_env(),
    }
    result = run("warcraftlogs", "zones", expect=EXIT_AUTH, env=blank, cwd=tmp_path)
    assert result.error_code in {"missing_public_auth", "missing_client_credentials"}, result.describe()
    assert "WARCRAFTLOGS_CLIENT_ID" in result.payload["error"]["message"], result.describe()


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


def test_a_fight_scope_that_matches_nothing_is_not_found_not_an_empty_slice(require):
    """An unknown fight id must not come back as a well-formed but empty table, graph, or roster.

    Warcraft Logs answers a fight id the report does not have with an empty slice, which reads as
    "nobody did anything". Every fight-scoped report surface has to say the fight is missing instead,
    and name it, even when the other listed fight id is real.
    """
    require("warcraftlogs")
    found = anchor()
    scope = ("--fight-id", str(found.fight_id), "--fight-id", "999999")
    for command, extra in (
        ("report-player-details", ()),
        ("report-events", ("--data-type", "casts")),
        ("report-table", ("--data-type", "damage-done")),
        ("report-graph", ("--data-type", "damage-done")),
        ("report-rankings", ()),
    ):
        result = run("warcraftlogs", command, found.code, *scope, *extra, expect=EXIT_NOT_FOUND, error_code="not_found")
        assert result.payload["error"]["details"]["missing_fight_ids"] == [999999], result.describe()
        assert found.code in result.payload["error"]["message"], result.describe()


def test_input_warcraft_logs_would_answer_unfiltered_is_a_usage_error(require):
    """Warcraft Logs ignores an unknown class, a partial guild scope, or a window past the fight.

    Each used to come back ``ok: true`` with unfiltered rows or an empty slice; a schema-rejected enum
    value came back as ``not_found``.
    """
    require("warcraftlogs")
    found = anchor()
    zone, boss = str(found.zone["id"]), str(found.fight["encounter_id"])
    for args in (
        ("encounter-rankings", "--zone-id", zone, "--boss-id", boss, "--class-name", "nopeclass"),
        ("reports", "--guild-name", pins.GUILD_NAME, "--limit", "1"),
        ("boss-kills", "--zone-id", zone, "--boss-id", boss, "--spec-name", "frsot"),
        (
            "report-events", found.code, "--fight-id", str(found.fight_id), "--data-type", "casts",
            "--start-time", str(int(found.fight["end_time"]) + 60_000), "--end-time", str(int(found.fight["end_time"]) + 120_000),
        ),
    ):
        result = run("warcraftlogs", *args, expect=EXIT_USAGE, error_code="invalid_query")
        assert result.stdout == "", result.describe()
    # An enum value is checked before any request, and the error lists the values Warcraft Logs takes.
    bad_enum = run(
        "warcraftlogs", "report-events", found.code, "--fight-id", str(found.fight_id), "--data-type", "nope",
        expect=EXIT_USAGE, error_code="invalid_argument",
    )
    assert "DamageDone" in bad_enum.payload["error"]["message"], bad_enum.describe()

    run("warcraftlogs", "guild-reports", *GUILD[:2], "zzqqnopeguild", expect=EXIT_NOT_FOUND, error_code="not_found")


def test_a_realm_in_any_spelling_reaches_its_warcraft_logs_slug(require):
    require("warcraftlogs")
    # Warcraft Logs' slug runs this realm's words together; the hyphenated spelling is the one users type.
    server = run("warcraftlogs", "server", "us", "Azjol-Nerub")
    assert server.data["server"]["slug"] == "azjolnerub", server.describe()


def test_a_russian_realm_name_reaches_its_warcraft_logs_slug(require):
    """Warcraft Logs slugs some Russian realms in English and keeps others in Cyrillic.

    ``Гордунни`` resolves under its own spelling; ``Ревущий фьорд`` is ``howling-fjord`` and
    ``Ясеневый лес`` is ``ashenvale``, found only through the EU server list. Each record names the
    realm the user typed.
    """
    require("warcraftlogs")
    for realm, slug in (("Ревущий фьорд", "howling-fjord"), ("Ясеневый лес", "ashenvale"), ("Гордунни", None)):
        server = run("warcraftlogs", "server", "eu", realm)
        assert server.data["server"]["name"] == realm, server.describe()
        if slug is not None:
            assert server.data["server"]["slug"] == slug, server.describe()


def test_an_oceanic_alias_reads_the_us_region_and_an_unknown_region_is_a_usage_error(require):
    """Oceanic realms are in Warcraft Logs' US region; ``oce`` used to be a false not_found."""
    require("warcraftlogs")
    server = run("warcraftlogs", "server", "oce", "frostmourne")
    assert server.data["server"]["region"]["slug"].lower() == "us", server.describe()
    assert server.data["server"]["subregion"]["name"] == "Oceanic", server.describe()
    offline = {**dead_proxy_env(), **no_cache_env()}
    run("warcraftlogs", "server", "xx", "illidan", expect=EXIT_USAGE, error_code="invalid_query", env=offline)


def test_zones_rejects_an_expansion_id_warcraft_logs_does_not_have(require):
    """Raider.IO's Midnight is 11; Warcraft Logs' is not, and an empty zone list used to say otherwise."""
    require("warcraftlogs")
    ids = {row["id"] for row in _rows(run("warcraftlogs", "expansions"), "expansions")}
    unknown = max(ids) + 50
    result = run("warcraftlogs", "zones", "--expansion-id", str(unknown), expect=EXIT_USAGE, error_code="invalid_query")
    assert "warcraftlogs expansions" in result.payload["error"]["message"], result.describe()


def test_a_dead_proxy_is_an_exit_5_envelope_on_stderr(require):
    require("warcraftlogs")
    # A target no other journey touches, so the session cache cannot mask the transport failure.
    result = run(
        "warcraftlogs",
        "server",
        "us",
        "proudmoore",
        expect=EXIT_NETWORK,
        error_code="network_error",
        env={**dead_proxy_env(), **no_cache_env()},
    )
    assert result.stdout == ""

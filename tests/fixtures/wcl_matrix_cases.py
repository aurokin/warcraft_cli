"""Warcraft Logs live-matrix case definitions."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from tests.fixtures.live_matrix import (
    CHARACTER_NAME,
    ENCOUNTER_PLEXUS,
    GUILD_NAME,
    GUILD_REALM,
    GUILD_REGION,
    PRIVATE_REPORT_CODE,
    PUBLIC_DIFFICULTY,
    PUBLIC_REPORT_CODE,
    SAMPLED_ANALYTICS_TAIL,
    ZONE_ID,
)


class AuthRequirement(Enum):
    CLIENT = "client"
    USER = "user"
    PRIVATE = "private"


@dataclass(frozen=True)
class LiveMatrixContext:
    public_report_url: str
    public_fight_id: int
    private_report_url: str | None
    private_fight_id: int | None
    aura_ability_id: int | None = None
    player_actor_id: int | None = None


@dataclass(frozen=True)
class MatrixCase:
    case_id: str
    command: str
    canonical_key: str
    auth: AuthRequirement
    build_args: Callable[[LiveMatrixContext], list[str]]
    nonempty_leaf: str | None = None


def _url(code: str, fight_id: int) -> str:
    return f"https://www.warcraftlogs.com/reports/{code}#fight={fight_id}"


def _static(args: list[str]) -> Callable[[LiveMatrixContext], list[str]]:
    return lambda _ctx: args


def _with_url(args: list[str]) -> Callable[[LiveMatrixContext], list[str]]:
    def builder(ctx: LiveMatrixContext) -> list[str]:
        out: list[str] = []
        for part in args:
            if part == "{url}":
                out.append(ctx.public_report_url)
            else:
                out.append(part)
        return out

    return builder


def _with_player_scope(args: list[str]) -> Callable[[LiveMatrixContext], list[str]]:
    def builder(ctx: LiveMatrixContext) -> list[str]:
        if ctx.player_actor_id is None:
            raise RuntimeError("player actor id unavailable for live matrix")
        out: list[str] = []
        for part in args:
            if part == "{url}":
                out.append(ctx.public_report_url)
            elif part == "{actor-id}":
                out.append(str(ctx.player_actor_id))
            else:
                out.append(part)
        return out

    return builder


def _with_aura_scope(args: list[str]) -> Callable[[LiveMatrixContext], list[str]]:
    def builder(ctx: LiveMatrixContext) -> list[str]:
        if ctx.aura_ability_id is None:
            raise RuntimeError("aura ability id unavailable for live matrix")
        out: list[str] = []
        for part in args:
            if part == "{url}":
                out.append(ctx.public_report_url)
            elif part == "{ability-id}":
                out.append(str(ctx.aura_ability_id))
            else:
                out.append(part)
        return out

    return builder


def _with_private_url(args: list[str]) -> Callable[[LiveMatrixContext], list[str]]:
    def builder(ctx: LiveMatrixContext) -> list[str]:
        if ctx.private_report_url is None:
            raise RuntimeError("private report URL unavailable")
        out: list[str] = []
        for part in args:
            if part == "{url}":
                out.append(ctx.private_report_url)
            else:
                out.append(part)
        return out

    return builder


def matrix_cases() -> tuple[MatrixCase, ...]:
    z = str(ZONE_ID)
    boss = str(ENCOUNTER_PLEXUS)
    diff = str(PUBLIC_DIFFICULTY)
    guild = [GUILD_REGION, GUILD_REALM, GUILD_NAME]
    sampled = list(SAMPLED_ANALYTICS_TAIL)

    return (
        MatrixCase("regions", "regions", "regions", AuthRequirement.CLIENT, _static(["regions"]), "regions"),
        MatrixCase("expansions", "expansions", "expansions", AuthRequirement.CLIENT, _static(["expansions"]), "expansions"),
        MatrixCase(
            "server",
            "server",
            "server",
            AuthRequirement.CLIENT,
            _static(["server", GUILD_REGION, GUILD_REALM]),
            "server",
        ),
        MatrixCase("zones", "zones", "zones", AuthRequirement.CLIENT, _static(["zones"]), "zones"),
        MatrixCase("zone", "zone", "zone", AuthRequirement.CLIENT, _static(["zone", z]), "zone"),
        MatrixCase("encounter", "encounter", "encounter", AuthRequirement.CLIENT, _static(["encounter", boss]), "encounter"),
        MatrixCase(
            "encounter-rankings",
            "encounter-rankings",
            "encounter_rankings",
            AuthRequirement.CLIENT,
            _static(["encounter-rankings", boss, "--zone-id", z, "--difficulty", diff, "--size", "5"]),
            "encounter_rankings",
        ),
        MatrixCase("doctor", "doctor", "doctor", AuthRequirement.CLIENT, _static(["doctor"]), "doctor"),
        MatrixCase("rate-limit", "rate-limit", "rate_limit", AuthRequirement.CLIENT, _static(["rate-limit"]), "rate_limit"),
        MatrixCase(
            "search-guild",
            "search",
            "search",
            AuthRequirement.CLIENT,
            _static(["search", GUILD_NAME, "--limit", "3"]),
            "search",
        ),
        MatrixCase(
            "resolve-guild",
            "resolve",
            "resolve",
            AuthRequirement.CLIENT,
            _static(["resolve", f"{GUILD_NAME} {GUILD_REALM}"]),
            "resolve",
        ),
        MatrixCase("guild", "guild", "guild", AuthRequirement.CLIENT, _static(["guild", *guild]), "guild"),
        MatrixCase(
            "guild-members",
            "guild-members",
            "guild_members",
            AuthRequirement.CLIENT,
            _static(["guild-members", *guild, "--limit", "5"]),
            "guild_members",
        ),
        MatrixCase(
            "guild-rankings",
            "guild-rankings",
            "guild_rankings",
            AuthRequirement.CLIENT,
            _static(["guild-rankings", *guild, "--zone-id", z, "--size", "10"]),
            "guild_rankings",
        ),
        MatrixCase(
            "guild-attendance",
            "guild-attendance",
            "guild_attendance",
            AuthRequirement.CLIENT,
            _static(["guild-attendance", *guild]),
            "guild_attendance",
        ),
        MatrixCase(
            "character",
            "character",
            "character",
            AuthRequirement.CLIENT,
            _static(["character", GUILD_REGION, GUILD_REALM, CHARACTER_NAME]),
            "character",
        ),
        MatrixCase(
            "character-rankings",
            "character-rankings",
            "character_rankings",
            AuthRequirement.CLIENT,
            _static(["character-rankings", GUILD_REGION, GUILD_REALM, CHARACTER_NAME, "--zone-id", z]),
            "character_rankings",
        ),
        MatrixCase(
            "reports-zone",
            "reports",
            "reports",
            AuthRequirement.CLIENT,
            _static(["reports", "--zone-id", z, "--limit", "3"]),
            "reports",
        ),
        MatrixCase(
            "guild-reports",
            "guild-reports",
            "guild_reports",
            AuthRequirement.CLIENT,
            _static(["guild-reports", *guild, "--limit", "2"]),
            "guild_reports",
        ),
        MatrixCase(
            "report",
            "report",
            "report",
            AuthRequirement.CLIENT,
            _static(["report", PUBLIC_REPORT_CODE]),
            "report",
        ),
        MatrixCase(
            "report-fights",
            "report-fights",
            "report_fights",
            AuthRequirement.CLIENT,
            _static(["report-fights", PUBLIC_REPORT_CODE, "--difficulty", diff]),
            "report_fights",
        ),
        MatrixCase(
            "boss-kills",
            "boss-kills",
            "boss_kills",
            AuthRequirement.CLIENT,
            _static(["boss-kills", *sampled]),
            "boss_kills",
        ),
        MatrixCase(
            "boss-kills-spec-filter",
            "boss-kills",
            "boss_kills",
            AuthRequirement.CLIENT,
            _static(["boss-kills", *sampled, "--spec-name", "balance"]),
            "boss_kills",
        ),
        MatrixCase(
            "top-kills",
            "top-kills",
            "top_kills",
            AuthRequirement.CLIENT,
            _static(["top-kills", *sampled]),
            "top_kills",
        ),
        MatrixCase(
            "spec-kill-samples",
            "spec-kill-samples",
            "spec_kill_samples",
            AuthRequirement.CLIENT,
            # A small sampled cohort may legitimately be empty; assert envelope shape only.
            _static(["spec-kill-samples", *sampled, "--spec-name", "balance"]),
            None,
        ),
        MatrixCase(
            "boss-spec-usage",
            "boss-spec-usage",
            "boss_spec_usage",
            AuthRequirement.CLIENT,
            _static(["boss-spec-usage", *sampled]),
            "boss_spec_usage",
        ),
        MatrixCase(
            "ability-usage-summary",
            "ability-usage-summary",
            "ability_usage_summary",
            AuthRequirement.CLIENT,
            _static(["ability-usage-summary", *sampled]),
            "ability_usage_summary",
        ),
        MatrixCase(
            "comp-samples",
            "comp-samples",
            "comp_samples",
            AuthRequirement.CLIENT,
            _static(["comp-samples", *sampled]),
            "comp_samples",
        ),
        MatrixCase(
            "kill-time-distribution",
            "kill-time-distribution",
            "kill_time_distribution",
            AuthRequirement.CLIENT,
            _static(["kill-time-distribution", *sampled]),
            "kill_time_distribution",
        ),
        MatrixCase(
            "report-encounter",
            "report-encounter",
            "report_encounter",
            AuthRequirement.CLIENT,
            _with_url(["report-encounter", "{url}"]),
            "report_encounter",
        ),
        MatrixCase(
            "report-encounter-players",
            "report-encounter-players",
            "report_encounter_players",
            AuthRequirement.CLIENT,
            _with_url(["report-encounter-players", "{url}"]),
            "report_encounter_players",
        ),
        MatrixCase(
            "report-encounter-casts",
            "report-encounter-casts",
            "report_encounter_casts",
            AuthRequirement.CLIENT,
            _with_url(["report-encounter-casts", "{url}", "--preview-limit", "5"]),
            "report_encounter_casts",
        ),
        MatrixCase(
            "report-encounter-buffs",
            "report-encounter-buffs",
            "report_encounter_buffs",
            AuthRequirement.CLIENT,
            _with_url(["report-encounter-buffs", "{url}", "--view-by", "source", "--preview-limit", "5"]),
            "report_encounter_buffs",
        ),
        MatrixCase(
            "report-encounter-aura-summary",
            "report-encounter-aura-summary",
            "report_encounter_aura_summary",
            AuthRequirement.CLIENT,
            _with_aura_scope(
                [
                    "report-encounter-aura-summary",
                    "{url}",
                    "--ability-id",
                    "{ability-id}",
                    "--window-start-ms",
                    "10000",
                    "--window-end-ms",
                    "50000",
                ]
            ),
            "aura_summary",
        ),
        MatrixCase(
            "report-encounter-aura-compare",
            "report-encounter-aura-compare",
            "report_encounter_aura_compare",
            AuthRequirement.CLIENT,
            _with_aura_scope(
                [
                    "report-encounter-aura-compare",
                    "{url}",
                    "--ability-id",
                    "{ability-id}",
                    "--left-window-start-ms",
                    "10000",
                    "--left-window-end-ms",
                    "50000",
                    "--right-window-start-ms",
                    "50000",
                    "--right-window-end-ms",
                    "90000",
                ]
            ),
            "comparison",
        ),
        MatrixCase(
            "report-player-talents",
            "report-player-talents",
            "report_player_talents",
            AuthRequirement.CLIENT,
            _with_player_scope(["report-player-talents", "{url}", "--actor-id", "{actor-id}"]),
            "talent_transport_packet",
        ),
        MatrixCase(
            "report-encounter-damage-source-summary",
            "report-encounter-damage-source-summary",
            "report_encounter_damage_source_summary",
            AuthRequirement.CLIENT,
            _with_url(["report-encounter-damage-source-summary", "{url}"]),
            "report_encounter_damage_source_summary",
        ),
        MatrixCase(
            "report-encounter-damage-target-summary",
            "report-encounter-damage-target-summary",
            "report_encounter_damage_target_summary",
            AuthRequirement.CLIENT,
            _with_url(["report-encounter-damage-target-summary", "{url}"]),
            "report_encounter_damage_target_summary",
        ),
        MatrixCase(
            "report-encounter-damage-breakdown",
            "report-encounter-damage-breakdown",
            "report_encounter_damage_breakdown",
            AuthRequirement.CLIENT,
            _with_url(["report-encounter-damage-breakdown", "{url}", "--view-by", "source"]),
            "report_encounter_damage_breakdown",
        ),
        MatrixCase(
            "report-events",
            "report-events",
            "report_events",
            AuthRequirement.CLIENT,
            lambda ctx: [
                "report-events",
                PUBLIC_REPORT_CODE,
                "--fight-id",
                str(ctx.public_fight_id),
                "--limit",
                "5",
            ],
            "report_events",
        ),
        MatrixCase(
            "report-events-casts",
            "report-events",
            "report_events",
            AuthRequirement.CLIENT,
            lambda ctx: [
                "report-events",
                PUBLIC_REPORT_CODE,
                "--fight-id",
                str(ctx.public_fight_id),
                "--data-type",
                "casts",
                "--limit",
                "5",
            ],
            "report_events",
        ),
        MatrixCase(
            "report-table",
            "report-table",
            "report_table",
            AuthRequirement.CLIENT,
            lambda ctx: [
                "report-table",
                PUBLIC_REPORT_CODE,
                "--data-type",
                "damage-done",
                "--fight-id",
                str(ctx.public_fight_id),
            ],
            "report_table",
        ),
        MatrixCase(
            "report-graph",
            "report-graph",
            "report_graph",
            AuthRequirement.CLIENT,
            lambda ctx: [
                "report-graph",
                PUBLIC_REPORT_CODE,
                "--data-type",
                "damage-done",
                "--fight-id",
                str(ctx.public_fight_id),
            ],
            "report_graph",
        ),
        MatrixCase(
            "report-master-data",
            "report-master-data",
            "report_master_data",
            AuthRequirement.CLIENT,
            _static(["report-master-data", PUBLIC_REPORT_CODE, "--actor-type", "Player"]),
            "report_master_data",
        ),
        MatrixCase(
            "report-player-details",
            "report-player-details",
            "report_player_details",
            AuthRequirement.CLIENT,
            lambda ctx: [
                "report-player-details",
                PUBLIC_REPORT_CODE,
                "--fight-id",
                str(ctx.public_fight_id),
            ],
            "report_player_details",
        ),
        MatrixCase(
            "report-rankings",
            "report-rankings",
            "report_rankings",
            AuthRequirement.CLIENT,
            lambda ctx: [
                "report-rankings",
                PUBLIC_REPORT_CODE,
                "--fight-id",
                str(ctx.public_fight_id),
                "--player-metric",
                "dps",
                "--timeframe",
                "historical",
                "--compare",
                "rankings",
            ],
            "report_rankings",
        ),
        MatrixCase(
            "graphql-introspect",
            "graphql",
            "graphql",
            AuthRequirement.CLIENT,
            _static(["graphql", "--introspect"]),
            "graphql",
        ),
        MatrixCase(
            "auth-status",
            "auth",
            "auth",
            AuthRequirement.CLIENT,
            _static(["auth", "status", "--no-live"]),
            "auth",
        ),
        MatrixCase(
            "auth-whoami",
            "auth",
            "auth",
            AuthRequirement.USER,
            _static(["auth", "whoami"]),
            "user",
        ),
        MatrixCase(
            "private-report",
            "report",
            "report",
            AuthRequirement.PRIVATE,
            _static(["report", PRIVATE_REPORT_CODE]),
            "report",
        ),
        MatrixCase(
            "private-report-encounter",
            "report-encounter",
            "report_encounter",
            AuthRequirement.PRIVATE,
            _with_private_url(["report-encounter", "{url}"]),
            "report_encounter",
        ),
    )

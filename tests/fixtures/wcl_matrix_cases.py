"""Warcraft Logs live-matrix case definitions.

Every case names a JSON path into the command's envelope plus the check that path must satisfy, so
a command that starts returning an empty or missing block fails instead of silently passing on the
canonical block itself. Volatile inputs (zone, boss, difficulty, report, ability, actor) come from
the runtime-discovered :class:`LiveMatrixContext`, never from a hard-coded pin.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from tests.fixtures.live_matrix import (
    CHARACTER_NAME,
    GUILD_NAME,
    GUILD_REALM,
    GUILD_REGION,
    SAMPLE_REPORT_PAGES,
    SAMPLE_REPORTS_PER_PAGE,
)


class AuthRequirement(Enum):
    CLIENT = "client"
    USER = "user"
    PRIVATE = "private"


class DataCheck(Enum):
    """What the case's ``data_path`` has to satisfy for the command to count as working."""

    NONEMPTY = "nonempty"
    """The list or dict at the path exists and has at least one entry."""

    POSITIVE = "positive"
    """The number at the path exists and is greater than zero."""

    PRESENT = "present"
    """The path exists and is not null. For leaves that are legitimately empty (rankings that have
    not been computed yet, an unranked guild) but must still be served."""

    TRUE = "true"
    """The boolean at the path is exactly ``True``."""

    SAMPLING_METADATA = "sampling_metadata"
    """The cohort may legitimately be empty; only the sampling metadata contract is asserted."""


@dataclass(frozen=True)
class LiveMatrixContext:
    """Runtime-discovered inputs for one matrix session."""

    zone_id: int
    boss_id: int
    difficulty: int
    sample_start_ms: int
    sample_end_ms: int
    public_report_code: str
    public_report_url: str
    public_fight_id: int
    aura_ability_id: int | None = None
    player_actor_id: int | None = None
    cast_ability_id: int | None = None
    private_report_code: str | None = None
    private_report_url: str | None = None


@dataclass(frozen=True)
class MatrixCase:
    case_id: str
    command: str
    canonical_key: str
    auth: AuthRequirement
    build_args: Callable[[LiveMatrixContext], list[str]]
    data_path: tuple[str, ...]
    check: DataCheck
    sampled: bool = False


def _url(code: str, fight_id: int) -> str:
    return f"https://www.warcraftlogs.com/reports/{code}#fight={fight_id}"


def _require(value: int | str | None, what: str) -> int | str:
    if value is None:
        raise RuntimeError(f"live matrix could not discover {what}")
    return value


def _sampled_tail(ctx: LiveMatrixContext) -> list[str]:
    """Scope a sampled-analytics command to the cohort that contains the discovered anchor kill."""
    return [
        "--zone-id",
        str(ctx.zone_id),
        "--boss-id",
        str(ctx.boss_id),
        "--difficulty",
        str(ctx.difficulty),
        "--report-pages",
        str(SAMPLE_REPORT_PAGES),
        "--reports-per-page",
        str(SAMPLE_REPORTS_PER_PAGE),
        "--start-time",
        str(ctx.sample_start_ms),
        "--end-time",
        str(ctx.sample_end_ms),
    ]


def _static(args: list[str]) -> Callable[[LiveMatrixContext], list[str]]:
    return lambda _ctx: args


GUILD = [GUILD_REGION, GUILD_REALM, GUILD_NAME]


def _reference_cases() -> tuple[MatrixCase, ...]:
    return (
        MatrixCase("regions", "regions", "regions", AuthRequirement.CLIENT, _static(["regions"]), ("regions",), DataCheck.NONEMPTY),
        MatrixCase(
            "expansions", "expansions", "expansions", AuthRequirement.CLIENT, _static(["expansions"]), ("expansions",), DataCheck.NONEMPTY
        ),
        MatrixCase(
            "server",
            "server",
            "server",
            AuthRequirement.CLIENT,
            _static(["server", GUILD_REGION, GUILD_REALM]),
            ("server", "slug"),
            DataCheck.PRESENT,
        ),
        MatrixCase("zones", "zones", "zones", AuthRequirement.CLIENT, _static(["zones"]), ("zones",), DataCheck.NONEMPTY),
        MatrixCase(
            "zone",
            "zone",
            "zone",
            AuthRequirement.CLIENT,
            lambda ctx: ["zone", str(ctx.zone_id)],
            ("zone", "encounters"),
            DataCheck.NONEMPTY,
        ),
        MatrixCase(
            "encounter",
            "encounter",
            "encounter",
            AuthRequirement.CLIENT,
            lambda ctx: ["encounter", str(ctx.boss_id)],
            ("encounter", "name"),
            DataCheck.PRESENT,
        ),
        MatrixCase(
            "encounter-rankings",
            "encounter-rankings",
            "encounter_rankings",
            AuthRequirement.CLIENT,
            lambda ctx: [
                "encounter-rankings",
                "--zone-id",
                str(ctx.zone_id),
                "--boss-id",
                str(ctx.boss_id),
                "--difficulty",
                str(ctx.difficulty),
                "--top",
                "5",
            ],
            ("encounter_rankings", "rows"),
            DataCheck.NONEMPTY,
        ),
        MatrixCase("doctor", "doctor", "doctor", AuthRequirement.CLIENT, _static(["doctor"]), ("doctor", "status"), DataCheck.PRESENT),
        MatrixCase(
            "rate-limit",
            "rate-limit",
            "rate_limit",
            AuthRequirement.CLIENT,
            _static(["rate-limit"]),
            ("rate_limit", "limit_per_hour"),
            DataCheck.POSITIVE,
        ),
        MatrixCase(
            "graphql-introspect",
            "graphql",
            "graphql",
            AuthRequirement.CLIENT,
            _static(["graphql", "--introspect"]),
            ("graphql", "types"),
            DataCheck.NONEMPTY,
        ),
    )


def _discovery_cases() -> tuple[MatrixCase, ...]:
    # Warcraft Logs discovery is deliberately narrow: it matches explicit report URLs and codes and
    # returns a hint for free text, so both cases have to use a report reference.
    return (
        MatrixCase(
            "search-report",
            "search",
            "search",
            AuthRequirement.CLIENT,
            lambda ctx: ["search", ctx.public_report_code, "--limit", "3"],
            ("search",),
            DataCheck.NONEMPTY,
        ),
        MatrixCase(
            "resolve-report",
            "resolve",
            "resolve",
            AuthRequirement.CLIENT,
            lambda ctx: ["resolve", ctx.public_report_url],
            ("resolve", "report_reference"),
            DataCheck.NONEMPTY,
        ),
    )


def _profile_cases() -> tuple[MatrixCase, ...]:
    return (
        MatrixCase("guild", "guild", "guild", AuthRequirement.CLIENT, _static(["guild", *GUILD]), ("guild", "name"), DataCheck.PRESENT),
        MatrixCase(
            "guild-members",
            "guild-members",
            "guild_members",
            AuthRequirement.CLIENT,
            _static(["guild-members", *GUILD, "--limit", "5"]),
            ("guild_members", "members"),
            DataCheck.NONEMPTY,
        ),
        MatrixCase(
            "guild-rankings",
            "guild-rankings",
            "guild_rankings",
            AuthRequirement.CLIENT,
            lambda ctx: ["guild-rankings", *GUILD, "--zone-id", str(ctx.zone_id), "--size", "10"],
            ("guild_rankings", "zone_ranking"),
            DataCheck.PRESENT,
        ),
        MatrixCase(
            "guild-attendance",
            "guild-attendance",
            "guild_attendance",
            AuthRequirement.CLIENT,
            _static(["guild-attendance", *GUILD]),
            ("guild_attendance", "attendance"),
            DataCheck.NONEMPTY,
        ),
        MatrixCase(
            "character",
            "character",
            "character",
            AuthRequirement.CLIENT,
            _static(["character", GUILD_REGION, GUILD_REALM, CHARACTER_NAME]),
            ("character", "name"),
            DataCheck.PRESENT,
        ),
        MatrixCase(
            "character-rankings",
            "character-rankings",
            "character_rankings",
            AuthRequirement.CLIENT,
            lambda ctx: ["character-rankings", GUILD_REGION, GUILD_REALM, CHARACTER_NAME, "--zone-id", str(ctx.zone_id)],
            # A character legitimately has no rankings yet in a freshly opened tier.
            ("character_rankings", "rankings"),
            DataCheck.PRESENT,
        ),
        MatrixCase(
            "reports-zone",
            "reports",
            "reports",
            AuthRequirement.CLIENT,
            lambda ctx: ["reports", "--zone-id", str(ctx.zone_id), "--limit", "3"],
            ("reports",),
            DataCheck.NONEMPTY,
        ),
        MatrixCase(
            "guild-reports",
            "guild-reports",
            "guild_reports",
            AuthRequirement.CLIENT,
            _static(["guild-reports", *GUILD, "--limit", "2"]),
            ("guild_reports",),
            DataCheck.NONEMPTY,
        ),
    )


def _sampled_cases() -> tuple[MatrixCase, ...]:
    return (
        MatrixCase(
            "boss-kills",
            "boss-kills",
            "boss_kills",
            AuthRequirement.CLIENT,
            lambda ctx: ["boss-kills", *_sampled_tail(ctx), "--top", "3"],
            ("boss_kills",),
            DataCheck.NONEMPTY,
            sampled=True,
        ),
        MatrixCase(
            "boss-kills-spec-filter",
            "boss-kills",
            "boss_kills",
            AuthRequirement.CLIENT,
            # The cohort is real but a single spec may be absent from it, so only the sampling
            # contract is guaranteed here.
            lambda ctx: ["boss-kills", *_sampled_tail(ctx), "--top", "3", "--spec-name", "balance"],
            ("boss_kills",),
            DataCheck.SAMPLING_METADATA,
            sampled=True,
        ),
        MatrixCase(
            "top-kills",
            "top-kills",
            "top_kills",
            AuthRequirement.CLIENT,
            lambda ctx: ["top-kills", *_sampled_tail(ctx), "--top", "3"],
            ("top_kills",),
            DataCheck.NONEMPTY,
            sampled=True,
        ),
        MatrixCase(
            "spec-kill-samples",
            "spec-kill-samples",
            "spec_kill_samples",
            AuthRequirement.CLIENT,
            lambda ctx: ["spec-kill-samples", *_sampled_tail(ctx), "--top", "3", "--spec-name", "balance"],
            ("spec_kill_samples",),
            DataCheck.SAMPLING_METADATA,
            sampled=True,
        ),
        MatrixCase(
            "boss-spec-usage",
            "boss-spec-usage",
            "boss_spec_usage",
            AuthRequirement.CLIENT,
            lambda ctx: ["boss-spec-usage", *_sampled_tail(ctx), "--top", "3"],
            ("boss_spec_usage",),
            DataCheck.NONEMPTY,
            sampled=True,
        ),
        MatrixCase(
            "ability-usage-summary",
            "ability-usage-summary",
            "ability_usage_summary",
            AuthRequirement.CLIENT,
            lambda ctx: [
                "ability-usage-summary",
                *_sampled_tail(ctx),
                "--ability-id",
                str(_require(ctx.cast_ability_id, "a cast ability id in the anchor kill")),
                "--preview-limit",
                "5",
            ],
            ("ability_usage_summary", "total_casts"),
            DataCheck.POSITIVE,
            sampled=True,
        ),
        MatrixCase(
            "comp-samples",
            "comp-samples",
            "comp_samples",
            AuthRequirement.CLIENT,
            lambda ctx: ["comp-samples", *_sampled_tail(ctx), "--top", "3"],
            ("comp_samples",),
            DataCheck.NONEMPTY,
            sampled=True,
        ),
        MatrixCase(
            "kill-time-distribution",
            "kill-time-distribution",
            "kill_time_distribution",
            AuthRequirement.CLIENT,
            lambda ctx: ["kill-time-distribution", *_sampled_tail(ctx)],
            ("kill_time_distribution", "rows"),
            DataCheck.NONEMPTY,
            sampled=True,
        ),
    )


def _report_encounter_cases() -> tuple[MatrixCase, ...]:
    return (
        MatrixCase(
            "report-encounter",
            "report-encounter",
            "report_encounter",
            AuthRequirement.CLIENT,
            lambda ctx: ["report-encounter", ctx.public_report_url],
            ("report_encounter", "fight"),
            DataCheck.NONEMPTY,
        ),
        MatrixCase(
            "report-encounter-players",
            "report-encounter-players",
            "report_encounter_players",
            AuthRequirement.CLIENT,
            lambda ctx: ["report-encounter-players", ctx.public_report_url],
            ("report_encounter_players", "player_details"),
            DataCheck.NONEMPTY,
        ),
        MatrixCase(
            "report-encounter-casts",
            "report-encounter-casts",
            "report_encounter_casts",
            AuthRequirement.CLIENT,
            lambda ctx: ["report-encounter-casts", ctx.public_report_url, "--preview-limit", "5"],
            ("report_encounter_casts", "casts", "by_source"),
            DataCheck.NONEMPTY,
        ),
        MatrixCase(
            "report-encounter-buffs",
            "report-encounter-buffs",
            "report_encounter_buffs",
            AuthRequirement.CLIENT,
            lambda ctx: ["report-encounter-buffs", ctx.public_report_url, "--view-by", "source", "--preview-limit", "5"],
            ("report_encounter_buffs", "buffs", "preview"),
            DataCheck.NONEMPTY,
        ),
        MatrixCase(
            "report-encounter-aura-summary",
            "report-encounter-aura-summary",
            "report_encounter_aura_summary",
            AuthRequirement.CLIENT,
            lambda ctx: [
                "report-encounter-aura-summary",
                ctx.public_report_url,
                "--ability-id",
                str(_require(ctx.aura_ability_id, "an aura ability id in the anchor kill")),
                "--window-start-ms",
                "10000",
                "--window-end-ms",
                "50000",
            ],
            ("report_encounter_aura_summary", "aura_summary"),
            DataCheck.NONEMPTY,
        ),
        MatrixCase(
            "report-encounter-aura-compare",
            "report-encounter-aura-compare",
            "report_encounter_aura_compare",
            AuthRequirement.CLIENT,
            lambda ctx: [
                "report-encounter-aura-compare",
                ctx.public_report_url,
                "--ability-id",
                str(_require(ctx.aura_ability_id, "an aura ability id in the anchor kill")),
                "--left-window-start-ms",
                "10000",
                "--left-window-end-ms",
                "50000",
                "--right-window-start-ms",
                "50000",
                "--right-window-end-ms",
                "90000",
            ],
            ("report_encounter_aura_compare", "comparison"),
            DataCheck.NONEMPTY,
        ),
        MatrixCase(
            "report-player-talents",
            "report-player-talents",
            "report_player_talents",
            AuthRequirement.CLIENT,
            lambda ctx: [
                "report-player-talents",
                ctx.public_report_url,
                "--actor-id",
                str(_require(ctx.player_actor_id, "a player actor id in the anchor kill")),
            ],
            ("report_player_talents", "talent_transport_packet"),
            DataCheck.NONEMPTY,
        ),
        MatrixCase(
            "report-encounter-damage-source-summary",
            "report-encounter-damage-source-summary",
            "report_encounter_damage_source_summary",
            AuthRequirement.CLIENT,
            lambda ctx: ["report-encounter-damage-source-summary", ctx.public_report_url],
            ("report_encounter_damage_source_summary", "damage_summary"),
            DataCheck.NONEMPTY,
        ),
        MatrixCase(
            "report-encounter-damage-target-summary",
            "report-encounter-damage-target-summary",
            "report_encounter_damage_target_summary",
            AuthRequirement.CLIENT,
            lambda ctx: ["report-encounter-damage-target-summary", ctx.public_report_url],
            ("report_encounter_damage_target_summary", "damage_summary"),
            DataCheck.NONEMPTY,
        ),
        MatrixCase(
            "report-encounter-damage-breakdown",
            "report-encounter-damage-breakdown",
            "report_encounter_damage_breakdown",
            AuthRequirement.CLIENT,
            lambda ctx: ["report-encounter-damage-breakdown", ctx.public_report_url, "--view-by", "source"],
            ("report_encounter_damage_breakdown", "table"),
            DataCheck.NONEMPTY,
        ),
    )


def _report_data_cases() -> tuple[MatrixCase, ...]:
    return (
        MatrixCase(
            "report",
            "report",
            "report",
            AuthRequirement.CLIENT,
            lambda ctx: ["report", ctx.public_report_code],
            ("report", "code"),
            DataCheck.PRESENT,
        ),
        MatrixCase(
            "report-fights",
            "report-fights",
            "report_fights",
            AuthRequirement.CLIENT,
            lambda ctx: ["report-fights", ctx.public_report_code, "--difficulty", str(ctx.difficulty)],
            ("report_fights", "fights"),
            DataCheck.NONEMPTY,
        ),
        MatrixCase(
            "report-events",
            "report-events",
            "report_events",
            AuthRequirement.CLIENT,
            lambda ctx: ["report-events", ctx.public_report_code, "--fight-id", str(ctx.public_fight_id), "--limit", "5"],
            ("report_events",),
            DataCheck.NONEMPTY,
        ),
        MatrixCase(
            "report-events-casts",
            "report-events",
            "report_events",
            AuthRequirement.CLIENT,
            lambda ctx: [
                "report-events",
                ctx.public_report_code,
                "--fight-id",
                str(ctx.public_fight_id),
                "--data-type",
                "casts",
                "--limit",
                "5",
            ],
            ("report_events",),
            DataCheck.NONEMPTY,
        ),
        MatrixCase(
            "report-table",
            "report-table",
            "report_table",
            AuthRequirement.CLIENT,
            lambda ctx: ["report-table", ctx.public_report_code, "--data-type", "damage-done", "--fight-id", str(ctx.public_fight_id)],
            ("report_table", "data", "entries"),
            DataCheck.NONEMPTY,
        ),
        MatrixCase(
            "report-graph",
            "report-graph",
            "report_graph",
            AuthRequirement.CLIENT,
            lambda ctx: ["report-graph", ctx.public_report_code, "--data-type", "damage-done", "--fight-id", str(ctx.public_fight_id)],
            ("report_graph", "data", "series"),
            DataCheck.NONEMPTY,
        ),
        MatrixCase(
            "report-master-data",
            "report-master-data",
            "report_master_data",
            AuthRequirement.CLIENT,
            lambda ctx: ["report-master-data", ctx.public_report_code, "--actor-type", "Player"],
            ("report_master_data", "actors"),
            DataCheck.NONEMPTY,
        ),
        MatrixCase(
            "report-player-details",
            "report-player-details",
            "report_player_details",
            AuthRequirement.CLIENT,
            lambda ctx: ["report-player-details", ctx.public_report_code, "--fight-id", str(ctx.public_fight_id)],
            ("report_player_details", "roles"),
            DataCheck.NONEMPTY,
        ),
        MatrixCase(
            "report-rankings",
            "report-rankings",
            "report_rankings",
            AuthRequirement.CLIENT,
            lambda ctx: [
                "report-rankings",
                ctx.public_report_code,
                "--fight-id",
                str(ctx.public_fight_id),
                "--player-metric",
                "dps",
                "--timeframe",
                "historical",
                "--compare",
                "rankings",
            ],
            # Warcraft Logs has not necessarily ranked a report that was uploaded minutes ago.
            ("report_rankings", "count"),
            DataCheck.PRESENT,
        ),
    )


def _auth_cases() -> tuple[MatrixCase, ...]:
    return (
        MatrixCase(
            "auth-status",
            "status",
            "auth",
            AuthRequirement.CLIENT,
            _static(["auth", "status", "--no-live"]),
            ("auth", "configured"),
            DataCheck.TRUE,
        ),
        MatrixCase(
            "auth-whoami", "whoami", "user", AuthRequirement.USER, _static(["auth", "whoami"]), ("user",), DataCheck.NONEMPTY
        ),
        MatrixCase(
            "private-report",
            "report",
            "report",
            AuthRequirement.PRIVATE,
            lambda ctx: ["report", str(_require(ctx.private_report_code, "a private guild report"))],
            ("report", "code"),
            DataCheck.PRESENT,
        ),
        MatrixCase(
            "private-report-encounter",
            "report-encounter",
            "report_encounter",
            AuthRequirement.PRIVATE,
            lambda ctx: ["report-encounter", str(_require(ctx.private_report_url, "a private guild report fight"))],
            ("report_encounter", "fight"),
            DataCheck.NONEMPTY,
        ),
    )


def matrix_cases() -> tuple[MatrixCase, ...]:
    return (
        *_reference_cases(),
        *_discovery_cases(),
        *_profile_cases(),
        *_sampled_cases(),
        *_report_encounter_cases(),
        *_report_data_cases(),
        *_auth_cases(),
    )

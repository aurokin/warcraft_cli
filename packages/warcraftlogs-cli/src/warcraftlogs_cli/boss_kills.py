"""Sampled boss-kill analytics across Warcraft Logs reports."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from warcraft_core.analytics import numeric_summary

from warcraftlogs_cli.client import ReportPlayerDetailsOptions, WarcraftLogsClient
from warcraftlogs_cli.report_payloads import fight_payload, report_brief_payload, report_payload, report_url
from warcraftlogs_cli.sampling_utils import (
    boss_matches,
    dict_at,
    fight_duration_ms,
    list_at,
    normalize_match_text,
    report_is_finished,
    sampled_spec_filter_notes,
    utc_now_z,
)


@dataclass(frozen=True, slots=True)
class CrossReportScope:
    """Cohort-shaping inputs shared by every sampled cross-report command.

    Field order is the emitted ``query`` key order: ``dataclasses.asdict`` on this
    object is what the sampled payloads echo back to the caller. ``top`` is the
    returned-row cap; commands without a ``--top`` flag leave it at the default and
    drop it from their query echo.
    """

    zone_id: int
    boss_id: int | None = None
    boss_name: str | None = None
    difficulty: int | None = None
    spec_name: str | None = None
    kill_time_min: float | None = None
    kill_time_max: float | None = None
    top: int = 10
    report_pages: int = 1
    reports_per_page: int = 25
    start_time: float | None = None
    end_time: float | None = None
    guild_region: str | None = None
    guild_realm: str | None = None
    guild_name: str | None = None


def player_spec_matches(actor: dict[str, Any], spec_name: str) -> bool:
    wanted = normalize_match_text(spec_name)
    for spec in list_at(actor, "specs"):
        if not isinstance(spec, dict):
            continue
        if normalize_match_text(str(spec.get("spec") or "")) == wanted:
            return True
    return False


def _player_details_roles(report: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    details = dict_at(report, "playerDetails")
    data = dict_at(details, "data")
    role_data = dict_at(data, "playerDetails") or data
    roles: dict[str, list[dict[str, Any]]] = {}
    for role in ("tanks", "healers", "dps"):
        roles[role] = [row for row in list_at(role_data, role) if isinstance(row, dict)]
    return roles


def matching_spec_players(report: dict[str, Any], *, spec_name: str) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for role, rows in _player_details_roles(report).items():
        for row in rows:
            if not player_spec_matches(row, spec_name):
                continue
            matches.append(
                {
                    "name": row.get("name"),
                    "id": row.get("id"),
                    "role": role,
                    "type": row.get("type"),
                    "matching_specs": [
                        spec
                        for spec in (list_at(row, "specs"))
                        if normalize_match_text(str(spec.get("spec") or "")) == normalize_match_text(spec_name)
                    ],
                }
            )
    return matches


def boss_kill_row(
    *,
    report: dict[str, Any],
    fight: dict[str, Any],
    matching_players: list[dict[str, Any]] | None = None,
    duplicate_reports: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    duration_ms = fight_duration_ms(fight)
    return {
        "report": report_brief_payload(report),
        "report_finished": report_is_finished(report),
        "guild": report_payload(report).get("guild"),
        "fight": fight_payload(fight),
        "duration_ms": duration_ms,
        "duration_seconds": round(duration_ms / 1000, 2) if duration_ms is not None else None,
        "matching_players": matching_players or [],
        # Other reports of this same pull, collapsed into this row by deduplicate_pulls.
        "duplicate_reports": duplicate_reports or [],
    }


# Two raiders in one group each uploading the pull yields two reports of a single kill. Their
# combat logs start seconds apart, so the same pull lands at slightly different wall-clock
# boundaries; anything further apart than this is a different pull.
DUPLICATE_PULL_TOLERANCE_MS = 5000


@dataclass(frozen=True, slots=True)
class _PullIdentity:
    """Everything except timing that has to agree before two sampled fights can be one pull."""

    encounter_id: int | None
    difficulty: int | None
    size: int | None
    guild_id: int | None
    guild_name: str | None


def _pull_identity(report: dict[str, Any], fight: dict[str, Any]) -> _PullIdentity:
    guild = dict_at(report, "guild")
    guild_id = guild.get("id")
    guild_name = guild.get("name")
    return _PullIdentity(
        encounter_id=fight.get("encounterID") if isinstance(fight.get("encounterID"), int) else None,
        difficulty=fight.get("difficulty") if isinstance(fight.get("difficulty"), int) else None,
        size=fight.get("size") if isinstance(fight.get("size"), int) else None,
        guild_id=guild_id if isinstance(guild_id, int) else None,
        guild_name=guild_name if isinstance(guild_name, str) else None,
    )


def _absolute_fight_window_ms(report: dict[str, Any], fight: dict[str, Any]) -> tuple[float, float] | None:
    """Wall-clock ``(start, end)`` of a fight: report start plus the report-relative fight offsets."""
    report_start = report.get("startTime")
    fight_start = fight.get("startTime")
    fight_end = fight.get("endTime")
    if not isinstance(report_start, (int, float)) or not isinstance(fight_start, (int, float)):
        return None
    if not isinstance(fight_end, (int, float)):
        return None
    return float(report_start) + float(fight_start), float(report_start) + float(fight_end)


@dataclass(slots=True)
class SampledPull:
    """One real pull, plus the citations of the other reports that logged the same pull."""

    report: dict[str, Any]
    fight: dict[str, Any]
    identity: _PullIdentity
    window_ms: tuple[float, float] | None
    duplicates: list[dict[str, Any]] = field(default_factory=list)

    def is_same_pull(self, identity: _PullIdentity, window_ms: tuple[float, float] | None) -> bool:
        if self.window_ms is None or window_ms is None or identity != self.identity:
            return False
        return all(abs(mine - other) <= DUPLICATE_PULL_TOLERANCE_MS for mine, other in zip(self.window_ms, window_ms, strict=True))


def _pull_citation(report: dict[str, Any], fight: dict[str, Any]) -> dict[str, Any]:
    return {"report_code": report.get("code"), "fight_id": fight.get("id")}


def deduplicate_pulls(candidates: Iterable[tuple[dict[str, Any], dict[str, Any]]]) -> list[SampledPull]:
    """Collapse one real pull logged in several reports into a single sampled kill.

    Warcraft Logs exposes no cross-report pull ID, so the match is deliberately narrow and is
    labelled in the payload rather than inferred silently (docs/foundation/SAFE_ANALYTICS_RULES.md):
    same encounter, difficulty, raid size and guild, with wall-clock start *and* end both within
    ``DUPLICATE_PULL_TOLERANCE_MS``. A fight whose absolute window cannot be computed is always kept.
    """
    pulls: list[SampledPull] = []
    for report, fight in candidates:
        identity = _pull_identity(report, fight)
        window_ms = _absolute_fight_window_ms(report, fight)
        existing = None
        if existing is None:
            pulls.append(SampledPull(report=report, fight=fight, identity=identity, window_ms=window_ms))
            continue
        existing.duplicates.append(_pull_citation(report, fight))
    return pulls


def sampled_dedupe_notes(sample: dict[str, Any]) -> list[str]:
    """Say so in the payload when sampled kills were collapsed, per SAFE_ANALYTICS_RULES.md."""
    removed = sample.get("duplicates_removed")
    if not isinstance(removed, int) or removed <= 0:
        return []
    return [
        f"{removed} sampled fight(s) were the same pull logged in more than one report (same encounter, "
        f"difficulty, raid size and guild, with start and end within {DUPLICATE_PULL_TOLERANCE_MS // 1000}s) "
        "and were collapsed into one kill; the collapsed report codes are on each kill's duplicate_reports"
    ]


def sampled_cross_report_freshness(
    cache_ttl_seconds: int | None = None,
    *,
    transport_counts: dict[str, int],
) -> dict[str, Any]:
    """Freshness for one sampled payload.

    ``sampled_at`` is when the command ran, not when the cohort was fetched: report listings and
    report details can be served from cache up to ``cache_ttl_seconds`` old. The transport counts
    make that visible, so a warm-cache rerun is distinguishable from a live scan.
    """
    upstream_request_count = transport_counts.get("upstream_request_count", 0)
    cache_hit_count = transport_counts.get("cache_hit_count", 0)
    return {
        "sampled_at": utc_now_z(),
        "cache_ttl_seconds": cache_ttl_seconds,
        "cache_hit_count": cache_hit_count,
        "upstream_request_count": upstream_request_count,
        "served_entirely_from_cache": upstream_request_count == 0 and cache_hit_count > 0,
    }


def sampled_cache_provenance(cache_ttl_seconds: int | None) -> dict[str, Any]:
    """Cache provenance for a sampled cohort.

    Sampling discovers reports and excludes live ones before scanning, so the
    cohort is always finished reports cached under the finished-report TTL.

    Caveat: within the short live→finished cache window (see docs/warcraftlogs/CACHING.md),
    a per-report detail fetch can transiently serve a still-cached live entry. The cohort
    intent is finished reports; the window is bounded by the short report TTL.
    """
    return {
        "finished": True,
        "live": False,
        "cache_ttl_seconds": cache_ttl_seconds,
        "source": "sampled_finished_reports",
    }


def sampled_sample_scope(
    *,
    ranking_basis: str,
    query: dict[str, Any],
    returned: int,
    excluded: int,
    truncated: bool,
) -> dict[str, Any]:
    """Consolidated scope block: ranking basis + cohort filters + returned/excluded counts.

    ``filters`` is a copy of the full ``query`` already emitted on the payload — every
    cohort-shaping input (boss/zone/difficulty/spec/kill-time, guild scope, time window,
    report-page budget) is preserved so two runs with different scope never look identical.
    """
    return {
        "ranking_basis": ranking_basis,
        "filters": dict(query) if isinstance(query, dict) else {},
        "returned": returned,
        "excluded": excluded,
        "truncated": truncated,
    }


def _row_citation_pairs(row: dict[str, Any]) -> list[tuple[str, int | None]]:
    """A row's own ``(report code, fight id)``, then every report that logged the same pull.

    The collapsed reports stay citable: a caller holding one of them must still be able to find
    the kill it was folded into.
    """
    candidates = [(dict_at(row, "report").get("code"), dict_at(row, "fight").get("id"))]
    candidates += [
        (entry.get("report_code"), entry.get("fight_id"))
        for entry in list_at(row, "duplicate_reports")
        if isinstance(entry, dict)
    ]
    return [
        (code, fight_id if isinstance(fight_id, int) else None)
        for code, fight_id in candidates
        if isinstance(code, str)
    ]


def sampled_cross_report_citations(
    rows: list[dict[str, Any]],
    *,
    limit: int = 20,
    root_url: str = "https://www.warcraftlogs.com",
) -> dict[str, Any]:
    sample_reports: list[dict[str, Any]] = []
    seen: set[tuple[str, int | None]] = set()
    for report_code, fight_id in (pair for row in rows for pair in _row_citation_pairs(row)):
        if (report_code, fight_id) in seen:
            continue
        seen.add((report_code, fight_id))
        sample_reports.append(
            {
                "report_code": report_code,
                "fight_id": fight_id,
                "report_url": report_url(report_code, fight_id=fight_id, root_url=root_url),
            }
        )
        if len(sample_reports) >= limit:
            break
    return {
        "sample_reports": sample_reports,
    }


def duration_bucket_rows(values: list[float], *, bucket_seconds: int) -> list[dict[str, Any]]:
    if not values:
        return []
    counts: dict[int, int] = {}
    for value in values:
        bucket_start = int(value // bucket_seconds) * bucket_seconds
        counts[bucket_start] = counts.get(bucket_start, 0) + 1
    rows = []
    total = len(values)
    for bucket_start, count in sorted(counts.items()):
        bucket_end = bucket_start + bucket_seconds
        rows.append(
            {
                "start_seconds": bucket_start,
                "end_seconds": bucket_end,
                "count": count,
                "percent": round((count / total) * 100, 2),
            }
        )
    return rows


def _fetch_zone_report_rows(
    client: WarcraftLogsClient,
    *,
    zone_id: int,
    guild_region: str | None,
    guild_realm: str | None,
    guild_name: str | None,
    report_pages: int,
    reports_per_page: int,
    start_time: float | None,
    end_time: float | None,
) -> list[dict[str, Any]]:
    report_rows: list[dict[str, Any]] = []
    for page in range(1, report_pages + 1):
        pagination = client.reports(
            guild_region=guild_region,
            guild_realm=guild_realm,
            guild_name=guild_name,
            limit=reports_per_page,
            page=page,
            start_time=start_time,
            end_time=end_time,
            zone_id=zone_id,
            game_zone_id=None,
        )
        page_rows = list_at(pagination, "data")
        report_rows.extend([row for row in page_rows if isinstance(row, dict)])
        if not pagination.get("has_more_pages"):
            break
    return report_rows


def _kill_duration_in_bounds(
    fight: dict[str, Any],
    *,
    kill_time_min: float | None,
    kill_time_max: float | None,
) -> float | None:
    duration_ms = fight_duration_ms(fight)
    if duration_ms is None:
        return None
    duration_seconds = duration_ms / 1000
    if kill_time_min is not None and duration_seconds < kill_time_min:
        return None
    if kill_time_max is not None and duration_seconds > kill_time_max:
        return None
    return duration_seconds


def _matching_players_for_fight(
    client: WarcraftLogsClient,
    *,
    report: dict[str, Any],
    fight: dict[str, Any],
    difficulty: int | None,
    spec_name: str | None,
) -> list[dict[str, Any]] | None:
    if not spec_name:
        return []
    details_report = client.report_player_details(
        code=str(report.get("code") or ""),
        allow_unlisted=False,
        options=ReportPlayerDetailsOptions(
            difficulty=difficulty,
            encounter_id=fight.get("encounterID") if isinstance(fight.get("encounterID"), int) else None,
            fight_ids=[int(fight["id"])] if isinstance(fight.get("id"), int) else None,
            include_combatant_info=True,
            kill_type="Kills",
        ),
        ttl_override=client._finished_report_ttl,
    )
    matching_players = matching_spec_players(details_report, spec_name=spec_name)
    if not matching_players:
        return None
    return matching_players


def _matching_kill_fights(
    client: WarcraftLogsClient,
    finished_reports: list[dict[str, Any]],
    *,
    boss_id: int | None,
    boss_name: str | None,
    difficulty: int | None,
    kill_time_min: float | None,
    kill_time_max: float | None,
) -> tuple[list[tuple[dict[str, Any], dict[str, Any]]], int]:
    """``(report, fight)`` pairs for every kill matching the cohort filters, and the fights scanned."""
    candidates: list[tuple[dict[str, Any], dict[str, Any]]] = []
    scanned_fight_count = 0
    for report in finished_reports:
        fights_payload = client.report_fights(
            code=str(report.get("code") or ""),
            difficulty=difficulty,
            allow_unlisted=False,
            ttl_override=client._finished_report_ttl,
        )
        for fight in list_at(fights_payload, "fights"):
            if not isinstance(fight, dict):
                continue
            scanned_fight_count += 1
            if not fight.get("kill"):
                continue
            if not boss_matches(fight, boss_id=boss_id, boss_name=boss_name):
                continue
            if _kill_duration_in_bounds(fight, kill_time_min=kill_time_min, kill_time_max=kill_time_max) is None:
                continue
            candidates.append((report, fight))
    return candidates, scanned_fight_count


@dataclass(frozen=True, slots=True)
class ScannedKills:
    """Rows for one sampled cohort plus the counts that make the sampling legible."""

    rows: list[dict[str, Any]]
    scanned_fight_count: int
    matched_boss_kill_count: int
    duplicates_removed: int


def _scan_finished_reports_for_boss_kills(
    client: WarcraftLogsClient,
    finished_reports: list[dict[str, Any]],
    *,
    boss_id: int | None,
    boss_name: str | None,
    difficulty: int | None,
    spec_name: str | None,
    kill_time_min: float | None,
    kill_time_max: float | None,
) -> ScannedKills:
    candidates, scanned_fight_count = _matching_kill_fights(
        client,
        finished_reports,
        boss_id=boss_id,
        boss_name=boss_name,
        difficulty=difficulty,
        kill_time_min=kill_time_min,
        kill_time_max=kill_time_max,
    )
    # Collapse before the per-fight player-details fetch, so a double-logged pull neither
    # double-counts nor costs a second upstream request.
    pulls = deduplicate_pulls(candidates)
    boss_kills: list[dict[str, Any]] = []
    for pull in pulls:
        matching_players = _matching_players_for_fight(
            client,
            report=pull.report,
            fight=pull.fight,
            difficulty=difficulty,
            spec_name=spec_name,
        )
        if spec_name and matching_players is None:
            continue
        boss_kills.append(
            boss_kill_row(
                report=pull.report,
                fight=pull.fight,
                matching_players=matching_players or [],
                duplicate_reports=pull.duplicates,
            )
        )

    boss_kills.sort(
        key=lambda row: (
            row.get("duration_ms") if isinstance(row.get("duration_ms"), (int, float)) else float("inf"),
            str((row.get("report") or {}).get("code") or ""),
            int((row.get("fight") or {}).get("id") or 0),
        )
    )
    return ScannedKills(
        rows=boss_kills,
        scanned_fight_count=scanned_fight_count,
        matched_boss_kill_count=len(pulls),
        duplicates_removed=len(candidates) - len(pulls),
    )


def collect_boss_kill_rows(client: WarcraftLogsClient, scope: CrossReportScope) -> dict[str, Any]:
    report_rows = _fetch_zone_report_rows(
        client,
        zone_id=scope.zone_id,
        guild_region=scope.guild_region,
        guild_realm=scope.guild_realm,
        guild_name=scope.guild_name,
        report_pages=scope.report_pages,
        reports_per_page=scope.reports_per_page,
        start_time=scope.start_time,
        end_time=scope.end_time,
    )
    live_reports = [row for row in report_rows if not report_is_finished(row)]
    finished_reports = [row for row in report_rows if report_is_finished(row)]
    scanned = _scan_finished_reports_for_boss_kills(
        client,
        finished_reports,
        boss_id=scope.boss_id,
        boss_name=scope.boss_name,
        difficulty=scope.difficulty,
        spec_name=scope.spec_name,
        kill_time_min=scope.kill_time_min,
        kill_time_max=scope.kill_time_max,
    )
    return {
        "rows": scanned.rows,
        "sample": {
            "source_report_count": len(report_rows),
            "finished_report_count": len(finished_reports),
            "skipped_live_report_count": len(live_reports),
            "scanned_fight_count": scanned.scanned_fight_count,
            # Distinct pulls: a kill logged by several raiders counts once, and duplicates_removed
            # says how many raw fights were collapsed to get there.
            "matched_boss_kill_count": scanned.matched_boss_kill_count,
            "duplicates_removed": scanned.duplicates_removed,
        },
    }


def boss_kills_payload(
    *,
    kind: str,
    rows: list[dict[str, Any]],
    sample: dict[str, Any],
    query: dict[str, Any],
    top: int,
    transport_counts: dict[str, int],
    cache_ttl_seconds: int | None = None,
    root_url: str = "https://www.warcraftlogs.com",
) -> dict[str, Any]:
    returned = rows[:top]
    excluded = max(0, len(rows) - len(returned))
    truncated = len(rows) > top
    return {
        "ok": True,
        "provider": "warcraftlogs",
        "kind": kind,
        "ranking_basis": "sampled_fastest_kills",
        "matching_rule": "sampled_zone_reports_filtered_by_optional_boss_difficulty_spec_and_kill_time",
        "query": query,
        "notes": [
            *sampled_spec_filter_notes(query.get("spec_name") if isinstance(query, dict) else None),
            *sampled_dedupe_notes(sample),
        ],
        "freshness": sampled_cross_report_freshness(cache_ttl_seconds, transport_counts=transport_counts),
        "cache_provenance": sampled_cache_provenance(cache_ttl_seconds),
        "sample_scope": sampled_sample_scope(
            ranking_basis="sampled_fastest_kills",
            query=query,
            returned=len(returned),
            excluded=excluded,
            truncated=truncated,
        ),
        "citations": sampled_cross_report_citations(rows, root_url=root_url),
        "sample": {
            **sample,
            "filtered_kill_count": len(rows),
            "returned_kill_count": len(returned),
            "excluded_kill_count": excluded,
            "truncated": truncated,
            "stable_source_only": True,
        },
        "count": len(returned),
        "kills": returned,
    }


def spec_filtered_kill_samples_payload(
    *,
    rows: list[dict[str, Any]],
    sample: dict[str, Any],
    query: dict[str, Any],
    top: int,
    transport_counts: dict[str, int],
    cache_ttl_seconds: int | None = None,
    root_url: str = "https://www.warcraftlogs.com",
) -> dict[str, Any]:
    # `rows` arrive sorted by ascending kill duration (shared collect_boss_kill_rows path),
    # so the returned head is the fastest qualifying kills. `sample_size` is the FULL matching
    # cohort, not the returned slice, and the truncation order is surfaced so consumers do not
    # mistake a fastest-kill head for a representative random sample.
    returned = rows[:top]
    spec_name = query.get("spec_name") if isinstance(query, dict) else None
    truncated = len(rows) > top
    matching_participant_count = sum(
        len(row.get("matching_players") or [])
        for row in rows
        if isinstance(row, dict)
    )
    notes = [
        *sampled_spec_filter_notes(spec_name),
        (
            "rows are sampled kills that contained at least one participant of the requested spec; "
            "this is a participant cohort, not a spec ranking leaderboard"
        ),
    ]
    if truncated:
        notes.append(
            "returned kills are the fastest qualifying kills (ascending duration); slower kills in "
            "the cohort are excluded by --top, so the returned subset is not a representative random sample"
        )
    return {
        "ok": True,
        "provider": "warcraftlogs",
        "kind": "spec_filtered_kill_samples",
        "cohort": "spec_filtered_participant_kill_cohort",
        "ranking_basis": "spec_filtered_participant_kill_samples",
        "matching_rule": "sampled_zone_reports_filtered_to_kills_containing_the_requested_participant_spec",
        "query": query,
        "notes": notes,
        "freshness": sampled_cross_report_freshness(cache_ttl_seconds, transport_counts=transport_counts),
        "cache_provenance": sampled_cache_provenance(cache_ttl_seconds),
        "sample_scope": sampled_sample_scope(
            ranking_basis="spec_filtered_participant_kill_samples",
            query=query,
            returned=len(returned),
            excluded=max(0, len(rows) - len(returned)),
            truncated=truncated,
        ),
        "citations": sampled_cross_report_citations(rows, root_url=root_url),
        "sample": {
            **sample,
            "spec_name": spec_name,
            "sample_size": len(rows),
            "matching_participant_count": matching_participant_count,
            "filtered_kill_count": len(rows),
            "returned_kill_count": len(returned),
            "excluded_kill_count": max(0, len(rows) - len(returned)),
            "truncated": truncated,
            "truncation_order": "fastest_kill_duration_ascending",
            "stable_source_only": True,
        },
        "count": len(returned),
        "kills": returned,
    }


def kill_time_distribution_payload(
    *,
    rows: list[dict[str, Any]],
    sample: dict[str, Any],
    query: dict[str, Any],
    bucket_seconds: int,
    transport_counts: dict[str, int],
    cache_ttl_seconds: int | None = None,
    root_url: str = "https://www.warcraftlogs.com",
) -> dict[str, Any]:
    durations = [
        float(duration)
        for duration in (row.get("duration_seconds") for row in rows)
        if isinstance(duration, (int, float))
    ]
    return {
        "ok": True,
        "provider": "warcraftlogs",
        "kind": "kill_time_distribution",
        "ranking_basis": "sampled_kill_time_distribution",
        "matching_rule": "sampled_zone_reports_filtered_by_optional_boss_difficulty_spec_and_kill_time",
        "query": query,
        "notes": [
            *sampled_spec_filter_notes(query.get("spec_name") if isinstance(query, dict) else None),
            *sampled_dedupe_notes(sample),
        ],
        "freshness": sampled_cross_report_freshness(cache_ttl_seconds, transport_counts=transport_counts),
        "cache_provenance": sampled_cache_provenance(cache_ttl_seconds),
        "sample_scope": sampled_sample_scope(
            ranking_basis="sampled_kill_time_distribution",
            query=query,
            returned=len(rows),
            excluded=0,
            truncated=False,
        ),
        "citations": sampled_cross_report_citations(rows, root_url=root_url),
        "sample": {
            **sample,
            "filtered_kill_count": len(rows),
            "stable_source_only": True,
        },
        "distribution": {
            "unit": "seconds",
            "bucket_seconds": bucket_seconds,
            "statistics": numeric_summary(durations),
            "rows": duration_bucket_rows(durations, bucket_seconds=bucket_seconds),
        },
        "fastest_kills_preview": rows[: min(5, len(rows))],
    }

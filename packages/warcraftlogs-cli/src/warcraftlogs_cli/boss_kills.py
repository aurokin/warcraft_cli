"""Sampled boss-kill analytics across Warcraft Logs reports."""

from __future__ import annotations

from typing import Any

from warcraft_core.analytics import numeric_summary

from warcraftlogs_cli.client import ReportPlayerDetailsOptions, WarcraftLogsClient
from warcraftlogs_cli.report_payloads import fight_payload, report_brief_payload, report_payload, report_url
from warcraftlogs_cli.sampling_utils import (
    boss_matches,
    fight_duration_ms,
    normalize_match_text,
    report_is_finished,
    sampled_spec_filter_notes,
    utc_now_z,
)


def player_spec_matches(actor: dict[str, Any], spec_name: str) -> bool:
    wanted = normalize_match_text(spec_name)
    for spec in actor.get("specs") if isinstance(actor.get("specs"), list) else []:
        if not isinstance(spec, dict):
            continue
        if normalize_match_text(str(spec.get("spec") or "")) == wanted:
            return True
    return False


def _player_details_roles(report: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    details = report.get("playerDetails") if isinstance(report.get("playerDetails"), dict) else {}
    data = details.get("data") if isinstance(details.get("data"), dict) else {}
    role_data = data.get("playerDetails") if isinstance(data.get("playerDetails"), dict) else data
    roles: dict[str, list[dict[str, Any]]] = {}
    for role in ("tanks", "healers", "dps"):
        rows = role_data.get(role) if isinstance(role_data.get(role), list) else []
        roles[role] = [row for row in rows if isinstance(row, dict)]
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
                        for spec in (row.get("specs") if isinstance(row.get("specs"), list) else [])
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
    }


def sampled_cross_report_freshness() -> dict[str, Any]:
    return {
        "sampled_at": utc_now_z(),
        "cache_ttl_seconds": None,
    }


def sampled_cross_report_citations(rows: list[dict[str, Any]], *, limit: int = 20) -> dict[str, Any]:
    sample_reports: list[dict[str, Any]] = []
    seen: set[tuple[str, int | None]] = set()
    for row in rows:
        report = row.get("report") if isinstance(row.get("report"), dict) else {}
        fight = row.get("fight") if isinstance(row.get("fight"), dict) else {}
        report_code = report.get("code") if isinstance(report.get("code"), str) else None
        fight_id = fight.get("id") if isinstance(fight.get("id"), int) else None
        if report_code is None:
            continue
        key = (report_code, fight_id)
        if key in seen:
            continue
        seen.add(key)
        sample_reports.append(
            {
                "report_code": report_code,
                "fight_id": fight_id,
                "report_url": report_url(report_code, fight_id=fight_id),
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
        page_rows = pagination.get("data") if isinstance(pagination.get("data"), list) else []
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
        ttl_override=client._guild_ttl,
    )
    matching_players = matching_spec_players(details_report, spec_name=spec_name)
    if not matching_players:
        return None
    return matching_players


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
) -> tuple[list[dict[str, Any]], int, int]:
    boss_kills: list[dict[str, Any]] = []
    scanned_fight_count = 0
    matched_boss_kill_count = 0

    for report in finished_reports:
        fights_payload = client.report_fights(
            code=str(report.get("code") or ""),
            difficulty=difficulty,
            allow_unlisted=False,
            ttl_override=client._guild_ttl,
        )
        fights = fights_payload.get("fights") if isinstance(fights_payload.get("fights"), list) else []
        for fight in fights:
            if not isinstance(fight, dict):
                continue
            scanned_fight_count += 1
            if not fight.get("kill"):
                continue
            if not boss_matches(fight, boss_id=boss_id, boss_name=boss_name):
                continue
            if _kill_duration_in_bounds(fight, kill_time_min=kill_time_min, kill_time_max=kill_time_max) is None:
                continue
            matched_boss_kill_count += 1
            matching_players = _matching_players_for_fight(
                client,
                report=report,
                fight=fight,
                difficulty=difficulty,
                spec_name=spec_name,
            )
            if spec_name and matching_players is None:
                continue
            boss_kills.append(
                boss_kill_row(
                    report=report,
                    fight=fight,
                    matching_players=matching_players or [],
                )
            )

    boss_kills.sort(
        key=lambda row: (
            row.get("duration_ms") if isinstance(row.get("duration_ms"), (int, float)) else float("inf"),
            str((row.get("report") or {}).get("code") or ""),
            int((row.get("fight") or {}).get("id") or 0),
        )
    )
    return boss_kills, scanned_fight_count, matched_boss_kill_count


def collect_boss_kill_rows(
    *,
    client: WarcraftLogsClient,
    zone_id: int,
    boss_id: int | None,
    boss_name: str | None,
    difficulty: int | None,
    spec_name: str | None,
    kill_time_min: float | None,
    kill_time_max: float | None,
    report_pages: int,
    reports_per_page: int,
    start_time: float | None,
    end_time: float | None,
    guild_region: str | None,
    guild_realm: str | None,
    guild_name: str | None,
) -> dict[str, Any]:
    report_rows = _fetch_zone_report_rows(
        client,
        zone_id=zone_id,
        guild_region=guild_region,
        guild_realm=guild_realm,
        guild_name=guild_name,
        report_pages=report_pages,
        reports_per_page=reports_per_page,
        start_time=start_time,
        end_time=end_time,
    )
    live_reports = [row for row in report_rows if not report_is_finished(row)]
    finished_reports = [row for row in report_rows if report_is_finished(row)]
    boss_kills, scanned_fight_count, matched_boss_kill_count = _scan_finished_reports_for_boss_kills(
        client,
        finished_reports,
        boss_id=boss_id,
        boss_name=boss_name,
        difficulty=difficulty,
        spec_name=spec_name,
        kill_time_min=kill_time_min,
        kill_time_max=kill_time_max,
    )
    return {
        "rows": boss_kills,
        "sample": {
            "source_report_count": len(report_rows),
            "finished_report_count": len(finished_reports),
            "skipped_live_report_count": len(live_reports),
            "scanned_fight_count": scanned_fight_count,
            "matched_boss_kill_count": matched_boss_kill_count,
        },
    }


def boss_kills_payload(
    *,
    kind: str,
    rows: list[dict[str, Any]],
    sample: dict[str, Any],
    query: dict[str, Any],
    top: int,
) -> dict[str, Any]:
    returned = rows[:top]
    return {
        "ok": True,
        "provider": "warcraftlogs",
        "kind": kind,
        "ranking_basis": "sampled_fastest_kills",
        "matching_rule": "sampled_zone_reports_filtered_by_optional_boss_difficulty_spec_and_kill_time",
        "query": query,
        "notes": sampled_spec_filter_notes(query.get("spec_name") if isinstance(query, dict) else None),
        "freshness": sampled_cross_report_freshness(),
        "citations": sampled_cross_report_citations(rows),
        "sample": {
            **sample,
            "filtered_kill_count": len(rows),
            "returned_kill_count": len(returned),
            "excluded_kill_count": max(0, len(rows) - len(returned)),
            "truncated": len(rows) > top,
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
        "matching_rule": "sampled_zone_reports_filtered_by_optional_boss_difficulty_spec_and_kill_time",
        "query": query,
        "notes": sampled_spec_filter_notes(query.get("spec_name") if isinstance(query, dict) else None),
        "freshness": sampled_cross_report_freshness(),
        "citations": sampled_cross_report_citations(rows),
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

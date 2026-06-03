"""Shared helpers for sampled cross-report Warcraft Logs analytics."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any


def utc_now_z() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_match_text(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def report_is_finished(report: dict[str, Any]) -> bool:
    end_time = report.get("endTime")
    return isinstance(end_time, (int, float)) and float(end_time) > 0


def report_cache_provenance(
    report: dict[str, Any],
    *,
    finished_ttl: int | None,
    live_ttl: int | None,
    source: str,
) -> dict[str, Any]:
    """Describe the applied report cache TTL keyed on finish state.

    Finished reports (``endTime > 0``) are cached under ``finished_ttl``; live
    reports under the short ``live_ttl`` and are flagged ``live: True``. Either TTL
    may be ``None`` when caching is disabled, in which case ``cache_ttl_seconds`` is
    ``null`` (nothing is stored).
    """
    finished = report_is_finished(report)
    return {
        "finished": finished,
        "live": not finished,
        "cache_ttl_seconds": finished_ttl if finished else live_ttl,
        "source": source,
    }


def fight_duration_ms(fight: dict[str, Any]) -> float | None:
    start_time = fight.get("startTime")
    end_time = fight.get("endTime")
    if not isinstance(start_time, (int, float)) or not isinstance(end_time, (int, float)):
        return None
    duration = float(end_time) - float(start_time)
    if duration <= 0:
        return None
    return duration


def boss_matches(fight: dict[str, Any], *, boss_id: int | None, boss_name: str | None) -> bool:
    if boss_id is not None and fight.get("encounterID") != boss_id:
        return False
    if boss_name is None:
        return True
    actual = normalize_match_text(str(fight.get("name") or ""))
    query = normalize_match_text(boss_name)
    if not query:
        return True
    return query in actual or actual in query


def sampled_spec_filter_notes(spec_name: str | None) -> list[str]:
    if not spec_name:
        return []
    return [
        (
            "spec_name filters sampled fights by matching participant specs before aggregation; "
            "these results are not a global spec ranking leaderboard"
        )
    ]

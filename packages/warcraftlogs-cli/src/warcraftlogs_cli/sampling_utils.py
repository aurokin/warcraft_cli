"""Shared helpers for sampled cross-report Warcraft Logs analytics."""

from __future__ import annotations

import re
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any


def dict_at(source: Mapping[str, Any], key: str) -> dict[str, Any]:
    """Return ``source[key]`` when it is a JSON object, else an empty dict.

    Warcraft Logs GraphQL responses are loosely typed; these two accessors keep the
    "narrow or default" idiom in one place instead of repeating isinstance ladders.
    """
    value = source.get(key)
    return value if isinstance(value, dict) else {}


def list_at(source: Mapping[str, Any], key: str) -> list[Any]:
    """Return ``source[key]`` when it is a JSON array, else an empty list."""
    value = source.get(key)
    return value if isinstance(value, list) else []


def utc_now_z() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_match_text(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"[^a-z0-9]+", "", value.lower())


# A report that is still being logged has endTime > 0: it is the timestamp of the latest event
# (seen live on 2026-09-24, when reports mid-raid had endTime seconds before now). So a report counts
# as finished only once it has been quiet this long; raid breaks are well under two hours.
LIVE_REPORT_QUIET_MS = 2 * 60 * 60 * 1000


def report_is_finished(report: dict[str, Any]) -> bool:
    end_time = report.get("endTime")
    return isinstance(end_time, (int, float)) and 0 < end_time <= time.time() * 1000 - LIVE_REPORT_QUIET_MS


def report_cache_provenance(
    report: dict[str, Any],
    *,
    finished_ttl: int | None,
    live_ttl: int | None,
    source: str,
) -> dict[str, Any]:
    """Describe the applied report cache TTL keyed on finish state.

    Finished reports (see ``report_is_finished``) are cached under ``finished_ttl``; live
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


def sampled_spec_filter_notes(spec_name: str | None, sample: Mapping[str, Any]) -> list[str]:
    if not spec_name:
        return []
    notes = [
        (
            "spec_name filters sampled fights by matching participant specs before aggregation; "
            "these results are not a global spec ranking leaderboard"
        )
    ]
    classes = list_at(sample, "matched_spec_classes")
    if len(classes) > 1:
        notes.append(
            f"spec_name {spec_name!r} matched that spec on more than one class ({', '.join(classes)}); "
            f"name the class too, for example '{spec_name} {classes[0]}'"
        )
    return notes

from __future__ import annotations

import re
import shlex
from typing import Any

from warcraft_core.wow_normalization import normalize_name, normalize_region, primary_realm_slug


def _shell(value: str) -> str:
    return shlex.quote(value)


def _follow_up(kind: str, region: str, realm: str, name: str) -> dict[str, Any]:
    command = f"wowprogress {kind} {_shell(region)} {_shell(realm)} {_shell(name)}"
    return {
        "provider": "wowprogress",
        "kind": kind,
        "surface": kind,
        "command": command,
    }


def _normalized_identity(region: str, realm: str, name: str) -> dict[str, str]:
    return {
        "region": normalize_region(region),
        "realm": primary_realm_slug(realm),
        "name": normalize_name(name),
    }


def _guild_history_tier_row(row: dict[str, Any]) -> dict[str, Any]:
    progress = _progress_snapshot(str(row.get("progress") or ""))
    return {
        "tier_key": row.get("tier_key"),
        "raid": row.get("raid"),
        "current": row.get("current"),
        "progress": progress["summary"],
        "bosses_killed": progress["bosses_killed"],
        "boss_count": progress["boss_count"],
        "difficulty": progress["difficulty"],
        "final_ranks": row.get("progress_ranks"),
        "item_level_average": row.get("item_level_average"),
        "item_level_ranks": row.get("item_level_ranks"),
        "first_kill_at": row.get("first_kill_at"),
        "last_kill_at": row.get("last_kill_at"),
        "encounter_count": row.get("encounter_count"),
        "page_url": row.get("page_url"),
    }


def _guild_ranks_row(row: dict[str, Any]) -> dict[str, Any]:
    history = _guild_history_tier_row(row)
    return {
        "tier_key": history["tier_key"],
        "raid": history["raid"],
        "current": history["current"],
        "progress": history["progress"],
        "difficulty": history["difficulty"],
        "final_ranks": history["final_ranks"],
        "item_level_average": history["item_level_average"],
        "item_level_ranks": history["item_level_ranks"],
        "last_kill_at": history["last_kill_at"],
        "page_url": history["page_url"],
    }


def _progress_snapshot(value: str | None) -> dict[str, Any]:
    text = str(value or "").strip()
    match = re.match(r"(?P<killed>\d+)/(?P<total>\d+)(?:\s*\((?P<difficulty>[^)]+)\))?$", text)
    if match is None:
        return {
            "summary": text or None,
            "bosses_killed": None,
            "boss_count": None,
            "difficulty": None,
        }
    return {
        "summary": text,
        "bosses_killed": int(match.group("killed")),
        "boss_count": int(match.group("total")),
        "difficulty": match.group("difficulty"),
    }


def _leaderboard_entry_snapshot(entry: dict[str, Any]) -> dict[str, Any]:
    progress = _progress_snapshot(entry.get("progress"))
    return {
        "rank": entry.get("rank"),
        "guild_name": entry.get("guild_name"),
        "guild_url": entry.get("guild_url"),
        "realm": entry.get("realm"),
        "realm_url": entry.get("realm_url"),
        "progress": progress["summary"],
        "bosses_killed": progress["bosses_killed"],
        "boss_count": progress["boss_count"],
        "difficulty": progress["difficulty"],
    }


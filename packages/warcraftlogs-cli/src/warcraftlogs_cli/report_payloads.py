"""Normalized report and fight payload shapes for Warcraft Logs CLI output."""

from __future__ import annotations

from typing import Any

from warcraft_core.wow_normalization import normalize_region


def region_payload(region: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": region.get("id"),
        "name": region.get("name"),
        "compact_name": region.get("compactName"),
        "slug": normalize_region(str(region.get("slug", ""))),
    }


def server_payload(server: dict[str, Any]) -> dict[str, Any]:
    region = server.get("region") if isinstance(server.get("region"), dict) else {}
    subregion = server.get("subregion") if isinstance(server.get("subregion"), dict) else {}
    return {
        "id": server.get("id"),
        "name": server.get("name"),
        "normalized_name": server.get("normalizedName"),
        "slug": server.get("slug"),
        "region": region_payload(region) if region else None,
        "subregion": {
            "id": subregion.get("id"),
            "name": subregion.get("name"),
        }
        if subregion
        else None,
        "connected_realm_id": server.get("connectedRealmID"),
        "season_id": server.get("seasonID"),
    }


def archive_status_payload(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return {
        "is_archived": value.get("isArchived"),
        "is_accessible": value.get("isAccessible"),
        "archive_date": value.get("archiveDate"),
    }


def report_payload(report: dict[str, Any]) -> dict[str, Any]:
    zone = report.get("zone") if isinstance(report.get("zone"), dict) else {}
    guild = report.get("guild") if isinstance(report.get("guild"), dict) else {}
    return {
        "code": report.get("code"),
        "title": report.get("title"),
        "start_time": report.get("startTime"),
        "end_time": report.get("endTime"),
        "visibility": report.get("visibility"),
        "archive_status": archive_status_payload(report.get("archiveStatus")),
        "segments": report.get("segments"),
        "exported_segments": report.get("exportedSegments"),
        "zone": {"id": zone.get("id"), "name": zone.get("name")} if zone else None,
        "guild": {
            "id": guild.get("id"),
            "name": guild.get("name"),
            "server": server_payload(guild.get("server")) if isinstance(guild.get("server"), dict) else None,
        }
        if guild
        else None,
    }


def report_brief_payload(report: dict[str, Any]) -> dict[str, Any]:
    zone = report.get("zone") if isinstance(report.get("zone"), dict) else {}
    return {
        "code": report.get("code"),
        "title": report.get("title"),
        "zone": {"id": zone.get("id"), "name": zone.get("name")} if zone else None,
    }


def report_url(code: str | None, *, fight_id: int | None = None, root_url: str = "https://www.warcraftlogs.com") -> str | None:
    if not isinstance(code, str) or not code.strip():
        return None
    base = f"{root_url.rstrip('/')}/reports/{code}"
    if isinstance(fight_id, int):
        return f"{base}#fight={fight_id}"
    return base


def fight_payload(fight: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": fight.get("id"),
        "name": fight.get("name"),
        "encounter_id": fight.get("encounterID"),
        "difficulty": fight.get("difficulty"),
        "kill": fight.get("kill"),
        "complete_raid": fight.get("completeRaid"),
        "start_time": fight.get("startTime"),
        "end_time": fight.get("endTime"),
        "fight_percentage": fight.get("fightPercentage"),
        "boss_percentage": fight.get("bossPercentage"),
        "average_item_level": fight.get("averageItemLevel"),
        "size": fight.get("size"),
    }

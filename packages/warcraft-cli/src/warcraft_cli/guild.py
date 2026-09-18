from __future__ import annotations

from typing import Any

from warcraft_core.shapes import as_dict, as_list
from warcraft_core.wow_normalization import normalize_name, normalize_region, primary_realm_slug


def normalized_identity(region: str, realm: str, name: str) -> dict[str, str]:
    return {
        "region": normalize_region(region),
        "realm": primary_realm_slug(realm),
        "name": normalize_name(name),
    }


def first_dict(items: Any) -> dict[str, Any] | None:
    if not isinstance(items, list):
        return None
    for item in items:
        if isinstance(item, dict):
            return item
    return None


def raiderio_guild_summary(payload: dict[str, Any]) -> dict[str, Any]:
    guild = as_dict(payload.get("guild"))
    raiding = as_dict(payload.get("raiding"))
    active_raid = first_dict(raiding.get("progression"))
    active_rankings = first_dict(raiding.get("rankings"))
    return {
        "guild": guild,
        "active_raid": {
            "key": active_raid.get("raid_slug") if isinstance(active_raid, dict) else None,
            "name": active_raid.get("raid_slug") if isinstance(active_raid, dict) else None,
            "summary": active_raid.get("summary") if isinstance(active_raid, dict) else None,
            "boss_count": active_raid.get("total_bosses") if isinstance(active_raid, dict) else None,
            "rankings": active_rankings,
        },
        "roster": {
            "member_count": guild.get("member_count"),
            "preview": payload.get("roster_preview"),
        },
        "citations": payload.get("citations"),
    }


def guild_merge_payload(identity: dict[str, str], *, raiderio: dict[str, Any]) -> dict[str, Any]:
    if raiderio.get("status") != "ok":
        return {
            "ok": False,
            "error": {
                "code": "guild_not_found",
                "message": "Raider.IO did not return a guild snapshot for that query.",
            },
            "query": identity,
            "sources": {"raiderio": raiderio},
        }
    guild = as_dict(as_dict(raiderio.get("summary")).get("guild"))
    return {
        "ok": True,
        "provider": "warcraft",
        "kind": "guild_snapshot",
        "query": identity,
        "guild": {
            "name": guild.get("name"),
            "region": guild.get("region") or identity["region"],
            "realm": guild.get("realm") or identity["realm"],
            "faction": guild.get("faction"),
        },
        "sources": {"raiderio": raiderio},
    }


def guild_rank_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Join the raiderio guild payload's progression rows with its rankings rows on ``raid_slug``."""
    raiding = as_dict(payload.get("raiding"))
    rankings_by_slug = {
        row.get("raid_slug"): row for row in as_list(raiding.get("rankings")) if isinstance(row, dict)
    }
    rows: list[dict[str, Any]] = []
    for progression in as_list(raiding.get("progression")):
        if not isinstance(progression, dict):
            continue
        ranking = as_dict(rankings_by_slug.get(progression.get("raid_slug")))
        rows.append(
            {
                **progression,
                "ranks": {
                    "normal": ranking.get("normal"),
                    "heroic": ranking.get("heroic"),
                    "mythic": ranking.get("mythic"),
                },
            }
        )
    return rows

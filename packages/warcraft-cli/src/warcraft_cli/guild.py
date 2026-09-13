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


def wowprogress_guild_summary(payload: dict[str, Any]) -> dict[str, Any]:
    guild = as_dict(payload.get("guild"))
    progress = as_dict(payload.get("progress"))
    encounters = as_dict(payload.get("encounters"))
    encounter_items = as_list(encounters.get("items"))
    return {
        "guild": guild,
        "active_raid": {
            "key": progress.get("tier_key"),
            "name": progress.get("raid"),
            "summary": progress.get("summary"),
            "boss_count": encounters.get("count") if encounters.get("count") is not None else len(encounter_items),
            "rankings": progress.get("ranks"),
        },
        "item_level": payload.get("item_level"),
        "encounters": encounters,
        "citations": payload.get("citations"),
    }


def guild_conflicts(raiderio: dict[str, Any] | None, wowprogress: dict[str, Any] | None) -> dict[str, Any]:
    reasons: list[str] = []
    different_window = False
    if raiderio and wowprogress:
        ri_summary_payload = as_dict(raiderio.get("summary"))
        wp_summary_payload = as_dict(wowprogress.get("summary"))
        ri_active = as_dict(ri_summary_payload.get("active_raid"))
        wp_active = as_dict(wp_summary_payload.get("active_raid"))
        ri_bosses = ri_active.get("boss_count")
        wp_bosses = wp_active.get("boss_count")
        ri_summary = str(ri_active.get("summary") or "")
        wp_summary = str(wp_active.get("summary") or "")
        if ri_bosses != wp_bosses or (ri_summary and wp_summary and ri_summary != wp_summary):
            different_window = True
            reasons.append("providers_report_different_active_raid_windows")
    return {
        "different_tier_window_detected": different_window,
        "reasons": reasons,
    }


def guild_merge_payload(identity: dict[str, str], *, raiderio: dict[str, Any], wowprogress: dict[str, Any]) -> dict[str, Any]:
    ri_ok = raiderio.get("status") == "ok"
    wp_ok = wowprogress.get("status") == "ok"
    if not ri_ok and not wp_ok:
        return {
            "ok": False,
            "error": {
                "code": "guild_not_found",
                "message": "No guild provider returned a guild snapshot for that query.",
            },
            "query": identity,
            "sources": {
                "raiderio": raiderio,
                "wowprogress": wowprogress,
            },
        }
    preferred_guild = (
        ((wowprogress.get("summary") or {}).get("guild") if wp_ok else None)
        or ((raiderio.get("summary") or {}).get("guild") if ri_ok else None)
        or {}
    )
    return {
        "ok": True,
        "provider": "warcraft",
        "kind": "guild_snapshot",
        "query": identity,
        "guild": {
            "name": preferred_guild.get("name"),
            "region": preferred_guild.get("region") or identity["region"],
            "realm": preferred_guild.get("realm") or identity["realm"],
            "faction": preferred_guild.get("faction"),
        },
        "sources": {
            "raiderio": raiderio,
            "wowprogress": wowprogress,
        },
        "conflicts": guild_conflicts(
            raiderio if ri_ok else None,
            wowprogress if wp_ok else None,
        ),
    }

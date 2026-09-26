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


def raiderio_guild_summary(payload: dict[str, Any]) -> dict[str, Any]:
    """Guild identity, every raid Raider.IO reports (progression joined to ranks), roster, citations.

    Raider.IO returns progression and rankings sorted by raid slug and carries no start/end window,
    so there is no honest way to name one row "the active raid" from this payload alone; every raid
    is returned instead of guessing.
    """
    guild = as_dict(payload.get("guild"))
    raids = guild_rank_rows(payload)
    return {
        "guild": guild,
        "raid_count": len(raids),
        "raids": raids,
        "roster": {
            "member_count": guild.get("member_count"),
            "preview": payload.get("roster_preview"),
        },
        "citations": payload.get("citations"),
    }


def guild_merge_payload(identity: dict[str, str], *, raiderio: dict[str, Any]) -> dict[str, Any]:
    if raiderio.get("status") != "ok":
        # The source error is passed through verbatim so `error.code` and the exit code the command
        # derives from it agree; a synthesized code here would exit 5 while claiming "not found".
        error = as_dict(raiderio.get("error")) or {
            "code": "provider_command_failed",
            "message": "Raider.IO did not return a guild snapshot for that query.",
        }
        return {
            "ok": False,
            "error": error,
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

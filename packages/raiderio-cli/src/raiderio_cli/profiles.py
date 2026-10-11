"""Output-free profile retrieval and normalization shared by CLI and composites."""
from __future__ import annotations

from typing import Any
from urllib.parse import unquote, urlparse

from warcraft_core.envelope import Envelope
from warcraft_core.provider import ProviderError
from warcraft_core.shapes import as_dict, as_list
from warcraft_core.wow_normalization import primary_realm_slug

from raiderio_cli.client import FetchedJson, page_freshness
from raiderio_cli.identity import raiderio_class_spec_identity
from raiderio_cli.provider import open_client, raiderio_envelope, transport_errors


def _raid_progression_summary(progress: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raid_slug, row in sorted(progress.items()):
        if not isinstance(row, dict):
            continue
        rows.append(
            {
                "raid_slug": raid_slug,
                "summary": row.get("summary") or "",
                "total_bosses": row.get("total_bosses"),
                "normal_bosses_killed": row.get("normal_bosses_killed"),
                "heroic_bosses_killed": row.get("heroic_bosses_killed"),
                "mythic_bosses_killed": row.get("mythic_bosses_killed"),
            }
        )
    return rows


def _guild_rankings_summary(rankings: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raid_slug, row in sorted(rankings.items()):
        if not isinstance(row, dict):
            continue
        rows.append(
            {
                "raid_slug": raid_slug,
                "normal": row.get("normal"),
                "heroic": row.get("heroic"),
                "mythic": row.get("mythic"),
            }
        )
    return rows


def _recent_run_summary(row: dict[str, Any]) -> dict[str, Any]:
    """One profile run under the leaderboard's field names: the profile calls the timer ``par_time_ms``
    and the chest count ``num_keystone_upgrades``, and names the dungeon as a plain string.

    ``spec``/``role`` are what the character played in that run, which need not be its active spec.
    """
    spec = as_dict(row.get("spec"))
    return {
        "mythic_level": row.get("mythic_level"),
        "dungeon": row.get("dungeon"),
        "short_name": row.get("short_name"),
        "completed_at": row.get("completed_at"),
        "score": row.get("score"),
        "num_chests": row.get("num_keystone_upgrades"),
        "clear_time_ms": row.get("clear_time_ms"),
        "keystone_time_ms": row.get("par_time_ms"),
        "run_id": row.get("keystone_run_id"),
        "url": row.get("url"),
        "spec": spec.get("name"),
        "spec_slug": spec.get("slug"),
        "role": row.get("role") or spec.get("role"),
    }


def _profile_realm_slug(profile: dict[str, Any]) -> str | None:
    """Raider.IO's own realm slug, read from ``profile_url`` (``/characters/<region>/<realm>/<name>``).

    Profiles carry only the display name, and slugging that locally is wrong for native-script realms
    (Raider.IO slugs ``Ревущий фьорд`` as ``howling-fjord``), so the URL is the source of truth.
    """
    parts = [part for part in urlparse(str(profile.get("profile_url") or "")).path.split("/") if part]
    if len(parts) >= 4:
        return unquote(parts[2])
    realm = profile.get("realm")
    return primary_realm_slug(realm) if isinstance(realm, str) and realm.strip() else None


def _character_identity(profile: dict[str, Any]) -> dict[str, Any]:
    """Summarize the character's identity block, including the normalized class/spec sibling."""
    class_value = profile.get("class")
    spec_value = profile.get("active_spec_name")
    return {
        "name": profile.get("name"),
        "region": profile.get("region"),
        # `realm` is the slug and `realm_name` the display name, as on search rows.
        "realm": _profile_realm_slug(profile),
        "realm_name": profile.get("realm"),
        "race": profile.get("race"),
        "class_name": class_value,
        "active_spec_name": spec_value,
        "class_spec_identity": raiderio_class_spec_identity(class_value, spec_value, source="character_profile"),
        "faction": profile.get("faction"),
        "profile_url": profile.get("profile_url"),
        "thumbnail_url": profile.get("thumbnail_url"),
        # When Raider.IO last read this character from Blizzard: the age of every field here, which
        # `freshness.fetched_at` (the request time) is not.
        "last_crawled_at": profile.get("last_crawled_at"),
    }


def _character_mythic_plus(profile: dict[str, Any]) -> dict[str, Any]:
    """Summarize current-season Mythic+ score, ranks, the best run per dungeon, and the recent runs."""
    recent_runs = [_recent_run_summary(row) for row in as_list(profile.get("mythic_plus_recent_runs")) if isinstance(row, dict)]
    best_runs = [_recent_run_summary(row) for row in as_list(profile.get("mythic_plus_best_runs")) if isinstance(row, dict)]
    scores = as_list(profile.get("mythic_plus_scores_by_season"))
    current = as_dict(scores[0]) if scores else {}
    return {
        "season": current.get("season"),
        "current_score": as_dict(current.get("scores")).get("all"),
        "current_score_color": as_dict(as_dict(current.get("segments")).get("all")).get("color"),
        "ranks": profile.get("mythic_plus_ranks"),
        # One row per dungeon the character has completed this season (Raider.IO's best run there);
        # a dungeon missing here has no completed run. `num_chests` 0 means it was not timed.
        "best_run_count": len(best_runs),
        "best_runs": best_runs,
        "recent_run_count": len(recent_runs),
        "recent_runs": recent_runs,
    }


def _character_payload(fetched: FetchedJson, *, cache_ttl_seconds: int) -> dict[str, Any]:
    """Build the ``raiderio character`` payload from a Raider.IO character profile."""
    profile = fetched.payload
    guild = as_dict(profile.get("guild"))
    raid_rows = _raid_progression_summary(as_dict(profile.get("raid_progression")))
    identity = _character_identity(profile)
    guild_realm = guild.get("realm")
    return {
        "character": identity,
        # Raider.IO's guild block carries no region (a guild is in its member's region) and no realm
        # slug: a guild on the character's realm takes its slug, any other is slugged from its name.
        "guild": {
            "name": guild.get("name"),
            "realm": identity["realm"]
            if guild_realm == profile.get("realm")
            else (primary_realm_slug(guild_realm) if isinstance(guild_realm, str) else None),
            "realm_name": guild_realm,
            "region": profile.get("region"),
        }
        if guild
        else None,
        "mythic_plus": _character_mythic_plus(profile),
        "raiding": {
            "raid_count": len(raid_rows),
            "progression": raid_rows,
        },
        "freshness": page_freshness(fetched, cache_ttl_seconds=cache_ttl_seconds),
        "citations": {
            "profile": profile.get("profile_url"),
        },
    }


# Raider.IO's guild roster roles, renamed to the tank/healer/dps every other row and filter here uses.
ROSTER_ROLE_NAMES: dict[str | None, str] = {"TANK": "tank", "HEALING": "healer", "DPS": "dps"}


def _guild_roster_preview(members: list[Any], *, limit: int) -> list[dict[str, Any]]:
    """The ``limit`` highest-ranked roster entries (guild master first) with class/spec identity.

    Raider.IO lists members in no useful order, so they are sorted by guild rank before the cut.
    """
    rows = sorted((as_dict(row) for row in members), key=lambda row: rank if isinstance(rank := row.get("rank"), int) else float("inf"))
    preview: list[dict[str, Any]] = []
    for row in rows[:limit]:
        character = as_dict(row.get("character"))
        preview.append(
            {
                "name": character.get("name"),
                # `realm` is the slug and `realm_name` the display name, as on the guild itself.
                "realm": _profile_realm_slug(character),
                "realm_name": character.get("realm"),
                "rank": row.get("rank"),
                "class_name": character.get("class"),
                "active_spec_name": character.get("active_spec_name"),
                "active_spec_role": ROSTER_ROLE_NAMES.get(role := character.get("active_spec_role"), role),
                "class_spec_identity": raiderio_class_spec_identity(
                    character.get("class"),
                    character.get("active_spec_name"),
                    source="guild_roster_preview",
                ),
            }
        )
    return preview


def _guild_payload(fetched: FetchedJson, *, cache_ttl_seconds: int, roster_limit: int = 10) -> dict[str, Any]:
    """Build the ``raiderio guild`` payload from a Raider.IO guild profile."""
    profile = fetched.payload
    members = as_list(profile.get("members"))
    raid_progression = _raid_progression_summary(as_dict(profile.get("raid_progression")))
    raid_rankings = _guild_rankings_summary(as_dict(profile.get("raid_rankings")))
    return {
        "guild": {
            "name": profile.get("name"),
            "region": profile.get("region"),
            "realm": _profile_realm_slug(profile),
            "realm_name": profile.get("realm"),
            "faction": profile.get("faction"),
            "profile_url": profile.get("profile_url"),
            # Members Raider.IO tracks, which can be fewer than the in-game roster.
            "member_count": len(members),
            "last_crawled_at": profile.get("last_crawled_at"),
        },
        "raiding": {
            "raid_count": len(raid_progression),
            "progression": raid_progression,
            "rankings": raid_rankings,
        },
        "roster_preview": _guild_roster_preview(members, limit=roster_limit),
        "roster_truncated": len(members) > roster_limit,
        "freshness": page_freshness(fetched, cache_ttl_seconds=cache_ttl_seconds),
        "citations": {
            "profile": profile.get("profile_url"),
        },
    }


def character_profile(region: str, realm: str, name: str) -> Envelope:
    """Read one exact character with the same evidence as the character command."""
    with transport_errors(), open_client() as client:
        fetched = client.character_profile(region=region, realm=realm, name=name)
        payload = _character_payload(fetched, cache_ttl_seconds=client.character_profile_ttl_seconds)
    return raiderio_envelope(command="character", kind="character_profile", payload=payload)


def guild_profile(region: str, realm: str, name: str, *, roster_limit: int = 10) -> Envelope:
    """Read one exact guild and a bounded, rank-ordered roster preview."""
    if not 0 <= roster_limit <= 1000:
        raise ProviderError("invalid_query", "Roster limit must be between 0 and 1000.")
    with transport_errors(), open_client() as client:
        fetched = client.guild_profile(region=region, realm=realm, name=name)
        payload = _guild_payload(fetched, cache_ttl_seconds=client.guild_profile_ttl_seconds, roster_limit=roster_limit)
    return raiderio_envelope(command="guild", kind="guild_profile", payload=payload)

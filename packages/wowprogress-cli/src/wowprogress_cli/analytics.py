from __future__ import annotations

import re
from datetime import UTC, datetime
from statistics import median
from typing import Any

from warcraft_core.analytics import (
    categorical_distribution as _categorical_distribution,
)
from warcraft_core.analytics import (
    count_map as _count_map,
)
from warcraft_core.analytics import (
    distribution_response as _distribution_response,
)
from warcraft_core.analytics import (
    numeric_distribution as _numeric_distribution,
)
from warcraft_core.analytics import (
    numeric_summary as _numeric_summary,
)

from wowprogress_cli.client import WowProgressClient
from wowprogress_cli.identity import _leaderboard_entry_snapshot, _progress_snapshot


def _sample_pve_leaderboard(
    client: WowProgressClient,
    *,
    region: str,
    realm: str | None,
    limit: int,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    payload = client.fetch_pve_leaderboard(region=region, realm=realm, limit=limit)
    entries = payload.get("entries") if isinstance(payload.get("entries"), list) else []
    snapshots = [_leaderboard_entry_snapshot(entry) for entry in entries if isinstance(entry, dict)]
    leaderboard = payload.get("leaderboard") if isinstance(payload.get("leaderboard"), dict) else {}
    meta = {
        "sampled_at": datetime.now(UTC).isoformat(),
        "cache_ttl_seconds": client.pve_leaderboard_ttl_seconds,
        "page_url": str((payload.get("citations") or {}).get("page") or leaderboard.get("page_url") or ""),
        "active_raid": leaderboard.get("active_raid"),
        "region": leaderboard.get("region") or region.lower(),
        "realm": leaderboard.get("realm"),
        "title": leaderboard.get("title"),
        "requested_limit": limit,
        "leaderboard_entry_count": len(snapshots),
    }
    return snapshots, meta, leaderboard


def _guild_profile_snapshot(*, leaderboard_entry: dict[str, Any], guild_payload: dict[str, Any]) -> dict[str, Any]:
    guild = guild_payload.get("guild") if isinstance(guild_payload.get("guild"), dict) else {}
    progress = guild_payload.get("progress") if isinstance(guild_payload.get("progress"), dict) else {}
    item_level = guild_payload.get("item_level") if isinstance(guild_payload.get("item_level"), dict) else {}
    encounters = guild_payload.get("encounters") if isinstance(guild_payload.get("encounters"), dict) else {}
    items = encounters.get("items") if isinstance(encounters.get("items"), list) else []
    return {
        "leaderboard_rank": leaderboard_entry.get("rank"),
        "guild_name": guild.get("name") or leaderboard_entry.get("guild_name"),
        "region": guild.get("region"),
        "realm": guild.get("realm") or leaderboard_entry.get("realm"),
        "faction": guild.get("faction"),
        "profile_url": guild.get("page_url") or leaderboard_entry.get("guild_url"),
        "armory_url": guild.get("armory_url"),
        "progress": progress.get("summary"),
        "bosses_killed": _progress_snapshot(progress.get("summary")).get("bosses_killed"),
        "boss_count": _progress_snapshot(progress.get("summary")).get("boss_count"),
        "difficulty": _progress_snapshot(progress.get("summary")).get("difficulty"),
        "progress_ranks": progress.get("ranks"),
        "item_level_average": item_level.get("average"),
        "item_level_group_size": item_level.get("group_size"),
        "item_level_ranks": item_level.get("ranks"),
        "encounter_count": encounters.get("count"),
        "encounters": [
            {
                "encounter": item.get("encounter"),
                "difficulty": item.get("difficulty"),
                "world_rank": item.get("world_rank"),
                "region_rank": item.get("region_rank"),
                "realm_rank": item.get("realm_rank"),
                "first_kill_at": item.get("first_kill_at"),
            }
            for item in items[:10]
            if isinstance(item, dict)
        ],
    }


def _sampled_pve_guild_profiles(
    client: WowProgressClient,
    *,
    region: str,
    realm: str | None,
    limit: int,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    entries, meta, leaderboard = _sample_pve_leaderboard(client, region=region, realm=realm, limit=limit)
    guild_profiles: list[dict[str, Any]] = []
    skipped_missing_profile_url = 0
    for entry in entries:
        guild_url = str(entry.get("guild_url") or "").strip()
        if not guild_url:
            skipped_missing_profile_url += 1
            continue
        payload = client.fetch_guild_page_url(guild_url)
        guild_profiles.append(_guild_profile_snapshot(leaderboard_entry=entry, guild_payload=payload))
    meta = {
        **meta,
        "guild_profile_count": len(guild_profiles),
        "skipped_missing_profile_url": skipped_missing_profile_url,
    }
    return guild_profiles, meta, leaderboard


def _sample_summary(entries: list[dict[str, Any]], *, meta: dict[str, Any]) -> dict[str, Any]:
    rank_values = [int(entry["rank"]) for entry in entries if isinstance(entry.get("rank"), int)]
    killed_values = [int(entry["bosses_killed"]) for entry in entries if isinstance(entry.get("bosses_killed"), int)]
    return {
        "sampled_at": meta["sampled_at"],
        "entry_count": len(entries),
        "sampling": {
            "requested_limit": meta.get("requested_limit"),
            "returned_entry_count": len(entries),
            "source_scope": "top leaderboard rows fetched from the requested WowProgress leaderboard page",
        },
        "active_raid": meta.get("active_raid"),
        "unique_realms": sorted({str(entry.get("realm") or "") for entry in entries if str(entry.get("realm") or "").strip()}),
        "difficulty_counts": _count_map([str(entry.get("difficulty") or "unknown") for entry in entries]),
        "rank": _numeric_summary(rank_values),
        "bosses_killed": _numeric_summary(killed_values),
    }


def _guild_profile_world_ranks(entries: list[dict[str, Any]]) -> list[int]:
    return [
        int(str((entry.get("progress_ranks") or {}).get("world")).replace(",", ""))
        for entry in entries
        if isinstance(entry.get("progress_ranks"), dict)
        and str((entry.get("progress_ranks") or {}).get("world") or "").replace(",", "").isdigit()
    ]


def _guild_profile_sample_summary(entries: list[dict[str, Any]], *, meta: dict[str, Any],
                                  filtering: dict[str, Any] | None = None) -> dict[str, Any]:
    item_levels = [float(entry["item_level_average"]) for entry in entries if isinstance(entry.get("item_level_average"), (int, float))]
    return {
        "sampled_at": meta["sampled_at"],
        "guild_profile_count": len(entries),
        "sampling": {
            "requested_limit": meta.get("requested_limit"),
            "source_leaderboard_entry_count": meta.get("leaderboard_entry_count"),
            "returned_guild_profile_count": len(entries),
            "skipped_missing_profile_url": meta.get("skipped_missing_profile_url", 0),
            "source_scope": "top leaderboard rows enriched with direct WowProgress guild pages when a profile URL is present",
        },
        "filtering": filtering,
        "active_raid": meta.get("active_raid"),
        "faction_counts": _count_map([str(entry.get("faction") or "unknown") for entry in entries]),
        "progress_counts": _count_map([str(entry.get("progress") or "unknown") for entry in entries]),
        "difficulty_counts": _count_map([str(entry.get("difficulty") or "unknown") for entry in entries]),
        "item_level_average": _numeric_summary(item_levels),
        "world_progress_rank": _numeric_summary(_guild_profile_world_ranks(entries)),
    }


def _normalize_slug_filters(values: list[str] | None) -> list[str]:
    normalized: list[str] = []
    for value in values or []:
        parts = [part for part in re.split(r"[^a-z0-9]+", value.strip().lower()) if part]
        cleaned = "-".join(parts)
        if cleaned and cleaned not in normalized:
            normalized.append(cleaned)
    return normalized


def _metric_within_bounds(value: Any, *, minimum: float | None, maximum: float | None) -> bool:
    if not isinstance(value, (int, float)):
        return True
    numeric_value = float(value)
    if minimum is not None and numeric_value < minimum:
        return False
    return not (maximum is not None and numeric_value > maximum)


def _normalized_encounter_values(entry: dict[str, Any]) -> set[str]:
    return {
        "-".join(part for part in re.split(r"[^a-z0-9]+", str(row.get("encounter") or "").strip().lower()) if part)
        for row in (entry.get("encounters") if isinstance(entry.get("encounters"), list) else [])
        if isinstance(row, dict)
    }


def _freshness_payload(meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "sampled_at": meta["sampled_at"],
        "cache_ttl_seconds": meta["cache_ttl_seconds"],
    }


def _citations_payload(meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "leaderboard_page": meta["page_url"],
    }


def _world_rank_value(entry: dict[str, Any]) -> int | None:
    progress_ranks = entry.get("progress_ranks") if isinstance(entry.get("progress_ranks"), dict) else {}
    raw = progress_ranks.get("world")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        cleaned = raw.replace(",", "")
        if cleaned.isdigit():
            return int(cleaned)
    return None


def _guild_profile_matches_filters(
    entry: dict[str, Any],
    *,
    faction: list[str],
    difficulty: list[str],
    world_rank_min: int | None,
    world_rank_max: int | None,
    item_level_min: float | None,
    item_level_max: float | None,
    encounter: list[str],
) -> bool:
    slug_filters = (
        ("faction", faction),
        ("difficulty", difficulty),
    )
    for field, expected in slug_filters:
        value = str(entry.get(field) or "").strip().lower().replace(" ", "-")
        if expected and value not in expected:
            return False

    if not _metric_within_bounds(_world_rank_value(entry), minimum=world_rank_min, maximum=world_rank_max):
        return False
    if not _metric_within_bounds(entry.get("item_level_average"), minimum=item_level_min, maximum=item_level_max):
        return False
    return not (encounter and not any(value in _normalized_encounter_values(entry) for value in encounter))


def _filter_guild_profiles(
    entries: list[dict[str, Any]],
    *,
    faction: list[str] | None,
    difficulty: list[str] | None,
    world_rank_min: int | None,
    world_rank_max: int | None,
    item_level_min: float | None,
    item_level_max: float | None,
    encounter: list[str] | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    normalized_faction = _normalize_slug_filters(faction)
    normalized_difficulty = _normalize_slug_filters(difficulty)
    normalized_encounter = _normalize_slug_filters(encounter)
    filtered = [
        entry
        for entry in entries
        if _guild_profile_matches_filters(
            entry,
            faction=normalized_faction,
            difficulty=normalized_difficulty,
            world_rank_min=world_rank_min,
            world_rank_max=world_rank_max,
            item_level_min=item_level_min,
            item_level_max=item_level_max,
            encounter=normalized_encounter,
        )
    ]
    return filtered, {
        "faction": normalized_faction,
        "difficulty": normalized_difficulty,
        "world_rank_min": world_rank_min,
        "world_rank_max": world_rank_max,
        "item_level_min": item_level_min,
        "item_level_max": item_level_max,
        "encounter": normalized_encounter,
        "source_profile_count": len(entries),
        "returned_profile_count": len(filtered),
        "excluded_profile_count": len(entries) - len(filtered),
    }


def _distribution_payload(metric: str, entries: list[dict[str, Any]], *, meta: dict[str, Any], query: dict[str, Any]) -> dict[str, Any]:
    sample = _sample_summary(entries, meta=meta)
    if metric == "rank":
        distribution = _numeric_distribution(
            [int(entry["rank"]) for entry in entries if isinstance(entry.get("rank"), int)],
            unit="entries",
        )
    else:
        field = {
            "realm": "realm",
            "difficulty": "difficulty",
            "bosses_killed": "bosses_killed",
        }.get(metric, "progress")
        values = [str(entry.get(field) or "unknown") for entry in entries]
        distribution = _categorical_distribution(values, unit="entries")
    return _distribution_response(
        provider="wowprogress",
        kind="pve_leaderboard_distribution",
        metric=metric,
        query=query,
        sample=sample,
        distribution=distribution,
        freshness=_freshness_payload(meta),
        citations=_citations_payload(meta),
    )


def _guild_profile_categorical_distribution_values(metric: str, entries: list[dict[str, Any]]) -> tuple[list[str], str] | None:
    if metric in {"faction", "progress"}:
        return [str(entry.get(metric) or "unknown") for entry in entries], "guild_profiles"
    if metric == "encounter":
        return [
            str(encounter.get("encounter") or "unknown")
            for entry in entries
            for encounter in (entry.get("encounters") if isinstance(entry.get("encounters"), list) else [])
            if isinstance(encounter, dict)
        ], "encounters"
    return None


def _guild_profile_numeric_distribution_values(metric: str, entries: list[dict[str, Any]]) -> tuple[list[int] | list[float], str] | None:
    if metric == "world_rank":
        return _guild_profile_world_ranks(entries), "guild_profiles"
    return [
        float(entry["item_level_average"])
        for entry in entries
        if isinstance(entry.get("item_level_average"), (int, float))
    ], "guild_profiles"


def _guild_profile_distribution_values(metric: str, entries: list[dict[str, Any]]) -> tuple[list[str] | list[int] | list[float], str, bool]:
    categorical = _guild_profile_categorical_distribution_values(metric, entries)
    if categorical is not None:
        values, unit = categorical
        return values, unit, False
    numeric = _guild_profile_numeric_distribution_values(metric, entries)
    values, unit = numeric if numeric is not None else ([], "guild_profiles")
    return values, unit, True


def _guild_profile_distribution_payload(
    metric: str,
    entries: list[dict[str, Any]],
    *,
    meta: dict[str, Any],
    query: dict[str, Any],
    filtering: dict[str, Any] | None = None,
) -> dict[str, Any]:
    sample = _guild_profile_sample_summary(entries, meta=meta, filtering=filtering)
    values, unit, numeric = _guild_profile_distribution_values(metric, entries)
    distribution = (
        _numeric_distribution(values, unit=unit)  # type: ignore[arg-type]
        if numeric
        else _categorical_distribution(values, unit=unit)  # type: ignore[arg-type]
    )
    return _distribution_response(
        provider="wowprogress",
        kind="pve_guild_profiles_distribution",
        metric=metric,
        query=query,
        sample=sample,
        distribution=distribution,
        freshness=_freshness_payload(meta),
        citations=_citations_payload(meta),
    )


def _nearest_guild_profile_rows(metric: str, target: float, entries: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for entry in entries:
        if metric == "item_level_average":
            raw_value = entry.get("item_level_average")
        else:
            raw_value = (entry.get("progress_ranks") or {}).get("world") if isinstance(entry.get("progress_ranks"), dict) else None
            if isinstance(raw_value, str):
                raw_value = int(raw_value.replace(",", "")) if raw_value.replace(",", "").isdigit() else None
        if not isinstance(raw_value, (int, float)):
            continue
        rows.append(
            {
                "value": float(raw_value),
                "distance": round(abs(float(raw_value) - target), 3),
                "entry": entry,
            }
        )
    rows.sort(key=lambda row: (float(row["distance"]), str((row["entry"] or {}).get("guild_name") or "")))
    return rows[:limit]


def _guild_profile_threshold_estimate(metric: str, nearest: list[dict[str, Any]]) -> tuple[str, list[int] | list[float], str]:
    if metric == "item_level_average":
        return (
            "world_rank",
            [
                int(str((row["entry"].get("progress_ranks") or {}).get("world")).replace(",", ""))
                for row in nearest
                if isinstance(row["entry"].get("progress_ranks"), dict)
                and str((row["entry"].get("progress_ranks") or {}).get("world") or "").replace(",", "").isdigit()
            ],
            "This estimates sampled world-progress ranks near a target item-level average for leaderboard guilds in the active raid.",
        )
    return (
        "item_level_average",
        [float(row["entry"]["item_level_average"]) for row in nearest if isinstance(row["entry"].get("item_level_average"), (int, float))],
        "This estimates sampled guild item-level averages near a target world-progress rank for the active raid.",
    )


def _guild_profile_threshold_payload(
    metric: str,
    target: float,
    entries: list[dict[str, Any]],
    *,
    meta: dict[str, Any],
    query: dict[str, Any],
    nearest_limit: int,
    filtering: dict[str, Any] | None = None,
) -> dict[str, Any]:
    nearest = _nearest_guild_profile_rows(metric, target, entries, limit=nearest_limit)
    estimate_metric, estimate_values, caveat = _guild_profile_threshold_estimate(metric, nearest)
    estimate = None
    if estimate_values:
        sorted_values = sorted(estimate_values)
        estimate = {
            "metric": estimate_metric,
            "count": len(sorted_values),
            "min": sorted_values[0],
            "max": sorted_values[-1],
            "average": round(sum(sorted_values) / len(sorted_values), 2),
            "median": median(sorted_values),
        }
    return {
        "provider": "wowprogress",
        "kind": "pve_guild_profiles_threshold",
        "metric": metric,
        "target": target,
        "query": query,
        "sample": _guild_profile_sample_summary(entries, meta=meta, filtering=filtering),
        "threshold": {
            "nearest_match_count": len(nearest),
            "nearest_matches": [
                {
                    "value": row["value"],
                    "distance": row["distance"],
                    "entry": row["entry"],
                }
                for row in nearest
            ],
            "estimate": estimate,
            "caveat": caveat,
        },
        "freshness": _freshness_payload(meta),
        "citations": _citations_payload(meta),
    }


def _nearest_threshold_rows(metric: str, target: float, entries: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for entry in entries:
        raw_value = entry.get("rank") if metric == "rank" else entry.get("bosses_killed")
        if not isinstance(raw_value, (int, float)):
            continue
        rows.append(
            {
                "value": float(raw_value),
                "distance": round(abs(float(raw_value) - target), 3),
                "entry": entry,
            }
        )
    rows.sort(
        key=lambda row: (
            float(row["distance"]),
            float(row["value"]),
            str((row["entry"] or {}).get("guild_name") or ""),
        )
    )
    return rows[:limit]


def _threshold_payload(metric: str, target: float, entries: list[dict[str, Any]], *,
                       meta: dict[str, Any], query: dict[str, Any], nearest_limit: int) -> dict[str, Any]:
    nearest = _nearest_threshold_rows(metric, target, entries, limit=nearest_limit)
    if metric == "rank":
        estimate_metric = "bosses_killed"
        estimate_values = [int(row["entry"]["bosses_killed"]) for row in nearest if isinstance(row["entry"].get("bosses_killed"), int)]
        caveat = (
            "This estimates raid-progression states near a sampled WowProgress leaderboard rank, "
            "not a universal guild-performance threshold."
        )
    else:
        estimate_metric = "rank"
        estimate_values = [int(row["entry"]["rank"]) for row in nearest if isinstance(row["entry"].get("rank"), int)]
        caveat = "This estimates leaderboard-rank ranges near a sampled boss-kill count for the active raid."
    estimate = None
    if estimate_values:
        sorted_values = sorted(estimate_values)
        estimate = {
            "metric": estimate_metric,
            "count": len(sorted_values),
            "min": sorted_values[0],
            "max": sorted_values[-1],
            "average": round(sum(sorted_values) / len(sorted_values), 2),
            "median": median(sorted_values),
        }
    return {
        "provider": "wowprogress",
        "kind": "pve_leaderboard_threshold",
        "metric": metric,
        "target": target,
        "query": query,
        "sample": _sample_summary(entries, meta=meta),
        "threshold": {
            "nearest_match_count": len(nearest),
            "nearest_matches": [
                {
                    "value": row["value"],
                    "distance": row["distance"],
                    "entry": row["entry"],
                }
                for row in nearest
            ],
            "estimate": estimate,
            "caveat": caveat,
        },
        "freshness": _freshness_payload(meta),
        "citations": _citations_payload(meta),
    }


def _load_pve_leaderboard_sample(
    client: WowProgressClient,
    *,
    region: str,
    realm: str | None,
    limit: int,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any], dict[str, Any]]:
    entries, meta, leaderboard = _sample_pve_leaderboard(client, region=region, realm=realm, limit=limit)
    query = {
        "region": region.lower(),
        "realm": realm.lower() if realm else None,
        "limit": limit,
    }
    return entries, meta, leaderboard, query


def _load_pve_guild_profile_sample(
    client: WowProgressClient,
    *,
    region: str,
    realm: str | None,
    limit: int,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any], dict[str, Any]]:
    entries, meta, leaderboard = _sampled_pve_guild_profiles(client, region=region, realm=realm, limit=limit)
    query = {
        "region": region.lower(),
        "realm": realm.lower() if realm else None,
        "limit": limit,
    }
    return entries, meta, leaderboard, query

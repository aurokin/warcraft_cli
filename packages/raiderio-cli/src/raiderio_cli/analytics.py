"""Sampled Raider.IO Mythic+ analytics: leaderboard sampling, filtering, summaries, distributions.

The Typer commands in ``main`` parse flags into :class:`SampleRequest` / :class:`RunFilters` and then
call into this module; nothing here touches Typer, prints, or raises ``typer.Exit``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from statistics import median
from typing import Any

from warcraft_core.analytics import (
    categorical_distribution,
    count_map,
    distribution_response,
    numeric_distribution,
    numeric_summary,
)
from warcraft_core.provider import ProviderError
from warcraft_core.shapes import as_dict, as_list

from raiderio_cli.client import RaiderIOClient
from raiderio_cli.identity import raiderio_class_spec_identity


@dataclass(frozen=True, slots=True)
class SampleRequest:
    """Scope and pagination inputs shared by every sampled Mythic+ command."""

    season: str
    region: str
    dungeon: str
    affixes: str
    page: int
    pages: int = 1
    limit: int = 100

    @property
    def season_param(self) -> str | None:
        return resolve_season_input(self.season)


@dataclass(frozen=True, slots=True)
class RunFilters:
    """Normalized ``--level-*``/``--score-*``/``--contains-*`` filters applied to a sampled run set."""

    level_min: int | None = None
    level_max: int | None = None
    score_min: float | None = None
    score_max: float | None = None
    contains_role: tuple[str, ...] = ()
    contains_class: tuple[str, ...] = ()
    contains_spec: tuple[str, ...] = ()
    player_region: tuple[str, ...] = ()

    def as_query(self) -> dict[str, Any]:
        return {
            "level_min": self.level_min,
            "level_max": self.level_max,
            "score_min": self.score_min,
            "score_max": self.score_max,
            "contains_role": list(self.contains_role),
            "contains_class": list(self.contains_class),
            "contains_spec": list(self.contains_spec),
            "player_region": list(self.player_region),
        }


def run_filters(
    *,
    level_min: int | None,
    level_max: int | None,
    score_min: float | None,
    score_max: float | None,
    contains_role: list[str] | None,
    contains_class: list[str] | None,
    contains_spec: list[str] | None,
    player_region: list[str] | None,
) -> RunFilters:
    return RunFilters(
        level_min=level_min,
        level_max=level_max,
        score_min=score_min,
        score_max=score_max,
        contains_role=tuple(_normalize_filter_values(contains_role)),
        contains_class=tuple(_normalize_filter_values(contains_class)),
        contains_spec=tuple(_normalize_filter_values(contains_spec)),
        player_region=tuple(_normalize_filter_values(player_region)),
    )


def analytics_query(request: SampleRequest, filters: RunFilters, *, meta: dict[str, Any], **extra: Any) -> dict[str, Any]:
    """Echo the resolved sampling scope so an agent can reproduce the exact request."""
    return {
        "season": meta.get("season") or request.season or None,
        "resolved_season": meta.get("season") or request.season_param,
        "region": request.region,
        "dungeon": request.dungeon,
        "affixes": request.affixes or None,
        "page": request.page,
        "pages": request.pages,
        "limit": request.limit,
        **extra,
        "filters": filters.as_query(),
    }


def ranking_roster_entry(entry: dict[str, Any]) -> dict[str, Any]:
    character = as_dict(entry.get("character"))
    realm = as_dict(character.get("realm"))
    region = as_dict(character.get("region"))
    class_row = as_dict(character.get("class"))
    spec_row = as_dict(character.get("spec"))
    path = character.get("path")
    return {
        "name": character.get("name"),
        "realm": realm.get("slug"),
        "region": region.get("slug"),
        "class_name": class_row.get("name"),
        "class_slug": class_row.get("slug"),
        "spec_name": spec_row.get("name"),
        "spec_slug": spec_row.get("slug"),
        "class_spec_identity": raiderio_class_spec_identity(
            class_row.get("name"), spec_row.get("name"), source="ranking_roster"
        ),
        "profile_url": f"https://raider.io{path}" if isinstance(path, str) and path.startswith("/") else None,
        "role": entry.get("role"),
    }


def ranking_run_summary(row: dict[str, Any]) -> dict[str, Any]:
    run = as_dict(row.get("run"))
    dungeon = as_dict(run.get("dungeon"))
    roster = as_list(run.get("roster"))
    return {
        "rank": row.get("rank"),
        "score": row.get("score"),
        "mythic_level": run.get("mythic_level"),
        "dungeon": dungeon.get("name"),
        "dungeon_slug": dungeon.get("slug"),
        "completed_at": run.get("completed_at"),
        "affixes": [affix.get("slug") for affix in as_list(run.get("weekly_modifiers")) if isinstance(affix, dict)],
        "roster": [ranking_roster_entry(entry) for entry in roster[:5] if isinstance(entry, dict)],
    }


def _run_snapshot(row: dict[str, Any]) -> dict[str, Any]:
    run = as_dict(row.get("run"))
    snapshot = ranking_run_summary(row)
    snapshot["run_id"] = run.get("keystone_run_id") or run.get("logged_run_id") or run.get("keystone_team_id")
    snapshot["season"] = run.get("season")
    snapshot["clear_time_ms"] = run.get("clear_time_ms")
    snapshot["keystone_time_ms"] = run.get("keystone_time_ms")
    snapshot["num_chests"] = run.get("num_chests")
    return snapshot


# Raider.IO returns a fixed 20 runs per page for /mythic-plus/runs. Used to derive how many
# pages a leaderboard must fetch to satisfy --limit; the emitted counts state the actual result
# so a short provider response is never a silent cap.
_RAIDERIO_RUNS_PAGE_SIZE = 20
_RAIDERIO_RUNS_MAX_PAGES = 10


def leaderboard_pages_for_limit(limit: int) -> int:
    pages = -(-limit // _RAIDERIO_RUNS_PAGE_SIZE)  # ceil division
    return max(1, min(_RAIDERIO_RUNS_MAX_PAGES, pages))


def resolve_season_input(season: str) -> str | None:
    """Map the ``--season`` option to a Raider.IO season request parameter.

    Empty or the ``current`` keyword (case-insensitive) resolve to ``None`` so the request
    omits the ``season`` param and the API applies its current default season; the effective
    slug is then recovered from the response. Any other value is an explicit season slug.
    """
    normalized = season.strip().lower()
    if not normalized or normalized == "current":
        return None
    return season.strip()


def response_season(payload: dict[str, Any]) -> str | None:
    """The season slug Raider.IO actually served, recovered from a ``/mythic-plus/runs`` response.

    The API echoes the season it applied under ``params.season`` (and not as a top-level key), so
    this is the only way ``resolved_season`` can be concrete when the request omitted ``--season``.
    """
    season = payload.get("season") or as_dict(payload.get("params")).get("season")
    return str(season) if isinstance(season, str) and season else None


def sample_leaderboard_runs(
    client: RaiderIOClient,
    *,
    season: str | None,
    region: str,
    dungeon: str,
    affixes: str | None,
    page: int,
    pages: int,
    limit: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    seen_run_ids: set[str] = set()
    runs: list[dict[str, Any]] = []
    leaderboard_urls: list[str] = []
    pages_fetched = 0
    effective_season = season
    for offset in range(pages):
        current_page = page + offset
        payload = client.mythic_plus_runs(
            season=season,
            region=region,
            dungeon=dungeon,
            affixes=affixes,
            page=current_page,
        )
        pages_fetched += 1
        served_season = response_season(payload)
        if served_season:
            effective_season = served_season
        leaderboard_url = payload.get("leaderboard_url")
        if isinstance(leaderboard_url, str) and leaderboard_url and leaderboard_url not in leaderboard_urls:
            leaderboard_urls.append(leaderboard_url)
        rankings = as_list(payload.get("rankings"))
        if not rankings:
            break
        for row in rankings:
            if not isinstance(row, dict):
                continue
            snapshot = _run_snapshot(row)
            run_id = str(snapshot.get("run_id") or f"{snapshot.get('rank')}:{snapshot.get('dungeon_slug')}:{snapshot.get('completed_at')}")
            if run_id in seen_run_ids:
                continue
            seen_run_ids.add(run_id)
            runs.append(snapshot)
            if len(runs) >= limit:
                break
        if len(runs) >= limit:
            break
    return runs, {
        "sampled_at": datetime.now(UTC).isoformat(),
        "season": effective_season,
        "pages_requested": pages,
        "pages_fetched": pages_fetched,
        "cache_ttl_seconds": client.mythic_plus_runs_ttl_seconds,
        "leaderboard_urls": leaderboard_urls,
    }


def _normalize_filter_values(values: list[str] | None) -> list[str]:
    normalized: list[str] = []
    for value in values or []:
        cleaned = value.strip().lower().replace("_", "-").replace(" ", "-")
        if cleaned and cleaned not in normalized:
            normalized.append(cleaned)
    return normalized


def _run_roster(run: dict[str, Any]) -> list[dict[str, Any]]:
    roster = run.get("roster")
    if not isinstance(roster, list):
        return []
    return [entry for entry in roster if isinstance(entry, dict)]


def _metric_meets_bounds(value: Any, *, minimum: float | None, maximum: float | None) -> bool:
    if minimum is None and maximum is None:
        return True
    if not isinstance(value, (int, float)):
        # A bound was asked for and this run has no metric to compare, so it cannot be claimed to be
        # inside the requested range; the exclusion is reported in `filtering.excluded_run_count`.
        return False
    numeric_value = float(value)
    if minimum is not None and numeric_value < minimum:
        return False
    return not (maximum is not None and numeric_value > maximum)


def _roster_field_values(
    roster: list[dict[str, Any]],
    *,
    primary_key: str,
    fallback_key: str | None = None,
    slugify_spaces: bool = False,
) -> set[str]:
    values: set[str] = set()
    for entry in roster:
        raw_value = entry.get(primary_key)
        if not raw_value and fallback_key is not None:
            raw_value = entry.get(fallback_key)
        text = str(raw_value or "").strip().lower()
        if slugify_spaces:
            text = text.replace(" ", "-")
        if text:
            values.add(text)
    return values


def _roster_contains_any(
    roster: list[dict[str, Any]],
    expected: list[str],
    *,
    primary_key: str,
    fallback_key: str | None = None,
    slugify_spaces: bool = False,
) -> bool:
    if not expected:
        return True
    values = _roster_field_values(
        roster,
        primary_key=primary_key,
        fallback_key=fallback_key,
        slugify_spaces=slugify_spaces,
    )
    return any(value in values for value in expected)


def run_matches_filters(
    run: dict[str, Any],
    *,
    level_min: int | None,
    level_max: int | None,
    score_min: float | None,
    score_max: float | None,
    contains_role: list[str],
    contains_class: list[str],
    contains_spec: list[str],
    player_region: list[str],
) -> bool:
    if not _metric_meets_bounds(run.get("mythic_level"), minimum=level_min, maximum=level_max):
        return False
    if not _metric_meets_bounds(run.get("score"), minimum=score_min, maximum=score_max):
        return False
    roster = _run_roster(run)
    if not _roster_contains_any(roster, contains_role, primary_key="role"):
        return False
    if not _roster_contains_any(roster, contains_class, primary_key="class_slug", fallback_key="class_name", slugify_spaces=True):
        return False
    if not _roster_contains_any(roster, contains_spec, primary_key="spec_slug", fallback_key="spec_name", slugify_spaces=True):
        return False
    return _roster_contains_any(roster, player_region, primary_key="region")


def _filtered_runs(runs: list[dict[str, Any]], filters: RunFilters) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    filtered = [
        run
        for run in runs
        if run_matches_filters(
            run,
            level_min=filters.level_min,
            level_max=filters.level_max,
            score_min=filters.score_min,
            score_max=filters.score_max,
            contains_role=list(filters.contains_role),
            contains_class=list(filters.contains_class),
            contains_spec=list(filters.contains_spec),
            player_region=list(filters.player_region),
        )
    ]
    return filtered, {
        **filters.as_query(),
        "source_run_count": len(runs),
        "returned_run_count": len(filtered),
        "excluded_run_count": len(runs) - len(filtered),
    }


def load_filtered_runs(
    client: RaiderIOClient,
    request: SampleRequest,
    filters: RunFilters,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    """Sample the Mythic+ run leaderboard for ``request`` and apply ``filters`` to the result."""
    runs, meta = sample_leaderboard_runs(
        client,
        season=request.season_param,
        region=request.region,
        dungeon=request.dungeon,
        affixes=request.affixes or None,
        page=request.page,
        pages=request.pages,
        limit=request.limit,
    )
    runs, filtering = _filtered_runs(runs, filters)
    return runs, meta, filtering


def _unique_player_keys(roster_entries: list[dict[str, Any]]) -> set[tuple[str, str, str]]:
    return {
        (
            str(entry.get("region") or ""),
            str(entry.get("realm") or ""),
            str(entry.get("name") or ""),
        )
        for entry in roster_entries
    }


def _sample_tag_values(rows: list[dict[str, Any]], field: str) -> list[str]:
    return sorted(
        {
            value
            for row in rows
            for value in as_list(row.get(field))
            if value
        }
    )


def _composition_key(run: dict[str, Any], *, mode: str) -> str:
    roster = as_list(run.get("roster"))
    parts: list[str] = []
    for entry in roster:
        if not isinstance(entry, dict):
            continue
        role = str(entry.get("role") or "unknown")
        if mode == "spec":
            label = str(entry.get("spec_slug") or entry.get("spec_name") or "unknown")
        else:
            label = str(entry.get("class_slug") or entry.get("class_name") or "unknown")
        parts.append(f"{role}:{label}")
    parts.sort()
    return " | ".join(parts) if parts else "unknown"


def sample_summary(runs: list[dict[str, Any]], *, meta: dict[str, Any]) -> dict[str, Any]:
    roster_entries = [entry for run in runs for entry in _run_roster(run)]
    role_values = [str(entry.get("role") or "unknown") for entry in roster_entries]
    region_values = [str(entry.get("region") or "unknown") for entry in roster_entries]
    dungeon_values = [str(run.get("dungeon") or "unknown") for run in runs]
    level_values: list[int | float] = [int(run["mythic_level"]) for run in runs if isinstance(run.get("mythic_level"), int)]
    unique_players = _unique_player_keys(roster_entries)
    return {
        "sampled_at": meta["sampled_at"],
        "season": meta.get("season"),
        "pages_requested": meta["pages_requested"],
        "pages_fetched": meta["pages_fetched"],
        "run_count": len(runs),
        "roster_entry_count": len(roster_entries),
        "unique_player_count": len(unique_players),
        "unique_dungeons": sorted({value for value in dungeon_values if value and value != "unknown"}),
        "role_counts": count_map(role_values),
        "player_region_counts": count_map(region_values),
        "mythic_level": numeric_summary(level_values),
    }


def _player_snapshot_key(entry: dict[str, Any]) -> tuple[str, str, str] | None:
    name = str(entry.get("name") or "").strip()
    realm = str(entry.get("realm") or "").strip().lower()
    region = str(entry.get("region") or "").strip().lower()
    if not name or not realm or not region:
        return None
    return region, realm, name


def _new_player_snapshot(key: tuple[str, str, str], entry: dict[str, Any]) -> dict[str, Any]:
    region, realm, name = key
    return {
        "name": name,
        "realm": realm,
        "region": region,
        "profile_url": entry.get("profile_url"),
        "appearance_count": 0,
        "roles": [],
        "class_slugs": [],
        "spec_slugs": [],
        "top_mythic_level": None,
        "top_score": None,
        "latest_completed_at": None,
        "dungeons": [],
        "dungeon_slugs": [],
    }


def _append_unique(snapshot: dict[str, Any], field: str, value: str) -> None:
    if not value:
        return
    values = snapshot.get(field)
    if isinstance(values, list) and value not in values:
        values.append(value)


def _normalized_roster_label(entry: dict[str, Any], primary_key: str, fallback_key: str) -> str:
    return str(entry.get(primary_key) or entry.get(fallback_key) or "").strip().lower().replace(" ", "-")


def _update_player_snapshot(snapshot: dict[str, Any], entry: dict[str, Any], run: dict[str, Any]) -> None:
    snapshot["appearance_count"] += 1
    _append_unique(snapshot, "roles", str(entry.get("role") or "").strip().lower())
    _append_unique(snapshot, "class_slugs", _normalized_roster_label(entry, "class_slug", "class_name"))
    _append_unique(snapshot, "spec_slugs", _normalized_roster_label(entry, "spec_slug", "spec_name"))

    mythic_level = run.get("mythic_level")
    if isinstance(mythic_level, int):
        current_top = snapshot.get("top_mythic_level")
        if not isinstance(current_top, int) or mythic_level > current_top:
            snapshot["top_mythic_level"] = mythic_level

    score = run.get("score")
    if isinstance(score, (int, float)):
        current_score = snapshot.get("top_score")
        if not isinstance(current_score, (int, float)) or float(score) > float(current_score):
            snapshot["top_score"] = float(score)

    completed_at = run.get("completed_at")
    if isinstance(completed_at, str):
        latest = snapshot.get("latest_completed_at")
        if not isinstance(latest, str) or completed_at > latest:
            snapshot["latest_completed_at"] = completed_at

    dungeon = run.get("dungeon")
    if isinstance(dungeon, str):
        _append_unique(snapshot, "dungeons", dungeon)
    dungeon_slug = run.get("dungeon_slug")
    if isinstance(dungeon_slug, str):
        _append_unique(snapshot, "dungeon_slugs", dungeon_slug)


def _player_snapshot_sort_key(row: dict[str, Any]) -> tuple[int, int, str]:
    top_level = row.get("top_mythic_level")
    return (
        -int(row.get("appearance_count") or 0),
        -(int(top_level) if isinstance(top_level, int) else -1),
        str(row.get("name") or ""),
    )


def player_snapshots(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    players: dict[tuple[str, str, str], dict[str, Any]] = {}
    for run in runs:
        for entry in _run_roster(run):
            key = _player_snapshot_key(entry)
            if key is None:
                continue
            snapshot = players.get(key)
            if snapshot is None:
                snapshot = _new_player_snapshot(key, entry)
                players[key] = snapshot
            _update_player_snapshot(snapshot, entry, run)
    snapshots = list(players.values())
    snapshots.sort(key=_player_snapshot_sort_key)
    return snapshots


def limit_player_snapshots(players: list[dict[str, Any]], *, player_limit: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    limited = players[:player_limit]
    return limited, {
        "source_player_count": len(players),
        "returned_player_count": len(limited),
        "truncated": len(players) > player_limit,
        "excluded_player_count": max(0, len(players) - len(limited)),
        "player_limit": player_limit,
    }


def player_sample_summary(
    players: list[dict[str, Any]],
    *,
    runs: list[dict[str, Any]],
    meta: dict[str, Any],
    filtering: dict[str, Any],
    player_sampling: dict[str, Any],
) -> dict[str, Any]:
    appearance_counts: list[int | float] = [
        int(player["appearance_count"]) for player in players if isinstance(player.get("appearance_count"), int)
    ]
    top_levels: list[int | float] = [
        int(player["top_mythic_level"]) for player in players if isinstance(player.get("top_mythic_level"), int)
    ]
    classes = _sample_tag_values(players, "class_slugs")
    specs = _sample_tag_values(players, "spec_slugs")
    summary = {
        **sample_summary(runs, meta=meta),
        "filtering": filtering,
        "player_sampling": player_sampling,
        "player_count": len(players),
        "unique_class_count": len(classes),
        "unique_spec_count": len(specs),
        "classes": classes,
        "specs": specs,
        "appearance_count": numeric_summary(appearance_counts),
        "top_mythic_level": numeric_summary(top_levels),
    }
    return summary


def freshness_payload(meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "sampled_at": meta["sampled_at"],
        "cache_ttl_seconds": meta["cache_ttl_seconds"],
    }


def runs_page_provenance(payload: dict[str, Any], *, cache_ttl_seconds: int) -> dict[str, Any]:
    """``freshness`` and ``citations`` for the single-page ``mythic-plus-runs`` read.

    The sampled siblings build theirs from sampling meta; this read has none, so the read time and
    the leaderboard URL Raider.IO echoes stand in. Without it the envelope's provenance is empty.

    ``sampled_at`` carries the same meaning as in the sampled siblings -- when this command read the
    response -- because a cache hit can be up to ``cache_ttl_seconds`` older than that upstream.
    """
    leaderboard_url = payload.get("leaderboard_url")
    return {
        "freshness": {"sampled_at": datetime.now(UTC).isoformat(), "cache_ttl_seconds": cache_ttl_seconds},
        "citations": {
            "leaderboard_urls": [leaderboard_url] if isinstance(leaderboard_url, str) and leaderboard_url else [],
        },
    }


def citations_payload(meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "leaderboard_urls": meta["leaderboard_urls"],
    }


def validated_metric(metric: str, allowed: tuple[str, ...]) -> str:
    """Return ``metric`` when the command supports it, so the error message cannot drift from help."""
    if metric not in allowed:
        raise ProviderError("invalid_query", f"--metric must be one of: {', '.join(allowed)}")
    return metric


def _run_distribution_values(metric: str, runs: list[dict[str, Any]]) -> tuple[list[int | float] | list[str], str, bool] | None:
    if metric == "mythic_level":
        levels: list[int | float] = [int(run["mythic_level"]) for run in runs if isinstance(run.get("mythic_level"), int)]
        return levels, "runs", True
    if metric == "dungeon":
        return [str(run.get("dungeon") or "unknown") for run in runs], "runs", False
    if metric == "composition":
        return [_composition_key(run, mode="spec") for run in runs], "runs", False
    if metric == "class_composition":
        return [_composition_key(run, mode="class") for run in runs], "runs", False
    return None


def _roster_metric_value(entry: dict[str, Any], metric: str) -> str:
    if metric == "role":
        return str(entry.get("role") or "unknown")
    if metric == "class":
        return str(entry.get("class_slug") or entry.get("class_name") or "unknown")
    if metric == "spec":
        return str(entry.get("spec_slug") or entry.get("spec_name") or "unknown")
    return str(entry.get("region") or "unknown")


def _roster_distribution_values(metric: str, runs: list[dict[str, Any]]) -> tuple[list[str], str]:
    return [
        _roster_metric_value(entry, metric)
        for run in runs
        for entry in _run_roster(run)
    ], "roster_entries"


def distribution_values(metric: str, runs: list[dict[str, Any]]) -> tuple[list[int | float] | list[str], str, bool]:
    run_values = _run_distribution_values(metric, runs)
    if run_values is not None:
        return run_values
    roster_values, unit = _roster_distribution_values(metric, runs)
    return roster_values, unit, False


def _player_numeric_distribution_values(metric: str, players: list[dict[str, Any]]) -> tuple[list[int], str] | None:
    if metric == "appearance_count":
        return [int(player["appearance_count"]) for player in players if isinstance(player.get("appearance_count"), int)], "players"
    if metric == "top_mythic_level":
        return [int(player["top_mythic_level"]) for player in players if isinstance(player.get("top_mythic_level"), int)], "players"
    return None


def _player_tag_distribution_values(metric: str, players: list[dict[str, Any]]) -> tuple[list[str], str] | None:
    field_map = {
        "class": ("class_slugs", "player_class_tags"),
        "spec": ("spec_slugs", "player_spec_tags"),
        "role": ("roles", "player_role_tags"),
    }
    field_info = field_map.get(metric)
    if field_info is None:
        return None
    field, unit = field_info
    return [str(value) for player in players for value in as_list(player.get(field)) if value], unit


def player_distribution_values(metric: str, players: list[dict[str, Any]]) -> tuple[list[int] | list[str], str, bool]:
    numeric_values = _player_numeric_distribution_values(metric, players)
    if numeric_values is not None:
        return numeric_values[0], numeric_values[1], True
    tag_values = _player_tag_distribution_values(metric, players)
    if tag_values is not None:
        return tag_values[0], tag_values[1], False
    return [str(player.get("region") or "unknown") for player in players], "players", False


def _distribution_block(values: Sequence[int | float] | Sequence[str], *, unit: str, numeric: bool) -> dict[str, Any]:
    # The value extractors already guarantee a homogeneous list; the filters only narrow the union for mypy.
    if numeric:
        return numeric_distribution([value for value in values if isinstance(value, (int, float))], unit=unit)
    return categorical_distribution([str(value) for value in values], unit=unit)


def distribution_payload(metric: str, runs: list[dict[str, Any]], *, meta: dict[str, Any], query: dict[str, Any]) -> dict[str, Any]:
    sample = sample_summary(runs, meta=meta)
    values, unit, numeric = distribution_values(metric, runs)
    return distribution_response(
        provider="raiderio",
        kind="mythic_plus_runs_distribution",
        metric=metric,
        query=query,
        sample=sample,
        distribution=_distribution_block(values, unit=unit, numeric=numeric),
        freshness=freshness_payload(meta),
        citations=citations_payload(meta),
    )


def player_distribution_payload(
    metric: str,
    players: list[dict[str, Any]],
    *,
    runs: list[dict[str, Any]],
    meta: dict[str, Any],
    query: dict[str, Any],
    filtering: dict[str, Any],
    player_sampling: dict[str, Any],
) -> dict[str, Any]:
    sample = player_sample_summary(players, runs=runs, meta=meta, filtering=filtering, player_sampling=player_sampling)
    values, unit, numeric = player_distribution_values(metric, players)
    distribution = _distribution_block(values, unit=unit, numeric=numeric)
    return distribution_response(
        provider="raiderio",
        kind="mythic_plus_players_distribution",
        metric=metric,
        query=query,
        sample=sample,
        distribution=distribution,
        freshness=freshness_payload(meta),
        citations=citations_payload(meta),
    )


def _nearest_threshold_rows(metric: str, target: float, runs: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for run in runs:
        raw_value = run.get("score") if metric == "score" else run.get("mythic_level")
        if not isinstance(raw_value, (int, float)):
            continue
        rows.append(
            {
                "value": float(raw_value),
                "distance": round(abs(float(raw_value) - target), 3),
                "run": run,
            }
        )
    rows.sort(
        key=lambda row: (
            float(row["distance"]),
            -float(row["value"]),
            str((row["run"] or {}).get("dungeon") or ""),
        )
    )
    return rows[:limit]


def threshold_payload(metric: str, target: float, runs: list[dict[str, Any]], *,
                       meta: dict[str, Any], query: dict[str, Any], nearest_limit: int) -> dict[str, Any]:
    nearest = _nearest_threshold_rows(metric, target, runs, limit=nearest_limit)
    estimate_values: list[int | float]
    if metric == "score":
        estimate_metric = "mythic_level"
        estimate_values = [int(row["run"]["mythic_level"]) for row in nearest if isinstance(row["run"].get("mythic_level"), int)]
        caveat = "This estimates run-level outcomes near a sampled Raider.IO run score, not player rating."
    else:
        estimate_metric = "score"
        estimate_values = [float(row["run"]["score"]) for row in nearest if isinstance(row["run"].get("score"), (int, float))]
        caveat = "This estimates sampled run scores near a target Mythic+ level."
    estimate: dict[str, Any] | None = None
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
        "provider": "raiderio",
        "kind": "mythic_plus_runs_threshold",
        "metric": metric,
        "target": target,
        "query": query,
        "sample": sample_summary(runs, meta=meta),
        "threshold": {
            "nearest_match_count": len(nearest),
            "nearest_matches": [
                {
                    "value": row["value"],
                    "distance": row["distance"],
                    "run": row["run"],
                }
                for row in nearest
            ],
            "estimate": estimate,
            "caveat": caveat,
        },
        "freshness": freshness_payload(meta),
        "citations": citations_payload(meta),
    }

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from statistics import median
from typing import Any

import httpx
import typer
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
from warcraft_core.output import emit
from warcraft_core.wow_normalization import normalize_name, normalize_region, primary_realm_slug

from raiderio_cli.client import RaiderIOClient, load_raiderio_cache_settings_from_env

app = typer.Typer(add_completion=False, help="Raider.IO profile and leaderboard CLI.")
sample_app = typer.Typer(add_completion=False, help="Sample-backed Raider.IO analytics primitives.")
distribution_app = typer.Typer(add_completion=False, help="Derived distributions built from Raider.IO samples.")
threshold_app = typer.Typer(add_completion=False, help="Threshold-style estimates derived from sampled Raider.IO runs.")
app.add_typer(sample_app, name="sample")
app.add_typer(distribution_app, name="distribution")
app.add_typer(threshold_app, name="threshold")


@dataclass(slots=True)
class RuntimeConfig:
    pretty: bool = False


def _cfg(ctx: typer.Context) -> RuntimeConfig:
    obj = ctx.obj
    if isinstance(obj, RuntimeConfig):
        return obj
    return RuntimeConfig()


def _emit(ctx: typer.Context, payload: dict[str, Any], *, err: bool = False) -> None:
    emit(payload, pretty=_cfg(ctx).pretty, err=err)


def _fail(ctx: typer.Context, code: str, message: str, *, status: int = 1) -> None:
    _emit(ctx, {"ok": False, "error": {"code": code, "message": message}}, err=True)
    raise typer.Exit(status)


def _client(ctx: typer.Context) -> RaiderIOClient:
    try:
        return RaiderIOClient()
    except ValueError as exc:
        _fail(ctx, "invalid_cache_config", str(exc))
        raise AssertionError("unreachable") from None


def _handle_http_error(ctx: typer.Context, exc: httpx.HTTPStatusError) -> None:
    status_code = exc.response.status_code
    code = "upstream_error"
    if status_code == 400:
        code = "invalid_query"
    elif status_code == 404:
        code = "not_found"
    elif status_code == 429:
        code = "rate_limited"
    message = f"Raider.IO request failed with HTTP {status_code}."
    try:
        payload = exc.response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        detail = payload.get("message") or payload.get("error")
        if isinstance(detail, str) and detail.strip():
            message = detail.strip()
    _fail(ctx, code, message, status=1)


def _normalize_search_query(query: str) -> tuple[str, str | None]:
    tokens = [token for token in query.strip().split() if token]
    type_hint: str | None = None
    kept: list[str] = []
    for token in tokens:
        lower = token.lower()
        if lower in {"guild", "guilds"}:
            type_hint = "guild"
            continue
        if lower in {"character", "characters", "char"}:
            type_hint = "character"
            continue
        kept.append(token)
    normalized = " ".join(kept).strip() or query.strip()
    return normalized, type_hint


def _normalize_structured_query(query: str) -> tuple[str, str | None, str | None, str | None, str | None]:
    normalized_query, type_hint = _normalize_search_query(query)
    tokens = [token for token in normalized_query.strip().split() if token]
    if len(tokens) < 3:
        return normalized_query, type_hint, None, None, None
    region = normalize_region(tokens[0])
    if region not in {"us", "eu", "kr", "tw", "cn"}:
        return normalized_query, type_hint, None, None, None
    realm = primary_realm_slug(tokens[1])
    name = normalize_name(" ".join(tokens[2:]).strip())
    if not name:
        return normalized_query, type_hint, None, None, None
    return normalized_query, type_hint, region, realm, name


def _query_terms(value: str) -> list[str]:
    return [part for part in value.lower().split() if part]


def _combined_match_text(*parts: str | None) -> str:
    return " ".join(part for part in parts if part).lower()


def _all_terms_match(query_terms: list[str], combined: str) -> bool:
    return bool(query_terms) and all(term in combined for term in query_terms)


def _entity_match_score(
    *,
    query: str,
    type_hint: str | None,
    kind: str,
    name: str,
    region: str | None,
    realm: str | None,
    exact_name_bonus: int,
    contains_bonus: int,
    all_terms_bonus: int,
    region_bonus: int,
    realm_bonus: int,
    type_hint_bonus: int,
    base_reasons: list[str] | None = None,
    base_score: int = 0,
) -> tuple[int, list[str]]:
    lowered_query = query.lower()
    name_lower = name.lower()
    combined = _combined_match_text(name, realm, region)
    query_terms = _query_terms(query)
    score = base_score
    reasons = list(base_reasons or [])
    if lowered_query == name_lower:
        score += exact_name_bonus
        reasons.append("exact_name")
    elif lowered_query in name_lower:
        score += contains_bonus
        reasons.append("name_contains_query")
    if _all_terms_match(query_terms, combined):
        score += all_terms_bonus
        reasons.append("all_terms_match")
    if region and any(term == region.lower() for term in query_terms):
        score += region_bonus
        reasons.append("region_match")
    if realm and any(term == realm.lower() for term in query_terms):
        score += realm_bonus
        reasons.append("realm_match")
    if type_hint and type_hint == kind:
        score += type_hint_bonus
        reasons.append("type_hint")
    return score, reasons


def _match_reasons(
    *,
    query: str,
    type_hint: str | None,
    kind: str,
    name: str,
    region: str | None,
    realm: str | None,
) -> tuple[int, list[str]]:
    return _entity_match_score(
        query=query,
        type_hint=type_hint,
        kind=kind,
        name=name,
        region=region,
        realm=realm,
        exact_name_bonus=50,
        contains_bonus=25,
        all_terms_bonus=20,
        region_bonus=8,
        realm_bonus=10,
        type_hint_bonus=15,
    )


def _follow_up_for_match(kind: str, region: str | None, realm: str | None, name: str) -> dict[str, Any]:
    base = {
        "provider": "raiderio",
        "kind": kind,
    }
    if kind == "character" and region and realm:
        return {
            **base,
            "surface": "character",
            "command": f"raiderio character {region} {realm} {name}",
        }
    if kind == "guild" and region and realm:
        return {
            **base,
            "surface": "guild",
            "command": f"raiderio guild {region} {realm} {name}",
        }
    return {
        **base,
        "surface": None,
        "command": None,
    }


def _structured_match_reasons(
    *,
    query: str,
    type_hint: str | None,
    kind: str,
    name: str,
    region: str,
    realm: str,
) -> tuple[int, list[str]]:
    return _entity_match_score(
        query=query,
        type_hint=type_hint,
        kind=kind,
        name=name,
        region=region,
        realm=realm,
        exact_name_bonus=45,
        contains_bonus=20,
        all_terms_bonus=25,
        region_bonus=12,
        realm_bonus=12,
        type_hint_bonus=20,
        base_reasons=["structured_probe"],
        base_score=12,
    )


def _candidate_from_character_profile(
    *,
    query: str,
    type_hint: str | None,
    payload: dict[str, Any],
    query_region: str,
    query_realm: str,
    query_name: str,
) -> dict[str, Any]:
    region = str(payload.get("region") or query_region).strip().lower()
    realm = str(payload.get("realm") or query_realm).strip()
    name = str(payload.get("name") or query_name).strip()
    score, reasons = _structured_match_reasons(
        query=query,
        type_hint=type_hint,
        kind="character",
        name=name,
        region=region,
        realm=realm,
    )
    return {
        "provider": "raiderio",
        "kind": "character",
        "id": payload.get("id") or payload.get("profile_url") or f"character:{region}:{realm}:{name}",
        "name": name,
        "region": region,
        "realm": realm.lower() if realm else None,
        "realm_name": realm,
        "faction": payload.get("faction"),
        "class_name": payload.get("class"),
        "active_spec_name": payload.get("active_spec_name"),
        "profile_url": payload.get("profile_url"),
        "ranking": {
            "score": score,
            "match_reasons": reasons,
        },
        "follow_up": _follow_up_for_match("character", query_region, query_realm, query_name),
    }


def _candidate_from_guild_profile(
    *,
    query: str,
    type_hint: str | None,
    payload: dict[str, Any],
    query_region: str,
    query_realm: str,
    query_name: str,
) -> dict[str, Any]:
    region = str(payload.get("region") or query_region).strip().lower()
    realm = str(payload.get("realm") or query_realm).strip()
    name = str(payload.get("name") or query_name).strip()
    score, reasons = _structured_match_reasons(
        query=query,
        type_hint=type_hint,
        kind="guild",
        name=name,
        region=region,
        realm=realm,
    )
    return {
        "provider": "raiderio",
        "kind": "guild",
        "id": payload.get("id") or payload.get("profile_url"),
        "name": name,
        "region": region,
        "realm": realm.lower() if realm else None,
        "realm_name": realm,
        "faction": payload.get("faction"),
        "profile_url": payload.get("profile_url"),
        "ranking": {
            "score": score,
            "match_reasons": reasons,
        },
        "follow_up": _follow_up_for_match("guild", query_region, query_realm, query_name),
    }


def _probe_structured_candidates(
    client: RaiderIOClient,
    *,
    query: str,
    type_hint: str | None,
    region: str | None,
    realm: str | None,
    name: str | None,
) -> list[dict[str, Any]]:
    if region is None or realm is None or name is None:
        return []
    candidates: list[dict[str, Any]] = []
    probe_kinds = [type_hint] if type_hint in {"character", "guild"} else ["character", "guild"]
    for probe_kind in probe_kinds:
        try:
            if probe_kind == "character":
                payload = client.character_profile_variants(region=region, realm=realm, name=name)
                candidates.append(
                    _candidate_from_character_profile(
                        query=query,
                        type_hint=type_hint,
                        payload=payload,
                        query_region=region,
                        query_realm=realm,
                        query_name=name,
                    )
                )
            else:
                payload = client.guild_profile_variants(region=region, realm=realm, name=name)
                candidates.append(
                    _candidate_from_guild_profile(
                        query=query,
                        type_hint=type_hint,
                        payload=payload,
                        query_region=region,
                        query_realm=realm,
                        query_name=name,
                    )
                )
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            if status_code in {400, 404}:
                continue
            raise
    return candidates


def _search_results_payload(
    query: str,
    raw_matches: list[dict[str, Any]],
    *,
    type_hint: str | None,
    limit: int,
    extra_candidates: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    results = _search_result_candidates(raw_matches, query=query, type_hint=type_hint)
    if extra_candidates:
        results.extend(extra_candidates)
    results = _dedupe_search_candidates(results)
    top = _sorted_search_candidates(results)[:limit]
    return {
        "provider": "raiderio",
        "query": query,
        "search_query": query,
        "count": len(results),
        "results": top,
        "truncated": len(results) > limit,
    }


def _resolve_payload(search_payload: dict[str, Any], *, limit: int) -> dict[str, Any]:
    results = search_payload.get("results")
    if not isinstance(results, list):
        results = []
    top = results[:limit]
    if not top:
        return {
            "provider": "raiderio",
            "query": search_payload.get("query"),
            "search_query": search_payload.get("search_query"),
            "resolved": False,
            "confidence": "none",
            "match": None,
            "next_command": None,
            "fallback_search_command": f'raiderio search "{search_payload.get("search_query")}"',
            "candidates": [],
        }
    best = top[0]
    follow_up = best.get("follow_up") if isinstance(best.get("follow_up"), dict) else {}
    best_score = _candidate_ranking_score(best)
    resolved = bool(follow_up.get("command")) and _resolve_candidate_is_confident(top)
    confidence = _resolve_confidence_label(best_score, resolved=resolved)
    return {
        "provider": "raiderio",
        "query": search_payload.get("query"),
        "search_query": search_payload.get("search_query"),
        "resolved": resolved,
        "confidence": confidence if top else "none",
        "match": best,
        "next_command": follow_up.get("command") if resolved else None,
        "fallback_search_command": None if resolved else f'raiderio search "{search_payload.get("search_query")}"',
        "candidates": top,
    }


def _search_result_candidate(row: dict[str, Any], *, query: str, type_hint: str | None) -> dict[str, Any] | None:
    kind = str(row.get("type") or "").strip().lower()
    if kind not in {"character", "guild"}:
        return None
    data = row.get("data") if isinstance(row.get("data"), dict) else {}
    region_row = data.get("region") if isinstance(data.get("region"), dict) else {}
    realm_row = data.get("realm") if isinstance(data.get("realm"), dict) else {}
    class_row = data.get("class") if isinstance(data.get("class"), dict) else {}
    name = str(data.get("displayName") or data.get("name") or row.get("name") or "").strip()
    region = str(region_row.get("slug") or "").strip() or None
    realm = str(realm_row.get("slug") or "").strip() or None
    score, reasons = _match_reasons(
        query=query,
        type_hint=type_hint,
        kind=kind,
        name=name,
        region=region,
        realm=realm,
    )
    path = data.get("path")
    profile_url = f"https://raider.io{path}" if isinstance(path, str) and path.startswith("/") else None
    return {
        "provider": "raiderio",
        "kind": kind,
        "id": data.get("id"),
        "name": name,
        "region": region,
        "region_name": region_row.get("name"),
        "realm": realm,
        "realm_name": realm_row.get("name"),
        "faction": data.get("faction"),
        "class_name": class_row.get("name"),
        "class_slug": class_row.get("slug"),
        "profile_url": profile_url,
        "path": path,
        "ranking": {
            "score": score,
            "match_reasons": reasons,
        },
        "follow_up": _follow_up_for_match(kind, region, realm, name),
    }


def _search_result_candidates(
    raw_matches: list[dict[str, Any]],
    *,
    query: str,
    type_hint: str | None,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for row in raw_matches:
        candidate = _search_result_candidate(row, query=query, type_hint=type_hint)
        if candidate is not None:
            results.append(candidate)
    return results


def _candidate_dedupe_key(row: dict[str, Any]) -> tuple[str, str | None, str | None, str]:
    return (
        str(row.get("kind") or ""),
        (str(row.get("region") or "").lower() or None),
        (str(row.get("realm") or "").lower() or None),
        str(row.get("name") or ""),
    )


def _candidate_ranking_score(row: dict[str, Any]) -> int:
    return int(((row.get("ranking") or {}).get("score")) or 0)


def _dedupe_search_candidates(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: dict[tuple[str, str | None, str | None, str], dict[str, Any]] = {}
    for row in results:
        key = _candidate_dedupe_key(row)
        existing = deduped.get(key)
        if existing is None or _candidate_ranking_score(row) > _candidate_ranking_score(existing):
            deduped[key] = row
    return list(deduped.values())


def _sorted_search_candidates(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        results,
        key=lambda item: (-_candidate_ranking_score(item), str(item.get("kind") or ""), str(item.get("name") or "")),
    )


def _resolve_candidate_is_confident(top: list[dict[str, Any]]) -> bool:
    best_score = _candidate_ranking_score(top[0])
    second_score = _candidate_ranking_score(top[1]) if len(top) > 1 else 0
    return best_score >= 45 and (len(top) == 1 or best_score - second_score >= 15)


def _resolve_confidence_label(best_score: int, *, resolved: bool) -> str:
    if resolved:
        return "high"
    if best_score >= 30:
        return "medium"
    return "low"


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
    dungeon = row.get("dungeon") if isinstance(row.get("dungeon"), dict) else {}
    return {
        "mythic_level": row.get("mythic_level"),
        "dungeon": dungeon.get("name"),
        "dungeon_slug": dungeon.get("slug"),
        "completed_at": row.get("completed_at"),
        "num_chests": row.get("num_chests"),
        "clear_time_ms": row.get("clear_time_ms"),
        "keystone_time_ms": row.get("keystone_time_ms"),
    }


def _ranking_roster_entry(entry: dict[str, Any]) -> dict[str, Any]:
    character = entry.get("character") if isinstance(entry.get("character"), dict) else {}
    realm = character.get("realm") if isinstance(character.get("realm"), dict) else {}
    region = character.get("region") if isinstance(character.get("region"), dict) else {}
    class_row = character.get("class") if isinstance(character.get("class"), dict) else {}
    spec_row = character.get("spec") if isinstance(character.get("spec"), dict) else {}
    path = character.get("path")
    return {
        "name": character.get("name"),
        "realm": realm.get("slug"),
        "region": region.get("slug"),
        "class_name": class_row.get("name"),
        "class_slug": class_row.get("slug"),
        "spec_name": spec_row.get("name"),
        "spec_slug": spec_row.get("slug"),
        "profile_url": f"https://raider.io{path}" if isinstance(path, str) and path.startswith("/") else None,
        "role": entry.get("role"),
    }


def _ranking_run_summary(row: dict[str, Any]) -> dict[str, Any]:
    run = row.get("run") if isinstance(row.get("run"), dict) else {}
    dungeon = run.get("dungeon") if isinstance(run.get("dungeon"), dict) else {}
    roster = run.get("roster") if isinstance(run.get("roster"), list) else []
    return {
        "rank": row.get("rank"),
        "score": row.get("score"),
        "mythic_level": run.get("mythic_level"),
        "dungeon": dungeon.get("name"),
        "dungeon_slug": dungeon.get("slug"),
        "completed_at": run.get("completed_at"),
        "affixes": [affix.get("slug") for affix in run.get("weekly_modifiers", []) if isinstance(affix, dict)],
        "roster": [_ranking_roster_entry(entry) for entry in roster[:5] if isinstance(entry, dict)],
    }


def _run_snapshot(row: dict[str, Any]) -> dict[str, Any]:
    run = row.get("run") if isinstance(row.get("run"), dict) else {}
    snapshot = _ranking_run_summary(row)
    snapshot["run_id"] = run.get("keystone_run_id") or run.get("logged_run_id") or run.get("keystone_team_id")
    snapshot["season"] = run.get("season")
    snapshot["clear_time_ms"] = run.get("clear_time_ms")
    snapshot["keystone_time_ms"] = run.get("keystone_time_ms")
    snapshot["num_chests"] = run.get("num_chests")
    return snapshot


def _sample_mythic_plus_runs(
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
        if payload.get("season"):
            effective_season = str(payload.get("season"))
        leaderboard_url = payload.get("leaderboard_url")
        if isinstance(leaderboard_url, str) and leaderboard_url and leaderboard_url not in leaderboard_urls:
            leaderboard_urls.append(leaderboard_url)
        rankings = payload.get("rankings") if isinstance(payload.get("rankings"), list) else []
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
    if not isinstance(value, (int, float)):
        return True
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


def _run_matches_filters(
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


def _filtered_runs(
    runs: list[dict[str, Any]],
    *,
    level_min: int | None,
    level_max: int | None,
    score_min: float | None,
    score_max: float | None,
    contains_role: list[str] | None,
    contains_class: list[str] | None,
    contains_spec: list[str] | None,
    player_region: list[str] | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    normalized_role = _normalize_filter_values(contains_role)
    normalized_class = _normalize_filter_values(contains_class)
    normalized_spec = _normalize_filter_values(contains_spec)
    normalized_region = _normalize_filter_values(player_region)
    filtered = [
        run
        for run in runs
        if _run_matches_filters(
            run,
            level_min=level_min,
            level_max=level_max,
            score_min=score_min,
            score_max=score_max,
            contains_role=normalized_role,
            contains_class=normalized_class,
            contains_spec=normalized_spec,
            player_region=normalized_region,
        )
    ]
    return filtered, {
        "level_min": level_min,
        "level_max": level_max,
        "score_min": score_min,
        "score_max": score_max,
        "contains_role": normalized_role,
        "contains_class": normalized_class,
        "contains_spec": normalized_spec,
        "player_region": normalized_region,
        "source_run_count": len(runs),
        "returned_run_count": len(filtered),
        "excluded_run_count": len(runs) - len(filtered),
    }


def _load_filtered_runs(
    client: RaiderIOClient,
    *,
    season: str | None,
    region: str,
    dungeon: str,
    affixes: str | None,
    page: int,
    pages: int,
    limit: int,
    level_min: int | None,
    level_max: int | None,
    score_min: float | None,
    score_max: float | None,
    contains_role: list[str] | None,
    contains_class: list[str] | None,
    contains_spec: list[str] | None,
    player_region: list[str] | None,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    runs, meta = _sample_mythic_plus_runs(
        client,
        season=season,
        region=region,
        dungeon=dungeon,
        affixes=affixes,
        page=page,
        pages=pages,
        limit=limit,
    )
    runs, filtering = _filtered_runs(
        runs,
        level_min=level_min,
        level_max=level_max,
        score_min=score_min,
        score_max=score_max,
        contains_role=contains_role,
        contains_class=contains_class,
        contains_spec=contains_spec,
        player_region=player_region,
    )
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
            for value in (row.get(field) if isinstance(row.get(field), list) else [])
            if value
        }
    )


def _composition_key(run: dict[str, Any], *, mode: str) -> str:
    roster = run.get("roster") if isinstance(run.get("roster"), list) else []
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


def _sample_summary(runs: list[dict[str, Any]], *, meta: dict[str, Any]) -> dict[str, Any]:
    roster_entries = [entry for run in runs for entry in _run_roster(run)]
    role_values = [str(entry.get("role") or "unknown") for entry in roster_entries]
    region_values = [str(entry.get("region") or "unknown") for entry in roster_entries]
    dungeon_values = [str(run.get("dungeon") or "unknown") for run in runs]
    level_values = [int(run["mythic_level"]) for run in runs if isinstance(run.get("mythic_level"), int)]
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
        "role_counts": _count_map(role_values),
        "player_region_counts": _count_map(region_values),
        "mythic_level": _numeric_summary(level_values),
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


def _player_snapshots(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
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
    snapshots.sort(
        key=lambda row: (
            -int(row.get("appearance_count") or 0),
            -(int(row.get("top_mythic_level")) if isinstance(row.get("top_mythic_level"), int) else -1),
            str(row.get("name") or ""),
        )
    )
    return snapshots


def _limit_player_snapshots(players: list[dict[str, Any]], *, player_limit: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    limited = players[:player_limit]
    return limited, {
        "source_player_count": len(players),
        "returned_player_count": len(limited),
        "truncated": len(players) > player_limit,
        "excluded_player_count": max(0, len(players) - len(limited)),
        "player_limit": player_limit,
    }


def _player_sample_summary(
    players: list[dict[str, Any]],
    *,
    runs: list[dict[str, Any]],
    meta: dict[str, Any],
    filtering: dict[str, Any],
    player_sampling: dict[str, Any],
) -> dict[str, Any]:
    appearance_counts = [int(player["appearance_count"]) for player in players if isinstance(player.get("appearance_count"), int)]
    top_levels = [int(player["top_mythic_level"]) for player in players if isinstance(player.get("top_mythic_level"), int)]
    classes = _sample_tag_values(players, "class_slugs")
    specs = _sample_tag_values(players, "spec_slugs")
    summary = {
        **_sample_summary(runs, meta=meta),
        "filtering": filtering,
        "player_sampling": player_sampling,
        "player_count": len(players),
        "unique_class_count": len(classes),
        "unique_spec_count": len(specs),
        "classes": classes,
        "specs": specs,
        "appearance_count": _numeric_summary(appearance_counts),
        "top_mythic_level": _numeric_summary(top_levels),
    }
    return summary


def _freshness_payload(meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "sampled_at": meta["sampled_at"],
        "cache_ttl_seconds": meta["cache_ttl_seconds"],
    }


def _citations_payload(meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "leaderboard_urls": meta["leaderboard_urls"],
    }


def _run_distribution_values(metric: str, runs: list[dict[str, Any]]) -> tuple[list[int | float] | list[str], str, bool] | None:
    if metric == "mythic_level":
        return [int(run["mythic_level"]) for run in runs if isinstance(run.get("mythic_level"), int)], "runs", True
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


def _distribution_values(metric: str, runs: list[dict[str, Any]]) -> tuple[list[int | float] | list[str], str, bool]:
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
    return [
        str(value)
        for player in players
        for value in (player.get(field) if isinstance(player.get(field), list) else [])
        if value
    ], unit


def _player_distribution_values(metric: str, players: list[dict[str, Any]]) -> tuple[list[int] | list[str], str, bool]:
    numeric_values = _player_numeric_distribution_values(metric, players)
    if numeric_values is not None:
        values, unit = numeric_values
        return values, unit, True
    tag_values = _player_tag_distribution_values(metric, players)
    if tag_values is not None:
        values, unit = tag_values
        return values, unit, False
    return [str(player.get("region") or "unknown") for player in players], "players", False


def _distribution_payload(metric: str, runs: list[dict[str, Any]], *, meta: dict[str, Any], query: dict[str, Any]) -> dict[str, Any]:
    sample = _sample_summary(runs, meta=meta)
    values, unit, numeric = _distribution_values(metric, runs)
    return _distribution_response(
        provider="raiderio",
        kind="mythic_plus_runs_distribution",
        metric=metric,
        query=query,
        sample=sample,
        distribution=(
            _numeric_distribution(values, unit=unit)  # type: ignore[arg-type]
            if numeric
            else _categorical_distribution(values, unit=unit)  # type: ignore[arg-type]
        ),
        freshness=_freshness_payload(meta),
        citations=_citations_payload(meta),
    )


def _player_distribution_payload(
    metric: str,
    players: list[dict[str, Any]],
    *,
    runs: list[dict[str, Any]],
    meta: dict[str, Any],
    query: dict[str, Any],
    filtering: dict[str, Any],
    player_sampling: dict[str, Any],
) -> dict[str, Any]:
    sample = _player_sample_summary(players, runs=runs, meta=meta, filtering=filtering, player_sampling=player_sampling)
    values, unit, numeric = _player_distribution_values(metric, players)
    distribution = (
        _numeric_distribution(values, unit=unit)  # type: ignore[arg-type]
        if numeric
        else _categorical_distribution(values, unit=unit)  # type: ignore[arg-type]
    )
    return _distribution_response(
        provider="raiderio",
        kind="mythic_plus_players_distribution",
        metric=metric,
        query=query,
        sample=sample,
        distribution=distribution,
        freshness=_freshness_payload(meta),
        citations=_citations_payload(meta),
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


def _threshold_payload(metric: str, target: float, runs: list[dict[str, Any]], *,
                       meta: dict[str, Any], query: dict[str, Any], nearest_limit: int) -> dict[str, Any]:
    nearest = _nearest_threshold_rows(metric, target, runs, limit=nearest_limit)
    if metric == "score":
        estimate_metric = "mythic_level"
        estimate_values = [int(row["run"]["mythic_level"]) for row in nearest if isinstance(row["run"].get("mythic_level"), int)]
        caveat = "This estimates run-level outcomes near a sampled Raider.IO run score, not player rating."
    else:
        estimate_metric = "score"
        estimate_values = [float(row["run"]["score"]) for row in nearest if isinstance(row["run"].get("score"), (int, float))]
        caveat = "This estimates sampled run scores near a target Mythic+ level."
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
        "provider": "raiderio",
        "kind": "mythic_plus_runs_threshold",
        "metric": metric,
        "target": target,
        "query": query,
        "sample": _sample_summary(runs, meta=meta),
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
        "freshness": _freshness_payload(meta),
        "citations": _citations_payload(meta),
    }


def _analytics_filter_query(
    *,
    level_min: int | None,
    level_max: int | None,
    score_min: float | None,
    score_max: float | None,
    contains_role: list[str] | None,
    contains_class: list[str] | None,
    contains_spec: list[str] | None,
    player_region: list[str] | None,
) -> dict[str, Any]:
    return {
        "level_min": level_min,
        "level_max": level_max,
        "score_min": score_min,
        "score_max": score_max,
        "contains_role": _normalize_filter_values(contains_role),
        "contains_class": _normalize_filter_values(contains_class),
        "contains_spec": _normalize_filter_values(contains_spec),
        "player_region": _normalize_filter_values(player_region),
    }


@app.callback()
def main_callback(
    ctx: typer.Context,
    pretty: bool = typer.Option(False, "--pretty", help="Pretty-print JSON output."),
) -> None:
    ctx.obj = RuntimeConfig(pretty=pretty)


@app.command("doctor")
def doctor(ctx: typer.Context) -> None:
    try:
        settings, static_ttl, character_ttl, guild_ttl, mplus_runs_ttl = load_raiderio_cache_settings_from_env()
    except ValueError as exc:
        _fail(ctx, "invalid_cache_config", str(exc))
        return
    _emit(
        ctx,
        {
            "provider": "raiderio",
            "status": "ready",
            "command": "doctor",
            "installed": True,
            "language": "python",
            "auth": {
                "required": False,
                "deferred": True,
            },
            "capabilities": {
                "search": "ready",
                "resolve": "ready",
                "character": "ready",
                "guild": "ready",
                "mythic_plus_runs": "ready",
                "sample_mythic_plus_runs": "ready",
                "sample_mythic_plus_players": "ready",
                "distribution_mythic_plus_runs": "ready",
                "distribution_mythic_plus_players": "ready",
                "threshold_mythic_plus_runs": "ready",
            },
            "cache": {
                "enabled": settings.enabled,
                "backend": settings.backend,
                "cache_dir": str(settings.cache_dir),
                "redis_url": settings.redis_url,
                "prefix": settings.prefix,
                "ttls": {
                    "static_data": static_ttl,
                    "character_profile": character_ttl,
                    "guild_profile": guild_ttl,
                    "mythic_plus_runs": mplus_runs_ttl,
                },
            },
        },
    )


@app.command("search")
def search(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="Free-text character or guild query."),
    limit: int = typer.Option(5, "--limit", min=1, max=50, help="Maximum results to return."),
    kind: str = typer.Option("all", "--kind", help="Optional result kind: all, character, or guild."),
) -> None:
    if kind not in {"all", "character", "guild"}:
        _fail(ctx, "invalid_query", "--kind must be one of: all, character, guild")
        return
    normalized_query, type_hint, region, realm, name = _normalize_structured_query(query)
    effective_kind = type_hint or kind
    try:
        with _client(ctx) as client:
            structured_candidates = _probe_structured_candidates(
                client,
                query=normalized_query,
                type_hint=type_hint,
                region=region,
                realm=realm,
                name=name,
            )
            if structured_candidates:
                _emit(
                    ctx,
                    _search_results_payload(
                        normalized_query,
                        [],
                        type_hint=type_hint,
                        limit=limit,
                        extra_candidates=structured_candidates,
                    ),
                )
                return
            payload = client.search(term=normalized_query, kind=effective_kind)
    except httpx.HTTPStatusError as exc:
        _handle_http_error(ctx, exc)
        return
    raw_matches = payload.get("matches") if isinstance(payload.get("matches"), list) else []
    _emit(ctx, _search_results_payload(normalized_query, raw_matches, type_hint=type_hint, limit=limit))


@app.command("resolve")
def resolve(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="Free-text character or guild query."),
    limit: int = typer.Option(5, "--limit", min=1, max=50, help="Maximum candidates to return."),
    kind: str = typer.Option("all", "--kind", help="Optional result kind: all, character, or guild."),
) -> None:
    if kind not in {"all", "character", "guild"}:
        _fail(ctx, "invalid_query", "--kind must be one of: all, character, guild")
        return
    normalized_query, type_hint, region, realm, name = _normalize_structured_query(query)
    effective_kind = type_hint or kind
    try:
        with _client(ctx) as client:
            structured_candidates = _probe_structured_candidates(
                client,
                query=normalized_query,
                type_hint=type_hint,
                region=region,
                realm=realm,
                name=name,
            )
            if structured_candidates:
                search_payload = _search_results_payload(
                    normalized_query,
                    [],
                    type_hint=type_hint,
                    limit=limit,
                    extra_candidates=structured_candidates,
                )
                _emit(ctx, _resolve_payload(search_payload, limit=limit))
                return
            payload = client.search(term=normalized_query, kind=effective_kind)
    except httpx.HTTPStatusError as exc:
        _handle_http_error(ctx, exc)
        return
    raw_matches = payload.get("matches") if isinstance(payload.get("matches"), list) else []
    search_payload = _search_results_payload(normalized_query, raw_matches, type_hint=type_hint, limit=limit)
    _emit(ctx, _resolve_payload(search_payload, limit=limit))


@app.command("character")
def character(
    ctx: typer.Context,
    region: str = typer.Argument(..., help="Region slug such as us or eu."),
    realm: str = typer.Argument(..., help="Realm slug or title."),
    name: str = typer.Argument(..., help="Character name."),
) -> None:
    try:
        with _client(ctx) as client:
            payload = client.character_profile_variants(region=region, realm=realm, name=name)
    except httpx.HTTPStatusError as exc:
        _handle_http_error(ctx, exc)
        return
    raid_rows = _raid_progression_summary(payload.get("raid_progression") or {})
    recent_runs = payload.get("mythic_plus_recent_runs") if isinstance(payload.get("mythic_plus_recent_runs"), list) else []
    scores = payload.get("mythic_plus_scores_by_season") if isinstance(payload.get("mythic_plus_scores_by_season"), list) else []
    current_scores = scores[0] if scores else {}
    current_score_value = (((current_scores.get("scores") or {}).get("all")) if isinstance(current_scores, dict) else None)
    current_score_color = (((current_scores.get("segments") or {}).get("all") or {}).get("color")
                           ) if isinstance(current_scores, dict) else None
    guild = payload.get("guild") if isinstance(payload.get("guild"), dict) else None
    _emit(
        ctx,
        {
            "character": {
                "name": payload.get("name"),
                "region": payload.get("region"),
                "realm": payload.get("realm"),
                "race": payload.get("race"),
                "class_name": payload.get("class"),
                "active_spec_name": payload.get("active_spec_name"),
                "faction": payload.get("faction"),
                "profile_url": payload.get("profile_url"),
                "thumbnail_url": payload.get("thumbnail_url"),
            },
            "guild": {
                "name": guild.get("name"),
                "realm": guild.get("realm"),
                "region": guild.get("region"),
            } if guild else None,
            "mythic_plus": {
                "season": current_scores.get("season") if isinstance(current_scores, dict) else None,
                "current_score": current_score_value,
                "current_score_color": current_score_color,
                "ranks": payload.get("mythic_plus_ranks"),
                "recent_run_count": len(recent_runs),
                "recent_runs": [_recent_run_summary(row) for row in recent_runs[:5] if isinstance(row, dict)],
            },
            "raiding": {
                "raid_count": len(raid_rows),
                "progression": raid_rows,
            },
            "citations": {
                "profile": payload.get("profile_url"),
            },
        },
    )


@app.command("guild")
def guild(
    ctx: typer.Context,
    region: str = typer.Argument(..., help="Region slug such as us or eu."),
    realm: str = typer.Argument(..., help="Realm slug or title."),
    name: str = typer.Argument(..., help="Guild name."),
) -> None:
    try:
        with _client(ctx) as client:
            payload = client.guild_profile_variants(region=region, realm=realm, name=name)
    except httpx.HTTPStatusError as exc:
        _handle_http_error(ctx, exc)
        return
    members = payload.get("members") if isinstance(payload.get("members"), list) else []
    raid_progression = _raid_progression_summary(payload.get("raid_progression") or {})
    raid_rankings = _guild_rankings_summary(payload.get("raid_rankings") or {})
    _emit(
        ctx,
        {
            "guild": {
                "name": payload.get("name"),
                "region": payload.get("region"),
                "realm": payload.get("realm"),
                "faction": payload.get("faction"),
                "profile_url": payload.get("profile_url"),
                "member_count": len(members),
            },
            "raiding": {
                "raid_count": len(raid_progression),
                "progression": raid_progression,
                "rankings": raid_rankings,
            },
            "roster_preview": [
                {
                    "name": ((row.get("character") or {}).get("name") if isinstance(row, dict) else None),
                    "realm": ((row.get("character") or {}).get("realm") if isinstance(row, dict) else None),
                    "class_name": ((row.get("character") or {}).get("class") if isinstance(row, dict) else None),
                    "active_spec_name": ((row.get("character") or {}).get("active_spec_name") if isinstance(row, dict) else None),
                }
                for row in members[:10]
                if isinstance(row, dict)
            ],
            "citations": {
                "profile": payload.get("profile_url"),
            },
        },
    )


@app.command("mythic-plus-runs")
def mythic_plus_runs(
    ctx: typer.Context,
    season: str = typer.Option("", "--season", help="Season slug. Defaults to Raider.IO current default season."),
    region: str = typer.Option("world", "--region", help="Region slug such as world, us, or eu."),
    dungeon: str = typer.Option("all", "--dungeon", help="Dungeon slug or all."),
    affixes: str = typer.Option("", "--affixes", help="Affix slug, fortified, tyrannical, current, or all."),
    page: int = typer.Option(0, "--page", min=0, help="Page of rankings to request."),
) -> None:
    try:
        with _client(ctx) as client:
            payload = client.mythic_plus_runs(
                season=season or None,
                region=region,
                dungeon=dungeon,
                affixes=affixes or None,
                page=page,
            )
    except httpx.HTTPStatusError as exc:
        _handle_http_error(ctx, exc)
        return
    rankings = payload.get("rankings") if isinstance(payload.get("rankings"), list) else []
    _emit(
        ctx,
        {
            "query": {
                "season": payload.get("season") or season or None,
                "region": payload.get("region") or region,
                "dungeon": payload.get("dungeon") or dungeon,
                "affixes": affixes or None,
                "page": page,
            },
            "count": len(rankings),
            "runs": [_ranking_run_summary(row) for row in rankings],
        },
    )


@sample_app.command("mythic-plus-runs")
def sample_mythic_plus_runs(
    ctx: typer.Context,
    season: str = typer.Option("", "--season", help="Season slug. Defaults to Raider.IO current default season."),
    region: str = typer.Option("world", "--region", help="Region slug such as world, us, or eu."),
    dungeon: str = typer.Option("all", "--dungeon", help="Dungeon slug or all."),
    affixes: str = typer.Option("", "--affixes", help="Affix slug, fortified, tyrannical, current, or all."),
    page: int = typer.Option(0, "--page", min=0, help="Starting page of rankings to request."),
    pages: int = typer.Option(1, "--pages", min=1, max=10, help="Number of pages to sample."),
    limit: int = typer.Option(100, "--limit", min=1, max=200, help="Maximum runs to retain in the sample."),
    level_min: int | None = typer.Option(None, "--level-min", min=0, help="Retain only runs at or above this Mythic+ level."),
    level_max: int | None = typer.Option(None, "--level-max", min=0, help="Retain only runs at or below this Mythic+ level."),
    score_min: float | None = typer.Option(None, "--score-min", help="Retain only runs at or above this sampled run score."),
    score_max: float | None = typer.Option(None, "--score-max", help="Retain only runs at or below this sampled run score."),
    contains_role: list[str] | None = typer.Option(
        None, "--contains-role", help="Retain only runs containing at least one roster role. Repeatable."),
    contains_class: list[str] | None = typer.Option(
        None, "--contains-class", help="Retain only runs containing at least one class slug or name. Repeatable."),
    contains_spec: list[str] | None = typer.Option(
        None, "--contains-spec", help="Retain only runs containing at least one spec slug or name. Repeatable."),
    player_region: list[str] | None = typer.Option(
        None, "--player-region", help="Retain only runs containing at least one player from the given region. Repeatable."),
) -> None:
    try:
        with _client(ctx) as client:
            runs, meta, filtering = _load_filtered_runs(
                client,
                season=season or None,
                region=region,
                dungeon=dungeon,
                affixes=affixes or None,
                page=page,
                pages=pages,
                limit=limit,
                level_min=level_min,
                level_max=level_max,
                score_min=score_min,
                score_max=score_max,
                contains_role=contains_role,
                contains_class=contains_class,
                contains_spec=contains_spec,
                player_region=player_region,
            )
    except httpx.HTTPStatusError as exc:
        _handle_http_error(ctx, exc)
        return
    query = {
        "season": meta.get("season") or season or None,
        "region": region,
        "dungeon": dungeon,
        "affixes": affixes or None,
        "page": page,
        "pages": pages,
        "limit": limit,
        "filters": _analytics_filter_query(
            level_min=level_min,
            level_max=level_max,
            score_min=score_min,
            score_max=score_max,
            contains_role=contains_role,
            contains_class=contains_class,
            contains_spec=contains_spec,
            player_region=player_region,
        ),
    }
    _emit(
        ctx,
        {
            "provider": "raiderio",
            "kind": "mythic_plus_runs_sample",
            "query": query,
            "sample": {
                **_sample_summary(runs, meta=meta),
                "filtering": filtering,
            },
            "runs": runs,
            "freshness": {
                "sampled_at": meta["sampled_at"],
                "cache_ttl_seconds": meta["cache_ttl_seconds"],
            },
            "citations": {
                "leaderboard_urls": meta["leaderboard_urls"],
            },
        },
    )


@sample_app.command("mythic-plus-players")
def sample_mythic_plus_players(
    ctx: typer.Context,
    season: str = typer.Option("", "--season", help="Season slug. Defaults to Raider.IO current default season."),
    region: str = typer.Option("world", "--region", help="Region slug such as world, us, or eu."),
    dungeon: str = typer.Option("all", "--dungeon", help="Dungeon slug or all."),
    affixes: str = typer.Option("", "--affixes", help="Affix slug, fortified, tyrannical, current, or all."),
    page: int = typer.Option(0, "--page", min=0, help="Starting page of rankings to request."),
    pages: int = typer.Option(1, "--pages", min=1, max=10, help="Number of pages to sample."),
    limit: int = typer.Option(100, "--limit", min=1, max=200, help="Maximum runs to retain in the source sample."),
    player_limit: int = typer.Option(100, "--player-limit", min=1, max=500, help="Maximum player snapshots to retain after deduping."),
    level_min: int | None = typer.Option(None, "--level-min", min=0, help="Retain only runs at or above this Mythic+ level."),
    level_max: int | None = typer.Option(None, "--level-max", min=0, help="Retain only runs at or below this Mythic+ level."),
    score_min: float | None = typer.Option(None, "--score-min", help="Retain only runs at or above this sampled run score."),
    score_max: float | None = typer.Option(None, "--score-max", help="Retain only runs at or below this sampled run score."),
    contains_role: list[str] | None = typer.Option(
        None, "--contains-role", help="Retain only runs containing at least one roster role. Repeatable."),
    contains_class: list[str] | None = typer.Option(
        None, "--contains-class", help="Retain only runs containing at least one class slug or name. Repeatable."),
    contains_spec: list[str] | None = typer.Option(
        None, "--contains-spec", help="Retain only runs containing at least one spec slug or name. Repeatable."),
    player_region: list[str] | None = typer.Option(
        None, "--player-region", help="Retain only runs containing at least one player from the given region. Repeatable."),
) -> None:
    try:
        with _client(ctx) as client:
            runs, meta, filtering = _load_filtered_runs(
                client,
                season=season or None,
                region=region,
                dungeon=dungeon,
                affixes=affixes or None,
                page=page,
                pages=pages,
                limit=limit,
                level_min=level_min,
                level_max=level_max,
                score_min=score_min,
                score_max=score_max,
                contains_role=contains_role,
                contains_class=contains_class,
                contains_spec=contains_spec,
                player_region=player_region,
            )
    except httpx.HTTPStatusError as exc:
        _handle_http_error(ctx, exc)
        return
    players, player_sampling = _limit_player_snapshots(_player_snapshots(runs), player_limit=player_limit)
    query = {
        "season": meta.get("season") or season or None,
        "region": region,
        "dungeon": dungeon,
        "affixes": affixes or None,
        "page": page,
        "pages": pages,
        "limit": limit,
        "player_limit": player_limit,
        "filters": _analytics_filter_query(
            level_min=level_min,
            level_max=level_max,
            score_min=score_min,
            score_max=score_max,
            contains_role=contains_role,
            contains_class=contains_class,
            contains_spec=contains_spec,
            player_region=player_region,
        ),
    }
    _emit(
        ctx,
        {
            "provider": "raiderio",
            "kind": "mythic_plus_players_sample",
            "query": query,
            "sample": _player_sample_summary(players, runs=runs, meta=meta, filtering=filtering, player_sampling=player_sampling),
            "players": players,
            "freshness": {
                "sampled_at": meta["sampled_at"],
                "cache_ttl_seconds": meta["cache_ttl_seconds"],
            },
            "citations": {
                "leaderboard_urls": meta["leaderboard_urls"],
            },
        },
    )


@distribution_app.command("mythic-plus-runs")
def distribution_mythic_plus_runs(
    ctx: typer.Context,
    metric: str = typer.Option("mythic_level", "--metric", help="Distribution metric: mythic_level, dungeon, role, or player_region."),
    season: str = typer.Option("", "--season", help="Season slug. Defaults to Raider.IO current default season."),
    region: str = typer.Option("world", "--region", help="Region slug such as world, us, or eu."),
    dungeon: str = typer.Option("all", "--dungeon", help="Dungeon slug or all."),
    affixes: str = typer.Option("", "--affixes", help="Affix slug, fortified, tyrannical, current, or all."),
    page: int = typer.Option(0, "--page", min=0, help="Starting page of rankings to request."),
    pages: int = typer.Option(1, "--pages", min=1, max=10, help="Number of pages to sample."),
    limit: int = typer.Option(100, "--limit", min=1, max=200, help="Maximum runs to retain in the sample."),
    level_min: int | None = typer.Option(None, "--level-min", min=0, help="Retain only runs at or above this Mythic+ level."),
    level_max: int | None = typer.Option(None, "--level-max", min=0, help="Retain only runs at or below this Mythic+ level."),
    score_min: float | None = typer.Option(None, "--score-min", help="Retain only runs at or above this sampled run score."),
    score_max: float | None = typer.Option(None, "--score-max", help="Retain only runs at or below this sampled run score."),
    contains_role: list[str] | None = typer.Option(
        None, "--contains-role", help="Retain only runs containing at least one roster role. Repeatable."),
    contains_class: list[str] | None = typer.Option(
        None, "--contains-class", help="Retain only runs containing at least one class slug or name. Repeatable."),
    contains_spec: list[str] | None = typer.Option(
        None, "--contains-spec", help="Retain only runs containing at least one spec slug or name. Repeatable."),
    player_region: list[str] | None = typer.Option(
        None, "--player-region", help="Retain only runs containing at least one player from the given region. Repeatable."),
) -> None:
    if metric not in {"mythic_level", "dungeon", "role", "player_region", "class", "spec", "composition", "class_composition"}:
        _fail(
            ctx,
            "invalid_query",
            "--metric must be one of: mythic_level, dungeon, role, player_region, "
            "class, spec, composition, class_composition",
        )
        return
    try:
        with _client(ctx) as client:
            runs, meta, filtering = _load_filtered_runs(
                client,
                season=season or None,
                region=region,
                dungeon=dungeon,
                affixes=affixes or None,
                page=page,
                pages=pages,
                limit=limit,
                level_min=level_min,
                level_max=level_max,
                score_min=score_min,
                score_max=score_max,
                contains_role=contains_role,
                contains_class=contains_class,
                contains_spec=contains_spec,
                player_region=player_region,
            )
    except httpx.HTTPStatusError as exc:
        _handle_http_error(ctx, exc)
        return
    query = {
        "season": meta.get("season") or season or None,
        "region": region,
        "dungeon": dungeon,
        "affixes": affixes or None,
        "page": page,
        "pages": pages,
        "limit": limit,
        "filters": _analytics_filter_query(
            level_min=level_min,
            level_max=level_max,
            score_min=score_min,
            score_max=score_max,
            contains_role=contains_role,
            contains_class=contains_class,
            contains_spec=contains_spec,
            player_region=player_region,
        ),
    }
    payload = _distribution_payload(metric, runs, meta=meta, query=query)
    if isinstance(payload.get("sample"), dict):
        payload["sample"]["filtering"] = filtering
    _emit(ctx, payload)


@distribution_app.command("mythic-plus-players")
def distribution_mythic_plus_players(
    ctx: typer.Context,
    metric: str = typer.Option("appearance_count", "--metric",
                               help="Distribution metric: appearance_count, top_mythic_level, class, spec, role, or player_region."),
    season: str = typer.Option("", "--season", help="Season slug. Defaults to Raider.IO current default season."),
    region: str = typer.Option("world", "--region", help="Region slug such as world, us, or eu."),
    dungeon: str = typer.Option("all", "--dungeon", help="Dungeon slug or all."),
    affixes: str = typer.Option("", "--affixes", help="Affix slug, fortified, tyrannical, current, or all."),
    page: int = typer.Option(0, "--page", min=0, help="Starting page of rankings to request."),
    pages: int = typer.Option(1, "--pages", min=1, max=10, help="Number of pages to sample."),
    limit: int = typer.Option(100, "--limit", min=1, max=200, help="Maximum runs to retain in the source sample."),
    player_limit: int = typer.Option(100, "--player-limit", min=1, max=500, help="Maximum player snapshots to retain after deduping."),
    level_min: int | None = typer.Option(None, "--level-min", min=0, help="Retain only runs at or above this Mythic+ level."),
    level_max: int | None = typer.Option(None, "--level-max", min=0, help="Retain only runs at or below this Mythic+ level."),
    score_min: float | None = typer.Option(None, "--score-min", help="Retain only runs at or above this sampled run score."),
    score_max: float | None = typer.Option(None, "--score-max", help="Retain only runs at or below this sampled run score."),
    contains_role: list[str] | None = typer.Option(
        None, "--contains-role", help="Retain only runs containing at least one roster role. Repeatable."),
    contains_class: list[str] | None = typer.Option(
        None, "--contains-class", help="Retain only runs containing at least one class slug or name. Repeatable."),
    contains_spec: list[str] | None = typer.Option(
        None, "--contains-spec", help="Retain only runs containing at least one spec slug or name. Repeatable."),
    player_region: list[str] | None = typer.Option(
        None, "--player-region", help="Retain only runs containing at least one player from the given region. Repeatable."),
) -> None:
    if metric not in {"appearance_count", "top_mythic_level", "class", "spec", "role", "player_region"}:
        _fail(ctx, "invalid_query", "--metric must be one of: appearance_count, top_mythic_level, class, spec, role, player_region")
        return
    try:
        with _client(ctx) as client:
            runs, meta, filtering = _load_filtered_runs(
                client,
                season=season or None,
                region=region,
                dungeon=dungeon,
                affixes=affixes or None,
                page=page,
                pages=pages,
                limit=limit,
                level_min=level_min,
                level_max=level_max,
                score_min=score_min,
                score_max=score_max,
                contains_role=contains_role,
                contains_class=contains_class,
                contains_spec=contains_spec,
                player_region=player_region,
            )
    except httpx.HTTPStatusError as exc:
        _handle_http_error(ctx, exc)
        return
    players, player_sampling = _limit_player_snapshots(_player_snapshots(runs), player_limit=player_limit)
    query = {
        "season": meta.get("season") or season or None,
        "region": region,
        "dungeon": dungeon,
        "affixes": affixes or None,
        "page": page,
        "pages": pages,
        "limit": limit,
        "player_limit": player_limit,
        "filters": _analytics_filter_query(
            level_min=level_min,
            level_max=level_max,
            score_min=score_min,
            score_max=score_max,
            contains_role=contains_role,
            contains_class=contains_class,
            contains_spec=contains_spec,
            player_region=player_region,
        ),
    }
    _emit(
        ctx,
        _player_distribution_payload(
            metric,
            players,
            runs=runs,
            meta=meta,
            query=query,
            filtering=filtering,
            player_sampling=player_sampling,
        ),
    )


@threshold_app.command("mythic-plus-runs")
def threshold_mythic_plus_runs(
    ctx: typer.Context,
    metric: str = typer.Option("score", "--metric", help="Threshold metric: score or mythic_level."),
    value: float = typer.Option(..., "--value", help="Target metric value to estimate around."),
    season: str = typer.Option("", "--season", help="Season slug. Defaults to Raider.IO current default season."),
    region: str = typer.Option("world", "--region", help="Region slug such as world, us, or eu."),
    dungeon: str = typer.Option("all", "--dungeon", help="Dungeon slug or all."),
    affixes: str = typer.Option("", "--affixes", help="Affix slug, fortified, tyrannical, current, or all."),
    page: int = typer.Option(0, "--page", min=0, help="Starting page of rankings to request."),
    pages: int = typer.Option(1, "--pages", min=1, max=10, help="Number of pages to sample."),
    limit: int = typer.Option(100, "--limit", min=1, max=200, help="Maximum runs to retain in the sample."),
    nearest: int = typer.Option(10, "--nearest", min=1, max=50, help="Number of nearest sampled runs to retain."),
    level_min: int | None = typer.Option(None, "--level-min", min=0, help="Retain only runs at or above this Mythic+ level."),
    level_max: int | None = typer.Option(None, "--level-max", min=0, help="Retain only runs at or below this Mythic+ level."),
    score_min: float | None = typer.Option(None, "--score-min", help="Retain only runs at or above this sampled run score."),
    score_max: float | None = typer.Option(None, "--score-max", help="Retain only runs at or below this sampled run score."),
    contains_role: list[str] | None = typer.Option(
        None, "--contains-role", help="Retain only runs containing at least one roster role. Repeatable."),
    contains_class: list[str] | None = typer.Option(
        None, "--contains-class", help="Retain only runs containing at least one class slug or name. Repeatable."),
    contains_spec: list[str] | None = typer.Option(
        None, "--contains-spec", help="Retain only runs containing at least one spec slug or name. Repeatable."),
    player_region: list[str] | None = typer.Option(
        None, "--player-region", help="Retain only runs containing at least one player from the given region. Repeatable."),
) -> None:
    if metric not in {"score", "mythic_level"}:
        _fail(ctx, "invalid_query", "--metric must be one of: score, mythic_level")
        return
    try:
        with _client(ctx) as client:
            runs, meta, filtering = _load_filtered_runs(
                client,
                season=season or None,
                region=region,
                dungeon=dungeon,
                affixes=affixes or None,
                page=page,
                pages=pages,
                limit=limit,
                level_min=level_min,
                level_max=level_max,
                score_min=score_min,
                score_max=score_max,
                contains_role=contains_role,
                contains_class=contains_class,
                contains_spec=contains_spec,
                player_region=player_region,
            )
    except httpx.HTTPStatusError as exc:
        _handle_http_error(ctx, exc)
        return
    query = {
        "season": meta.get("season") or season or None,
        "region": region,
        "dungeon": dungeon,
        "affixes": affixes or None,
        "page": page,
        "pages": pages,
        "limit": limit,
        "nearest": nearest,
        "filters": _analytics_filter_query(
            level_min=level_min,
            level_max=level_max,
            score_min=score_min,
            score_max=score_max,
            contains_role=contains_role,
            contains_class=contains_class,
            contains_spec=contains_spec,
            player_region=player_region,
        ),
    }
    payload = _threshold_payload(metric, value, runs, meta=meta, query=query, nearest_limit=nearest)
    if isinstance(payload.get("sample"), dict):
        payload["sample"]["filtering"] = filtering
    _emit(ctx, payload)


def run() -> None:
    app()


if __name__ == "__main__":
    run()

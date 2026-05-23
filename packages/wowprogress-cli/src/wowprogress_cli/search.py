from __future__ import annotations

import re
from typing import Any

import typer
from warcraft_core.wow_normalization import normalize_name, normalize_region, realm_matches, realm_slug_variants

from wowprogress_cli.client import WowProgressClient, WowProgressClientError
from wowprogress_cli.context import _client, _handle_client_error
from wowprogress_cli.identity import _follow_up

EXCLUDED_QUERY_TERMS = frozenset(
    {
        "recruit",
        "recruiting",
        "recruitment",
        "apply",
        "application",
        "applications",
        "roster",
        "progression",
    }
)


def _structured_search_hint(query: str) -> dict[str, Any]:
    return {
        "provider": "wowprogress",
        "query": query,
        "search_query": query,
        "count": 0,
        "results": [],
        "truncated": False,
        "message": (
            "WowProgress search expects structured queries like 'us illidan Liquid', "
            "'guild us illidan Liquid', or 'character us illidan Imonthegcd'."
        ),
        "suggested_queries": [
            "us illidan Liquid",
            "guild us illidan Liquid",
            "character us illidan Imonthegcd",
        ],
    }


def _query_tokens(query: str) -> list[str]:
    return [token for token in query.strip().split() if token]


def _strip_excluded_terms(tokens: list[str]) -> tuple[list[str], list[str]]:
    kept = list(tokens)
    excluded: list[str] = []
    while kept and kept[-1].lower() in EXCLUDED_QUERY_TERMS:
        excluded.insert(0, kept.pop())
    return kept, excluded


def _structured_candidates(tokens: list[str]) -> list[tuple[str, str]]:
    if len(tokens) < 3:
        return []
    trailing = tokens[1:]
    candidates: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for split_index in range(len(trailing) - 1, 0, -1):
        name = normalize_name(" ".join(trailing[split_index:]).strip())
        for realm in realm_slug_variants(" ".join(trailing[:split_index])):
            candidate = (realm, name)
            if realm and name and candidate not in seen:
                candidates.append(candidate)
                seen.add(candidate)
    return candidates


def _normalize_structured_query(query: str) -> tuple[str, str | None, str | None, list[tuple[str, str]], list[str]]:
    tokens = _query_tokens(query)
    kind: str | None = None
    kept: list[str] = []
    for token in tokens:
        lower = token.lower()
        if kind is None and lower in {"guild", "guilds"}:
            kind = "guild"
            continue
        if kind is None and lower in {"character", "characters", "char"}:
            kind = "character"
            continue
        kept.append(token)
    kept, excluded_terms = _strip_excluded_terms(kept)
    if len(kept) < 3:
        normalized = " ".join(kept).strip() or query.strip()
        return normalized, kind, None, [], excluded_terms
    region = normalize_region(kept[0])
    candidates = _structured_candidates(kept)
    if not candidates:
        normalized = " ".join(kept).strip()
        return normalized, kind, region, [], excluded_terms
    primary_realm, primary_name = candidates[0]
    normalized = " ".join(part for part in ([kind] if kind else []) + [region, primary_realm, primary_name]).strip()
    return normalized, kind, region, candidates, excluded_terms


def _normalized_token_text(value: str) -> str:
    lowered = value.strip().lower()
    parts = [part for part in re.split(r"[^a-z0-9]+", lowered) if part]
    return " ".join(parts)


def _normalized_realm_matches(query_realm: str, resolved_realm: str) -> bool:
    return realm_matches(query_realm, resolved_realm)


def _query_terms(query: str) -> list[str]:
    return [part for part in query.lower().split() if part]


def _combined_match_text(*parts: str) -> str:
    return " ".join(parts).lower()


def _score_reason_bonus(*, reasons: list[str], condition: bool, amount: int, reason: str) -> int:
    if condition:
        reasons.append(reason)
        return amount
    return 0


def _score_match(
    *,
    query: str,
    kind_hint: str | None,
    kind: str,
    name: str,
    region: str,
    realm: str,
    query_name: str,
    query_realm: str,
) -> tuple[int, list[str]]:
    lowered_query = query.lower()
    name_lower = name.lower()
    combined = _combined_match_text(name, realm, region)
    normalized_name = _normalized_token_text(name)
    normalized_query_name = _normalized_token_text(query_name)
    normalized_realm = _normalized_token_text(realm)
    normalized_query_realm = _normalized_token_text(query_realm)
    score = 0
    reasons: list[str] = ["route_resolved"]
    score += _score_reason_bonus(
        reasons=reasons,
        condition=bool(normalized_query_name and normalized_query_name == normalized_name),
        amount=35,
        reason="exact_target_name",
    )
    if lowered_query == name_lower:
        score += _score_reason_bonus(reasons=reasons, condition=True, amount=50, reason="exact_name")
    elif lowered_query in name_lower:
        score += _score_reason_bonus(reasons=reasons, condition=True, amount=20, reason="name_contains_query")
    score += _score_reason_bonus(
        reasons=reasons,
        condition=_normalized_realm_matches(normalized_query_realm, normalized_realm),
        amount=15,
        reason="exact_target_realm",
    )
    terms = _query_terms(query)
    score += _score_reason_bonus(
        reasons=reasons,
        condition=bool(terms) and all(term in combined for term in terms),
        amount=20,
        reason="all_terms_match",
    )
    score += _score_reason_bonus(
        reasons=reasons,
        condition=any(term == region.lower() for term in terms),
        amount=10,
        reason="region_match",
    )
    score += _score_reason_bonus(
        reasons=reasons,
        condition=any(term == realm.lower() for term in terms),
        amount=10,
        reason="realm_match",
    )
    score += _score_reason_bonus(
        reasons=reasons,
        condition=bool(kind_hint and kind_hint == kind),
        amount=15,
        reason="type_hint",
    )
    score += 10
    return score, reasons


def _candidate_from_probe(
    query: str,
    *,
    kind_hint: str | None,
    payload: dict[str, Any],
    query_region: str,
    query_realm: str,
    query_name: str,
) -> dict[str, Any]:
    search_kind = str(payload.get("_search_kind") or "").strip().lower()
    if search_kind == "character":
        return _character_candidate_from_probe(
            query=query,
            kind_hint=kind_hint,
            payload=payload,
            query_region=query_region,
            query_realm=query_realm,
            query_name=query_name,
        )
    return _guild_candidate_from_probe(
        query=query,
        kind_hint=kind_hint,
        payload=payload,
        query_region=query_region,
        query_realm=query_realm,
        query_name=query_name,
    )


def _character_candidate_from_probe(
    *,
    query: str,
    kind_hint: str | None,
    payload: dict[str, Any],
    query_region: str,
    query_realm: str,
    query_name: str,
) -> dict[str, Any]:
    character = payload.get("character") if isinstance(payload.get("character"), dict) else {}
    name = str(character.get("name") or "").strip()
    region = str(character.get("region") or "").strip()
    realm = str(character.get("realm") or "").strip()
    page_url = character.get("page_url")
    score, reasons = _score_match(
        query=query,
        kind_hint=kind_hint,
        kind="character",
        name=name,
        region=region,
        realm=realm,
        query_name=query_name,
        query_realm=query_realm,
    )
    return {
        "provider": "wowprogress",
        "kind": "character",
        "id": page_url or f"character:{region}:{realm}:{name}",
        "name": name,
        "region": region,
        "realm": realm,
        "guild_name": character.get("guild_name"),
        "class_name": character.get("class_name"),
        "race": character.get("race"),
        "level": character.get("level"),
        "profile_url": page_url,
        "ranking": {"score": score, "match_reasons": reasons},
        "follow_up": _follow_up("character", query_region, query_realm, query_name),
    }


def _guild_candidate_from_probe(
    *,
    query: str,
    kind_hint: str | None,
    payload: dict[str, Any],
    query_region: str,
    query_realm: str,
    query_name: str,
) -> dict[str, Any]:
    guild = payload.get("guild") if isinstance(payload.get("guild"), dict) else {}
    name = str(guild.get("name") or "").strip()
    region = str(guild.get("region") or "").strip()
    realm = str(guild.get("realm") or "").strip()
    page_url = guild.get("page_url")
    score, reasons = _score_match(
        query=query,
        kind_hint=kind_hint,
        kind="guild",
        name=name,
        region=region,
        realm=realm,
        query_name=query_name,
        query_realm=query_realm,
    )
    return {
        "provider": "wowprogress",
        "kind": "guild",
        "id": page_url or f"guild:{region}:{realm}:{name}",
        "name": name,
        "region": region,
        "realm": realm,
        "faction": guild.get("faction"),
        "profile_url": page_url,
        "ranking": {"score": score, "match_reasons": reasons},
        "follow_up": _follow_up("guild", query_region, query_realm, query_name),
    }


def _search_payload(
    *,
    query: str,
    normalized_query: str,
    kind_hint: str | None,
    candidates: list[dict[str, Any]],
    limit: int,
    message: str | None = None,
) -> dict[str, Any]:
    sorted_rows = _sorted_search_candidates(candidates)
    return {
        "provider": "wowprogress",
        "query": query,
        "search_query": normalized_query,
        "query_kind": kind_hint,
        "count": len(sorted_rows),
        "results": sorted_rows[:limit],
        "truncated": len(sorted_rows) > limit,
        "message": message,
    }


def _candidate_score(candidate: dict[str, Any]) -> int:
    return int(((candidate.get("ranking") or {}).get("score")) or 0)


def _sorted_search_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        candidates,
        key=lambda item: (-_candidate_score(item), str(item.get("kind") or ""), str(item.get("name") or "")),
    )


def _distinct_result_kinds(results: list[dict[str, Any]]) -> list[str]:
    return sorted(
        {
            str(item.get("kind") or "").strip().lower()
            for item in results
            if isinstance(item, dict) and str(item.get("kind") or "").strip()
        }
    )


def _resolve_is_confident(
    *,
    best: dict[str, Any] | None,
    second: dict[str, Any] | None,
    query_kind: str | None,
    distinct_kinds: list[str],
) -> bool:
    if best is None:
        return False
    if not _has_follow_up_command(best):
        return False
    best_score = _candidate_score(best)
    second_score = _candidate_score(second) if second is not None else 0
    if not _meets_score_confidence(best_score, second_score=second_score, has_second=second is not None):
        return False
    return not _is_ambiguous_untyped_result(query_kind, distinct_kinds)


def _resolve_confidence_label(best: dict[str, Any] | None, *, resolved: bool) -> str:
    if best is None:
        return "none"
    if resolved:
        return "high"
    if _candidate_score(best) >= 40:
        return "medium"
    return "low"


def _has_follow_up_command(candidate: dict[str, Any]) -> bool:
    follow_up = candidate.get("follow_up") if isinstance(candidate.get("follow_up"), dict) else {}
    return bool(follow_up.get("command"))


def _meets_score_confidence(best_score: int, *, second_score: int, has_second: bool) -> bool:
    return best_score >= 55 and (not has_second or best_score - second_score >= 15)


def _is_ambiguous_untyped_result(query_kind: str | None, distinct_kinds: list[str]) -> bool:
    return query_kind is None and len(distinct_kinds) > 1


def _resolve_payload(search_payload: dict[str, Any]) -> dict[str, Any]:
    results = search_payload.get("results")
    if not isinstance(results, list):
        results = []
    best = results[0] if results else None
    second = results[1] if len(results) > 1 else None
    query_kind = search_payload.get("query_kind")
    distinct_kinds = _distinct_result_kinds(results)
    follow_up = best.get("follow_up") if isinstance((best or {}).get("follow_up"), dict) else {}
    resolved = _resolve_is_confident(best=best, second=second, query_kind=query_kind, distinct_kinds=distinct_kinds)
    confidence = _resolve_confidence_label(best, resolved=resolved)
    return {
        "provider": "wowprogress",
        "query": search_payload.get("query"),
        "search_query": search_payload.get("search_query"),
        "query_kind": query_kind,
        "resolved": resolved,
        "confidence": confidence,
        "match": best,
        "next_command": follow_up.get("command") if resolved else None,
        "fallback_search_command": None if resolved else f'wowprogress search "{search_payload.get("search_query")}"',
        "candidates": results,
        "message": search_payload.get("message"),
    }


def _probe_search_candidates(
    *,
    client: WowProgressClient,
    kind_hint: str | None,
    region: str,
    query_candidates: list[tuple[str, str]],
) -> list[dict[str, Any]]:
    probe_types = ["char", "guild"]
    if kind_hint == "character":
        probe_types = ["char"]
    elif kind_hint == "guild":
        probe_types = ["guild"]
    candidates: list[dict[str, Any]] = []
    for realm, name in query_candidates:
        match_query = " ".join(part for part in (region, realm, name) if part)
        split_results: list[dict[str, Any]] = []
        for probe_type in probe_types:
            payload = client.probe_search_route(region=region, realm=realm, name=name, obj_type=probe_type)
            if payload is None:
                continue
            split_results.append(
                _candidate_from_probe(
                    match_query,
                    kind_hint=kind_hint,
                    payload=payload,
                    query_region=region,
                    query_realm=realm,
                    query_name=name,
                )
            )
            if kind_hint is not None:
                break
        if split_results:
            candidates.extend(split_results)
            break
    return candidates


def _search_candidates(ctx: typer.Context, query: str, *, limit: int) -> dict[str, Any]:
    normalized_query, kind_hint, region, query_candidates, excluded_terms = _normalize_structured_query(query)
    if region is None or not query_candidates:
        payload = _structured_search_hint(query)
        if excluded_terms:
            payload["excluded_terms"] = excluded_terms
        return payload
    candidates: list[dict[str, Any]] = []
    try:
        with _client(ctx) as client:
            candidates = _probe_search_candidates(
                client=client,
                kind_hint=kind_hint,
                region=region,
                query_candidates=query_candidates,
            )
    except WowProgressClientError as exc:
        _handle_client_error(ctx, exc)
        raise AssertionError("unreachable") from None
    message = None if candidates else "WowProgress did not resolve that structured guild or character query."
    payload = _search_payload(
        query=query,
        normalized_query=normalized_query,
        kind_hint=kind_hint,
        candidates=candidates,
        limit=limit,
        message=message,
    )
    if excluded_terms:
        payload["excluded_terms"] = excluded_terms
        payload["normalization_hint"] = {
            "code": "excluded_query_terms",
            "message": (
                "Trailing query terms were excluded to keep the WowProgress lookup "
                "on a supported structured guild or character route."
            ),
        }
    if query_candidates:
        payload["normalized_candidates"] = [
            {
                "region": region,
                "realm": realm,
                "name": name,
            }
            for realm, name in query_candidates
        ]
    return payload

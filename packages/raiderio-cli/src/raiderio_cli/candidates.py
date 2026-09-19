"""Search and resolve candidate construction: query normalization, match scoring, and ranking.

``provider`` calls into this module for every ``search``/``resolve`` payload. Nothing here performs
I/O beyond the structured probe, and nothing here prints or raises ``typer.Exit``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
from warcraft_core.shapes import as_dict
from warcraft_core.wow_normalization import normalize_name, normalize_region, primary_realm_slug, realm_slug_variants

from raiderio_cli.client import RaiderIOClient
from raiderio_cli.identity import raiderio_class_spec_identity

STRUCTURED_REGIONS = frozenset({"us", "eu", "kr", "tw", "cn"})
# Realm display names run to three words ("Sisters of Elune"), so a structured query is split at
# every realm length up to three rather than assuming the realm is one token.
MAX_REALM_TOKENS = 3


@dataclass(frozen=True, slots=True)
class StructuredProbe:
    """One `<region> <realm> <name>` reading of a query, ready to be looked up directly."""

    region: str
    realm: str
    name: str


TYPE_HINT_TOKENS = {
    "guild": "guild",
    "guilds": "guild",
    "character": "character",
    "characters": "character",
    "char": "character",
}


def _normalize_search_query(query: str) -> tuple[str, str | None]:
    """Split a leading or trailing ``guild``/``character`` hint off the query.

    Only the outer tokens count. Realm and guild names contain these words ("Sisters of Elune
    Guild Wars"), and stripping the word wherever it appears searches for a name nobody has.
    """
    tokens = [token for token in query.strip().split() if token]
    if not tokens:
        return query.strip(), None
    type_hint = TYPE_HINT_TOKENS.get(tokens[0].lower())
    kept = tokens[1:] if type_hint else tokens
    if type_hint is None and len(tokens) > 1:
        type_hint = TYPE_HINT_TOKENS.get(tokens[-1].lower())
        kept = tokens[:-1] if type_hint else tokens
    normalized = " ".join(kept).strip() or query.strip()
    return normalized, type_hint


def normalize_structured_query(query: str) -> tuple[str, str | None, list[StructuredProbe]]:
    """Return the query without its type hint, the hint, and every realm/name split worth probing.

    ``us tarren mill Cotti`` is ambiguous -- the realm may be one or two tokens -- so both readings
    are returned, longest realm last, and the caller probes them in order.
    """
    normalized_query, type_hint = _normalize_search_query(query)
    tokens = [token for token in normalized_query.strip().split() if token]
    if len(tokens) < 3:
        return normalized_query, type_hint, []
    region = normalize_region(tokens[0])
    if region not in STRUCTURED_REGIONS:
        return normalized_query, type_hint, []
    probes: list[StructuredProbe] = []
    for realm_token_count in range(1, min(MAX_REALM_TOKENS, len(tokens) - 2) + 1):
        realm = primary_realm_slug(" ".join(tokens[1 : 1 + realm_token_count]))
        name = normalize_name(" ".join(tokens[1 + realm_token_count :]))
        if realm and name:
            probes.append(StructuredProbe(region=region, realm=realm, name=name))
    return normalized_query, type_hint, probes


def _query_terms(value: str) -> list[str]:
    return [part for part in value.lower().split() if part]


def _combined_match_text(name: str, realm: str | None, region: str | None) -> str:
    """The lowercased text a query term has to appear in, including the realm's slug spellings.

    Raider.IO echoes realm display names (``Mal'Ganis``), so without the slug spellings the CLI
    itself emits (``malganis``, ``mal-ganis``) a query written the way ``next_command`` writes it
    would lose the all-terms credit and never resolve.
    """
    parts = [name, realm, region, *(realm_slug_variants(realm) if realm else [])]
    return " ".join(part for part in parts if part).lower()


def _all_terms_match(query_terms: list[str], combined: str) -> bool:
    return bool(query_terms) and all(term in combined for term in query_terms)


def _realm_term_matches(query_terms: list[str], realm: str) -> bool:
    """True when a run of query terms names ``realm``, compared through the shared slug variants.

    Raider.IO echoes realm display names (``Mal'Ganis``, ``Tarren Mill``), so comparing raw strings
    term by term misses both the slug spellings the CLI itself emits (``malganis``, ``mal-ganis``)
    and every realm written as more than one word.
    """
    realm_variants = set(realm_slug_variants(realm))
    if not realm_variants:
        return False
    spans = (
        " ".join(query_terms[start:end])
        for start in range(len(query_terms))
        for end in range(start + 1, len(query_terms) + 1)
    )
    return any(realm_variants & set(realm_slug_variants(span)) for span in spans)


@dataclass(frozen=True, slots=True)
class _MatchWeights:
    """Points a candidate earns for each way it can match the query."""

    exact_name: int
    contains: int
    all_terms: int
    region: int
    realm: int
    type_hint: int


def _entity_match_score(
    *,
    query: str,
    type_hint: str | None,
    kind: str,
    name: str,
    region: str | None,
    realm: str | None,
    weights: _MatchWeights,
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
        score += weights.exact_name
        reasons.append("exact_name")
    elif lowered_query in name_lower:
        score += weights.contains
        reasons.append("name_contains_query")
    if _all_terms_match(query_terms, combined):
        score += weights.all_terms
        reasons.append("all_terms_match")
    if region and any(term == region.lower() for term in query_terms):
        score += weights.region
        reasons.append("region_match")
    if realm and _realm_term_matches(query_terms, realm):
        score += weights.realm
        reasons.append("realm_match")
    if type_hint and type_hint == kind:
        score += weights.type_hint
        reasons.append("type_hint")
    return score, reasons


def match_reasons(
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
        weights=_MatchWeights(exact_name=50, contains=25, all_terms=20, region=8, realm=10, type_hint=15),
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
        weights=_MatchWeights(exact_name=45, contains=20, all_terms=25, region=12, realm=12, type_hint=20),
        base_reasons=["structured_probe"],
        base_score=12,
    )


def candidate_from_character_profile(
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
        "class_spec_identity": raiderio_class_spec_identity(
            payload.get("class"), payload.get("active_spec_name"), source="resolve_character_profile"
        ),
        "profile_url": payload.get("profile_url"),
        "ranking": {
            "score": score,
            "match_reasons": reasons,
        },
        "follow_up": _follow_up_for_match("character", query_region, query_realm, query_name),
    }


def candidate_from_guild_profile(
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


def _probe_one_split(
    client: RaiderIOClient,
    probe: StructuredProbe,
    *,
    query: str,
    type_hint: str | None,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    probe_kinds = [type_hint] if type_hint in {"character", "guild"} else ["character", "guild"]
    for probe_kind in probe_kinds:
        builder = candidate_from_character_profile if probe_kind == "character" else candidate_from_guild_profile
        fetch = client.character_profile_variants if probe_kind == "character" else client.guild_profile_variants
        try:
            payload = fetch(region=probe.region, realm=probe.realm, name=probe.name)
        except httpx.HTTPStatusError as exc:
            # 400/404 is "no such character/guild on that realm", which is the normal answer for a
            # split that read the wrong number of realm tokens; anything else is a real failure.
            if exc.response.status_code in {400, 404}:
                continue
            raise
        candidates.append(
            builder(
                query=query,
                type_hint=type_hint,
                payload=payload,
                query_region=probe.region,
                query_realm=probe.realm,
                query_name=probe.name,
            )
        )
    return candidates


def probe_structured_candidates(
    client: RaiderIOClient,
    *,
    query: str,
    type_hint: str | None,
    probes: list[StructuredProbe],
) -> list[dict[str, Any]]:
    """Look the query up directly, stopping at the first realm/name split that exists upstream."""
    for probe in probes:
        candidates = _probe_one_split(client, probe, query=query, type_hint=type_hint)
        if candidates:
            return candidates
    return []


def search_result_candidate(row: dict[str, Any], *, query: str, type_hint: str | None) -> dict[str, Any] | None:
    kind = str(row.get("type") or "").strip().lower()
    if kind not in {"character", "guild"}:
        return None
    data = as_dict(row.get("data"))
    region_row = as_dict(data.get("region"))
    realm_row = as_dict(data.get("realm"))
    class_row = as_dict(data.get("class"))
    name = str(data.get("displayName") or data.get("name") or row.get("name") or "").strip()
    region = str(region_row.get("slug") or "").strip() or None
    realm = str(realm_row.get("slug") or "").strip() or None
    score, reasons = match_reasons(
        query=query,
        type_hint=type_hint,
        kind=kind,
        name=name,
        region=region,
        realm=realm,
    )
    path = data.get("path")
    profile_url = f"https://raider.io{path}" if isinstance(path, str) and path.startswith("/") else None
    candidate: dict[str, Any] = {
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
    # Guild candidates have no actor class/spec, so identity is character-only (class-only here:
    # search rows expose class but not spec).
    if kind == "character":
        candidate["class_spec_identity"] = raiderio_class_spec_identity(
            class_row.get("name"), None, source="search_result"
        )
    return candidate


def search_result_candidates(
    raw_matches: list[dict[str, Any]],
    *,
    query: str,
    type_hint: str | None,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for row in raw_matches:
        candidate = search_result_candidate(row, query=query, type_hint=type_hint)
        if candidate is not None:
            results.append(candidate)
    return results


def candidate_dedupe_key(row: dict[str, Any]) -> tuple[str, str | None, str | None, str]:
    return (
        str(row.get("kind") or ""),
        (str(row.get("region") or "").lower() or None),
        (str(row.get("realm") or "").lower() or None),
        str(row.get("name") or ""),
    )


def candidate_ranking_score(row: dict[str, Any]) -> int:
    return int(((row.get("ranking") or {}).get("score")) or 0)


def dedupe_search_candidates(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: dict[tuple[str, str | None, str | None, str], dict[str, Any]] = {}
    for row in results:
        key = candidate_dedupe_key(row)
        existing = deduped.get(key)
        if existing is None or candidate_ranking_score(row) > candidate_ranking_score(existing):
            deduped[key] = row
    return list(deduped.values())


def sorted_search_candidates(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        results,
        key=lambda item: (-candidate_ranking_score(item), str(item.get("kind") or ""), str(item.get("name") or "")),
    )


def resolve_candidate_is_confident(top: list[dict[str, Any]]) -> bool:
    best_score = candidate_ranking_score(top[0])
    second_score = candidate_ranking_score(top[1]) if len(top) > 1 else 0
    return best_score >= 45 and (len(top) == 1 or best_score - second_score >= 15)


def resolve_confidence_label(best_score: int, *, resolved: bool) -> str:
    if resolved:
        return "high"
    if best_score >= 30:
        return "medium"
    return "low"

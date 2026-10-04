"""Search and resolve candidate construction: query normalization, match scoring, and ranking.

``provider`` calls into this module for every ``search``/``resolve`` payload. Nothing here performs
I/O beyond the structured probe, and nothing here prints or raises ``typer.Exit``.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx
from warcraft_core.discovery import ResolveConfidence, discovery_row
from warcraft_core.shapes import as_dict
from warcraft_core.wow_normalization import normalize_name, primary_realm_slug, profile_region, realm_slug_variants

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
    """Split a *leading* ``guild``/``character`` hint off the query.

    Only the first token counts. Entities are named after these words -- Raider.IO has a guild
    called "Liquid Guild" on Illidan -- so a trailing one is part of the name, and dropping it
    searches for something else and answers confidently with the wrong entity.
    """
    tokens = [token for token in query.strip().split() if token]
    if not tokens:
        return query.strip(), None
    type_hint = TYPE_HINT_TOKENS.get(tokens[0].lower())
    normalized = " ".join(tokens[1:]).strip() if type_hint else " ".join(tokens)
    return normalized or query.strip(), type_hint


def normalize_structured_query(query: str) -> tuple[str, str | None, list[StructuredProbe]]:
    """Return the query without its type hint, the hint, and every realm/name split worth probing.

    ``us tarren mill Cotti`` is ambiguous -- the realm may be one or two tokens -- so both readings
    are returned, longest realm last, and the caller probes them in order.
    """
    normalized_query, type_hint = _normalize_search_query(query)
    tokens = [token for token in normalized_query.strip().split() if token]
    if len(tokens) < 3:
        return normalized_query, type_hint, []
    region = profile_region(tokens[0])
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

    Raider.IO echoes realm display names (``Mal'Ganis``), so without the slug spelling the CLI
    itself emits (``malganis``) a query written the way ``next_command`` writes it would lose the
    all-terms credit and never resolve.
    """
    parts = [name, realm, region, *(realm_slug_variants(realm) if realm else [])]
    return " ".join(part for part in parts if part).lower()


def _all_terms_match(query_terms: list[str], combined: str) -> bool:
    """Every term appears in ``combined``, a realm term in any slug spelling (``mal-ganis`` finds ``malganis``)."""
    return bool(query_terms) and all(any(form in combined for form in (term, *realm_slug_variants(term))) for term in query_terms)


def _realm_term_matches(query_terms: list[str], realm: str) -> bool:
    """True when a run of query terms names ``realm``, compared through the shared slug variants.

    Raider.IO echoes realm display names (``Mal'Ganis``, ``Tarren Mill``), so comparing raw strings
    term by term misses both the slug spellings (``malganis``, ``mal-ganis``) and every realm
    written as more than one word.
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
    # A region alias (``oce``, ``na``) names the region the profile lives in, not text in the row.
    region_terms = {term for term in query_terms if region and profile_region(term) == region.lower()}
    if _all_terms_match([term for term in query_terms if term not in region_terms], combined):
        score += weights.all_terms
        reasons.append("all_terms_match")
    if region_terms:
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


def _follow_up_command(kind: str, region: str | None, realm: str | None, name: str) -> str | None:
    """The ``raiderio character|guild`` command for a match, shell-quoted: guild names have spaces."""
    return shlex.join(["raiderio", kind, region, realm, name]) if region and realm else None


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
    return discovery_row(
        provider="raiderio",
        kind="character",
        id=payload.get("id") or payload.get("profile_url") or f"character:{region}:{realm}:{name}",
        name=name,
        url=payload.get("profile_url"),
        score=score,
        match_reasons=reasons,
        command=_follow_up_command("character", query_region, query_realm, query_name),
        surface="character",
        region=region,
        realm=primary_realm_slug(realm) if realm else None,
        realm_name=realm,
        faction=payload.get("faction"),
        class_name=payload.get("class"),
        active_spec_name=payload.get("active_spec_name"),
        class_spec_identity=raiderio_class_spec_identity(
            payload.get("class"), payload.get("active_spec_name"), source="resolve_character_profile"
        ),
    )


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
    return discovery_row(
        provider="raiderio",
        kind="guild",
        id=payload.get("id") or payload.get("profile_url") or f"guild:{region}:{realm}:{name}",
        name=name,
        url=payload.get("profile_url"),
        score=score,
        match_reasons=reasons,
        command=_follow_up_command("guild", query_region, query_realm, query_name),
        surface="guild",
        region=region,
        realm=primary_realm_slug(realm) if realm else None,
        realm_name=realm,
        faction=payload.get("faction"),
    )


def _probe_one_split(
    client: RaiderIOClient,
    probe: StructuredProbe,
    *,
    query: str,
    type_hint: str | None,
    kind: str | None,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for probe_kind in [kind] if kind else ["character", "guild"]:
        builder = candidate_from_character_profile if probe_kind == "character" else candidate_from_guild_profile
        fetch = client.character_profile if probe_kind == "character" else client.guild_profile
        try:
            payload = fetch(region=probe.region, realm=probe.realm, name=probe.name).payload
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
    kind: str | None,
    probes: list[StructuredProbe],
) -> list[dict[str, Any]]:
    """Look the query up directly, stopping at the first realm/name split that exists upstream.

    ``kind`` is the one entity type to look up (``None`` looks up both); ``type_hint`` only scores.
    """
    for probe in probes:
        candidates = _probe_one_split(client, probe, query=query, type_hint=type_hint, kind=kind)
        if candidates:
            return candidates
    return []


def _candidate_url(kind: str, path: Any, *, region: str | None, realm: str | None, name: str) -> str | None:
    if isinstance(path, str) and path.startswith("/"):
        return f"https://raider.io{path}"
    # Site search sends ``path`` for guild rows only; a character's page lives at a fixed layout.
    if kind == "character" and region and realm and name:
        return f"https://raider.io/characters/{region}/{realm}/{quote(name)}"
    return None


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
    url = _candidate_url(kind, path, region=region, realm=realm, name=name)
    command = _follow_up_command(kind, region, realm, name)
    candidate = discovery_row(
        provider="raiderio",
        kind=kind,
        id=data.get("id") or f"{kind}:{region}:{realm}:{name}",
        name=name,
        url=url,
        score=score,
        match_reasons=reasons,
        command=command,
        # "none", as on every provider, so filtering rows on their surface never picks one nothing can open.
        surface=kind if command else "none",
        region=region,
        region_name=region_row.get("name"),
        realm=realm,
        realm_name=realm_row.get("name"),
        faction=data.get("faction"),
        class_name=class_row.get("name"),
        class_slug=class_row.get("slug"),
        path=path,
    )
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


def resolve_confidence(ranked: list[dict[str, Any]]) -> ResolveConfidence:
    """High only for a strong top row with a command and a clear lead over the runner-up."""
    if not ranked:
        return "none"
    best_score = candidate_ranking_score(ranked[0])
    second_score = candidate_ranking_score(ranked[1]) if len(ranked) > 1 else 0
    if ranked[0]["follow_up"]["command"] and best_score >= 45 and (len(ranked) == 1 or best_score - second_score >= 15):
        return "high"
    return "medium" if best_score >= 30 else "low"

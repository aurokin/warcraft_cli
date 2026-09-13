"""Wowhead search ranking, resolve confidence, follow-up commands, and link-record ranking.

This is the stated seam for scoring behavior: ``wowhead_cli.provider``, ``wowhead_cli.guides`` and
``wowhead_cli.main`` all consume it, and the ranking tests target it directly instead of reaching
into ``main``.
"""

from __future__ import annotations

import math
from typing import Any

from wowhead_cli.entity_types import RESOLVE_ENTITY_TYPES, SEARCH_TYPE_HINTS
from wowhead_cli.expansion_profiles import ExpansionProfile, parse_entity_from_wowhead_url, resolve_expansion
from wowhead_cli.wowhead_client import entity_url, guide_url, suggestion_entity_type

FOLLOW_UP_COMMENT_TERMS = {"comment", "comments", "discussion", "discussions"}


FOLLOW_UP_RELATION_TERMS = {
    "body",
    "detail",
    "details",
    "entities",
    "full",
    "link",
    "linked",
    "links",
    "markup",
    "reference",
    "references",
    "related",
    "relation",
    "relations",
    "source",
    "sources",
}


def query_terms(query: str) -> list[str]:
    normalized = " ".join(query.lower().split())
    return [term for term in normalized.split(" ") if term]


def score_text_match(query: str, *values: Any) -> int:
    terms = query_terms(query)
    if not terms:
        return 0
    haystacks = []
    for value in values:
        if isinstance(value, str) and value.strip():
            haystacks.append(value.lower())
    if not haystacks:
        return 0
    score = 0
    for term in terms:
        for haystack in haystacks:
            if term in haystack:
                score += 1
    joined = " ".join(haystacks)
    query_normalized = " ".join(query.lower().split())
    if query_normalized and query_normalized in joined:
        score += max(2, len(terms))
    return score


def search_type_hints(query: str) -> set[str]:
    normalized = " ".join(query.lower().split())
    if not normalized:
        return set()
    hinted: set[str] = set()
    for entity_type, phrases in SEARCH_TYPE_HINTS.items():
        for phrase in phrases:
            if phrase in normalized:
                hinted.add(entity_type)
                break
    return hinted


def search_ranking_query(query: str) -> str:
    filtered_terms = [
        term
        for term in query_terms(query)
        if term not in FOLLOW_UP_COMMENT_TERMS and term not in FOLLOW_UP_RELATION_TERMS
    ]
    if filtered_terms:
        return " ".join(filtered_terms)
    return " ".join(query.lower().split())


def search_query_for_ranking(query: str) -> str:
    """Rank a Wowhead entity URL by its type/id instead of its raw slug text."""
    entity = parse_entity_from_wowhead_url(query)
    if entity is not None:
        entity_type, entity_id = entity
        return f"{entity_type} {entity_id}"
    return search_ranking_query(query)


def search_follow_up_kind(query: str) -> str:
    terms = set(query_terms(query))
    if terms & FOLLOW_UP_COMMENT_TERMS:
        return "comments"
    if terms & FOLLOW_UP_RELATION_TERMS:
        return "relations"
    return "summary"


def search_follow_up(candidate: dict[str, Any], *, query: str, expansion: ExpansionProfile) -> dict[str, Any] | None:
    entity_type = candidate.get("entity_type")
    entity_id = candidate.get("id")
    if not isinstance(entity_type, str) or not isinstance(entity_id, int):
        return None

    prefix = command_prefix_for_expansion(expansion)
    intent = search_follow_up_kind(query)
    if entity_type == "guide":
        guide_command = f"{prefix} guide {entity_id}"
        guide_full_command = f"{prefix} guide-full {entity_id}"
        recommended_command = guide_command
        recommended_surface = "guide"
        reason = "guide_summary"
        alternatives = [guide_full_command]
        if intent == "relations":
            recommended_command = guide_full_command
            recommended_surface = "guide-full"
            reason = "guide_relation_intent"
            alternatives = [guide_command]
        elif intent == "comments":
            reason = "guide_comment_intent"
            alternatives = [guide_full_command]
        return {
            "recommended_surface": recommended_surface,
            "recommended_command": recommended_command,
            "reason": reason,
            "alternatives": alternatives,
        }

    if entity_type not in RESOLVE_ENTITY_TYPES:
        return None

    entity_command = f"{prefix} entity {entity_type} {entity_id}"
    entity_page_command = f"{prefix} entity-page {entity_type} {entity_id}"
    comments_command = f"{prefix} comments {entity_type} {entity_id}"
    recommended_command = entity_command
    recommended_surface = "entity"
    reason = "entity_summary"
    alternatives = [entity_page_command, comments_command]
    if intent == "relations":
        recommended_command = entity_page_command
        recommended_surface = "entity-page"
        reason = "entity_relation_intent"
        alternatives = [entity_command, comments_command]
    elif intent == "comments":
        recommended_command = comments_command
        recommended_surface = "comments"
        reason = "entity_comment_intent"
        alternatives = [entity_command, entity_page_command]
    return {
        "recommended_surface": recommended_surface,
        "recommended_command": recommended_command,
        "reason": reason,
        "alternatives": alternatives,
    }


def str_field(row: dict[str, Any], key: str) -> str:
    """Read a string field from an untyped upstream record, falling back to an empty string."""
    value = row.get(key)
    return value if isinstance(value, str) else ""


def int_field(row: dict[str, Any], key: str) -> int:
    """Read an integer field from an untyped upstream record, falling back to 0."""
    value = row.get(key)
    return value if isinstance(value, int) else 0


def exact_match_score(normalized_query: str, *, name_normalized: str, display_normalized: str) -> tuple[int, list[str]]:
    if normalized_query and name_normalized == normalized_query:
        return 30, ["exact_name"]
    if normalized_query and display_normalized == normalized_query:
        return 26, ["exact_display_name"]
    return 0, []


def prefix_and_contains_score(normalized_query: str, *, name_normalized: str, display_normalized: str) -> tuple[int, list[str]]:
    if normalized_query and name_normalized.startswith(normalized_query):
        return 10, ["name_prefix"]
    if normalized_query and display_normalized.startswith(normalized_query):
        return 8, ["display_name_prefix"]
    if normalized_query and name_normalized and normalized_query in name_normalized:
        return 14, ["name_contains_query"]
    if normalized_query and display_normalized and normalized_query in display_normalized:
        return 12, ["display_name_contains_query"]
    return 0, []


def term_match_score(terms: list[str], *, haystacks: list[str]) -> tuple[int, list[str]]:
    if not terms or not haystacks:
        return 0, []
    joined = " ".join(haystacks)
    if all(term in joined for term in terms):
        return len(terms) * 3, ["all_terms_match"]
    return 0, []


def type_hint_score(query: str, *, entity_type: str | None) -> tuple[int, list[str]]:
    hinted_types = search_type_hints(query)
    if entity_type in hinted_types:
        return 9, ["type_hint"]
    return 0, []


def popularity_score(popularity: int, *, entity_type: str | None) -> tuple[int, list[str]]:
    reasons: list[str] = []
    score = 0
    if popularity > 0:
        score += min(6, int(math.log10(popularity + 1) * 2))
        reasons.append("popularity")
    if entity_type is not None:
        score += 1
    return score, reasons


def search_result_score_and_reasons(
    row: dict[str, Any], *, query: str, ranking_query: str
) -> tuple[int, list[str]]:
    normalized_query = " ".join(ranking_query.lower().split())
    terms = query_terms(ranking_query)
    name = str_field(row, "name")
    display_name = str_field(row, "displayName")
    type_name = str_field(row, "typeName")
    entity_type = suggestion_entity_type(row)
    popularity = int_field(row, "popularity")

    haystacks = [value.lower() for value in (name, display_name, type_name) if value]
    name_normalized = name.lower().strip()
    display_normalized = display_name.lower().strip()
    reasons: list[str] = []
    score = 0

    for part_score, part_reasons in (
        exact_match_score(normalized_query, name_normalized=name_normalized, display_normalized=display_normalized),
        prefix_and_contains_score(normalized_query, name_normalized=name_normalized, display_normalized=display_normalized),
        term_match_score(terms, haystacks=haystacks),
        type_hint_score(query, entity_type=entity_type),
        popularity_score(popularity, entity_type=entity_type),
    ):
        score += part_score
        reasons.extend(part_reasons)

    unique_reasons: list[str] = []
    seen: set[str] = set()
    for reason in reasons:
        if reason in seen:
            continue
        seen.add(reason)
        unique_reasons.append(reason)
    return score, unique_reasons


def normalize_resolve_entity_types(values: list[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in values:
        for candidate in raw.split(","):
            value = candidate.strip().lower()
            if not value:
                continue
            if value not in RESOLVE_ENTITY_TYPES:
                raise ValueError(
                    f"Unsupported resolve entity type {value!r}. Expected one of: {', '.join(sorted(RESOLVE_ENTITY_TYPES))}."
                )
            if value in seen:
                continue
            seen.add(value)
            normalized.append(value)
    return tuple(normalized)


def search_result_url(*, entity_type: str | None, entity_id: int | None, expansion: ExpansionProfile) -> str | None:
    if not isinstance(entity_id, int):
        return None
    if entity_type == "guide":
        return guide_url(entity_id, expansion=expansion)
    if entity_type:
        return entity_url(entity_type, entity_id, expansion=expansion)
    return None


def normalize_search_results(
    results: list[Any],
    *,
    query: str,
    expansion: ExpansionProfile,
    entity_types: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    selected_entity_types = set(entity_types)
    ranking_query = search_ranking_query(query)
    normalized: list[dict[str, Any]] = []
    for index, row in enumerate(results):
        if not isinstance(row, dict):
            continue
        entity_type = suggestion_entity_type(row)
        if selected_entity_types and entity_type not in selected_entity_types:
            continue
        entity_id = row.get("id")
        popularity = int_field(row, "popularity")
        search_score, match_reasons = search_result_score_and_reasons(
            row,
            query=query,
            ranking_query=ranking_query,
        )
        candidate = {
            "id": entity_id,
            "name": row.get("name"),
            "type_id": row.get("type"),
            "type_name": row.get("typeName"),
            "entity_type": entity_type,
            "url": search_result_url(
                entity_type=entity_type,
                entity_id=entity_id if isinstance(entity_id, int) else None,
                expansion=expansion,
            ),
            "ranking": {
                "score": search_score,
                "match_reasons": match_reasons,
            },
            "metadata": {
                "popularity": popularity,
                "icon": row.get("icon"),
                "quality": row.get("quality"),
                "side": row.get("side"),
                "display_name": row.get("displayName"),
            },
            "_sort": (-search_score, -popularity, index),
        }
        follow_up = search_follow_up(candidate, query=query, expansion=expansion)
        if follow_up is not None:
            candidate["follow_up"] = follow_up
        normalized.append(candidate)
    normalized.sort(key=lambda row: row["_sort"])
    for row in normalized:
        row.pop("_sort", None)
    return normalized


def command_prefix_for_expansion(expansion: ExpansionProfile) -> str:
    if expansion.key == resolve_expansion(None).key:
        return "wowhead"
    return f"wowhead --expansion {expansion.key}"


def resolve_next_command(candidate: dict[str, Any], *, expansion: ExpansionProfile) -> str | None:
    follow_up = candidate.get("follow_up") if isinstance(candidate, dict) else None
    if isinstance(follow_up, dict):
        command = follow_up.get("recommended_command")
        if isinstance(command, str) and command:
            return command
    entity_type = candidate.get("entity_type")
    entity_id = candidate.get("id")
    if not isinstance(entity_type, str) or not isinstance(entity_id, int):
        return None
    prefix = command_prefix_for_expansion(expansion)
    if entity_type == "guide":
        return f"{prefix} guide {entity_id}"
    if entity_type in RESOLVE_ENTITY_TYPES:
        return f"{prefix} entity {entity_type} {entity_id}"
    return None


def resolve_confidence(candidates: list[dict[str, Any]], *, entity_types: tuple[str, ...]) -> str:
    if not candidates:
        return "none"
    top_ranking = candidates[0].get("ranking", {})
    top_score = int(top_ranking.get("score") or 0)
    second_score = int(candidates[1].get("ranking", {}).get("score") or 0) if len(candidates) > 1 else 0
    margin = top_score - second_score
    reasons = set(top_ranking.get("match_reasons") or [])
    if is_high_confidence_exact_match(reasons, margin=margin, second_score=second_score):
        return "high"
    if is_high_confidence_score(top_score, margin=margin):
        return "high"
    if is_filtered_high_confidence(entity_types, top_score=top_score, margin=margin):
        return "high"
    if is_medium_confidence_score(top_score, margin=margin):
        return "medium"
    return "low"


def is_high_confidence_exact_match(reasons: set[str], *, margin: int, second_score: int) -> bool:
    return ("exact_name" in reasons or "exact_display_name" in reasons) and (margin >= 4 or second_score == 0)


def is_high_confidence_score(top_score: int, *, margin: int) -> bool:
    return top_score >= 24 and margin >= 6


def is_filtered_high_confidence(entity_types: tuple[str, ...], *, top_score: int, margin: int) -> bool:
    return bool(entity_types) and top_score >= 18 and margin >= 4


def is_medium_confidence_score(top_score: int, *, margin: int) -> bool:
    return top_score >= 18 and margin >= 4


SOURCE_KIND_PRIORITY = {
    "gatherer": 0,
    "href": 1,
}

PREVIEW_TYPE_PRIORITY: dict[str, int] = {
    "npc": 0,
    "quest": 1,
    "spell": 2,
    "object": 3,
    "item": 4,
    "achievement": 5,
    "zone": 6,
    "faction": 7,
    "currency": 8,
    "pet": 9,
    "battle-pet": 10,
    "mount": 11,
    "recipe": 12,
    "transmog-set": 13,
    "guide": 14,
}

CONTEXTUAL_PREVIEW_TYPE_PRIORITY: dict[str, dict[str, int]] = {
    "currency": {
        "npc": 0,
        "quest": 1,
        "spell": 2,
        "object": 3,
        "faction": 4,
        "zone": 5,
        "item": 6,
        "currency": 7,
    },
    "zone": {
        "npc": 0,
        "quest": 1,
        "object": 2,
        "spell": 3,
        "item": 4,
        "zone": 5,
        "faction": 6,
    },
    "item": {
        "npc": 0,
        "quest": 1,
        "spell": 2,
        "achievement": 3,
        "zone": 4,
        "object": 5,
        "item": 6,
    },
    "npc": {
        "npc": 0,
        "quest": 1,
        "spell": 2,
        "item": 3,
        "object": 4,
    },
    "quest": {
        "npc": 0,
        "item": 1,
        "quest": 2,
        "spell": 3,
        "object": 4,
        "currency": 5,
    },
    "guide": {
        "spell": 0,
        "item": 1,
        "npc": 2,
        "quest": 3,
        "object": 4,
    },
}


def link_source_kinds(record: dict[str, Any]) -> list[str]:
    raw_sources = record.get("sources")
    values: list[str] = []
    if isinstance(raw_sources, list):
        for raw in raw_sources:
            if isinstance(raw, str) and raw not in values:
                values.append(raw)
    source_kind = record.get("source_kind")
    if isinstance(source_kind, str) and source_kind not in values:
        values.append(source_kind)
    if not values:
        return []
    return sorted(values, key=lambda value: (SOURCE_KIND_PRIORITY.get(value, 99), value))


def preview_type_rank(record: dict[str, Any], *, source_entity_type: str) -> int:
    entity_type = record.get("entity_type")
    if not isinstance(entity_type, str):
        return 99
    contextual = CONTEXTUAL_PREVIEW_TYPE_PRIORITY.get(source_entity_type)
    if contextual is not None and entity_type in contextual:
        return contextual[entity_type]
    return PREVIEW_TYPE_PRIORITY.get(entity_type, 99)

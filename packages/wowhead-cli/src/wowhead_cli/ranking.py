"""Wowhead search ranking, resolve confidence, follow-up commands, and link-record ranking.

This is the stated seam for scoring behavior: ``wowhead_cli.provider``, ``wowhead_cli.guides`` and
``wowhead_cli.main`` all consume it, and the ranking tests target it directly instead of reaching
into ``main``.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from wowhead_cli.entity_types import PARSER_ENTITY_TYPES, RESOLVE_ENTITY_TYPES, SEARCH_TYPE_HINTS
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
            "command": recommended_command,
            "reason": reason,
            "alternatives": alternatives,
        }

    if entity_type == "news":
        # `news-post` takes a URL, and Wowhead resolves the short /news=<id> form to the article.
        return {
            "recommended_surface": "news-post",
            "command": f"{prefix} news-post {entity_url('news', entity_id, expansion=expansion)}",
            "reason": "news_post_summary",
            "alternatives": [],
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
        "command": recommended_command,
        "reason": reason,
        "alternatives": alternatives,
    }


def str_field(row: dict[str, Any], key: str) -> str:
    """Read a string field from an untyped upstream record, falling back to an empty string."""
    value = row.get(key)
    return value if isinstance(value, str) else ""


EXACT_NAME_SCORE = 30


def exact_match_score(normalized_query: str, *, name_normalized: str, display_normalized: str) -> tuple[int, list[str]]:
    if normalized_query and name_normalized == normalized_query:
        return EXACT_NAME_SCORE, ["exact_name"]
    if normalized_query and display_normalized == normalized_query:
        return 26, ["exact_display_name"]
    return 0, []


def prefix_and_contains_score(normalized_query: str, *, name_normalized: str, display_normalized: str) -> tuple[int, list[str]]:
    """Score the query as a prefix of the name, or failing that as a phrase inside it.

    A title that merely contains the query ("Legion Remix Fury Warrior Guide") never outscores one
    that starts with it, so each contains score sits below both prefix scores.
    """
    if normalized_query and name_normalized.startswith(normalized_query):
        return 10, ["name_prefix"]
    if normalized_query and display_normalized.startswith(normalized_query):
        return 8, ["display_name_prefix"]
    if normalized_query and name_normalized and normalized_query in name_normalized:
        return 6, ["name_contains_query"]
    if normalized_query and display_normalized and normalized_query in display_normalized:
        return 4, ["display_name_contains_query"]
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


# Wowhead's suggestion response carries two overlapping row lists. `results` is the flat list its
# dropdown shows, ordered by the `popularity` ordinal (0 = most viewed), which puts proc spells and
# news posts ahead of the entity a query names. `categories.database` is the relevance order the
# site shows for database entities, and its head row is the entity the query means. Only the leading
# rows earn a bonus: the head bonus clears an exact name match (`exact_name` plus `name_prefix`, 40)
# so upstream's best answer outranks a same-named secondary entity, while the step between ranks
# stays under that, so an exactly named row one rank down still wins.
UPSTREAM_DATABASE_RANK_BONUS: tuple[int, ...] = (42, 28, 14)

SuggestionKey = tuple[int, int]


def suggestion_key(row: dict[str, Any]) -> SuggestionKey | None:
    """Wowhead's own ``(type, id)`` identity for a suggestion row, shared by `results` and `categories`."""
    type_id = row.get("type")
    entity_id = row.get("id")
    if isinstance(type_id, int) and isinstance(entity_id, int):
        return type_id, entity_id
    return None


def upstream_database_ranks(response: dict[str, Any]) -> dict[SuggestionKey, int]:
    """Read Wowhead's relevance order for database entities out of a suggestion response."""
    categories = response.get("categories")
    rows = categories.get("database") if isinstance(categories, dict) else None
    if not isinstance(rows, list):
        return {}
    ranks: dict[SuggestionKey, int] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        key = suggestion_key(row)
        if key is not None and key not in ranks:
            ranks[key] = index
    return ranks


def merge_suggestion_lists(response: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Union Wowhead's `results` with every `categories` list, keeping one row per ``(type, id)``.

    `results` is only the dropdown's ~10 rows. `categories` carries the rest of what Wowhead matched,
    and the entity a query names is sometimes only there: in the captured "argent dawn" response
    (tests/fixtures/wowhead/search_suggestions_argent_dawn.json), Faction 529 "Argent Dawn" heads
    `categories.database` and is not in `results`. Each kept row is a copy of its first occurrence
    with ``suggestion_lists`` naming each list it appeared in once, and the summary reports the rows
    each list sent and how many duplicates the merge removed.
    """
    categories = response.get("categories")
    lists = [("results", response.get("results")), *(categories.items() if isinstance(categories, dict) else ())]
    merged: dict[tuple[int | str, int], dict[str, Any]] = {}
    list_rows: dict[str, int] = {}
    for list_name, rows in lists:
        if not isinstance(rows, list):
            continue
        dict_rows = [row for row in rows if isinstance(row, dict)]
        list_rows[list_name] = len(dict_rows)
        for index, row in enumerate(dict_rows):
            kept = merged.setdefault(suggestion_key(row) or (list_name, index), {**row, "suggestion_lists": []})
            if list_name not in kept["suggestion_lists"]:
                kept["suggestion_lists"].append(list_name)
    received = sum(list_rows.values())
    summary = {
        "rule": "one row per Wowhead (type, id) across `results` and every `categories` list",
        "list_rows": list_rows,
        "rows_received": received,
        "unique_rows": len(merged),
        "duplicates_merged": received - len(merged),
    }
    return list(merged.values()), summary


def database_rank_score(database_rank: int | None, *, entity_type: str | None) -> tuple[int, list[str]]:
    """Score how highly Wowhead's own database ranking placed the row, plus a point for a routable row."""
    score = 1 if entity_type is not None else 0
    if database_rank is None or database_rank >= len(UPSTREAM_DATABASE_RANK_BONUS):
        return score, []
    return score + UPSTREAM_DATABASE_RANK_BONUS[database_rank], ["upstream_database_rank"]


def search_result_score_and_reasons(
    row: dict[str, Any], *, query: str, ranking_query: str, database_rank: int | None = None
) -> tuple[int, list[str]]:
    normalized_query = " ".join(ranking_query.lower().split())
    terms = query_terms(ranking_query)
    name = str_field(row, "name")
    display_name = str_field(row, "displayName")
    type_name = str_field(row, "typeName")
    entity_type = suggestion_entity_type(row)

    haystacks = [value.lower() for value in (name, display_name, type_name) if value]
    name_normalized = name.lower().strip()
    display_normalized = display_name.lower().strip()
    reasons: list[str] = []
    score = 0

    # Wowhead ranks database rows on text the suggestion never shows (descriptions, criteria), so its
    # rank only counts for a row whose own name shares a word with the query.
    named = any(term in name_normalized or term in display_normalized for term in terms)
    for part_score, part_reasons in (
        exact_match_score(normalized_query, name_normalized=name_normalized, display_normalized=display_normalized),
        prefix_and_contains_score(normalized_query, name_normalized=name_normalized, display_normalized=display_normalized),
        term_match_score(terms, haystacks=haystacks),
        type_hint_score(query, entity_type=entity_type),
        database_rank_score(database_rank if named else None, entity_type=entity_type),
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


STALE_GUIDE_REASON = "stale_guide"
STALE_GUIDE_DAYS = 180

_UPDATED_FOOTER_RE = re.compile(r"Updated:\s*(?P<year>\d{4})/(?P<month>\d{2})/(?P<day>\d{2})")


def suggestion_updated_date(row: dict[str, Any]) -> date | None:
    """Wowhead stamps guide suggestions with ``pinFooterText: "Updated: YYYY/MM/DD"``; read that date."""
    match = _UPDATED_FOOTER_RE.search(str_field(row, "pinFooterText"))
    if match is None:
        return None
    try:
        return date(int(match["year"]), int(match["month"]), int(match["day"]))
    except ValueError:
        return None


def mark_stale_guides(candidates: list[dict[str, Any]]) -> None:
    """Flag guide candidates that trail the freshest guide in the same response by a long way.

    Wowhead's suggestion list happily mixes current class guides with guides for retired
    limited-time events, and nothing else in the payload tells them apart. Marking the laggards
    keeps the freshness visible to the caller and stops `resolve` claiming high confidence in one.
    """
    guides = [row for row in candidates if row.get("entity_type") == "guide"]
    updates = [row["metadata"]["updated"] for row in guides if row["metadata"].get("updated")]
    if not updates:
        return
    newest = max(updates)
    for row in guides:
        updated = row["metadata"].get("updated")
        if updated is None or (date.fromisoformat(newest) - date.fromisoformat(updated)).days <= STALE_GUIDE_DAYS:
            continue
        row["ranking"]["match_reasons"].append(STALE_GUIDE_REASON)


def normalize_search_results(
    results: list[Any],
    *,
    query: str,
    expansion: ExpansionProfile,
    entity_types: tuple[str, ...] = (),
    database_ranks: dict[SuggestionKey, int] | None = None,
) -> list[dict[str, Any]]:
    selected_entity_types = set(entity_types)
    ranking_query = search_ranking_query(query)
    ranks = database_ranks or {}
    normalized: list[dict[str, Any]] = []
    for index, row in enumerate(results):
        if not isinstance(row, dict):
            continue
        entity_type = suggestion_entity_type(row)
        if selected_entity_types and entity_type not in selected_entity_types:
            continue
        entity_id = row.get("id")
        popularity = row.get("popularity")
        updated = suggestion_updated_date(row)
        key = suggestion_key(row)
        search_score, match_reasons = search_result_score_and_reasons(
            row,
            query=query,
            ranking_query=ranking_query,
            database_rank=ranks.get(key) if key is not None else None,
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
                # Only `results` rows carry the ordinal; a row that came from `categories` has none.
                "popularity": popularity if isinstance(popularity, int) else None,
                "suggestion_lists": row.get("suggestion_lists"),
                "icon": row.get("icon"),
                "quality": row.get("quality"),
                "side": row.get("side"),
                "display_name": row.get("displayName"),
                "updated": updated.isoformat() if updated is not None else None,
            },
            # The source index is the tiebreak: `results` rows first, in Wowhead's `popularity`
            # order, then the category-only rows in the order Wowhead listed them.
            "_sort": (-search_score, index),
        }
        follow_up = search_follow_up(candidate, query=query, expansion=expansion)
        if follow_up is not None:
            candidate["follow_up"] = follow_up
        normalized.append(candidate)
    mark_stale_guides(normalized)
    # A guide the response itself shows is long superseded sorts after every other row, whatever its
    # title scores: a query for a class guide means the current one, not a retired event's.
    normalized.sort(key=lambda row: (STALE_GUIDE_REASON in row["ranking"]["match_reasons"], row["_sort"]))
    for row in normalized:
        row.pop("_sort", None)
    return normalized


def command_prefix_for_expansion(expansion: ExpansionProfile) -> str:
    if expansion.key == resolve_expansion(None).key:
        return "wowhead"
    return f"wowhead --expansion {expansion.key}"


# How far ahead of the best database entity an article has to score before it answers `resolve`.
# One exact name match: the article's own title has to be what the query names, not just words the
# entity shares with it.
ARTICLE_OVER_ENTITY_MARGIN = EXACT_NAME_SCORE


def top_candidate_score(candidates: list[dict[str, Any]]) -> int:
    """The ranking score of the leading candidate, or 0 when the group is empty."""
    if not candidates:
        return 0
    return int(candidates[0].get("ranking", {}).get("score") or 0)


def preferred_resolve_candidates(
    candidates: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split `resolve` candidates into the group that answers and the group that trails it.

    Wowhead's suggestions mix database entities with news posts and world events, and a headline
    often matches the query text better than the item it is written about. `resolve` answers with an
    entity unless an article leads it by `ARTICLE_OVER_ENTITY_MARGIN`, which is how a query that
    names a headline still resolves to that news post.
    """
    entities = [row for row in candidates if row.get("entity_type") in PARSER_ENTITY_TYPES]
    articles = [row for row in candidates if row.get("entity_type") not in PARSER_ENTITY_TYPES]
    if entities and top_candidate_score(articles) - top_candidate_score(entities) < ARTICLE_OVER_ENTITY_MARGIN:
        return entities, articles
    return articles, entities


def resolve_next_command(candidate: dict[str, Any]) -> str | None:
    """The command `resolve` recommends, read off the follow-up block `normalize_search_results` attached."""
    follow_up = candidate.get("follow_up")
    if not isinstance(follow_up, dict):
        return None
    command = follow_up.get("command")
    return command if isinstance(command, str) and command else None


def resolve_confidence(candidates: list[dict[str, Any]], *, entity_types: tuple[str, ...]) -> str:
    if not candidates:
        return "none"
    top_ranking = candidates[0].get("ranking", {})
    top_score = int(top_ranking.get("score") or 0)
    second_score = int(candidates[1].get("ranking", {}).get("score") or 0) if len(candidates) > 1 else 0
    margin = top_score - second_score
    reasons = set(top_ranking.get("match_reasons") or [])
    high = (
        is_high_confidence_exact_match(reasons, margin=margin, second_score=second_score)
        or is_high_confidence_score(top_score, margin=margin)
        or is_filtered_high_confidence(entity_types, top_score=top_score, margin=margin)
    )
    if high:
        # A guide the response itself shows to be far behind its siblings is never a confident
        # answer, so `resolve` reports it as a candidate instead of recommending a command for it.
        return "medium" if STALE_GUIDE_REASON in reasons else "high"
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

"""Wowhead search ranking, resolve confidence, follow-up commands, and link-record ranking.

This is the stated seam for scoring behavior: ``wowhead_cli.provider``, ``wowhead_cli.guides`` and
``wowhead_cli.main`` all consume it, and the ranking tests target it directly instead of reaching
into ``main``.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Collection, Mapping
from datetime import date
from typing import Any
from urllib.parse import urlparse

from warcraft_core.discovery import ResolveConfidence, discovery_row, single_word_named

from wowhead_cli.entity_types import PARSER_ENTITY_TYPES, RESOLVE_ENTITY_TYPES, SEARCH_TYPE_HINTS
from wowhead_cli.expansion_profiles import (
    EXPANSION_PREFIXES,
    UNPROFILED_PATH_PREFIXES,
    ExpansionProfile,
    is_wowhead_host,
    normalize_wowhead_url,
    parse_entity_from_wowhead_url,
    resolve_expansion,
)
from wowhead_cli.wowhead_client import entity_url, guide_url, suggestion_entity_type

PROVIDER_NAME = "wowhead"

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


# Words too common to say what a query is about: "the argent dawn" means argent and dawn, and must not
# match "Wards of the Dread Citadel" on "the".
MATCH_STOPWORDS = frozenset({"the", "of", "a", "an", "and", "in", "on", "for", "to"})


def word_tokens(text: str) -> set[str]:
    """Lowercase whole words, with apostrophes dropped so "un'goro" and "Ungoro" are the same word."""
    return set(re.findall(r"\w+", re.sub(r"['\u2019]", "", text.lower())))


def match_terms(query: str) -> list[str]:
    """The query words search ranking matches row names against, stopwords removed."""
    return sorted(word_tokens(query) - MATCH_STOPWORDS)


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


def _same_word(term: str, word: str) -> bool:
    """Whether two words differ at most by a plural or possessive ending ("hotfix"/"Hotfixes", "mage"/"Mage's").

    "-es" counts only after a sibilant, so "notes" is not "not" and "capes" is not "cap".
    """
    short, long = sorted((term, word), key=len)
    return long in (short, f"{short}s") or (long == f"{short}es" and short.endswith(("s", "x", "z", "ch", "sh")))


def listing_match_score(query: str, *values: Any) -> int:
    """Score a news, blue-tracker or guides listing row: 0 unless every query word is a word in ``values``.

    Otherwise one point per (word, value) hit, plus a bonus when the values hold the query as a phrase.
    Stopwords count only when the query has nothing else. Words match up to a plural or possessive
    ending, so "hotfix" matches "Hotfixes" and "mage" matches "Mage's", but "mage" matches neither
    "Damage" nor "Magelord".
    """
    terms = set(match_terms(query)) or word_tokens(query)
    texts = [value.lower() for value in values if isinstance(value, str) and value.strip()]
    hits = [{term for term in terms if any(_same_word(term, word) for word in word_tokens(text))} for text in texts]
    if not terms or not terms <= set().union(*hits):
        return 0
    score = sum(len(value_hits) for value_hits in hits)
    phrase = " ".join(query.lower().split())
    if re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", " ".join(texts)):
        score += max(2, len(terms))
    return score


def search_type_hints(query: str) -> set[str]:
    """Entity types a query names as whole words ("conquest" does not hint quest)."""
    normalized = " ".join(query.lower().split())
    return {
        entity_type
        for entity_type, phrases in SEARCH_TYPE_HINTS.items()
        if any(re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", normalized) for phrase in phrases)
    }


def search_ranking_query(query: str) -> str:
    filtered_terms = [
        term
        for term in query_terms(query)
        if term not in FOLLOW_UP_COMMENT_TERMS and term not in FOLLOW_UP_RELATION_TERMS
    ]
    if filtered_terms:
        return " ".join(filtered_terms)
    return " ".join(query.lower().split())


TYPE_HINT_WORDS = frozenset(phrase for phrases in SEARCH_TYPE_HINTS.values() for phrase in phrases if " " not in phrase)


def untyped_search_query(query: str) -> str:
    """`query` without the words that name an entity type: "hogger npc" is "hogger"."""
    return " ".join(term for term in query_terms(query) if term not in TYPE_HINT_WORDS)


def search_follow_up_kind(query: str) -> str:
    terms = set(query_terms(query))
    if terms & FOLLOW_UP_COMMENT_TERMS:
        return "comments"
    if terms & FOLLOW_UP_RELATION_TERMS:
        return "relations"
    return "summary"


def search_follow_up(entity_type: str | None, entity_id: Any, *, intent: str, expansion: ExpansionProfile) -> dict[str, Any] | None:
    """The command that opens a search row, steered by the query's intent from ``search_follow_up_kind``."""
    if not isinstance(entity_type, str) or not isinstance(entity_id, int):
        return None

    prefix = command_prefix_for_expansion(expansion)
    if entity_type == "guide":
        guide_command = f"{prefix} guide {entity_id}"
        guide_full_command = f"{prefix} guide-full {entity_id}"
        recommended_command = guide_command
        surface = "guide"
        reason = "guide_summary"
        alternatives = [guide_full_command]
        if intent == "relations":
            recommended_command = guide_full_command
            surface = "guide-full"
            reason = "guide_relation_intent"
            alternatives = [guide_command]
        elif intent == "comments":
            reason = "guide_comment_intent"
            alternatives = [guide_full_command]
        return {
            "command": recommended_command,
            "surface": surface,
            "reason": reason,
            "alternative_commands": alternatives,
        }

    if entity_type == "news":
        # `news-post` takes a URL, and Wowhead resolves the short /news=<id> form to the article.
        return {
            "command": f"{prefix} news-post {entity_url('news', entity_id, expansion=expansion)}",
            "surface": "news-post",
            "reason": "news_post_summary",
            "alternative_commands": [],
        }

    if entity_type not in RESOLVE_ENTITY_TYPES:
        return None

    entity_command = f"{prefix} entity {entity_type} {entity_id}"
    entity_page_command = f"{prefix} entity-page {entity_type} {entity_id}"
    comments_command = f"{prefix} comments {entity_type} {entity_id}"
    recommended_command = entity_command
    surface = "entity"
    reason = "entity_summary"
    alternatives = [entity_page_command, comments_command]
    if intent == "relations":
        recommended_command = entity_page_command
        surface = "entity-page"
        reason = "entity_relation_intent"
        alternatives = [entity_command, comments_command]
    elif intent == "comments":
        recommended_command = comments_command
        surface = "comments"
        reason = "entity_comment_intent"
        alternatives = [entity_command, entity_page_command]
    return {
        "command": recommended_command,
        "surface": surface,
        "reason": reason,
        "alternative_commands": alternatives,
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


def _holds_words(text: str, query: str, *, at_start: bool) -> bool:
    """Whether `text` holds `query` as whole words, up to a plural ending, and at its start when `at_start`.

    "frost" is a prefix of "Frost Shock" and "valorstone" of "Valorstones", but not of "Frostscale's
    Mystic Frond"; "frost" is not inside "Winterspring Frostsaber".
    """
    start = "^" if at_start else r"(?<!\w)"
    return bool(query) and re.search(rf"{start}{re.escape(query)}(?:e?s)?(?!\w)", text) is not None


def prefix_and_contains_score(normalized_query: str, *, name_normalized: str, display_normalized: str) -> tuple[int, list[str]]:
    """Score the query as the leading words of the name, or failing that as words inside it.

    A title that merely contains the query ("Legion Remix Fury Warrior Guide") never outscores one
    that starts with it, so each contains score sits below both prefix scores.
    """
    if _holds_words(name_normalized, normalized_query, at_start=True):
        return 10, ["name_prefix"]
    if _holds_words(display_normalized, normalized_query, at_start=True):
        return 8, ["display_name_prefix"]
    if _holds_words(name_normalized, normalized_query, at_start=False):
        return 6, ["name_contains_query"]
    if _holds_words(display_normalized, normalized_query, at_start=False):
        return 4, ["display_name_contains_query"]
    return 0, []


def term_match_score(terms: list[str], *, haystacks: list[str]) -> tuple[int, list[str]]:
    """Score the query terms a row's text holds as whole words: 3 each when it holds all, else 1 each.

    Words match up to a plural ending, so "spirit beasts" holds every word of "Spirit Beast". A
    possessive is a different word: "onyxia" does not hold every word of "Onyxia's Lair", so that row
    cannot close in on the NPC "Onyxia".
    """
    text = " ".join(haystacks)
    words = word_tokens(text)
    plain_words = {word for word in re.findall(r"[\w'\u2019]+", text.lower()) if word.isalnum()}
    matched = sum(1 for term in terms if term in words or any(_same_word(term, word) for word in plain_words))
    if not matched:
        return 0, []
    if matched == len(terms):
        return matched * 3, ["all_terms_match"]
    return matched, ["some_terms_match"]


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
# `categories.guides` orders guides the same way. Every current class guide shares a class-guide
# query's words, so only that order says which is the main one; its bonus steps clear resolve's
# 6-point margin but stay far under the database head's, so a top guide never outranks the entity
# Wowhead's database list puts first. It only counts for a query that asks for a guide: for an entity
# query ("spirit beasts") it would only narrow the margin by which the entity leads.
UPSTREAM_RANK_BONUS: dict[str, tuple[int, ...]] = {"database": (42, 28, 14), "guides": (21, 14, 7)}

SuggestionKey = tuple[int, int]


def suggestion_key(row: dict[str, Any]) -> SuggestionKey | None:
    """Wowhead's own ``(type, id)`` identity for a suggestion row, shared by `results` and `categories`."""
    type_id = row.get("type")
    entity_id = row.get("id")
    if isinstance(type_id, int) and isinstance(entity_id, int):
        return type_id, entity_id
    return None


def upstream_rank_bonuses(
    response: dict[str, Any], *, query: str, entity_types: tuple[str, ...] = ()
) -> dict[SuggestionKey, int]:
    """The bonus each leading row of Wowhead's `categories.database` and `categories.guides` order earns.

    The guides order counts only when the caller asks for a guide, in `query` or with `entity_types`.
    """
    categories = response.get("categories")
    if not isinstance(categories, dict):
        return {}
    asks_for_guide = "guide" in search_type_hints(query) or "guide" in entity_types
    bonuses: dict[SuggestionKey, int] = {}
    for list_name, steps in UPSTREAM_RANK_BONUS.items():
        if list_name == "guides" and not asks_for_guide:
            continue
        rows = categories.get(list_name)
        for row, bonus in zip(rows if isinstance(rows, list) else [], steps, strict=False):
            key = suggestion_key(row) if isinstance(row, dict) else None
            if key is not None:
                bonuses.setdefault(key, bonus)
    return bonuses


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


def upstream_rank_score(rank_bonus: int | None, *, entity_type: str | None) -> tuple[int, list[str]]:
    """Score how highly Wowhead's own ordering placed the row, plus a point for a routable row."""
    score = 1 if entity_type is not None else 0
    if not rank_bonus:
        return score, []
    return score + rank_bonus, ["upstream_database_rank"]


def search_result_score_and_reasons(
    row: dict[str, Any], *, query: str, ranking_query: str, rank_bonus: int | None = None
) -> tuple[int, list[str]]:
    normalized_query = " ".join(ranking_query.lower().split())
    terms = match_terms(ranking_query)
    name = str_field(row, "name")
    display_name = str_field(row, "displayName")
    type_name = str_field(row, "typeName")
    entity_type = suggestion_entity_type(row)

    haystacks = [value.lower() for value in (name, display_name, type_name) if value]
    name_normalized = name.lower().strip()
    display_normalized = display_name.lower().strip()
    reasons: list[str] = []
    score = 0

    exact = exact_match_score(normalized_query, name_normalized=name_normalized, display_normalized=display_normalized)
    prefix = prefix_and_contains_score(
        normalized_query, name_normalized=name_normalized, display_normalized=display_normalized
    )
    # Wowhead ranks database rows on text the suggestion never shows (descriptions, criteria), so its
    # rank only counts for a row whose own name shares a word with the query or contains the query
    # ("valorstone" names "Valorstones").
    named = bool(exact[1] or prefix[1] or word_tokens(f"{name} {display_name}").intersection(terms))
    for part_score, part_reasons in (
        exact,
        prefix,
        term_match_score(terms, haystacks=haystacks),
        type_hint_score(query, entity_type=entity_type),
        upstream_rank_score(rank_bonus if named else None, entity_type=entity_type),
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


def split_choices(values: list[str] | None, *, allowed: Collection[str], label: str) -> tuple[str, ...]:
    """Split a repeatable, comma-separated option into lower-cased values, deduped in order.

    A value outside ``allowed`` raises ValueError naming ``label`` and the allowed values.
    """
    normalized: list[str] = []
    for raw in values or []:
        for candidate in raw.split(","):
            value = candidate.strip().lower()
            if not value or value in normalized:
                continue
            if value not in allowed:
                raise ValueError(f"Unsupported {label} {value!r}. Expected one of: {', '.join(sorted(allowed))}.")
            normalized.append(value)
    return tuple(normalized)


# The follow-up of a row no command opens (a world event, a companion, a type this CLI does not map).
# Its ``url`` still opens it in a browser.
NO_FOLLOW_UP: dict[str, Any] = {"command": None, "surface": "none"}


def search_row(
    *,
    kind: str,
    id: Any,
    name: Any,
    url: str | None,
    score: int,
    match_reasons: list[str],
    follow_up: dict[str, Any] | None,
    **extra: Any,
) -> dict[str, Any]:
    """One search/resolve row in the shared shape; ``kind`` is snake-cased (``transmog-set`` -> ``transmog_set``)."""
    follow_up_extra = dict(follow_up or NO_FOLLOW_UP)
    return discovery_row(
        provider=PROVIDER_NAME,
        kind=re.sub(r"[^a-z0-9]+", "_", kind.lower()).strip("_"),
        id=id,
        name=name,
        url=url,
        score=score,
        match_reasons=match_reasons,
        command=follow_up_extra.pop("command"),
        surface=follow_up_extra.pop("surface"),
        follow_up_extra=follow_up_extra,
        **extra,
    )


def search_result_url(*, entity_type: str | None, entity_id: int | None, expansion: ExpansionProfile) -> str | None:
    if not isinstance(entity_id, int):
        return None
    if entity_type == "guide":
        return guide_url(entity_id, expansion=expansion)
    if entity_type:
        return entity_url(entity_type, entity_id, expansion=expansion)
    return None


def url_entity_result(url: str, *, expansion: ExpansionProfile) -> dict[str, Any] | None:
    """The search answer for a Wowhead entity URL: the entity it names, or None when it names none.

    Wowhead's suggestions endpoint matches names, so neither the URL nor its type and id find
    anything there. The URL already identifies the entity, so the answer is that entity and the
    command that opens it. Nothing is fetched, so the entity's URL stands in for its name.
    """
    entity = parse_entity_from_wowhead_url(url)
    if entity is None:
        return None
    entity_type, entity_id = entity
    entity_page_url = search_result_url(entity_type=entity_type, entity_id=entity_id, expansion=expansion)
    # Types `resolve` never returns (mount, recipe, ...) still open with `entity`.
    follow_up = search_follow_up(entity_type, entity_id, intent="summary", expansion=expansion) or {
        "command": f"{command_prefix_for_expansion(expansion)} entity {entity_type} {entity_id}",
        "surface": "entity",
        "reason": "entity_summary",
        "alternative_commands": [],
    }
    return search_row(
        kind=entity_type,
        id=entity_id,
        name=entity_page_url or url,
        url=entity_page_url,
        score=EXACT_NAME_SCORE,
        match_reasons=["url_entity"],
        follow_up=follow_up,
        entity_type=entity_type,
    )


# The command that reads a Wowhead page from its URL, keyed by the page path's first segment
# ("news" also covers /news=<id>).
URL_PAGE_COMMANDS = {
    "guide": "guide",
    "news": "news-post",
    "blue-tracker/topic": "blue-topic",
    "talent-calc": "talent-calc",
    "profession-tree-calc": "profession-tree",
    "dressing-room": "dressing-room",
    "list": "profiler",
}
_LOCALE_SEGMENT_RE = re.compile(r"[a-z]{2}(?:-[A-Z]{2})?")


def page_path_parts(path: str) -> list[str]:
    """The segments of a Wowhead path after any leading expansion and locale prefixes (`classic/de/guide=3143`)."""
    parts = [part for part in path.split("/") if part]
    while parts and (parts[0] in EXPANSION_PREFIXES | UNPROFILED_PATH_PREFIXES or _LOCALE_SEGMENT_RE.fullmatch(parts[0])):
        parts = parts[1:]
    return parts


def _url_page_command(parts: list[str], url: str) -> tuple[str, str | None] | None:
    """The command and argument that open the page whose path segments are `parts`, or None."""
    if parts in (["news"], ["blue-tracker"]):
        return parts[0], None
    if parts[0] == "guides" and len(parts) > 1:
        return "guides", "/".join(parts[1:])
    head = "/".join(parts[:2]) if parts[0] == "blue-tracker" else parts[0].split("=", 1)[0]
    command = URL_PAGE_COMMANDS.get(head)
    return None if command is None else (command, url)


def url_page_result(url: str, *, expansion: ExpansionProfile) -> dict[str, Any] | None:
    """The search answer for a Wowhead guide, news, blue-tracker, tool or listing URL, or None for any other URL.

    Like `url_entity_result` nothing is fetched: the row is the command that reads the page, and the
    page's URL stands in for its id and name. `/guide=<id>` URLs are entity URLs and never get here.
    """
    normalized = normalize_wowhead_url(url)
    if normalized is None or not is_wowhead_host(urlparse(normalized).hostname or ""):
        return None
    parts = page_path_parts(urlparse(normalized).path)
    page = _url_page_command(parts, normalized) if parts else None
    if page is None:
        return None
    surface, argument = page
    command = f"{command_prefix_for_expansion(expansion)} {surface}"
    entity_type = {"guide": "guide", "news-post": "news"}.get(surface)
    return search_row(
        kind=entity_type or surface,
        id=normalized,
        name=normalized,
        url=normalized,
        score=EXACT_NAME_SCORE,
        match_reasons=["url_page"],
        follow_up={
            "command": f"{command} {shlex.quote(argument)}" if argument else command,
            "surface": surface,
            "reason": "url_page",
            "alternative_commands": [],
        },
        entity_type=entity_type,
    )


STALE_GUIDE_REASON = "stale_guide"
STALE_GUIDE_DAYS = 180

# How strongly a row's own text matches the query: an exact name, a name that starts with the query,
# a match of the whole query, or only some of its words. `type_hint` and `stale_guide` say nothing
# about the row's text, so a row with only those has strength 0 and matches nothing in the query.
MATCH_STRENGTH = {
    "exact_name": 4,
    "exact_display_name": 4,
    "name_prefix": 3,
    "display_name_prefix": 3,
    "name_contains_query": 2,
    "display_name_contains_query": 2,
    "all_terms_match": 2,
    "upstream_database_rank": 2,
    "some_terms_match": 1,
}


def match_strength(row: dict[str, Any]) -> int:
    return max((MATCH_STRENGTH.get(reason, 0) for reason in row["ranking"]["match_reasons"]), default=0)


def is_stale(row: dict[str, Any]) -> bool:
    return STALE_GUIDE_REASON in row["ranking"]["match_reasons"]


def order_search_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Order rows by score, then move each stale guide below every current row that matches as strongly.

    A retired event guide that merely shares a class guide's words ("Legion Remix Fury Warrior
    Guide") sorts after the current guides whatever its title scores, but one the query names more
    closely than any current row ("Fury Warrior PvP Guide") still leads.
    """
    ranked = sorted(rows, key=lambda row: row["_sort"])
    current = [row for row in ranked if not is_stale(row)]
    slotted = [((index, False), row) for index, row in enumerate(current)]
    for row in ranked:
        if is_stale(row):
            strength = match_strength(row)
            anchor = max((index for index, other in enumerate(current) if match_strength(other) >= strength), default=-1)
            slotted.append(((anchor, True), row))
    return [row for _, row in sorted(slotted, key=lambda pair: pair[0])]


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


# Blizzard tags internal test entries "(DNT)" (do not translate). Wowhead's database lists them, and a
# name such as "Test Warbound until equipped (DNT)" can match a real query word for word.
_INTERNAL_ENTRY_RE = re.compile(r"\(dnt\)", re.IGNORECASE)


def normalize_search_results(
    results: list[Any],
    *,
    query: str,
    expansion: ExpansionProfile,
    entity_types: tuple[str, ...] = (),
    rank_bonuses: dict[SuggestionKey, int] | None = None,
    literal: bool = False,
) -> tuple[list[dict[str, Any]], int]:
    """Score and order suggestion rows, dropping the ones whose text matches nothing in the query.

    Follow-up words ("comments", "links") steer each row's follow-up command and are left out of the
    ranking, unless `literal` says the whole query is a name ("Soul Link"). Internal "(DNT)" test
    entries are left out unless the query says "dnt".
    Returns the kept rows and how many rows were dropped for matching nothing.
    """
    selected_entity_types = set(entity_types)
    keep_internal = "dnt" in word_tokens(query)
    ranking_query = " ".join(query.lower().split()) if literal else search_ranking_query(query)
    intent = "summary" if literal else search_follow_up_kind(query)
    bonuses = rank_bonuses or {}
    normalized: list[dict[str, Any]] = []
    for index, row in enumerate(results):
        if not isinstance(row, dict):
            continue
        entity_type = suggestion_entity_type(row)
        if selected_entity_types and entity_type not in selected_entity_types:
            continue
        if not keep_internal and _INTERNAL_ENTRY_RE.search(str_field(row, "name")):
            continue
        entity_id = row.get("id")
        popularity = row.get("popularity")
        updated = suggestion_updated_date(row)
        key = suggestion_key(row)
        search_score, match_reasons = search_result_score_and_reasons(
            row,
            query=query,
            ranking_query=ranking_query,
            rank_bonus=bonuses.get(key) if key is not None else None,
        )
        candidate = search_row(
            kind=entity_type or str_field(row, "typeName") or "unknown",
            id=entity_id,
            name=row.get("name"),
            url=search_result_url(
                entity_type=entity_type,
                entity_id=entity_id if isinstance(entity_id, int) else None,
                expansion=expansion,
            ),
            score=search_score,
            match_reasons=match_reasons,
            follow_up=search_follow_up(entity_type, entity_id, intent=intent, expansion=expansion),
            type_id=row.get("type"),
            type_name=row.get("typeName"),
            entity_type=entity_type,
            metadata={
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
            _sort=(-search_score, index),
        )
        normalized.append(candidate)
    mark_stale_guides(normalized)
    matched = order_search_rows([row for row in normalized if match_strength(row) > 0])
    for row in matched:
        row.pop("_sort", None)
    return matched, len(normalized) - len(matched)


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
    return int(candidates[0]["ranking"]["score"]) if candidates else 0


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


def lacks_query_number(query: str, row: Mapping[str, Any]) -> bool:
    """Whether the row's names miss a number the query names ("season 3", "tier 2")."""
    names = word_tokens(f"{row.get('name') or ''} {(row.get('metadata') or {}).get('display_name') or ''}")
    return any(term.isdigit() and term not in names for term in match_terms(query))


def resolve_confidence(
    candidates: list[dict[str, Any]], *, query: str, entity_types: tuple[str, ...]
) -> ResolveConfidence:
    if not candidates:
        return "none"
    top_ranking = candidates[0]["ranking"]
    top_score = int(top_ranking["score"])
    second_score = int(candidates[1]["ranking"]["score"]) if len(candidates) > 1 else 0
    margin = top_score - second_score
    reasons = set(top_ranking["match_reasons"])
    high = (
        is_high_confidence_exact_match(reasons, margin=margin, second_score=second_score)
        or is_high_confidence_score(top_score, margin=margin)
        or is_filtered_high_confidence(entity_types, top_score=top_score, margin=margin)
    )
    if high:
        # A row missing some of the query's words ("Resilient Keystone 12" for "midnight season 2
        # mythic+ dungeons") is not a confident answer unless it is of the type the query names, as
        # "Restoration Druid Healing Guide" is for "resto druid guide", and even then not when a
        # missing word is a number ("Season 2" for "keystone legend season 3 achievement"). A guide
        # the response itself shows to be far behind its siblings never is, nor a row with no command
        # to run (a world event). `resolve` reports any of those as a candidate instead of
        # recommending a command.
        partial = "some_terms_match" in reasons and (
            "type_hint" not in reasons or lacks_query_number(query, candidates[0])
        )
        commandless = candidates[0]["follow_up"]["command"] is None
        return "medium" if partial or commandless or STALE_GUIDE_REASON in reasons else "high"
    if is_medium_confidence_score(top_score, margin=margin):
        return "medium"
    return "low"


def names_single_word(word: str, row: Mapping[str, Any]) -> bool:
    """Wowhead's own answer to a one-word query beyond the row's name: its display name, or either
    name up to the plural ending ranking already tolerates ("valorstone" names "Valorstones")."""
    names = (str(row.get("name") or ""), str((row.get("metadata") or {}).get("display_name") or ""))
    return any(name and (single_word_named(word, name) or _same_word(word, name.lower().strip())) for name in names)


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

"""Warcraft Wiki query normalization and result ranking.

The scorer stays wiki-specific: it combines MediaWiki title conventions (``API Foo``,
``UIHANDLER Bar``, ``World of Warcraft: Legion``) with the content families produced by
:mod:`warcraft_wiki_cli.page_parser`, so it cannot reuse the shared ``score_article_match``
weights. Only the provider-noise stripping is shared (``warcraft_content.search.normalize_query``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from warcraft_content.article_discovery import ArticleKind, article_candidate, sort_article_candidates
from warcraft_content.search import normalize_query

from warcraft_wiki_cli.client import WarcraftWikiClient
from warcraft_wiki_cli.page_parser import PROGRAMMING_FAMILIES, classify_article_family

PROVIDER_NAME = "warcraft-wiki"
WIKI_ARTICLE_KIND = ArticleKind(surface="article", type_name="Article", entity_type="article", metadata_key="title")

# MediaWiki full-text rank is a real signal, but we cannot see why a row matched (the snippet is
# truncated), so it stays small enough that it can never outweigh an actual title match.
UPSTREAM_RANK_MAX_SCORE = 10
SYSTEM_REFERENCE_FAMILIES = {
    "system_reference",
    "expansion_reference",
    "profession_reference",
    "class_reference",
    "faction_reference",
    "zone_reference",
}

# Reasons that mean the row's own text covers the whole query. The positional baseline, the intent
# bonus and the family bonus are collected by completely unrelated pages, and ``snippet_match`` fires
# on a single shared word, so none of them belong here. ``is_confident_match`` refuses a top row that
# carries none of these however high it scored.
QUERY_COVERAGE_REASONS = frozenset(
    {
        "exact_title",
        "exact_api_title",
        "exact_handler_title",
        "exact_event_title",
        "title_prefix",
        "title_contains_query",
        "normalized_title_match",
        "all_terms_match",
        "guide_title_terms",
        "expansion_alias_match",
    }
)

# Leading words that name an article family rather than the subject ("lore Jaina" -> "jaina").
QUERY_FAMILY_HINT_TERMS = {
    "article",
    "articles",
    "faction",
    "factions",
    "guide",
    "guides",
    "lore",
    "reference",
    "references",
    "story",
    "stories",
    "tutorial",
    "tutorials",
}
# Dropped only when the query has more words left; "zone scaling" is a page title, not a hint.
CONDITIONAL_FAMILY_HINT_TERMS = {"zone", "zones", "class", "classes", "profession", "professions", "expansion", "expansions"}


@dataclass(frozen=True, slots=True)
class SearchOutcome:
    """Ranked wiki search results plus the query rewriting that produced them."""

    normalized_query: str
    excluded_terms: list[str]
    results: list[dict[str, Any]]
    total_count: int


def normalize_wiki_query(query: str) -> tuple[str, list[str]]:
    """Strip the word "wiki" and any leading family-hint terms; returns (normalized query, dropped terms)."""
    base = normalize_query(query, strip_terms=("wiki",))
    kept_terms = base.split()
    excluded_terms: list[str] = []
    while kept_terms:
        head = kept_terms[0]
        if head in QUERY_FAMILY_HINT_TERMS:
            excluded_terms.append(kept_terms.pop(0))
            continue
        conditional = head in CONDITIONAL_FAMILY_HINT_TERMS and len(kept_terms) >= 2
        if conditional and not (head in {"zone", "zones"} and kept_terms[1] == "scaling"):
            excluded_terms.append(kept_terms.pop(0))
            continue
        break
    if not kept_terms:
        return base, []
    return " ".join(kept_terms), excluded_terms


def _collapsed_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _query_intents(query: str) -> set[str]:
    lowered = query.lower()
    intents: set[str] = set()
    if any(token in lowered for token in ("api", "function", "widget", "framexml", "lua", "cvar", "xml", "handler", "event", "addon")):
        intents.add("programming")
    if any(token in lowered for token in ("patch", "changes", "hotfix")):
        intents.add("patch")
    if any(token in lowered for token in ("zone", "zones", "renown", "housing", "profession", "expansion", "faction")):
        intents.add("systems")
    if any(token in lowered for token in ("lore", "story", "character", "characters")):
        intents.add("lore")
    return intents


def _exact_title_score(
    query: str,
    normalized_query: str,
    lowered_title: str,
    normalized_title: str,
    *,
    family: str,
) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []

    if lowered_title == query:
        score += 50
        reasons.append("exact_title")
    if family == "api_function" and normalized_title == f"api{normalized_query}":
        score += 40
        reasons.append("exact_api_title")
    if family == "ui_handler" and normalized_title == f"uihandler{normalized_query}":
        score += 40
        reasons.append("exact_handler_title")
    if family == "event_reference" and normalized_title == f"event{normalized_query}":
        score += 40
        reasons.append("exact_event_title")
    if lowered_title.startswith(query):
        score += 20
        reasons.append("title_prefix")
    if query in lowered_title:
        score += 12
        reasons.append("title_contains_query")
    if normalized_query and normalized_query in normalized_title:
        score += 10
        reasons.append("normalized_title_match")
    return score, reasons


def _term_match_score(query: str, lowered_title: str, lowered_snippet: str, *, family: str) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []
    terms = [term for term in query.split() if term]
    haystack = f"{lowered_title} {lowered_snippet}".strip()
    if terms and all(term in haystack for term in terms):
        score += 10
        reasons.append("all_terms_match")
    if terms and all(term in lowered_title for term in terms) and family in {"howto_programming", "guide_reference"}:
        score += 36
        reasons.append("guide_title_terms")
    if lowered_snippet and any(term in lowered_snippet for term in terms):
        score += 4
        reasons.append("snippet_match")
    return score, reasons


def _title_match_score(query: str, title: str, snippet: str, *, family: str) -> tuple[int, list[str]]:
    normalized_query = _collapsed_text(query)
    normalized_title = _collapsed_text(title)
    lowered_title = title.lower()
    lowered_snippet = snippet.lower()
    score = 0
    reasons: list[str] = []

    for part_score, part_reasons in (
        _exact_title_score(query, normalized_query, lowered_title, normalized_title, family=family),
        _term_match_score(query, lowered_title, lowered_snippet, family=family),
    ):
        score += part_score
        reasons.extend(part_reasons)
    return score, reasons


def _intent_family_score(original_query: str, *, family: str) -> tuple[int, list[str]]:
    intents = _query_intents(original_query)
    score = 0
    reasons: list[str] = []
    if "programming" in intents and family in PROGRAMMING_FAMILIES:
        score += 20
        reasons.append("intent_programming")
    if "systems" in intents and family in SYSTEM_REFERENCE_FAMILIES:
        score += 18
        reasons.append("intent_systems")
    if "patch" in intents and family in {"patch_reference", "api_changes"}:
        score += 18
        reasons.append("intent_patch")
    if "lore" in intents and family == "lore_reference":
        score += 16
        reasons.append("intent_lore")
    return score, reasons


def _family_baseline_score(query: str, title: str, *, family: str) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []
    lowered_title = title.lower()
    if family in {"api_function", "ui_handler", "event_reference"}:
        score += 8
        reasons.append(f"family_{family}")
    elif family in {
        "framework_page",
        "system_reference",
        "expansion_reference",
        "profession_reference",
        "class_reference",
        "zone_reference",
        "patch_reference",
    }:
        score += 4
        reasons.append(f"family_{family}")

    if family == "expansion_reference" and lowered_title.startswith("world of warcraft:"):
        suffix = lowered_title.split(":", 1)[1].strip()
        if query == suffix:
            score += 24
            reasons.append("expansion_alias_match")
    return score, reasons


def score_wiki_match(original_query: str, query: str, title: str, snippet: str, *, ordinal: int) -> tuple[int, list[str], str]:
    """Score one search row; returns (score, match reasons, content family). ``ordinal`` is the upstream rank."""
    family = classify_article_family(title)
    score = max(0, UPSTREAM_RANK_MAX_SCORE - ordinal)
    reasons: list[str] = [f"upstream_rank_{ordinal + 1}"]
    for part_score, part_reasons in (
        _title_match_score(query, title, snippet, family=family),
        _intent_family_score(original_query, family=family),
        _family_baseline_score(query, title, family=family),
    ):
        score += part_score
        reasons.extend(part_reasons)
    return score, reasons, family


def search_results(client: WarcraftWikiClient, query: str, *, limit: int) -> SearchOutcome:
    """Run the MediaWiki search for ``query`` and rank the rows into article candidates."""
    normalized_query, excluded_terms = normalize_wiki_query(query)
    fetch_limit = max(limit * 5, 25)
    total_count, rows = client.search_articles(normalized_query, limit=fetch_limit)
    matches: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        title = row["title"]
        score, reasons, family = score_wiki_match(query, normalized_query, title, row.get("snippet") or "", ordinal=index)
        matches.append(
            article_candidate(
                ref=title,
                name=title,
                url=row["url"],
                score=score,
                reasons=reasons,
                provider_command=PROVIDER_NAME,
                kind=WIKI_ARTICLE_KIND,
                metadata={"title": title, "content_family": family},
            )
        )
    sort_article_candidates(matches)
    return SearchOutcome(normalized_query, excluded_terms, matches[:limit], total_count)


def title_names_query(title: str, query: str) -> bool:
    """True when every word of ``query`` appears in ``title`` itself, ignoring case and separators.

    The absolute relevance floor for the typed ``api``/``event`` surfaces. ``all_terms_match`` also
    fires on the search snippet, so a page that merely *talks about* the query covers it
    (``UIHANDLER OnEvent`` for ``PLAYER_LOGIN``); the page that *is* the answer carries the name in
    its title (``Event:PLAYER LOGIN``). Separators are collapsed so ``PLAYER_LOGIN`` matches
    ``PLAYER LOGIN``, and terms are matched individually so a phrase query still lands
    (``key down handler`` -> ``UIHANDLER OnKeyDown``).
    """
    normalized_query, _ = normalize_wiki_query(query)
    collapsed_title = _collapsed_text(title)
    terms = [_collapsed_text(term) for term in normalized_query.split()]
    return bool(terms) and all(term in collapsed_title for term in terms)


def _covers_query(row: dict[str, Any]) -> bool:
    """True when the row earned at least one reason that ties its own text to the whole query."""
    return bool(QUERY_COVERAGE_REASONS.intersection(row["ranking"]["match_reasons"]))


def is_confident_match(results: list[dict[str, Any]]) -> bool:
    """True when the top candidate covers the query and no other covering candidate rivals it.

    The coverage requirement is what stops a row from being "confident" on the strength of its
    upstream rank, its family and one shared word, which is how ``event PLAYER_LOGIN`` used to return
    ``UIHANDLER OnEvent``. Rows that do not cover the query are not rivals either: inside a typed
    surface every candidate already collected the same family and intent bonuses, so comparing the
    top score against a non-matching runner-up only measures that shared floor.
    """
    if not results or not _covers_query(results[0]):
        return False
    rivals = [row for row in results[1:] if _covers_query(row)]
    if not rivals:
        return True
    top_score = int(results[0]["ranking"]["score"])
    return top_score >= 70 or top_score >= int(rivals[0]["ranking"]["score"]) + 18

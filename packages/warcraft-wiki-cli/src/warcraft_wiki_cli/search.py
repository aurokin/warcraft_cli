"""Warcraft Wiki query normalization and result ranking.

The scorer stays wiki-specific: it combines MediaWiki title conventions (``API Foo``,
``UIHANDLER Bar``, ``World of Warcraft: Legion``) with the content families produced by
:mod:`warcraft_wiki_cli.page_parser`, so it cannot reuse the shared ``score_article_match``
weights. Only the query tokenization and the provider-noise stripping are shared
(``warcraft_content.search.tokenize_query`` and ``normalize_query``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from warcraft_content.article_discovery import ArticleKind, article_candidate, sort_article_candidates
from warcraft_content.search import normalize_query, tokenize_query
from warcraft_core.provider import ProviderError

from warcraft_wiki_cli.client import WarcraftWikiClient
from warcraft_wiki_cli.page_parser import PROGRAMMING_FAMILIES, classify_article_family

PROVIDER_NAME = "warcraft-wiki"
WIKI_ARTICLE_KIND = ArticleKind(surface="article", type_name="Article", entity_type="article", metadata_key="title")

# MediaWiki full-text rank is a real signal, but we cannot see why a row matched (the snippet is
# truncated), so it stays small enough that it can never outweigh an actual title match.
UPSTREAM_RANK_MAX_SCORE = 10
# A title that is a whole-word phrase inside a longer query ("world boss sha of anger" -> "Sha of
# Anger") names the subject the qualifiers are about. Scaled by the share of query words the title
# spells out (18, 12 and 6 points for "Sha of Anger", "World boss" and "Sha" there), so the upstream
# rank can still reorder two partial titles. A title that spells out the whole query, qualifier
# included ("Sha of Anger (Anniversary)"), earns ``exact_title`` instead: with ``normalized_title_match``
# that is 60 points, more than this bonus, the upstream rank and the snippet reasons together (at most
# 54) when both titles share a content family. ``is_confident_match`` discounts this bonus, so it
# never makes a row confident.
QUERY_CONTAINS_TITLE_MAX_SCORE = 30
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
# ``search_results`` keeps them when a page is titled with the whole query ("class hall").
QUERY_FAMILY_HINT_TERMS = {
    "article",
    "articles",
    "class",
    "classes",
    "expansion",
    "expansions",
    "faction",
    "factions",
    "guide",
    "guides",
    "lore",
    "profession",
    "professions",
    "reference",
    "references",
    "story",
    "stories",
    "tutorial",
    "tutorials",
    "zone",
    "zones",
}

# MediaWiki namespace tokens that mash two words into one title word, so a typed query is allowed to
# spell them out ("key down handler" -> "UIHANDLER OnKeyDown"). Every other title word is split only
# where the title itself marks a boundary: a separator or a camel-case hump.
TITLE_WORD_EXPANSIONS = {"uihandler": ("ui", "handler")}
_TITLE_WORD_RE = re.compile(r"[A-Za-z0-9]+")
_TITLE_COMPONENT_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+")


@dataclass(frozen=True, slots=True)
class SearchOutcome:
    """Every ranked wiki search row plus the query rewriting that produced them."""

    normalized_query: str
    excluded_terms: list[str]
    results: list[dict[str, Any]]
    total_count: int


def normalize_wiki_query(query: str) -> tuple[str, list[str]]:
    """Strip the word "wiki" and any leading family-hint terms; returns (normalized query, dropped terms)."""
    base = normalize_query(query, strip_terms=("wiki",))
    kept_terms = base.split()
    excluded_terms: list[str] = []
    while kept_terms and kept_terms[0] in QUERY_FAMILY_HINT_TERMS:
        excluded_terms.append(kept_terms.pop(0))
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

    # Compared word by word, so a disambiguation title counts its parenthetical as words ("xuen tactics"
    # is exactly "Xuen (tactics)") while digit groups stay apart ("patch 1.12" is not "Patch 1.1.2").
    title_words = _TITLE_WORD_RE.findall(lowered_title)
    if lowered_title == query or (title_words and title_words == _TITLE_WORD_RE.findall(query.lower())):
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


def _query_contains_title_score(query: str, title: str) -> tuple[int, list[str]]:
    query_words = _TITLE_WORD_RE.findall(query.lower())
    title_words = _TITLE_WORD_RE.findall(title.lower())
    size = len(title_words)
    if not size or size >= len(query_words):
        return 0, []
    if not any(query_words[start : start + size] == title_words for start in range(len(query_words) - size + 1)):
        return 0, []
    return round(QUERY_CONTAINS_TITLE_MAX_SCORE * size / len(query_words)), ["query_contains_title"]


def score_wiki_match(original_query: str, query: str, title: str, snippet: str, *, ordinal: int) -> tuple[int, list[str], str]:
    """Score one search row; returns (score, match reasons, content family). ``ordinal`` is the upstream rank."""
    family = classify_article_family(title)
    score = max(0, UPSTREAM_RANK_MAX_SCORE - ordinal)
    reasons: list[str] = [f"upstream_rank_{ordinal + 1}"]
    for part_score, part_reasons in (
        _title_match_score(query, title, snippet, family=family),
        _query_contains_title_score(query, title),
        _intent_family_score(original_query, family=family),
        _family_baseline_score(query, title, family=family),
    ):
        score += part_score
        reasons.extend(part_reasons)
    return score, reasons, family


def _ranked_matches(client: WarcraftWikiClient, original_query: str, search_query: str, *, limit: int) -> tuple[int, list[dict[str, Any]]]:
    """MediaWiki's total hit count and every fetched row for ``search_query`` as ranked candidates."""
    total_count, rows = client.search_articles(search_query, limit=max(limit * 5, 25))
    matches: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        title = row["title"]
        score, reasons, family = score_wiki_match(original_query, search_query, title, row.get("snippet") or "", ordinal=index)
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
    return total_count, matches


def search_results(client: WarcraftWikiClient, query: str, *, limit: int) -> SearchOutcome:
    """Run the MediaWiki search for ``query`` and rank every fetched row; callers apply ``limit``.

    A leading family word is dropped ("class druid" -> "druid") unless a page is titled with the whole
    query ("class hall" is the "Class Hall" page), so the query as typed is searched first whenever a
    word would be dropped.
    """
    normalized_query, excluded_terms = normalize_wiki_query(query)
    if not normalized_query:
        raise ProviderError("invalid_query", "Query cannot be empty.")
    if excluded_terms:
        full_query = " ".join([*excluded_terms, normalized_query])
        total_count, matches = _ranked_matches(client, query, full_query, limit=limit)
        if any("exact_title" in row["ranking"]["match_reasons"] for row in matches):
            return SearchOutcome(full_query, [], matches, total_count)
    total_count, matches = _ranked_matches(client, query, normalized_query, limit=limit)
    return SearchOutcome(normalized_query, excluded_terms, matches, total_count)


@dataclass(frozen=True, slots=True)
class _TitleWord:
    """One word of a title, lowercased, plus every offset a query term may start or end at."""

    text: str
    boundaries: frozenset[int]


def _title_words(title: str) -> list[_TitleWord]:
    """Split ``title`` into words, marking the camel-case humps a query term is allowed to align to."""
    words: list[_TitleWord] = []
    for raw in _TITLE_WORD_RE.findall(title):
        expansion = TITLE_WORD_EXPANSIONS.get(raw.lower())
        if expansion is not None:
            words.extend(_TitleWord(part, frozenset({0, len(part)})) for part in expansion)
            continue
        starts = {match.start() for match in _TITLE_COMPONENT_RE.finditer(raw)}
        words.append(_TitleWord(raw.lower(), frozenset(starts | {len(raw)})))
    return words


def _term_spans(word: _TitleWord, term: str) -> list[range]:
    """Every place ``term`` sits inside ``word`` while starting and ending on a component boundary."""
    return [
        range(start, start + len(term))
        for start in word.boundaries
        if start + len(term) in word.boundaries and word.text.startswith(term, start)
    ]


def title_names_query(title: str, query: str) -> bool:
    """True when ``title`` spells the query out rather than merely containing its letters.

    The absolute relevance floor for the typed ``api``/``event`` surfaces. Every query word has to
    match a whole title word or a whole camel-case component of one (``key down handler`` ->
    ``UIHANDLER OnKeyDown``, separators on either side are irrelevant so ``PLAYER_LOGIN`` ->
    ``Event:PLAYER LOGIN``), and the query has to account for at least one title word end to end.
    Plain containment is not a name: it lets ``is`` name ``API UnitIsPlayer`` and ``UnitHealth``
    name ``API UnitHealthMax``. ``all_terms_match`` also fires on the search snippet, so without
    this floor a page that merely *talks about* the query looks like the answer (``UIHANDLER
    OnEvent`` for ``PLAYER_LOGIN``); the page that *is* the answer carries the name in its title.
    """
    normalized_query, _ = normalize_wiki_query(query)
    terms = tokenize_query(normalized_query)
    words = _title_words(title)
    if not terms or not words:
        return False
    covered: list[set[int]] = [set() for _ in words]
    for term in terms:
        spans = [(index, span) for index, word in enumerate(words) for span in _term_spans(word, term)]
        if not spans:
            return False
        for index, span in spans:
            covered[index].update(span)
    return any(len(marks) == len(word.text) for word, marks in zip(words, covered, strict=True))


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
    top = results[0]["ranking"]
    # ``query_contains_title`` orders a subject above pages that mention it, but a title that is only
    # part of the query is no evidence the page is the answer. Rows keep no score breakdown, so the
    # bonus comes off at its maximum.
    top_score = int(top["score"]) - (QUERY_CONTAINS_TITLE_MAX_SCORE if "query_contains_title" in top["match_reasons"] else 0)
    return top_score >= 70 or top_score >= int(rivals[0]["ranking"]["score"]) + 18

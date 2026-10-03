"""Shared query tokenization and article-title scoring for guide/article providers.

Providers keep their own family boosts and penalties; this module holds the part
that was drifting between them (query normalization, term splitting, and the
exact/prefix/contains/all-terms title score).
"""

from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import dataclass

DEFAULT_TOKEN_RE = re.compile(r"[a-z0-9+]+")


def tokenize_query(query: str, *, stop_words: Collection[str] = ()) -> tuple[str, ...]:
    """Lowercase, split on non-alphanumerics (keeping '+'), drop stop words, keep first-seen order without duplicates."""
    seen: dict[str, None] = {}
    for term in DEFAULT_TOKEN_RE.findall(query.lower()):
        if term not in stop_words:
            seen.setdefault(term, None)
    return tuple(seen)


# Community shorthand for classes and specs, spelled the way guide sites title their pages.
CLASS_SPEC_ALIASES: dict[str, str] = {
    "dk": "death knight",
    "bdk": "blood death knight",
    "fdk": "frost death knight",
    "udk": "unholy death knight",
    "dh": "demon hunter",
    "veng": "vengeance",
    "bm": "beast mastery",
    "mm": "marksmanship",
    "sv": "survival",
    "pally": "paladin",
    "pal": "paladin",
    "ret": "retribution",
    "prot": "protection",
    "resto": "restoration",
    "disc": "discipline",
    "sp": "shadow priest",
    "spriest": "shadow priest",
    "mw": "mistweaver",
    "ww": "windwalker",
    "sub": "subtlety",
    "sin": "assassination",
    "assa": "assassination",
    "ele": "elemental",
    "enh": "enhancement",
    "enha": "enhancement",
    "lock": "warlock",
    "aff": "affliction",
    "demo": "demonology",
    "destro": "destruction",
    "aug": "augmentation",
    "dev": "devastation",
    "pres": "preservation",
    "boomy": "balance",
    "boomkin": "balance",
}
# "aug rune" is an Augment Rune, not an Augmentation Evoker: spelling it out left Method's augment rune
# pages unmatched, so shorthand followed by "rune" stays as typed.
_CLASS_SPEC_ALIAS_RE = re.compile(r"\b(" + "|".join(map(re.escape, CLASS_SPEC_ALIASES)) + r")\b(?!\s+runes?\b)")


def expand_class_spec_aliases(query: str) -> str:
    """Lowercase ``query`` and spell out whole-word class/spec shorthand: ``ret pally`` is ``retribution paladin``."""
    return _CLASS_SPEC_ALIAS_RE.sub(lambda match: CLASS_SPEC_ALIASES[match.group(1)], query.lower())


def normalize_query(query: str, *, strip_terms: Collection[str]) -> str:
    """Lowercase and remove provider/noise words (whole words only); fall back to the raw query when nothing remains."""
    normalized = query.lower()
    if strip_terms:
        pattern = r"\b(" + "|".join(re.escape(term) for term in strip_terms) + r")\b"
        normalized = re.sub(pattern, " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized or query.strip().lower()


@dataclass(frozen=True, slots=True)
class ArticleMatchWeights:
    exact: int = 40
    prefix: int = 15
    contains: int = 10
    all_terms: int = 16


DEFAULT_ARTICLE_MATCH_WEIGHTS = ArticleMatchWeights()


def score_article_match(
    query: str,
    candidate: str,
    *,
    weights: ArticleMatchWeights = DEFAULT_ARTICLE_MATCH_WEIGHTS,
) -> tuple[int, list[str]]:
    """Score how well a normalized query matches a lowercase candidate title; returns (score, reasons)."""
    if not query or not candidate:
        return 0, []
    score = 0
    reasons: list[str] = []
    if candidate == query:
        score += weights.exact
        reasons.append("exact_name")
    if candidate.startswith(query):
        score += weights.prefix
        reasons.append("name_prefix")
    if query in candidate:
        score += weights.contains
        reasons.append("name_contains_query")
    query_terms = query.split()
    if query_terms and all(term in candidate for term in query_terms):
        score += weights.all_terms
        reasons.append("all_terms_match")
    return score, reasons

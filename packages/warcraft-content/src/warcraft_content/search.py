"""Shared query tokenization and article-title scoring for guide/article providers.

Providers keep their own family boosts and penalties; this module holds the part
that was drifting between them (query normalization, term splitting, and the
exact/prefix/contains/all-terms title score).
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from typing import Any

DEFAULT_TOKEN_RE = re.compile(r"[a-z0-9+]+")


def tokenize_query(query: str, *, stop_words: Collection[str] = ()) -> tuple[str, ...]:
    """Lowercase, split on non-alphanumerics (keeping '+'), drop stop words, keep first-seen order without duplicates."""
    seen: dict[str, None] = {}
    for term in DEFAULT_TOKEN_RE.findall(query.lower()):
        if term not in stop_words:
            seen.setdefault(term, None)
    return tuple(seen)


def singular_words(words: set[str]) -> set[str]:
    """``words`` plus each one without a plural 's' or 'es', so two sets meet on a shared singular.

    ``build`` keeps the ``...-spec-builds-talents`` pages, ``delves`` the delve guides and ``boss`` the
    ``...-raid-bosses-...`` pages.
    """
    singulars = set(words)
    for word in words:
        if len(word) > 3 and word.endswith("s"):
            singulars.add(word[:-1])
            if len(word) > 4 and word.endswith("es"):
                singulars.add(word[:-2])
    return singulars


_APOSTROPHE_RE = re.compile(r"['\u2019]")
_SEPARATOR_RE = re.compile(r"[^a-z0-9+]+")


def fold_punctuation(text: str) -> str:
    """Lowercase, drop apostrophes and turn every other separator into a space.

    Guide sites slug page titles this way (``Nerub-ar Palace`` is ``nerub-ar-palace``, ``K'aresh`` is
    ``karesh``), so a query and a title folded alike match on their words. Wiki titles keep their
    punctuation (``Patch 12.1.0/API changes``), so only the guide-site rankers fold.
    """
    return _SEPARATOR_RE.sub(" ", _APOSTROPHE_RE.sub("", text.lower())).strip()


def punctuation_spellings(query: str) -> tuple[str, ...]:
    """``query`` as typed, with its hyphens dropped, and with its apostrophes turned into spaces.

    Guide sites slug punctuation both ways: Icy Veins has ``nerubar-palace-raid-guide`` and
    ``...-nerub-ar-palace-raid-guide``, and ``K'aresh`` is ``karesh`` while ``Zul'Aman`` is ``zul-aman``.
    The rankers score every spelling and keep each page's best.
    """
    return tuple(dict.fromkeys((query, query.replace("-", ""), _APOSTROPHE_RE.sub(" ", query))))


def best_scored(candidates: Iterable[dict[str, Any] | None]) -> dict[str, Any] | None:
    """The highest-scoring candidate row (first on a tie), or None when no spelling matched."""
    return max(filter(None, candidates), key=lambda row: int(row["ranking"]["score"]), default=None)


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


# Every spelling of Mythic+: ``m+``, ``m plus``, ``mythic+``, ``mythic plus``. Each provider substitutes
# the words its own pages use.
MYTHIC_PLUS_RE = re.compile(r"\bm(?:ythic)?(?:\s*\+|\s+plus\b)")


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

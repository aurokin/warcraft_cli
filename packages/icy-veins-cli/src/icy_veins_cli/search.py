"""Icy Veins sitemap ranking: query normalization plus family-aware boosts and penalties.

Query tokenization and the exact/prefix/contains/all-terms title score come from
``warcraft_content.search`` so they cannot drift from the other article providers; everything
below it (guide-family boosts, slug penalties, resolve confidence) is Icy Veins specific.
"""

from __future__ import annotations

from typing import Any

from warcraft_content.article_discovery import article_candidate, sort_article_candidates
from warcraft_content.search import normalize_query, score_article_match, tokenize_query

from icy_veins_cli.client import IcyVeinsClient

PROVIDER_NAME = "icy-veins"
QUERY_STRIP_TERMS = ("icy", "veins", "guide", "guides")
# ``keywords`` is an all-of test; ``any_keywords`` fires on a single term. Singular/plural spellings
# of the same word belong in ``any_keywords``, never in ``keywords``, or the hint can never fire.
UNSUPPORTED_QUERY_HINTS: dict[str, dict[str, Any]] = {
    "patch_notes": {
        "keywords": {"patch", "notes"},
        "message": "Icy Veins patch-note and news-like WoW pages are currently out of scope for the supported guide surface.",
    },
    "class_changes": {
        "keywords": {"class", "changes"},
        "message": "Icy Veins latest-class-changes style WoW pages are currently out of scope for the supported guide surface.",
    },
    "hotfixes": {
        "any_keywords": {"hotfix", "hotfixes"},
        "message": "Icy Veins hotfix and news-like WoW pages are currently out of scope for the supported guide surface.",
    },
    "news": {
        "keywords": {"news"},
        "message": "Icy Veins news-like WoW pages are currently out of scope for the supported guide surface.",
    },
}
# Slug words that carry no ranking signal because they say nothing about which guide is meant.
# Class and spec names must never appear here: exempting one class from the off-query slug penalty
# hands it a permanent head start on every query that names no class.
NEUTRAL_SLUG_TERMS = {
    "guide",
    "guides",
    "pve",
    "pvp",
    "healing",
    "tank",
    "dps",
}
SPECIALIZED_QUERY_TERMS = {
    "easy",
    "mode",
    "leveling",
    "pvp",
    "build",
    "builds",
    "talent",
    "talents",
    "rotation",
    "cooldown",
    "cooldowns",
    "abilities",
    "stats",
    "gems",
    "enchants",
    "consumables",
    "gear",
    "bis",
    "resources",
    "mythic+",
    "mythic",
    "plus",
    "macros",
    "addons",
    "ui",
    "simulation",
    "simulations",
    "sim",
    "raid",
    "remix",
    "torghast",
    "expansion",
    "midnight",
}
ROLE_QUERY_TERMS = {"healing", "tank", "dps"}
SPECIALIZED_FAMILY_RULES: tuple[dict[str, Any], ...] = (
    {"family": "easy_mode", "score": 28, "reason": "family_easy_mode", "all_terms": {"easy", "mode"}},
    {"family": "leveling", "score": 24, "reason": "family_leveling", "all_terms": {"leveling"}},
    {"family": "pvp", "score": 24, "reason": "family_pvp", "all_terms": {"pvp"}},
    {
        "family": "spec_builds_talents",
        "score": 24,
        "reason": "family_builds_talents",
        "any_terms": {"build", "builds", "talent", "talents"},
    },
    {
        "family": "rotation_guide",
        "score": 24,
        "reason": "family_rotation",
        "any_terms": {"rotation", "cooldown", "cooldowns", "abilities"},
    },
    {
        "family": "stat_priority",
        "score": 24,
        "reason": "family_stat_priority",
        "phrases": (" stat priority ", " stats "),
    },
    {
        "family": "gems_enchants_consumables",
        "score": 24,
        "reason": "family_gems_enchants",
        "any_terms": {"gems", "enchants", "consumables"},
    },
    {
        "family": "gear_best_in_slot",
        "score": 24,
        "reason": "family_gear",
        "any_terms": {"gear", "bis"},
        "phrases": (" best in slot ",),
    },
    {
        "family": "spell_summary",
        "score": 24,
        "reason": "family_spell_summary",
        "phrases": (" spell summary ", " spell list ", " glossary "),
    },
    {"family": "resources", "score": 18, "reason": "family_resources", "all_terms": {"resources"}},
    {
        "family": "mythic_plus_tips",
        "score": 18,
        "reason": "family_mythic_plus",
        "phrases": ("mythic+", " mythic plus "),
    },
    {
        "family": "macros_addons",
        "score": 18,
        "reason": "family_macros_addons",
        "any_terms": {"macros", "addons", "ui"},
        "phrases": ("add-ons",),
    },
    {
        "family": "simulations",
        "score": 18,
        "reason": "family_simulations",
        "any_terms": {"simulation", "simulations", "sim"},
    },
    {"family": "raid_guide", "score": 18, "reason": "family_raid_guide", "all_terms": {"raid"}},
    {
        "family": "expansion_guide",
        "score": 18,
        "reason": "family_expansion_guide",
        "any_terms": {"expansion"},
        "phrases": (" war within ", " midnight "),
    },
    {
        "family": "special_event_guide",
        "score": 18,
        "reason": "family_special_event",
        "any_terms": {"remix", "torghast"},
    },
)


def normalize_search_query(query: str) -> str:
    """Drop the provider and 'guide' noise words so ranking sees only the meaningful part of the query."""
    return normalize_query(query, strip_terms=QUERY_STRIP_TERMS)


def query_terms(query: str) -> set[str]:
    return set(tokenize_query(query))


def unsupported_scope_hint(query: str) -> dict[str, Any] | None:
    """Return a scope hint when the query asks for a WoW page family Icy Veins support excludes."""
    terms = query_terms(query)
    if not terms:
        return None
    for code, config in UNSUPPORTED_QUERY_HINTS.items():
        all_keywords = set(config.get("keywords") or ())
        any_keywords = set(config.get("any_keywords") or ())
        if (all_keywords and all_keywords <= terms) or (any_keywords & terms):
            return {"code": code, "message": config["message"]}
    return None


def _score_broad_family_match(content_family: str, *, terms: set[str]) -> tuple[int, list[str]]:
    if content_family == "class_hub" and len(terms) == 1 and not (terms & SPECIALIZED_QUERY_TERMS):
        return 18, ["family_class_hub"]
    if content_family == "role_guide" and len(terms) == 1 and (ROLE_QUERY_TERMS & terms):
        return 18, ["family_role_guide"]
    return 0, []


def _specialized_family_rule_matches(
    rule: dict[str, Any],
    *,
    content_family: str,
    terms: set[str],
    joined: str,
    lowered_query: str,
) -> bool:
    if content_family != rule["family"]:
        return False
    all_terms = rule.get("all_terms")
    if all_terms and not set(all_terms) <= terms:
        return False
    any_terms = set(rule.get("any_terms") or ())
    phrase_matches = any(phrase in joined or phrase in lowered_query for phrase in tuple(rule.get("phrases") or ()))
    if any_terms or rule.get("phrases"):
        return bool(any_terms & terms) or phrase_matches
    return True


def _score_specialized_family_match(query: str, content_family: str, *, terms: set[str], joined: str) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []
    lowered_query = query.lower()
    for rule in SPECIALIZED_FAMILY_RULES:
        if _specialized_family_rule_matches(
            rule,
            content_family=content_family,
            terms=terms,
            joined=joined,
            lowered_query=lowered_query,
        ):
            score += int(rule["score"])
            reasons.append(str(rule["reason"]))
    return score, reasons


def _score_family_penalties(content_family: str, *, terms: set[str], joined: str) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []
    if content_family in {"class_hub", "role_guide"} and (terms & SPECIALIZED_QUERY_TERMS):
        score -= 14
        reasons.append("penalty_broad_hub")
    if content_family == "raid_guide" and "raid" not in terms:
        score -= 12
        reasons.append("penalty_raid_variant")
    if content_family in {"expansion_guide", "special_event_guide"} and not (
        {"remix", "torghast", "midnight", "expansion"} & terms or " war within " in joined
    ):
        score -= 10
        reasons.append("penalty_specialized_variant")
    return score, reasons


def score_family_match(query: str, *, content_family: str | None) -> tuple[int, list[str]]:
    """Boost or penalize a candidate by how well the query intent matches its Icy Veins guide family."""
    if not query or not content_family:
        return 0, []
    terms = query_terms(query)
    joined = f" {query.lower()} "
    score = 0
    reasons: list[str] = []
    for family_score, family_reasons in (
        _score_broad_family_match(content_family, terms=terms),
        _score_specialized_family_match(query, content_family, terms=terms, joined=joined),
        _score_family_penalties(content_family, terms=terms, joined=joined),
    ):
        score += family_score
        reasons.extend(family_reasons)
    return score, reasons


def score_slug_match(query: str, candidate: str, *, slug: str) -> tuple[int, list[str]]:
    """Shared article title score plus Icy Veins slug shape signals (intro pages boosted, off-query slug words penalized)."""
    score, reasons = score_article_match(query, candidate)
    if not query or not candidate:
        return score, reasons
    if slug.endswith("-guide"):
        if any(token in slug for token in ("leveling", "dps", "pvp")):
            score += 2
            reasons.append("specialized_guide")
        else:
            score += 16
            reasons.append("intro_guide")
    query_words = query.split()
    penalty_terms = [term for term in slug.split("-") if term and term not in query_words and term not in NEUTRAL_SLUG_TERMS]
    if penalty_terms:
        score -= len(penalty_terms) * 3
    return score, reasons


def search_results(
    client: IcyVeinsClient,
    query: str,
    *,
    limit: int,
) -> tuple[str, list[dict[str, Any]], int, dict[str, Any] | None]:
    """Rank sitemap guides against ``query``; returns (normalized query, top matches, total matches, scope hint)."""
    normalized_query = normalize_search_query(query)
    scope_hint = unsupported_scope_hint(normalized_query)
    if scope_hint is not None:
        return normalized_query, [], 0, scope_hint
    matches: list[dict[str, Any]] = []
    for row in client.sitemap_guides():
        slug = row["slug"]
        name = row["name"]
        content_family = row.get("content_family")
        candidate = f"{name.lower()} {slug.replace('-', ' ')}"
        score, reasons = score_slug_match(normalized_query, candidate, slug=slug)
        family_score, family_reasons = score_family_match(normalized_query, content_family=content_family)
        score += family_score
        reasons.extend(family_reasons)
        if normalized_query and reasons and set(reasons) <= {"intro_guide", "specialized_guide"}:
            continue
        if score <= 0:
            continue
        candidate_row = article_candidate(
            ref=slug,
            name=name,
            url=row["url"],
            score=score,
            reasons=reasons,
            provider_command=PROVIDER_NAME,
        )
        candidate_row["metadata"]["content_family"] = content_family
        matches.append(candidate_row)
    sort_article_candidates(matches)
    return normalized_query, matches[:limit], len(matches), None


def resolve_is_confident(top: dict[str, Any] | None, second: dict[str, Any] | None) -> bool:
    """Decide whether the top candidate is a good enough match to answer a resolve outright."""
    if top is None:
        return False
    top_score = top["ranking"]["score"]
    second_score = second["ranking"]["score"] if second else 0
    top_reasons = set(top["ranking"]["match_reasons"])
    return (
        top_score >= 50
        or top_score >= second_score + 15
        or ("family_easy_mode" in top_reasons and top_score >= second_score + 10 and top_score >= 35)
        or ("intro_guide" in top_reasons and top_score >= second_score + 6 and top_score >= 30)
    )

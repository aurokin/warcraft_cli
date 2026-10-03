"""Icy Veins guide ranking: query normalization plus family-aware boosts and penalties.

Candidates are the sitemap's guides plus, once the sitemap has stopped being updated (it froze in
2025), the pages the sitemap lacks that the site-wide guide menu links (current pages) or the site
index lists (every page an ``index-refresh`` crawl has found, past seasons included). The index
also lends its page headlines to matching, so a boss page titled "... in The Voidspire" is found by
the raid's name. Search reads the index file only; it never crawls.

Query tokenization and the exact/prefix/contains/all-terms title score come from
``warcraft_content.search`` so they cannot drift from the other article providers; everything
below it (guide-family boosts, slug penalties, resolve confidence) is Icy Veins specific.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import httpx
from warcraft_content.article_discovery import article_candidate, sort_article_candidates
from warcraft_content.search import (
    MYTHIC_PLUS_RE,
    best_scored,
    expand_class_spec_aliases,
    fold_punctuation,
    normalize_query,
    punctuation_spellings,
    score_article_match,
    singular_words,
    tokenize_query,
)
from warcraft_core.identity import unique_spec_class
from warcraft_core.provider import ProviderError

from icy_veins_cli.client import SITE_MENU_SEED_URL, IcyVeinsClient
from icy_veins_cli.page_parser import CLASS_HUB_SLUGS, classify_guide_slug, guide_url, slug_display_name
from icy_veins_cli.site_index import SiteIndex, load_site_index

PROVIDER_NAME = "icy-veins"
QUERY_STRIP_TERMS = ("icy", "veins", "guide", "guides")
# Words that say nothing about which guide is meant, so they neither rank nor keep a row.
QUERY_STOP_WORDS = frozenset({"a", "an", "and", "for", "how", "in", "of", "on", "the", "to"})
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
# The introduction to a spec, a class or a role. Every other ``-guide`` page (a spec's leveling, PvP
# or pets page, a season hub, a dungeon page) is one topic among many and must not outrank the page
# that introduces what the query names.
INTRO_FAMILIES = frozenset({"spec_guide", "class_hub", "role_guide"})
# Pages the sitemap has not seen updated for a year behind its newest page (past seasons, retired
# raids) rank below current ones that match the query as well.
STALE_AFTER = timedelta(days=365)
STALE_PENALTY = 10
# A sitemap whose newest page is this far behind today has stopped being updated, so pages published
# since are missing from it: search and resolve then also read the site menu and say so in their
# provenance.
SITEMAP_STALE_AFTER = timedelta(days=30)
# A local site index older than this misses the pages published since; search says so.
SITE_INDEX_STALE_AFTER = timedelta(days=7)
# Transmog set and item-model pages (a sixth of the sitemap) name a class, so a class query matches
# them; unless the query asks for transmog they rank below every other page.
TRANSMOG_QUERY_TERMS = frozenset({"transmog", "transmogrification"})
TRANSMOG_PENALTY = 30
# A class/spec guide slug starts ``<spec>-<class>-`` (``shadow-priest-``, ``beast-mastery-hunter-``). A query
# naming that spec ranks these pages above pages that only share the word (``shadow-enclave-delve-guide``);
# every class sharing the spec gets the same boost, so ``frost`` stays a near-tie.
_CLASS_SLUGS = "|".join(slug.removesuffix("-guide") for slug in CLASS_HUB_SLUGS)
SPEC_GUIDE_SLUG_RE = re.compile(rf"^(?!(?:{_CLASS_SLUGS})-)(?P<spec>[a-z-]+?)-(?:{_CLASS_SLUGS})-")
SPEC_NAME_BONUS = 6
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
        "phrases": (" mythic plus ",),
    },
    {
        "family": "macros_addons",
        "score": 18,
        "reason": "family_macros_addons",
        "any_terms": {"macros", "addons", "ui"},
        "phrases": (" add ons ",),
    },
    {
        "family": "simulations",
        "score": 18,
        "reason": "family_simulations",
        "any_terms": {"simulation", "simulations", "sim"},
    },
    {"family": "raid_guide", "score": 18, "reason": "family_raid_guide", "all_terms": {"raid"}},
    {"family": "dungeon_guide", "score": 18, "reason": "family_dungeon_guide", "any_terms": {"dungeon", "dungeons"}},
    {"family": "delve_guide", "score": 18, "reason": "family_delve_guide", "any_terms": {"delve", "delves"}},
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


def _spell_out_plus(text: str) -> str:
    return MYTHIC_PLUS_RE.sub(" mythic plus ", text).replace("+", " plus ")


def normalize_search_query(query: str) -> str:
    """Drop the provider and 'guide' noise words so ranking sees only the meaningful part of the query.

    Every M+ spelling (``m+``, ``mythic+``) reads as "mythic plus", the words Icy Veins titles its
    pages with, and any other '+' is spelled out. Punctuation is folded the way the slugs fold it, so
    ``Nerub-ar Palace`` and ``K'aresh`` match ``nerub-ar-palace-raid-guide`` and ``karesh-zone-guide``.
    """
    return normalize_query(
        fold_punctuation(_spell_out_plus(expand_class_spec_aliases(query))), strip_terms=QUERY_STRIP_TERMS
    )


def query_terms(query: str) -> set[str]:
    return set(tokenize_query(query, stop_words=QUERY_STOP_WORDS))


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


def _rule_words(rule: dict[str, Any]) -> set[str]:
    """The query words that can fire ``rule``."""
    words = set(rule.get("all_terms") or ()) | set(rule.get("any_terms") or ())
    for phrase in rule.get("phrases") or ():
        words.update(phrase.split())
    return words


def _specialized_family_rule_matches(
    rule: dict[str, Any],
    *,
    content_family: str,
    terms: set[str],
    candidate_words: set[str],
    joined: str,
    lowered_query: str,
) -> bool:
    if content_family != rule["family"]:
        return False
    # A page that matches only the words naming its family is not what the rest of the query asks
    # for: every spec's M+ tips page holds "mythic plus", none holds "tier list".
    other_terms = terms - _rule_words(rule) - NEUTRAL_SLUG_TERMS
    if other_terms and not singular_words(other_terms) & candidate_words:
        return False
    all_terms = rule.get("all_terms")
    if all_terms and not set(all_terms) <= terms:
        return False
    any_terms = set(rule.get("any_terms") or ())
    phrase_matches = any(phrase in joined or phrase in lowered_query for phrase in tuple(rule.get("phrases") or ()))
    if any_terms or rule.get("phrases"):
        return bool(any_terms & terms) or phrase_matches
    return True


def _score_specialized_family_match(
    query: str, content_family: str, *, terms: set[str], candidate_words: set[str], joined: str
) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []
    lowered_query = query.lower()
    for rule in SPECIALIZED_FAMILY_RULES:
        if _specialized_family_rule_matches(
            rule,
            content_family=content_family,
            terms=terms,
            candidate_words=candidate_words,
            joined=joined,
            lowered_query=lowered_query,
        ):
            score += int(rule["score"])
            reasons.append(str(rule["reason"]))
    return score, reasons


def _score_family_penalties(content_family: str, *, slug: str, terms: set[str], joined: str) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []
    if content_family in {"class_hub", "role_guide"} and (terms & SPECIALIZED_QUERY_TERMS):
        score -= 14
        reasons.append("penalty_broad_hub")
    # Only a spec's raid variant (``frost-mage-pve-dps-<raid>-raid-guide``) yields to the spec guide; a raid's
    # own guide is what a query naming that raid wants.
    if content_family == "raid_guide" and "raid" not in terms and SPEC_GUIDE_SLUG_RE.match(slug):
        score -= 12
        reasons.append("penalty_raid_variant")
    if content_family in {"expansion_guide", "special_event_guide"} and not (
        {"remix", "torghast", "midnight", "expansion"} & terms or " war within " in joined
    ):
        score -= 10
        reasons.append("penalty_specialized_variant")
    if content_family == "transmog" and not terms & TRANSMOG_QUERY_TERMS:
        score -= TRANSMOG_PENALTY
        reasons.append("penalty_transmog")
    return score, reasons


def score_family_match(
    query: str, *, slug: str, content_family: str | None, candidate_words: set[str]
) -> tuple[int, list[str]]:
    """Boost or penalize a candidate by how well the query intent matches its Icy Veins guide family.

    ``candidate_words`` are the singular forms of the words in the candidate's title and slug.
    """
    if not query or not content_family:
        return 0, []
    terms = query_terms(query)
    joined = f" {query.lower()} "
    score = 0
    reasons: list[str] = []
    for family_score, family_reasons in (
        _score_broad_family_match(content_family, terms=terms),
        _score_specialized_family_match(query, content_family, terms=terms, candidate_words=candidate_words, joined=joined),
        _score_family_penalties(content_family, slug=slug, terms=terms, joined=joined),
    ):
        score += family_score
        reasons.extend(family_reasons)
    return score, reasons


def score_slug_match(query: str, candidate: str, *, slug: str, content_family: str | None) -> tuple[int, list[str]]:
    """Shared article title score plus Icy Veins slug shape signals (intro pages boosted, off-query slug words penalized)."""
    score, reasons = score_article_match(query, candidate)
    if not query or not candidate:
        return score, reasons
    if content_family in INTRO_FAMILIES:
        # Healer specs also publish a secondary ``-pve-dps-guide``; it scores clearly below their
        # ``-pve-healing-guide`` so a healer query still resolves to the healing guide.
        score += 10 if slug.endswith("-pve-dps-guide") else 16
        reasons.append("intro_guide")
    elif slug.endswith("-guide"):
        score += 2
        reasons.append("specialized_guide")
    query_words = query.split()
    penalty_terms = [term for term in slug.split("-") if term and term not in query_words and term not in NEUTRAL_SLUG_TERMS]
    if penalty_terms:
        score -= len(penalty_terms) * 3
    return score, reasons


def newest_sitemap_lastmod(rows: list[dict[str, Any]]) -> str | None:
    return max((row["sitemap_lastmod"] for row in rows if row.get("sitemap_lastmod")), default=None)


def _stale_before(newest: str | None) -> str | None:
    """Cut-off date for stale pages: a year before the newest ``sitemap_lastmod`` in the sitemap.

    Anchored to the sitemap rather than the clock, so ranking depends only on the data it ranks.
    """
    return (date.fromisoformat(newest) - STALE_AFTER).isoformat() if newest else None


def sitemap_is_stale(newest: str | None, *, today: date) -> bool:
    return newest is not None and today - date.fromisoformat(newest) > SITEMAP_STALE_AFTER


def _site_index_note(site_index: SiteIndex | None) -> str:
    """What the stale-sitemap warning adds about the site index when the user has not built one."""
    if site_index is None:
        return " No site index is available; run `icy-veins index-refresh` to build one."
    if site_index.bundled:
        return (
            f" The site index is the snapshot bundled with this release (refreshed {site_index.refreshed_at}); "
            "run `icy-veins index-refresh` to index the pages published since."
        )
    return ""


def _site_index_warning(site_index: SiteIndex | None, index_gap: tuple[str, ...], *, today: date) -> str | None:
    """A warning when the local index is over a week old or the live menu links pages it lacks."""
    if site_index is None:
        return None
    problems: list[str] = []
    refreshed = datetime.fromisoformat(site_index.refreshed_at).date() if site_index.refreshed_at else None
    if not site_index.bundled and (refreshed is None or today - refreshed > SITE_INDEX_STALE_AFTER):
        problems.append(f"was last refreshed {refreshed}")
    if index_gap:
        shown = ", ".join(index_gap[:5]) + (", ..." if len(index_gap) > 5 else "")
        problems.append(f"lacks {len(index_gap)} pages the live site menu links ({shown})")
    if not problems:
        return None
    return (
        f"The Icy Veins site index {' and '.join(problems)}; run `icy-veins index-refresh` to add the pages "
        "published since and the pages they link."
    )


def sitemap_provenance(
    sitemap_url: str,
    newest: str | None,
    *,
    today: date,
    site_menu_warning: str | None = None,
    site_index: SiteIndex | None = None,
    index_gap: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Where discovery read its guides from, with a warning when the sitemap has stopped being updated.

    A stale sitemap is when search also reads the site menu and the site index; ``site_menu_warning``
    is set when that menu could not be read, and ``site_index_warning`` when the index is out of date.
    """
    provenance: dict[str, Any] = {"sitemap_url": sitemap_url, "sitemap_newest_lastmod": newest}
    if not sitemap_is_stale(newest, today=today):
        return provenance
    provenance["site_menu_url"] = SITE_MENU_SEED_URL
    if site_index is not None:
        provenance.update(site_index_path=site_index.path, site_index_refreshed_at=site_index.refreshed_at)
    note = _site_index_note(site_index)
    if site_menu_warning is None:
        provenance["sitemap_warning"] = (
            f"The Icy Veins sitemap was last updated {newest}; guides published since then are found only through "
            'the site-wide guide menu (results with metadata.source "site_menu"), which links current pages only, '
            'and the site index (metadata.source "site_index"), which lists the pages an `icy-veins index-refresh` '
            f"crawl has found.{note} Open any other guide directly with `icy-veins guide <slug-or-url>`."
        )
    else:
        provenance["sitemap_warning"] = (
            f"The Icy Veins sitemap was last updated {newest}, so guides published or retitled since then are missing "
            f'from these results unless the site index lists them (metadata.source "site_index").{note} Open a known '
            "guide directly with `icy-veins guide <slug-or-url>`."
        )
        provenance["site_menu_warning"] = site_menu_warning
    if (index_warning := _site_index_warning(site_index, index_gap, today=today)) is not None:
        provenance["site_index_warning"] = index_warning
    return provenance


def _add_site_menu_rows(client: IcyVeinsClient, rows: list[dict[str, Any]]) -> tuple[str | None, set[str]]:
    """Append the site-menu pages ``rows`` lacks and return every slug the menu links.

    A menu that cannot be read must not fail search: the sitemap rows are still ranked, and the
    warning returned instead of the slugs says why the menu's pages are missing.
    """
    try:
        menu_rows = client.site_menu_guides()
    except httpx.HTTPStatusError as exc:
        reason = f"HTTP {exc.response.status_code}"
    except (ProviderError, httpx.HTTPError) as exc:
        reason = str(exc) or type(exc).__name__
    else:
        listed = {row["slug"] for row in rows}
        rows.extend({**row, "source": "site_menu"} for row in menu_rows if row["slug"] not in listed)
        return None, {row["slug"] for row in menu_rows}
    warning = (
        f"The Icy Veins guide menu on {SITE_MENU_SEED_URL} could not be read ({reason}), so guides missing from "
        "the sitemap are missing from these results."
    )
    return warning, set()


def _indexed_family(slug: str, parent: str | None) -> str | None:
    """A page's family, with the breadcrumb settling what the slug cannot.

    The current raid's boss pages are ``<boss>-raid-guide`` like the raid's own guide; their
    breadcrumb parent is that raid guide, so they are boss pages and do not take the raid boost.
    """
    content_family = classify_guide_slug(slug)
    if content_family == "raid_guide" and parent and classify_guide_slug(parent) == "raid_guide":
        return "raid_encounter"
    return content_family


def _add_site_index_rows(site_index: SiteIndex, rows: list[dict[str, Any]]) -> None:
    """Join the site index into ``rows``, which the sitemap and the menu filled.

    A listed page gains the index's headline for matching, its publication date and its breadcrumb
    parent; a classified page nobody listed becomes a "site_index" row named by its headline. The old
    slug of a renamed page is dropped when its new slug is listed, so the page is not ranked twice.
    """
    listed = {row["slug"]: row for row in rows}
    for slug, entry in site_index.pages.items():
        if entry["status"] != "ok" or (content_family := _indexed_family(slug, entry["parent"])) is None:
            continue
        indexed = {"content_family": content_family, "date_published": entry["date_published"], "parent": entry["parent"]}
        if slug in listed:
            listed[slug].update(indexed, index_title=entry["title"])
        else:
            rows.append(
                {
                    "slug": slug,
                    "name": entry["title"] or slug_display_name(slug),
                    "url": guide_url(slug),
                    "sitemap_lastmod": None,
                    "source": "site_index",
                    **indexed,
                }
            )
    present = {row["slug"] for row in rows}
    renamed = {slug for slug, entry in site_index.pages.items() if entry["status"] == "redirect" and entry["redirect_to"] in present}
    rows[:] = [row for row in rows if row["slug"] not in renamed]


def _scored_candidate(row: dict[str, Any], query: str, terms: set[str], *, stale_before: str | None) -> dict[str, Any] | None:
    slug = row["slug"]
    content_family = row.get("content_family")
    # Spelled out like the query, so a page titled with shorthand ("Disc Belt Guide") still matches it.
    # A site-menu page also matches on its menu title ("Glory Raid Achievement"), and an indexed page
    # on its headline ("Vorasius Raid Guide in The Voidspire for Midnight Season 1").
    title = " ".join(filter(None, (row["name"], row.get("menu_title"), row.get("index_title"))))
    candidate = fold_punctuation(_spell_out_plus(expand_class_spec_aliases(f"{title} {slug.replace('-', ' ')}")))
    # Icy Veins drops "plus" from its newer seasonal slugs (``midnight-mythic-season-2-guide``) but not from
    # older ones or from headlines ("Midnight Mythic+ Season 1 Guide"). Leading with the "mythic plus season"
    # spelling scores every season alike on it, prefix included, so only the stale penalty separates
    # seasons, while the page's own spelling, kept after it, still matches.
    if "mythic season" in candidate:
        candidate = candidate.replace("mythic season", "mythic plus season") + " " + candidate
    # Family boosts alone (a class hub for any one-word query) must not surface an unrelated guide,
    # and a term only counts as a whole word: "dh" is not a match for "headhunters".
    candidate_words = singular_words(set(tokenize_query(candidate)))
    if not terms & candidate_words:
        return None
    score, reasons = score_slug_match(query, candidate, slug=slug, content_family=content_family)
    # A raid's own guide is also titled by the raid's name alone: "venomous abyss" for ``venomous-abyss-raid-guide``.
    if query in {normalize_search_query(name.replace("-", " ")) for name in (slug, slug.removesuffix("-raid-guide"))}:
        reasons.append("exact_title")
    family_score, family_reasons = score_family_match(query, slug=slug, content_family=content_family, candidate_words=candidate_words)
    score += family_score
    reasons.extend(family_reasons)
    if query and reasons and set(reasons) <= {"intro_guide", "specialized_guide"}:
        return None
    lastmod = row.get("sitemap_lastmod")
    # A site-menu or site-index page has no lastmod and is never stale: the live menu links only
    # current pages, and the index's dateModified was bulk-reset in 2026, so it dates nothing.
    if stale_before and lastmod and lastmod < stale_before:
        score -= STALE_PENALTY
        reasons.append("penalty_stale_page")
    if score <= 0:
        return None
    # Added after the cut-off, so the spec bonus reorders the rows that match and never admits one.
    spec_slug = SPEC_GUIDE_SLUG_RE.match(slug)
    if spec_slug and set(spec_slug["spec"].split("-")) <= set(query.split()):
        score += SPEC_NAME_BONUS
        reasons.append("spec_name")
    candidate_row = article_candidate(
        ref=slug,
        name=row["name"],
        url=row["url"],
        score=score,
        reasons=reasons,
        provider=PROVIDER_NAME,
    )
    candidate_row["metadata"].update(
        content_family=content_family,
        sitemap_lastmod=lastmod,
        date_published=row.get("date_published"),
        parent=row.get("parent"),
        source=row["source"],
    )
    return candidate_row


def _recency(row: dict[str, Any]) -> str:
    """How new a page is, for breaking score ties.

    A site-menu page is current. A page only the index knows is dated by when it was published, which
    for a page published since the sitemap froze is newer than any sitemap date.
    """
    metadata = row["metadata"]
    if metadata["source"] == "site_menu":
        return "9999-12-31"
    return str(metadata["sitemap_lastmod"] or metadata["date_published"] or "")


@dataclass(frozen=True, slots=True)
class SearchOutcome:
    """Every ranked match, so resolve judges confidence on all of them and only the caller trims to ``--limit``."""

    normalized_query: str
    matches: list[dict[str, Any]]
    scope_hint: dict[str, Any] | None = None
    sitemap_newest_lastmod: str | None = None
    site_menu_warning: str | None = None
    site_index: SiteIndex | None = None
    # Slugs the live menu links that the site index lacks.
    index_gap: tuple[str, ...] = ()


def search_results(client: IcyVeinsClient, query: str, *, today: date) -> SearchOutcome:
    """Rank the sitemap guides, plus the site-menu and site-index ones when the sitemap is stale, against ``query``."""
    normalized_query = normalize_search_query(query)
    scope_hint = unsupported_scope_hint(normalized_query)
    if scope_hint is not None:
        return SearchOutcome(normalized_query, [], scope_hint)
    spellings = [
        (spelling, singular_words(query_terms(spelling))) for spelling in map(normalize_search_query, punctuation_spellings(query))
    ]
    sitemap_rows = client.sitemap_guides()
    # Anchored to the sitemap's own dates: site-menu rows have none.
    newest = newest_sitemap_lastmod(sitemap_rows)
    rows = [{**row, "source": "sitemap"} for row in sitemap_rows]
    site_menu_warning: str | None = None
    site_index: SiteIndex | None = None
    index_gap: tuple[str, ...] = ()
    if sitemap_is_stale(newest, today=today):
        site_menu_warning, menu_slugs = _add_site_menu_rows(client, rows)
        site_index = load_site_index()
        if site_index is not None:
            _add_site_index_rows(site_index, rows)
            index_gap = tuple(sorted(menu_slugs - set(site_index.pages)))
    stale_before = _stale_before(newest)
    matches = [
        candidate
        for candidate in (
            best_scored(_scored_candidate(row, spelling, terms, stale_before=stale_before) for spelling, terms in spellings)
            for row in rows
        )
        if candidate is not None
    ]
    sort_article_candidates(matches)
    # That sort dates rows by the sitemap alone; a site-menu or site-index page wins its ties as the newer page.
    matches.sort(key=_recency, reverse=True)
    matches.sort(key=lambda row: -int(row["ranking"]["score"]))
    return SearchOutcome(normalized_query, matches, None, newest, site_menu_warning, site_index, index_gap)


# The slug endings of a page about one place, beside the raid guides ``exact_title`` covers:
# ``karesh-zone-guide`` is the Icy Veins page for K'aresh.
PLACE_GUIDE_SUFFIXES = ("zoneguide", "dungeonguide", "heroicdungeonguide", "delveguide")


def names_single_word(word: str, row: Mapping[str, Any]) -> bool:
    """Icy Veins' own answer to a one-word query: the page titled by it, a place's page, or a unique spec's guide.

    ``exact_title`` is the page whose slug, guide words stripped, is the word ("druid" for
    ``druid-guide``). A spec word only one class has ("shadow") names that spec's guide; a shared
    one ("frost") names none.
    """
    reasons = row["ranking"]["match_reasons"]
    place_page = str(row["id"]).replace("-", "") in {f"{word}{suffix}" for suffix in PLACE_GUIDE_SUFFIXES}
    spec_guide = row["metadata"].get("content_family") == "spec_guide" and "spec_name" in reasons
    return "exact_title" in reasons or place_page or (spec_guide and unique_spec_class(word) is not None)


def resolve_is_confident(results: list[dict[str, Any]]) -> bool:
    """Decide whether the top candidate is a good enough match to answer a resolve outright."""
    if not results:
        return False
    top, *rivals = results
    top_score = top["ranking"]["score"]
    second_score = rivals[0]["ranking"]["score"] if rivals else 0
    top_reasons = set(top["ranking"]["match_reasons"])
    # A tie or a near-tie is never an answer, however high both candidates score: "frost" is a mage
    # and a death knight spec, and only the off-query slug words tell those guides apart. The one
    # exception is a hub named exactly by the query whose close rivals are all its own sub-pages
    # ("player housing" over ``player-housing-interior-guide``, or over ``housing-decor-guide``, whose
    # breadcrumb parent the site index knows to be the hub).
    hub_prefix = top["id"].removesuffix("guide") if "exact_title" in top_reasons else None
    return (
        top_score >= second_score + 15
        or ("family_easy_mode" in top_reasons and top_score >= second_score + 10 and top_score >= 35)
        or ("intro_guide" in top_reasons and top_score >= second_score + 6 and top_score >= 30)
        or (
            hub_prefix is not None
            and all(
                row["id"].startswith(hub_prefix) or row["metadata"].get("parent") == top["id"]
                for row in rivals
                if row["ranking"]["score"] > top_score - 15
            )
        )
    )

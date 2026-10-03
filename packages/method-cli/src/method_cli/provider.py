"""Pure Method.gg provider surface.

Nothing here prints or raises ``typer.Exit``: every function returns an envelope or raises
``ProviderError``. ``method_cli.main`` is the thin Typer layer over these functions, and the
``warcraft`` wrapper can call ``PROVIDER`` in-process.
"""

from __future__ import annotations

import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from warcraft_api.cache import redacted_redis_url
from warcraft_content.article_bundle import article_export_dir, bundle_query_payload
from warcraft_content.article_discovery import (
    article_candidate,
    article_follow_up,
    article_resolve_payload,
    article_search_payload,
    sort_article_candidates,
)
from warcraft_content.article_provider_cli import (
    article_doctor_payload,
    guide_bundle_payload,
    guide_export_payload,
    guide_redirect,
    preview_block,
    require_article_content,
    transport_errors,
    with_analysis_surfaces,
)
from warcraft_content.search import (
    MYTHIC_PLUS_RE,
    ArticleMatchWeights,
    best_scored,
    expand_class_spec_aliases,
    fold_punctuation,
    normalize_query,
    punctuation_spellings,
    score_article_match,
    singular_words,
    tokenize_query,
)
from warcraft_core.discovery import RESOLVE_KIND, SEARCH_KIND
from warcraft_core.envelope import Envelope, success_envelope
from warcraft_core.identity import unique_spec_class
from warcraft_core.provider import ProviderError, ProviderSurface

from method_cli.client import METHOD_SITEMAP_URL, MethodClient, guide_ref_parts, load_method_cache_settings_from_env
from method_cli.page_parser import UNSUPPORTED_ROOT_GUIDE_SLUGS, classify_guide_family, guide_url

PROVIDER_NAME: Final = "method"
PROVIDER_LABEL: Final = "Method"
# Method guide titles are short, so an all-terms hit is worth less here than on long article titles.
MATCH_WEIGHTS: Final = ArticleMatchWeights(all_terms=8)
QUERY_NOISE_TERMS: Final = ("method", "guide", "guides")
FAMILY_SCORE_BOOST: Final = 12
FAMILY_QUERY_KEYWORDS: Final[dict[str, frozenset[str]]] = {
    "profession_guide": frozenset(
        {
            "profession",
            "professions",
            "alchemy",
            "blacksmithing",
            "enchanting",
            "engineering",
            "herbalism",
            "inscription",
            "jewelcrafting",
            "leatherworking",
            "mining",
            "skinning",
            "tailoring",
            "fishing",
            "cooking",
        }
    ),
    "delve_guide": frozenset({"delve", "delves"}),
    "reputation_guide": frozenset({"renown", "reputation"}),
}
# Words naming a section every Method class guide has (``/guides/<spec>/<section>``), mapped to its slug.
# A class guide is titled by its spec alone, so ``arcane mage talents`` matched nothing: the word is
# scored away for class guides and the row's follow-up opens that section.
SECTION_QUERY_TERMS: Final[dict[str, str]] = {
    **dict.fromkeys(("talent", "talents", "build", "builds"), "talents"),
    **dict.fromkeys(("gear", "gearing", "bis"), "gearing"),
    **dict.fromkeys(("stat", "stats", "race", "races", "consumables", "enchants", "gems"), "stats-races-and-consumables"),
    **dict.fromkeys(("rotation", "playstyle", "opener", "openers"), "playstyle-and-rotation"),
    **dict.fromkeys(("interface", "macro", "macros", "ui", "addons"), "interface-and-macros"),
}
# (hint code, query terms that must all be present, message) for roots we intentionally exclude from discovery.
UNSUPPORTED_QUERY_HINTS: Final[tuple[tuple[str, frozenset[str], str], ...]] = (
    (
        "tier_list",
        frozenset({"tier", "list"}),
        "Method tier-list index roots are currently out of scope for the supported Method surface.",
    ),
)
SUPPORTED_SCOPE: Final[dict[str, Any]] = {
    "content_families": [
        "class_guide",
        "profession_guide",
        "delve_guide",
        "reputation_guide",
        "article_guide",
    ],
    "url_patterns": ["/guides/<slug>", "/guides/<slug>/<section>"],
    "unsupported_roots": sorted(UNSUPPORTED_ROOT_GUIDE_SLUGS),
    "notes": [
        "search and resolve are limited to the currently supported Method guide families",
        "tier-list and world-of-warcraft roots are intentionally excluded because they currently use unsupported index-style templates",
        "premium, login, and non-guide Method surfaces are intentionally out of scope",
    ],
}
GUIDE_QUERY_KINDS: Final = frozenset({"sections", "navigation", "linked_entities", "build_references", "analysis_surfaces"})
SITEMAP_PROVENANCE: Final[dict[str, Any]] = {"sitemap_url": METHOD_SITEMAP_URL}


def _envelope(
    *,
    command: str,
    kind: str,
    payload: dict[str, Any],
    query: Any = None,
    provenance: dict[str, Any] | None = None,
) -> Envelope:
    return success_envelope(provider=PROVIDER_NAME, command=command, kind=kind, data=payload, query=query, provenance=provenance)


def open_client() -> MethodClient:
    try:
        return MethodClient()
    except ValueError as exc:
        raise ProviderError("invalid_cache_config", str(exc)) from exc


@dataclass(frozen=True, slots=True)
class SearchOutcome:
    """Every ranked match, so resolve judges confidence on all of them and only the caller trims to ``--limit``."""

    normalized_query: str
    matches: list[dict[str, Any]]
    scope_hint: dict[str, str] | None = None


def _unsupported_scope_hint(terms: set[str]) -> dict[str, str] | None:
    if not terms:
        return None
    for code, keywords, message in UNSUPPORTED_QUERY_HINTS:
        if keywords <= terms:
            return {"code": code, "message": message}
    return None


def _family_score_boost(content_family: str, terms: set[str]) -> tuple[int, list[str]]:
    keywords = FAMILY_QUERY_KEYWORDS.get(content_family)
    if not keywords or not (terms & keywords):
        return 0, []
    return FAMILY_SCORE_BOOST, ["content_family_match"]


def _scored_candidate(row: dict[str, Any], normalized_query: str, terms: set[str]) -> dict[str, Any] | None:
    slug = row["slug"]
    content_family = classify_guide_family(slug)
    if content_family == "unsupported_index":
        return None
    name = row["name"]
    # Spelled out like the query, so a page titled with shorthand still matches it.
    candidate = expand_class_spec_aliases(f"{name} {slug.replace('-', ' ')}")
    # A term only counts as a whole word: "mage" is not a match for "damage", nor "lore" for "lorewalking".
    if not singular_words(terms) & singular_words(set(tokenize_query(candidate))):
        return None
    score, reasons = score_article_match(normalized_query, candidate, weights=MATCH_WEIGHTS)
    family_boost, family_reasons = _family_score_boost(content_family, terms)
    score += family_boost
    reasons.extend(family_reasons)
    if score <= 0:
        return None
    candidate_row = article_candidate(
        ref=slug,
        name=name,
        url=row["url"],
        score=score,
        reasons=reasons,
        provider=PROVIDER_NAME,
    )
    candidate_row["metadata"].update(content_family=content_family, sitemap_lastmod=row.get("sitemap_lastmod"))
    return candidate_row


def _section_candidate(row: dict[str, Any], spelling: str, section: str) -> dict[str, Any] | None:
    """A class guide matched on ``spelling`` without its section words, following up with that section."""
    if classify_guide_family(row["slug"]) != "class_guide":
        return None
    kept = " ".join(word for word in spelling.split() if word not in SECTION_QUERY_TERMS)
    candidate = _scored_candidate(row, kept, set(tokenize_query(kept)))
    if candidate is None:
        return None
    ref = f"{row['slug']}/{section}"
    candidate["ranking"]["match_reasons"].append("section_query")
    candidate["metadata"]["section_slug"] = section
    candidate.update(url=guide_url(row["slug"], section), follow_up=article_follow_up(PROVIDER_NAME, ref))
    return candidate


def _query_section(normalized_query: str) -> str | None:
    """The class-guide section a query names, when it also names something besides sections."""
    words = normalized_query.split()
    sections = [SECTION_QUERY_TERMS[word] for word in words if word in SECTION_QUERY_TERMS]
    return sections[0] if sections and len(sections) < len(words) else None


def _normalize_search_query(query: str) -> str:
    # Method never writes "Mythic+" or "Mythic Plus": its M+ pages are about "mythic dungeons".
    return normalize_query(
        fold_punctuation(MYTHIC_PLUS_RE.sub("mythic dungeon", expand_class_spec_aliases(query))), strip_terms=QUERY_NOISE_TERMS
    )


def search_results(client: MethodClient, query: str) -> SearchOutcome:
    """Rank the sitemap's supported guide slugs against ``query``; every match is kept, callers trim to ``--limit``."""
    normalized_query = _normalize_search_query(query)
    scope_hint = _unsupported_scope_hint(set(tokenize_query(normalized_query)))
    if scope_hint is not None:
        return SearchOutcome(normalized_query, [], scope_hint)
    spellings = [(spelling, set(tokenize_query(spelling))) for spelling in map(_normalize_search_query, punctuation_spellings(query))]
    section = _query_section(normalized_query)
    matches = [
        candidate
        for candidate in (
            best_scored(
                [
                    *(_scored_candidate(row, spelling, terms) for spelling, terms in spellings),
                    *(_section_candidate(row, spelling, section) for spelling, _ in spellings if section),
                ]
            )
            for row in client.sitemap_guides()
        )
        if candidate is not None
    ]
    sort_article_candidates(matches)
    return SearchOutcome(normalized_query, matches)


def _search_outcome(query: str) -> SearchOutcome:
    if not query.strip():
        raise ProviderError("invalid_query", "Query cannot be empty.")
    with open_client() as client, transport_errors(PROVIDER_LABEL):
        return search_results(client, query)


def _reject_unsupported_surface(payload: dict[str, Any]) -> None:
    if payload["guide"].get("supported_surface") is not False:
        return
    guide = payload["guide"]
    raise ProviderError(
        "unsupported_guide_surface",
        f"Unsupported Method guide surface for slug={guide['slug']!r} family={guide.get('content_family')!r}.",
    )


def _fetch_guide_page(client: MethodClient, guide_ref: str) -> dict[str, Any]:
    try:
        guide_ref_parts(guide_ref)
    except ValueError as exc:
        raise ProviderError("invalid_guide_ref", str(exc)) from exc
    # Anything that fails past this point is a page problem, not a bad argument.
    with transport_errors(PROVIDER_LABEL, missing_message=f"Guide not found: {guide_ref}"):
        try:
            payload = client.fetch_guide_page(guide_ref)
        except ValueError as exc:
            raise ProviderError("parse_failed", f"Could not parse the Method guide page for {guide_ref}: {exc}") from exc
    _reject_unsupported_surface(payload)
    payload = require_article_content(payload, provider_label=PROVIDER_LABEL)
    payload["redirect"] = guide_redirect(
        provider_label=PROVIDER_LABEL, requested=guide_ref_parts(guide_ref)[0], served=payload["guide"]["slug"]
    )
    return payload


def _guide_summary_payload(page_payload: dict[str, Any]) -> dict[str, Any]:
    guide = dict(page_payload["guide"])
    article = dict(page_payload["article"])
    navigation = list(page_payload["navigation"])
    fetch_more_command = shlex.join(["method", "guide-full", guide["slug"]])
    return {
        "guide": guide,
        "redirect": page_payload["redirect"],
        "page": dict(page_payload["page"]),
        "navigation": {
            "count": len(navigation),
            "items": navigation,
        },
        "article": {
            "text": article["text"],
            "headings": article["headings"],
            "section_count": len(article["sections"]),
            "section_preview": [
                {
                    "title": section["title"],
                    "level": section["level"],
                    "ordinal": section["ordinal"],
                }
                for section in article["sections"][:5]
            ],
        },
        "linked_entities": preview_block(list(page_payload["linked_entities"]), fetch_more_command=fetch_more_command),
        "build_references": preview_block(list(page_payload.get("build_references") or []), fetch_more_command=fetch_more_command),
        "analysis_surfaces": preview_block(list(page_payload.get("analysis_surfaces") or []), fetch_more_command=fetch_more_command),
        "citations": {
            "page": guide["page_url"],
        },
    }


def _navigation_items(initial: dict[str, Any]) -> list[dict[str, Any]]:
    guide = initial["guide"]
    if initial["navigation"]:
        return list(initial["navigation"])
    if guide["content_family"] == "class_guide":
        # Every class guide has a section switcher; walking only this page would pass one section off as the guide.
        raise ProviderError(
            "parse_failed",
            f"No guide navigation parsed from {guide['page_url']}; the Method navigation layout has probably changed.",
            details={"page_url": guide["page_url"]},
        )
    return [
        {
            "title": guide["section_title"],
            "url": guide["page_url"],
            "section_slug": guide["section_slug"],
            "active": True,
            "ordinal": 1,
        }
    ]


def _guide_pages_payload(client: MethodClient, guide_ref: str) -> dict[str, Any]:
    initial = _fetch_guide_page(client, guide_ref)
    return guide_bundle_payload(
        initial,
        _navigation_items(initial),
        fetch_page=client.fetch_guide_page,
        provider=PROVIDER_NAME,
        provider_label=PROVIDER_LABEL,
    )


def guide(guide_ref: str) -> Envelope:
    """One Method guide page plus a preview of its linked entities, build references, and analysis surfaces."""
    with open_client() as client, transport_errors(PROVIDER_LABEL):
        payload = _guide_summary_payload(with_analysis_surfaces(_fetch_guide_page(client, guide_ref), provider=PROVIDER_NAME))
    return _envelope(command="guide", kind="guide", payload=payload, query=guide_ref, provenance=payload["citations"])


def guide_full(guide_ref: str) -> Envelope:
    """Every navigation page of a Method guide with merged linked entities, build references, and analysis surfaces."""
    with open_client() as client, transport_errors(PROVIDER_LABEL):
        payload = _guide_pages_payload(client, guide_ref)
    return _envelope(command="guide-full", kind="guide_full", payload=payload, query=guide_ref, provenance=payload["citations"])


def guide_export(guide_ref: str, *, out: Path | None = None) -> Envelope:
    """Write every page of a Method guide to a local bundle directory and return its counts and file list."""
    try:
        slug, _section_slug = guide_ref_parts(guide_ref)
    except ValueError as exc:
        raise ProviderError("invalid_guide_ref", str(exc)) from exc
    export_dir = article_export_dir(out, provider=PROVIDER_NAME, ref_slug=slug)
    with open_client() as client, transport_errors(PROVIDER_LABEL):
        bundle = _guide_pages_payload(client, guide_ref)
    payload = guide_export_payload(bundle, provider=PROVIDER_NAME, export_dir=export_dir)
    return _envelope(command="guide-export", kind="guide_export", payload=payload, query=guide_ref, provenance=bundle["citations"])


def guide_query(
    bundle_ref: str,
    query: str,
    *,
    limit: int = 5,
    kinds: set[str] | None = None,
    section_title: str | None = None,
) -> Envelope:
    """Search an exported Method bundle on disk; no network access."""
    payload = bundle_query_payload(
        bundle_ref, query, limit=limit, kinds=kinds, allowed_kinds=GUIDE_QUERY_KINDS, section_title=section_title
    )
    return _envelope(command="guide-query", kind="guide_query", payload=payload, query=query)


def _is_confident_match(results: list[dict[str, Any]]) -> bool:
    if not results:
        return False
    top_score = int(results[0]["ranking"]["score"])
    second_score = int(results[1]["ranking"]["score"]) if len(results) > 1 else 0
    return top_score >= second_score + 15


def _names_single_word(word: str, row: Mapping[str, Any]) -> bool:
    """Method's own answer to a one-word query: a spec word only one class has names that spec's guide ("shadow" -> ``shadow-priest``)."""
    actor_class = unique_spec_class(word)
    return actor_class is not None and str(row["id"]).replace("-", "") == f"{word}{actor_class}"


class MethodProvider:
    """Sitemap-backed discovery over the supported Method.gg guide families."""

    name = PROVIDER_NAME

    def search(self, query: str, *, limit: int = 5, **options: Any) -> Envelope:
        outcome = _search_outcome(query)
        payload = article_search_payload(
            query=query,
            search_query=outcome.normalized_query,
            matches=outcome.matches,
            limit=limit,
            scope_hint=outcome.scope_hint,
        )
        return _envelope(command="search", kind=SEARCH_KIND, payload=payload, query=query, provenance=SITEMAP_PROVENANCE)

    def resolve(self, target: str, **options: Any) -> Envelope:
        limit = int(options.get("limit", 5))
        outcome = _search_outcome(target)
        payload = article_resolve_payload(
            provider_command=PROVIDER_NAME,
            query=target,
            search_query=outcome.normalized_query,
            matches=outcome.matches,
            limit=limit,
            # Judged on every match: ``--limit`` must not hide the near-tied rival that makes it ambiguous.
            resolved=_is_confident_match(outcome.matches),
            scope_hint=outcome.scope_hint,
            single_word_identity=_names_single_word,
        )
        return _envelope(command="resolve", kind=RESOLVE_KIND, payload=payload, query=target, provenance=SITEMAP_PROVENANCE)

    def doctor(self, **options: Any) -> Envelope:
        try:
            settings, sitemap_ttl, page_ttl = load_method_cache_settings_from_env()
        except ValueError as exc:
            raise ProviderError("invalid_cache_config", str(exc)) from exc
        payload = article_doctor_payload(
            settings,
            redis_url=redacted_redis_url(settings.redis_url),
            sitemap_ttl=sitemap_ttl,
            page_ttl=page_ttl,
            supported_scope=SUPPORTED_SCOPE,
        )
        return _envelope(command="doctor", kind="doctor", payload=payload)


PROVIDER: ProviderSurface = MethodProvider()

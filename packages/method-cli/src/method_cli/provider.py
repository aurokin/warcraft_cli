"""Pure Method.gg provider surface.

Nothing here prints or raises ``typer.Exit``: every function returns an envelope or raises
``ProviderError``. ``method_cli.main`` is the thin Typer layer over these functions, and the
``warcraft`` wrapper can call ``PROVIDER`` in-process.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, cast

import httpx
from warcraft_content.article_bundle import (
    default_article_export_dir,
    load_article_bundle,
    query_article_bundle,
    write_article_bundle,
)
from warcraft_content.article_discovery import (
    article_candidate,
    merge_article_build_references,
    merge_article_linked_entities,
    sort_article_candidates,
)
from warcraft_content.article_provider_cli import (
    build_article_resolve_response,
    build_article_search_response,
    unsupported_guide_surface_message,
)
from warcraft_content.guide_analysis import extract_guide_analysis_surfaces, merge_guide_analysis_surfaces
from warcraft_content.search import ArticleMatchWeights, normalize_query, score_article_match, tokenize_query
from warcraft_core.envelope import ENVELOPE_KEYS, Envelope, success_envelope, with_legacy_keys
from warcraft_core.provider import ProviderError, ProviderSurface

from method_cli.client import METHOD_SITEMAP_URL, MethodClient, guide_ref_parts, load_method_cache_settings_from_env
from method_cli.page_parser import UNSUPPORTED_ROOT_GUIDE_SLUGS, classify_guide_family

PROVIDER_NAME: Final = "method"
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
PREVIEW_LIMIT: Final = 10


def _envelope(
    *,
    command: str,
    kind: str,
    payload: dict[str, Any],
    query: Any = None,
    provenance: dict[str, Any] | None = None,
) -> Envelope:
    """Wrap a Method payload in the shared envelope, repeating the historical top-level keys as deprecated copies."""
    envelope = success_envelope(
        provider=PROVIDER_NAME,
        command=command,
        kind=kind,
        data=payload,
        query=query,
        provenance=provenance,
    )
    # Payload keys that share a name with an envelope key (query, provider, command, ok) already
    # carry the same value in the envelope, so only the remaining keys are copied to the top level.
    legacy = {key: value for key, value in payload.items() if key not in ENVELOPE_KEYS}
    # with_legacy_keys returns a plain dict because the legacy copies live outside the TypedDict.
    return cast(Envelope, with_legacy_keys(envelope, legacy))


@contextmanager
def transport_errors() -> Iterator[None]:
    """Translate httpx transport failures into ProviderError so no caller sees a traceback."""
    try:
        yield
    except httpx.TimeoutException as exc:
        raise ProviderError("timeout", str(exc) or "Method request timed out") from exc
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        details = {"status_code": status, "url": str(exc.request.url)}
        if status in (401, 403):
            raise ProviderError("auth_failed", str(exc), details=details) from exc
        if status == 404:
            raise ProviderError("not_found", str(exc), details=details) from exc
        raise ProviderError("upstream_error", str(exc), details=details) from exc
    except httpx.RequestError as exc:
        raise ProviderError("network_error", f"{type(exc).__name__}: {exc}", details={"url": str(exc.request.url)}) from exc


def open_client() -> MethodClient:
    try:
        return MethodClient()
    except ValueError as exc:
        raise ProviderError("invalid_cache_config", str(exc)) from exc


@dataclass(frozen=True, slots=True)
class SearchOutcome:
    normalized_query: str
    results: list[dict[str, Any]]
    total_count: int
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
    candidate = f"{name.lower()} {slug.replace('-', ' ')}"
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
        provider_command=PROVIDER_NAME,
    )
    candidate_row["metadata"]["content_family"] = content_family
    return candidate_row


def search_results(client: MethodClient, query: str, *, limit: int) -> SearchOutcome:
    """Rank the sitemap's supported guide slugs against ``query``."""
    normalized_query = normalize_query(query, strip_terms=QUERY_NOISE_TERMS)
    terms = set(tokenize_query(normalized_query))
    scope_hint = _unsupported_scope_hint(terms)
    if scope_hint is not None:
        return SearchOutcome(normalized_query, [], 0, scope_hint)
    matches = [
        candidate
        for candidate in (_scored_candidate(row, normalized_query, terms) for row in client.sitemap_guides())
        if candidate is not None
    ]
    sort_article_candidates(matches)
    return SearchOutcome(normalized_query, matches[:limit], len(matches))


def _search_outcome(query: str, *, limit: int) -> SearchOutcome:
    with open_client() as client, transport_errors():
        return search_results(client, query, limit=limit)


def _reject_unsupported_surface(payload: dict[str, Any]) -> None:
    if payload["guide"].get("supported_surface") is not False:
        return
    raise ProviderError(
        "unsupported_guide_surface",
        unsupported_guide_surface_message(
            provider_name="Method",
            slug=payload["guide"]["slug"],
            content_family=payload["guide"].get("content_family"),
        ),
    )


def _require_article_content(payload: dict[str, Any]) -> dict[str, Any]:
    """Reject a page whose article container did not parse.

    When Method changes its guide template the article selectors stop matching and the parser
    produces an article with no body and no sections; returning that as a success would look like an
    empty guide instead of a broken parser.
    """
    article = payload["article"]
    if article["sections"] or article["text"]:
        return payload
    page_url = payload["guide"]["page_url"]
    raise ProviderError(
        "parse_failed",
        f"No article content parsed from {page_url}; the Method page layout has probably changed.",
        details={"page_url": page_url},
    )


def _fetch_guide_page(client: MethodClient, guide_ref: str) -> dict[str, Any]:
    try:
        guide_ref_parts(guide_ref)
    except ValueError as exc:
        raise ProviderError("invalid_guide_ref", str(exc)) from exc
    # Anything that fails past this point is a page problem, not a bad argument.
    try:
        payload = client.fetch_guide_page(guide_ref)
    except ValueError as exc:
        raise ProviderError("parse_failed", f"Could not parse the Method guide page for {guide_ref}: {exc}") from exc
    _reject_unsupported_surface(payload)
    return _require_article_content(payload)


def _preview_block(rows: list[dict[str, Any]], fetch_more_command: str) -> dict[str, Any]:
    return {
        "count": len(rows),
        "items": rows[:PREVIEW_LIMIT],
        "more_available": len(rows) > PREVIEW_LIMIT,
        "fetch_more_command": fetch_more_command,
    }


def _guide_summary_payload(page_payload: dict[str, Any]) -> dict[str, Any]:
    guide = dict(page_payload["guide"])
    article = dict(page_payload["article"])
    navigation = list(page_payload["navigation"])
    fetch_more_command = f"method guide-full {guide['slug']}"
    return {
        "guide": guide,
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
        "linked_entities": _preview_block(list(page_payload["linked_entities"]), fetch_more_command),
        "build_references": _preview_block(list(page_payload.get("build_references") or []), fetch_more_command),
        "analysis_surfaces": _preview_block(list(page_payload.get("analysis_surfaces") or []), fetch_more_command),
        "citations": {
            "page": guide["page_url"],
        },
    }


def _with_analysis_surfaces(page_payload: dict[str, Any]) -> dict[str, Any]:
    page_payload["analysis_surfaces"] = extract_guide_analysis_surfaces(page_payload, provider=PROVIDER_NAME)
    return page_payload


def _fetch_navigation_page(client: MethodClient, page_url: str) -> dict[str, Any]:
    with transport_errors():
        page_payload = client.fetch_guide_page(page_url)
    return _with_analysis_surfaces(_require_article_content(page_payload))


def _failed_page_row(item: dict[str, Any], *, code: str, message: str) -> dict[str, Any]:
    return {"url": item["url"], "section_slug": item.get("section_slug"), "error": {"code": code, "message": message}}


def _fetch_navigation_pages(
    client: MethodClient,
    initial: dict[str, Any],
    nav_items: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Fetch every navigation page, keeping the pages that parsed and recording the ones that did not.

    One unreachable or unparsable sibling page must not cost the caller the whole bundle, so each
    failure becomes a row in the returned list and the bundle reports it.
    """
    initial_url = initial["guide"]["page_url"]
    pages: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in nav_items:
        page_url = item["url"]
        if page_url in seen:
            continue
        seen.add(page_url)
        if page_url == initial_url:
            pages.append(_with_analysis_surfaces(initial))
            continue
        try:
            pages.append(_fetch_navigation_page(client, page_url))
        except ProviderError as exc:
            failures.append(_failed_page_row(item, code=exc.code, message=exc.message))
        except ValueError as exc:
            failures.append(_failed_page_row(item, code="parse_failed", message=str(exc)))
    if initial_url not in seen:
        pages.insert(0, _with_analysis_surfaces(initial))
    return pages, failures


def _guide_pages_payload(client: MethodClient, guide_ref: str) -> dict[str, Any]:
    initial = _fetch_guide_page(client, guide_ref)
    nav_items = initial["navigation"] or [
        {
            "title": initial["guide"]["section_title"],
            "url": initial["guide"]["page_url"],
            "section_slug": initial["guide"]["section_slug"],
            "active": True,
            "ordinal": 1,
        }
    ]
    pages, failed_pages = _fetch_navigation_pages(client, initial, nav_items)
    guide = dict(initial["guide"])
    guide["page_count"] = len(pages)
    linked_entities = merge_article_linked_entities(pages)
    build_references = merge_article_build_references(pages)
    analysis_surfaces = merge_guide_analysis_surfaces(pages)
    return {
        "guide": guide,
        "page": dict(initial["page"]),
        "navigation": {
            "count": len(nav_items),
            "items": nav_items,
        },
        "pages": [
            {
                "guide": page["guide"],
                "page": page["page"],
                "article": page["article"],
                "build_references": page.get("build_references") or [],
                "analysis_surfaces": page.get("analysis_surfaces") or [],
            }
            for page in pages
        ],
        "linked_entities": {
            "count": len(linked_entities),
            "items": linked_entities,
        },
        "build_references": {
            "count": len(build_references),
            "items": build_references,
        },
        "analysis_surfaces": {
            "count": len(analysis_surfaces),
            "items": analysis_surfaces,
        },
        # Navigation pages that could not be fetched or parsed; their content is missing from every
        # merged block above.
        "failed_pages": {
            "count": len(failed_pages),
            "items": failed_pages,
        },
        "citations": {
            "page": guide["page_url"],
            "pages": [page["guide"]["page_url"] for page in pages],
        },
    }


def guide(guide_ref: str) -> Envelope:
    """One Method guide page plus a preview of its linked entities, build references, and analysis surfaces."""
    with open_client() as client, transport_errors():
        payload = _guide_summary_payload(_with_analysis_surfaces(_fetch_guide_page(client, guide_ref)))
    return _envelope(command="guide", kind="guide", payload=payload, query=guide_ref, provenance=payload["citations"])


def guide_full(guide_ref: str) -> Envelope:
    """Every navigation page of a Method guide with merged linked entities, build references, and analysis surfaces."""
    with open_client() as client, transport_errors():
        payload = _guide_pages_payload(client, guide_ref)
    return _envelope(command="guide-full", kind="guide_full", payload=payload, query=guide_ref, provenance=payload["citations"])


def guide_export(guide_ref: str, *, out: Path | None = None) -> Envelope:
    """Write every page of a Method guide to a local bundle directory and return its manifest."""
    try:
        slug, _section_slug = guide_ref_parts(guide_ref)
    except ValueError as exc:
        raise ProviderError("invalid_guide_ref", str(exc)) from exc
    export_dir = out.expanduser() if out is not None else default_article_export_dir(PROVIDER_NAME, slug)
    with open_client() as client, transport_errors():
        pages_payload = _guide_pages_payload(client, guide_ref)
    manifest = write_article_bundle(pages_payload, provider=PROVIDER_NAME, export_dir=export_dir)
    payload = {
        "guide": pages_payload["guide"],
        "counts": manifest["counts"],
        "output_dir": str(export_dir),
        "manifest": manifest,
        "failed_pages": pages_payload["failed_pages"],
    }
    return _envelope(
        command="guide-export",
        kind="guide_export",
        payload=payload,
        query=guide_ref,
        provenance=pages_payload["citations"],
    )


def guide_query(
    bundle_ref: str,
    query: str,
    *,
    limit: int = 5,
    kinds: set[str] | None = None,
    section_title: str | None = None,
) -> Envelope:
    """Search an exported Method bundle on disk; no network access."""
    export_dir = Path(bundle_ref).expanduser()
    if not export_dir.exists():
        raise ProviderError("invalid_bundle", f"Bundle directory not found: {export_dir}")
    selected_kinds = set(kinds) if kinds else set(GUIDE_QUERY_KINDS)
    invalid = sorted(selected_kinds - GUIDE_QUERY_KINDS)
    if invalid:
        raise ProviderError("invalid_kind", f"Unsupported guide-query kinds: {', '.join(invalid)}")
    section_title_filter = section_title.strip().lower() if section_title and section_title.strip() else None
    bundle = load_article_bundle(export_dir)
    payload = query_article_bundle(
        bundle,
        query=query,
        limit=limit,
        kinds=selected_kinds,
        section_title_filter=section_title_filter,
    )
    payload["guide"] = bundle["manifest"]["guide"]
    payload["output_dir"] = str(export_dir)
    return _envelope(command="guide-query", kind="guide_query", payload=payload, query=query)


def _is_confident_match(results: list[dict[str, Any]]) -> bool:
    if not results:
        return False
    top_score = int(results[0]["ranking"]["score"])
    second_score = int(results[1]["ranking"]["score"]) if len(results) > 1 else 0
    return top_score >= 50 or top_score >= second_score + 15


class MethodProvider:
    """Sitemap-backed discovery over the supported Method.gg guide families."""

    name = PROVIDER_NAME

    def search(self, query: str, *, limit: int = 5, **options: Any) -> Envelope:
        outcome = _search_outcome(query, limit=limit)
        payload = build_article_search_response(
            query=query,
            search_query=outcome.normalized_query,
            results=outcome.results,
            total_count=outcome.total_count,
            scope_hint=outcome.scope_hint,
        )
        return _envelope(command="search", kind="search_results", payload=payload, query=query, provenance=SITEMAP_PROVENANCE)

    def resolve(self, target: str, **options: Any) -> Envelope:
        limit = int(options.get("limit", 5))
        outcome = _search_outcome(target, limit=limit)
        payload = build_article_resolve_response(
            provider_command=PROVIDER_NAME,
            query=target,
            search_query=outcome.normalized_query,
            results=outcome.results,
            total_count=outcome.total_count,
            resolved=_is_confident_match(outcome.results),
            scope_hint=outcome.scope_hint,
        )
        return _envelope(command="resolve", kind="resolve_match", payload=payload, query=target, provenance=SITEMAP_PROVENANCE)

    def doctor(self, **options: Any) -> Envelope:
        try:
            settings, sitemap_ttl, page_ttl = load_method_cache_settings_from_env()
        except ValueError as exc:
            raise ProviderError("invalid_cache_config", str(exc)) from exc
        payload = {
            "provider": PROVIDER_NAME,
            "status": "ready",
            "command": "doctor",
            "installed": True,
            "language": "python",
            "capabilities": {
                "search": "ready",
                "resolve": "ready",
                "guide": "ready",
                "guide_full": "ready",
                "guide_export": "ready",
                "guide_query": "ready",
            },
            "supported_scope": SUPPORTED_SCOPE,
            "cache": {
                "enabled": settings.enabled,
                "backend": settings.backend,
                "cache_dir": str(settings.cache_dir),
                "redis_url": settings.redis_url,
                "prefix": settings.prefix,
                "ttls": {
                    "sitemap": sitemap_ttl,
                    "page_html": page_ttl,
                },
            },
        }
        return _envelope(command="doctor", kind="doctor", payload=payload)


PROVIDER: ProviderSurface = MethodProvider()

"""Pure Icy Veins provider surface.

Every function here returns an ``Envelope`` or raises ``ProviderError``; nothing prints and nothing
raises ``typer.Exit``, so the ``warcraft`` wrapper can call ``PROVIDER`` in-process.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

import httpx
from warcraft_content.article_bundle import (
    default_article_export_dir,
    load_article_bundle,
    query_article_bundle,
    write_article_bundle,
)
from warcraft_content.article_discovery import merge_article_build_references, merge_article_linked_entities
from warcraft_content.article_provider_cli import build_article_resolve_response, build_article_search_response
from warcraft_content.guide_analysis import extract_guide_analysis_surfaces, merge_guide_analysis_surfaces
from warcraft_core.envelope import ENVELOPE_KEYS, Envelope, success_envelope, with_legacy_keys
from warcraft_core.provider import ProviderError, ProviderSurface

from icy_veins_cli.client import IcyVeinsClient, guide_ref_parts, load_icy_veins_cache_settings_from_env
from icy_veins_cli.page_parser import classify_guide_slug, guide_traversal_scope
from icy_veins_cli.search import PROVIDER_NAME, resolve_is_confident, search_results

BUNDLE_QUERY_KINDS = ("sections", "navigation", "linked_entities", "build_references", "analysis_surfaces")
_PREVIEW_LIMIT = 10


@contextmanager
def transport_errors(*, missing_message: str | None = None) -> Iterator[None]:
    """Translate httpx transport failures into ``ProviderError`` so callers get an envelope, never a traceback."""
    try:
        yield
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        details = {"status_code": status, "url": str(exc.request.url)}
        if status in (401, 403):
            raise ProviderError("auth_failed", str(exc), details=details) from exc
        if status == 404:
            raise ProviderError("not_found", missing_message or str(exc), details=details) from exc
        raise ProviderError("upstream_error", f"Icy Veins request failed with status {status}", details=details) from exc
    except httpx.TimeoutException as exc:
        raise ProviderError("timeout", f"{type(exc).__name__}: {exc}") from exc
    except httpx.RequestError as exc:
        raise ProviderError("network_error", f"{type(exc).__name__}: {exc}") from exc


def _envelope(command: str, kind: str, data: dict[str, Any], *, query: Any = None, provenance: dict[str, Any] | None = None) -> Envelope:
    """Wrap an Icy Veins payload in the shared envelope, keeping the historical top-level keys as legacy copies."""
    envelope = success_envelope(
        provider=PROVIDER_NAME,
        command=command,
        kind=kind,
        data=data,
        query=query,
        provenance=provenance,
    )
    legacy = {key: value for key, value in data.items() if key not in ENVELOPE_KEYS}
    # with_legacy_keys returns a plain dict because the legacy copies live outside the TypedDict.
    return cast(Envelope, with_legacy_keys(envelope, legacy))


@contextmanager
def _client() -> Iterator[IcyVeinsClient]:
    try:
        client = IcyVeinsClient()
    except ValueError as exc:
        raise ProviderError("invalid_cache_config", str(exc)) from exc
    with client:
        yield client


def _require_article_content(page_payload: dict[str, Any]) -> dict[str, Any]:
    """Reject a page whose article container did not parse.

    Icy Veins rebuilds its guide layout periodically. When the article selectors stop matching, the
    parser produces an article with no body and no sections; returning that as a success would look
    like an empty guide instead of a broken parser.
    """
    article = page_payload["article"]
    if article["sections"] or article["text"]:
        return page_payload
    page_url = page_payload["guide"]["page_url"]
    raise ProviderError(
        "parse_failed",
        f"No article content parsed from {page_url}; the Icy Veins page layout has probably changed.",
        details={"page_url": page_url},
    )


def _supported_guide_ref(guide_ref: str) -> tuple[str, str]:
    try:
        slug = guide_ref_parts(guide_ref)
    except ValueError as exc:
        raise ProviderError("invalid_guide_ref", str(exc)) from exc
    content_family = classify_guide_slug(slug)
    if content_family is None:
        raise ProviderError("invalid_guide_ref", f"Unsupported Icy Veins guide reference: {guide_ref}")
    return slug, content_family


def doctor(**options: Any) -> Envelope:
    """Report installation state, capabilities, and the resolved HTTP cache configuration."""
    del options
    try:
        settings, sitemap_ttl, page_ttl = load_icy_veins_cache_settings_from_env()
    except ValueError as exc:
        raise ProviderError("invalid_cache_config", str(exc)) from exc
    return _envelope(
        "doctor",
        "doctor",
        {
            "status": "ready",
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
            "cache": {
                "enabled": settings.enabled,
                "backend": settings.backend,
                "cache_dir": str(settings.cache_dir),
                "redis_url": settings.redis_url,
                "prefix": settings.prefix,
                "ttls": {"sitemap": sitemap_ttl, "page_html": page_ttl},
            },
        },
    )


def search(query: str, *, limit: int = 5, **options: Any) -> Envelope:
    """Rank Icy Veins WoW guides from the sitemap against a free-text query."""
    del options
    with _client() as client, transport_errors():
        normalized_query, results, total_count, scope_hint = search_results(client, query, limit=limit)
    data = build_article_search_response(
        query=query,
        search_query=normalized_query,
        results=results,
        total_count=total_count,
        scope_hint=scope_hint,
    )
    return _envelope("search", "search_results", data, query=query)


def resolve(target: str, *, limit: int = 5, **options: Any) -> Envelope:
    """Resolve a free-text query to the single best Icy Veins guide, with the candidate list attached."""
    del options
    with _client() as client, transport_errors():
        normalized_query, results, total_count, scope_hint = search_results(client, target, limit=limit)
    data = resolve_payload(
        query=target,
        search_query=normalized_query,
        results=results,
        total_count=total_count,
        scope_hint=scope_hint,
    )
    return _envelope("resolve", "resolve_match", data, query=target)


def resolve_payload(
    *,
    query: str,
    search_query: str,
    results: list[dict[str, Any]],
    total_count: int,
    scope_hint: dict[str, Any] | None,
) -> dict[str, Any]:
    """Shape the resolve payload, deciding ``resolved`` with the Icy Veins confidence rule."""
    return build_article_resolve_response(
        provider_command=PROVIDER_NAME,
        query=query,
        search_query=search_query,
        results=results,
        total_count=total_count,
        resolved=resolve_is_confident(results[0] if results else None, results[1] if len(results) > 1 else None),
        scope_hint=scope_hint,
    )


def _preview_block(items: list[dict[str, Any]], *, fetch_more_command: str) -> dict[str, Any]:
    return {
        "count": len(items),
        "items": items[:_PREVIEW_LIMIT],
        "more_available": len(items) > _PREVIEW_LIMIT,
        "fetch_more_command": fetch_more_command,
    }


def _guide_summary(page_payload: dict[str, Any]) -> dict[str, Any]:
    guide = dict(page_payload["guide"])
    navigation = list(page_payload["navigation"])
    page_toc = list(page_payload["page_toc"])
    article = dict(page_payload["article"])
    fetch_more_command = f"icy-veins guide-full {guide['slug']}"
    return {
        "guide": guide,
        "page": dict(page_payload["page"]),
        "navigation": {"count": len(navigation), "items": navigation},
        "page_toc": {"count": len(page_toc), "items": page_toc},
        "article": {
            "intro_text": article.get("intro_text") or "",
            "text": article["text"],
            "headings": article["headings"],
            "section_count": len(article["sections"]),
            "section_preview": [
                {"title": section["title"], "level": section["level"], "ordinal": section["ordinal"]}
                for section in article["sections"][:5]
            ],
        },
        "linked_entities": _preview_block(list(page_payload["linked_entities"]), fetch_more_command=fetch_more_command),
        "build_references": _preview_block(list(page_payload.get("build_references") or []), fetch_more_command=fetch_more_command),
        "analysis_surfaces": _preview_block(list(page_payload.get("analysis_surfaces") or []), fetch_more_command=fetch_more_command),
        "citations": dict(page_payload.get("citations") or {}),
    }


def guide(guide_ref: str) -> Envelope:
    """Fetch and summarize a single Icy Veins guide page."""
    _supported_guide_ref(guide_ref)
    with _client() as client, transport_errors(missing_message=f"Guide not found: {guide_ref}"):
        page_payload = _require_article_content(client.fetch_guide_page(guide_ref))
    page_payload["analysis_surfaces"] = extract_guide_analysis_surfaces(page_payload, provider=PROVIDER_NAME)
    summary = _guide_summary(page_payload)
    return _envelope("guide", "guide", summary, query=guide_ref, provenance=summary["citations"])


def _traversal_navigation(initial: dict[str, Any]) -> list[dict[str, Any]]:
    traversal_scope = guide_traversal_scope(initial["guide"].get("content_family"))
    nav_items = initial["navigation"] if traversal_scope == "family_navigation" else []
    return list(nav_items) or [
        {
            "title": initial["guide"]["section_title"],
            "url": initial["guide"]["page_url"],
            "section_slug": initial["guide"]["section_slug"],
            "active": True,
            "ordinal": 1,
        }
    ]


def _with_analysis_surfaces(page_payload: dict[str, Any]) -> dict[str, Any]:
    page_payload["analysis_surfaces"] = extract_guide_analysis_surfaces(page_payload, provider=PROVIDER_NAME)
    return page_payload


def _fetch_family_page(client: IcyVeinsClient, page_url: str) -> dict[str, Any]:
    with transport_errors(missing_message=f"Guide page not found: {page_url}"):
        page_payload = client.fetch_guide_page(page_url)
    return _with_analysis_surfaces(_require_article_content(page_payload))


def _failed_page_row(item: dict[str, Any], *, code: str, message: str) -> dict[str, Any]:
    return {"url": item["url"], "section_slug": item.get("section_slug"), "error": {"code": code, "message": message}}


def _fetch_family_pages(
    client: IcyVeinsClient,
    initial: dict[str, Any],
    nav_items: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Fetch every family page, keeping the pages that parsed and recording the ones that did not.

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
            pages.append(_fetch_family_page(client, page_url))
        except ProviderError as exc:
            failures.append(_failed_page_row(item, code=exc.code, message=exc.message))
        except ValueError as exc:
            failures.append(_failed_page_row(item, code="parse_failed", message=str(exc)))
    if initial_url not in seen:
        pages.insert(0, _with_analysis_surfaces(initial))
    return pages, failures


def _guide_bundle(client: IcyVeinsClient, guide_ref: str) -> dict[str, Any]:
    with transport_errors(missing_message=f"Guide not found: {guide_ref}"):
        initial = _require_article_content(client.fetch_guide_page(guide_ref))
    nav_items = _traversal_navigation(initial)
    pages, failed_pages = _fetch_family_pages(client, initial, nav_items)
    guide_row = dict(initial["guide"])
    guide_row["page_count"] = len(pages)
    linked_entities = merge_article_linked_entities(pages)
    build_references = merge_article_build_references(pages)
    analysis_surfaces = merge_guide_analysis_surfaces(pages)
    return {
        "guide": guide_row,
        "page": dict(initial["page"]),
        "navigation": {"count": len(nav_items), "items": nav_items},
        "pages": [
            {
                "guide": page["guide"],
                "page": page["page"],
                "page_toc": page["page_toc"],
                "article": page["article"],
                "build_references": page.get("build_references") or [],
                "analysis_surfaces": page.get("analysis_surfaces") or [],
            }
            for page in pages
        ],
        "linked_entities": {"count": len(linked_entities), "items": linked_entities},
        "build_references": {"count": len(build_references), "items": build_references},
        "analysis_surfaces": {"count": len(analysis_surfaces), "items": analysis_surfaces},
        # Family pages that could not be fetched or parsed; their content is missing from every
        # merged block above.
        "failed_pages": {"count": len(failed_pages), "items": failed_pages},
        "citations": {
            "page": guide_row["page_url"],
            "comments": (initial.get("citations") or {}).get("comments"),
            "pages": [page["guide"]["page_url"] for page in pages],
        },
    }


def guide_full(guide_ref: str) -> Envelope:
    """Fetch every page in the guide's family navigation and merge them into one payload."""
    _supported_guide_ref(guide_ref)
    with _client() as client:
        payload = _guide_bundle(client, guide_ref)
    return _envelope("guide-full", "guide_full", payload, query=guide_ref, provenance=payload["citations"])


def guide_export(guide_ref: str, *, out: Path | None = None) -> Envelope:
    """Write the full guide bundle (pages, entities, analysis surfaces) to a local export directory."""
    slug, _ = _supported_guide_ref(guide_ref)
    export_dir = out.expanduser() if out is not None else default_article_export_dir(PROVIDER_NAME, slug)
    with _client() as client:
        payload = _guide_bundle(client, guide_ref)
    manifest = write_article_bundle(payload, provider=PROVIDER_NAME, export_dir=export_dir)
    return _envelope(
        "guide-export",
        "guide_export",
        {
            "guide": payload["guide"],
            "output_dir": str(export_dir),
            "counts": manifest["counts"],
            "files": manifest["files"],
            "failed_pages": payload["failed_pages"],
        },
        query=guide_ref,
        provenance=payload["citations"],
    )


def guide_query(
    bundle: Path,
    query: str,
    *,
    limit: int = 5,
    kinds: list[str] | None = None,
    section_title: str | None = None,
) -> Envelope:
    """Search a previously exported guide bundle without touching the network."""
    selected_kinds = set(kinds or BUNDLE_QUERY_KINDS)
    invalid = sorted(selected_kinds - set(BUNDLE_QUERY_KINDS))
    if invalid:
        raise ProviderError("invalid_query_kind", f"Unsupported query kinds: {', '.join(invalid)}")
    export_dir = bundle.expanduser()
    if not export_dir.exists():
        # A path that is not there is a missing target (exit 4); a directory that holds no readable
        # manifest is a bad bundle, which ``load_article_bundle`` reports as invalid_bundle (exit 1).
        # The CLI rejects a file before this with a usage error, as `method guide-query` does.
        raise ProviderError("not_found", f"Bundle directory not found: {export_dir}")
    result = query_article_bundle(
        load_article_bundle(export_dir),
        query=query,
        limit=limit,
        kinds=selected_kinds,
        section_title_filter=section_title.lower() if section_title else None,
    )
    return _envelope("guide-query", "bundle_query", {"bundle": str(bundle), **result}, query=query)


class IcyVeinsProvider:
    """``ProviderSurface`` implementation backed by the pure functions in this module."""

    name = PROVIDER_NAME

    def search(self, query: str, *, limit: int = 5, **options: Any) -> Envelope:
        return search(query, limit=limit, **options)

    def resolve(self, target: str, **options: Any) -> Envelope:
        return resolve(target, **options)

    def doctor(self, **options: Any) -> Envelope:
        return doctor(**options)


PROVIDER: ProviderSurface = IcyVeinsProvider()

__all__ = [
    "PROVIDER",
    "IcyVeinsProvider",
    "doctor",
    "guide",
    "guide_export",
    "guide_full",
    "guide_query",
    "resolve",
    "resolve_payload",
    "search",
    "transport_errors",
]

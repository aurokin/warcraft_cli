"""Pure Icy Veins provider surface.

Every function here returns an ``Envelope`` or raises ``ProviderError``; nothing prints and nothing
raises ``typer.Exit``, so the ``warcraft`` wrapper can call ``PROVIDER`` in-process.
"""

from __future__ import annotations

import shlex
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any

from warcraft_api.cache import redacted_redis_url
from warcraft_content.article_bundle import (
    default_article_export_dir,
    load_article_bundle,
    query_article_bundle,
    write_article_bundle,
)
from warcraft_content.article_discovery import merge_article_build_references, merge_article_linked_entities
from warcraft_content.article_provider_cli import (
    article_doctor_payload,
    build_article_resolve_response,
    build_article_search_response,
    fetch_navigation_pages,
    guide_redirect,
    preview_block,
    require_article_content,
    transport_errors,
    with_analysis_surfaces,
)
from warcraft_content.guide_analysis import merge_guide_analysis_surfaces
from warcraft_core.envelope import Envelope, success_envelope
from warcraft_core.provider import ProviderError, ProviderSurface

from icy_veins_cli.client import ICY_VEINS_SITEMAP_URL, IcyVeinsClient, guide_ref_parts, load_icy_veins_cache_settings_from_env
from icy_veins_cli.page_parser import NAVIGATION_REQUIRED_FAMILIES, classify_guide_slug, guide_traversal_scope
from icy_veins_cli.search import PROVIDER_NAME, SearchOutcome, resolve_is_confident, search_results, sitemap_provenance

BUNDLE_QUERY_KINDS = ("sections", "navigation", "linked_entities", "build_references", "analysis_surfaces")
PROVIDER_LABEL = "Icy Veins"


def _envelope(command: str, kind: str, data: dict[str, Any], *, query: Any = None, provenance: dict[str, Any] | None = None) -> Envelope:
    return success_envelope(provider=PROVIDER_NAME, command=command, kind=kind, data=data, query=query, provenance=provenance)


@contextmanager
def _client() -> Iterator[IcyVeinsClient]:
    try:
        client = IcyVeinsClient()
    except ValueError as exc:
        raise ProviderError("invalid_cache_config", str(exc)) from exc
    with client:
        yield client


def _supported_guide_ref(guide_ref: str) -> tuple[str, str]:
    try:
        slug = guide_ref_parts(guide_ref)
    except ValueError as exc:
        raise ProviderError("invalid_guide_ref", str(exc)) from exc
    content_family = classify_guide_slug(slug)
    if content_family is None:
        raise ProviderError("invalid_guide_ref", f"Unsupported Icy Veins guide reference: {guide_ref}")
    return slug, content_family


def _require_query(query: str) -> None:
    if not query.strip():
        raise ProviderError("invalid_query", "Query cannot be empty.")


def doctor(**options: Any) -> Envelope:
    """Report installation state, capabilities, and the resolved HTTP cache configuration."""
    del options
    try:
        settings, sitemap_ttl, page_ttl = load_icy_veins_cache_settings_from_env()
    except ValueError as exc:
        raise ProviderError("invalid_cache_config", str(exc)) from exc
    data = article_doctor_payload(
        settings, redis_url=redacted_redis_url(settings.redis_url), sitemap_ttl=sitemap_ttl, page_ttl=page_ttl
    )
    return _envelope("doctor", "doctor", data)


def _search_outcome(query: str) -> SearchOutcome:
    _require_query(query)
    with _client() as client, transport_errors(PROVIDER_LABEL):
        return search_results(client, query, today=date.today())


def _sitemap_provenance(outcome: SearchOutcome) -> dict[str, Any]:
    return sitemap_provenance(
        ICY_VEINS_SITEMAP_URL, outcome.sitemap_newest_lastmod, today=date.today(), site_menu_warning=outcome.site_menu_warning
    )


def search(query: str, *, limit: int = 5, **options: Any) -> Envelope:
    """Rank Icy Veins WoW guides from the sitemap and the site-wide guide menu against a free-text query."""
    del options
    outcome = _search_outcome(query)
    data = build_article_search_response(
        query=query,
        search_query=outcome.normalized_query,
        results=outcome.matches[:limit],
        total_count=len(outcome.matches),
        scope_hint=outcome.scope_hint,
    )
    return _envelope("search", "search_results", data, query=query, provenance=_sitemap_provenance(outcome))


def resolve(target: str, *, limit: int = 5, **options: Any) -> Envelope:
    """Resolve a free-text query to the single best Icy Veins guide, with the candidate list attached.

    Confidence is judged on every ranked match: ``--limit`` only trims the candidates shown, so a
    near-tied rival the limit cuts off still keeps the answer unresolved.
    """
    del options
    outcome = _search_outcome(target)
    data = build_article_resolve_response(
        provider_command=PROVIDER_NAME,
        query=target,
        search_query=outcome.normalized_query,
        results=outcome.matches[:limit],
        total_count=len(outcome.matches),
        resolved=resolve_is_confident(outcome.matches),
        scope_hint=outcome.scope_hint,
    )
    return _envelope("resolve", "resolve_match", data, query=target, provenance=_sitemap_provenance(outcome))


def _guide_summary(page_payload: dict[str, Any]) -> dict[str, Any]:
    guide = dict(page_payload["guide"])
    navigation = list(page_payload["navigation"])
    page_toc = list(page_payload["page_toc"])
    article = dict(page_payload["article"])
    fetch_more_command = shlex.join(["icy-veins", "guide-full", guide["slug"]])
    return {
        "guide": guide,
        "redirect": page_payload["redirect"],
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
        "linked_entities": preview_block(list(page_payload["linked_entities"]), fetch_more_command=fetch_more_command),
        "build_references": preview_block(list(page_payload.get("build_references") or []), fetch_more_command=fetch_more_command),
        "analysis_surfaces": preview_block(list(page_payload.get("analysis_surfaces") or []), fetch_more_command=fetch_more_command),
        "citations": dict(page_payload.get("citations") or {}),
    }


def _fetch_requested_page(client: IcyVeinsClient, guide_ref: str) -> dict[str, Any]:
    """Fetch and parse the page the caller asked for; the ref is already validated, so a failure here is the page's."""
    with transport_errors(PROVIDER_LABEL, missing_message=f"Guide not found: {guide_ref}"):
        try:
            page_payload = client.fetch_guide_page(guide_ref)
        except ValueError as exc:
            raise ProviderError("parse_failed", f"Could not parse the Icy Veins guide page for {guide_ref}: {exc}") from exc
    page_payload = require_article_content(page_payload, provider_label=PROVIDER_LABEL)
    page_payload["redirect"] = guide_redirect(
        provider_label=PROVIDER_LABEL, requested=guide_ref_parts(guide_ref), served=page_payload["guide"]["slug"]
    )
    return page_payload


def guide(guide_ref: str) -> Envelope:
    """Fetch and summarize a single Icy Veins guide page."""
    _supported_guide_ref(guide_ref)
    with _client() as client:
        page_payload = _fetch_requested_page(client, guide_ref)
    summary = _guide_summary(with_analysis_surfaces(page_payload, provider=PROVIDER_NAME))
    return _envelope("guide", "guide", summary, query=guide_ref, provenance=summary["citations"])


def _traversal_navigation(initial: dict[str, Any]) -> list[dict[str, Any]]:
    guide_row = initial["guide"]
    content_family = guide_row.get("content_family")
    if guide_traversal_scope(content_family) == "family_navigation" and initial["navigation"]:
        return list(initial["navigation"])
    if content_family in NAVIGATION_REQUIRED_FAMILIES:
        # Walking only this page would pass a one-page bundle off as the whole guide.
        raise ProviderError(
            "parse_failed",
            f"No page navigation parsed from {guide_row['page_url']}; the Icy Veins page switcher layout has probably changed.",
            details={"page_url": guide_row["page_url"]},
        )
    return [
        {
            "title": guide_row["section_title"],
            "url": guide_row["page_url"],
            "section_slug": guide_row["section_slug"],
            "active": True,
            "ordinal": 1,
        }
    ]


def _guide_bundle(client: IcyVeinsClient, guide_ref: str) -> dict[str, Any]:
    initial = _fetch_requested_page(client, guide_ref)
    nav_items = _traversal_navigation(initial)
    pages, failed_pages = fetch_navigation_pages(
        initial, nav_items, fetch_page=client.fetch_guide_page, provider=PROVIDER_NAME, provider_label=PROVIDER_LABEL
    )
    guide_row = dict(initial["guide"])
    guide_row["page_count"] = len(pages)
    linked_entities = merge_article_linked_entities(pages)
    build_references = merge_article_build_references(pages)
    analysis_surfaces = merge_guide_analysis_surfaces(pages)
    return {
        "guide": guide_row,
        "redirect": initial["redirect"],
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
            "redirect": payload["redirect"],
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
        raise ProviderError("invalid_argument", f"Unsupported query kinds: {', '.join(invalid)}")
    loaded = load_article_bundle(bundle.expanduser())
    result = query_article_bundle(
        loaded,
        query=query,
        limit=limit,
        kinds=selected_kinds,
        section_title_filter=section_title.lower() if section_title else None,
    )
    return _envelope(
        "guide-query",
        "guide_query",
        {"bundle": str(bundle), "guide": loaded["manifest"].get("guide"), **result},
        query=query,
    )


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
    "search",
]

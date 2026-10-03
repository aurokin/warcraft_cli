"""Pure Icy Veins provider surface.

Every function here returns an ``Envelope`` or raises ``ProviderError``; nothing prints and nothing
raises ``typer.Exit``, so the ``warcraft`` wrapper can call ``PROVIDER`` in-process.
"""

from __future__ import annotations

import shlex
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from warcraft_api.cache import redacted_redis_url
from warcraft_content.article_bundle import article_export_dir, bundle_query_payload
from warcraft_content.article_discovery import article_resolve_payload, article_search_payload
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
from warcraft_content.site_crawler import CrawlResult, PageLink, crawl
from warcraft_core.envelope import Envelope, success_envelope
from warcraft_core.exit_codes import error_code_for_http_status
from warcraft_core.provider import ProviderError, ProviderSurface

from icy_veins_cli.client import (
    ICY_VEINS_SITEMAP_URL,
    INDEX_REFRESH_RATE_LIMITER,
    SITE_MENU_SEED_URL,
    IcyVeinsClient,
    guide_ref_parts,
    load_icy_veins_cache_settings_from_env,
)
from icy_veins_cli.page_parser import (
    NAVIGATION_REQUIRED_FAMILIES,
    classify_guide_slug,
    guide_traversal_scope,
    parse_sitemap_slugs,
    read_index_page,
)
from icy_veins_cli.search import PROVIDER_NAME, SearchOutcome, resolve_is_confident, search_results, sitemap_provenance
from icy_veins_cli.site_index import load_site_index, merge_crawl, save_site_index

BUNDLE_QUERY_KINDS = ("sections", "navigation", "linked_entities", "build_references", "analysis_surfaces")
PROVIDER_LABEL = "Icy Veins"
# Spent on the pages a run has never seen first, then on revisits of the previous index. A full run
# costs one request per indexed page plus one per new page (243 over the bundled snapshot), so a run
# over a grown index ends partial and the next run starts with the revisits it deferred.
DEFAULT_INDEX_MAX_REQUESTS = 250


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
    data["capabilities"]["index_refresh"] = "ready"
    site_index = load_site_index()
    data["site_index"] = (
        {"path": site_index.path, "bundled": site_index.bundled, "refreshed_at": site_index.refreshed_at, "pages": len(site_index.pages)}
        if site_index is not None
        else None
    )
    return _envelope("doctor", "doctor", data)


def _search_outcome(query: str) -> SearchOutcome:
    _require_query(query)
    with _client() as client, transport_errors(PROVIDER_LABEL):
        return search_results(client, query, today=date.today())


def _sitemap_provenance(outcome: SearchOutcome) -> dict[str, Any]:
    return sitemap_provenance(
        ICY_VEINS_SITEMAP_URL,
        outcome.sitemap_newest_lastmod,
        today=date.today(),
        site_menu_warning=outcome.site_menu_warning,
        site_index=outcome.site_index,
        index_gap=outcome.index_gap,
    )


def search(query: str, *, limit: int = 5, **options: Any) -> Envelope:
    """Rank Icy Veins WoW guides from the sitemap, the site-wide guide menu and the site index against a free-text query."""
    del options
    outcome = _search_outcome(query)
    data = article_search_payload(
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
    data = article_resolve_payload(
        provider_command=PROVIDER_NAME,
        query=target,
        search_query=outcome.normalized_query,
        matches=outcome.matches,
        limit=limit,
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
    payload = guide_bundle_payload(
        initial,
        _traversal_navigation(initial),
        fetch_page=client.fetch_guide_page,
        provider=PROVIDER_NAME,
        provider_label=PROVIDER_LABEL,
        extra_page_keys=("page_toc",),
    )
    payload["citations"]["comments"] = (initial.get("citations") or {}).get("comments")
    return payload


def guide_full(guide_ref: str) -> Envelope:
    """Fetch every page in the guide's family navigation and merge them into one payload."""
    _supported_guide_ref(guide_ref)
    with _client() as client:
        payload = _guide_bundle(client, guide_ref)
    return _envelope("guide-full", "guide_full", payload, query=guide_ref, provenance=payload["citations"])


def guide_export(guide_ref: str, *, out: Path | None = None) -> Envelope:
    """Write the full guide bundle (pages, entities, analysis surfaces) to a local export directory."""
    slug, _ = _supported_guide_ref(guide_ref)
    export_dir = article_export_dir(out, provider=PROVIDER_NAME, ref_slug=slug)
    with _client() as client:
        bundle = _guide_bundle(client, guide_ref)
    data = guide_export_payload(bundle, provider=PROVIDER_NAME, export_dir=export_dir)
    return _envelope("guide-export", "guide_export", data, query=guide_ref, provenance=bundle["citations"])


def guide_query(
    bundle: Path,
    query: str,
    *,
    limit: int = 5,
    kinds: list[str] | None = None,
    section_title: str | None = None,
) -> Envelope:
    """Search a previously exported guide bundle without touching the network."""
    data = bundle_query_payload(
        bundle, query, limit=limit, kinds=kinds, allowed_kinds=BUNDLE_QUERY_KINDS, section_title=section_title
    )
    return _envelope("guide-query", "guide_query", data, query=query)


def _seed_failure(result: CrawlResult) -> ProviderError:
    """Why the crawl could not read its first page (the site-menu seed), as the error ``index-refresh`` exits with."""
    status = result.errors[0]["status"] if result.errors else 404
    code = "parse_failed" if status == 200 else "network_error" if status == 0 else error_code_for_http_status(status)
    message = (
        f"The Icy Veins page {SITE_MENU_SEED_URL} listed no guide links; its layout has probably changed."
        if code == "parse_failed"
        else f"Could not read the Icy Veins page {SITE_MENU_SEED_URL} the index crawl starts from."
    )
    return ProviderError(code, message, details={"page_url": SITE_MENU_SEED_URL, "errors": result.errors})


def index_refresh(*, max_requests: int = DEFAULT_INDEX_MAX_REQUESTS) -> Envelope:
    """Crawl Icy Veins for the pages its frozen sitemap lacks and merge them into the local site index.

    The crawl fetches the site menu's pages and follows every link to a page the sitemap does not
    list, skipping the pages the previous index already holds, then re-reads those indexed pages,
    least recently seen first, at one request a second at most. It stops at the first 403, 429 or
    Cloudflare challenge (``blocked``), after a few server or transport failures in a row
    (``unavailable``) and at ``max_requests``; in every case what it read is merged (the previous
    rows are all kept) and ``partial`` is true. A blocked or unavailable run keeps the previous
    ``refreshed_at``, and a run that read nothing writes nothing. A seed page that lists no links
    fails as ``parse_failed`` and leaves the index untouched. Every page read also lands in the page
    cache.
    """
    previous = load_site_index()
    with _client() as client:
        with transport_errors(PROVIDER_LABEL):
            sitemap_slugs = parse_sitemap_slugs(client.sitemap_text())

        def should_expand(link: PageLink) -> bool:
            return link.source == "menu" or guide_ref_parts(link.url) not in sitemap_slugs

        indexed = [row for row in (previous.pages.values() if previous else ()) if row["status"] == "ok"]
        revisit = sorted(indexed, key=lambda row: row["last_seen"])
        result = crawl(
            [SITE_MENU_SEED_URL, *(previous.frontier if previous else ())],
            fetch=client.crawl_fetch,
            read_page=read_index_page,
            should_expand=should_expand,
            max_requests=max_requests,
            revisit=[row["url"] for row in revisit],
        )
    if result.stop_reason == "seed_failed":
        raise _seed_failure(result)
    merged, counts = merge_crawl(previous, result, now=datetime.now(UTC))
    # A run stopped before it read anything leaves the previous index (local or bundled) as it was.
    read_anything = bool(result.pages or result.aliases or result.not_found)
    index_path = str(save_site_index(merged)) if read_anything else previous.path if previous else None
    data = {
        "index_path": index_path,
        "partial": result.partial,
        "stop_reason": result.stop_reason,
        "counts": {
            "fetched": result.requests,
            "cached": result.cached,
            "pages": len(result.pages),
            "new": len(counts.new),
            "aliases": len(result.aliases),
            "dropped": len(counts.dropped),
            "errors": len(result.errors),
            "frontier": len(result.frontier),
            "total": len(merged.pages),
        },
        "new_pages": counts.new,
        "blocked": result.blocked,
        "errors": result.errors,
        "previous_index": {"path": previous.path, "bundled": previous.bundled, "refreshed_at": previous.refreshed_at} if previous else None,
    }
    provenance = {
        "seed_url": SITE_MENU_SEED_URL,
        "sitemap_url": ICY_VEINS_SITEMAP_URL,
        "min_interval_seconds": INDEX_REFRESH_RATE_LIMITER.min_interval_seconds,
    }
    return _envelope("index-refresh", "index_refresh", data, query={"max_requests": max_requests}, provenance=provenance)


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
    "index_refresh",
    "resolve",
    "search",
]

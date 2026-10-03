"""Response builders and page helpers shared by the article providers' pure surfaces (Icy Veins, Method)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Protocol

import httpx
from warcraft_core.exit_codes import error_code_for_http_status
from warcraft_core.provider import ProviderError

from warcraft_content.article_discovery import article_resolve_payload, article_search_payload
from warcraft_content.guide_analysis import extract_guide_analysis_surfaces

PREVIEW_LIMIT = 10


def build_article_search_response(
    *,
    query: str,
    search_query: str,
    results: list[dict[str, Any]],
    total_count: int,
    scope_hint: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = article_search_payload(
        query=query,
        search_query=search_query,
        results=results,
        total_count=total_count,
    )
    if scope_hint is not None:
        payload["scope_hint"] = scope_hint
    return payload


def build_article_resolve_response(
    *,
    provider_command: str,
    query: str,
    search_query: str,
    results: list[dict[str, Any]],
    total_count: int,
    resolved: bool,
    scope_hint: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = article_resolve_payload(
        provider_command=provider_command,
        query=query,
        search_query=search_query,
        results=results,
        total_count=total_count,
        resolved=resolved,
    )
    if scope_hint is not None:
        payload["scope_hint"] = scope_hint
    return payload


def unsupported_guide_surface_message(*, provider_name: str, slug: str, content_family: str | None) -> str:
    return f"Unsupported {provider_name} guide surface for slug={slug!r} family={content_family!r}."


def guide_redirect(*, provider_label: str, requested: str, served: str) -> dict[str, str] | None:
    """The ``redirect`` block of a guide payload: set when the site served another guide than the one asked for.

    Sites retire a guide by redirecting its URL (Icy Veins sends ``mistweaver-monk-legion-remix-guide`` to
    the healing guide), and everything else in the payload then describes ``served``, so an agent that
    asked for ``requested`` has to be told rather than left to notice the slug changed.
    """
    if requested.casefold() == served.casefold():
        return None
    return {
        "requested": requested,
        "served": served,
        "message": (
            f"{provider_label} served {served} instead of {requested}, usually because {requested} was retired; "
            f"everything in this payload describes {served}."
        ),
    }


@contextmanager
def transport_errors(provider_label: str, *, missing_message: str | None = None) -> Iterator[None]:
    """Translate httpx transport failures into ``ProviderError`` so callers get an envelope, never a traceback."""
    try:
        yield
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        details = {"status_code": status, "url": str(exc.request.url)}
        code = error_code_for_http_status(status)
        if code == "not_found":
            raise ProviderError(code, missing_message or str(exc), details=details) from exc
        raise ProviderError(code, f"{provider_label} request failed with status {status}", details=details) from exc
    except httpx.TimeoutException as exc:
        raise ProviderError("timeout", f"{type(exc).__name__}: {exc}") from exc
    except httpx.RequestError as exc:
        raise ProviderError("network_error", f"{type(exc).__name__}: {exc}") from exc


def preview_block(items: list[dict[str, Any]], *, fetch_more_command: str) -> dict[str, Any]:
    return {
        "count": len(items),
        "items": items[:PREVIEW_LIMIT],
        "more_available": len(items) > PREVIEW_LIMIT,
        "fetch_more_command": fetch_more_command,
    }


def require_article_content(page_payload: dict[str, Any], *, provider_label: str) -> dict[str, Any]:
    """Reject a page whose article container did not parse.

    The guide sites rebuild their layouts periodically. When the article selectors stop matching,
    the parser produces an article with no body and no sections; returning that as a success would
    look like an empty guide instead of a broken parser.
    """
    article = page_payload["article"]
    if article["sections"] or article["text"]:
        return page_payload
    page_url = page_payload["guide"]["page_url"]
    raise ProviderError(
        "parse_failed",
        f"No article content parsed from {page_url}; the {provider_label} page layout has probably changed.",
        details={"page_url": page_url},
    )


def with_analysis_surfaces(page_payload: dict[str, Any], *, provider: str) -> dict[str, Any]:
    page_payload["analysis_surfaces"] = extract_guide_analysis_surfaces(page_payload, provider=provider)
    return page_payload


def _failed_page_row(item: dict[str, Any], *, code: str, message: str) -> dict[str, Any]:
    return {"url": item["url"], "section_slug": item.get("section_slug"), "error": {"code": code, "message": message}}


def fetch_navigation_pages(
    initial: dict[str, Any],
    nav_items: list[dict[str, Any]],
    *,
    fetch_page: Callable[[str], dict[str, Any]],
    provider: str,
    provider_label: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Fetch every page the guide's navigation lists, keeping the pages that parsed and recording the ones that did not.

    One unreachable or unparsable sibling page must not cost the caller the whole bundle, so each
    failure becomes a row in the returned list and the bundle reports it. ``initial`` is the page
    already fetched; ``fetch_page`` fetches and parses one page URL.
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
            pages.append(with_analysis_surfaces(initial, provider=provider))
            continue
        try:
            with transport_errors(provider_label, missing_message=f"Guide page not found: {page_url}"):
                page_payload = fetch_page(page_url)
            pages.append(with_analysis_surfaces(require_article_content(page_payload, provider_label=provider_label), provider=provider))
        except ProviderError as exc:
            failures.append(_failed_page_row(item, code=exc.code, message=exc.message))
        except ValueError as exc:
            failures.append(_failed_page_row(item, code="parse_failed", message=str(exc)))
    if initial_url not in seen:
        pages.insert(0, with_analysis_surfaces(initial, provider=provider))
    return pages, failures


class CacheSettingsView(Protocol):
    """The parts of a provider's resolved cache settings that ``doctor`` reports."""

    @property
    def enabled(self) -> bool: ...

    @property
    def backend(self) -> str: ...

    @property
    def cache_dir(self) -> Path: ...

    @property
    def prefix(self) -> str: ...


def article_doctor_payload(
    settings: CacheSettingsView,
    *,
    redis_url: str | None,
    sitemap_ttl: int,
    page_ttl: int,
    supported_scope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """``doctor`` data for a sitemap-backed article provider; ``redis_url`` must already be redacted."""
    payload: dict[str, Any] = {
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
    }
    if supported_scope is not None:
        payload["supported_scope"] = supported_scope
    payload["cache"] = {
        "enabled": settings.enabled,
        "backend": settings.backend,
        "cache_dir": str(settings.cache_dir),
        "redis_url": redis_url,
        "prefix": settings.prefix,
        "ttls": {"sitemap": sitemap_ttl, "page_html": page_ttl},
    }
    return payload

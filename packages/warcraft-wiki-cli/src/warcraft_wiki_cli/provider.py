"""Pure Warcraft Wiki provider surface.

Nothing here prints or raises ``typer.Exit``: every function returns an envelope or raises
``ProviderError``. ``warcraft_wiki_cli.main`` is the thin Typer layer over these functions, and the
``warcraft`` wrapper can call ``PROVIDER`` in-process.
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
from warcraft_content.article_discovery import article_resolve_payload, article_search_payload
from warcraft_core.envelope import ENVELOPE_KEYS, Envelope, success_envelope, with_legacy_keys
from warcraft_core.provider import ProviderError, ProviderSurface

from warcraft_wiki_cli.client import WIKI_API_URL, WarcraftWikiAPIError, WarcraftWikiClient, load_warcraft_wiki_cache_settings_from_env
from warcraft_wiki_cli.page_parser import article_slug, normalize_article_ref
from warcraft_wiki_cli.search import PROVIDER_NAME, SearchOutcome, is_confident_match, search_results

API_REFERENCE_FAMILIES = frozenset({"api_function", "framework_page", "xml_schema", "cvar", "api_changes"})
EVENT_REFERENCE_FAMILIES = frozenset({"ui_handler", "framework_page"})
ARTICLE_QUERY_KINDS = frozenset({"sections", "navigation", "linked_entities"})
API_PROVENANCE = {"api_url": WIKI_API_URL, "source": "warcraft_wiki_mediawiki_api"}
CAPABILITIES = {
    "search": "ready",
    "resolve": "ready",
    "article": "ready",
    "article_full": "ready",
    "api": "ready",
    "api_full": "ready",
    "event": "ready",
    "event_full": "ready",
    "article_export": "ready",
    "article_query": "ready",
}
# MediaWiki error codes that mean "the page does not exist" rather than "the request failed".
_API_ERROR_CODES = {"missingtitle": "not_found", "invalidtitle": "not_found"}


def _envelope(
    *,
    command: str,
    kind: str,
    payload: dict[str, Any],
    query: Any = None,
    provenance: dict[str, Any] | None = None,
) -> Envelope:
    """Wrap a wiki payload in the shared envelope, keeping the historical top-level keys as legacy copies."""
    envelope = success_envelope(
        provider=PROVIDER_NAME,
        command=command,
        kind=kind,
        data=payload,
        query=query,
        provenance=provenance,
    )
    legacy = {key: value for key, value in payload.items() if key not in ENVELOPE_KEYS}
    # with_legacy_keys returns a plain dict because the legacy copies are outside the TypedDict.
    return cast(Envelope, with_legacy_keys(envelope, legacy))


@contextmanager
def transport_errors() -> Iterator[None]:
    """Translate wiki API and httpx transport failures into ProviderError so no command leaks a traceback."""
    try:
        yield
    except WarcraftWikiAPIError as exc:
        raise ProviderError(_API_ERROR_CODES.get(exc.code, exc.code), exc.message) from exc
    except httpx.TimeoutException as exc:
        raise ProviderError("timeout", str(exc) or "Warcraft Wiki request timed out") from exc
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


def open_client() -> WarcraftWikiClient:
    try:
        return WarcraftWikiClient()
    except ValueError as exc:
        raise ProviderError("invalid_cache_config", str(exc)) from exc


def _search_outcome(query: str, *, limit: int) -> SearchOutcome:
    with open_client() as client, transport_errors():
        return search_results(client, query, limit=limit)


def _discovery_payload(payload: dict[str, Any], outcome: SearchOutcome) -> dict[str, Any]:
    """Record which family-hint terms the query rewriting dropped, so agents can see the rewrite."""
    if outcome.excluded_terms:
        payload["excluded_terms"] = outcome.excluded_terms
        payload["normalization_hint"] = "excluded_family_hint_terms"
    return payload


def _article_summary(page_payload: dict[str, Any]) -> dict[str, Any]:
    article = dict(page_payload["article"])
    page = dict(page_payload["page"])
    navigation = list((page_payload.get("navigation") or {}).get("items") or [])
    content = dict(page_payload["article_content"])
    reference = dict(page_payload.get("reference") or {})
    linked_entities = list(page_payload["linked_entities"])
    return {
        "article": article,
        "page": page,
        "reference": reference,
        "navigation": {
            "count": len(navigation),
            "items": navigation[:25],
        },
        "content": {
            "text": content["text"],
            "headings": content["headings"],
            "section_count": len(content["sections"]),
            "section_preview": [
                {
                    "title": section["title"],
                    "level": section["level"],
                    "ordinal": section["ordinal"],
                }
                for section in content["sections"][:10]
            ],
        },
        "linked_entities": {
            "count": len(linked_entities),
            "items": linked_entities[:10],
            "more_available": len(linked_entities) > 10,
            "fetch_more_command": f"warcraft-wiki article-full {article['title']!r}",
        },
        "citations": {
            "page": article["page_url"],
        },
    }


def _article_payload_from_initial(initial: dict[str, Any]) -> dict[str, Any]:
    article = dict(initial["article"])
    article["page_count"] = 1
    return {
        "article": article,
        "page": dict(initial["page"]),
        "reference": dict(initial.get("reference") or {}),
        "navigation": dict(initial["navigation"]),
        "pages": [
            {
                "article_meta": dict(initial["article"]),
                "page": dict(initial["page"]),
                "reference": dict(initial.get("reference") or {}),
                "article": dict(initial["article_content"]),
            }
        ],
        "linked_entities": {
            "count": len(initial["linked_entities"]),
            "items": list(initial["linked_entities"]),
        },
        "citations": dict(initial["citations"]),
    }


def _typed_search_queries(query: str, *, surface: str) -> list[str]:
    normalized = normalize_article_ref(query)
    candidates = [normalized]
    lowered = normalized.lower()
    if surface == "api":
        if not lowered.startswith("api "):
            candidates.append(f"API {normalized}")
    elif surface == "event":
        if not lowered.startswith("uihandler "):
            candidates.append(f"UIHANDLER {normalized}")
        if lowered != "events":
            candidates.append(f"event {normalized}")
    ordered: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        value = candidate.strip()
        if value and value.lower() not in seen:
            ordered.append(value)
            seen.add(value.lower())
    return ordered


def _typed_allowed_families(surface: str) -> frozenset[str]:
    return API_REFERENCE_FAMILIES if surface == "api" else EVENT_REFERENCE_FAMILIES


def _typed_direct_article_result(
    client: WarcraftWikiClient,
    *,
    direct_refs: list[str],
    allowed_families: frozenset[str],
) -> dict[str, Any] | None:
    for candidate_ref in direct_refs:
        try:
            initial = client.fetch_article_page(candidate_ref)
        except WarcraftWikiAPIError:
            continue
        if initial["article"]["content_family"] in allowed_families:
            return initial
    return None


def _typed_ranked_results(
    client: WarcraftWikiClient,
    *,
    query: str,
    surface: str,
    allowed_families: frozenset[str],
    limit: int,
) -> tuple[list[dict[str, Any]], list[str], int]:
    ranked: dict[str, dict[str, Any]] = {}
    query_trace: list[str] = []
    total_count = 0
    for search_query in _typed_search_queries(query, surface=surface):
        outcome = search_results(client, search_query, limit=limit)
        query_trace.append(outcome.normalized_query)
        total_count = max(total_count, outcome.total_count)
        for row in outcome.results:
            family = str((row.get("metadata") or {}).get("content_family") or "")
            if family not in allowed_families:
                continue
            existing = ranked.get(row["id"])
            if existing is None or int(row["ranking"]["score"]) > int(existing["ranking"]["score"]):
                ranked[row["id"]] = row
    results = sorted(ranked.values(), key=lambda row: (-int(row["ranking"]["score"]), row["name"], row["id"]))
    return results[:limit], query_trace, total_count


def _typed_search_match(results: list[dict[str, Any]], *, query: str, surface: str) -> dict[str, Any]:
    if not results or not is_confident_match(results):
        raise WarcraftWikiAPIError(
            f"invalid_{surface}_ref",
            f"Unable to resolve {surface} reference from query: {query}",
        )
    return results[0]


def _typed_result_payload(
    initial: dict[str, Any],
    *,
    query: str,
    search_queries: list[str],
    surface: str,
    full: bool,
    resolved_from: str,
    resolution: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result = _article_payload_from_initial(initial) if full else _article_summary(initial)
    result["query"] = query
    result["search_queries"] = search_queries
    result["resolved_from"] = resolved_from
    result["resolved_surface"] = surface
    if resolution is not None:
        result["resolution"] = resolution
    return result


def _typed_article_payload(
    client: WarcraftWikiClient,
    query: str,
    *,
    surface: str,
    full: bool,
    limit: int = 10,
) -> dict[str, Any]:
    allowed_families = _typed_allowed_families(surface)
    direct_refs = _typed_search_queries(query, surface=surface)
    initial = _typed_direct_article_result(client, direct_refs=direct_refs, allowed_families=allowed_families)
    if initial is not None:
        return _typed_result_payload(
            initial,
            query=query,
            search_queries=direct_refs,
            surface=surface,
            full=full,
            resolved_from="direct_fetch",
        )

    results, query_trace, total_count = _typed_ranked_results(
        client,
        query=query,
        surface=surface,
        allowed_families=allowed_families,
        limit=limit,
    )
    top = _typed_search_match(results, query=query, surface=surface)
    initial = client.fetch_article_page(str(top["id"]))
    if initial["article"]["content_family"] not in allowed_families:
        raise WarcraftWikiAPIError(
            f"invalid_{surface}_ref",
            f"Resolved article is not a supported {surface} reference: {initial['article']['title']}",
        )
    return _typed_result_payload(
        initial,
        query=query,
        search_queries=query_trace,
        surface=surface,
        full=full,
        resolved_from="search",
        resolution={
            "resolved": True,
            "match": top,
            "candidates": results,
            "count": total_count,
        },
    )


def article(article_ref: str, *, full: bool = False) -> Envelope:
    """One wiki article: a summary with previews, or every extracted section when ``full``."""
    with open_client() as client, transport_errors():
        initial = client.fetch_article_page(article_ref)
    payload = _article_payload_from_initial(initial) if full else _article_summary(initial)
    command = "article-full" if full else "article"
    return _envelope(command=command, kind=command.replace("-", "_"), payload=payload, query=article_ref, provenance=payload["citations"])


def typed_reference(query: str, *, surface: str, full: bool = False) -> Envelope:
    """Resolve ``query`` to an API or event reference page and return it; ``surface`` is "api" or "event"."""
    with open_client() as client, transport_errors():
        payload = _typed_article_payload(client, query, surface=surface, full=full)
    command = f"{surface}-full" if full else surface
    return _envelope(command=command, kind=command.replace("-", "_"), payload=payload, query=query, provenance=payload["citations"])


def article_export(article_ref: str, *, out: Path | None = None) -> Envelope:
    """Write a wiki article bundle to disk and return the manifest."""
    article_title = normalize_article_ref(article_ref)
    default_dir = default_article_export_dir(PROVIDER_NAME, article_slug(article_title), prefix="article")
    export_dir = out.expanduser() if out is not None else default_dir
    with open_client() as client, transport_errors():
        initial = client.fetch_article_page(article_ref)
    page_payload = _article_payload_from_initial(initial)
    manifest = write_article_bundle(
        page_payload,
        provider=PROVIDER_NAME,
        export_dir=export_dir,
        resource_key="article",
        page_resource_key="article_meta",
    )
    payload = {
        "provider": PROVIDER_NAME,
        "article": page_payload["article"],
        "output_dir": str(export_dir),
        "counts": manifest["counts"],
        "files": manifest["files"],
    }
    return _envelope(
        command="article-export",
        kind="article_export",
        payload=payload,
        query=article_ref,
        provenance=page_payload["citations"],
    )


def article_query(
    bundle_ref: str | Path,
    query: str,
    *,
    limit: int = 5,
    kinds: set[str] | None = None,
    section_title: str | None = None,
) -> Envelope:
    """Search an exported wiki article bundle on disk."""
    export_dir = Path(bundle_ref).expanduser()
    if not export_dir.exists():
        raise ProviderError("invalid_bundle", f"Bundle directory not found: {export_dir}")
    selected_kinds = set(kinds) if kinds else set(ARTICLE_QUERY_KINDS)
    invalid = sorted(selected_kinds - ARTICLE_QUERY_KINDS)
    if invalid:
        raise ProviderError("invalid_query_kind", f"Unsupported query kinds: {', '.join(invalid)}")
    bundle_payload = load_article_bundle(export_dir)
    result = query_article_bundle(
        bundle_payload,
        query=query,
        limit=limit,
        kinds=selected_kinds,
        section_title_filter=section_title.lower() if section_title else None,
    )
    resource_key = str(bundle_payload["manifest"].get("resource_key") or "guide")
    payload = {
        "provider": PROVIDER_NAME,
        resource_key: bundle_payload["manifest"].get(resource_key),
        "bundle": str(bundle_ref),
        **result,
    }
    return _envelope(command="article-query", kind="article_query", payload=payload, query=query)


class WarcraftWikiProvider:
    """Warcraft Wiki provider: MediaWiki-backed article discovery and family-aware reference lookups."""

    name = PROVIDER_NAME

    def search(self, query: str, *, limit: int = 5, **options: Any) -> Envelope:
        outcome = _search_outcome(query, limit=limit)
        payload = _discovery_payload(
            article_search_payload(
                query=query,
                search_query=outcome.normalized_query,
                results=outcome.results,
                total_count=outcome.total_count,
            ),
            outcome,
        )
        return _envelope(command="search", kind="search_results", payload=payload, query=query, provenance=API_PROVENANCE)

    def resolve(self, target: str, *, limit: int = 5, **options: Any) -> Envelope:
        outcome = _search_outcome(target, limit=limit)
        payload = _discovery_payload(
            article_resolve_payload(
                provider_command=PROVIDER_NAME,
                query=target,
                search_query=outcome.normalized_query,
                results=outcome.results,
                total_count=outcome.total_count,
                resolved=is_confident_match(outcome.results),
            ),
            outcome,
        )
        return _envelope(command="resolve", kind="resolve_match", payload=payload, query=target, provenance=API_PROVENANCE)

    def doctor(self, **options: Any) -> Envelope:
        try:
            settings, search_ttl, page_ttl = load_warcraft_wiki_cache_settings_from_env()
        except ValueError as exc:
            raise ProviderError("invalid_cache_config", str(exc)) from exc
        payload = {
            "provider": PROVIDER_NAME,
            "status": "ready",
            "command": "doctor",
            "installed": True,
            "language": "python",
            "capabilities": dict(CAPABILITIES),
            "cache": {
                "enabled": settings.enabled,
                "backend": settings.backend,
                "cache_dir": str(settings.cache_dir),
                "redis_url": settings.redis_url,
                "prefix": settings.prefix,
                "ttls": {
                    "search": search_ttl,
                    "page_html": page_ttl,
                },
            },
        }
        return _envelope(command="doctor", kind="doctor", payload=payload, provenance=API_PROVENANCE)


PROVIDER: ProviderSurface = WarcraftWikiProvider()

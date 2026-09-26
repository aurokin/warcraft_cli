from __future__ import annotations

from typing import Any

from warcraft_content.article_discovery import article_resolve_payload, article_search_payload


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

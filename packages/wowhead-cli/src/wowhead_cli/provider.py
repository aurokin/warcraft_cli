"""Pure Wowhead provider surface.

Every function here returns an ``Envelope`` or raises ``ProviderError``; nothing prints and nothing
raises ``typer.Exit``, so the ``warcraft`` wrapper can call ``PROVIDER`` in-process. The Typer
commands in ``wowhead_cli.main`` are thin wrappers over these functions.
"""

from __future__ import annotations

import shlex
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, cast

import httpx
from warcraft_api.cache import CacheSettings, load_cache_settings_from_env
from warcraft_core.envelope import ENVELOPE_KEYS, Envelope, success_envelope, with_legacy_keys
from warcraft_core.provider import ProviderError

from wowhead_cli.doctor import build_doctor_payload
from wowhead_cli.expansion_profiles import (
    ExpansionProfile,
    detect_expansion_from_url,
    resolve_expansion,
)
from wowhead_cli.ranking import (
    command_prefix_for_expansion,
    normalize_resolve_entity_types,
    normalize_search_results,
    resolve_confidence,
    resolve_next_command,
    search_query_for_ranking,
    search_ranking_query,
)
from wowhead_cli.wowhead_client import WowheadClient, search_url

PROVIDER_NAME = "wowhead"


@dataclass(frozen=True, slots=True)
class ExpansionSelection:
    """Which Wowhead expansion profile a call runs against, and why it was chosen."""

    profile: ExpansionProfile
    source: str  # "flag" when the caller named it, "url" when auto-detected, otherwise "default"


def select_expansion(expansion: str | None = None, *, url_hint: str | None = None) -> ExpansionSelection:
    """Pick the expansion profile: an explicit key wins, otherwise auto-detect from a Wowhead URL."""
    if expansion:
        try:
            return ExpansionSelection(resolve_expansion(expansion), "flag")
        except ValueError as exc:
            raise ProviderError("invalid_argument", str(exc)) from exc
    if url_hint:
        detected = detect_expansion_from_url(url_hint)
        if detected is not None:
            return ExpansionSelection(detected, "url")
    return ExpansionSelection(resolve_expansion(None), "default")


@contextmanager
def transport_errors() -> Iterator[None]:
    """Translate httpx transport failures into ``ProviderError`` so callers get an envelope, never a traceback."""
    try:
        yield
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        details = {"status_code": status, "url": str(exc.request.url)}
        if status in (401, 403):
            raise ProviderError("auth_failed", f"Wowhead returned HTTP {status}", details=details) from exc
        if status == 404:
            raise ProviderError("not_found", f"Wowhead returned HTTP {status}", details=details) from exc
        raise ProviderError("http_error", f"Wowhead returned HTTP {status}", details=details) from exc
    except httpx.TimeoutException as exc:
        raise ProviderError("timeout", f"{type(exc).__name__}: {exc}") from exc
    except httpx.HTTPError as exc:
        raise ProviderError("network_error", str(exc)) from exc


def open_client(profile: ExpansionProfile) -> WowheadClient:
    """Build a Wowhead HTTP client, turning bad cache configuration into a ``ProviderError``."""
    try:
        return WowheadClient(expansion=profile)
    except ValueError as exc:
        raise ProviderError("invalid_cache_config", str(exc)) from exc


def cache_settings_payload(settings: CacheSettings) -> dict[str, Any]:
    """Render resolved cache settings as the JSON block every cache-aware command reports."""
    ttls = settings.ttls
    return {
        "enabled": settings.enabled,
        "backend": settings.backend,
        "cache_dir": str(settings.cache_dir),
        "redis_url": settings.redis_url,
        "prefix": settings.prefix,
        "ttls": {
            "search_suggestions": ttls.search_suggestions,
            "tooltip_meta": ttls.tooltip_meta,
            "entity_page_html": ttls.entity_page_html,
            "guide_page_html": ttls.guide_page_html,
            "page_html": ttls.page_html,
            "comment_replies": ttls.comment_replies,
            "entity_response": ttls.entity_response,
        },
    }


def envelope(command: str, kind: str, data: dict[str, Any], *, query: Any = None) -> Envelope:
    """Wrap a Wowhead payload in the shared envelope, keeping its historical top-level keys as legacy copies."""
    base = success_envelope(provider=PROVIDER_NAME, command=command, kind=kind, data=data, query=query)
    legacy = {key: value for key, value in data.items() if key not in ENVELOPE_KEYS}
    # with_legacy_keys returns a plain dict because the legacy copies live outside the TypedDict.
    return cast(Envelope, with_legacy_keys(base, legacy))


def _validated_query(raw: str) -> str:
    """Reject an empty or whitespace-only query locally instead of spending a request on it."""
    query = raw.strip()
    if not query:
        raise ProviderError("invalid_query", "Query cannot be empty.")
    return query


def _suggestion_results(client: WowheadClient, search_query: str) -> list[Any]:
    with transport_errors():
        try:
            response = client.search_suggestions(search_query)
        except ValueError as exc:
            raise ProviderError("parse_error", str(exc)) from exc
    results = response.get("results")
    if not isinstance(results, list):
        raise ProviderError("unexpected_response", "Missing or invalid 'results' payload from Wowhead.")
    return results


def search(query: str, *, limit: int = 10, expansion: str | None = None, **options: Any) -> Envelope:
    """Rank Wowhead search suggestions for a free-text query or Wowhead URL."""
    del options
    query = _validated_query(query)
    selection = select_expansion(expansion, url_hint=query)
    profile = selection.profile
    search_query = search_query_for_ranking(query)
    client = open_client(profile)
    results = _suggestion_results(client, search_query)
    normalized = normalize_search_results(results, query=query, expansion=profile)
    returned = normalized[:limit]
    data: dict[str, Any] = {
        "query": query,
        "search_query": search_query,
        "expansion": profile.key,
        "expansion_source": selection.source,
        "search_url": search_url(search_query, expansion=profile),
        "count": len(returned),
        "total_matches": len(normalized),
        "truncated": len(normalized) > len(returned),
        "results": returned,
    }
    return envelope("search", "search_results", data, query=query)


def resolve(
    target: str,
    *,
    limit: int = 5,
    entity_types: tuple[str, ...] | list[str] = (),
    expansion: str | None = None,
    **options: Any,
) -> Envelope:
    """Resolve a query to the single most likely Wowhead entity plus the follow-up command to run."""
    del options
    target = _validated_query(target)
    selection = select_expansion(expansion)
    profile = selection.profile
    try:
        selected_entity_types = normalize_resolve_entity_types(list(entity_types))
    except ValueError as exc:
        raise ProviderError("invalid_argument", str(exc)) from exc
    search_query = search_ranking_query(target)
    client = open_client(profile)
    results = _suggestion_results(client, search_query)
    candidates = normalize_search_results(results, query=target, expansion=profile, entity_types=selected_entity_types)
    confidence = resolve_confidence(candidates, entity_types=selected_entity_types)
    top_candidate = candidates[0] if candidates else None
    returned = candidates[:limit]
    next_command = resolve_next_command(top_candidate) if top_candidate is not None and confidence == "high" else None
    search_command = f"{command_prefix_for_expansion(profile)} search {shlex.quote(target)}"
    data: dict[str, Any] = {
        "query": target,
        "search_query": search_query,
        "expansion": profile.key,
        "search_url": search_url(search_query, expansion=profile),
        "filters": {"entity_types": list(selected_entity_types)},
        "resolved": next_command is not None,
        "confidence": confidence,
        "match": top_candidate,
        "next_command": next_command,
        "fallback_search_command": None if next_command is not None else search_command,
        "count": len(returned),
        "total_matches": len(candidates),
        "truncated": len(candidates) > len(returned),
        "candidates": returned,
    }
    return envelope("resolve", "resolve_match", data, query=target)


def doctor(*, live: bool = True, expansion: str | None = None, **options: Any) -> Envelope:
    """Report Wowhead endpoint reachability, parser shape checks, and cache/runtime readiness."""
    del options
    profile = select_expansion(expansion).profile
    try:
        settings = load_cache_settings_from_env()
    except ValueError as exc:
        raise ProviderError("invalid_cache_config", str(exc)) from exc
    data = build_doctor_payload(profile, live=live, cache=cache_settings_payload(settings))
    return envelope("doctor", "doctor", data)


class WowheadProvider:
    """``ProviderSurface`` implementation backed by the pure functions in this module."""

    name = PROVIDER_NAME

    search = staticmethod(search)
    resolve = staticmethod(resolve)
    doctor = staticmethod(doctor)


PROVIDER = WowheadProvider()

__all__ = [
    "PROVIDER",
    "ExpansionSelection",
    "WowheadProvider",
    "cache_settings_payload",
    "doctor",
    "envelope",
    "open_client",
    "resolve",
    "search",
    "select_expansion",
    "transport_errors",
]

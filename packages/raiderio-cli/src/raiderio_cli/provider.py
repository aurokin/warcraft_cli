"""Pure Raider.IO provider surface: search, resolve, and doctor without any CLI coupling.

``PROVIDER`` satisfies :class:`warcraft_core.provider.ProviderSurface`; the Typer commands in
``main`` and the ``warcraft`` wrapper both call it. Nothing here prints or raises ``typer.Exit``:
failures raise :class:`~warcraft_core.provider.ProviderError`.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, cast

import httpx
from warcraft_core.envelope import ENVELOPE_KEYS, Envelope, error_envelope, success_envelope, with_legacy_keys
from warcraft_core.provider import ProviderError, ProviderSurface
from warcraft_core.shapes import as_dict, as_list

from raiderio_cli.candidates import (
    candidate_ranking_score,
    dedupe_search_candidates,
    normalize_structured_query,
    probe_structured_candidates,
    resolve_candidate_is_confident,
    resolve_confidence_label,
    search_result_candidates,
    sorted_search_candidates,
)
from raiderio_cli.client import RaiderIOClient, load_raiderio_cache_settings_from_env

PROVIDER_NAME = "raiderio"
SEARCH_KINDS = ("all", "character", "guild")
# Kept in sync with the wrapper registry: tests/test_warcraft_wrapper.py compares these strings.
CAPABILITIES = {
    "search": "ready",
    "resolve": "ready",
    "character": "ready",
    "guild": "ready",
    "mythic_plus_runs": "ready",
    "sample_mythic_plus_runs": "ready",
    "sample_mythic_plus_players": "ready",
    "distribution_mythic_plus_runs": "ready",
    "distribution_mythic_plus_players": "ready",
    "threshold_mythic_plus_runs": "ready",
    "mythic_plus_leaderboard": "ready",
    "raid_leaderboard": "ready",
    "raid_catalog": "ready",
}


def raiderio_envelope(*, command: str, kind: str, payload: dict[str, Any]) -> Envelope:
    """Wrap a flat Raider.IO payload in the shared envelope.

    ``payload`` keys that are envelope keys (``query``, ``provider``, ``kind``) are consumed by the
    envelope; the rest land in ``data`` and are also copied to the top level, where agents have read
    them since 0.1.0. Those flat copies are deprecated: read ``data``.
    """
    data = {key: value for key, value in payload.items() if key not in ENVELOPE_KEYS}
    provenance = {key: payload[key] for key in ("freshness", "citations") if key in payload}
    envelope = success_envelope(
        provider=PROVIDER_NAME,
        command=command,
        kind=kind,
        data=data,
        query=payload.get("query"),
        provenance=provenance,
    )
    # with_legacy_keys returns a plain dict because the legacy keys are provider-specific.
    return cast(Envelope, with_legacy_keys(envelope, data))


def _upstream_message(exc: httpx.HTTPStatusError) -> str:
    """Prefer the upstream Raider.IO ``message`` when the error response carries one."""
    try:
        payload = exc.response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        detail = payload.get("message") or payload.get("error")
        if isinstance(detail, str) and detail.strip():
            return detail.strip()
    return f"Raider.IO request failed with HTTP {exc.response.status_code}."


@contextmanager
def transport_errors() -> Iterator[None]:
    """Translate httpx transport failures into ProviderError so no caller sees a traceback."""
    try:
        yield
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        message = _upstream_message(exc)
        code = {400: "invalid_query", 401: "auth_failed", 403: "auth_failed", 404: "not_found", 429: "rate_limited"}.get(
            status, "upstream_error"
        )
        # Raider.IO answers a missing character/guild with HTTP 400 "Could not find requested <x>"
        # and a malformed request with HTTP 400 "Invalid request query input". Only the latter is a
        # usage error, so the message is what separates exit 4 (not found) from exit 2.
        if status == 400 and message.lower().startswith("could not find"):
            code = "not_found"
        raise ProviderError(code, message, details={"status_code": status, "url": str(exc.request.url)}) from exc
    except httpx.TimeoutException as exc:
        raise ProviderError("timeout", f"Raider.IO request timed out: {exc}") from exc
    except httpx.RequestError as exc:
        raise ProviderError("network_error", f"Raider.IO request failed: {type(exc).__name__}: {exc}") from exc


def open_client() -> RaiderIOClient:
    """Build a cache-configured Raider.IO client, or fail with the config error."""
    try:
        return RaiderIOClient()
    except ValueError as exc:
        raise ProviderError("invalid_cache_config", str(exc)) from exc


def validated_kind(kind: str) -> str:
    """Return ``kind`` when it is a supported search scope."""
    if kind not in SEARCH_KINDS:
        raise ProviderError("invalid_query", "--kind must be one of: all, character, guild")
    return kind


def _search_results_payload(
    query: str,
    raw_matches: list[dict[str, Any]],
    *,
    type_hint: str | None,
    limit: int,
    extra_candidates: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    results = search_result_candidates(raw_matches, query=query, type_hint=type_hint)
    if extra_candidates:
        results.extend(extra_candidates)
    results = dedupe_search_candidates(results)
    top = sorted_search_candidates(results)[:limit]
    return {
        "provider": "raiderio",
        "query": query,
        "search_query": query,
        "count": len(results),
        "results": top,
        "truncated": len(results) > limit,
    }


def _resolve_payload(search_payload: dict[str, Any], *, limit: int) -> dict[str, Any]:
    top = as_list(search_payload.get("results"))[:limit]
    if not top:
        return {
            "provider": "raiderio",
            "query": search_payload.get("query"),
            "search_query": search_payload.get("search_query"),
            "resolved": False,
            "confidence": "none",
            "match": None,
            "next_command": None,
            "fallback_search_command": f'raiderio search "{search_payload.get("search_query")}"',
            "candidates": [],
        }
    best = top[0]
    follow_up = as_dict(best.get("follow_up"))
    best_score = candidate_ranking_score(best)
    resolved = bool(follow_up.get("command")) and resolve_candidate_is_confident(top)
    confidence = resolve_confidence_label(best_score, resolved=resolved)
    return {
        "provider": "raiderio",
        "query": search_payload.get("query"),
        "search_query": search_payload.get("search_query"),
        "resolved": resolved,
        "confidence": confidence if top else "none",
        "match": best,
        "next_command": follow_up.get("command") if resolved else None,
        "fallback_search_command": None if resolved else f'raiderio search "{search_payload.get("search_query")}"',
        "candidates": top,
    }


def search_results(client: RaiderIOClient, query: str, *, limit: int, kind: str) -> dict[str, Any]:
    """Rank Raider.IO character and guild matches for a free-text query."""
    normalized_query, type_hint, region, realm, name = normalize_structured_query(query)
    structured_candidates = probe_structured_candidates(
        client,
        query=normalized_query,
        type_hint=type_hint,
        region=region,
        realm=realm,
        name=name,
    )
    if structured_candidates:
        return _search_results_payload(
            normalized_query,
            [],
            type_hint=type_hint,
            limit=limit,
            extra_candidates=structured_candidates,
        )
    payload = client.search(term=normalized_query, kind=type_hint or kind)
    raw_matches = [row for row in as_list(payload.get("matches")) if isinstance(row, dict)]
    return _search_results_payload(normalized_query, raw_matches, type_hint=type_hint, limit=limit)


def doctor_report() -> dict[str, Any]:
    """Describe installation state, capabilities, and resolved cache configuration."""
    settings, static_ttl, character_ttl, guild_ttl, mplus_runs_ttl, raid_rankings_ttl = load_raiderio_cache_settings_from_env()
    return {
        "status": "ready",
        "installed": True,
        "language": "python",
        "auth": {
            "required": False,
            "deferred": True,
        },
        "capabilities": dict(CAPABILITIES),
        "cache": {
            "enabled": settings.enabled,
            "backend": settings.backend,
            "cache_dir": str(settings.cache_dir),
            "redis_url": settings.redis_url,
            "prefix": settings.prefix,
            "ttls": {
                "static_data": static_ttl,
                "character_profile": character_ttl,
                "guild_profile": guild_ttl,
                "mythic_plus_runs": mplus_runs_ttl,
                "raid_rankings": raid_rankings_ttl,
            },
        },
    }


@dataclass(slots=True)
class RaiderIOProvider:
    """In-process Raider.IO surface shared by the ``raiderio`` binary and the ``warcraft`` wrapper."""

    name: str = PROVIDER_NAME

    def search(self, query: str, *, limit: int = 10, **options: Any) -> Envelope:
        kind = validated_kind(str(options.get("kind", "all")))
        with transport_errors(), open_client() as client:
            payload = search_results(client, query, limit=limit, kind=kind)
        return raiderio_envelope(command="search", kind="search_results", payload=payload)

    def resolve(self, target: str, **options: Any) -> Envelope:
        limit = int(options.get("limit", 5))
        kind = validated_kind(str(options.get("kind", "all")))
        with transport_errors(), open_client() as client:
            payload = search_results(client, target, limit=limit, kind=kind)
        return raiderio_envelope(
            command="resolve",
            kind="resolve_match",
            payload=_resolve_payload(payload, limit=limit),
        )

    def doctor(self, **options: Any) -> Envelope:
        try:
            report = doctor_report()
        except ValueError as exc:
            return error_envelope(provider=self.name, command="doctor", code="invalid_cache_config", message=str(exc))
        return raiderio_envelope(command="doctor", kind="doctor", payload=report)


PROVIDER: ProviderSurface = RaiderIOProvider()

"""Pure Raider.IO provider surface: search, resolve, and doctor without any CLI coupling.

``PROVIDER`` satisfies :class:`warcraft_core.provider.ProviderSurface`; the Typer commands in
``main`` and the ``warcraft`` wrapper both call it. Nothing here prints or raises ``typer.Exit``:
failures raise :class:`~warcraft_core.provider.ProviderError`.
"""

from __future__ import annotations

import shlex
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import httpx
from warcraft_api.cache import redacted_redis_url
from warcraft_core.envelope import ENVELOPE_KEYS, Envelope, error_envelope, success_envelope
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
    envelope; the rest land in ``data``.
    """
    data = {key: value for key, value in payload.items() if key not in ENVELOPE_KEYS}
    provenance = {key: payload[key] for key in ("freshness", "citations") if key in payload}
    return success_envelope(
        provider=PROVIDER_NAME,
        command=command,
        kind=kind,
        data=data,
        query=payload.get("query"),
        provenance=provenance,
    )


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
        # Raider.IO answers a missing character/guild with HTTP 400 "Could not find requested <x>", an
        # unknown realm with HTTP 400 "Failed to find realm <x> in region <y>", and a malformed request
        # with HTTP 400 "Invalid request query input". Only the last is a usage error, so the message
        # is what separates exit 4 (not found) from exit 2.
        if status == 400 and message.lower().startswith(("could not find", "failed to find")):
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
        raise ProviderError("invalid_argument", "--kind must be one of: all, character, guild")
    return kind


def _resolve_payload(query: str, ranked: list[dict[str, Any]], *, limit: int) -> dict[str, Any]:
    """Judge confidence on every ranked candidate; ``limit`` only trims the ``candidates`` shown.

    Truncating first would hide the rivals: ``--limit 1`` leaves one row, which always looks unique.
    """
    fallback = shlex.join(["raiderio", "search", query])
    if not ranked:
        return {
            "provider": "raiderio",
            "query": query,
            "search_query": query,
            "resolved": False,
            "confidence": "none",
            "match": None,
            "next_command": None,
            "fallback_search_command": fallback,
            "candidates": [],
        }
    best = ranked[0]
    follow_up = as_dict(best.get("follow_up"))
    resolved = bool(follow_up.get("command")) and resolve_candidate_is_confident(ranked)
    return {
        "provider": "raiderio",
        "query": query,
        "search_query": query,
        "resolved": resolved,
        "confidence": resolve_confidence_label(candidate_ranking_score(best), resolved=resolved),
        "match": best,
        "next_command": follow_up.get("command") if resolved else None,
        "fallback_search_command": None if resolved else fallback,
        "candidates": ranked[:limit],
    }


def ranked_candidates(client: RaiderIOClient, query: str, *, kind: str) -> tuple[str, list[dict[str, Any]]]:
    """Every deduplicated Raider.IO character and guild match for a free-text query, best first.

    Returns the query with its leading type hint removed alongside the rows. A leading
    ``guild``/``character`` word in the query only narrows the lookups and scores. An explicit
    ``kind`` other than ``all`` wins over that word and filters every candidate, so ``--kind guild``
    never answers with a character, even when nothing of that kind exists.
    """
    normalized_query, type_hint, probes = normalize_structured_query(query)
    explicit_kind = None if kind == "all" else kind
    lookup_kind = explicit_kind or type_hint
    candidates = probe_structured_candidates(
        client,
        query=normalized_query,
        type_hint=type_hint,
        kind=lookup_kind,
        probes=probes,
    )
    if not candidates:
        payload = client.search(term=normalized_query, kind=lookup_kind)
        raw_matches = [row for row in as_list(payload.get("matches")) if isinstance(row, dict)]
        candidates = search_result_candidates(raw_matches, query=normalized_query, type_hint=type_hint)
    if explicit_kind:
        candidates = [row for row in candidates if row["kind"] == explicit_kind]
    return normalized_query, sorted_search_candidates(dedupe_search_candidates(candidates))


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
            "redis_url": redacted_redis_url(settings.redis_url),
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
            search_query, ranked = ranked_candidates(client, query, kind=kind)
        payload = {
            "provider": "raiderio",
            "query": search_query,
            "search_query": search_query,
            "count": len(ranked),
            "results": ranked[:limit],
            "truncated": len(ranked) > limit,
        }
        return raiderio_envelope(command="search", kind="search_results", payload=payload)

    def resolve(self, target: str, **options: Any) -> Envelope:
        limit = int(options.get("limit", 5))
        kind = validated_kind(str(options.get("kind", "all")))
        with transport_errors(), open_client() as client:
            search_query, ranked = ranked_candidates(client, target, kind=kind)
        return raiderio_envelope(
            command="resolve",
            kind="resolve_match",
            payload=_resolve_payload(search_query, ranked, limit=limit),
        )

    def doctor(self, **options: Any) -> Envelope:
        try:
            report = doctor_report()
        except ValueError as exc:
            return error_envelope(provider=self.name, command="doctor", code="invalid_cache_config", message=str(exc))
        return raiderio_envelope(command="doctor", kind="doctor", payload=report)


PROVIDER: ProviderSurface = RaiderIOProvider()

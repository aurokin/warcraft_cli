"""Pure Lorrgs provider surface.

Functions here never print and never raise ``typer.Exit``: they return an ``Envelope`` or raise
``ProviderError``. ``lorrgs_cli.main`` wraps them for the CLI and the ``warcraft`` wrapper can call
``PROVIDER`` in-process.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, cast

import httpx
from warcraft_core.envelope import ENVELOPE_KEYS, Envelope, success_envelope, with_legacy_keys
from warcraft_core.provider import ProviderError, ProviderSurface

from lorrgs_cli.client import API_HOST, OPENAPI_URL, PROVIDER_NAME, SITE_HOST, LorrgsClient, LorrgsClientError
from lorrgs_cli.search import resolve_payload, search_candidates

CAPABILITIES: dict[str, str] = {
    "doctor": "ready",
    "search": "ready",
    "resolve": "ready",
    "roles": "ready",
    "classes": "ready",
    "specs": "ready",
    "spec": "ready",
    "spec_spells": "ready",
    "zones": "ready",
    "season": "ready",
    "current_season": "ready",
    "zone": "ready",
    "zone_bosses": "ready",
    "bosses": "ready",
    "boss": "ready",
    "boss_spells": "ready",
    "spell": "ready",
    "trinkets": "ready",
    "spec_ranking": "ready",
    "spec_ranking_info": "ready",
    "comp_ranking": "ready",
    "report_overview": "ready_cached_only",
    "user_report": "ready_cached_only",
    "user_report_fights": "ready_cached_only",
}

NOTES: list[str] = [
    "Lorrgs visualizes Warcraft Logs-derived cooldown timelines for top parses by spec and boss.",
    "This provider intentionally exposes raw Lorrgs JSON plus source URLs; it does not synthesize cooldown plans.",
    "Read-only CLI surface: queued load/dirty endpoints are intentionally not exposed.",
    "Search/resolve understand Lorrgs ranking URLs, Warcraft Logs report URLs, and free text spec/boss pairs.",
]

# Lorrgs takes no credentials at all (doctor reports flow "none"), so a 401/403 can never mean "bad
# or missing credentials". It means Lorrgs, or the Warcraft Logs report behind it, refuses to serve
# that resource anonymously — an unreadable target, not an auth problem. Mapping it to auth_failed
# (exit 3) told agents to authenticate against a provider they can never authenticate to.
_HTTP_STATUS_CODES: dict[int, str] = {401: "not_found", 403: "not_found", 404: "not_found", 422: "invalid_query", 429: "rate_limited"}
_REFUSAL_STATUSES = frozenset({401, 403})


def _dual_emit(envelope: Envelope, payload: Mapping[str, Any]) -> Envelope:
    """Envelope plus deprecated top-level copies of the payload keys agents read today."""
    legacy = {key: value for key, value in payload.items() if key not in ENVELOPE_KEYS}
    # with_legacy_keys returns a plain dict; the envelope keys it carries are untouched.
    return cast(Envelope, with_legacy_keys(envelope, legacy))


def _response_detail(response: httpx.Response) -> str | None:
    try:
        payload = response.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    detail = payload.get("detail")
    if isinstance(detail, str) and detail.strip():
        return detail.strip()
    return None


def _status_message(exc: httpx.HTTPStatusError) -> str:
    status = exc.response.status_code
    detail = _response_detail(exc.response)
    if status in _REFUSAL_STATUSES:
        return (
            f"Lorrgs refused to serve {exc.request.url} ({detail or f'HTTP {status}'}). Lorrgs takes no "
            "credentials, so this is not an authentication problem: the resource is private, or Lorrgs "
            "has not loaded that report."
        )
    return detail or f"Lorrgs API returned HTTP {status} for {exc.request.url}."


def provider_error(exc: Exception) -> ProviderError:
    """Translate a client or transport failure into the shared error vocabulary."""
    if isinstance(exc, LorrgsClientError):
        return ProviderError(exc.code, exc.message)
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        code = _HTTP_STATUS_CODES.get(status, "http_error")
        return ProviderError(code, _status_message(exc), details={"status_code": status, "url": str(exc.request.url)})
    if isinstance(exc, httpx.TimeoutException):
        return ProviderError("timeout", f"Lorrgs API request timed out: {exc}.")
    return ProviderError("network_error", f"Lorrgs API request failed: {exc}.")


def _provenance(source_url: str | None = None) -> dict[str, Any]:
    provenance: dict[str, Any] = {
        "api_host": API_HOST,
        "site": SITE_HOST,
        "source": "lorrgs_public_api",
        "upstream_data_sources": ["warcraftlogs", "wowhead_tooltips"],
        "verified": True,
    }
    if source_url is not None:
        return {"source_url": source_url, **provenance}
    return provenance


def _envelope_data(kind: str, payload: Any) -> dict[str, Any]:
    """Coerce one Lorrgs route payload into the object shape the envelope requires.

    ``/api/zones`` answers with a bare JSON array, unlike ``/api/specs`` and ``/api/bosses``, which
    wrap theirs. Key an array under the payload kind so ``data`` matches the wrapped routes'
    shape (``{"zones": [...]}``) instead of breaking the envelope contract.
    """
    if isinstance(payload, dict):
        return payload
    return {kind: payload}


def call_api(command: str, kind: str, query: dict[str, Any], call: Callable[[LorrgsClient], dict[str, Any]]) -> Envelope:
    """Run one Lorrgs API call and wrap its payload in the success envelope."""
    with LorrgsClient() as client:
        try:
            result = call(client)
        except (LorrgsClientError, httpx.HTTPError) as exc:
            raise provider_error(exc) from exc
    return success_envelope(
        provider=PROVIDER_NAME,
        command=command,
        kind=kind,
        data=_envelope_data(kind, result["payload"]),
        query=query,
        provenance=_provenance(result["source_url"]),
    )


def search(query: str, *, limit: int = 5, **options: Any) -> Envelope:
    """Rank Lorrgs surfaces for a URL, report reference, or free-text spec/boss query."""
    with LorrgsClient() as client:
        try:
            payload = search_candidates(client, query, limit=limit)
        except (LorrgsClientError, httpx.HTTPError) as exc:
            raise provider_error(exc) from exc
    envelope = success_envelope(
        provider=PROVIDER_NAME,
        command="search",
        kind="search_results",
        data=payload,
        query=query,
        provenance=_provenance(),
    )
    return _dual_emit(envelope, payload)


def resolve(target: str, *, limit: int = 5, **options: Any) -> Envelope:
    """Resolve a Lorrgs query to a single next command when the top candidate is unambiguous."""
    with LorrgsClient() as client:
        try:
            payload = resolve_payload(client, target, limit=limit)
        except (LorrgsClientError, httpx.HTTPError) as exc:
            raise provider_error(exc) from exc
    envelope = success_envelope(
        provider=PROVIDER_NAME,
        command="resolve",
        kind="resolution",
        data=payload,
        query=target,
        provenance=_provenance(),
    )
    return _dual_emit(envelope, payload)


def doctor(**options: Any) -> Envelope:
    """Report Lorrgs auth posture, endpoints, and per-surface capability state."""
    payload: dict[str, Any] = {
        "status": "partial",
        "installed": True,
        "language": "python",
        "auth": {"required": False, "configured": True, "flow": "none"},
        "endpoints": {"site": SITE_HOST, "api": API_HOST, "openapi": OPENAPI_URL},
        "capabilities": dict(CAPABILITIES),
        "notes": list(NOTES),
    }
    envelope = success_envelope(provider=PROVIDER_NAME, command="doctor", kind="doctor", data=payload)
    return _dual_emit(envelope, payload)


class LorrgsProvider:
    """In-process Lorrgs surface for the ``warcraft`` wrapper."""

    name = PROVIDER_NAME

    search = staticmethod(search)
    resolve = staticmethod(resolve)
    doctor = staticmethod(doctor)


PROVIDER: ProviderSurface = LorrgsProvider()

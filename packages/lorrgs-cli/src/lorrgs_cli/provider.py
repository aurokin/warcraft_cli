"""Pure Lorrgs provider surface.

Functions here never print and never raise ``typer.Exit``: they return an ``Envelope`` or raise
``ProviderError``. ``lorrgs_cli.main`` wraps them for the CLI and the ``warcraft`` wrapper can call
``PROVIDER`` in-process.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

import httpx
from warcraft_api.cache import redacted_redis_url
from warcraft_core.discovery import RESOLVE_KIND, SEARCH_KIND
from warcraft_core.envelope import Envelope, success_envelope
from warcraft_core.provider import ProviderError, ProviderSurface
from warcraft_core.wow_specs import close_specs, lookup_spec

from lorrgs_cli.client import (
    API_HOST,
    DIFFICULTIES,
    OPENAPI_URL,
    PROVIDER_NAME,
    SITE_HOST,
    LorrgsClient,
    LorrgsClientError,
    load_lorrgs_cache_settings_from_env,
)
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
    "report_overview": "ready",
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
            "credentials, so this is not an authentication problem: the resource is private."
        )
    return detail or f"Lorrgs API returned HTTP {status} for {exc.request.url}."


def provider_error(exc: Exception) -> ProviderError:
    """Translate a client or transport failure into the shared error vocabulary."""
    if isinstance(exc, LorrgsClientError):
        return ProviderError(exc.code, exc.message)
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        code = _HTTP_STATUS_CODES.get(status, "upstream_error")
        return ProviderError(code, _status_message(exc), details={"status_code": status, "url": str(exc.request.url)})
    if isinstance(exc, httpx.TimeoutException):
        return ProviderError("timeout", f"Lorrgs API request timed out: {exc}.")
    return ProviderError("network_error", f"Lorrgs API request failed: {exc}.")


def _provenance(result: dict[str, Any] | None = None) -> dict[str, Any]:
    """Shared provenance, plus the source URL of one API answer when given."""
    provenance: dict[str, Any] = {
        "api_host": API_HOST,
        "site": SITE_HOST,
        "source": "lorrgs_public_api",
        "upstream_data_sources": ["warcraftlogs", "wowhead_tooltips"],
        "verified": True,
    }
    if result is None:
        return provenance
    return {"source_url": result["source_url"], **provenance}


def open_client() -> LorrgsClient:
    """Build a cache-configured Lorrgs client, or fail with the cache config error."""
    try:
        return LorrgsClient()
    except ValueError as exc:
        raise ProviderError("invalid_cache_config", str(exc)) from exc


def _envelope_data(kind: str, payload: Any) -> dict[str, Any]:
    """Coerce one Lorrgs route payload into the object shape the envelope requires.

    ``/api/zones`` answers with a bare JSON array, unlike ``/api/specs`` and ``/api/bosses``, which
    wrap theirs. Key an array under the payload kind so ``data`` matches the wrapped routes'
    shape (``{"zones": [...]}``) instead of breaking the envelope contract.
    """
    if isinstance(payload, dict):
        return payload
    return {kind: payload}


# Composition filters are `<role or spec>.<op>.<count>`; Lorrgs answers any other spelling (`heal>=4`,
# `heal.ne.4`) with HTTP 500 rather than a validation error.
_COMP_FILTER = re.compile(r"[a-z]+(?:-[a-z]+)*\.(?:eq|gt|gte|lt|lte)\.\d+")
# The `lorrgs roles` codes a composition filter counts. Any other name (the display name "healer",
# or util/mix/item) is not an error upstream: it silently matches no report.
COMP_ROLES = ("tank", "heal", "mdps", "rdps")


def validated_difficulty(difficulty: str) -> str:
    """Reject a difficulty Lorrgs does not rank before the request turns it into a 404."""
    value = difficulty.strip().lower()
    if value not in DIFFICULTIES:
        raise ProviderError("invalid_query", f"--difficulty must be one of: {', '.join(DIFFICULTIES)} (got {difficulty!r}).")
    return value


def validated_comp_filters(values: list[str] | None, *, flag: str) -> list[str] | None:
    """Reject a composition filter Lorrgs cannot parse, naming the syntax it takes."""
    for value in values or []:
        if not _COMP_FILTER.fullmatch(value):
            raise ProviderError(
                "invalid_query",
                f"{flag} takes <name>.<op>.<count> with op one of eq, gt, gte, lt, lte, e.g. heal.gte.4 (got {value!r}).",
            )
        if flag == "--role" and value.split(".")[0] not in COMP_ROLES:
            raise ProviderError("invalid_query", f"--role names one of: {', '.join(COMP_ROLES)} (got {value!r}).")
    return values


def lorrgs_spec_slug(text: str) -> str:
    """The Lorrgs slug for any provider's spelling of a spec (balance-druid -> druid-balance).

    Text that names no one spec (other-trinkets, a typo, a bare shared spec like frost) is kept as
    typed, so Lorrgs still answers it: a page for its own pseudo-specs, a 404 otherwise.
    """
    spec = lookup_spec(text)
    return spec.lorrgs_slug if spec else text


def lorrgs_comp_spec_filters(values: list[str] | None) -> list[str] | None:
    """``--spec`` composition filters with the spec spelled as Lorrgs does (BeastMastery.gte.1 -> hunter-beastmastery.gte.1)."""
    if not values:
        return values
    return [lorrgs_spec_slug(name) + dot + rest for name, dot, rest in (value.partition(".") for value in values)]


def note_empty_ranking(result: dict[str, Any], subject: str) -> dict[str, Any]:
    """Say so when Lorrgs answers a ranking with no reports, so ``[]`` is not read as an answer.

    ``subject`` names what was ranked, e.g. "composition reports for <boss> with these filters".
    """
    payload = result["payload"]
    if isinstance(payload, dict) and payload.get("reports") == []:
        payload["notes"] = [
            f"Lorrgs returned no {subject}: the upstream ranking is empty, so there is nothing to rank yet. "
            "It does not mean nobody plays or logs this."
        ]
    return result


def call_api(command: str, kind: str, query: dict[str, Any], call: Callable[[LorrgsClient], dict[str, Any]]) -> Envelope:
    """Run one Lorrgs API call and wrap its payload in the success envelope."""
    with open_client() as client:
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
        provenance=_provenance(result),
    )


def call_spec_api(
    command: str,
    kind: str,
    spec_text: str,
    query: dict[str, Any],
    call: Callable[[LorrgsClient, str], dict[str, Any]],
) -> Envelope:
    """Run a spec route with ``spec_text`` spelled as Lorrgs does; ``query.spec_slug`` echoes the slug sent.

    When the text names no spec and Lorrgs answers not_found, ``details.suggestions`` lists the
    closest Lorrgs spec slugs, if any are close.
    """
    slug = lorrgs_spec_slug(spec_text)
    try:
        return call_api(command, kind, {**query, "spec_slug": slug}, lambda client: call(client, slug))
    except ProviderError as exc:
        suggestions = [spec.lorrgs_slug for spec in close_specs(spec_text)] if lookup_spec(spec_text) is None else []
        if exc.code == "not_found" and suggestions:
            exc.details = {**(exc.details or {}), "suggestions": suggestions}
        raise


def search(query: str, *, limit: int = 5, **options: Any) -> Envelope:
    """Rank Lorrgs surfaces for a URL, report reference, or free-text spec/boss query."""
    with open_client() as client:
        try:
            payload = search_candidates(client, query, limit=limit)
        except (LorrgsClientError, httpx.HTTPError) as exc:
            raise provider_error(exc) from exc
    return success_envelope(
        provider=PROVIDER_NAME,
        command="search",
        kind=SEARCH_KIND,
        data=payload,
        query=query,
        provenance=_provenance(),
    )


def resolve(target: str, *, limit: int = 5, **options: Any) -> Envelope:
    """Resolve a Lorrgs query to a single next command when the top candidate is unambiguous."""
    with open_client() as client:
        try:
            payload = resolve_payload(client, target, limit=limit)
        except (LorrgsClientError, httpx.HTTPError) as exc:
            raise provider_error(exc) from exc
    return success_envelope(
        provider=PROVIDER_NAME,
        command="resolve",
        kind=RESOLVE_KIND,
        data=payload,
        query=target,
        provenance=_provenance(),
    )


def doctor(**options: Any) -> Envelope:
    """Report Lorrgs auth posture, endpoints, and per-surface capability state."""
    try:
        settings, static_ttl, ranking_ttl, report_ttl = load_lorrgs_cache_settings_from_env()
    except ValueError as exc:
        raise ProviderError("invalid_cache_config", str(exc)) from exc
    payload: dict[str, Any] = {
        # Lorrgs takes no credentials, so nothing can leave a surface unconfigured.
        "status": "ready",
        "installed": True,
        "language": "python",
        "auth": {"required": False, "configured": True, "flow": "none"},
        "endpoints": {"site": SITE_HOST, "api": API_HOST, "openapi": OPENAPI_URL},
        "capabilities": dict(CAPABILITIES),
        "cache": {
            "enabled": settings.enabled,
            "backend": settings.backend,
            "cache_dir": str(settings.cache_dir),
            "redis_url": redacted_redis_url(settings.redis_url),
            "prefix": settings.prefix,
            "ttls": {"static_metadata": static_ttl, "rankings": ranking_ttl, "loaded_fights": report_ttl},
        },
        "notes": list(NOTES),
    }
    return success_envelope(provider=PROVIDER_NAME, command="doctor", kind="doctor", data=payload)


class LorrgsProvider:
    """In-process Lorrgs surface for the ``warcraft`` wrapper."""

    name = PROVIDER_NAME

    search = staticmethod(search)
    resolve = staticmethod(resolve)
    doctor = staticmethod(doctor)


PROVIDER: ProviderSurface = LorrgsProvider()

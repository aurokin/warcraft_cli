"""Pure Raidbots provider surface.

Functions here never print and never raise ``typer.Exit``: they return an ``Envelope`` or raise
``ProviderError``. ``raidbots_cli.main`` wraps them for the CLI and the ``warcraft`` wrapper can
call ``PROVIDER`` in-process.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Final, cast

import httpx
from warcraft_core.envelope import ENVELOPE_KEYS, Envelope, success_envelope, with_legacy_keys
from warcraft_core.provider import ProviderError, ProviderSurface

from raidbots_cli.client import (
    InvalidReportReference,
    RaidbotsClient,
    ReportNotAvailable,
    load_raidbots_cache_settings_from_env,
    load_raidbots_urls_from_env,
    resolve_report_id,
)
from raidbots_cli.report import parse_report
from raidbots_cli.simc_input import classify_simc_input, simc_handoff

PROVIDER_NAME: Final = "raidbots"

CAPABILITIES: Final[dict[str, str]] = {
    "search": "not_supported",
    "resolve": "not_supported",
    "inspect_report": "ready",
    "input": "ready",
    "explain_input": "ready",
    "submit": "not_supported",
}

NOTES: Final[list[str]] = [
    "Report consumption only: fetch + parse public reports and bridge to local simc.",
    "Submission is deferred (no sanctioned Raidbots API); generate input locally and paste it.",
]

# Raidbots publishes no report index and no search API, so the two generic surfaces every provider
# exposes return a structured stub instead of an error; doctor and the wrapper registry say the same.
NOT_SUPPORTED_MESSAGE: Final = (
    "Raidbots exposes no public report index; open a known report with `raidbots inspect-report <url-or-id>`."
)
SUGGESTED_COMMAND: Final = "raidbots inspect-report <url-or-id>"

# Raidbots needs no auth, and data.json redirects to a public GCS bucket that answers 403 (not 404)
# for an object that does not exist or has expired — so a 403 here means "no such report", never
# "bad credentials".
_HTTP_STATUS_CODES: Final[dict[int, str]] = {400: "invalid_query", 403: "not_found", 404: "not_found", 429: "rate_limited"}


def _dual_emit(envelope: Envelope, payload: dict[str, Any]) -> Envelope:
    """Envelope plus deprecated top-level copies of the payload keys agents read today."""
    legacy = {key: value for key, value in payload.items() if key not in ENVELOPE_KEYS}
    return cast(Envelope, with_legacy_keys(envelope, legacy))


def provider_error(exc: Exception) -> ProviderError:
    """Translate a transport failure into the shared error-code vocabulary."""
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        code = _HTTP_STATUS_CODES.get(status, "upstream_error")
        details = {"status_code": status, "url": str(exc.request.url)}
        message = f"Raidbots request failed with HTTP {status}."
        if code == "not_found":
            message = (
                f"Raidbots has no readable report at {exc.request.url} (HTTP {status}): the report id "
                "is wrong, or the report has expired or is private."
            )
        return ProviderError(code, message, details=details)
    if isinstance(exc, httpx.TimeoutException):
        return ProviderError("timeout", f"Raidbots request timed out: {exc}")
    return ProviderError("network_error", f"Raidbots request failed: {exc}")


def _client() -> RaidbotsClient:
    try:
        return RaidbotsClient()
    except ValueError as exc:
        raise ProviderError("invalid_cache_config", str(exc)) from exc


def _report_id(reference: str) -> str:
    try:
        # Pass the configured (env-overridable) report path template so URL input parsing
        # round-trips the report URLs this CLI emits even when the template is overridden.
        return resolve_report_id(reference, load_raidbots_urls_from_env().report_path_template)
    except InvalidReportReference as exc:
        raise ProviderError("invalid_report", str(exc)) from exc


def _freshness(client: RaidbotsClient) -> dict[str, Any]:
    # retrieved_at is always accurate (when this CLI produced the response). from_cache flags
    # that the payload may be up to cache_ttl_seconds old, rather than implying a live fetch.
    return {
        "retrieved_at": datetime.now(UTC).isoformat(),
        "from_cache": client.last_from_cache,
        "cache_ttl_seconds": client.report_ttl_seconds,
    }


def _citations(client: RaidbotsClient, report_id: str) -> dict[str, Any]:
    urls = client.urls
    return {
        "report_url": urls.report_url(report_id),
        "data_json_url": urls.data_url(report_id),
        "simc_input_url": urls.input_url(report_id),
    }


def _not_supported(command: str, kind: str, query: str) -> Envelope:
    payload: dict[str, Any] = {
        "results": [],
        "count": 0,
        "not_supported": True,
        "message": NOT_SUPPORTED_MESSAGE,
        "suggested_command": SUGGESTED_COMMAND,
    }
    envelope = success_envelope(provider=PROVIDER_NAME, command=command, kind=kind, data=payload, query=query)
    return _dual_emit(envelope, payload)


def search(query: str, *, limit: int = 10, **options: Any) -> Envelope:
    """Return the structured not-supported stub: Raidbots has no searchable report index."""
    return _not_supported("search", "search_results", query)


def resolve(target: str, **options: Any) -> Envelope:
    """Return the structured not-supported stub: Raidbots resolves nothing but a known report ID."""
    return _not_supported("resolve", "resolve_match", target)


def doctor(**options: Any) -> Envelope:
    """Report install status, capabilities, cache configuration, and the resolved URL templates."""
    try:
        settings, report_ttl = load_raidbots_cache_settings_from_env()
    except ValueError as exc:
        raise ProviderError("invalid_cache_config", str(exc)) from exc
    urls = load_raidbots_urls_from_env()
    payload: dict[str, Any] = {
        "status": "partial",
        "installed": True,
        "language": "python",
        "auth": {"required": False, "deferred": True},
        "capabilities": dict(CAPABILITIES),
        "url_templates": urls.templates(),
        "cache": {
            "enabled": settings.enabled,
            "backend": settings.backend,
            "cache_dir": str(settings.cache_dir),
            "redis_url": settings.redis_url,
            "prefix": settings.prefix,
            "ttls": {"report": report_ttl},
        },
        "notes": list(NOTES),
    }
    envelope = success_envelope(provider=PROVIDER_NAME, command="doctor", kind="doctor", data=payload)
    return _dual_emit(envelope, payload)


def inspect_report(reference: str, *, include_raw: bool = True) -> Envelope:
    """Fetch a report's `data.json` and parse it into a kind-aware summary."""
    report_id = _report_id(reference)
    with _client() as client:
        try:
            data = client.report_data(report_id)
            report = parse_report(data, report_id=report_id)
        except httpx.HTTPError as exc:
            raise provider_error(exc) from exc
        except ValueError as exc:
            # Covers invalid JSON and non-object bodies from report_data as well as parse failures.
            raise ProviderError("invalid_report", str(exc)) from exc
        freshness = _freshness(client)
        citations = _citations(client, report_id)
    payload: dict[str, Any] = {
        "report": report,
        "scope": {"type": "raidbots_report", "kind": report.get("kind")},
        "freshness": freshness,
        "citations": citations,
    }
    if include_raw:
        payload["raw"] = data
    envelope = success_envelope(
        provider=PROVIDER_NAME,
        command="inspect-report",
        kind="report",
        data=payload,
        query=report_id,
        provenance={**citations, **freshness},
    )
    return _dual_emit(envelope, payload)


def report_input(reference: str) -> Envelope:
    """Fetch a report's SimC input and pair it with the local `simc` handoff."""
    report_id = _report_id(reference)
    with _client() as client:
        try:
            text = client.report_input(report_id)
        except httpx.HTTPError as exc:
            raise provider_error(exc) from exc
        except ReportNotAvailable as exc:
            raise ProviderError("not_found", str(exc)) from exc
        freshness = _freshness(client)
        citations = _citations(client, report_id)
    classification = classify_simc_input(text)
    payload: dict[str, Any] = {
        "report_id": report_id,
        "input": text,
        "handoff": simc_handoff(text, classification),
        "scope": {"type": "raidbots_simc_input", "sim_type_guess": classification["sim_type_guess"]},
        "freshness": freshness,
        "citations": citations,
    }
    envelope = success_envelope(
        provider=PROVIDER_NAME,
        command="input",
        kind="simc_input",
        data=payload,
        query=report_id,
        provenance={**citations, **freshness},
    )
    return _dual_emit(envelope, payload)


def explain_input(text: str) -> Envelope:
    """Classify SimC addon/profile text locally and explain the local `simc` handoff. No network."""
    if not text.strip():
        raise ProviderError("invalid_query", "No SimC input provided (use --text, --file, or stdin).")
    classification = classify_simc_input(text)
    payload: dict[str, Any] = {
        "scope": {"type": "raidbots_simc_input", "sim_type_guess": classification["sim_type_guess"]},
        "handoff": simc_handoff(text, classification),
    }
    envelope = success_envelope(provider=PROVIDER_NAME, command="explain-input", kind="simc_input", data=payload)
    return _dual_emit(envelope, payload)


class RaidbotsProvider:
    """In-process Raidbots surface for the ``warcraft`` wrapper."""

    name = PROVIDER_NAME

    search = staticmethod(search)
    resolve = staticmethod(resolve)
    doctor = staticmethod(doctor)


PROVIDER: ProviderSurface = RaidbotsProvider()

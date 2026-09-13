"""Pure Warcraft Logs provider surface.

Functions here never print and never raise ``typer.Exit``: they return an ``Envelope`` or raise
``ProviderError``. ``warcraftlogs_cli.main`` wraps them for the CLI, and the ``warcraft`` wrapper
can call ``PROVIDER`` in-process instead of shelling out.

The payload builders still live in ``warcraftlogs_cli.main`` (a 6k-line module that also owns the
50+ report commands), so they are imported lazily inside each function: importing them at module
scope would make ``main`` -> ``provider`` -> ``main`` a real import cycle.
"""

from __future__ import annotations

from typing import Any

from warcraft_core.envelope import ENVELOPE_KEYS, Envelope, success_envelope
from warcraft_core.exit_codes import EXIT_AUTH, exit_code_for
from warcraft_core.provider import ProviderError, ProviderSurface

from warcraftlogs_cli.client import RETAIL_PROFILE, WarcraftLogsClientError, WarcraftLogsSiteProfile, resolve_site_profile

PROVIDER_NAME = "warcraftlogs"

# Warcraft Logs error codes that mean "not authorised", on top of the shared vocabulary.
AUTH_ERROR_CODES = frozenset({"missing_client_credentials", "missing_public_auth", "missing_user_auth", "user_token_expired"})


def provider_error(exc: WarcraftLogsClientError) -> ProviderError:
    """Translate a client failure into the shared error vocabulary and its exit code."""
    exit_code = EXIT_AUTH if exc.code in AUTH_ERROR_CODES else exit_code_for(exc.code)
    return ProviderError(exc.code, exc.message, exit_code=exit_code)


def site_profile(options: dict[str, Any]) -> WarcraftLogsSiteProfile:
    """Read the optional ``site`` option, accepting either a profile object or its key."""
    site = options.get("site")
    if isinstance(site, WarcraftLogsSiteProfile):
        return site
    if isinstance(site, str) and site:
        try:
            return resolve_site_profile(site)
        except ValueError as exc:
            raise ProviderError("invalid_query", str(exc)) from exc
    return RETAIL_PROFILE


def payload_body(payload: dict[str, Any]) -> dict[str, Any]:
    """The part of a legacy flat payload that belongs under the envelope's ``data``."""
    return {key: value for key, value in payload.items() if key not in ENVELOPE_KEYS}


def search(query: str, *, limit: int = 10, **options: Any) -> Envelope:
    """Match an explicit Warcraft Logs report URL or code; free text returns a discovery hint."""
    del limit  # Explicit report discovery returns at most one result.
    from warcraftlogs_cli.main import _explicit_report_reference, _report_search_payload

    site = site_profile(options)
    payload = _report_search_payload(query, ref=_explicit_report_reference(query), site=site)
    return success_envelope(
        provider=PROVIDER_NAME, command="search", kind="search_results", data=payload_body(payload), query=query
    )


def resolve(target: str, **options: Any) -> Envelope:
    """Resolve an explicit Warcraft Logs report URL or code to a single report reference."""
    from warcraftlogs_cli.main import _explicit_report_reference, _report_resolve_payload

    site = site_profile(options)
    payload = _report_resolve_payload(target, ref=_explicit_report_reference(target), site=site)
    return success_envelope(
        provider=PROVIDER_NAME, command="resolve", kind="resolution", data=payload_body(payload), query=target
    )


def doctor(**options: Any) -> Envelope:
    """Report auth, site-profile, and per-command readiness. Pass ``live=False`` to skip auth probes."""
    from warcraftlogs_cli.main import _doctor_payload

    site = site_profile(options)
    live = bool(options.get("live", True))
    payload = _doctor_payload(live=live, site=site)
    return success_envelope(provider=PROVIDER_NAME, command="doctor", kind="doctor", data=payload_body(payload))


class WarcraftLogsProvider:
    """Object form of the surface so the wrapper can hold one handle per provider."""

    name = PROVIDER_NAME

    search = staticmethod(search)
    resolve = staticmethod(resolve)
    doctor = staticmethod(doctor)


PROVIDER: ProviderSurface = WarcraftLogsProvider()

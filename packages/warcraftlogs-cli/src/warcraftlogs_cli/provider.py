"""Pure Warcraft Logs provider surface.

Functions here never print and never raise ``typer.Exit``: they return an ``Envelope`` or raise
``ProviderError``. ``warcraftlogs_cli.main`` wraps them for the CLI, and the ``warcraft`` wrapper
can call ``PROVIDER`` in-process instead of shelling out.
"""

from __future__ import annotations

from typing import Any

from warcraft_core.discovery import RESOLVE_KIND, SEARCH_KIND
from warcraft_core.envelope import ENVELOPE_KEYS, Envelope, success_envelope
from warcraft_core.provider import ProviderError, ProviderSurface

from warcraftlogs_cli.client import RETAIL_PROFILE, WarcraftLogsSiteProfile, resolve_site_profile
from warcraftlogs_cli.services import doctor_payload, explicit_report_reference, report_resolve_payload, report_search_payload

PROVIDER_NAME = "warcraftlogs"


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
    """A command's flat payload minus the envelope keys: what goes under the envelope's ``data``."""
    return {key: value for key, value in payload.items() if key not in ENVELOPE_KEYS}


def search(query: str, *, limit: int = 10, **options: Any) -> Envelope:
    """Match an explicit Warcraft Logs report URL or code; free text returns a discovery hint."""
    if not query.strip():
        raise ProviderError("invalid_query", "Query cannot be empty.")
    site = site_profile(options)
    data = report_search_payload(query, ref=explicit_report_reference(query), site=site, limit=limit)
    return success_envelope(provider=PROVIDER_NAME, command="search", kind=SEARCH_KIND, data=data, query=query)


def resolve(target: str, **options: Any) -> Envelope:
    """Resolve an explicit Warcraft Logs report URL or code to a single report reference."""
    if not target.strip():
        raise ProviderError("invalid_query", "Query cannot be empty.")
    site = site_profile(options)
    data = report_resolve_payload(target, ref=explicit_report_reference(target), site=site)
    return success_envelope(provider=PROVIDER_NAME, command="resolve", kind=RESOLVE_KIND, data=data, query=target)


def doctor(**options: Any) -> Envelope:
    """Report auth, site-profile, and per-command readiness. Pass ``live=False`` to skip auth probes."""
    site = site_profile(options)
    live = bool(options.get("live", True))
    payload = doctor_payload(live=live, site=site)
    return success_envelope(provider=PROVIDER_NAME, command="doctor", kind="doctor", data=payload_body(payload))


class WarcraftLogsProvider:
    """Object form of the surface so the wrapper can hold one handle per provider."""

    name = PROVIDER_NAME

    search = staticmethod(search)
    resolve = staticmethod(resolve)
    doctor = staticmethod(doctor)


PROVIDER: ProviderSurface = WarcraftLogsProvider()

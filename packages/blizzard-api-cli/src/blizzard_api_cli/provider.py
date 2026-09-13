"""Pure Blizzard provider surface: search, resolve, doctor, and the shared Game Data / Profile read.

Nothing here prints or raises ``typer.Exit``; every function returns an ``Envelope`` or raises
``ProviderError``. The Typer commands in ``main.py`` are thin wrappers over these functions and the
``warcraft`` wrapper can call ``PROVIDER`` in process. See docs/foundation/ERROR_CONTRACT.md.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, cast

import httpx
from warcraft_core.auth import provider_auth_status
from warcraft_core.envelope import Envelope, success_envelope, with_legacy_keys
from warcraft_core.exit_codes import EXIT_AUTH
from warcraft_core.paths import provider_state_path
from warcraft_core.provider import ProviderError, ProviderSurface

from blizzard_api_cli.auth import (
    PROVIDER as PROVIDER_NAME,
)
from blizzard_api_cli.auth import (
    BlizzardAuthConfig,
    blizzard_provider_env_path,
    load_blizzard_auth_config,
)
from blizzard_api_cli.client import (
    CLIENT_CREDENTIALS_STATE_PROVIDER,
    SUPPORTED_REGIONS,
    VERIFIED_REGIONS,
    BlizzardClient,
    BlizzardClientError,
    verification_note,
)

# Blizzard ships as an experimental provider: the surface is still small (three reads plus doctor)
# and search/resolve are stubs. Routing for us/eu/kr/tw is live-confirmed, so those payloads carry
# provenance.verified=true; CN is unreachable from here and stays false.
TIER = "experimental"

# Client error codes that mean "the caller is not authenticated"; everything else the client raises
# is an input or response-shape problem and keeps the generic exit code from exit_code_for().
_AUTH_ERROR_CODES = frozenset({"missing_client_credentials"})

_HTTP_STATUS_ERROR_CODES = {401: "auth_failed", 403: "auth_failed", 404: "not_found", 429: "rate_limited"}


def provider_error(exc: BlizzardClientError | httpx.HTTPError) -> ProviderError:
    """Translate a client or transport failure into the shared error code + exit code vocabulary."""
    if isinstance(exc, BlizzardClientError):
        return ProviderError(exc.code, exc.message, exit_code=EXIT_AUTH if exc.code in _AUTH_ERROR_CODES else None)
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return ProviderError(
            _HTTP_STATUS_ERROR_CODES.get(status, "upstream_error"),
            f"Blizzard API returned HTTP {status} for {exc.request.url}.",
            details={"status_code": status, "url": str(exc.request.url)},
        )
    if isinstance(exc, httpx.TimeoutException):
        return ProviderError("timeout", f"Blizzard API request timed out: {exc}.")
    return ProviderError("network_error", f"Blizzard API request failed: {exc}.")


def _dual_emit(command: str, kind: str, query: Any, payload: dict[str, Any]) -> Envelope:
    # Envelope whose data is `payload`, with payload's keys repeated at the top level. The top-level
    # copies are the pre-envelope shape agents already read and are deprecated; see ERROR_CONTRACT.md.
    envelope = success_envelope(provider=PROVIDER_NAME, command=command, kind=kind, query=query, data=payload)
    return cast(Envelope, with_legacy_keys(envelope, payload))


def _auth_payload(auth: BlizzardAuthConfig) -> dict[str, Any]:
    state = provider_auth_status(PROVIDER_NAME)
    return {
        "required": True,
        "configured": auth.configured,
        "client_credentials_configured": auth.configured,
        "flow": "oauth_client_credentials",
        "active_mode": "client_credentials",
        "endpoint_family": "client",
        "credential_source": auth.credential_source,
        "lookup_order": [".env.local", blizzard_provider_env_path(), "environment"],
        "state": state,
        "state_path": str(provider_state_path(PROVIDER_NAME)),
        # The client-credentials flow caches its token under a distinct provider key, so surface that
        # cache here too — otherwise doctor would report "no token" even after a successful command.
        "token_cache": provider_auth_status(CLIENT_CREDENTIALS_STATE_PROVIDER),
        "token_cache_path": str(provider_state_path(CLIENT_CREDENTIALS_STATE_PROVIDER)),
    }


def _region_payload(auth: BlizzardAuthConfig) -> dict[str, Any]:
    # Region/namespace routing is honored by the Game Data/Profile commands and is live-confirmed
    # for every region except CN, whose host and OAuth server are unreachable from outside China.
    return {
        "configured": auth.region,
        "default": "us",
        "supported_regions": list(SUPPORTED_REGIONS),
        "namespace_classes": ["dynamic", "static", "profile"],
        "routing": "ready",
        "verification": {
            "retail": "live_confirmed",
            "classic": "live_confirmed",
            "verified_regions": sorted(VERIFIED_REGIONS),
            "unverified_regions": sorted(set(SUPPORTED_REGIONS) - VERIFIED_REGIONS),
            "note": verification_note(),
        },
    }


def doctor_envelope() -> Envelope:
    """Install state, auth posture, region routing, and capability metadata for this provider."""
    auth = load_blizzard_auth_config()
    return _dual_emit(
        "doctor",
        "doctor",
        None,
        {
            "status": "partial",
            "tier": TIER,
            "installed": True,
            "language": "python",
            "auth": _auth_payload(auth),
            "region": _region_payload(auth),
            "capabilities": {
                "doctor": "ready",
                "search": "coming_soon",
                "resolve": "coming_soon",
                "game_data": "ready",
                "profile": "ready",
            },
            "notes": [
                "Experimental tier: the read surface is small (realm, item, character) and "
                "search/resolve are stubs. Payloads routed to a CN namespace report "
                "provenance.verified=false; every other region is live-confirmed.",
                "Game Data (realm, item) and Profile (character) commands ship with live OAuth "
                "client-credentials auth and region/namespace routing.",
                verification_note(),
                "Second OAuth validation point for the shared auth architecture (phase 3, docs/architecture/AUTH_ARCHITECTURE.md).",
            ],
        },
    )


def coming_soon_envelope(command: str, query: str) -> Envelope:
    """Structured stub for the surfaces doctor advertises as coming_soon (search, resolve)."""
    # A caller probing the advertised surface gets a JSON envelope with an explicit coming_soon flag
    # instead of Click's generic "No such command" error.
    return _dual_emit(
        command,
        "coming_soon",
        query,
        {
            "coming_soon": True,
            "results": [],
            "resolved": False,
            "match": None,
            "message": (
                f"blizzard {command} is not implemented yet; use the explicit Game Data / Profile reads. "
                "Try `blizzard realm <slug>`, `blizzard item <id>`, or `blizzard character <realm> <name>`."
            ),
            "suggested_command": "blizzard realm illidan",
        },
    )


def fetch(
    command: str,
    kind: str,
    query: Mapping[str, Any],
    call: Callable[[BlizzardClient], dict[str, Any]],
) -> Envelope:
    """Run one Game Data / Profile read and wrap the result in the shared success envelope."""
    client = BlizzardClient()
    try:
        result = call(client)
    except (BlizzardClientError, httpx.HTTPError) as exc:
        raise provider_error(exc) from exc
    finally:
        client.close()
    routing = result["routing"]
    return success_envelope(
        provider=PROVIDER_NAME,
        command=command,
        kind=kind,
        query=dict(query),
        provenance={
            "region": routing.region,
            "namespace": routing.namespace,
            "namespace_class": routing.namespace_class,
            "game_version": routing.game_version,
            "locale": routing.locale,
            "source_url": result["source_url"],
            "verified": routing.region in VERIFIED_REGIONS,
            "verification_note": verification_note(routing.region),
        },
        data=result["payload"],
    )


@dataclass(slots=True)
class BlizzardProvider:
    """In-process surface for the Blizzard provider; search and resolve are not implemented yet."""

    name: str = PROVIDER_NAME

    def search(self, query: str, *, limit: int = 10, **options: Any) -> Envelope:
        del limit, options
        return coming_soon_envelope("search", query)

    def resolve(self, target: str, **options: Any) -> Envelope:
        del options
        return coming_soon_envelope("resolve", target)

    def doctor(self, **options: Any) -> Envelope:
        del options
        return doctor_envelope()


PROVIDER: ProviderSurface = BlizzardProvider()

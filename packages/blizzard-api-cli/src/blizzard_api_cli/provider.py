"""Pure Blizzard provider surface: search, resolve, doctor, and the shared Game Data / Profile read.

Nothing here prints or raises ``typer.Exit``; every function returns an ``Envelope`` or raises
``ProviderError``. The Typer commands in ``main.py`` are thin wrappers over these functions and the
``warcraft`` wrapper can call ``PROVIDER`` in process. See docs/foundation/ERROR_CONTRACT.md.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

import httpx
from warcraft_api.cache import cache_backend_health, redacted_redis_url
from warcraft_core.auth import provider_auth_status
from warcraft_core.envelope import Envelope, success_envelope
from warcraft_core.exit_codes import EXIT_AUTH, EXIT_USAGE, error_code_for_http_status
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
    SUPPORTED_GAME_VERSIONS,
    SUPPORTED_REGIONS,
    VERIFIED_REGIONS,
    BlizzardClient,
    BlizzardClientError,
    load_blizzard_cache_settings_from_env,
    verification_note,
)

# Blizzard ships as a supported provider: the surface is explicit reads only and search/resolve
# are unsupported. The per-region verification posture lives in client.VERIFIED_REGIONS
# and client.verification_note(), which --help, doctor and every payload all quote.
TIER = "supported"

# Exit codes for the client's own error codes, which ERROR_CONTRACT's table does not name (so they
# would otherwise all exit 1). The routing codes are raised while validating flag values, before any
# request: a mistyped --region/--game-version is a usage error, and must exit 2 like the equivalent
# mistake on every other binary, so an agent can tell "fix the command" from "something broke".
# Anything else the client raises (invalid_response) is a real failure and keeps the generic code.
_EXIT_CODE_BY_CLIENT_CODE = {
    "missing_client_credentials": EXIT_AUTH,
    "unsupported_region": EXIT_USAGE,
    "unsupported_game_version": EXIT_USAGE,
}

def provider_error(exc: BlizzardClientError | httpx.HTTPError) -> ProviderError:
    """Translate a client or transport failure into the shared error code + exit code vocabulary."""
    if isinstance(exc, BlizzardClientError):
        return ProviderError(exc.code, exc.message, exit_code=_EXIT_CODE_BY_CLIENT_CODE.get(exc.code))
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return ProviderError(
            error_code_for_http_status(status),
            f"Blizzard API returned HTTP {status} for {exc.request.url}.",
            details={"status_code": status, "url": str(exc.request.url)},
        )
    if isinstance(exc, httpx.TimeoutException):
        return ProviderError("timeout", f"Blizzard API request timed out: {exc}.")
    return ProviderError("network_error", f"Blizzard API request failed: {exc}.")


def _auth_payload(auth: BlizzardAuthConfig) -> dict[str, Any]:
    return {
        "required": True,
        "configured": auth.configured,
        "flow": "oauth_client_credentials",
        "active_mode": "client_credentials",
        "endpoint_family": "client",
        "credential_source": auth.credential_source,
        "lookup_order": [".env.local", blizzard_provider_env_path(), "environment"],
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
            **dict.fromkeys(SUPPORTED_GAME_VERSIONS, "live_confirmed"),
            "verified_regions": sorted(VERIFIED_REGIONS),
            "unverified_regions": sorted(set(SUPPORTED_REGIONS) - VERIFIED_REGIONS),
            "note": verification_note(),
        },
    }


def _cache_payload() -> dict[str, Any]:
    try:
        settings, static_ttl, dynamic_ttl, snapshot_ttl = load_blizzard_cache_settings_from_env()
    except ValueError as exc:
        # Every read fails on this config, so doctor must not report the provider ready.
        return {"available": False, "error": {"code": "invalid_cache_config", "message": str(exc)}}
    return {
        "enabled": settings.enabled,
        "backend": settings.backend,
        "cache_dir": str(settings.cache_dir),
        "redis_url": redacted_redis_url(settings.redis_url),
        "prefix": settings.prefix,
        "ttls": {"static": static_ttl, "dynamic_and_profile": dynamic_ttl, "snapshot": snapshot_ttl},
        **cache_backend_health(settings),
    }


def doctor_envelope() -> Envelope:
    """Install state, auth posture, region routing, and capability metadata for this provider."""
    auth = load_blizzard_auth_config()
    # Every read needs client credentials, so without them the reads are blocked, not ready.
    reads = "ready" if auth.configured else "requires_client_credentials"
    cache = _cache_payload()
    return success_envelope(
        provider=PROVIDER_NAME,
        command="doctor",
        kind="doctor",
        data={
            # A Redis cache that does not answer, or a cache config that does not parse, degrades every read too.
            "status": "ready" if auth.configured and cache.get("available") is not False else "degraded",
            "tier": TIER,
            "installed": True,
            "language": "python",
            "auth": _auth_payload(auth),
            "region": _region_payload(auth),
            "capabilities": {
                "doctor": "ready",
                "search": "not_supported",
                "resolve": "not_supported",
                "game_data": reads,
                "profile": reads,
            },
            "cache": cache,
            "notes": [
                "Supported tier: the surface is explicit reads (realm, item, character, PvP, "
                "collections, auctions) and free-text discovery is unsupported.",
                "Game Data (realm, item, pvp-season, pvp-leaderboard, auctions, commodities) and Profile "
                "(character, pvp-character, collections) commands ship with live OAuth "
                "client-credentials auth and region/namespace routing.",
                verification_note(),
            ],
        },
    )


def unsupported_discovery(command: Literal["search", "resolve"], query: str) -> Envelope:
    """Typed reads are supported; free-text discovery is outside this provider's scope."""
    raise ProviderError(
        "unsupported_operation", f"Blizzard {command} is unsupported. Use an explicit realm, item, or character lookup.",
        exit_code=EXIT_USAGE, details={"operation": command, "available_commands": ["realm", "item", "character"]},
    )


def fetch(
    command: str,
    kind: str,
    query: Mapping[str, Any],
    call: Callable[[BlizzardClient], dict[str, Any]],
) -> Envelope:
    """Run one Game Data / Profile read and wrap the result in the shared success envelope."""
    try:
        client = BlizzardClient()
    except ValueError as exc:
        raise ProviderError("invalid_cache_config", str(exc)) from exc
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
    """In-process surface for the Blizzard provider; free-text search and resolve are unsupported."""

    name: str = PROVIDER_NAME

    def search(self, query: str, *, limit: int = 10, **options: Any) -> Envelope:
        del limit, options
        return unsupported_discovery("search", query)

    def resolve(self, target: str, **options: Any) -> Envelope:
        del options
        return unsupported_discovery("resolve", target)

    def doctor(self, **options: Any) -> Envelope:
        del options
        return doctor_envelope()


PROVIDER: ProviderSurface = BlizzardProvider()

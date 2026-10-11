"""Pure CurseForge provider surface. The Typer commands and the ``warcraft`` wrapper both call these.

CurseForge is an experimental provider because the surface is small — one addon lookup plus doctor,
with free-text discovery unsupported — and ``doctor`` reports ``tier: experimental``. The endpoints the
addon lookup uses are confirmed against live traffic, so its payloads carry
``provenance.verified: true``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from warcraft_api.cache import cache_backend_health, redacted_redis_url
from warcraft_core.envelope import Envelope, success_envelope
from warcraft_core.exit_codes import EXIT_USAGE
from warcraft_core.provider import ProviderError, ProviderSurface

from curseforge_cli.auth import (
    API_KEY_ENV,
    PROVIDER_NAME,
    CurseForgeAuthConfig,
    curseforge_provider_env_path,
    load_curseforge_auth_config,
)
from curseforge_cli.client import (
    WOW_GAME_ID,
    CurseForgeClient,
    CurseForgeClientError,
    load_curseforge_cache_settings_from_env,
    verification_note,
)

TIER = "experimental"


def _auth_payload(auth: CurseForgeAuthConfig) -> dict[str, Any]:
    return {
        "required": True,
        "configured": auth.configured,
        "flow": "api_key",
        "key_env": API_KEY_ENV,
        "credential_source": auth.credential_source,
        "lookup_order": [".env.local", curseforge_provider_env_path(), "environment"],
    }


def _cache_payload() -> dict[str, Any]:
    try:
        settings, ttl = load_curseforge_cache_settings_from_env()
    except ValueError as exc:
        # Every read fails on this config, so doctor must not report the provider ready.
        return {"available": False, "error": {"code": "invalid_cache_config", "message": str(exc)}}
    return {
        "enabled": settings.enabled,
        "backend": settings.backend,
        "cache_dir": str(settings.cache_dir),
        "redis_url": redacted_redis_url(settings.redis_url),
        "prefix": settings.prefix,
        "ttls": {"addon": ttl},
        **cache_backend_health(settings),
    }


def doctor_envelope() -> Envelope:
    """Install state, API-key auth posture, and capability metadata; never raises."""
    auth = load_curseforge_auth_config()
    cache = _cache_payload()
    data: dict[str, Any] = {
        # The addon lookup needs the API key, so without one the provider can do nothing useful; a
        # Redis cache that does not answer or a cache config that does not parse also degrades it.
        "status": "ready" if auth.configured and cache.get("available") is not False else "degraded",
        "tier": TIER,
        "installed": True,
        "language": "python",
        "auth": _auth_payload(auth),
        "capabilities": {
            "doctor": "ready",
            "search": "not_supported",
            "resolve": "not_supported",
            "addon": "ready" if auth.configured else "requires_api_key",
        },
        "cache": cache,
        "notes": [
            f"curseforge is an {TIER} provider: the surface is one addon lookup plus doctor, and "
            "free-text search/resolve are unsupported. The endpoints it does use are live-confirmed.",
            "addon lookup returns CurseForge metadata, the latest files, and the latest file's "
            "changelog over the public CurseForge API (x-api-key auth, gameId=1).",
            verification_note(),
            "Free-text discovery is outside the addon lookup scope; use an explicit slug or mod id.",
        ],
    }
    return success_envelope(provider=PROVIDER_NAME, command="doctor", kind="doctor", data=data)


def unsupported_discovery(command: Literal["search", "resolve"], query: str) -> Envelope:
    """Discovery is outside the supported addon lookup surface."""
    raise ProviderError(
        "unsupported_operation", f"CurseForge {command} is unsupported. Use addon <slug-or-id> for an explicit lookup.",
        exit_code=EXIT_USAGE, details={"operation": command, "available_commands": ["addon"]},
    )


def addon_envelope(slug_or_id: str) -> Envelope:
    """Resolve a WoW addon and return its metadata, latest files, and latest changelog.

    Raises ``CurseForgeClientError`` or ``httpx.HTTPError``; the CLI layer turns those into an
    error envelope with the contract exit code.
    """
    try:
        client = CurseForgeClient()
    except ValueError as exc:
        raise CurseForgeClientError("invalid_cache_config", str(exc)) from exc
    try:
        result = client.fetch_addon(slug_or_id)
    finally:
        client.close()
    return success_envelope(
        provider=PROVIDER_NAME,
        command="addon",
        kind="addon",
        query={"addon": slug_or_id, "game_id": WOW_GAME_ID},
        provenance={
            "game_id": WOW_GAME_ID,
            "mod_id": result["mod_id"],
            "slug": result.get("slug"),
            "resolved_by": result["resolved_by"],
            "source_urls": result["source_urls"],
            "verified": True,
            "verification_note": verification_note(),
        },
        data=result["data"],
    )


@dataclass(slots=True)
class CurseForgeProvider:
    """CurseForge search/resolve/doctor surface; free-text search and resolve are unsupported."""

    name: str = PROVIDER_NAME

    def search(self, query: str, *, limit: int = 10, **options: Any) -> Envelope:
        return unsupported_discovery("search", query)

    def resolve(self, target: str, **options: Any) -> Envelope:
        return unsupported_discovery("resolve", target)

    def doctor(self, **options: Any) -> Envelope:
        return doctor_envelope()


PROVIDER: ProviderSurface = CurseForgeProvider()

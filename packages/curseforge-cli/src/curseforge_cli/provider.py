"""Pure CurseForge provider surface. The Typer commands and the ``warcraft`` wrapper both call these.

CurseForge is an experimental provider because the surface is small — one addon lookup plus doctor,
with search/resolve still stubs — and ``doctor`` reports ``tier: experimental``. The endpoints the
addon lookup uses are confirmed against live traffic, so its payloads carry
``provenance.verified: true``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

from warcraft_core.envelope import Envelope, success_envelope, with_legacy_keys
from warcraft_core.provider import ProviderSurface

from curseforge_cli.auth import (
    API_KEY_ENV,
    PROVIDER_NAME,
    CurseForgeAuthConfig,
    curseforge_provider_env_path,
    load_curseforge_auth_config,
)
from curseforge_cli.client import WOW_GAME_ID, CurseForgeClient, verification_note

TIER = "experimental"


def _dual_emit(envelope: Envelope, legacy: Mapping[str, Any]) -> Envelope:
    """Envelope plus the deprecated top-level copies the wrapper and existing agents still read."""
    return cast("Envelope", with_legacy_keys(envelope, legacy))


def _auth_payload(auth: CurseForgeAuthConfig) -> dict[str, Any]:
    return {
        "required": True,
        "configured": auth.configured,
        "flow": "api_key",
        "key_env": API_KEY_ENV,
        "credential_source": auth.credential_source,
        "lookup_order": [".env.local", curseforge_provider_env_path(), "environment"],
    }


def doctor_envelope() -> Envelope:
    """Install state, API-key auth posture, and capability metadata; never raises."""
    auth = load_curseforge_auth_config()
    data: dict[str, Any] = {
        "status": "partial",
        "tier": TIER,
        "installed": True,
        "language": "python",
        "auth": _auth_payload(auth),
        "capabilities": {
            "doctor": "ready",
            "search": "coming_soon",
            "resolve": "coming_soon",
            "addon": "ready",
        },
        "notes": [
            f"curseforge is an {TIER} provider: the surface is one addon lookup plus doctor, and "
            "search/resolve are stubs. The endpoints it does use are live-confirmed.",
            "addon lookup returns CurseForge metadata, the latest files, and the latest file's "
            "changelog over the public CurseForge API (x-api-key auth, gameId=1).",
            verification_note(),
            "search/resolve are not implemented yet (report-style addon lookup is the first slice).",
        ],
    }
    return _dual_emit(success_envelope(provider=PROVIDER_NAME, command="doctor", kind="doctor", data=data), data)


def coming_soon_envelope(command: str, query: str) -> Envelope:
    """Structured stub for an advertised-but-unimplemented surface, so probing it is not a Click error."""
    data: dict[str, Any] = {
        "coming_soon": True,
        "results": [],
        "resolved": False,
        "match": None,
        "message": (
            f"curseforge {command} is not implemented yet; addon lookup by slug or mod id is the "
            "first slice. Use `curseforge addon <slug-or-id>`."
        ),
        "suggested_command": "curseforge addon deadly-boss-mods",
    }
    envelope = success_envelope(
        provider=PROVIDER_NAME,
        command=command,
        kind="coming_soon",
        query={"query": query},
        data=data,
    )
    return _dual_emit(envelope, data)


def addon_envelope(slug_or_id: str) -> Envelope:
    """Resolve a WoW addon and return its metadata, latest files, and latest changelog.

    Raises ``CurseForgeClientError`` or ``httpx.HTTPError``; the CLI layer turns those into an
    error envelope with the contract exit code.
    """
    client = CurseForgeClient()
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
    """CurseForge search/resolve/doctor surface; search and resolve are unimplemented stubs."""

    name: str = PROVIDER_NAME

    def search(self, query: str, *, limit: int = 10, **options: Any) -> Envelope:
        return coming_soon_envelope("search", query)

    def resolve(self, target: str, **options: Any) -> Envelope:
        return coming_soon_envelope("resolve", target)

    def doctor(self, **options: Any) -> Envelope:
        return doctor_envelope()


PROVIDER: ProviderSurface = CurseForgeProvider()

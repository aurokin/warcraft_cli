from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
import typer
from warcraft_core.auth import provider_auth_status
from warcraft_core.output import emit
from warcraft_core.paths import provider_state_path

from blizzard_api_cli.auth import (
    PROVIDER,
    BlizzardAuthConfig,
    blizzard_provider_env_path,
    load_blizzard_auth_config,
)
from blizzard_api_cli.client import (
    CLIENT_CREDENTIALS_STATE_PROVIDER,
    SUPPORTED_REGIONS,
    BlizzardClient,
    BlizzardClientError,
    verification_note,
)

app = typer.Typer(add_completion=False, help="Official Blizzard Battle.net World of Warcraft API CLI.")


@dataclass(slots=True)
class RuntimeConfig:
    pretty: bool = False


def _cfg(ctx: typer.Context) -> RuntimeConfig:
    obj = ctx.obj
    if isinstance(obj, RuntimeConfig):
        return obj
    return RuntimeConfig()


def _emit(ctx: typer.Context, payload: dict[str, Any], *, err: bool = False) -> None:
    emit(payload, pretty=_cfg(ctx).pretty, err=err)


@app.callback()
def main_callback(
    ctx: typer.Context,
    pretty: bool = typer.Option(False, "--pretty", help="Pretty-print JSON output."),
) -> None:
    ctx.obj = RuntimeConfig(pretty=pretty)


def _auth_payload(auth: BlizzardAuthConfig) -> dict[str, Any]:
    state = provider_auth_status(PROVIDER)
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
        "state_path": str(provider_state_path(PROVIDER)),
        # The client-credentials flow caches its token under a distinct provider key, so surface that
        # cache here too — otherwise doctor would report "no token" even after a successful command.
        "token_cache": provider_auth_status(CLIENT_CREDENTIALS_STATE_PROVIDER),
        "token_cache_path": str(provider_state_path(CLIENT_CREDENTIALS_STATE_PROVIDER)),
    }


def _region_payload(auth: BlizzardAuthConfig) -> dict[str, Any]:
    # Region/namespace routing is honored by the Game Data/Profile commands. The hosts, OAuth token
    # URL, and namespace strings follow documented Blizzard API conventions but have not been
    # confirmed against live endpoints in this environment, so verification stays explicit.
    return {
        "configured": auth.region,
        "default": "us",
        "supported_regions": list(SUPPORTED_REGIONS),
        "namespace_classes": ["dynamic", "static", "profile"],
        "routing": "ready",
        "verification": {
            "retail": "pending_live_confirmation",
            "classic": "pending_live_confirmation",
            "note": verification_note(),
        },
    }


@app.command("doctor")
def doctor(ctx: typer.Context) -> None:
    auth = load_blizzard_auth_config()
    _emit(
        ctx,
        {
            "ok": True,
            "provider": PROVIDER,
            "status": "partial",
            "command": "doctor",
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
                "Game Data (realm, item) and Profile (character) commands ship with live OAuth "
                "client-credentials auth and region/namespace routing.",
                verification_note(),
                "Second OAuth validation point for the shared auth architecture (phase 3, docs/architecture/AUTH_ARCHITECTURE.md).",
            ],
        },
    )


_REGION_OPTION = typer.Option(None, "--region", "-r", help="Blizzard region (us, eu, kr, tw, cn). Defaults to BLIZZARD_REGION or us.")
_CLASSIC_OPTION = typer.Option(False, "--classic", help="Shorthand for --game-version classic (classic namespaces are best-effort).")
_GAME_VERSION_OPTION = typer.Option(None, "--game-version", help="Game version to route: retail (default) or classic.")
_LOCALE_OPTION = typer.Option(None, "--locale", help="Locale passed through to Blizzard (default en_US). Not validated.")


def _error_payload(command: str, exc: Exception) -> dict[str, Any]:
    if isinstance(exc, BlizzardClientError):
        code, message = exc.code, exc.message
    elif isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        code = "http_error"
        message = f"Blizzard API returned HTTP {status} for {exc.request.url}."
    else:  # httpx.RequestError (timeouts, connection failures) after retries are exhausted.
        code = "network_error"
        message = f"Blizzard API request failed: {exc}."
    return {
        "ok": False,
        "provider": PROVIDER,
        "command": command,
        "error": {"code": code, "message": message},
    }


def _success_payload(command: str, kind: str, query: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    routing = result["routing"]
    return {
        "ok": True,
        "provider": PROVIDER,
        "command": command,
        "kind": kind,
        "query": query,
        "provenance": {
            "region": routing.region,
            "namespace": routing.namespace,
            "namespace_class": routing.namespace_class,
            "game_version": routing.game_version,
            "locale": routing.locale,
            "source_url": result["source_url"],
            "verified": False,
            "verification_note": verification_note(),
        },
        "data": result["payload"],
    }


def _run_command(
    ctx: typer.Context,
    command: str,
    kind: str,
    query: dict[str, Any],
    call: Callable[[BlizzardClient], dict[str, Any]],
) -> None:
    client = BlizzardClient()
    try:
        result = call(client)
    except (BlizzardClientError, httpx.HTTPError) as exc:
        _emit(ctx, _error_payload(command, exc), err=True)
        raise typer.Exit(1) from exc
    finally:
        client.close()
    _emit(ctx, _success_payload(command, kind, query, result))


@app.command("realm")
def realm(
    ctx: typer.Context,
    slug: str = typer.Argument(..., help="Realm slug, e.g. illidan."),
    region: str | None = _REGION_OPTION,
    classic: bool = _CLASSIC_OPTION,
    game_version: str | None = _GAME_VERSION_OPTION,
    locale: str | None = _LOCALE_OPTION,
) -> None:
    """Fetch a connected-realm-class realm record from the dynamic Game Data namespace."""
    query = {"slug": slug, "region": region, "game_version": game_version, "classic": classic, "locale": locale}
    _run_command(
        ctx,
        "realm",
        "realm",
        query,
        lambda client: client.fetch_realm(slug, region=region, game_version=game_version, classic=classic, locale=locale),
    )


@app.command("item")
def item(
    ctx: typer.Context,
    item_id: int = typer.Argument(..., help="Numeric item id, e.g. 19019."),
    region: str | None = _REGION_OPTION,
    classic: bool = _CLASSIC_OPTION,
    game_version: str | None = _GAME_VERSION_OPTION,
    locale: str | None = _LOCALE_OPTION,
) -> None:
    """Fetch an item record from the static Game Data namespace."""
    query = {"item_id": item_id, "region": region, "game_version": game_version, "classic": classic, "locale": locale}
    _run_command(
        ctx,
        "item",
        "item",
        query,
        lambda client: client.fetch_item(item_id, region=region, game_version=game_version, classic=classic, locale=locale),
    )


@app.command("character")
def character(
    ctx: typer.Context,
    realm_slug: str = typer.Argument(..., help="Realm slug, e.g. illidan."),
    name: str = typer.Argument(..., help="Character name."),
    region: str | None = _REGION_OPTION,
    classic: bool = _CLASSIC_OPTION,
    game_version: str | None = _GAME_VERSION_OPTION,
    locale: str | None = _LOCALE_OPTION,
) -> None:
    """Fetch a character profile from the profile namespace (retail only)."""
    query = {"realm": realm_slug, "name": name, "region": region, "game_version": game_version, "classic": classic, "locale": locale}
    _run_command(
        ctx,
        "character",
        "character",
        query,
        lambda client: client.fetch_character(realm_slug, name, region=region, game_version=game_version, classic=classic, locale=locale),
    )


def run() -> None:
    app()


if __name__ == "__main__":
    run()

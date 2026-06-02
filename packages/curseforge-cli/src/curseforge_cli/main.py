from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
import typer
from warcraft_core.output import emit

from curseforge_cli.auth import (
    API_KEY_ENV,
    PROVIDER,
    CurseForgeAuthConfig,
    curseforge_provider_env_path,
    load_curseforge_auth_config,
)
from curseforge_cli.client import (
    WOW_GAME_ID,
    CurseForgeClient,
    CurseForgeClientError,
    verification_note,
)

app = typer.Typer(add_completion=False, help="Public CurseForge addon API CLI (World of Warcraft).")


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


def _auth_payload(auth: CurseForgeAuthConfig) -> dict[str, Any]:
    return {
        "required": True,
        "configured": auth.configured,
        "flow": "api_key",
        "key_env": API_KEY_ENV,
        "credential_source": auth.credential_source,
        "lookup_order": [".env.local", curseforge_provider_env_path(), "environment"],
    }


@app.command("doctor")
def doctor(ctx: typer.Context) -> None:
    auth = load_curseforge_auth_config()
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
            "capabilities": {
                "doctor": "ready",
                "search": "coming_soon",
                "resolve": "coming_soon",
                "addon": "ready",
            },
            "notes": [
                "addon lookup returns CurseForge metadata, the latest files, and the latest file's "
                "changelog over the public CurseForge API (x-api-key auth, gameId=1).",
                verification_note(),
                "search/resolve are not implemented yet (report-style addon lookup is the first slice).",
            ],
        },
    )


def _error_payload(command: str, exc: Exception) -> dict[str, Any]:
    if isinstance(exc, CurseForgeClientError):
        code, message = exc.code, exc.message
    elif isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        code = "http_error"
        message = f"CurseForge API returned HTTP {status} for {exc.request.url}."
    else:  # httpx.RequestError (timeouts, connection failures) after retries are exhausted.
        code = "network_error"
        message = f"CurseForge API request failed: {exc}."
    return {
        "ok": False,
        "provider": PROVIDER,
        "command": command,
        "error": {"code": code, "message": message},
    }


def _success_payload(command: str, kind: str, query: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": True,
        "provider": PROVIDER,
        "command": command,
        "kind": kind,
        "query": query,
        "provenance": {
            "game_id": WOW_GAME_ID,
            "mod_id": result["mod_id"],
            "slug": result.get("slug"),
            "resolved_by": result["resolved_by"],
            "source_urls": result["source_urls"],
            "verified": False,
            "verification_note": verification_note(),
        },
        "data": result["data"],
    }


def _run_command(
    ctx: typer.Context,
    command: str,
    kind: str,
    query: dict[str, Any],
    call: Callable[[CurseForgeClient], dict[str, Any]],
) -> None:
    client = CurseForgeClient()
    try:
        result = call(client)
    except (CurseForgeClientError, httpx.HTTPError) as exc:
        _emit(ctx, _error_payload(command, exc), err=True)
        raise typer.Exit(1) from exc
    finally:
        client.close()
    _emit(ctx, _success_payload(command, kind, query, result))


@app.command("addon")
def addon(
    ctx: typer.Context,
    slug_or_id: str = typer.Argument(..., help="CurseForge addon slug (e.g. deadly-boss-mods) or numeric mod id."),
) -> None:
    """Fetch a WoW addon's metadata, latest files, and latest changelog by slug or mod id."""
    query = {"addon": slug_or_id, "game_id": WOW_GAME_ID}
    _run_command(ctx, "addon", "addon", query, lambda client: client.fetch_addon(slug_or_id))


def run() -> None:
    app()


if __name__ == "__main__":
    run()

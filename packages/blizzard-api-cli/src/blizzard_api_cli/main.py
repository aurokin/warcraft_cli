from __future__ import annotations

from dataclasses import dataclass
from typing import Any

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

app = typer.Typer(add_completion=False, help="Official Blizzard Battle.net World of Warcraft API CLI (scaffold + doctor).")


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
    }


def _region_payload(auth: BlizzardAuthConfig) -> dict[str, Any]:
    # Region/namespace routing lands with the Game Data/Profile endpoint slice. The scaffold only
    # surfaces a configured region (discovered through the same env-file precedence as credentials)
    # so doctor stays honest about what is wired, rather than implying retail/classic routing yet.
    return {
        "configured": auth.region,
        "routing": "deferred",
        "note": "Region- and namespace-aware endpoint routing is implemented with the Game Data/Profile slice (deferred).",
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
                "game_data": "coming_soon",
                "profile": "coming_soon",
            },
            "notes": [
                "Scaffold slice: doctor and auth posture only. Game Data and Profile endpoints are not implemented yet.",
                "Second OAuth validation point for the shared auth architecture (phase 3, docs/architecture/AUTH_ARCHITECTURE.md).",
            ],
        },
    )


def run() -> None:
    app()


if __name__ == "__main__":
    run()

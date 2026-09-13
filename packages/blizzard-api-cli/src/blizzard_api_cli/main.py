from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

import typer
from warcraft_core.cli import emit, fail, guarded_run, install_common_callback
from warcraft_core.provider import ProviderError

from blizzard_api_cli.client import BlizzardClient
from blizzard_api_cli.provider import PROVIDER, PROVIDER_NAME, fetch

app = typer.Typer(
    add_completion=False,
    help="Official Blizzard Battle.net World of Warcraft API CLI (experimental: endpoints are unverified).",
)
install_common_callback(app, provider=PROVIDER_NAME)


@app.command("doctor")
def doctor(ctx: typer.Context) -> None:
    """Report install state, auth posture, region routing, and capability metadata."""
    emit(ctx, PROVIDER.doctor())


_REGION_OPTION = typer.Option(None, "--region", "-r", help="Blizzard region (us, eu, kr, tw, cn). Defaults to BLIZZARD_REGION or us.")
_CLASSIC_OPTION = typer.Option(False, "--classic", help="Shorthand for --game-version classic (classic namespaces are best-effort).")
_GAME_VERSION_OPTION = typer.Option(None, "--game-version", help="Game version to route: retail (default) or classic.")
_LOCALE_OPTION = typer.Option(None, "--locale", help="Locale passed through to Blizzard (default en_US). Not validated.")


def _run_command(
    ctx: typer.Context,
    command: str,
    kind: str,
    query: Mapping[str, Any],
    call: Callable[[BlizzardClient], dict[str, Any]],
) -> None:
    try:
        payload = fetch(command, kind, query, call)
    except ProviderError as exc:
        fail(ctx, exc.code, exc.message, exit_code=exc.exit_code, details=exc.details)
    emit(ctx, payload)


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
    realm_slug: str = typer.Argument(..., help="Realm slug the character plays on, e.g. illidan."),
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


@app.command("search")
def search(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="Free-text query. Discovery search is not implemented yet."),
    limit: int = typer.Option(5, "--limit", min=1, max=50, help="Unused until blizzard search ships."),
) -> None:
    """Coming soon: free-text discovery search is not implemented yet."""
    emit(ctx, PROVIDER.search(query, limit=limit))


@app.command("resolve")
def resolve(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="Free-text query. Conservative resolution is not implemented yet."),
    limit: int = typer.Option(5, "--limit", min=1, max=50, help="Unused until blizzard resolve ships."),
) -> None:
    """Coming soon: conservative resolution is not implemented yet."""
    emit(ctx, PROVIDER.resolve(query, limit=limit))


def run() -> None:
    guarded_run(app, provider=PROVIDER_NAME)


if __name__ == "__main__":
    run()

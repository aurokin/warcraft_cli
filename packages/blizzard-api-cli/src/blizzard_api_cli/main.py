from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

import typer
from warcraft_core.cli import emit, fail, guarded_run, install_common_callback
from warcraft_core.provider import ProviderError

from blizzard_api_cli.client import CHARACTER_SECTIONS, BlizzardClient, verification_note
from blizzard_api_cli.provider import PROVIDER, PROVIDER_NAME, fetch
from blizzard_api_cli.summaries import COLLECTION_VIEWS

app = typer.Typer(
    add_completion=False,
    # The verification sentence comes from the client so --help, doctor and provenance never
    # disagree about which regions are confirmed.
    help=(
        "Official Blizzard Battle.net World of Warcraft API CLI. Experimental tier: explicit reads "
        "(realm, item, character, PvP seasons/leaderboards/ratings, collections, auction prices); "
        "search/resolve are stubs. " + verification_note()
    ),
)
install_common_callback(app, provider=PROVIDER_NAME)


@app.command("doctor")
def doctor(ctx: typer.Context) -> None:
    """Report install state, auth posture, region routing, and capability metadata."""
    emit(ctx, PROVIDER.doctor())


_REGION_OPTION = typer.Option(None, "--region", "-r", help="Blizzard region (us, eu, kr, tw, cn). Defaults to BLIZZARD_REGION or us.")
_CLASSIC_OPTION = typer.Option(
    False,
    "--classic",
    help="Shorthand for --game-version classic: progression Classic (Mists of Pandaria Classic today), not Era or Anniversary.",
)
_GAME_VERSION_OPTION = typer.Option(
    None,
    "--game-version",
    help="Game version to route: retail (default), classic (progression Classic), classic-era, or classic-anniversary.",
)
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


_REALM_ARGUMENT = typer.Argument(..., help="Realm slug the character plays on, e.g. illidan.")
_NAME_ARGUMENT = typer.Argument(..., help="Character name.")
_REGION_FIRST_NAME_ARGUMENT = typer.Argument(
    None,
    metavar="[NAME]",
    help="With three arguments they are REGION REALM NAME, as raiderio and warcraftlogs take them.",
    show_default=False,
)
_SEASON_OPTION = typer.Option(None, "--season", min=1, help="PvP season id (see `blizzard pvp-season`). Defaults to the current season.")
_ITEM_ID_OPTION = typer.Option(
    None, "--item-id", min=1, help="Only these item ids (repeatable), in the order given; ids with no listing go to not_listed."
)
_AUCTION_LIMIT_OPTION = typer.Option(20, "--limit", min=1, max=500, help="Most-listed items to return when no --item-id is given.")


def _character_args(
    ctx: typer.Context, region: str | None, realm: str, name: str, region_first_name: str | None
) -> tuple[str | None, str, str]:
    """``REALM NAME`` as given, or ``REGION REALM NAME`` shifted into (region, realm, name)."""
    if region_first_name is None:
        return region, realm, name
    if region is not None and region.strip().lower() != realm.strip().lower():
        fail(ctx, "invalid_query", f"The region is given twice: {realm!r} as an argument and {region!r} as --region.")
    return realm, name, region_first_name


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
    realm_slug: str = _REALM_ARGUMENT,
    name: str = _NAME_ARGUMENT,
    region_first_name: str | None = _REGION_FIRST_NAME_ARGUMENT,
    region: str | None = _REGION_OPTION,
    classic: bool = _CLASSIC_OPTION,
    game_version: str | None = _GAME_VERSION_OPTION,
    locale: str | None = _LOCALE_OPTION,
    section: str | None = typer.Option(
        None,
        "--section",
        help=f"Read one sub-resource instead of the profile summary: {', '.join(CHARACTER_SECTIONS)}.",
    ),
) -> None:
    """Fetch a character profile from the profile namespace: REALM NAME --region R, or REGION REALM NAME.

    ``--section`` reads one linked sub-resource instead (PvP ratings, professions, collections),
    which the summary only names as hrefs that need the OAuth token to follow.
    """
    region, realm_slug, name = _character_args(ctx, region, realm_slug, name, region_first_name)
    query = {
        "realm": realm_slug,
        "name": name,
        "region": region,
        "game_version": game_version,
        "classic": classic,
        "locale": locale,
        "section": section,
    }
    _run_command(
        ctx,
        "character",
        "character_section" if section else "character",
        query,
        lambda client: client.fetch_character(
            realm_slug, name, region=region, game_version=game_version, classic=classic, locale=locale, section=section
        ),
    )


@app.command("pvp-season")
def pvp_season(
    ctx: typer.Context,
    season: int | None = typer.Argument(None, min=1, help="PvP season id. Defaults to the current season.", show_default=False),
    region: str | None = _REGION_OPTION,
    classic: bool = _CLASSIC_OPTION,
    game_version: str | None = _GAME_VERSION_OPTION,
    locale: str | None = _LOCALE_OPTION,
) -> None:
    """A PvP season: name, start, every season id, its leaderboard brackets, and its title rating cutoffs."""
    routing = {"region": region, "game_version": game_version, "classic": classic, "locale": locale}
    _run_command(ctx, "pvp-season", "pvp_season", {"season": season, **routing}, lambda client: client.fetch_pvp_season(season, **routing))


@app.command("pvp-leaderboard")
def pvp_leaderboard(
    ctx: typer.Context,
    bracket: str = typer.Argument(
        ..., help="Leaderboard name from `blizzard pvp-season`: 2v2, 3v3, rbg, shuffle-overall, blitz-<class>-<spec>, ..."
    ),
    season: int | None = _SEASON_OPTION,
    limit: int = typer.Option(25, "--limit", min=1, max=5000, help="Top ranks to return (Blizzard publishes up to ~5000)."),
    region: str | None = _REGION_OPTION,
    classic: bool = _CLASSIC_OPTION,
    game_version: str | None = _GAME_VERSION_OPTION,
) -> None:
    """The top ranks of one PvP leaderboard: rank, rating, character, realm, faction, season record."""
    routing = {"region": region, "game_version": game_version, "classic": classic}
    query = {"bracket": bracket, "season": season, "limit": limit, **routing}
    _run_command(
        ctx,
        "pvp-leaderboard",
        "pvp_leaderboard",
        query,
        lambda client: client.fetch_pvp_leaderboard(bracket, season=season, limit=limit, **routing),
    )


@app.command("pvp-character")
def pvp_character(
    ctx: typer.Context,
    realm_slug: str = _REALM_ARGUMENT,
    name: str = _NAME_ARGUMENT,
    region_first_name: str | None = _REGION_FIRST_NAME_ARGUMENT,
    region: str | None = _REGION_OPTION,
    classic: bool = _CLASSIC_OPTION,
    game_version: str | None = _GAME_VERSION_OPTION,
    locale: str | None = _LOCALE_OPTION,
) -> None:
    """A character's honor level, battleground record, and rating and record in every bracket it has played."""
    region, realm_slug, name = _character_args(ctx, region, realm_slug, name, region_first_name)
    routing = {"region": region, "game_version": game_version, "classic": classic, "locale": locale}
    query = {"realm": realm_slug, "name": name, **routing}
    _run_command(ctx, "pvp-character", "pvp_character", query, lambda client: client.fetch_pvp_character(realm_slug, name, **routing))


@app.command("collections")
def collections(
    ctx: typer.Context,
    realm_slug: str = _REALM_ARGUMENT,
    name: str = _NAME_ARGUMENT,
    region_first_name: str | None = _REGION_FIRST_NAME_ARGUMENT,
    kind: list[str] | None = typer.Option(
        None, "--kind", help=f"Collection to read (repeatable): {', '.join(COLLECTION_VIEWS)}. Defaults to all of them."
    ),
    match: str | None = typer.Option(None, "--match", help="Keep only entries whose name contains this text (case-insensitive)."),
    limit: int = typer.Option(20, "--limit", min=1, max=5000, help="Most entries to list per collection, by name."),
    region: str | None = _REGION_OPTION,
    classic: bool = _CLASSIC_OPTION,
    game_version: str | None = _GAME_VERSION_OPTION,
    locale: str | None = _LOCALE_OPTION,
) -> None:
    """A character's mounts, pets, toys, heirlooms and transmog appearances: counts plus a filtered list."""
    region, realm_slug, name = _character_args(ctx, region, realm_slug, name, region_first_name)
    kinds = list(dict.fromkeys(value.strip().lower() for value in kind)) if kind else list(COLLECTION_VIEWS)
    if unknown := [value for value in kinds if value not in COLLECTION_VIEWS]:
        fail(ctx, "invalid_query", f"--kind must be one of: {', '.join(COLLECTION_VIEWS)}; got {', '.join(map(repr, unknown))}.")
    routing = {"region": region, "game_version": game_version, "classic": classic, "locale": locale}
    query = {"realm": realm_slug, "name": name, "kind": kinds, "match": match, "limit": limit, **routing}
    _run_command(
        ctx,
        "collections",
        "collections",
        query,
        lambda client: client.fetch_collections(realm_slug, name, kinds=kinds, match=match, limit=limit, **routing),
    )


@app.command("auctions")
def auctions(
    ctx: typer.Context,
    realm_slug: str = typer.Argument(..., help="Any realm of the connected realm whose auction house to read, e.g. illidan."),
    item_id: list[int] | None = _ITEM_ID_OPTION,
    limit: int = _AUCTION_LIMIT_OPTION,
    region: str | None = _REGION_OPTION,
    classic: bool = _CLASSIC_OPTION,
    game_version: str | None = _GAME_VERSION_OPTION,
) -> None:
    """A connected realm's auction house summarized per item id: listings, units, min and median unit price.

    Retail lists commodities (ore, herbs, reagents) region-wide instead; read them with `commodities`.
    """
    item_ids = item_id or []
    routing = {"region": region, "game_version": game_version, "classic": classic}
    query = {"realm": realm_slug, "item_id": item_ids, "limit": limit, **routing}
    _run_command(
        ctx, "auctions", "auctions", query, lambda client: client.fetch_auctions(realm_slug, item_ids=item_ids, limit=limit, **routing)
    )


@app.command("commodities")
def commodities(
    ctx: typer.Context,
    item_id: list[int] | None = _ITEM_ID_OPTION,
    limit: int = _AUCTION_LIMIT_OPTION,
    region: str | None = _REGION_OPTION,
    classic: bool = _CLASSIC_OPTION,
    game_version: str | None = _GAME_VERSION_OPTION,
) -> None:
    """The region-wide retail commodity market summarized per item id: listings, units, min and median unit price."""
    item_ids = item_id or []
    routing = {"region": region, "game_version": game_version, "classic": classic}
    query = {"item_id": item_ids, "limit": limit, **routing}
    _run_command(
        ctx, "commodities", "commodities", query, lambda client: client.fetch_commodities(item_ids=item_ids, limit=limit, **routing)
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

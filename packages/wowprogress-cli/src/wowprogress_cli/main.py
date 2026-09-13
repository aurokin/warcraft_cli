from __future__ import annotations

from collections.abc import Callable

import typer
from warcraft_core.cli import emit, fail, guarded_run, install_common_callback
from warcraft_core.envelope import Envelope
from warcraft_core.provider import ProviderError

from wowprogress_cli.analytics import GuildProfileFilters
from wowprogress_cli.client import WowProgressClient
from wowprogress_cli.provider import (
    GUILD_PROFILE_METRICS,
    GUILD_PROFILE_THRESHOLD_METRICS,
    LEADERBOARD_METRICS,
    LEADERBOARD_THRESHOLD_METRICS,
    PROVIDER_NAME,
)
from wowprogress_cli.provider import character as provider_character
from wowprogress_cli.provider import distribution_pve_guild_profiles as provider_distribution_guild_profiles
from wowprogress_cli.provider import distribution_pve_leaderboard as provider_distribution_leaderboard
from wowprogress_cli.provider import doctor as provider_doctor
from wowprogress_cli.provider import guild as provider_guild
from wowprogress_cli.provider import guild_history as provider_guild_history
from wowprogress_cli.provider import guild_ranks as provider_guild_ranks
from wowprogress_cli.provider import guild_snapshot as provider_guild_snapshot
from wowprogress_cli.provider import history_trajectory as provider_history_trajectory
from wowprogress_cli.provider import leaderboard as provider_leaderboard
from wowprogress_cli.provider import resolve as provider_resolve
from wowprogress_cli.provider import sample_pve_guild_profiles as provider_sample_guild_profiles
from wowprogress_cli.provider import sample_pve_leaderboard as provider_sample_leaderboard
from wowprogress_cli.provider import search as provider_search
from wowprogress_cli.provider import threshold_pve_guild_profiles as provider_threshold_guild_profiles
from wowprogress_cli.provider import threshold_pve_leaderboard as provider_threshold_leaderboard

# Re-exported so tests and the wrapper can patch client methods through this module.
__all__ = ["WowProgressClient", "app", "run"]

app = typer.Typer(add_completion=False, help="WowProgress rankings and profile CLI.")
install_common_callback(app, provider=PROVIDER_NAME)
sample_app = typer.Typer(add_completion=False, help="Sample-backed WowProgress analytics primitives.")
distribution_app = typer.Typer(add_completion=False, help="Derived distributions built from WowProgress samples.")
threshold_app = typer.Typer(add_completion=False, help="Threshold-style estimates derived from sampled WowProgress leaderboard rows.")
app.add_typer(sample_app, name="sample")
app.add_typer(distribution_app, name="distribution")
app.add_typer(threshold_app, name="threshold")

RegionArgument = typer.Argument(..., help="Region slug such as us or eu.")
RealmArgument = typer.Argument(..., help="Realm slug or title.")
RealmOption = typer.Option(None, "--realm", help="Optional realm slug to narrow the PvE leaderboard.")
FactionOption = typer.Option(None, "--faction", help="Retain only guild profiles matching the given faction. Repeatable.")
DifficultyOption = typer.Option(
    None, "--difficulty", help="Retain only guild profiles matching the given progression difficulty. Repeatable.")
WorldRankMinOption = typer.Option(None, "--world-rank-min", min=1, help="Retain only guild profiles at or above this world rank.")
WorldRankMaxOption = typer.Option(None, "--world-rank-max", min=1, help="Retain only guild profiles at or below this world rank.")
ItemLevelMinOption = typer.Option(None, "--item-level-min", help="Retain only guild profiles at or above this average item level.")
ItemLevelMaxOption = typer.Option(None, "--item-level-max", help="Retain only guild profiles at or below this average item level.")
EncounterOption = typer.Option(None, "--encounter", help="Retain only guild profiles containing the given encounter name. Repeatable.")


def _emit_surface(ctx: typer.Context, build: Callable[[], Envelope]) -> None:
    """Run a pure provider call and turn ``ProviderError`` into the shared error envelope."""
    try:
        payload = build()
    except ProviderError as exc:
        fail(ctx, exc.code, exc.message, exit_code=exc.exit_code, details=exc.details)
    emit(ctx, payload)


def _filters(
    faction: list[str] | None,
    difficulty: list[str] | None,
    world_rank_min: int | None,
    world_rank_max: int | None,
    item_level_min: float | None,
    item_level_max: float | None,
    encounter: list[str] | None,
) -> GuildProfileFilters:
    return GuildProfileFilters(
        faction=list(faction or []),
        difficulty=list(difficulty or []),
        world_rank_min=world_rank_min,
        world_rank_max=world_rank_max,
        item_level_min=item_level_min,
        item_level_max=item_level_max,
        encounter=list(encounter or []),
    )


@app.command("doctor")
def doctor(ctx: typer.Context) -> None:
    """Report WowProgress transport mode, cache configuration, and per-surface capability state."""
    _emit_surface(ctx, provider_doctor)


@app.command("search")
def search(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="Structured query like 'us illidan Liquid' or 'character us illidan Imonthegcd'."),
    limit: int = typer.Option(5, "--limit", min=1, max=50, help="Maximum results to return."),
) -> None:
    """Probe WowProgress guild and character routes for a structured query."""
    _emit_surface(ctx, lambda: provider_search(query, limit=limit))


@app.command("resolve")
def resolve(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="Structured query like 'guild us illidan Liquid' or 'us illidan Imonthegcd'."),
    limit: int = typer.Option(5, "--limit", min=1, max=50, help="Maximum candidates to inspect."),
) -> None:
    """Resolve a structured WowProgress query to one next command when it is unambiguous."""
    _emit_surface(ctx, lambda: provider_resolve(query, limit=limit))


@app.command("guild")
def guild(
    ctx: typer.Context,
    region: str = RegionArgument,
    realm: str = RealmArgument,
    name: str = typer.Argument(..., help="Guild name."),
) -> None:
    """Fetch one WowProgress guild page."""
    _emit_surface(ctx, lambda: provider_guild(region, realm, name))


@app.command("guild-history")
def guild_history(
    ctx: typer.Context,
    region: str = RegionArgument,
    realm: str = RealmArgument,
    name: str = typer.Argument(..., help="Guild name."),
) -> None:
    """Fetch every archived WowProgress tier page for one guild."""
    _emit_surface(ctx, lambda: provider_guild_history(region, realm, name))


@app.command("guild-ranks")
def guild_ranks(
    ctx: typer.Context,
    region: str = RegionArgument,
    realm: str = RealmArgument,
    name: str = typer.Argument(..., help="Guild name."),
) -> None:
    """Report per-tier world, region, and realm ranks for one guild."""
    _emit_surface(ctx, lambda: provider_guild_ranks(region, realm, name))


@app.command("guild-snapshot")
def guild_snapshot(
    ctx: typer.Context,
    region: str = RegionArgument,
    realm: str = RealmArgument,
    name: str = typer.Argument(..., help="Guild name."),
) -> None:
    """Compose current progress, ranks, item level, encounters, and a per-tier rank series."""
    _emit_surface(ctx, lambda: provider_guild_snapshot(region, realm, name))


@app.command("history-trajectory")
def history_trajectory(
    ctx: typer.Context,
    region: str = RegionArgument,
    realm: str = RealmArgument,
    name: str = typer.Argument(..., help="Guild name."),
) -> None:
    """Report per-tier rank and item-level trajectory (oldest -> newest) with tier-over-tier deltas."""
    _emit_surface(ctx, lambda: provider_history_trajectory(region, realm, name))


@app.command("character")
def character(
    ctx: typer.Context,
    region: str = RegionArgument,
    realm: str = RealmArgument,
    name: str = typer.Argument(..., help="Character name."),
) -> None:
    """Fetch one WowProgress character page."""
    _emit_surface(ctx, lambda: provider_character(region, realm, name))


@app.command("leaderboard")
def leaderboard(
    ctx: typer.Context,
    kind: str = typer.Argument(..., help="Leaderboard kind. Phase 1 supports only 'pve'."),
    region: str = typer.Argument(..., help="Region slug such as world, us, or eu."),
    realm: str | None = RealmOption,
    limit: int = typer.Option(25, "--limit", min=1, max=100, help="Maximum leaderboard rows to return."),
) -> None:
    """Fetch one WowProgress PvE leaderboard page."""
    _emit_surface(ctx, lambda: provider_leaderboard(kind, region, realm=realm, limit=limit))


@sample_app.command("pve-leaderboard")
def sample_pve_leaderboard(
    ctx: typer.Context,
    region: str = typer.Option(..., "--region", help="Region slug such as world, us, or eu."),
    realm: str | None = RealmOption,
    limit: int = typer.Option(25, "--limit", min=1, max=100, help="Maximum leaderboard rows to sample."),
) -> None:
    """Sample the top rows of a WowProgress PvE leaderboard with explicit sampling boundaries."""
    _emit_surface(ctx, lambda: provider_sample_leaderboard(region=region, realm=realm, limit=limit))


@distribution_app.command("pve-leaderboard")
def distribution_pve_leaderboard(
    ctx: typer.Context,
    metric: str = typer.Option("progress", "--metric", help=f"Distribution metric: {', '.join(LEADERBOARD_METRICS)}."),
    region: str = typer.Option(..., "--region", help="Region slug such as world, us, or eu."),
    realm: str | None = RealmOption,
    limit: int = typer.Option(50, "--limit", min=1, max=100, help="Maximum leaderboard rows to sample."),
) -> None:
    """Summarize one metric across a sampled WowProgress PvE leaderboard slice."""
    _emit_surface(ctx, lambda: provider_distribution_leaderboard(metric=metric, region=region, realm=realm, limit=limit))


@threshold_app.command("pve-leaderboard")
def threshold_pve_leaderboard(
    ctx: typer.Context,
    metric: str = typer.Option("rank", "--metric", help=f"Threshold metric: {', '.join(LEADERBOARD_THRESHOLD_METRICS)}."),
    value: float = typer.Option(..., "--value", help="Target metric value to estimate against the sampled leaderboard."),
    region: str = typer.Option(..., "--region", help="Region slug such as world, us, or eu."),
    realm: str | None = RealmOption,
    limit: int = typer.Option(50, "--limit", min=1, max=100, help="Maximum leaderboard rows to sample."),
    nearest: int = typer.Option(10, "--nearest", min=1, max=50, help="Maximum nearby rows to include in the threshold estimate."),
) -> None:
    """Estimate where a target metric value sits inside a sampled PvE leaderboard slice."""
    _emit_surface(
        ctx,
        lambda: provider_threshold_leaderboard(
            metric=metric, value=value, region=region, realm=realm, limit=limit, nearest=nearest
        ),
    )


@sample_app.command("pve-guild-profiles")
def sample_pve_guild_profiles(
    ctx: typer.Context,
    region: str = typer.Option(..., "--region", help="Region slug such as world, us, or eu."),
    realm: str | None = RealmOption,
    limit: int = typer.Option(10, "--limit", min=1, max=25, help="Maximum top leaderboard guild profiles to fetch."),
    faction: list[str] | None = FactionOption,
    difficulty: list[str] | None = DifficultyOption,
    world_rank_min: int | None = WorldRankMinOption,
    world_rank_max: int | None = WorldRankMaxOption,
    item_level_min: float | None = ItemLevelMinOption,
    item_level_max: float | None = ItemLevelMaxOption,
    encounter: list[str] | None = EncounterOption,
) -> None:
    """Enrich the top leaderboard rows with their WowProgress guild pages."""
    filters = _filters(faction, difficulty, world_rank_min, world_rank_max, item_level_min, item_level_max, encounter)
    _emit_surface(ctx, lambda: provider_sample_guild_profiles(region=region, realm=realm, limit=limit, filters=filters))


@distribution_app.command("pve-guild-profiles")
def distribution_pve_guild_profiles(
    ctx: typer.Context,
    metric: str = typer.Option("progress", "--metric", help=f"Distribution metric: {', '.join(GUILD_PROFILE_METRICS)}."),
    region: str = typer.Option(..., "--region", help="Region slug such as world, us, or eu."),
    realm: str | None = RealmOption,
    limit: int = typer.Option(10, "--limit", min=1, max=25, help="Maximum top leaderboard guild profiles to fetch."),
    faction: list[str] | None = FactionOption,
    difficulty: list[str] | None = DifficultyOption,
    world_rank_min: int | None = WorldRankMinOption,
    world_rank_max: int | None = WorldRankMaxOption,
    item_level_min: float | None = ItemLevelMinOption,
    item_level_max: float | None = ItemLevelMaxOption,
    encounter: list[str] | None = EncounterOption,
) -> None:
    """Summarize one metric across sampled WowProgress guild profiles."""
    filters = _filters(faction, difficulty, world_rank_min, world_rank_max, item_level_min, item_level_max, encounter)
    _emit_surface(
        ctx,
        lambda: provider_distribution_guild_profiles(metric=metric, region=region, realm=realm, limit=limit, filters=filters),
    )


@threshold_app.command("pve-guild-profiles")
def threshold_pve_guild_profiles(
    ctx: typer.Context,
    metric: str = typer.Option("world_rank", "--metric", help=f"Threshold metric: {', '.join(GUILD_PROFILE_THRESHOLD_METRICS)}."),
    value: float = typer.Option(..., "--value", help="Target metric value to estimate against the sampled guild profiles."),
    region: str = typer.Option(..., "--region", help="Region slug such as world, us, or eu."),
    realm: str | None = RealmOption,
    limit: int = typer.Option(10, "--limit", min=1, max=25, help="Maximum top leaderboard guild profiles to fetch."),
    nearest: int = typer.Option(5, "--nearest", min=1, max=25, help="Maximum nearby guild profiles to include in the threshold estimate."),
    faction: list[str] | None = FactionOption,
    difficulty: list[str] | None = DifficultyOption,
    world_rank_min: int | None = WorldRankMinOption,
    world_rank_max: int | None = WorldRankMaxOption,
    item_level_min: float | None = ItemLevelMinOption,
    item_level_max: float | None = ItemLevelMaxOption,
    encounter: list[str] | None = EncounterOption,
) -> None:
    """Estimate where a target metric value sits inside a sampled guild-profile slice."""
    filters = _filters(faction, difficulty, world_rank_min, world_rank_max, item_level_min, item_level_max, encounter)
    _emit_surface(
        ctx,
        lambda: provider_threshold_guild_profiles(
            metric=metric, value=value, region=region, realm=realm, limit=limit, nearest=nearest, filters=filters
        ),
    )


def run() -> None:
    guarded_run(app, provider=PROVIDER_NAME)


if __name__ == "__main__":
    run()

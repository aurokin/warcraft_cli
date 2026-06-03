from __future__ import annotations

from datetime import UTC, datetime

import typer

from wowprogress_cli.analytics import (
    _distribution_payload,
    _filter_guild_profiles,
    _guild_profile_distribution_payload,
    _guild_profile_sample_summary,
    _guild_profile_threshold_payload,
    _history_trajectory_rows,
    _load_pve_guild_profile_sample,
    _load_pve_leaderboard_sample,
    _sample_summary,
    _threshold_payload,
)
from wowprogress_cli.client import (
    DEFAULT_IMPERSONATE,
    WowProgressClient,
    WowProgressClientError,
    load_wowprogress_cache_settings_from_env,
)
from wowprogress_cli.context import RuntimeConfig, _client, _emit, _fail, _handle_client_error
from wowprogress_cli.identity import _guild_history_tier_row, _guild_ranks_row, _normalized_identity
from wowprogress_cli.search import _resolve_payload, _search_candidates

WowProgressClient = WowProgressClient  # re-export for tests and stable import surface

app = typer.Typer(add_completion=False, help="WowProgress rankings and profile CLI.")
sample_app = typer.Typer(add_completion=False, help="Sample-backed WowProgress analytics primitives.")
distribution_app = typer.Typer(add_completion=False, help="Derived distributions built from WowProgress samples.")
threshold_app = typer.Typer(add_completion=False, help="Threshold-style estimates derived from sampled WowProgress leaderboard rows.")
app.add_typer(sample_app, name="sample")
app.add_typer(distribution_app, name="distribution")
app.add_typer(threshold_app, name="threshold")

# Re-export private helpers used by unit tests (import from main for stability).


@app.callback()
def main_callback(
    ctx: typer.Context,
    pretty: bool = typer.Option(False, "--pretty", help="Pretty-print JSON output."),
) -> None:
    ctx.obj = RuntimeConfig(pretty=pretty)


@app.command("doctor")
def doctor(ctx: typer.Context) -> None:
    try:
        settings, guild_ttl, character_ttl, leaderboard_ttl = load_wowprogress_cache_settings_from_env()
    except ValueError as exc:
        _fail(ctx, "invalid_cache_config", str(exc))
        return
    _emit(
        ctx,
        {
            "provider": "wowprogress",
            "status": "ready",
            "command": "doctor",
            "installed": True,
            "language": "python",
            "auth": {
                "required": False,
                "deferred": True,
            },
            "transport": {
                "mode": "browser_fingerprint_http",
                "impersonate": DEFAULT_IMPERSONATE,
            },
            "capabilities": {
                "search": "ready",
                "resolve": "ready",
                "guild": "ready",
                "guild_history": "ready",
                "guild_ranks": "ready",
                "guild_snapshot": "ready",
                "history_trajectory": "ready",
                "character": "ready",
                "leaderboard": "ready",
                "sample_pve_leaderboard": "ready",
                "distribution_pve_leaderboard": "ready",
                "threshold_pve_leaderboard": "ready",
                "sample_pve_guild_profiles": "ready",
                "distribution_pve_guild_profiles": "ready",
                "threshold_pve_guild_profiles": "ready",
            },
            "cache": {
                "enabled": settings.enabled,
                "backend": settings.backend,
                "cache_dir": str(settings.cache_dir),
                "redis_url": settings.redis_url,
                "prefix": settings.prefix,
                "ttls": {
                    "guild_page": guild_ttl,
                    "character_page": character_ttl,
                    "leaderboard_page": leaderboard_ttl,
                },
            },
        },
    )


@app.command("search")
def search(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="Structured query like 'us illidan Liquid' or 'character us illidan Imonthegcd'."),
    limit: int = typer.Option(5, "--limit", min=1, max=50, help="Maximum results to return."),
) -> None:
    _emit(ctx, _search_candidates(ctx, query, limit=limit))


@app.command("resolve")
def resolve(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="Structured query like 'guild us illidan Liquid' or 'us illidan Imonthegcd'."),
    limit: int = typer.Option(5, "--limit", min=1, max=50, help="Maximum candidates to inspect."),
) -> None:
    _emit(ctx, _resolve_payload(_search_candidates(ctx, query, limit=limit)))


@app.command("guild")
def guild(
    ctx: typer.Context,
    region: str = typer.Argument(..., help="Region slug such as us or eu."),
    realm: str = typer.Argument(..., help="Realm slug or title."),
    name: str = typer.Argument(..., help="Guild name."),
) -> None:
    normalized = _normalized_identity(region, realm, name)
    try:
        with _client(ctx) as client:
            payload = client.fetch_guild_page_variants(**normalized)
    except WowProgressClientError as exc:
        _handle_client_error(ctx, exc)
        return
    _emit(ctx, payload)


@app.command("guild-history")
def guild_history(
    ctx: typer.Context,
    region: str = typer.Argument(..., help="Region slug such as us or eu."),
    realm: str = typer.Argument(..., help="Realm slug or title."),
    name: str = typer.Argument(..., help="Guild name."),
) -> None:
    normalized = _normalized_identity(region, realm, name)
    try:
        with _client(ctx) as client:
            payload = client.fetch_guild_history(**normalized)
    except WowProgressClientError as exc:
        _handle_client_error(ctx, exc)
        return
    history = payload.get("history") if isinstance(payload.get("history"), list) else []
    _emit(
        ctx,
        {
            **payload,
            "query": normalized,
            "count": len(history),
            "tiers": [_guild_history_tier_row(row) for row in history if isinstance(row, dict)],
        },
    )


@app.command("guild-ranks")
def guild_ranks(
    ctx: typer.Context,
    region: str = typer.Argument(..., help="Region slug such as us or eu."),
    realm: str = typer.Argument(..., help="Realm slug or title."),
    name: str = typer.Argument(..., help="Guild name."),
) -> None:
    normalized = _normalized_identity(region, realm, name)
    try:
        with _client(ctx) as client:
            payload = client.fetch_guild_history(**normalized)
    except WowProgressClientError as exc:
        _handle_client_error(ctx, exc)
        return
    history = payload.get("history") if isinstance(payload.get("history"), list) else []
    _emit(
        ctx,
        {
            "provider": "wowprogress",
            "kind": "guild_ranks",
            "query": normalized,
            "guild": payload.get("guild"),
            "count": len(history),
            "tiers": [_guild_ranks_row(row) for row in history if isinstance(row, dict)],
            "citations": payload.get("citations"),
        },
    )


def _guild_freshness(client: WowProgressClient) -> dict[str, object]:
    # cache_ttl_seconds is null when caching is disabled (WOWPROGRESS_CACHE_BACKEND=none) so the
    # block never claims a TTL that is not actually applied.
    return {
        "sampled_at": datetime.now(UTC).isoformat(),
        "cache_ttl_seconds": client.guild_page_ttl_seconds if client.cache_enabled else None,
    }


@app.command("guild-snapshot")
def guild_snapshot(
    ctx: typer.Context,
    region: str = typer.Argument(..., help="Region slug such as us or eu."),
    realm: str = typer.Argument(..., help="Realm slug or title."),
    name: str = typer.Argument(..., help="Guild name."),
) -> None:
    """Single normalized snapshot: current progress + ranks + item level + encounter summary,
    plus a per-tier rank series. Built from one guild-history traversal (no extra guild-page fetch
    in the command layer)."""
    normalized = _normalized_identity(region, realm, name)
    try:
        with _client(ctx) as client:
            payload = client.fetch_guild_history(**normalized)
            freshness = _guild_freshness(client)
    except WowProgressClientError as exc:
        _handle_client_error(ctx, exc)
        return
    history = payload.get("history") if isinstance(payload.get("history"), list) else []
    rows = [row for row in history if isinstance(row, dict)]
    _emit(
        ctx,
        {
            "provider": "wowprogress",
            "kind": "guild_snapshot",
            "query": normalized,
            "guild": payload.get("guild"),
            "progress": payload.get("current_progress"),
            # Current-state values from the main guild page (survive an empty history series).
            "item_level": payload.get("current_item_level"),
            "encounters": payload.get("current_encounters"),
            "rank_series": [_guild_ranks_row(row) for row in rows],
            "citations": payload.get("citations"),
            "freshness": freshness,
        },
    )


@app.command("history-trajectory")
def history_trajectory(
    ctx: typer.Context,
    region: str = typer.Argument(..., help="Region slug such as us or eu."),
    realm: str = typer.Argument(..., help="Realm slug or title."),
    name: str = typer.Argument(..., help="Guild name."),
) -> None:
    """Per-tier rank + item-level trajectory (oldest -> newest) with tier-over-tier deltas."""
    normalized = _normalized_identity(region, realm, name)
    try:
        with _client(ctx) as client:
            payload = client.fetch_guild_history(**normalized)
            freshness = _guild_freshness(client)
    except WowProgressClientError as exc:
        _handle_client_error(ctx, exc)
        return
    history = payload.get("history") if isinstance(payload.get("history"), list) else []
    tiers = _history_trajectory_rows(history)
    _emit(
        ctx,
        {
            "provider": "wowprogress",
            "kind": "history_trajectory",
            "query": normalized,
            "guild": payload.get("guild"),
            "count": len(tiers),
            "tiers": tiers,
            "notes": [
                "Each tier row is the guild's final-for-tier snapshot (source-native WowProgress tier "
                "pages), not a live or in-progress value.",
                "delta_vs_previous compares consecutive tiers, which are different raids/difficulties; "
                "treat it as descriptive movement, not a normalized skill metric. A rank is 'improved' "
                "when its world/region/realm number is lower (better).",
            ],
            "citations": payload.get("citations"),
            "freshness": freshness,
        },
    )


@app.command("character")
def character(
    ctx: typer.Context,
    region: str = typer.Argument(..., help="Region slug such as us or eu."),
    realm: str = typer.Argument(..., help="Realm slug or title."),
    name: str = typer.Argument(..., help="Character name."),
) -> None:
    normalized = _normalized_identity(region, realm, name)
    try:
        with _client(ctx) as client:
            payload = client.fetch_character_page_variants(**normalized)
    except WowProgressClientError as exc:
        _handle_client_error(ctx, exc)
        return
    _emit(ctx, payload)


@app.command("leaderboard")
def leaderboard(
    ctx: typer.Context,
    kind: str = typer.Argument(..., help="Leaderboard kind. Phase 1 supports only 'pve'."),
    region: str = typer.Argument(..., help="Region slug such as world, us, or eu."),
    realm: str | None = typer.Option(None, "--realm", help="Optional realm slug to narrow the PvE leaderboard."),
    limit: int = typer.Option(25, "--limit", min=1, max=100, help="Maximum leaderboard rows to return."),
) -> None:
    if kind.lower() != "pve":
        _fail(ctx, "invalid_query", "WowProgress phase 1 supports only the 'pve' leaderboard.")
        return
    try:
        with _client(ctx) as client:
            payload = client.fetch_pve_leaderboard(region=region, realm=realm, limit=limit)
    except WowProgressClientError as exc:
        _handle_client_error(ctx, exc)
        return
    _emit(ctx, payload)


@sample_app.command("pve-leaderboard")
def sample_pve_leaderboard(
    ctx: typer.Context,
    region: str = typer.Option(..., "--region", help="Region slug such as world, us, or eu."),
    realm: str | None = typer.Option(None, "--realm", help="Optional realm slug to narrow the PvE leaderboard."),
    limit: int = typer.Option(25, "--limit", min=1, max=100, help="Maximum leaderboard rows to sample."),
) -> None:
    try:
        with _client(ctx) as client:
            entries, meta, leaderboard, query = _load_pve_leaderboard_sample(client, region=region, realm=realm, limit=limit)
    except WowProgressClientError as exc:
        _handle_client_error(ctx, exc)
        return
    _emit(
        ctx,
        {
            "provider": "wowprogress",
            "kind": "pve_leaderboard_sample",
            "query": query,
            "leaderboard": leaderboard,
            "sample": _sample_summary(entries, meta=meta),
            "entries": entries,
            "freshness": {
                "sampled_at": meta["sampled_at"],
                "cache_ttl_seconds": meta["cache_ttl_seconds"],
            },
            "citations": {
                "leaderboard_page": meta["page_url"],
            },
        },
    )


@distribution_app.command("pve-leaderboard")
def distribution_pve_leaderboard(
    ctx: typer.Context,
    metric: str = typer.Option("progress", "--metric", help="Distribution metric: progress, difficulty, realm, bosses_killed, rank."),
    region: str = typer.Option(..., "--region", help="Region slug such as world, us, or eu."),
    realm: str | None = typer.Option(None, "--realm", help="Optional realm slug to narrow the PvE leaderboard."),
    limit: int = typer.Option(50, "--limit", min=1, max=100, help="Maximum leaderboard rows to sample."),
) -> None:
    if metric not in {"progress", "difficulty", "realm", "bosses_killed", "rank"}:
        _fail(ctx, "invalid_query", "--metric must be one of: progress, difficulty, realm, bosses_killed, rank")
        return
    try:
        with _client(ctx) as client:
            entries, meta, _leaderboard, query = _load_pve_leaderboard_sample(client, region=region, realm=realm, limit=limit)
    except WowProgressClientError as exc:
        _handle_client_error(ctx, exc)
        return
    _emit(
        ctx,
        _distribution_payload(
            metric,
            entries,
            meta=meta,
            query=query,
        ),
    )


@threshold_app.command("pve-leaderboard")
def threshold_pve_leaderboard(
    ctx: typer.Context,
    metric: str = typer.Option("rank", "--metric", help="Threshold metric: rank or bosses_killed."),
    value: float = typer.Option(..., "--value", help="Target metric value to estimate against the sampled leaderboard."),
    region: str = typer.Option(..., "--region", help="Region slug such as world, us, or eu."),
    realm: str | None = typer.Option(None, "--realm", help="Optional realm slug to narrow the PvE leaderboard."),
    limit: int = typer.Option(50, "--limit", min=1, max=100, help="Maximum leaderboard rows to sample."),
    nearest: int = typer.Option(10, "--nearest", min=1, max=50, help="Maximum nearby rows to include in the threshold estimate."),
) -> None:
    if metric not in {"rank", "bosses_killed"}:
        _fail(ctx, "invalid_query", "--metric must be one of: rank, bosses_killed")
        return
    try:
        with _client(ctx) as client:
            entries, meta, _leaderboard, query = _load_pve_leaderboard_sample(client, region=region, realm=realm, limit=limit)
    except WowProgressClientError as exc:
        _handle_client_error(ctx, exc)
        return
    _emit(
        ctx,
        _threshold_payload(
            metric,
            value,
            entries,
            meta=meta,
            query=query,
            nearest_limit=nearest,
        ),
    )


@sample_app.command("pve-guild-profiles")
def sample_pve_guild_profiles(
    ctx: typer.Context,
    region: str = typer.Option(..., "--region", help="Region slug such as world, us, or eu."),
    realm: str | None = typer.Option(None, "--realm", help="Optional realm slug to narrow the PvE leaderboard."),
    limit: int = typer.Option(10, "--limit", min=1, max=25, help="Maximum top leaderboard guild profiles to fetch."),
    faction: list[str] | None = typer.Option(None, "--faction", help="Retain only guild profiles matching the given faction. Repeatable."),
    difficulty: list[str] | None = typer.Option(
        None, "--difficulty", help="Retain only guild profiles matching the given progression difficulty. Repeatable."),
    world_rank_min: int | None = typer.Option(None, "--world-rank-min", min=1,
                                              help="Retain only guild profiles at or above this world rank."),
    world_rank_max: int | None = typer.Option(None, "--world-rank-max", min=1,
                                              help="Retain only guild profiles at or below this world rank."),
    item_level_min: float | None = typer.Option(
        None, "--item-level-min", help="Retain only guild profiles at or above this average item level."),
    item_level_max: float | None = typer.Option(
        None, "--item-level-max", help="Retain only guild profiles at or below this average item level."),
    encounter: list[str] | None = typer.Option(
        None, "--encounter", help="Retain only guild profiles containing the given encounter name. Repeatable."),
) -> None:
    try:
        with _client(ctx) as client:
            entries, meta, leaderboard, query = _load_pve_guild_profile_sample(client, region=region, realm=realm, limit=limit)
    except WowProgressClientError as exc:
        _handle_client_error(ctx, exc)
        return
    entries, filtering = _filter_guild_profiles(
        entries,
        faction=faction,
        difficulty=difficulty,
        world_rank_min=world_rank_min,
        world_rank_max=world_rank_max,
        item_level_min=item_level_min,
        item_level_max=item_level_max,
        encounter=encounter,
    )
    _emit(
        ctx,
        {
            "provider": "wowprogress",
            "kind": "pve_guild_profiles_sample",
            "query": {
                **query,
                "filters": {
                    "faction": filtering["faction"],
                    "difficulty": filtering["difficulty"],
                    "world_rank_min": filtering["world_rank_min"],
                    "world_rank_max": filtering["world_rank_max"],
                    "item_level_min": filtering["item_level_min"],
                    "item_level_max": filtering["item_level_max"],
                    "encounter": filtering["encounter"],
                },
            },
            "leaderboard": leaderboard,
            "sample": _guild_profile_sample_summary(entries, meta=meta, filtering=filtering),
            "guild_profiles": entries,
            "freshness": {
                "sampled_at": meta["sampled_at"],
                "cache_ttl_seconds": meta["cache_ttl_seconds"],
            },
            "citations": {
                "leaderboard_page": meta["page_url"],
            },
        },
    )


@distribution_app.command("pve-guild-profiles")
def distribution_pve_guild_profiles(
    ctx: typer.Context,
    metric: str = typer.Option("progress", "--metric",
                               help="Distribution metric: progress, faction, item_level_average, world_rank, encounter."),
    region: str = typer.Option(..., "--region", help="Region slug such as world, us, or eu."),
    realm: str | None = typer.Option(None, "--realm", help="Optional realm slug to narrow the PvE leaderboard."),
    limit: int = typer.Option(10, "--limit", min=1, max=25, help="Maximum top leaderboard guild profiles to fetch."),
    faction: list[str] | None = typer.Option(None, "--faction", help="Retain only guild profiles matching the given faction. Repeatable."),
    difficulty: list[str] | None = typer.Option(
        None, "--difficulty", help="Retain only guild profiles matching the given progression difficulty. Repeatable."),
    world_rank_min: int | None = typer.Option(None, "--world-rank-min", min=1,
                                              help="Retain only guild profiles at or above this world rank."),
    world_rank_max: int | None = typer.Option(None, "--world-rank-max", min=1,
                                              help="Retain only guild profiles at or below this world rank."),
    item_level_min: float | None = typer.Option(
        None, "--item-level-min", help="Retain only guild profiles at or above this average item level."),
    item_level_max: float | None = typer.Option(
        None, "--item-level-max", help="Retain only guild profiles at or below this average item level."),
    encounter: list[str] | None = typer.Option(
        None, "--encounter", help="Retain only guild profiles containing the given encounter name. Repeatable."),
) -> None:
    if metric not in {"progress", "faction", "item_level_average", "world_rank", "encounter"}:
        _fail(ctx, "invalid_query", "--metric must be one of: progress, faction, item_level_average, world_rank, encounter")
        return
    try:
        with _client(ctx) as client:
            entries, meta, _leaderboard, query = _load_pve_guild_profile_sample(client, region=region, realm=realm, limit=limit)
    except WowProgressClientError as exc:
        _handle_client_error(ctx, exc)
        return
    entries, filtering = _filter_guild_profiles(
        entries,
        faction=faction,
        difficulty=difficulty,
        world_rank_min=world_rank_min,
        world_rank_max=world_rank_max,
        item_level_min=item_level_min,
        item_level_max=item_level_max,
        encounter=encounter,
    )
    _emit(
        ctx,
        _guild_profile_distribution_payload(
            metric,
            entries,
            meta=meta,
            query={
                **query,
                "filters": {
                    "faction": filtering["faction"],
                    "difficulty": filtering["difficulty"],
                    "world_rank_min": filtering["world_rank_min"],
                    "world_rank_max": filtering["world_rank_max"],
                    "item_level_min": filtering["item_level_min"],
                    "item_level_max": filtering["item_level_max"],
                    "encounter": filtering["encounter"],
                },
            },
            filtering=filtering,
        ),
    )


@threshold_app.command("pve-guild-profiles")
def threshold_pve_guild_profiles(
    ctx: typer.Context,
    metric: str = typer.Option("world_rank", "--metric", help="Threshold metric: world_rank or item_level_average."),
    value: float = typer.Option(..., "--value", help="Target metric value to estimate against the sampled guild profiles."),
    region: str = typer.Option(..., "--region", help="Region slug such as world, us, or eu."),
    realm: str | None = typer.Option(None, "--realm", help="Optional realm slug to narrow the PvE leaderboard."),
    limit: int = typer.Option(10, "--limit", min=1, max=25, help="Maximum top leaderboard guild profiles to fetch."),
    nearest: int = typer.Option(5, "--nearest", min=1, max=25, help="Maximum nearby guild profiles to include in the threshold estimate."),
    faction: list[str] | None = typer.Option(None, "--faction", help="Retain only guild profiles matching the given faction. Repeatable."),
    difficulty: list[str] | None = typer.Option(
        None, "--difficulty", help="Retain only guild profiles matching the given progression difficulty. Repeatable."),
    world_rank_min: int | None = typer.Option(None, "--world-rank-min", min=1,
                                              help="Retain only guild profiles at or above this world rank."),
    world_rank_max: int | None = typer.Option(None, "--world-rank-max", min=1,
                                              help="Retain only guild profiles at or below this world rank."),
    item_level_min: float | None = typer.Option(
        None, "--item-level-min", help="Retain only guild profiles at or above this average item level."),
    item_level_max: float | None = typer.Option(
        None, "--item-level-max", help="Retain only guild profiles at or below this average item level."),
    encounter: list[str] | None = typer.Option(
        None, "--encounter", help="Retain only guild profiles containing the given encounter name. Repeatable."),
) -> None:
    if metric not in {"world_rank", "item_level_average"}:
        _fail(ctx, "invalid_query", "--metric must be one of: world_rank, item_level_average")
        return
    try:
        with _client(ctx) as client:
            entries, meta, _leaderboard, query = _load_pve_guild_profile_sample(client, region=region, realm=realm, limit=limit)
    except WowProgressClientError as exc:
        _handle_client_error(ctx, exc)
        return
    entries, filtering = _filter_guild_profiles(
        entries,
        faction=faction,
        difficulty=difficulty,
        world_rank_min=world_rank_min,
        world_rank_max=world_rank_max,
        item_level_min=item_level_min,
        item_level_max=item_level_max,
        encounter=encounter,
    )
    _emit(
        ctx,
        _guild_profile_threshold_payload(
            metric,
            value,
            entries,
            meta=meta,
            query={
                **query,
                "filters": {
                    "faction": filtering["faction"],
                    "difficulty": filtering["difficulty"],
                    "world_rank_min": filtering["world_rank_min"],
                    "world_rank_max": filtering["world_rank_max"],
                    "item_level_min": filtering["item_level_min"],
                    "item_level_max": filtering["item_level_max"],
                    "encounter": filtering["encounter"],
                },
            },
            nearest_limit=nearest,
            filtering=filtering,
        ),
    )


def run() -> None:
    app()


if __name__ == "__main__":
    run()

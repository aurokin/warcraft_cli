from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import httpx
import typer
from warcraft_core.cli import command_path, emit, fail, guarded_run, install_common_callback
from warcraft_core.provider import ProviderError
from warcraft_core.shapes import as_dict, as_list

from raiderio_cli.analytics import (
    analytics_query,
    citations_payload,
    distribution_payload,
    freshness_payload,
    limit_player_snapshots,
    load_filtered_runs,
    player_distribution_payload,
    player_sample_summary,
    player_snapshots,
    resolve_season_input,
    response_season,
    run_filters,
    sample_leaderboard_runs,
    sample_request,
    sample_summary,
    threshold_payload,
    unknown_season_error,
    validated_metric,
)
from raiderio_cli.client import RAIDERIO_BASE_URL, FetchedJson, page_freshness, validated_region
from raiderio_cli.profiles import character_profile, guild_profile
from raiderio_cli.provider import (
    PROVIDER,
    PROVIDER_NAME,
    open_client,
    raiderio_envelope,
    transport_errors,
)
from raiderio_cli.raids import raid_catalog_payload, sample_raid_rankings, validated_raid_scope

app = typer.Typer(add_completion=False, help="Raider.IO profile and leaderboard CLI.")
sample_app = typer.Typer(add_completion=False, help="Sample-backed Raider.IO analytics primitives.")
distribution_app = typer.Typer(add_completion=False, help="Derived distributions built from Raider.IO samples.")
threshold_app = typer.Typer(add_completion=False, help="Threshold-style estimates derived from sampled Raider.IO runs.")
leaderboard_app = typer.Typer(add_completion=False, help="Season- and raid-scoped Raider.IO leaderboard views.")
app.add_typer(sample_app, name="sample")
app.add_typer(distribution_app, name="distribution")
app.add_typer(threshold_app, name="threshold")
app.add_typer(leaderboard_app, name="leaderboard")
install_common_callback(app, provider=PROVIDER_NAME)

RUN_DISTRIBUTION_METRICS = ("mythic_level", "dungeon", "role", "player_region", "class", "spec", "composition", "class_composition")
PLAYER_DISTRIBUTION_METRICS = ("appearance_count", "top_mythic_level", "class", "spec", "role", "player_region")
THRESHOLD_METRICS = ("score", "mythic_level")

# Scope and filter options shared by the leaderboard and every sampled Mythic+ command.
_SEASON = typer.Option("", "--season", help="Season slug, or 'current' (the default) for the Raider.IO current season.")
_REGION = typer.Option("world", "--region", help="world, us, eu, kr, tw, cn, or an alias such as na.")
_DUNGEON = typer.Option("all", "--dungeon", help="Dungeon slug or all.")
_AFFIXES = typer.Option("", "--affixes", help="Affix slug, fortified, tyrannical, current, or all.")
_PAGE = typer.Option(0, "--page", min=0, help="0-based 20-run page to start from: 0 = ranks 1-20, 1 = ranks 21-40.")
_PAGES = typer.Option(None, "--pages", min=1, max=10, help="Most 20-run pages to read. Defaults to as many as --limit needs.")
_EXPANSION_HELP = "Raider.IO expansion id: 11 = Midnight, 10 = The War Within, 9 = Dragonflight (Warcraft Logs uses 7 for Midnight)."
_SAMPLE_LIMIT = typer.Option(100, "--limit", min=1, max=200, help="Maximum runs to retain in the sample.")
_PLAYER_LIMIT = typer.Option(100, "--player-limit", min=1, max=500, help="Maximum player snapshots to retain after deduping.")
_LEVEL_MIN = typer.Option(None, "--level-min", min=0, help="Retain only runs at or above this Mythic+ level.")
_LEVEL_MAX = typer.Option(None, "--level-max", min=0, help="Retain only runs at or below this Mythic+ level.")
_SCORE_MIN = typer.Option(None, "--score-min", help="Retain only runs at or above this sampled run score.")
_SCORE_MAX = typer.Option(None, "--score-max", help="Retain only runs at or below this sampled run score.")
_CONTAINS_ROLE = typer.Option(
    None, "--contains-role", help="Retain only runs containing at least one of these roles (tank, healer, dps). Repeatable."
)
_CONTAINS_CLASS = typer.Option(None, "--contains-class", help="Retain only runs containing at least one class slug or name. Repeatable.")
_CONTAINS_SPEC = typer.Option(None, "--contains-spec", help="Retain only runs containing at least one spec slug or name. Repeatable.")
_PLAYER_REGION = typer.Option(
    None, "--player-region", help="Retain only runs containing at least one player from the given region. Repeatable."
)


@contextmanager
def _command_errors(ctx: typer.Context) -> Iterator[None]:
    """Run a command body, turning its failures into the shared error envelope.

    Wrapping the command body is required even though ``guarded_run`` exists: the ``warcraft``
    wrapper and the tests invoke this Typer app directly and never pass through ``run()``.
    """
    try:
        with transport_errors():
            yield
    except ProviderError as exc:
        fail(ctx, exc.code, exc.message, exit_code=exc.exit_code, details=exc.details)


@app.command("doctor")
def doctor(ctx: typer.Context) -> None:
    """Report Raider.IO readiness, per-command capabilities, and resolved cache settings."""
    with _command_errors(ctx):
        emit(ctx, PROVIDER.doctor())


@app.command("search")
def search(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="Free-text character or guild query."),
    limit: int = typer.Option(5, "--limit", min=1, max=50, help="Maximum results to return."),
    kind: str = typer.Option("all", "--kind", help="Optional result kind: all, character, or guild."),
) -> None:
    """Rank Raider.IO character and guild candidates for a free-text query."""
    with _command_errors(ctx):
        emit(ctx, PROVIDER.search(query, limit=limit, kind=kind))


@app.command("resolve")
def resolve(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="Free-text character or guild query."),
    limit: int = typer.Option(5, "--limit", min=1, max=50, help="Maximum candidates to return."),
    kind: str = typer.Option("all", "--kind", help="Optional result kind: all, character, or guild."),
) -> None:
    """Resolve a free-text query to one Raider.IO entity plus the follow-up command to run."""
    with _command_errors(ctx):
        emit(ctx, PROVIDER.resolve(query, limit=limit, kind=kind))


@app.command("character")
def character(
    ctx: typer.Context,
    region: str = typer.Argument(..., help="Region slug such as us or eu."),
    realm: str = typer.Argument(..., help="Realm slug or title."),
    name: str = typer.Argument(..., help="Character name."),
) -> None:
    """Return a character profile with identity, guild, Mythic+ score, and raid progression."""
    with _command_errors(ctx):
        emit(ctx, character_profile(region, realm, name))


@app.command("guild")
def guild(
    ctx: typer.Context,
    region: str = typer.Argument(..., help="Region slug such as us or eu."),
    realm: str = typer.Argument(..., help="Realm slug or title."),
    name: str = typer.Argument(..., help="Guild name."),
    roster_limit: int = typer.Option(
        10, "--roster-limit", min=0, max=1000, help="Roster members to return, highest guild rank first (Raider.IO tracks up to 1000)."
    ),
) -> None:
    """Return a guild profile with raid progression, raid rankings, and the roster by guild rank."""
    with _command_errors(ctx):
        emit(ctx, guild_profile(region, realm, name, roster_limit=roster_limit))


@leaderboard_app.command("mythic-plus")
def leaderboard_mythic_plus(
    ctx: typer.Context,
    season: str = _SEASON,
    region: str = _REGION,
    dungeon: str = _DUNGEON,
    affixes: str = _AFFIXES,
    page: int = _PAGE,
    limit: int = typer.Option(20, "--limit", min=1, max=200, help="Maximum leaderboard rows to return."),
) -> None:
    """Return the season-scoped top Mythic+ runs with sampling freshness and citations.

    Emits the explicit ``resolved_season`` plus sampled freshness and leaderboard citations so the
    rows are provenance-safe. It fetches as many ranking pages as ``--limit`` requires and reports
    returned-vs-requested counts so a short provider response is explicit, not a silent cap.
    """
    with _command_errors(ctx), open_client() as client:
        request = sample_request(season=season, region=region, dungeon=dungeon, affixes=affixes, page=page, pages=None, limit=limit)
        runs, meta = sample_leaderboard_runs(client, request)
    emit(
        ctx,
        raiderio_envelope(
            command=command_path(ctx),
            kind="mythic_plus_leaderboard",
            payload={
                "query": {
                    "season": meta.get("season") or season or None,
                    "resolved_season": meta.get("season") or request.season_param,
                    "region": request.region,
                    "dungeon": dungeon,
                    "affixes": affixes or None,
                    "page": page,
                    "limit": limit,
                },
                "count": len(runs),
                "sample": {
                    "requested_limit": limit,
                    "returned_run_count": len(runs),
                    "pages_requested": meta["pages_requested"],
                    "pages_fetched": meta["pages_fetched"],
                    "duplicates_removed": meta["duplicates_removed"],
                    # False => the provider ran out of ranked runs before --limit (not a silent cap).
                    "limit_reached": len(runs) >= limit,
                },
                "runs": runs,
                "freshness": freshness_payload(meta),
                "citations": citations_payload(meta),
            },
        ),
    )


@leaderboard_app.command("raids")
def leaderboard_raids(
    ctx: typer.Context,
    raid: str = typer.Option(..., "--raid", help="Raid slug from `raiderio raids`, such as liberation-of-undermine."),
    difficulty: str = typer.Option("mythic", "--difficulty", help="normal, heroic, or mythic."),
    region: str = typer.Option("world", "--region", help="world, us, eu, kr, tw, cn, or an alias such as na."),
    realm: str = typer.Option("", "--realm", help="Realm slug or display name to narrow to (requires a standard --region)."),
    page: int = typer.Option(0, "--page", min=0, help="0-based 20-row page to start from: 0 = ranks 1-20, 1 = ranks 21-40."),
    limit: int = typer.Option(20, "--limit", min=1, max=200, help="Maximum guild rows to return."),
) -> None:
    """Return the guild raid rankings for one raid and difficulty with freshness and citations.

    Replaces the retired WowProgress guild leaderboards. ``rank`` is relative to the requested
    scope (realm position when ``--realm`` is set); ``region_rank`` is always region-wide. It fetches
    as many 20-row pages as ``--limit`` requires and reports returned-vs-requested counts.
    """
    with _command_errors(ctx), open_client() as client:
        difficulty, region, realm_slug = validated_raid_scope(difficulty=difficulty, region=region, realm=realm)
        rows, meta = sample_raid_rankings(
            client,
            raid=raid.strip(),
            difficulty=difficulty,
            region=region,
            realm=realm_slug,
            page=page,
            limit=limit,
        )
    emit(
        ctx,
        raiderio_envelope(
            command=command_path(ctx),
            kind="raid_leaderboard",
            payload={
                "query": {
                    "raid": raid.strip(),
                    "difficulty": difficulty,
                    "region": region,
                    "realm": realm_slug,
                    "page": page,
                    "limit": limit,
                },
                "count": len(rows),
                "sample": {
                    "requested_limit": limit,
                    "returned_row_count": len(rows),
                    "pages_requested": meta["pages_requested"],
                    "pages_fetched": meta["pages_fetched"],
                    # False => the provider ran out of ranked guilds before --limit (not a silent cap).
                    "limit_reached": len(rows) >= limit,
                },
                "rows": rows,
                "freshness": freshness_payload(meta),
                "citations": citations_payload(meta),
            },
        ),
    )


@app.command("raids")
def raids(
    ctx: typer.Context,
    expansion_id: int = typer.Option(
        11, "--expansion-id", min=1, help=_EXPANSION_HELP
    ),
) -> None:
    """List the raid slugs (and encounter slugs) Raider.IO knows for one expansion.

    Each row carries the per-region ``starts``/``ends`` timestamps, so the raid a guild is currently
    progressing is the one whose window covers now.
    """
    with _command_errors(ctx), open_client() as client:
        static_data = client.raid_static_data(expansion_id=expansion_id)
        payload = raid_catalog_payload(
            static_data, expansion_id=expansion_id, cache_ttl_seconds=client.static_data_ttl_seconds
        )
    emit(ctx, raiderio_envelope(command=command_path(ctx), kind="raid_catalog", payload=payload))


@app.command("affixes")
def affixes(
    ctx: typer.Context,
    region: str = typer.Option("us", "--region", help="us, eu, kr, tw, cn, or an alias such as na."),
) -> None:
    """Return this week's Mythic+ affixes in one region."""
    with _command_errors(ctx), open_client() as client:
        region = validated_region(region, allowed=("us", "eu", "kr", "tw", "cn"))
        fetched = client.mythic_plus_affixes(region=region)
        body = fetched.payload
        payload = {
            "query": {"region": region},
            "title": body.get("title"),
            "affixes": [
                {key: row.get(key) for key in ("id", "name", "description", "wowhead_url")}
                for row in as_list(body.get("affix_details"))
                if isinstance(row, dict)
            ],
            "freshness": page_freshness(fetched, cache_ttl_seconds=client.mythic_plus_runs_ttl_seconds),
            "citations": {
                "affixes_url": f"{RAIDERIO_BASE_URL}/mythic-plus/affixes?region={region}&locale=en",
                "leaderboard": body.get("leaderboard_url"),
            },
        }
    emit(ctx, raiderio_envelope(command=command_path(ctx), kind="mythic_plus_affixes", payload=payload))


@app.command("dungeons")
def dungeons(
    ctx: typer.Context,
    expansion_id: int = typer.Option(
        11, "--expansion-id", min=1, help=_EXPANSION_HELP
    ),
) -> None:
    """List the Mythic+ seasons Raider.IO knows for one expansion, each with its dungeon pool and slugs.

    Seasons come newest first, with per-region ``starts``/``ends``: the current pool is the main
    season whose window covers now. The dungeon slugs are what ``--dungeon`` takes.
    """
    with _command_errors(ctx), open_client() as client:
        fetched = client.mythic_plus_static_data(expansion_id=expansion_id)
        seasons = [
            {
                "slug": season.get("slug"),
                "name": season.get("name"),
                "is_main_season": season.get("is_main_season"),
                "starts": as_dict(season.get("starts")),
                "ends": as_dict(season.get("ends")),
                "dungeons": [
                    {key: dungeon.get(key) for key in ("slug", "name", "short_name", "keystone_timer_seconds")}
                    for dungeon in as_list(season.get("dungeons"))
                    if isinstance(dungeon, dict)
                ],
            }
            for season in as_list(fetched.payload.get("seasons"))
            if isinstance(season, dict)
        ]
        if not seasons:
            # Raider.IO answers an id it has no Mythic+ data for with an empty list, not an error.
            raise ProviderError(
                "invalid_query",
                f"Raider.IO has no Mythic+ seasons for expansion id {expansion_id}. It numbers expansions "
                "11 = Midnight, 10 = The War Within, 9 = Dragonflight (Warcraft Logs numbers them differently).",
            )
        payload = {
            "query": {"expansion_id": expansion_id},
            "count": len(seasons),
            "seasons": seasons,
            "freshness": page_freshness(fetched, cache_ttl_seconds=client.static_data_ttl_seconds),
            "citations": {"static_data_url": f"{RAIDERIO_BASE_URL}/mythic-plus/static-data?expansion_id={expansion_id}"},
        }
    emit(ctx, raiderio_envelope(command=command_path(ctx), kind="mythic_plus_dungeons", payload=payload))


# Raider.IO's season-cutoffs keys, highest percentile first.
_CUTOFF_PERCENTILES = (("p999", "top 0.1%"), ("p990", "top 1%"), ("p900", "top 10%"), ("p750", "top 25%"), ("p600", "top 40%"))


def _cutoff_population(block: Any) -> dict[str, Any] | None:
    block = as_dict(block)
    if not block:
        return None
    return {
        "rating": block.get("quantileMinValue"),
        "population_count": block.get("quantilePopulationCount"),
        "total_population": block.get("totalPopulationCount"),
    }


def _iso_updated_at(value: Any) -> Any:
    """Raider.IO's JavaScript ``Date`` string (``Sat Oct 03 2026 10:36:16 GMT+0000 (...)``) as ISO-8601 UTC.

    A value in any other shape is passed through unchanged rather than dropped.
    """
    try:
        parsed = datetime.strptime(str(value).split(" (", 1)[0], "%a %b %d %Y %H:%M:%S GMT%z")
    except ValueError:
        return value
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _cutoffs_payload(fetched: FetchedJson, *, season: str, region: str, cache_ttl_seconds: int) -> dict[str, Any]:
    """The ``raiderio cutoffs`` payload: the lowest rating inside each top percentile, overall and per faction."""
    cutoffs = as_dict(fetched.payload.get("cutoffs"))
    rows = [
        {
            "percentile": label,
            "quantile": as_dict(as_dict(cutoffs.get(key)).get("all")).get("quantile"),
            "all": _cutoff_population(as_dict(cutoffs.get(key)).get("all")),
            "horde": _cutoff_population(as_dict(cutoffs.get(key)).get("horde")),
            "alliance": _cutoff_population(as_dict(cutoffs.get(key)).get("alliance")),
        }
        for key, label in _CUTOFF_PERCENTILES
        if isinstance(cutoffs.get(key), dict)
    ]
    return {
        "query": {"season": season, "region": region},
        "updated_at": _iso_updated_at(cutoffs.get("updatedAt")),
        "count": len(rows),
        "cutoffs": rows,
        "freshness": page_freshness(fetched, cache_ttl_seconds=cache_ttl_seconds),
        "citations": {"cutoffs_url": f"{RAIDERIO_BASE_URL}/mythic-plus/season-cutoffs?season={season}&region={region}"},
    }


@app.command("cutoffs")
def cutoffs(
    ctx: typer.Context,
    season: str = _SEASON,
    region: str = typer.Option("us", "--region", help="us, eu, kr, tw, cn, or an alias such as na."),
) -> None:
    """Return the Mythic+ rating it takes to be in the top 0.1%, 1%, 10%, 25% and 40% of one region, per faction.

    This is player rating (the profile's Mythic+ score), unlike ``threshold``, which works on single runs.
    """
    with _command_errors(ctx), open_client() as client:
        region = validated_region(region, allowed=("us", "eu", "kr", "tw", "cn"))
        # The endpoint has no current-season default, so the current slug comes from a leaderboard page.
        season_slug = resolve_season_input(season) or response_season(client.mythic_plus_runs().payload)
        if not season_slug:
            raise ProviderError("upstream_error", "Raider.IO did not name its current season; pass --season.")
        try:
            fetched = client.season_cutoffs(season=season_slug, region=region)
        except httpx.HTTPStatusError as exc:
            # Raider.IO answers an unknown season here with HTTP 404 "Could not find data for season <slug>".
            if exc.response.status_code != 404:
                raise
            raise unknown_season_error(season_slug, exc) from exc
        payload = _cutoffs_payload(fetched, season=season_slug, region=region, cache_ttl_seconds=client.mythic_plus_runs_ttl_seconds)
    emit(ctx, raiderio_envelope(command=command_path(ctx), kind="mythic_plus_cutoffs", payload=payload))


@sample_app.command("mythic-plus-runs")
def sample_mythic_plus_runs(
    ctx: typer.Context,
    season: str = _SEASON,
    region: str = _REGION,
    dungeon: str = _DUNGEON,
    affixes: str = _AFFIXES,
    page: int = _PAGE,
    pages: int | None = _PAGES,
    limit: int = _SAMPLE_LIMIT,
    level_min: int | None = _LEVEL_MIN,
    level_max: int | None = _LEVEL_MAX,
    score_min: float | None = _SCORE_MIN,
    score_max: float | None = _SCORE_MAX,
    contains_role: list[str] | None = _CONTAINS_ROLE,
    contains_class: list[str] | None = _CONTAINS_CLASS,
    contains_spec: list[str] | None = _CONTAINS_SPEC,
    player_region: list[str] | None = _PLAYER_REGION,
) -> None:
    """Return a filtered sample of Mythic+ leaderboard runs with sampling counts and citations."""
    with _command_errors(ctx), open_client() as client:
        request = sample_request(season=season, region=region, dungeon=dungeon, affixes=affixes, page=page, pages=pages, limit=limit)
        filters = run_filters(
            level_min=level_min, level_max=level_max, score_min=score_min, score_max=score_max, contains_role=contains_role,
            contains_class=contains_class, contains_spec=contains_spec, player_region=player_region,
        )
        runs, meta, filtering = load_filtered_runs(client, request, filters)
    emit(
        ctx,
        raiderio_envelope(
            command=command_path(ctx),
            kind="mythic_plus_runs_sample",
            payload={
                "query": analytics_query(request, filters, meta=meta),
                "sample": {**sample_summary(runs, meta=meta), "filtering": filtering},
                "runs": runs,
                "freshness": freshness_payload(meta),
                "citations": citations_payload(meta),
            },
        ),
    )


@sample_app.command("mythic-plus-players")
def sample_mythic_plus_players(
    ctx: typer.Context,
    season: str = _SEASON,
    region: str = _REGION,
    dungeon: str = _DUNGEON,
    affixes: str = _AFFIXES,
    page: int = _PAGE,
    pages: int | None = _PAGES,
    limit: int = _SAMPLE_LIMIT,
    player_limit: int = _PLAYER_LIMIT,
    level_min: int | None = _LEVEL_MIN,
    level_max: int | None = _LEVEL_MAX,
    score_min: float | None = _SCORE_MIN,
    score_max: float | None = _SCORE_MAX,
    contains_role: list[str] | None = _CONTAINS_ROLE,
    contains_class: list[str] | None = _CONTAINS_CLASS,
    contains_spec: list[str] | None = _CONTAINS_SPEC,
    player_region: list[str] | None = _PLAYER_REGION,
) -> None:
    """Return deduped player snapshots built from a filtered sample of Mythic+ runs."""
    with _command_errors(ctx), open_client() as client:
        request = sample_request(season=season, region=region, dungeon=dungeon, affixes=affixes, page=page, pages=pages, limit=limit)
        filters = run_filters(
            level_min=level_min, level_max=level_max, score_min=score_min, score_max=score_max, contains_role=contains_role,
            contains_class=contains_class, contains_spec=contains_spec, player_region=player_region,
        )
        runs, meta, filtering = load_filtered_runs(client, request, filters)
    players, player_sampling = limit_player_snapshots(player_snapshots(runs), player_limit=player_limit)
    emit(
        ctx,
        raiderio_envelope(
            command=command_path(ctx),
            kind="mythic_plus_players_sample",
            payload={
                "query": analytics_query(request, filters, meta=meta, player_limit=player_limit),
                "sample": player_sample_summary(
                    players, runs=runs, meta=meta, filtering=filtering, player_sampling=player_sampling
                ),
                "players": players,
                "freshness": freshness_payload(meta),
                "citations": citations_payload(meta),
            },
        ),
    )


@distribution_app.command("mythic-plus-runs")
def distribution_mythic_plus_runs(
    ctx: typer.Context,
    metric: str = typer.Option(
        "mythic_level", "--metric", help=f"Distribution metric: {', '.join(RUN_DISTRIBUTION_METRICS)}."
    ),
    season: str = _SEASON,
    region: str = _REGION,
    dungeon: str = _DUNGEON,
    affixes: str = _AFFIXES,
    page: int = _PAGE,
    pages: int | None = _PAGES,
    limit: int = _SAMPLE_LIMIT,
    level_min: int | None = _LEVEL_MIN,
    level_max: int | None = _LEVEL_MAX,
    score_min: float | None = _SCORE_MIN,
    score_max: float | None = _SCORE_MAX,
    contains_role: list[str] | None = _CONTAINS_ROLE,
    contains_class: list[str] | None = _CONTAINS_CLASS,
    contains_spec: list[str] | None = _CONTAINS_SPEC,
    player_region: list[str] | None = _PLAYER_REGION,
) -> None:
    """Return a run-level distribution of the sampled runs over one --metric."""
    with _command_errors(ctx):
        metric = validated_metric(metric, RUN_DISTRIBUTION_METRICS)
        request = sample_request(season=season, region=region, dungeon=dungeon, affixes=affixes, page=page, pages=pages, limit=limit)
        filters = run_filters(
            level_min=level_min, level_max=level_max, score_min=score_min, score_max=score_max, contains_role=contains_role,
            contains_class=contains_class, contains_spec=contains_spec, player_region=player_region,
        )
        with open_client() as client:
            runs, meta, filtering = load_filtered_runs(client, request, filters)
    payload = distribution_payload(metric, runs, meta=meta, query=analytics_query(request, filters, meta=meta))
    payload["sample"]["filtering"] = filtering
    emit(ctx, raiderio_envelope(command=command_path(ctx), kind="mythic_plus_runs_distribution", payload=payload))


@distribution_app.command("mythic-plus-players")
def distribution_mythic_plus_players(
    ctx: typer.Context,
    metric: str = typer.Option(
        "appearance_count", "--metric", help=f"Distribution metric: {', '.join(PLAYER_DISTRIBUTION_METRICS)}."
    ),
    season: str = _SEASON,
    region: str = _REGION,
    dungeon: str = _DUNGEON,
    affixes: str = _AFFIXES,
    page: int = _PAGE,
    pages: int | None = _PAGES,
    limit: int = _SAMPLE_LIMIT,
    player_limit: int = _PLAYER_LIMIT,
    level_min: int | None = _LEVEL_MIN,
    level_max: int | None = _LEVEL_MAX,
    score_min: float | None = _SCORE_MIN,
    score_max: float | None = _SCORE_MAX,
    contains_role: list[str] | None = _CONTAINS_ROLE,
    contains_class: list[str] | None = _CONTAINS_CLASS,
    contains_spec: list[str] | None = _CONTAINS_SPEC,
    player_region: list[str] | None = _PLAYER_REGION,
) -> None:
    """Return a player-level distribution of the sampled participants over one --metric."""
    with _command_errors(ctx):
        metric = validated_metric(metric, PLAYER_DISTRIBUTION_METRICS)
        request = sample_request(season=season, region=region, dungeon=dungeon, affixes=affixes, page=page, pages=pages, limit=limit)
        filters = run_filters(
            level_min=level_min, level_max=level_max, score_min=score_min, score_max=score_max, contains_role=contains_role,
            contains_class=contains_class, contains_spec=contains_spec, player_region=player_region,
        )
        with open_client() as client:
            runs, meta, filtering = load_filtered_runs(client, request, filters)
    players, player_sampling = limit_player_snapshots(player_snapshots(runs), player_limit=player_limit)
    payload = player_distribution_payload(
        metric,
        players,
        runs=runs,
        meta=meta,
        query=analytics_query(request, filters, meta=meta, player_limit=player_limit),
        filtering=filtering,
        player_sampling=player_sampling,
    )
    emit(ctx, raiderio_envelope(command=command_path(ctx), kind="mythic_plus_players_distribution", payload=payload))


@threshold_app.command("mythic-plus-runs")
def threshold_mythic_plus_runs(
    ctx: typer.Context,
    metric: str = typer.Option("score", "--metric", help=f"Threshold metric: {', '.join(THRESHOLD_METRICS)}."),
    value: float = typer.Option(..., "--value", help="Target metric value to estimate around."),
    season: str = _SEASON,
    region: str = _REGION,
    dungeon: str = _DUNGEON,
    affixes: str = _AFFIXES,
    page: int = _PAGE,
    pages: int | None = _PAGES,
    limit: int = _SAMPLE_LIMIT,
    nearest: int = typer.Option(10, "--nearest", min=1, max=50, help="Number of nearest sampled runs to retain."),
    level_min: int | None = _LEVEL_MIN,
    level_max: int | None = _LEVEL_MAX,
    score_min: float | None = _SCORE_MIN,
    score_max: float | None = _SCORE_MAX,
    contains_role: list[str] | None = _CONTAINS_ROLE,
    contains_class: list[str] | None = _CONTAINS_CLASS,
    contains_spec: list[str] | None = _CONTAINS_SPEC,
    player_region: list[str] | None = _PLAYER_REGION,
) -> None:
    """Estimate the sampled runs nearest a target score or Mythic+ level."""
    with _command_errors(ctx):
        metric = validated_metric(metric, THRESHOLD_METRICS)
        request = sample_request(season=season, region=region, dungeon=dungeon, affixes=affixes, page=page, pages=pages, limit=limit)
        filters = run_filters(
            level_min=level_min, level_max=level_max, score_min=score_min, score_max=score_max, contains_role=contains_role,
            contains_class=contains_class, contains_spec=contains_spec, player_region=player_region,
        )
        with open_client() as client:
            runs, meta, filtering = load_filtered_runs(client, request, filters)
    payload = threshold_payload(
        metric,
        value,
        runs,
        meta=meta,
        query=analytics_query(request, filters, meta=meta, nearest=nearest),
        nearest_limit=nearest,
    )
    payload["sample"]["filtering"] = filtering
    emit(ctx, raiderio_envelope(command=command_path(ctx), kind="mythic_plus_runs_threshold", payload=payload))


def run() -> None:
    guarded_run(app, provider=PROVIDER_NAME)


if __name__ == "__main__":
    run()

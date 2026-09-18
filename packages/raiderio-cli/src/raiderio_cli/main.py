from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import typer
from warcraft_core.cli import emit, fail, guarded_run, install_common_callback
from warcraft_core.identity import IdentityConfidence, class_spec_identity_payload
from warcraft_core.provider import ProviderError
from warcraft_core.shapes import as_dict, as_list

from raiderio_cli.analytics import (
    SampleRequest,
    analytics_query,
    citations_payload,
    distribution_payload,
    freshness_payload,
    leaderboard_pages_for_limit,
    limit_player_snapshots,
    load_filtered_runs,
    player_distribution_payload,
    player_sample_summary,
    player_snapshots,
    ranking_run_summary,
    resolve_season_input,
    response_season,
    run_filters,
    sample_leaderboard_runs,
    sample_summary,
    threshold_payload,
)
from raiderio_cli.identity import raiderio_class_spec_identity
from raiderio_cli.provider import (
    PROVIDER,
    PROVIDER_NAME,
    open_client,
    raiderio_envelope,
    transport_errors,
)
from raiderio_cli.raids import raid_catalog_rows, sample_raid_rankings, validated_raid_scope

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


@contextmanager
def _command_errors(ctx: typer.Context) -> Iterator[None]:
    """Turn provider and transport failures into the shared error envelope instead of a traceback.

    Wrapping the command body is required even though ``guarded_run`` exists: the ``warcraft``
    wrapper and the tests invoke this Typer app directly and never pass through ``run()``.
    """
    try:
        with transport_errors():
            yield
    except ProviderError as exc:
        fail(ctx, exc.code, exc.message, exit_code=exc.exit_code, details=exc.details)


def _raid_progression_summary(progress: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raid_slug, row in sorted(progress.items()):
        if not isinstance(row, dict):
            continue
        rows.append(
            {
                "raid_slug": raid_slug,
                "summary": row.get("summary") or "",
                "total_bosses": row.get("total_bosses"),
                "normal_bosses_killed": row.get("normal_bosses_killed"),
                "heroic_bosses_killed": row.get("heroic_bosses_killed"),
                "mythic_bosses_killed": row.get("mythic_bosses_killed"),
            }
        )
    return rows


def _guild_rankings_summary(rankings: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raid_slug, row in sorted(rankings.items()):
        if not isinstance(row, dict):
            continue
        rows.append(
            {
                "raid_slug": raid_slug,
                "normal": row.get("normal"),
                "heroic": row.get("heroic"),
                "mythic": row.get("mythic"),
            }
        )
    return rows


def _recent_run_summary(row: dict[str, Any]) -> dict[str, Any]:
    dungeon = as_dict(row.get("dungeon"))
    return {
        "mythic_level": row.get("mythic_level"),
        "dungeon": dungeon.get("name"),
        "dungeon_slug": dungeon.get("slug"),
        "completed_at": row.get("completed_at"),
        "num_chests": row.get("num_chests"),
        "clear_time_ms": row.get("clear_time_ms"),
        "keystone_time_ms": row.get("keystone_time_ms"),
    }


def _character_identity(profile: dict[str, Any]) -> dict[str, Any]:
    """Summarize the character's identity block, including the normalized class/spec sibling."""
    class_value = profile.get("class")
    spec_value = profile.get("active_spec_name")
    actor_class = class_value if isinstance(class_value, str) and class_value.strip() else None
    active_spec = spec_value if isinstance(spec_value, str) and spec_value.strip() else None
    # Only the fully-resolved (class + spec) profile is a high-confidence identity; partial or
    # missing source data must not advertise high confidence to downstream handoff ranking.
    confidence: IdentityConfidence = "high" if actor_class and active_spec else "none"
    return {
        "name": profile.get("name"),
        "region": profile.get("region"),
        "realm": profile.get("realm"),
        "race": profile.get("race"),
        "class_name": class_value,
        "active_spec_name": spec_value,
        "class_spec_identity": class_spec_identity_payload(
            actor_class=actor_class,
            spec=active_spec,
            provider=PROVIDER_NAME,
            source="character_profile",
            confidence=confidence,
        ),
        "faction": profile.get("faction"),
        "profile_url": profile.get("profile_url"),
        "thumbnail_url": profile.get("thumbnail_url"),
    }


def _character_mythic_plus(profile: dict[str, Any]) -> dict[str, Any]:
    """Summarize current-season Mythic+ score, ranks, and the most recent runs."""
    recent_runs = as_list(profile.get("mythic_plus_recent_runs"))
    scores = as_list(profile.get("mythic_plus_scores_by_season"))
    current = as_dict(scores[0]) if scores else {}
    return {
        "season": current.get("season"),
        "current_score": as_dict(current.get("scores")).get("all"),
        "current_score_color": as_dict(as_dict(current.get("segments")).get("all")).get("color"),
        "ranks": profile.get("mythic_plus_ranks"),
        "recent_run_count": len(recent_runs),
        "recent_runs": [_recent_run_summary(row) for row in recent_runs[:5] if isinstance(row, dict)],
    }


def _character_payload(profile: dict[str, Any]) -> dict[str, Any]:
    """Build the ``raiderio character`` payload from a Raider.IO character profile."""
    guild = as_dict(profile.get("guild"))
    raid_rows = _raid_progression_summary(as_dict(profile.get("raid_progression")))
    return {
        "character": _character_identity(profile),
        "guild": {
            "name": guild.get("name"),
            "realm": guild.get("realm"),
            "region": guild.get("region"),
        }
        if guild
        else None,
        "mythic_plus": _character_mythic_plus(profile),
        "raiding": {
            "raid_count": len(raid_rows),
            "progression": raid_rows,
        },
        "citations": {
            "profile": profile.get("profile_url"),
        },
    }


def _guild_roster_preview(members: list[Any]) -> list[dict[str, Any]]:
    """Return the first ten roster entries with normalized class/spec identity."""
    preview: list[dict[str, Any]] = []
    for row in members[:10]:
        character = as_dict(as_dict(row).get("character"))
        preview.append(
            {
                "name": character.get("name"),
                "realm": character.get("realm"),
                "class_name": character.get("class"),
                "active_spec_name": character.get("active_spec_name"),
                "class_spec_identity": raiderio_class_spec_identity(
                    character.get("class"),
                    character.get("active_spec_name"),
                    source="guild_roster_preview",
                ),
            }
        )
    return preview


def _guild_payload(profile: dict[str, Any]) -> dict[str, Any]:
    """Build the ``raiderio guild`` payload from a Raider.IO guild profile."""
    members = as_list(profile.get("members"))
    raid_progression = _raid_progression_summary(as_dict(profile.get("raid_progression")))
    raid_rankings = _guild_rankings_summary(as_dict(profile.get("raid_rankings")))
    return {
        "guild": {
            "name": profile.get("name"),
            "region": profile.get("region"),
            "realm": profile.get("realm"),
            "faction": profile.get("faction"),
            "profile_url": profile.get("profile_url"),
            "member_count": len(members),
        },
        "raiding": {
            "raid_count": len(raid_progression),
            "progression": raid_progression,
            "rankings": raid_rankings,
        },
        "roster_preview": _guild_roster_preview(members),
        "citations": {
            "profile": profile.get("profile_url"),
        },
    }


@app.command("doctor")
def doctor(ctx: typer.Context) -> None:
    """Report Raider.IO readiness, per-command capabilities, and resolved cache settings."""
    payload = PROVIDER.doctor()
    error = payload.get("error")
    if error is not None:
        fail(ctx, error["code"], error["message"])
    emit(ctx, payload)


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
    with _command_errors(ctx), open_client() as client:
        profile = client.character_profile_variants(region=region, realm=realm, name=name)
    emit(ctx, raiderio_envelope(command="character", kind="character_profile", payload=_character_payload(profile)))


@app.command("guild")
def guild(
    ctx: typer.Context,
    region: str = typer.Argument(..., help="Region slug such as us or eu."),
    realm: str = typer.Argument(..., help="Realm slug or title."),
    name: str = typer.Argument(..., help="Guild name."),
) -> None:
    """Return a guild profile with raid progression, raid rankings, and a roster preview."""
    with _command_errors(ctx), open_client() as client:
        profile = client.guild_profile_variants(region=region, realm=realm, name=name)
    emit(ctx, raiderio_envelope(command="guild", kind="guild_profile", payload=_guild_payload(profile)))


@app.command("mythic-plus-runs")
def mythic_plus_runs(
    ctx: typer.Context,
    season: str = typer.Option("", "--season", help="Season slug. Defaults to Raider.IO current default season."),
    region: str = typer.Option("world", "--region", help="Region slug such as world, us, or eu."),
    dungeon: str = typer.Option("all", "--dungeon", help="Dungeon slug or all."),
    affixes: str = typer.Option("", "--affixes", help="Affix slug, fortified, tyrannical, current, or all."),
    page: int = typer.Option(0, "--page", min=0, help="Page of rankings to request."),
) -> None:
    """Return one page of the Mythic+ run leaderboard for a region and dungeon."""
    with _command_errors(ctx), open_client() as client:
        payload = client.mythic_plus_runs(
            season=resolve_season_input(season),
            region=region,
            dungeon=dungeon,
            affixes=affixes or None,
            page=page,
        )
    rankings = as_list(payload.get("rankings"))
    served_season = response_season(payload)
    emit(
        ctx,
        raiderio_envelope(
            command="mythic-plus-runs",
            kind="mythic_plus_runs",
            payload={
                "query": {
                    "season": served_season or season or None,
                    "resolved_season": served_season or resolve_season_input(season),
                    "region": payload.get("region") or region,
                    "dungeon": payload.get("dungeon") or dungeon,
                    "affixes": affixes or None,
                    "page": page,
                },
                "count": len(rankings),
                "runs": [ranking_run_summary(row) for row in rankings if isinstance(row, dict)],
            },
        ),
    )


@leaderboard_app.command("mythic-plus")
def leaderboard_mythic_plus(
    ctx: typer.Context,
    season: str = typer.Option("", "--season", help="Season slug, or 'current' for the Raider.IO current default season."),
    region: str = typer.Option("world", "--region", help="Region slug such as world, us, or eu."),
    dungeon: str = typer.Option("all", "--dungeon", help="Dungeon slug or all."),
    affixes: str = typer.Option("", "--affixes", help="Affix slug, fortified, tyrannical, current, or all."),
    page: int = typer.Option(0, "--page", min=0, help="Page of rankings to request."),
    limit: int = typer.Option(20, "--limit", min=1, max=200, help="Maximum leaderboard rows to return."),
) -> None:
    """Return the season-scoped top Mythic+ runs with sampling freshness and citations.

    A thin view over the same sampled-run primitive used by ``sample`` / ``distribution`` --
    emits the explicit ``resolved_season`` plus sampled freshness and leaderboard citations so
    the rows are provenance-safe. It fetches as many ranking pages as ``--limit`` requires and
    reports returned-vs-requested counts so a short provider response is explicit, not a silent cap.
    """
    pages = leaderboard_pages_for_limit(limit)
    with _command_errors(ctx), open_client() as client:
        runs, meta = sample_leaderboard_runs(
            client,
            season=resolve_season_input(season),
            region=region,
            dungeon=dungeon,
            affixes=affixes or None,
            page=page,
            pages=pages,
            limit=limit,
        )
    emit(
        ctx,
        raiderio_envelope(
            command="mythic-plus",
            kind="mythic_plus_leaderboard",
            payload={
                "query": {
                    "season": meta.get("season") or season or None,
                    "resolved_season": meta.get("season") or resolve_season_input(season),
                    "region": region,
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
    region: str = typer.Option("world", "--region", help="world, us, eu, kr, tw, or cn."),
    realm: str = typer.Option("", "--realm", help="Realm slug to narrow to (requires a standard --region)."),
    page: int = typer.Option(0, "--page", min=0, help="20-row page of rankings to start from."),
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
            command="raids",
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
        11, "--expansion-id", min=1, help="Expansion id: 11 = Midnight, 10 = The War Within, 9 = Dragonflight."
    ),
) -> None:
    """List the raid slugs (and encounter slugs) Raider.IO knows for one expansion."""
    with _command_errors(ctx), open_client() as client:
        payload = client.raid_static_data(expansion_id=expansion_id)
    rows = raid_catalog_rows(payload)
    emit(
        ctx,
        raiderio_envelope(
            command="raids",
            kind="raid_catalog",
            payload={"query": {"expansion_id": expansion_id}, "count": len(rows), "rows": rows},
        ),
    )


@sample_app.command("mythic-plus-runs")
def sample_mythic_plus_runs(
    ctx: typer.Context,
    season: str = typer.Option("", "--season", help="Season slug. Defaults to Raider.IO current default season."),
    region: str = typer.Option("world", "--region", help="Region slug such as world, us, or eu."),
    dungeon: str = typer.Option("all", "--dungeon", help="Dungeon slug or all."),
    affixes: str = typer.Option("", "--affixes", help="Affix slug, fortified, tyrannical, current, or all."),
    page: int = typer.Option(0, "--page", min=0, help="Starting page of rankings to request."),
    pages: int = typer.Option(1, "--pages", min=1, max=10, help="Number of pages to sample."),
    limit: int = typer.Option(100, "--limit", min=1, max=200, help="Maximum runs to retain in the sample."),
    level_min: int | None = typer.Option(None, "--level-min", min=0, help="Retain only runs at or above this Mythic+ level."),
    level_max: int | None = typer.Option(None, "--level-max", min=0, help="Retain only runs at or below this Mythic+ level."),
    score_min: float | None = typer.Option(None, "--score-min", help="Retain only runs at or above this sampled run score."),
    score_max: float | None = typer.Option(None, "--score-max", help="Retain only runs at or below this sampled run score."),
    contains_role: list[str] | None = typer.Option(
        None, "--contains-role", help="Retain only runs containing at least one roster role. Repeatable."),
    contains_class: list[str] | None = typer.Option(
        None, "--contains-class", help="Retain only runs containing at least one class slug or name. Repeatable."),
    contains_spec: list[str] | None = typer.Option(
        None, "--contains-spec", help="Retain only runs containing at least one spec slug or name. Repeatable."),
    player_region: list[str] | None = typer.Option(
        None, "--player-region", help="Retain only runs containing at least one player from the given region. Repeatable."),
) -> None:
    """Return a filtered sample of Mythic+ leaderboard runs with sampling counts and citations."""
    request = SampleRequest(season=season, region=region, dungeon=dungeon, affixes=affixes, page=page, pages=pages, limit=limit)
    filters = run_filters(
        level_min=level_min,
        level_max=level_max,
        score_min=score_min,
        score_max=score_max,
        contains_role=contains_role,
        contains_class=contains_class,
        contains_spec=contains_spec,
        player_region=player_region,
    )
    with _command_errors(ctx), open_client() as client:
        runs, meta, filtering = load_filtered_runs(client, request, filters)
    emit(
        ctx,
        raiderio_envelope(
            command="mythic-plus-runs",
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
    season: str = typer.Option("", "--season", help="Season slug. Defaults to Raider.IO current default season."),
    region: str = typer.Option("world", "--region", help="Region slug such as world, us, or eu."),
    dungeon: str = typer.Option("all", "--dungeon", help="Dungeon slug or all."),
    affixes: str = typer.Option("", "--affixes", help="Affix slug, fortified, tyrannical, current, or all."),
    page: int = typer.Option(0, "--page", min=0, help="Starting page of rankings to request."),
    pages: int = typer.Option(1, "--pages", min=1, max=10, help="Number of pages to sample."),
    limit: int = typer.Option(100, "--limit", min=1, max=200, help="Maximum runs to retain in the source sample."),
    player_limit: int = typer.Option(100, "--player-limit", min=1, max=500, help="Maximum player snapshots to retain after deduping."),
    level_min: int | None = typer.Option(None, "--level-min", min=0, help="Retain only runs at or above this Mythic+ level."),
    level_max: int | None = typer.Option(None, "--level-max", min=0, help="Retain only runs at or below this Mythic+ level."),
    score_min: float | None = typer.Option(None, "--score-min", help="Retain only runs at or above this sampled run score."),
    score_max: float | None = typer.Option(None, "--score-max", help="Retain only runs at or below this sampled run score."),
    contains_role: list[str] | None = typer.Option(
        None, "--contains-role", help="Retain only runs containing at least one roster role. Repeatable."),
    contains_class: list[str] | None = typer.Option(
        None, "--contains-class", help="Retain only runs containing at least one class slug or name. Repeatable."),
    contains_spec: list[str] | None = typer.Option(
        None, "--contains-spec", help="Retain only runs containing at least one spec slug or name. Repeatable."),
    player_region: list[str] | None = typer.Option(
        None, "--player-region", help="Retain only runs containing at least one player from the given region. Repeatable."),
) -> None:
    """Return deduped player snapshots built from a filtered sample of Mythic+ runs."""
    request = SampleRequest(season=season, region=region, dungeon=dungeon, affixes=affixes, page=page, pages=pages, limit=limit)
    filters = run_filters(
        level_min=level_min,
        level_max=level_max,
        score_min=score_min,
        score_max=score_max,
        contains_role=contains_role,
        contains_class=contains_class,
        contains_spec=contains_spec,
        player_region=player_region,
    )
    with _command_errors(ctx), open_client() as client:
        runs, meta, filtering = load_filtered_runs(client, request, filters)
    players, player_sampling = limit_player_snapshots(player_snapshots(runs), player_limit=player_limit)
    emit(
        ctx,
        raiderio_envelope(
            command="mythic-plus-players",
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
    metric: str = typer.Option("mythic_level", "--metric", help="Distribution metric: mythic_level, dungeon, role, or player_region."),
    season: str = typer.Option("", "--season", help="Season slug. Defaults to Raider.IO current default season."),
    region: str = typer.Option("world", "--region", help="Region slug such as world, us, or eu."),
    dungeon: str = typer.Option("all", "--dungeon", help="Dungeon slug or all."),
    affixes: str = typer.Option("", "--affixes", help="Affix slug, fortified, tyrannical, current, or all."),
    page: int = typer.Option(0, "--page", min=0, help="Starting page of rankings to request."),
    pages: int = typer.Option(1, "--pages", min=1, max=10, help="Number of pages to sample."),
    limit: int = typer.Option(100, "--limit", min=1, max=200, help="Maximum runs to retain in the sample."),
    level_min: int | None = typer.Option(None, "--level-min", min=0, help="Retain only runs at or above this Mythic+ level."),
    level_max: int | None = typer.Option(None, "--level-max", min=0, help="Retain only runs at or below this Mythic+ level."),
    score_min: float | None = typer.Option(None, "--score-min", help="Retain only runs at or above this sampled run score."),
    score_max: float | None = typer.Option(None, "--score-max", help="Retain only runs at or below this sampled run score."),
    contains_role: list[str] | None = typer.Option(
        None, "--contains-role", help="Retain only runs containing at least one roster role. Repeatable."),
    contains_class: list[str] | None = typer.Option(
        None, "--contains-class", help="Retain only runs containing at least one class slug or name. Repeatable."),
    contains_spec: list[str] | None = typer.Option(
        None, "--contains-spec", help="Retain only runs containing at least one spec slug or name. Repeatable."),
    player_region: list[str] | None = typer.Option(
        None, "--player-region", help="Retain only runs containing at least one player from the given region. Repeatable."),
) -> None:
    """Return a run-level distribution (level, dungeon, role, class, spec, or composition)."""
    if metric not in RUN_DISTRIBUTION_METRICS:
        fail(ctx, "invalid_query", f"--metric must be one of: {', '.join(RUN_DISTRIBUTION_METRICS)}")
    request = SampleRequest(season=season, region=region, dungeon=dungeon, affixes=affixes, page=page, pages=pages, limit=limit)
    filters = run_filters(
        level_min=level_min,
        level_max=level_max,
        score_min=score_min,
        score_max=score_max,
        contains_role=contains_role,
        contains_class=contains_class,
        contains_spec=contains_spec,
        player_region=player_region,
    )
    with _command_errors(ctx), open_client() as client:
        runs, meta, filtering = load_filtered_runs(client, request, filters)
    payload = distribution_payload(metric, runs, meta=meta, query=analytics_query(request, filters, meta=meta))
    payload["sample"]["filtering"] = filtering
    emit(ctx, raiderio_envelope(command="mythic-plus-runs", kind="mythic_plus_runs_distribution", payload=payload))


@distribution_app.command("mythic-plus-players")
def distribution_mythic_plus_players(
    ctx: typer.Context,
    metric: str = typer.Option("appearance_count", "--metric",
                               help="Distribution metric: appearance_count, top_mythic_level, class, spec, role, or player_region."),
    season: str = typer.Option("", "--season", help="Season slug. Defaults to Raider.IO current default season."),
    region: str = typer.Option("world", "--region", help="Region slug such as world, us, or eu."),
    dungeon: str = typer.Option("all", "--dungeon", help="Dungeon slug or all."),
    affixes: str = typer.Option("", "--affixes", help="Affix slug, fortified, tyrannical, current, or all."),
    page: int = typer.Option(0, "--page", min=0, help="Starting page of rankings to request."),
    pages: int = typer.Option(1, "--pages", min=1, max=10, help="Number of pages to sample."),
    limit: int = typer.Option(100, "--limit", min=1, max=200, help="Maximum runs to retain in the source sample."),
    player_limit: int = typer.Option(100, "--player-limit", min=1, max=500, help="Maximum player snapshots to retain after deduping."),
    level_min: int | None = typer.Option(None, "--level-min", min=0, help="Retain only runs at or above this Mythic+ level."),
    level_max: int | None = typer.Option(None, "--level-max", min=0, help="Retain only runs at or below this Mythic+ level."),
    score_min: float | None = typer.Option(None, "--score-min", help="Retain only runs at or above this sampled run score."),
    score_max: float | None = typer.Option(None, "--score-max", help="Retain only runs at or below this sampled run score."),
    contains_role: list[str] | None = typer.Option(
        None, "--contains-role", help="Retain only runs containing at least one roster role. Repeatable."),
    contains_class: list[str] | None = typer.Option(
        None, "--contains-class", help="Retain only runs containing at least one class slug or name. Repeatable."),
    contains_spec: list[str] | None = typer.Option(
        None, "--contains-spec", help="Retain only runs containing at least one spec slug or name. Repeatable."),
    player_region: list[str] | None = typer.Option(
        None, "--player-region", help="Retain only runs containing at least one player from the given region. Repeatable."),
) -> None:
    """Return a player-level distribution (appearances, top level, class, spec, role, or region)."""
    if metric not in PLAYER_DISTRIBUTION_METRICS:
        fail(ctx, "invalid_query", f"--metric must be one of: {', '.join(PLAYER_DISTRIBUTION_METRICS)}")
    request = SampleRequest(season=season, region=region, dungeon=dungeon, affixes=affixes, page=page, pages=pages, limit=limit)
    filters = run_filters(
        level_min=level_min,
        level_max=level_max,
        score_min=score_min,
        score_max=score_max,
        contains_role=contains_role,
        contains_class=contains_class,
        contains_spec=contains_spec,
        player_region=player_region,
    )
    with _command_errors(ctx), open_client() as client:
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
    emit(ctx, raiderio_envelope(command="mythic-plus-players", kind="mythic_plus_players_distribution", payload=payload))


@threshold_app.command("mythic-plus-runs")
def threshold_mythic_plus_runs(
    ctx: typer.Context,
    metric: str = typer.Option("score", "--metric", help="Threshold metric: score or mythic_level."),
    value: float = typer.Option(..., "--value", help="Target metric value to estimate around."),
    season: str = typer.Option("", "--season", help="Season slug. Defaults to Raider.IO current default season."),
    region: str = typer.Option("world", "--region", help="Region slug such as world, us, or eu."),
    dungeon: str = typer.Option("all", "--dungeon", help="Dungeon slug or all."),
    affixes: str = typer.Option("", "--affixes", help="Affix slug, fortified, tyrannical, current, or all."),
    page: int = typer.Option(0, "--page", min=0, help="Starting page of rankings to request."),
    pages: int = typer.Option(1, "--pages", min=1, max=10, help="Number of pages to sample."),
    limit: int = typer.Option(100, "--limit", min=1, max=200, help="Maximum runs to retain in the sample."),
    nearest: int = typer.Option(10, "--nearest", min=1, max=50, help="Number of nearest sampled runs to retain."),
    level_min: int | None = typer.Option(None, "--level-min", min=0, help="Retain only runs at or above this Mythic+ level."),
    level_max: int | None = typer.Option(None, "--level-max", min=0, help="Retain only runs at or below this Mythic+ level."),
    score_min: float | None = typer.Option(None, "--score-min", help="Retain only runs at or above this sampled run score."),
    score_max: float | None = typer.Option(None, "--score-max", help="Retain only runs at or below this sampled run score."),
    contains_role: list[str] | None = typer.Option(
        None, "--contains-role", help="Retain only runs containing at least one roster role. Repeatable."),
    contains_class: list[str] | None = typer.Option(
        None, "--contains-class", help="Retain only runs containing at least one class slug or name. Repeatable."),
    contains_spec: list[str] | None = typer.Option(
        None, "--contains-spec", help="Retain only runs containing at least one spec slug or name. Repeatable."),
    player_region: list[str] | None = typer.Option(
        None, "--player-region", help="Retain only runs containing at least one player from the given region. Repeatable."),
) -> None:
    """Estimate the sampled runs nearest a target score or Mythic+ level."""
    if metric not in THRESHOLD_METRICS:
        fail(ctx, "invalid_query", f"--metric must be one of: {', '.join(THRESHOLD_METRICS)}")
    request = SampleRequest(season=season, region=region, dungeon=dungeon, affixes=affixes, page=page, pages=pages, limit=limit)
    filters = run_filters(
        level_min=level_min,
        level_max=level_max,
        score_min=score_min,
        score_max=score_max,
        contains_role=contains_role,
        contains_class=contains_class,
        contains_spec=contains_spec,
        player_region=player_region,
    )
    with _command_errors(ctx), open_client() as client:
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
    emit(ctx, raiderio_envelope(command="mythic-plus-runs", kind="mythic_plus_runs_threshold", payload=payload))


def run() -> None:
    guarded_run(app, provider=PROVIDER_NAME)


if __name__ == "__main__":
    run()

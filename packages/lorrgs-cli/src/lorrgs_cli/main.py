from __future__ import annotations

from collections.abc import Callable
from typing import Any

import typer
from warcraft_core.cli import emit, fail, guarded_run, install_common_callback
from warcraft_core.envelope import Envelope
from warcraft_core.provider import ProviderError

from lorrgs_cli.client import PROVIDER_NAME, LorrgsClient
from lorrgs_cli.provider import call_api, note_empty_comp_ranking
from lorrgs_cli.provider import doctor as provider_doctor
from lorrgs_cli.provider import resolve as provider_resolve
from lorrgs_cli.provider import search as provider_search
from lorrgs_cli.search import parse_report_reference

app = typer.Typer(add_completion=False, help="Lorrgs cooldown timeline and composition ranking CLI.")
install_common_callback(app, provider=PROVIDER_NAME)


def _emit_surface(ctx: typer.Context, build: Callable[[], Envelope]) -> None:
    """Run a pure provider call and turn ``ProviderError`` into the shared error envelope."""
    try:
        payload = build()
    except ProviderError as exc:
        fail(ctx, exc.code, exc.message, exit_code=exc.exit_code, details=exc.details)
    emit(ctx, payload)


def _run_command(
    ctx: typer.Context,
    command: str,
    kind: str,
    query: dict[str, Any],
    call: Callable[[LorrgsClient], dict[str, Any]],
) -> None:
    _emit_surface(ctx, lambda: call_api(command, kind, query, call))


def _report_reference_or_fail(ctx: typer.Context, report_ref: str) -> tuple[str, int | None, str | None]:
    ref = parse_report_reference(report_ref)
    if ref is None:
        fail(
            ctx,
            "invalid_report_ref",
            "Expected a Warcraft Logs report URL, Lorrgs user_report URL, or mixed alphanumeric report code.",
        )
    return ref.code, ref.fight_id, ref.report_type


@app.command("doctor")
def doctor(ctx: typer.Context) -> None:
    """Report Lorrgs auth posture, endpoints, and per-surface capability state."""
    _emit_surface(ctx, provider_doctor)


@app.command("roles")
def roles(ctx: typer.Context) -> None:
    """List Lorrgs raid roles."""
    _run_command(ctx, "roles", "roles", {}, lambda client: client.roles())


@app.command("classes")
def classes(ctx: typer.Context) -> None:
    """List Lorrgs player classes."""
    _run_command(ctx, "classes", "classes", {}, lambda client: client.classes())


@app.command("specs")
def specs(ctx: typer.Context) -> None:
    """List Lorrgs specs with their slugs and roles."""
    _run_command(ctx, "specs", "specs", {}, lambda client: client.specs())


@app.command("search")
def search(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="Search Lorrgs routes, Warcraft Logs report refs, or spec/boss text."),
    limit: int = typer.Option(5, "--limit", min=1, max=50, help="Maximum results to return."),
) -> None:
    """Search Lorrgs by explicit URL/ref or by spec/boss terms."""
    _emit_surface(ctx, lambda: provider_search(query, limit=limit))


@app.command("resolve")
def resolve(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="Resolve a Lorrgs URL/ref or spec/boss query into a next command."),
    limit: int = typer.Option(5, "--limit", min=1, max=50, help="Maximum candidates to list; ambiguity is judged over all of them."),
) -> None:
    """Resolve a Lorrgs query conservatively: an ambiguous query resolves to nothing, not a guess."""
    _emit_surface(ctx, lambda: provider_resolve(query, limit=limit))


@app.command("spec")
def spec(
    ctx: typer.Context,
    spec_slug: str = typer.Argument(..., help="Lorrgs full spec slug, e.g. mage-frost."),
) -> None:
    """Fetch metadata for one Lorrgs spec."""
    query = {"spec_slug": spec_slug}
    _run_command(ctx, "spec", "spec", query, lambda client: client.spec(spec_slug))


@app.command("spec-spells")
def spec_spells(
    ctx: typer.Context,
    spec_slug: str = typer.Argument(..., help="Lorrgs full spec slug, e.g. mage-frost."),
) -> None:
    """Fetch the tracked cooldown spells for one spec."""
    query = {"spec_slug": spec_slug}
    _run_command(ctx, "spec-spells", "spec_spells", query, lambda client: client.spec_spells(spec_slug))


@app.command("zones")
def zones(ctx: typer.Context) -> None:
    """List Lorrgs raid zones."""
    _run_command(ctx, "zones", "zones", {}, lambda client: client.zones())


@app.command("season")
def season(
    ctx: typer.Context,
    season_slug: str = typer.Argument("current", help="Lorrgs season slug; use current for the current season."),
) -> None:
    """Fetch season-to-raid partition metadata for one season."""
    query = {"season_slug": season_slug}
    _run_command(ctx, "season", "season", query, lambda client: client.season(season_slug))


@app.command("current-season")
def current_season(ctx: typer.Context) -> None:
    """Fetch season-to-raid partition metadata for the current season."""
    query = {"season_slug": "current"}
    _run_command(ctx, "current-season", "season", query, lambda client: client.season("current"))


@app.command("zone")
def zone(
    ctx: typer.Context,
    zone_id: float = typer.Argument(..., help="Lorrgs/Warcraft Logs zone id."),
) -> None:
    """Fetch metadata for one raid zone."""
    query = {"zone_id": zone_id}
    _run_command(ctx, "zone", "zone", query, lambda client: client.zone(zone_id))


@app.command("zone-bosses")
def zone_bosses(
    ctx: typer.Context,
    zone_id: float = typer.Argument(..., help="Lorrgs/Warcraft Logs zone id."),
) -> None:
    """List the bosses in one raid zone."""
    query = {"zone_id": zone_id}
    _run_command(ctx, "zone-bosses", "zone_bosses", query, lambda client: client.zone_bosses(zone_id))


@app.command("bosses")
def bosses(ctx: typer.Context) -> None:
    """List every boss Lorrgs tracks."""
    _run_command(ctx, "bosses", "bosses", {}, lambda client: client.bosses())


@app.command("boss")
def boss(
    ctx: typer.Context,
    boss_slug: str = typer.Argument(..., help="Lorrgs boss slug, e.g. chimaerus-the-undreamt-god."),
) -> None:
    """Fetch metadata for one boss."""
    query = {"boss_slug": boss_slug}
    _run_command(ctx, "boss", "boss", query, lambda client: client.boss(boss_slug))


@app.command("boss-spells")
def boss_spells(
    ctx: typer.Context,
    boss_slug: str = typer.Argument(..., help="Lorrgs boss slug, e.g. chimaerus-the-undreamt-god."),
) -> None:
    """Fetch the tracked boss abilities for one encounter."""
    query = {"boss_slug": boss_slug}
    _run_command(ctx, "boss-spells", "boss_spells", query, lambda client: client.boss_spells(boss_slug))


@app.command("spell")
def spell(
    ctx: typer.Context,
    spell_id: int = typer.Argument(..., help="Numeric spell id."),
) -> None:
    """Fetch Lorrgs metadata for one spell id."""
    query = {"spell_id": spell_id}
    _run_command(ctx, "spell", "spell", query, lambda client: client.spell(spell_id))


@app.command("trinkets")
def trinkets(ctx: typer.Context) -> None:
    """List the trinkets Lorrgs tracks on timelines."""
    _run_command(ctx, "trinkets", "trinkets", {}, lambda client: client.trinkets())


@app.command("spec-ranking")
def spec_ranking(
    ctx: typer.Context,
    spec_slug: str = typer.Argument(..., help="Lorrgs full spec slug, e.g. mage-frost."),
    boss_slug: str = typer.Argument(..., help="Lorrgs boss slug, e.g. chimaerus-the-undreamt-god."),
    difficulty: str = typer.Option("mythic", "--difficulty", help="Lorrgs difficulty slug; defaults to mythic."),
    metric: str | None = typer.Option(None, "--metric", help="Metric override, e.g. dps or hps. Defaults by spec role."),
) -> None:
    """Fetch top-parse cooldown timelines for one spec on one encounter."""
    query = {"spec_slug": spec_slug, "boss_slug": boss_slug, "difficulty": difficulty, "metric": metric}
    _run_command(
        ctx,
        "spec-ranking",
        "spec_ranking",
        query,
        lambda client: client.spec_ranking(spec_slug=spec_slug, boss_slug=boss_slug, difficulty=difficulty, metric=metric),
    )


@app.command("spec-ranking-info")
def spec_ranking_info(
    ctx: typer.Context,
    spec_slug: str = typer.Argument(..., help="Lorrgs full spec slug, e.g. mage-frost."),
    boss_slug: str = typer.Argument(..., help="Lorrgs boss slug, e.g. chimaerus-the-undreamt-god."),
    difficulty: str = typer.Option("mythic", "--difficulty", help="Lorrgs difficulty slug; defaults to mythic."),
    metric: str | None = typer.Option(None, "--metric", help="Metric override, e.g. dps or hps. Defaults by spec role."),
) -> None:
    """Fetch metadata for a spec ranking without the large report timeline list."""
    query = {"spec_slug": spec_slug, "boss_slug": boss_slug, "difficulty": difficulty, "metric": metric}
    _run_command(
        ctx,
        "spec-ranking-info",
        "spec_ranking_info",
        query,
        lambda client: client.spec_ranking_info(spec_slug=spec_slug, boss_slug=boss_slug, difficulty=difficulty, metric=metric),
    )


@app.command("comp-ranking")
def comp_ranking(
    ctx: typer.Context,
    boss_slug: str = typer.Argument(..., help="Lorrgs boss slug, e.g. chimaerus-the-undreamt-god."),
    limit: int = typer.Option(20, "--limit", min=1, max=50, help="Maximum report rows to request."),
    role: list[str] | None = typer.Option(None, "--role", help="Composition role filter expression; repeatable."),
    spec_filter: list[str] | None = typer.Option(None, "--spec", help="Composition spec filter expression; repeatable."),
    killtime_min: int = typer.Option(0, "--killtime-min", min=0, help="Minimum kill time in seconds."),
    killtime_max: int = typer.Option(0, "--killtime-max", min=0, help="Maximum kill time in seconds."),
) -> None:
    """Fetch top composition ranking rows for an encounter."""
    query = {
        "boss_slug": boss_slug,
        "limit": limit,
        "roles": role or [],
        "specs": spec_filter or [],
        "killtime_min": killtime_min,
        "killtime_max": killtime_max,
    }
    _run_command(
        ctx,
        "comp-ranking",
        "comp_ranking",
        query,
        lambda client: note_empty_comp_ranking(
            client.comp_ranking(
                boss_slug=boss_slug,
                limit=limit,
                roles=role,
                specs=spec_filter,
                killtime_min=killtime_min,
                killtime_max=killtime_max,
            ),
            boss_slug,
        ),
    )


@app.command("user-report")
def user_report(
    ctx: typer.Context,
    report_ref: str = typer.Argument(
        ...,
        help="Warcraft Logs report URL, Lorrgs user_report URL, or report code already cached by Lorrgs.",
    ),
) -> None:
    """Fetch an already-cached Lorrgs user report overview."""
    report_id, _fight_id, _report_type = _report_reference_or_fail(ctx, report_ref)
    query = {"report_ref": report_ref, "report_id": report_id}
    _run_command(ctx, "user-report", "user_report", query, lambda client: client.user_report(report_id))


@app.command("report-overview")
def report_overview(
    ctx: typer.Context,
    report_ref: str = typer.Argument(..., help="Warcraft Logs report URL, Lorrgs user_report URL, or report code."),
    refresh: bool = typer.Option(False, "--refresh/--no-refresh", help="Ask Lorrgs to refresh overview metadata."),
) -> None:
    """Fetch or load a Lorrgs report overview without queueing fight/player timeline work."""
    report_id, fight_id, report_type = _report_reference_or_fail(ctx, report_ref)
    query = {"report_ref": report_ref, "report_id": report_id, "fight_id": fight_id, "report_type": report_type, "refresh": refresh}
    _run_command(ctx, "report-overview", "report_overview", query, lambda client: client.report_overview(report_id, refresh=refresh))


@app.command("user-report-fights")
def user_report_fights(
    ctx: typer.Context,
    report_ref: str = typer.Argument(
        ...,
        help="Warcraft Logs report URL, Lorrgs user_report URL, or report code already cached by Lorrgs.",
    ),
    fight: str | None = typer.Option(None, "--fight", help="Dot-separated fight ids, e.g. 2.4.15. Defaults to fight id from URL."),
    player: str | None = typer.Option(None, "--player", help="Optional dot-separated player source ids, e.g. 1.5.20."),
    data_type: str | None = typer.Option(None, "--type", help="Optional report view type, e.g. damage-done. Defaults to type from URL."),
) -> None:
    """Fetch selected fights from an already-cached Lorrgs user report."""
    report_id, parsed_fight_id, parsed_report_type = _report_reference_or_fail(ctx, report_ref)
    resolved_fight = fight or (str(parsed_fight_id) if parsed_fight_id is not None else None)
    resolved_type = data_type or parsed_report_type
    if not resolved_fight:
        fail(ctx, "missing_fight", "Pass --fight or provide a report URL containing fight=<id>.")
    query = {"report_ref": report_ref, "report_id": report_id, "fight": resolved_fight, "player": player, "type": resolved_type}
    _run_command(
        ctx,
        "user-report-fights",
        "user_report_fights",
        query,
        lambda client: client.user_report_fights(report_id=report_id, fight=resolved_fight, player=player, data_type=resolved_type),
    )


def run() -> None:
    guarded_run(app, provider=PROVIDER_NAME)


if __name__ == "__main__":
    run()

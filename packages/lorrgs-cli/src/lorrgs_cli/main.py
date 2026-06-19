from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, NoReturn

import httpx
import typer
from warcraft_core.output import emit

from lorrgs_cli.client import API_HOST, OPENAPI_URL, PROVIDER, SITE_HOST, LorrgsClient, LorrgsClientError
from lorrgs_cli.search import parse_report_reference, resolve_payload, search_candidates

app = typer.Typer(add_completion=False, help="Lorrgs cooldown timeline and composition ranking CLI.")


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


@app.command("doctor")
def doctor(ctx: typer.Context) -> None:
    _emit(
        ctx,
        {
            "ok": True,
            "provider": PROVIDER,
            "status": "partial",
            "command": "doctor",
            "installed": True,
            "language": "python",
            "auth": {
                "required": False,
                "configured": True,
                "flow": "none",
            },
            "endpoints": {
                "site": SITE_HOST,
                "api": API_HOST,
                "openapi": OPENAPI_URL,
            },
            "capabilities": {
                "doctor": "ready",
                "search": "ready",
                "resolve": "ready",
                "roles": "ready",
                "classes": "ready",
                "specs": "ready",
                "spec": "ready",
                "spec_spells": "ready",
                "zones": "ready",
                "season": "ready",
                "current_season": "ready",
                "zone": "ready",
                "zone_bosses": "ready",
                "bosses": "ready",
                "boss": "ready",
                "boss_spells": "ready",
                "spell": "ready",
                "trinkets": "ready",
                "spec_ranking": "ready",
                "spec_ranking_info": "ready",
                "comp_ranking": "ready",
                "report_overview": "ready",
                "user_report": "ready_cached_only",
                "user_report_fights": "ready_cached_only",
            },
            "notes": [
                "Lorrgs visualizes Warcraft Logs-derived cooldown timelines for top parses by spec and boss.",
                "This provider intentionally exposes raw Lorrgs JSON plus source URLs; it does not synthesize cooldown plans.",
                "Read-only CLI surface: queued load/dirty endpoints are intentionally not exposed.",
                "Search/resolve understand Lorrgs ranking URLs, Warcraft Logs report URLs, and free text spec/boss pairs.",
            ],
        },
    )


def _error_payload(command: str, exc: Exception) -> dict[str, Any]:
    if isinstance(exc, LorrgsClientError):
        code, message = exc.code, exc.message
    elif isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        code = "http_error"
        if status == 404:
            code = "not_found"
        elif status == 422:
            code = "invalid_query"
        elif status == 429:
            code = "rate_limited"
        message = f"Lorrgs API returned HTTP {status} for {exc.request.url}."
        detail = _response_detail(exc.response)
        if detail:
            message = detail
    else:
        code = "network_error"
        message = f"Lorrgs API request failed: {exc}."
    return {
        "ok": False,
        "provider": PROVIDER,
        "command": command,
        "error": {"code": code, "message": message},
    }


def _response_detail(response: httpx.Response) -> str | None:
    try:
        payload = response.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    detail = payload.get("detail")
    if isinstance(detail, str) and detail.strip():
        return detail.strip()
    return None


def _success_payload(command: str, kind: str, query: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": True,
        "provider": PROVIDER,
        "command": command,
        "kind": kind,
        "query": query,
        "provenance": {
            "source_url": result["source_url"],
            "api_host": API_HOST,
            "site": SITE_HOST,
            "source": "lorrgs_public_api",
            "upstream_data_sources": ["warcraftlogs", "wowhead_tooltips"],
            "verified": True,
        },
        "data": result["payload"],
    }


def _run_command(
    ctx: typer.Context,
    command: str,
    kind: str,
    query: dict[str, Any],
    call: Callable[[LorrgsClient], dict[str, Any]],
) -> None:
    client = LorrgsClient()
    try:
        result = call(client)
    except (LorrgsClientError, httpx.HTTPError) as exc:
        _emit(ctx, _error_payload(command, exc), err=True)
        raise typer.Exit(1) from exc
    finally:
        client.close()
    _emit(ctx, _success_payload(command, kind, query, result))


def _fail(ctx: typer.Context, command: str, code: str, message: str) -> NoReturn:
    _emit(ctx, {"ok": False, "provider": PROVIDER, "command": command, "error": {"code": code, "message": message}}, err=True)
    raise typer.Exit(1)


def _report_reference_or_fail(ctx: typer.Context, command: str, report_ref: str) -> tuple[str, int | None, str | None]:
    ref = parse_report_reference(report_ref)
    if ref is None:
        _fail(
            ctx,
            command,
            "invalid_report_ref",
            "Expected a Warcraft Logs report URL, Lorrgs user_report URL, or mixed alphanumeric report code.",
        )
    return ref.code, ref.fight_id, ref.report_type


@app.command("roles")
def roles(ctx: typer.Context) -> None:
    _run_command(ctx, "roles", "roles", {}, lambda client: client.roles())


@app.command("classes")
def classes(ctx: typer.Context) -> None:
    _run_command(ctx, "classes", "classes", {}, lambda client: client.classes())


@app.command("specs")
def specs(ctx: typer.Context) -> None:
    _run_command(ctx, "specs", "specs", {}, lambda client: client.specs())


@app.command("search")
def search(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="Search Lorrgs routes, Warcraft Logs report refs, or spec/boss text."),
    limit: int = typer.Option(5, "--limit", min=1, max=50, help="Maximum results to return."),
) -> None:
    """Search Lorrgs by explicit URL/ref or by spec/boss terms."""
    with LorrgsClient() as client:
        try:
            payload = search_candidates(client, query, limit=limit)
        except (LorrgsClientError, httpx.HTTPError) as exc:
            _emit(ctx, _error_payload("search", exc), err=True)
            raise typer.Exit(1) from exc
    _emit(ctx, payload)


@app.command("resolve")
def resolve(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="Resolve a Lorrgs URL/ref or spec/boss query into a next command."),
    limit: int = typer.Option(5, "--limit", min=1, max=50, help="Maximum candidates to inspect."),
) -> None:
    """Resolve a Lorrgs query conservatively."""
    with LorrgsClient() as client:
        try:
            payload = resolve_payload(search_candidates(client, query, limit=limit))
        except (LorrgsClientError, httpx.HTTPError) as exc:
            _emit(ctx, _error_payload("resolve", exc), err=True)
            raise typer.Exit(1) from exc
    _emit(ctx, payload)


@app.command("spec")
def spec(
    ctx: typer.Context,
    spec_slug: str = typer.Argument(..., help="Lorrgs full spec slug, e.g. mage-frost."),
) -> None:
    query = {"spec_slug": spec_slug}
    _run_command(ctx, "spec", "spec", query, lambda client: client.spec(spec_slug))


@app.command("spec-spells")
def spec_spells(
    ctx: typer.Context,
    spec_slug: str = typer.Argument(..., help="Lorrgs full spec slug, e.g. mage-frost."),
) -> None:
    query = {"spec_slug": spec_slug}
    _run_command(ctx, "spec-spells", "spec_spells", query, lambda client: client.spec_spells(spec_slug))


@app.command("zones")
def zones(ctx: typer.Context) -> None:
    _run_command(ctx, "zones", "zones", {}, lambda client: client.zones())


@app.command("season")
def season(
    ctx: typer.Context,
    season_slug: str = typer.Argument("current", help="Lorrgs season slug; use current for the current season."),
) -> None:
    query = {"season_slug": season_slug}
    _run_command(ctx, "season", "season", query, lambda client: client.season(season_slug))


@app.command("current-season")
def current_season(ctx: typer.Context) -> None:
    query = {"season_slug": "current"}
    _run_command(ctx, "current-season", "season", query, lambda client: client.season("current"))


@app.command("zone")
def zone(
    ctx: typer.Context,
    zone_id: float = typer.Argument(..., help="Lorrgs/Warcraft Logs zone id."),
) -> None:
    query = {"zone_id": zone_id}
    _run_command(ctx, "zone", "zone", query, lambda client: client.zone(zone_id))


@app.command("zone-bosses")
def zone_bosses(
    ctx: typer.Context,
    zone_id: float = typer.Argument(..., help="Lorrgs/Warcraft Logs zone id."),
) -> None:
    query = {"zone_id": zone_id}
    _run_command(ctx, "zone-bosses", "zone_bosses", query, lambda client: client.zone_bosses(zone_id))


@app.command("bosses")
def bosses(ctx: typer.Context) -> None:
    _run_command(ctx, "bosses", "bosses", {}, lambda client: client.bosses())


@app.command("boss")
def boss(
    ctx: typer.Context,
    boss_slug: str = typer.Argument(..., help="Lorrgs boss slug, e.g. chimaerus-the-undreamt-god."),
) -> None:
    query = {"boss_slug": boss_slug}
    _run_command(ctx, "boss", "boss", query, lambda client: client.boss(boss_slug))


@app.command("boss-spells")
def boss_spells(
    ctx: typer.Context,
    boss_slug: str = typer.Argument(..., help="Lorrgs boss slug, e.g. chimaerus-the-undreamt-god."),
) -> None:
    query = {"boss_slug": boss_slug}
    _run_command(ctx, "boss-spells", "boss_spells", query, lambda client: client.boss_spells(boss_slug))


@app.command("spell")
def spell(
    ctx: typer.Context,
    spell_id: int = typer.Argument(..., help="Numeric spell id."),
) -> None:
    query = {"spell_id": spell_id}
    _run_command(ctx, "spell", "spell", query, lambda client: client.spell(spell_id))


@app.command("trinkets")
def trinkets(ctx: typer.Context) -> None:
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
        lambda client: client.comp_ranking(
            boss_slug=boss_slug,
            limit=limit,
            roles=role,
            specs=spec_filter,
            killtime_min=killtime_min,
            killtime_max=killtime_max,
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
    report_id, _fight_id, _report_type = _report_reference_or_fail(ctx, "user-report", report_ref)
    query = {"report_ref": report_ref, "report_id": report_id}
    _run_command(ctx, "user-report", "user_report", query, lambda client: client.user_report(report_id))


@app.command("report-overview")
def report_overview(
    ctx: typer.Context,
    report_ref: str = typer.Argument(..., help="Warcraft Logs report URL, Lorrgs user_report URL, or report code."),
    refresh: bool = typer.Option(False, "--refresh/--no-refresh", help="Ask Lorrgs to refresh overview metadata."),
) -> None:
    """Fetch or load a Lorrgs report overview without queueing fight/player timeline work."""
    report_id, fight_id, report_type = _report_reference_or_fail(ctx, "report-overview", report_ref)
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
    report_id, parsed_fight_id, parsed_report_type = _report_reference_or_fail(ctx, "user-report-fights", report_ref)
    resolved_fight = fight or (str(parsed_fight_id) if parsed_fight_id is not None else None)
    resolved_type = data_type or parsed_report_type
    if not resolved_fight:
        _fail(ctx, "user-report-fights", "missing_fight", "Pass --fight or provide a report URL containing fight=<id>.")
    query = {"report_ref": report_ref, "report_id": report_id, "fight": resolved_fight, "player": player, "type": resolved_type}
    _run_command(
        ctx,
        "user-report-fights",
        "user_report_fights",
        query,
        lambda client: client.user_report_fights(report_id=report_id, fight=resolved_fight or "", player=player, data_type=resolved_type),
    )


def run() -> None:
    app()


if __name__ == "__main__":
    run()

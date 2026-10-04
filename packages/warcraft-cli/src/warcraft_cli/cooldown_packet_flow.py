"""Provider orchestration behind `warcraft cooldown-packet`.

The Typer command owns the flag surface; this module owns the sequence of provider calls and the
packet it assembles. Provider calls arrive as an injected ``fetch`` callable so the command keeps a
single seam (``warcraft_cli.main._provider_payload_result``) and this module never imports it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, NoReturn

import typer
from warcraft_core.cli import emit, fail
from warcraft_core.exit_codes import EXIT_GENERIC, EXIT_NOT_FOUND, EXIT_USAGE
from warcraft_core.shapes import as_dict, as_list
from warcraft_core.wow_specs import lookup_spec

from warcraft_cli.cooldown_packet import (
    build_phase_windows,
    int_or_none,
    normalize_lorrgs_casts,
    normalize_warcraftlogs_actor_casts,
    raw_phase_markers,
    received_aura_spell_ids,
    selected_phase_window,
    source_command,
    spell_catalog,
    spell_summary,
    timestamp_in_window,
    top_parse_fights,
    top_parse_samples,
    tracked_spell_ids,
    warcraftlogs_phase_windows,
)
from warcraft_cli.providers import (
    ProviderFetch,
    parse_lorrgs_report_reference,
    provider_payload_data,
    source_exit_code,
    wrapper_envelope,
)


def _fail_cooldown_packet(
    ctx: typer.Context,
    *,
    code: str,
    message: str,
    query: dict[str, Any],
    details: dict[str, Any] | None = None,
    exit_code: int = EXIT_GENERIC,
) -> NoReturn:
    """Emit the packet failure envelope. Structured context goes under ``error.details``."""
    fail(ctx, code, message, exit_code=exit_code, query=query, details=details)


def _cooldown_provider_payload(
    ctx: typer.Context,
    provider: str,
    args: list[str],
    *,
    fetch: ProviderFetch,
    expansion: str | None,
    query: dict[str, Any],
    error_code: str,
    error_message: str,
    required: bool = True,
) -> dict[str, Any]:
    result = fetch(provider, args, expansion=expansion)
    if result.get("status") == "ok" or not required:
        return result
    _fail_cooldown_packet(
        ctx,
        code=error_code,
        message=error_message,
        query=query,
        details={"source": result.get("error"), "provider": provider},
        exit_code=source_exit_code(result),
    )


def _provider_graphql_warnings(provider_result: dict[str, Any] | None) -> list[Any]:
    """Keep partial-error evidence whether a typed read or raw GraphQL call supplied it."""
    payload = as_dict(as_dict(provider_result).get("payload"))
    provenance = as_dict(payload.get("provenance"))
    warnings = list(as_list(_data_of(provider_result).get("graphql_warnings")))
    for warning in as_list(provenance.get("graphql_warnings")):
        if warning not in warnings:
            warnings.append(warning)
    return warnings


def _provider_source(provider_result: dict[str, Any] | None, *, command: str, args: list[str]) -> dict[str, Any]:
    raw_payload = provider_result.get("payload") if isinstance(provider_result, dict) else None
    payload: dict[str, Any] = as_dict(raw_payload)
    raw_provenance = payload.get("provenance")
    provenance: dict[str, Any] = as_dict(raw_provenance)
    raw_report = _data_of(provider_result).get("report")
    report: dict[str, Any] = as_dict(raw_report)
    warnings = _provider_graphql_warnings(provider_result)
    status = provider_result.get("status") if isinstance(provider_result, dict) else "not_requested"
    return {
        "provider": provider_result.get("provider") if isinstance(provider_result, dict) else None,
        "status": "partial" if warnings and status == "ok" else status,
        "command": source_command(command, args),
        "source_url": provenance.get("source_url"),
        "report": report or None,
        "provenance": provenance,
        "graphql_warnings": warnings,
        "notes": as_list(_data_of(provider_result).get("notes")),
        "error": provider_result.get("error") if isinstance(provider_result, dict) else None,
    }


def _find_lorrgs_fight(data: dict[str, Any], fight_id: int) -> dict[str, Any] | None:
    raw_fights = data.get("fights")
    fights: list[Any] = as_list(raw_fights)
    for fight in fights:
        if isinstance(fight, dict) and fight.get("fight_id") == fight_id:
            return fight
    return None


def _available_lorrgs_players(fight: dict[str, Any]) -> list[dict[str, Any]]:
    raw_players = fight.get("players")
    players: list[Any] = as_list(raw_players)
    available = []
    for player in players:
        if not isinstance(player, dict):
            continue
        available.append(
            {
                "name": player.get("name"),
                "source_id": player.get("source_id"),
                "spec_slug": player.get("spec_slug"),
                "class_slug": player.get("class_slug"),
            }
        )
    return available


def _resolve_player(
    players: list[dict[str, Any]],
    *,
    id_key: str,
    actor_id: int | None,
    actor_name: str | None,
) -> tuple[dict[str, Any] | None, str | None]:
    """The roster row ``--actor-id`` (matched on ``id_key``) or ``--actor-name`` names, or why none."""
    if actor_id is not None:
        for player in players:
            if int_or_none(player.get(id_key)) == actor_id:
                return player, None
        return None, "actor_id_not_found"
    if actor_name is not None and actor_name.strip():
        normalized_name = actor_name.strip().casefold()
        matches = [
            player
            for player in players
            if isinstance(player.get("name"), str) and str(player["name"]).casefold() == normalized_name
        ]
        if len(matches) == 1:
            return matches[0], None
        if len(matches) > 1:
            return None, "ambiguous_actor_name"
        return None, "actor_name_not_found"
    return None, "missing_actor"


def _find_warcraftlogs_fight(provider_result: dict[str, Any] | None, fight_id: int) -> dict[str, Any] | None:
    raw_fights = _data_of(provider_result).get("fights")
    fights: list[Any] = as_list(raw_fights)
    for fight in fights:
        if isinstance(fight, dict) and fight.get("id") == fight_id:
            return fight
    return None


def _phase_deaths(state: CooldownState) -> dict[str, Any] | None:
    """The player's deaths from the cached Lorrgs timeline; ``None`` when Lorrgs did not supply it.

    Deaths come only from that timeline, so without it a count of zero would be a guess.
    """
    if state.lorrgs_unavailable is not None:
        return None
    raw = as_list(state.player.get("deaths"))
    window = state.selected_window
    selected = [
        death
        for death in raw
        if isinstance(death, dict)
        and window is not None
        and (timestamp := int_or_none(death.get("ts")) or int_or_none(death.get("timestamp"))) is not None
        and timestamp_in_window(timestamp, window)
    ]
    return {"count": len(raw), "raw": raw, "selected_phase_count": len(selected), "selected_phase": selected}


@dataclass(frozen=True, slots=True)
class CooldownRequest:
    """The `warcraft cooldown-packet` flag surface, as values."""

    report_ref: str
    fight_id: int | None
    actor_id: int | None
    actor_name: str | None
    phase: int
    spec_slug: str | None
    boss_slug: str | None
    difficulty: str | None
    metric: str | None
    sample_limit: int
    event_limit: int
    spell_ids: list[int] | None
    allow_unlisted: bool
    expansion: str | None


@dataclass(slots=True)
class CooldownState:
    """Everything the steps resolve, in the order they resolve it."""

    query: dict[str, Any] = field(default_factory=dict)
    report_code: str = ""
    fight_id: int = 0
    lorrgs_fight_args: list[str] = field(default_factory=list)
    lorrgs_result: dict[str, Any] | None = None
    lorrgs_fight: dict[str, Any] = field(default_factory=dict)
    player: dict[str, Any] = field(default_factory=dict)
    actor_id: int = 0
    spec_slug: str = ""
    boss: dict[str, Any] = field(default_factory=dict)
    boss_slug: str | None = None
    # The Lorrgs boss list, read only when Lorrgs could not name the fight's boss.
    bosses_args: list[str] = field(default_factory=list)
    bosses_result: dict[str, Any] | None = None
    lorrgs_phases: list[Any] = field(default_factory=list)
    phase_windows: list[dict[str, Any]] = field(default_factory=list)
    selected_window: dict[str, Any] | None = None
    # Which provider the phase windows came from: "lorrgs", "warcraftlogs", or None without windows.
    phase_source: str | None = None
    # The Warcraft Logs phase transitions, read only when Lorrgs could not supply the fight.
    wcl_phases_args: list[str] = field(default_factory=list)
    wcl_phases_result: dict[str, Any] | None = None
    # Set when the cached Lorrgs user report is missing: the packet keeps its Warcraft Logs half.
    lorrgs_unavailable: dict[str, Any] | None = None
    # The Warcraft Logs roster, read only when Lorrgs could not name the player.
    roster_args: list[str] = field(default_factory=list)
    roster_result: dict[str, Any] | None = None
    spec_spells_args: list[str] = field(default_factory=list)
    spec_spells_result: dict[str, Any] | None = None
    cooldown_catalog: dict[int, dict[str, Any]] = field(default_factory=dict)
    tracked_ids: set[int] = field(default_factory=set)
    # Lorrgs externals the player did not cast, left out of both sides: top parses hold them as auras received.
    received_aura_ids: set[int] = field(default_factory=set)
    boss_spells_args: list[str] | None = None
    boss_spells_result: dict[str, Any] | None = None
    boss_catalog: dict[int, dict[str, Any]] = field(default_factory=dict)
    wcl_fights_args: list[str] = field(default_factory=list)
    wcl_fights_result: dict[str, Any] | None = None
    wcl_fight: dict[str, Any] | None = None
    fight_start_time_ms: int = 0
    events_args: list[str] = field(default_factory=list)
    events_result: dict[str, Any] | None = None
    player_casts: dict[str, Any] = field(default_factory=dict)
    ranking_args: list[str] | None = None
    ranking_result: dict[str, Any] | None = None
    # The top parses' Warcraft Logs phase transitions, read only when the player's windows came from there.
    sample_phases_args: list[str] = field(default_factory=list)
    sample_phases_result: dict[str, Any] | None = None
    # Why the top-parse comparison is unavailable, and what to do about it; None when it ran.
    comparison_reason: str | None = None
    comparison_note: str | None = None
    comparison: dict[str, Any] = field(default_factory=dict)


def _data_of(result: dict[str, Any] | None) -> dict[str, Any]:
    """The provider envelope's ``data`` body, the only contract-stable home of its fields."""
    raw = result.get("payload") if isinstance(result, dict) else None
    return provider_payload_data(raw)


def _resolve_reference(ctx: typer.Context, request: CooldownRequest, state: CooldownState) -> None:
    parsed_ref = parse_lorrgs_report_reference(request.report_ref)
    state.query = {
        "report_ref": request.report_ref,
        "fight_id": request.fight_id,
        "actor_id": request.actor_id,
        "actor_name": request.actor_name,
        "phase": request.phase,
        "spec_slug": request.spec_slug,
        "boss_slug": request.boss_slug,
        "difficulty": request.difficulty,
        "metric": request.metric,
        "sample_limit": request.sample_limit,
        "event_limit": request.event_limit,
        "spell_ids": request.spell_ids or [],
        "allow_unlisted": request.allow_unlisted,
    }
    if parsed_ref is None:
        _fail_cooldown_packet(
            ctx,
            code="invalid_report_ref",
            message="Expected a Warcraft Logs report URL, Lorrgs user_report URL, or 16-character report code.",
            query=state.query,
            exit_code=EXIT_USAGE,
        )
    resolved_fight_id = request.fight_id or parsed_ref.fight_id
    if resolved_fight_id is None:
        _fail_cooldown_packet(
            ctx,
            code="missing_fight",
            message="Pass --fight-id or provide a report URL containing fight=<id>.",
            query={**state.query, "report_code": parsed_ref.code},
            exit_code=EXIT_USAGE,
        )
    state.report_code = parsed_ref.code
    state.fight_id = resolved_fight_id
    state.query.update(
        {
            "report_code": parsed_ref.code,
            "fight_id": resolved_fight_id,
            "report_type": parsed_ref.report_type,
        }
    )
    state.lorrgs_fight_args = ["user-report-fights", request.report_ref, "--fight", str(resolved_fight_id)]
    if parsed_ref.report_type:
        state.lorrgs_fight_args += ["--type", parsed_ref.report_type]


def _lorrgs_lookup_failure(error: Any) -> str:
    """Why Lorrgs could not supply the fight.

    Lorrgs maps 401/403/404 alike to ``not_found``, so only that code means "this report is not
    cached". A timeout, a rate limit or a transport failure must report itself instead of blaming
    the report, or an agent retries the wrong thing.
    """
    details = as_dict(error)
    if str(details.get("code") or "") == "not_found":
        return (
            "Lorrgs has no cached copy of this report, so phase markers are unavailable. Lorrgs only serves "
            "reports it has already cached; load the report at https://lorrgs.io to add them."
        )
    reason = str(details.get("message") or "").strip() or "Lorrgs returned no usable payload"
    reason = reason if reason.endswith((".", "!", "?")) else f"{reason}."
    return f"Lorrgs could not serve this report, so phase markers are unavailable: {reason}"


def _load_lorrgs_fight(request: CooldownRequest, state: CooldownState, fetch: ProviderFetch) -> None:
    result = fetch("lorrgs", state.lorrgs_fight_args, expansion=request.expansion)
    state.lorrgs_result = result
    if result.get("status") != "ok":
        state.lorrgs_unavailable = {
            "code": "lorrgs_fight_lookup_failed",
            "message": _lorrgs_lookup_failure(result.get("error")),
            "source": result.get("error"),
        }
        return
    fight = _find_lorrgs_fight(_data_of(result), state.fight_id)
    if fight is None:
        state.lorrgs_unavailable = {
            "code": "lorrgs_fight_not_found",
            "message": "Lorrgs cached this report but not the selected fight, so phase markers are unavailable.",
            "source": None,
        }
        return
    if not _available_lorrgs_players(fight):
        state.lorrgs_unavailable = {
            "code": "lorrgs_fight_has_no_players",
            "message": "Lorrgs cached this fight without its players, so phase markers are unavailable.",
            "source": None,
        }
        return
    state.lorrgs_fight = fight


def _fail_unresolved_player(
    ctx: typer.Context, state: CooldownState, code: str, *, message: str, available: list[dict[str, Any]]
) -> NoReturn:
    exit_code = EXIT_NOT_FOUND if code.endswith("_not_found") else EXIT_USAGE if code == "missing_actor" else EXIT_GENERIC
    _fail_cooldown_packet(
        ctx, code=code, message=message, query=state.query, details={"available_players": available}, exit_code=exit_code
    )


def _roster_spec_slug(actor: dict[str, Any]) -> str | None:
    """The Lorrgs spec slug of a Warcraft Logs roster row, from its class/spec identity."""
    identity = as_dict(as_dict(actor.get("class_spec_identity")).get("identity"))
    actor_class, spec_key = identity.get("actor_class"), identity.get("spec")
    if not isinstance(actor_class, str) or not isinstance(spec_key, str):
        return None
    spec = lookup_spec(spec_key, class_hint=actor_class)
    return spec.lorrgs_slug if spec is not None else None


def _select_player_without_lorrgs(
    ctx: typer.Context, request: CooldownRequest, state: CooldownState, fetch: ProviderFetch
) -> None:
    """Find the actor (--actor-id or --actor-name) in the Warcraft Logs roster of the selected fight.

    The roster row also supplies the name, class and spec Lorrgs would have; --spec-slug is needed
    only when the row names no spec. An actor the fight does not have fails instead of returning an
    empty cast list.
    """
    state.roster_args = ["report-player-details", state.report_code, "--fight-id", str(state.fight_id)]
    if request.allow_unlisted:
        state.roster_args.append("--allow-unlisted")
    state.roster_result = _cooldown_provider_payload(
        ctx,
        "warcraftlogs",
        state.roster_args,
        fetch=fetch,
        expansion=request.expansion,
        query=state.query,
        error_code="warcraftlogs_roster_failed",
        error_message="Warcraft Logs fight roster lookup failed.",
    )
    roles = as_dict(as_dict(_data_of(state.roster_result).get("player_details")).get("roles"))
    actors = [row for role in ("tanks", "healers", "dps") for row in as_list(roles.get(role)) if isinstance(row, dict)]
    actor, actor_error = _resolve_player(actors, id_key="id", actor_id=request.actor_id, actor_name=request.actor_name)
    lorrgs_reason = as_dict(state.lorrgs_unavailable).get("message")
    if actor is None:
        _fail_unresolved_player(
            ctx,
            state,
            actor_error or "actor_not_found",
            message=(
                f"{lorrgs_reason} Could not resolve the selected player in the roster of fight {state.fight_id} "
                f"of Warcraft Logs report {state.report_code}; pass --actor-id or --actor-name from "
                "details.available_players."
            ),
            available=[{key: row.get(key) for key in ("id", "name", "type")} for row in actors],
        )
    state.actor_id = int(int_or_none(actor.get("id")) or 0)
    spec_slug = (request.spec_slug or "").strip() or _roster_spec_slug(actor)
    if not spec_slug:
        _fail_cooldown_packet(
            ctx,
            code="spec_slug_missing",
            message=f"{lorrgs_reason} The Warcraft Logs roster names no spec for the selected player. Pass --spec-slug.",
            query={**state.query, "actor_id": state.actor_id},
            details={"player": {key: actor.get(key) for key in ("id", "name", "type")}},
            exit_code=EXIT_USAGE,
        )
    actor_type = actor.get("type")
    state.player = {"name": actor.get("name"), "class_slug": actor_type.lower() if isinstance(actor_type, str) else None}
    state.spec_slug = spec_slug
    state.boss_slug = request.boss_slug or _boss_slug_of_encounter(request, state, fetch)
    state.query.update(
        {"actor_id": state.actor_id, "actor_name": actor.get("name"), "spec_slug": state.spec_slug, "boss_slug": state.boss_slug}
    )


def _boss_slug_of_encounter(request: CooldownRequest, state: CooldownState, fetch: ProviderFetch) -> str | None:
    """The Lorrgs boss of the Warcraft Logs fight's encounter: Lorrgs boss ids are encounter ids.

    A failed lookup only leaves the comparison without a boss, as it was before the lookup.
    """
    encounter_id = int_or_none(as_dict(state.wcl_fight).get("encounter_id"))
    if not encounter_id:
        return None
    state.bosses_args = ["bosses"]
    state.bosses_result = fetch("lorrgs", state.bosses_args, expansion=request.expansion)
    if state.bosses_result.get("status") != "ok":
        return None
    boss = next(
        (row for row in as_list(_data_of(state.bosses_result).get("bosses")) if isinstance(row, dict) and row.get("id") == encounter_id),
        {},
    )
    slug = boss.get("full_name_slug")
    return slug if isinstance(slug, str) and slug else None


def _require_spec_of_player_class(ctx: typer.Context, state: CooldownState) -> None:
    """Spell the spec as Lorrgs does, then reject a --spec-slug of another class than the player's.

    --spec-slug takes any provider's spelling (frost-death-knight, DeathKnight-Frost, bdk), and a bare
    spec several classes share (frost) takes the player's class. A spec of another class would compare
    the wrong spec, so it fails.
    """
    player_class = state.player.get("class_slug") or _spec_class_slug(str(state.player.get("spec_slug") or ""))
    spec = lookup_spec(state.spec_slug, class_hint=player_class if isinstance(player_class, str) else None)
    if spec is not None:
        state.spec_slug = state.query["spec_slug"] = spec.lorrgs_slug
    spec_class = _spec_class_slug(state.spec_slug)
    if isinstance(player_class, str) and spec_class is not None and spec_class != player_class:
        _fail_cooldown_packet(
            ctx,
            code="invalid_query",
            message=f"--spec-slug {state.spec_slug} is a {spec_class} spec, but actor {state.actor_id} is a {player_class}.",
            query=state.query,
            details={"player": state.player},
            exit_code=EXIT_USAGE,
        )


def _select_player(ctx: typer.Context, request: CooldownRequest, state: CooldownState, fetch: ProviderFetch) -> None:
    if state.lorrgs_unavailable is not None:
        _load_warcraftlogs_fight(ctx, request, state, fetch)
        _select_player_without_lorrgs(ctx, request, state, fetch)
        _require_spec_of_player_class(ctx, state)
        return
    players = [player for player in as_list(state.lorrgs_fight.get("players")) if isinstance(player, dict)]
    player, player_error = _resolve_player(
        players, id_key="source_id", actor_id=request.actor_id, actor_name=request.actor_name
    )
    if player is None:
        _fail_unresolved_player(
            ctx,
            state,
            player_error or "actor_not_found",
            message="Could not resolve the selected player in the Lorrgs fight payload.",
            available=_available_lorrgs_players(state.lorrgs_fight),
        )
    resolved_actor_id = int_or_none(player.get("source_id"))
    if resolved_actor_id is None:
        _fail_cooldown_packet(
            ctx,
            code="actor_id_missing",
            message="The selected player did not include a report-local source id.",
            query=state.query,
            details={"player": player},
        )
    resolved_spec_slug = request.spec_slug or (player.get("spec_slug") if isinstance(player.get("spec_slug"), str) else None)
    if resolved_spec_slug is None:
        _fail_cooldown_packet(
            ctx,
            code="spec_slug_missing",
            message="The selected player did not include a Lorrgs spec slug. Pass --spec-slug.",
            query={**state.query, "actor_id": resolved_actor_id},
            details={"player": player},
            exit_code=EXIT_USAGE,
        )
    raw_boss = state.lorrgs_fight.get("boss")
    state.boss = as_dict(raw_boss)
    state.player = player
    state.actor_id = resolved_actor_id
    state.spec_slug = resolved_spec_slug
    state.boss_slug = request.boss_slug or (state.boss.get("boss_slug") if isinstance(state.boss.get("boss_slug"), str) else None)
    state.query.update(
        {
            "actor_id": resolved_actor_id,
            "actor_name": player.get("name"),
            "spec_slug": resolved_spec_slug,
            "boss_slug": state.boss_slug,
        }
    )
    _require_spec_of_player_class(ctx, state)


# The fight's phase transitions and the encounters' phase names, the Warcraft Logs source of phase
# windows when Lorrgs has no copy of the report. `report-fights` does not carry them.
_WARCRAFTLOGS_PHASES_QUERY = (
    "query CooldownPacketPhases($code: String!, $fightIDs: [Int], $allowUnlisted: Boolean) {"
    " reportData { report(code: $code, allowUnlisted: $allowUnlisted) {"
    " phases { encounterID phases { id name } }"
    " fights(fightIDs: $fightIDs) { id encounterID startTime endTime phaseTransitions { id startTime } } } } }"
)


def _load_warcraftlogs_phases(request: CooldownRequest, state: CooldownState, fetch: ProviderFetch) -> None:
    """Phase windows from the Warcraft Logs fight, when it has phase transitions; a failed lookup only
    leaves the phase unavailable, as it was before the lookup."""
    state.wcl_phases_args = [
        "graphql", "--query", _WARCRAFTLOGS_PHASES_QUERY, "--report-code", state.report_code, "--fight-id", str(state.fight_id),
    ]
    if request.allow_unlisted:
        state.wcl_phases_args.append("--allow-unlisted")
    state.wcl_phases_result = fetch("warcraftlogs", state.wcl_phases_args, expansion=request.expansion)
    if state.wcl_phases_result.get("status") == "ok":
        report = as_dict(as_dict(_data_of(state.wcl_phases_result).get("reportData")).get("report"))
        state.phase_windows = warcraftlogs_phase_windows(report, state.fight_id)


def _top_parse_phases_query(count: int) -> str:
    """One aliased query for the phase transitions of ``count`` top-parse fights (``$c<i>``/``$f<i>``)."""
    variables = ", ".join(f"$c{index}: String!, $f{index}: [Int]" for index in range(count))
    fight_fields = "id encounterID startTime endTime phaseTransitions { id startTime }"
    reports = " ".join(
        f"r{index}: report(code: $c{index}) {{ fights(fightIDs: $f{index}) {{ {fight_fields} }} }}" for index in range(count)
    )
    return f"query CooldownPacketTopParsePhases({variables}) {{ reportData {{ {reports} }} }}"


def _top_parse_warcraftlogs_windows(
    request: CooldownRequest, state: CooldownState, fetch: ProviderFetch, ranking: dict[str, Any]
) -> dict[tuple[str, int], list[dict[str, Any]]]:
    """Each sampled top parse's Warcraft Logs phase windows, numbered as the player's fight's are.

    Lorrgs places its own phase markers (on some bosses only the intermission ends), so a Lorrgs P2
    is not the Warcraft Logs P2. A failed lookup leaves every sample without a window.
    """
    fights = top_parse_fights(ranking, sample_limit=request.sample_limit, analyzed_fight=(state.report_code, state.fight_id))
    if not fights:
        return {}
    variables: dict[str, Any] = {}
    for index, (code, fight_id) in enumerate(fights):
        variables.update({f"c{index}": code, f"f{index}": [fight_id]})
    state.sample_phases_args = ["graphql", "--query", _top_parse_phases_query(len(fights)), "--variables-json", json.dumps(variables)]
    state.sample_phases_result = fetch("warcraftlogs", state.sample_phases_args, expansion=request.expansion)
    if state.sample_phases_result.get("status") != "ok":
        return {}
    answers = as_dict(_data_of(state.sample_phases_result).get("reportData"))
    phase_names = as_dict(as_dict(_data_of(state.wcl_phases_result).get("reportData")).get("report")).get("phases")
    return {
        (code, fight_id): warcraftlogs_phase_windows(
            {"phases": phase_names, "fights": as_list(as_dict(answers.get(f"r{index}")).get("fights"))}, fight_id
        )
        for index, (code, fight_id) in enumerate(fights)
        if isinstance(answers.get(f"r{index}"), dict)
    }


def _select_phase(ctx: typer.Context, request: CooldownRequest, state: CooldownState, fetch: ProviderFetch) -> None:
    if state.lorrgs_unavailable is not None:
        _load_warcraftlogs_phases(request, state, fetch)
        if not state.phase_windows:
            # No phase markers from either provider; `phase.status` says so and the cast sections
            # fall back to the whole fight rather than silently reporting an empty phase.
            return
        state.phase_source = "warcraftlogs"
    else:
        state.phase_source = "lorrgs"
        state.lorrgs_phases = as_list(state.lorrgs_fight.get("phases"))
        state.phase_windows = build_phase_windows(state.lorrgs_phases, state.lorrgs_fight.get("duration"))
    window = selected_phase_window(state.phase_windows, request.phase)
    if window is None:
        _fail_cooldown_packet(
            ctx,
            code="phase_not_found",
            message=f"The selected phase index is not present in the {state.phase_source} phase markers.",
            query=state.query,
            details={"phase_windows": state.phase_windows, "raw_phase_markers": raw_phase_markers(state.lorrgs_phases)},
        )
    state.selected_window = window


def _load_spell_catalogs(ctx: typer.Context, request: CooldownRequest, state: CooldownState, fetch: ProviderFetch) -> None:
    state.spec_spells_args = ["spec-spells", state.spec_slug]
    state.spec_spells_result = _cooldown_provider_payload(
        ctx,
        "lorrgs",
        state.spec_spells_args,
        fetch=fetch,
        expansion=request.expansion,
        query=state.query,
        error_code="lorrgs_spec_spells_failed",
        error_message="Lorrgs spec spell metadata lookup failed. Pass --spell-id for each cooldown to track.",
        # Explicit --spell-id values are the tracked set, so the metadata only names them.
        required=not request.spell_ids,
    )
    state.cooldown_catalog = spell_catalog(_data_of(state.spec_spells_result))
    state.tracked_ids = tracked_spell_ids(state.cooldown_catalog, request.spell_ids)
    if not state.tracked_ids:
        _fail_cooldown_packet(
            ctx,
            code="no_tracked_spells",
            message="No tracked cooldown spell ids were available. Pass --spell-id or choose a spec with Lorrgs spell metadata.",
            query=state.query,
        )
    if state.boss_slug:
        state.boss_spells_args = ["boss-spells", state.boss_slug]
        state.boss_spells_result = _cooldown_provider_payload(
            ctx,
            "lorrgs",
            state.boss_spells_args,
            fetch=fetch,
            expansion=request.expansion,
            query=state.query,
            error_code="lorrgs_boss_spells_failed",
            error_message="Lorrgs boss spell metadata lookup failed.",
            required=False,
        )
    state.boss_catalog = spell_catalog(_data_of(state.boss_spells_result))


def _load_warcraftlogs_fight(ctx: typer.Context, request: CooldownRequest, state: CooldownState, fetch: ProviderFetch) -> None:
    """Find the fight in the Warcraft Logs report before its roster or casts, so a fight it lacks fails ``fight_not_found``."""
    state.wcl_fights_args = ["report-fights", state.report_code]
    if request.allow_unlisted:
        state.wcl_fights_args.append("--allow-unlisted")
    state.wcl_fights_result = _cooldown_provider_payload(
        ctx,
        "warcraftlogs",
        state.wcl_fights_args,
        fetch=fetch,
        expansion=request.expansion,
        query=state.query,
        error_code="warcraftlogs_fights_failed",
        error_message="Warcraft Logs fight lookup failed.",
    )
    state.wcl_fight = _find_warcraftlogs_fight(state.wcl_fights_result, state.fight_id)
    if state.wcl_fight is None:
        _fail_cooldown_packet(
            ctx,
            code="fight_not_found",
            message=f"Warcraft Logs report {state.report_code} has no fight {state.fight_id}.",
            query=state.query,
            details={
                "available_fight_ids": [
                    fight.get("id") for fight in as_list(_data_of(state.wcl_fights_result).get("fights"))
                    if isinstance(fight, dict)
                ]
            },
            exit_code=EXIT_NOT_FOUND,
        )
    fight_start_time_ms = int_or_none(state.wcl_fight.get("start_time"))
    if fight_start_time_ms is None:
        _fail_cooldown_packet(
            ctx,
            code="fight_start_missing",
            message="Warcraft Logs did not return a fight start timestamp for relative event conversion.",
            query=state.query,
            details={"fight": state.wcl_fight},
        )
    state.fight_start_time_ms = fight_start_time_ms


def _load_warcraftlogs_casts(ctx: typer.Context, request: CooldownRequest, state: CooldownState, fetch: ProviderFetch) -> None:
    if state.wcl_fight is None:
        _load_warcraftlogs_fight(ctx, request, state, fetch)
    state.events_args = [
        "report-events",
        state.report_code,
        "--fight-id",
        str(state.fight_id),
        "--source-id",
        str(state.actor_id),
        "--data-type",
        "casts",
        "--limit",
        str(request.event_limit),
    ]
    if request.allow_unlisted:
        state.events_args.append("--allow-unlisted")
    state.events_result = _cooldown_provider_payload(
        ctx,
        "warcraftlogs",
        state.events_args,
        fetch=fetch,
        expansion=request.expansion,
        query=state.query,
        error_code="warcraftlogs_events_failed",
        error_message="Warcraft Logs cast-event lookup failed.",
    )
    if not request.spell_ids:
        state.received_aura_ids = received_aura_spell_ids(
            state.cooldown_catalog, state.tracked_ids, _data_of(state.events_result), source_id=state.actor_id
        )
        state.tracked_ids -= state.received_aura_ids
    state.player_casts = normalize_warcraftlogs_actor_casts(
        _data_of(state.events_result),
        fight_start_time_ms=state.fight_start_time_ms,
        catalog=state.cooldown_catalog,
        spell_ids=state.tracked_ids,
        source_id=state.actor_id,
        window=state.selected_window,
    )
    state.player_casts["complete"] = (
        not _provider_graphql_warnings(state.events_result) and state.player_casts.get("next_page_timestamp") is None
    )


# Warcraft Logs raid difficulty ids that Lorrgs ranks, as Lorrgs difficulty slugs.
_LORRGS_DIFFICULTY_BY_WARCRAFTLOGS_ID = {4: "heroic", 5: "mythic"}


def _ranking_difficulty(request: CooldownRequest, state: CooldownState) -> str | None:
    """``--difficulty`` when passed, otherwise the analyzed fight's own difficulty, never a guess."""
    if request.difficulty:
        return request.difficulty
    fight_difficulty = as_dict(state.wcl_fight).get("difficulty")
    difficulty = _LORRGS_DIFFICULTY_BY_WARCRAFTLOGS_ID.get(fight_difficulty) if isinstance(fight_difficulty, int) else None
    if difficulty is None:
        state.comparison_reason = "unranked_difficulty"
        state.comparison_note = (
            f"The Warcraft Logs fight's difficulty is {fight_difficulty!r}, which names no Lorrgs ranking "
            "(heroic or mythic), so the top-parse comparison was skipped. Pass --difficulty to compare anyway."
        )
    return difficulty


def _comparison_difficulty(request: CooldownRequest, state: CooldownState) -> str | None:
    """The Lorrgs difficulty to rank against, or ``None`` with the reason recorded on ``state``."""
    if request.sample_limit == 0:
        state.comparison_reason = "disabled_by_sample_limit"
        return None
    if not state.boss_slug:
        state.comparison_reason = "no_boss_slug"
        state.comparison_note = (
            "Lorrgs lists no boss for this fight's Warcraft Logs encounter"
            if state.lorrgs_unavailable is not None
            else "Lorrgs did not name this fight's boss"
        ) + ", so the top-parse comparison was skipped. Pass --boss-slug (see `warcraft lorrgs bosses`) to compare anyway."
        return None
    return _ranking_difficulty(request, state)


def _load_ranking_comparison(ctx: typer.Context, request: CooldownRequest, state: CooldownState, fetch: ProviderFetch) -> None:
    difficulty = _comparison_difficulty(request, state)
    state.query["difficulty"] = difficulty or request.difficulty
    if difficulty is not None:
        state.ranking_args = ["spec-ranking", state.spec_slug, str(state.boss_slug), "--difficulty", difficulty]
        if request.metric:
            state.ranking_args += ["--metric", request.metric]
        state.ranking_result = _cooldown_provider_payload(
            ctx,
            "lorrgs",
            state.ranking_args,
            fetch=fetch,
            expansion=request.expansion,
            query=state.query,
            error_code="lorrgs_spec_ranking_failed",
            error_message="Lorrgs top-parse ranking lookup failed.",
            required=False,
        )
    raw_ranking = (
        _data_of(state.ranking_result)
        if isinstance(state.ranking_result, dict) and state.ranking_result.get("status") == "ok"
        else None
    )
    ranking_payload = raw_ranking if isinstance(raw_ranking, dict) else None
    if difficulty is not None and ranking_payload is None:
        state.comparison_reason = "lorrgs_spec_ranking_failed"
        state.comparison_note = (
            "Lorrgs top-parse comparison was unavailable; inspect sources.lorrgs_spec_ranking.error for details."
        )
    warcraftlogs_phases = None
    if state.phase_source == "warcraftlogs" and state.selected_window is not None and ranking_payload is not None:
        warcraftlogs_phases = (state.selected_window, _top_parse_warcraftlogs_windows(request, state, fetch, ranking_payload))
    state.comparison = top_parse_samples(
        ranking_payload,
        phase=request.phase,
        sample_limit=request.sample_limit,
        spell_catalog=state.cooldown_catalog,
        boss_catalog=state.boss_catalog,
        spell_ids=state.tracked_ids,
        player_phase_count=len(state.phase_windows),
        analyzed_fight=(state.report_code, state.fight_id),
        warcraftlogs_phases=warcraftlogs_phases,
    )
    _comparison_disclosures(request, state)


def _comparison_disclosures(request: CooldownRequest, state: CooldownState) -> None:
    """Distinguish missing samples and incomplete player evidence from a ready comparison."""
    if state.comparison["status"] == "no_phase_data":
        reasons = {sample["phase_unavailable_reason"] for sample in state.comparison["samples"]}
        state.comparison_reason = reasons.pop() if len(reasons) == 1 else "no_sample_has_phase"
        hint = (
            "; Lorrgs ranking fights often carry no phase markers"
            if state.comparison_reason == "top_parse_has_no_phase_markers" and state.phase_source != "warcraftlogs"
            else ""
        )
        state.comparison_note = (
            f"No top-parse sample has a P{request.phase} window (see comparison.samples[].phase_unavailable_reason"
            f"{hint}), so no top-parse casts were compared for this phase."
        )
    if state.comparison["status"] == "no_samples":
        state.comparison_reason = "no_top_parse_samples"
        state.comparison_note = "Lorrgs returned no usable top-parse samples, so no top-parse casts were compared."
    if state.comparison["status"] == "ready" and not state.player_casts["complete"]:
        state.comparison["status"] = "partial"
        state.comparison_reason = "incomplete_player_casts"
        state.comparison_note = (
            "Player cast events are incomplete; keep the top-parse sample evidence "
            "but do not treat player counts as complete."
        )
    state.comparison["player_casts_complete"] = state.player_casts["complete"]
    state.comparison["reason"] = None if state.comparison["status"] == "ready" else state.comparison_reason


def _source_refs(state: CooldownState) -> dict[str, Any]:
    return {
        "lorrgs_user_report_fights": _provider_source(state.lorrgs_result, command="lorrgs", args=state.lorrgs_fight_args),
        "lorrgs_spec_spells": _provider_source(state.spec_spells_result, command="lorrgs", args=state.spec_spells_args),
        "lorrgs_boss_spells": _provider_source(state.boss_spells_result, command="lorrgs", args=state.boss_spells_args or []),
        "warcraftlogs_report_fights": _provider_source(
            state.wcl_fights_result, command="warcraftlogs", args=state.wcl_fights_args
        ),
        "warcraftlogs_report_events": _provider_source(state.events_result, command="warcraftlogs", args=state.events_args),
        "warcraftlogs_report_player_details": _provider_source(
            state.roster_result, command="warcraftlogs", args=state.roster_args
        ),
        "warcraftlogs_phase_transitions": _provider_source(
            state.wcl_phases_result, command="warcraftlogs", args=state.wcl_phases_args
        ),
        "lorrgs_spec_ranking": _provider_source(state.ranking_result, command="lorrgs", args=state.ranking_args or []),
        "warcraftlogs_top_parse_phase_transitions": _provider_source(
            state.sample_phases_result, command="warcraftlogs", args=state.sample_phases_args
        ),
        "lorrgs_bosses": _provider_source(state.bosses_result, command="lorrgs", args=state.bosses_args),
    }


def _lorrgs_section(state: CooldownState) -> dict[str, Any]:
    """Whether the cached Lorrgs report backed this packet, and what is missing when it did not."""
    if state.lorrgs_unavailable is None:
        return {"status": "ok", "reason": None, "message": None, "source": None, "missing": []}
    return {
        "status": "unavailable",
        "reason": state.lorrgs_unavailable["code"],
        "message": state.lorrgs_unavailable["message"],
        "source": state.lorrgs_unavailable["source"],
        "missing": [
            *([] if state.phase_windows else ["phase_windows"]),
            "boss_casts",
            "lorrgs_cached_player_timeline",
            "player_deaths",
            "fight_metadata",
        ],
    }


def _notes(state: CooldownState, lorrgs_player_casts: list[Any]) -> list[str]:
    """What the packet's evidence is and where it is incomplete; each note matches the payload."""
    notes = [
        "Player casts come from Warcraft Logs cast events so cached Lorrgs user reports do not need per-player timeline generation.",
    ]
    notes.extend(
        f"{source['command']} returned partial GraphQL errors; inspect sources.{name}.graphql_warnings "
        "before treating its evidence as complete."
        for name, source in _source_refs(state).items() if source["graphql_warnings"]
    )
    if state.phase_source == "lorrgs":
        notes.append("Phase windows are derived from Lorrgs phase transition markers; labels are one-based P1/P2/etc.")
    elif state.phase_source == "warcraftlogs":
        notes.append(
            "Lorrgs did not supply this report, so phase windows come from the Warcraft Logs fight's phase "
            "transitions; labels are one-based P1/P2/etc. in order, and each window's phase_id and name are "
            "the encounter phase it is. Top-parse samples are segmented by their own Warcraft Logs phase "
            "transitions the same way, not by Lorrgs markers, and a sample counts only when its window of "
            "that number is the same encounter phase."
        )
    if state.received_aura_ids:
        notes.append(
            "Externals this player did not cast in the fight (cooldowns.received_auras, e.g. Power Infusion or "
            "Bloodlust from another player) are left out of both sides: top parses record them as auras received, "
            "and the player's side holds only the player's own casts, so they would always read as missed."
        )
    if state.comparison.get("excluded_analyzed_fight"):
        notes.append(
            "The analyzed fight is itself a top parse; it is left out of the samples so the player is not "
            "compared with themselves."
        )
    if state.comparison.get("status") in {"ready", "partial"}:
        notes.append("Top-parse samples are comparison evidence, not universal cooldown recommendations.")
        if state.comparison["phase_sample_count"] < state.comparison["sample_count"]:
            notes.append(
                "Some top-parse samples have no window for this phase and are left out of "
                "selected_phase_spell_frequency; see comparison.samples[].phase_unavailable_reason."
            )
    if state.lorrgs_unavailable is not None:
        if state.selected_window is None:
            notes.append(
                "Neither Lorrgs nor the Warcraft Logs fight supplied phase markers, so the requested --phase "
                "was not applied, cooldowns.player_casts covers the whole fight and every selected-phase "
                "section is empty. See the lorrgs section for the reason."
            )
        notes.append(
            "Without the Lorrgs roster the player's name and class come from the Warcraft Logs roster "
            "of the fight, and player.deaths is null: deaths come only from the Lorrgs timeline."
        )
    elif not lorrgs_player_casts:
        notes.append(
            "Cached Lorrgs user-report data did not include player cooldown casts for this actor; "
            "Warcraft Logs events fill that gap."
        )
    if state.player_casts.get("next_page_timestamp") is not None:
        notes.append("Warcraft Logs returned next_page_timestamp; increase --event-limit or paginate before treating counts as complete.")
    if state.spec_spells_result is not None and state.spec_spells_result.get("status") != "ok":
        notes.append(
            "Lorrgs spec spell metadata was unavailable, so the --spell-id cooldowns are named spell:<id>; "
            "inspect sources.lorrgs_spec_spells.error for details."
        )
    if isinstance(state.boss_spells_result, dict) and state.boss_spells_result.get("status") != "ok":
        notes.append(
            "Lorrgs boss spell metadata was unavailable, so boss casts are named spell:<id>; "
            "inspect sources.lorrgs_boss_spells.error for details."
        )
    if state.comparison_note is not None:
        notes.append(state.comparison_note)
    return notes


def _spec_class_slug(spec_slug: str) -> str | None:
    """The class half of a Lorrgs spec slug, which is ``<class>-<spec>`` (``deathknight-blood``)."""
    class_slug, separator, _spec = spec_slug.partition("-")
    return class_slug if separator and class_slug else None


def _packet_payload(state: CooldownState) -> dict[str, Any]:
    raw_player_casts = state.player.get("casts")
    lorrgs_player_casts: list[Any] = as_list(raw_player_casts)
    raw_boss_casts = state.boss.get("casts")
    boss_casts: list[Any] = as_list(raw_boss_casts)
    return {
        "ok": True,
        "provider": "warcraft",
        "kind": "cooldown_packet",
        "query": state.query,
        "report_url": f"https://www.warcraftlogs.com/reports/{state.report_code}#fight={state.fight_id}",
        "lorrgs": _lorrgs_section(state),
        "phase": {
            "status": "unavailable" if state.selected_window is None else "ready",
            "source": state.phase_source,
            "unavailable_reason": state.lorrgs_unavailable["code"]
            if state.lorrgs_unavailable and state.selected_window is None
            else None,
            # `requested` is the --phase the caller asked for; `selected` is null when no phase
            # markers existed, which is the only case where the request went unapplied.
            "requested": state.query.get("phase"),
            "selected": state.selected_window,
            "windows": state.phase_windows,
            "raw_markers": raw_phase_markers(state.lorrgs_phases),
        },
        "fight": {
            "lorrgs": {
                "fight_id": state.lorrgs_fight.get("fight_id"),
                "duration_ms": state.lorrgs_fight.get("duration"),
                "difficulty": state.lorrgs_fight.get("difficulty"),
                "kill": state.lorrgs_fight.get("kill"),
                "percent": state.lorrgs_fight.get("percent"),
                "start_time": state.lorrgs_fight.get("start_time"),
            },
            "warcraftlogs": state.wcl_fight,
        },
        "player": {
            "name": state.player.get("name") or state.query.get("actor_name"),
            "source_id": state.actor_id,
            "spec_slug": state.spec_slug,
            "class_slug": state.player.get("class_slug") or _spec_class_slug(state.spec_slug),
            "total": state.player.get("total"),
            "deaths": _phase_deaths(state),
        },
        "boss": {
            "boss_slug": state.boss_slug,
            "selected_phase_casts": normalize_lorrgs_casts(
                boss_casts,
                catalog=state.boss_catalog,
                window=state.selected_window,
            ),
        },
        "cooldowns": {
            "tracked_spell_count": len(state.tracked_ids),
            "tracked_spells": [spell_summary(spell_id, catalog=state.cooldown_catalog) for spell_id in sorted(state.tracked_ids)],
            "received_auras": [spell_summary(spell_id, catalog=state.cooldown_catalog) for spell_id in sorted(state.received_aura_ids)],
            "player_casts": state.player_casts,
            "lorrgs_cached_player_timeline": {
                "raw_cast_count": len(lorrgs_player_casts),
                "selected_phase_casts": normalize_lorrgs_casts(
                    lorrgs_player_casts,
                    catalog=state.cooldown_catalog,
                    window=state.selected_window,
                    spell_ids=state.tracked_ids,
                ),
            },
        },
        "comparison": state.comparison,
        "sources": _source_refs(state),
        "notes": _notes(state, lorrgs_player_casts),
    }


def emit_cooldown_packet(ctx: typer.Context, request: CooldownRequest, *, fetch: ProviderFetch) -> None:
    """Run the provider sequence and emit the packet, or exit 1 with a structured failure envelope."""
    state = CooldownState()
    _resolve_reference(ctx, request, state)
    _load_lorrgs_fight(request, state, fetch)
    _select_player(ctx, request, state, fetch)
    _select_phase(ctx, request, state, fetch)
    _load_spell_catalogs(ctx, request, state, fetch)
    _load_warcraftlogs_casts(ctx, request, state, fetch)
    _load_ranking_comparison(ctx, request, state, fetch)
    emit(ctx, wrapper_envelope(ctx.info_name or "", _packet_payload(state)))

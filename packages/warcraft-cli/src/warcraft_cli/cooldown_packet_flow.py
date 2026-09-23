"""Provider orchestration behind `warcraft cooldown-packet`.

The Typer command owns the flag surface; this module owns the sequence of provider calls and the
packet it assembles. Provider calls arrive as an injected ``fetch`` callable so the command keeps a
single seam (``warcraft_cli.main._provider_payload_result``) and this module never imports it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, NoReturn, Protocol

import typer
from warcraft_core.cli import emit, fail
from warcraft_core.exit_codes import EXIT_GENERIC, EXIT_USAGE
from warcraft_core.shapes import as_dict, as_list

from warcraft_cli.cooldown_packet import (
    build_phase_windows,
    normalize_lorrgs_casts,
    normalize_warcraftlogs_actor_casts,
    raw_phase_markers,
    selected_phase_window,
    source_command,
    spell_catalog,
    spell_summary,
    top_parse_samples,
    tracked_spell_ids,
)
from warcraft_cli.providers import (
    parse_lorrgs_report_reference,
    provider_payload_data,
    source_exit_code,
    wrapper_envelope,
)


def _emit(ctx: typer.Context, payload: Mapping[str, Any], *, err: bool = False) -> None:
    emit(ctx, wrapper_envelope(ctx.info_name or "", payload), err=err)



class ProviderFetch(Protocol):
    """Runs one provider command and returns ``{provider, status, payload, error?, exit_code}``."""

    def __call__(self, provider: str, args: list[str], *, expansion: str | None) -> dict[str, Any]: ...


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


def _provider_source(provider_result: dict[str, Any] | None, *, command: str, args: list[str]) -> dict[str, Any]:
    raw_payload = provider_result.get("payload") if isinstance(provider_result, dict) else None
    payload: dict[str, Any] = as_dict(raw_payload)
    raw_provenance = payload.get("provenance")
    provenance: dict[str, Any] = as_dict(raw_provenance)
    raw_report = _data_of(provider_result).get("report")
    report: dict[str, Any] = as_dict(raw_report)
    return {
        "provider": provider_result.get("provider") if isinstance(provider_result, dict) else None,
        "status": provider_result.get("status") if isinstance(provider_result, dict) else "not_requested",
        "command": source_command(command, args),
        "source_url": provenance.get("source_url"),
        "report": report or None,
        "error": provider_result.get("error") if isinstance(provider_result, dict) else None,
    }


def _find_lorrgs_fight(data: dict[str, Any], fight_id: int) -> dict[str, Any] | None:
    raw_fights = data.get("fights")
    fights: list[Any] = as_list(raw_fights)
    for fight in fights:
        if isinstance(fight, dict) and fight.get("fight_id") == fight_id:
            return fight
    return fights[0] if len(fights) == 1 and isinstance(fights[0], dict) else None


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


def _resolve_lorrgs_player(
    fight: dict[str, Any],
    *,
    actor_id: int | None,
    actor_name: str | None,
) -> tuple[dict[str, Any] | None, str | None]:
    raw_players = fight.get("players")
    players = [player for player in raw_players if isinstance(player, dict)] if isinstance(raw_players, list) else []
    if actor_id is not None:
        for player in players:
            if _cooldown_int(player.get("source_id")) == actor_id:
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


def _phase_deaths(deaths: Any, *, window: dict[str, Any] | None) -> dict[str, Any]:
    raw = as_list(deaths)
    selected = []
    for death in raw:
        if not isinstance(death, dict):
            continue
        timestamp = _cooldown_int(death.get("ts")) or _cooldown_int(death.get("timestamp"))
        if timestamp is None or window is None:
            continue
        start_ms = _cooldown_int(window.get("start_ms"))
        end_ms = _cooldown_int(window.get("end_ms"))
        if start_ms is not None and end_ms is not None and start_ms <= timestamp < end_ms:
            selected.append(death)
    return {"count": len(raw), "raw": raw, "selected_phase_count": len(selected), "selected_phase": selected}


def _cooldown_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    return None




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
    lorrgs_phases: list[Any] = field(default_factory=list)
    phase_windows: list[dict[str, Any]] = field(default_factory=list)
    selected_window: dict[str, Any] | None = None
    # Set when the cached Lorrgs user report is missing: the packet keeps its Warcraft Logs half.
    lorrgs_unavailable: dict[str, Any] | None = None
    spec_spells_args: list[str] = field(default_factory=list)
    spec_spells_result: dict[str, Any] | None = None
    cooldown_catalog: dict[int, dict[str, Any]] = field(default_factory=dict)
    tracked_ids: set[int] = field(default_factory=set)
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
    # Set when the comparison was skipped because no Lorrgs difficulty could be named for the fight.
    unranked_difficulty_note: str | None = None
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


_LORRGS_FALLBACK_ADVICE = (
    "Re-run with --actor-id <report source id> and --spec-slug <lorrgs spec slug> to build the "
    "Warcraft Logs half of the packet without Lorrgs phase context."
)
_LORRGS_UNCACHED_ADVICE = (
    "Lorrgs only serves reports it has already cached, which most guild and private reports are not. "
    f"{_LORRGS_FALLBACK_ADVICE} Or load the report at https://lorrgs.io first."
)


def _lorrgs_lookup_failure(error: Any) -> tuple[str, str]:
    """Why Lorrgs could not supply the fight, as ``(message, advice)``.

    Lorrgs maps 401/403/404 alike to ``not_found``, so only that code means "this report is not
    cached". A timeout, a rate limit or a transport failure must report itself instead of blaming
    the report, or an agent retries the wrong thing.
    """
    details = as_dict(error)
    if str(details.get("code") or "") == "not_found":
        return (
            "Lorrgs has no cached copy of this report, so phase markers are unavailable.",
            _LORRGS_UNCACHED_ADVICE,
        )
    reason = str(details.get("message") or "").strip() or "Lorrgs returned no usable payload"
    reason = reason if reason.endswith((".", "!", "?")) else f"{reason}."
    return (
        f"Lorrgs could not serve this report, so phase markers are unavailable: {reason}",
        _LORRGS_FALLBACK_ADVICE,
    )


def _degrade_without_lorrgs(
    ctx: typer.Context,
    request: CooldownRequest,
    state: CooldownState,
    *,
    code: str,
    message: str,
    advice: str,
    source: Any,
    exit_code: int,
) -> None:
    """Continue without the cached Lorrgs report when the caller supplied what Lorrgs would have.

    Lorrgs supplies phase markers, the actor's report-local source id, and the spec slug. Only the
    last two can come from flags, so without them there is nothing left to build and the command
    fails naming the precondition instead of pretending.
    """
    if request.actor_id is None or not (request.spec_slug or "").strip():
        _fail_cooldown_packet(
            ctx,
            code=code,
            message=f"{message} {advice}",
            query=state.query,
            details={
                "source": source,
                "provider": "lorrgs",
                "required_flags": ["--actor-id", "--spec-slug"],
            },
            exit_code=exit_code,
        )
    state.lorrgs_unavailable = {"code": code, "message": message, "source": source}


def _load_lorrgs_fight(ctx: typer.Context, request: CooldownRequest, state: CooldownState, fetch: ProviderFetch) -> None:
    result = fetch("lorrgs", state.lorrgs_fight_args, expansion=request.expansion)
    state.lorrgs_result = result
    if result.get("status") != "ok":
        message, advice = _lorrgs_lookup_failure(result.get("error"))
        _degrade_without_lorrgs(
            ctx,
            request,
            state,
            code="lorrgs_fight_lookup_failed",
            message=message,
            advice=advice,
            source=result.get("error"),
            exit_code=source_exit_code(result),
        )
        return
    fight = _find_lorrgs_fight(_data_of(result), state.fight_id)
    if fight is None:
        _degrade_without_lorrgs(
            ctx,
            request,
            state,
            code="lorrgs_fight_not_found",
            message="Lorrgs cached this report but not the selected fight, so phase markers are unavailable.",
            advice=_LORRGS_FALLBACK_ADVICE,
            source=None,
            exit_code=EXIT_GENERIC,
        )
        return
    state.lorrgs_fight = fight


def _select_player_without_lorrgs(request: CooldownRequest, state: CooldownState) -> None:
    """Take the actor and spec from the flags ``_degrade_without_lorrgs`` already required."""
    state.actor_id = int(request.actor_id or 0)
    state.spec_slug = str(request.spec_slug or "")
    state.boss_slug = request.boss_slug
    state.query.update(
        {
            "actor_id": state.actor_id,
            "spec_slug": state.spec_slug,
            "boss_slug": state.boss_slug,
        }
    )


def _select_player(ctx: typer.Context, request: CooldownRequest, state: CooldownState) -> None:
    if state.lorrgs_unavailable is not None:
        _select_player_without_lorrgs(request, state)
        return
    player, player_error = _resolve_lorrgs_player(
        state.lorrgs_fight, actor_id=request.actor_id, actor_name=request.actor_name
    )
    if player is None:
        _fail_cooldown_packet(
            ctx,
            code=player_error or "actor_not_found",
            message="Could not resolve the selected player in the Lorrgs fight payload.",
            query=state.query,
            details={"available_players": _available_lorrgs_players(state.lorrgs_fight)},
        )
    resolved_actor_id = _cooldown_int(player.get("source_id"))
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


def _select_phase(ctx: typer.Context, request: CooldownRequest, state: CooldownState) -> None:
    if state.lorrgs_unavailable is not None:
        # No phase markers exist without Lorrgs; `phase.status` says so and the cast sections fall
        # back to the whole fight rather than silently reporting an empty phase.
        return
    raw_phases = state.lorrgs_fight.get("phases")
    state.lorrgs_phases = as_list(raw_phases)
    state.phase_windows = build_phase_windows(state.lorrgs_phases, state.lorrgs_fight.get("duration"))
    window = selected_phase_window(state.phase_windows, request.phase)
    if window is None:
        _fail_cooldown_packet(
            ctx,
            code="phase_not_found",
            message="The selected phase index is not present in the Lorrgs phase markers.",
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
        error_message="Lorrgs spec spell metadata lookup failed.",
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


def _load_warcraftlogs_casts(ctx: typer.Context, request: CooldownRequest, state: CooldownState, fetch: ProviderFetch) -> None:
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
    fight_start_time_ms = _cooldown_int(state.wcl_fight.get("start_time") if isinstance(state.wcl_fight, dict) else None)
    if fight_start_time_ms is None:
        _fail_cooldown_packet(
            ctx,
            code="fight_start_missing",
            message="Warcraft Logs did not return a fight start timestamp for relative event conversion.",
            query=state.query,
            details={"fight": state.wcl_fight or {}},
        )
    state.fight_start_time_ms = fight_start_time_ms
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
    state.player_casts = normalize_warcraftlogs_actor_casts(
        _data_of(state.events_result),
        fight_start_time_ms=fight_start_time_ms,
        catalog=state.cooldown_catalog,
        spell_ids=state.tracked_ids,
        window=state.selected_window,
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
        state.unranked_difficulty_note = (
            f"The Warcraft Logs fight's difficulty is {fight_difficulty!r}, which names no Lorrgs ranking "
            "(heroic or mythic), so the top-parse comparison was skipped. Pass --difficulty to compare anyway."
        )
    return difficulty


def _load_ranking_comparison(ctx: typer.Context, request: CooldownRequest, state: CooldownState, fetch: ProviderFetch) -> None:
    difficulty = _ranking_difficulty(request, state) if request.sample_limit > 0 and state.boss_slug else None
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
    state.comparison = top_parse_samples(
        ranking_payload,
        phase=request.phase,
        sample_limit=request.sample_limit,
        spell_catalog=state.cooldown_catalog,
        boss_catalog=state.boss_catalog,
        spell_ids=state.tracked_ids,
    )


def _source_refs(state: CooldownState) -> dict[str, Any]:
    return {
        "lorrgs_user_report_fights": _provider_source(state.lorrgs_result, command="lorrgs", args=state.lorrgs_fight_args),
        "lorrgs_spec_spells": _provider_source(state.spec_spells_result, command="lorrgs", args=state.spec_spells_args),
        "lorrgs_boss_spells": _provider_source(state.boss_spells_result, command="lorrgs", args=state.boss_spells_args or []),
        "warcraftlogs_report_fights": _provider_source(
            state.wcl_fights_result, command="warcraftlogs", args=state.wcl_fights_args
        ),
        "warcraftlogs_report_events": _provider_source(state.events_result, command="warcraftlogs", args=state.events_args),
        "lorrgs_spec_ranking": _provider_source(state.ranking_result, command="lorrgs", args=state.ranking_args or []),
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
        "missing": ["phase_windows", "boss_casts", "lorrgs_cached_player_timeline", "fight_metadata"],
    }


def _notes(state: CooldownState, lorrgs_player_casts: list[Any]) -> list[str]:
    notes = [
        "Phase windows are derived from Lorrgs/Warcraft Logs phase transition markers; labels are one-based P1/P2/etc.",
        "Player casts come from Warcraft Logs cast events so cached Lorrgs user reports do not need per-player timeline generation.",
        "Top-parse samples are comparison evidence, not universal cooldown recommendations.",
    ]
    if state.lorrgs_unavailable is not None:
        notes.append(
            "Lorrgs did not supply this report, so there are no phase windows: the requested --phase "
            "was not applied, cooldowns.player_casts covers the whole fight and every selected-phase "
            "section is empty. See the lorrgs section for the reason."
        )
        notes.append(
            "Without the Lorrgs roster the player is identified by flags only: player.name is "
            "--actor-name (null when it was not passed) and player.class_slug is the class half of --spec-slug."
        )
    if state.player_casts.get("next_page_timestamp") is not None:
        notes.append("Warcraft Logs returned next_page_timestamp; increase --event-limit or paginate before treating counts as complete.")
    if not lorrgs_player_casts:
        notes.append(
            "Cached Lorrgs user-report data did not include player cooldown casts for this actor; "
            "Warcraft Logs events fill that gap."
        )
    if isinstance(state.ranking_result, dict) and state.ranking_result.get("status") == "error":
        notes.append("Lorrgs top-parse comparison was unavailable; inspect sources.lorrgs_spec_ranking.error for details.")
    if state.unranked_difficulty_note is not None:
        notes.append(state.unranked_difficulty_note)
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
            "unavailable_reason": state.lorrgs_unavailable["code"] if state.lorrgs_unavailable else None,
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
            "deaths": _phase_deaths(state.player.get("deaths"), window=state.selected_window),
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
    _load_lorrgs_fight(ctx, request, state, fetch)
    _select_player(ctx, request, state)
    _select_phase(ctx, request, state)
    _load_spell_catalogs(ctx, request, state, fetch)
    _load_warcraftlogs_casts(ctx, request, state, fetch)
    _load_ranking_comparison(ctx, request, state, fetch)
    _emit(ctx, _packet_payload(state))

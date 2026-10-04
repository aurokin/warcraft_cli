from __future__ import annotations

import shlex
from collections import Counter
from typing import Any

from warcraft_core.shapes import as_dict, as_list


def build_phase_windows(phases: list[Any], duration_ms: int | float | None) -> list[dict[str, Any]]:
    """One window per phase. Without a fight duration the last window is open-ended (``end_ms`` null)."""
    duration = int(duration_ms) if isinstance(duration_ms, (int, float)) and duration_ms > 0 else None
    # Lorrgs drops the pull phase transition before serializing `phases`; stored markers are
    # transitions into P2/P3/etc., so P2 starts at markers[0], not markers[1].
    markers = sorted(
        {
            int(marker)
            for marker in (_phase_marker_ms(row) for row in phases)
            if marker is not None and marker > 0 and (duration is None or marker < duration)
        }
    )
    ends: list[int | None] = [*markers, duration]
    return [
        {
            "phase": index,
            "label": f"P{index}",
            "start_ms": start_ms,
            "end_ms": end_ms,
            "duration_ms": None if end_ms is None else end_ms - start_ms,
            "start_source": "pull" if index == 1 else "lorrgs_phase_transition",
            "end_source": "lorrgs_phase_transition" if index <= len(markers) else ("fight_end" if end_ms is not None else "unknown"),
        }
        for index, (start_ms, end_ms) in enumerate(zip([0, *markers], ends, strict=True), start=1)
    ]


def selected_phase_window(windows: list[dict[str, Any]], phase: int) -> dict[str, Any] | None:
    for window in windows:
        if window.get("phase") == phase:
            return window
    return None


def _encounter_phase_names(report: dict[str, Any], encounter_id: Any) -> dict[int | None, Any]:
    return {
        int_or_none(phase.get("id")): phase.get("name")
        for encounter in as_list(report.get("phases"))
        if isinstance(encounter, dict) and encounter.get("encounterID") == encounter_id
        for phase in as_list(encounter.get("phases"))
        if isinstance(phase, dict)
    }


def _phase_visits(fight: dict[str, Any], fight_start: int) -> list[tuple[int, int]]:
    """``(ms from the pull, phase id)`` of each phase transition, in time order."""
    return sorted(
        (max(0, timestamp - fight_start), phase)
        for row in as_list(fight.get("phaseTransitions"))
        if isinstance(row, dict)
        and (phase := int_or_none(row.get("id"))) is not None
        and (timestamp := int_or_none(row.get("startTime"))) is not None
    )


def warcraftlogs_phase_windows(report: dict[str, Any], fight_id: int) -> list[dict[str, Any]]:
    """One window per phase transition of a Warcraft Logs fight, in ms from the pull.

    ``report`` is the GraphQL ``report`` object with ``phases`` (the encounters' phase names) and
    ``fights`` (``startTime``, ``endTime``, ``phaseTransitions``). Windows are numbered in order like
    the Lorrgs ones, so ``--phase`` and the top-parse comparison mean the same window on either path;
    ``phase_id`` and ``name`` are the encounter phase each window is (1, 2, 1, 2, 1 on a returning boss).
    """
    fight = next((row for row in as_list(report.get("fights")) if isinstance(row, dict) and row.get("id") == fight_id), {})
    start, end = int_or_none(fight.get("startTime")), int_or_none(fight.get("endTime"))
    visits = [] if start is None else _phase_visits(fight, start)
    if start is None or not visits:
        return []
    names = _encounter_phase_names(report, fight.get("encounterID"))
    ends: list[int | None] = [visit_start for visit_start, _ in visits[1:]]
    ends.append(None if end is None else end - start)
    return [
        {
            "phase": index,
            "label": f"P{index}",
            "phase_id": phase,
            "name": names.get(phase),
            "start_ms": start_ms,
            "end_ms": end_ms,
            "duration_ms": None if end_ms is None else end_ms - start_ms,
            "start_source": "warcraftlogs_phase_transition",
            "end_source": "warcraftlogs_phase_transition" if index < len(visits) else ("fight_end" if end_ms is not None else "unknown"),
        }
        for index, ((start_ms, phase), end_ms) in enumerate(zip(visits, ends, strict=True), start=1)
    ]


def raw_phase_markers(phases: list[Any]) -> list[dict[str, Any]]:
    markers: list[dict[str, Any]] = []
    for index, row in enumerate(phases, start=1):
        marker = _phase_marker_ms(row)
        if marker is None:
            continue
        markers.append(
            {
                "transition_index": index,
                "timestamp_ms": marker,
                "phase_after_transition": index + 1,
                "raw": row,
            }
        )
    return markers


def spell_catalog(spell_data: dict[str, Any]) -> dict[int, dict[str, Any]]:
    """Index a Lorrgs spell map (the ``data`` body of spec-spells/boss-spells) by spell id."""
    data: dict[str, Any] = as_dict(spell_data)
    catalog: dict[int, dict[str, Any]] = {}
    for key, value in data.items():
        if not isinstance(value, dict):
            continue
        spell_id = int_or_none(value.get("spell_id")) or int_or_none(key)
        if spell_id is None:
            continue
        catalog[spell_id] = {**value, "spell_id": spell_id}
    return catalog


def tracked_spell_ids(catalog: dict[int, dict[str, Any]], explicit_spell_ids: list[int] | None) -> set[int]:
    if explicit_spell_ids:
        return {int(spell_id) for spell_id in explicit_spell_ids}
    return {
        spell_id
        for spell_id, spell in catalog.items()
        if bool(spell.get("query")) or bool(spell.get("show"))
    }


def received_aura_spell_ids(
    catalog: dict[int, dict[str, Any]], tracked: set[int], events_payload: dict[str, Any], *, source_id: int
) -> set[int]:
    """Tracked externals (Lorrgs' ``other-externals``: Power Infusion, Bloodlust, Ironbark) the actor did not cast.

    Lorrgs files them for every spec and records them on top parses as auras received, under one id
    for every variant (Heroism and Time Warp are both Bloodlust 2825). For a player who did not cast
    one, the player's side cannot hold it, so compared it would always read as missed. One the player
    cast (a priest's Power Infusion) stays compared.
    """
    cast_ids = {
        int_or_none(row.get("abilityGameID"))
        for row in as_list(events_payload.get("events"))
        if isinstance(row, dict) and row.get("type") == "cast" and int_or_none(row.get("sourceID")) == source_id
    }
    return {
        spell_id
        for spell_id in tracked - cast_ids
        if as_dict(catalog.get(spell_id)).get("spell_type") == "other-externals"
    }


def spell_summary(spell_id: int | None, *, catalog: dict[int, dict[str, Any]]) -> dict[str, Any] | None:
    if spell_id is None:
        return None
    spell = catalog.get(spell_id)
    if not isinstance(spell, dict):
        return {"spell_id": spell_id, "name": f"spell:{spell_id}"}
    return {
        "spell_id": spell_id,
        "name": spell.get("name") if isinstance(spell.get("name"), str) else f"spell:{spell_id}",
        "event_type": spell.get("event_type") if isinstance(spell.get("event_type"), str) else None,
        "spell_type": spell.get("spell_type") if isinstance(spell.get("spell_type"), str) else None,
        "cooldown_seconds": spell.get("cooldown") if isinstance(spell.get("cooldown"), (int, float)) else None,
        "duration_seconds": spell.get("duration") if isinstance(spell.get("duration"), (int, float)) else None,
        "show": bool(spell.get("show")),
        "query": bool(spell.get("query")),
        "tags": as_list(spell.get("tags")),
        "wowhead_data": spell.get("wowhead_data") if isinstance(spell.get("wowhead_data"), str) else None,
        "tooltip_info": spell.get("tooltip_info") if isinstance(spell.get("tooltip_info"), str) else None,
    }


def normalize_lorrgs_casts(
    casts: list[Any],
    *,
    catalog: dict[int, dict[str, Any]],
    window: dict[str, Any] | None = None,
    spell_ids: set[int] | None = None,
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for cast in casts:
        if not isinstance(cast, dict):
            continue
        spell_id = int_or_none(cast.get("id"))
        timestamp_ms = int_or_none(cast.get("ts"))
        if spell_id is None or timestamp_ms is None:
            continue
        if spell_ids is not None and spell_id not in spell_ids:
            continue
        if window is not None and not timestamp_in_window(timestamp_ms, window):
            continue
        normalized.append(
            {
                "timestamp_ms": timestamp_ms,
                "spell": spell_summary(spell_id, catalog=catalog),
                "cast_number": int_or_none(cast.get("c")),
                "duration_ms": int_or_none(cast.get("d")),
            }
        )
    return sorted(normalized, key=lambda row: int(row["timestamp_ms"]))


def normalize_warcraftlogs_actor_casts(
    events_payload: dict[str, Any],
    *,
    fight_start_time_ms: int,
    catalog: dict[int, dict[str, Any]],
    spell_ids: set[int],
    source_id: int,
    window: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The actor's tracked cooldown casts from one Warcraft Logs Casts page.

    The page was requested with ``--source-id``, but the rows are checked too: a cast by anyone else
    is counted in ``other_source_cast_count`` and never attributed to this actor.
    """
    raw_events = events_payload.get("events")
    events: list[Any] = as_list(raw_events)
    tracked: list[dict[str, Any]] = []
    phase_tracked: list[dict[str, Any]] = []
    by_spell: Counter[int] = Counter()
    phase_by_spell: Counter[int] = Counter()
    other_source_casts = 0
    for row in events:
        # The Casts data type also returns begincast and empowerstart/empowerend rows; only the
        # `cast` row marks one use of the spell.
        if not isinstance(row, dict) or row.get("type") != "cast":
            continue
        if int_or_none(row.get("sourceID")) != source_id:
            other_source_casts += 1
            continue
        spell_id = int_or_none(row.get("abilityGameID"))
        timestamp = int_or_none(row.get("timestamp"))
        if spell_id is None or timestamp is None or spell_id not in spell_ids:
            continue
        relative_ms = timestamp - fight_start_time_ms
        normalized = {
            "timestamp_ms": relative_ms,
            "report_timestamp_ms": timestamp,
            "spell": spell_summary(spell_id, catalog=catalog),
            "type": row.get("type"),
            "target_id": int_or_none(row.get("targetID")),
        }
        tracked.append(normalized)
        by_spell[spell_id] += 1
        if window is not None and timestamp_in_window(relative_ms, window):
            phase_tracked.append(normalized)
            phase_by_spell[spell_id] += 1
    return {
        "raw_event_count": len(events),
        "other_source_cast_count": other_source_casts,
        "next_page_timestamp": events_payload.get("next_page_timestamp"),
        "tracked_spell_count": len(spell_ids),
        "tracked_cast_count": len(tracked),
        "tracked_casts": sorted(tracked, key=lambda row: int(row["timestamp_ms"])),
        "tracked_casts_by_spell": _count_rows(by_spell, catalog=catalog),
        "selected_phase_cast_count": len(phase_tracked),
        "selected_phase_casts": sorted(phase_tracked, key=lambda row: int(row["timestamp_ms"])),
        "selected_phase_casts_by_spell": _count_rows(phase_by_spell, catalog=catalog),
    }


def _sample_for_fight(
    report: dict[str, Any],
    fight: dict[str, Any],
    *,
    phase: int,
    spell_catalog: dict[int, dict[str, Any]],
    boss_catalog: dict[int, dict[str, Any]],
    spell_ids: set[int],
    encounter_has_phases: bool,
) -> dict[str, Any] | None:
    """One top-parse comparison row, or ``None`` when the fight has no usable player.

    Lorrgs spec-ranking fights often carry no phase markers. Their single whole-fight window only
    stands for a phase when nothing shows the encounter has several, so on a multi-phase encounter a
    marker-less top parse gets no window instead of the whole fight's casts.
    """
    players = as_list(fight.get("players"))
    player = next((row for row in players if isinstance(row, dict)), None)
    if player is None:
        return None
    windows = build_phase_windows(as_list(fight.get("phases")), fight.get("duration"))
    no_markers = len(windows) == 1 and encounter_has_phases
    window = None if no_markers else selected_phase_window(windows, phase)
    unavailable_reason = None
    if window is None:
        unavailable_reason = "top_parse_has_no_phase_markers" if no_markers else "phase_not_in_top_parse"
    raw_boss = fight.get("boss")
    boss: dict[str, Any] = as_dict(raw_boss)
    casts: list[dict[str, Any]] = []
    boss_casts: list[dict[str, Any]] = []
    if window is not None:
        casts = normalize_lorrgs_casts(
            as_list(player.get("casts")), catalog=spell_catalog, window=window, spell_ids=spell_ids
        )
        boss_casts = normalize_lorrgs_casts(as_list(boss.get("casts")), catalog=boss_catalog, window=window)
    return {
        "report_id": report.get("report_id"),
        "region": report.get("region"),
        "fight_id": fight.get("fight_id"),
        "duration_ms": fight.get("duration"),
        "phase_window": window,
        "phase_available": window is not None,
        "phase_unavailable_reason": unavailable_reason,
        "player": {
            "name": player.get("name"),
            "source_id": player.get("source_id"),
            "spec_slug": player.get("spec_slug"),
            "total": player.get("total"),
        },
        "selected_phase_casts": casts,
        "selected_phase_boss_casts": boss_casts,
    }


def _any_fight_has_phase_markers(reports: list[Any]) -> bool:
    return any(
        len(build_phase_windows(as_list(fight.get("phases")), fight.get("duration"))) > 1
        for report in reports
        if isinstance(report, dict)
        for fight in as_list(report.get("fights"))
        if isinstance(fight, dict)
    )


def _record_sample_spells(casts: list[dict[str, Any]], frequency: Counter[int], total_casts: Counter[int]) -> None:
    """Count each tracked spell once per sample in ``frequency`` and once per cast in ``total_casts``."""
    seen_in_sample: set[int] = set()
    for cast in casts:
        spell = cast.get("spell")
        if isinstance(spell, dict) and isinstance(spell.get("spell_id"), int):
            spell_id = int(spell["spell_id"])
            seen_in_sample.add(spell_id)
            total_casts[spell_id] += 1
    for spell_id in seen_in_sample:
        frequency[spell_id] += 1


def top_parse_samples(
    ranking_data: dict[str, Any] | None,
    *,
    phase: int,
    sample_limit: int,
    spell_catalog: dict[int, dict[str, Any]],
    boss_catalog: dict[int, dict[str, Any]],
    spell_ids: set[int],
    player_phase_count: int,
    analyzed_fight: tuple[str, int],
) -> dict[str, Any]:
    """Top-parse samples for the selected phase.

    ``status`` is ``no_phase_data`` when samples were read but none has a window for the phase, and
    ``sample_fraction`` counts only the samples that have one (``phase_sample_count``). The encounter
    has several phases when the player's fight or any top-parse fight shows more than one window.
    ``analyzed_fight`` (report code, fight id) is skipped when it is a top parse, so the player is
    never compared with themselves; ``excluded_analyzed_fight`` says when that happened.
    """
    if ranking_data is None:
        return {
            "status": "unavailable",
            "sample_count": 0,
            "phase_sample_count": 0,
            "excluded_analyzed_fight": False,
            "samples": [],
            "selected_phase_spell_frequency": [],
        }
    data: dict[str, Any] = as_dict(ranking_data)
    reports = as_list(data.get("reports"))
    encounter_has_phases = player_phase_count > 1 or _any_fight_has_phase_markers(reports)
    samples: list[dict[str, Any]] = []
    frequency: Counter[int] = Counter()
    total_casts: Counter[int] = Counter()
    excluded_analyzed_fight = False
    for report in reports:
        if len(samples) >= sample_limit:
            break
        if not isinstance(report, dict):
            continue
        for fight in as_list(report.get("fights")):
            if len(samples) >= sample_limit:
                break
            if not isinstance(fight, dict):
                continue
            if (report.get("report_id"), fight.get("fight_id")) == analyzed_fight:
                excluded_analyzed_fight = True
                continue
            sample = _sample_for_fight(
                report,
                fight,
                phase=phase,
                spell_catalog=spell_catalog,
                boss_catalog=boss_catalog,
                spell_ids=spell_ids,
                encounter_has_phases=encounter_has_phases,
            )
            if sample is None:
                continue
            _record_sample_spells(sample["selected_phase_casts"], frequency, total_casts)
            samples.append(sample)
    phase_sample_count = sum(1 for sample in samples if sample["phase_available"])
    return {
        "status": "no_phase_data" if samples and not phase_sample_count else "ready",
        "sample_count": len(samples),
        "phase_sample_count": phase_sample_count,
        "excluded_analyzed_fight": excluded_analyzed_fight,
        "available_report_count": len(reports),
        "samples": samples,
        "selected_phase_spell_frequency": _frequency_rows(
            frequency,
            total_casts=total_casts,
            catalog=spell_catalog,
            denominator=max(1, phase_sample_count),
        ),
    }


def source_command(command: str, args: list[str]) -> str:
    """The provider call as a command line a shell runs verbatim."""
    return shlex.join(["warcraft", command, *args])


def _count_rows(counts: Counter[int], *, catalog: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"spell": spell_summary(spell_id, catalog=catalog), "count": count}
        for spell_id, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]


def _frequency_rows(
    counts: Counter[int],
    *,
    total_casts: Counter[int],
    catalog: dict[int, dict[str, Any]],
    denominator: int,
) -> list[dict[str, Any]]:
    return [
        {
            "spell": spell_summary(spell_id, catalog=catalog),
            "sample_count": count,
            "sample_fraction": count / denominator,
            "total_casts": total_casts.get(spell_id, 0),
        }
        for spell_id, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]


def _phase_marker_ms(row: Any) -> int | None:
    if not isinstance(row, dict):
        return None
    return int_or_none(row.get("ts")) or int_or_none(row.get("timestamp"))


def timestamp_in_window(timestamp_ms: int, window: dict[str, Any]) -> bool:
    """Whether ``timestamp_ms`` falls in ``[start_ms, end_ms)``; a null ``end_ms`` is open-ended."""
    start_ms = int_or_none(window.get("start_ms"))
    end_ms = int_or_none(window.get("end_ms"))
    if start_ms is None:
        return False
    return start_ms <= timestamp_ms and (end_ms is None or timestamp_ms < end_ms)


def int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    return None

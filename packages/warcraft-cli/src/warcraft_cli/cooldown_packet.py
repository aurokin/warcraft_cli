from __future__ import annotations

from collections import Counter
from typing import Any

from warcraft_core.shapes import as_dict, as_list


def build_phase_windows(phases: list[Any], duration_ms: int | float | None) -> list[dict[str, Any]]:
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
    boundaries = [0, *markers]
    if duration is not None:
        boundaries.append(duration)
    windows: list[dict[str, Any]] = []
    for index, start_ms in enumerate(boundaries[:-1], start=1):
        end_ms = boundaries[index]
        windows.append(
            {
                "phase": index,
                "label": f"P{index}",
                "start_ms": start_ms,
                "end_ms": end_ms,
                "duration_ms": end_ms - start_ms,
                "start_source": "pull" if index == 1 else "lorrgs_phase_transition",
                "end_source": "fight_end" if duration is not None and end_ms == duration else "lorrgs_phase_transition",
            }
        )
    if not windows and duration is not None:
        windows.append(
            {
                "phase": 1,
                "label": "P1",
                "start_ms": 0,
                "end_ms": duration,
                "duration_ms": duration,
                "start_source": "pull",
                "end_source": "fight_end",
            }
        )
    return windows


def selected_phase_window(windows: list[dict[str, Any]], phase: int) -> dict[str, Any] | None:
    for window in windows:
        if window.get("phase") == phase:
            return window
    return None


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
        spell_id = _int_or_none(value.get("spell_id")) or _int_or_none(key)
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
        spell_id = _int_or_none(cast.get("id"))
        timestamp_ms = _int_or_none(cast.get("ts"))
        if spell_id is None or timestamp_ms is None:
            continue
        if spell_ids is not None and spell_id not in spell_ids:
            continue
        if window is not None and not _timestamp_in_window(timestamp_ms, window):
            continue
        normalized.append(
            {
                "timestamp_ms": timestamp_ms,
                "spell": spell_summary(spell_id, catalog=catalog),
                "cast_number": _int_or_none(cast.get("c")),
                "duration_ms": _int_or_none(cast.get("d")),
            }
        )
    return sorted(normalized, key=lambda row: int(row["timestamp_ms"]))


def normalize_warcraftlogs_actor_casts(
    events_payload: dict[str, Any],
    *,
    fight_start_time_ms: int,
    catalog: dict[int, dict[str, Any]],
    spell_ids: set[int],
    window: dict[str, Any] | None = None,
) -> dict[str, Any]:
    raw_events = events_payload.get("events")
    events: list[Any] = as_list(raw_events)
    tracked: list[dict[str, Any]] = []
    phase_tracked: list[dict[str, Any]] = []
    by_spell: Counter[int] = Counter()
    phase_by_spell: Counter[int] = Counter()
    for row in events:
        if not isinstance(row, dict):
            continue
        spell_id = _int_or_none(row.get("abilityGameID"))
        timestamp = _int_or_none(row.get("timestamp"))
        if spell_id is None or timestamp is None or spell_id not in spell_ids:
            continue
        relative_ms = timestamp - fight_start_time_ms
        normalized = {
            "timestamp_ms": relative_ms,
            "report_timestamp_ms": timestamp,
            "spell": spell_summary(spell_id, catalog=catalog),
            "type": row.get("type"),
            "target_id": _int_or_none(row.get("targetID")),
        }
        tracked.append(normalized)
        by_spell[spell_id] += 1
        if window is not None and _timestamp_in_window(relative_ms, window):
            phase_tracked.append(normalized)
            phase_by_spell[spell_id] += 1
    return {
        "raw_event_count": len(events),
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
) -> dict[str, Any] | None:
    """One top-parse comparison row, or ``None`` when the fight has no usable player."""
    players = _list_or_empty(fight.get("players"))
    player = next((row for row in players if isinstance(row, dict)), None)
    if player is None:
        return None
    windows = build_phase_windows(_list_or_empty(fight.get("phases")), fight.get("duration"))
    window = selected_phase_window(windows, phase)
    raw_boss = fight.get("boss")
    boss: dict[str, Any] = as_dict(raw_boss)
    casts: list[dict[str, Any]] = []
    boss_casts: list[dict[str, Any]] = []
    if window is not None:
        casts = normalize_lorrgs_casts(
            _list_or_empty(player.get("casts")), catalog=spell_catalog, window=window, spell_ids=spell_ids
        )
        boss_casts = normalize_lorrgs_casts(_list_or_empty(boss.get("casts")), catalog=boss_catalog, window=window)
    return {
        "report_id": report.get("report_id"),
        "region": report.get("region"),
        "fight_id": fight.get("fight_id"),
        "duration_ms": fight.get("duration"),
        "phase_window": window,
        "phase_available": window is not None,
        "player": {
            "name": player.get("name"),
            "source_id": player.get("source_id"),
            "spec_slug": player.get("spec_slug"),
            "total": player.get("total"),
        },
        "selected_phase_casts": casts,
        "selected_phase_boss_casts": boss_casts,
    }


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
) -> dict[str, Any]:
    if ranking_data is None:
        return {"status": "unavailable", "sample_count": 0, "samples": [], "selected_phase_spell_frequency": []}
    data: dict[str, Any] = as_dict(ranking_data)
    reports = _list_or_empty(data.get("reports"))
    samples: list[dict[str, Any]] = []
    frequency: Counter[int] = Counter()
    total_casts: Counter[int] = Counter()
    for report in reports:
        if len(samples) >= sample_limit:
            break
        if not isinstance(report, dict):
            continue
        for fight in _list_or_empty(report.get("fights")):
            if len(samples) >= sample_limit:
                break
            if not isinstance(fight, dict):
                continue
            sample = _sample_for_fight(
                report,
                fight,
                phase=phase,
                spell_catalog=spell_catalog,
                boss_catalog=boss_catalog,
                spell_ids=spell_ids,
            )
            if sample is None:
                continue
            _record_sample_spells(sample["selected_phase_casts"], frequency, total_casts)
            samples.append(sample)
    return {
        "status": "ready",
        "sample_count": len(samples),
        "available_report_count": len(reports),
        "samples": samples,
        "selected_phase_spell_frequency": _frequency_rows(
            frequency,
            total_casts=total_casts,
            catalog=spell_catalog,
            denominator=max(1, len(samples)),
        ),
    }


def source_command(command: str, args: list[str]) -> str:
    return " ".join(["warcraft", command, *[_quote_arg(arg) for arg in args]])


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
    return _int_or_none(row.get("ts")) or _int_or_none(row.get("timestamp"))


def _timestamp_in_window(timestamp_ms: int, window: dict[str, Any]) -> bool:
    start_ms = _int_or_none(window.get("start_ms"))
    end_ms = _int_or_none(window.get("end_ms"))
    if start_ms is None or end_ms is None:
        return False
    return start_ms <= timestamp_ms < end_ms


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    return None


def _list_or_empty(value: Any) -> list[Any]:
    return as_list(value)


def _quote_arg(value: str) -> str:
    if value and all(character.isalnum() or character in "-_=./" for character in value):
        return value
    return "'" + value.replace("'", "'\"'\"'") + "'"

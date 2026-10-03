from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from warcraft_core.simc_json2 import (
    dps_error,
    game_version,
    metric_count,
    metric_mean,
    profileset_metric,
    profileset_result_rows,
    stop_reason,
)


@dataclass(frozen=True, slots=True)
class SimReportSummary:
    version: str | None
    game_version: str | None
    player: dict[str, Any]
    iterations_completed: int | None
    run_settings: dict[str, Any]
    runtime: dict[str, Any]
    metrics: dict[str, Any]
    # Every actor after players[0], and the ranked profileset rows when the profile defined any.
    other_actors: list[dict[str, Any]]
    profilesets: dict[str, Any] | None


def load_sim_report(path: str | Path) -> dict[str, Any]:
    """Read a SimC json2 report from disk."""
    resolved = Path(path).expanduser().resolve()
    report = json.loads(resolved.read_text())
    if not isinstance(report, dict):
        raise RuntimeError("SimC JSON report was not a JSON object.")
    return report


def summarize_sim_report(report: dict[str, Any]) -> SimReportSummary:
    """Reduce a raw SimC JSON report to the fields the CLI reports."""
    sim = report.get("sim") if isinstance(report, dict) else None
    if not isinstance(sim, dict):
        raise RuntimeError("SimC JSON report did not contain sim metadata.")
    players = sim.get("players") if isinstance(sim.get("players"), list) else []
    if not players or not isinstance(players[0], dict):
        raise RuntimeError("SimC JSON report did not contain players.")
    player = players[0]
    options = _dict_field(sim, "options")
    stats = _dict_field(sim, "statistics")
    collected = _dict_field(player, "collected_data")
    iterations_completed = metric_count(collected.get("fight_length")) or metric_count(stats.get("simulation_length"))
    metrics = _metrics_block(collected, options)
    return SimReportSummary(
        version=_text(report.get("version")),
        game_version=game_version(options),
        player=_player_block(player),
        iterations_completed=iterations_completed,
        run_settings=_run_settings_block(options, iterations_completed=iterations_completed, metrics=metrics),
        runtime=_runtime_block(stats),
        metrics=metrics,
        other_actors=[
            {"player": _player_block(other), "metrics": _metrics_block(_dict_field(other, "collected_data"), options)}
            for other in players[1:]
            if isinstance(other, dict)
        ],
        profilesets=_profilesets_block(sim.get("profilesets")),
    )


def _player_block(player: dict[str, Any]) -> dict[str, Any]:
    return {"name": _text(player.get("name")), "spec": _text(player.get("specialization")), "role": _text(player.get("role"))}


def _profilesets_block(profilesets: Any) -> dict[str, Any] | None:
    if not isinstance(profilesets, (dict, list)):
        return None
    rows = profileset_result_rows(profilesets)
    return {"metric": profileset_metric(profilesets), "result_count": len(rows), "results": rows}


def _dict_field(source: dict[str, Any], key: str) -> dict[str, Any]:
    value = source.get(key)
    return value if isinstance(value, dict) else {}


def _text(value: Any) -> str | None:
    return str(value) if value is not None else None


def _metrics_block(collected: dict[str, Any], options: dict[str, Any]) -> dict[str, Any]:
    keys = ("dps", "dtps", "hps", "deaths", "fight_length", "absorb", "heal")
    metrics: dict[str, Any] = {key: metric_mean(collected.get(key)) for key in keys}
    metrics["dps_error"] = dps_error(collected, options)
    return metrics


def _target_error_percent(*, metrics: dict[str, Any], iterations_completed: int | None) -> float | None:
    """Observed DPS error as a percentage; only meaningful once more than one iteration ran."""
    dps = metrics.get("dps")
    dps_error = metrics.get("dps_error")
    if not isinstance(dps, float) or not dps or not isinstance(dps_error, float):
        return None
    if not isinstance(iterations_completed, int) or iterations_completed <= 1:
        return None
    return round(dps_error / dps * 100.0, 3)


def _run_settings_block(options: dict[str, Any], *, iterations_completed: int | None, metrics: dict[str, Any]) -> dict[str, Any]:
    return {
        "iterations_completed": iterations_completed,
        "target_error_requested": options.get("target_error"),
        "target_error_percent": _target_error_percent(metrics=metrics, iterations_completed=iterations_completed),
        "threads": options.get("threads"),
        "fight_style": options.get("fight_style"),
        "desired_targets": options.get("desired_targets"),
        "max_time": options.get("max_time"),
        "vary_combat_length": options.get("vary_combat_length"),
        "seed": options.get("seed"),
        "stop_reason": stop_reason(options),
    }


def _runtime_block(stats: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "elapsed_time_seconds",
        "elapsed_cpu_seconds",
        "init_time_seconds",
        "merge_time_seconds",
        "analyze_time_seconds",
    )
    return {key: stats.get(key) for key in keys}


def sim_report_payload(
    summary: SimReportSummary,
    *,
    profile_path: str | None,
    preset: str,
    input_source: str,
    json_report_path: str | None,
    command: list[str],
    iterations_requested: int | None,
    disclosures: list[str],
) -> dict[str, Any]:
    return {
        "status": "completed",
        "disclosures": disclosures,
        "preset": preset,
        "input_source": input_source,
        "profile_path": profile_path,
        "json_report_path": json_report_path,
        "command": command,
        "simc_version": summary.version,
        "game_version": summary.game_version,
        "player": summary.player,
        # Not json2's `options.iterations`: SimC rewrites it to the work done (iterations + threads - 1).
        "run_settings": {"iterations_requested": iterations_requested, **summary.run_settings},
        "runtime": summary.runtime,
        "metrics": summary.metrics,
        "actor_count": 1 + len(summary.other_actors),
        "other_actors": summary.other_actors,
        "profilesets": summary.profilesets,
    }

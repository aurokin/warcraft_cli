from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class SimReportSummary:
    version: str | None
    game_version: str | None
    player_name: str | None
    player_spec: str | None
    player_role: str | None
    iterations_completed: int | None
    run_settings: dict[str, Any]
    runtime: dict[str, Any]
    metrics: dict[str, Any]


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
    iterations_completed = _metric_count(collected.get("fight_length")) or _metric_count(stats.get("simulation_length"))
    metrics = _metrics_block(collected)
    return SimReportSummary(
        version=_text(report.get("version")),
        game_version=_game_version(options),
        player_name=_text(player.get("name")),
        player_spec=_text(player.get("specialization")),
        player_role=_text(player.get("role")),
        iterations_completed=iterations_completed,
        run_settings=_run_settings_block(options, iterations_completed=iterations_completed, metrics=metrics),
        runtime=_runtime_block(stats),
        metrics=metrics,
    )


def _dict_field(source: dict[str, Any], key: str) -> dict[str, Any]:
    value = source.get(key)
    return value if isinstance(value, dict) else {}


def _text(value: Any) -> str | None:
    return str(value) if value is not None else None


def _game_version(options: dict[str, Any]) -> str | None:
    """Read the live client version out of the report's dbc block."""
    dbc = _dict_field(options, "dbc")
    version_used = dbc.get("version_used")
    live_info = _dict_field(dbc, version_used) if isinstance(version_used, str) else {}
    wow_version = live_info.get("wow_version")
    return wow_version if isinstance(wow_version, str) else None


def _metrics_block(collected: dict[str, Any]) -> dict[str, Any]:
    keys = ("dps", "dtps", "hps", "deaths", "fight_length", "absorb", "heal")
    metrics: dict[str, Any] = {key: _metric_mean(collected.get(key)) for key in keys}
    metrics["dps_error"] = _metric_mean(collected.get("dpse"))
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
        "iterations_requested": options.get("iterations"),
        "iterations_completed": iterations_completed,
        "target_error_requested": options.get("target_error"),
        "target_error_percent": _target_error_percent(metrics=metrics, iterations_completed=iterations_completed),
        "threads": options.get("threads"),
        "fight_style": options.get("fight_style"),
        "desired_targets": options.get("desired_targets"),
        "max_time": options.get("max_time"),
        "vary_combat_length": options.get("vary_combat_length"),
        "seed": options.get("seed"),
        "stop_reason": _stop_reason(options=options, iterations_completed=iterations_completed),
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
) -> dict[str, Any]:
    return {
        "provider": "simc",
        "status": "completed",
        "preset": preset,
        "input_source": input_source,
        "profile_path": profile_path,
        "json_report_path": json_report_path,
        "command": command,
        "simc_version": summary.version,
        "game_version": summary.game_version,
        "player": {
            "name": summary.player_name,
            "spec": summary.player_spec,
            "role": summary.player_role,
        },
        "run_settings": summary.run_settings,
        "runtime": summary.runtime,
        "metrics": summary.metrics,
    }


def _metric_mean(metric: Any) -> float | None:
    if isinstance(metric, dict):
        value = metric.get("mean")
        if isinstance(value, (int, float)):
            return float(value)
    if isinstance(metric, (int, float)):
        return float(metric)
    return None


def _metric_count(metric: Any) -> int | None:
    if isinstance(metric, dict):
        value = metric.get("count")
        if isinstance(value, int):
            return value
    return None


def _stop_reason(*, options: dict[str, Any], iterations_completed: int | None) -> str:
    target_error = options.get("target_error")
    iterations_requested = options.get("iterations")
    if isinstance(target_error, (int, float)) and float(target_error) > 0:
        if isinstance(iterations_requested, int) and isinstance(iterations_completed, int) and iterations_completed < iterations_requested:
            return "target_error_reached"
        return "target_error_requested"
    return "fixed_iterations_completed"

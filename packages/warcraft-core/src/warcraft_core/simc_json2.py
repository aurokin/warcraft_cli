"""Readers for SimulationCraft ``json2`` reports, shared by ``simc`` and ``raidbots`` (whose ``data.json`` is json2).

Every json2 field is optional, so each reader returns ``None`` (or a neutral value) instead of raising.
"""

from __future__ import annotations

from typing import Any


def metric_mean(metric: Any) -> float | None:
    """The ``mean`` of a json2 sample-data block, or the value itself when the report wrote a bare number."""
    if isinstance(metric, dict):
        value = metric.get("mean")
        if isinstance(value, (int, float)):
            return float(value)
    if isinstance(metric, (int, float)):
        return float(metric)
    return None


def metric_count(metric: Any) -> int | None:
    if isinstance(metric, dict):
        value = metric.get("count")
        if isinstance(value, int):
            return value
    return None


def dps_error(collected: dict[str, Any], options: dict[str, Any]) -> float | None:
    """SimC's "DPS Error": the half-width of the confidence interval around mean DPS.

    SimC computes it as ``dps.mean_std_dev * confidence_estimator`` (report_helper.cpp). The json2
    ``collected_data.dpse`` is effective DPS, about equal to DPS, not an error. ``mean_std_dev`` is
    only written for non-simple sample data, so the error is null when the report does not carry it.
    """
    dps = collected.get("dps")
    std_dev = dps.get("mean_std_dev") if isinstance(dps, dict) else None
    estimator = options.get("confidence_estimator")
    if isinstance(std_dev, (int, float)) and isinstance(estimator, (int, float)):
        return float(std_dev) * float(estimator)
    return None


def stop_reason(*, options: dict[str, Any], iterations_completed: int | None) -> str:
    """Why the sim stopped: a requested target error reached early, a target error requested, or fixed iterations."""
    target_error = options.get("target_error")
    iterations_requested = options.get("iterations")
    if isinstance(target_error, (int, float)) and float(target_error) > 0:
        if isinstance(iterations_requested, int) and isinstance(iterations_completed, int) and iterations_completed < iterations_requested:
            return "target_error_reached"
        return "target_error_requested"
    return "fixed_iterations_completed"


def game_version(options: dict[str, Any]) -> str | None:
    """The live client version out of the report's ``options.dbc`` block."""
    dbc = options.get("dbc")
    version_used = dbc.get("version_used") if isinstance(dbc, dict) else None
    live_info = dbc.get(version_used) if isinstance(dbc, dict) and isinstance(version_used, str) else None
    wow_version = live_info.get("wow_version") if isinstance(live_info, dict) else None
    return wow_version if isinstance(wow_version, str) else None

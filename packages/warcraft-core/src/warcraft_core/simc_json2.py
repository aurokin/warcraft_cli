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


def stop_reason(options: dict[str, Any]) -> str:
    """Whether the sim ran under a target error or for a fixed iteration count.

    json2 cannot say whether a target error ended the run early: SimC rewrites ``options.iterations``
    to the count it actually ran, so the requested cap is not in the report.
    """
    target_error = options.get("target_error")
    if isinstance(target_error, (int, float)) and float(target_error) > 0:
        return "target_error_requested"
    return "fixed_iterations_completed"


def game_version(options: dict[str, Any]) -> str | None:
    """The live client version out of the report's ``options.dbc`` block."""
    dbc = options.get("dbc")
    version_used = dbc.get("version_used") if isinstance(dbc, dict) else None
    live_info = dbc.get(version_used) if isinstance(dbc, dict) and isinstance(version_used, str) else None
    wow_version = live_info.get("wow_version") if isinstance(live_info, dict) else None
    return wow_version if isinstance(wow_version, str) else None


def profileset_result_rows(profilesets: Any) -> list[dict[str, Any]]:
    """The ``sim.profilesets`` result rows, best mean first."""
    if isinstance(profilesets, dict):
        if isinstance(profilesets.get("results"), list):
            rows = profilesets["results"]
        else:
            # Fallback for a name->row mapping with no explicit `results` list. `metric` and
            # other non-row scalar entries are filtered out by the isinstance(row, dict) guard.
            rows = [
                {"name": name, **row} if "name" not in row else row
                for name, row in profilesets.items()
                if isinstance(row, dict)
            ]
    elif isinstance(profilesets, list):
        rows = profilesets
    else:
        rows = None
    if not isinstance(rows, list):
        return []
    parsed: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        mean = row.get("mean")
        parsed.append(
            {
                "name": str(row.get("name")) if row.get("name") is not None else None,
                "mean": float(mean) if isinstance(mean, (int, float)) else None,
                "min": float(row["min"]) if isinstance(row.get("min"), (int, float)) else None,
                "max": float(row["max"]) if isinstance(row.get("max"), (int, float)) else None,
                "median": float(row["median"]) if isinstance(row.get("median"), (int, float)) else None,
                "stddev": float(row["stddev"]) if isinstance(row.get("stddev"), (int, float)) else None,
            }
        )
    parsed.sort(key=lambda item: (item.get("mean") is None, -(item.get("mean") or 0.0), item.get("name") or ""))
    return parsed


def profileset_metric(profilesets: Any) -> str | None:
    """The metric ``sim.profilesets`` ranked its rows by."""
    if not isinstance(profilesets, dict):
        return None
    metric = profilesets.get("metric")
    if isinstance(metric, list) and metric:
        return str(metric[0])
    if isinstance(metric, str) and metric.strip():
        return metric.strip()
    return None

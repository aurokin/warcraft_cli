from __future__ import annotations

from typing import Any

# Raidbots `data.json` is standard SimC `json2` output (top-level `version`/`sim`)
# with Raidbots metadata added under `simbot`. We cannot import simc_cli (provider
# CLIs must stay independent), so the small defensive json2 helpers below mirror
# simc_cli.report rather than reuse it.


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
        if (
            isinstance(iterations_requested, int)
            and isinstance(iterations_completed, int)
            and iterations_completed < iterations_requested
        ):
            return "target_error_reached"
        return "target_error_requested"
    return "fixed_iterations_completed"


def _game_version(options: dict[str, Any]) -> str | None:
    dbc = options.get("dbc") if isinstance(options.get("dbc"), dict) else {}
    version_used = dbc.get("version_used")
    live_info = dbc.get(version_used) if isinstance(version_used, str) and isinstance(dbc.get(version_used), dict) else {}
    return live_info.get("wow_version") if isinstance(live_info, dict) else None


def _actor_summary(player: dict[str, Any]) -> dict[str, Any]:
    talents = player.get("talents")
    return {
        "name": str(player.get("name")) if player.get("name") is not None else None,
        "spec": str(player.get("specialization")) if player.get("specialization") is not None else None,
        "role": str(player.get("role")) if player.get("role") is not None else None,
        "class": str(player.get("class")) if player.get("class") is not None else None,
        "level": player.get("level"),
        "race": str(player.get("race")) if player.get("race") is not None else None,
        "talents_present": bool(talents),
    }


def _quick_sim_metrics(player: dict[str, Any]) -> dict[str, Any]:
    collected = player.get("collected_data") if isinstance(player.get("collected_data"), dict) else {}
    return {
        "dps": _metric_mean(collected.get("dps")),
        "dps_error": _metric_mean(collected.get("dpse")),
        "dtps": _metric_mean(collected.get("dtps")),
        "hps": _metric_mean(collected.get("hps")),
        "fight_length": _metric_mean(collected.get("fight_length")),
    }


def _run_settings(options: dict[str, Any], statistics: dict[str, Any], player: dict[str, Any] | None) -> dict[str, Any]:
    collected = player.get("collected_data") if isinstance(player, dict) and isinstance(player.get("collected_data"), dict) else {}
    iterations_completed = _metric_count(collected.get("fight_length")) or _metric_count(statistics.get("simulation_length"))
    return {
        "iterations_requested": options.get("iterations"),
        "iterations_completed": iterations_completed,
        "target_error": options.get("target_error"),
        "fight_style": options.get("fight_style"),
        "desired_targets": options.get("desired_targets"),
        "max_time": options.get("max_time"),
        "threads": options.get("threads"),
        "stop_reason": _stop_reason(options=options, iterations_completed=iterations_completed),
    }


def _profileset_result_rows(profilesets: Any) -> list[dict[str, Any]]:
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


def _profileset_metric(profilesets: Any) -> str | None:
    if not isinstance(profilesets, dict):
        return None
    metric = profilesets.get("metric")
    if isinstance(metric, list) and metric:
        return str(metric[0])
    if isinstance(metric, str) and metric.strip():
        return metric.strip()
    return None


def _has_profilesets(profilesets: Any) -> bool:
    # Presence-based: if `sim.profilesets` is present as a container at all, this was a
    # multi-profile run (Top Gear / Droptimizer). Quick sims never emit `sim.profilesets`, so
    # even an empty list/dict must route to multi_profile, not be misread as a quick sim off
    # players[0] (which in a multi-profile run is the baseline template, not a user actor).
    return isinstance(profilesets, (dict, list))


def _simbot_metadata(report: dict[str, Any]) -> dict[str, Any]:
    simbot = report.get("simbot") if isinstance(report.get("simbot"), dict) else {}
    return {
        "sim_type": simbot.get("simType") or simbot.get("type"),
        "title": simbot.get("title"),
    }


def parse_report(report: dict[str, Any], *, report_id: str) -> dict[str, Any]:
    """Parse a Raidbots `data.json` payload into a structured, kind-aware summary.

    Raises ValueError if the payload is not recognizable SimC json2.
    """
    sim = report.get("sim") if isinstance(report, dict) else None
    if not isinstance(sim, dict):
        raise ValueError("Raidbots report did not contain SimC `sim` metadata.")
    options = sim.get("options") if isinstance(sim.get("options"), dict) else {}
    statistics = sim.get("statistics") if isinstance(sim.get("statistics"), dict) else {}
    players = sim.get("players") if isinstance(sim.get("players"), list) else []
    baseline = players[0] if players and isinstance(players[0], dict) else None
    profilesets = sim.get("profilesets")

    common: dict[str, Any] = {
        "report_id": report_id,
        "simc_version": str(report.get("version")) if report.get("version") is not None else None,
        "game_version": _game_version(options),
        "simbot": _simbot_metadata(report),
        "run_settings": _run_settings(options, statistics, baseline),
    }

    if _has_profilesets(profilesets):
        # Multi-profile sim (Top Gear / Droptimizer). players[0] is the baseline
        # profile, NOT a chosen "user actor"; per-actor damage/buff data is stripped,
        # so the meaningful output is the ranked profileset result rows.
        rows = _profileset_result_rows(profilesets)
        common.update(
            {
                "kind": "multi_profile",
                "baseline_actor": _actor_summary(baseline) if baseline is not None else None,
                "profilesets": {
                    "metric": _profileset_metric(profilesets),
                    "result_count": len(rows),
                    "results": rows,
                },
            }
        )
        return common

    if baseline is not None:
        common.update(
            {
                "kind": "quick_sim",
                "actor": _actor_summary(baseline),
                "metrics": _quick_sim_metrics(baseline),
            }
        )
        return common

    common["kind"] = "unknown"
    return common

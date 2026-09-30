from __future__ import annotations

import shutil
import statistics
import tempfile
from dataclasses import dataclass
from pathlib import Path

from simc_cli.repo import RepoPaths
from simc_cli.run import _run


@dataclass(slots=True)
class FirstCastResult:
    seed: int
    time: float | None
    log_path: Path


@dataclass(slots=True)
class ActionHit:
    action: str
    scheduled_at: float | None
    performed_at: float | None


def run_first_casts(
    paths: RepoPaths,
    profile: str | Path,
    action: str,
    seeds: int,
    max_time: int,
    desired_targets: int,
    fight_style: str,
) -> list[FirstCastResult]:
    simc = paths.build_simc
    profile_path = Path(profile).expanduser().resolve()
    if not simc.exists():
        raise FileNotFoundError(f"SimC binary not found: {simc}")
    if not profile_path.exists():
        raise FileNotFoundError(f"Profile not found: {profile_path}")

    # The per-seed logs stay behind for the caller (each result carries its log_path); a failed run
    # leaves nothing worth keeping, so its directory is removed.
    temp_dir = Path(tempfile.mkdtemp(prefix="simc-cli-"))
    results: list[FirstCastResult] = []
    for seed in range(1, seeds + 1):
        result = _run(
            [
                str(simc),
                str(profile_path),
                "iterations=1",
                f"max_time={max_time}",
                "vary_combat_length=0",
                f"desired_targets={desired_targets}",
                f"fight_style={fight_style}",
                "log=1",
                f"seed={seed}",
                "allow_experimental_specializations=1",
            ],
            cwd=paths.root,
        )
        if result.returncode != 0:
            shutil.rmtree(temp_dir, ignore_errors=True)
            message = result.stderr.strip() or result.stdout.strip() or "SimulationCraft first-cast run failed."
            raise RuntimeError(message)
        log_path = temp_dir / f"seed_{seed}.log"
        log_path.write_text(result.stdout)
        results.append(FirstCastResult(seed=seed, time=first_action_time(result.stdout, action), log_path=log_path))
    return results


def first_action_time(log_text: str, action: str) -> float | None:
    needle = f"Action '{action}'"
    for line in log_text.splitlines():
        if needle not in line:
            continue
        if "performs Action" not in line:
            continue
        timestamp, _, _ = line.partition(" ")
        try:
            return float(timestamp)
        except ValueError:
            return None
    return None


def summarize_first_casts(results: list[FirstCastResult]) -> dict[str, float | int]:
    values = [result.time for result in results if result.time is not None]
    if not values:
        return {"samples": len(results), "found": 0}
    return {
        "samples": len(results),
        "found": len(values),
        "min": min(values),
        "avg": statistics.mean(values),
        "max": max(values),
    }


def first_action_hits(log_path: str | Path, actions: list[str]) -> list[ActionHit]:
    lines = Path(log_path).read_text().splitlines()
    hits: list[ActionHit] = []
    for action in actions:
        needle = f"Action '{action}'"
        scheduled_at = None
        performed_at = None
        for line in lines:
            if needle not in line:
                continue
            timestamp = _parse_timestamp(line)
            if "schedules execute for Action" in line and scheduled_at is None:
                scheduled_at = timestamp
            if "performs Action" in line and performed_at is None:
                performed_at = timestamp
            if scheduled_at is not None and performed_at is not None:
                break
        hits.append(ActionHit(action=action, scheduled_at=scheduled_at, performed_at=performed_at))
    return hits


def _parse_timestamp(line: str) -> float | None:
    timestamp, _, _ = line.partition(" ")
    try:
        return float(timestamp)
    except ValueError:
        return None

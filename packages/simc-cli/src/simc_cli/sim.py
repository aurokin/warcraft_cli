from __future__ import annotations

import re
import shutil
import statistics
import tempfile
from dataclasses import dataclass
from pathlib import Path

from simc_cli.build_input import ACTOR_LINE_RE, DEFAULT_RACE_BY_CLASS
from simc_cli.repo import RepoPaths
from simc_cli.run import _run

# `<time> Player '<actor>' performs Action '<action>' (<id>) ...` (`Enemy '<actor>'` for an enemy). Pets are
# players too, named `<owner>_<pet>`.
_ACTION_LOG_RE = re.compile(
    r"^\S+ (?:Player|Enemy) '(?P<actor>[^']*)' (?P<verb>performs|schedules execute for) Action '(?P<action>[^']*)'"
)


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
    # The actor whose cast ``performed_at`` times: the player or one of its pets.
    actor: str | None = None


def primary_actor_name(profile_text: str) -> str | None:
    """The name of the profile's first actor, the player ``simc sim`` reports as players[0]."""
    for raw_line in profile_text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        match = ACTOR_LINE_RE.match(line)
        if match and match.group(1) in DEFAULT_RACE_BY_CLASS:
            return match.group(2)
    return None


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
    # A pet casting an action of the same name is not the player's first cast.
    actor = primary_actor_name(profile_path.read_text())

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
        results.append(FirstCastResult(seed=seed, time=first_action_time(result.stdout, action, actor), log_path=log_path))
    return results


def first_action_time(log_text: str, action: str, actor: str | None) -> float | None:
    """When ``actor`` first performed ``action``; any actor's cast counts only when ``actor`` is None."""
    return _first_hits(log_text.splitlines(), action, actor).performed_at


def _first_hits(lines: list[str], action: str, actor: str | None) -> ActionHit:
    hit = ActionHit(action=action, scheduled_at=None, performed_at=None)
    for line in lines:
        match = _ACTION_LOG_RE.match(line)
        if match is None or match.group("action") != action or actor not in (None, match.group("actor")):
            continue
        timestamp = _parse_timestamp(line)
        if match.group("verb") == "performs":
            if hit.performed_at is None:
                hit.performed_at, hit.actor = timestamp, match.group("actor")
        elif hit.scheduled_at is None:
            hit.scheduled_at = timestamp
        if hit.scheduled_at is not None and hit.performed_at is not None:
            break
    return hit


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


def first_action_hits(log_path: str | Path, actions: list[str], actor: str | None = None) -> list[ActionHit]:
    lines = Path(log_path).read_text().splitlines()
    return [_first_hits(lines, action, actor) for action in actions]


def _parse_timestamp(line: str) -> float | None:
    timestamp, _, _ = line.partition(" ")
    try:
        return float(timestamp)
    except ValueError:
        return None

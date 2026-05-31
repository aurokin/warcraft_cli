from __future__ import annotations

import re
import shlex
from typing import Any

# Local-only classification of SimulationCraft addon / profile text. raidbots must
# not import simc_cli (provider independence), so the suggested local commands below
# are emitted as plain strings for the agent / wrapper to run.

_WOW_CLASSES = frozenset(
    {
        "deathknight",
        "demonhunter",
        "druid",
        "evoker",
        "hunter",
        "mage",
        "monk",
        "paladin",
        "priest",
        "rogue",
        "shaman",
        "warlock",
        "warrior",
    }
)

_ACTOR_RE = re.compile(r"^([a-z_]+)\s*=\s*\"?([^\"\n]+)\"?\s*$")
_PROFILESET_RE = re.compile(r'^profileset\.(?:"([^"]+)"|([^+=\s]+))')
_OPTION_KEYS = (
    "iterations",
    "target_error",
    "fight_style",
    "desired_targets",
    "max_time",
    "calculate_scale_factors",
)


def _iter_clean_lines(text: str) -> list[str]:
    lines: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        lines.append(line)
    return lines


def _scalar_assignments(lines: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in lines:
        if "=" not in line or line.startswith("profileset."):
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key and key not in values:
            values[key] = value.strip()
    return values


def _find_actor(lines: list[str]) -> tuple[str | None, str | None]:
    for line in lines:
        match = _ACTOR_RE.match(line)
        if match and match.group(1) in _WOW_CLASSES:
            return match.group(1), match.group(2).strip()
    return None, None


def _profileset_names(lines: list[str]) -> set[str]:
    names: set[str] = set()
    for line in lines:
        match = _PROFILESET_RE.match(line)
        if match:
            names.add(match.group(1) or match.group(2))
    return names


def classify_simc_input(text: str) -> dict[str, Any]:
    lines = _iter_clean_lines(text)
    assignments = _scalar_assignments(lines)

    actor_class, actor_name = _find_actor(lines)
    profileset_names = _profileset_names(lines)
    copy_count = sum(1 for line in lines if line.startswith("copy="))
    talents_lines = [line for line in lines if line.startswith("talents=")]
    options = {key: assignments[key] for key in _OPTION_KEYS if key in assignments}

    if profileset_names or copy_count:
        sim_type_guess = "top_gear_or_droptimizer"
    elif actor_class is not None:
        sim_type_guess = "quick_sim"
    else:
        sim_type_guess = "advanced"

    return {
        "sim_type_guess": sim_type_guess,
        "actor_class": actor_class,
        "actor_name": actor_name,
        "spec": assignments.get("spec"),
        "profileset_count": len(profileset_names),
        "copy_count": copy_count,
        "talents_present": bool(talents_lines),
        "options": options,
    }


def _talents_value(text: str) -> str | None:
    for line in _iter_clean_lines(text):
        if line.startswith("talents="):
            value = line.partition("=")[2].strip()
            return value or None
    return None


_SIM_TYPE_EXPLANATIONS = {
    "quick_sim": "Raidbots would run a single-profile Quick Sim and report DPS with a detailed breakdown.",
    "top_gear_or_droptimizer": (
        "Raidbots would expand the profilesets into a multi-profile Top Gear / Droptimizer run and rank the "
        "variants by the chosen metric (per-actor damage/buff detail is not retained)."
    ),
    "advanced": "Raidbots would run this as an Advanced Sim, executing the raw SimC input as written.",
}


def simc_handoff(text: str, classification: dict[str, Any]) -> dict[str, Any]:
    """Build a ready-to-paste handoff: the SimC input plus suggested local `simc` commands.

    No simc import: the commands are strings for the agent / wrapper to run.
    """
    commands: list[dict[str, Any]] = [
        {
            "purpose": "Run the full profile locally instead of on the Raidbots cloud.",
            "command": "simc sim -",
            "stdin": "the SimC input above",
        }
    ]
    talents = _talents_value(text)
    actor_class = classification.get("actor_class")
    spec = classification.get("spec")
    # A bare SimC talent code cannot resolve class/spec on its own, so only suggest the
    # talent-decode commands when both are known, and pass them explicitly. shlex.quote keeps
    # the (untrusted, report-sourced) values shell-safe.
    if talents and actor_class and spec:
        identity = f"--actor-class {shlex.quote(str(actor_class))} --spec {shlex.quote(str(spec))}"
        talents_arg = shlex.quote(talents)
        commands.append(
            {
                "purpose": "Decode the talent loadout.",
                "command": f"simc decode-build {identity} --talents {talents_arg}",
            }
        )
        commands.append(
            {
                "purpose": "Summarize the build's APL and active talents.",
                "command": f"simc describe-build {identity} --talents {talents_arg}",
            }
        )

    return {
        "ready_to_paste": text,
        "classification": classification,
        "raidbots_behavior": _SIM_TYPE_EXPLANATIONS.get(
            classification.get("sim_type_guess", "advanced"),
            _SIM_TYPE_EXPLANATIONS["advanced"],
        ),
        "suggested_simc_commands": commands,
        "note": (
            "raidbots does not run SimC; these commands run locally (directly or via the warcraft wrapper). "
            "To execute on the Raidbots cloud, paste the input above into raidbots.com."
        ),
    }

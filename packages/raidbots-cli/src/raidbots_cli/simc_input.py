from __future__ import annotations

import re
import shlex
from typing import Any

from warcraft_core.identity import normalize_actor_class
from warcraft_core.wow_specs import WOW_CLASS_NAMES

# Local-only classification of SimulationCraft addon / profile text. raidbots must
# not import simc_cli (provider independence), so the suggested local commands below
# are emitted as plain strings for the agent / wrapper to run.

_WOW_CLASSES = frozenset(WOW_CLASS_NAMES)

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
_SPLIT_TALENT_KEYS = ("class_talents", "spec_talents", "hero_talents")


def _iter_clean_lines(text: str) -> list[str]:
    lines: list[str] = []
    for raw in (text or "").splitlines():
        # SimC treats `#` as a comment delimiter anywhere on a line; strip trailing inline
        # comments (and drop comment-only/blank lines) so values like `spec=frost # note`
        # don't carry the comment into classification. Mirrors simc-cli's build-text parsing.
        line = raw.split("#", 1)[0].strip()
        if not line:
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
        if key:
            values[key] = value.strip()
    return values


def _find_actor(lines: list[str]) -> tuple[str | None, str | None]:
    for line in lines:
        match = _ACTOR_RE.match(line)
        if not match:
            continue
        # SimC takes both `deathknight` and `death_knight`; either normalizes to the class key.
        actor_class = normalize_actor_class(match.group(1))
        if actor_class in _WOW_CLASSES:
            return actor_class, match.group(2).strip()
    return None, None


def _first_actor_assignments(lines: list[str]) -> dict[str, str]:
    """Read only the first actor's options; later actors and copies own their builds."""
    _, first_name = _find_actor(lines)
    if first_name is None:
        return _scalar_assignments(lines)
    values: dict[str, str] = {}
    seen_actor = False
    selected = False
    for line in lines:
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if normalize_actor_class(key) in _WOW_CLASSES:
            selected = not seen_actor
            seen_actor = True
        elif key == "active":
            selected = value.strip('"') == first_name
        elif key in {"copy", "pet"}:
            selected = False
        elif selected and not key.startswith("profileset."):
            values[key] = value
    return values


def _profileset_names(lines: list[str]) -> set[str]:
    names: set[str] = set()
    for line in lines:
        match = _PROFILESET_RE.match(line)
        if match:
            names.add(match.group(1) or match.group(2))
    return names


def looks_like_simc_input(text: str) -> bool:
    """Whether the text has at least one SimC ``key=value`` line; prose or binary has none."""
    return any("=" in line for line in _iter_clean_lines(text))


def classify_simc_input(text: str) -> dict[str, Any]:
    lines = _iter_clean_lines(text)
    assignments = _first_actor_assignments(lines)

    actor_class, actor_name = _find_actor(lines)
    profileset_names = _profileset_names(lines)
    copy_count = sum(1 for line in lines if "=" in line and line.split("=", 1)[0].strip() == "copy")
    global_assignments = _scalar_assignments(lines)
    options = {key: global_assignments[key] for key in _OPTION_KEYS if key in global_assignments}

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
        "talents_present": bool(assignments.get("talents")) or any(assignments.get(key) for key in _SPLIT_TALENT_KEYS),
        "options": options,
    }


def _talents_value(text: str) -> str | None:
    # Split on the first `=` and strip the key so `talents = CYG` (space-padded, valid SimC)
    # is recognized like `talents=CYG`, consistent with _scalar_assignments / _find_actor.
    return _first_actor_assignments(_iter_clean_lines(text)).get("talents") or None


def _split_talents(text: str) -> dict[str, str]:
    assignments = _first_actor_assignments(_iter_clean_lines(text))
    return {key: assignments[key] for key in _SPLIT_TALENT_KEYS if assignments.get(key)}


_SIM_TYPE_EXPLANATIONS = {
    "quick_sim": "Raidbots would run a single-profile Quick Sim and report DPS with a detailed breakdown.",
    "top_gear_or_droptimizer": (
        "Raidbots would expand the profilesets into a multi-profile Top Gear / Droptimizer run and rank the "
        "variants by the chosen metric (per-actor damage/buff detail is not retained)."
    ),
    "advanced": "Raidbots would run this as an Advanced Sim, executing the raw SimC input as written.",
}


def simc_handoff(text: str, classification: dict[str, Any]) -> dict[str, Any]:
    """Build the local handoff for SimC input: what Raidbots would do with it and suggested `simc` commands.

    No simc import: the commands are strings for the agent / wrapper to run.
    """
    commands: list[dict[str, Any]] = [
        {
            "purpose": "Run the full profile locally instead of on the Raidbots cloud.",
            "command": "simc sim -",
            "stdin": "the SimC input (data.input from `raidbots input`, or the text given to explain-input)",
        }
    ]
    talents = _talents_value(text)
    split_talents = _split_talents(text)
    actor_class = classification.get("actor_class")
    spec = classification.get("spec")
    # A bare SimC talent code cannot resolve class/spec on its own, so only suggest the
    # talent-decode commands when both are known, and pass them explicitly. shlex.quote keeps
    # the (untrusted, report-sourced) values shell-safe. Split tree options override
    # their trees in a combined loadout, so carry both forms when both are present.
    talent_parts = [f"--talents {shlex.quote(talents)}"] if talents else []
    talent_parts.extend(
        f"--{key.replace('_', '-')} {shlex.quote(split_talents[key])}"
        for key in _SPLIT_TALENT_KEYS if key in split_talents
    )
    talents_flags = " ".join(talent_parts)
    if talents_flags and actor_class and spec:
        identity = f"--actor-class {shlex.quote(str(actor_class))} --spec {shlex.quote(str(spec))}"
        commands.append(
            {
                "purpose": "Decode the talent loadout.",
                "command": f"simc decode-build {identity} {talents_flags}",
            }
        )
        commands.append(
            {
                "purpose": "Summarize the build's APL and active talents.",
                "command": f"simc describe-build {identity} {talents_flags}",
                # Unlike decode-build, describe-build needs an APL: without --apl-path it resolves the
                # default <class>_<spec> APL from a checked-out SimC repo and fails (not_found) if absent.
                "requires": "a checked-out SimC repo (run `simc doctor`), or pass --apl-path explicitly.",
            }
        )

    return {
        "classification": classification,
        "raidbots_behavior": _SIM_TYPE_EXPLANATIONS.get(
            classification.get("sim_type_guess", "advanced"),
            _SIM_TYPE_EXPLANATIONS["advanced"],
        ),
        "suggested_simc_commands": commands,
        "note": (
            "raidbots does not run SimC; these commands run locally (directly or via the warcraft wrapper). "
            "To execute on the Raidbots cloud, paste the SimC input into raidbots.com."
        ),
    }

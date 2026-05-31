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
        if key and key not in values:
            values[key] = value.strip()
    return values


def _normalize_class_token(value: str) -> str:
    # SimC accepts both the addon/profile actor form (`deathknight`) and the documented
    # manual-creation keyword form (`death_knight`); collapse to the canonical no-underscore
    # form so either is recognized. Mirrors simc-cli's _normalize_actor_class.
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _find_actor(lines: list[str]) -> tuple[str | None, str | None]:
    for line in lines:
        match = _ACTOR_RE.match(line)
        if not match:
            continue
        actor_class = _normalize_class_token(match.group(1))
        if actor_class in _WOW_CLASSES:
            return actor_class, match.group(2).strip()
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
        "talents_present": "talents" in assignments or any(key in assignments for key in _SPLIT_TALENT_KEYS),
        "options": options,
    }


def _talents_value(text: str) -> str | None:
    # Split on the first `=` and strip the key so `talents = CYG` (space-padded, valid SimC)
    # is recognized like `talents=CYG`, consistent with _scalar_assignments / _find_actor.
    for line in _iter_clean_lines(text):
        key, sep, value = line.partition("=")
        if sep and key.strip() == "talents":
            value = value.strip()
            return value or None
    return None


def _split_talents(text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for line in _iter_clean_lines(text):
        key, sep, value = line.partition("=")
        key = key.strip()
        if sep and key in _SPLIT_TALENT_KEYS and key not in found:
            value = value.strip()
            if value:
                found[key] = value
    return found


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
    split_talents = _split_talents(text)
    actor_class = classification.get("actor_class")
    spec = classification.get("spec")
    # A bare SimC talent code cannot resolve class/spec on its own, so only suggest the
    # talent-decode commands when both are known, and pass them explicitly. shlex.quote keeps
    # the (untrusted, report-sourced) values shell-safe. Prefer the combined `talents=` line;
    # fall back to the split class/spec/hero keys (the form edited/saved profiles use) so the
    # decode/describe handoff still fires. simc decode-build/describe-build accept both forms.
    if talents:
        talents_flags = f"--talents {shlex.quote(talents)}"
    else:
        talents_flags = " ".join(
            f"--{key.replace('_', '-')} {shlex.quote(split_talents[key])}"
            for key in _SPLIT_TALENT_KEYS
            if key in split_talents
        )
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

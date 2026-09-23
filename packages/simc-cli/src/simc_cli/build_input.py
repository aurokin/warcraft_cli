from __future__ import annotations

import json
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from warcraft_core.identity import (
    IdentityConfidence,
    validate_talent_transport_packet,
)
from warcraft_core.identity import (
    parse_wowhead_talent_calc_ref as parse_shared_wowhead_talent_calc_ref,
)
from warcraft_core.talent_transport import specialization_ids, tokenize_talent_name

from simc_cli.repo import RepoPaths
from simc_cli.trait_data import TieredEntry, load_trait_table

ACTOR_LINE_RE = re.compile(r'^([a-z_]+)\s*=\s*"?(.*?)"?$')
TALENT_DEBUG_RE = re.compile(
    r"adding (?P<tree>class|spec|hero|selection) talent (?P<name>.+?) "
    r"\(node=(?P<node>\d+) entry=(?P<entry>\d+) rank=(?P<rank>\d+)/(?P<max_rank>\d+)\)"
)
# SimC prints this once per hero tree the build actually selected, in two shapes depending on which
# code path activated it: `activating sub tree Sunfury (id=39)` from a hash, `... (39)` otherwise.
SUB_TREE_DEBUG_RE = re.compile(r"activating sub tree (?P<name>.+?) \((?:id=)?(?P<id>\d+)\)")
# SimC's `log=1` line when a talent option overwrites a rank the talent hash already allocated. It is
# the only place SimC reports the per-entry ranks it spread over a tiered node.
OVERWRITE_LOG_RE = re.compile(r"Overwriting talent (?P<name>.+?) \((?P<entry>\d+)\), rank (?P<rank>\d+) -> 0")
# SimC keeps simulating after this one: the decode profile carries no gear on purpose.
BENIGN_INIT_ERROR = "has no weapon equipped"
# SimC's debug stream does not always end a line before writing an error, so the marker is matched
# anywhere on the line rather than anchored to its start.
_ERROR_LINE_RE = re.compile(r"Error:\s*(?P<message>.+?)\s*$")

DEFAULT_RACE_BY_CLASS = {
    "deathknight": "human",
    "demonhunter": "night_elf",
    "druid": "night_elf",
    "evoker": "dracthyr",
    "hunter": "dwarf",
    "mage": "human",
    "monk": "pandaren",
    "paladin": "human",
    "priest": "human",
    "rogue": "human",
    "shaman": "orc",
    "warlock": "human",
    "warrior": "human",
}


@dataclass(frozen=True, slots=True)
class TalentStrings:
    """The talent-string flag group; the four values are always supplied together by a build-input command."""

    talents: str | None = None
    class_talents: str | None = None
    spec_talents: str | None = None
    hero_talents: str | None = None


@dataclass(slots=True)
class BuildSpec:
    actor_class: str | None = None
    spec: str | None = None
    talents: str | None = None
    class_talents: str | None = None
    spec_talents: str | None = None
    hero_talents: str | None = None
    source_kind: str | None = None
    source_notes: list[str] = field(default_factory=list)
    transport_form: str | None = None
    transport_status: str | None = None
    transport_source: str | None = None


def has_talent_data(build_spec: BuildSpec) -> bool:
    return any([build_spec.talents, build_spec.class_talents, build_spec.spec_talents, build_spec.hero_talents])


@dataclass(slots=True)
class DecodedTalent:
    tree: str
    name: str
    token: str
    rank: int
    max_rank: int
    entry: int = 0
    # False for a tiered node decoded from a talent hash: SimC spreads the node's ranks over its
    # entries and then prints the leftover (always 0), so the talent is taken at an unknown rank.
    rank_known: bool = True

    @property
    def taken(self) -> bool:
        return self.rank > 0 or not self.rank_known


@dataclass(frozen=True, slots=True)
class HeroTree:
    name: str
    id: int


@dataclass(slots=True)
class BuildResolution:
    actor_class: str
    spec: str
    enabled_talents: set[str]
    talents_by_tree: dict[str, list[DecodedTalent]]
    source_kind: str | None
    generated_profile_text: str | None
    source_notes: list[str]
    hero_tree: HeroTree | None = None
    # Hero talents the hash granted for a hero tree the build did not select. SimC disables them,
    # so they are reported separately instead of counting as part of the build.
    inactive_hero_talents: list[DecodedTalent] = field(default_factory=list)


class SimcBuildError(RuntimeError):
    """SimC rejected the build input. Carries only the SimC error lines plus a bounded preview."""

    def __init__(self, message: str, *, output_preview: list[str], returncode: int) -> None:
        super().__init__(message)
        self.output_preview = output_preview
        self.returncode = returncode


PREVIEW_LINES = 20
PREVIEW_LINE_CHARS = 200


def bounded_output_preview(output: str) -> list[str]:
    """The tail of SimC's ``debug=1`` stream, bounded in both dimensions.

    A line count alone is not a bound: SimC prints the enemy's stat block on a single ~4 KB line, so
    each line is clipped as well to keep the error envelope small enough to read.
    """
    return [
        line if len(line) <= PREVIEW_LINE_CHARS else f"{line[:PREVIEW_LINE_CHARS]}... ({len(line)} chars, truncated)"
        for line in output.splitlines()[-PREVIEW_LINES:]
    ]


def _load_build_packet(path: str) -> tuple[dict[str, Any], str]:
    resolved = Path(path).expanduser().resolve()
    raw = json.loads(resolved.read_text())
    packet = validate_talent_transport_packet(raw)
    return packet, str(resolved)


def _identity_value(packet: dict[str, Any], key: str) -> str | None:
    build_identity = packet.get("build_identity")
    if isinstance(build_identity, dict):
        class_spec_identity = build_identity.get("class_spec_identity")
        if isinstance(class_spec_identity, dict):
            identity = class_spec_identity.get("identity")
            if isinstance(identity, dict):
                value = identity.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    return None


def _validated_packet_identity(packet: dict[str, Any]) -> tuple[str | None, str | None]:
    actor_class = _normalize_actor_class(_identity_value(packet, "actor_class"))
    spec = _normalize_spec_name(_identity_value(packet, "spec"))
    validation = packet.get("validation")
    if not isinstance(validation, dict) or validation.get("status") != "validated":
        return None, None
    validated_actor_class = _normalize_actor_class(validation.get(
        "actor_class")) if isinstance(validation.get("actor_class"), str) else None
    validated_spec = _normalize_spec_name(validation.get("spec")) if isinstance(validation.get("spec"), str) else None
    if actor_class and spec and actor_class == validated_actor_class and spec == validated_spec:
        return actor_class, spec
    return None, None


def _packet_spec_from_wowhead_ref(
    transport_forms: dict[str, Any],
    source_notes: list[str],
    transport_status_text: str | None,
    resolved_path: str,
) -> BuildSpec | None:
    wowhead_ref = transport_forms.get("wowhead_talent_calc_url")
    if not (isinstance(wowhead_ref, str) and wowhead_ref.strip()):
        return None
    parsed = parse_wowhead_talent_calc_ref(wowhead_ref)
    if parsed is None or not parsed.talents:
        raise ValueError(f"Invalid wowhead_talent_calc_url transport form in build packet: {resolved_path}")
    source_notes.append("transport form: wowhead_talent_calc_url")
    return BuildSpec(
        actor_class=parsed.actor_class,
        spec=parsed.spec,
        talents=parsed.talents,
        source_kind="wowhead_talent_calc_url",
        source_notes=source_notes,
        transport_form="wowhead_talent_calc_url",
        transport_status=transport_status_text,
        transport_source=resolved_path,
    )


def _packet_spec_from_wow_export(
    transport_forms: dict[str, Any],
    source_notes: list[str],
    transport_status_text: str | None,
    resolved_path: str,
) -> BuildSpec | None:
    wow_export = transport_forms.get("wow_talent_export")
    if not (isinstance(wow_export, str) and wow_export.strip()):
        return None
    source_notes.extend(
        [
            "transport form: wow_talent_export",
            "class/spec metadata came from packet contents and was not independently validated",
        ]
    )
    return BuildSpec(
        actor_class=None,
        spec=None,
        talents=wow_export.strip(),
        source_kind="wow_talent_export",
        source_notes=source_notes,
        transport_form="wow_talent_export",
        transport_status=transport_status_text,
        transport_source=resolved_path,
    )


def _packet_spec_from_split_talents(
    transport_forms: dict[str, Any],
    packet: dict[str, Any],
    source_notes: list[str],
    transport_status_text: str | None,
    resolved_path: str,
) -> BuildSpec | None:
    split = transport_forms.get("simc_split_talents")
    if not isinstance(split, dict):
        return None
    class_talents = split.get("class_talents")
    spec_talents = split.get("spec_talents")
    hero_talents = split.get("hero_talents")
    if not any(isinstance(value, str) and value.strip() for value in (class_talents, spec_talents, hero_talents)):
        return None
    packet_actor_class, packet_spec = _validated_packet_identity(packet)
    if transport_status_text != "validated" or not (packet_actor_class and packet_spec):
        raise ValueError(
            f"simc_split_talents transport form requires a validated packet identity: {resolved_path}. "
            "Run simc validate-talent-transport first for raw_only packets."
        )
    source_notes.extend(
        [
            "transport form: simc_split_talents",
            "class/spec metadata came from packet contents and was validated with the split transport",
        ]
    )
    return BuildSpec(
        actor_class=packet_actor_class,
        spec=packet_spec,
        class_talents=class_talents.strip() if isinstance(class_talents, str) and class_talents.strip() else None,
        spec_talents=spec_talents.strip() if isinstance(spec_talents, str) and spec_talents.strip() else None,
        hero_talents=hero_talents.strip() if isinstance(hero_talents, str) and hero_talents.strip() else None,
        source_kind="simc_split_talents",
        source_notes=source_notes,
        transport_form="simc_split_talents",
        transport_status=transport_status_text,
        transport_source=resolved_path,
    )


def extract_build_spec_from_packet(path: str) -> BuildSpec:
    packet, resolved_path = _load_build_packet(path)
    raw_transport_forms = packet.get("transport_forms")
    transport_forms: dict[str, Any] = raw_transport_forms if isinstance(raw_transport_forms, dict) else {}
    source_notes = [f"build packet: {resolved_path}", "talent transport packet"]
    source = packet.get("source")
    if isinstance(source, dict):
        provider = source.get("provider")
        packet_source = source.get("source")
        if isinstance(provider, str) and provider.strip():
            source_notes.append(f"packet provider: {provider.strip()}")
        if isinstance(packet_source, str) and packet_source.strip():
            source_notes.append(f"packet source: {packet_source.strip()}")
    transport_status = packet.get("transport_status")
    transport_status_text = transport_status.strip() if isinstance(transport_status, str) and transport_status.strip() else None

    spec = _packet_spec_from_wowhead_ref(transport_forms, source_notes, transport_status_text, resolved_path)
    if spec is not None:
        return spec
    spec = _packet_spec_from_wow_export(transport_forms, source_notes, transport_status_text, resolved_path)
    if spec is not None:
        return spec
    spec = _packet_spec_from_split_talents(transport_forms, packet, source_notes, transport_status_text, resolved_path)
    if spec is not None:
        return spec

    raise ValueError(
        f"Build packet does not include a supported transport form for simc analysis: {resolved_path}. "
        "Run simc validate-talent-transport first for raw_only packets."
    )


@dataclass(slots=True)
class BuildIdentity:
    actor_class: str | None
    spec: str | None
    confidence: IdentityConfidence
    source: str
    candidate_count: int
    candidates: list[tuple[str, str]] = field(default_factory=list)
    source_notes: list[str] = field(default_factory=list)


def infer_actor_and_spec_from_apl(apl_path: str | Path) -> tuple[str | None, str | None]:
    stem = Path(apl_path).stem
    if "_" not in stem:
        return None, None
    actor_class, spec = stem.split("_", 1)
    return actor_class, spec


def _normalize_actor_class(value: str | None) -> str | None:
    if not value:
        return None
    normalized = re.sub(r"[^a-z0-9]+", "", value.lower())
    return normalized or None


def _normalize_spec_name(value: str | None) -> str | None:
    if not value:
        return None
    normalized = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return normalized or None


def _has_trusted_identity_hint(build_spec: BuildSpec) -> bool:
    return any(
        note.startswith(("inferred from apl:", "profile:", "build file:"))
        or note in {"command-line build options", "inline build text"}
        for note in build_spec.source_notes
    )


class UnsupportedBuildReference(ValueError):
    """A build reference SimC has no way to decode; ``reference_type`` names what it was."""

    def __init__(self, message: str, *, reference_type: str) -> None:
        super().__init__(message)
        self.reference_type = reference_type


WOWHEAD_HOST = "wowhead.com"
TALENT_CALC_SEGMENT = "talent-calc"
BLIZZARD_CALC_SEGMENT = "blizzard"


def _url_path_segments(ref: str) -> list[str] | None:
    """The path segments of ``ref`` when it is an absolute URL, else None."""
    parsed = urlparse(ref)
    if not parsed.scheme or not parsed.netloc:
        return None
    return [segment for segment in parsed.path.split("/") if segment]


def wowhead_blizzard_build_code(ref: str) -> str | None:
    """The talent hash in a Wowhead ``/talent-calc/blizzard/<hash>`` URL.

    That URL names no class or spec, so the hash reads as a plain WoW export. ``simc modify-build``
    publishes exactly this URL for its result, so the CLI has to be able to read its own output back.
    """
    segments = _url_path_segments(ref)
    host = (urlparse(ref).hostname or "").lower()
    if segments is None or not (host == WOWHEAD_HOST or host.endswith(f".{WOWHEAD_HOST}")):
        return None
    if TALENT_CALC_SEGMENT not in segments:
        return None
    tail = segments[segments.index(TALENT_CALC_SEGMENT) + 1:]
    return tail[1] if len(tail) == 2 and tail[0] == BLIZZARD_CALC_SEGMENT else None


def reject_unsupported_build_reference(ref: str) -> None:
    """Refuse a URL that is no build reference instead of handing it to SimC as if it were a hash."""
    if _url_path_segments(ref) is None or _raw_wowhead_talent_calc_ref(ref) is not None:
        return
    raise UnsupportedBuildReference(
        f"Cannot decode this build reference: {ref}. simc decodes a WoW talent export string, a "
        "Wowhead talent-calc URL that names the class and spec, and a Wowhead "
        "/talent-calc/blizzard/<hash> URL.",
        reference_type="url",
    )


def _raw_wowhead_talent_calc_ref(ref: str) -> dict[str, str | None] | None:
    return parse_shared_wowhead_talent_calc_ref(ref)


def _ensure_exact_wowhead_talent_calc_ref(ref: str) -> dict[str, str | None] | None:
    parsed = _raw_wowhead_talent_calc_ref(ref)
    if parsed is None:
        return None
    if not parsed["build_code"]:
        raise UnsupportedBuildReference(
            f"Wowhead talent-calc URL carries no build code: {ref}. Copy the URL with the build code "
            "on the end.",
            reference_type="wowhead_talent_calc_url",
        )
    return parsed


def parse_wowhead_talent_calc_ref(ref: str) -> BuildSpec | None:
    parsed = _ensure_exact_wowhead_talent_calc_ref(ref)
    if parsed is None:
        return None

    source_notes = ["wowhead talent-calc url"]
    build_code = parsed["build_code"]
    if build_code:
        source_notes.append("wowhead build code")
    return BuildSpec(
        actor_class=parsed["actor_class"],
        spec=parsed["spec"],
        talents=build_code,
        source_kind="wowhead_talent_calc_url",
        source_notes=source_notes,
    )


def detect_build_text_source_kind(text: str) -> str | None:
    non_empty_lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not non_empty_lines:
        return None
    if len(non_empty_lines) == 1:
        shared_ref = _raw_wowhead_talent_calc_ref(non_empty_lines[0])
        if shared_ref is not None:
            return "wowhead_talent_calc_url"
        if wowhead_blizzard_build_code(non_empty_lines[0]):
            return "wow_talent_export"
    if len(non_empty_lines) == 1 and "=" not in non_empty_lines[0]:
        return "wow_talent_export"

    saw_actor_line = False
    saw_talents = False
    saw_split_talents = False
    for raw_line in non_empty_lines:
        line = raw_line.split("#", 1)[0].strip()
        if not line or "=" not in line:
            continue
        key, _value = line.split("=", 1)
        key = key.strip()
        actor_match = ACTOR_LINE_RE.match(line)
        if actor_match and key in DEFAULT_RACE_BY_CLASS:
            saw_actor_line = True
            continue
        if key == "talents":
            saw_talents = True
        elif key in {"class_talents", "spec_talents", "hero_talents"}:
            saw_split_talents = True

    if saw_split_talents:
        return "simc_split_talents"
    if saw_actor_line or saw_talents:
        return "simc_profile"
    return "simc_build_text"


def _single_line_build_spec(spec: BuildSpec, non_empty_lines: list[str]) -> BuildSpec | None:
    if len(non_empty_lines) != 1:
        return None
    line = non_empty_lines[0]
    wowhead_ref = parse_wowhead_talent_calc_ref(line)
    if wowhead_ref is not None:
        return wowhead_ref
    blizzard_code = wowhead_blizzard_build_code(line)
    if blizzard_code:
        spec.talents = blizzard_code
        spec.source_notes.append("wowhead talent-calc blizzard url")
        return spec
    reject_unsupported_build_reference(line)
    if "=" not in line:
        spec.talents = line
        spec.source_notes.append("single-line talent export")
        return spec
    return None


def _parse_simc_build_text_lines(spec: BuildSpec, non_empty_lines: list[str]) -> None:
    for raw_line in non_empty_lines:
        line = raw_line.split("#", 1)[0].strip()
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"')
        actor_match = ACTOR_LINE_RE.match(line)
        if actor_match and key in DEFAULT_RACE_BY_CLASS:
            spec.actor_class = key
            spec.source_notes.append(f"actor line: {key}")
            continue
        if key == "spec":
            spec.spec = value
        elif key == "talents":
            spec.talents = value
        elif key == "class_talents":
            spec.class_talents = value
        elif key == "spec_talents":
            spec.spec_talents = value
        elif key == "hero_talents":
            spec.hero_talents = value


def extract_build_spec_from_text(text: str) -> BuildSpec:
    spec = BuildSpec()
    spec.source_kind = detect_build_text_source_kind(text)
    non_empty_lines = [line.strip() for line in text.splitlines() if line.strip()]
    single = _single_line_build_spec(spec, non_empty_lines)
    if single is not None:
        return single

    _parse_simc_build_text_lines(spec, non_empty_lines)
    if spec.talents:
        spec.source_notes.append("simc talents input")
    if spec.class_talents or spec.spec_talents or spec.hero_talents:
        spec.source_notes.append("split talent strings")
    return spec


def merge_build_specs(*specs: BuildSpec) -> BuildSpec:
    merged = BuildSpec()
    for spec in specs:
        if spec.actor_class:
            merged.actor_class = spec.actor_class
        if spec.spec:
            merged.spec = spec.spec
        if spec.talents:
            merged.talents = spec.talents
        if spec.class_talents:
            merged.class_talents = spec.class_talents
        if spec.spec_talents:
            merged.spec_talents = spec.spec_talents
        if spec.hero_talents:
            merged.hero_talents = spec.hero_talents
        if spec.source_kind:
            merged.source_kind = spec.source_kind
        if spec.transport_form:
            merged.transport_form = spec.transport_form
        if spec.transport_status:
            merged.transport_status = spec.transport_status
        if spec.transport_source:
            merged.transport_source = spec.transport_source
        merged.source_notes.extend(spec.source_notes)
    return merged


def build_profile_text(build_spec: BuildSpec) -> str:
    actor_class = build_spec.actor_class
    if not actor_class:
        raise ValueError("Build spec must include actor_class.")
    race = DEFAULT_RACE_BY_CLASS.get(actor_class, "human")
    lines = [
        f'{actor_class}="simc_decode"',
        "level=90",
        f"race={race}",
        f"spec={build_spec.spec}",
    ]
    if build_spec.talents:
        lines.append(f"talents={build_spec.talents}")
    if build_spec.class_talents:
        lines.append(f"class_talents={build_spec.class_talents}")
    if build_spec.spec_talents:
        lines.append(f"spec_talents={build_spec.spec_talents}")
    if build_spec.hero_talents:
        lines.append(f"hero_talents={build_spec.hero_talents}")
    return "\n".join(lines) + "\n"


def parse_debug_talents(output: str) -> dict[str, list[DecodedTalent]]:
    talents_by_tree: dict[str, list[DecodedTalent]] = {"class": [], "spec": [], "hero": [], "selection": []}
    for line in output.splitlines():
        match = TALENT_DEBUG_RE.search(line)
        if not match:
            continue
        tree = match.group("tree")
        name = match.group("name")
        if tree == "selection":
            continue
        rank = int(match.group("rank"))
        talents_by_tree[tree].append(
            DecodedTalent(
                tree=tree,
                name=name,
                token=tokenize_talent_name(name),
                rank=rank,
                max_rank=int(match.group("max_rank")),
                entry=int(match.group("entry")),
                rank_known=rank > 0,
            )
        )
    return talents_by_tree


def parse_active_hero_trees(output: str) -> list[HeroTree]:
    """Read the hero trees SimC activated for the build, in the order it printed them."""
    trees: list[HeroTree] = []
    seen: set[int] = set()
    for line in output.splitlines():
        match = SUB_TREE_DEBUG_RE.search(line)
        if not match:
            continue
        tree_id = int(match.group("id"))
        if tree_id not in seen:
            seen.add(tree_id)
            trees.append(HeroTree(name=match.group("name").strip(), id=tree_id))
    return trees


def simc_build_errors(output: str) -> list[str]:
    """SimC error lines that mean the build input was rejected.

    The decode profile deliberately carries no gear, so SimC's "no weapon equipped" initialization
    error is expected and is not a rejection of the talents.
    """
    errors: list[str] = []
    for line in output.splitlines():
        match = _ERROR_LINE_RE.search(line)
        if match is None:
            continue
        message = match.group("message")
        if BENIGN_INIT_ERROR in message:
            continue
        errors.append(message)
    return errors


def normalize_talents_input(value: str | None) -> str | None:
    if not value:
        return None
    stripped = value.strip()
    if stripped.startswith("talents="):
        return stripped.split("=", 1)[1].strip()
    wowhead_ref = parse_wowhead_talent_calc_ref(stripped)
    if wowhead_ref is not None and wowhead_ref.talents:
        return wowhead_ref.talents
    blizzard_code = wowhead_blizzard_build_code(stripped)
    if blizzard_code:
        return blizzard_code
    reject_unsupported_build_reference(stripped)
    return stripped


def detect_talents_option_source_kind(*, talents: TalentStrings) -> str | None:
    if talents.class_talents or talents.spec_talents or talents.hero_talents:
        return "simc_split_talents"
    if not talents.talents:
        return None
    stripped = talents.talents.strip()
    if _raw_wowhead_talent_calc_ref(stripped) is not None:
        return "wowhead_talent_calc_url"
    if stripped.startswith("talents="):
        return "simc_profile"
    return "wow_talent_export"


def _reject_blank_build_options(supplied: dict[str, str | None]) -> None:
    """Refuse a build-input option that was passed with an empty value.

    An empty ``--talents`` used to be indistinguishable from an omitted one, so the command answered
    with an empty build and ``ok: true`` instead of saying the input carried nothing.
    """
    blank = sorted(name for name, value in supplied.items() if value is not None and not value.strip())
    if blank:
        raise ValueError(f"Build input options were given an empty value: {', '.join(blank)}.")


def load_build_spec(
    *,
    apl_path: str | Path | None,
    profile_path: str | None,
    build_file: str | None,
    build_text: str | None,
    talents: TalentStrings,
    actor_class: str | None,
    spec_name: str | None,
    build_packet: str | None = None,
) -> BuildSpec:
    _reject_blank_build_options(
        {
            "--profile-path": profile_path,
            "--build-file": build_file,
            "--build-packet": build_packet,
            "--build-text": build_text,
            "--talents": talents.talents,
            "--class-talents": talents.class_talents,
            "--spec-talents": talents.spec_talents,
            "--hero-talents": talents.hero_talents,
            "--actor-class": actor_class,
            "--spec": spec_name,
        }
    )
    if build_packet and any(
        value
        for value in (
            profile_path,
            build_file,
            build_text,
            talents.talents,
            talents.class_talents,
            talents.spec_talents,
            talents.hero_talents,
            actor_class,
            spec_name,
        )
    ):
        raise ValueError("Cannot combine --build-packet with other explicit build input options.")

    inferred = BuildSpec()
    if apl_path:
        inferred_class, inferred_spec = infer_actor_and_spec_from_apl(apl_path)
        inferred.actor_class = inferred_class
        inferred.spec = inferred_spec
        if inferred.actor_class or inferred.spec:
            inferred.source_notes.append(f"inferred from apl: {Path(apl_path).stem}")

    from_talents_option = BuildSpec()
    if talents.talents:
        from_talents_option = parse_wowhead_talent_calc_ref(talents.talents) or BuildSpec()
        if from_talents_option.source_notes:
            from_talents_option.source_notes.append("command-line talents option")

    explicit = BuildSpec(
        actor_class=actor_class,
        spec=spec_name,
        talents=normalize_talents_input(talents.talents),
        class_talents=talents.class_talents,
        spec_talents=talents.spec_talents,
        hero_talents=talents.hero_talents,
        source_kind=detect_talents_option_source_kind(talents=talents),
        source_notes=["command-line build options"] if any([talents.talents, talents.class_talents, talents.spec_talents,
                                                            talents.hero_talents, actor_class, spec_name]) else [],
    )

    from_profile = BuildSpec()
    if profile_path:
        resolved = Path(profile_path).expanduser().resolve()
        from_profile = extract_build_spec_from_text(resolved.read_text())
        from_profile.source_notes.append(f"profile: {resolved}")

    from_build_file = BuildSpec()
    if build_file:
        resolved = Path(build_file).expanduser().resolve()
        from_build_file = extract_build_spec_from_text(resolved.read_text())
        from_build_file.source_notes.append(f"build file: {resolved}")

    from_build_packet = BuildSpec()
    if build_packet:
        from_build_packet = extract_build_spec_from_packet(build_packet)

    from_build_text = BuildSpec()
    if build_text:
        from_build_text = extract_build_spec_from_text(build_text)
        from_build_text.source_notes.append("inline build text")

    return merge_build_specs(inferred, from_profile, from_build_file, from_build_packet, from_build_text, from_talents_option, explicit)


def _direct_build_identity(build_spec: BuildSpec) -> tuple[BuildSpec, BuildIdentity]:
    source = "direct"
    confidence: IdentityConfidence = "high"
    if build_spec.source_kind == "wowhead_talent_calc_url":
        source = "wowhead_talent_calc_url"
    elif build_spec.source_kind == "simc_split_talents":
        source = "simc_split_talents"
    elif build_spec.source_kind == "wow_talent_export":
        source = "wow_talent_export"
        confidence = "medium"
    elif any(note.startswith("inferred from apl:") for note in build_spec.source_notes):
        # Class and spec came from an APL file name, not from the build data itself.
        source = "apl_path"
        confidence = "medium"
    return (
        build_spec,
        BuildIdentity(
            actor_class=build_spec.actor_class,
            spec=build_spec.spec,
            confidence=confidence,
            source=source,
            candidate_count=1,
            candidates=[(build_spec.actor_class, build_spec.spec)] if build_spec.actor_class and build_spec.spec else [],
            source_notes=build_spec.source_notes[:],
        ),
    )


def _probe_build_matches(
    repo: RepoPaths,
    build_spec: BuildSpec,
    *,
    unverified_packet_transport: bool,
    trusted_identity_hint: bool,
) -> list[tuple[str, str]]:
    # Every spec SimC's generated data knows, healers included: they ship no APL, so a candidate list
    # drawn from APL files could never identify a healer build.
    candidate_specs = sorted(specialization_ids(repo.root))
    if build_spec.actor_class and (not unverified_packet_transport or trusted_identity_hint):
        candidate_specs = [item for item in candidate_specs if item[0] == build_spec.actor_class]
    if build_spec.spec and (not unverified_packet_transport or trusted_identity_hint):
        candidate_specs = [item for item in candidate_specs if item[1] == build_spec.spec]

    matches: list[tuple[str, str]] = []
    for actor_class, spec in candidate_specs:
        probe_spec = BuildSpec(
            actor_class=actor_class,
            spec=spec,
            talents=build_spec.talents,
            class_talents=build_spec.class_talents,
            spec_talents=build_spec.spec_talents,
            hero_talents=build_spec.hero_talents,
            source_kind=build_spec.source_kind,
            source_notes=build_spec.source_notes[:],
        )
        try:
            resolution = decode_build(repo, probe_spec)
        except (FileNotFoundError, RuntimeError, ValueError):
            continue
        if resolution.enabled_talents:
            matches.append((actor_class, spec))
    return matches


def identify_build(repo: RepoPaths, build_spec: BuildSpec) -> tuple[BuildSpec, BuildIdentity]:
    unverified_packet_transport = getattr(build_spec, "transport_form", None) == "wow_talent_export"
    trusted_identity_hint = _has_trusted_identity_hint(build_spec)

    if build_spec.actor_class and build_spec.spec and not unverified_packet_transport:
        return _direct_build_identity(build_spec)

    # Without talent data there is nothing reliable to probe.
    if not has_talent_data(build_spec):
        return (
            build_spec,
            BuildIdentity(
                actor_class=build_spec.actor_class,
                spec=build_spec.spec,
                confidence="none",
                source="missing_build_data",
                candidate_count=0,
                source_notes=build_spec.source_notes[:],
            ),
        )

    matches = _probe_build_matches(
        repo,
        build_spec,
        unverified_packet_transport=unverified_packet_transport,
        trusted_identity_hint=trusted_identity_hint,
    )

    if len(matches) == 1:
        actor_class, spec = matches[0]
        identified = merge_build_specs(build_spec, BuildSpec(actor_class=actor_class, spec=spec))
        identified.source_notes.append("identified by SimC probe")
        return (
            identified,
            BuildIdentity(
                actor_class=actor_class,
                spec=spec,
                confidence="high",
                source="simc_probe",
                candidate_count=len(matches),
                candidates=matches,
                source_notes=identified.source_notes[:],
            ),
        )

    unresolved = BuildSpec(
        actor_class=None if unverified_packet_transport else build_spec.actor_class,
        spec=None if unverified_packet_transport else build_spec.spec,
        talents=build_spec.talents,
        class_talents=build_spec.class_talents,
        spec_talents=build_spec.spec_talents,
        hero_talents=build_spec.hero_talents,
        source_kind=build_spec.source_kind,
        source_notes=build_spec.source_notes[:],
        transport_form=build_spec.transport_form,
        transport_status=build_spec.transport_status,
        transport_source=build_spec.transport_source,
    )
    return (
        unresolved,
        BuildIdentity(
            actor_class=None if unverified_packet_transport else build_spec.actor_class,
            spec=None if unverified_packet_transport else build_spec.spec,
            confidence="low" if matches else "none",
            source="simc_probe",
            candidate_count=len(matches),
            candidates=matches,
            source_notes=build_spec.source_notes[:],
        ),
    )


SIMC_BUILD_ARGS = (
    "iterations=1",
    "max_time=1",
    "vary_combat_length=0",
    "desired_targets=1",
    "fight_style=Patchwerk",
    "allow_experimental_specializations=1",
)


@dataclass(frozen=True, slots=True)
class SimcRun:
    output: str
    returncode: int
    saved_profile: str | None


def _run_simc(repo: RepoPaths, profile_text: str, *, extra_args: tuple[str, ...], save: bool = False) -> SimcRun:
    """Run the checkout's SimC binary over ``profile_text``, optionally keeping the profile it saves."""
    with tempfile.TemporaryDirectory(prefix="simc-cli-build-") as temp_dir:
        save_path = Path(temp_dir) / "saved.simc"
        if save:
            profile_text += f"save={save_path}\n"
        profile_path = Path(temp_dir) / "build.simc"
        profile_path.write_text(profile_text)
        cmd = [str(repo.build_simc), str(profile_path), *SIMC_BUILD_ARGS, *extra_args]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)  # noqa: S603
        saved_profile = save_path.read_text() if save and save_path.exists() else None
    return SimcRun(output=proc.stdout + proc.stderr, returncode=proc.returncode, saved_profile=saved_profile)


def _join_talent_options(*values: str | None) -> str | None:
    parts = [value for value in values if value]
    return "/".join(parts) or None


def _probe_overwritten_ranks(repo: RepoPaths, build_spec: BuildSpec, zeroed: dict[str, list[str]]) -> dict[int, int]:
    """Re-run the build with ``zeroed`` entries set to rank 0 and read back the ranks SimC overwrites."""
    probe = BuildSpec(
        actor_class=build_spec.actor_class,
        spec=build_spec.spec,
        talents=build_spec.talents,
        class_talents=_join_talent_options(build_spec.class_talents, "/".join(zeroed.get("class", []))),
        spec_talents=_join_talent_options(build_spec.spec_talents, "/".join(zeroed.get("spec", []))),
        hero_talents=_join_talent_options(build_spec.hero_talents, "/".join(zeroed.get("hero", []))),
    )
    run = _run_simc(repo, build_profile_text(probe), extra_args=("log=1",))
    return {int(match.group("entry")): int(match.group("rank")) for match in OVERWRITE_LOG_RE.finditer(run.output)}


def _expand_tiered_talents(repo: RepoPaths, build_spec: BuildSpec, talents_by_tree: dict[str, list[DecodedTalent]]) -> None:
    """Replace each tiered node's single decoded row with one row per entry, carrying its real rank.

    A talent hash allocates a tiered node as one total that SimC spreads over the node's entries, and
    its decode prints a single line per node holding whatever rank was left over (always 0). The
    per-entry ranks therefore never reach the debug stream, and a build that cannot be re-serialized
    loses the whole node on every ``modify-build`` tree swap. Setting those entries to rank 0 in a
    second run makes SimC log the rank it overwrites, which is the rank the build actually had.
    """
    placeholders = [
        (tree, talent) for tree in ("class", "spec", "hero") for talent in talents_by_tree[tree] if not talent.rank_known
    ]
    if not placeholders:
        return
    siblings_by_entry = load_trait_table(repo.root).tiered_siblings_by_entry
    pending: list[tuple[str, DecodedTalent, tuple[TieredEntry, ...]]] = []
    zeroed: dict[str, list[str]] = {}
    for tree, talent in placeholders:
        siblings = siblings_by_entry.get(talent.entry)
        if siblings is None:
            continue
        pending.append((tree, talent, siblings))
        zeroed.setdefault(tree, []).extend(f"{sibling.entry}:0" for sibling in siblings)
    if not pending:
        return

    ranks = _probe_overwritten_ranks(repo, build_spec, zeroed)
    expanded_by_entry: dict[int, list[DecodedTalent]] = {}
    for tree, talent, siblings in pending:
        rows = [
            DecodedTalent(
                tree=tree,
                name=talent.name,
                token=talent.token,
                rank=ranks[sibling.entry],
                max_rank=sibling.max_rank,
                entry=sibling.entry,
            )
            for sibling in siblings
            if ranks.get(sibling.entry)
        ]
        if rows:
            expanded_by_entry[talent.entry] = rows
    for tree in ("class", "spec", "hero"):
        talents_by_tree[tree] = [
            row for talent in talents_by_tree[tree] for row in expanded_by_entry.get(talent.entry, [talent])
        ]


def decode_build(repo: RepoPaths, build_spec: BuildSpec) -> BuildResolution:
    if not build_spec.actor_class or not build_spec.spec:
        raise ValueError("Need both actor class and spec to decode talent strings.")
    if not has_talent_data(build_spec):
        # An APL-only view (priority, inactive-actions) decodes a build that carries no talents.
        return BuildResolution(
            actor_class=build_spec.actor_class,
            spec=build_spec.spec,
            enabled_talents=set(),
            talents_by_tree={"class": [], "spec": [], "hero": [], "selection": []},
            source_kind=build_spec.source_kind,
            generated_profile_text=None,
            source_notes=build_spec.source_notes[:],
        )
    if not repo.build_simc.exists():
        raise FileNotFoundError(f"SimC binary not found: {repo.build_simc}")

    profile_text = build_profile_text(build_spec)
    run = _run_simc(repo, profile_text, extra_args=("debug=1",))
    output = run.output

    errors = simc_build_errors(output)
    if errors:
        raise SimcBuildError(
            " ".join(errors),
            output_preview=bounded_output_preview(output),
            returncode=run.returncode,
        )
    talents_by_tree = parse_debug_talents(output)
    if not any(talents_by_tree[tree] for tree in ("class", "spec", "hero")):
        raise SimcBuildError(
            f"SimC exited {run.returncode} without printing any talents for the build.",
            output_preview=bounded_output_preview(output),
            returncode=run.returncode,
        )
    _expand_tiered_talents(repo, build_spec, talents_by_tree)

    hero_trees = parse_active_hero_trees(output)
    hero_tree = hero_trees[0] if len(hero_trees) == 1 else None
    inactive_hero = _split_inactive_hero_talents(repo, talents_by_tree, hero_trees)
    enabled_talents = {
        talent.token
        for tree in ("class", "spec", "hero")
        for talent in talents_by_tree[tree]
        if talent.taken
    }
    notes = build_spec.source_notes[:] + [f"decoded via {repo.build_simc}"]
    return BuildResolution(
        actor_class=build_spec.actor_class,
        spec=build_spec.spec,
        enabled_talents=enabled_talents,
        talents_by_tree=talents_by_tree,
        source_kind=build_spec.source_kind,
        generated_profile_text=profile_text,
        source_notes=notes,
        hero_tree=hero_tree,
        inactive_hero_talents=inactive_hero,
    )


def _split_inactive_hero_talents(
    repo: RepoPaths,
    talents_by_tree: dict[str, list[DecodedTalent]],
    hero_trees: list[HeroTree],
) -> list[DecodedTalent]:
    """Move hero talents belonging to an unselected hero tree out of ``talents_by_tree``.

    A talent hash grants the keystones of every hero tree it touches; SimC then disables the ones
    outside the selected tree. Without this the decoded build claims talents the sim will never use,
    which flips APL branches that dispatch on a hero keystone.
    """
    hero_talents = talents_by_tree["hero"]
    if not hero_trees or not hero_talents:
        return []
    active_ids = {tree.id for tree in hero_trees}
    sub_trees = load_trait_table(repo.root).hero_sub_tree_by_entry
    active: list[DecodedTalent] = []
    inactive: list[DecodedTalent] = []
    for talent in hero_talents:
        sub_tree = sub_trees.get(talent.entry)
        (inactive if sub_tree is not None and sub_tree not in active_ids else active).append(talent)
    talents_by_tree["hero"] = active
    return inactive


def tree_entries_string(talents: list[DecodedTalent]) -> str:
    """Serialize decoded talents into SimC ``entry:rank/...`` format."""
    parts = []
    for talent in talents:
        if talent.rank > 0 and talent.entry:
            parts.append(f"{talent.entry}:{talent.rank}")
    return "/".join(parts)


@dataclass(slots=True)
class TreeDiff:
    added: list[DecodedTalent]
    removed: list[DecodedTalent]
    changed: list[tuple[DecodedTalent, DecodedTalent]]


def diff_talent_trees(
    base_talents: list[DecodedTalent],
    other_talents: list[DecodedTalent],
) -> TreeDiff:
    """Diff two talent lists from the same tree by entry ID.

    A talent whose rank could not be read (a tiered node from a hash) still counts as taken, so
    losing it shows up as a removal instead of vanishing from the diff.
    """
    base_by_entry = {t.entry: t for t in base_talents if t.taken and t.entry}
    other_by_entry = {t.entry: t for t in other_talents if t.taken and t.entry}
    added = [other_by_entry[e] for e in sorted(other_by_entry.keys() - base_by_entry.keys())]
    removed = [base_by_entry[e] for e in sorted(base_by_entry.keys() - other_by_entry.keys())]
    changed = []
    for entry in sorted(base_by_entry.keys() & other_by_entry.keys()):
        base_rank = base_by_entry[entry].rank if base_by_entry[entry].rank_known else None
        other_rank = other_by_entry[entry].rank if other_by_entry[entry].rank_known else None
        if base_rank != other_rank:
            changed.append((base_by_entry[entry], other_by_entry[entry]))
    return TreeDiff(added=added, removed=removed, changed=changed)


def encode_build(repo: RepoPaths, build_spec: BuildSpec) -> str:
    """Run SimC to encode a BuildSpec and return the combined ``talents=`` export string."""
    if not build_spec.actor_class or not build_spec.spec:
        raise ValueError("Need both actor class and spec to encode talents.")
    if not repo.build_simc.exists():
        raise FileNotFoundError(f"SimC binary not found: {repo.build_simc}")

    # SimC drops a gearless actor before it reaches the profile-generation step, so the save file
    # would never be written. Default gear keeps the player active; talents are unaffected.
    run = _run_simc(repo, build_profile_text(build_spec) + "load_default_gear=1\n", extra_args=(), save=True)

    if run.saved_profile is None:
        errors = simc_build_errors(run.output)
        raise SimcBuildError(
            " ".join(errors) or "SimC did not produce a saved profile.",
            output_preview=bounded_output_preview(run.output),
            returncode=run.returncode,
        )

    for line in run.saved_profile.splitlines():
        if line.startswith("talents="):
            return line.split("=", 1)[1].strip()

    raise SimcBuildError(
        "Saved SimC profile did not contain a talents= line.",
        output_preview=bounded_output_preview(run.output),
        returncode=run.returncode,
    )

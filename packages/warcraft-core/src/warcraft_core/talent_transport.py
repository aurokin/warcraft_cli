from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any, TypeGuard

from warcraft_core.identity import normalize_actor_class, normalize_spec_name

ACTOR_LINE_RE = re.compile(r'^([a-z_]+)\s*=\s*"?(.*?)"?$')
TALENT_DEBUG_RE = re.compile(
    r"adding (?P<tree>class|spec|hero|selection) talent (?P<name>.+?) "
    r"\(node=(?P<node>\d+) entry=(?P<entry>\d+) rank=(?P<rank>\d+)/(?P<max_rank>\d+)\)"
)

CLASS_ID_BY_ACTOR_CLASS = {
    "warrior": 1,
    "paladin": 2,
    "hunter": 3,
    "rogue": 4,
    "priest": 5,
    "deathknight": 6,
    "shaman": 7,
    "mage": 8,
    "warlock": 9,
    "monk": 10,
    "druid": 11,
    "demonhunter": 12,
    "evoker": 13,
}

TREE_NAME_BY_INDEX = {
    1: "class",
    2: "spec",
    3: "hero",
}

SPECIALIZATION_LINE_RE = re.compile(
    r"(?P<enum_name>[A-Z_]+)\s*=\s*(?P<spec_id>\d+)\s*,"
)
TRAIT_ROW_RE = re.compile(
    r'\{\s*'
    r'(?P<tree_index>\d+),\s*'
    r'(?P<class_id>\d+),\s*'
    r'(?P<entry_id>\d+),\s*'
    r'(?P<node_id>\d+),\s*'
    r'(?P<max_rank>\d+),\s*'
    r'\d+,\s*\d+,\s*\d+,\s*\d+,\s*\d+,\s*'
    r'-?\d+,\s*-?\d+,\s*'
    r'(?P<selection_index>\d+),\s*'
    r'"(?P<name>[^"]+)",\s*'
    r'\{\s*(?P<spec_ids>[^}]*)\},\s*'
    r'\{\s*[^}]*\},\s*'
    r'(?P<hero_tree_id>\d+),\s*'
    r'(?P<node_type>\d+)\s*'
    r'\},?'
)
HERO_TREE_ROW_RE = re.compile(r'\{\s*(?P<hero_tree_id>\d+),\s*"(?P<name>[^"]+)",\s*\d+\s*\},?')

CLASS_ENUM_NAME_BY_ACTOR_CLASS = {
    actor_class: actor_class.replace("deathknight", "death_knight").replace("demonhunter", "demon_hunter").upper()
    for actor_class in CLASS_ID_BY_ACTOR_CLASS
}


def _is_transport_int(value: Any) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass(frozen=True, slots=True)
class TraitRecord:
    tree: str
    class_id: int
    entry_id: int
    node_id: int
    max_rank: int
    name: str
    token: str
    spec_ids: tuple[int, ...]
    hero_tree_id: int
    hero_tree_name: str | None
    node_type: int
    selection_index: int


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


@dataclass(slots=True)
class DecodedTalent:
    tree: str
    name: str
    token: str
    rank: int
    max_rank: int
    entry: int = 0


@dataclass(slots=True)
class BuildResolution:
    actor_class: str
    spec: str
    enabled_talents: set[str]
    talents_by_tree: dict[str, list[DecodedTalent]]
    source_kind: str | None
    generated_profile_text: str | None
    source_notes: list[str]


@dataclass(frozen=True, slots=True)
class RoundTripResult:
    wow_talent_export: str
    # {"class": {entry: rank}, "spec": {...}, "hero": {...}} as decoded from the export.
    entries_by_tree: dict[str, dict[int, int]]


class RoundTripError(RuntimeError):
    """The executor could not encode/decode the build (missing binary, SimC failure, bad spec)."""


RoundTripExecutor = Callable[[BuildSpec], RoundTripResult]


@dataclass(frozen=True, slots=True)
class TalentTransportBackend:
    """What SimC-backed validation needs: where generated trait .inc files live and how to round-trip a build.

    Core never runs SimulationCraft itself; simc_cli supplies the executor.
    """

    trait_data_root: Path
    round_trip: RoundTripExecutor


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


def tokenize_talent_name(name: str) -> str:
    text = name.lower().replace("'", "")
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return text.strip("_")


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
        talents_by_tree[tree].append(
            DecodedTalent(
                tree=tree,
                name=name,
                token=tokenize_talent_name(name),
                rank=int(match.group("rank")),
                max_rank=int(match.group("max_rank")),
                entry=int(match.group("entry")),
            )
        )
    return talents_by_tree


def _generated_file(repo_root: Path, relative: str) -> Path:
    return repo_root / "engine" / "dbc" / "generated" / relative


def _parse_int_list(value: str) -> tuple[int, ...]:
    items: list[int] = []
    for raw in value.split(","):
        text = raw.strip()
        if not text:
            continue
        items.append(int(text))
    return tuple(items)


def _specialization_identity(enum_name: str) -> tuple[str, str] | None:
    for actor_class, class_enum_name in sorted(CLASS_ENUM_NAME_BY_ACTOR_CLASS.items(), key=lambda item: len(item[1]), reverse=True):
        prefix = f"{class_enum_name}_"
        if not enum_name.startswith(prefix):
            continue
        spec_name = enum_name[len(prefix):]
        if not spec_name:
            return None
        normalized_spec = normalize_spec_name(spec_name.replace("_", " "))
        if normalized_spec:
            return actor_class, normalized_spec
        return None
    return None


@cache
def _specialization_ids(repo_root_text: str) -> dict[tuple[str, str], int]:
    repo_root = Path(repo_root_text)
    path = _generated_file(repo_root, "sc_specialization_data.inc")
    if not path.exists():
        return {}
    ids: dict[tuple[str, str], int] = {}
    for match in SPECIALIZATION_LINE_RE.finditer(path.read_text()):
        enum_name = match.group("enum_name").strip()
        identity = _specialization_identity(enum_name)
        if identity is None:
            continue
        actor_class, spec = identity
        ids[(actor_class, spec)] = int(match.group("spec_id"))
    return ids


@cache
def _hero_tree_names(repo_root_text: str) -> dict[int, str]:
    repo_root = Path(repo_root_text)
    path = _generated_file(repo_root, "trait_data.inc")
    if not path.exists():
        return {}
    names: dict[int, str] = {}
    for match in HERO_TREE_ROW_RE.finditer(path.read_text()):
        names[int(match.group("hero_tree_id"))] = match.group("name").strip()
    return names


@cache
def _trait_records(repo_root_text: str) -> dict[tuple[int, int, int], list[TraitRecord]]:
    repo_root = Path(repo_root_text)
    path = _generated_file(repo_root, "trait_data.inc")
    if not path.exists():
        return {}
    hero_tree_names = _hero_tree_names(repo_root_text)
    records: dict[tuple[int, int, int], list[TraitRecord]] = {}
    for match in TRAIT_ROW_RE.finditer(path.read_text()):
        tree_index = int(match.group("tree_index"))
        tree = TREE_NAME_BY_INDEX.get(tree_index)
        if tree is None:
            continue
        entry_id = int(match.group("entry_id"))
        node_id = int(match.group("node_id"))
        class_id = int(match.group("class_id"))
        hero_tree_id = int(match.group("hero_tree_id"))
        record = TraitRecord(
            tree=tree,
            class_id=class_id,
            entry_id=entry_id,
            node_id=node_id,
            max_rank=int(match.group("max_rank")),
            name=match.group("name").strip(),
            token=tokenize_talent_name(match.group("name").strip()),
            spec_ids=_parse_int_list(match.group("spec_ids")),
            hero_tree_id=hero_tree_id,
            hero_tree_name=hero_tree_names.get(hero_tree_id),
            node_type=int(match.group("node_type")),
            selection_index=int(match.group("selection_index")),
        )
        records.setdefault((entry_id, node_id, class_id), []).append(record)
    return records


def decoded_entries_by_tree(resolution: BuildResolution) -> dict[str, dict[int, int]]:
    rows: dict[str, dict[int, int]] = {"class": {}, "spec": {}, "hero": {}}
    for tree in ("class", "spec", "hero"):
        for talent in resolution.talents_by_tree.get(tree, []):
            if talent.entry and talent.rank > 0:
                rows[tree][talent.entry] = talent.rank
    return rows


def _split_transport_forms(resolved_rows: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, list[str]] = {"class": [], "spec": [], "hero": []}
    for row in resolved_rows:
        tree = row.get("tree")
        entry = row.get("entry")
        rank = row.get("rank")
        if tree in grouped and isinstance(entry, int) and isinstance(rank, int) and rank > 0:
            grouped[tree].append(f"{entry}:{rank}")
    if not any(grouped.values()):
        return {}
    return {
        "simc_split_talents": {
            "class_talents": "/".join(grouped["class"]) or None,
            "spec_talents": "/".join(grouped["spec"]) or None,
            "hero_talents": "/".join(grouped["hero"]) or None,
        }
    }


def _build_spec_from_transport(
    *,
    actor_class: str,
    spec: str,
    transport_forms: dict[str, Any],
) -> BuildSpec:
    raw_split = transport_forms.get("simc_split_talents")
    split: dict[str, Any] = raw_split if isinstance(raw_split, dict) else {}
    return BuildSpec(
        actor_class=actor_class,
        spec=spec,
        class_talents=split.get("class_talents"),
        spec_talents=split.get("spec_talents"),
        hero_talents=split.get("hero_talents"),
        source_kind="simc_split_talents",
    )


def _resolve_transport_rows(
    talent_tree_rows: list[dict[str, Any]],
    records: dict[tuple[int, int, int], list[TraitRecord]],
    *,
    class_id: int,
    spec_id: int | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Match each raw talent row to exactly one generated trait record; return (resolved, unresolved)."""
    resolved_rows: list[dict[str, Any]] = []
    unresolved_rows: list[dict[str, Any]] = []
    for row in talent_tree_rows:
        entry = row.get("entry")
        node_id = row.get("node_id")
        rank = row.get("rank")
        if not _is_transport_int(entry) or not _is_transport_int(node_id) or not _is_transport_int(rank):
            unresolved_rows.append({"row": row, "reason": "missing_entry_node_or_rank"})
            continue
        entry_id = int(entry)
        node_id_value = int(node_id)
        rank_value = int(rank)
        candidates = records.get((entry_id, node_id_value, class_id), [])
        if spec_id:
            candidates = [candidate for candidate in candidates if not any(candidate.spec_ids) or spec_id in candidate.spec_ids]
        if len(candidates) != 1:
            unresolved_rows.append(
                {
                    "entry": entry_id,
                    "node_id": node_id_value,
                    "rank": rank_value,
                    "candidate_count": len(candidates),
                    "reason": "ambiguous_trait_resolution" if candidates else "trait_not_found",
                }
            )
            continue
        record = candidates[0]
        resolved_rows.append(
            {
                "entry": entry_id,
                "node_id": node_id_value,
                "rank": rank_value,
                "tree": record.tree,
                "name": record.name,
                "token": record.token,
                "max_rank": record.max_rank,
                "hero_tree_id": record.hero_tree_id or None,
                "hero_tree": record.hero_tree_name,
                "node_type": record.node_type,
                "selection_index": record.selection_index,
            }
        )
    return resolved_rows, unresolved_rows


def _expected_entries_by_tree(resolved_rows: list[dict[str, Any]]) -> dict[str, dict[int, int]]:
    return {
        tree: {
            row["entry"]: row["rank"]
            for row in resolved_rows
            if row["tree"] == tree and isinstance(row.get("entry"), int) and isinstance(row.get("rank"), int) and row["rank"] > 0
        }
        for tree in ("class", "spec", "hero")
    }


def _not_validated(reason: str, **details: Any) -> dict[str, Any]:
    """Shape the "no usable transport form" result; ``details`` become extra ``validation`` keys."""
    return {
        "transport_forms": {},
        "validation": {"status": "not_validated", "reason": reason, **details},
    }


def _round_trip_validation(
    backend: TalentTransportBackend,
    *,
    actor_class: str,
    spec: str,
    resolved_rows: list[dict[str, Any]],
    transport_forms: dict[str, Any],
) -> dict[str, Any]:
    """Re-encode the resolved rows through SimC and confirm the decoded entries come back unchanged."""
    build_spec = _build_spec_from_transport(
        actor_class=actor_class,
        spec=spec,
        transport_forms=transport_forms,
    )
    try:
        round_trip = backend.round_trip(build_spec)
    except RoundTripError as exc:
        return _not_validated("simc_round_trip_failed", message=str(exc), resolved_entries=resolved_rows)

    expected_by_tree = _expected_entries_by_tree(resolved_rows)
    actual_by_tree = round_trip.entries_by_tree
    if actual_by_tree != expected_by_tree:
        return _not_validated(
            "simc_round_trip_mismatch",
            resolved_entries=resolved_rows,
            expected_entries_by_tree=expected_by_tree,
            actual_entries_by_tree=actual_by_tree,
        )

    return {
        "transport_forms": transport_forms,
        "validation": {
            "status": "validated",
            "source": "simc_trait_data_round_trip",
            "actor_class": actor_class,
            "spec": spec,
            "resolved_entries": resolved_rows,
            "round_trip": {
                "wow_talent_export": round_trip.wow_talent_export,
                "matched": True,
            },
        },
    }


def validate_talent_tree_transport(
    *,
    actor_class: str | None,
    spec: str | None,
    talent_tree_rows: list[dict[str, Any]],
    backend: TalentTransportBackend | None,
) -> dict[str, Any]:
    normalized_actor_class = normalize_actor_class(actor_class)
    normalized_spec = normalize_spec_name(spec)
    if not normalized_actor_class or not normalized_spec:
        return _not_validated("missing_class_spec_identity")

    class_id = CLASS_ID_BY_ACTOR_CLASS.get(normalized_actor_class)
    if class_id is None:
        return _not_validated("unsupported_actor_class", actor_class=normalized_actor_class)

    if backend is None:
        return _not_validated("simc_backend_unavailable")

    repo_root_text = str(backend.trait_data_root.expanduser().resolve())
    spec_id = _specialization_ids(repo_root_text).get((normalized_actor_class, normalized_spec))
    if spec_id is None:
        return _not_validated("unsupported_class_spec", actor_class=normalized_actor_class, spec=normalized_spec)

    records = _trait_records(repo_root_text)
    if not records:
        return _not_validated("simc_trait_data_unavailable", repo_root=repo_root_text)

    resolved_rows, unresolved_rows = _resolve_transport_rows(talent_tree_rows, records, class_id=class_id, spec_id=spec_id)
    if unresolved_rows:
        return _not_validated(
            "simc_trait_resolution_incomplete",
            resolved_entries=resolved_rows,
            unresolved_entries=unresolved_rows,
        )

    transport_forms = _split_transport_forms(resolved_rows)
    if not transport_forms:
        return _not_validated("no_ranked_talent_entries", resolved_entries=resolved_rows)

    return _round_trip_validation(
        backend,
        actor_class=normalized_actor_class,
        spec=normalized_spec,
        resolved_rows=resolved_rows,
        transport_forms=transport_forms,
    )


def _decoded_talent(*, tree: str, entry: int, rank: int, name: str) -> DecodedTalent:
    return DecodedTalent(
        tree=tree,
        entry=entry,
        rank=rank,
        max_rank=rank,
        name=name,
        token=tokenize_talent_name(name),
    )

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

from warcraft_core.identity import normalize_actor_class, normalize_spec_name

from simc_cli.build_input import BuildResolution, BuildSpec, DecodedTalent, decode_build, encode_build, tokenize_talent_name
from simc_cli.repo import discover_repo

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


def _is_transport_int(value: Any) -> bool:
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


def _decoded_entries_by_tree(resolution: BuildResolution) -> dict[str, dict[int, int]]:
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
    split = transport_forms.get("simc_split_talents") if isinstance(transport_forms.get("simc_split_talents"), dict) else {}
    return BuildSpec(
        actor_class=actor_class,
        spec=spec,
        class_talents=split.get("class_talents"),
        spec_talents=split.get("spec_talents"),
        hero_talents=split.get("hero_talents"),
        source_kind="simc_split_talents",
    )


def validate_talent_tree_transport(
    *,
    actor_class: str | None,
    spec: str | None,
    talent_tree_rows: list[dict[str, Any]],
    repo_root: str | Path | None = None,
) -> dict[str, Any]:
    normalized_actor_class = normalize_actor_class(actor_class)
    normalized_spec = normalize_spec_name(spec)
    if not normalized_actor_class or not normalized_spec:
        return {
            "transport_forms": {},
            "validation": {
                "status": "not_validated",
                "reason": "missing_class_spec_identity",
            },
        }

    class_id = CLASS_ID_BY_ACTOR_CLASS.get(normalized_actor_class)
    if class_id is None:
        return {
            "transport_forms": {},
            "validation": {
                "status": "not_validated",
                "reason": "unsupported_actor_class",
                "actor_class": normalized_actor_class,
            },
        }

    repo = discover_repo(repo_root)
    repo_root_text = str(repo.root.resolve())
    spec_id = _specialization_ids(repo_root_text).get((normalized_actor_class, normalized_spec))
    if spec_id is None:
        return {
            "transport_forms": {},
            "validation": {
                "status": "not_validated",
                "reason": "unsupported_class_spec",
                "actor_class": normalized_actor_class,
                "spec": normalized_spec,
            },
        }

    records = _trait_records(repo_root_text)
    if not records:
        return {
            "transport_forms": {},
            "validation": {
                "status": "not_validated",
                "reason": "simc_trait_data_unavailable",
                "repo_root": repo_root_text,
            },
        }

    resolved_rows: list[dict[str, Any]] = []
    unresolved_rows: list[dict[str, Any]] = []
    for row in talent_tree_rows:
        entry = row.get("entry")
        node_id = row.get("node_id")
        rank = row.get("rank")
        if not _is_transport_int(entry) or not _is_transport_int(node_id) or not _is_transport_int(rank):
            unresolved_rows.append({"row": row, "reason": "missing_entry_node_or_rank"})
            continue
        candidates = records.get((entry, node_id, class_id), [])
        if spec_id:
            spec_candidates = [
                candidate for candidate in candidates if not any(candidate.spec_ids) or spec_id in candidate.spec_ids
            ]
            candidates = spec_candidates
        if len(candidates) != 1:
            unresolved_rows.append(
                {
                    "entry": entry,
                    "node_id": node_id,
                    "rank": rank,
                    "candidate_count": len(candidates),
                    "reason": "ambiguous_trait_resolution" if candidates else "trait_not_found",
                }
            )
            continue
        record = candidates[0]
        resolved_rows.append(
            {
                "entry": entry,
                "node_id": node_id,
                "rank": rank,
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

    if unresolved_rows:
        return {
            "transport_forms": {},
            "validation": {
                "status": "not_validated",
                "reason": "simc_trait_resolution_incomplete",
                "resolved_entries": resolved_rows,
                "unresolved_entries": unresolved_rows,
            },
        }

    transport_forms = _split_transport_forms(resolved_rows)
    if not transport_forms:
        return {
            "transport_forms": {},
            "validation": {
                "status": "not_validated",
                "reason": "no_ranked_talent_entries",
                "resolved_entries": resolved_rows,
            },
        }
    build_spec = _build_spec_from_transport(
        actor_class=normalized_actor_class,
        spec=normalized_spec,
        transport_forms=transport_forms,
    )
    try:
        encoded_export = encode_build(repo, build_spec)
        round_trip_resolution = decode_build(
            repo,
            BuildSpec(actor_class=normalized_actor_class, spec=normalized_spec, talents=encoded_export),
        )
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        return {
            "transport_forms": {},
            "validation": {
                "status": "not_validated",
                "reason": "simc_round_trip_failed",
                "message": str(exc),
                "resolved_entries": resolved_rows,
            },
        }

    expected_by_tree = {
        tree: {
            row["entry"]: row["rank"]
            for row in resolved_rows
            if row["tree"] == tree and isinstance(row.get("entry"), int) and isinstance(row.get("rank"), int) and row["rank"] > 0
        }
        for tree in ("class", "spec", "hero")
    }
    actual_by_tree = _decoded_entries_by_tree(round_trip_resolution)
    if actual_by_tree != expected_by_tree:
        return {
            "transport_forms": {},
            "validation": {
                "status": "not_validated",
                "reason": "simc_round_trip_mismatch",
                "resolved_entries": resolved_rows,
                "expected_entries_by_tree": expected_by_tree,
                "actual_entries_by_tree": actual_by_tree,
            },
        }

    return {
        "transport_forms": transport_forms,
        "validation": {
            "status": "validated",
            "source": "simc_trait_data_round_trip",
            "actor_class": normalized_actor_class,
            "spec": normalized_spec,
            "resolved_entries": resolved_rows,
            "round_trip": {
                "wow_talent_export": encoded_export,
                "matched": True,
            },
        },
    }


def _decoded_talent(*, tree: str, entry: int, rank: int, name: str) -> DecodedTalent:
    return DecodedTalent(
        tree=tree,
        entry=entry,
        rank=rank,
        max_rank=rank,
        name=name,
        token=tokenize_talent_name(name),
    )

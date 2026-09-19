"""Per-entry talent-tree facts that SimC's ``debug=1`` output leaves out.

Two things the talent code needs are absent from the debug lines:

* which hero tree a hero talent belongs to. A talent hash grants the keystones of *both* hero trees
  and SimC then disables the unselected one, so a decode that keeps them all claims talents the sim
  will never use.
* which tree an arbitrary talent entry lives in, which ``modify-build`` needs to put an edit into the
  matching ``class_talents``/``spec_talents``/``hero_talents`` string.

``engine/dbc/generated/trait_data.inc`` in the checkout carries both. ``warcraft_core`` owns that
file's row format; this module only indexes the rows the way the decoder and the editor read them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from warcraft_core.talent_transport import TRAIT_ROW_RE, TREE_NAME_BY_INDEX, tokenize_talent_name

_CACHE: dict[tuple[str, int, int], TraitTable] = {}


@dataclass(frozen=True, slots=True)
class TraitTable:
    """Per-entry tree membership for one SimulationCraft checkout."""

    tree_by_entry: dict[int, str] = field(default_factory=dict)
    class_id_by_entry: dict[int, int] = field(default_factory=dict)
    hero_sub_tree_by_entry: dict[int, int] = field(default_factory=dict)
    # (class id, tokenized talent name) -> the trees that name appears in for that class.
    trees_by_name: dict[tuple[int, str], set[str]] = field(default_factory=dict)

    def tree_for_entry(self, entry: int) -> str | None:
        return self.tree_by_entry.get(entry)

    def tree_for_name(self, class_id: int, name: str) -> str | None:
        """The tree a talent name belongs to, or None when it is unknown or spans several trees."""
        trees = self.trees_by_name.get((class_id, tokenize_talent_name(name)))
        return next(iter(trees)) if trees and len(trees) == 1 else None


def trait_data_path(repo_root: Path) -> Path:
    return repo_root / "engine" / "dbc" / "generated" / "trait_data.inc"


def parse_trait_table(text: str) -> TraitTable:
    table = TraitTable()
    for match in TRAIT_ROW_RE.finditer(text):
        tree = TREE_NAME_BY_INDEX.get(int(match.group("tree_index")))
        if tree is None:
            continue
        entry = int(match.group("entry_id"))
        class_id = int(match.group("class_id"))
        table.tree_by_entry[entry] = tree
        table.class_id_by_entry[entry] = class_id
        if tree == "hero":
            table.hero_sub_tree_by_entry[entry] = int(match.group("hero_tree_id"))
        key = (class_id, tokenize_talent_name(match.group("name")))
        table.trees_by_name.setdefault(key, set()).add(tree)
    return table


def load_trait_table(repo_root: Path) -> TraitTable:
    """Read (and memoize per file revision) the checkout's trait table."""
    path = trait_data_path(repo_root)
    try:
        stat = path.stat()
    except OSError as exc:
        raise FileNotFoundError(
            f"SimulationCraft trait data not found: {path}. "
            "Talents cannot be attributed to a tree without it."
        ) from exc
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    cached = _CACHE.get(key)
    if cached is None:
        cached = parse_trait_table(path.read_text())
        _CACHE[key] = cached
    return cached

"""Per-entry talent-tree facts that SimC's ``debug=1`` output leaves out.

Two things the talent code needs are absent from the debug lines:

* which hero tree a hero talent belongs to. A talent hash grants the keystones of *both* hero trees
  and SimC then disables the unselected one, so a decode that keeps them all claims talents the sim
  will never use.
* which tree an arbitrary talent entry lives in, which ``modify-build`` needs to put an edit into the
  matching ``class_talents``/``spec_talents``/``hero_talents`` string.
* which entries share a tiered node. SimC spreads a tiered node's ranks over its entries and prints
  only one debug line for the node, so the decoder has to know the siblings to read the ranks back.

``engine/dbc/generated/trait_data.inc`` in the checkout carries both. ``warcraft_core`` owns that
file's row format; this module only indexes the rows the way the decoder and the editor read them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from warcraft_core.talent_transport import (
    CLASS_ID_BY_ACTOR_CLASS,
    TRAIT_ROW_RE,
    TREE_NAME_BY_INDEX,
    tokenize_talent_name,
)

_CACHE: dict[tuple[str, int, int], TraitTable] = {}

# trait_data.inc's node_type column: SimC's NODE_TIERED and NODE_CHOICE.
NODE_TIERED = 1
NODE_CHOICE = 2


@dataclass(frozen=True, slots=True)
class TieredEntry:
    """One entry of a tiered node, in the order SimC fills them."""

    entry: int
    max_rank: int


@dataclass(frozen=True, slots=True)
class TraitTable:
    """Per-entry tree membership for one SimulationCraft checkout."""

    tree_by_entry: dict[int, str] = field(default_factory=dict)
    class_id_by_entry: dict[int, int] = field(default_factory=dict)
    hero_sub_tree_by_entry: dict[int, int] = field(default_factory=dict)
    # (class id, tokenized talent name) -> every entry of that class carrying the name.
    entries_by_name: dict[tuple[int, str], list[int]] = field(default_factory=dict)
    # The specs that can take an entry. Absent means every spec of its class; spec-tree entries are
    # always restricted, and so are some class-tree entries (Chi Burst is Brewmaster's). A hero entry
    # takes the specs of its hero tree's selection rows, not its own spec tags, as SimC's
    # trait_data_t::is_hero_trait_available does: Augmentation's Chronowarden entries are tagged only
    # for Preservation, yet Augmentation's selection rows offer Chronowarden and Scalecommander.
    spec_ids_by_entry: dict[int, frozenset[int]] = field(default_factory=dict)
    # Every entry of a tiered node, keyed by each of those entries.
    tiered_siblings_by_entry: dict[int, tuple[TieredEntry, ...]] = field(default_factory=dict)
    # The node of every entry on a choice node, where a build can take only one of the entries.
    choice_node_by_entry: dict[int, int] = field(default_factory=dict)

    def _available(self, entry: int, class_id: int, spec_id: int) -> bool:
        specs = self.spec_ids_by_entry.get(entry)
        return self.class_id_by_entry.get(entry) == class_id and (specs is None or spec_id in specs)

    def tree_for_entry(self, entry: int, *, class_id: int, spec_id: int) -> str | None:
        """The tree an entry id belongs to, or None when the spec cannot take it."""
        return self.tree_by_entry.get(entry) if self._available(entry, class_id, spec_id) else None

    def entries_for_name(self, name: str, *, class_id: int, spec_id: int) -> list[int]:
        """Every entry carrying a talent name that this spec can take."""
        entries = self.entries_by_name.get((class_id, tokenize_talent_name(name)), [])
        return [entry for entry in entries if self._available(entry, class_id, spec_id)]

    def choice_nodes(self, entries: list[int]) -> frozenset[int]:
        """The choice nodes among the nodes these entries sit on."""
        return frozenset(self.choice_node_by_entry[entry] for entry in entries if entry in self.choice_node_by_entry)

    def tree_for_name(self, name: str, *, class_id: int, spec_id: int) -> str | None:
        """The tree a talent name belongs to for this spec, or None when it has none or spans several trees."""
        trees = {self.tree_by_entry[entry] for entry in self.entries_for_name(name, class_id=class_id, spec_id=spec_id)}
        return next(iter(trees)) if len(trees) == 1 else None


class SimcNotReadyError(FileNotFoundError):
    """The checkout cannot serve a build command: its binary is missing, cannot run or crashed, or SimC's generated data is missing."""


def trait_data_path(repo_root: Path) -> Path:
    return repo_root / "engine" / "dbc" / "generated" / "trait_data.inc"


def parse_trait_table(text: str) -> TraitTable:
    table = TraitTable()
    tiered_by_node: dict[int, list[TieredEntry]] = {}
    specs_by_hero_tree: dict[tuple[int, int], set[int]] = {}
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
        table.entries_by_name.setdefault((class_id, tokenize_talent_name(match.group("name"))), []).append(entry)
        spec_ids = frozenset(int(value) for value in match.group("spec_ids").split(",") if int(value))
        if tree == "selection":
            specs_by_hero_tree.setdefault((class_id, int(match.group("hero_tree_id"))), set()).update(spec_ids)
        elif spec_ids and tree != "hero":
            table.spec_ids_by_entry[entry] = spec_ids
        if int(match.group("node_type")) == NODE_CHOICE:
            table.choice_node_by_entry[entry] = int(match.group("node_id"))
        if int(match.group("node_type")) == NODE_TIERED:
            siblings = tiered_by_node.setdefault(int(match.group("node_id")), [])
            siblings.append(TieredEntry(entry=entry, max_rank=int(match.group("max_rank"))))
    for entry, sub_tree in table.hero_sub_tree_by_entry.items():
        specs = specs_by_hero_tree.get((table.class_id_by_entry[entry], sub_tree), set())
        table.spec_ids_by_entry[entry] = frozenset(specs)
    for siblings in tiered_by_node.values():
        frozen = tuple(siblings)
        for sibling in siblings:
            table.tiered_siblings_by_entry[sibling.entry] = frozen
    return table


def load_trait_table(repo_root: Path) -> TraitTable:
    """Read (and memoize per file revision) the checkout's trait table."""
    path = trait_data_path(repo_root)
    try:
        stat = path.stat()
    except OSError as exc:
        raise SimcNotReadyError(
            f"SimulationCraft trait data not found: {path}. "
            "Talents cannot be attributed to a tree without it."
        ) from exc
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    cached = _CACHE.get(key)
    if cached is None:
        cached = parse_trait_table(path.read_text())
        _CACHE[key] = cached
    return cached


class UnknownTalentError(ValueError):
    """Talent names that name no talent of the actor's class."""

    def __init__(self, values: list[str], message: str | None = None) -> None:
        super().__init__(
            message
            or f"Not a talent of this class: {', '.join(values)}. Pass the talent's display name or its SimC token."
        )
        self.values = values


def resolve_talent_tokens(repo_root: Path, actor_class: str | None, values: set[str]) -> set[str]:
    """Tokenize talent names and reject the ones the class has no talent for.

    SimC matches talents by token, so ``--disable "Spear Hand Strike"`` silently matched nothing
    until the display name was tokenized, and a misspelled value still does.
    """
    if not values:
        return set()
    class_id = CLASS_ID_BY_ACTOR_CLASS.get(actor_class or "")
    if class_id is None:
        names = ", ".join(sorted(values))
        raise UnknownTalentError(sorted(values), f"No actor class to check talents against: {names}. Pass --actor-class.")
    table = load_trait_table(repo_root)
    tokens = {value: tokenize_talent_name(value) for value in values}
    unknown = sorted(value for value, token in tokens.items() if (class_id, token) not in table.entries_by_name)
    if unknown:
        raise UnknownTalentError(unknown)
    return set(tokens.values())

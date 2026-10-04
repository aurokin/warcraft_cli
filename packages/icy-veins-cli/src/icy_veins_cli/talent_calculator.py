"""Convert an Icy Veins talent calculator build into the WoW loadout import string simc decodes.

A port of the calculator's own ``exportString()`` (``midnight-talent-calculator-*.js``). The
calculator's URL hash is ``spec-class-spec-apex-hero-pvp`` (no apex part on older hashes), each part
in base64 with ``:`` for ``/`` and bits read least significant first. The spec part is the spec id
on 12 bits. The class, spec and hero parts list the points spent, in the order they were spent: 6
bits for the node's index among that tree's node ids sorted ascending, plus 1 bit naming the entry
of a choice node. The hero part starts with 1 bit naming the hero tree (0 left, 1 right). The apex
part spends one apex rank per 6-bit entry, and the PvP part names up to three PvP talents.

The import string is the Blizzard loadout format: version 2 on 8 bits, the spec id on 16, a zero
128-bit tree hash, then one record per node id of the whole class tree (every spec's nodes, both hero
trees, the hero selection node and the apex nodes), sorted ascending. PvP talents have no place in
it. The node data comes from the calculator's per-class tree JSON (``TREE_DATA_URL``).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from warcraft_content.guide_page import talent_export_reference
from warcraft_core.identity import build_identity_payload
from warcraft_core.wow_specs import WOW_SPECS, raiderio_class_slug

HASH_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+:"
EXPORT_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
TREE_DATA_URL = "https://static.icy-veins.com/json/midnight-talent-calculator/{class_slug}.json"
CALCULATOR_REFERENCE_TYPE = "icy_veins_talent_calc_url"
CONVERSION_SOURCE = "guide_talent_calculator_conversion"
EXPORT_VERSION = 2
# The calculator writes an apex node with fewer than this many ranks as partially ranked.
APEX_FULL_RANKS = 4

TreeLoader = Callable[[str], Mapping[str, Any]]


class ConversionError(ValueError):
    """The hash cannot be turned into an import string that loads the build the calculator shows."""


class _BitReader:
    def __init__(self, text: str, alphabet: str = HASH_ALPHABET) -> None:
        if any(char not in alphabet for char in text):
            raise ConversionError(f"{text!r} has characters outside its base64 alphabet")
        self._bits = [alphabet.index(char) >> bit & 1 for char in text for bit in range(6)]
        self._index = 0

    def read(self, width: int) -> int | None:
        """The next ``width`` bits, or ``None`` when fewer remain (the padding at the end of a part)."""
        if self._index + width > len(self._bits):
            return None
        value = sum(bit << offset for offset, bit in enumerate(self._bits[self._index : self._index + width]))
        self._index += width
        return value


class _BitWriter:
    def __init__(self) -> None:
        self._bits: list[int] = []

    def write(self, value: int, width: int) -> None:
        self._bits.extend(value >> offset & 1 for offset in range(width))

    def text(self) -> str:
        bits = self._bits + [0] * (-len(self._bits) % 6)
        return "".join(EXPORT_ALPHABET[sum(bit << offset for offset, bit in enumerate(bits[i : i + 6]))] for i in range(0, len(bits), 6))


def hash_spec_id(code: str) -> int:
    """The spec id a calculator hash starts with: its first two characters, 12 bits."""
    try:
        return _BitReader(code[:2]).read(12) or 0
    except ConversionError:
        return 0


def calculator_build_identity(code: str, *, source: str, notes: tuple[str, ...]) -> dict[str, object]:
    """The build identity of a calculator build: the spec id its hash starts with names the class and spec."""
    spec_id = hash_spec_id(code)
    spec = next((spec for spec in WOW_SPECS if spec.spec_id == spec_id), None)
    return build_identity_payload(
        actor_class=spec.class_key if spec else None,
        spec=spec.key if spec else None,
        confidence="high" if spec else "none",
        source=source,
        source_notes=notes,
    )


def tree_data_url(spec_id: int) -> str | None:
    """The calculator's tree data for the spec's class, or ``None`` for a spec id no class has."""
    spec = next((spec for spec in WOW_SPECS if spec.spec_id == spec_id), None)
    return TREE_DATA_URL.format(class_slug=raiderio_class_slug(spec.class_key).replace("-", "_")) if spec else None


@dataclass(slots=True)
class _Spent:
    """The points the hash spends, per node id, and the entry each choice node took."""

    points: dict[int, int] = field(default_factory=dict)
    choices: dict[int, int] = field(default_factory=dict)


def _nodes_by_id(nodes: Mapping[str, Any]) -> dict[int, dict[str, Any]]:
    return {int(node_id): node for node_id, node in nodes.items()}


def _read_tree(reader: _BitReader, nodes: dict[int, dict[str, Any]], spent: _Spent, *, tree: str) -> None:
    node_ids = sorted(nodes)
    while (index := reader.read(6)) is not None:
        if index >= len(node_ids):
            raise ConversionError(f"the {tree} tree part names node index {index}, past the {len(node_ids)} nodes of that tree")
        node_id = node_ids[index]
        if nodes[node_id]["type"] == "choice":
            choice = reader.read(1)
            if choice is None:
                raise ConversionError(f"the {tree} tree part ends inside the choice of node {node_id}")
            spent.choices[node_id] = choice
        spent.points[node_id] = spent.points.get(node_id, 0) + 1


def _max_ranks(node: Mapping[str, Any]) -> int:
    return 1 if node["type"] == "choice" else int(node["spells"][0]["maxRanks"])


def _ranks(node: Mapping[str, Any], spent: _Spent) -> int:
    """The node's ranks once the hash is read: a granted node starts with one."""
    return spent.points.get(int(node["id"]), 0) + (1 if node.get("alreadyMaxedOut") else 0)


def _name(node: Mapping[str, Any]) -> str:
    return " / ".join(str(spell.get("name")) for spell in node["spells"]) or str(node["id"])


def _check_tree(nodes: dict[int, dict[str, Any]], spent: _Spent, *, tree: str, extra_points: int = 0) -> None:
    """Refuse a build the game would not load: overranked nodes, or nodes bought without their prerequisites.

    The calculator only exports a partially ranked node it made active, which a node bought without
    its prerequisites never is, so a hash like that does not show the build it spells out.
    """
    tree_points = sum(spent.points.get(node_id, 0) for node_id in nodes) + extra_points
    for node_id, node in nodes.items():
        if not spent.points.get(node_id):
            continue
        if _ranks(node, spent) > _max_ranks(node):
            raise ConversionError(f"the {tree} tree spends {_ranks(node, spent)} ranks on {_name(node)}, which has {_max_ranks(node)}")
        previous = [nodes[int(previous_id)] for previous_id in node.get("previousNodeIds") or () if int(previous_id) in nodes]
        if previous and not any(_ranks(prior, spent) >= _max_ranks(prior) for prior in previous):
            raise ConversionError(f"the {tree} tree takes {_name(node)} without a fully ranked node leading to it")
        if tree_points < (required := int(node.get("spentAmountRequired") or 0)):
            raise ConversionError(f"the {tree} tree takes {_name(node)} before spending the {required} points it requires")


def _all_node_ids(tree_data: Mapping[str, Any]) -> list[int]:
    node_ids: set[int] = {int(node_id) for node_id in tree_data.get("unusedNodeIds") or ()}
    for spec in tree_data["specs"].values():
        hero = spec["hero"]
        for nodes in (spec["classNodes"], spec["specNodes"], hero["left"]["nodes"], hero["right"]["nodes"]):
            node_ids.update(int(node_id) for node_id in nodes)
        node_ids.update((int(hero["metaNodeId"]), int(spec["apexNode"]["id"])))
    return sorted(node_ids)


def _write_ranked(writer: _BitWriter, ranks: int, max_ranks: int) -> None:
    """A purchased node: fully ranked, or partially ranked with its rank count."""
    writer.write(1, 1)  # selected
    writer.write(1, 1)  # purchased
    if ranks < max_ranks:
        writer.write(1, 1)  # partially ranked
        writer.write(ranks, 6)
    else:
        writer.write(0, 1)
    writer.write(0, 1)  # not a choice


def _write_choice(writer: _BitWriter, entry: int) -> None:
    for bit in (1, 1, 0, 1):  # selected, purchased, fully ranked, a choice
        writer.write(bit, 1)
    writer.write(entry, 2)


def _write_node(writer: _BitWriter, node: Mapping[str, Any], spent: _Spent) -> None:
    node_id = int(node["id"])
    ranks = _ranks(node, spent)
    if node.get("alreadyMaxedOut") and not spent.points.get(node_id):
        writer.write(1, 1)  # selected
        writer.write(0, 1)  # granted, not purchased
    elif ranks == 0:
        writer.write(0, 1)
    elif node["type"] == "choice":
        _write_choice(writer, spent.choices[node_id])
    else:
        _write_ranked(writer, ranks, _max_ranks(node))


def _write_records(
    writer: _BitWriter,
    node_ids: Iterable[int],
    nodes: Mapping[int, Mapping[str, Any]],
    spent: _Spent,
    *,
    hero: tuple[int, int | None],
    apex: tuple[int, int],
) -> None:
    hero_meta_id, hero_choice = hero
    apex_id, apex_ranks = apex
    for node_id in node_ids:
        if (node := nodes.get(node_id)) is not None:
            _write_node(writer, node, spent)
        elif node_id == hero_meta_id and hero_choice is not None:
            _write_choice(writer, hero_choice)
        elif node_id == apex_id and apex_ranks:
            _write_ranked(writer, apex_ranks, APEX_FULL_RANKS)
        else:
            writer.write(0, 1)


def _hash_parts(code: str) -> tuple[str, str, str, str, str]:
    """The spec, class, spec-tree, apex and hero parts of a hash; a five-part hash predates the apex part."""
    parts = code.split("-")
    if len(parts) == 6:
        return parts[0], parts[1], parts[2], parts[3], parts[4]
    if len(parts) == 5:
        return parts[0], parts[1], parts[2], "", parts[3]
    raise ConversionError(f"the hash has {len(parts)} parts, not the 5 or 6 the calculator writes")


def _names_pvp_talents(code: str) -> bool:
    """Whether a hash's last part, its PvP part, picks any PvP talent ("A" is a zero sextet)."""
    parts = code.split("-")
    return len(parts) in (5, 6) and bool(parts[-1].strip("A"))


def _spec_data(tree_data: Mapping[str, Any], spec_id: int) -> Mapping[str, Any]:
    spec: Mapping[str, Any] | None = next((spec for spec in tree_data["specs"].values() if int(spec["id"]) == spec_id), None)
    if spec is None:
        raise ConversionError(f"the calculator's tree data has no spec {spec_id}")
    return spec


def _apex_ranks(code: str, apex: Mapping[str, Any]) -> int:
    """One rank per 6-bit entry, up to the ranks the apex node's spells hold, as the calculator reads it."""
    reader = _BitReader(code)
    entries = 0
    while reader.read(6) is not None:
        entries += 1
    return min(entries, sum(int(spell["maxRanks"]) for spell in apex["spells"]))


def _export_string(code: str, tree_data: Mapping[str, Any]) -> str:
    spec_part, class_part, spec_tree_part, apex_part, hero_part = _hash_parts(code)
    spec_id = _BitReader(spec_part).read(12) or 0
    spec = _spec_data(tree_data, spec_id)
    spent = _Spent()
    class_nodes = _nodes_by_id(spec["classNodes"])
    spec_nodes = _nodes_by_id(spec["specNodes"])
    _read_tree(_BitReader(class_part), class_nodes, spent, tree="class")
    _read_tree(_BitReader(spec_tree_part), spec_nodes, spent, tree="spec")
    apex = spec["apexNode"]
    apex_ranks = _apex_ranks(apex_part, apex)
    hero_choice: int | None = None
    hero_nodes: dict[int, dict[str, Any]] = {}
    if hero_part:
        hero_reader = _BitReader(hero_part)
        hero_choice = hero_reader.read(1) or 0
        hero_nodes = _nodes_by_id(spec["hero"]["right" if hero_choice else "left"]["nodes"])
        _read_tree(hero_reader, hero_nodes, spent, tree="hero")
    _check_tree(class_nodes, spent, tree="class")
    _check_tree(spec_nodes, spent, tree="spec", extra_points=apex_ranks)
    _check_tree(hero_nodes, spent, tree="hero")
    writer = _BitWriter()
    writer.write(EXPORT_VERSION, 8)
    writer.write(spec_id, 16)
    writer.write(0, 128)  # the tree hash, which the game does not check on import
    _write_records(
        writer,
        _all_node_ids(tree_data),
        {**class_nodes, **spec_nodes, **hero_nodes},
        spent,
        hero=(int(spec["hero"]["metaNodeId"]), hero_choice),
        apex=(int(apex["id"]), apex_ranks),
    )
    return writer.text()


def talent_export_string(code: str, tree_data: Mapping[str, Any]) -> str:
    """The WoW loadout import string for calculator hash ``code``, given the class's calculator tree data.

    It is the string the calculator's own export button gives for the build. Raises
    ``ConversionError`` naming the reason when the hash cannot be read against this tree data or
    spells out a build the game would refuse.
    """
    try:
        return _export_string(code, tree_data)
    except ConversionError:
        raise
    except (KeyError, IndexError, TypeError, ValueError, AttributeError) as exc:
        raise ConversionError(f"the calculator's tree data is not in the shape this converter reads ({type(exc).__name__}: {exc})") from exc


def _purchased_talents(export: str, node_ids: list[int]) -> tuple[int, dict[int, tuple[int | None, int | None]]] | None:
    """The spec id and the purchased nodes (ranks when partial, entry when a choice) of an import string.

    Granted nodes are left out: the game's own export marks the unchosen hero tree's root as
    granted where the calculator leaves it unselected, and neither changes the talents loaded.
    ``None`` when the string does not read as a loadout over these node ids.
    """
    try:
        reader = _BitReader(export, EXPORT_ALPHABET)
    except ConversionError:
        return None
    if reader.read(8) != EXPORT_VERSION or (spec_id := reader.read(16)) is None or reader.read(128) is None:
        return None
    purchased: dict[int, tuple[int | None, int | None]] = {}
    for node_id in node_ids:
        if not reader.read(1):
            continue
        if not reader.read(1):
            continue
        ranks = reader.read(6) if reader.read(1) else None
        entry = reader.read(2) if reader.read(1) else None
        purchased[node_id] = (ranks, entry)
    return spec_id, purchased


def same_talents(left: str, right: str, tree_data: Mapping[str, Any]) -> bool:
    """Whether two import strings buy the same talents over this class's tree data."""
    node_ids = _all_node_ids(tree_data)
    left_talents = _purchased_talents(left, node_ids)
    return left_talents is not None and left_talents == _purchased_talents(right, node_ids)


def _converted_reference(calculator_row: Mapping[str, Any], export: str, tree_url: str) -> dict[str, Any]:
    label = calculator_row.get("label")
    row = talent_export_reference(export, label=label, source_url=str(calculator_row["source_url"]), provider="icy-veins")
    row["build_identity"] = calculator_build_identity(
        str(calculator_row["build_code"]),
        source="icy_veins_talent_calc_hash",
        notes=("class/spec came from the spec id of the Icy Veins talent calculator build this import string was converted from",),
    )
    row["source"] = {
        "provider": "icy-veins",
        "source": CONVERSION_SOURCE,
        "converted_from": calculator_row["url"],
        "tree_data_url": tree_url,
        "note": "converted from the Icy Veins talent calculator build the way the calculator's export button does; "
        "a WoW import string carries no PvP talents",
    }
    return row


def _conversion(row: Mapping[str, Any], load_tree: TreeLoader) -> tuple[str, str, Mapping[str, Any]]:
    """The import string for a calculator row, the tree data URL it used, and that tree data."""
    tree_url = tree_data_url(hash_spec_id(str(row["build_code"])))
    if tree_url is None:
        raise ConversionError("the hash names a spec id no class has")
    tree_data = load_tree(tree_url)
    return talent_export_string(str(row["build_code"]), tree_data), tree_url, tree_data


def convert_calculator_builds(rows: list[dict[str, Any]], *, load_tree: TreeLoader, keep_calculator_urls: bool) -> list[dict[str, Any]]:
    """Add a converted ``wow_talent_export`` row after each ``icy_veins_talent_calc_url`` row.

    The calculator row gains ``conversion``: ``converted`` with the import string, or ``failed`` with
    the reason, in which case it stays the build's only row. A build whose talents a page's published
    import string already buys adds no row and points at that string. A converted calculator row is
    dropped unless ``keep_calculator_urls`` (the PvP pages) or its hash names PvP talents, which an
    import string cannot carry; ``load_tree`` raises ``ConversionError`` when the tree data is unreadable.
    """
    published = [str(row["build_code"]) for row in rows if row["reference_type"] == "wow_talent_export"]
    converted: dict[str, dict[str, Any]] = {}
    result: list[dict[str, Any]] = []
    for row in rows:
        if row["reference_type"] != CALCULATOR_REFERENCE_TYPE:
            result.append(row)
            continue
        try:
            export, tree_url, tree_data = _conversion(row, load_tree)
        except ConversionError as exc:
            result.append({**row, "conversion": {"status": "failed", "reason": str(exc)}})
            continue
        twin = next((code for code in published if same_talents(code, export, tree_data)), None)
        if keep_calculator_urls or _names_pvp_talents(str(row["build_code"])):
            result.append({**row, "conversion": {"status": "converted", "wow_talent_export": twin or export, "tree_data_url": tree_url}})
        if twin is not None:
            continue
        if (earlier := converted.get(export)) is not None:
            earlier["label"] = " / ".join(filter(None, (earlier["label"], row.get("label")))) or None
            continue
        converted[export] = _converted_reference(row, export, tree_url)
        result.append(converted[export])
    return result

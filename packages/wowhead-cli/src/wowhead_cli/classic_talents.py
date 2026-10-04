"""Decode a Wowhead classic-era talent calculator build code into trees, talents and ranks.

Wowhead's classic calculators (Classic Era and Season of Discovery, TBC, Wrath, Cataclysm, the
Classic PTR and WoW Forever) load ``nether.wowhead.com/<prefix>/data/talents-classic?dv=..&db=..``:
every tree's talents with their row, column and per-rank spell ids, plus the talents' spell names.
The build code is ``TalentCalcClassic.js``'s hash: one digit per talent (its rank), talents in row
then column order, trees in the class's tree order joined by ``-``, trailing zeros and dashes
dropped, and ``_``-joined suffixes for glyphs, runes, level and race. A code that starts with a
capital letter is the calculator's older packed form. WoW Forever prefixes the code with the data
file's ``hashVersion`` (``v2``).

Mists of Pandaria Classic loads the same data file but uses ``TalentCalcMists.js``: one digit per
tier (0 for no choice, else the column 1-3), six tiers of three talents.

The tree order and tree names are not in the data file; they come from Wowhead's
``WH.Wow.PlayerClass.Specialization`` table and its ``specialization.names`` page data.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from typing import Any

from warcraft_core.identity import TIERED_TALENT_CALCULATORS
from warcraft_core.talent_transport import CLASS_ID_BY_ACTOR_CLASS

# The data file a classic calculator page loads; the dv/db query pins its version.
_TALENT_DATA_URL_RE = re.compile(
    r"https://nether\.wowhead\.com/(?:[a-z-]+/)?data/talents-classic\?dv=\d+&(?:amp;)?db=\d+"
)
_PAGE_DATA_RE = re.compile(r'WH\.setPageData\("wow\.talentCalcClassic\.[a-z-]+\.data",\s*')
_SPELL_NAMES_RE = re.compile(r"WH\.Gatherer\.addData\(6,\s*\d+,\s*")

# Tree ids per class in the calculator's order (TalentCalcClassic splits the code on ``-`` in this order).
_CLASSIC_TREE_ORDER: dict[str, tuple[int, int, int]] = {
    "druid": (283, 281, 282),
    "hunter": (361, 363, 362),
    "mage": (81, 41, 61),
    "paladin": (382, 383, 381),
    "priest": (201, 202, 203),
    "rogue": (182, 181, 183),
    "shaman": (261, 263, 262),
    "warlock": (302, 303, 301),
    "warrior": (161, 164, 163),
}
_WRATH_TREE_ORDER = {**_CLASSIC_TREE_ORDER, "deathknight": (398, 399, 400)}
_CATA_TREE_ORDER: dict[str, tuple[int, int, int]] = {
    "deathknight": (398, 399, 400),
    "druid": (752, 750, 748),
    "hunter": (811, 807, 809),
    "mage": (799, 851, 823),
    "paladin": (831, 839, 855),
    "priest": (760, 813, 795),
    "rogue": (182, 181, 183),
    "shaman": (261, 263, 262),
    "warlock": (871, 867, 865),
    "warrior": (746, 815, 845),
}
TREE_ORDER_BY_CALCULATOR: dict[str, dict[str, tuple[int, int, int]]] = {
    "classic": _CLASSIC_TREE_ORDER,
    "classic-ptr": _CLASSIC_TREE_ORDER,
    "tbc": _CLASSIC_TREE_ORDER,
    "wotlk": _WRATH_TREE_ORDER,
    "cata": _CATA_TREE_ORDER,
    "forever": _CLASSIC_TREE_ORDER,
}
_TREE_NAMES: dict[int, str] = {
    41: "Fire", 61: "Frost", 81: "Arcane", 161: "Arms", 163: "Protection", 164: "Fury",
    181: "Combat", 182: "Assassination", 183: "Subtlety", 201: "Discipline", 202: "Holy", 203: "Shadow",
    261: "Elemental", 262: "Restoration", 263: "Enhancement", 281: "Feral Combat", 282: "Restoration",
    283: "Balance", 301: "Destruction", 302: "Affliction", 303: "Demonology", 361: "Beast Mastery",
    362: "Survival", 363: "Marksmanship", 381: "Retribution", 382: "Holy", 383: "Protection",
    398: "Blood", 399: "Frost", 400: "Unholy",
    746: "Arms", 748: "Restoration", 750: "Feral Combat", 752: "Balance", 760: "Discipline", 795: "Shadow",
    799: "Arcane", 807: "Marksmanship", 809: "Survival", 811: "Beast Mastery", 813: "Holy", 815: "Fury",
    823: "Frost", 831: "Holy", 839: "Protection", 845: "Protection", 851: "Fire", 855: "Retribution",
    865: "Destruction", 867: "Demonology", 871: "Affliction",
}
# The calculators whose packed (capital-letter) codes were checked against Wowhead's own calculator.
_PACKED_CODE_CALCULATORS = frozenset({"classic", "tbc", "wotlk"})
MOP_TIER_LEVELS = (15, 30, 45, 60, 75, 90)
_MOP_TALENTS_PER_TIER = 3
# Every calculator decoded here; each was checked build by build against Wowhead's rendered calculator
# (docs/wowhead/README.md lists the builds). Add one only after the same check.
CLASSIC_CALCULATORS = frozenset(TREE_ORDER_BY_CALCULATOR) | TIERED_TALENT_CALCULATORS


class BuildCodeError(ValueError):
    """A build code the calculator data cannot account for (a rank over the talent's maximum, a stray tree)."""


def talent_data_url(html: str) -> str | None:
    """The versioned talent data file URL a classic calculator page loads, else None."""
    match = _TALENT_DATA_URL_RE.search(html)
    return match.group(0).replace("&amp;", "&") if match else None


def _decode_object_after(text: str, pattern: re.Pattern[str]) -> list[Any]:
    decoder = json.JSONDecoder()
    return [decoder.raw_decode(text, match.end())[0] for match in pattern.finditer(text)]


def _spell_names(text: str) -> dict[str, str]:
    names: dict[str, str] = {}
    for block in _decode_object_after(text, _SPELL_NAMES_RE):
        if isinstance(block, dict):
            names.update((spell_id, row["name_enus"]) for spell_id, row in block.items() if isinstance(row, dict) and row.get("name_enus"))
    return names


def _valid_tree_rows(rows: object) -> bool:
    if not isinstance(rows, list):
        return False
    for row in rows:
        if not isinstance(row, list) or len(row) != 4:
            return False
        if not all(isinstance(value, int) and not isinstance(value, bool) for value in row[:3]):
            return False
        ranks = row[3]
        if not isinstance(ranks, list) or not ranks or not all(isinstance(spell, int) and spell > 0 for spell in ranks):
            return False
    return True


def is_compact_talent_data(data: object) -> bool:
    """Whether a cached value has the compact decoder schema; corrupt entries are fetched again."""
    if not isinstance(data, dict) or not isinstance(data.get("hash_version"), str):
        return False
    trees, tiers, names = (data.get(key) for key in ("trees", "class_tiers", "names"))
    if not isinstance(trees, dict) or not isinstance(tiers, dict) or not isinstance(names, dict):
        return False
    return (
        all(_valid_tree_rows(rows) for rows in trees.values())
        and all(
            isinstance(spells, list)
            and len(spells) % _MOP_TALENTS_PER_TIER == 0
            and all(isinstance(spell, int) and spell > 0 for spell in spells)
            for spells in tiers.values()
        )
        and all(isinstance(name, str) for name in names.values())
    )


def _tree_rows(tree: dict[str, Any]) -> list[list[Any]]:
    """A tree's talents as ``[talent_id, row, col, [rank spell ids]]`` in the calculator's order.

    TalentCalcClassic walks the talents row by row and column by column; two talents in one cell
    (Cataclysm's Arcane tree) keep JavaScript's integer key order, so the lower talent id first.
    """
    try:
        rows = [
            [int(talent_id), talent["row"], talent["col"], [int(spell_id) for spell_id in talent["ranks"]]]
            for talent_id, talent in tree.items()
            if isinstance(talent, dict) and isinstance(talent.get("ranks"), list) and talent["ranks"]
        ]
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("A talent tree holds a malformed talent row.") from exc
    if not _valid_tree_rows(rows):
        raise ValueError("A talent tree holds invalid talent positions or rank spell ids.")
    return sorted(rows, key=lambda row: (row[1], row[2], row[0]))


def _class_tier_spells(group: dict[str, list[Any]]) -> list[int]:
    try:
        return [int(spell_id) for spell_id in group[min(group, key=int)]]
    except (TypeError, ValueError) as exc:
        raise ValueError("A class tier list holds invalid spec or spell ids.") from exc


def compact_talent_data(text: str) -> dict[str, Any]:
    """The parts of a talent data file the decoder reads: trees, class tier lists, talent names, hash version.

    ``trees`` maps a tree id to :func:`_tree_rows`; ``class_tiers`` maps a Mists class id to its
    tier talents' spell ids, three per tier; ``names`` holds the first-rank spell names.
    """
    page_data = _decode_object_after(text, _PAGE_DATA_RE)
    if not page_data or not isinstance(page_data[0], dict) or not isinstance(page_data[0].get("talents"), dict):
        raise ValueError("The talent data file holds no talentCalcClassic data.")
    data = page_data[0]
    trees: dict[str, list[list[Any]]] = {}
    class_tiers: dict[str, list[int]] = {}
    for group_id, group in data["talents"].items():
        if not isinstance(group, dict) or not group:
            continue
        if all(isinstance(row, list) for row in group.values()):
            # Mists: class id -> spec id -> the class's tier talents; the calculator reads the first spec's.
            class_tiers[group_id] = _class_tier_spells(group)
        else:
            trees[group_id] = _tree_rows(group)
    wanted = {str(row[3][0]) for rows in trees.values() for row in rows}
    wanted.update(str(spell_id) for spells in class_tiers.values() for spell_id in spells)
    compact = {
        "hash_version": data.get("hashVersion") or "",
        "trees": trees,
        "class_tiers": class_tiers,
        "names": {spell_id: name for spell_id, name in _spell_names(text).items() if spell_id in wanted},
    }
    if not is_compact_talent_data(compact):
        raise ValueError("The talent data file has an invalid compact decoder schema.")
    return compact


def _unpack_packed_code(code: str, tree_sizes: list[list[int]]) -> str:
    """TalentCalcClassic's packed form back to the digit form.

    Per tree: a header byte (high nibble 0, low nibble the byte count), then 2-bit codes per talent:
    0 and 1 are that rank, 3 the talent's maximum rank, 2 reads the next code plus 2.
    """
    try:
        raw = base64.b64decode(code.replace("_", "/").replace("-", "+") + "=" * (-len(code) % 4), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise BuildCodeError(f"Packed build code {code!r} is not base64.") from exc
    data = list(raw)
    trees: list[str] = []
    for max_ranks in tree_sizes:
        if not data or data[0] >> 4:
            break
        count = data[0] & 15
        if len(data) < count + 1:
            raise BuildCodeError("Packed build code ends before its tree's declared byte count.")
        codes = [(byte >> shift) & 3 for byte in data[1: count + 1] for shift in (6, 4, 2, 0)]
        digits = ""
        for max_rank in max_ranks:
            if not codes:
                break
            value = codes.pop(0)
            if value == 3:
                value = max_rank
            elif value == 2:
                if not codes:
                    raise BuildCodeError("Packed build code ends before a talent rank's second 2-bit value.")
                value = codes.pop(0) + 2
            digits += str(value)
        trees.append(digits)
        data = data[count + 1:]
    return "-".join(trees)


def _split_suffixes(code: str) -> tuple[str, dict[str, Any]]:
    """The points part of a classic code and its ``_`` suffixes (TalentCalcClassic.splitPointsHash)."""
    points, *parts = code.split("_")
    suffix: dict[str, Any] = {}
    for part in parts:
        if level := re.fullmatch(r"l([1-9]\d?)", part):
            suffix["player_level"] = int(level.group(1))
        elif race := re.fullmatch(r"r([1-9]\d*)", part):
            suffix["race_id"] = int(race.group(1))
        elif not re.fullmatch(r"t[0-5]", part) and "glyphs_or_runes_code" not in suffix:
            suffix["glyphs_or_runes_code"] = part
    return points, suffix


def _talent_row(talent: list[Any], rank: int, names: dict[str, str]) -> dict[str, Any]:
    talent_id, row, col, ranks = talent
    return {
        "name": names.get(str(ranks[0])),
        "rank": rank,
        "max_rank": len(ranks),
        "spell_id": ranks[rank - 1],
        "talent_id": talent_id,
        "row": row,
        "col": col,
    }


def _decode_tree(tree_id: int, digits: str, talents: list[list[Any]], names: dict[str, str]) -> dict[str, Any]:
    if len(digits) > len(talents):
        raise BuildCodeError(f"The {_TREE_NAMES.get(tree_id, tree_id)} part {digits!r} has more digits than the tree has talents.")
    taken: list[dict[str, Any]] = []
    for digit, talent in zip(digits, talents, strict=False):
        if not digit.isdigit():
            raise BuildCodeError(f"Build code digit {digit!r} is not a rank.")
        rank = int(digit)
        if rank > len(talent[3]):
            raise BuildCodeError(f"Rank {rank} is over talent {talent[0]}'s maximum of {len(talent[3])}.")
        if rank:
            taken.append(_talent_row(talent, rank, names))
    return {
        "tree_id": tree_id,
        "name": _TREE_NAMES.get(tree_id),
        "points": sum(row["rank"] for row in taken),
        "talents": taken,
    }


def decode_classic_points(data: dict[str, Any], *, calculator: str, actor_class: str, build_code: str) -> dict[str, Any]:
    """Decode a TalentCalcClassic build code into per-tree points and talents; raises BuildCodeError."""
    tree_ids = TREE_ORDER_BY_CALCULATOR[calculator].get(actor_class)
    if tree_ids is None:
        raise BuildCodeError(f"The {calculator} calculator has no {actor_class} trees.")
    tree_talents = [data["trees"].get(str(tree_id)) or [] for tree_id in tree_ids]
    if not all(tree_talents):
        raise BuildCodeError(f"The talent data file has no {actor_class} trees.")
    code = build_code
    version = str(data.get("hash_version") or "")
    if version:
        if not code.startswith(version):
            raise BuildCodeError(f"The {calculator} calculator's build codes start with {version!r}; Wowhead reads this one as no talents.")
        code = code[len(version):]
    if code[:1].isupper():
        # The packed form is unpacked whole, before any suffix split: its base64 alphabet holds ``_``.
        if calculator not in _PACKED_CODE_CALCULATORS:
            raise BuildCodeError(f"Packed build codes are not decoded for the {calculator} calculator.")
        code = _unpack_packed_code(code, [[len(talent[3]) for talent in talents] for talents in tree_talents])
    points, suffix = _split_suffixes(code)
    parts = points.split("-")
    if len(parts) > len(tree_ids):
        raise BuildCodeError(f"Build code names {len(parts)} trees; a {actor_class} has {len(tree_ids)}.")
    trees = [
        _decode_tree(tree_id, parts[index] if index < len(parts) else "", talents, data["names"])
        for index, (tree_id, talents) in enumerate(zip(tree_ids, tree_talents, strict=True))
    ]
    return {
        "format": "classic_points",
        "points_total": sum(tree["points"] for tree in trees),
        "points_by_tree": "/".join(str(tree["points"]) for tree in trees),
        "trees": trees,
        **suffix,
    }


def decode_mop_tiers(data: dict[str, Any], *, actor_class: str, build_code: str) -> dict[str, Any]:
    """Decode a TalentCalcMists tier code (one 0-3 digit per tier) into the chosen talents; raises BuildCodeError."""
    class_id = CLASS_ID_BY_ACTOR_CLASS.get(actor_class)
    spells = data["class_tiers"].get(str(class_id)) if class_id is not None else None
    if not spells:
        raise BuildCodeError(f"The talent data file has no {actor_class} talents.")
    tier_count = len(spells) // _MOP_TALENTS_PER_TIER
    if re.fullmatch(r"[0-3]*", build_code) is None or len(build_code) > tier_count:
        raise BuildCodeError(f"Build code {build_code!r} is not one 0-3 choice for each of the {tier_count} tiers.")
    tiers = []
    for index in range(tier_count):
        choice = int(build_code[index]) if index < len(build_code) else 0
        row: dict[str, Any] = {"tier": index + 1, "level": MOP_TIER_LEVELS[min(index, len(MOP_TIER_LEVELS) - 1)], "choice": choice}
        if choice:
            spell_id = spells[index * _MOP_TALENTS_PER_TIER + choice - 1]
            row["talent"] = {"name": data["names"].get(str(spell_id)), "spell_id": spell_id}
        tiers.append(row)
    return {"format": "mop_tiers", "tiers_chosen": sum(1 for row in tiers if row["choice"]), "tiers": tiers}


def undecodable_reason(calculator: str, *, spec_named: bool) -> str | None:
    """Why a build from ``calculator`` is not decoded, or None when it can be."""
    if spec_named and calculator not in TIERED_TALENT_CALCULATORS:
        return f"The {calculator} calculator's paths name no spec, so this spec-named path is not one of its builds."
    return None


def decode_build(data: dict[str, Any], *, calculator: str, actor_class: str, build_code: str) -> dict[str, Any]:
    """The decoded ``talents`` block for a build, or ``decoded: false`` with the reason the code does not fit the data."""
    try:
        if calculator in TIERED_TALENT_CALCULATORS:
            decoded = decode_mop_tiers(data, actor_class=actor_class, build_code=build_code)
        else:
            decoded = decode_classic_points(data, calculator=calculator, actor_class=actor_class, build_code=build_code)
    except BuildCodeError as exc:
        return {"decoded": False, "reason": str(exc)}
    return {"decoded": True, **decoded}

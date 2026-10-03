"""The retail class/spec table and the lookup that reads any provider's spelling of a spec.

Providers spell one spec many ways: Lorrgs ``hunter-beastmastery``, Raider.IO ``hunter-beast-mastery``,
Warcraft Logs ``BeastMastery``, guide sites ``beast-mastery-hunter``, and people ``bm hunter``.
:func:`lookup_spec` maps all of them to one :class:`WowSpec`, whose properties render the spelling
each provider takes. Keys are the identity payloads' own: ``deathknight`` and ``beast_mastery``.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from typing import Final

WOW_CLASS_NAMES: Final[dict[str, str]] = {
    "deathknight": "Death Knight",
    "demonhunter": "Demon Hunter",
    "druid": "Druid",
    "evoker": "Evoker",
    "hunter": "Hunter",
    "mage": "Mage",
    "monk": "Monk",
    "paladin": "Paladin",
    "priest": "Priest",
    "rogue": "Rogue",
    "shaman": "Shaman",
    "warlock": "Warlock",
    "warrior": "Warrior",
}

# Community shorthand for classes and specs, spelled the way guide sites title their pages.
CLASS_SPEC_ALIASES: Final[dict[str, str]] = {
    "dk": "death knight",
    "bdk": "blood death knight",
    "fdk": "frost death knight",
    "udk": "unholy death knight",
    "dh": "demon hunter",
    "veng": "vengeance",
    "bm": "beast mastery",
    "mm": "marksmanship",
    "sv": "survival",
    "pally": "paladin",
    "pal": "paladin",
    "ret": "retribution",
    "prot": "protection",
    "resto": "restoration",
    "disc": "discipline",
    "sp": "shadow priest",
    "spriest": "shadow priest",
    "mw": "mistweaver",
    "ww": "windwalker",
    "sub": "subtlety",
    "sin": "assassination",
    "assa": "assassination",
    "ele": "elemental",
    "enh": "enhancement",
    "enha": "enhancement",
    "lock": "warlock",
    "aff": "affliction",
    "demo": "demonology",
    "destro": "destruction",
    "aug": "augmentation",
    "dev": "devastation",
    "pres": "preservation",
    "boomy": "balance",
    "boomkin": "balance",
}


def _hyphenated(name: str) -> str:
    return name.lower().replace(" ", "-")


def raiderio_class_slug(class_key: str) -> str:
    """Raider.IO's class slug (``death-knight``), which Wowhead also takes as a talent-calc path segment."""
    return _hyphenated(WOW_CLASS_NAMES[class_key])


def warcraftlogs_class_slug(class_key: str) -> str:
    """Warcraft Logs' CamelCase class slug (``DeathKnight``)."""
    return WOW_CLASS_NAMES[class_key].replace(" ", "")


@dataclass(frozen=True, slots=True)
class WowSpec:
    class_key: str
    key: str
    # Blizzard's specialization id, which Lorrgs and Wowhead builds carry.
    spec_id: int

    @property
    def name(self) -> str:
        return self.key.replace("_", " ").title()

    @property
    def lorrgs_slug(self) -> str:
        return f"{self.class_key}-{self.key.replace('_', '')}"

    @property
    def raiderio_spec_slug(self) -> str:
        return _hyphenated(self.name)

    @property
    def raiderio_slug(self) -> str:
        return f"{raiderio_class_slug(self.class_key)}-{self.raiderio_spec_slug}"

    @property
    def warcraftlogs_spec_slug(self) -> str:
        return self.name.replace(" ", "")


WOW_SPECS: Final[tuple[WowSpec, ...]] = tuple(
    WowSpec(class_key, key, spec_id)
    for class_key, specs in (
        ("deathknight", (("blood", 250), ("frost", 251), ("unholy", 252))),
        ("demonhunter", (("havoc", 577), ("vengeance", 581), ("devourer", 1480))),
        ("druid", (("balance", 102), ("feral", 103), ("guardian", 104), ("restoration", 105))),
        ("evoker", (("devastation", 1467), ("preservation", 1468), ("augmentation", 1473))),
        ("hunter", (("beast_mastery", 253), ("marksmanship", 254), ("survival", 255))),
        ("mage", (("arcane", 62), ("fire", 63), ("frost", 64))),
        ("monk", (("brewmaster", 268), ("mistweaver", 270), ("windwalker", 269))),
        ("paladin", (("holy", 65), ("protection", 66), ("retribution", 70))),
        ("priest", (("discipline", 256), ("holy", 257), ("shadow", 258))),
        ("rogue", (("assassination", 259), ("outlaw", 260), ("subtlety", 261))),
        ("shaman", (("elemental", 262), ("enhancement", 263), ("restoration", 264))),
        ("warlock", (("affliction", 265), ("demonology", 266), ("destruction", 267))),
        ("warrior", (("arms", 71), ("fury", 72), ("protection", 73))),
    )
    for key, spec_id in specs
)


def _compact(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _spelling(text: str) -> str:
    """``text`` with its whole-word shorthand spelled out, compacted: ``bm hunter`` is ``beastmasteryhunter``."""
    return "".join(_compact(CLASS_SPEC_ALIASES.get(word, word)) for word in re.split(r"[^a-z0-9]+", text.lower()))


def _specs_by_spelling() -> dict[str, tuple[WowSpec, ...]]:
    """Every compacted spelling of a spec: the bare spec, which several classes can share (frost), and
    the spec with its class in either order (deathknightfrost, frostdeathknight), which names one spec."""
    index: dict[str, tuple[WowSpec, ...]] = {}
    for spec in WOW_SPECS:
        for text in {_compact(spec.key), _compact(spec.class_key + spec.key), _compact(spec.key + spec.class_key)}:
            index[text] = (*index.get(text, ()), spec)
    return index


_SPECS_BY_SPELLING: Final = _specs_by_spelling()


def lookup_class(text: str) -> str | None:
    """The class key any provider's spelling names (``Death Knight``, ``death-knight``, ``DeathKnight``, ``dk``)."""
    spelling = _spelling(text)
    return spelling if spelling in WOW_CLASS_NAMES else None


def lookup_spec(text: str, class_hint: str | None = None) -> WowSpec | None:
    """The one spec ``text`` names in any provider's spelling, or None.

    A bare spec several classes share (frost, holy, protection, restoration) names none unless
    ``class_hint`` (any class spelling) picks one; the hint never overrides a class ``text`` names.
    """
    specs = _SPECS_BY_SPELLING.get(_spelling(text), ())
    if len(specs) > 1 and class_hint is not None:
        hinted = lookup_class(class_hint)
        specs = tuple(spec for spec in specs if spec.class_key == hinted)
    return specs[0] if len(specs) == 1 else None


def close_specs(text: str, *, limit: int = 5) -> list[WowSpec]:
    """Specs whose spellings are closest to ``text``, for a ``suggestions`` list on a miss."""
    matches = difflib.get_close_matches(_spelling(text), _SPECS_BY_SPELLING, n=limit, cutoff=0.75)
    return list(dict.fromkeys(spec for match in matches for spec in _SPECS_BY_SPELLING[match]))[:limit]

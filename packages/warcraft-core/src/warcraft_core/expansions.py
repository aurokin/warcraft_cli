"""Shared expansion vocabulary: keys, aliases, and per-site mappings used by every binary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True, slots=True)
class Expansion:
    key: str
    label: str
    aliases: tuple[str, ...]
    # Wowhead URL path prefix: "" for retail, None when Wowhead has no site for this expansion.
    wowhead_path_prefix: str | None
    # Warcraft Logs site profile; None when Warcraft Logs has no site for this expansion.
    warcraftlogs_site: str | None


EXPANSIONS: Final[tuple[Expansion, ...]] = (
    Expansion("retail", "Retail / Default", ("default", "live", "wowhead"), "", "retail"),
    Expansion("classic", "Classic Era", ("vanilla",), "classic", "classic"),
    Expansion("tbc", "Burning Crusade Classic", ("burning-crusade", "bc"), "tbc", "classic"),
    Expansion("wotlk", "Wrath of the Lich King Classic", ("wrath",), "wotlk", "classic"),
    Expansion("cata", "Cataclysm Classic", ("cataclysm",), "cata", "classic"),
    Expansion("mop-classic", "Mists of Pandaria Classic", ("mop", "mists"), "mop-classic", "classic"),
    Expansion("ptr", "Retail PTR", (), "ptr", None),
    Expansion("beta", "Retail Beta", (), "beta", None),
    Expansion("classic-ptr", "Classic PTR", ("classicptr",), "classic-ptr", None),
    Expansion(
        "fresh",
        "Classic Fresh / Anniversary",
        ("classic-fresh", "classicfresh", "anniversary", "classic-anniversary"),
        None,
        "fresh",
    ),
)

_BY_KEY: Final[dict[str, Expansion]] = {expansion.key: expansion for expansion in EXPANSIONS}
_ALIAS_TO_KEY: Final[dict[str, str]] = {
    alias: expansion.key for expansion in EXPANSIONS for alias in (expansion.key, *expansion.aliases)
}


def list_expansions() -> tuple[Expansion, ...]:
    return EXPANSIONS


def expansion_keys() -> tuple[str, ...]:
    return tuple(expansion.key for expansion in EXPANSIONS)


def normalize_expansion_key(value: str) -> str:
    return value.strip().lower().replace("_", "-")


def resolve_expansion(value: str | None) -> Expansion:
    if value is None or value.strip() == "":
        return _BY_KEY["retail"]
    key = _ALIAS_TO_KEY.get(normalize_expansion_key(value))
    if key is None:
        options = ", ".join(expansion_keys())
        raise ValueError(f"Unknown expansion {value!r}. Supported: {options}")
    return _BY_KEY[key]


def warcraftlogs_site_for_expansion(key: str) -> str:
    expansion = _BY_KEY.get(key)
    if expansion is None or expansion.warcraftlogs_site is None:
        raise ValueError(f"Expansion {key!r} does not map to a Warcraft Logs site profile.")
    return expansion.warcraftlogs_site


def wowhead_path_prefixes() -> frozenset[str]:
    return frozenset(expansion.wowhead_path_prefix for expansion in EXPANSIONS if expansion.wowhead_path_prefix)

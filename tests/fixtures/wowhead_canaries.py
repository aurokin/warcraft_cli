"""Pinned Wowhead parser canary entities (AUR-359)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ParserCanary:
    case_id: str
    expansion: str
    entity_type: str
    entity_id: int
    label: str


PARSER_CANARIES: tuple[ParserCanary, ...] = (
    ParserCanary("retail-item", "retail", "item", 19019, "Thunderfury"),
    ParserCanary("retail-npc", "retail", "npc", 12056, "Baron Geddon"),
    ParserCanary("retail-spell", "retail", "spell", 40827, "Molten Armor"),
    ParserCanary("retail-quest", "retail", "quest", 76487, "The Art of War"),
    ParserCanary("retail-object", "retail", "object", 181332, "Ornate Treasure Chest"),
    ParserCanary("wotlk-item", "wotlk", "item", 49623, "Shadowmourne"),
    ParserCanary("classic-item", "classic", "item", 19019, "Thunderfury (Classic Era page)"),
)

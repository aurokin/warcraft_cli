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
    ParserCanary("retail-quest", "retail", "quest", 5441, "Lazy Peons"),
    ParserCanary("retail-object", "retail", "object", 181332, "Ornate Treasure Chest"),
    ParserCanary("wotlk-item", "wotlk", "item", 49623, "Shadowmourne"),
    ParserCanary("classic-item", "classic", "item", 19019, "Thunderfury (Classic Era page)"),
)


# The entity key `suggestion_entity_type_from_type_id` must derive for a row Wowhead labels with a
# given `typeName`. Keyed by Wowhead's own label rather than by the numeric id, so renumbering an id
# in entity_types.py fails these checks instead of being restated by them.
SUGGESTION_TYPE_NAME_TO_ENTITY: dict[str, str] = {
    "NPC": "npc",
    "Object": "object",
    "Item": "item",
    "Quest": "quest",
    "Spell": "spell",
    "Zone": "zone",
    "Faction": "faction",
    "Hunter Pet": "pet",
    "Achievement": "achievement",
    "World Event": "event",
    "Currency": "currency",
    "Guide": "guide",
    "Transmog Set": "transmog-set",
    "News Post": "news",
}

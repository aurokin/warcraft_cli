"""The Wowhead entity types this CLI understands, and how they map onto Wowhead's own type ids.

``suggestion_type_ids`` are the numeric ``type`` values Wowhead puts on rows of the search
suggestions endpoint. ``resolve_supported`` therefore only holds for types that endpoint can emit:
mounts, recipes and battle pets have no suggestion type of their own (they come back as items,
spells or NPCs), so ``resolve --entity-type`` rejects them instead of filtering every row away.

Types this CLI cannot parse are still listed when Wowhead's ``<type>=<id>`` URL form reaches them
(``news``, ``event``): a search row nobody can open is worse than one with no follow-up command.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class WowheadEntityType:
    key: str
    suggestion_type_ids: tuple[int, ...] = ()
    search_hint_terms: tuple[str, ...] = ()
    parser_supported: bool = True
    resolve_supported: bool = False
    hydrate_supported: bool = False


ENTITY_TYPE_DEFS: tuple[WowheadEntityType, ...] = (
    WowheadEntityType(
        key="achievement",
        suggestion_type_ids=(10,),
        search_hint_terms=("achievement", "achievements"),
        parser_supported=True,
        resolve_supported=True,
        hydrate_supported=True,
    ),
    WowheadEntityType(
        key="battle-pet",
        search_hint_terms=("battle pet", "battle pets"),
        parser_supported=True,
        hydrate_supported=True,
    ),
    WowheadEntityType(
        key="companion",
        suggestion_type_ids=(112,),
        search_hint_terms=("companion", "companions"),
        parser_supported=True,
        resolve_supported=False,
        hydrate_supported=False,
    ),
    WowheadEntityType(
        key="currency",
        suggestion_type_ids=(17,),
        search_hint_terms=("currency", "currencies", "token", "tokens"),
        parser_supported=True,
        resolve_supported=True,
        hydrate_supported=True,
    ),
    WowheadEntityType(
        key="event",
        suggestion_type_ids=(12,),
        parser_supported=False,
        hydrate_supported=False,
    ),
    WowheadEntityType(
        key="faction",
        suggestion_type_ids=(8,),
        search_hint_terms=("faction", "factions", "reputation", "rep"),
        parser_supported=True,
        resolve_supported=True,
        hydrate_supported=True,
    ),
    WowheadEntityType(
        key="guide",
        suggestion_type_ids=(100,),
        search_hint_terms=("guide", "guides"),
        parser_supported=True,
        resolve_supported=True,
        hydrate_supported=False,
    ),
    WowheadEntityType(
        key="item",
        suggestion_type_ids=(3,),
        search_hint_terms=("item", "items", "gear"),
        parser_supported=True,
        resolve_supported=True,
        hydrate_supported=True,
    ),
    WowheadEntityType(
        key="mount",
        search_hint_terms=("mount", "mounts"),
        parser_supported=True,
        hydrate_supported=True,
    ),
    WowheadEntityType(
        key="news",
        suggestion_type_ids=(162,),
        parser_supported=False,
        hydrate_supported=False,
    ),
    WowheadEntityType(
        key="npc",
        suggestion_type_ids=(1,),
        search_hint_terms=("npc", "npcs", "mob", "mobs"),
        parser_supported=True,
        resolve_supported=True,
        hydrate_supported=True,
    ),
    WowheadEntityType(
        key="object",
        suggestion_type_ids=(2,),
        search_hint_terms=("object", "objects"),
        parser_supported=True,
        resolve_supported=True,
        hydrate_supported=True,
    ),
    WowheadEntityType(
        key="pet",
        suggestion_type_ids=(9,),
        search_hint_terms=("pet", "pets", "hunter pet", "hunter pets"),
        parser_supported=True,
        resolve_supported=True,
        hydrate_supported=True,
    ),
    WowheadEntityType(
        key="quest",
        suggestion_type_ids=(5,),
        search_hint_terms=("quest", "quests"),
        parser_supported=True,
        resolve_supported=True,
        hydrate_supported=True,
    ),
    WowheadEntityType(
        key="recipe",
        search_hint_terms=("recipe", "recipes"),
        parser_supported=True,
        hydrate_supported=True,
    ),
    WowheadEntityType(
        key="spell",
        suggestion_type_ids=(6,),
        search_hint_terms=("spell", "spells", "ability", "abilities", "talent", "talents"),
        parser_supported=True,
        resolve_supported=True,
        hydrate_supported=True,
    ),
    WowheadEntityType(
        key="transmog-set",
        suggestion_type_ids=(101,),
        search_hint_terms=("transmog", "transmog set", "transmog sets", "set", "sets"),
        parser_supported=True,
        resolve_supported=True,
        hydrate_supported=True,
    ),
    WowheadEntityType(
        key="zone",
        suggestion_type_ids=(7,),
        search_hint_terms=("zone", "zones"),
        parser_supported=True,
        resolve_supported=True,
        hydrate_supported=True,
    ),
)

SUGGESTION_TYPE_TO_ENTITY: dict[int, str] = {
    type_id: row.key
    for row in ENTITY_TYPE_DEFS
    for type_id in row.suggestion_type_ids
}
SEARCH_TYPE_HINTS: dict[str, set[str]] = {
    row.key: set(row.search_hint_terms)
    for row in ENTITY_TYPE_DEFS
    if row.search_hint_terms
}
PARSER_ENTITY_TYPES = frozenset(row.key for row in ENTITY_TYPE_DEFS if row.parser_supported)
RESOLVE_ENTITY_TYPES = frozenset(row.key for row in ENTITY_TYPE_DEFS if row.resolve_supported)
HYDRATABLE_ENTITY_TYPES = frozenset(row.key for row in ENTITY_TYPE_DEFS if row.hydrate_supported)
DEFAULT_HYDRATE_ENTITY_TYPES = ("spell", "item", "npc")


def suggestion_entity_type_from_type_id(type_id: int) -> str | None:
    return SUGGESTION_TYPE_TO_ENTITY.get(type_id)

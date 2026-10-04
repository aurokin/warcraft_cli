"""Compact views of the Blizzard payloads too large or too nested to hand an agent raw.

The ``*_view`` reducers run on the raw body before it is cached (see ``BlizzardClient._get``), so a
replay reads the compact form and never re-parses megabytes: a PvP leaderboard is ~2 MB, a
character's transmog collection ~1 MB, the region's commodity market ~26 MB. The ``*_page``
functions run on every read and apply the caller's filter and limit to an already-reduced view.
Everything here is pure: no I/O, no printing.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

# A bracket as Blizzard names it in a leaderboard or a character's pvp-summary links: 2v2, 3v3,
# rbg, shuffle-overall, blitz-<class>-<spec>. One path segment, so it cannot reach another endpoint.
BRACKET_PATTERN = re.compile(r"[a-z0-9-]+")
_BRACKET_HREF = re.compile(r"/pvp-bracket/([a-z0-9-]+)(?:\?|$)")


def _dig(value: Any, *keys: str) -> Any:
    """``value[k1][k2]...``, or ``None`` as soon as a level is missing or not a mapping."""
    for key in keys:
        if not isinstance(value, Mapping):
            return None
        value = value.get(key)
    return value


def _rows(payload: Any, key: str) -> list[Mapping[str, Any]]:
    rows = _dig(payload, key)
    return [row for row in rows if isinstance(row, Mapping)] if isinstance(rows, list) else []


def _record(stats: Any) -> dict[str, Any]:
    return {"played": _dig(stats, "played"), "won": _dig(stats, "won"), "lost": _dig(stats, "lost")}


def character_ref(payload: Any) -> dict[str, Any]:
    """The character a profile sub-resource names, without its hrefs."""
    return {
        "name": _dig(payload, "character", "name"),
        "id": _dig(payload, "character", "id"),
        "realm": _dig(payload, "character", "realm", "slug"),
    }


# --- PvP ------------------------------------------------------------------------------------------


def _epoch_ms_to_iso(value: Any) -> str | None:
    if not isinstance(value, int):
        return None
    return datetime.fromtimestamp(value / 1000, UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def pvp_season_summary(index: Mapping[str, Any], detail: Mapping[str, Any], leaderboards: Mapping[str, Any]) -> dict[str, Any]:
    """A season's name and start, the current season id, every season id (newest first) and its brackets."""
    return {
        "season_name": detail.get("season_name"),
        "season_start": _epoch_ms_to_iso(detail.get("season_start_timestamp")),
        "current_season_id": _dig(index, "current_season", "id"),
        "seasons": sorted((row["id"] for row in _rows(index, "seasons") if isinstance(row.get("id"), int)), reverse=True),
        "brackets": [row["name"] for row in _rows(leaderboards, "leaderboards") if isinstance(row.get("name"), str)],
    }


def pvp_rewards_view(payload: Mapping[str, Any]) -> dict[str, Any]:
    """A season's title cutoffs (Gladiator, Legend, Hero, ...), one row per bracket/spec/faction."""
    return {
        "rewards": [
            {
                "bracket": _dig(row, "bracket", "type"),
                "achievement": _dig(row, "achievement", "name"),
                "achievement_id": _dig(row, "achievement", "id"),
                "rating_cutoff": row.get("rating_cutoff"),
                # Shuffle and Blitz cutoffs are per spec; spec names repeat across classes (Frost),
                # so the id is the unambiguous half.
                "specialization": _dig(row, "specialization", "name"),
                "specialization_id": _dig(row, "specialization", "id"),
                "faction": _dig(row, "faction", "type"),
            }
            for row in _rows(payload, "rewards")
        ]
    }


def leaderboard_view(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Every ranked entry of one leaderboard as a flat row: rank, rating, character, record, tier."""
    return {
        "bracket": payload.get("name"),
        "bracket_type": _dig(payload, "bracket", "type"),
        "season_id": _dig(payload, "season", "id"),
        "entries": [
            {
                "rank": row.get("rank"),
                "rating": row.get("rating"),
                "name": _dig(row, "character", "name"),
                "realm": _dig(row, "character", "realm", "slug"),
                "character_id": _dig(row, "character", "id"),
                "faction": _dig(row, "faction", "type"),
                **_record(row.get("season_match_statistics")),
                "tier_id": _dig(row, "tier", "id"),
            }
            for row in _rows(payload, "entries")
        ],
    }


def bracket_names(pvp_summary: Mapping[str, Any]) -> list[str]:
    """The bracket names a character's pvp-summary links to, in Blizzard's order."""
    names: list[str] = []
    for link in _rows(pvp_summary, "brackets"):
        match = _BRACKET_HREF.search(str(link.get("href") or ""))
        if match and match.group(1) not in names:
            names.append(match.group(1))
    return names


def pvp_bracket_row(name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    """One rated bracket from a character's pvp-bracket read. Rounds exist only for Solo Shuffle."""
    row = {
        "bracket": name,
        "bracket_type": _dig(payload, "bracket", "type"),
        "season_id": _dig(payload, "season", "id"),
        "rating": payload.get("rating"),
        "tier_id": _dig(payload, "tier", "id"),
        "specialization": _dig(payload, "specialization", "name"),
        "season": _record(payload.get("season_match_statistics")),
        "weekly": _record(payload.get("weekly_match_statistics")),
    }
    if "season_round_statistics" in payload:
        row["season_rounds"] = _record(payload["season_round_statistics"])
        row["weekly_rounds"] = _record(payload.get("weekly_round_statistics"))
    return row


def pvp_character_summary(pvp_summary: Mapping[str, Any], brackets: Sequence[dict[str, Any]]) -> dict[str, Any]:
    return {
        "character": character_ref(pvp_summary),
        "honor_level": pvp_summary.get("honor_level"),
        "honorable_kills": pvp_summary.get("honorable_kills"),
        "brackets": list(brackets),
        "battlegrounds": [
            {"map": _dig(row, "world_map", "name"), **_record(row.get("match_statistics"))}
            for row in _rows(pvp_summary, "pvp_map_statistics")
        ],
    }


# --- Collections ----------------------------------------------------------------------------------


def _by_name(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda row: (str(row.get("name") or "").casefold(), row.get("id") or 0))


def _mounts_view(payload: Mapping[str, Any]) -> dict[str, Any]:
    rows = [
        {
            "id": _dig(row, "mount", "id"),
            "name": _dig(row, "mount", "name"),
            "is_favorite": bool(row.get("is_favorite")),
            # False for a mount this character cannot ride (another faction's, another class's).
            "is_useable": bool(row.get("is_useable")),
        }
        for row in _rows(payload, "mounts")
    ]
    return {"count": len(rows), "rows": _by_name(rows)}


def _pets_view(payload: Mapping[str, Any]) -> dict[str, Any]:
    rows = [
        {
            "id": row.get("id"),
            "name": _dig(row, "species", "name"),
            "species_id": _dig(row, "species", "id"),
            "nickname": row.get("name"),
            "level": row.get("level"),
            "quality": _dig(row, "quality", "type"),
            "is_favorite": bool(row.get("is_favorite")),
        }
        for row in _rows(payload, "pets")
    ]
    return {
        "count": len(rows),
        "unique_species": len({row["species_id"] for row in rows}),
        "rows": _by_name(rows),
    }


def _toys_view(payload: Mapping[str, Any]) -> dict[str, Any]:
    rows = [
        {"id": _dig(row, "toy", "id"), "name": _dig(row, "toy", "name"), "is_favorite": bool(row.get("is_favorite"))}
        for row in _rows(payload, "toys")
    ]
    return {"count": len(rows), "rows": _by_name(rows)}


def _heirlooms_view(payload: Mapping[str, Any]) -> dict[str, Any]:
    rows = [
        {"id": _dig(row, "heirloom", "id"), "name": _dig(row, "heirloom", "name"), "upgrade_level": _dig(row, "upgrade", "level")}
        for row in _rows(payload, "heirlooms")
    ]
    return {"count": len(rows), "rows": _by_name(rows)}


def _transmogs_view(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Appearance counts per slot plus the collected appearance sets; single appearances are ids only."""
    slots = {
        str(_dig(row, "slot", "type")): len(appearances) if isinstance(appearances := row.get("appearances"), list) else 0
        for row in _rows(payload, "slots")
    }
    sets = [{"id": row.get("id"), "name": row.get("name")} for row in _rows(payload, "appearance_sets")]
    return {
        "count": len(sets),
        "appearance_count": sum(slots.values()),
        "appearances_by_slot": slots,
        "rows": _by_name(sets),
    }


# Collection kinds `blizzard collections` reads, each a /collections/<kind> sub-resource, in output order.
COLLECTION_VIEWS: dict[str, Callable[[Mapping[str, Any]], dict[str, Any]]] = {
    "mounts": _mounts_view,
    "pets": _pets_view,
    "toys": _toys_view,
    "heirlooms": _heirlooms_view,
    "transmogs": _transmogs_view,
}


def collection_page(view: Mapping[str, Any], *, match: str | None, limit: int) -> dict[str, Any]:
    """One reduced collection with ``rows`` cut to the names containing ``match``, then to ``limit``.

    ``count`` stays the whole collection; ``matched`` is how many rows the filter kept.
    """
    rows = list(view.get("rows") or [])
    if match:
        needle = match.casefold()
        rows = [row for row in rows if needle in str(row.get("name") or "").casefold()]
    page = {key: value for key, value in view.items() if key != "rows"}
    return {**page, "matched": len(rows), "returned": min(len(rows), limit), "truncated": len(rows) > limit, "items": rows[:limit]}


# --- Auctions -------------------------------------------------------------------------------------


def _unit_price(auction: Mapping[str, Any]) -> int | None:
    """Commodities carry ``unit_price``; realm auctions carry ``buyout`` for the whole listing."""
    unit_price = auction.get("unit_price")
    if isinstance(unit_price, int):
        return unit_price
    buyout, quantity = auction.get("buyout"), auction.get("quantity")
    if isinstance(buyout, int) and isinstance(quantity, int) and quantity > 0:
        return buyout // quantity
    return None


def _unit_weighted_median(prices: list[tuple[int, int]]) -> int:
    """The lowest price at which half of the listed units are available (``prices`` = (price, units))."""
    prices.sort()
    half = sum(units for _, units in prices) / 2
    seen = 0
    for price, units in prices:
        seen += units
        if seen >= half:
            return price
    return prices[-1][0]


def auctions_view(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Per item id: listings, units, cheapest and unit-weighted median unit price, most-listed first.

    Listings with only a bid (no buyout) count toward listings and units but not toward prices, so
    an item listed only that way has ``null`` prices.
    """
    listings: dict[int, int] = defaultdict(int)
    units: dict[int, int] = defaultdict(int)
    prices: dict[int, list[tuple[int, int]]] = defaultdict(list)
    auctions = _rows(payload, "auctions")
    for auction in auctions:
        item_id, quantity = _dig(auction, "item", "id"), auction.get("quantity")
        if not isinstance(item_id, int):
            continue
        quantity = quantity if isinstance(quantity, int) and quantity > 0 else 1
        listings[item_id] += 1
        units[item_id] += quantity
        if (price := _unit_price(auction)) is not None:
            prices[item_id].append((price, quantity))
    items: list[dict[str, Any]] = [
        {
            "item_id": item_id,
            "auctions": listings[item_id],
            "quantity": units[item_id],
            "min_unit_price": min(price for price, _ in prices[item_id]) if prices[item_id] else None,
            "median_unit_price": _unit_weighted_median(prices[item_id]) if prices[item_id] else None,
        }
        for item_id in listings
    ]
    items.sort(key=lambda row: (-row["auctions"], row["item_id"]))
    return {"auction_count": len(auctions), "items": items}


def auctions_page(view: Mapping[str, Any], *, item_ids: Sequence[int], limit: int) -> dict[str, Any]:
    """The requested item ids (in the order asked, every one of them), or the ``limit`` most-listed items."""
    items: list[dict[str, Any]] = list(view.get("items") or [])
    page: dict[str, Any] = {"auction_count": view.get("auction_count"), "item_count": len(items)}
    if item_ids:
        by_id = {row["item_id"]: row for row in items}
        wanted = list(dict.fromkeys(item_ids))
        return {
            **page,
            "items": [by_id[item_id] for item_id in wanted if item_id in by_id],
            "not_listed": [item_id for item_id in wanted if item_id not in by_id],
        }
    return {**page, "returned": min(len(items), limit), "truncated": len(items) > limit, "items": items[:limit]}

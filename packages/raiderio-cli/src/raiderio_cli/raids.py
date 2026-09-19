"""Raider.IO guild raid rankings and the raid catalog: validation, paging, and row normalization.

The Typer commands in ``main`` pass validated flags in and emit what comes out; nothing here
touches Typer, prints, or raises ``typer.Exit``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from warcraft_core.provider import ProviderError
from warcraft_core.shapes import as_dict, as_list
from warcraft_core.wow_normalization import normalize_region, primary_realm_slug

from raiderio_cli.client import RAIDERIO_BASE_URL, RAIDERIO_SITE_BASE_URL, RaiderIOClient

RAID_DIFFICULTIES = ("normal", "heroic", "mythic")
RAID_REGIONS = ("world", "us", "eu", "kr", "tw", "cn")
# Rows per API request. The endpoint accepts up to 200, but a fixed page keeps ``--page`` meaning
# the same 20-row slice regardless of ``--limit``, matching ``leaderboard mythic-plus``.
RAID_RANKINGS_PAGE_SIZE = 20


def validated_raid_scope(*, difficulty: str, region: str, realm: str | None) -> tuple[str, str, str | None]:
    """Normalize and check the ``--difficulty``/``--region``/``--realm`` combination.

    ``--region`` accepts the same aliases as every other command (``na`` -> ``us``) and ``--realm``
    accepts a display name (``Tarren Mill``), which becomes the slug both the API and the citation
    URL need.
    """
    difficulty = difficulty.strip().lower()
    region = normalize_region(region)
    realm_input = (realm or "").strip()
    realm = primary_realm_slug(realm_input) if realm_input else None
    if difficulty not in RAID_DIFFICULTIES:
        raise ProviderError("invalid_query", f"--difficulty must be one of: {', '.join(RAID_DIFFICULTIES)}")
    if region not in RAID_REGIONS:
        raise ProviderError("invalid_query", f"--region must be one of: {', '.join(RAID_REGIONS)}")
    if realm and region == "world":
        raise ProviderError("invalid_query", "--realm requires a standard --region (us, eu, kr, tw, cn), not world")
    return difficulty, region, realm


def raid_pages_for_limit(limit: int) -> int:
    return max(1, -(-limit // RAID_RANKINGS_PAGE_SIZE))  # ceil division


def raid_rankings_url(*, raid: str, difficulty: str, region: str, realm: str | None) -> str:
    """The raider.io rankings page for a scope (the site's own URL layout).

    The realm is percent-encoded: ``primary_realm_slug`` passes non-ASCII realms (``Ревущий
    фьорд``) through unchanged, spaces and all, and a citation with a raw space is not a URL.
    """
    url = f"{RAIDERIO_SITE_BASE_URL}/{raid}/rankings/{region}/{difficulty}"
    return f"{url}?realm={quote(realm, safe='')}" if realm else url


def _defeated_encounter(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "slug": row.get("slug"),
        "first_defeated": row.get("firstDefeated"),
        "last_defeated": row.get("lastDefeated"),
    }


def _pulled_encounter(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "slug": row.get("slug"),
        "num_pulls": row.get("numPulls"),
        "best_percent": row.get("bestPercent"),
        "is_defeated": row.get("isDefeated"),
        "pull_started_at": row.get("pullStartedAt"),
    }


def raid_ranking_row(row: dict[str, Any]) -> dict[str, Any]:
    """Flatten one ``raidRankings`` entry. ``rank`` is relative to the requested scope."""
    guild = as_dict(row.get("guild"))
    realm = as_dict(guild.get("realm"))
    region = as_dict(guild.get("region"))
    path = guild.get("path")
    defeated = [_defeated_encounter(entry) for entry in as_list(row.get("encountersDefeated")) if isinstance(entry, dict)]
    pulled = [_pulled_encounter(entry) for entry in as_list(row.get("encountersPulled")) if isinstance(entry, dict)]
    return {
        "rank": row.get("rank"),
        "region_rank": row.get("regionRank"),
        "guild": {
            "name": guild.get("name"),
            "realm": realm.get("slug"),
            "realm_name": realm.get("name"),
            "region": region.get("slug"),
            "faction": guild.get("faction"),
            "profile_url": f"{RAIDERIO_SITE_BASE_URL}{path}" if isinstance(path, str) and path else None,
        },
        "encounters_defeated_count": len(defeated),
        "encounters_pulled_count": len(pulled),
        "encounters_defeated": defeated,
        "encounters_pulled": pulled,
    }


def sample_raid_rankings(
    client: RaiderIOClient,
    *,
    raid: str,
    difficulty: str,
    region: str,
    realm: str | None,
    page: int,
    limit: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Fetch as many 20-row pages as ``limit`` needs, deduped by guild, stopping at a short page.

    The returned meta carries the keys ``freshness_payload`` / ``citations_payload`` expect.
    """
    pages = raid_pages_for_limit(limit)
    rows: list[dict[str, Any]] = []
    seen_guilds: set[str] = set()
    pages_fetched = 0
    for offset in range(pages):
        payload = client.raid_rankings(
            raid=raid,
            difficulty=difficulty,
            region=region,
            realm=realm,
            limit=RAID_RANKINGS_PAGE_SIZE,
            page=page + offset,
        )
        pages_fetched += 1
        rankings = [row for row in as_list(payload.get("raidRankings")) if isinstance(row, dict)]
        for row in rankings:
            guild_key = str(as_dict(row.get("guild")).get("id") or f"{row.get('rank')}:{as_dict(row.get('guild')).get('path')}")
            if guild_key in seen_guilds:
                continue
            seen_guilds.add(guild_key)
            rows.append(raid_ranking_row(row))
        if len(rows) >= limit or len(rankings) < RAID_RANKINGS_PAGE_SIZE:
            break
    return rows[:limit], {
        "sampled_at": datetime.now(UTC).isoformat(),
        "pages_requested": pages,
        "pages_fetched": pages_fetched,
        "cache_ttl_seconds": client.raid_rankings_ttl_seconds,
        "leaderboard_urls": [raid_rankings_url(raid=raid, difficulty=difficulty, region=region, realm=realm)],
    }


def _raid_catalog_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Stable fields of each raid in a ``/raiding/static-data`` response."""
    rows: list[dict[str, Any]] = []
    for raid in as_list(payload.get("raids")):
        if not isinstance(raid, dict):
            continue
        rows.append(
            {
                "id": raid.get("id"),
                "slug": raid.get("slug"),
                "name": raid.get("name"),
                "short_name": raid.get("short_name"),
                "starts": as_dict(raid.get("starts")),
                "ends": as_dict(raid.get("ends")),
                "encounters": [
                    {"id": encounter.get("id"), "slug": encounter.get("slug"), "name": encounter.get("name")}
                    for encounter in as_list(raid.get("encounters"))
                    if isinstance(encounter, dict)
                ],
            }
        )
    return rows


def raid_catalog_payload(payload: dict[str, Any], *, expansion_id: int, cache_ttl_seconds: int) -> dict[str, Any]:
    """The ``raiderio raids`` payload: catalog rows plus where they came from and how fresh they are."""
    rows = _raid_catalog_rows(payload)
    return {
        "query": {"expansion_id": expansion_id},
        "count": len(rows),
        "rows": rows,
        # `sampled_at` is when this command read the catalog, the same meaning the sampled siblings
        # give it: a cache hit can be up to `cache_ttl_seconds` older than that upstream.
        "freshness": {"sampled_at": datetime.now(UTC).isoformat(), "cache_ttl_seconds": cache_ttl_seconds},
        "citations": {"static_data_url": f"{RAIDERIO_BASE_URL}/raiding/static-data?expansion_id={expansion_id}"},
    }

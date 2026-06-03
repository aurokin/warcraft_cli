from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote, urljoin

from bs4 import BeautifulSoup, Tag
from warcraft_core.identity import class_spec_identity_payload, encounter_identity_payload

WOWPROGRESS_BASE_URL = "https://www.wowprogress.com"


def _slug(value: str) -> str:
    return quote(value.strip().replace(" ", "-"), safe="-_.~")


def guild_url(region: str, realm: str, name: str) -> str:
    return f"{WOWPROGRESS_BASE_URL}/guild/{_slug(region.lower())}/{_slug(realm.lower())}/{_slug(name)}"


def character_url(region: str, realm: str, name: str) -> str:
    return f"{WOWPROGRESS_BASE_URL}/character/{_slug(region.lower())}/{_slug(realm.lower())}/{_slug(name)}"


def leaderboard_url(region: str, realm: str | None = None) -> str:
    base = f"{WOWPROGRESS_BASE_URL}/pve/{_slug(region.lower())}"
    if realm:
        return f"{base}/{_slug(realm.lower())}"
    return base


def _clean_text(value: str) -> str:
    cleaned = value.replace("\xa0", " ").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", cleaned).strip()


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


def _absolute_url(href: str) -> str:
    return urljoin(WOWPROGRESS_BASE_URL, href)


def _find_link_after(tag: Tag, pattern: str) -> Tag | None:
    return tag.find_next("a", href=re.compile(pattern))


def _clean_guild_name(raw: str) -> str:
    text = _clean_text(raw)
    text = re.sub(r"\s+Guild$", "", text).strip()
    return text.strip('"').strip()


def _first_header_text(table: Tag) -> str:
    row = table.find("tr")
    if row is None:
        return ""
    headers = row.find_all(["th", "td"])
    return _clean_text(" ".join(cell.get_text(" ", strip=True) for cell in headers))


def _table_rows(table: Tag) -> list[list[Tag]]:
    rows: list[list[Tag]] = []
    for row in table.find_all("tr"):
        cells = row.find_all(["th", "td"], recursive=False)
        if cells:
            rows.append(cells)
    return rows


def _extract_ranks(container: Tag) -> dict[str, str | None]:
    ranks: dict[str, str | None] = {"world": None, "region": None, "realm": None}
    for row in container.find_all("tr"):
        cells = row.find_all("td", recursive=False)
        if len(cells) < 2:
            continue
        label = _clean_text(cells[0].get_text(" ", strip=True)).rstrip(":").strip().lower()
        value = _clean_text(cells[1].get_text(" ", strip=True)) or None
        if label == "world":
            ranks["world"] = value
        elif label in {"us", "eu", "tw", "kr", "cn", "west"}:
            ranks["region"] = value
        elif label == "realm":
            ranks["realm"] = value
    return ranks


def _metric_text(container: Tag, prefix: str) -> str | None:
    for child in container.find_all(["div", "a", "strong", "span"], recursive=False):
        text = _clean_text(child.get_text(" ", strip=True))
        if text.startswith(prefix):
            return text
    text = _clean_text(container.get_text(" ", strip=True))
    match = re.search(rf"{re.escape(prefix)}\s*([^:]+?)(?:\s+(?:World|US|EU|West|realm)\s*:|$)", text, re.IGNORECASE)
    if match:
        return f"{prefix} {match.group(1).strip()}".strip()
    return None


def _extract_metric_value(metric_text: str | None, prefix: str) -> str | None:
    if not metric_text:
        return None
    value = metric_text.removeprefix(prefix).strip()
    return value or None


def _find_table_by_header(soup: BeautifulSoup, expected_headers: tuple[str, ...]) -> Tag | None:
    wanted = tuple(header.lower() for header in expected_headers)
    for table in soup.find_all("table"):
        row = table.find("tr")
        if row is None:
            continue
        headers = [_clean_text(cell.get_text(" ", strip=True)).lower() for cell in row.find_all(["th", "td"])]
        if all(any(header == wanted_value for header in headers) for wanted_value in wanted):
            return table
    return None


def _parse_progress_table(table: Tag) -> tuple[dict[str, Any], dict[str, Any]]:
    row = table.find("tr")
    if row is None:
        return {}, {}
    cells = row.find_all("td", recursive=False)
    if len(cells) < 2:
        return {}, {}
    progress_text = _metric_text(cells[0], "Progress:")
    item_level_text = _metric_text(cells[1], "Item Level:")
    item_match = re.match(r"Item Level:\s*([0-9.]+)(?:\s*\(([^)]+)\))?", item_level_text or "")
    progress_summary = _extract_metric_value(progress_text, "Progress:")
    progress_match = re.search(r"([0-9]+/[0-9]+\s*\([^)]+\)|[0-9]+/[0-9]+)", progress_summary or "")
    progress = {
        "summary": progress_match.group(1) if progress_match else progress_summary,
        "ranks": _extract_ranks(cells[0]),
    }
    item_level = {
        "average": float(item_match.group(1)) if item_match else None,
        "group_size": item_match.group(2) if item_match else None,
        "ranks": _extract_ranks(cells[1]),
    }
    return progress, item_level


def _tier_key_from_url(url: str) -> str | None:
    match = re.search(r"/rating\.(tier[0-9]+)", url)
    if match is None:
        return None
    return match.group(1)


def _guild_history_links(soup: BeautifulSoup, *, current_url: str) -> list[dict[str, Any]]:
    links: list[dict[str, Any]] = []
    seen: set[str] = set()
    for tag in soup.find_all("a", href=re.compile(r"/guild/.+/rating\.tier[0-9]+")):
        href = str(tag.get("href") or "").strip()
        page_url = _absolute_url(href)
        if page_url in seen:
            continue
        seen.add(page_url)
        tier_key = _tier_key_from_url(page_url)
        if tier_key is None:
            continue
        links.append(
            {
                "tier_key": tier_key,
                "raid": _clean_text(tag.get_text(" ", strip=True)) or tier_key,
                "page_url": page_url,
                "current": page_url.rstrip("/") == current_url.rstrip("/"),
            }
        )
    if links and not any(link.get("current") for link in links):
        links[0]["current"] = True
    return links


def _parse_guild_encounters(table: Tag) -> list[dict[str, Any]]:
    rows = _table_rows(table)
    data_rows = [cells for cells in rows[1:] if len(cells) >= 7]
    if not data_rows:
        direct_cells = [child for child in table.children if isinstance(child, Tag) and child.name == "td" and child.get("colspan") is None]
        data_rows = [direct_cells[index: index + 7] for index in range(0, len(direct_cells), 7) if len(direct_cells[index: index + 7]) == 7]
    encounters: list[dict[str, Any]] = []
    for cells in data_rows:
        detail_link = cells[0].find("a", href=True)
        if detail_link is None:
            continue
        raw_name = _clean_text(detail_link.get_text(" ", strip=True)).lstrip("+").strip()
        difficulty = None
        encounter_name = raw_name
        if ": " in raw_name:
            prefix, rest = raw_name.split(": ", 1)
            difficulty = prefix.strip()
            encounter_name = rest.strip()
        video_links = [_absolute_url(link["href"]) for link in cells[2].find_all("a", href=True)]
        encounters.append(
            {
                "encounter": encounter_name,
                "difficulty": difficulty,
                "first_kill_at": _clean_text(cells[1].get_text(" ", strip=True)) or None,
                "world_rank": _clean_text(cells[3].get_text(" ", strip=True)) or None,
                "region_rank": _clean_text(cells[4].get_text(" ", strip=True)) or None,
                "realm_rank": _clean_text(cells[5].get_text(" ", strip=True)) or None,
                "fastest_kill": _clean_text(cells[6].get_text(" ", strip=True)) or None,
                "detail_url": _absolute_url(detail_link["href"]),
                "video_count": len(video_links),
                "video_urls": video_links,
                # WowProgress exposes only the encounter name (no stable encounter/journal id),
                # so identity is name-only -> normalized.
                "encounter_identity": encounter_identity_payload(
                    encounter_id=None,
                    name=encounter_name,
                    provider="wowprogress",
                    source="guild_encounter_row",
                ),
            }
        )
    return encounters


def parse_guild_page(html: str, *, url: str, region: str, realm: str, name: str) -> dict[str, Any]:
    soup = _soup(html)
    heading = soup.find("h1")
    if heading is None:
        raise ValueError("Missing guild heading.")
    guild_name = _clean_guild_name(heading.get_text(" ", strip=True)) or name
    faction = None
    faction_tag = heading.find_next("strong")
    if faction_tag is not None:
        faction = _clean_text(faction_tag.get_text(" ", strip=True)) or None
    realm_link = _find_link_after(heading, r"^/pve/")
    armory_link = _find_link_after(heading, r"(battle\.net|worldofwarcraft\.com)")
    history_links = _guild_history_links(soup, current_url=url)
    progress_table = next((table for table in soup.find_all("table") if "Progress:" in table.get_text(
        " ", strip=True) and "Item Level:" in table.get_text(" ", strip=True)), None)
    progress, item_level = _parse_progress_table(progress_table) if progress_table is not None else ({}, {})
    current_history = next((link for link in history_links if link.get("current")), None)
    if current_history is not None:
        progress["raid"] = current_history.get("raid")
        progress["tier_key"] = current_history.get("tier_key")
    encounters_table = _find_table_by_header(soup, ("Encounter", "First Kill", "World", "Realm"))
    encounters = _parse_guild_encounters(encounters_table) if encounters_table is not None else []
    return {
        "guild": {
            "name": guild_name,
            "region": region.lower(),
            "realm": _clean_text(realm_link.get_text(" ", strip=True)) if realm_link is not None else realm,
            "faction": faction,
            "page_url": url,
            "armory_url": armory_link.get("href") if armory_link is not None else None,
        },
        "progress": progress,
        "item_level": item_level,
        "encounters": {
            "count": len(encounters),
            "items": encounters,
        },
        "history_links": history_links,
        "citations": {
            "page": url,
        },
    }


# WoW has exactly two two-word classes; every other class is a single token. Without this the
# trailing token alone is taken as the class ("Death Knight" -> "Knight", "Demon Hunter" -> "Hunter"),
# which also leaked the wrong token into the raw class_name field.
_TWO_WORD_CLASSES = {("death", "knight"), ("demon", "hunter")}


def _parse_character_summary(text: str) -> tuple[str | None, str | None, int | None]:
    # _clean_text always returns a str (empty for whitespace-only input), so .split() is safe and a
    # summary-less page yields [] -> the unknown (None, None, None) tuple rather than raising.
    tokens = _clean_text(text).split()
    if len(tokens) < 3:
        return None, None, None
    level = int(tokens[-1]) if tokens[-1].isdigit() else None
    if level is None:
        return None, None, None
    if len(tokens) >= 4 and (tokens[-3].lower(), tokens[-2].lower()) in _TWO_WORD_CLASSES:
        class_name = f"{tokens[-3]} {tokens[-2]}".title()
        race = " ".join(tokens[:-3]).title()
    else:
        class_name = tokens[-2].title()
        race = " ".join(tokens[:-2]).title()
    return race or None, class_name or None, level


def _extract_detail_field(block_text: str, label: str) -> str | None:
    match = re.search(rf"{re.escape(label)}\s*([^:]+?)(?=\s+[A-Z][a-zA-Z ]+:|$)", block_text)
    if not match:
        return None
    value = _clean_text(match.group(1))
    return value or None


def _parse_character_metric_table(table: Tag, prefix: str) -> dict[str, Any]:
    row = table.find("tr")
    if row is None:
        return {}
    cells = row.find_all("td", recursive=False)
    if not cells:
        return {}
    metric_text = _metric_text(cells[0], prefix)
    text = _clean_text(cells[0].get_text(" ", strip=True))
    payload: dict[str, Any] = {"ranks": _extract_ranks(cells[0])}
    if prefix == "Item Level:":
        match = re.match(r"Item Level:\s*([0-9.]+)", metric_text or "")
        payload["value"] = float(match.group(1)) if match else None
    elif prefix == "SimDPS:":
        match = re.match(r"SimDPS:\s*([0-9.]+)", metric_text or "")
        payload["value"] = float(match.group(1)) if match else None
        for label, key in (("calculated:", "calculated"), ("version:", "version"), ("spec:", "spec")):
            found = _extract_detail_field(text, label)
            if found is not None:
                payload[key] = found
    return payload


def _parse_character_raid_tables(soup: BeautifulSoup) -> list[dict[str, Any]]:
    raids: list[dict[str, Any]] = []
    for table in soup.find_all("table"):
        rows = _table_rows(table)
        if not rows:
            continue
        first_header = _clean_text(rows[0][0].get_text(" ", strip=True))
        if not first_header.endswith("Bosses"):
            continue
        raid_name = first_header.removesuffix(" Bosses").strip()
        bosses: list[dict[str, Any]] = []
        for cells in rows[1:]:
            if len(cells) < 2:
                continue
            boss_name = _clean_text(cells[0].get_text(" ", strip=True))
            if not boss_name:
                continue
            boss_row = {
                "boss": boss_name,
                "first_seen_kill": _clean_text(cells[1].get_text(" ", strip=True)) or None,
            }
            if len(cells) >= 3:
                score = _clean_text(cells[2].get_text(" ", strip=True)) or None
                if score is not None:
                    boss_row["score"] = score
            bosses.append(boss_row)
        if bosses:
            raids.append({"raid": raid_name, "bosses": bosses})
    return raids


def parse_character_page(html: str, *, url: str, region: str, realm: str, name: str) -> dict[str, Any]:
    soup = _soup(html)
    heading = soup.find("h1")
    if heading is None:
        raise ValueError("Missing character heading.")
    character_name = _clean_text(heading.get_text(" ", strip=True)) or name
    header_container = heading.parent if heading.parent is not None else soup
    realm_link = header_container.find("a", href=re.compile(r"^/gearscore/"))
    guild_link = header_container.find("a", href=re.compile(r"^/guild/"))
    armory_link = header_container.find("a", href=re.compile(r"(battle\.net|worldofwarcraft\.com)"))
    header_text = _clean_text(header_container.get_text(" ", strip=True))
    summary_text = header_text
    for raw in (
        character_name,
        _clean_text(realm_link.get_text(" ", strip=True)) if realm_link else "",
        _clean_text(guild_link.get_text(" ", strip=True)) if guild_link else "",
        "(armory)",
    ):
        if raw:
            summary_text = summary_text.replace(raw, " ")
    race, class_name, level = _parse_character_summary(summary_text)
    detail_block = _clean_text(soup.get_text(" ", strip=True))
    item_level_table = next((table for table in soup.find_all("table") if "Item Level:" in table.get_text(" ", strip=True)), None)
    sim_dps_table = next((table for table in soup.find_all("table") if "SimDPS:" in table.get_text(" ", strip=True)), None)
    pve_heading = next((h for h in soup.find_all("h2") if _clean_text(h.get_text(" ", strip=True)).startswith("PvE Score:")), None)
    pve_match = re.search(r"PvE Score:\s*([0-9.]+)", _clean_text(pve_heading.get_text(" ", strip=True)) if pve_heading is not None else "")
    raids = _parse_character_raid_tables(soup)
    sim_dps = _parse_character_metric_table(sim_dps_table, "SimDPS:") if sim_dps_table is not None else {}
    sim_dps_spec = sim_dps.get("spec") if isinstance(sim_dps, dict) else None
    return {
        "character": {
            "name": character_name,
            "region": region.lower(),
            "realm": _clean_text(realm_link.get_text(" ", strip=True)) if realm_link is not None else realm,
            "guild_name": _clean_text(guild_link.get_text(" ", strip=True)) if guild_link is not None else None,
            "guild_url": _absolute_url(str(guild_link["href"])) if guild_link is not None else None,
            "race": race,
            "class_name": class_name,
            "level": level,
            "page_url": url,
            "armory_url": armory_link.get("href") if armory_link is not None else None,
            # Class is parsed from free header text and the spec (when present) comes from the
            # SimDPS table, so this is normalized-without-confidence rather than a source-backed pair.
            "class_spec_identity": class_spec_identity_payload(
                actor_class=class_name,
                spec=sim_dps_spec,
                provider="wowprogress",
                source="character_page",
                confidence="none",
            ),
        },
        "profile": {
            "languages": _extract_detail_field(detail_block, "Languages:"),
            "looking_for_guild": _extract_detail_field(detail_block, "Looking for guild:"),
            "raids_per_week": _extract_detail_field(detail_block, "Raids per week:"),
            "mythic_plus_dungeons": _extract_detail_field(detail_block, "Mythic Plus Dungeons:"),
            "specs_playing": _extract_detail_field(detail_block, "Specs playing:"),
        },
        "item_level": _parse_character_metric_table(item_level_table, "Item Level:") if item_level_table is not None else {},
        "sim_dps": sim_dps,
        "pve": {
            "score": float(pve_match.group(1)) if pve_match else None,
            "raids": raids,
        },
        "citations": {
            "page": url,
        },
    }


def parse_pve_leaderboard_page(
    html: str,
    *,
    url: str,
    region: str,
    realm: str | None,
    limit: int,
) -> dict[str, Any]:
    soup = _soup(html)
    heading = soup.find("h1")
    title = _clean_text(heading.get_text(" ", strip=True)) if heading is not None else "Mythic Progress"
    active_raid = None
    for header in soup.find_all("h2"):
        text = _clean_text(header.get_text(" ", strip=True))
        if text.startswith("Mythic "):
            active_raid = text.removeprefix("Mythic ").strip()
            break
    table = _find_table_by_header(soup, ("Guild", "Realm", "Progress"))
    entries: list[dict[str, Any]] = []
    if table is not None:
        for cells in _table_rows(table)[1:]:
            if len(cells) < 4:
                continue
            rank_text = _clean_text(cells[0].get_text(" ", strip=True))
            if not rank_text.isdigit():
                continue
            guild_link = cells[1].find("a", href=True)
            realm_link = cells[2].find("a", href=True)
            if guild_link is None:
                continue
            progress_text = _clean_text(cells[3].get_text(" ", strip=True)) or None
            progress_match = re.search(r"([0-9]+/[0-9]+\s*\([^)]+\)|[0-9]+/[0-9]+)", progress_text or "")
            entries.append(
                {
                    "rank": int(rank_text),
                    "guild_name": _clean_text(guild_link.get_text(" ", strip=True)),
                    "guild_url": _absolute_url(guild_link["href"]),
                    "realm": _clean_text(cells[2].get_text(" ", strip=True)) or None,
                    "realm_url": _absolute_url(realm_link["href"]) if realm_link is not None else None,
                    "progress": progress_match.group(1) if progress_match else progress_text,
                }
            )
            if len(entries) >= limit:
                break
    return {
        "leaderboard": {
            "kind": "pve",
            "title": title,
            "region": region.lower(),
            "realm": realm.lower() if realm else None,
            "active_raid": active_raid,
            "page_url": url,
        },
        "count": len(entries),
        "entries": entries,
        "citations": {
            "page": url,
        },
    }

from __future__ import annotations

from collections.abc import Iterable
from typing import Any
from urllib.parse import quote, unquote, urljoin, urlparse

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

WIKI_BASE_URL = "https://warcraft.wiki.gg"

PROGRAMMING_FRAMEWORK_TITLES = {
    "world of warcraft api",
    "warcraft wiki:api",
    "widget api",
    "widget script handlers",
    "framexml functions",
    "framexml api",
    "lua functions",
    "lua api",
    "xml schema",
    "console variables",
    "events",
    "hyperlinks",
    "macro commands",
    "ui escape sequences",
    "warcraft wiki:interface customization",
    "user interface customization guide",
    "ui faq/addon author resources",
    "howtos",
}

PROGRAMMING_HOWTO_TITLES = {
    "ace3 for dummies",
    "create a wow addon in 15 minutes",
    "creating a slash command",
    "handling events",
    "introduction to lua",
    "saving variables between game sessions",
    "using the addon namespace",
    "using the interface options addons panel",
    "warcraft wiki:interface customization",
    "user interface customization guide",
    "ui faq/addon author resources",
    "howtos",
}

SYSTEM_REFERENCE_TITLES = {
    "renown",
    "zone scaling",
    "housing",
    "profession",
    "expansion",
}

PROFESSION_REFERENCE_TITLES = {
    "alchemy",
    "archaeology",
    "blacksmithing",
    "cooking",
    "enchanting",
    "engineering",
    "fishing",
    "first aid",
    "herbalism",
    "inscription",
    "jewelcrafting",
    "leatherworking",
    "mining",
    "skinning",
    "tailoring",
}

EXPANSION_REFERENCE_TITLES = {
    "world of warcraft",
    "world of warcraft: the burning crusade",
    "world of warcraft: wrath of the lich king",
    "world of warcraft: cataclysm",
    "world of warcraft: mists of pandaria",
    "world of warcraft: warlords of draenor",
    "world of warcraft: legion",
    "world of warcraft: battle for azeroth",
    "world of warcraft: shadowlands",
    "world of warcraft: dragonflight",
    "world of warcraft: the war within",
    "world of warcraft: midnight",
}

CLASS_REFERENCE_TITLES = {
    "death knight",
    "demon hunter",
    "druid",
    "evoker",
    "hunter",
    "mage",
    "monk",
    "paladin",
    "priest",
    "rogue",
    "shaman",
    "warlock",
    "warrior",
}


def normalize_article_ref(article_ref: str) -> str:
    value = article_ref.strip()
    if value.startswith("http://") or value.startswith("https://"):
        parsed = urlparse(value)
        marker = "/wiki/"
        if marker in parsed.path:
            title = parsed.path.split(marker, 1)[1]
            value = unquote(title).replace("_", " ")
    elif value.startswith("/wiki/"):
        value = unquote(value.split("/wiki/", 1)[1]).replace("_", " ")
    return value.replace("_", " ").strip()


def article_slug(title: str) -> str:
    value = normalize_article_ref(title).lower()
    chars: list[str] = []
    previous_dash = False
    for ch in value:
        if ch.isalnum():
            chars.append(ch)
            previous_dash = False
            continue
        if not previous_dash:
            chars.append("-")
            previous_dash = True
    return "".join(chars).strip("-") or "article"


def article_url(title: str) -> str:
    normalized = normalize_article_ref(title).replace(" ", "_")
    return f"{WIKI_BASE_URL}/wiki/{quote(normalized, safe='/:()')}"


def _normalized_title_key(title: str) -> str:
    return normalize_article_ref(title).strip().lower()


def classify_article_family(title: str) -> str:
    normalized = _normalized_title_key(title)
    if normalized.startswith("api change summaries"):
        return "api_changes"
    if normalized.endswith("/api changes") or normalized == "api change summaries":
        return "api_changes"
    if normalized.startswith("api "):
        return "api_function"
    if normalized.startswith("uihandler "):
        return "ui_handler"
    if normalized in PROGRAMMING_HOWTO_TITLES:
        return "howto_programming"
    if normalized.startswith("patch ") and ("api changes" not in normalized):
        return "patch_reference"
    if normalized in PROGRAMMING_FRAMEWORK_TITLES:
        if normalized == "xml schema":
            return "xml_schema"
        if normalized == "console variables":
            return "cvar"
        return "framework_page"
    if normalized in CLASS_REFERENCE_TITLES:
        return "class_reference"
    if normalized in PROFESSION_REFERENCE_TITLES:
        return "profession_reference"
    if normalized in EXPANSION_REFERENCE_TITLES:
        return "expansion_reference"
    if normalized in SYSTEM_REFERENCE_TITLES:
        if normalized == "expansion":
            return "expansion_reference"
        if normalized == "profession":
            return "profession_reference"
        if normalized == "zone scaling":
            return "zone_reference"
        return "system_reference"
    if normalized.startswith("world of warcraft:") or normalized.startswith("warcraft:"):
        return "lore_reference"
    if "guide" in normalized or "howto" in normalized or "tutorial" in normalized:
        return "guide_reference"
    return "general_article"


def _strip_html(html_text: str) -> str:
    soup = BeautifulSoup(html_text, "html.parser")
    return soup.get_text(" ", strip=True)


def parse_search_results(payload: dict[str, Any]) -> tuple[int, list[dict[str, Any]]]:
    query = payload.get("query") if isinstance(payload.get("query"), dict) else {}
    search_info = query.get("searchinfo") if isinstance(query.get("searchinfo"), dict) else {}
    total_hits = int(search_info.get("totalhits") or 0)
    rows: list[dict[str, Any]] = []
    for row in query.get("search") or []:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "").strip()
        if not title:
            continue
        snippet = _strip_html(str(row.get("snippet") or ""))
        rows.append(
            {
                "title": title,
                "pageid": row.get("pageid"),
                "timestamp": row.get("timestamp"),
                "snippet": snippet,
                "url": article_url(title),
            }
        )
    return total_hits, rows


def _heading_title(tag: Tag) -> str:
    headline = tag.find(class_="mw-headline")
    if headline is not None:
        return headline.get_text(" ", strip=True)
    return tag.get_text(" ", strip=True)


def _heading_anchor(tag: Tag, *, fallback_title: str) -> str:
    headline = tag.find(class_="mw-headline")
    if headline is not None and headline.get("id"):
        return str(headline["id"])
    if tag.get("id"):
        return str(tag["id"])
    return fallback_title.replace(" ", "_")


def _iter_root_children(root: Tag) -> Iterable[Tag | NavigableString]:
    for child in root.children:
        if isinstance(child, NavigableString):
            if child.strip():
                yield child
            continue
        if isinstance(child, Tag):
            yield child


def _render_nodes(nodes: list[Tag | NavigableString]) -> tuple[str, str]:
    html = "".join(str(node) for node in nodes).strip()
    if not html:
        return "", ""
    soup = BeautifulSoup(html, "html.parser")
    return html, soup.get_text(" ", strip=True)


def _extract_sections(root: Tag) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    sections: list[dict[str, Any]] = []
    headings: list[dict[str, Any]] = []
    current_title = "Introduction"
    current_anchor = "Introduction"
    current_level = 1
    current_nodes: list[Tag | NavigableString] = []
    ordinal = 1

    def flush() -> None:
        nonlocal ordinal, current_nodes
        html, text = _render_nodes(current_nodes)
        if not text:
            current_nodes = []
            return
        sections.append(
            {
                "title": current_title,
                "level": current_level,
                "ordinal": ordinal,
                "anchor": current_anchor,
                "text": text,
                "html": html,
            }
        )
        ordinal += 1
        current_nodes = []

    for child in _iter_root_children(root):
        if isinstance(child, Tag) and child.name in {"h2", "h3", "h4", "h5", "h6"}:
            flush()
            current_title = _heading_title(child)
            current_anchor = _heading_anchor(child, fallback_title=current_title)
            current_level = int(child.name[1])
            headings.append(
                {
                    "title": current_title,
                    "level": current_level,
                    "ordinal": len(headings) + 1,
                    "anchor": current_anchor,
                }
            )
            continue
        current_nodes.append(child)
    flush()
    return headings, sections


def _extract_linked_entities(root: Tag) -> list[dict[str, Any]]:
    entities: dict[str, dict[str, Any]] = {}
    for link in root.find_all("a", href=True):
        href = str(link.get("href") or "")
        if not href.startswith("/wiki/"):
            continue
        if "action=edit" in href or "veaction=edit" in href:
            continue
        if href.startswith("/wiki/File:") or href.startswith("/wiki/Category:") or href.startswith("/wiki/Special:"):
            continue
        if href.startswith("/wiki/Help:") or href.startswith("/wiki/Template:"):
            continue
        full_url = urljoin(WIKI_BASE_URL, href)
        if "#" in full_url:
            full_url = full_url.split("#", 1)[0]
        title = normalize_article_ref(href)
        entities[title] = {
            "type": "wiki_article",
            "id": title,
            "name": link.get_text(" ", strip=True) or title,
            "url": full_url,
        }
    return sorted(entities.values(), key=lambda row: str(row["id"]).lower())


def _clean_root(root: Tag, *, family: str) -> Tag:
    cleaned = BeautifulSoup(str(root), "html.parser")
    output = cleaned.select_one(".mw-parser-output") or cleaned
    for comment in output.find_all(string=lambda value: isinstance(value, Comment)):
        comment.extract()
    for selector in (".mw-editsection", ".toc", ".noprint", ".mw-empty-elt", "style", "script"):
        for tag in output.select(selector):
            tag.decompose()
    for selector in (".navbox", ".vertical-navbox", ".infobox", ".catlinks", ".mw-hidden-catlinks"):
        for tag in output.select(selector):
            tag.decompose()
    if family in {"api_function", "ui_handler", "framework_page", "xml_schema", "cvar", "api_changes", "howto_programming"}:
        for tag in output.select(".nomobile, .thumb, .gallery, .mw-references-wrap"):
            tag.decompose()
        children = [child for child in output.children if isinstance(child, Tag)]
        for child in children[:3]:
            text = child.get_text(" ", strip=True)
            normalized = text.lower()
            if (
                "main menu" in normalized
                or ("game types" in normalized and "wowprogramming" in normalized)
                or ("wow api" in normalized and "framexml api" in normalized)
            ):
                child.decompose()
    return output


def _refine_article_family(*, title: str, family: str, headings: list[dict[str, Any]], sections: list[dict[str, Any]]) -> str:
    if family != "general_article":
        return family
    normalized = _normalized_title_key(title)
    section_titles = {str(section.get("title") or "").strip().lower() for section in sections}
    heading_titles = {str(heading.get("title") or "").strip().lower() for heading in headings}
    titles = section_titles | heading_titles
    if "biography" in titles:
        return "lore_reference"
    if "reputation" in titles and ({"members", "organization", "history"} & titles):
        return "faction_reference"
    zone_markers = {
        "geography",
        "maps and subregions",
        "adjacent regions",
        "quest and travel hubs",
        "notable characters",
        "battle pets",
        "wild creatures",
    }
    if len(zone_markers & titles) >= 2:
        return "zone_reference"
    if normalized in PROFESSION_REFERENCE_TITLES and ({"official overview", "alchemy training", "patch changes", "see also"} & titles):
        return "profession_reference"
    if normalized.startswith("world of warcraft:") and ({"features", "new zones", "dungeons and raids"} & titles):
        return "expansion_reference"
    if normalized == "faction":
        return "faction_reference"
    return family


def _section_lookup(sections: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    lookup: dict[str, dict[str, Any]] = {}
    for section in sections:
        title = str(section.get("title") or "").strip().lower().replace(" ", "_")
        if title:
            lookup[title] = section
    return lookup


def _first_code_block_text(root: Tag) -> str | None:
    block = root.select_one(".mw-highlight")
    if block is None:
        return None
    text = block.get_text(" ", strip=True)
    return text or None


def extract_reference_metadata(*, title: str, family: str, text: str, sections: list[dict[str, Any]], root: Tag) -> dict[str, Any]:
    metadata: dict[str, Any] = {"content_family": family}
    section_map = _section_lookup(sections)
    metadata["summary"] = sections[0]["text"] if sections else text[:240]
    metadata["example"] = section_map.get("example", {}).get("text")
    metadata["patch_changes"] = section_map.get("patch_changes", {}).get("text")
    metadata["see_also"] = section_map.get("see_also", {}).get("text")
    metadata["references"] = section_map.get("references", {}).get("text")
    if family not in {"api_function", "ui_handler", "framework_page", "xml_schema", "cvar", "api_changes", "howto_programming"}:
        return metadata
    metadata["programming_reference"] = True
    metadata["signature"] = _first_code_block_text(root)
    metadata["arguments"] = section_map.get("arguments", {}).get("text")
    metadata["returns"] = section_map.get("returns", {}).get("text")
    metadata["details"] = section_map.get("details", {}).get("text")
    return metadata


def parse_article_page(payload: dict[str, Any], *, source_title: str) -> dict[str, Any]:
    parse = payload.get("parse") if isinstance(payload.get("parse"), dict) else {}
    title = str(parse.get("title") or source_title).strip()
    display_title = _strip_html(str(parse.get("displaytitle") or title))
    html = str((parse.get("text") or {}).get("*") or "")
    soup = BeautifulSoup(f"<div>{html}</div>", "html.parser")
    root = soup.select_one(".mw-parser-output")
    if root is None:
        root = soup.div if soup.div is not None else soup
    family = classify_article_family(title)
    root = _clean_root(root, family=family)
    headings, sections = _extract_sections(root)
    family = _refine_article_family(title=title, family=family, headings=headings, sections=sections)
    text = root.get_text(" ", strip=True)
    navigation = [
        {
            "title": _strip_html(str(row.get("line") or "")),
            "url": f"{article_url(title)}#{row.get('anchor')}",
            "section_slug": str(row.get("anchor") or ""),
            "active": True,
            "ordinal": index + 1,
        }
        for index, row in enumerate(parse.get("sections") or [])
        if isinstance(row, dict) and row.get("line") and row.get("anchor")
    ]
    return {
        "article": {
            "title": title,
            "slug": article_slug(title),
            "display_title": display_title,
            "page_url": article_url(title),
            "section_slug": article_slug(title),
            "section_title": display_title or title,
            "page_count": 1,
            "content_family": family,
        },
        "page": {
            "title": display_title or title,
            "description": sections[0]["text"][:240] if sections else text[:240],
            "canonical_url": article_url(title),
        },
        "navigation": {
            "count": len(navigation),
            "items": navigation,
        },
        "article_content": {
            "html": html,
            "text": text,
            "headings": headings,
            "sections": sections,
        },
        "reference": extract_reference_metadata(title=title, family=family, text=text, sections=sections, root=root),
        "linked_entities": _extract_linked_entities(root),
        "citations": {
            "page": article_url(title),
        },
    }

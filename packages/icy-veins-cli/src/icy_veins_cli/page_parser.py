from __future__ import annotations

import json
import re
from html import unescape
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag
from warcraft_core.identity import ability_identity_payload, build_reference_payload

ICY_VEINS_BASE_URL = "https://www.icy-veins.com"
WOWHEAD_LINK_RE = re.compile(
    r"^(?P<entity_type>achievement|currency|faction|item|mount|npc|object|pet|quest|spell|zone)=(?P<id>\d+)(?:/|$)"
)
CLASS_HUB_SLUGS = {
    "death-knight-guide",
    "demon-hunter-guide",
    "druid-guide",
    "evoker-guide",
    "hunter-guide",
    "mage-guide",
    "monk-guide",
    "paladin-guide",
    "priest-guide",
    "rogue-guide",
    "shaman-guide",
    "warlock-guide",
    "warrior-guide",
}
ROLE_GUIDE_SLUGS = {
    "healing-guide",
}
GUIDE_KEYWORDS = (
    "guide",
    "easy-mode",
    "spec-builds-talents",
    "rotation-cooldowns-abilities",
    "stat-priority",
    "gems-enchants-consumables",
    "gear-best-in-slot",
    "spell-summary",
    "resources",
    "mythic-plus-tips",
    "macros-addons",
    "simulations",
    "leveling",
    "pvp",
)
DISPLAY_TOKEN_MAP = {
    "pve": "PvE",
    "pvp": "PvP",
    "dps": "DPS",
    "bis": "BiS",
    "ui": "UI",
    "mythic": "Mythic",
    "plus": "Plus",
}
SUBPAGE_SUFFIX_FAMILIES = (
    ("-spec-builds-talents", "spec_builds_talents"),
    ("-rotation-cooldowns-abilities", "rotation_guide"),
    ("-stat-priority", "stat_priority"),
    ("-gems-enchants-consumables", "gems_enchants_consumables"),
    ("-gear-best-in-slot", "gear_best_in_slot"),
    ("-spell-summary", "spell_summary"),
    ("-resources", "resources"),
    ("-mythic-plus-tips", "mythic_plus_tips"),
    ("-macros-addons", "macros_addons"),
    ("-simulations", "simulations"),
)
SPECIAL_EVENT_KEYWORDS = (
    "remix-guide",
    "torghast-guide",
)


def clean_text(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    text = unescape(re.sub(r"\s+", " ", value)).strip()
    return text or None


def _strip_heading_prefix(title: str) -> str:
    return re.sub(r"^\d+(?:\.\d+)*\.\s*", "", title).strip()


def guide_ref_parts(guide_ref: str) -> str:
    raw = guide_ref.strip()
    if not raw:
        raise ValueError("Guide reference cannot be empty.")
    if raw.startswith("http://") or raw.startswith("https://"):
        parsed = urlparse(raw)
        path = parsed.path
    else:
        path = raw if raw.startswith("/") else f"/wow/{raw}"
    match = re.match(r"^/wow/(?P<slug>[^/?#]+)/?$", path)
    if not match:
        raise ValueError(f"Unsupported Icy Veins guide reference: {guide_ref}")
    return match.group("slug")


def guide_url(slug: str) -> str:
    return f"{ICY_VEINS_BASE_URL}/wow/{slug}"


def slug_display_name(slug: str) -> str:
    parts = [part for part in slug.split("-") if part]
    tokens: list[str] = []
    for part in parts:
        replacement = DISPLAY_TOKEN_MAP.get(part.lower())
        tokens.append(replacement if replacement is not None else part.capitalize())
    return " ".join(tokens) or slug


def classify_guide_slug(slug: str) -> str | None:
    normalized = slug.strip().lower()
    if not normalized:
        return None
    if normalized in CLASS_HUB_SLUGS:
        return "class_hub"
    if normalized in ROLE_GUIDE_SLUGS:
        return "role_guide"
    if normalized.endswith("-easy-mode"):
        return "easy_mode"
    if normalized.endswith("-leveling-guide"):
        return "leveling"
    if "-pvp-guide" in normalized:
        return "pvp"
    for suffix, family in SUBPAGE_SUFFIX_FAMILIES:
        if normalized.endswith(suffix):
            return family
    if normalized.endswith("-raid-guide"):
        return "raid_guide"
    if normalized.endswith("-the-war-within-pve-guide"):
        return "expansion_guide"
    if any(keyword in normalized for keyword in SPECIAL_EVENT_KEYWORDS):
        return "special_event_guide"
    if normalized.endswith("-guide"):
        return "spec_guide"
    return None


def is_supported_guide_slug(slug: str) -> bool:
    return classify_guide_slug(slug) is not None


def guide_traversal_scope(content_family: str | None) -> str:
    if content_family in {"class_hub", "role_guide"}:
        return "current_page"
    return "family_navigation"


def _meta_content(soup: BeautifulSoup, **attrs: str) -> str | None:
    tag = soup.find("meta", attrs=attrs)
    if tag is None:
        return None
    return clean_text(tag.get("content"))


def _link_href(soup: BeautifulSoup, **attrs: str) -> str | None:
    tag = soup.find("link", attrs=attrs)
    if tag is None:
        return None
    href = tag.get("href")
    if not isinstance(href, str):
        return None
    href = href.strip()
    return href or None


def _parse_json_ld_article(soup: BeautifulSoup) -> dict[str, Any] | None:
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        text = script.string or script.get_text(strip=True)
        if not text:
            continue
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            continue
        candidates: list[dict[str, Any]] = []
        if isinstance(value, dict):
            candidates = [value]
        elif isinstance(value, list):
            candidates = [row for row in value if isinstance(row, dict)]
        for candidate in candidates:
            if candidate.get("@type") == "Article":
                return candidate
    return None


def _extract_data_layer(soup: BeautifulSoup) -> dict[str, Any]:
    for script in soup.find_all("script"):
        text = script.string or script.get_text()
        if "page_type" not in text or "dataLayer" not in text:
            continue
        match = re.search(r"dataLayer\s*=\s*\[\s*({.*?})\s*\];", text, flags=re.DOTALL)
        if not match:
            continue
        raw_object = match.group(1)
        normalized = re.sub(r"'([^']+)'\s*:", r'"\1":', raw_object)
        normalized = re.sub(r":\s*'([^']*)'", lambda m: ': ' + json.dumps(m.group(1)), normalized)
        try:
            parsed = json.loads(normalized)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}


def _extract_family_navigation(soup: BeautifulSoup, *, current_url: str) -> list[dict[str, Any]]:
    container = soup.select_one(".toc_page_list")
    if not isinstance(container, Tag):
        return []
    current_path = urlparse(current_url).path.rstrip("/")
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    anchors = container.select(".toc_page_center_item .toc_page_list_item a, .toc_page_list_items .toc_page_list_item a")
    for ordinal, anchor in enumerate(anchors, start=1):
        href = anchor.get("href")
        if not isinstance(href, str):
            continue
        url = urljoin(ICY_VEINS_BASE_URL, href)
        if url in seen:
            continue
        seen.add(url)
        title = clean_text(anchor.get_text(" ", strip=True))
        if not title:
            continue
        parent = anchor.parent if isinstance(anchor.parent, Tag) else None
        classes = parent.get("class", []) if isinstance(parent, Tag) else []
        items.append(
            {
                "title": title,
                "url": url,
                "section_slug": guide_ref_parts(url),
                "active": "selected" in classes or urlparse(url).path.rstrip("/") == current_path,
                "ordinal": ordinal,
            }
        )
    return items


def _extract_page_toc(soup: BeautifulSoup, *, current_url: str) -> list[dict[str, Any]]:
    container = soup.select_one(".toc_page_content_items")
    if not isinstance(container, Tag):
        return []
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for ordinal, anchor in enumerate(container.select("a[href]"), start=1):
        href = anchor.get("href")
        if not isinstance(href, str):
            continue
        url = urljoin(current_url, href)
        if url in seen:
            continue
        seen.add(url)
        parsed = urlparse(url)
        title = clean_text(_strip_heading_prefix(anchor.get_text(" ", strip=True)))
        if not title:
            continue
        items.append(
            {
                "title": title,
                "url": url,
                "anchor": parsed.fragment or None,
                "ordinal": ordinal,
            }
        )
    return items


def _extract_intro_text(soup: BeautifulSoup) -> str:
    intro = soup.select_one(".page_content_header_intro")
    if not isinstance(intro, Tag):
        return ""
    text = clean_text(intro.get_text(" ", strip=True))
    return text or ""


def _article_tag(soup: BeautifulSoup) -> Tag | None:
    article = soup.select_one(".page_content_container > .page_content")
    if isinstance(article, Tag):
        return article
    return None


def _clone_article(article: Tag) -> Tag:
    cloned = BeautifulSoup(str(article), "html.parser").find("div", class_="page_content")
    if not isinstance(cloned, Tag):
        raise ValueError("Failed to clone Icy Veins article node.")
    for node in cloned.select(
        "script, style, noscript, .raider-io-links, .hidden_section_controls, .page_content_footer, .toc_mobile"
    ):
        node.decompose()
    return cloned


def _heading_from_tag(tag: Tag) -> tuple[str, int] | None:
    if tag.name in {"h2", "h3", "h4"}:
        title = clean_text(tag.get_text(" ", strip=True))
        if not title:
            return None
        return _strip_heading_prefix(title), int(tag.name[1])
    if tag.name == "div" and "heading_container" in (tag.get("class") or []):
        heading = tag.find(re.compile(r"^h[234]$"))
        if not isinstance(heading, Tag):
            return None
        title = clean_text(heading.get_text(" ", strip=True))
        if not title:
            return None
        return _strip_heading_prefix(title), int(heading.name[1])
    return None


def _extract_headings(article: Tag) -> list[dict[str, Any]]:
    headings: list[dict[str, Any]] = []
    ordinal = 0
    for node in article.find_all(["div", "h2", "h3", "h4"]):
        if not isinstance(node, Tag):
            continue
        if node.name in {"h2", "h3", "h4"} and isinstance(node.parent, Tag) and "heading_container" in (node.parent.get("class") or []):
            continue
        heading = _heading_from_tag(node)
        if heading is None:
            continue
        ordinal += 1
        title, level = heading
        headings.append({"title": title, "level": level, "ordinal": ordinal})
    return headings


def _append_section_content(section: dict[str, Any], node: Any) -> None:
    if not isinstance(node, Tag):
        text = clean_text(str(node))
        if text:
            section["text_parts"].append(text)
        return
    html = str(node).strip()
    text = clean_text(node.get_text(" ", strip=True))
    if html:
        section["html_parts"].append(html)
    if text:
        section["text_parts"].append(text)


def _extract_sections(article: Tag, *, fallback_title: str) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    ordinal = 0
    for child in article.children:
        if not isinstance(child, Tag):
            continue
        heading = _heading_from_tag(child)
        if heading is not None:
            ordinal += 1
            title, level = heading
            current = {
                "title": title,
                "level": level,
                "ordinal": ordinal,
                "html_parts": [],
                "text_parts": [],
            }
            sections.append(current)
            continue
        if current is None:
            ordinal += 1
            current = {
                "title": fallback_title,
                "level": 2,
                "ordinal": ordinal,
                "html_parts": [],
                "text_parts": [],
            }
            sections.append(current)
        _append_section_content(current, child)
    normalized: list[dict[str, Any]] = []
    for section in sections:
        text = clean_text(" ".join(section["text_parts"]))
        html = "\n".join(section["html_parts"]).strip()
        normalized.append(
            {
                "title": section["title"],
                "level": section["level"],
                "ordinal": section["ordinal"],
                "text": text or "",
                "html": html,
            }
        )
    return [section for section in normalized if section["text"] or section["html"]]


def _extract_linked_entities(article: Tag, *, source_url: str) -> list[dict[str, Any]]:
    items: dict[tuple[str, str | int], dict[str, Any]] = {}
    for anchor in article.find_all("a", href=True):
        href = anchor.get("href")
        if not isinstance(href, str):
            continue
        url = urljoin(source_url, href)
        parsed = urlparse(url)
        name = clean_text(anchor.get_text(" ", strip=True))
        if "wowhead.com" in parsed.netloc:
            path = parsed.path.lstrip("/")
            match = WOWHEAD_LINK_RE.match(path)
            if match is None:
                continue
            entity_type = match.group("entity_type")
            entity_id: str | int = int(match.group("id"))
            key = (entity_type, entity_id)
        elif parsed.netloc.endswith("icy-veins.com") and parsed.path.startswith("/wow/"):
            entity_type = "page"
            entity_id = guide_ref_parts(url)
            key = (entity_type, entity_id)
        else:
            continue
        record = items.get(key)
        if record is None:
            row: dict[str, Any] = {
                "type": entity_type,
                "id": entity_id,
                "name": name,
                "url": url,
                "source_url": source_url,
            }
            # Spell links carry a Wowhead spell id, so they get a canonical ability identity.
            # Other entity types (item/npc/page/...) are left unchanged.
            if entity_type == "spell" and isinstance(entity_id, int):
                row["ability_identity"] = ability_identity_payload(
                    spell_id=entity_id,
                    name=name or None,
                    provider="icy-veins",
                    source="guide_linked_entity",
                )
            items[key] = row
            continue
        if not record.get("name") and name:
            record["name"] = name
            if entity_type == "spell" and isinstance(entity_id, int) and isinstance(record.get("ability_identity"), dict):
                record["ability_identity"] = ability_identity_payload(
                    spell_id=entity_id,
                    name=name or None,
                    provider="icy-veins",
                    source="guide_linked_entity",
                )
    return sorted(items.values(), key=lambda row: (row["type"], str(row["id"])))


def _extract_build_references(article: Tag, *, source_url: str) -> list[dict[str, Any]]:
    items: dict[str, dict[str, Any]] = {}
    for anchor in article.find_all("a", href=True):
        href = anchor.get("href")
        if not isinstance(href, str):
            continue
        payload = build_reference_payload(
            ref=urljoin(source_url, href),
            provider="icy-veins",
            source="guide_embedded_link",
            source_url=source_url,
            label=clean_text(anchor.get_text(" ", strip=True)),
            notes=["embedded Icy Veins guide link"],
        )
        if payload is None:
            continue
        items[str(payload["url"])] = payload
    return sorted(items.values(), key=lambda row: str(row["url"]))


def parse_guide_page(html: str, *, source_url: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    canonical_url = _link_href(soup, rel="canonical") or source_url
    canonical_url = urljoin(ICY_VEINS_BASE_URL, canonical_url)
    slug = guide_ref_parts(canonical_url)
    content_family = classify_guide_slug(slug)
    article_json = _parse_json_ld_article(soup) or {}
    data_layer = _extract_data_layer(soup)
    page_title = clean_text(article_json.get("headline")) or _meta_content(soup, property="og:title") or clean_text(
        soup.title.get_text(" ", strip=True) if soup.title else None
    )
    description = clean_text(article_json.get("description")) or _meta_content(soup, name="description")
    author_name = None
    author_value = article_json.get("author")
    if isinstance(author_value, dict):
        author_name = clean_text(author_value.get("name"))
    if author_name is None:
        author_tag = soup.select_one(".page_author span[style]")
        if isinstance(author_tag, Tag):
            author_name = clean_text(author_tag.get_text(" ", strip=True))
    published_at = clean_text(article_json.get("datePublished"))
    last_updated = clean_text(article_json.get("dateModified"))
    if last_updated is None:
        date = clean_text(soup.select_one(".local_date_date").get_text(" ", strip=True) if soup.select_one(".local_date_date") else None)
        hour = clean_text(soup.select_one(".local_date_hour").get_text(" ", strip=True) if soup.select_one(".local_date_hour") else None)
        if date and hour:
            last_updated = f"{date} {hour}"
        elif date:
            last_updated = date
    navigation = _extract_family_navigation(soup, current_url=canonical_url)
    active_nav = next((item for item in navigation if item["active"]), None)
    section_title = active_nav["title"] if active_nav is not None else slug_display_name(slug)
    page_toc = _extract_page_toc(soup, current_url=canonical_url)
    comments_tag = soup.select_one(".page_comments a[href]")
    comments_url = None
    if isinstance(comments_tag, Tag):
        href = comments_tag.get("href")
        if isinstance(href, str):
            comments_url = urljoin(canonical_url, href)
    article_tag = _article_tag(soup)
    article_html = ""
    article_text = ""
    headings: list[dict[str, Any]] = []
    sections: list[dict[str, Any]] = []
    linked_entities: list[dict[str, Any]] = []
    build_references: list[dict[str, Any]] = []
    if article_tag is not None:
        article = _clone_article(article_tag)
        article_html = "".join(str(child) for child in article.contents).strip()
        article_text = clean_text(article.get_text("\n", strip=True)) or ""
        headings = _extract_headings(article)
        sections = _extract_sections(article, fallback_title=section_title)
        linked_entities = _extract_linked_entities(article, source_url=canonical_url)
        build_references = _extract_build_references(article, source_url=canonical_url)
    intro_text = _extract_intro_text(soup)
    return {
        "page": {
            "title": page_title,
            "description": description,
            "canonical_url": canonical_url,
            "page_type": clean_text(str(data_layer.get("page_type"))) if data_layer.get("page_type") is not None else None,
        },
        "guide": {
            "slug": slug,
            "page_url": canonical_url,
            "section_slug": slug,
            "section_title": section_title,
            "content_family": content_family,
            "supported_surface": content_family is not None,
            "traversal_scope": guide_traversal_scope(content_family),
            "author": author_name,
            "last_updated": last_updated,
            "published_at": published_at,
        },
        "navigation": navigation,
        "page_toc": page_toc,
        "article": {
            "html": article_html,
            "text": article_text,
            "intro_text": intro_text,
            "headings": headings,
            "sections": sections,
        },
        "linked_entities": linked_entities,
        "build_references": build_references,
        "citations": {
            "page": canonical_url,
            "comments": comments_url,
        },
    }


def parse_sitemap_guides(xml_text: str) -> list[dict[str, Any]]:
    urls = re.findall(r"<loc>(https://www\.icy-veins\.com/wow/[^<]+)</loc>", xml_text)
    seen: set[str] = set()
    guides: list[dict[str, Any]] = []
    for url in urls:
        try:
            slug = guide_ref_parts(url)
        except ValueError:
            continue
        if slug in seen:
            continue
        content_family = classify_guide_slug(slug)
        if content_family is None and not any(keyword in slug for keyword in GUIDE_KEYWORDS):
            continue
        if content_family is None:
            continue
        seen.add(slug)
        guides.append(
            {
                "slug": slug,
                "name": slug_display_name(slug),
                "url": url,
                "content_family": content_family,
            }
        )
    guides.sort(key=lambda row: row["name"].lower())
    return guides

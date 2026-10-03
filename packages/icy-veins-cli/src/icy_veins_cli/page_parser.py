from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag
from warcraft_content.guide_page import WOWHEAD_LINK_RE, extract_talent_export_builds
from warcraft_content.html_sections import clean_text, extract_headings, extract_sections
from warcraft_content.site_crawler import PageLink, PageRead
from warcraft_core.identity import ability_identity_payload, build_reference_payload
from warcraft_core.wow_specs import WOW_CLASS_NAMES, raiderio_class_slug

ICY_VEINS_BASE_URL = "https://www.icy-veins.com"
GUIDE_PATH_RE = re.compile(r"^/wow/(?P<slug>[^/?#]+)/?$")
SITEMAP_ENTRY_RE = re.compile(r"<loc>(https://www\.icy-veins\.com/wow/[^<]+)</loc>\s*(?:<lastmod>([^<]*)</lastmod>)?")
CLASS_HUB_SLUGS = frozenset(f"{raiderio_class_slug(class_key)}-guide" for class_key in WOW_CLASS_NAMES)
ROLE_GUIDE_SLUGS = {
    "healing-guide",
}
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
# The site-wide guide menu every WoW guide page carries. It links the current season's pages, which
# the sitemap (frozen since 2025-10-05) does not list. Its "View all" links repeat a section's hub
# under a generic title, so a hub's own entry names it.
SITE_MENU_LINK_SELECTOR = 'nav.iv-subnav a[href*="/wow/"]'
SITE_MENU_VIEW_ALL_CLASS = "iv-subnav__view-all"
# A spec's own guide is ``<spec>-<class>-pve-<role>-guide``; every other ``-guide`` page (season hubs,
# dungeon, reputation and event pages) is an ``article_guide``, not a spec guide.
SPEC_GUIDE_RE = re.compile(r"-pve-(?:dps|healing|tank)-guide$")
# A spec's per-expansion page (``mistweaver-monk-the-war-within-pve-guide``) and the expansion hub
# (``midnight-expansion-guide``).
EXPANSION_GUIDE_SUFFIXES = ("-the-war-within-pve-guide", "-expansion-guide")
# A spec's PvP sub-pages (``mistweaver-monk-pvp-talents-and-builds``) belong with its ``-pvp-guide``.
_CLASS_NAMES = "|".join(slug.removesuffix("-guide") for slug in sorted(CLASS_HUB_SLUGS))
SPEC_PVP_SUBPAGE_RE = re.compile(rf"^[a-z-]+?-(?:{_CLASS_NAMES})-pvp-")
# A raid boss page: per-difficulty encounter journals, strategy write-ups, ability and loot lists, and
# the boss guides published inside a raid (``broodtwister-ovi-nax-raid-guide-in-nerub-ar-palace``).
# The current raid's bosses are ``<boss>-raid-guide`` and classify as raid guides.
RAID_ENCOUNTER_RE = re.compile(
    r"-encounter-journal$|-strategy-guide(?:-|$)|-strategy-tactics|-(?:dps|healer|tank)-strategy$|-abilities$"
    r"|-raid-guide-in-|^raid-guide-|-lfr-guide-|-loot$|-loot-in-"
)
# Older raid hubs: ``firelands-raid``, ``aberrus-the-shadowed-crucible-raid-guide-for-dragonflight``,
# ``fated-castle-nathria-raid-guides-in-shadowlands``.
RAID_HUB_RE = re.compile(r"-raid$|-raid-guide-for-|-raid-guides-(?:for|in)-")
DUNGEON_GUIDE_RE = re.compile(r"(?:^|-)dungeons?-guide(?:-tww)?$")
DELVE_GUIDE_RE = re.compile(r"(?:^|-)delve(?:s|rs)?-.*guide(?:-tww)?$")
PROFESSION_RE = re.compile(r"^professions(?:-|$)|-profession-")
# Section hubs (``void-assaults-hub``) and the per-expansion archives (``guides-for-legion``).
HUB_RE = re.compile(r"-hub$|^guides-for-")
# Pages of transmog sets and item models (about 650 in the sitemap). They stay findable for a transmog
# query but rank below everything else (see ``icy_veins_cli.search``); the transmog hubs themselves
# end in ``-guide``/``-guides`` and stay article guides.
TRANSMOG_RE = re.compile(r"^transmogrification-|-transmog(?:-|$)")
# Families whose pages always carry the spec's page switcher: a page of one of these that parses with
# no navigation means the switcher markup moved, not that the guide has a single page.
NAVIGATION_REQUIRED_FAMILIES = frozenset({"spec_guide", *(family for _, family in SUBPAGE_SUFFIX_FAMILIES)})

# Icy Veins rebuilt the WoW guide pages on an Astro layout in 2026: the family switcher moved from
# ``.toc_page_list`` to ``.table-of-contents``, the on-page contents from ``.toc_page_content_items``
# to ``.content-toc``, and the body from ``.page_content`` to ``.guide-page-content``. Each layout
# below lists the new selector first and keeps the legacy one so captured pre-redesign pages (and
# any page the site has not migrated yet) still parse.
FAMILY_NAVIGATION_LAYOUTS = (
    (".table-of-contents", "nav a[href]"),
    (".toc_page_list", ".toc_page_center_item .toc_page_list_item a, .toc_page_list_items .toc_page_list_item a"),
)
# Class hubs have no per-page switcher; their Astro family navigation is the class dropdown in the
# guide header, which lists the thirteen class hubs the legacy ``.toc_page_list`` also listed. Spec
# pages carry the same dropdown, so it is read on class hubs only: a spec page whose switcher stopped
# matching must not turn into a family of every class hub.
CLASS_HUB_NAVIGATION_LAYOUTS = (*FAMILY_NAVIGATION_LAYOUTS, (".guide-header__selectors", ".dropdown__menu a[href]"))
PAGE_TOC_LAYOUTS = (
    (".content-toc", ".content-toc__item[href], a[href]"),
    (".toc_page_content_items", "a[href]"),
)
# The GTM dataLayer is a single-element array on the legacy pages and a bare object on the Astro ones.
DATA_LAYER_PATTERNS = (
    re.compile(r"dataLayer\s*=\s*\[\s*({.*?})\s*\];", flags=re.DOTALL),
    re.compile(r"dataLayer\s*=\s*({.*?})\s*;", flags=re.DOTALL),
)
# Icy Veins publishes talent builds as WoW loadout import strings rather than talent-calc links:
# one ``.export-string`` block per build, with the visible build name in ``__title`` and the raw
# import string in ``__code``.
TALENT_EXPORT_SELECTOR = ".export-string"
TALENT_EXPORT_CODE_SELECTOR = ".export-string__code"
TALENT_EXPORT_TITLE_SELECTOR = ".export-string__title"
INTRO_SELECTOR = ".guide-intro, .page_content_header_intro"
ARTICLE_SELECTOR = ".guide-page-content, .page_content_container > .page_content"
# Page furniture that lives inside the article container but is not article prose. The first line is
# shared, the second is the Astro layout, the third the legacy layout, and the fourth is the ``1.2.``
# numbering Icy Veins renders beside each heading (the heading itself is not numbered).
ARTICLE_CHROME_SELECTOR = ", ".join(
    (
        "script, style, noscript, .raider-io-links",
        ".table-of-contents, .content-toc, .guide-intro, .app-banner, .back-to-top, .internal-links",
        ".hidden_section_controls, .page_content_footer, .toc_mobile",
        ".heading_container > span",
    )
)


def _strip_toc_number(title: str) -> str:
    """Drop the ``1.`` / ``2.3.`` numbering Icy Veins renders in its on-page contents list.

    Only the contents list is numbered; the headings it points at are not, so their titles are used
    as-is and stay comparable with these.
    """
    return re.sub(r"^\d+(?:\.\d+)*\.\s*", "", title).strip()


def guide_slug_from_url(url: str) -> str | None:
    """Slug of a single-segment ``/wow/<slug>`` page, or ``None`` for any other Icy Veins URL shape.

    Guides link to news posts and other multi-segment ``/wow/`` pages, so callers that walk page
    links need to skip those instead of treating them as guide references.
    """
    match = GUIDE_PATH_RE.match(urlparse(url).path)
    return match.group("slug") if match else None


def guide_ref_parts(guide_ref: str) -> str:
    raw = guide_ref.strip()
    if not raw:
        raise ValueError("Guide reference cannot be empty.")
    if not raw.startswith(("http://", "https://", "/")):
        raw = f"/wow/{raw}"
    slug = guide_slug_from_url(raw)
    if slug is None:
        raise ValueError(f"Unsupported Icy Veins guide reference: {guide_ref}")
    return slug


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
    # Before every class family: a set page names its class (``transmogrification-priest-pvp-...-set``).
    if TRANSMOG_RE.search(normalized) and not normalized.endswith(("-guide", "-guides")):
        return "transmog"
    if normalized.endswith("-easy-mode"):
        return "easy_mode"
    if normalized.endswith("-leveling-guide"):
        return "leveling"
    if "-pvp-guide" in normalized or SPEC_PVP_SUBPAGE_RE.match(normalized):
        return "pvp"
    for suffix, family in SUBPAGE_SUFFIX_FAMILIES:
        if normalized.endswith(suffix):
            return family
    # Before the raid hubs: a strategy page can end in ``-raid`` (``orgozoa-strategy-guide-in-the-eternal-palace-raid``).
    if RAID_ENCOUNTER_RE.search(normalized):
        return "raid_encounter"
    if normalized.endswith("-raid-guide") or RAID_HUB_RE.search(normalized):
        return "raid_guide"
    if normalized.endswith(EXPANSION_GUIDE_SUFFIXES):
        return "expansion_guide"
    if any(keyword in normalized for keyword in SPECIAL_EVENT_KEYWORDS):
        return "special_event_guide"
    if SPEC_GUIDE_RE.search(normalized):
        return "spec_guide"
    return _classify_topic_slug(normalized)


def _classify_topic_slug(normalized: str) -> str | None:
    """Families of the pages that belong to no class: bosses, dungeons, delves, professions, tier lists, hubs.

    Change analyses (``arcane-mage-patch-9-1-changes-analysis``, ``latest-mage-class-changes``) stay
    unclassified: they are news-like, the scope ``search`` declines for "class changes".
    """
    if DUNGEON_GUIDE_RE.search(normalized):
        return "dungeon_guide"
    if DELVE_GUIDE_RE.search(normalized):
        return "delve_guide"
    if PROFESSION_RE.search(normalized) and "changes" not in normalized:
        return "profession"
    if "tier-list" in normalized:
        return "tier_list"
    if HUB_RE.search(normalized):
        return "hub"
    if normalized.endswith(("-guide", "-guides", "-to-do-list")):
        return "article_guide"
    return None


def guide_traversal_scope(content_family: str | None) -> str:
    if content_family in {"class_hub", "role_guide"}:
        return "current_page"
    return "family_navigation"


def _attribute(tag: Tag | None, name: str) -> str | None:
    """Read one attribute as a plain string; bs4 returns a list for multi-valued attributes."""
    if tag is None:
        return None
    value = tag.get(name)
    return value if isinstance(value, str) else None


def _first_container(soup: BeautifulSoup, layouts: tuple[tuple[str, str], ...]) -> tuple[Tag | None, str]:
    """Return the first layout container present in ``soup`` with the anchor selector to use inside it."""
    for container_selector, anchor_selector in layouts:
        container = soup.select_one(container_selector)
        if isinstance(container, Tag):
            return container, anchor_selector
    return None, ""


def _meta_content(soup: BeautifulSoup, *, attribute: str, value: str) -> str | None:
    return clean_text(_attribute(soup.select_one(f'meta[{attribute}="{value}"]'), "content"))


def _link_href(soup: BeautifulSoup, *, rel: str) -> str | None:
    href = _attribute(soup.select_one(f'link[rel~="{rel}"]'), "href")
    return href.strip() or None if href else None


def _json_ld_object(soup: BeautifulSoup, schema_type: str) -> dict[str, Any] | None:
    """The first JSON-LD object of ``schema_type`` ("Article", "BreadcrumbList") on the page."""
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
            if candidate.get("@type") == schema_type:
                return candidate
    return None


def _extract_data_layer(soup: BeautifulSoup) -> dict[str, Any]:
    for script in soup.find_all("script"):
        text = script.string or script.get_text()
        if "page_type" not in text or "dataLayer" not in text:
            continue
        match = next((found for found in (pattern.search(text) for pattern in DATA_LAYER_PATTERNS) if found), None)
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


def _extract_family_navigation(soup: BeautifulSoup, *, current_url: str, content_family: str | None) -> list[dict[str, Any]]:
    layouts = CLASS_HUB_NAVIGATION_LAYOUTS if content_family == "class_hub" else FAMILY_NAVIGATION_LAYOUTS
    container, anchor_selector = _first_container(soup, layouts)
    if container is None:
        return []
    current_path = urlparse(current_url).path.rstrip("/")
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    anchors = container.select(anchor_selector)
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
        section_slug = guide_slug_from_url(url)
        if section_slug is None:
            # Not a guide page (news posts, tool pages): it is not part of this guide's family.
            continue
        parent = anchor.parent if isinstance(anchor.parent, Tag) else None
        raw_classes = parent.get("class") if parent is not None else None
        classes = raw_classes if isinstance(raw_classes, list) else ([raw_classes] if isinstance(raw_classes, str) else [])
        items.append(
            {
                "title": title,
                "url": url,
                "section_slug": section_slug,
                "active": bool({"selected", "active"} & set(classes)) or urlparse(url).path.rstrip("/") == current_path,
                "ordinal": ordinal,
            }
        )
    return items


def _extract_page_toc(soup: BeautifulSoup, *, current_url: str) -> list[dict[str, Any]]:
    container, anchor_selector = _first_container(soup, PAGE_TOC_LAYOUTS)
    if container is None:
        return []
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for ordinal, anchor in enumerate(container.select(anchor_selector), start=1):
        href = anchor.get("href")
        if not isinstance(href, str):
            continue
        url = urljoin(current_url, href)
        if url in seen:
            continue
        seen.add(url)
        parsed = urlparse(url)
        title = clean_text(_strip_toc_number(anchor.get_text(" ", strip=True)))
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
    intro = soup.select_one(INTRO_SELECTOR)
    if not isinstance(intro, Tag):
        return ""
    text = clean_text(intro.get_text(" ", strip=True))
    return text or ""


def _article_tag(soup: BeautifulSoup) -> Tag | None:
    article = soup.select_one(ARTICLE_SELECTOR)
    if isinstance(article, Tag):
        return article
    return None


def _clone_article(article: Tag) -> Tag:
    cloned = BeautifulSoup(str(article), "html.parser").find(article.name)
    if not isinstance(cloned, Tag):
        raise ValueError("Failed to clone Icy Veins article node.")
    for node in cloned.select(ARTICLE_CHROME_SELECTOR):
        node.decompose()
    return cloned


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
        elif parsed.netloc.endswith("icy-veins.com") and (slug := guide_slug_from_url(url)) is not None:
            entity_type = "page"
            entity_id = slug
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


def _talent_export_code(block: Tag) -> str | None:
    code_tag = block.select_one(TALENT_EXPORT_CODE_SELECTOR)
    return code_tag.get_text(strip=True) if isinstance(code_tag, Tag) else None


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
    for row in extract_talent_export_builds(
        article,
        source_url=source_url,
        provider="icy-veins",
        block_selector=TALENT_EXPORT_SELECTOR,
        title_selector=TALENT_EXPORT_TITLE_SELECTOR,
        read_code=_talent_export_code,
    ):
        items.setdefault(str(row["url"]), row)
    return sorted(items.values(), key=lambda row: str(row["url"]))


@dataclass(frozen=True, slots=True)
class _PageMeta:
    """Title/description/attribution scraped from JSON-LD, meta tags, and the visible byline."""

    title: str | None
    description: str | None
    author: str | None
    published_at: str | None
    last_updated: str | None


def _selector_text(soup: BeautifulSoup, selector: str) -> str | None:
    tag = soup.select_one(selector)
    return clean_text(tag.get_text(" ", strip=True)) if isinstance(tag, Tag) else None


def _author_name(soup: BeautifulSoup, article_json: dict[str, Any]) -> str | None:
    author_value = article_json.get("author")
    if isinstance(author_value, dict):
        name = clean_text(author_value.get("name"))
        if name is not None:
            return name
    return _selector_text(soup, ".page_author span[style]")


def _byline_last_updated(soup: BeautifulSoup) -> str | None:
    """Fall back to the rendered 'Last updated on <date> at <hour>' byline when JSON-LD has no dateModified."""
    date = _selector_text(soup, ".local_date_date")
    hour = _selector_text(soup, ".local_date_hour")
    if date and hour:
        return f"{date} {hour}"
    return date


def _page_meta(soup: BeautifulSoup, article_json: dict[str, Any]) -> _PageMeta:
    title = (
        clean_text(article_json.get("headline"))
        or _meta_content(soup, attribute="property", value="og:title")
        or clean_text(soup.title.get_text(" ", strip=True) if soup.title else None)
    )
    return _PageMeta(
        title=title,
        description=clean_text(article_json.get("description")) or _meta_content(soup, attribute="name", value="description"),
        author=_author_name(soup, article_json),
        published_at=clean_text(article_json.get("datePublished")),
        last_updated=clean_text(article_json.get("dateModified")) or _byline_last_updated(soup),
    )


def _comments_url(soup: BeautifulSoup, *, canonical_url: str) -> str | None:
    comments_tag = soup.select_one(".page_comments a[href]")
    if not isinstance(comments_tag, Tag):
        return None
    href = comments_tag.get("href")
    return urljoin(canonical_url, href) if isinstance(href, str) else None


def _article_payload(
    soup: BeautifulSoup,
    *,
    canonical_url: str,
    section_title: str,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Return the article block plus the linked entities and build references found inside it."""
    intro_text = _extract_intro_text(soup)
    article_tag = _article_tag(soup)
    if article_tag is None:
        empty: dict[str, Any] = {"html": "", "text": "", "intro_text": intro_text, "headings": [], "sections": []}
        return empty, [], []
    article = _clone_article(article_tag)
    payload = {
        "html": "".join(str(child) for child in article.contents).strip(),
        "text": clean_text(article.get_text("\n", strip=True)) or "",
        "intro_text": intro_text,
        "headings": extract_headings(article),
        "sections": extract_sections(article, fallback_title=section_title),
    }
    return (
        payload,
        _extract_linked_entities(article, source_url=canonical_url),
        _extract_build_references(article, source_url=canonical_url),
    )


def parse_guide_page(html: str, *, source_url: str) -> dict[str, Any]:
    """Parse one Icy Veins WoW guide page into the shared article payload shape."""
    soup = BeautifulSoup(html, "html.parser")
    canonical_url = urljoin(ICY_VEINS_BASE_URL, _link_href(soup, rel="canonical") or source_url)
    slug = guide_ref_parts(canonical_url)
    content_family = classify_guide_slug(slug)
    meta = _page_meta(soup, _json_ld_object(soup, "Article") or {})
    data_layer = _extract_data_layer(soup)
    navigation = _extract_family_navigation(soup, current_url=canonical_url, content_family=content_family)
    active_nav = next((item for item in navigation if item["active"]), None)
    section_title = active_nav["title"] if active_nav is not None else slug_display_name(slug)
    article, linked_entities, build_references = _article_payload(
        soup,
        canonical_url=canonical_url,
        section_title=section_title,
    )
    page_type = data_layer.get("page_type")
    return {
        "page": {
            "title": meta.title,
            "description": meta.description,
            "canonical_url": canonical_url,
            "page_type": clean_text(str(page_type)) if page_type is not None else None,
        },
        "guide": {
            "slug": slug,
            "page_url": canonical_url,
            "section_slug": slug,
            "section_title": section_title,
            "content_family": content_family,
            "supported_surface": content_family is not None,
            "traversal_scope": guide_traversal_scope(content_family),
            "author": meta.author,
            "last_updated": meta.last_updated,
            "published_at": meta.published_at,
        },
        "navigation": navigation,
        "page_toc": _extract_page_toc(soup, current_url=canonical_url),
        "article": article,
        "linked_entities": linked_entities,
        "build_references": build_references,
        "citations": {
            "page": canonical_url,
            "comments": _comments_url(soup, canonical_url=canonical_url),
        },
    }


def _menu_link_title(anchor: Tag) -> str | None:
    """The link's own text, without child badges such as "NEW!"."""
    own_text = clean_text(" ".join(anchor.find_all(string=True, recursive=False)))
    return own_text or clean_text(anchor.get_text(" ", strip=True))


def parse_site_menu_guides(html: str) -> list[dict[str, Any]]:
    """Every supported guide the site-wide menu links, in the sitemap row shape with no ``sitemap_lastmod``.

    ``menu_title`` keeps the menu's wording ("Mythic+ Season 2"), which only makes sense inside its
    menu section, so ``name`` is built from the slug like a sitemap row's.
    """
    anchors = BeautifulSoup(html, "html.parser").select(SITE_MENU_LINK_SELECTOR)
    # Stable sort: a hub's own entry wins over the "View all" link to the same page.
    anchors.sort(key=lambda anchor: SITE_MENU_VIEW_ALL_CLASS in (anchor.get("class") or []))
    guides: dict[str, dict[str, Any]] = {}
    for anchor in anchors:
        href = _attribute(anchor, "href")
        slug = guide_slug_from_url(urljoin(ICY_VEINS_BASE_URL, href)) if href else None
        if slug is None or slug in guides or (content_family := classify_guide_slug(slug)) is None:
            continue
        guides[slug] = {
            "slug": slug,
            "name": slug_display_name(slug),
            "url": guide_url(slug),
            "content_family": content_family,
            "sitemap_lastmod": None,
            "menu_title": _menu_link_title(anchor),
        }
    return sorted(guides.values(), key=lambda row: row["name"].lower())


def _date_part(timestamp: str) -> str | None:
    """The ``YYYY-MM-DD`` part of a sitemap ``<lastmod>`` or a JSON-LD date, or ``None`` when it is missing or not a date."""
    try:
        return date.fromisoformat(timestamp[:10]).isoformat()
    except ValueError:
        return None


def parse_sitemap_slugs(xml_text: str) -> set[str]:
    """Every ``/wow/<slug>`` page the sitemap lists, classified or not."""
    return {slug for url, _ in SITEMAP_ENTRY_RE.findall(xml_text) if (slug := guide_slug_from_url(url)) is not None}


def parse_sitemap_guides(xml_text: str) -> list[dict[str, Any]]:
    entries = SITEMAP_ENTRY_RE.findall(xml_text)
    seen: set[str] = set()
    guides: list[dict[str, Any]] = []
    for url, lastmod in entries:
        slug = guide_slug_from_url(url)
        if slug is None or slug in seen:
            continue
        content_family = classify_guide_slug(slug)
        if content_family is None:
            continue
        seen.add(slug)
        guides.append(
            {
                "slug": slug,
                "name": slug_display_name(slug),
                "url": url,
                "content_family": content_family,
                "sitemap_lastmod": _date_part(lastmod),
            }
        )
    guides.sort(key=lambda row: row["name"].lower())
    return guides


def _site_link_slug(href: str | None) -> str | None:
    """Slug of a link to a ``www.icy-veins.com/wow/<slug>`` page; None for any other link."""
    if not href:
        return None
    url = urljoin(ICY_VEINS_BASE_URL, href)
    return guide_slug_from_url(url) if urlparse(url).netloc == "www.icy-veins.com" else None


def _breadcrumb_parent(soup: BeautifulSoup) -> str | None:
    """Slug of the hub the page's JSON-LD breadcrumb trail puts it under; None when that is the WoW home page."""
    trail = (_json_ld_object(soup, "BreadcrumbList") or {}).get("itemListElement")
    if not isinstance(trail, list) or len(trail) < 2 or not isinstance(trail[-2], dict):
        return None
    parent = trail[-2].get("item")
    return _site_link_slug(parent.get("@id")) if isinstance(parent, dict) else None


def read_index_page(url: str, html: str) -> PageRead | None:
    """One crawled page as a site-index row plus every ``/wow/<slug>`` page it links; None when it is not a WoW page.

    The canonical link is the page's identity, so a renamed page fetched at its old URL reads as the
    new one. The row keeps the JSON-LD headline (``og:title`` carries markup) and both dates; the
    page's HTML is not kept. A link in the site-wide menu has the source "menu", any other "page".
    """
    del url
    soup = BeautifulSoup(html, "html.parser")
    slug = _site_link_slug(_link_href(soup, rel="canonical"))
    if slug is None:
        return None
    article = _json_ld_object(soup, "Article") or {}
    links: dict[str, PageLink] = {}
    for source, anchors in (("menu", soup.select(SITE_MENU_LINK_SELECTOR)), ("page", soup.find_all("a", href=True))):
        for anchor in anchors:
            link_slug = _site_link_slug(_attribute(anchor, "href"))
            if link_slug is not None and link_slug != slug:
                links.setdefault(link_slug, PageLink(guide_url(link_slug), source))
    row = {
        "slug": slug,
        "url": guide_url(slug),
        "title": clean_text(article.get("headline")),
        "date_published": _date_part(str(article.get("datePublished") or "")),
        "date_modified": _date_part(str(article.get("dateModified") or "")),
        "parent": _breadcrumb_parent(soup),
    }
    return PageRead(guide_url(slug), row, tuple(links.values()))

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag
from warcraft_content.guide_page import extract_build_references, extract_linked_entities
from warcraft_content.html_sections import clean_text, extract_headings, extract_sections
from warcraft_core.wow_specs import WOW_SPECS, raiderio_class_slug

METHOD_BASE_URL = "https://www.method.gg"
SUPPORTED_GUIDE_PATH_RE = re.compile(r"^/guides/(?P<slug>[a-z0-9-]+)(?:/(?P<section>[^/?#]+))?/?$")
# Method publishes talent builds as WoW loadout import strings rather than talent-calc links: one
# ``.df-talent-block`` per build, with the visible build name in ``.talent-title`` and the raw
# import string in the ``data-talent`` attribute of ``.talent-embed``.
TALENT_BUILD_SELECTOR = ".df-talent-block"
TALENT_BUILD_EMBED_SELECTOR = ".talent-embed[data-talent]"
TALENT_BUILD_TITLE_SELECTOR = ".talent-title"
# Method titles a class guide "<spec>-<class>" (beast-mastery-hunter, frost-death-knight). Other slugs
# that merely end in a class, such as unlocking-void-elf-demon-hunter, are one-page articles.
SPEC_GUIDE_SLUGS = frozenset(f"{spec.raiderio_spec_slug}-{raiderio_class_slug(spec.class_key)}" for spec in WOW_SPECS)
UNSUPPORTED_ROOT_GUIDE_SLUGS = {"tier-list", "world-of-warcraft"}
WRITTEN_BY_RE = re.compile(r"^Written by\s+(?P<author>.+?)\s*-\s*(?P<date>\d{1,2}(?:st|nd|rd|th)\s+\w+,?\s+\d{4})$")
DISPLAY_DATE_RE = re.compile(r"(?P<day>\d{1,2})(?:st|nd|rd|th)?\s+(?P<month>[A-Za-z]{3,}),?\s+(?P<year>\d{4})")
SITEMAP_URL_RE = re.compile(r"<url>(.*?)</url>", flags=re.DOTALL)
SITEMAP_LOC_RE = re.compile(r"<loc>([^<]+)</loc>")
SITEMAP_LASTMOD_RE = re.compile(r"<lastmod>([^<]+)</lastmod>")
SITEMAP_GUIDE_URL_RE = re.compile(r"^https://www\.method\.gg/guides/(?P<slug>[^/]+)$")


def guide_ref_parts(guide_ref: str) -> tuple[str, str | None]:
    raw = guide_ref.strip()
    if not raw:
        raise ValueError("Guide reference cannot be empty.")
    if raw.startswith("http://") or raw.startswith("https://"):
        parsed = urlparse(raw)
        path = parsed.path
    else:
        path = raw if raw.startswith("/") else f"/guides/{raw}"
    # Method slugs are lowercase: ``Frost-Mage`` is ``frost-mage``, and a slug of any other characters
    # is a malformed reference, rejected before any request.
    match = SUPPORTED_GUIDE_PATH_RE.match(path.lower())
    if not match:
        raise ValueError(f"Unsupported Method guide reference: {guide_ref}")
    return match.group("slug"), match.group("section")


def guide_section_from_url(url: str) -> tuple[str, str | None] | None:
    """``(slug, section)`` for a ``/guides/...`` page, or ``None`` for any other Method URL shape.

    Method's own guide navigation mixes in non-guide links such as the ``/guides`` index, so callers
    that walk page links need to skip those instead of treating them as guide references.
    """
    match = SUPPORTED_GUIDE_PATH_RE.match(urlparse(url).path)
    return (match.group("slug"), match.group("section")) if match else None


def guide_url(slug: str, section_slug: str | None = None) -> str:
    suffix = f"/{section_slug}" if section_slug else ""
    return f"{METHOD_BASE_URL}/guides/{slug}{suffix}"


def classify_guide_family(slug: str) -> str:
    if slug in UNSUPPORTED_ROOT_GUIDE_SLUGS:
        return "unsupported_index"
    if slug.endswith("-profession-guide"):
        return "profession_guide"
    if slug.endswith("-delve-guide"):
        return "delve_guide"
    if slug.endswith("-renown-reputation-guide") or slug.endswith("-reputation-guide"):
        return "reputation_guide"
    if slug in SPEC_GUIDE_SLUGS:
        return "class_guide"
    return "article_guide"


def _meta_content(soup: BeautifulSoup, **attrs: str) -> str | None:
    tag = soup.find("meta", attrs=dict(attrs))
    if not isinstance(tag, Tag):
        return None
    content = tag.get("content")
    return clean_text(content) if isinstance(content, str) else None


def _link_href(soup: BeautifulSoup, **attrs: str) -> str | None:
    tag = soup.find("link", attrs=dict(attrs))
    if not isinstance(tag, Tag):
        return None
    href = tag.get("href")
    if not isinstance(href, str):
        return None
    href = href.strip()
    return href or None


def _extract_navigation(soup: BeautifulSoup, *, current_url: str) -> list[dict[str, Any]]:
    current_path = urlparse(current_url).path.rstrip("/")
    items: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for ordinal, anchor in enumerate(soup.select("ul.guide-navigation a, .guide-navigation a"), start=1):
        href = anchor.get("href")
        if not isinstance(href, str):
            continue
        title = clean_text(anchor.get_text(" ", strip=True))
        if not title:
            continue
        url = urljoin(METHOD_BASE_URL, href)
        key = (title, url)
        if key in seen:
            continue
        seen.add(key)
        parts = guide_section_from_url(url)
        if parts is None:
            # Not a guide page (the /guides index, marketing links): not part of this guide.
            continue
        path = urlparse(url).path.rstrip("/")
        section_slug = parts[1]
        parent = anchor.parent
        classes = parent.get("class") if isinstance(parent, Tag) else None
        active = (classes is not None and "active" in classes) or path == current_path
        items.append(
            {
                "title": title,
                "url": url,
                "section_slug": section_slug or "introduction",
                "active": active,
                "ordinal": ordinal,
            }
        )
    return items


def _article_tag(soup: BeautifulSoup) -> Tag | None:
    article = soup.select_one("article.guide-main-content, .guide-main-content")
    if isinstance(article, Tag):
        return article
    return None


def _first_text(soup: BeautifulSoup, selectors: tuple[str, ...]) -> str | None:
    for selector in selectors:
        tag = soup.select_one(selector)
        if not isinstance(tag, Tag):
            continue
        text = clean_text(tag.get_text(" ", strip=True))
        if text:
            return text
    return None


def _clone_article(article: Tag) -> Tag:
    soup = BeautifulSoup(str(article), "html.parser")
    cloned = soup.find("article") or soup.find(class_="guide-main-content")
    if not isinstance(cloned, Tag):
        raise ValueError("Failed to clone Method article node.")
    for node in cloned.select("script, style, noscript, .premium-video, .mobile-video-wrap"):
        node.decompose()
    return cloned


def _talent_export_code(block: Tag) -> str | None:
    embed = block.select_one(TALENT_BUILD_EMBED_SELECTOR)
    code = embed.get("data-talent") if isinstance(embed, Tag) else None
    return code.strip() if isinstance(code, str) else None


def _normalize_author_and_last_updated(author: str | None, last_updated: str | None) -> tuple[str | None, str | None]:
    if isinstance(author, str):
        match = WRITTEN_BY_RE.match(author)
        if match:
            normalized_author = clean_text(match.group("author"))
            normalized_date = clean_text(match.group("date"))
            return normalized_author, last_updated or normalized_date
    return author, last_updated


def _iso_date(value: str) -> str | None:
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return None


def last_updated_iso(text: str | None) -> str | None:
    """``2026-08-11`` from Method's ``Last Updated: 11th Aug, 2026`` or ``4th August 2025``; ``None`` when unreadable."""
    match = DISPLAY_DATE_RE.search(text or "")
    if match is None:
        return None
    try:
        return datetime.strptime(f"{match['day']} {match['month'][:3]} {match['year']}", "%d %b %Y").date().isoformat()
    except ValueError:
        return None


def parse_guide_page(html: str, *, source_url: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    canonical_url = _link_href(soup, rel="canonical") or source_url
    canonical_url = urljoin(METHOD_BASE_URL, canonical_url)
    guide_slug, section_slug = guide_ref_parts(canonical_url)
    content_family = classify_guide_family(guide_slug)
    page_title = clean_text(_meta_content(soup, property="og:title")) or clean_text(
        soup.title.get_text(" ", strip=True) if soup.title else None)
    description = _meta_content(soup, name="description")
    navigation = _extract_navigation(soup, current_url=canonical_url)
    active_nav = next((item for item in navigation if item["active"]), None)
    display_section_title = active_nav["title"] if active_nav is not None else (section_slug or "Introduction").replace("-", " ").title()
    patch = _first_text(soup, (".guide-author", ".guides-titles .guide-author"))
    last_updated = _first_text(soup, (".guide-update-date", ".guides-titles .guide-update-date"))
    author = _first_text(soup, (".guides-author-block .author-name", ".author-name", "[itemprop='author']"))
    author, last_updated = _normalize_author_and_last_updated(author, last_updated)
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
        headings = extract_headings(article)
        sections = extract_sections(article, fallback_title=display_section_title)
        linked_entities = sorted(
            extract_linked_entities(article, source_url=canonical_url, provider="method"),
            key=lambda row: (row["type"], row["id"]),
        )
        build_references = extract_build_references(
            article,
            source_url=canonical_url,
            provider="method",
            site_label="Method",
            block_selector=TALENT_BUILD_SELECTOR,
            title_selector=TALENT_BUILD_TITLE_SELECTOR,
            read_code=_talent_export_code,
        )
    return {
        "page": {
            "title": page_title,
            "description": description,
            "canonical_url": canonical_url,
        },
        "guide": {
            "slug": guide_slug,
            "page_url": canonical_url,
            "section_slug": section_slug or "introduction",
            "section_title": display_section_title,
            "author": author,
            # ISO, comparable across providers; the page's own wording stays in ``last_updated_text``.
            "last_updated": last_updated_iso(last_updated),
            "last_updated_text": last_updated,
            "patch": patch,
            "content_family": content_family,
            "supported_surface": content_family != "unsupported_index",
        },
        "navigation": navigation,
        "article": {
            "html": article_html,
            "text": article_text,
            "headings": headings,
            "sections": sections,
        },
        "linked_entities": linked_entities,
        "build_references": build_references,
    }


def parse_sitemap_guides(xml_text: str) -> list[dict[str, Any]]:
    seen: set[str] = set()
    guides: list[dict[str, Any]] = []
    for entry in SITEMAP_URL_RE.findall(xml_text):
        loc = SITEMAP_LOC_RE.search(entry)
        match = SITEMAP_GUIDE_URL_RE.match(loc.group(1).strip()) if loc else None
        if not match:
            continue
        slug = match.group("slug")
        if slug in seen:
            continue
        seen.add(slug)
        lastmod = SITEMAP_LASTMOD_RE.search(entry)
        guides.append(
            {
                "slug": slug,
                "name": clean_text(slug.replace("-", " ").title()) or slug,
                "url": match.group(0),
                "sitemap_lastmod": _iso_date(lastmod.group(1)[:10]) if lastmod else None,
            }
        )
    guides.sort(key=lambda row: row["name"].lower())
    return guides

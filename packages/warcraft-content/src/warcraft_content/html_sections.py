"""Heading-driven sectioning of a parsed guide article body.

Icy Veins and Method both wrap their headings in layout containers (``div.heading_container`` and
``div.guide-section-title``), so a scan of the article's direct children alone finds no heading at
all and hands back the whole page as one untitled section. The walk here descends only into
elements that actually contain a heading, which keeps ordinary content blocks whole.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html import unescape
from typing import Any

from bs4 import Tag

# Both sites use h2 for page sections, h3 for builds and sub-topics, and h4 for per-item notes.
HEADING_TAG_RE = re.compile(r"^h[234]$")


def clean_text(value: str | None) -> str | None:
    """Collapse whitespace and resolve HTML entities; ``None`` when nothing readable is left."""
    if not isinstance(value, str):
        return None
    text = unescape(re.sub(r"\s+", " ", value)).strip()
    return text or None


def extract_headings(article: Tag) -> list[dict[str, Any]]:
    """Every heading in the article in document order, however deeply the layout nests it."""
    headings: list[dict[str, Any]] = []
    for node in article.find_all(HEADING_TAG_RE):
        heading = _heading_title_and_level(node)
        if heading is None:
            continue
        headings.append({"title": heading[0], "level": heading[1], "ordinal": len(headings) + 1})
    return headings


def extract_sections(article: Tag, *, fallback_title: str) -> list[dict[str, Any]]:
    """Cut the article into one section per heading, in document order.

    Content that precedes the first heading becomes a section titled ``fallback_title``. Sections
    that carry neither text nor markup are dropped, and the surviving ordinals stay the ones the
    document order gave them so a dropped section is visible as a gap.
    """
    sections: list[_Section] = []
    _split_sections(article, sections, fallback_title=fallback_title)
    rows = [
        {
            "title": section.title,
            "level": section.level,
            "ordinal": ordinal,
            "text": clean_text(" ".join(section.text_parts)) or "",
            "html": "\n".join(section.html_parts).strip(),
        }
        for ordinal, section in enumerate(sections, start=1)
    ]
    return [row for row in rows if row["text"] or row["html"]]


@dataclass(slots=True)
class _Section:
    """One heading's content while it is still being collected."""

    title: str
    level: int
    html_parts: list[str] = field(default_factory=list)
    text_parts: list[str] = field(default_factory=list)


def _heading_title_and_level(heading: Tag) -> tuple[str, int] | None:
    title = clean_text(heading.get_text(" ", strip=True))
    if not title:
        return None
    return title, int(heading.name[1])


def _split_sections(node: Tag, sections: list[_Section], *, fallback_title: str) -> None:
    for child in node.children:
        if isinstance(child, Tag):
            heading = _heading_title_and_level(child) if HEADING_TAG_RE.match(child.name) else None
            if heading is not None:
                sections.append(_Section(title=heading[0], level=heading[1]))
                continue
            if child.find(HEADING_TAG_RE) is not None:
                _split_sections(child, sections, fallback_title=fallback_title)
                continue
            html = str(child).strip()
            text = clean_text(child.get_text(" ", strip=True))
        else:
            # Prose written straight into a wrapper, with no element of its own to carry it.
            html = ""
            text = clean_text(str(child))
            if text is None:
                continue
        if not sections:
            sections.append(_Section(title=fallback_title, level=2))
        if html:
            sections[-1].html_parts.append(html)
        if text:
            sections[-1].text_parts.append(text)

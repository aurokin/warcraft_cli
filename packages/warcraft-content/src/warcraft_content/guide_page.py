"""Guide-page parsing pieces the Icy Veins and Method page parsers share."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import Tag
from warcraft_core.identity import ability_identity_payload, build_identity_payload, build_reference_payload

from warcraft_content.html_sections import clean_text

WOWHEAD_LINK_RE = re.compile(
    r"^(?P<entity_type>achievement|currency|faction|item|mount|npc|object|pet|quest|spell|zone)=(?P<id>\d+)(?:/|$)"
)
# A WoW loadout import string as Blizzard's client generates it: one long run of base64 characters.
WOW_TALENT_EXPORT_RE = re.compile(r"^[A-Za-z0-9+/]{40,}$")


def talent_export_reference(code: str, *, label: str | None, source_url: str, provider: str) -> dict[str, Any]:
    """One published WoW loadout import string, in the shared build-reference row shape.

    ``url`` carries the import string itself: a ``wow_talent_export`` reference has no link to point
    at, the string is what identifies it, and it is exactly what ``simc --build-text`` consumes.
    """
    return {
        "kind": "build_reference",
        "reference_type": "wow_talent_export",
        "url": code,
        "label": label,
        "build_code": code,
        "source_url": source_url,
        "build_identity": build_identity_payload(
            actor_class=None,
            spec=None,
            confidence="none",
            source="guide_talent_export_string",
            source_notes=(
                "build code came from a WoW loadout import string published in the guide",
                "class and spec are not read off this reference; decode the import string to identify them",
            ),
        ),
        "source": {"provider": provider, "source": "guide_talent_export_string"},
    }


def extract_linked_entities(
    article: Tag,
    *,
    source_url: str,
    provider: str,
    site_page: Callable[[str], str | None] | None = None,
) -> list[dict[str, Any]]:
    """One row per Wowhead entity (and, with ``site_page``, per own-site page) the article links, in link order.

    ``site_page`` returns the page id of a link to one of the provider's own pages, or ``None``. A
    spell row carries a canonical ability identity built from the first non-empty link text.
    Callers sort the rows.
    """
    items: dict[tuple[str, str | int], dict[str, Any]] = {}
    for anchor in article.find_all("a", href=True):
        href = anchor.get("href")
        if not isinstance(href, str):
            continue
        url = urljoin(source_url, href)
        parsed = urlparse(url)
        match = WOWHEAD_LINK_RE.match(parsed.path.lstrip("/")) if "wowhead.com" in parsed.netloc else None
        page_id = site_page(url) if match is None and site_page is not None else None
        if match is not None:
            key: tuple[str, str | int] = (match.group("entity_type"), int(match.group("id")))
        elif page_id is not None:
            key = ("page", page_id)
        else:
            continue
        name = clean_text(anchor.get_text(" ", strip=True))
        record = items.setdefault(key, {"type": key[0], "id": key[1], "name": name, "url": url, "source_url": source_url})
        if not record["name"] and name:
            record["name"] = name
    for row in items.values():
        if row["type"] == "spell":
            row["ability_identity"] = ability_identity_payload(
                spell_id=row["id"], name=row["name"] or None, provider=provider, source="guide_linked_entity"
            )
    return list(items.values())


def extract_build_references(
    article: Tag,
    *,
    source_url: str,
    provider: str,
    site_label: str,
    block_selector: str,
    title_selector: str,
    read_code: Callable[[Tag], str | None],
) -> list[dict[str, Any]]:
    """The article's Wowhead talent-calc links plus one row per talent block whose import string ``read_code`` finds.

    Each site wraps a build in its own block markup and stores the string differently, so the
    provider passes the block/title selectors and the function that reads the string out of a block.
    """
    items: dict[str, dict[str, Any]] = {}
    for anchor in article.find_all("a", href=True):
        href = anchor.get("href")
        if not isinstance(href, str):
            continue
        payload = build_reference_payload(
            ref=urljoin(source_url, href),
            provider=provider,
            source="guide_embedded_link",
            source_url=source_url,
            label=clean_text(anchor.get_text(" ", strip=True)),
            notes=[f"embedded {site_label} guide link"],
        )
        if payload is not None:
            items[str(payload["url"])] = payload
    for block in article.select(block_selector):
        code = read_code(block)
        if code is None or not WOW_TALENT_EXPORT_RE.match(code):
            continue
        title_tag = block.select_one(title_selector)
        label = clean_text(title_tag.get_text(" ", strip=True)) if isinstance(title_tag, Tag) else None
        items.setdefault(code, talent_export_reference(code, label=label, source_url=source_url, provider=provider))
    return sorted(items.values(), key=lambda row: str(row["url"]))

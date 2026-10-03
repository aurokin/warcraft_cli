"""Guide-page parsing pieces the Icy Veins and Method page parsers share."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from bs4 import Tag
from warcraft_core.identity import build_identity_payload

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


def extract_talent_export_builds(
    article: Tag,
    *,
    source_url: str,
    provider: str,
    block_selector: str,
    title_selector: str,
    read_code: Callable[[Tag], str | None],
) -> list[dict[str, Any]]:
    """One build reference per talent block whose import string ``read_code`` finds in it.

    Each site wraps a build in its own block markup and stores the string differently, so the
    provider passes the block/title selectors and the function that reads the string out of a block.
    """
    rows: list[dict[str, Any]] = []
    for block in article.select(block_selector):
        code = read_code(block)
        if code is None or not WOW_TALENT_EXPORT_RE.match(code):
            continue
        title_tag = block.select_one(title_selector)
        label = clean_text(title_tag.get_text(" ", strip=True)) if isinstance(title_tag, Tag) else None
        rows.append(talent_export_reference(code, label=label, source_url=source_url, provider=provider))
    return rows

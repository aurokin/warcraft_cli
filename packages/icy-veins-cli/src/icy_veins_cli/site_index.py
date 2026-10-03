"""The Icy Veins site index: every WoW page an ``index-refresh`` crawl has found, kept across runs.

The sitemap froze in 2025 and the site menu links current pages only, so this index is how search
finds the rest: boss, dungeon and delve pages, past seasons, renamed pages. It is durable data under
``warcraft_core.paths.provider_data_root("icy-veins")``, never the TTL cache, and holds page metadata
only (no HTML). A run merges into the previous index rather than replacing it, so a page stays
findable after current pages stop linking it. Until a user runs ``index-refresh``, search reads the
snapshot bundled with the package (``icy_veins_cli/data/site_index.json``).
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from importlib import resources
from pathlib import Path
from typing import Any

from warcraft_content.site_crawler import CrawlResult
from warcraft_core.paths import provider_data_root

from icy_veins_cli.page_parser import guide_ref_parts

INDEX_FORMAT = 1
INDEX_FILE_NAME = "site_index.json"
BUNDLED_INDEX = resources.files("icy_veins_cli").joinpath("data", INDEX_FILE_NAME)
# What ``page_parser.read_index_page`` reads from a page besides its slug and URL; a redirect row has none of it.
PAGE_FIELDS = ("title", "date_published", "date_modified", "parent")


@dataclass(slots=True)
class SiteIndex:
    """Index rows keyed by slug; a renamed page keeps a ``status: "redirect"`` row naming its new slug."""

    pages: dict[str, dict[str, Any]] = field(default_factory=dict)
    refreshed_at: str | None = None
    # Pages a capped run discovered and did not fetch; the next run starts with them.
    frontier: list[str] = field(default_factory=list)
    path: str = ""
    bundled: bool = False

    def payload(self) -> dict[str, Any]:
        return {
            "format": INDEX_FORMAT,
            "refreshed_at": self.refreshed_at,
            "frontier": self.frontier,
            # Sorted, so a regenerated bundled snapshot diffs by page.
            "pages": [self.pages[slug] for slug in sorted(self.pages)],
        }


@dataclass(frozen=True, slots=True)
class MergeCounts:
    new: list[str]
    dropped: list[str]


def local_index_path() -> Path:
    return provider_data_root("icy-veins") / INDEX_FILE_NAME


def _parse_index(text: str, *, path: str, bundled: bool) -> SiteIndex | None:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or payload.get("format") != INDEX_FORMAT or not isinstance(payload.get("pages"), list):
        return None
    pages = {row["slug"]: row for row in payload["pages"] if isinstance(row, dict) and isinstance(row.get("slug"), str)}
    return SiteIndex(pages, payload.get("refreshed_at"), list(payload.get("frontier") or []), path, bundled)


def load_site_index() -> SiteIndex | None:
    """The local index, else the bundled snapshot; an unreadable local file counts as missing."""
    path = local_index_path()
    if path.is_file():
        local = _parse_index(path.read_text(encoding="utf-8"), path=str(path), bundled=False)
        if local is not None:
            return local
    if not BUNDLED_INDEX.is_file():
        return None
    return _parse_index(BUNDLED_INDEX.read_text(encoding="utf-8"), path=str(BUNDLED_INDEX), bundled=True)


def save_site_index(index: SiteIndex) -> Path:
    """Write ``index`` to the local index path atomically: a reader sees the old file or the new one, never half."""
    path = local_index_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{INDEX_FILE_NAME}.", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(index.payload(), stream, indent=1, ensure_ascii=False)
            stream.write("\n")
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise
    return path


def merge_crawl(previous: SiteIndex | None, result: CrawlResult, *, now: datetime) -> tuple[SiteIndex, MergeCounts]:
    """``previous`` updated with what ``result`` read: pages it never saw again are kept as they were.

    A fetched page refreshes its row and keeps its ``first_seen`` and discovery ``source``; a 301
    becomes a redirect row naming the new slug; a 404 removes the row. A run that was blocked or
    found the site down keeps the previous ``refreshed_at``: it did not finish a refresh, so the
    index's age warnings stand.
    """
    today = now.date().isoformat()
    pages = {slug: dict(row) for slug, row in (previous.pages if previous else {}).items()}
    new: list[str] = []

    def upsert(slug: str, fields: dict[str, Any], source: str) -> None:
        earlier = pages.get(slug)
        if earlier is None:
            new.append(slug)
        pages[slug] = {
            **fields,
            "source": earlier["source"] if earlier else source,
            "first_seen": earlier["first_seen"] if earlier else today,
            "last_seen": today,
        }

    for crawled in result.pages:
        upsert(crawled.page.row["slug"], {**crawled.page.row, "status": "ok", "redirect_to": None}, crawled.source)
    sources = {crawled.requested_url: crawled.source for crawled in result.pages}
    for requested, served in result.aliases.items():
        slug = guide_ref_parts(requested)
        alias = {"slug": slug, "url": requested, **dict.fromkeys(PAGE_FIELDS), "status": "redirect", "redirect_to": guide_ref_parts(served)}
        upsert(slug, alias, sources[requested])
    dropped = [slug for slug in map(guide_ref_parts, result.not_found) if pages.pop(slug, None) is not None]
    stopped_early = result.stop_reason in {"blocked", "unavailable"}
    refreshed_at = previous.refreshed_at if previous and stopped_early else now.isoformat(timespec="seconds")
    merged = SiteIndex(pages, refreshed_at, result.frontier)
    return merged, MergeCounts([slug for slug in new if pages[slug]["status"] == "ok"], dropped)

"""Date-window, text, and facet filters shared by the Wowhead listing commands.

``news``, ``blue-tracker`` and ``guides`` all page through Wowhead listings and filter rows the
same way, so the primitives live here instead of in ``main`` where nothing outside a Typer command
could reach them.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

from wowhead_cli.page_parser import clean_markup_text
from wowhead_cli.wowhead_client import WOWHEAD_BASE_URL

# Wowhead writes listing times as wall-clock US Central with no offset: `news` renders
# "2026/09/18 at 3:30 PM" and `blue-tracker` sends "2026-09-18 18:48:08". Both are confirmed against
# the matching article pages, which carry datePublished 2026-09-18T15:30:00-05:00 and
# 2026-09-18T18:03:10-05:00; a January post carries -06:00, so the offset follows US DST.
WOWHEAD_DISPLAY_TIMEZONE = ZoneInfo("America/Chicago")
_DISPLAY_TIMESTAMP_FORMAT = "%Y/%m/%d at %I:%M %p"


def _parse_iso8601(raw: str) -> datetime | None:
    """ISO 8601 as Wowhead writes it, leaving a missing offset for the caller to interpret."""
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def parse_listing_timestamp(value: Any) -> datetime | None:
    """Parse a Wowhead listing timestamp into an aware UTC value.

    Accepts the rendered news form and ISO 8601. A value with no offset is read as US Central,
    which is the zone Wowhead's listings are written in; reading it as UTC shifts posts across
    day boundaries and makes ``--date-from`` / ``--date-to`` select the wrong rows.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    try:
        parsed: datetime | None = datetime.strptime(raw, _DISPLAY_TIMESTAMP_FORMAT)
    except ValueError:
        parsed = _parse_iso8601(raw)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=WOWHEAD_DISPLAY_TIMEZONE)
    return parsed.astimezone(UTC)


def parse_iso8601_utc(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    parsed = _parse_iso8601(value.strip())
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def parse_date_bound(value: str | None, *, end_of_day: bool) -> datetime | None:
    if value is None or not value.strip():
        return None
    raw = value.strip()
    if "T" not in raw and " " not in raw:
        try:
            parsed_date = datetime.fromisoformat(raw).date()
        except ValueError:
            return None
        if end_of_day:
            return datetime.combine(parsed_date, datetime.max.time(), tzinfo=UTC)
        return datetime.combine(parsed_date, datetime.min.time(), tzinfo=UTC)
    parsed = parse_iso8601_utc(raw)
    if parsed is None:
        return None
    return parsed


def clean_htmlish_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = re.sub(r"<[^>]+>", " ", value)
    text = clean_markup_text(text)
    text = " ".join(text.split())
    return text or None


def absolute_wowhead_url(value: Any, *, fallback: str | None = None) -> str | None:
    if isinstance(value, str) and value.strip():
        return urljoin(WOWHEAD_BASE_URL, value.strip())
    return fallback


def normalize_text_filters(values: list[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    for value in values:
        for part in value.split(","):
            candidate = part.strip().lower()
            if candidate and candidate not in normalized:
                normalized.append(candidate)
    return tuple(normalized)


def text_filter_match(value: Any, filters: tuple[str, ...]) -> bool:
    if not filters:
        return True
    if not isinstance(value, str):
        return False
    return value.strip().lower() in filters


def limited_result_block(rows: list[dict[str, Any]], *, limit: int) -> dict[str, Any]:
    """Return the rows a ``--limit`` allows, plus how many matched before it cut them off."""
    returned = rows[:limit]
    return {
        "count": len(returned),
        "total_matches": len(rows),
        "truncated": len(rows) > len(returned),
        "results": returned,
    }


def collect_timeline_facets(results: list[dict[str, Any]], *, fields: dict[str, str]) -> dict[str, list[str]]:
    facets: dict[str, list[str]] = {}
    for label, key in fields.items():
        values = sorted(
            {
                str(value).strip()
                for row in results
                for value in [row.get(key)]
                if isinstance(value, str) and value.strip()
            }
        )
        facets[label] = values
    return facets

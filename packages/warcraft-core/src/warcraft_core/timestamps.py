"""UTC timestamps as the binaries write and read them: ISO 8601 to the second, with ``Z`` for UTC."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


def iso_now_utc() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso8601_utc(value: Any) -> datetime | None:
    """``value`` as an aware UTC datetime, reading a naive timestamp as UTC; ``None`` when it is not ISO 8601 text."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)

"""Runtime narrowing for JSON payloads.

Provider payloads are ``dict[str, Any]`` all the way down and upstream sources null or omit nested
objects and arrays without warning, so every reader proves a value is a mapping or a sequence
before touching it. These helpers do that once instead of at each call site.
"""

from __future__ import annotations

from typing import Any


def as_dict(value: Any) -> dict[str, Any]:
    """Return ``value`` when it is a JSON object, otherwise an empty object."""
    return value if isinstance(value, dict) else {}


def as_list(value: Any) -> list[Any]:
    """Return ``value`` when it is a JSON array, otherwise an empty array."""
    return value if isinstance(value, list) else []

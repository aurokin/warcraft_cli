"""Conservative filtering of bounded provider candidates for root discovery."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from warcraft_core.shapes import as_dict


def filter_candidates(payload: Mapping[str, Any], *, entity_types: tuple[str, ...], surface: str, limit: int) -> dict[str, Any]:
    """Filter an existing bounded response; never promote a different resolve candidate to certainty."""
    data = dict(as_dict(payload.get("data")))
    field = "results" if surface == "search" else "candidates"
    original = [row for row in data.get(field) or [] if isinstance(row, dict)]
    selected = [row for row in original if (row.get("entity_type") or row.get("kind")) in entity_types]
    data.update({field: selected, "count": len(selected)})
    if surface == "resolve":
        match = as_dict(data.get("match"))
        if (match.get("entity_type") or match.get("kind")) not in entity_types:
            data.update(resolved=False, match=None, confidence=None, next_command=None)
    data["entity_scope"] = {
        "mode": "bounded_candidate_filter",
        "entity_types": list(entity_types),
        "candidate_limit": limit,
        "candidates_examined": len(original),
        "exhaustive": False,
        "note": "Filters the provider's bounded candidate response; unreturned matching candidates may exist.",
    }
    # Original total_matches describes an unfiltered provider query; preserve it under an honest name.
    if "total_matches" in data:
        data["unfiltered_total_matches"] = data.pop("total_matches")
    return {**payload, "data": data}

"""The one data shape every provider's ``search`` and ``resolve`` surfaces return.

Every row in ``results``, ``candidates`` and ``match`` carries the same core: ``provider``, ``kind``,
``id``, ``name``, ``url`` (null when the row has no page), ``ranking.score``,
``ranking.match_reasons``, ``follow_up.command`` and ``follow_up.surface``. Providers add their own
keys beside that core.

``count`` is always the length of the list beside it, ``total_matches`` is how many matches the
provider knows exist, and ``truncated`` says there are more than the list holds. A resolve answer is
``resolved`` exactly when its confidence is ``high``, and only then carries a ``next_command``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final, Literal

SEARCH_KIND: Final = "search_results"
RESOLVE_KIND: Final = "resolve_match"

ResolveConfidence = Literal["high", "medium", "low", "none"]


def discovery_row(
    *,
    provider: str,
    kind: str,
    id: str | int,
    name: str,
    url: str | None,
    score: int,
    match_reasons: Sequence[str],
    command: str | None,
    surface: str,
    ranking_extra: Mapping[str, Any] | None = None,
    follow_up_extra: Mapping[str, Any] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """One search/resolve row: the shared core, then the provider's own keys."""
    return {
        "provider": provider,
        "kind": kind,
        "id": id,
        "name": name,
        "url": url,
        "ranking": {"score": score, "match_reasons": list(match_reasons), **(ranking_extra or {})},
        "follow_up": {"command": command, "surface": surface, **(follow_up_extra or {})},
        **extra,
    }


def search_data(
    *,
    search_query: str | None,
    ranked: Sequence[dict[str, Any]],
    limit: int,
    total_matches: int | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """``search`` data for every ranked row; ``limit`` trims ``results``.

    ``total_matches`` defaults to the ranked list's length; pass the upstream hit count when the
    upstream reports one. The provider's ``extra`` keys come first and never override the core.
    """
    results = list(ranked[:limit])
    total = len(ranked) if total_matches is None else total_matches
    if total < len(ranked):
        raise ValueError(f"total_matches {total} is below the {len(ranked)} ranked rows")
    return {
        **extra,
        "search_query": search_query,
        "count": len(results),
        "total_matches": total,
        "truncated": total > len(results),
        "results": results,
    }


def resolve_data(
    *,
    search_query: str | None,
    ranked: Sequence[dict[str, Any]],
    limit: int,
    confidence: ResolveConfidence,
    fallback_search_command: str | None,
    total_matches: int | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """``resolve`` data judged on every ranked row; ``limit`` trims only ``candidates``.

    Truncating before judging would hide the rivals: ``--limit 1`` leaves one row, which always looks
    unique. The top row is the ``match`` whatever the confidence, and only a ``high`` answer is
    resolved, with its ``follow_up.command`` as the ``next_command``.
    """
    if not ranked and confidence != "none":
        raise ValueError(f"confidence {confidence!r} needs a ranked row")
    match = ranked[0] if ranked else None
    resolved = confidence == "high"
    next_command = match["follow_up"]["command"] if resolved and match is not None else None
    if resolved and not next_command:
        raise ValueError("a high-confidence match needs a follow_up.command")
    page = search_data(search_query=search_query, ranked=ranked, limit=limit, total_matches=total_matches)
    return {
        **extra,
        "search_query": search_query,
        "resolved": resolved,
        "confidence": confidence,
        "match": match,
        "next_command": next_command,
        "fallback_search_command": None if resolved else fallback_search_command,
        "count": page["count"],
        "total_matches": page["total_matches"],
        "truncated": page["truncated"],
        "candidates": page["results"],
    }


def stub_data(
    *,
    surface: Literal["search", "resolve"],
    flag: Literal["coming_soon", "not_supported"],
    search_query: str | None,
    message: str,
    suggested_command: str,
) -> dict[str, Any]:
    """The empty ``search``/``resolve`` data of a surface a provider does not offer, flagged as such.

    ``total_matches`` is null: the provider cannot know how many matches exist.
    """
    if surface == "search":
        data = search_data(search_query=search_query, ranked=[], limit=0)
    else:
        data = resolve_data(search_query=search_query, ranked=[], limit=0, confidence="none", fallback_search_command=None)
    return {**data, "total_matches": None, flag: True, "message": message, "suggested_command": suggested_command}

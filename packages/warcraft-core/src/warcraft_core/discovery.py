"""The one data shape every provider's ``search`` and ``resolve`` surfaces return.

Every row in ``results``, ``candidates`` and ``match`` carries the same core: ``provider``, ``kind``,
``id``, ``name``, ``url`` (null when the row has no page), ``ranking.score``,
``ranking.match_reasons``, ``follow_up.command`` and ``follow_up.surface``. Providers add their own
keys beside that core.

``count`` is always the length of the list beside it, ``total_matches`` is how many matches the
provider knows exist, and ``truncated`` says there are more than the list holds. A resolve answer is
``resolved`` exactly when its confidence is ``high``, and only then carries a ``next_command``; an
unresolved answer with a ``match`` carries the provider's ``fallback_search_command``, and one with no
match carries none, since that search would come back just as empty.

A one-word query is answered at ``high`` only by a row that word names (see ``resolve_data``).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Final, Literal

from warcraft_core.envelope import Envelope, success_envelope

SEARCH_KIND: Final = "search_results"
RESOLVE_KIND: Final = "resolve_match"

ResolveConfidence = Literal["high", "medium", "low", "none"]
# A provider's own "this row is what the word names" test, beyond its name: an exact wiki or API
# title, an expansion alias, a unique spec word naming that spec's guide. Called with the plain
# word and the top row.
SingleWordIdentity = Callable[[str, Mapping[str, Any]], bool]

# Letters, joined only by inner apostrophes or hyphens ("k'aresh"). A digit, '/', ':', '.' or '_'
# makes the query an identifier or a reference instead.
_PLAIN_WORD = re.compile(r"[^\W\d_]+(?:['\u2019-][^\W\d_]+)*")
# What separates a title's head from its qualifier: "Thunderfury, Blessed Blade of the Windseeker".
_TITLE_HEAD_SEPARATOR = re.compile(r"[,:]")


def plain_word(query: str | None) -> str | None:
    """The query casefolded when it is exactly one plain alphabetic word, else ``None``."""
    text = (query or "").strip()
    return text.casefold() if _PLAIN_WORD.fullmatch(text) else None


def _folded(text: str) -> str:
    return " ".join(re.sub(r"['\u2019]", "", text.casefold()).split())


def title_match(query: str, name: str) -> Literal["exact", "title_prefix"] | None:
    """Whether ``query`` names ``name``: the whole name (``exact``), its head before the first ',' or
    ':' (``title_prefix``), or neither.

    Apostrophes, case and spacing are ignored, so "karesh" names "K'aresh". "illidan" does not name
    "Illidan Stormrage", nor "shadow" "In the Catalyst's Shadow". The providers' one-word rule and
    the wrapper's ranking both use this one test.
    """
    folded = _folded(query)
    if not folded:
        return None
    if folded == _folded(name):
        return "exact"
    return "title_prefix" if folded == _folded(_TITLE_HEAD_SEPARATOR.split(name, maxsplit=1)[0]) else None


def single_word_named(word: str, name: str) -> bool:
    """Whether ``word`` names ``name`` (``title_match``)."""
    return title_match(word, name) is not None


def _single_word_capped(
    search_query: str | None, match: Mapping[str, Any] | None, identity: SingleWordIdentity | None
) -> bool:
    """Whether a ``high`` answer to a one-word query falls to ``medium``: the word does not name its match."""
    word = plain_word(search_query)
    if word is None or match is None or single_word_named(word, str(match["name"])):
        return False
    return identity is None or not identity(word, match)


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
    single_word_identity: SingleWordIdentity | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """``resolve`` data judged on every ranked row; ``limit`` trims only ``candidates``.

    Truncating before judging would hide the rivals: ``--limit 1`` leaves one row, which always looks
    unique. The top row is the ``match`` whatever the confidence, and only a ``high`` answer is
    resolved, with its ``follow_up.command`` as the ``next_command``.

    When ``search_query`` (the query after the provider's own stripping) is one plain word, a
    ``high`` answer stays ``high`` only if that word names the match (``single_word_named``) or the
    provider's ``single_word_identity`` accepts it; otherwise it drops to ``medium`` and the data
    carries ``confidence_cap``.
    """
    if not ranked and confidence != "none":
        raise ValueError(f"confidence {confidence!r} needs a ranked row")
    match = ranked[0] if ranked else None
    capped = confidence == "high" and _single_word_capped(search_query, match, single_word_identity)
    if capped:
        confidence = "medium"
    resolved = confidence == "high"
    next_command = match["follow_up"]["command"] if resolved and match is not None else None
    if resolved and not next_command:
        raise ValueError("a high-confidence match needs a follow_up.command")
    page = search_data(search_query=search_query, ranked=ranked, limit=limit, total_matches=total_matches)
    cap = {"confidence_cap": {"rule": "single_word_query", "from": "high"}} if capped else {}
    return {
        **extra,
        **cap,
        "search_query": search_query,
        "resolved": resolved,
        "confidence": confidence,
        "match": match,
        "next_command": next_command,
        "fallback_search_command": None if resolved or match is None else fallback_search_command,
        "count": page["count"],
        "total_matches": page["total_matches"],
        "truncated": page["truncated"],
        "candidates": page["results"],
    }


def stub_envelope(
    *,
    provider: str,
    surface: Literal["search", "resolve"],
    flag: Literal["coming_soon", "not_supported"],
    query: str,
    message: str,
    suggested_command: str,
) -> Envelope:
    """The ``search``/``resolve`` envelope of a surface a provider does not offer: empty data, flagged as such.

    A caller probing the advertised surface gets this instead of Click's "No such command".
    ``total_matches`` is null: the provider cannot know how many matches exist.
    """
    if surface == "search":
        data = search_data(search_query=query, ranked=[], limit=0)
    else:
        data = resolve_data(search_query=query, ranked=[], limit=0, confidence="none", fallback_search_command=None)
    return success_envelope(
        provider=provider,
        command=surface,
        kind=SEARCH_KIND if surface == "search" else RESOLVE_KIND,
        query=query,
        data={**data, "total_matches": None, flag: True, "message": message, "suggested_command": suggested_command},
    )

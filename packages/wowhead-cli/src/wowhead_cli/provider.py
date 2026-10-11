"""Pure Wowhead provider surface.

Every function here returns an ``Envelope`` or raises ``ProviderError``; nothing prints and nothing
raises ``typer.Exit``, so the ``warcraft`` wrapper can call ``PROVIDER`` in-process. The Typer
commands in ``wowhead_cli.main`` are thin wrappers over these functions.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from warcraft_api.cache import CacheSettings, cache_backend_health, load_cache_settings_from_env, redacted_redis_url
from warcraft_core.discovery import RESOLVE_KIND, SEARCH_KIND, resolve_data, search_data
from warcraft_core.envelope import Envelope, success_envelope
from warcraft_core.exit_codes import error_code_for_http_status
from warcraft_core.provider import ProviderError

from wowhead_cli.doctor import build_doctor_payload
from wowhead_cli.entity_types import ENTITY_TYPE_KEYS, RESOLVE_ENTITY_TYPES
from wowhead_cli.expansion_profiles import (
    ExpansionProfile,
    detect_expansion_from_ref,
    is_wowhead_url,
    resolve_expansion,
)
from wowhead_cli.ranking import (
    PROVIDER_NAME,
    URL_PAGE_COMMANDS,
    command_prefix_for_expansion,
    merge_suggestion_lists,
    names_single_word,
    normalize_search_results,
    preferred_resolve_candidates,
    resolve_confidence,
    search_ranking_query,
    search_type_hints,
    split_choices,
    untyped_search_query,
    upstream_rank_bonuses,
    url_entity_result,
    url_page_result,
)
from wowhead_cli.wowhead_client import TALENT_CALC_DATA_TTL_SECONDS, WowheadClient, search_url


@dataclass(frozen=True, slots=True)
class ExpansionSelection:
    """Which Wowhead expansion profile a call runs against, and why it was chosen."""

    profile: ExpansionProfile
    source: str  # "flag" when the caller named it, "url" when auto-detected, otherwise "default"


def select_expansion(expansion: str | None = None, *, url_hint: str | None = None) -> ExpansionSelection:
    """Pick the expansion profile: an explicit key wins, otherwise auto-detect from a Wowhead URL or path."""
    if expansion:
        try:
            return ExpansionSelection(resolve_expansion(expansion), "flag")
        except ValueError as exc:
            raise ProviderError("invalid_argument", str(exc)) from exc
    if url_hint:
        detected = detect_expansion_from_ref(url_hint)
        if detected is not None:
            return ExpansionSelection(detected, "url")
    return ExpansionSelection(resolve_expansion(None), "default")


# The entity type in a tooltip (``/tooltip/item/19019``) or page (``/item=19019/slug``) URL.
_REQUESTED_ENTITY_TYPE_RE = re.compile(
    r"/(?:tooltip/(?P<tooltip>[a-z][a-z-]*)/(?P<tooltip_id>\d+)|(?P<page>[a-z][a-z-]*)=\d+(?:/[^/]*)?)$"
)


def unknown_entity_type_error(entity_type: str, *, details: dict[str, Any], tooltip_id: str | None = None) -> ProviderError:
    """The usage error for a TYPE this CLI does not know that Wowhead did not answer as an entity either.

    Wowhead may have types the table lacks, so such a request still goes out; when Wowhead answers
    404 or with another page, the TYPE is usually misspelled (``items``), not the entity missing.
    Wowhead's tooltip endpoint also 404s on some real page types (``class``, ``title``), so a tooltip
    404 (``tooltip_id`` set) points at ``entity-page`` instead of calling the type wrong.
    """
    known = f"Known entity types: {', '.join(sorted(ENTITY_TYPE_KEYS))}."
    if tooltip_id is not None:
        message = (
            f"Wowhead's tooltip endpoint has no {entity_type!r} {tooltip_id}; if the type is right, "
            f"`wowhead entity-page {entity_type} {tooltip_id}` may still answer. {known}"
        )
    else:
        message = f"{entity_type!r} is not an entity type this CLI knows, and Wowhead did not answer it as one. {known}"
    return ProviderError("invalid_argument", message, details=details)


@contextmanager
def transport_errors() -> Iterator[None]:
    """Translate httpx transport failures into ``ProviderError`` so callers get an envelope, never a traceback.

    The one Wowhead mapping: every command's requests go through it (``main._upstream``).
    """
    try:
        yield
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        details = {"status_code": status, "url": str(exc.request.url)}
        match = _REQUESTED_ENTITY_TYPE_RE.search(exc.request.url.path) if status == 404 else None
        entity_type = (match.group("tooltip") or match.group("page")) if match else None
        if match and entity_type is not None and entity_type not in ENTITY_TYPE_KEYS:
            raise unknown_entity_type_error(entity_type, details=details, tooltip_id=match.group("tooltip_id")) from exc
        raise ProviderError(error_code_for_http_status(status), f"Wowhead returned HTTP {status}", details=details) from exc
    except httpx.TimeoutException as exc:
        raise ProviderError("timeout", f"{type(exc).__name__}: {exc}") from exc
    except httpx.HTTPError as exc:
        raise ProviderError("network_error", str(exc)) from exc


def open_client(profile: ExpansionProfile) -> WowheadClient:
    """Build a Wowhead HTTP client, turning bad cache configuration into a ``ProviderError``."""
    try:
        return WowheadClient(expansion=profile)
    except ValueError as exc:
        raise ProviderError("invalid_cache_config", str(exc)) from exc


def cache_settings_payload(settings: CacheSettings) -> dict[str, Any]:
    """Render resolved cache settings as the JSON block every cache-aware command reports."""
    ttls = settings.ttls
    return {
        "enabled": settings.enabled,
        "backend": settings.backend,
        "cache_dir": str(settings.cache_dir),
        "redis_url": redacted_redis_url(settings.redis_url),
        "prefix": settings.prefix,
        "ttls": {
            "search_suggestions": ttls.search_suggestions,
            "tooltip_meta": ttls.tooltip_meta,
            "entity_page_html": ttls.entity_page_html,
            "guide_page_html": ttls.guide_page_html,
            "page_html": ttls.page_html,
            "comment_replies": ttls.comment_replies,
            "entity_response": ttls.entity_response,
            "talent_calc_data": TALENT_CALC_DATA_TTL_SECONDS,
        },
    }


def envelope(command: str, kind: str, data: dict[str, Any], *, query: Any = None) -> Envelope:
    """Wrap a Wowhead payload in the shared envelope."""
    return success_envelope(provider=PROVIDER_NAME, command=command, kind=kind, data=data, query=query)


def _validated_query(raw: str) -> str:
    """Reject an empty or whitespace-only query locally instead of spending a request on it."""
    query = raw.strip()
    if not query:
        raise ProviderError("invalid_query", "Query cannot be empty.")
    return query


def _url_answer(query: str, *, profile: ExpansionProfile) -> dict[str, Any] | None:
    """The one row a Wowhead URL names, or None for any other query.

    Wowhead's suggestions endpoint matches names, so a Wowhead URL sent there finds nothing; a
    Wowhead URL naming no entity or page a command reads fails instead of answering empty.
    """
    row = url_entity_result(query, expansion=profile) or url_page_result(query, expansion=profile)
    if row is None and is_wowhead_url(query):
        commands = ", ".join([*URL_PAGE_COMMANDS.values(), "entity --url", "entity-page --url"])
        raise ProviderError(
            "invalid_query",
            f"{query!r} is not an entity, guide, news, blue-tracker, tool or listing URL. "
            f"Commands that take a Wowhead URL: {commands}.",
        )
    return row


def _fetch_ranked(
    client: WowheadClient,
    search_query: str,
    *,
    query: str,
    profile: ExpansionProfile,
    entity_types: tuple[str, ...],
    literal: bool,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Fetch Wowhead's suggestions for `search_query`, merge its row lists, and rank them against `query`.

    Returns the ranked rows and the merge summary the payload reports as ``suggestion_merge``, which
    also counts the rows dropped for matching nothing in the query.
    """
    with transport_errors():
        try:
            response = client.search_suggestions(search_query)
        except ValueError as exc:
            raise ProviderError("parse_failed", str(exc)) from exc
    if not isinstance(response.get("results"), list):
        raise ProviderError("invalid_response", "Missing or invalid 'results' payload from Wowhead.")
    rows, merge = merge_suggestion_lists(response)
    ranked, unmatched = normalize_search_results(
        rows,
        query=query,
        expansion=profile,
        entity_types=entity_types,
        rank_bonuses=upstream_rank_bonuses(response, query=query, entity_types=entity_types),
        literal=literal,
    )
    return ranked, {**merge, "unmatched_rows_dropped": unmatched}


def _has_exact_row(ranked: list[dict[str, Any]]) -> bool:
    return any({"exact_name", "exact_display_name"} & set(row["ranking"]["match_reasons"]) for row in ranked)


def _ranked_suggestions(
    client: WowheadClient,
    query: str,
    *,
    profile: ExpansionProfile,
    entity_types: tuple[str, ...] = (),
) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    """Rank Wowhead's suggestions for `query`, returning the text sent upstream, the rows, and the merge summary.

    Follow-up words ("thunderfury comments") are dropped from the upstream text. When the query has
    any, the whole text is tried first, and kept when a row is named exactly that: "Soul Link" and
    "Body and Soul" are spells, not "soul" plus a follow-up word.

    Wowhead matches every word against row names, so a type word ("hogger npc") finds nothing unless
    the names hold it, as guide titles hold "guide". When no row has a type the query names, and no
    row is named exactly the query ("Battle Pet Training" is a spell), the text is sent again without
    its type words, and that answer is kept when it has such a row. A mount, battle pet or recipe
    counts as named by the types Wowhead returns it as (item, spell, NPC).
    """
    literal_query = " ".join(query.lower().split())
    stripped_query = search_ranking_query(query)
    if stripped_query != literal_query:
        ranked, merge = _fetch_ranked(
            client, literal_query, query=query, profile=profile, entity_types=entity_types, literal=True
        )
        if _has_exact_row(ranked):
            return literal_query, ranked, merge
    ranked, merge = _fetch_ranked(
        client, stripped_query, query=query, profile=profile, entity_types=entity_types, literal=False
    )
    hinted = search_type_hints(query)
    untyped_query = untyped_search_query(stripped_query)
    if (
        hinted
        and untyped_query not in ("", stripped_query)
        and not _has_exact_row(ranked)
        and not any(row["entity_type"] in hinted for row in ranked)
    ):
        untyped_ranked, untyped_merge = _fetch_ranked(
            client, untyped_query, query=query, profile=profile, entity_types=entity_types, literal=False
        )
        if any(row["entity_type"] in hinted for row in untyped_ranked):
            return untyped_query, untyped_ranked, untyped_merge
    return stripped_query, ranked, merge


def _entity_type_filter(entity_types: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """The ``--entity-type`` values, limited to the types Wowhead's suggestions can carry."""
    try:
        return split_choices(list(entity_types), allowed=RESOLVE_ENTITY_TYPES, label="entity type")
    except ValueError as exc:
        raise ProviderError("invalid_argument", str(exc)) from exc


def search(
    query: str,
    *,
    limit: int = 10,
    entity_types: tuple[str, ...] | list[str] = (),
    expansion: str | None = None,
    **options: Any,
) -> Envelope:
    """Rank Wowhead search suggestions for a free-text query or Wowhead URL."""
    del options
    query = _validated_query(query)
    selection = select_expansion(expansion, url_hint=query)
    profile = selection.profile
    selected_entity_types = _entity_type_filter(entity_types)
    url_row = _url_answer(query, profile=profile)
    search_query: str | None = None
    merge: dict[str, Any] | None = None
    if url_row is not None:
        normalized = [url_row] if not selected_entity_types or url_row["entity_type"] in selected_entity_types else []
    else:
        search_query, normalized, merge = _ranked_suggestions(
            open_client(profile), query, profile=profile, entity_types=selected_entity_types
        )
    data = search_data(
        search_query=search_query,
        ranked=normalized,
        limit=limit,
        query=query,
        expansion=profile.key,
        expansion_source=selection.source,
        search_url=search_url(search_query, expansion=profile) if search_query is not None else None,
        filters={"entity_types": list(selected_entity_types)},
        suggestion_merge=merge,
    )
    return envelope("search", SEARCH_KIND, data, query=query)


def resolve(
    target: str,
    *,
    limit: int = 5,
    entity_types: tuple[str, ...] | list[str] = (),
    expansion: str | None = None,
    **options: Any,
) -> Envelope:
    """Resolve a query to the single most likely Wowhead entity plus the follow-up command to run."""
    del options
    target = _validated_query(target)
    profile = select_expansion(expansion, url_hint=target).profile
    selected_entity_types = _entity_type_filter(entity_types)
    url_row = _url_answer(target, profile=profile)
    search_query: str | None = None
    merge: dict[str, Any] | None = None
    if url_row is not None:
        # A URL names one thing; outside the --entity-type filter it is no answer, not a match.
        ranked = [url_row] if not selected_entity_types or url_row["entity_type"] in selected_entity_types else []
    else:
        search_query, ranked, merge = _ranked_suggestions(
            open_client(profile), target, profile=profile, entity_types=selected_entity_types
        )
    answering, trailing = preferred_resolve_candidates(ranked)
    # The answering group leads, so the match is its top row whatever the confidence.
    data = resolve_data(
        search_query=search_query,
        ranked=answering + trailing,
        limit=limit,
        confidence=resolve_confidence(answering, query=target, entity_types=selected_entity_types),
        fallback_search_command=f"{command_prefix_for_expansion(profile)} search {shlex.quote(target)}",
        single_word_identity=names_single_word,
        query=target,
        expansion=profile.key,
        search_url=search_url(search_query, expansion=profile) if search_query is not None else None,
        filters={"entity_types": list(selected_entity_types)},
        suggestion_merge=merge,
    )
    return envelope("resolve", RESOLVE_KIND, data, query=target)


def doctor(*, live: bool = True, expansion: str | None = None, **options: Any) -> Envelope:
    """Report Wowhead endpoint reachability, parser shape checks, and cache/runtime readiness."""
    del options
    profile = select_expansion(expansion).profile
    try:
        settings = load_cache_settings_from_env()
    except ValueError as exc:
        raise ProviderError("invalid_cache_config", str(exc)) from exc
    # Settings parse without touching Redis, so ping it, or a dead Redis reads as a cold cache.
    cache = {**cache_settings_payload(settings), **cache_backend_health(settings)}
    data = build_doctor_payload(profile, live=live, cache=cache)
    return envelope("doctor", "doctor", data)


class WowheadProvider:
    """``ProviderSurface`` implementation backed by the pure functions in this module."""

    name = PROVIDER_NAME

    search = staticmethod(search)
    resolve = staticmethod(resolve)
    doctor = staticmethod(doctor)


PROVIDER = WowheadProvider()

__all__ = [
    "PROVIDER",
    "ExpansionSelection",
    "WowheadProvider",
    "cache_settings_payload",
    "doctor",
    "envelope",
    "guide_full",
    "guide_export",
    "open_client",
    "resolve",
    "search",
    "select_expansion",
    "transport_errors",
]


def guide_full(guide_ref: str, *, expansion: str | None = None, max_links: int = 250,
               include_replies: bool = False) -> Envelope:
    """Read the full guide with no command context or console output."""
    from wowhead_cli.guide_services import build_guide_full_payload

    selection = select_expansion(expansion, url_hint=guide_ref)
    with open_client(selection.profile) as client:
        data, _ = build_guide_full_payload(client, guide_ref=guide_ref, max_links=max_links, include_replies=include_replies)
    return envelope("guide-full", "guide_full", data)


def guide_export(guide_ref: str, *, out: Path | None = None, expansion: str | None = None,
                 max_links: int = 250, include_replies: bool = False) -> Envelope:
    """Export a guide through the same service as the CLI, without capturing global output."""
    from wowhead_cli.guide_services import export_guide_bundle

    selection = select_expansion(expansion, url_hint=guide_ref)
    with open_client(selection.profile) as client:
        data = export_guide_bundle(client, guide_ref=guide_ref, out=out, max_links=max_links, include_replies=include_replies)
    return envelope("guide-export", "guide_export", data)


def talent_calc(reference: str, *, listed_build_limit: int = 20, expansion: str | None = None) -> Envelope:
    """Parse and enrich one calculator reference, including provider-backed classic decoding."""
    from wowhead_cli.talent_services import base_talent_calc_payload, enrich_talent_calc_payload
    selection = select_expansion(expansion, url_hint=reference)
    payload = base_talent_calc_payload(selection.profile, ref=reference)
    if not 1 <= listed_build_limit <= 100:
        raise ProviderError("invalid_argument", "listed_build_limit must be between 1 and 100.")
    with open_client(selection.profile) as client:
        payload = enrich_talent_calc_payload(client, payload, listed_build_limit=listed_build_limit,
                                             fail_on_fetch_error=True, decode_talents=True)
    return envelope("talent-calc", "talent_calc", payload)


def talent_calc_packet(reference: str, *, listed_build_limit: int = 20, expansion: str | None = None) -> Envelope:
    """Build an exact transport packet; page metadata is optional evidence, never a requirement."""
    from wowhead_cli.talent_services import base_talent_calc_payload, require_packet_reference, talent_packet_payload
    selection = select_expansion(expansion, url_hint=reference)
    payload = base_talent_calc_payload(selection.profile, ref=reference)
    require_packet_reference(payload)
    if not 1 <= listed_build_limit <= 100:
        raise ProviderError("invalid_argument", "listed_build_limit must be between 1 and 100.")
    with open_client(selection.profile) as client:
        payload = talent_packet_payload(client, payload, listed_build_limit=listed_build_limit)
    return envelope("talent-calc-packet", "talent_calc_packet", payload)

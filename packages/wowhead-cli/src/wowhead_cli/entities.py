"""Entity, entity-page, and compare payload helpers for the Wowhead CLI.

This is the stated public seam for entity behavior: ``wowhead_cli.main`` composes these plain
functions inside its Typer commands, and the entity tests call them directly instead of reaching
into ``main``.
"""

from __future__ import annotations

import shlex
from collections.abc import Callable
from typing import Any

from wowhead_cli.expansion_profiles import ExpansionProfile
from wowhead_cli.page_parser import (
    extract_comments_dataset,
    extract_gatherer_entities,
    extract_linked_entities_from_href,
    normalize_comments,
    sort_comments,
)
from wowhead_cli.ranking import (
    SOURCE_KIND_PRIORITY,
    command_prefix_for_expansion,
    link_source_kinds,
    preview_type_rank,
)
from wowhead_cli.wowhead_client import entity_url

LOW_SIGNAL_LINK_NAMES = frozenset(
    {
        "achievement",
        "battle pet",
        "currency",
        "faction",
        "guide",
        "item",
        "mount",
        "npc",
        "object",
        "pet",
        "quest",
        "recipe",
        "spell",
        "transmog set",
        "zone",
    }
)


def truncate_text(value: Any, *, max_chars: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


def _normalize_link_name(value: Any, *, entity_type: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    name = value.strip()
    if not name:
        return None
    normalized = name.lower()
    if normalized.startswith("http") or normalized.startswith("www.") or "wowhead.com/" in normalized:
        return None
    if entity_type:
        normalized_type = entity_type.replace("-", " ").strip().lower()
        if normalized == normalized_type or normalized == normalized_type.replace(" ", ""):
            return None
    if normalized in LOW_SIGNAL_LINK_NAMES:
        return None
    return name


def _link_name_rank(record: dict[str, Any]) -> int:
    entity_type = record.get("entity_type") if isinstance(record.get("entity_type"), str) else None
    return 0 if _normalize_link_name(record.get("name"), entity_type=entity_type) is not None else 1


def _link_source_rank(record: dict[str, Any]) -> int:
    sources = link_source_kinds(record)
    if len(sources) > 1:
        return 0
    if "gatherer" in sources:
        return 1
    if "href" in sources:
        return 2
    source_kind = record.get("source_kind")
    if source_kind == "gatherer":
        return 1
    if source_kind == "href":
        return 2
    return 3


def _preview_sort_key(record: dict[str, Any], *, source_entity_type: str) -> tuple[int, int, int, int]:
    link_id = record.get("id")
    return (
        _link_name_rank(record),
        preview_type_rank(record, source_entity_type=source_entity_type),
        _link_source_rank(record),
        link_id if isinstance(link_id, int) else 0,
    )


def _preferred_source_kind(record: dict[str, Any]) -> str | None:
    sources = link_source_kinds(record)
    if sources:
        return sources[0]
    source_kind = record.get("source_kind")
    if isinstance(source_kind, str):
        return source_kind
    return None


def _merge_link_records(existing: dict[str, Any], candidate: dict[str, Any], *, link_type: str) -> dict[str, Any]:
    merged = dict(existing)

    existing_name = _normalize_link_name(merged.get("name"), entity_type=link_type)
    candidate_name = _normalize_link_name(candidate.get("name"), entity_type=link_type)
    if existing_name is None and candidate_name is not None:
        merged["name"] = candidate_name
    elif existing_name is not None:
        merged["name"] = existing_name

    for link_field in (
        "url",
        "citation_url",
        "source_url",
        "gatherer_data_type",
    ):
        if merged.get(link_field) in (None, "") and candidate.get(link_field) not in (None, ""):
            merged[link_field] = candidate[link_field]

    source_urls: list[str] = []
    for value in (merged.get("source_url"), candidate.get("source_url")):
        if isinstance(value, str) and value and value not in source_urls:
            source_urls.append(value)
    if source_urls:
        merged["source_urls"] = source_urls

    source_kinds: list[str] = []
    for value in link_source_kinds(existing) + link_source_kinds(candidate):
        if value not in source_kinds:
            source_kinds.append(value)
    if source_kinds:
        merged["sources"] = sorted(source_kinds, key=lambda value: (SOURCE_KIND_PRIORITY.get(value, 99), value))

    preferred_source_kind = _preferred_source_kind(merged) or _preferred_source_kind(candidate)
    if preferred_source_kind is not None:
        merged["source_kind"] = preferred_source_kind

    return merged


def _normalize_link_record(record: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(record)
    sources = link_source_kinds(normalized)
    if sources:
        normalized["sources"] = sources
        normalized["source_kind"] = sources[0]
    elif "sources" in normalized:
        normalized.pop("sources", None)
    source_url = normalized.get("source_url")
    if isinstance(source_url, str) and source_url:
        normalized["source_urls"] = [source_url]
    return normalized


def _select_preview_records(
    records: list[dict[str, Any]],
    *,
    source_entity_type: str,
    limit: int,
) -> list[dict[str, Any]]:
    if limit <= 0 or not records:
        return []
    ranked = sorted(records, key=lambda record: _preview_sort_key(record, source_entity_type=source_entity_type))
    selected: list[dict[str, Any]] = []
    selected_keys: set[tuple[str, int]] = set()
    used_types: set[str] = set()

    for unique_type_only in (True, False):
        for record in ranked:
            entity_type = record.get("entity_type")
            entity_id = record.get("id")
            if not isinstance(entity_type, str) or not isinstance(entity_id, int):
                continue
            key = (entity_type, entity_id)
            if key in selected_keys:
                continue
            if unique_type_only and entity_type in used_types:
                continue
            selected.append(record)
            selected_keys.add(key)
            used_types.add(entity_type)
            if len(selected) >= limit:
                return selected
    return selected[:limit]


def dedupe_links(
    links: list[dict[str, Any]],
    *,
    entity_type: str,
    entity_id: int,
) -> list[dict[str, Any]]:
    """Merge duplicate link records across the whole input, so no later source is cut off early.

    Truncation belongs to the caller (see ``truncated_link_block``): stopping here would drop
    records that only appear late in the list and would hide the real total from the payload.
    """
    deduped: list[dict[str, Any]] = []
    seen_index: dict[tuple[str, int], int] = {}
    for record in links:
        link_type = record.get("entity_type")
        link_id = record.get("id")
        if not isinstance(link_type, str) or not isinstance(link_id, int):
            continue
        if link_type == entity_type and link_id == entity_id:
            continue
        key = (link_type, link_id)
        existing_index = seen_index.get(key)
        if existing_index is not None:
            existing = deduped[existing_index]
            deduped[existing_index] = _merge_link_records(existing, record, link_type=link_type)
            continue
        seen_index[key] = len(deduped)
        deduped.append(_normalize_link_record(record))
    return deduped


def truncated_link_block(deduped: list[dict[str, Any]], *, max_links: int) -> dict[str, Any]:
    """Cut a deduped link list down to ``max_links`` and say so in the payload rather than silently."""
    items = deduped[:max_links]
    return {
        "count": len(items),
        "total": len(deduped),
        "truncated": len(deduped) > len(items),
        "items": items,
    }


def _summarize_linked_entity(record: dict[str, Any]) -> dict[str, Any]:
    entity_type = record.get("entity_type") if isinstance(record.get("entity_type"), str) else None
    return {
        "type": record.get("entity_type"),
        "id": record.get("id"),
        "name": _normalize_link_name(record.get("name"), entity_type=entity_type),
        "url": record.get("url"),
    }


def entity_page_fetch_more_command(
    entity_type: str, entity_id: int, link_count: int, *, expansion: ExpansionProfile
) -> str:
    """The `entity-page` command that returns the full link list, routed to the active expansion."""
    max_links = min(max(link_count, 200), 2000)
    prefix = command_prefix_for_expansion(expansion)
    return f"{prefix} entity-page {shlex.quote(entity_type)} {entity_id} --max-links {max_links}"


def build_linked_entity_preview(
    links: list[dict[str, Any]],
    *,
    entity_type: str,
    entity_id: int,
    preview_limit: int,
    fetch_more_command: str | None = None,
    fetch_more_command_builder: Callable[[int], str] | None = None,
) -> dict[str, Any]:
    def render_fetch_more(count: int) -> str | None:
        if fetch_more_command_builder is not None:
            return fetch_more_command_builder(count)
        return fetch_more_command

    if preview_limit <= 0:
        return {
            "count": 0,
            "counts_by_type": {},
            "items": [],
            "more_available": False,
            "fetch_more_command": render_fetch_more(0),
        }
    deduped = dedupe_links(links, entity_type=entity_type, entity_id=entity_id)
    preview_items = _select_preview_records(deduped, source_entity_type=entity_type, limit=preview_limit)
    counts_by_type: dict[str, int] = {}
    for row in deduped:
        link_type = row.get("entity_type")
        if not isinstance(link_type, str):
            continue
        counts_by_type[link_type] = counts_by_type.get(link_type, 0) + 1
    return {
        "count": len(deduped),
        "counts_by_type": counts_by_type,
        "items": [_summarize_linked_entity(row) for row in preview_items],
        "more_available": len(deduped) > len(preview_items),
        "fetch_more_command": render_fetch_more(len(deduped)),
    }


def entity_page_needs_fetch(
    *,
    include_comments: bool,
    linked_entity_preview_limit: int,
    tooltip_from_page_metadata: bool,
) -> bool:
    return include_comments or linked_entity_preview_limit > 0 or tooltip_from_page_metadata


def entity_comments_payload(
    *,
    html: str | None,
    page_url: str,
    include_comments: bool,
    include_all_comments: bool,
    top_comment_limit: int,
    top_comment_chars: int,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if not include_comments or html is None:
        return None, None
    try:
        raw_comments = extract_comments_dataset(html)
    except ValueError:
        raw_comments = []

    sampled_comments: list[dict[str, Any]] = []
    all_comments: list[dict[str, Any]] = []
    if include_all_comments:
        all_comments = normalize_comments(
            sort_comments(raw_comments, "newest"),
            page_url=page_url,
            include_replies=True,
        )
    else:
        ranked = sort_comments(raw_comments, "rating")
        sampled_norm = normalize_comments(
            ranked[:top_comment_limit],
            page_url=page_url,
            include_replies=False,
        )
        for row in sampled_norm:
            sampled_comments.append(
                {
                    "id": row.get("id"),
                    "user": row.get("user"),
                    "rating": row.get("rating"),
                    "date": row.get("date"),
                    "body": truncate_text(row.get("body"), max_chars=top_comment_chars),
                    "citation_url": row.get("citation_url"),
                }
            )
    all_comments_included = len(all_comments) == len(raw_comments) if include_all_comments else len(sampled_comments) == len(raw_comments)
    comments_payload: dict[str, Any] = {
        "count": len(raw_comments),
        "all_comments_included": all_comments_included,
        "needs_raw_fetch": not all_comments_included,
    }
    if include_all_comments:
        comments_payload["items"] = all_comments
    else:
        comments_payload["top"] = sampled_comments
    return comments_payload, {"comments": f"{page_url}#comments"}


def entity_linked_entities_payload(
    *,
    html: str | None,
    page_url: str,
    page_entity_type: str,
    page_entity_id: int,
    requested_entity_type: str,
    requested_entity_id: int,
    linked_entity_preview_limit: int,
    expansion: ExpansionProfile,
) -> dict[str, Any] | None:
    if html is None or linked_entity_preview_limit <= 0:
        return None
    return build_linked_entity_preview(
        extract_linked_entities_from_href(html, source_url=page_url) + extract_gatherer_entities(html, source_url=page_url),
        entity_type=page_entity_type,
        entity_id=page_entity_id,
        preview_limit=linked_entity_preview_limit,
        fetch_more_command_builder=lambda count: entity_page_fetch_more_command(
            requested_entity_type, requested_entity_id, count, expansion=expansion
        ),
    )


def comparison_entity_record(
    *,
    ref: str,
    entity_type: str,
    entity_id: int,
    canonical_url: str,
    tooltip: dict[str, Any],
    metadata: dict[str, str | None],
    linked_entities: dict[str, Any],
    raw_comments: list[dict[str, Any]],
    sampled_comments: list[dict[str, Any]],
) -> tuple[dict[str, Any], set[tuple[str, int]]]:
    link_set: set[tuple[str, int]] = set()
    for row in linked_entities["items"]:
        link_type = row.get("entity_type")
        link_id = row.get("id")
        if isinstance(link_type, str) and isinstance(link_id, int):
            link_set.add((link_type, link_id))
    return (
        {
            "ref": ref,
            "entity": {
                "type": entity_type,
                "id": entity_id,
                "page_url": canonical_url,
            },
            "summary": {
                "name": tooltip.get("name"),
                "quality": tooltip.get("quality"),
                "icon": tooltip.get("icon"),
                "title": metadata.get("title"),
                "description": metadata.get("description"),
            },
            "linked_entities": linked_entities,
            "comments": {
                "count": len(raw_comments),
                "top": sampled_comments,
            },
            "citations": {
                "comments": f"{canonical_url}#comments",
            },
        },
        link_set,
    )


def comparison_field_diffs(entity_records: list[dict[str, Any]], *, comparable_fields: list[str]) -> dict[str, Any]:
    field_diffs: dict[str, Any] = {}
    for field_name in comparable_fields:
        values: dict[str, Any] = {}
        for row in entity_records:
            ref = row.get("ref")
            if not isinstance(ref, str):
                continue
            summary = row.get("summary")
            value = summary.get(field_name) if isinstance(summary, dict) else None
            values[ref] = value
        unique_values = {repr(v) for v in values.values()}
        field_diffs[field_name] = {
            "all_equal": len(unique_values) <= 1,
            "values": values,
        }
    return field_diffs


def comparison_linked_entities_summary(
    *,
    refs_in_order: list[str],
    entity_link_sets: dict[str, set[tuple[str, int]]],
    expansion: ExpansionProfile,
    max_shared_links: int,
    max_unique_links: int,
) -> dict[str, Any]:
    all_sets = [entity_link_sets[ref] for ref in refs_in_order]
    shared = set.intersection(*all_sets) if all_sets else set()
    shared_links_all = [
        {
            "entity_type": link_type,
            "id": link_id,
            "url": entity_url(link_type, link_id, expansion=expansion),
        }
        for link_type, link_id in sorted(shared)
    ]
    unique_by_ref: dict[str, list[dict[str, Any]]] = {}
    unique_counts: dict[str, int] = {}
    for ref in refs_in_order:
        mine = entity_link_sets[ref]
        others_union: set[tuple[str, int]] = set()
        for other_ref, other_links in entity_link_sets.items():
            if other_ref != ref:
                others_union |= other_links
        unique_pairs = sorted(mine - others_union)
        unique_counts[ref] = len(unique_pairs)
        unique_by_ref[ref] = [
            {
                "entity_type": link_type,
                "id": link_id,
                "url": entity_url(link_type, link_id, expansion=expansion),
            }
            for link_type, link_id in unique_pairs[:max_unique_links]
        ]
    return {
        "shared_count_total": len(shared_links_all),
        "shared_count_returned": min(len(shared_links_all), max_shared_links),
        "shared_items": shared_links_all[:max_shared_links],
        "unique_count_total_by_entity": unique_counts,
        "unique_by_entity": unique_by_ref,
    }

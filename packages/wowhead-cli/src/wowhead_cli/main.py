from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import math
import shlex
from pathlib import Path
import re
from typing import Any, Callable
from urllib.parse import urljoin, urlparse

import httpx
import typer

from warcraft_core.identity import build_identity_payload, build_reference_transport_packet_payload, validate_talent_transport_packet
from warcraft_core.output import (
    DiagnosticsCollector,
    OutputOptions,
    OutputProjectionError,
    resolve_output_options,
    shape_payload,
)
from warcraft_content.guide_analysis import extract_section_chunk_analysis_surfaces
from wowhead_cli.citation_pack import citation_pack_from_compare, citation_pack_from_entity
from wowhead_cli.linked_graph import build_linked_graph_payload, normalize_relation_option
from wowhead_cli.compare_presets import resolve_compare_options
from wowhead_cli.cache import (
    clear_file_cache,
    clear_redis_cache,
    inspect_file_cache,
    inspect_redis_cache,
    load_cache_settings_from_env,
    repair_file_cache,
)
from wowhead_cli.entity_types import (
    DEFAULT_HYDRATE_ENTITY_TYPES,
    HYDRATABLE_ENTITY_TYPES,
    RESOLVE_ENTITY_TYPES,
    SEARCH_TYPE_HINTS,
)
from wowhead_cli.expansion_profiles import ExpansionProfile, list_profiles, resolve_expansion
from wowhead_cli.output import emit
from wowhead_cli.page_parser import (
    clean_markup_text,
    extract_comments_dataset,
    extract_gatherer_entities,
    extract_guide_rating,
    extract_guide_section_chunks,
    extract_guide_sections,
    extract_linked_entities_from_href,
    extract_json_ld,
    extract_json_script,
    extract_listview_data,
    extract_markup_by_target,
    extract_markup_urls,
    normalize_comments,
    parse_page_meta_json,
    parse_page_metadata,
    sort_comments,
)
from wowhead_cli.wowhead_client import (
    WOWHEAD_BASE_URL,
    WowheadClient,
    blue_tracker_url,
    entity_url,
    guide_category_url,
    guide_url,
    news_url,
    search_url,
    suggestion_entity_type,
    tool_url,
)

app = typer.Typer(
    add_completion=False,
    help="Agent-first CLI for querying Wowhead without browser automation.",
)

EXPANSION_PREFIXES = frozenset(
    profile.path_prefix for profile in list_profiles() if profile.path_prefix
)


@dataclass(slots=True)
class RuntimeConfig:
    stream: bool = False
    expansion: ExpansionProfile = field(default_factory=lambda: resolve_expansion(None))
    normalize_canonical_to_expansion: bool = False
    output: OutputOptions = field(default_factory=OutputOptions)
    diagnostics: DiagnosticsCollector = field(default_factory=DiagnosticsCollector)
    citation_pack: bool = False


@dataclass(slots=True)
class EntityAccessPlan:
    requested_type: str
    requested_id: int
    page_entity_type: str
    page_entity_id: int
    tooltip_entity_type: str | None
    tooltip_entity_id: int | None
    tooltip_from_page_metadata: bool = False
    page_from_tooltip_redirect: bool = False


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

PREVIEW_TYPE_PRIORITY: dict[str, int] = {
    "npc": 0,
    "quest": 1,
    "spell": 2,
    "object": 3,
    "item": 4,
    "achievement": 5,
    "zone": 6,
    "faction": 7,
    "currency": 8,
    "pet": 9,
    "battle-pet": 10,
    "mount": 11,
    "recipe": 12,
    "transmog-set": 13,
    "guide": 14,
}

CONTEXTUAL_PREVIEW_TYPE_PRIORITY: dict[str, dict[str, int]] = {
    "currency": {
        "npc": 0,
        "quest": 1,
        "spell": 2,
        "object": 3,
        "faction": 4,
        "zone": 5,
        "item": 6,
        "currency": 7,
    },
    "zone": {
        "npc": 0,
        "quest": 1,
        "object": 2,
        "spell": 3,
        "item": 4,
        "zone": 5,
        "faction": 6,
    },
    "item": {
        "npc": 0,
        "quest": 1,
        "spell": 2,
        "achievement": 3,
        "zone": 4,
        "object": 5,
        "item": 6,
    },
    "npc": {
        "npc": 0,
        "quest": 1,
        "spell": 2,
        "item": 3,
        "object": 4,
    },
    "quest": {
        "npc": 0,
        "item": 1,
        "quest": 2,
        "spell": 3,
        "object": 4,
        "currency": 5,
    },
    "guide": {
        "spell": 0,
        "item": 1,
        "npc": 2,
        "quest": 3,
        "object": 4,
    },
}


def _cfg(ctx: typer.Context) -> RuntimeConfig:
    obj = ctx.obj
    if isinstance(obj, RuntimeConfig):
        return obj
    return RuntimeConfig()


def _client(ctx: typer.Context) -> WowheadClient:
    cfg = _cfg(ctx)
    try:
        return WowheadClient(expansion=cfg.expansion)
    except ValueError as exc:
        _fail(ctx, "invalid_cache_config", str(exc))
        raise AssertionError("unreachable")


def _fail(ctx: typer.Context, code: str, message: str, *, status: int = 1) -> None:
    payload = {
        "ok": False,
        "error": {
            "code": code,
            "message": message,
        },
    }
    cfg = _cfg(ctx)
    emit(payload, pretty=cfg.output.pretty, err=True)
    raise typer.Exit(status)


def _validated_transport_packet(ctx: typer.Context, packet: Any, *, command_name: str) -> dict[str, Any]:
    try:
        return validate_talent_transport_packet(packet)
    except ValueError as exc:
        _fail(ctx, "invalid_transport_packet", f"{command_name} produced an invalid talent transport packet: {exc}")
        raise AssertionError("unreachable")


def _load_cache_settings_or_fail(ctx: typer.Context):
    try:
        return load_cache_settings_from_env()
    except ValueError as exc:
        _fail(ctx, "invalid_cache_config", str(exc))
        raise AssertionError("unreachable")


def _normalize_cache_namespaces(values: list[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in values:
        for candidate in raw.split(","):
            value = candidate.strip()
            if not value:
                continue
            if value in seen:
                continue
            seen.add(value)
            normalized.append(value)
    return tuple(normalized)


def _cache_settings_payload(settings: Any) -> dict[str, Any]:
    ttls = settings.ttls
    return {
        "enabled": settings.enabled,
        "backend": settings.backend,
        "cache_dir": str(settings.cache_dir),
        "redis_url": settings.redis_url,
        "prefix": settings.prefix,
        "ttls": {
            "search_suggestions": ttls.search_suggestions,
            "tooltip_meta": ttls.tooltip_meta,
            "entity_page_html": ttls.entity_page_html,
            "guide_page_html": ttls.guide_page_html,
            "page_html": ttls.page_html,
            "comment_replies": ttls.comment_replies,
            "entity_response": ttls.entity_response,
        },
    }


def _prune_zero_counts(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    filtered: dict[str, Any] = {}
    for key, raw in value.items():
        if isinstance(raw, dict):
            nested = _prune_zero_counts(raw)
            filtered[key] = nested
            continue
        if isinstance(raw, int) and raw == 0 and key in {"active", "expired", "invalid", "total"}:
            continue
        filtered[key] = raw
    return filtered


def _cache_namespace_sort_key(item: tuple[str, Any]) -> tuple[int, str]:
    name, counts = item
    total = counts.get("total") if isinstance(counts, dict) else 0
    return (-int(total or 0), name)


def _cache_stats_payload(stats: dict[str, Any], *, summary: bool, namespace_limit: int, hide_zero: bool) -> dict[str, Any]:
    payload = dict(stats)
    totals = payload.get("totals")
    namespaces = payload.get("namespaces")
    if isinstance(totals, dict) and hide_zero:
        payload["totals"] = _prune_zero_counts(totals)
    if not isinstance(namespaces, dict):
        return payload

    sorted_namespaces = sorted(namespaces.items(), key=_cache_namespace_sort_key)
    if summary:
        top_namespaces: list[dict[str, Any]] = []
        for name, counts in sorted_namespaces[:namespace_limit]:
            row = {"namespace": name}
            if isinstance(counts, dict):
                row.update(_prune_zero_counts(counts) if hide_zero else counts)
            top_namespaces.append(row)
        payload.pop("namespaces", None)
        payload["namespace_count"] = len(namespaces)
        payload["top_namespaces"] = top_namespaces
        payload["truncated_namespaces"] = len(sorted_namespaces) > namespace_limit
        return payload

    payload["namespaces"] = {
        name: (_prune_zero_counts(counts) if hide_zero and isinstance(counts, dict) else counts)
        for name, counts in sorted_namespaces
    }
    return payload


def _build_entity_access_plan(entity_type: str, entity_id: int) -> EntityAccessPlan:
    normalized_type = entity_type.strip().lower()
    if normalized_type == "faction":
        return EntityAccessPlan(
            requested_type=normalized_type,
            requested_id=entity_id,
            page_entity_type="faction",
            page_entity_id=entity_id,
            tooltip_entity_type=None,
            tooltip_entity_id=None,
            tooltip_from_page_metadata=True,
        )
    if normalized_type == "pet":
        return EntityAccessPlan(
            requested_type=normalized_type,
            requested_id=entity_id,
            page_entity_type="pet",
            page_entity_id=entity_id,
            tooltip_entity_type=None,
            tooltip_entity_id=None,
            tooltip_from_page_metadata=True,
        )
    if normalized_type == "recipe":
        return EntityAccessPlan(
            requested_type=normalized_type,
            requested_id=entity_id,
            page_entity_type="spell",
            page_entity_id=entity_id,
            tooltip_entity_type="spell",
            tooltip_entity_id=entity_id,
        )
    if normalized_type == "mount":
        return EntityAccessPlan(
            requested_type=normalized_type,
            requested_id=entity_id,
            page_entity_type=normalized_type,
            page_entity_id=entity_id,
            tooltip_entity_type="mount",
            tooltip_entity_id=entity_id,
            page_from_tooltip_redirect=True,
        )
    if normalized_type == "battle-pet":
        return EntityAccessPlan(
            requested_type=normalized_type,
            requested_id=entity_id,
            page_entity_type=normalized_type,
            page_entity_id=entity_id,
            tooltip_entity_type="battle-pet",
            tooltip_entity_id=entity_id,
            page_from_tooltip_redirect=True,
        )
    return EntityAccessPlan(
        requested_type=normalized_type,
        requested_id=entity_id,
        page_entity_type=normalized_type,
        page_entity_id=entity_id,
        tooltip_entity_type=normalized_type,
        tooltip_entity_id=entity_id,
    )


def _parse_tooltip_final_ref(final_url: str) -> tuple[str, int] | None:
    parsed = urlparse(final_url)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 3 or parts[0] != "tooltip":
        return None
    entity_type = parts[1]
    raw_id = parts[2]
    if not raw_id.isdigit():
        return None
    return entity_type, int(raw_id)


def _build_tooltip_from_page_metadata(metadata: dict[str, str | None]) -> tuple[str | None, dict[str, Any]]:
    title = metadata.get("title")
    description = metadata.get("description")
    parts = [part.strip() for part in (title, description) if isinstance(part, str) and part.strip()]
    payload: dict[str, Any] = {}
    if parts:
        entity_name = title if isinstance(title, str) and title.strip() else None
        tooltip_text = _clean_tooltip_text(" ".join(parts))
        payload["text"] = tooltip_text
        tooltip_summary = _build_tooltip_summary(tooltip_text, entity_name=entity_name)
        if tooltip_summary:
            payload["summary"] = tooltip_summary
    return title if isinstance(title, str) and title.strip() else None, payload


def _parse_entity_ref_token(token: str) -> tuple[str, int]:
    if ":" not in token:
        raise ValueError(f"Invalid entity reference {token!r}. Expected <type>:<id>.")
    entity_type, entity_id_raw = token.split(":", 1)
    if not entity_type:
        raise ValueError(f"Invalid entity reference {token!r}. Missing type.")
    try:
        entity_id = int(entity_id_raw)
    except ValueError as exc:
        raise ValueError(f"Invalid entity id in {token!r}.") from exc
    if entity_id <= 0:
        raise ValueError(f"Entity id must be positive in {token!r}.")
    return entity_type, entity_id


def _parse_guide_id_token(token: str) -> int | None:
    value = token.strip()
    if not value:
        return None
    if value.isdigit():
        guide_id = int(value)
    elif value.startswith("guide="):
        raw_id = value.split("=", 1)[1]
        if not raw_id.isdigit():
            raise ValueError(f"Invalid guide id in {token!r}.")
        guide_id = int(raw_id)
    else:
        return None
    if guide_id <= 0:
        raise ValueError(f"Guide id must be positive in {token!r}.")
    return guide_id


def _extract_guide_id_from_path(path: str) -> int | None:
    for segment in [part for part in path.split("/") if part]:
        if not segment.startswith("guide="):
            continue
        raw_id = segment.split("=", 1)[1]
        if not raw_id.isdigit():
            raise ValueError(f"Invalid guide id in path {path!r}.")
        guide_id = int(raw_id)
        if guide_id <= 0:
            raise ValueError(f"Guide id must be positive in path {path!r}.")
        return guide_id
    return None


def _resolve_guide_lookup_input(
    token: str,
    *,
    expansion: ExpansionProfile,
) -> tuple[str, int | None]:
    raw = token.strip()
    if not raw:
        raise ValueError("Guide reference cannot be empty.")

    direct_id = _parse_guide_id_token(raw)
    if direct_id is not None:
        return guide_url(direct_id, expansion=expansion), direct_id

    parsed = urlparse(raw)
    if parsed.scheme in {"http", "https"}:
        host = (parsed.hostname or "").lower()
        if host != "wowhead.com" and not host.endswith(".wowhead.com"):
            raise ValueError("Guide URL must point to wowhead.com.")
        if not parsed.path:
            raise ValueError("Guide URL is missing a path.")
        guide_id = _extract_guide_id_from_path(parsed.path)
        return raw, guide_id

    normalized = raw.lstrip("/")
    if not normalized:
        raise ValueError("Guide reference cannot be empty.")

    relative_id = _parse_guide_id_token(normalized)
    if relative_id is not None:
        return guide_url(relative_id, expansion=expansion), relative_id

    root_segment = normalized.split("/", 1)[0]
    if root_segment in EXPANSION_PREFIXES:
        lookup_url = f"{WOWHEAD_BASE_URL}/{normalized}"
    else:
        lookup_url = f"{expansion.wowhead_base}/{normalized}"
    guide_id = _extract_guide_id_from_path(f"/{normalized}")
    return lookup_url, guide_id


def _truncate_text(value: Any, *, max_chars: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


def _stream_rows(payload: dict[str, Any]) -> tuple[str, list[Any]] | None:
    for key in ("results", "comments"):
        rows = payload.get(key)
        if isinstance(rows, list) and rows:
            return key, rows
    linked = payload.get("linked_entities")
    if isinstance(linked, dict):
        items = linked.get("items")
        if isinstance(items, list) and items:
            return "linked_entities.items", items
    return None


def _emit_jsonl(ctx: typer.Context, payload: dict[str, Any], *, err: bool = False) -> None:
    from wowhead_cli.output import to_json

    spec = _stream_rows(payload)
    if spec is None:
        emit(payload, pretty=_cfg(ctx).output.pretty, err=err)
        return

    field, rows = spec
    header = dict(payload)
    if field == "linked_entities.items":
        linked = dict(header.get("linked_entities") or {})
        linked["items"] = []
        header["linked_entities"] = linked
    else:
        header[field] = []
    header["stream"] = {"field": field, "count": len(rows)}
    typer.echo(to_json(header, pretty=False), err=err)
    for row in rows:
        typer.echo(to_json({"record": row}, pretty=False), err=err)


def _attach_citation_pack(payload: dict[str, Any], *, enabled: bool) -> dict[str, Any]:
    if not enabled or payload.get("ok") is False:
        return payload
    if "comparison" in payload and isinstance(payload.get("entities"), list):
        payload = dict(payload)
        payload["citation_pack"] = citation_pack_from_compare(payload)
        return payload
    if isinstance(payload.get("entity"), dict):
        payload = dict(payload)
        payload["citation_pack"] = citation_pack_from_entity(payload)
    return payload


def _emit(ctx: typer.Context, payload: dict[str, Any], *, err: bool = False) -> None:
    cfg = _cfg(ctx)
    payload = _attach_citation_pack(payload, enabled=cfg.citation_pack)
    try:
        rendered = shape_payload(payload, cfg.output, diagnostics=cfg.diagnostics)
    except OutputProjectionError as exc:
        _fail(ctx, "missing_fields", str(exc))
        return
    if cfg.stream:
        _emit_jsonl(ctx, rendered, err=err)
        return
    emit(rendered, pretty=cfg.output.pretty, err=err)


def _hydrate_missing_comment_replies(
    client: WowheadClient,
    selected: list[dict[str, Any]],
    *,
    max_concurrency: int,
) -> int:
    from concurrent.futures import ThreadPoolExecutor

    pending: list[tuple[dict[str, Any], int]] = []
    for row in selected:
        if not isinstance(row, dict):
            continue
        comment_id = row.get("id")
        expected = row.get("nreplies")
        current = row.get("replies")
        if not isinstance(comment_id, int) or not isinstance(expected, int):
            continue
        current_count = len(current) if isinstance(current, list) else 0
        if expected <= current_count:
            continue
        pending.append((row, comment_id))

    if not pending:
        return 0

    workers = max(1, min(max_concurrency, len(pending)))
    hydrated = 0

    def _fetch(item: tuple[dict[str, Any], int]) -> tuple[dict[str, Any], list[Any] | None]:
        row, comment_id = item
        try:
            return row, client.comment_replies(comment_id)
        except httpx.HTTPError:
            return row, None

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for row, replies in pool.map(_fetch, pending):
            if replies is None:
                continue
            row["replies"] = replies
            hydrated += 1
    return hydrated


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
    sources = _link_source_kinds(record)
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


def _preview_type_rank(record: dict[str, Any], *, source_entity_type: str) -> int:
    entity_type = record.get("entity_type")
    if not isinstance(entity_type, str):
        return 99
    contextual = CONTEXTUAL_PREVIEW_TYPE_PRIORITY.get(source_entity_type)
    if contextual is not None and entity_type in contextual:
        return contextual[entity_type]
    return PREVIEW_TYPE_PRIORITY.get(entity_type, 99)


def _preview_sort_key(record: dict[str, Any], *, source_entity_type: str) -> tuple[int, int, int, int]:
    link_id = record.get("id")
    return (
        _link_name_rank(record),
        _preview_type_rank(record, source_entity_type=source_entity_type),
        _link_source_rank(record),
        link_id if isinstance(link_id, int) else 0,
    )


SOURCE_KIND_PRIORITY = {
    "gatherer": 0,
    "href": 1,
}


def _link_source_kinds(record: dict[str, Any]) -> list[str]:
    raw_sources = record.get("sources")
    values: list[str] = []
    if isinstance(raw_sources, list):
        for raw in raw_sources:
            if isinstance(raw, str) and raw not in values:
                values.append(raw)
    source_kind = record.get("source_kind")
    if isinstance(source_kind, str) and source_kind not in values:
        values.append(source_kind)
    if not values:
        return []
    return sorted(values, key=lambda value: (SOURCE_KIND_PRIORITY.get(value, 99), value))


def _preferred_source_kind(record: dict[str, Any]) -> str | None:
    sources = _link_source_kinds(record)
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

    for field in (
        "url",
        "citation_url",
        "source_url",
        "gatherer_data_type",
    ):
        if merged.get(field) in (None, "") and candidate.get(field) not in (None, ""):
            merged[field] = candidate[field]

    source_urls: list[str] = []
    for value in (merged.get("source_url"), candidate.get("source_url")):
        if isinstance(value, str) and value and value not in source_urls:
            source_urls.append(value)
    if source_urls:
        merged["source_urls"] = source_urls

    source_kinds: list[str] = []
    for value in _link_source_kinds(existing) + _link_source_kinds(candidate):
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
    sources = _link_source_kinds(normalized)
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


def _dedupe_links(
    links: list[dict[str, Any]],
    *,
    entity_type: str,
    entity_id: int,
    max_links: int,
) -> list[dict[str, Any]]:
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
        if len(deduped) >= max_links:
            break
    return deduped


BRACKET_FRAGMENT_RE = re.compile(r"""\[[^\]]*\]""")
ADJACENT_SENTENCE_RE = re.compile(r"""(?P<sentence>[A-Z][^.?!]{8,}?)(?:[.?!])\s+(?P=sentence)(?:[.?!])""")
FLAVOR_QUOTE_RE = re.compile(r'''\s*"[^"]{20,}"''')
PAREN_OPEN_SPACE_RE = re.compile(r"""\(\s+""")
PAREN_CLOSE_SPACE_RE = re.compile(r"""\s+\)""")
PLUS_STAT_RE = re.compile(r"""(?<!\S)\+\s+(\d)""")
MONEY_LABEL_RE = re.compile(r"""(?P<label>Sell Price:|Cost:)\s+(?P<amount>\d[\d,]*(?:\s+\d[\d,]*){0,2})""")
TOOLTIP_SUMMARY_MARKERS = (
    "Use:",
    "Chance on hit:",
    "Equip:",
    "Chance on strike:",
    "Chance on melee hit:",
)
TOOLTIP_DESCRIPTION_MARKERS = (
    "A ",
    "An ",
    "Calls forth",
    "Blasts",
    "Deals",
    "Heals",
    "Summons",
    "Teleport",
    "Help ",
)
TOOLTIP_METADATA_TERMS = (
    "Talent",
    "Passive",
    "Instant",
    "Requires",
    "Range",
    "Melee",
    "Cooldown",
    "Cast",
    "Runes",
)
SENTENCE_END_RE = re.compile(r"""[.?!](?:\s|$)""")


def _format_money_amount(amount: str) -> str:
    parts = amount.split()
    if len(parts) == 1:
        return f"{parts[0]}g"
    suffixes = ("g", "s", "c")
    formatted = [f"{part}{suffixes[index]}" for index, part in enumerate(parts[: len(suffixes)])]
    return " ".join(formatted)


def _clean_tooltip_text(text: str) -> str:
    cleaned = BRACKET_FRAGMENT_RE.sub(" ", text)
    cleaned = FLAVOR_QUOTE_RE.sub(" ", cleaned)
    cleaned = cleaned.replace("[", " ").replace("]", " ")
    cleaned = PAREN_OPEN_SPACE_RE.sub("(", cleaned)
    cleaned = PAREN_CLOSE_SPACE_RE.sub(")", cleaned)
    cleaned = PLUS_STAT_RE.sub(r"+\1", cleaned)
    cleaned = MONEY_LABEL_RE.sub(lambda match: f"{match.group('label')} {_format_money_amount(match.group('amount'))}", cleaned)
    cleaned = cleaned.replace(" .", ".").replace(" ,", ",")
    cleaned = " ".join(cleaned.split())
    while True:
        collapsed = ADJACENT_SENTENCE_RE.sub(r"\g<sentence>.", cleaned)
        if collapsed == cleaned:
            break
        cleaned = collapsed
    return cleaned.strip()


def _strip_leading_entity_name(text: str, *, entity_name: str | None) -> str:
    if not entity_name:
        return text
    if not text.startswith(entity_name):
        return text
    remainder = text[len(entity_name) :].lstrip(" :-")
    return remainder.strip() or text


def _prefer_tooltip_summary_span(text: str) -> str:
    best_index: int | None = None
    for marker in TOOLTIP_SUMMARY_MARKERS:
        index = text.find(marker)
        if index <= 0:
            continue
        if best_index is None or index < best_index:
            best_index = index
    if best_index is None:
        return text
    prefix = text[:best_index].strip()
    if len(prefix) < 24:
        return text
    return text[best_index:].strip()


def _prefer_first_summary_sentence(text: str) -> str:
    match = SENTENCE_END_RE.search(text)
    if match is None:
        return text
    sentence = text[: match.end()].strip()
    if len(sentence) < 24:
        return text
    return sentence


def _prefer_descriptive_summary_span(text: str) -> str:
    if any(text.startswith(marker) for marker in TOOLTIP_SUMMARY_MARKERS):
        return text
    prefix_lower = text.lower()
    if not any(term.lower() in prefix_lower for term in TOOLTIP_METADATA_TERMS):
        return text

    best_index: int | None = None
    for marker in TOOLTIP_DESCRIPTION_MARKERS:
        index = text.find(marker)
        if index < 12:
            continue
        if best_index is None or index < best_index:
            best_index = index

    if best_index is None:
        return text
    return text[best_index:].strip()


def _build_tooltip_summary(text: str, *, entity_name: str | None, max_chars: int = 220) -> str | None:
    if not text:
        return None
    summary = _strip_leading_entity_name(text, entity_name=entity_name)
    summary = _prefer_tooltip_summary_span(summary)
    summary = _prefer_descriptive_summary_span(summary)
    summary = _prefer_first_summary_sentence(summary)
    if len(summary) <= max_chars:
        return summary
    clipped = summary[: max_chars - 3]
    if " " in clipped:
        clipped = clipped.rsplit(" ", 1)[0]
    return clipped.rstrip(" ,;:-") + "..."


def _normalize_tooltip_payload(tooltip: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    entity_name = tooltip.get("name")
    name = entity_name if isinstance(entity_name, str) and entity_name.strip() else None

    tooltip_payload = dict(tooltip)
    tooltip_html = tooltip_payload.pop("tooltip", None)
    tooltip_payload.pop("name", None)

    if isinstance(tooltip_html, str):
        tooltip_payload["html"] = tooltip_html
        tooltip_text = _clean_tooltip_text(clean_markup_text(tooltip_html))
        tooltip_payload["text"] = tooltip_text
        tooltip_summary = _build_tooltip_summary(tooltip_text, entity_name=name)
        if tooltip_summary:
            tooltip_payload["summary"] = tooltip_summary
    elif isinstance(tooltip_payload.get("text"), str):
        tooltip_text = _clean_tooltip_text(str(tooltip_payload["text"]))
        tooltip_payload["text"] = tooltip_text
        tooltip_summary = _build_tooltip_summary(tooltip_text, entity_name=name)
        if tooltip_summary:
            tooltip_payload["summary"] = tooltip_summary

    return name, tooltip_payload


def _summarize_linked_entity(record: dict[str, Any]) -> dict[str, Any]:
    entity_type = record.get("entity_type") if isinstance(record.get("entity_type"), str) else None
    return {
        "type": record.get("entity_type"),
        "id": record.get("id"),
        "name": _normalize_link_name(record.get("name"), entity_type=entity_type),
        "url": record.get("url"),
    }


def _entity_page_fetch_more_command(entity_type: str, entity_id: int, link_count: int) -> str:
    max_links = min(max(link_count, 200), 2000)
    return f"wowhead entity-page {entity_type} {entity_id} --max-links {max_links}"


def _fetch_entity_page(
    ctx: typer.Context,
    client: WowheadClient,
    entity_type: str,
    entity_id: int,
) -> tuple[str, dict[str, str | None]]:
    try:
        html = client.entity_page_html(entity_type, entity_id)
    except httpx.HTTPStatusError as exc:
        _fail(ctx, "http_error", f"Wowhead returned HTTP {exc.response.status_code}")
    except httpx.HTTPError as exc:
        _fail(ctx, "network_error", str(exc))
    fallback_url = entity_url(entity_type, entity_id, expansion=client.expansion)
    metadata = parse_page_metadata(html, fallback_url=fallback_url)
    return html, metadata


def _resolve_page_fetch_target(
    ctx: typer.Context,
    client: WowheadClient,
    *,
    entity_type: str,
    entity_id: int,
    data_env: int | None = None,
) -> EntityAccessPlan:
    plan = _build_entity_access_plan(entity_type, entity_id)
    if not plan.page_from_tooltip_redirect:
        return plan
    if plan.tooltip_entity_type is None or plan.tooltip_entity_id is None:
        _fail(ctx, "unsupported_entity_type", f"{entity_type!r} does not define a tooltip route for page resolution.")
    try:
        _, final_url = client.tooltip_with_metadata(
            plan.tooltip_entity_type,
            plan.tooltip_entity_id,
            data_env=data_env,
        )
    except httpx.HTTPStatusError as exc:
        _fail(ctx, "http_error", f"Wowhead returned HTTP {exc.response.status_code}")
    except httpx.HTTPError as exc:
        _fail(ctx, "network_error", str(exc))
    except ValueError as exc:
        _fail(ctx, "parse_error", str(exc))

    resolved = _parse_tooltip_final_ref(final_url)
    if resolved is None:
        _fail(ctx, "unexpected_response", f"Could not resolve a page target for {entity_type} {entity_id}.")
    page_entity_type, page_entity_id = resolved
    plan.page_entity_type = page_entity_type
    plan.page_entity_id = page_entity_id
    return plan


def _normalize_canonical_entity_url(
    raw_url: str | None,
    *,
    expansion: ExpansionProfile,
    entity_type: str,
    entity_id: int,
) -> str:
    base = entity_url(entity_type, entity_id, expansion=expansion)
    if not raw_url:
        return base
    parsed = urlparse(raw_url)
    parts = [part for part in parsed.path.split("/") if part]
    marker = f"{entity_type}={entity_id}"
    try:
        index = parts.index(marker)
    except ValueError:
        return base
    if index + 1 < len(parts):
        return f"{base}/{parts[index + 1]}"
    return base


def _slugify_path_fragment(value: str) -> str:
    slug_chars: list[str] = []
    last_dash = False
    for char in value.lower():
        if char.isalnum():
            slug_chars.append(char)
            last_dash = False
            continue
        if last_dash:
            continue
        slug_chars.append("-")
        last_dash = True
    rendered = "".join(slug_chars).strip("-")
    return rendered or "guide"


def _guide_export_root() -> Path:
    return Path.cwd() / "wowhead_exports"


def _default_guide_export_dir(payload: dict[str, Any]) -> Path:
    guide = payload.get("guide")
    page = payload.get("page")
    guide_id = guide.get("id") if isinstance(guide, dict) else None
    title = page.get("title") if isinstance(page, dict) else None
    slug_source = title if isinstance(title, str) and title.strip() else str(guide_id or "guide")
    if isinstance(guide_id, int):
        name = f"guide-{guide_id}-{_slugify_path_fragment(slug_source)}"
    else:
        name = f"guide-{_slugify_path_fragment(slug_source)}"
    return _guide_export_root() / name


def _write_json_file(path: Path, payload: Any) -> None:
    rendered = json.dumps(payload, indent=2, sort_keys=True)
    path.write_text(f"{rendered}\n", encoding="utf-8")


def _write_jsonl_file(path: Path, rows: list[Any]) -> None:
    content = "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows)
    path.write_text(content, encoding="utf-8")


def _write_optional_text_file(path: Path, value: Any) -> bool:
    if not isinstance(value, str):
        return False
    path.write_text(value, encoding="utf-8")
    return True


def _iso_now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _hydrate_guide_linked_entities(
    ctx: typer.Context,
    client: WowheadClient,
    *,
    linked_items: list[Any],
    hydrate_types: tuple[str, ...],
    hydrate_limit: int,
) -> list[dict[str, Any]]:
    hydrated_rows: list[dict[str, Any]] = []
    selected_types = set(hydrate_types)
    for row in linked_items:
        if len(hydrated_rows) >= hydrate_limit:
            break
        if not isinstance(row, dict):
            continue
        entity_type = row.get("entity_type")
        entity_id = row.get("id")
        if not isinstance(entity_type, str) or not isinstance(entity_id, int):
            continue
        if entity_type not in selected_types:
            continue
        payload = _build_entity_payload(
            ctx,
            client,
            entity_type=entity_type,
            entity_id=entity_id,
            data_env=None,
            include_comments=False,
            include_all_comments=False,
            linked_entity_preview_limit=0,
        )
        hydrated_rows.append(payload)
    return hydrated_rows


def _read_json_file(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_jsonl_file(path: Path) -> list[Any]:
    rows: list[Any] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rows.append(json.loads(line))
    return rows


def _query_terms(query: str) -> list[str]:
    normalized = " ".join(query.lower().split())
    return [term for term in normalized.split(" ") if term]


def _score_text_match(query: str, *values: Any) -> int:
    terms = _query_terms(query)
    if not terms:
        return 0
    haystacks = []
    for value in values:
        if isinstance(value, str) and value.strip():
            haystacks.append(value.lower())
    if not haystacks:
        return 0
    score = 0
    for term in terms:
        for haystack in haystacks:
            if term in haystack:
                score += 1
    joined = " ".join(haystacks)
    query_normalized = " ".join(query.lower().split())
    if query_normalized and query_normalized in joined:
        score += max(2, len(terms))
    return score


FOLLOW_UP_COMMENT_TERMS = {"comment", "comments", "discussion", "discussions"}
FOLLOW_UP_RELATION_TERMS = {
    "body",
    "detail",
    "details",
    "entities",
    "full",
    "link",
    "linked",
    "links",
    "markup",
    "reference",
    "references",
    "related",
    "relation",
    "relations",
    "source",
    "sources",
}


def _search_type_hints(query: str) -> set[str]:
    normalized = " ".join(query.lower().split())
    if not normalized:
        return set()
    hinted: set[str] = set()
    for entity_type, phrases in SEARCH_TYPE_HINTS.items():
        for phrase in phrases:
            if phrase in normalized:
                hinted.add(entity_type)
                break
    return hinted


def _search_ranking_query(query: str) -> str:
    filtered_terms = [
        term
        for term in _query_terms(query)
        if term not in FOLLOW_UP_COMMENT_TERMS and term not in FOLLOW_UP_RELATION_TERMS
    ]
    if filtered_terms:
        return " ".join(filtered_terms)
    return " ".join(query.lower().split())


def _search_follow_up_kind(query: str) -> str:
    terms = set(_query_terms(query))
    if terms & FOLLOW_UP_COMMENT_TERMS:
        return "comments"
    if terms & FOLLOW_UP_RELATION_TERMS:
        return "relations"
    return "summary"


def _search_follow_up(candidate: dict[str, Any], *, query: str, expansion: ExpansionProfile) -> dict[str, Any] | None:
    entity_type = candidate.get("entity_type")
    entity_id = candidate.get("id")
    if not isinstance(entity_type, str) or not isinstance(entity_id, int):
        return None

    prefix = _command_prefix_for_expansion(expansion)
    intent = _search_follow_up_kind(query)
    if entity_type == "guide":
        guide_command = f"{prefix} guide {entity_id}"
        guide_full_command = f"{prefix} guide-full {entity_id}"
        recommended_command = guide_command
        recommended_surface = "guide"
        reason = "guide_summary"
        alternatives = [guide_full_command]
        if intent == "relations":
            recommended_command = guide_full_command
            recommended_surface = "guide-full"
            reason = "guide_relation_intent"
            alternatives = [guide_command]
        elif intent == "comments":
            reason = "guide_comment_intent"
            alternatives = [guide_full_command]
        return {
            "recommended_surface": recommended_surface,
            "recommended_command": recommended_command,
            "reason": reason,
            "alternatives": alternatives,
        }

    if entity_type not in RESOLVE_ENTITY_TYPES:
        return None

    entity_command = f"{prefix} entity {entity_type} {entity_id}"
    entity_page_command = f"{prefix} entity-page {entity_type} {entity_id}"
    comments_command = f"{prefix} comments {entity_type} {entity_id}"
    recommended_command = entity_command
    recommended_surface = "entity"
    reason = "entity_summary"
    alternatives = [entity_page_command, comments_command]
    if intent == "relations":
        recommended_command = entity_page_command
        recommended_surface = "entity-page"
        reason = "entity_relation_intent"
        alternatives = [entity_command, comments_command]
    elif intent == "comments":
        recommended_command = comments_command
        recommended_surface = "comments"
        reason = "entity_comment_intent"
        alternatives = [entity_command, entity_page_command]
    return {
        "recommended_surface": recommended_surface,
        "recommended_command": recommended_command,
        "reason": reason,
        "alternatives": alternatives,
    }


def _exact_match_score(normalized_query: str, *, name_normalized: str, display_normalized: str) -> tuple[int, list[str]]:
    if normalized_query and name_normalized == normalized_query:
        return 30, ["exact_name"]
    if normalized_query and display_normalized == normalized_query:
        return 26, ["exact_display_name"]
    return 0, []


def _prefix_and_contains_score(normalized_query: str, *, name_normalized: str, display_normalized: str) -> tuple[int, list[str]]:
    if normalized_query and name_normalized.startswith(normalized_query):
        return 10, ["name_prefix"]
    if normalized_query and display_normalized.startswith(normalized_query):
        return 8, ["display_name_prefix"]
    if normalized_query and name_normalized and normalized_query in name_normalized:
        return 14, ["name_contains_query"]
    if normalized_query and display_normalized and normalized_query in display_normalized:
        return 12, ["display_name_contains_query"]
    return 0, []


def _term_match_score(terms: set[str], *, haystacks: list[str]) -> tuple[int, list[str]]:
    if not terms or not haystacks:
        return 0, []
    joined = " ".join(haystacks)
    if all(term in joined for term in terms):
        return len(terms) * 3, ["all_terms_match"]
    return 0, []


def _type_hint_score(query: str, *, entity_type: str | None) -> tuple[int, list[str]]:
    hinted_types = _search_type_hints(query)
    if entity_type in hinted_types:
        return 9, ["type_hint"]
    return 0, []


def _popularity_score(popularity: int, *, entity_type: str | None) -> tuple[int, list[str]]:
    reasons: list[str] = []
    score = 0
    if popularity > 0:
        score += min(6, int(math.log10(popularity + 1) * 2))
        reasons.append("popularity")
    if entity_type is not None:
        score += 1
    return score, reasons


def _search_result_score_and_reasons(
    row: dict[str, Any], *, query: str, ranking_query: str
) -> tuple[int, list[str]]:
    normalized_query = " ".join(ranking_query.lower().split())
    terms = _query_terms(ranking_query)
    name = row.get("name") if isinstance(row.get("name"), str) else ""
    display_name = row.get("displayName") if isinstance(row.get("displayName"), str) else ""
    type_name = row.get("typeName") if isinstance(row.get("typeName"), str) else ""
    entity_type = suggestion_entity_type(row)
    popularity = row.get("popularity") if isinstance(row.get("popularity"), int) else 0

    haystacks = [value.lower() for value in (name, display_name, type_name) if value]
    name_normalized = name.lower().strip()
    display_normalized = display_name.lower().strip()
    reasons: list[str] = []
    score = 0

    for part_score, part_reasons in (
        _exact_match_score(normalized_query, name_normalized=name_normalized, display_normalized=display_normalized),
        _prefix_and_contains_score(normalized_query, name_normalized=name_normalized, display_normalized=display_normalized),
        _term_match_score(terms, haystacks=haystacks),
        _type_hint_score(query, entity_type=entity_type),
        _popularity_score(popularity, entity_type=entity_type),
    ):
        score += part_score
        reasons.extend(part_reasons)

    unique_reasons: list[str] = []
    seen: set[str] = set()
    for reason in reasons:
        if reason in seen:
            continue
        seen.add(reason)
        unique_reasons.append(reason)
    return score, unique_reasons


def _normalize_resolve_entity_types(values: list[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in values:
        for candidate in raw.split(","):
            value = candidate.strip().lower()
            if not value:
                continue
            if value not in RESOLVE_ENTITY_TYPES:
                raise ValueError(
                    f"Unsupported resolve entity type {value!r}. Expected one of: {', '.join(sorted(RESOLVE_ENTITY_TYPES))}."
                )
            if value in seen:
                continue
            seen.add(value)
            normalized.append(value)
    return tuple(normalized)


def _search_result_url(*, entity_type: str | None, entity_id: int | None, expansion: ExpansionProfile) -> str | None:
    if not isinstance(entity_id, int):
        return None
    if entity_type == "guide":
        return guide_url(entity_id, expansion=expansion)
    if entity_type:
        return entity_url(entity_type, entity_id, expansion=expansion)
    return None


def _normalize_search_results(
    results: list[Any],
    *,
    query: str,
    expansion: ExpansionProfile,
    entity_types: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    selected_entity_types = set(entity_types)
    ranking_query = _search_ranking_query(query)
    normalized: list[dict[str, Any]] = []
    for index, row in enumerate(results):
        if not isinstance(row, dict):
            continue
        entity_type = suggestion_entity_type(row)
        if selected_entity_types and entity_type not in selected_entity_types:
            continue
        entity_id = row.get("id")
        popularity = row.get("popularity") if isinstance(row.get("popularity"), int) else 0
        search_score, match_reasons = _search_result_score_and_reasons(
            row,
            query=query,
            ranking_query=ranking_query,
        )
        candidate = {
            "id": entity_id,
            "name": row.get("name"),
            "type_id": row.get("type"),
            "type_name": row.get("typeName"),
            "entity_type": entity_type,
            "url": _search_result_url(entity_type=entity_type, entity_id=entity_id if isinstance(entity_id, int) else None, expansion=expansion),
            "ranking": {
                "score": search_score,
                "match_reasons": match_reasons,
            },
            "metadata": {
                "popularity": popularity,
                "icon": row.get("icon"),
                "quality": row.get("quality"),
                "side": row.get("side"),
                "display_name": row.get("displayName"),
            },
            "_sort": (-search_score, -popularity, index),
        }
        follow_up = _search_follow_up(candidate, query=query, expansion=expansion)
        if follow_up is not None:
            candidate["follow_up"] = follow_up
        normalized.append(candidate)
    normalized.sort(key=lambda row: row["_sort"])
    for row in normalized:
        row.pop("_sort", None)
    return normalized


def _command_prefix_for_expansion(expansion: ExpansionProfile) -> str:
    if expansion.key == resolve_expansion(None).key:
        return "wowhead"
    return f"wowhead --expansion {expansion.key}"


def _resolve_next_command(candidate: dict[str, Any], *, expansion: ExpansionProfile) -> str | None:
    follow_up = candidate.get("follow_up") if isinstance(candidate, dict) else None
    if isinstance(follow_up, dict):
        command = follow_up.get("recommended_command")
        if isinstance(command, str) and command:
            return command
    entity_type = candidate.get("entity_type")
    entity_id = candidate.get("id")
    if not isinstance(entity_type, str) or not isinstance(entity_id, int):
        return None
    prefix = _command_prefix_for_expansion(expansion)
    if entity_type == "guide":
        return f"{prefix} guide {entity_id}"
    if entity_type in RESOLVE_ENTITY_TYPES:
        return f"{prefix} entity {entity_type} {entity_id}"
    return None


def _resolve_confidence(candidates: list[dict[str, Any]], *, entity_types: tuple[str, ...]) -> str:
    if not candidates:
        return "none"
    top_ranking = candidates[0].get("ranking", {})
    top_score = int(top_ranking.get("score") or 0)
    second_score = int(candidates[1].get("ranking", {}).get("score") or 0) if len(candidates) > 1 else 0
    margin = top_score - second_score
    reasons = set(top_ranking.get("match_reasons") or [])
    if _is_high_confidence_exact_match(reasons, margin=margin, second_score=second_score):
        return "high"
    if _is_high_confidence_score(top_score, margin=margin):
        return "high"
    if _is_filtered_high_confidence(entity_types, top_score=top_score, margin=margin):
        return "high"
    if _is_medium_confidence_score(top_score, margin=margin):
        return "medium"
    return "low"


def _is_high_confidence_exact_match(reasons: set[str], *, margin: int, second_score: int) -> bool:
    return ("exact_name" in reasons or "exact_display_name" in reasons) and (margin >= 4 or second_score == 0)


def _is_high_confidence_score(top_score: int, *, margin: int) -> bool:
    return top_score >= 24 and margin >= 6


def _is_filtered_high_confidence(entity_types: tuple[str, ...], *, top_score: int, margin: int) -> bool:
    return bool(entity_types) and top_score >= 18 and margin >= 4


def _is_medium_confidence_score(top_score: int, *, margin: int) -> bool:
    return top_score >= 18 and margin >= 4


def _truncate_preview(value: str, *, max_chars: int = 220) -> str:
    text = " ".join(value.split())
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


def _normalize_query_kinds(values: list[str]) -> tuple[str, ...]:
    allowed = {
        "sections",
        "analysis_surfaces",
        "navigation",
        "linked_entities",
        "gatherer_entities",
        "comments",
    }
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in values:
        for candidate in raw.split(","):
            value = candidate.strip().lower()
            if not value:
                continue
            if value not in allowed:
                raise ValueError(
                    f"Unsupported query kind {value!r}. Expected one of: {', '.join(sorted(allowed))}."
                )
            if value in seen:
                continue
            seen.add(value)
            normalized.append(value)
    return tuple(normalized)


def _normalize_link_source_filters(values: list[str]) -> tuple[str, ...]:
    allowed = {"href", "gatherer", "multi"}
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in values:
        for candidate in raw.split(","):
            value = candidate.strip().lower()
            if not value:
                continue
            if value not in allowed:
                raise ValueError(
                    f"Unsupported linked source filter {value!r}. Expected one of: {', '.join(sorted(allowed))}."
                )
            if value in seen:
                continue
            seen.add(value)
            normalized.append(value)
    return tuple(normalized)


def _normalize_hydrate_types(values: list[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    seen: set[str] = set()
    raw_values = values or list(DEFAULT_HYDRATE_ENTITY_TYPES)
    for raw in raw_values:
        for candidate in raw.split(","):
            value = candidate.strip().lower()
            if not value:
                continue
            if value not in HYDRATABLE_ENTITY_TYPES:
                raise ValueError(
                    f"Unsupported hydrate entity type {value!r}. Expected one of: {', '.join(sorted(HYDRATABLE_ENTITY_TYPES))}."
                )
            if value in seen:
                continue
            seen.add(value)
            normalized.append(value)
    return tuple(normalized)


def _linked_source_filter_matches(record: dict[str, Any], *, selected_sources: tuple[str, ...]) -> bool:
    if not selected_sources:
        return True
    sources = set(_link_source_kinds(record))
    if "multi" in selected_sources and len(sources) > 1:
        return True
    for source in selected_sources:
        if source == "multi":
            continue
        if source in sources:
            return True
    return False


def _linked_source_match_rank(record: dict[str, Any]) -> int:
    sources = _link_source_kinds(record)
    if len(sources) > 1:
        return 0
    if "gatherer" in sources:
        return 1
    if "href" in sources:
        return 2
    return 3


def _linked_entity_query_score(row: dict[str, Any], *, query: str) -> int:
    score = _score_text_match(query, row.get("name"), row.get("entity_type"), row.get("url"))
    if score <= 0:
        return 0
    score += _score_text_match(query, row.get("name"))
    if len(_link_source_kinds(row)) > 1:
        score += 1
    return score


GUIDE_QUERY_KIND_PRIORITY = {
    "linked_entity": 0,
    "section": 1,
    "analysis_surface": 2,
    "navigation": 3,
    "comment": 4,
    "gatherer_entity": 5,
}


def _guide_query_match_sort_key(row: dict[str, Any]) -> tuple[int, int, int, int, str, int]:
    kind = row.get("kind") if isinstance(row.get("kind"), str) else ""
    entity_type = row.get("entity_type") if isinstance(row.get("entity_type"), str) else ""
    link_id = row.get("id")
    ordinal = row.get("ordinal")
    numeric = ordinal if isinstance(ordinal, int) else (link_id if isinstance(link_id, int) else 0)
    if kind == "linked_entity":
        return (
            -int(row.get("score") or 0),
            GUIDE_QUERY_KIND_PRIORITY.get(kind, 99),
            _linked_source_match_rank(row),
            _preview_type_rank(row, source_entity_type="guide"),
            entity_type,
            numeric,
        )
    return (
        -int(row.get("score") or 0),
        GUIDE_QUERY_KIND_PRIORITY.get(kind, 99),
        99,
        99,
        entity_type or kind,
        numeric,
    )


def _guide_query_top_dedupe_key(row: dict[str, Any]) -> tuple[str, int] | None:
    kind = row.get("kind")
    if kind not in {"linked_entity", "gatherer_entity"}:
        return None
    entity_type = row.get("entity_type")
    entity_id = row.get("id")
    if not isinstance(entity_type, str) or not isinstance(entity_id, int):
        return None
    return entity_type, entity_id


def _guide_query_kind_enabled(selected_kinds: tuple[str, ...], value: str) -> bool:
    if not selected_kinds:
        return True
    return value in selected_kinds


def _guide_section_matches(
    *,
    sections: list[Any],
    query: str,
    section_title_filter: str | None,
) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for row in sections:
        if not isinstance(row, dict):
            continue
        title = row.get("title")
        if section_title_filter and (not isinstance(title, str) or section_title_filter not in title.lower()):
            continue
        score = _score_text_match(query, row.get("title"), row.get("content_text"))
        if score <= 0:
            continue
        matches.append(
            {
                "kind": "section",
                "score": score + _score_text_match(query, row.get("title")),
                "ordinal": row.get("ordinal"),
                "level": row.get("level"),
                "title": row.get("title"),
                "preview": _truncate_preview(row.get("content_text") or ""),
                "citation_url": None,
            }
        )
    matches.sort(key=lambda row: (-row["score"], row.get("ordinal") or 0))
    return matches


def _guide_navigation_matches(*, navigation_links: list[Any], query: str, page_url: str | None) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for row in navigation_links:
        if not isinstance(row, dict):
            continue
        score = _score_text_match(query, row.get("label"), row.get("url"))
        if score <= 0:
            continue
        matches.append(
            {
                "kind": "navigation",
                "score": score + _score_text_match(query, row.get("label")),
                "label": row.get("label"),
                "url": row.get("url"),
                "citation_url": row.get("source_url") or page_url,
            }
        )
    matches.sort(key=lambda row: (-row["score"], row.get("label") or ""))
    return matches


def _guide_analysis_surface_matches(*, analysis_surfaces: list[Any], query: str) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for row in analysis_surfaces:
        if not isinstance(row, dict):
            continue
        score = _score_text_match(
            query,
            " ".join(
                part
                for part in (
                    " ".join(str(tag) for tag in row.get("surface_tags") or []),
                    str(row.get("section_title") or ""),
                    str(row.get("page_title") or ""),
                    str(row.get("text_preview") or ""),
                )
                if part
            ),
        )
        if score <= 0:
            continue
        citation = row.get("citation")
        matches.append(
            {
                "kind": "analysis_surface",
                "score": score,
                "surface_tags": row.get("surface_tags") or [],
                "section_title": row.get("section_title"),
                "section_ordinal": row.get("section_ordinal"),
                "page_title": row.get("page_title"),
                "preview": row.get("text_preview"),
                "citation_url": citation.get("page_url") if isinstance(citation, dict) else row.get("page_url"),
            }
        )
    matches.sort(key=lambda row: (-row["score"], row.get("section_ordinal") or 0, row.get("section_title") or ""))
    return matches


def _guide_linked_entity_matches(
    *,
    linked_entities: list[Any],
    query: str,
    selected_link_sources: tuple[str, ...],
) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for row in linked_entities:
        if not isinstance(row, dict):
            continue
        if not _linked_source_filter_matches(row, selected_sources=selected_link_sources):
            continue
        score = _linked_entity_query_score(row, query=query)
        if score <= 0:
            continue
        matches.append(
            {
                "kind": "linked_entity",
                "score": score,
                "entity_type": row.get("entity_type"),
                "id": row.get("id"),
                "name": row.get("name"),
                "url": row.get("url"),
                "citation_url": row.get("citation_url"),
                "sources": _link_source_kinds(row),
            }
        )
    matches.sort(key=_guide_query_match_sort_key)
    return matches


def _guide_gatherer_matches(*, gatherer_entities: list[Any], query: str) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for row in gatherer_entities:
        if not isinstance(row, dict):
            continue
        score = _score_text_match(query, row.get("name"), row.get("entity_type"), row.get("url"))
        if score <= 0:
            continue
        matches.append(
            {
                "kind": "gatherer_entity",
                "score": score + _score_text_match(query, row.get("name")),
                "entity_type": row.get("entity_type"),
                "id": row.get("id"),
                "name": row.get("name"),
                "url": row.get("url"),
                "citation_url": row.get("citation_url"),
            }
        )
    matches.sort(key=lambda row: (-row["score"], row.get("entity_type") or "", row.get("id") or 0))
    return matches


def _guide_comment_matches(*, comments: list[Any], query: str) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for row in comments:
        if not isinstance(row, dict):
            continue
        score = _score_text_match(query, row.get("user"), row.get("body"))
        if score <= 0:
            continue
        matches.append(
            {
                "kind": "comment",
                "score": score + _score_text_match(query, row.get("user")),
                "id": row.get("id"),
                "user": row.get("user"),
                "preview": _truncate_preview(row.get("body") or ""),
                "citation_url": row.get("citation_url"),
            }
        )
    matches.sort(key=lambda row: (-row["score"], row.get("id") or 0))
    return matches


def _guide_query_top_matches(*, match_groups: list[list[dict[str, Any]]], limit: int) -> list[dict[str, Any]]:
    top_matches: list[dict[str, Any]] = []
    for group in match_groups:
        top_matches.extend(group[:limit])
    top_matches.sort(key=_guide_query_match_sort_key)
    deduped_top_matches: list[dict[str, Any]] = []
    seen_top_keys: set[tuple[str, int]] = set()
    for row in top_matches:
        dedupe_key = _guide_query_top_dedupe_key(row)
        if dedupe_key is not None:
            if dedupe_key in seen_top_keys:
                continue
            seen_top_keys.add(dedupe_key)
        deduped_top_matches.append(row)
    return deduped_top_matches[:limit]


def _guide_query_payload(
    *,
    export_dir: Path,
    corpus: dict[str, Any],
    query: str,
    selected_kinds: tuple[str, ...],
    section_title_filter: str | None,
    selected_link_sources: tuple[str, ...],
    limit: int,
) -> dict[str, Any]:
    manifest = corpus["manifest"]
    page = manifest.get("page") if isinstance(manifest, dict) else {}
    guide = manifest.get("guide") if isinstance(manifest, dict) else {}
    page_url = page.get("canonical_url") if isinstance(page, dict) else None

    section_matches = (
        _guide_section_matches(sections=corpus["sections"], query=query, section_title_filter=section_title_filter)
        if _guide_query_kind_enabled(selected_kinds, "sections")
        else []
    )
    for row in section_matches:
        row["citation_url"] = page_url
    analysis_surface_matches = (
        _guide_analysis_surface_matches(analysis_surfaces=corpus["analysis_surfaces"], query=query)
        if _guide_query_kind_enabled(selected_kinds, "analysis_surfaces")
        else []
    )
    navigation_matches = (
        _guide_navigation_matches(navigation_links=corpus["navigation_links"], query=query, page_url=page_url)
        if _guide_query_kind_enabled(selected_kinds, "navigation")
        else []
    )
    linked_entity_matches = (
        _guide_linked_entity_matches(
            linked_entities=corpus["linked_entities"],
            query=query,
            selected_link_sources=selected_link_sources,
        )
        if _guide_query_kind_enabled(selected_kinds, "linked_entities")
        else []
    )
    gatherer_matches = (
        _guide_gatherer_matches(gatherer_entities=corpus["gatherer_entities"], query=query)
        if _guide_query_kind_enabled(selected_kinds, "gatherer_entities")
        else []
    )
    comment_matches = (
        _guide_comment_matches(comments=corpus["comments"], query=query)
        if _guide_query_kind_enabled(selected_kinds, "comments")
        else []
    )

    return {
        "output_dir": str(export_dir),
        "guide": guide,
        "page": page,
        "filters": {
            "kinds": list(selected_kinds),
            "section_title": section_title_filter,
            "linked_sources": list(selected_link_sources),
        },
        "matches": {
            "sections": section_matches[:limit],
            "analysis_surfaces": analysis_surface_matches[:limit],
            "navigation": navigation_matches[:limit],
            "linked_entities": linked_entity_matches[:limit],
            "gatherer_entities": gatherer_matches[:limit],
            "comments": comment_matches[:limit],
        },
        "counts": {
            "sections": len(section_matches),
            "analysis_surfaces": len(analysis_surface_matches),
            "navigation": len(navigation_matches),
            "linked_entities": len(linked_entity_matches),
            "gatherer_entities": len(gatherer_matches),
            "comments": len(comment_matches),
        },
        "top": _guide_query_top_matches(
            match_groups=[
                section_matches,
                analysis_surface_matches,
                navigation_matches,
                linked_entity_matches,
                gatherer_matches,
                comment_matches,
            ],
            limit=limit,
        ),
    }


def _build_linked_entity_preview(
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
    deduped = _dedupe_links(
        links,
        entity_type=entity_type,
        entity_id=entity_id,
        max_links=max(len(links), 1),
    )
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


def _entity_page_needs_fetch(
    *,
    include_comments: bool,
    linked_entity_preview_limit: int,
    tooltip_from_page_metadata: bool,
) -> bool:
    return include_comments or linked_entity_preview_limit > 0 or tooltip_from_page_metadata


def _entity_comments_payload(
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
                    "body": _truncate_text(row.get("body"), max_chars=top_comment_chars),
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


def _entity_linked_entities_payload(
    *,
    html: str | None,
    page_url: str,
    page_entity_type: str,
    page_entity_id: int,
    requested_entity_type: str,
    requested_entity_id: int,
    linked_entity_preview_limit: int,
) -> dict[str, Any] | None:
    if html is None or linked_entity_preview_limit <= 0:
        return None
    return _build_linked_entity_preview(
        extract_linked_entities_from_href(html, source_url=page_url)
        + extract_gatherer_entities(html, source_url=page_url),
        entity_type=page_entity_type,
        entity_id=page_entity_id,
        preview_limit=linked_entity_preview_limit,
        fetch_more_command_builder=lambda count: _entity_page_fetch_more_command(requested_entity_type, requested_entity_id, count),
    )


def _comparison_entity_record(
    *,
    ref: str,
    entity_type: str,
    entity_id: int,
    canonical_url: str,
    tooltip: dict[str, Any],
    metadata: dict[str, str | None],
    deduped_links: list[dict[str, Any]],
    raw_comments: list[dict[str, Any]],
    sampled_comments: list[dict[str, Any]],
) -> tuple[dict[str, Any], set[tuple[str, int]]]:
    link_set: set[tuple[str, int]] = set()
    for row in deduped_links:
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
            "linked_entities": {
                "count": len(deduped_links),
                "items": deduped_links,
            },
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


def _comparison_field_diffs(entity_records: list[dict[str, Any]], *, comparable_fields: list[str]) -> dict[str, Any]:
    field_diffs: dict[str, Any] = {}
    for field in comparable_fields:
        values: dict[str, Any] = {}
        for row in entity_records:
            ref = row.get("ref")
            if not isinstance(ref, str):
                continue
            summary = row.get("summary")
            value = summary.get(field) if isinstance(summary, dict) else None
            values[ref] = value
        unique_values = {repr(v) for v in values.values()}
        field_diffs[field] = {
            "all_equal": len(unique_values) <= 1,
            "values": values,
        }
    return field_diffs


def _comparison_linked_entities_summary(
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


def _build_entity_payload(
    ctx: typer.Context,
    client: WowheadClient,
    *,
    entity_type: str,
    entity_id: int,
    data_env: int | None,
    include_comments: bool,
    include_all_comments: bool,
    linked_entity_preview_limit: int,
    top_comment_limit: int = 3,
    top_comment_chars: int = 320,
) -> dict[str, Any]:
    cfg = _cfg(ctx)
    cached_payload = client.get_cached_entity_response(
        requested_type=entity_type,
        requested_id=entity_id,
        data_env=data_env,
        include_comments=include_comments,
        include_all_comments=include_all_comments,
        linked_entity_preview_limit=linked_entity_preview_limit,
    )
    if isinstance(cached_payload, dict):
        return cached_payload

    plan = _build_entity_access_plan(entity_type, entity_id)
    tooltip: dict[str, Any] = {}
    tooltip_final_url: str | None = None
    try:
        if plan.tooltip_entity_type is not None and plan.tooltip_entity_id is not None:
            if plan.page_from_tooltip_redirect:
                tooltip, tooltip_final_url = client.tooltip_with_metadata(
                    plan.tooltip_entity_type,
                    plan.tooltip_entity_id,
                    data_env=data_env,
                )
            else:
                tooltip = client.tooltip(
                    plan.tooltip_entity_type,
                    plan.tooltip_entity_id,
                    data_env=data_env,
                )
    except httpx.HTTPStatusError as exc:
        _fail(ctx, "http_error", f"Wowhead returned HTTP {exc.response.status_code}")
    except httpx.HTTPError as exc:
        _fail(ctx, "network_error", str(exc))
    except ValueError as exc:
        _fail(ctx, "parse_error", str(exc))

    if plan.page_from_tooltip_redirect and tooltip_final_url is not None:
        resolved = _parse_tooltip_final_ref(tooltip_final_url)
        if resolved is None:
            _fail(ctx, "unexpected_response", f"Could not resolve a page target for {entity_type} {entity_id}.")
        plan.page_entity_type, plan.page_entity_id = resolved

    canonical = entity_url(plan.page_entity_type, plan.page_entity_id, expansion=cfg.expansion)
    page_url = canonical
    entity_name, tooltip_payload = _normalize_tooltip_payload(tooltip)
    html: str | None = None
    metadata: dict[str, str | None] | None = None

    if _entity_page_needs_fetch(
        include_comments=include_comments,
        linked_entity_preview_limit=linked_entity_preview_limit,
        tooltip_from_page_metadata=plan.tooltip_from_page_metadata,
    ):
        html, metadata = _fetch_entity_page(ctx, client, plan.page_entity_type, plan.page_entity_id)
        page_url = metadata["canonical_url"] or canonical

    if plan.tooltip_from_page_metadata and metadata is not None:
        entity_name, tooltip_payload = _build_tooltip_from_page_metadata(metadata)
    comments_payload, comments_citations = _entity_comments_payload(
        html=html,
        page_url=page_url,
        include_comments=include_comments,
        include_all_comments=include_all_comments,
        top_comment_limit=top_comment_limit,
        top_comment_chars=top_comment_chars,
    )

    payload = {
        "expansion": cfg.expansion.key,
        "entity": {
            "type": entity_type,
            "id": entity_id,
            "name": entity_name,
            "page_url": page_url,
        },
    }
    if tooltip_payload:
        payload["tooltip"] = tooltip_payload
    if comments_citations is not None:
        payload["citations"] = comments_citations
    linked_entities_payload = _entity_linked_entities_payload(
        html=html,
        page_url=page_url,
        page_entity_type=plan.page_entity_type,
        page_entity_id=plan.page_entity_id,
        requested_entity_type=entity_type,
        requested_entity_id=entity_id,
        linked_entity_preview_limit=linked_entity_preview_limit,
    )
    if linked_entities_payload is not None:
        payload["linked_entities"] = linked_entities_payload
    if comments_payload is not None:
        payload["comments"] = comments_payload

    client.set_cached_entity_response(
        payload,
        requested_type=entity_type,
        requested_id=entity_id,
        data_env=data_env,
        include_comments=include_comments,
        include_all_comments=include_all_comments,
        linked_entity_preview_limit=linked_entity_preview_limit,
    )
    return payload


def _load_or_build_cached_entity_payload(
    ctx: typer.Context,
    client: WowheadClient,
    *,
    entity_type: str,
    entity_id: int,
    data_env: int | None,
    include_comments: bool,
    include_all_comments: bool,
    linked_entity_preview_limit: int,
) -> tuple[dict[str, Any], str]:
    cached_payload = client.get_cached_entity_response(
        requested_type=entity_type,
        requested_id=entity_id,
        data_env=data_env,
        include_comments=include_comments,
        include_all_comments=include_all_comments,
        linked_entity_preview_limit=linked_entity_preview_limit,
    )
    if isinstance(cached_payload, dict):
        return cached_payload, "entity_cache"
    return (
        _build_entity_payload(
            ctx,
            client,
            entity_type=entity_type,
            entity_id=entity_id,
            data_env=data_env,
            include_comments=include_comments,
            include_all_comments=include_all_comments,
            linked_entity_preview_limit=linked_entity_preview_limit,
        ),
        "live_fetch",
    )


def _hydrate_source_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        source = row.get("storage_source")
        if not isinstance(source, str):
            continue
        counts[source] = counts.get(source, 0) + 1
    return counts


def _fetch_guide_page(
    ctx: typer.Context,
    client: WowheadClient,
    *,
    guide_ref: str,
) -> tuple[str, int | None, str, dict[str, str | None], str]:
    try:
        lookup_url, guide_id = _resolve_guide_lookup_input(guide_ref, expansion=client.expansion)
    except ValueError as exc:
        _fail(ctx, "invalid_argument", str(exc))

    try:
        default_lookup = guide_url(guide_id, expansion=client.expansion) if guide_id is not None else None
        if guide_id is not None and lookup_url == default_lookup:
            html = client.guide_page_html(guide_id)
        else:
            html = client.page_html(lookup_url)
    except httpx.HTTPStatusError as exc:
        _fail(ctx, "http_error", f"Wowhead returned HTTP {exc.response.status_code}")
    except httpx.HTTPError as exc:
        _fail(ctx, "network_error", str(exc))

    metadata = parse_page_metadata(html, fallback_url=lookup_url)
    canonical_url = metadata["canonical_url"] or lookup_url
    return html, guide_id, lookup_url, metadata, canonical_url


def _collect_guide_linked_entities(
    *,
    html: str,
    canonical_url: str,
    guide_id: int | None,
    max_links: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    guide_entity_id = guide_id or 0
    href_entities = _dedupe_links(
        extract_linked_entities_from_href(html, source_url=canonical_url),
        entity_type="guide",
        entity_id=guide_entity_id,
        max_links=max_links,
    )
    gatherer_entities = extract_gatherer_entities(html, source_url=canonical_url)
    merged_entities = _dedupe_links(
        href_entities + gatherer_entities,
        entity_type="guide",
        entity_id=guide_entity_id,
        max_links=max_links,
    )
    return href_entities, gatherer_entities, merged_entities


def _build_guide_full_payload(
    ctx: typer.Context,
    *,
    guide_ref: str,
    max_links: int,
    include_replies: bool,
    client: WowheadClient | None = None,
) -> tuple[dict[str, Any], str]:
    cfg = _cfg(ctx)
    resolved_client = client or _client(ctx)
    html, guide_id, lookup_url, metadata, canonical_url = _fetch_guide_page(
        ctx,
        resolved_client,
        guide_ref=guide_ref,
    )

    raw_comments: list[dict[str, Any]]
    try:
        raw_comments = extract_comments_dataset(html)
    except ValueError:
        raw_comments = []
    comments = normalize_comments(
        raw_comments,
        page_url=canonical_url,
        include_replies=include_replies,
    )

    href_entities, gatherer_entities, linked_entities = _collect_guide_linked_entities(
        html=html,
        canonical_url=canonical_url,
        guide_id=guide_id,
        max_links=max_links,
    )

    page_meta_json = parse_page_meta_json(html)
    json_ld = extract_json_ld(html)
    guide_body_markup = extract_markup_by_target(html, target="guide-body")
    guide_nav_markup = extract_markup_by_target(html, target="interior-sidebar-related-markup")
    author_name = None
    author_profiles: dict[str, Any] | None = None
    author_embed: dict[str, Any] | None = None

    try:
        parsed_author = extract_json_script(html, "data.guide.author")
        if isinstance(parsed_author, str):
            author_name = parsed_author
    except (ValueError, json.JSONDecodeError):
        author_name = None

    try:
        parsed_profiles = extract_json_script(html, "data.guide.author.profiles")
        if isinstance(parsed_profiles, dict):
            author_profiles = parsed_profiles
    except (ValueError, json.JSONDecodeError):
        author_profiles = None

    try:
        parsed_embed = extract_json_script(html, "data.guide.aboutTheAuthor.embedData")
        if isinstance(parsed_embed, dict):
            author_embed = parsed_embed
    except (ValueError, json.JSONDecodeError):
        author_embed = None

    body_sections = extract_guide_sections(guide_body_markup) if isinstance(guide_body_markup, str) else []
    body_section_chunks = extract_guide_section_chunks(guide_body_markup) if isinstance(guide_body_markup, str) else []
    analysis_surfaces = extract_section_chunk_analysis_surfaces(
        provider="wowhead",
        page_url=canonical_url,
        page_title=metadata["title"],
        section_chunks=body_section_chunks,
    )

    payload: dict[str, Any] = {
        "expansion": cfg.expansion.key,
        "guide": {
            "input": guide_ref,
            "id": guide_id,
            "lookup_url": lookup_url,
            "page_url": canonical_url,
        },
        "page": {
            "title": metadata["title"],
            "description": metadata["description"],
            "canonical_url": canonical_url,
        },
        "author": {
            "name": author_name,
            "profiles": author_profiles or {},
            "about": author_embed or {},
        },
        "rating": extract_guide_rating(html),
        "body": {
            "raw_markup": guide_body_markup,
            "sections": body_sections,
            "section_chunks": body_section_chunks,
            "summary": clean_markup_text(guide_body_markup[:2000]) if isinstance(guide_body_markup, str) else None,
        },
        "navigation": {
            "raw_markup": guide_nav_markup,
            "links": extract_markup_urls(guide_nav_markup, source_url=canonical_url)
            if isinstance(guide_nav_markup, str)
            else [],
        },
        "linked_entities": {
            "count": len(linked_entities),
            "items": linked_entities,
            "source_counts": {
                "href": len(href_entities),
                "gatherer": len(gatherer_entities),
                "merged": len(linked_entities),
            },
        },
        "gatherer_entities": {
            "count": len(gatherer_entities),
            "items": gatherer_entities,
        },
        "comments": {
            "count": len(comments),
            "include_replies": include_replies,
            "all_comments_included": True,
            "items": comments,
        },
        "analysis_surfaces": {
            "count": len(analysis_surfaces),
            "items": analysis_surfaces,
        },
        "structured_data": json_ld,
        "citations": {
            "page": canonical_url,
            "comments": f"{canonical_url}#comments",
        },
    }
    if isinstance(page_meta_json, dict):
        payload["page_meta"] = {
            "page": page_meta_json.get("page"),
            "server_time": page_meta_json.get("serverTime"),
            "available_data_envs": page_meta_json.get("availableDataEnvs"),
            "env_domain": page_meta_json.get("envDomain"),
        }
    return payload, html


def _load_guide_export(export_dir: Path) -> dict[str, Any]:
    manifest_path = export_dir / "manifest.json"
    if not manifest_path.exists():
        raise ValueError(f"Missing manifest file at {manifest_path}.")
    manifest = _read_json_file(manifest_path)
    if not isinstance(manifest, dict):
        raise ValueError("Guide export manifest is not a JSON object.")

    files = manifest.get("files")
    if not isinstance(files, dict):
        raise ValueError("Guide export manifest is missing its files map.")

    def load_jsonl_from_manifest(key: str) -> list[Any]:
        filename = files.get(key)
        if not isinstance(filename, str):
            return []
        path = export_dir / filename
        if not path.exists():
            return []
        return _read_jsonl_file(path)

    return {
        "manifest": manifest,
        "sections": load_jsonl_from_manifest("sections_jsonl"),
        "navigation_links": load_jsonl_from_manifest("navigation_links_jsonl"),
        "linked_entities": load_jsonl_from_manifest("linked_entities_jsonl"),
        "gatherer_entities": load_jsonl_from_manifest("gatherer_entities_jsonl"),
        "comments": load_jsonl_from_manifest("comments_jsonl"),
        "analysis_surfaces": load_jsonl_from_manifest("analysis_surfaces_jsonl"),
    }


def _parse_iso8601_utc(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_date_bound(value: str | None, *, end_of_day: bool) -> datetime | None:
    if value is None or not value.strip():
        return None
    raw = value.strip()
    if "T" not in raw and " " not in raw:
        try:
            parsed_date = datetime.fromisoformat(raw).date()
        except ValueError:
            return None
        if end_of_day:
            return datetime.combine(parsed_date, datetime.max.time(), tzinfo=timezone.utc)
        return datetime.combine(parsed_date, datetime.min.time(), tzinfo=timezone.utc)
    parsed = _parse_iso8601_utc(raw)
    if parsed is None:
        return None
    return parsed


def _clean_htmlish_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = re.sub(r"<[^>]+>", " ", value)
    text = clean_markup_text(text)
    text = " ".join(text.split())
    return text or None


def _absolute_wowhead_url(value: Any, *, fallback: str | None = None) -> str | None:
    if isinstance(value, str) and value.strip():
        return urljoin(WOWHEAD_BASE_URL, value.strip())
    return fallback


def _normalize_text_filters(values: list[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    for value in values:
        for part in value.split(","):
            candidate = part.strip().lower()
            if candidate and candidate not in normalized:
                normalized.append(candidate)
    return tuple(normalized)


def _text_filter_match(value: Any, filters: tuple[str, ...]) -> bool:
    if not filters:
        return True
    if not isinstance(value, str):
        return False
    return value.strip().lower() in filters


def _collect_timeline_facets(results: list[dict[str, Any]], *, fields: dict[str, str]) -> dict[str, list[str]]:
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


def _guide_sort_key(row: dict[str, Any], *, sort_by: str, fallback_index: int) -> tuple[Any, ...]:
    if sort_by == "updated":
        updated_at = _parse_iso8601_utc(row.get("last_updated"))
        return (updated_at is None, -(updated_at.timestamp()) if updated_at is not None else 0.0, fallback_index)
    if sort_by == "published":
        published_at = _parse_iso8601_utc(row.get("published_at"))
        return (published_at is None, -(published_at.timestamp()) if published_at is not None else 0.0, fallback_index)
    if sort_by == "rating":
        rating = row.get("rating")
        votes = row.get("votes")
        rating_value = float(rating) if isinstance(rating, (int, float)) else float("-inf")
        vote_value = int(votes) if isinstance(votes, int) else -1
        return (-rating_value, -vote_value, fallback_index)
    match_score = row.get("match_score")
    if isinstance(match_score, (int, float)):
        return (-float(match_score), fallback_index)
    return (fallback_index,)


def _timeline_result_matches(
    *,
    query: str | None,
    values: list[Any],
    posted_at: datetime | None,
    date_from: datetime | None,
    date_to: datetime | None,
) -> tuple[bool, int]:
    if date_from is not None and (posted_at is None or posted_at < date_from):
        return False, 0
    if date_to is not None and (posted_at is None or posted_at > date_to):
        return False, 0
    normalized_query = query.strip() if isinstance(query, str) else ""
    if not normalized_query:
        return True, 0
    score = _score_text_match(normalized_query, *values)
    return score > 0, score


def _collect_timeline_pages(
    *,
    ctx: typer.Context,
    page: int,
    pages: int,
    fetch_page: Callable[[int], str],
    extract_page: Callable[[str], tuple[list[dict[str, Any]], int | None]],
    normalize_row: Callable[[dict[str, Any]], dict[str, Any] | None],
    query: str | None,
    date_from: datetime | None,
    date_to: datetime | None,
) -> dict[str, Any]:
    if page <= 0:
        _fail(ctx, "invalid_argument", "--page must be >= 1.")
    if pages <= 0:
        _fail(ctx, "invalid_argument", "--pages must be >= 1.")

    results: list[dict[str, Any]] = []
    total_pages: int | None = None
    pages_scanned = 0
    stop_reason: str | None = None

    for current_page in range(page, page + pages):
        try:
            html = fetch_page(current_page)
        except httpx.HTTPStatusError as exc:
            _fail(ctx, "http_error", f"Wowhead returned HTTP {exc.response.status_code}")
        except httpx.HTTPError as exc:
            _fail(ctx, "network_error", str(exc))

        try:
            rows, extracted_total_pages = extract_page(html)
        except (ValueError, json.JSONDecodeError) as exc:
            _fail(ctx, "parse_error", str(exc))

        if total_pages is None:
            total_pages = extracted_total_pages
        pages_scanned += 1
        if not rows:
            stop_reason = "empty_page"
            break

        reached_older_than_window = False
        for raw_row in rows:
            normalized_row = normalize_row(raw_row)
            if normalized_row is None:
                continue
            posted_at = _parse_iso8601_utc(normalized_row.get("posted"))
            if date_from is not None and posted_at is not None and posted_at < date_from:
                reached_older_than_window = True
                continue
            matched, score = _timeline_result_matches(
                query=query,
                values=[
                    normalized_row.get("title"),
                    normalized_row.get("preview"),
                    normalized_row.get("body_preview"),
                    normalized_row.get("author"),
                    normalized_row.get("topic"),
                    normalized_row.get("type_name"),
                    normalized_row.get("forum_area"),
                    normalized_row.get("forum"),
                ],
                posted_at=posted_at,
                date_from=date_from,
                date_to=date_to,
            )
            if not matched:
                continue
            normalized_row["match_score"] = score
            results.append(normalized_row)

        if reached_older_than_window:
            stop_reason = "date_from_reached"
            break
        if total_pages is not None and current_page >= total_pages:
            stop_reason = "last_page_reached"
            break

    return {
        "results": results,
        "pages_scanned": pages_scanned,
        "total_pages": total_pages,
        "stop_reason": stop_reason,
    }


def _extract_news_page_data(html: str) -> tuple[list[dict[str, Any]], int | None]:
    data = extract_json_script(html, "data.news.newsData")
    if not isinstance(data, dict):
        raise ValueError("Unexpected Wowhead news payload shape.")
    rows = data.get("newsPosts")
    total_pages = data.get("totalPages")
    if not isinstance(rows, list):
        raise ValueError("Missing or invalid Wowhead news posts payload.")
    return [row for row in rows if isinstance(row, dict)], total_pages if isinstance(total_pages, int) else None


def _extract_blue_tracker_page_data(html: str) -> tuple[list[dict[str, Any]], int | None]:
    data = extract_json_script(html, "data.blueTracker.default")
    if not isinstance(data, dict):
        raise ValueError("Unexpected Wowhead blue tracker payload shape.")
    rows = data.get("entries")
    total_topics = data.get("totalTopics")
    if not isinstance(rows, list):
        raise ValueError("Missing or invalid Wowhead blue tracker entries payload.")
    total_pages = None
    if isinstance(total_topics, int) and total_topics >= 0:
        total_pages = max(1, math.ceil(total_topics / 50))
    return [row for row in rows if isinstance(row, dict)], total_pages


def _normalize_news_row(row: dict[str, Any]) -> dict[str, Any] | None:
    post_id = row.get("id")
    title = row.get("title")
    post_url = row.get("postUrl")
    if not isinstance(post_id, int) or not isinstance(title, str) or not isinstance(post_url, str):
        return None
    absolute_url = urljoin(WOWHEAD_BASE_URL, post_url)
    preview = _clean_htmlish_text(row.get("preview"))
    return {
        "id": post_id,
        "title": title,
        "posted": row.get("postedFull") if isinstance(row.get("postedFull"), str) else row.get("posted"),
        "posted_short": row.get("postedShort"),
        "author": row.get("author"),
        "author_page": urljoin(WOWHEAD_BASE_URL, row["authorPage"]) if isinstance(row.get("authorPage"), str) else None,
        "type_id": row.get("typeId"),
        "type_name": row.get("typeName"),
        "url": absolute_url,
        "citation_url": absolute_url,
        "preview": preview,
        "thumbnail_url": row.get("thumbnailUrl"),
        "topic": title,
    }


def _normalize_blue_tracker_row(row: dict[str, Any]) -> dict[str, Any] | None:
    topic_id = row.get("id")
    title = row.get("title")
    topic_url = row.get("url")
    if not isinstance(topic_id, int) or not isinstance(title, str) or not isinstance(topic_url, str):
        return None
    absolute_url = urljoin(WOWHEAD_BASE_URL, topic_url)
    body_preview = _clean_htmlish_text(row.get("body"))
    author = row.get("author") if isinstance(row.get("author"), str) and row.get("author") else row.get("name")
    return {
        "id": topic_id,
        "title": title,
        "topic": title,
        "posted": row.get("posted") if isinstance(row.get("posted"), str) else None,
        "author": author,
        "region": row.get("region"),
        "forum_area": row.get("forumArea"),
        "forum": row.get("forum"),
        "url": absolute_url,
        "citation_url": absolute_url,
        "body_preview": body_preview,
        "blueposts": row.get("blueposts"),
        "posts": row.get("posts"),
        "blues": row.get("blues"),
        "score": row.get("score"),
        "maxscore": row.get("maxscore"),
        "last_post": row.get("lastPost"),
        "last_blue": row.get("lastblue"),
        "job_title": row.get("jobtitle"),
    }


def _normalize_guide_category_row(row: dict[str, Any]) -> dict[str, Any] | None:
    guide_id = row.get("id")
    title = row.get("title")
    name = row.get("name")
    url = row.get("url")
    if not isinstance(guide_id, int) or not isinstance(title, str) or not isinstance(name, str) or not isinstance(url, str):
        return None
    return {
        "id": guide_id,
        "name": name,
        "title": title,
        "url": url,
        "author": row.get("author"),
        "author_page": row.get("authorPage"),
        "patch": row.get("patch"),
        "published_at": row.get("when"),
        "last_updated": row.get("lastEdit"),
        "category": row.get("category"),
        "category_names": row.get("categoryNames"),
        "category_path": row.get("categoryPath"),
        "class_id": row.get("class"),
        "spec_id": row.get("spec"),
        "comments": row.get("comments"),
        "rating": row.get("rating"),
        "votes": row.get("nvotes"),
    }


def _normalize_tool_ref(ref: str, *, tool_slug: str, expansion: ExpansionProfile) -> str:
    raw = ref.strip()
    if not raw:
        raise ValueError(f"{tool_slug} reference cannot be empty.")
    known_talent_calc_classes = {
        "deathknight",
        "death-knight",
        "demonhunter",
        "demon-hunter",
        "druid",
        "evoker",
        "hunter",
        "mage",
        "monk",
        "paladin",
        "priest",
        "rogue",
        "shaman",
        "warlock",
        "warrior",
    }
    lowered_raw = raw.lower()
    url_candidate = f"https://{raw}" if lowered_raw.startswith(("wowhead.com/", "www.wowhead.com/")) else raw
    parsed = urlparse(url_candidate)
    if parsed.scheme and parsed.netloc:
        hostname = parsed.hostname.lower() if isinstance(parsed.hostname, str) else ""
        if hostname != "wowhead.com" and not hostname.endswith(".wowhead.com"):
            raise ValueError(f"{tool_slug} URL must point to wowhead.com.")
        return url_candidate
    normalized = raw.lstrip("/")
    if tool_slug == "talent-calc":
        raw_parts = normalized.split("/")
        if raw_parts and raw_parts[-1] == "":
            raw_parts = raw_parts[:-1]
        if any(part == "" for part in raw_parts):
            raise ValueError("talent-calc reference must not include empty path segments.")
        parts = raw_parts
        has_expansion_prefix = bool(parts and parts[0] in EXPANSION_PREFIXES)
        if parts and parts[0] in EXPANSION_PREFIXES:
            parts = parts[1:]
        if not parts:
            raise ValueError("talent-calc reference cannot be empty.")
        if parts[0] == "talent-calc":
            if len(parts) not in {3, 4} or parts[1] not in known_talent_calc_classes:
                raise ValueError("talent-calc reference must be a Wowhead talent-calc path or class/spec ref.")
        elif parts[0] not in known_talent_calc_classes or len(parts) not in {2, 3}:
            raise ValueError("talent-calc reference must be a Wowhead talent-calc path or class/spec ref.")
        if has_expansion_prefix:
            prefixed_parts = [raw_parts[0], *parts]
            if parts[0] != "talent-calc":
                prefixed_parts = [raw_parts[0], "talent-calc", *parts]
            return tool_url("/".join(prefixed_parts), expansion="retail")
    if not normalized.startswith(f"{tool_slug}/") and normalized != tool_slug:
        normalized = f"{tool_slug}/{normalized}"
    return tool_url(normalized, expansion=expansion)


def _parse_talent_calc_state(state_url: str) -> dict[str, Any]:
    parsed = urlparse(state_url)
    raw_parts = parsed.path.split("/")
    if raw_parts and raw_parts[0] == "":
        raw_parts = raw_parts[1:]
    if raw_parts and raw_parts[-1] == "":
        raw_parts = raw_parts[:-1]
    if any(part == "" for part in raw_parts):
        raise ValueError("Talent calculator URL must not include empty path segments.")
    parts = raw_parts
    expansion = "retail"
    if parts and parts[0] in EXPANSION_PREFIXES:
        expansion = parts[0]
        parts = parts[1:]
    if not parts or parts[0] != "talent-calc":
        raise ValueError("Talent calculator URL must point to /talent-calc.")
    if len(parts) not in {3, 4}:
        raise ValueError("Talent calculator URL must use /talent-calc/<class>/<spec>[/<build-code>].")
    class_slug = parts[1] if len(parts) > 1 else None
    spec_slug = parts[2] if len(parts) > 2 else None
    build_code = parts[3] if len(parts) > 3 else None
    return {
        "expansion": expansion,
        "class_slug": class_slug,
        "spec_slug": spec_slug,
        "build_code": build_code,
        "path_segments": parts[1:],
        "has_build_code": build_code is not None,
    }


def _extract_talent_calc_listed_builds(html: str, *, limit: int) -> dict[str, Any] | None:
    try:
        payload = extract_json_script(html, "data.wow.talentCalcDragonflight.live.talentBuilds")
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    rows: list[dict[str, Any]] = []
    for raw_id, raw_row in payload.items():
        if not isinstance(raw_row, dict):
            continue
        row_id = raw_row.get("id")
        if not isinstance(row_id, int):
            try:
                row_id = int(raw_id)
            except ValueError:
                row_id = None
        row = {
            "id": row_id,
            "name": raw_row.get("name"),
            "hash": raw_row.get("hash"),
            "spec_id": raw_row.get("spec"),
            "listed": raw_row.get("isListed"),
        }
        rows.append(row)
    rows.sort(key=lambda row: (row.get("name") or "", row.get("id") or 0))
    return {
        "count": len(rows),
        "items": rows[:limit],
    }


def _load_talent_calc_context(
    ctx: typer.Context,
    *,
    ref: str,
    listed_build_limit: int,
) -> tuple[str, dict[str, Any], dict[str, Any], str, dict[str, Any] | None]:
    cfg = _cfg(ctx)
    try:
        state_url = _normalize_tool_ref(ref, tool_slug="talent-calc", expansion=cfg.expansion)
        state = _parse_talent_calc_state(state_url)
    except ValueError as exc:
        _fail(ctx, "invalid_tool_ref", str(exc))
        raise AssertionError("unreachable")
    client = _client(ctx)
    try:
        html = client.page_html(state_url)
    except httpx.HTTPStatusError as exc:
        _fail(ctx, "http_error", f"Wowhead returned HTTP {exc.response.status_code}")
        raise AssertionError("unreachable")
    except httpx.HTTPError as exc:
        _fail(ctx, "network_error", str(exc))
        raise AssertionError("unreachable")
    metadata = parse_page_metadata(html, fallback_url=state_url)
    page_url = _absolute_wowhead_url(metadata.get("canonical_url"), fallback=state_url) or state_url
    listed_builds = _extract_talent_calc_listed_builds(html, limit=listed_build_limit)
    return state_url, state, metadata, page_url, listed_builds


def _base_talent_calc_payload(
    ctx: typer.Context,
    *,
    ref: str,
) -> dict[str, Any]:
    cfg = _cfg(ctx)
    try:
        state_url = _normalize_tool_ref(ref, tool_slug="talent-calc", expansion=cfg.expansion)
        state = _parse_talent_calc_state(state_url)
    except ValueError as exc:
        _fail(ctx, "invalid_tool_ref", str(exc))
        raise AssertionError("unreachable")
    return {
        "expansion": str(state.get("expansion") or cfg.expansion.key),
        "tool": {
            "kind": "talent-calc",
            "input": ref,
            "state_url": state_url,
            "page_url": state_url,
            **state,
        },
        "build_identity": build_identity_payload(
            actor_class=state.get("class_slug"),
            spec=state.get("spec_slug"),
            confidence="high" if state.get("class_slug") and state.get("spec_slug") else "none",
            source="wowhead_talent_calc_url",
            candidates=[(state.get("class_slug"), state.get("spec_slug"))] if state.get("class_slug") and state.get("spec_slug") else None,
            source_notes=["class/spec came from the explicit Wowhead talent-calc URL path"],
        ),
        "page": {
            "title": None,
            "description": None,
            "canonical_url": state_url,
        },
        "citations": {
            "page": state_url,
        },
    }


def _enrich_talent_calc_payload_with_page_data(
    ctx: typer.Context,
    payload: dict[str, Any],
    *,
    listed_build_limit: int,
    fail_on_fetch_error: bool,
) -> dict[str, Any]:
    state_url = str(payload["tool"]["state_url"])
    try:
        client = _client(ctx)
    except typer.Exit:
        if fail_on_fetch_error:
            raise
        return payload
    try:
        html = client.page_html(state_url)
    except httpx.HTTPStatusError as exc:
        if fail_on_fetch_error:
            _fail(ctx, "http_error", f"Wowhead returned HTTP {exc.response.status_code}")
            raise AssertionError("unreachable")
        return payload
    except httpx.HTTPError as exc:
        if fail_on_fetch_error:
            _fail(ctx, "network_error", str(exc))
            raise AssertionError("unreachable")
        return payload
    metadata = parse_page_metadata(html, fallback_url=state_url)
    page_url = _absolute_wowhead_url(metadata.get("canonical_url"), fallback=state_url) or state_url
    enriched_payload = dict(payload)
    enriched_tool = dict(payload["tool"])
    enriched_tool["page_url"] = page_url
    enriched_payload["tool"] = enriched_tool
    enriched_payload["page"] = {
        "title": metadata.get("title"),
        "description": metadata.get("description"),
        "canonical_url": page_url,
    }
    listed_builds = _extract_talent_calc_listed_builds(html, limit=listed_build_limit)
    if listed_builds is not None:
        enriched_payload["listed_builds"] = listed_builds
    return enriched_payload


def _talent_calc_payload(
    ctx: typer.Context,
    *,
    ref: str,
    listed_build_limit: int,
) -> dict[str, Any]:
    payload = _base_talent_calc_payload(ctx, ref=ref)
    return _enrich_talent_calc_payload_with_page_data(
        ctx,
        payload,
        listed_build_limit=listed_build_limit,
        fail_on_fetch_error=True,
    )


def _parse_profession_tree_state(state_url: str) -> dict[str, Any]:
    parsed = urlparse(state_url)
    parts = [part for part in parsed.path.split("/") if part]
    if parts and parts[0] in EXPANSION_PREFIXES:
        parts = parts[1:]
    if not parts or parts[0] != "profession-tree-calc":
        raise ValueError("Profession tree URL must point to /profession-tree-calc.")
    profession_slug = parts[1] if len(parts) > 1 else None
    loadout_code = parts[2] if len(parts) > 2 else None
    return {
        "profession_slug": profession_slug,
        "loadout_code": loadout_code,
        "path_segments": parts[1:],
        "has_loadout_code": loadout_code is not None,
    }


def _normalize_dressing_room_ref(ref: str, *, expansion: ExpansionProfile) -> str:
    raw = ref.strip()
    if not raw:
        raise ValueError("dressing-room reference cannot be empty.")
    parsed = urlparse(raw)
    if parsed.scheme and parsed.netloc:
        if not parsed.netloc.endswith("wowhead.com"):
            raise ValueError("dressing-room URL must point to wowhead.com.")
        return raw
    if raw.startswith("#"):
        return f"{tool_url('dressing-room', expansion=expansion)}{raw}"
    normalized = raw.lstrip("/")
    if normalized.startswith("dressing-room"):
        return tool_url(normalized, expansion=expansion)
    return f"{tool_url('dressing-room', expansion=expansion)}#{normalized}"


def _parse_dressing_room_state(state_url: str) -> dict[str, Any]:
    parsed = urlparse(state_url)
    parts = [part for part in parsed.path.split("/") if part]
    if parts and parts[0] in EXPANSION_PREFIXES:
        parts = parts[1:]
    if not parts or parts[0] != "dressing-room":
        raise ValueError("Dressing room URL must point to /dressing-room.")
    state_hash = parsed.fragment or None
    return {
        "share_hash": state_hash,
        "has_share_hash": state_hash is not None,
        "hash_length": len(state_hash) if isinstance(state_hash, str) else 0,
    }


def _normalize_profiler_ref(ref: str, *, expansion: ExpansionProfile) -> str:
    raw = ref.strip()
    if not raw:
        raise ValueError("profiler reference cannot be empty.")
    parsed = urlparse(raw)
    if parsed.scheme and parsed.netloc:
        if not parsed.netloc.endswith("wowhead.com"):
            raise ValueError("profiler URL must point to wowhead.com.")
        return raw
    normalized = raw.lstrip("/")
    if normalized.startswith("list?") or normalized.startswith("list/") or normalized == "list":
        return tool_url(normalized, expansion=expansion)
    return f"{tool_url('list', expansion=expansion)}?list={normalized}"


def _parse_profiler_state(state_url: str) -> dict[str, Any]:
    parsed = urlparse(state_url)
    parts = [part for part in parsed.path.split("/") if part]
    if parts and parts[0] in EXPANSION_PREFIXES:
        parts = parts[1:]
    if not parts or parts[0] != "list":
        raise ValueError("Profiler URL must point to /list.")
    list_param = None
    for candidate in (parsed.query or "").split("&"):
        if candidate.startswith("list="):
            list_param = candidate.split("=", 1)[1]
            break
    list_parts = [part for part in list_param.split("/") if part] if isinstance(list_param, str) else []
    return {
        "list_ref": list_param,
        "list_parts": list_parts,
        "list_id": list_parts[0] if len(list_parts) > 0 else None,
        "region_slug": list_parts[1] if len(list_parts) > 1 else None,
        "realm_slug": list_parts[2] if len(list_parts) > 2 else None,
        "character_name": list_parts[3] if len(list_parts) > 3 else None,
        "has_list_ref": list_param is not None,
    }


def _normalize_news_post_ref(ref: str, *, expansion: ExpansionProfile) -> str:
    raw = ref.strip()
    if not raw:
        raise ValueError("news post reference cannot be empty.")
    parsed = urlparse(raw)
    if parsed.scheme and parsed.netloc:
        if not parsed.netloc.endswith("wowhead.com"):
            raise ValueError("news post URL must point to wowhead.com.")
        return raw
    normalized = raw.lstrip("/")
    if normalized.startswith("news/"):
        return tool_url(normalized, expansion=expansion)
    raise ValueError("news post ref must be a full Wowhead news URL or /news/... path.")


def _extract_news_post_markup(html: str) -> str | None:
    marker = "WH.markup.printHtml("
    index = html.find(marker)
    if index < 0:
        return None
    cursor = index + len(marker)
    while cursor < len(html) and html[cursor].isspace():
        cursor += 1
    try:
        parsed, _offset = json.JSONDecoder().raw_decode(html[cursor:])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, str) else None


def _extract_news_recent_posts(html: str, *, limit: int) -> dict[str, Any] | None:
    try:
        payload = extract_json_script(html, "data.WH.News.recentPosts")
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    normalized: dict[str, Any] = {}
    for section_name, rows in payload.items():
        if not isinstance(section_name, str) or not isinstance(rows, list):
            continue
        items: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            url = row.get("url")
            if not isinstance(url, str):
                continue
            items.append(
                {
                    "title": row.get("name"),
                    "url": _absolute_wowhead_url(url, fallback=url),
                    "author": row.get("author"),
                    "posted_short": row.get("time"),
                    "type_name": row.get("newsTypeName"),
                    "region": row.get("region"),
                    "is_blue_tracker": bool(row.get("blue")),
                    "is_news": bool(row.get("news")),
                }
            )
        normalized[section_name] = {
            "count": len(items),
            "items": items[:limit],
        }
    return normalized or None


def _normalize_blue_topic_ref(ref: str, *, expansion: ExpansionProfile) -> str:
    raw = ref.strip()
    if not raw:
        raise ValueError("blue topic reference cannot be empty.")
    parsed = urlparse(raw)
    if parsed.scheme and parsed.netloc:
        if not parsed.netloc.endswith("wowhead.com"):
            raise ValueError("blue topic URL must point to wowhead.com.")
        return raw
    normalized = raw.lstrip("/")
    if normalized.startswith("blue-tracker/topic/"):
        return tool_url(normalized, expansion=expansion)
    raise ValueError("blue topic ref must be a full Wowhead blue-tracker URL or /blue-tracker/topic/... path.")


def _guide_bundle_is_fresh(manifest: dict[str, Any], *, max_age_hours: int) -> bool:
    cutoff = datetime.now(timezone.utc)
    exported_at = _parse_iso8601_utc(manifest.get("exported_at"))
    if exported_at is None:
        return False
    age_seconds = (cutoff - exported_at).total_seconds()
    if age_seconds > max_age_hours * 3600:
        return False

    hydration = manifest.get("hydration")
    if isinstance(hydration, dict) and hydration.get("enabled") is True:
        hydrated_at = _parse_iso8601_utc(hydration.get("hydrated_at"))
        if hydrated_at is None:
            return False
        hydrated_age_seconds = (cutoff - hydrated_at).total_seconds()
        if hydrated_age_seconds > max_age_hours * 3600:
            return False
    return True


def _is_stored_at_fresh(stored_at: Any, *, max_age_hours: int) -> bool:
    parsed = _parse_iso8601_utc(stored_at)
    if parsed is None:
        return False
    age_seconds = (datetime.now(timezone.utc) - parsed).total_seconds()
    return age_seconds <= max_age_hours * 3600


def _infer_guide_export_options(manifest: dict[str, Any]) -> tuple[str, int, bool, bool, tuple[str, ...], int]:
    export_options = manifest.get("export_options")
    guide = manifest.get("guide")
    hydration = manifest.get("hydration")

    guide_ref: str | None = None
    if isinstance(export_options, dict) and isinstance(export_options.get("guide_ref"), str):
        guide_ref = export_options["guide_ref"]
    elif isinstance(guide, dict):
        input_ref = guide.get("input")
        guide_id = guide.get("id")
        if isinstance(input_ref, str) and input_ref.strip():
            guide_ref = input_ref
        elif isinstance(guide_id, int):
            guide_ref = str(guide_id)
    if guide_ref is None:
        raise ValueError("Bundle manifest is missing a guide reference for refresh.")

    max_links = 250
    include_replies = False
    if isinstance(export_options, dict):
        if isinstance(export_options.get("max_links"), int):
            max_links = export_options["max_links"]
        if isinstance(export_options.get("include_replies"), bool):
            include_replies = export_options["include_replies"]
    elif isinstance(manifest.get("comments"), dict):
        include_replies = bool(manifest["comments"].get("include_replies"))

    hydrate_enabled = False
    hydrate_types: tuple[str, ...] = ()
    hydrate_limit = 100
    if isinstance(hydration, dict):
        hydrate_enabled = hydration.get("enabled") is True
        raw_types = hydration.get("types")
        if isinstance(raw_types, list):
            hydrate_types = tuple(value for value in raw_types if isinstance(value, str))
        raw_limit = hydration.get("limit")
        if isinstance(raw_limit, int):
            hydrate_limit = raw_limit
    if hydrate_enabled and not hydrate_types:
        hydrate_types = tuple(DEFAULT_HYDRATE_ENTITY_TYPES)

    return guide_ref, max_links, include_replies, hydrate_enabled, hydrate_types, hydrate_limit


def _guide_bundle_index_path(root: Path) -> Path:
    return root / "index.json"


def _bundle_hydration_summary(manifest: dict[str, Any], *, counts: dict[str, Any]) -> dict[str, Any]:
    hydration = manifest.get("hydration")
    enabled = False
    types: list[str] = []
    limit = 0
    hydrated_at = None
    source_counts: dict[str, int] = {}
    if isinstance(hydration, dict):
        enabled = hydration.get("enabled") is True
        raw_types = hydration.get("types")
        if isinstance(raw_types, list):
            types = [value for value in raw_types if isinstance(value, str)]
        raw_limit = hydration.get("limit")
        if isinstance(raw_limit, int):
            limit = raw_limit
        raw_hydrated_at = hydration.get("hydrated_at")
        hydrated_at = raw_hydrated_at if isinstance(raw_hydrated_at, str) else None
        raw_source_counts = hydration.get("source_counts")
        if isinstance(raw_source_counts, dict):
            source_counts = {
                key: value
                for key, value in raw_source_counts.items()
                if isinstance(key, str) and isinstance(value, int)
            }
    hydrated_entities = counts.get("hydrated_entities") if isinstance(counts.get("hydrated_entities"), int) else 0
    return {
        "enabled": enabled,
        "types": types,
        "limit": limit,
        "hydrated_at": hydrated_at,
        "hydrated_entities": hydrated_entities,
        "source_counts": source_counts,
    }


def _bundle_freshness_summary(manifest: dict[str, Any], *, max_age_hours: int) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    exported_at_raw = manifest.get("exported_at")
    exported_at = _parse_iso8601_utc(exported_at_raw if isinstance(exported_at_raw, str) else None)
    hydration = manifest.get("hydration")
    hydrated_at_raw = hydration.get("hydrated_at") if isinstance(hydration, dict) else None
    hydrated_at = _parse_iso8601_utc(hydrated_at_raw if isinstance(hydrated_at_raw, str) else None)

    bundle_reasons: list[str] = []
    bundle_age_hours: float | None = None
    if exported_at is None:
        bundle_status = "stale"
        bundle_reasons.append("missing_exported_at")
    else:
        bundle_age_hours = round((now - exported_at).total_seconds() / 3600, 2)
        if bundle_age_hours > max_age_hours:
            bundle_status = "stale"
            bundle_reasons.append("max_age_exceeded")
        else:
            bundle_status = "fresh"

    hydration_status = "disabled"
    hydration_reasons = ["disabled"]
    hydration_age_hours: float | None = None
    if isinstance(hydration, dict) and hydration.get("enabled") is True:
        hydration_reasons = []
        if bundle_status != "fresh":
            hydration_reasons.append("bundle_stale")
        if hydrated_at is None:
            hydration_reasons.append("missing_hydrated_at")
        else:
            hydration_age_hours = round((now - hydrated_at).total_seconds() / 3600, 2)
            if hydration_age_hours > max_age_hours:
                hydration_reasons.append("max_age_exceeded")
        hydration_status = "fresh" if not hydration_reasons else "stale"

    return {
        "max_age_hours": max_age_hours,
        "bundle": bundle_status,
        "bundle_reasons": bundle_reasons,
        "bundle_age_hours": bundle_age_hours,
        "hydration": hydration_status,
        "hydration_reasons": hydration_reasons,
        "hydration_age_hours": hydration_age_hours,
    }


def _bundle_index_row(child: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    guide = manifest.get("guide")
    page = manifest.get("page")
    counts = manifest.get("counts")
    counts_dict = counts if isinstance(counts, dict) else {}
    return {
        "path": str(child),
        "dir_name": child.name,
        "guide_id": guide.get("id") if isinstance(guide, dict) else None,
        "title": page.get("title") if isinstance(page, dict) else None,
        "canonical_url": page.get("canonical_url") if isinstance(page, dict) else None,
        "expansion": manifest.get("expansion"),
        "export_version": manifest.get("export_version"),
        "counts": counts_dict,
        "exported_at": manifest.get("exported_at") if isinstance(manifest.get("exported_at"), str) else None,
        "guide_fetched_at": (
            manifest.get("guide_fetched_at") if isinstance(manifest.get("guide_fetched_at"), str) else None
        ),
        "hydration": _bundle_hydration_summary(manifest, counts=counts_dict),
    }


def _bundle_row_with_freshness(row: dict[str, Any], *, max_age_hours: int) -> dict[str, Any]:
    hydration = row.get("hydration")
    manifest_like = {
        "exported_at": row.get("exported_at"),
        "hydration": {
            "enabled": hydration.get("enabled") if isinstance(hydration, dict) else False,
            "hydrated_at": hydration.get("hydrated_at") if isinstance(hydration, dict) else None,
        },
    }
    enriched = dict(row)
    enriched["freshness"] = _bundle_freshness_summary(manifest_like, max_age_hours=max_age_hours)
    return enriched


def _bundle_search_score_and_reasons(row: dict[str, Any], *, query: str) -> tuple[int, list[str]]:
    reasons: list[str] = []
    score = 0
    query_normalized = " ".join(query.lower().split())

    title = row.get("title")
    dir_name = row.get("dir_name")
    canonical_url = row.get("canonical_url")
    expansion = row.get("expansion")
    guide_id = row.get("guide_id")
    hydration = row.get("hydration")
    freshness = row.get("freshness")

    if isinstance(guide_id, int) and query_normalized == str(guide_id):
        score += 15
        reasons.append("guide_id")

    title_score = _score_text_match(query, title)
    if title_score > 0:
        score += title_score * 4
        reasons.append("title")

    dir_score = _score_text_match(query, dir_name)
    if dir_score > 0:
        score += dir_score * 3
        reasons.append("dir_name")

    url_score = _score_text_match(query, canonical_url)
    if url_score > 0:
        score += url_score * 2
        reasons.append("canonical_url")

    expansion_score = _score_text_match(query, expansion)
    if expansion_score > 0:
        score += expansion_score * 2
        reasons.append("expansion")

    if isinstance(hydration, dict):
        if hydration.get("enabled") is True and query_normalized in {"hydrated", "hydration"}:
            score += 3
            reasons.append("hydration_enabled")
        raw_types = hydration.get("types")
        if isinstance(raw_types, list):
            hydration_type_score = _score_text_match(query, *raw_types)
            if hydration_type_score > 0:
                score += hydration_type_score * 2
                reasons.append("hydration_types")

    if isinstance(freshness, dict):
        bundle_status = freshness.get("bundle")
        hydration_status = freshness.get("hydration")
        if isinstance(bundle_status, str) and _score_text_match(query, bundle_status) > 0:
            score += 2
            reasons.append("bundle_freshness")
        if isinstance(hydration_status, str) and hydration_status != "disabled" and _score_text_match(query, hydration_status) > 0:
            score += 2
            reasons.append("hydration_freshness")

    unique_reasons: list[str] = []
    seen: set[str] = set()
    for reason in reasons:
        if reason in seen:
            continue
        seen.add(reason)
        unique_reasons.append(reason)
    return score, unique_reasons


def _guide_bundle_query_command(row: dict[str, Any], *, query: str, root: Path) -> str:
    guide_id = row.get("guide_id")
    selector = str(guide_id) if isinstance(guide_id, int) else shlex.quote(str(row.get("path")))
    quoted_query = shlex.quote(query)
    quoted_root = shlex.quote(str(root))
    return f"wowhead guide-query {selector} {quoted_query} --root {quoted_root}"


def _guide_bundle_match_total(match_counts: dict[str, Any]) -> int:
    return sum(value for value in match_counts.values() if isinstance(value, int))


def _guide_bundle_meta(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "path": row.get("path"),
        "dir_name": row.get("dir_name"),
        "guide_id": row.get("guide_id"),
        "title": row.get("title"),
        "canonical_url": row.get("canonical_url"),
        "expansion": row.get("expansion"),
        "freshness": row.get("freshness"),
    }


def _bundle_query_result(
    row: dict[str, Any],
    *,
    query: str,
    result: dict[str, Any],
    root: Path,
) -> dict[str, Any] | None:
    match_counts = result["counts"]
    match_count = _guide_bundle_match_total(match_counts)
    if match_count <= 0:
        return None
    return {
        **dict(row),
        "match_count": match_count,
        "match_counts": match_counts,
        "best_score": max((int(item.get("score") or 0) for item in result["top"]), default=0),
        "top": result["top"],
        "suggested_query_command": _guide_bundle_query_command(row, query=query, root=root),
    }


def _bundle_query_top_matches(row: dict[str, Any], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    bundle_meta = _guide_bundle_meta(row)
    return [{**dict(item), "bundle": bundle_meta} for item in rows]


def _scan_guide_bundle_rows(root: Path) -> list[dict[str, Any]]:
    if not root.exists() or not root.is_dir():
        return []

    corpora: list[dict[str, Any]] = []
    for child in sorted(root.iterdir(), key=lambda path: path.name):
        if not child.is_dir():
            continue
        manifest_path = child / "manifest.json"
        if not manifest_path.exists():
            continue
        try:
            manifest = _read_json_file(manifest_path)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(manifest, dict):
            continue
        corpora.append(_bundle_index_row(child, manifest))
    corpora.sort(key=lambda row: ((row.get("title") or "").lower(), row["path"]))
    return corpora


def _write_guide_bundle_index(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    bundles = _scan_guide_bundle_rows(root)
    index_payload = {
        "index_version": 1,
        "updated_at": _iso_now_utc(),
        "root": str(root),
        "count": len(bundles),
        "bundles": bundles,
    }
    _write_json_file(_guide_bundle_index_path(root), index_payload)


def _load_guide_bundle_index(root: Path) -> list[dict[str, Any]] | None:
    index_path = _guide_bundle_index_path(root)
    if not index_path.exists():
        return None
    try:
        payload = _read_json_file(index_path)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    bundles = payload.get("bundles")
    if not isinstance(bundles, list):
        return None
    normalized_rows: list[dict[str, Any]] = []
    for row in bundles:
        if not isinstance(row, dict):
            return None
        path_value = row.get("path")
        if not isinstance(path_value, str):
            return None
        manifest_path = Path(path_value) / "manifest.json"
        if not manifest_path.exists():
            return None
        normalized_rows.append(row)
    normalized_rows.sort(key=lambda row: ((row.get("title") or "").lower(), row["path"]))
    return normalized_rows


def _discover_guide_corpora(root: Path, *, max_age_hours: int) -> list[dict[str, Any]]:
    base_rows = _load_guide_bundle_index(root)
    if base_rows is None:
        base_rows = _scan_guide_bundle_rows(root)
    return [_bundle_row_with_freshness(row, max_age_hours=max_age_hours) for row in base_rows]


def _bundle_freshness_rollups(rows: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    bundle_counts: dict[str, int] = {}
    hydration_counts: dict[str, int] = {}
    for row in rows:
        freshness = row.get("freshness") if isinstance(row.get("freshness"), dict) else None
        if not isinstance(freshness, dict):
            continue
        if freshness.get("bundle") == "stale":
            for reason in freshness.get("bundle_reasons") or []:
                if isinstance(reason, str):
                    bundle_counts[reason] = bundle_counts.get(reason, 0) + 1
        if freshness.get("hydration") == "stale":
            for reason in freshness.get("hydration_reasons") or []:
                if isinstance(reason, str):
                    hydration_counts[reason] = hydration_counts.get(reason, 0) + 1
    return {
        "bundle": dict(sorted(bundle_counts.items())),
        "hydration": dict(sorted(hydration_counts.items())),
    }


def _bundle_file_details(export_dir: Path, manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    files = manifest.get("files")
    if not isinstance(files, dict):
        return {}
    details: dict[str, dict[str, Any]] = {}
    for key, relative_path in files.items():
        if not isinstance(key, str) or not isinstance(relative_path, str):
            continue
        full_path = export_dir / relative_path
        details[key] = {
            "path": str(full_path),
            "exists": full_path.exists(),
        }
    return details


def _load_bundle_entities_manifest(export_dir: Path) -> dict[str, Any] | None:
    path = export_dir / "entities" / "manifest.json"
    if not path.exists():
        return None
    try:
        payload = _read_json_file(path)
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _bundle_observed_counts(corpus: dict[str, Any], entities_manifest: dict[str, Any] | None) -> dict[str, int]:
    hydrated_entities = 0
    if isinstance(entities_manifest, dict):
        raw_count = entities_manifest.get("count")
        if isinstance(raw_count, int):
            hydrated_entities = raw_count
        else:
            items = entities_manifest.get("items")
            if isinstance(items, list):
                hydrated_entities = len(items)
    return {
        "sections": len(corpus.get("sections") or []),
        "analysis_surfaces": len(corpus.get("analysis_surfaces") or []),
        "navigation_links": len(corpus.get("navigation_links") or []),
        "linked_entities": len(corpus.get("linked_entities") or []),
        "gatherer_entities": len(corpus.get("gatherer_entities") or []),
        "hydrated_entities": hydrated_entities,
        "comments": len(corpus.get("comments") or []),
    }


def _bundle_index_status(root: Path, *, export_dir: Path) -> dict[str, Any]:
    index_path = _guide_bundle_index_path(root)
    status = {
        "root": str(root),
        "path": str(index_path),
        "exists": index_path.exists(),
        "valid": False,
        "contains_bundle": False,
        "count": 0,
    }
    if not status["exists"]:
        return status
    rows = _load_guide_bundle_index(root)
    if rows is None:
        return status
    status["valid"] = True
    status["count"] = len(rows)
    status["contains_bundle"] = any(str(export_dir) == row.get("path") for row in rows if isinstance(row, dict))
    return status


def _bundle_inspection_issues(
    *,
    manifest: dict[str, Any],
    file_details: dict[str, dict[str, Any]],
    observed_counts: dict[str, int],
    index_status: dict[str, Any],
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for key, detail in file_details.items():
        if detail.get("exists") is True:
            continue
        issues.append(
            {
                "code": "missing_file",
                "file": key,
                "path": detail.get("path"),
            }
        )

    manifest_counts = manifest.get("counts")
    if isinstance(manifest_counts, dict):
        for key, observed_value in observed_counts.items():
            expected = manifest_counts.get(key)
            if isinstance(expected, int) and expected != observed_value:
                issues.append(
                    {
                        "code": "count_mismatch",
                        "field": key,
                        "manifest": expected,
                        "observed": observed_value,
                    }
                )

    if index_status.get("exists") is True and index_status.get("valid") is not True:
        issues.append(
            {
                "code": "invalid_index",
                "path": index_status.get("path"),
            }
        )
    elif index_status.get("valid") is True and index_status.get("contains_bundle") is not True:
        issues.append(
            {
                "code": "index_missing_bundle",
                "path": index_status.get("path"),
            }
        )
    return issues


def _guide_bundle_inspection_summary(payload: dict[str, Any]) -> dict[str, Any]:
    missing_files = [row.get("file") for row in payload.get("issues") or [] if row.get("code") == "missing_file"]
    count_mismatches = [
        {
            "field": row.get("field"),
            "manifest": row.get("manifest"),
            "observed": row.get("observed"),
        }
        for row in payload.get("issues") or []
        if row.get("code") == "count_mismatch"
    ]
    return {
        "output_dir": payload.get("output_dir"),
        "guide": payload.get("guide"),
        "page": payload.get("page"),
        "expansion": payload.get("expansion"),
        "freshness": payload.get("freshness"),
        "hydration": payload.get("hydration"),
        "index": payload.get("index"),
        "issue_count": len(payload.get("issues") or []),
        "issue_codes": sorted({row.get("code") for row in payload.get("issues") or [] if isinstance(row.get("code"), str)}),
        "missing_files": sorted(file_name for file_name in missing_files if isinstance(file_name, str)),
        "count_mismatches": count_mismatches,
    }


def _guide_bundle_inspection_payload(
    *,
    export_dir: Path,
    corpus: dict[str, Any],
    max_age_hours: int,
) -> dict[str, Any]:
    manifest = corpus["manifest"]
    counts = manifest.get("counts") if isinstance(manifest.get("counts"), dict) else {}
    entities_manifest = _load_bundle_entities_manifest(export_dir)
    observed_counts = _bundle_observed_counts(corpus, entities_manifest)
    file_details = _bundle_file_details(export_dir, manifest)
    index_status = _bundle_index_status(export_dir.parent, export_dir=export_dir)
    issues = _bundle_inspection_issues(
        manifest=manifest,
        file_details=file_details,
        observed_counts=observed_counts,
        index_status=index_status,
    )
    payload = {
        "output_dir": str(export_dir),
        "guide": manifest.get("guide"),
        "page": manifest.get("page"),
        "expansion": manifest.get("expansion"),
        "export_version": manifest.get("export_version"),
        "exported_at": manifest.get("exported_at"),
        "guide_fetched_at": manifest.get("guide_fetched_at"),
        "freshness": _bundle_freshness_summary(manifest, max_age_hours=max_age_hours),
        "counts": {
            "manifest": counts,
            "observed": observed_counts,
        },
        "hydration": _bundle_hydration_summary(manifest, counts=counts),
        "files": file_details,
        "index": index_status,
        "export_options": manifest.get("export_options") if isinstance(manifest.get("export_options"), dict) else {},
        "issues": issues,
    }
    if isinstance(entities_manifest, dict):
        payload["entities_manifest"] = {
            "count": entities_manifest.get("count") if isinstance(entities_manifest.get("count"), int) else observed_counts["hydrated_entities"],
            "hydrated_at": entities_manifest.get("hydrated_at") if isinstance(entities_manifest.get("hydrated_at"), str) else None,
            "counts_by_type": entities_manifest.get("counts_by_type") if isinstance(entities_manifest.get("counts_by_type"), dict) else {},
            "counts_by_storage_source": entities_manifest.get("counts_by_storage_source") if isinstance(entities_manifest.get("counts_by_storage_source"), dict) else {},
    }
    return payload


def _write_guide_export_assets(
    *,
    export_dir: Path,
    payload: dict[str, Any],
    html: str,
) -> tuple[dict[str, str], dict[str, list[Any] | dict[str, Any] | None]]:
    files_written: dict[str, str] = {}

    guide_json_path = export_dir / "guide.json"
    _write_json_file(guide_json_path, payload)
    files_written["guide_json"] = guide_json_path.name

    page_html_path = export_dir / "page.html"
    page_html_path.write_text(html, encoding="utf-8")
    files_written["page_html"] = page_html_path.name

    body = payload.get("body")
    if isinstance(body, dict) and _write_optional_text_file(export_dir / "body.markup.txt", body.get("raw_markup")):
        files_written["body_markup"] = "body.markup.txt"

    navigation = payload.get("navigation")
    if isinstance(navigation, dict) and _write_optional_text_file(
        export_dir / "navigation.markup.txt",
        navigation.get("raw_markup"),
    ):
        files_written["navigation_markup"] = "navigation.markup.txt"

    def write_jsonl_asset(asset_key: str, file_name: str, items: Any) -> list[Any]:
        rows = items if isinstance(items, list) else []
        _write_jsonl_file(export_dir / file_name, rows)
        files_written[asset_key] = file_name
        return rows

    sections = write_jsonl_asset(
        "sections_jsonl",
        "sections.jsonl",
        body.get("section_chunks") if isinstance(body, dict) else [],
    )
    nav_links = write_jsonl_asset(
        "navigation_links_jsonl",
        "navigation-links.jsonl",
        navigation.get("links") if isinstance(navigation, dict) else [],
    )
    linked_items = write_jsonl_asset(
        "linked_entities_jsonl",
        "linked-entities.jsonl",
        (payload.get("linked_entities") or {}).get("items") if isinstance(payload.get("linked_entities"), dict) else [],
    )
    gatherer_items = write_jsonl_asset(
        "gatherer_entities_jsonl",
        "gatherer-entities.jsonl",
        (payload.get("gatherer_entities") or {}).get("items") if isinstance(payload.get("gatherer_entities"), dict) else [],
    )
    comment_items = write_jsonl_asset(
        "comments_jsonl",
        "comments.jsonl",
        (payload.get("comments") or {}).get("items") if isinstance(payload.get("comments"), dict) else [],
    )
    analysis_surface_items = write_jsonl_asset(
        "analysis_surfaces_jsonl",
        "analysis-surfaces.jsonl",
        (payload.get("analysis_surfaces") or {}).get("items") if isinstance(payload.get("analysis_surfaces"), dict) else [],
    )

    structured_data = payload.get("structured_data")
    if structured_data is not None:
        structured_data_path = export_dir / "structured-data.json"
        _write_json_file(structured_data_path, structured_data)
        files_written["structured_data_json"] = structured_data_path.name

    return files_written, {
        "body": body if isinstance(body, dict) else None,
        "navigation": navigation if isinstance(navigation, dict) else None,
        "sections": sections,
        "navigation_links": nav_links,
        "linked_items": linked_items,
        "gatherer_items": gatherer_items,
        "comment_items": comment_items,
        "analysis_surfaces": analysis_surface_items,
    }


def _existing_hydrated_items_by_key(entities_dir: Path) -> dict[tuple[str, int], dict[str, Any]]:
    existing_entities_manifest_path = entities_dir / "manifest.json"
    existing_entities_manifest = (
        _read_json_file(existing_entities_manifest_path)
        if existing_entities_manifest_path.exists()
        else None
    )
    existing_items_by_key: dict[tuple[str, int], dict[str, Any]] = {}
    if isinstance(existing_entities_manifest, dict):
        existing_items = existing_entities_manifest.get("items")
        if isinstance(existing_items, list):
            for row in existing_items:
                if not isinstance(row, dict):
                    continue
                existing_type = row.get("entity_type")
                existing_id = row.get("id")
                if isinstance(existing_type, str) and isinstance(existing_id, int):
                    existing_items_by_key[(existing_type, existing_id)] = row
    return existing_items_by_key


def _hydrate_guide_linked_entities(
    ctx: typer.Context,
    *,
    client: WowheadClient,
    export_dir: Path,
    linked_items: list[Any],
    hydrate_linked_entities: bool,
    hydrate_types: tuple[str, ...],
    hydrate_limit: int,
    rehydrate_max_age_hours: int | None,
    force_rehydrate: bool,
) -> tuple[list[dict[str, Any]], str | None, dict[str, str]]:
    files_written: dict[str, str] = {}
    hydrated_summary_items: list[dict[str, Any]] = []
    hydrated_at: str | None = None
    if not hydrate_linked_entities or not isinstance(linked_items, list):
        return hydrated_summary_items, hydrated_at, files_written

    entities_dir = export_dir / "entities"
    existing_items_by_key = _existing_hydrated_items_by_key(entities_dir)
    selected_types = set(hydrate_types)
    for row in linked_items:
        if len(hydrated_summary_items) >= hydrate_limit:
            break
        if not isinstance(row, dict):
            continue
        hydrated_type = row.get("entity_type")
        hydrated_id = row.get("id")
        if not isinstance(hydrated_type, str) or not isinstance(hydrated_id, int):
            continue
        if hydrated_type not in selected_types:
            continue

        entity_path = entities_dir / hydrated_type / f"{hydrated_id}.json"
        existing_summary = existing_items_by_key.get((hydrated_type, hydrated_id))
        existing_payload: dict[str, Any] | None = None
        stored_at = existing_summary.get("stored_at") if isinstance(existing_summary, dict) else None
        can_reuse_existing = (
            not force_rehydrate
            and rehydrate_max_age_hours is not None
            and entity_path.exists()
            and _is_stored_at_fresh(stored_at, max_age_hours=rehydrate_max_age_hours)
        )
        if can_reuse_existing:
            loaded = _read_json_file(entity_path)
            existing_payload = loaded if isinstance(loaded, dict) else None

        if existing_payload is not None:
            payload_row = existing_payload
            storage_source = "bundle_store"
        else:
            payload_row, storage_source = _load_or_build_cached_entity_payload(
                ctx,
                client,
                entity_type=hydrated_type,
                entity_id=hydrated_id,
                data_env=None,
                include_comments=False,
                include_all_comments=False,
                linked_entity_preview_limit=0,
            )
            entity_path.parent.mkdir(parents=True, exist_ok=True)
            _write_json_file(entity_path, payload_row)
            stored_at = _iso_now_utc()

        entity = payload_row.get("entity")
        if not isinstance(entity, dict):
            continue
        hydrated_summary_items.append(
            {
                "entity_type": hydrated_type,
                "id": hydrated_id,
                "name": entity.get("name"),
                "page_url": entity.get("page_url"),
                "path": str(entity_path.relative_to(export_dir)),
                "stored_at": stored_at,
                "storage_source": storage_source,
            }
        )

    if hydrated_summary_items:
        counts_by_type: dict[str, int] = {}
        for row in hydrated_summary_items:
            hydrated_type = row.get("entity_type")
            if not isinstance(hydrated_type, str):
                continue
            counts_by_type[hydrated_type] = counts_by_type.get(hydrated_type, 0) + 1
        hydrated_at = _iso_now_utc()
        entities_manifest = {
            "hydrated_at": hydrated_at,
            "count": len(hydrated_summary_items),
            "types": list(hydrate_types),
            "counts_by_type": counts_by_type,
            "counts_by_storage_source": _hydrate_source_counts(hydrated_summary_items),
            "items": hydrated_summary_items,
        }
        entities_manifest_path = entities_dir / "manifest.json"
        _write_json_file(entities_manifest_path, entities_manifest)
        files_written["entities_manifest_json"] = "entities/manifest.json"

    return hydrated_summary_items, hydrated_at, files_written


def _guide_export_manifest(
    *,
    export_dir: Path,
    payload: dict[str, Any],
    guide_ref: str,
    max_links: int,
    include_replies: bool,
    hydrate_linked_entities: bool,
    hydrate_types: tuple[str, ...],
    hydrate_limit: int,
    hydrated_summary_items: list[dict[str, Any]],
    hydrated_at: str | None,
    files_written: dict[str, str],
    sections: list[Any],
    nav_links: list[Any],
    linked_items: list[Any],
    gatherer_items: list[Any],
    comment_items: list[Any],
    analysis_surfaces: list[Any],
) -> dict[str, Any]:
    exported_at = _iso_now_utc()
    return {
        "export_version": 2,
        "exported_at": exported_at,
        "guide_fetched_at": exported_at,
        "expansion": payload.get("expansion"),
        "output_dir": str(export_dir),
        "guide": payload.get("guide"),
        "page": {
            "title": payload.get("page", {}).get("title") if isinstance(payload.get("page"), dict) else None,
            "canonical_url": payload.get("page", {}).get("canonical_url")
            if isinstance(payload.get("page"), dict)
            else None,
        },
        "counts": {
            "sections": len(sections) if isinstance(sections, list) else 0,
            "navigation_links": len(nav_links) if isinstance(nav_links, list) else 0,
            "linked_entities": len(linked_items) if isinstance(linked_items, list) else 0,
            "gatherer_entities": len(gatherer_items) if isinstance(gatherer_items, list) else 0,
            "hydrated_entities": len(hydrated_summary_items),
            "comments": len(comment_items) if isinstance(comment_items, list) else 0,
            "analysis_surfaces": len(analysis_surfaces) if isinstance(analysis_surfaces, list) else 0,
        },
        "hydration": {
            "enabled": hydrate_linked_entities,
            "types": list(hydrate_types),
            "limit": hydrate_limit if hydrate_linked_entities else 0,
            "hydrated_at": hydrated_at,
            "source_counts": _hydrate_source_counts(hydrated_summary_items),
        },
        "export_options": {
            "guide_ref": guide_ref,
            "max_links": max_links,
            "include_replies": include_replies,
        },
        "files": files_written,
    }


def _write_guide_export_bundle(
    ctx: typer.Context,
    *,
    client: WowheadClient,
    guide_ref: str,
    export_dir: Path,
    max_links: int,
    include_replies: bool,
    hydrate_linked_entities: bool,
    hydrate_types: tuple[str, ...],
    hydrate_limit: int,
    rehydrate_max_age_hours: int | None = None,
    force_rehydrate: bool = False,
) -> dict[str, Any]:
    payload, html = _build_guide_full_payload(
        ctx,
        guide_ref=guide_ref,
        max_links=max_links,
        include_replies=include_replies,
        client=client,
    )
    export_dir.mkdir(parents=True, exist_ok=True)

    files_written, bundle_parts = _write_guide_export_assets(export_dir=export_dir, payload=payload, html=html)
    linked_items = bundle_parts["linked_items"] if isinstance(bundle_parts["linked_items"], list) else []
    hydrated_summary_items, hydrated_at, hydration_files = _hydrate_guide_linked_entities(
        ctx,
        client=client,
        export_dir=export_dir,
        linked_items=linked_items,
        hydrate_linked_entities=hydrate_linked_entities,
        hydrate_types=hydrate_types,
        hydrate_limit=hydrate_limit,
        rehydrate_max_age_hours=rehydrate_max_age_hours,
        force_rehydrate=force_rehydrate,
    )
    files_written.update(hydration_files)
    manifest = _guide_export_manifest(
        export_dir=export_dir,
        payload=payload,
        guide_ref=guide_ref,
        max_links=max_links,
        include_replies=include_replies,
        hydrate_linked_entities=hydrate_linked_entities,
        hydrate_types=hydrate_types,
        hydrate_limit=hydrate_limit,
        hydrated_summary_items=hydrated_summary_items,
        hydrated_at=hydrated_at,
        files_written=files_written,
        sections=bundle_parts["sections"] if isinstance(bundle_parts["sections"], list) else [],
        nav_links=bundle_parts["navigation_links"] if isinstance(bundle_parts["navigation_links"], list) else [],
        linked_items=linked_items,
        gatherer_items=bundle_parts["gatherer_items"] if isinstance(bundle_parts["gatherer_items"], list) else [],
        comment_items=bundle_parts["comment_items"] if isinstance(bundle_parts["comment_items"], list) else [],
        analysis_surfaces=bundle_parts["analysis_surfaces"] if isinstance(bundle_parts["analysis_surfaces"], list) else [],
    )
    manifest_path = export_dir / "manifest.json"
    _write_json_file(manifest_path, manifest)
    manifest["files"]["manifest_json"] = manifest_path.name
    _write_json_file(manifest_path, manifest)
    _write_guide_bundle_index(export_dir.parent)
    return manifest


def _looks_like_path(value: str) -> bool:
    return value.startswith(("/", ".", "~")) or "/" in value


def _resolve_corpus_ref(corpus_ref: str, *, root: Path | None) -> Path:
    raw = corpus_ref.strip()
    if not raw:
        raise ValueError("Bundle reference cannot be empty.")

    expanded = Path(raw).expanduser()
    if expanded.exists():
        if not expanded.is_dir():
            raise ValueError(f"Bundle path {expanded} is not a directory.")
        return expanded.resolve()
    if _looks_like_path(raw):
        raise ValueError(f"Bundle path {expanded} does not exist.")

    search_root = (root or _guide_export_root()).expanduser()
    corpora = _discover_guide_corpora(search_root, max_age_hours=24)
    if not corpora:
        raise ValueError(f"No exported bundles found under {search_root}.")

    lowered = raw.lower()

    def exact_matches() -> list[dict[str, Any]]:
        matches: list[dict[str, Any]] = []
        for row in corpora:
            guide_id = row.get("guide_id")
            title = row.get("title")
            dir_name = row.get("dir_name")
            if isinstance(guide_id, int) and str(guide_id) == raw:
                matches.append(row)
                continue
            if isinstance(dir_name, str) and dir_name.lower() == lowered:
                matches.append(row)
                continue
            if isinstance(title, str) and title.lower() == lowered:
                matches.append(row)
        return matches

    matches = exact_matches()
    if not matches:
        matches = []
        for row in corpora:
            title = row.get("title")
            dir_name = row.get("dir_name")
            if isinstance(title, str) and lowered in title.lower():
                matches.append(row)
                continue
            if isinstance(dir_name, str) and lowered in dir_name.lower():
                matches.append(row)

    if not matches:
        raise ValueError(f"No bundle matched {raw!r} under {search_root}.")
    if len(matches) > 1:
        options = ", ".join(row.get("dir_name") or row["path"] for row in matches[:5])
        raise ValueError(f"Bundle selector {raw!r} is ambiguous under {search_root}. Matches: {options}")
    return Path(matches[0]["path"])


@app.callback()
def cli(
    ctx: typer.Context,
    pretty: bool = typer.Option(
        False,
        "--pretty",
        help="Pretty-print JSON for human reading. Default output is compact JSON.",
    ),
    expansion: str = typer.Option(
        "retail",
        "--expansion",
        help="Expansion profile key/alias (for example: retail, classic, tbc, wotlk, cata, mop-classic, ptr).",
    ),
    normalize_canonical_to_expansion: bool = typer.Option(
        False,
        "--normalize-canonical-to-expansion/--no-normalize-canonical-to-expansion",
        help="Rewrite canonical entity page URLs to the selected expansion path when canonical redirects across profiles.",
    ),
    compact: bool = typer.Option(
        False,
        "--compact",
        help="Truncate long string fields to reduce payload size.",
    ),
    fields: list[str] = typer.Option(
        [],
        "--fields",
        help="Return only selected fields (dot paths). Repeat or pass comma-separated values.",
    ),
    stream: bool = typer.Option(
        False,
        "--stream",
        help="Emit large result arrays as JSONL (header line plus one record per row).",
    ),
    citation_pack: bool = typer.Option(
        False,
        "--citation-pack",
        help="Attach a deterministic citation_pack with source URLs and per-claim anchors.",
    ),
    profile: str | None = typer.Option(
        None,
        "--profile",
        help="Output profile preset: agent (default compact JSON), human (pretty JSON), debug (pretty JSON + diagnostics).",
    ),
    fields_strict: bool = typer.Option(
        False,
        "--fields-strict",
        help="Fail when a requested --fields dot-path is missing from the payload.",
    ),
    compact_max_chars: int = typer.Option(
        280,
        "--compact-max-chars",
        min=40,
        max=10_000,
        help="Maximum string length before --compact truncation adds an ellipsis.",
    ),
) -> None:
    try:
        expansion_profile = resolve_expansion(expansion)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--expansion") from exc
    try:
        output = resolve_output_options(
            profile=profile,
            pretty=pretty,
            compact=compact,
            compact_max_chars=compact_max_chars,
            fields=fields,
            fields_strict=fields_strict,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--profile") from exc
    ctx.obj = RuntimeConfig(
        stream=stream,
        expansion=expansion_profile,
        normalize_canonical_to_expansion=normalize_canonical_to_expansion,
        output=output,
        citation_pack=citation_pack,
    )


@app.command("doctor")
def doctor(
    ctx: typer.Context,
    no_live: bool = typer.Option(
        False,
        "--no-live",
        help="Skip live Wowhead endpoint probes and report cache/runtime readiness only.",
    ),
) -> None:
    from wowhead_cli.doctor import build_doctor_payload

    cfg = _cfg(ctx)
    try:
        settings = load_cache_settings_from_env()
    except ValueError as exc:
        _fail(ctx, "invalid_cache_config", str(exc))
        return
    cache_payload = _cache_settings_payload(settings)
    payload = build_doctor_payload(cfg.expansion, live=not no_live, cache=cache_payload)
    _emit(ctx, payload)


@app.command("expansions")
def expansions(ctx: typer.Context) -> None:
    profiles = list_profiles()
    payload = {
        "default": resolve_expansion(None).key,
        "profiles": [
            {
                "key": profile.key,
                "label": profile.label,
                "path_prefix": profile.path_prefix,
                "data_env": profile.data_env,
                "aliases": list(profile.aliases),
                "legacy_subdomains": list(profile.legacy_subdomains),
                "wowhead_base": profile.wowhead_base,
                "nether_base": profile.nether_base,
            }
            for profile in profiles
        ],
    }
    _emit(ctx, payload)


@app.command("cache-inspect")
def cache_inspect(
    ctx: typer.Context,
    show_redis_prefixes: bool = typer.Option(
        False,
        "--show-redis-prefixes",
        help="For Redis backends, include a bounded summary of other prefixes in the same Redis.",
    ),
    redis_prefix_limit: int = typer.Option(
        10,
        "--redis-prefix-limit",
        min=1,
        max=100,
        help="Maximum number of Redis prefixes to include when --show-redis-prefixes is used.",
    ),
    summary: bool = typer.Option(
        False,
        "--summary",
        help="Return a compact cache summary instead of the full namespace listing.",
    ),
    namespace_limit: int = typer.Option(
        10,
        "--namespace-limit",
        min=1,
        max=100,
        help="Maximum namespaces to include in summary mode.",
    ),
    hide_zero: bool = typer.Option(
        False,
        "--hide-zero",
        help="Omit zero-valued count fields from cache stats.",
    ),
) -> None:
    settings = _load_cache_settings_or_fail(ctx)
    if settings.backend == "file":
        stats = inspect_file_cache(settings.cache_dir)
    else:
        stats = inspect_redis_cache(
            settings.redis_url,
            prefix=settings.prefix,
            include_prefix_visibility=show_redis_prefixes,
            prefix_limit=redis_prefix_limit,
        )
    payload = {
        "settings": _cache_settings_payload(settings),
        "stats": _cache_stats_payload(stats, summary=summary, namespace_limit=namespace_limit, hide_zero=hide_zero),
    }
    _emit(ctx, payload)


@app.command("cache-repair")
def cache_repair(
    ctx: typer.Context,
    apply: bool = typer.Option(
        False,
        "--apply/--dry-run",
        help="Apply the repair instead of only reporting candidates.",
    ),
    expired_only: bool = typer.Option(
        False,
        "--expired-only/--all",
        help="Restrict legacy file-cache repair to expired entries only.",
    ),
    sample_limit: int = typer.Option(
        10,
        "--sample-limit",
        min=1,
        max=100,
        help="Maximum legacy cache paths to sample in the repair report.",
    ),
) -> None:
    settings = _load_cache_settings_or_fail(ctx)
    if settings.backend != "file":
        _fail(ctx, "invalid_argument", "cache-repair is currently only supported for file cache backends.")
    result = repair_file_cache(settings.cache_dir, apply=apply, expired_only=expired_only, sample_limit=sample_limit)
    payload = {
        "settings": _cache_settings_payload(settings),
        "repair": result,
    }
    if apply:
        payload["remaining"] = inspect_file_cache(settings.cache_dir)
    _emit(ctx, payload)


@app.command("cache-clear")
def cache_clear(
    ctx: typer.Context,
    namespace: list[str] = typer.Option(
        [],
        "--namespace",
        help="Restrict clearing to one or more cache namespaces. Repeat or pass comma-separated values.",
    ),
    expired_only: bool = typer.Option(
        False,
        "--expired-only/--all",
        help="Only clear expired file-cache entries. Ignored by default when clearing all entries.",
    ),
) -> None:
    settings = _load_cache_settings_or_fail(ctx)
    selected_namespaces = _normalize_cache_namespaces(namespace)
    if settings.backend == "file":
        removed = clear_file_cache(
            settings.cache_dir,
            namespaces=selected_namespaces,
            expired_only=expired_only,
        )
        remaining = inspect_file_cache(settings.cache_dir)
    else:
        if expired_only:
            _fail(ctx, "invalid_argument", "--expired-only is only supported for file cache backends.")
        try:
            removed = clear_redis_cache(
                settings.redis_url,
                prefix=settings.prefix,
                namespaces=selected_namespaces,
            )
        except ValueError as exc:
            _fail(ctx, "invalid_cache_config", str(exc))
        remaining = inspect_redis_cache(settings.redis_url, prefix=settings.prefix)
    payload = {
        "settings": _cache_settings_payload(settings),
        "namespaces": list(selected_namespaces),
        "expired_only": expired_only,
        "removed": removed,
        "remaining": remaining,
    }
    _emit(ctx, payload)


@app.command("resolve")
def resolve(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="Natural-language query to resolve to the best next command."),
    entity_type: list[str] = typer.Option(
        [],
        "--entity-type",
        help="Restrict resolution to one or more entity types. Repeat or pass comma-separated values.",
    ),
    limit: int = typer.Option(
        5,
        "--limit",
        min=1,
        max=20,
        help="Maximum fallback candidates to return.",
    ),
) -> None:
    cfg = _cfg(ctx)
    client = _client(ctx)
    try:
        selected_entity_types = _normalize_resolve_entity_types(entity_type)
    except ValueError as exc:
        _fail(ctx, "invalid_argument", str(exc))
    search_query_text = _search_ranking_query(query)
    try:
        response = client.search_suggestions(search_query_text)
    except httpx.HTTPStatusError as exc:
        _fail(ctx, "http_error", f"Wowhead returned HTTP {exc.response.status_code}")
    except httpx.HTTPError as exc:
        _fail(ctx, "network_error", str(exc))
    except ValueError as exc:
        _fail(ctx, "parse_error", str(exc))

    results = response.get("results")
    if not isinstance(results, list):
        _fail(ctx, "unexpected_response", "Missing or invalid 'results' payload from Wowhead.")

    candidates = _normalize_search_results(
        results,
        query=query,
        expansion=cfg.expansion,
        entity_types=selected_entity_types,
    )
    confidence = _resolve_confidence(candidates, entity_types=selected_entity_types)
    top_candidate = candidates[0] if candidates else None
    next_command = (
        _resolve_next_command(top_candidate, expansion=cfg.expansion)
        if isinstance(top_candidate, dict) and confidence == "high"
        else None
    )
    search_command = f"{_command_prefix_for_expansion(cfg.expansion)} search {shlex.quote(query)}"
    payload = {
        "query": query,
        "search_query": search_query_text,
        "expansion": cfg.expansion.key,
        "search_url": search_url(search_query_text, expansion=cfg.expansion),
        "filters": {
            "entity_types": list(selected_entity_types),
        },
        "resolved": next_command is not None,
        "confidence": confidence,
        "match": top_candidate,
        "next_command": next_command,
        "fallback_search_command": None if next_command is not None else search_command,
        "count": len(candidates),
        "candidates": candidates[:limit],
    }
    _emit(ctx, payload)


@app.command("search")
def search(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="Search text."),
    limit: int = typer.Option(
        10,
        "--limit",
        min=1,
        max=50,
        help="Maximum number of results to return.",
    ),
) -> None:
    cfg = _cfg(ctx)
    client = _client(ctx)
    search_query_text = _search_ranking_query(query)
    try:
        response = client.search_suggestions(search_query_text)
    except httpx.HTTPStatusError as exc:
        _fail(ctx, "http_error", f"Wowhead returned HTTP {exc.response.status_code}")
    except httpx.HTTPError as exc:
        _fail(ctx, "network_error", str(exc))
    except ValueError as exc:
        _fail(ctx, "parse_error", str(exc))

    results = response.get("results")
    if not isinstance(results, list):
        _fail(ctx, "unexpected_response", "Missing or invalid 'results' payload from Wowhead.")

    normalized = _normalize_search_results(results, query=query, expansion=cfg.expansion)

    payload: dict[str, Any] = {
        "query": query,
        "search_query": search_query_text,
        "expansion": cfg.expansion.key,
        "search_url": search_url(search_query_text, expansion=cfg.expansion),
        "count": len(normalized),
        "results": normalized[:limit],
    }
    _emit(ctx, payload)


@app.command("news")
def news(
    ctx: typer.Context,
    query: str | None = typer.Argument(None, help="Optional topic text used to filter Wowhead news posts."),
    author: list[str] = typer.Option(
        [],
        "--author",
        help="Restrict matches to one or more author names. Repeat or pass comma-separated values.",
    ),
    type_name: list[str] = typer.Option(
        [],
        "--type",
        help="Restrict matches to one or more Wowhead news types such as Live or PTR. Repeat or pass comma-separated values.",
    ),
    page: int = typer.Option(
        1,
        "--page",
        min=1,
        help="First Wowhead news page to scan.",
    ),
    pages: int = typer.Option(
        1,
        "--pages",
        min=1,
        max=100,
        help="Maximum number of pages to scan for matches.",
    ),
    limit: int = typer.Option(
        20,
        "--limit",
        min=1,
        max=200,
        help="Maximum number of matching posts to return from the scanned page window.",
    ),
    date_from: str | None = typer.Option(
        None,
        "--date-from",
        help="Inclusive UTC lower bound. Accepts YYYY-MM-DD or full ISO-8601 timestamps.",
    ),
    date_to: str | None = typer.Option(
        None,
        "--date-to",
        help="Inclusive UTC upper bound. Accepts YYYY-MM-DD or full ISO-8601 timestamps.",
    ),
) -> None:
    cfg = _cfg(ctx)
    client = _client(ctx)
    selected_authors = _normalize_text_filters(author)
    selected_types = _normalize_text_filters(type_name)
    parsed_date_from = _parse_date_bound(date_from, end_of_day=False)
    if date_from is not None and parsed_date_from is None:
        _fail(ctx, "invalid_argument", f"Invalid --date-from value {date_from!r}.")
    parsed_date_to = _parse_date_bound(date_to, end_of_day=True)
    if date_to is not None and parsed_date_to is None:
        _fail(ctx, "invalid_argument", f"Invalid --date-to value {date_to!r}.")
    if (
        parsed_date_from is not None
        and parsed_date_to is not None
        and parsed_date_from > parsed_date_to
    ):
        _fail(ctx, "invalid_argument", "--date-from must be <= --date-to.")

    collected = _collect_timeline_pages(
        ctx=ctx,
        page=page,
        pages=pages,
        fetch_page=lambda current_page: client.news_page_html(page=current_page),
        extract_page=_extract_news_page_data,
        normalize_row=_normalize_news_row,
        query=query,
        date_from=parsed_date_from,
        date_to=parsed_date_to,
    )
    filtered_results = [
        row
        for row in collected["results"]
        if _text_filter_match(row.get("author"), selected_authors)
        and _text_filter_match(row.get("type_name"), selected_types)
    ]
    payload = {
        "query": query,
        "expansion": cfg.expansion.key,
        "news_url": news_url(page=page, expansion=cfg.expansion),
        "filters": {
            "authors": list(selected_authors),
            "types": list(selected_types),
            "date_from": parsed_date_from.isoformat() if parsed_date_from is not None else None,
            "date_to": parsed_date_to.isoformat() if parsed_date_to is not None else None,
        },
        "scan": {
            "page": page,
            "pages_requested": pages,
            "pages_scanned": collected["pages_scanned"],
            "total_pages": collected["total_pages"],
            "stop_reason": collected["stop_reason"],
        },
        "count": len(filtered_results),
        "results": filtered_results[:limit],
        "facets": _collect_timeline_facets(filtered_results, fields={"authors": "author", "types": "type_name"}),
    }
    _emit(ctx, payload)


@app.command("blue-tracker")
def blue_tracker(
    ctx: typer.Context,
    query: str | None = typer.Argument(None, help="Optional topic text used to filter blue tracker topics."),
    author: list[str] = typer.Option(
        [],
        "--author",
        help="Restrict matches to one or more blue-post author names. Repeat or pass comma-separated values.",
    ),
    region: list[str] = typer.Option(
        [],
        "--region",
        help="Restrict matches to one or more regions such as us or eu. Repeat or pass comma-separated values.",
    ),
    forum: list[str] = typer.Option(
        [],
        "--forum",
        help="Restrict matches to one or more forum names. Repeat or pass comma-separated values.",
    ),
    page: int = typer.Option(
        1,
        "--page",
        min=1,
        help="First Wowhead blue-tracker page to scan.",
    ),
    pages: int = typer.Option(
        1,
        "--pages",
        min=1,
        max=100,
        help="Maximum number of pages to scan for matches.",
    ),
    limit: int = typer.Option(
        20,
        "--limit",
        min=1,
        max=200,
        help="Maximum number of matching topics to return from the scanned page window.",
    ),
    date_from: str | None = typer.Option(
        None,
        "--date-from",
        help="Inclusive UTC lower bound. Accepts YYYY-MM-DD or full ISO-8601 timestamps.",
    ),
    date_to: str | None = typer.Option(
        None,
        "--date-to",
        help="Inclusive UTC upper bound. Accepts YYYY-MM-DD or full ISO-8601 timestamps.",
    ),
) -> None:
    cfg = _cfg(ctx)
    client = _client(ctx)
    selected_authors = _normalize_text_filters(author)
    selected_regions = _normalize_text_filters(region)
    selected_forums = _normalize_text_filters(forum)
    parsed_date_from = _parse_date_bound(date_from, end_of_day=False)
    if date_from is not None and parsed_date_from is None:
        _fail(ctx, "invalid_argument", f"Invalid --date-from value {date_from!r}.")
    parsed_date_to = _parse_date_bound(date_to, end_of_day=True)
    if date_to is not None and parsed_date_to is None:
        _fail(ctx, "invalid_argument", f"Invalid --date-to value {date_to!r}.")
    if (
        parsed_date_from is not None
        and parsed_date_to is not None
        and parsed_date_from > parsed_date_to
    ):
        _fail(ctx, "invalid_argument", "--date-from must be <= --date-to.")

    collected = _collect_timeline_pages(
        ctx=ctx,
        page=page,
        pages=pages,
        fetch_page=lambda current_page: client.blue_tracker_page_html(page=current_page),
        extract_page=_extract_blue_tracker_page_data,
        normalize_row=_normalize_blue_tracker_row,
        query=query,
        date_from=parsed_date_from,
        date_to=parsed_date_to,
    )
    filtered_results = [
        row
        for row in collected["results"]
        if _text_filter_match(row.get("author"), selected_authors)
        and _text_filter_match(row.get("region"), selected_regions)
        and _text_filter_match(row.get("forum"), selected_forums)
    ]
    payload = {
        "query": query,
        "expansion": cfg.expansion.key,
        "blue_tracker_url": blue_tracker_url(page=page, expansion=cfg.expansion),
        "filters": {
            "authors": list(selected_authors),
            "regions": list(selected_regions),
            "forums": list(selected_forums),
            "date_from": parsed_date_from.isoformat() if parsed_date_from is not None else None,
            "date_to": parsed_date_to.isoformat() if parsed_date_to is not None else None,
        },
        "scan": {
            "page": page,
            "pages_requested": pages,
            "pages_scanned": collected["pages_scanned"],
            "total_pages": collected["total_pages"],
            "stop_reason": collected["stop_reason"],
        },
        "count": len(filtered_results),
        "results": filtered_results[:limit],
        "facets": _collect_timeline_facets(
            filtered_results,
            fields={"authors": "author", "regions": "region", "forums": "forum"},
        ),
    }
    _emit(ctx, payload)


@app.command("news-post")
def news_post(
    ctx: typer.Context,
    ref: str = typer.Argument(
        ...,
        help="Full Wowhead news URL or /news/... path returned by `wowhead news`.",
    ),
    related_limit: int = typer.Option(
        5,
        "--related-limit",
        min=1,
        max=25,
        help="Maximum related rows to keep from each embedded recent-post bucket.",
    ),
) -> None:
    cfg = _cfg(ctx)
    try:
        page_url = _normalize_news_post_ref(ref, expansion=cfg.expansion)
    except ValueError as exc:
        _fail(ctx, "invalid_ref", str(exc))
    client = _client(ctx)
    try:
        html = client.page_html(page_url)
    except httpx.HTTPStatusError as exc:
        _fail(ctx, "http_error", f"Wowhead returned HTTP {exc.response.status_code}")
    except httpx.HTTPError as exc:
        _fail(ctx, "network_error", str(exc))
    metadata = parse_page_metadata(html, fallback_url=page_url)
    canonical_url = _absolute_wowhead_url(metadata.get("canonical_url"), fallback=page_url)
    markup = _extract_news_post_markup(html) or ""
    sections = extract_guide_sections(markup) if markup else []
    section_chunks = extract_guide_section_chunks(markup) if markup else []
    recent_posts = _extract_news_recent_posts(html, limit=related_limit)
    author_embed = None
    try:
        raw_author = extract_json_script(html, "data.newsPost.aboutTheAuthor.embedData")
        if isinstance(raw_author, dict):
            author_embed = raw_author
    except (ValueError, json.JSONDecodeError):
        author_embed = None

    payload = {
        "expansion": cfg.expansion.key,
        "post": {
            "input": ref,
            "page_url": page_url,
            "title": metadata.get("title"),
        },
        "page": {
            "title": metadata.get("title"),
            "description": metadata.get("description"),
            "canonical_url": canonical_url,
        },
        "content": {
            "text": clean_markup_text(markup),
            "section_count": len(sections),
            "sections": sections,
            "section_chunks": section_chunks,
        },
        "citations": {
            "page": page_url,
        },
    }
    if author_embed is not None:
        payload["author"] = author_embed
    if recent_posts is not None:
        payload["related"] = recent_posts
    _emit(ctx, payload)


@app.command("blue-topic")
def blue_topic(
    ctx: typer.Context,
    ref: str = typer.Argument(
        ...,
        help="Full Wowhead blue-tracker topic URL or /blue-tracker/topic/... path returned by `wowhead blue-tracker`.",
    ),
) -> None:
    cfg = _cfg(ctx)
    try:
        page_url = _normalize_blue_topic_ref(ref, expansion=cfg.expansion)
    except ValueError as exc:
        _fail(ctx, "invalid_ref", str(exc))
    client = _client(ctx)
    try:
        html = client.page_html(page_url)
    except httpx.HTTPStatusError as exc:
        _fail(ctx, "http_error", f"Wowhead returned HTTP {exc.response.status_code}")
    except httpx.HTTPError as exc:
        _fail(ctx, "network_error", str(exc))
    metadata = parse_page_metadata(html, fallback_url=page_url)
    canonical_url = _absolute_wowhead_url(metadata.get("canonical_url"), fallback=page_url)
    try:
        topic_payload = extract_json_script(html, "data.blueTracker.topic")
    except (ValueError, json.JSONDecodeError) as exc:
        _fail(ctx, "parse_error", str(exc))
    entries = topic_payload.get("entries") if isinstance(topic_payload, dict) else None
    if not isinstance(entries, list):
        _fail(ctx, "unexpected_response", "Missing or invalid blue topic entries payload.")
    posts: list[dict[str, Any]] = []
    for row in entries:
        if not isinstance(row, dict):
            continue
        posts.append(
            {
                "post_id": row.get("post"),
                "topic_id": row.get("topic"),
                "author": row.get("author"),
                "author_page": _absolute_wowhead_url(row.get("authorUrl")),
                "avatar": row.get("avatar"),
                "posted": row.get("posted"),
                "posted_full": row.get("date"),
                "updated": row.get("updated"),
                "body_html": row.get("body"),
                "body_text": _clean_htmlish_text(row.get("body")),
                "region": row.get("region"),
                "forum_area": row.get("forumArea"),
                "forum_area_slug": row.get("forumAreaSlug"),
                "forum": row.get("forum"),
                "job_title": row.get("jobtitle"),
                "blue": bool(row.get("blue")),
                "system": bool(row.get("system")),
                "index": row.get("index"),
            }
        )
    participants = sorted({post["author"] for post in posts if isinstance(post.get("author"), str) and post["author"]})
    blue_authors = sorted(
        {
            post["author"]
            for post in posts
            if post.get("blue") and isinstance(post.get("author"), str) and post["author"]
        }
    )

    payload = {
        "expansion": cfg.expansion.key,
        "topic": {
            "input": ref,
            "page_url": page_url,
            "title": metadata.get("title"),
        },
        "page": {
            "title": metadata.get("title"),
            "description": metadata.get("description"),
            "canonical_url": canonical_url,
        },
        "posts": {
            "count": len(posts),
            "items": posts,
        },
        "summary": {
            "participants": participants,
            "participant_count": len(participants),
            "blue_authors": blue_authors,
            "blue_author_count": len(blue_authors),
        },
        "citations": {
            "page": page_url,
        },
    }
    _emit(ctx, payload)


def _validated_guides_filters(
    *,
    category: str,
    author: list[str],
    updated_after: str | None,
    updated_before: str | None,
    patch_min: int | None,
    patch_max: int | None,
    sort_by: str,
) -> tuple[str, tuple[str, ...], datetime | None, datetime | None]:
    if sort_by not in {"relevance", "updated", "published", "rating"}:
        raise ValueError("--sort must be one of: relevance, updated, published, rating.")
    selected_authors = _normalize_text_filters(author)
    parsed_updated_after = _parse_date_bound(updated_after, end_of_day=False)
    if updated_after is not None and parsed_updated_after is None:
        raise ValueError(f"Invalid --updated-after value {updated_after!r}.")
    parsed_updated_before = _parse_date_bound(updated_before, end_of_day=True)
    if updated_before is not None and parsed_updated_before is None:
        raise ValueError(f"Invalid --updated-before value {updated_before!r}.")
    if parsed_updated_after is not None and parsed_updated_before is not None and parsed_updated_after > parsed_updated_before:
        raise ValueError("--updated-after must be <= --updated-before.")
    if patch_min is not None and patch_max is not None and patch_min > patch_max:
        raise ValueError("--patch-min must be <= --patch-max.")
    normalized_category = category.strip().strip("/")
    if not normalized_category:
        raise ValueError("Guide category cannot be empty.")
    return normalized_category, selected_authors, parsed_updated_after, parsed_updated_before


def _guide_row_matches_filters(
    row: dict[str, Any],
    *,
    selected_authors: tuple[str, ...],
    parsed_updated_after: datetime | None,
    parsed_updated_before: datetime | None,
    patch_min: int | None,
    patch_max: int | None,
) -> bool:
    if not _text_filter_match(row.get("author"), selected_authors):
        return False
    updated_at = _parse_iso8601_utc(row.get("last_updated"))
    if parsed_updated_after is not None and (updated_at is None or updated_at < parsed_updated_after):
        return False
    if parsed_updated_before is not None and (updated_at is None or updated_at > parsed_updated_before):
        return False
    patch_value = row.get("patch")
    if patch_min is not None and (not isinstance(patch_value, int) or patch_value < patch_min):
        return False
    if patch_max is not None and (not isinstance(patch_value, int) or patch_value > patch_max):
        return False
    return True


def _filtered_guide_category_rows(
    rows: list[Any],
    *,
    query_text: str | None,
    selected_authors: tuple[str, ...],
    parsed_updated_after: datetime | None,
    parsed_updated_before: datetime | None,
    patch_min: int | None,
    patch_max: int | None,
    sort_by: str,
) -> list[dict[str, Any]]:
    normalized_rows: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        normalized_row = _normalize_guide_category_row(row)
        if normalized_row is None:
            continue
        if not _guide_row_matches_filters(
            normalized_row,
            selected_authors=selected_authors,
            parsed_updated_after=parsed_updated_after,
            parsed_updated_before=parsed_updated_before,
            patch_min=patch_min,
            patch_max=patch_max,
        ):
            continue
        if query_text is not None:
            score = _score_text_match(
                query_text,
                normalized_row.get("title"),
                normalized_row.get("name"),
                normalized_row.get("author"),
                normalized_row.get("category_path"),
            )
            if score <= 0:
                continue
            normalized_row["match_score"] = score
        normalized_row["_sort"] = _guide_sort_key(normalized_row, sort_by=sort_by, fallback_index=index)
        normalized_rows.append(normalized_row)
    normalized_rows.sort(key=lambda row: row.pop("_sort"))
    return normalized_rows


def _guides_payload(
    *,
    cfg: RuntimeConfig,
    category: str,
    query: str | None,
    sort_by: str,
    selected_authors: tuple[str, ...],
    parsed_updated_after: datetime | None,
    parsed_updated_before: datetime | None,
    patch_min: int | None,
    patch_max: int | None,
    normalized_rows: list[dict[str, Any]],
    limit: int,
) -> dict[str, Any]:
    return {
        "query": query,
        "expansion": cfg.expansion.key,
        "category": category,
        "guides_url": guide_category_url(category, expansion=cfg.expansion),
        "filters": {
            "authors": list(selected_authors),
            "updated_after": parsed_updated_after.isoformat() if parsed_updated_after is not None else None,
            "updated_before": parsed_updated_before.isoformat() if parsed_updated_before is not None else None,
            "patch_min": patch_min,
            "patch_max": patch_max,
            "sort": sort_by,
        },
        "count": len(normalized_rows),
        "results": normalized_rows[:limit],
        "facets": _collect_timeline_facets(
            normalized_rows,
            fields={"authors": "author", "category_paths": "category_path"},
        ),
    }


@app.command("guides")
def guides(
    ctx: typer.Context,
    category: str = typer.Argument(..., help="Wowhead guide category slug such as classes, professions, or raids."),
    query: str | None = typer.Argument(None, help="Optional text used to filter guide rows within the category."),
    author: list[str] = typer.Option(
        [],
        "--author",
        help="Restrict matches to one or more guide author names. Repeat or pass comma-separated values.",
    ),
    updated_after: str | None = typer.Option(
        None,
        "--updated-after",
        help="Inclusive lower bound for guide last-updated timestamps. Accepts YYYY-MM-DD or full ISO-8601 timestamps.",
    ),
    updated_before: str | None = typer.Option(
        None,
        "--updated-before",
        help="Inclusive upper bound for guide last-updated timestamps. Accepts YYYY-MM-DD or full ISO-8601 timestamps.",
    ),
    patch_min: int | None = typer.Option(
        None,
        "--patch-min",
        min=0,
        help="Minimum patch build number to keep.",
    ),
    patch_max: int | None = typer.Option(
        None,
        "--patch-max",
        min=0,
        help="Maximum patch build number to keep.",
    ),
    sort_by: str = typer.Option(
        "relevance",
        "--sort",
        help="Sort results by relevance, updated, published, or rating.",
        show_default=True,
    ),
    limit: int = typer.Option(
        20,
        "--limit",
        min=1,
        max=200,
        help="Maximum matching guides to return.",
    ),
) -> None:
    cfg = _cfg(ctx)
    client = _client(ctx)
    try:
        normalized_category, selected_authors, parsed_updated_after, parsed_updated_before = _validated_guides_filters(
            category=category,
            author=author,
            updated_after=updated_after,
            updated_before=updated_before,
            patch_min=patch_min,
            patch_max=patch_max,
            sort_by=sort_by,
        )
    except ValueError as exc:
        _fail(ctx, "invalid_argument", str(exc))
    try:
        html = client.guide_category_page_html(normalized_category)
    except httpx.HTTPStatusError as exc:
        _fail(ctx, "http_error", f"Wowhead returned HTTP {exc.response.status_code}")
    except httpx.HTTPError as exc:
        _fail(ctx, "network_error", str(exc))

    try:
        rows = extract_listview_data(html, "guides")
    except (ValueError, json.JSONDecodeError) as exc:
        _fail(ctx, "parse_error", str(exc))

    query_text = query.strip() if isinstance(query, str) and query.strip() else None
    normalized_rows = _filtered_guide_category_rows(
        rows,
        query_text=query_text,
        selected_authors=selected_authors,
        parsed_updated_after=parsed_updated_after,
        parsed_updated_before=parsed_updated_before,
        patch_min=patch_min,
        patch_max=patch_max,
        sort_by=sort_by,
    )

    _emit(
        ctx,
        _guides_payload(
            cfg=cfg,
            category=normalized_category,
            query=query,
            sort_by=sort_by,
            selected_authors=selected_authors,
            parsed_updated_after=parsed_updated_after,
            parsed_updated_before=parsed_updated_before,
            patch_min=patch_min,
            patch_max=patch_max,
            normalized_rows=normalized_rows,
            limit=limit,
        ),
    )


@app.command("talent-calc")
def talent_calc(
    ctx: typer.Context,
    ref: str = typer.Argument(
        ...,
        help="Wowhead talent calculator URL, path, or class/spec/build ref such as druid/balance/<code>.",
    ),
    listed_build_limit: int = typer.Option(
        10,
        "--listed-build-limit",
        min=1,
        max=100,
        help="Maximum embedded listed builds to return when the page exposes them.",
    ),
) -> None:
    _emit(ctx, _talent_calc_payload(ctx, ref=ref, listed_build_limit=listed_build_limit))


@app.command("talent-calc-packet")
def talent_calc_packet(
    ctx: typer.Context,
    ref: str = typer.Argument(
        ...,
        help="Wowhead talent calculator URL, path, or class/spec/build ref with a build code such as druid/balance/<code>.",
    ),
    listed_build_limit: int = typer.Option(
        10,
        "--listed-build-limit",
        min=1,
        max=100,
        help="Maximum embedded listed builds to return when the page exposes them.",
    ),
    out: str | None = typer.Option(None, "--out", help="Optional path to write just the exact talent transport packet JSON."),
) -> None:
    payload = _base_talent_calc_payload(ctx, ref=ref)
    if not payload["tool"].get("has_build_code"):
        _fail(ctx, "invalid_tool_ref", "talent-calc packet refs must include an explicit build code.")
        raise AssertionError("unreachable")
    payload = _enrich_talent_calc_payload_with_page_data(
        ctx,
        payload,
        listed_build_limit=listed_build_limit,
        fail_on_fetch_error=False,
    )
    packet = _validated_transport_packet(
        ctx,
        build_reference_transport_packet_payload(
            ref=str(payload["tool"]["state_url"]),
            provider="wowhead",
            source="wowhead_talent_calc_url",
            source_url=str(payload["page"]["canonical_url"]),
            notes=["exact transport packet came from an explicit Wowhead talent-calc ref"],
            scope={"type": "wowhead_talent_calc", "expansion": str(payload["tool"]["expansion"])},
        ),
        command_name="wowhead talent-calc-packet",
    )
    written_packet_path: str | None = None
    if isinstance(out, str) and out.strip():
        try:
            output_path = Path(out).expanduser().resolve()
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(json.dumps(packet, indent=2) + "\n", encoding="utf-8")
            written_packet_path = str(output_path)
        except OSError as exc:
            _fail(ctx, "transport_packet_write_failed", f"Failed to write talent transport packet: {exc}")
            raise AssertionError("unreachable")
    _emit(
        ctx,
        {
            "provider": "wowhead",
            "kind": "talent_calc_packet",
            **payload,
            "talent_transport_packet": packet,
            "written_packet_path": written_packet_path,
        },
    )


@app.command("profession-tree")
def profession_tree(
    ctx: typer.Context,
    ref: str = typer.Argument(
        ...,
        help="Wowhead profession tree URL, path, or profession/loadout ref such as alchemy/BCuA.",
    ),
) -> None:
    cfg = _cfg(ctx)
    try:
        state_url = _normalize_tool_ref(ref, tool_slug="profession-tree-calc", expansion=cfg.expansion)
        state = _parse_profession_tree_state(state_url)
    except ValueError as exc:
        _fail(ctx, "invalid_tool_ref", str(exc))
    client = _client(ctx)
    try:
        html = client.page_html(state_url)
    except httpx.HTTPStatusError as exc:
        _fail(ctx, "http_error", f"Wowhead returned HTTP {exc.response.status_code}")
    except httpx.HTTPError as exc:
        _fail(ctx, "network_error", str(exc))
    metadata = parse_page_metadata(html, fallback_url=state_url)
    canonical_url = _absolute_wowhead_url(metadata.get("canonical_url"), fallback=state_url)

    payload = {
        "expansion": cfg.expansion.key,
        "tool": {
            "kind": "profession-tree",
            "input": ref,
            "state_url": state_url,
            "page_url": canonical_url or state_url,
            **state,
        },
        "page": {
            "title": metadata.get("title"),
            "description": metadata.get("description"),
            "canonical_url": canonical_url,
        },
        "citations": {
            "page": state_url,
        },
    }
    _emit(ctx, payload)


@app.command("dressing-room")
def dressing_room(
    ctx: typer.Context,
    ref: str = typer.Argument(
        ...,
        help="Wowhead dressing room URL, path, or raw share hash.",
    ),
) -> None:
    cfg = _cfg(ctx)
    try:
        state_url = _normalize_dressing_room_ref(ref, expansion=cfg.expansion)
        state = _parse_dressing_room_state(state_url)
    except ValueError as exc:
        _fail(ctx, "invalid_tool_ref", str(exc))
    client = _client(ctx)
    fetch_url = tool_url("dressing-room", expansion=cfg.expansion)
    try:
        html = client.page_html(fetch_url)
    except httpx.HTTPStatusError as exc:
        _fail(ctx, "http_error", f"Wowhead returned HTTP {exc.response.status_code}")
    except httpx.HTTPError as exc:
        _fail(ctx, "network_error", str(exc))
    metadata = parse_page_metadata(html, fallback_url=state_url)
    canonical_url = _absolute_wowhead_url(metadata.get("canonical_url"), fallback=fetch_url)

    payload = {
        "expansion": cfg.expansion.key,
        "tool": {
            "kind": "dressing-room",
            "input": ref,
            "state_url": state_url,
            "page_url": canonical_url or fetch_url,
            **state,
        },
        "page": {
            "title": metadata.get("title"),
            "description": metadata.get("description"),
            "canonical_url": canonical_url,
        },
        "citations": {
            "page": state_url,
        },
    }
    _emit(ctx, payload)


@app.command("profiler")
def profiler(
    ctx: typer.Context,
    ref: str = typer.Argument(
        ...,
        help="Wowhead profiler URL, path, or raw list ref such as 97060220/us/illidan/Roguecane.",
    ),
) -> None:
    cfg = _cfg(ctx)
    try:
        state_url = _normalize_profiler_ref(ref, expansion=cfg.expansion)
        state = _parse_profiler_state(state_url)
    except ValueError as exc:
        _fail(ctx, "invalid_tool_ref", str(exc))
    client = _client(ctx)
    fetch_url = tool_url("list", expansion=cfg.expansion)
    try:
        html = client.page_html(fetch_url)
    except httpx.HTTPStatusError as exc:
        _fail(ctx, "http_error", f"Wowhead returned HTTP {exc.response.status_code}")
    except httpx.HTTPError as exc:
        _fail(ctx, "network_error", str(exc))
    metadata = parse_page_metadata(html, fallback_url=state_url)
    canonical_url = _absolute_wowhead_url(metadata.get("canonical_url"), fallback=fetch_url)

    payload = {
        "expansion": cfg.expansion.key,
        "tool": {
            "kind": "profiler",
            "input": ref,
            "state_url": state_url,
            "page_url": canonical_url or fetch_url,
            **state,
        },
        "page": {
            "title": metadata.get("title"),
            "description": metadata.get("description"),
            "canonical_url": canonical_url,
        },
        "citations": {
            "page": state_url,
        },
    }
    _emit(ctx, payload)


@app.command("guide")
def guide(
    ctx: typer.Context,
    guide_ref: str = typer.Argument(
        ...,
        help="Guide id, Wowhead guide URL, or guide path.",
    ),
    comment_sample: int = typer.Option(
        3,
        "--comment-sample",
        min=0,
        max=20,
        help="Top comments to include (sorted by rating).",
    ),
    comment_chars: int = typer.Option(
        320,
        "--comment-chars",
        min=60,
        max=2000,
        help="Maximum characters for each sampled comment body.",
    ),
    linked_entity_preview_limit: int = typer.Option(
        5,
        "--linked-entity-preview-limit",
        min=0,
        max=50,
        help="Maximum linked entities to include as a lightweight preview. Set to 0 to disable.",
    ),
) -> None:
    cfg = _cfg(ctx)
    client = _client(ctx)
    html, guide_id, lookup_url, metadata, canonical_url = _fetch_guide_page(
        ctx,
        client,
        guide_ref=guide_ref,
    )

    raw_comments: list[dict[str, Any]]
    try:
        raw_comments = extract_comments_dataset(html)
    except ValueError:
        raw_comments = []

    sampled_comments: list[dict[str, Any]] = []
    if comment_sample > 0 and raw_comments:
        ranked = sort_comments(raw_comments, "rating")
        sampled_norm = normalize_comments(
            ranked[:comment_sample],
            page_url=canonical_url,
            include_replies=False,
        )
        for row in sampled_norm:
            sampled_comments.append(
                {
                    "id": row.get("id"),
                    "user": row.get("user"),
                    "rating": row.get("rating"),
                    "date": row.get("date"),
                    "body": _truncate_text(row.get("body"), max_chars=comment_chars),
                    "citation_url": row.get("citation_url"),
                }
            )

    href_entities, gatherer_entities, merged_entities = _collect_guide_linked_entities(
        html=html,
        canonical_url=canonical_url,
        guide_id=guide_id,
        max_links=5000,
    )
    linked_preview = _build_linked_entity_preview(
        merged_entities,
        entity_type="guide",
        entity_id=guide_id or 0,
        preview_limit=linked_entity_preview_limit,
        fetch_more_command=f"wowhead guide-full {guide_ref}",
    )
    linked_preview["source_counts"] = {
        "href": len(href_entities),
        "gatherer": len(gatherer_entities),
        "merged": linked_preview["count"],
    }
    guide_body_markup = extract_markup_by_target(html, target="guide-body")
    analysis_surfaces = extract_section_chunk_analysis_surfaces(
        provider="wowhead",
        page_url=canonical_url,
        page_title=metadata["title"],
        section_chunks=extract_guide_section_chunks(guide_body_markup) if isinstance(guide_body_markup, str) else [],
    )

    page_meta_json = parse_page_meta_json(html)
    payload: dict[str, Any] = {
        "expansion": cfg.expansion.key,
        "guide": {
            "input": guide_ref,
            "id": guide_id,
            "lookup_url": lookup_url,
            "page_url": canonical_url,
        },
        "query": {
            "comment_sample": comment_sample,
            "comment_chars": comment_chars,
        },
        "page": {
            "title": metadata["title"],
            "description": metadata["description"],
            "canonical_url": canonical_url,
        },
        "comments": {
            "count": len(raw_comments),
            "top": sampled_comments,
        },
        "linked_entities": linked_preview,
        "analysis_surfaces": {
            "count": len(analysis_surfaces),
            "items": analysis_surfaces[:10],
            "more_available": len(analysis_surfaces) > 10,
            "fetch_more_command": f"wowhead guide-full {guide_ref}",
        },
        "citations": {
            "page": canonical_url,
            "comments": f"{canonical_url}#comments",
        },
    }
    if isinstance(page_meta_json, dict):
        payload["page_meta"] = {
            "page": page_meta_json.get("page"),
            "server_time": page_meta_json.get("serverTime"),
            "available_data_envs": page_meta_json.get("availableDataEnvs"),
            "env_domain": page_meta_json.get("envDomain"),
        }
    _emit(ctx, payload)


@app.command("guide-full")
def guide_full(
    ctx: typer.Context,
    guide_ref: str = typer.Argument(
        ...,
        help="Guide id, Wowhead guide URL, or guide path.",
    ),
    max_links: int = typer.Option(
        250,
        "--max-links",
        min=1,
        max=2000,
        help="Maximum linked entities to return.",
    ),
    include_replies: bool = typer.Option(
        False,
        "--include-replies/--no-include-replies",
        help="Include inline replies already present in the embedded comments payload.",
    ),
) -> None:
    payload, _html = _build_guide_full_payload(
        ctx,
        guide_ref=guide_ref,
        max_links=max_links,
        include_replies=include_replies,
    )
    _emit(ctx, payload)


@app.command("guide-export")
def guide_export(
    ctx: typer.Context,
    guide_ref: str = typer.Argument(
        ...,
        help="Guide id, Wowhead guide URL, or guide path.",
    ),
    out: Path | None = typer.Option(
        None,
        "--out",
        file_okay=False,
        dir_okay=True,
        writable=True,
        resolve_path=True,
        help="Directory to write exported guide assets into. Defaults to ./wowhead_exports/<guide-slug>/",
    ),
    max_links: int = typer.Option(
        250,
        "--max-links",
        min=1,
        max=2000,
        help="Maximum linked entities to return.",
    ),
    include_replies: bool = typer.Option(
        False,
        "--include-replies/--no-include-replies",
        help="Include inline replies already present in the embedded comments payload.",
    ),
    hydrate_linked_entities: bool = typer.Option(
        False,
        "--hydrate-linked-entities/--no-hydrate-linked-entities",
        help="Hydrate selected linked entities into local entity JSON files using the normalized entity contract.",
    ),
    hydrate_type: list[str] = typer.Option(
        [],
        "--hydrate-type",
        help="Restrict hydrated linked entity types. Repeat or pass comma-separated values from: achievement, battle-pet, currency, faction, item, mount, npc, object, pet, quest, recipe, spell, transmog-set, zone. Defaults to spell,item,npc when hydration is enabled.",
    ),
    hydrate_limit: int = typer.Option(
        100,
        "--hydrate-limit",
        min=1,
        max=1000,
        help="Maximum linked entities to hydrate when --hydrate-linked-entities is enabled.",
    ),
) -> None:
    client = _client(ctx)
    selected_hydrate_types: tuple[str, ...] = ()
    if hydrate_linked_entities:
        try:
            selected_hydrate_types = _normalize_hydrate_types(hydrate_type)
        except ValueError as exc:
            _fail(ctx, "invalid_argument", str(exc))
    if out is None:
        preview_payload, _ = _build_guide_full_payload(
            ctx,
            guide_ref=guide_ref,
            max_links=max_links,
            include_replies=include_replies,
            client=client,
        )
        export_dir = _default_guide_export_dir(preview_payload).expanduser()
    else:
        export_dir = out.expanduser()
    manifest = _write_guide_export_bundle(
        ctx,
        client=client,
        guide_ref=guide_ref,
        export_dir=export_dir,
        max_links=max_links,
        include_replies=include_replies,
        hydrate_linked_entities=hydrate_linked_entities,
        hydrate_types=selected_hydrate_types,
        hydrate_limit=hydrate_limit,
    )
    _emit(ctx, manifest)


@app.command("guide-query")
def guide_query(
    ctx: typer.Context,
    bundle_ref: str = typer.Argument(
        ...,
        help="Bundle directory path or selector (guide id, bundle dir name, or title match).",
    ),
    query: str = typer.Argument(..., help="Query text to search within the exported bundle."),
    limit: int = typer.Option(
        5,
        "--limit",
        min=1,
        max=50,
        help="Maximum matches to return per category and in the flattened top list.",
    ),
    kind: list[str] = typer.Option(
        [],
        "--kind",
        help="Restrict search kinds. Repeat or pass comma-separated values from: sections, analysis_surfaces, navigation, linked_entities, gatherer_entities, comments.",
    ),
    section_title: str | None = typer.Option(
        None,
        "--section-title",
        help="Restrict section searching to section titles containing this text.",
    ),
    linked_source: list[str] = typer.Option(
        [],
        "--linked-source",
        help="Restrict merged linked-entity matches by provenance. Repeat or pass comma-separated values from: href, gatherer, multi.",
    ),
    root: Path | None = typer.Option(
        None,
        "--root",
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="Root directory used to resolve non-path bundle selectors. Defaults to ./wowhead_exports/.",
    ),
) -> None:
    try:
        export_dir = _resolve_corpus_ref(bundle_ref, root=root)
        corpus = _load_guide_export(export_dir)
    except (ValueError, json.JSONDecodeError) as exc:
        _fail(ctx, "invalid_bundle", str(exc))
    try:
        selected_kinds = _normalize_query_kinds(kind)
    except ValueError as exc:
        _fail(ctx, "invalid_argument", str(exc))
    try:
        selected_link_sources = _normalize_link_source_filters(linked_source)
    except ValueError as exc:
        _fail(ctx, "invalid_argument", str(exc))

    section_title_filter = section_title.strip().lower() if isinstance(section_title, str) and section_title.strip() else None
    payload = {
        "query": query,
        **_guide_query_payload(
            export_dir=export_dir,
            corpus=corpus,
            query=query,
            selected_kinds=selected_kinds,
            section_title_filter=section_title_filter,
            selected_link_sources=selected_link_sources,
            limit=limit,
        ),
    }
    _emit(ctx, payload)


@app.command("guide-bundle-query")
def guide_bundle_query(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="Query text to search across exported bundle content."),
    root: Path | None = typer.Option(
        None,
        "--root",
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="Root directory containing exported guide bundles. Defaults to ./wowhead_exports/.",
    ),
    limit: int = typer.Option(
        5,
        "--limit",
        min=1,
        max=50,
        help="Maximum matches to return in the flattened top list and per bundle top results.",
    ),
    bundle_limit: int = typer.Option(
        5,
        "--bundle-limit",
        min=1,
        max=50,
        help="Maximum matching bundles to return.",
    ),
    kind: list[str] = typer.Option(
        [],
        "--kind",
        help="Restrict search kinds. Repeat or pass comma-separated values from: sections, analysis_surfaces, navigation, linked_entities, gatherer_entities, comments.",
    ),
    section_title: str | None = typer.Option(
        None,
        "--section-title",
        help="Restrict section searching to section titles containing this text.",
    ),
    linked_source: list[str] = typer.Option(
        [],
        "--linked-source",
        help="Restrict merged linked-entity matches by provenance. Repeat or pass comma-separated values from: href, gatherer, multi.",
    ),
    max_age_hours: int = typer.Option(
        24,
        "--max-age-hours",
        min=1,
        max=24 * 30,
        help="Freshness window in hours used for bundle freshness summaries.",
    ),
) -> None:
    resolved_root = (root or _guide_export_root()).expanduser()
    bundles = _discover_guide_corpora(resolved_root, max_age_hours=max_age_hours)
    try:
        selected_kinds = _normalize_query_kinds(kind)
    except ValueError as exc:
        _fail(ctx, "invalid_argument", str(exc))
    try:
        selected_link_sources = _normalize_link_source_filters(linked_source)
    except ValueError as exc:
        _fail(ctx, "invalid_argument", str(exc))

    section_title_filter = section_title.strip().lower() if isinstance(section_title, str) and section_title.strip() else None
    aggregate_counts = {
        "sections": 0,
        "analysis_surfaces": 0,
        "navigation": 0,
        "linked_entities": 0,
        "gatherer_entities": 0,
        "comments": 0,
    }
    matched_bundles: list[dict[str, Any]] = []
    top_matches: list[dict[str, Any]] = []

    for bundle in bundles:
        export_dir = Path(bundle["path"])
        try:
            corpus = _load_guide_export(export_dir)
        except (ValueError, json.JSONDecodeError):
            continue
        result = _guide_query_payload(
            export_dir=export_dir,
            corpus=corpus,
            query=query,
            selected_kinds=selected_kinds,
            section_title_filter=section_title_filter,
            selected_link_sources=selected_link_sources,
            limit=limit,
        )
        bundle_result = _bundle_query_result(bundle, query=query, result=result, root=resolved_root)
        if bundle_result is None:
            continue
        for key in aggregate_counts:
            aggregate_counts[key] += int(bundle_result["match_counts"].get(key) or 0)
        matched_bundles.append(bundle_result)
        top_matches.extend(_bundle_query_top_matches(bundle, result["top"]))

    matched_bundles.sort(
        key=lambda row: (
            -int(row.get("best_score") or 0),
            -int(row.get("match_count") or 0),
            (row.get("title") or "").lower(),
            row.get("path") or "",
        )
    )
    top_matches.sort(
        key=lambda row: _guide_query_match_sort_key(row)
        + ((row.get("bundle") or {}).get("title") or "", (row.get("bundle") or {}).get("path") or "")
    )

    payload = {
        "query": query,
        "root": str(resolved_root),
        "max_age_hours": max_age_hours,
        "searched_bundle_count": len(bundles),
        "count": len(matched_bundles),
        "stale_reason_counts": _bundle_freshness_rollups(bundles),
        "filters": {
            "kinds": list(selected_kinds),
            "section_title": section_title_filter,
            "linked_sources": list(selected_link_sources),
        },
        "counts": aggregate_counts,
        "bundles": matched_bundles[:bundle_limit],
        "top": top_matches[:limit],
    }
    _emit(ctx, payload)


@app.command("guide-bundle-search")
def guide_bundle_search(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="Query text to search across exported bundle metadata."),
    root: Path | None = typer.Option(
        None,
        "--root",
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="Root directory containing exported guide bundles. Defaults to ./wowhead_exports/.",
    ),
    limit: int = typer.Option(
        5,
        "--limit",
        min=1,
        max=50,
        help="Maximum matching bundles to return.",
    ),
    max_age_hours: int = typer.Option(
        24,
        "--max-age-hours",
        min=1,
        max=24 * 30,
        help="Freshness window in hours used for bundle freshness summaries.",
    ),
) -> None:
    normalized_query = " ".join(query.split())
    if not normalized_query:
        _fail(ctx, "invalid_argument", "query cannot be empty.")

    resolved_root = (root or _guide_export_root()).expanduser()
    bundles = _discover_guide_corpora(resolved_root, max_age_hours=max_age_hours)
    matches: list[dict[str, Any]] = []
    for row in bundles:
        score, reasons = _bundle_search_score_and_reasons(row, query=normalized_query)
        if score <= 0:
            continue
        match = dict(row)
        match["score"] = score
        match["match_reasons"] = reasons
        match["suggested_query_command"] = _guide_bundle_query_command(row, query=normalized_query, root=resolved_root)
        matches.append(match)
    matches.sort(key=lambda row: (-row["score"], (row.get("title") or "").lower(), row.get("path") or ""))
    payload = {
        "query": normalized_query,
        "root": str(resolved_root),
        "max_age_hours": max_age_hours,
        "count": len(matches),
        "stale_reason_counts": _bundle_freshness_rollups(bundles),
        "matches": matches[:limit],
    }
    _emit(ctx, payload)


@app.command("guide-bundle-list")
def guide_bundle_list(
    ctx: typer.Context,
    root: Path | None = typer.Option(
        None,
        "--root",
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="Root directory containing exported guide bundles. Defaults to ./wowhead_exports/.",
    ),
    max_age_hours: int = typer.Option(
        24,
        "--max-age-hours",
        min=1,
        max=24 * 30,
        help="Freshness window in hours used for the list's bundle and hydration status summaries.",
    ),
) -> None:
    resolved_root = (root or _guide_export_root()).expanduser()
    bundles = _discover_guide_corpora(resolved_root, max_age_hours=max_age_hours)
    payload = {
        "root": str(resolved_root),
        "count": len(bundles),
        "max_age_hours": max_age_hours,
        "stale_reason_counts": _bundle_freshness_rollups(bundles),
        "bundles": bundles,
    }
    _emit(ctx, payload)


@app.command("guide-bundle-inspect")
def guide_bundle_inspect(
    ctx: typer.Context,
    bundle_ref: str = typer.Argument(
        ...,
        help="Bundle directory path or selector (guide id, bundle dir name, or title match).",
    ),
    root: Path | None = typer.Option(
        None,
        "--root",
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="Root directory used to resolve non-path bundle selectors. Defaults to ./wowhead_exports/.",
    ),
    max_age_hours: int = typer.Option(
        24,
        "--max-age-hours",
        min=1,
        max=24 * 30,
        help="Freshness window in hours used for bundle and hydration freshness summaries.",
    ),
    summary: bool = typer.Option(
        False,
        "--summary",
        help="Return a compact inspection payload focused on freshness and issues.",
    ),
) -> None:
    try:
        export_dir = _resolve_corpus_ref(bundle_ref, root=root)
        corpus = _load_guide_export(export_dir)
    except (ValueError, json.JSONDecodeError) as exc:
        _fail(ctx, "invalid_bundle", str(exc))
    payload = _guide_bundle_inspection_payload(
        export_dir=export_dir,
        corpus=corpus,
        max_age_hours=max_age_hours,
    )
    _emit(ctx, _guide_bundle_inspection_summary(payload) if summary else payload)


@app.command("guide-bundle-index-rebuild")
def guide_bundle_index_rebuild(
    ctx: typer.Context,
    root: Path | None = typer.Option(
        None,
        "--root",
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="Root directory containing exported guide bundles. Defaults to ./wowhead_exports/.",
    ),
) -> None:
    resolved_root = (root or _guide_export_root()).expanduser()
    previous_rows = _load_guide_bundle_index(resolved_root)
    index_path = _guide_bundle_index_path(resolved_root)
    previous = {
        "exists": index_path.exists(),
        "valid": previous_rows is not None,
        "count": len(previous_rows or []),
    }
    _write_guide_bundle_index(resolved_root)
    current_rows = _load_guide_bundle_index(resolved_root)
    if current_rows is None:
        _fail(ctx, "index_rebuild_failed", f"Failed to rebuild bundle index under {resolved_root}.")
    payload = {
        "root": str(resolved_root),
        "count": len(current_rows),
        "index": {
            "path": str(index_path),
            "updated": True,
            "previous": previous,
            "current": {
                "exists": True,
                "valid": True,
                "count": len(current_rows),
            },
        },
    }
    _emit(ctx, payload)


@app.command("guide-bundle-refresh")
def guide_bundle_refresh(
    ctx: typer.Context,
    bundle_ref: str = typer.Argument(
        ...,
        help="Bundle directory path or selector (guide id, bundle dir name, or title match).",
    ),
    root: Path | None = typer.Option(
        None,
        "--root",
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
        help="Root directory used to resolve non-path bundle selectors. Defaults to ./wowhead_exports/.",
    ),
    max_age_hours: int = typer.Option(
        24,
        "--max-age-hours",
        min=1,
        max=24 * 30,
        help="Default freshness window in hours. If omitted, bundles newer than 24 hours are treated as fresh.",
    ),
    force: bool = typer.Option(
        False,
        "--force/--no-force",
        help="Refresh even when the bundle is still within the freshness window.",
    ),
) -> None:
    try:
        export_dir = _resolve_corpus_ref(bundle_ref, root=root)
        corpus = _load_guide_export(export_dir)
    except (ValueError, json.JSONDecodeError) as exc:
        _fail(ctx, "invalid_bundle", str(exc))

    manifest = corpus["manifest"]
    if not isinstance(manifest, dict):
        _fail(ctx, "invalid_bundle", "Bundle manifest is not a JSON object.")

    try:
        guide_ref, max_links, include_replies, hydrate_enabled, hydrate_types, hydrate_limit = _infer_guide_export_options(
            manifest
        )
    except ValueError as exc:
        _fail(ctx, "invalid_bundle", str(exc))

    is_fresh = _guide_bundle_is_fresh(manifest, max_age_hours=max_age_hours)
    if is_fresh and not force:
        refreshed_manifest = dict(manifest)
        refreshed_manifest["refresh"] = {
            "updated": False,
            "reason": "fresh",
            "max_age_hours": max_age_hours,
        }
        _emit(ctx, refreshed_manifest)
        return

    client = _client(ctx)
    refreshed_manifest = _write_guide_export_bundle(
        ctx,
        client=client,
        guide_ref=guide_ref,
        export_dir=export_dir,
        max_links=max_links,
        include_replies=include_replies,
        hydrate_linked_entities=hydrate_enabled,
        hydrate_types=hydrate_types,
        hydrate_limit=hydrate_limit,
        rehydrate_max_age_hours=max_age_hours,
        force_rehydrate=force,
    )
    refreshed_manifest["refresh"] = {
        "updated": True,
        "reason": "forced" if force else "stale",
        "max_age_hours": max_age_hours,
    }
    _emit(ctx, refreshed_manifest)


@app.command("entity")
def entity(
    ctx: typer.Context,
    entity_type: str = typer.Argument(..., help="Wowhead entity type. Example: item, quest, npc."),
    entity_id: int = typer.Argument(..., help="Wowhead entity id."),
    data_env: int | None = typer.Option(
        None,
        "--data-env",
        help="Override Wowhead tooltip dataEnv value. Defaults to selected expansion profile.",
    ),
    include_comments: bool = typer.Option(
        True,
        "--include-comments/--no-include-comments",
        help="Include page comments in entity output.",
    ),
    include_all_comments: bool = typer.Option(
        False,
        "--include-all-comments/--top-comments-only",
        help="Include all parsed comments instead of only a top-rated summary.",
    ),
    linked_entity_preview_limit: int = typer.Option(
        5,
        "--linked-entity-preview-limit",
        min=0,
        max=50,
        help="Maximum linked entities to include as a lightweight preview. Set to 0 to disable.",
    ),
) -> None:
    client = _client(ctx)
    payload = _build_entity_payload(
        ctx,
        client,
        entity_type=entity_type,
        entity_id=entity_id,
        data_env=data_env,
        include_comments=include_comments,
        include_all_comments=include_all_comments,
        linked_entity_preview_limit=linked_entity_preview_limit,
    )
    _emit(ctx, payload)


@app.command("entity-page")
def entity_page(
    ctx: typer.Context,
    entity_type: str = typer.Argument(..., help="Wowhead entity type. Example: item, quest, npc."),
    entity_id: int = typer.Argument(..., help="Wowhead entity id."),
    max_links: int = typer.Option(
        200,
        "--max-links",
        min=1,
        max=2000,
        help="Maximum linked entities to return.",
    ),
    include_gatherer: bool = typer.Option(
        True,
        "--include-gatherer/--no-include-gatherer",
        help="Include linked entities discovered from WH.Gatherer.addData payloads.",
    ),
) -> None:
    cfg = _cfg(ctx)
    client = _client(ctx)
    plan = _resolve_page_fetch_target(
        ctx,
        client,
        entity_type=entity_type,
        entity_id=entity_id,
    )
    html, metadata = _fetch_entity_page(ctx, client, plan.page_entity_type, plan.page_entity_id)

    raw_canonical = metadata["canonical_url"] or entity_url(plan.page_entity_type, plan.page_entity_id, expansion=cfg.expansion)
    canonical_url = (
        _normalize_canonical_entity_url(
            raw_canonical,
            expansion=cfg.expansion,
            entity_type=plan.page_entity_type,
            entity_id=plan.page_entity_id,
        )
        if cfg.normalize_canonical_to_expansion
        else raw_canonical
    )
    links_href = extract_linked_entities_from_href(html, source_url=canonical_url)
    links = links_href
    if include_gatherer:
        links = links + extract_gatherer_entities(html, source_url=canonical_url)

    deduped = _dedupe_links(
        links,
        entity_type=plan.page_entity_type,
        entity_id=plan.page_entity_id,
        max_links=max_links,
    )

    page_meta_json = parse_page_meta_json(html)

    payload: dict[str, Any] = {
        "expansion": cfg.expansion.key,
        "normalize_canonical_to_expansion": cfg.normalize_canonical_to_expansion,
        "entity": {
            "type": entity_type,
            "id": entity_id,
            "page_url": canonical_url,
        },
        "page": {
            "title": metadata["title"],
            "description": metadata["description"],
            "canonical_url": canonical_url,
        },
        "linked_entities": {
            "count": len(deduped),
            "items": deduped,
        },
        "citations": {
            "page": canonical_url,
            "comments": f"{canonical_url}#comments",
        },
    }
    if isinstance(page_meta_json, dict):
        payload["page_meta"] = {
            "page": page_meta_json.get("page"),
            "server_time": page_meta_json.get("serverTime"),
            "available_data_envs": page_meta_json.get("availableDataEnvs"),
            "env_domain": page_meta_json.get("envDomain"),
        }
    _emit(ctx, payload)


@app.command("comments")
def comments(
    ctx: typer.Context,
    entity_type: str = typer.Argument(..., help="Wowhead entity type. Example: item, quest, npc."),
    entity_id: int = typer.Argument(..., help="Wowhead entity id."),
    limit: int = typer.Option(
        25,
        "--limit",
        min=1,
        max=500,
        help="Maximum number of top-level comments to return.",
    ),
    sort: str = typer.Option(
        "newest",
        "--sort",
        help="Sort mode for top-level comments: newest | oldest | rating.",
    ),
    min_rating: int | None = typer.Option(
        None,
        "--min-rating",
        help="Filter out comments below this rating.",
    ),
    include_replies: bool = typer.Option(
        True,
        "--include-replies/--no-include-replies",
        help="Include reply objects for each comment.",
    ),
    hydrate_missing_replies: bool = typer.Option(
        False,
        "--hydrate-missing-replies/--no-hydrate-missing-replies",
        help="Fetch missing replies via /comment/show-replies when embedded data is incomplete.",
    ),
    max_concurrency: int = typer.Option(
        4,
        "--max-concurrency",
        min=1,
        max=16,
        help="Maximum parallel reply hydration requests when --hydrate-missing-replies is enabled.",
    ),
    linked_entity_preview_limit: int = typer.Option(
        5,
        "--linked-entity-preview-limit",
        min=0,
        max=50,
        help="Maximum linked entities to include as a lightweight preview. Set to 0 to disable.",
    ),
) -> None:
    if sort not in {"newest", "oldest", "rating"}:
        _fail(ctx, "invalid_argument", "sort must be one of: newest, oldest, rating.")

    cfg = _cfg(ctx)
    client = _client(ctx)
    plan = _resolve_page_fetch_target(
        ctx,
        client,
        entity_type=entity_type,
        entity_id=entity_id,
    )
    html, metadata = _fetch_entity_page(ctx, client, plan.page_entity_type, plan.page_entity_id)
    raw_canonical = metadata["canonical_url"] or entity_url(plan.page_entity_type, plan.page_entity_id, expansion=cfg.expansion)
    canonical_url = (
        _normalize_canonical_entity_url(
            raw_canonical,
            expansion=cfg.expansion,
            entity_type=plan.page_entity_type,
            entity_id=plan.page_entity_id,
        )
        if cfg.normalize_canonical_to_expansion
        else raw_canonical
    )

    try:
        raw_comments = extract_comments_dataset(html)
    except ValueError as exc:
        _fail(ctx, "parse_error", str(exc))

    if min_rating is not None:
        filtered: list[dict[str, Any]] = []
        for row in raw_comments:
            rating = row.get("rating")
            if isinstance(rating, int) and rating >= min_rating:
                filtered.append(row)
        raw_comments = filtered

    raw_comments = sort_comments(raw_comments, sort)
    selected = raw_comments[:limit]

    hydrated_count = 0
    if hydrate_missing_replies and include_replies:
        hydrated_count = _hydrate_missing_comment_replies(
            client,
            selected,
            max_concurrency=max_concurrency,
        )

    normalized = normalize_comments(
        selected,
        page_url=canonical_url,
        include_replies=include_replies,
    )

    payload = {
        "expansion": cfg.expansion.key,
        "normalize_canonical_to_expansion": cfg.normalize_canonical_to_expansion,
        "entity": {
            "type": entity_type,
            "id": entity_id,
            "page_url": canonical_url,
        },
        "query": {
            "limit": limit,
            "sort": sort,
            "min_rating": min_rating,
            "include_replies": include_replies,
            "hydrate_missing_replies": hydrate_missing_replies,
            "max_concurrency": max_concurrency,
        },
        "counts": {
            "embedded_comments": len(raw_comments),
            "returned_comments": len(normalized),
            "hydrated_reply_threads": hydrated_count,
        },
        "comments": normalized,
        "citations": {
            "page": canonical_url,
            "comments": f"{canonical_url}#comments",
        },
    }
    if linked_entity_preview_limit > 0:
        payload["linked_entities"] = _build_linked_entity_preview(
            extract_linked_entities_from_href(html, source_url=canonical_url)
            + extract_gatherer_entities(html, source_url=canonical_url),
            entity_type=plan.page_entity_type,
            entity_id=plan.page_entity_id,
            preview_limit=linked_entity_preview_limit,
            fetch_more_command_builder=lambda count: _entity_page_fetch_more_command(entity_type, entity_id, count),
        )
    _emit(ctx, payload)


@app.command("compare")
def compare(
    ctx: typer.Context,
    entities: list[str] = typer.Argument(
        ...,
        help="Entity references in <type>:<id> form. Example: item:19019 item:19351",
    ),
    preset: str | None = typer.Option(
        None,
        "--preset",
        help="Comparison preset: gear, quest, or spell (tunes field diffs, link limits, and comment sampling).",
    ),
    max_links_per_entity: int | None = typer.Option(
        None,
        "--max-links-per-entity",
        min=1,
        max=2000,
        help="Maximum linked entities to parse per entity.",
    ),
    max_shared_links: int | None = typer.Option(
        None,
        "--max-shared-links",
        min=1,
        max=2000,
        help="Maximum shared linked entities to include in output.",
    ),
    max_unique_links: int | None = typer.Option(
        None,
        "--max-unique-links",
        min=1,
        max=5000,
        help="Maximum unique linked entities to include per compared entity.",
    ),
    comment_sample: int | None = typer.Option(
        None,
        "--comment-sample",
        min=0,
        max=20,
        help="Top comments to include per entity (sorted by rating).",
    ),
    comment_chars: int | None = typer.Option(
        None,
        "--comment-chars",
        min=60,
        max=2000,
        help="Maximum characters for each sampled comment body.",
    ),
    include_gatherer: bool | None = typer.Option(
        None,
        "--include-gatherer/--no-include-gatherer",
        help="Include linked entities from WH.Gatherer.addData payloads.",
    ),
) -> None:
    if len(entities) < 2:
        _fail(ctx, "invalid_argument", "compare requires at least two entity references.")

    try:
        options = resolve_compare_options(
            preset=preset,
            max_links_per_entity=max_links_per_entity,
            max_shared_links=max_shared_links,
            max_unique_links=max_unique_links,
            comment_sample=comment_sample,
            comment_chars=comment_chars,
            include_gatherer=include_gatherer,
        )
    except ValueError as exc:
        _fail(ctx, "invalid_argument", str(exc))
        return

    parsed_refs: list[tuple[str, int, str]] = []
    for token in entities:
        try:
            entity_type, entity_id = _parse_entity_ref_token(token)
        except ValueError as exc:
            _fail(ctx, "invalid_argument", str(exc))
        parsed_refs.append((entity_type, entity_id, token))

    cfg = _cfg(ctx)
    client = _client(ctx)
    entity_records: list[dict[str, Any]] = []
    entity_link_sets: dict[str, set[tuple[str, int]]] = {}

    for entity_type, entity_id, token in parsed_refs:
        try:
            tooltip = client.tooltip(entity_type, entity_id)
        except httpx.HTTPStatusError as exc:
            _fail(ctx, "http_error", f"Wowhead returned HTTP {exc.response.status_code} for {token}")
        except httpx.HTTPError as exc:
            _fail(ctx, "network_error", f"{token}: {exc}")
        except ValueError as exc:
            _fail(ctx, "parse_error", f"{token}: {exc}")

        html, metadata = _fetch_entity_page(ctx, client, entity_type, entity_id)
        raw_canonical = metadata["canonical_url"] or entity_url(entity_type, entity_id, expansion=cfg.expansion)
        canonical_url = (
            _normalize_canonical_entity_url(
                raw_canonical,
                expansion=cfg.expansion,
                entity_type=entity_type,
                entity_id=entity_id,
            )
            if cfg.normalize_canonical_to_expansion
            else raw_canonical
        )

        links = extract_linked_entities_from_href(html, source_url=canonical_url)
        if options.include_gatherer:
            links = links + extract_gatherer_entities(html, source_url=canonical_url)
        deduped_links = _dedupe_links(
            links,
            entity_type=entity_type,
            entity_id=entity_id,
            max_links=options.max_links_per_entity,
        )

        raw_comments: list[dict[str, Any]] = []
        try:
            raw_comments = extract_comments_dataset(html)
        except ValueError:
            raw_comments = []
        sampled_comments: list[dict[str, Any]] = []
        if options.comment_sample > 0 and raw_comments:
            ranked = sort_comments(raw_comments, "rating")
            sampled_norm = normalize_comments(
                ranked[:options.comment_sample],
                page_url=canonical_url,
                include_replies=False,
            )
            for row in sampled_norm:
                sampled_comments.append(
                    {
                        "id": row.get("id"),
                        "user": row.get("user"),
                        "rating": row.get("rating"),
                        "date": row.get("date"),
                        "body": _truncate_text(row.get("body"), max_chars=options.comment_chars),
                        "citation_url": row.get("citation_url"),
                    }
                )

        ref = f"{entity_type}:{entity_id}"
        record, link_set = _comparison_entity_record(
            ref=ref,
            entity_type=entity_type,
            entity_id=entity_id,
            canonical_url=canonical_url,
            tooltip=tooltip,
            metadata=metadata,
            deduped_links=deduped_links,
            raw_comments=raw_comments,
            sampled_comments=sampled_comments,
        )
        entity_link_sets[ref] = link_set
        entity_records.append(record)

    refs_in_order = [row["ref"] for row in entity_records]

    payload = {
        "expansion": cfg.expansion.key,
        "normalize_canonical_to_expansion": cfg.normalize_canonical_to_expansion,
        "inputs": refs_in_order,
        "preset": {
            "key": options.preset.key,
            "label": options.preset.label,
            "comparable_fields": list(options.comparable_fields),
        },
        "comparison": {
            "fields": _comparison_field_diffs(
                entity_records,
                comparable_fields=list(options.comparable_fields),
            ),
            "linked_entities": _comparison_linked_entities_summary(
                refs_in_order=refs_in_order,
                entity_link_sets=entity_link_sets,
                expansion=cfg.expansion,
                max_shared_links=options.max_shared_links,
                max_unique_links=options.max_unique_links,
            ),
        },
        "entities": entity_records,
    }
    _emit(ctx, payload)


@app.command("linked-graph")
def linked_graph(
    ctx: typer.Context,
    entity_type: str = typer.Argument(..., help="Root Wowhead entity type. Example: item, quest, npc."),
    entity_id: int = typer.Argument(..., help="Root Wowhead entity id."),
    depth: int = typer.Option(1, "--depth", min=1, max=2, help="Traversal depth (1 = direct links, 2 = one additional hop)."),
    relation: list[str] | None = typer.Option(
        None,
        "--relation",
        help="Retain only edges whose target entity type matches. Repeatable or comma-separated.",
    ),
    limit: int = typer.Option(50, "--limit", min=1, max=500, help="Maximum graph nodes to retain."),
    max_fetches: int = typer.Option(10, "--max-fetches", min=1, max=50, help="Maximum entity pages to fetch while traversing."),
    include_gatherer: bool = typer.Option(
        True,
        "--include-gatherer/--no-include-gatherer",
        help="Include gatherer-linked entities when parsing pages.",
    ),
) -> None:
    cfg = _cfg(ctx)
    client = _client(ctx)
    root_url = entity_url(entity_type, entity_id, expansion=cfg.expansion)

    def fetch_page(page_type: str, page_id: int) -> tuple[str, dict[str, str | None]]:
        return _fetch_entity_page(ctx, client, page_type, page_id)

    try:
        payload = build_linked_graph_payload(
            root_type=entity_type,
            root_id=entity_id,
            root_url=root_url,
            fetch_page=fetch_page,
            depth=depth,
            relation_filter=normalize_relation_option(relation),
            node_limit=limit,
            max_fetches=max_fetches,
            include_gatherer=include_gatherer,
        )
    except ValueError as exc:
        _fail(ctx, "invalid_argument", str(exc))
        return

    _emit(
        ctx,
        {
            "provider": "wowhead",
            "kind": "linked_graph",
            "expansion": cfg.expansion.key,
            **payload,
        },
    )


def run() -> None:
    app()


if __name__ == "__main__":
    run()

from __future__ import annotations

import json
import math
import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, NoReturn
from urllib.parse import urljoin, urlparse

import httpx
import typer
from warcraft_api.cache import (
    CacheSettings,
    clear_file_cache,
    clear_redis_cache,
    inspect_file_cache,
    inspect_redis_cache,
    load_cache_settings_from_env,
    repair_file_cache,
)
from warcraft_content.guide_analysis import extract_section_chunk_analysis_surfaces
from warcraft_core.cli import (
    CompactMaxCharsOption,
    CompactOption,
    FieldsOption,
    FieldsStrictOption,
    PrettyOption,
    ProfileOption,
    RuntimeConfig,
    cfg_as,
    configure,
    emit,
    fail,
    guarded_run,
)
from warcraft_core.envelope import ENVELOPE_KEYS, SCHEMA_VERSION, Envelope
from warcraft_core.identity import build_identity_payload, build_reference_transport_packet_payload, validate_talent_transport_packet
from warcraft_core.output import DEFAULT_COMPACT_MAX_CHARS, OutputProjectionError, shape_payload, to_json
from warcraft_core.output import emit as emit_json
from warcraft_core.provider import ProviderError

from wowhead_cli import provider
from wowhead_cli.citation_pack import citation_pack_from_compare, citation_pack_from_entity
from wowhead_cli.comments_intelligence import build_comments_intelligence, filter_raw_comments
from wowhead_cli.compare_presets import ResolvedCompareOptions, resolve_compare_options
from wowhead_cli.entities import (
    build_linked_entity_preview,
    comparison_entity_record,
    comparison_field_diffs,
    comparison_linked_entities_summary,
    dedupe_links,
    entity_comments_payload,
    entity_linked_entities_payload,
    entity_page_fetch_more_command,
    entity_page_needs_fetch,
    restore_cached_normalization_version,
    truncate_text,
    truncated_link_block,
)
from wowhead_cli.entity_types import (
    DEFAULT_HYDRATE_ENTITY_TYPES,
    HYDRATABLE_ENTITY_TYPES,
)
from wowhead_cli.expansion_profiles import (
    ExpansionProfile,
    detect_expansion_from_url,
    expansion_url_policy_issues,
    list_profiles,
    parse_entity_from_wowhead_url,
    resolve_expansion,
)
from wowhead_cli.guides import (
    GuideExportOptions,
    GuideHydrationResult,
    default_guide_export_dir,
    filtered_guide_category_rows,
    guide_export_manifest,
    guide_export_root,
    guide_query_match_sort_key,
    guide_query_payload,
    guides_payload,
    hydrate_source_counts,
    infer_guide_export_options,
    iso_now_utc,
    read_json_file,
    read_jsonl_file,
    validated_guides_filters,
    write_guide_export_assets,
    write_json_file,
)
from wowhead_cli.linked_graph import build_linked_graph_payload, normalize_relation_option
from wowhead_cli.listing_filters import (
    absolute_wowhead_url,
    clean_htmlish_text,
    collect_timeline_facets,
    limited_result_block,
    normalize_text_filters,
    parse_date_bound,
    parse_iso8601_utc,
    parse_listing_timestamp,
    text_filter_match,
)
from wowhead_cli.normalization import attach_entity_normalization, attach_entity_page_normalization
from wowhead_cli.page_parser import (
    clean_markup_text,
    extract_comments_dataset,
    extract_gatherer_entities,
    extract_guide_rating,
    extract_guide_section_chunks,
    extract_guide_sections,
    extract_json_ld,
    extract_json_script,
    extract_linked_entities_from_href,
    extract_listview_data,
    extract_markup_by_target,
    extract_markup_urls,
    normalize_comments,
    parse_page_meta_json,
    parse_page_metadata,
    sort_comments,
)
from wowhead_cli.provider import cache_settings_payload
from wowhead_cli.ranking import (
    command_prefix_for_expansion,
    score_text_match,
)
from wowhead_cli.wowhead_client import (
    WOWHEAD_BASE_URL,
    WowheadClient,
    blue_tracker_url,
    entity_url,
    guide_url,
    news_url,
    tool_url,
)

PROVIDER_NAME = "wowhead"

app = typer.Typer(
    add_completion=False,
    help="Agent-first CLI for querying Wowhead without browser automation.",
)

EXPANSION_PREFIXES = frozenset(
    profile.path_prefix for profile in list_profiles() if profile.path_prefix
)


@dataclass(slots=True)
class WowheadConfig(RuntimeConfig):
    """Shared runtime config plus the Wowhead-only global flags (expansion routing, stream, citation pack)."""

    stream: bool = False
    expansion: ExpansionProfile = field(default_factory=lambda: resolve_expansion(None))
    expansion_explicit: bool = False
    expansion_source: str = "default"
    normalize_canonical_to_expansion: bool = False
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


def _cfg(ctx: typer.Context) -> WowheadConfig:
    """Narrow the shared runtime config to Wowhead's subclass; the app callback always installs it."""
    return cfg_as(ctx, WowheadConfig)


def _client(ctx: typer.Context) -> WowheadClient:
    cfg = _cfg(ctx)
    try:
        return WowheadClient(expansion=cfg.expansion)
    except ValueError as exc:
        fail(ctx, "invalid_cache_config", str(exc))


def _validated_transport_packet(ctx: typer.Context, packet: Any, *, command_name: str) -> dict[str, Any]:
    try:
        return validate_talent_transport_packet(packet)
    except ValueError as exc:
        fail(ctx, "invalid_transport_packet", f"{command_name} produced an invalid talent transport packet: {exc}")


def _load_cache_settings_or_fail(ctx: typer.Context) -> CacheSettings:
    try:
        return load_cache_settings_from_env()
    except ValueError as exc:
        fail(ctx, "invalid_cache_config", str(exc))


def _fail_http_status(ctx: typer.Context, exc: httpx.HTTPStatusError, *, context: str | None = None) -> NoReturn:
    """Map an upstream HTTP status onto the shared error-code vocabulary (404 -> not found, 401/403 -> auth)."""
    status = exc.response.status_code
    code = {401: "auth_failed", 403: "auth_failed", 404: "not_found"}.get(status, "http_error")
    suffix = f" for {context}" if context else ""
    fail(ctx, code, f"Wowhead returned HTTP {status}{suffix}", details={"status_code": status})


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


def _apply_url_expansion(ctx: typer.Context, url_hint: str | None) -> WowheadConfig:
    cfg = _cfg(ctx)
    if cfg.expansion_explicit or not url_hint:
        return cfg
    detected = detect_expansion_from_url(url_hint)
    if detected is None or detected.key == cfg.expansion.key:
        return cfg
    updated = WowheadConfig(
        provider=cfg.provider,
        output=cfg.output,
        stream=cfg.stream,
        expansion=detected,
        expansion_explicit=cfg.expansion_explicit,
        expansion_source="url",
        normalize_canonical_to_expansion=cfg.normalize_canonical_to_expansion,
        citation_pack=cfg.citation_pack,
    )
    ctx.obj = updated
    return updated


def _expansion_policy_notes(cfg: WowheadConfig, *urls: str | None) -> list[str]:
    notes: list[str] = []
    for url in urls:
        notes.extend(expansion_url_policy_issues(url, profile=cfg.expansion))
    return notes


def _parse_entity_ref_token(token: str) -> tuple[str, int]:
    entity = parse_entity_from_wowhead_url(token)
    if entity is not None:
        return entity
    if ":" not in token:
        raise ValueError(f"Invalid entity reference {token!r}. Expected <type>:<id> or a Wowhead entity URL.")
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
    lookup_url = f"{WOWHEAD_BASE_URL}/{normalized}" if root_segment in EXPANSION_PREFIXES else f"{expansion.wowhead_base}/{normalized}"
    guide_id = _extract_guide_id_from_path(f"/{normalized}")
    return lookup_url, guide_id


def _stream_rows(payload: dict[str, Any]) -> tuple[str, list[Any]] | None:
    for key in ("results", "comments"):
        rows = payload.get(key)
        if isinstance(rows, list):
            return key, rows
    linked = payload.get("linked_entities")
    if isinstance(linked, dict):
        items = linked.get("items")
        if isinstance(items, list):
            return "linked_entities.items", items
    return None


def _emit_jsonl(ctx: typer.Context, payload: dict[str, Any], *, err: bool = False) -> None:
    """Write an already-shaped envelope as JSONL: a header line, then one ``{"record": ...}`` line per row.

    The header is the envelope with the streamed collection emptied and ``data.stream`` naming it.
    """
    data = payload.get("data")
    spec = _stream_rows(data) if isinstance(data, dict) else None
    if not isinstance(data, dict) or spec is None:
        emit_json(payload, pretty=_cfg(ctx).output.pretty, err=err)
        return

    field, rows = spec
    header_data = _without_stream_rows(dict(data), field)
    header_data["stream"] = {"field": field, "count": len(rows)}
    header = {**payload, "data": header_data}
    typer.echo(to_json(header, pretty=False), err=err)
    for row in rows:
        typer.echo(to_json({"record": row}, pretty=False), err=err)


def _without_stream_rows(mapping: dict[str, Any], field: str) -> dict[str, Any]:
    """Empty the streamed collection in ``mapping`` (rows are emitted as separate JSONL records)."""
    if field == "linked_entities.items":
        linked = dict(mapping.get("linked_entities") or {})
        linked["items"] = []
        mapping["linked_entities"] = linked
    elif field in mapping:
        mapping[field] = []
    return mapping


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


def _with_envelope_keys(ctx: typer.Context, payload: dict[str, Any]) -> dict[str, Any]:
    """Wrap a command payload in the shared envelope (docs/foundation/ERROR_CONTRACT.md).

    Command bodies build flat payloads: their envelope-named keys (``query``, ``kind``) fill the
    envelope and everything else moves under ``data``. A provider-surface envelope passes through.
    ``schema_version`` is always the envelope's: cached entity responses carry the normalization
    version under that name.
    """
    command = ctx.command.name or ""
    defaults: dict[str, Any] = {
        "ok": True,
        "provider": PROVIDER_NAME,
        "command": command,
        "kind": command.replace("-", "_"),
        "query": None,
        "provenance": {},
        "data": {key: value for key, value in payload.items() if key not in ENVELOPE_KEYS},
    }
    envelope_keys = {key: value for key, value in payload.items() if key in ENVELOPE_KEYS}
    return {**defaults, **envelope_keys, "schema_version": SCHEMA_VERSION}


def _emit(ctx: typer.Context, payload: dict[str, Any], *, err: bool = False) -> None:
    cfg = _cfg(ctx)
    payload = _attach_citation_pack(payload, enabled=cfg.citation_pack)
    payload = _with_envelope_keys(ctx, payload)
    if not cfg.stream:
        emit(ctx, payload, err=err)
        return
    try:
        rendered = shape_payload(payload, cfg.output)
    except OutputProjectionError as exc:
        fail(ctx, "missing_fields", str(exc), details={"missing_fields": list(exc.missing_fields)})
    _emit_jsonl(ctx, rendered, err=err)


def _emit_surface(ctx: typer.Context, build: Callable[[], Envelope]) -> None:
    """Run a pure provider call and turn ``ProviderError`` into the shared error envelope."""
    try:
        payload = build()
    except ProviderError as exc:
        fail(ctx, exc.code, exc.message, exit_code=exc.exit_code, details=exc.details)
    _emit(ctx, dict(payload))


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
    remainder = text[len(entity_name):].lstrip(" :-")
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


def _tooltip_block_for_normalization(payload: dict[str, Any]) -> dict[str, Any] | None:
    tooltip = payload.get("tooltip")
    if not isinstance(tooltip, dict):
        return None
    merged = dict(tooltip)
    entity = payload.get("entity")
    if isinstance(entity, dict):
        name = entity.get("name")
        if isinstance(name, str) and name.strip():
            merged.setdefault("name", name.strip())
    return merged


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


def _fetch_entity_page(
    ctx: typer.Context,
    client: WowheadClient,
    entity_type: str,
    entity_id: int,
) -> tuple[str, dict[str, str | None]]:
    try:
        html = client.entity_page_html(entity_type, entity_id)
    except httpx.HTTPStatusError as exc:
        _fail_http_status(ctx, exc)
    except httpx.HTTPError as exc:
        fail(ctx, "network_error", str(exc))
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
        fail(ctx, "unsupported_entity_type", f"{entity_type!r} does not define a tooltip route for page resolution.")
    try:
        _, final_url = client.tooltip_with_metadata(
            plan.tooltip_entity_type,
            plan.tooltip_entity_id,
            data_env=data_env,
        )
    except httpx.HTTPStatusError as exc:
        _fail_http_status(ctx, exc)
    except httpx.HTTPError as exc:
        fail(ctx, "network_error", str(exc))
    except ValueError as exc:
        fail(ctx, "parse_error", str(exc))

    resolved = _parse_tooltip_final_ref(final_url)
    if resolved is None:
        fail(ctx, "unexpected_response", f"Could not resolve a page target for {entity_type} {entity_id}.")
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


def _cached_entity_payload(
    client: WowheadClient,
    *,
    entity_type: str,
    entity_id: int,
    data_env: int | None,
    include_comments: bool,
    include_all_comments: bool,
    linked_entity_preview_limit: int,
) -> dict[str, Any] | None:
    """Return the cached entity payload, backfilling normalization for entries cached before it existed."""
    cached = client.get_cached_entity_response(
        requested_type=entity_type,
        requested_id=entity_id,
        data_env=data_env,
        include_comments=include_comments,
        include_all_comments=include_all_comments,
        linked_entity_preview_limit=linked_entity_preview_limit,
    )
    if not isinstance(cached, dict):
        return None
    if "normalized" in cached:
        return restore_cached_normalization_version(cached)
    return attach_entity_normalization(
        cached,
        entity_type=entity_type,
        tooltip=_tooltip_block_for_normalization(cached),
    )


def _entity_tooltip(
    ctx: typer.Context, client: WowheadClient, plan: EntityAccessPlan, *, data_env: int | None
) -> tuple[dict[str, Any], str | None]:
    """Fetch the tooltip the access plan points at, mapping transport failures onto the error envelope."""
    if plan.tooltip_entity_type is None or plan.tooltip_entity_id is None:
        return {}, None
    try:
        if plan.page_from_tooltip_redirect:
            return client.tooltip_with_metadata(plan.tooltip_entity_type, plan.tooltip_entity_id, data_env=data_env)
        return client.tooltip(plan.tooltip_entity_type, plan.tooltip_entity_id, data_env=data_env), None
    except httpx.HTTPStatusError as exc:
        _fail_http_status(ctx, exc)
    except httpx.HTTPError as exc:
        fail(ctx, "network_error", str(exc))
    except ValueError as exc:
        fail(ctx, "parse_error", str(exc))


def _entity_payload_blocks(
    payload: dict[str, Any],
    *,
    policy_notes: list[str],
    tooltip_payload: dict[str, Any],
    comments_citations: dict[str, Any] | None,
    linked_entities_payload: dict[str, Any] | None,
    comments_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    """Attach the optional entity blocks, omitting the ones this request did not produce."""
    optional = {
        "notes": policy_notes or None,
        "tooltip": tooltip_payload or None,
        "citations": comments_citations,
        "linked_entities": linked_entities_payload,
        "comments": comments_payload,
    }
    payload.update({key: value for key, value in optional.items() if value is not None})
    return payload


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
    cached_payload = _cached_entity_payload(
        client,
        entity_type=entity_type,
        entity_id=entity_id,
        data_env=data_env,
        include_comments=include_comments,
        include_all_comments=include_all_comments,
        linked_entity_preview_limit=linked_entity_preview_limit,
    )
    if cached_payload is not None:
        return cached_payload

    plan = _build_entity_access_plan(entity_type, entity_id)
    tooltip, tooltip_final_url = _entity_tooltip(ctx, client, plan, data_env=data_env)

    if plan.page_from_tooltip_redirect and tooltip_final_url is not None:
        resolved = _parse_tooltip_final_ref(tooltip_final_url)
        if resolved is None:
            fail(ctx, "unexpected_response", f"Could not resolve a page target for {entity_type} {entity_id}.")
        plan.page_entity_type, plan.page_entity_id = resolved

    canonical = entity_url(plan.page_entity_type, plan.page_entity_id, expansion=cfg.expansion)
    page_url = canonical
    entity_name, tooltip_payload = _normalize_tooltip_payload(tooltip)
    html: str | None = None
    metadata: dict[str, str | None] | None = None

    if entity_page_needs_fetch(
        include_comments=include_comments,
        linked_entity_preview_limit=linked_entity_preview_limit,
        tooltip_from_page_metadata=plan.tooltip_from_page_metadata,
    ):
        html, metadata = _fetch_entity_page(ctx, client, plan.page_entity_type, plan.page_entity_id)
        page_url = metadata["canonical_url"] or canonical

    if plan.tooltip_from_page_metadata and metadata is not None:
        entity_name, tooltip_payload = _build_tooltip_from_page_metadata(metadata)
    comments_payload, comments_citations = entity_comments_payload(
        html=html,
        page_url=page_url,
        include_comments=include_comments,
        include_all_comments=include_all_comments,
        top_comment_limit=top_comment_limit,
        top_comment_chars=top_comment_chars,
    )

    payload = _entity_payload_blocks(
        {
            "expansion": cfg.expansion.key,
            "expansion_source": cfg.expansion_source,
            "entity": {
                "type": entity_type,
                "id": entity_id,
                "name": entity_name,
                "page_url": page_url,
            },
        },
        policy_notes=_expansion_policy_notes(cfg, page_url, canonical),
        tooltip_payload=tooltip_payload,
        comments_citations=comments_citations,
        linked_entities_payload=entity_linked_entities_payload(
            html=html,
            page_url=page_url,
            page_entity_type=plan.page_entity_type,
            page_entity_id=plan.page_entity_id,
            requested_entity_type=entity_type,
            requested_entity_id=entity_id,
            linked_entity_preview_limit=linked_entity_preview_limit,
            expansion=cfg.expansion,
        ),
        comments_payload=comments_payload,
    )

    payload = attach_entity_normalization(payload, entity_type=entity_type, tooltip=tooltip, page=metadata)
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
    cached_payload = _cached_entity_payload(
        client,
        entity_type=entity_type,
        entity_id=entity_id,
        data_env=data_env,
        include_comments=include_comments,
        include_all_comments=include_all_comments,
        linked_entity_preview_limit=linked_entity_preview_limit,
    )
    if cached_payload is not None:
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


def _page_meta_block(page_meta_json: Any) -> dict[str, Any] | None:
    """Wowhead's embedded page meta, renamed to the snake_case keys the payloads expose."""
    if not isinstance(page_meta_json, dict):
        return None
    return {
        "page": page_meta_json.get("page"),
        "server_time": page_meta_json.get("serverTime"),
        "available_data_envs": page_meta_json.get("availableDataEnvs"),
        "env_domain": page_meta_json.get("envDomain"),
    }


def _fetch_guide_page(
    ctx: typer.Context,
    client: WowheadClient,
    *,
    guide_ref: str,
) -> tuple[str, int | None, str, dict[str, str | None], str]:
    try:
        lookup_url, guide_id = _resolve_guide_lookup_input(guide_ref, expansion=client.expansion)
    except ValueError as exc:
        fail(ctx, "invalid_argument", str(exc))

    try:
        default_lookup = guide_url(guide_id, expansion=client.expansion) if guide_id is not None else None
        html = client.guide_page_html(guide_id) if guide_id is not None and lookup_url == default_lookup else client.page_html(lookup_url)
    except httpx.HTTPStatusError as exc:
        _fail_http_status(ctx, exc)
    except httpx.HTTPError as exc:
        fail(ctx, "network_error", str(exc))

    metadata = parse_page_metadata(html, fallback_url=lookup_url)
    canonical_url = metadata["canonical_url"] or lookup_url
    return html, guide_id, lookup_url, metadata, canonical_url


def _collect_guide_linked_entities(
    *,
    html: str,
    canonical_url: str,
    guide_id: int | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Return the guide's deduped href links, its raw gatherer records, and the merge of both.

    Nothing is cut short here: gatherer records enrich (or add to) the href links no matter how
    many links the page carries, and callers truncate the merged list afterwards so the payload can
    report what the limit dropped.
    """
    guide_entity_id = guide_id or 0
    gatherer_entities = extract_gatherer_entities(html, source_url=canonical_url)
    href_entities = dedupe_links(
        extract_linked_entities_from_href(html, source_url=canonical_url),
        entity_type="guide",
        entity_id=guide_entity_id,
    )
    merged_entities = dedupe_links(
        href_entities + gatherer_entities,
        entity_type="guide",
        entity_id=guide_entity_id,
    )
    return href_entities, gatherer_entities, merged_entities


def _guide_json_script_value[T](html: str, key: str, expected: type[T]) -> T | None:
    """Read one embedded ``data.guide...`` JSON value, tolerating missing or malformed scripts."""
    try:
        parsed = extract_json_script(html, key)
    except (ValueError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, expected) else None


def _guide_author_block(html: str) -> dict[str, Any]:
    return {
        "name": _guide_json_script_value(html, "data.guide.author", str),
        "profiles": _guide_json_script_value(html, "data.guide.author.profiles", dict) or {},
        "about": _guide_json_script_value(html, "data.guide.aboutTheAuthor.embedData", dict) or {},
    }


def _guide_analysis_surfaces(
    *,
    section_chunks: list[dict[str, Any]],
    canonical_url: str,
    page_title: str | None,
) -> list[dict[str, Any]]:
    return extract_section_chunk_analysis_surfaces(
        provider="wowhead",
        page_url=canonical_url,
        page_title=page_title,
        section_chunks=section_chunks,
    )


def _guide_body_block(guide_body_markup: str | None) -> dict[str, Any]:
    """Guide body markup plus its parsed sections, chunks, and leading summary text."""
    if not isinstance(guide_body_markup, str):
        return {"raw_markup": guide_body_markup, "sections": [], "section_chunks": [], "summary": None}
    return {
        "raw_markup": guide_body_markup,
        "sections": extract_guide_sections(guide_body_markup),
        "section_chunks": extract_guide_section_chunks(guide_body_markup),
        "summary": clean_markup_text(guide_body_markup[:2000]),
    }


def _guide_navigation_block(guide_nav_markup: str | None, *, canonical_url: str) -> dict[str, Any]:
    return {
        "raw_markup": guide_nav_markup,
        "links": extract_markup_urls(guide_nav_markup, source_url=canonical_url)
        if isinstance(guide_nav_markup, str)
        else [],
    }


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

    href_entities, gatherer_entities, merged_entities = _collect_guide_linked_entities(
        html=html,
        canonical_url=canonical_url,
        guide_id=guide_id,
    )
    linked_entities_block = truncated_link_block(merged_entities, max_links=max_links)

    body = _guide_body_block(extract_markup_by_target(html, target="guide-body"))
    navigation = _guide_navigation_block(
        extract_markup_by_target(html, target="interior-sidebar-related-markup"),
        canonical_url=canonical_url,
    )
    analysis_surfaces = _guide_analysis_surfaces(
        section_chunks=body["section_chunks"],
        canonical_url=canonical_url,
        page_title=metadata["title"],
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
        "author": _guide_author_block(html),
        "rating": extract_guide_rating(html),
        "body": body,
        "navigation": navigation,
        "linked_entities": {
            **linked_entities_block,
            "source_counts": {
                "href": len(href_entities),
                "gatherer": len(gatherer_entities),
                "merged": linked_entities_block["total"],
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
        "structured_data": extract_json_ld(html),
        "citations": {
            "page": canonical_url,
            "comments": f"{canonical_url}#comments",
        },
    }
    page_meta = _page_meta_block(parse_page_meta_json(html))
    if page_meta is not None:
        payload["page_meta"] = page_meta
    return payload, html


def _load_guide_export(export_dir: Path) -> dict[str, Any]:
    manifest_path = export_dir / "manifest.json"
    if not manifest_path.exists():
        raise ValueError(f"Missing manifest file at {manifest_path}.")
    manifest = read_json_file(manifest_path)
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
        return read_jsonl_file(path)

    return {
        "manifest": manifest,
        "sections": load_jsonl_from_manifest("sections_jsonl"),
        "navigation_links": load_jsonl_from_manifest("navigation_links_jsonl"),
        "linked_entities": load_jsonl_from_manifest("linked_entities_jsonl"),
        "gatherer_entities": load_jsonl_from_manifest("gatherer_entities_jsonl"),
        "comments": load_jsonl_from_manifest("comments_jsonl"),
        "analysis_surfaces": load_jsonl_from_manifest("analysis_surfaces_jsonl"),
    }


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
    score = score_text_match(normalized_query, *values)
    return score > 0, score


@dataclass(slots=True)
class _TimelineScanState:
    """Rows a listing scan kept, plus how many of the scanned timestamps it could read.

    A row whose timestamp cannot be parsed is excluded from a date window, so the counts travel
    into the payload's ``scan`` block: a silently empty ``--date-from`` result is exactly the
    failure mode Wowhead's rendered timestamps caused before.
    """

    results: list[dict[str, Any]] = field(default_factory=list)
    parsed_timestamps: int = 0
    unparsed_timestamps: int = 0


def _scan_timeline_page(
    rows: list[dict[str, Any]],
    *,
    state: _TimelineScanState,
    normalize_row: Callable[[dict[str, Any]], dict[str, Any] | None],
    query: str | None,
    date_from: datetime | None,
    date_to: datetime | None,
) -> bool:
    """Filter one listing page into ``state``; report whether it ran past the ``--date-from`` bound."""
    reached_older_than_window = False
    for raw_row in rows:
        normalized_row = normalize_row(raw_row)
        if normalized_row is None:
            continue
        posted_at = parse_iso8601_utc(normalized_row.get("posted_at"))
        if posted_at is None:
            state.unparsed_timestamps += 1
        else:
            state.parsed_timestamps += 1
            if date_from is not None and posted_at < date_from:
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
        state.results.append(normalized_row)
    return reached_older_than_window


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
        fail(ctx, "invalid_argument", "--page must be >= 1.")
    if pages <= 0:
        fail(ctx, "invalid_argument", "--pages must be >= 1.")

    state = _TimelineScanState()
    total_pages: int | None = None
    pages_scanned = 0
    stop_reason: str | None = None

    for current_page in range(page, page + pages):
        try:
            html = fetch_page(current_page)
        except httpx.HTTPStatusError as exc:
            _fail_http_status(ctx, exc)
        except httpx.HTTPError as exc:
            fail(ctx, "network_error", str(exc))

        try:
            rows, extracted_total_pages = extract_page(html)
        except (ValueError, json.JSONDecodeError) as exc:
            fail(ctx, "parse_error", str(exc))

        if total_pages is None:
            total_pages = extracted_total_pages
        pages_scanned += 1
        if not rows:
            stop_reason = "empty_page"
            break

        reached_older_than_window = _scan_timeline_page(
            rows,
            state=state,
            normalize_row=normalize_row,
            query=query,
            date_from=date_from,
            date_to=date_to,
        )
        if reached_older_than_window:
            stop_reason = "date_from_reached"
            break
        if total_pages is not None and current_page >= total_pages:
            stop_reason = "last_page_reached"
            break

    if (date_from is not None or date_to is not None) and state.parsed_timestamps == 0 and state.unparsed_timestamps:
        fail(
            ctx,
            "parse_error",
            f"None of the {state.unparsed_timestamps} scanned Wowhead rows carried a timestamp this "
            "CLI can read, so --date-from/--date-to cannot be applied.",
        )

    return {
        "results": state.results,
        "pages_scanned": pages_scanned,
        "total_pages": total_pages,
        "stop_reason": stop_reason,
        "unparsed_timestamps": state.unparsed_timestamps,
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
    preview = clean_htmlish_text(row.get("preview"))
    posted = row.get("postedFull") if isinstance(row.get("postedFull"), str) else row.get("posted")
    posted_at = parse_listing_timestamp(posted)
    return {
        "id": post_id,
        "title": title,
        "posted": posted,
        "posted_at": posted_at.isoformat() if posted_at is not None else None,
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
    body_preview = clean_htmlish_text(row.get("body"))
    author = row.get("author") if isinstance(row.get("author"), str) and row.get("author") else row.get("name")
    posted = row.get("posted") if isinstance(row.get("posted"), str) else None
    posted_at = parse_listing_timestamp(posted)
    return {
        "id": topic_id,
        "title": title,
        "topic": title,
        "posted": posted,
        "posted_at": posted_at.isoformat() if posted_at is not None else None,
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


KNOWN_TALENT_CALC_CLASSES = frozenset(
    {
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
)


def _absolute_tool_url(raw: str, *, tool_slug: str) -> str | None:
    """Return the absolute Wowhead URL for ``raw`` when it already is one, else None."""
    lowered = raw.lower()
    url_candidate = f"https://{raw}" if lowered.startswith(("wowhead.com/", "www.wowhead.com/")) else raw
    parsed = urlparse(url_candidate)
    if not (parsed.scheme and parsed.netloc):
        return None
    hostname = parsed.hostname.lower() if isinstance(parsed.hostname, str) else ""
    if hostname != "wowhead.com" and not hostname.endswith(".wowhead.com"):
        raise ValueError(f"{tool_slug} URL must point to wowhead.com.")
    return url_candidate


def _talent_calc_ref_parts(normalized: str) -> list[str]:
    parts = normalized.split("/")
    if parts and parts[-1] == "":
        parts = parts[:-1]
    if any(part == "" for part in parts):
        raise ValueError("talent-calc reference must not include empty path segments.")
    return parts


def _validated_talent_calc_parts(parts: list[str]) -> list[str]:
    """Validate the class/spec portion of a relative talent-calc ref (expansion prefix already stripped)."""
    if not parts:
        raise ValueError("talent-calc reference cannot be empty.")
    invalid = ValueError("talent-calc reference must be a Wowhead talent-calc path or class/spec ref.")
    if parts[0] == "talent-calc":
        if len(parts) not in {3, 4} or parts[1] not in KNOWN_TALENT_CALC_CLASSES:
            raise invalid
    elif parts[0] not in KNOWN_TALENT_CALC_CLASSES or len(parts) not in {2, 3}:
        raise invalid
    return parts


def _expansion_prefixed_talent_calc_url(normalized: str) -> str | None:
    """Route an expansion-prefixed relative talent-calc ref to its retail tool URL, else None."""
    raw_parts = _talent_calc_ref_parts(normalized)
    has_expansion_prefix = bool(raw_parts and raw_parts[0] in EXPANSION_PREFIXES)
    parts = _validated_talent_calc_parts(raw_parts[1:] if has_expansion_prefix else raw_parts)
    if not has_expansion_prefix:
        return None
    prefixed_parts = [raw_parts[0], *parts] if parts[0] == "talent-calc" else [raw_parts[0], "talent-calc", *parts]
    return tool_url("/".join(prefixed_parts), expansion="retail")


def _normalize_tool_ref(ref: str, *, tool_slug: str, expansion: ExpansionProfile) -> str:
    raw = ref.strip()
    if not raw:
        raise ValueError(f"{tool_slug} reference cannot be empty.")
    absolute = _absolute_tool_url(raw, tool_slug=tool_slug)
    if absolute is not None:
        return absolute
    normalized = raw.lstrip("/")
    if tool_slug == "talent-calc":
        prefixed = _expansion_prefixed_talent_calc_url(normalized)
        if prefixed is not None:
            return prefixed
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
        fail(ctx, "invalid_tool_ref", str(exc))
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
            _fail_http_status(ctx, exc)
        return payload
    except httpx.HTTPError as exc:
        if fail_on_fetch_error:
            fail(ctx, "network_error", str(exc))
        return payload
    metadata = parse_page_metadata(html, fallback_url=state_url)
    page_url = absolute_wowhead_url(metadata.get("canonical_url"), fallback=state_url) or state_url
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
                    "url": absolute_wowhead_url(url, fallback=url),
                    "author": row.get("author"),
                    "posted_short": row.get("time"),
                    "type_name": row.get("newsTypeName"),
                    "region": row.get("region"),
                    "is_blue_tracker": bool(row.get("blue")),
                    "is_news": bool(row.get("news")),
                }
            )
        normalized[section_name] = {
            "count": len(items[:limit]),
            "total": len(items),
            "truncated": len(items) > limit,
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
    cutoff = datetime.now(UTC)
    exported_at = parse_iso8601_utc(manifest.get("exported_at"))
    if exported_at is None:
        return False
    age_seconds = (cutoff - exported_at).total_seconds()
    if age_seconds > max_age_hours * 3600:
        return False

    hydration = manifest.get("hydration")
    if isinstance(hydration, dict) and hydration.get("enabled") is True:
        hydrated_at = parse_iso8601_utc(hydration.get("hydrated_at"))
        if hydrated_at is None:
            return False
        hydrated_age_seconds = (cutoff - hydrated_at).total_seconds()
        if hydrated_age_seconds > max_age_hours * 3600:
            return False
    return True


def _is_stored_at_fresh(stored_at: Any, *, max_age_hours: int) -> bool:
    parsed = parse_iso8601_utc(stored_at)
    if parsed is None:
        return False
    age_seconds = (datetime.now(UTC) - parsed).total_seconds()
    return age_seconds <= max_age_hours * 3600


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
    now = datetime.now(UTC)
    exported_at_raw = manifest.get("exported_at")
    exported_at = parse_iso8601_utc(exported_at_raw if isinstance(exported_at_raw, str) else None)
    hydration = manifest.get("hydration")
    hydrated_at_raw = hydration.get("hydrated_at") if isinstance(hydration, dict) else None
    hydrated_at = parse_iso8601_utc(hydrated_at_raw if isinstance(hydrated_at_raw, str) else None)

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

    title_score = score_text_match(query, title)
    if title_score > 0:
        score += title_score * 4
        reasons.append("title")

    dir_score = score_text_match(query, dir_name)
    if dir_score > 0:
        score += dir_score * 3
        reasons.append("dir_name")

    url_score = score_text_match(query, canonical_url)
    if url_score > 0:
        score += url_score * 2
        reasons.append("canonical_url")

    expansion_score = score_text_match(query, expansion)
    if expansion_score > 0:
        score += expansion_score * 2
        reasons.append("expansion")

    if isinstance(hydration, dict):
        if hydration.get("enabled") is True and query_normalized in {"hydrated", "hydration"}:
            score += 3
            reasons.append("hydration_enabled")
        raw_types = hydration.get("types")
        if isinstance(raw_types, list):
            hydration_type_score = score_text_match(query, *raw_types)
            if hydration_type_score > 0:
                score += hydration_type_score * 2
                reasons.append("hydration_types")

    if isinstance(freshness, dict):
        bundle_status = freshness.get("bundle")
        hydration_status = freshness.get("hydration")
        if isinstance(bundle_status, str) and score_text_match(query, bundle_status) > 0:
            score += 2
            reasons.append("bundle_freshness")
        if isinstance(hydration_status, str) and hydration_status != "disabled" and score_text_match(query, hydration_status) > 0:
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
            manifest = read_json_file(manifest_path)
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
        "updated_at": iso_now_utc(),
        "root": str(root),
        "count": len(bundles),
        "bundles": bundles,
    }
    write_json_file(_guide_bundle_index_path(root), index_payload)


def _load_guide_bundle_index(root: Path) -> list[dict[str, Any]] | None:
    index_path = _guide_bundle_index_path(root)
    if not index_path.exists():
        return None
    try:
        payload = read_json_file(index_path)
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
        payload = read_json_file(path)
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
            "count": (
                entities_manifest.get("count")
                if isinstance(entities_manifest.get("count"), int)
                else observed_counts["hydrated_entities"]
            ),
            "hydrated_at": (
                entities_manifest.get("hydrated_at")
                if isinstance(entities_manifest.get("hydrated_at"), str)
                else None
            ),
            "counts_by_type": (
                entities_manifest.get("counts_by_type")
                if isinstance(entities_manifest.get("counts_by_type"), dict)
                else {}
            ),
            "counts_by_storage_source": (
                entities_manifest.get("counts_by_storage_source")
                if isinstance(entities_manifest.get("counts_by_storage_source"), dict)
                else {}
            ),
        }
    return payload


def _existing_hydrated_items_by_key(entities_dir: Path) -> dict[tuple[str, int], dict[str, Any]]:
    existing_entities_manifest_path = entities_dir / "manifest.json"
    existing_entities_manifest = (
        read_json_file(existing_entities_manifest_path)
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


def _hydratable_entity_ref(row: Any, *, selected_types: set[str]) -> tuple[str, int] | None:
    if not isinstance(row, dict):
        return None
    entity_type = row.get("entity_type")
    entity_id = row.get("id")
    if not isinstance(entity_type, str) or not isinstance(entity_id, int):
        return None
    if entity_type not in selected_types:
        return None
    return entity_type, entity_id


def _load_or_store_hydrated_entity(
    ctx: typer.Context,
    client: WowheadClient,
    *,
    entity_path: Path,
    entity_type: str,
    entity_id: int,
    stored_at: Any,
    reuse_existing: bool,
) -> tuple[dict[str, Any], str, Any]:
    """Return the entity payload, where it came from, and when it was stored in the bundle."""
    if reuse_existing:
        loaded = read_json_file(entity_path)
        if isinstance(loaded, dict):
            return loaded, "bundle_store", stored_at
    payload_row, storage_source = _load_or_build_cached_entity_payload(
        ctx,
        client,
        entity_type=entity_type,
        entity_id=entity_id,
        data_env=None,
        include_comments=False,
        include_all_comments=False,
        linked_entity_preview_limit=0,
    )
    entity_path.parent.mkdir(parents=True, exist_ok=True)
    write_json_file(entity_path, payload_row)
    return payload_row, storage_source, iso_now_utc()


def _write_hydrated_entities_manifest(
    entities_dir: Path,
    *,
    items: list[dict[str, Any]],
    hydrate_types: tuple[str, ...],
) -> GuideHydrationResult:
    if not items:
        return GuideHydrationResult(items=items, hydrated_at=None, files_written={})
    counts_by_type: dict[str, int] = {}
    for row in items:
        hydrated_type = row.get("entity_type")
        if not isinstance(hydrated_type, str):
            continue
        counts_by_type[hydrated_type] = counts_by_type.get(hydrated_type, 0) + 1
    hydrated_at = iso_now_utc()
    write_json_file(
        entities_dir / "manifest.json",
        {
            "hydrated_at": hydrated_at,
            "count": len(items),
            "types": list(hydrate_types),
            "counts_by_type": counts_by_type,
            "counts_by_storage_source": hydrate_source_counts(items),
            "items": items,
        },
    )
    return GuideHydrationResult(
        items=items,
        hydrated_at=hydrated_at,
        files_written={"entities_manifest_json": "entities/manifest.json"},
    )


def _hydrate_guide_linked_entities(
    ctx: typer.Context,
    *,
    client: WowheadClient,
    export_dir: Path,
    linked_items: list[Any],
    options: GuideExportOptions,
) -> GuideHydrationResult:
    if not options.hydrate_linked_entities or not isinstance(linked_items, list):
        return GuideHydrationResult(items=[], hydrated_at=None, files_written={})

    entities_dir = export_dir / "entities"
    existing_items_by_key = _existing_hydrated_items_by_key(entities_dir)
    selected_types = set(options.hydrate_types)
    hydrated_summary_items: list[dict[str, Any]] = []
    for row in linked_items:
        if len(hydrated_summary_items) >= options.hydrate_limit:
            break
        ref = _hydratable_entity_ref(row, selected_types=selected_types)
        if ref is None:
            continue
        hydrated_type, hydrated_id = ref
        entity_path = entities_dir / hydrated_type / f"{hydrated_id}.json"
        existing_summary = existing_items_by_key.get(ref)
        stored_at = existing_summary.get("stored_at") if isinstance(existing_summary, dict) else None
        reuse_existing = (
            not options.force_rehydrate
            and options.rehydrate_max_age_hours is not None
            and entity_path.exists()
            and _is_stored_at_fresh(stored_at, max_age_hours=options.rehydrate_max_age_hours)
        )
        payload_row, storage_source, stored_at = _load_or_store_hydrated_entity(
            ctx,
            client,
            entity_path=entity_path,
            entity_type=hydrated_type,
            entity_id=hydrated_id,
            stored_at=stored_at,
            reuse_existing=reuse_existing,
        )

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

    return _write_hydrated_entities_manifest(
        entities_dir,
        items=hydrated_summary_items,
        hydrate_types=options.hydrate_types,
    )


def _write_guide_export_bundle(
    ctx: typer.Context,
    *,
    client: WowheadClient,
    export_dir: Path,
    options: GuideExportOptions,
) -> dict[str, Any]:
    payload, html = _build_guide_full_payload(
        ctx,
        guide_ref=options.guide_ref,
        max_links=options.max_links,
        include_replies=options.include_replies,
        client=client,
    )
    export_dir.mkdir(parents=True, exist_ok=True)

    files_written, assets = write_guide_export_assets(export_dir=export_dir, payload=payload, html=html)
    hydration = _hydrate_guide_linked_entities(
        ctx,
        client=client,
        export_dir=export_dir,
        linked_items=assets.linked_items,
        options=options,
    )
    files_written.update(hydration.files_written)
    manifest = guide_export_manifest(
        export_dir=export_dir,
        payload=payload,
        options=options,
        assets=assets,
        hydration=hydration,
        files_written=files_written,
    )
    manifest_path = export_dir / "manifest.json"
    write_json_file(manifest_path, manifest)
    manifest["files"]["manifest_json"] = manifest_path.name
    write_json_file(manifest_path, manifest)
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

    search_root = (root or guide_export_root()).expanduser()
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
    pretty: PrettyOption = False,
    expansion: str | None = typer.Option(
        None,
        "--expansion",
        help="Expansion profile key/alias (for example: retail, classic, tbc, wotlk, cata, mop-classic, ptr). "
        "When omitted, defaults to retail but may auto-detect from a Wowhead URL in supported commands.",
    ),
    normalize_canonical_to_expansion: bool = typer.Option(
        False,
        "--normalize-canonical-to-expansion/--no-normalize-canonical-to-expansion",
        help="Rewrite canonical entity page URLs to the selected expansion path when canonical redirects across profiles.",
    ),
    compact: CompactOption = False,
    fields: FieldsOption = None,
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
    profile: ProfileOption = None,
    fields_strict: FieldsStrictOption = False,
    compact_max_chars: CompactMaxCharsOption = DEFAULT_COMPACT_MAX_CHARS,
) -> None:
    """Global options shared by every wowhead subcommand; pass them before the subcommand name."""
    try:
        expansion_profile = resolve_expansion(expansion)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--expansion") from exc
    configure(
        ctx,
        provider=PROVIDER_NAME,
        pretty=pretty,
        compact=compact,
        fields=fields,
        fields_strict=fields_strict,
        profile=profile,
        compact_max_chars=compact_max_chars,
        config=WowheadConfig(
            stream=stream,
            expansion=expansion_profile,
            expansion_explicit=expansion is not None,
            expansion_source="flag" if expansion is not None else "default",
            normalize_canonical_to_expansion=normalize_canonical_to_expansion,
            citation_pack=citation_pack,
        ),
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
    """Report Wowhead endpoint reachability, parser shape checks, and cache/runtime readiness."""
    cfg = _cfg(ctx)
    _emit_surface(ctx, lambda: provider.doctor(live=not no_live, expansion=cfg.expansion.key))


@app.command("expansion-detect")
def expansion_detect(
    ctx: typer.Context,
    url: str = typer.Argument(..., help="Wowhead URL to inspect."),
) -> None:
    """Detect which expansion profile a Wowhead URL belongs to and whether it matches the selected one."""
    profile = detect_expansion_from_url(url)
    entity = parse_entity_from_wowhead_url(url)
    payload: dict[str, Any] = {
        "url": url,
        "detected_expansion": profile.key if profile is not None else None,
        "entity": {"type": entity[0], "id": entity[1]} if entity is not None else None,
    }
    cfg = _cfg(ctx)
    if profile is not None:
        payload["matches_selected_expansion"] = profile.key == cfg.expansion.key
        payload["selected_expansion"] = cfg.expansion.key
        payload["selected_expansion_explicit"] = cfg.expansion_explicit
    _emit(ctx, payload)


@app.command("expansions")
def expansions(ctx: typer.Context) -> None:
    """List the supported Wowhead expansion profiles and their URL/data-env routing."""
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
    """Report the cache backend configuration and per-namespace entry counts."""
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
        "settings": cache_settings_payload(settings),
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
    """Delete unreadable or expired file-cache entries and report what was repaired."""
    settings = _load_cache_settings_or_fail(ctx)
    if settings.backend != "file":
        fail(ctx, "invalid_argument", "cache-repair is currently only supported for file cache backends.")
    result = repair_file_cache(settings.cache_dir, apply=apply, expired_only=expired_only, sample_limit=sample_limit)
    payload = {
        "settings": cache_settings_payload(settings),
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
    """Clear cached Wowhead responses for the selected namespaces or for the whole cache."""
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
            fail(ctx, "invalid_argument", "--expired-only is only supported for file cache backends.")
        try:
            removed = clear_redis_cache(
                settings.redis_url,
                prefix=settings.prefix,
                namespaces=selected_namespaces,
            )
        except ValueError as exc:
            fail(ctx, "invalid_cache_config", str(exc))
        remaining = inspect_redis_cache(settings.redis_url, prefix=settings.prefix)
    payload = {
        "settings": cache_settings_payload(settings),
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
    """Resolve a name or URL to the single most likely Wowhead entity plus a follow-up command."""
    cfg = _cfg(ctx)
    _emit_surface(
        ctx,
        lambda: provider.resolve(query, limit=limit, entity_types=entity_type, expansion=cfg.expansion.key),
    )


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
    """Search Wowhead suggestions and return ranked entity candidates."""
    cfg = _apply_url_expansion(ctx, query)
    explicit = cfg.expansion.key if cfg.expansion_explicit else None
    _emit_surface(ctx, lambda: provider.search(query, limit=limit, expansion=explicit))


@dataclass(frozen=True, slots=True)
class TimelineScanOptions:
    """Shared ``news``/``blue-tracker`` scan window: topic, page range, and parsed date bounds."""

    query: str | None
    page: int
    pages: int
    limit: int
    date_from: datetime | None
    date_to: datetime | None


def _validated_date_window(ctx: typer.Context, date_from: str | None, date_to: str | None) -> tuple[datetime | None, datetime | None]:
    parsed_date_from = parse_date_bound(date_from, end_of_day=False)
    if date_from is not None and parsed_date_from is None:
        fail(ctx, "invalid_argument", f"Invalid --date-from value {date_from!r}.")
    parsed_date_to = parse_date_bound(date_to, end_of_day=True)
    if date_to is not None and parsed_date_to is None:
        fail(ctx, "invalid_argument", f"Invalid --date-to value {date_to!r}.")
    if parsed_date_from is not None and parsed_date_to is not None and parsed_date_from > parsed_date_to:
        fail(ctx, "invalid_argument", "--date-from must be <= --date-to.")
    return parsed_date_from, parsed_date_to


def _timeline_scan_block(collected: dict[str, Any], *, options: TimelineScanOptions) -> dict[str, Any]:
    return {
        "page": options.page,
        "pages_requested": options.pages,
        "pages_scanned": collected["pages_scanned"],
        "total_pages": collected["total_pages"],
        "stop_reason": collected["stop_reason"],
        # Rows a date window had to drop because Wowhead's timestamp did not parse.
        "unparsed_timestamps": collected["unparsed_timestamps"],
    }


def _news_payload(
    ctx: typer.Context,
    *,
    client: WowheadClient,
    cfg: WowheadConfig,
    options: TimelineScanOptions,
    selected_authors: tuple[str, ...],
    selected_types: tuple[str, ...],
) -> dict[str, Any]:
    collected = _collect_timeline_pages(
        ctx=ctx,
        page=options.page,
        pages=options.pages,
        fetch_page=lambda current_page: client.news_page_html(page=current_page),
        extract_page=_extract_news_page_data,
        normalize_row=_normalize_news_row,
        query=options.query,
        date_from=options.date_from,
        date_to=options.date_to,
    )
    filtered_results = [
        row
        for row in collected["results"]
        if text_filter_match(row.get("author"), selected_authors)
        and text_filter_match(row.get("type_name"), selected_types)
    ]
    return {
        "query": options.query,
        "expansion": cfg.expansion.key,
        "news_url": news_url(page=options.page, expansion=cfg.expansion),
        "filters": {
            "authors": list(selected_authors),
            "types": list(selected_types),
            "date_from": options.date_from.isoformat() if options.date_from is not None else None,
            "date_to": options.date_to.isoformat() if options.date_to is not None else None,
        },
        "scan": _timeline_scan_block(collected, options=options),
        **limited_result_block(filtered_results, limit=options.limit),
        "facets": collect_timeline_facets(filtered_results, fields={"authors": "author", "types": "type_name"}),
    }


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
    """List Wowhead news posts with topic, date-window, and listing-field filters."""
    parsed_date_from, parsed_date_to = _validated_date_window(ctx, date_from, date_to)
    _emit(
        ctx,
        _news_payload(
            ctx,
            client=_client(ctx),
            cfg=_cfg(ctx),
            options=TimelineScanOptions(
                query=query,
                page=page,
                pages=pages,
                limit=limit,
                date_from=parsed_date_from,
                date_to=parsed_date_to,
            ),
            selected_authors=normalize_text_filters(author),
            selected_types=normalize_text_filters(type_name),
        ),
    )


def _blue_tracker_payload(
    ctx: typer.Context,
    *,
    client: WowheadClient,
    cfg: WowheadConfig,
    options: TimelineScanOptions,
    selected_authors: tuple[str, ...],
    selected_regions: tuple[str, ...],
    selected_forums: tuple[str, ...],
) -> dict[str, Any]:
    collected = _collect_timeline_pages(
        ctx=ctx,
        page=options.page,
        pages=options.pages,
        fetch_page=lambda current_page: client.blue_tracker_page_html(page=current_page),
        extract_page=_extract_blue_tracker_page_data,
        normalize_row=_normalize_blue_tracker_row,
        query=options.query,
        date_from=options.date_from,
        date_to=options.date_to,
    )
    filtered_results = [
        row
        for row in collected["results"]
        if text_filter_match(row.get("author"), selected_authors)
        and text_filter_match(row.get("region"), selected_regions)
        and text_filter_match(row.get("forum"), selected_forums)
    ]
    return {
        "query": options.query,
        "expansion": cfg.expansion.key,
        "blue_tracker_url": blue_tracker_url(page=options.page, expansion=cfg.expansion),
        "filters": {
            "authors": list(selected_authors),
            "regions": list(selected_regions),
            "forums": list(selected_forums),
            "date_from": options.date_from.isoformat() if options.date_from is not None else None,
            "date_to": options.date_to.isoformat() if options.date_to is not None else None,
        },
        "scan": _timeline_scan_block(collected, options=options),
        **limited_result_block(filtered_results, limit=options.limit),
        "facets": collect_timeline_facets(
            filtered_results,
            fields={"authors": "author", "regions": "region", "forums": "forum"},
        ),
    }


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
    """List Wowhead blue-tracker posts with topic, date-window, and listing-field filters."""
    parsed_date_from, parsed_date_to = _validated_date_window(ctx, date_from, date_to)
    _emit(
        ctx,
        _blue_tracker_payload(
            ctx,
            client=_client(ctx),
            cfg=_cfg(ctx),
            options=TimelineScanOptions(
                query=query,
                page=page,
                pages=pages,
                limit=limit,
                date_from=parsed_date_from,
                date_to=parsed_date_to,
            ),
            selected_authors=normalize_text_filters(author),
            selected_regions=normalize_text_filters(region),
            selected_forums=normalize_text_filters(forum),
        ),
    )


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
    """Fetch one Wowhead news article with body markup, related posts, and citations."""
    cfg = _cfg(ctx)
    try:
        page_url = _normalize_news_post_ref(ref, expansion=cfg.expansion)
    except ValueError as exc:
        fail(ctx, "invalid_ref", str(exc))
    client = _client(ctx)
    try:
        html = client.page_html(page_url)
    except httpx.HTTPStatusError as exc:
        _fail_http_status(ctx, exc)
    except httpx.HTTPError as exc:
        fail(ctx, "network_error", str(exc))
    metadata = parse_page_metadata(html, fallback_url=page_url)
    canonical_url = absolute_wowhead_url(metadata.get("canonical_url"), fallback=page_url)
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
    """Fetch one Wowhead blue-tracker topic with its posts, participants, and citations."""
    cfg = _cfg(ctx)
    try:
        page_url = _normalize_blue_topic_ref(ref, expansion=cfg.expansion)
    except ValueError as exc:
        fail(ctx, "invalid_ref", str(exc))
    client = _client(ctx)
    try:
        html = client.page_html(page_url)
    except httpx.HTTPStatusError as exc:
        _fail_http_status(ctx, exc)
    except httpx.HTTPError as exc:
        fail(ctx, "network_error", str(exc))
    metadata = parse_page_metadata(html, fallback_url=page_url)
    canonical_url = absolute_wowhead_url(metadata.get("canonical_url"), fallback=page_url)
    try:
        topic_payload = extract_json_script(html, "data.blueTracker.topic")
    except (ValueError, json.JSONDecodeError) as exc:
        fail(ctx, "parse_error", str(exc))
    entries = topic_payload.get("entries") if isinstance(topic_payload, dict) else None
    if not isinstance(entries, list):
        fail(ctx, "unexpected_response", "Missing or invalid blue topic entries payload.")
    posts: list[dict[str, Any]] = []
    for row in entries:
        if not isinstance(row, dict):
            continue
        posts.append(
            {
                "post_id": row.get("post"),
                "topic_id": row.get("topic"),
                "author": row.get("author"),
                "author_page": absolute_wowhead_url(row.get("authorUrl")),
                "avatar": row.get("avatar"),
                "posted": row.get("posted"),
                "posted_full": row.get("date"),
                "updated": row.get("updated"),
                "body_html": row.get("body"),
                "body_text": clean_htmlish_text(row.get("body")),
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
    """List the guides in a Wowhead guide category with author, patch, and updated-window filters."""
    cfg = _cfg(ctx)
    client = _client(ctx)
    try:
        normalized_category, filters = validated_guides_filters(
            category=category,
            author=author,
            updated_after=updated_after,
            updated_before=updated_before,
            patch_min=patch_min,
            patch_max=patch_max,
            sort_by=sort_by,
        )
    except ValueError as exc:
        fail(ctx, "invalid_argument", str(exc))
    try:
        html = client.guide_category_page_html(normalized_category)
    except httpx.HTTPStatusError as exc:
        _fail_http_status(ctx, exc)
    except httpx.HTTPError as exc:
        fail(ctx, "network_error", str(exc))

    try:
        rows = extract_listview_data(html, "guides")
    except (ValueError, json.JSONDecodeError) as exc:
        fail(ctx, "parse_error", str(exc))

    query_text = query.strip() if isinstance(query, str) and query.strip() else None
    normalized_rows = filtered_guide_category_rows(rows, query_text=query_text, filters=filters)

    _emit(
        ctx,
        guides_payload(
            expansion=cfg.expansion,
            category=normalized_category,
            query=query,
            filters=filters,
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
    """Decode a Wowhead talent calculator ref into class, spec, and build state."""
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
    """Emit an exact talent transport packet from a Wowhead talent calculator ref."""
    payload = _base_talent_calc_payload(ctx, ref=ref)
    if not payload["tool"].get("has_build_code"):
        fail(ctx, "invalid_tool_ref", "talent-calc packet refs must include an explicit build code.")
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
            fail(ctx, "transport_packet_write_failed", f"Failed to write talent transport packet: {exc}")
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
    """Decode a Wowhead profession tree calculator ref into profession and loadout state."""
    cfg = _cfg(ctx)
    try:
        state_url = _normalize_tool_ref(ref, tool_slug="profession-tree-calc", expansion=cfg.expansion)
        state = _parse_profession_tree_state(state_url)
    except ValueError as exc:
        fail(ctx, "invalid_tool_ref", str(exc))
    client = _client(ctx)
    try:
        html = client.page_html(state_url)
    except httpx.HTTPStatusError as exc:
        _fail_http_status(ctx, exc)
    except httpx.HTTPError as exc:
        fail(ctx, "network_error", str(exc))
    metadata = parse_page_metadata(html, fallback_url=state_url)
    canonical_url = absolute_wowhead_url(metadata.get("canonical_url"), fallback=state_url)

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
    """Normalize a Wowhead dressing-room ref and report its cited state URL."""
    cfg = _cfg(ctx)
    try:
        state_url = _normalize_dressing_room_ref(ref, expansion=cfg.expansion)
        state = _parse_dressing_room_state(state_url)
    except ValueError as exc:
        fail(ctx, "invalid_tool_ref", str(exc))
    client = _client(ctx)
    fetch_url = tool_url("dressing-room", expansion=cfg.expansion)
    try:
        html = client.page_html(fetch_url)
    except httpx.HTTPStatusError as exc:
        _fail_http_status(ctx, exc)
    except httpx.HTTPError as exc:
        fail(ctx, "network_error", str(exc))
    metadata = parse_page_metadata(html, fallback_url=state_url)
    canonical_url = absolute_wowhead_url(metadata.get("canonical_url"), fallback=fetch_url)

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
    """Normalize a Wowhead profiler list ref and report its list, region, realm, and name parts."""
    cfg = _cfg(ctx)
    try:
        state_url = _normalize_profiler_ref(ref, expansion=cfg.expansion)
        state = _parse_profiler_state(state_url)
    except ValueError as exc:
        fail(ctx, "invalid_tool_ref", str(exc))
    client = _client(ctx)
    fetch_url = tool_url("list", expansion=cfg.expansion)
    try:
        html = client.page_html(fetch_url)
    except httpx.HTTPStatusError as exc:
        _fail_http_status(ctx, exc)
    except httpx.HTTPError as exc:
        fail(ctx, "network_error", str(exc))
    metadata = parse_page_metadata(html, fallback_url=state_url)
    canonical_url = absolute_wowhead_url(metadata.get("canonical_url"), fallback=fetch_url)

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


@dataclass(frozen=True, slots=True)
class GuideSummaryOptions:
    """``wowhead guide`` sampling limits: sampled comments, comment length, linked-entity preview."""

    comment_sample: int
    comment_chars: int
    linked_entity_preview_limit: int


def _guide_full_command(guide_ref: str, *, expansion: ExpansionProfile) -> str:
    """The `guide-full` follow-up for a guide ref, routed to the active expansion."""
    return f"{command_prefix_for_expansion(expansion)} guide-full {guide_ref}"


def _guide_sampled_comments(
    raw_comments: list[dict[str, Any]],
    *,
    canonical_url: str,
    comment_sample: int,
    comment_chars: int,
) -> list[dict[str, Any]]:
    """Top-rated comments trimmed to the flag limits, in the shape the guide payload exposes."""
    if comment_sample <= 0 or not raw_comments:
        return []
    ranked = sort_comments(raw_comments, "rating")
    sampled = normalize_comments(ranked[:comment_sample], page_url=canonical_url, include_replies=False)
    return [
        {
            "id": row.get("id"),
            "user": row.get("user"),
            "rating": row.get("rating"),
            "date": row.get("date"),
            "body": truncate_text(row.get("body"), max_chars=comment_chars),
            "citation_url": row.get("citation_url"),
        }
        for row in sampled
    ]


def _guide_linked_entity_preview(
    html: str,
    *,
    canonical_url: str,
    guide_ref: str,
    guide_id: int | None,
    preview_limit: int,
    expansion: ExpansionProfile,
) -> dict[str, Any]:
    href_entities, gatherer_entities, merged_entities = _collect_guide_linked_entities(
        html=html,
        canonical_url=canonical_url,
        guide_id=guide_id,
    )
    preview = build_linked_entity_preview(
        merged_entities,
        entity_type="guide",
        entity_id=guide_id or 0,
        preview_limit=preview_limit,
        fetch_more_command=_guide_full_command(guide_ref, expansion=expansion),
    )
    preview["source_counts"] = {
        "href": len(href_entities),
        "gatherer": len(gatherer_entities),
        "merged": preview["count"],
    }
    return preview


def _guide_summary_payload(
    ctx: typer.Context,
    *,
    guide_ref: str,
    options: GuideSummaryOptions,
) -> dict[str, Any]:
    cfg = _cfg(ctx)
    client = _client(ctx)
    html, guide_id, lookup_url, metadata, canonical_url = _fetch_guide_page(ctx, client, guide_ref=guide_ref)

    try:
        raw_comments = extract_comments_dataset(html)
    except ValueError:
        raw_comments = []

    guide_body_markup = extract_markup_by_target(html, target="guide-body")
    analysis_surfaces = _guide_analysis_surfaces(
        section_chunks=extract_guide_section_chunks(guide_body_markup) if isinstance(guide_body_markup, str) else [],
        canonical_url=canonical_url,
        page_title=metadata["title"],
    )

    payload: dict[str, Any] = {
        "expansion": cfg.expansion.key,
        "guide": {
            "input": guide_ref,
            "id": guide_id,
            "lookup_url": lookup_url,
            "page_url": canonical_url,
        },
        "query": {
            "comment_sample": options.comment_sample,
            "comment_chars": options.comment_chars,
        },
        "page": {
            "title": metadata["title"],
            "description": metadata["description"],
            "canonical_url": canonical_url,
        },
        "comments": {
            "count": len(raw_comments),
            "top": _guide_sampled_comments(
                raw_comments,
                canonical_url=canonical_url,
                comment_sample=options.comment_sample,
                comment_chars=options.comment_chars,
            ),
        },
        "linked_entities": _guide_linked_entity_preview(
            html,
            canonical_url=canonical_url,
            guide_ref=guide_ref,
            guide_id=guide_id,
            preview_limit=options.linked_entity_preview_limit,
            expansion=cfg.expansion,
        ),
        "analysis_surfaces": {
            "count": len(analysis_surfaces),
            "items": analysis_surfaces[:10],
            "more_available": len(analysis_surfaces) > 10,
            "fetch_more_command": _guide_full_command(guide_ref, expansion=cfg.expansion),
        },
        "citations": {
            "page": canonical_url,
            "comments": f"{canonical_url}#comments",
        },
    }
    page_meta = _page_meta_block(parse_page_meta_json(html))
    if page_meta is not None:
        payload["page_meta"] = page_meta
    return payload


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
    """Fetch one Wowhead guide: analysis surfaces, linked entities, comments, and page metadata (sections are in guide-full)."""
    _emit(
        ctx,
        _guide_summary_payload(
            ctx,
            guide_ref=guide_ref,
            options=GuideSummaryOptions(
                comment_sample=comment_sample,
                comment_chars=comment_chars,
                linked_entity_preview_limit=linked_entity_preview_limit,
            ),
        ),
    )


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
    """Fetch one Wowhead guide with every section, comment, and linked entity hydrated."""
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
        help=(
            "Restrict hydrated linked entity types. Repeat or pass comma-separated values from: "
            "achievement, battle-pet, currency, faction, item, mount, npc, object, pet, quest, "
            "recipe, spell, transmog-set, zone. Defaults to spell,item,npc when hydration is enabled."
        ),
    ),
    hydrate_limit: int = typer.Option(
        100,
        "--hydrate-limit",
        min=1,
        max=1000,
        help="Maximum linked entities to hydrate when --hydrate-linked-entities is enabled.",
    ),
) -> None:
    """Export a Wowhead guide bundle (manifest, sections, entities) to a local directory."""
    client = _client(ctx)
    selected_hydrate_types: tuple[str, ...] = ()
    if hydrate_linked_entities:
        try:
            selected_hydrate_types = _normalize_hydrate_types(hydrate_type)
        except ValueError as exc:
            fail(ctx, "invalid_argument", str(exc))
    if out is None:
        preview_payload, _ = _build_guide_full_payload(
            ctx,
            guide_ref=guide_ref,
            max_links=max_links,
            include_replies=include_replies,
            client=client,
        )
        export_dir = default_guide_export_dir(preview_payload).expanduser()
    else:
        export_dir = out.expanduser()
    manifest = _write_guide_export_bundle(
        ctx,
        client=client,
        export_dir=export_dir,
        options=GuideExportOptions(
            guide_ref=guide_ref,
            max_links=max_links,
            include_replies=include_replies,
            hydrate_linked_entities=hydrate_linked_entities,
            hydrate_types=selected_hydrate_types,
            hydrate_limit=hydrate_limit,
        ),
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
        help=(
            "Restrict search kinds. Repeat or pass comma-separated values from: sections, "
            "analysis_surfaces, navigation, linked_entities, gatherer_entities, comments."
        ),
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
    """Query one guide for the sections, links, and comments that match a query string."""
    try:
        export_dir = _resolve_corpus_ref(bundle_ref, root=root)
        corpus = _load_guide_export(export_dir)
    except (ValueError, json.JSONDecodeError) as exc:
        fail(ctx, "invalid_bundle", str(exc))
    try:
        selected_kinds = _normalize_query_kinds(kind)
    except ValueError as exc:
        fail(ctx, "invalid_argument", str(exc))
    try:
        selected_link_sources = _normalize_link_source_filters(linked_source)
    except ValueError as exc:
        fail(ctx, "invalid_argument", str(exc))

    section_title_filter = section_title.strip().lower() if isinstance(section_title, str) and section_title.strip() else None
    payload = {
        "query": query,
        **guide_query_payload(
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


@dataclass(frozen=True, slots=True)
class GuideBundleQueryOptions:
    """Filters applied to every local bundle by ``wowhead guide-bundle-query``."""

    query: str
    limit: int
    selected_kinds: tuple[str, ...]
    section_title_filter: str | None
    selected_link_sources: tuple[str, ...]


@dataclass(slots=True)
class GuideBundleQueryMatches:
    """Ranked bundle rows, the flattened top matches, and the per-kind match totals."""

    bundles: list[dict[str, Any]]
    top: list[dict[str, Any]]
    counts: dict[str, int]


def _guide_bundle_query_matches(
    bundles: list[dict[str, Any]],
    *,
    root: Path,
    options: GuideBundleQueryOptions,
) -> GuideBundleQueryMatches:
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
        result = guide_query_payload(
            export_dir=export_dir,
            corpus=corpus,
            query=options.query,
            selected_kinds=options.selected_kinds,
            section_title_filter=options.section_title_filter,
            selected_link_sources=options.selected_link_sources,
            limit=options.limit,
        )
        bundle_result = _bundle_query_result(bundle, query=options.query, result=result, root=root)
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
        key=lambda row: guide_query_match_sort_key(row)
        + ((row.get("bundle") or {}).get("title") or "", (row.get("bundle") or {}).get("path") or "")
    )
    return GuideBundleQueryMatches(bundles=matched_bundles, top=top_matches, counts=aggregate_counts)


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
        help=(
            "Restrict search kinds. Repeat or pass comma-separated values from: sections, "
            "analysis_surfaces, navigation, linked_entities, gatherer_entities, comments."
        ),
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
    """Query every local guide bundle under a corpus root and rank the matching guides."""
    resolved_root = (root or guide_export_root()).expanduser()
    bundles = _discover_guide_corpora(resolved_root, max_age_hours=max_age_hours)
    try:
        selected_kinds = _normalize_query_kinds(kind)
        selected_link_sources = _normalize_link_source_filters(linked_source)
    except ValueError as exc:
        fail(ctx, "invalid_argument", str(exc))

    options = GuideBundleQueryOptions(
        query=query,
        limit=limit,
        selected_kinds=selected_kinds,
        section_title_filter=section_title.strip().lower()
        if isinstance(section_title, str) and section_title.strip()
        else None,
        selected_link_sources=selected_link_sources,
    )
    matches = _guide_bundle_query_matches(bundles, root=resolved_root, options=options)

    _emit(
        ctx,
        {
            "query": query,
            "root": str(resolved_root),
            "max_age_hours": max_age_hours,
            "searched_bundle_count": len(bundles),
            "count": len(matches.bundles),
            "stale_reason_counts": _bundle_freshness_rollups(bundles),
            "filters": {
                "kinds": list(options.selected_kinds),
                "section_title": options.section_title_filter,
                "linked_sources": list(options.selected_link_sources),
            },
            "counts": matches.counts,
            "bundles": matches.bundles[:bundle_limit],
            "top": matches.top[:limit],
        },
    )


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
    """Search local guide bundles by title, guide id, or directory name."""
    normalized_query = " ".join(query.split())
    if not normalized_query:
        fail(ctx, "invalid_argument", "query cannot be empty.")

    resolved_root = (root or guide_export_root()).expanduser()
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
        "count": len(matches[:limit]),
        "total_matches": len(matches),
        "truncated": len(matches) > limit,
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
    """List the local guide bundles under a corpus root with freshness and hydration summaries."""
    resolved_root = (root or guide_export_root()).expanduser()
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
    """Inspect one local guide bundle for missing files, stale data, and hydration gaps."""
    try:
        export_dir = _resolve_corpus_ref(bundle_ref, root=root)
        corpus = _load_guide_export(export_dir)
    except (ValueError, json.JSONDecodeError) as exc:
        fail(ctx, "invalid_bundle", str(exc))
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
    """Rebuild the local guide bundle index from the bundles on disk."""
    resolved_root = (root or guide_export_root()).expanduser()
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
        fail(ctx, "index_rebuild_failed", f"Failed to rebuild bundle index under {resolved_root}.")
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
    """Re-export stale local guide bundles using their recorded export options."""
    try:
        export_dir = _resolve_corpus_ref(bundle_ref, root=root)
        corpus = _load_guide_export(export_dir)
    except (ValueError, json.JSONDecodeError) as exc:
        fail(ctx, "invalid_bundle", str(exc))

    manifest = corpus["manifest"]
    if not isinstance(manifest, dict):
        fail(ctx, "invalid_bundle", "Bundle manifest is not a JSON object.")

    try:
        recorded_options = infer_guide_export_options(manifest)
    except ValueError as exc:
        fail(ctx, "invalid_bundle", str(exc))

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
        export_dir=export_dir,
        options=replace(recorded_options, rehydrate_max_age_hours=max_age_hours, force_rehydrate=force),
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
    url: str | None = typer.Option(
        None,
        "--url",
        help="Wowhead entity page URL. Overrides type/id and auto-selects expansion when --expansion is omitted.",
    ),
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
    """Fetch a Wowhead entity tooltip with optional comments and linked entities."""
    resolved_type = entity_type
    resolved_id = entity_id
    if url is not None:
        _apply_url_expansion(ctx, url)
        parsed = parse_entity_from_wowhead_url(url)
        if parsed is None:
            fail(ctx, "invalid_argument", f"Could not parse entity from URL {url!r}.")
        resolved_type, resolved_id = parsed
    client = _client(ctx)
    payload = _build_entity_payload(
        ctx,
        client,
        entity_type=resolved_type,
        entity_id=resolved_id,
        data_env=data_env,
        include_comments=include_comments,
        include_all_comments=include_all_comments,
        linked_entity_preview_limit=linked_entity_preview_limit,
    )
    _emit(ctx, payload)


def _entity_page_payload(
    ctx: typer.Context,
    *,
    entity_type: str,
    entity_id: int,
    max_links: int,
    include_gatherer: bool,
) -> dict[str, Any]:
    cfg = _cfg(ctx)
    client = _client(ctx)
    plan = _resolve_page_fetch_target(ctx, client, entity_type=entity_type, entity_id=entity_id)
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
    links = extract_linked_entities_from_href(html, source_url=canonical_url)
    if include_gatherer:
        links = links + extract_gatherer_entities(html, source_url=canonical_url)

    deduped = dedupe_links(links, entity_type=plan.page_entity_type, entity_id=plan.page_entity_id)

    payload: dict[str, Any] = {
        "expansion": cfg.expansion.key,
        "expansion_source": cfg.expansion_source,
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
        "linked_entities": truncated_link_block(deduped, max_links=max_links),
        "citations": {
            "page": canonical_url,
            "comments": f"{canonical_url}#comments",
        },
    }
    policy_notes = _expansion_policy_notes(cfg, canonical_url, raw_canonical)
    if policy_notes:
        payload["notes"] = policy_notes
    page_meta = _page_meta_block(parse_page_meta_json(html))
    if page_meta is not None:
        payload["page_meta"] = page_meta
    return attach_entity_page_normalization(
        payload,
        entity_type=entity_type,
        page={
            "title": metadata.get("title"),
            "description": metadata.get("description"),
            "canonical_url": metadata.get("canonical_url"),
        },
    )


@app.command("entity-page")
def entity_page(
    ctx: typer.Context,
    entity_type: str = typer.Argument(..., help="Wowhead entity type. Example: item, quest, npc."),
    entity_id: int = typer.Argument(..., help="Wowhead entity id."),
    url: str | None = typer.Option(
        None,
        "--url",
        help="Wowhead entity page URL. Overrides type/id and auto-selects expansion when --expansion is omitted.",
    ),
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
    """Fetch a Wowhead entity page with parsed metadata and its linked entities.

    Comments are a separate surface: run `wowhead comments TYPE ID`.
    """
    resolved_type = entity_type
    resolved_id = entity_id
    if url is not None:
        _apply_url_expansion(ctx, url)
        parsed = parse_entity_from_wowhead_url(url)
        if parsed is None:
            fail(ctx, "invalid_argument", f"Could not parse entity from URL {url!r}.")
        resolved_type, resolved_id = parsed
    _emit(
        ctx,
        _entity_page_payload(
            ctx,
            entity_type=resolved_type,
            entity_id=resolved_id,
            max_links=max_links,
            include_gatherer=include_gatherer,
        ),
    )


@dataclass(frozen=True, slots=True)
class CommentsQueryOptions:
    """Every ``wowhead comments`` flag: selection, filtering, hydration, and insight limits."""

    limit: int
    sort: str
    min_rating: int | None
    include_replies: bool
    hydrate_missing_replies: bool
    max_concurrency: int
    linked_entity_preview_limit: int
    date_from: str | None
    date_to: str | None
    min_replies: int | None
    author: str | None
    keywords: tuple[str, ...]
    insights: bool
    insight_limit: int


def _selected_comment_rows(
    raw_comments: list[dict[str, Any]],
    *,
    options: CommentsQueryOptions,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Apply the rating, date, author, and keyword filters, then sort as the flags asked."""
    if options.min_rating is not None:
        raw_comments = [
            row
            for row in raw_comments
            if isinstance(row.get("rating"), int) and row["rating"] >= options.min_rating
        ]
    filtered, filter_metadata = filter_raw_comments(
        raw_comments,
        date_from=options.date_from,
        date_to=options.date_to,
        min_replies=options.min_replies,
        author=options.author,
        keywords=options.keywords,
    )
    return sort_comments(filtered, options.sort), filter_metadata


def _comments_query_block(options: CommentsQueryOptions) -> dict[str, Any]:
    return {
        "limit": options.limit,
        "sort": options.sort,
        "min_rating": options.min_rating,
        "date_from": options.date_from,
        "date_to": options.date_to,
        "min_replies": options.min_replies,
        "author": options.author,
        "keywords": list(options.keywords),
        "include_replies": options.include_replies,
        "hydrate_missing_replies": options.hydrate_missing_replies,
        "max_concurrency": options.max_concurrency,
        "insights": options.insights,
        "insight_limit": options.insight_limit,
    }


def _comments_payload(
    ctx: typer.Context,
    *,
    entity_type: str,
    entity_id: int,
    options: CommentsQueryOptions,
) -> dict[str, Any]:
    cfg = _cfg(ctx)
    client = _client(ctx)
    plan = _resolve_page_fetch_target(ctx, client, entity_type=entity_type, entity_id=entity_id)
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
        embedded_comments = extract_comments_dataset(html)
    except ValueError as exc:
        fail(ctx, "parse_error", str(exc))

    embedded_total = len(embedded_comments)
    filtered_comments, filter_metadata = _selected_comment_rows(embedded_comments, options=options)
    selected = filtered_comments[: options.limit]

    hydrated_count = 0
    if options.hydrate_missing_replies and options.include_replies:
        hydrated_count = _hydrate_missing_comment_replies(client, selected, max_concurrency=options.max_concurrency)

    normalized = normalize_comments(selected, page_url=canonical_url, include_replies=options.include_replies)

    payload = {
        "expansion": cfg.expansion.key,
        "normalize_canonical_to_expansion": cfg.normalize_canonical_to_expansion,
        "entity": {
            "type": entity_type,
            "id": entity_id,
            "page_url": canonical_url,
        },
        "query": _comments_query_block(options),
        "counts": {
            "embedded_comments": embedded_total,
            "filtered_comments": len(filtered_comments),
            "returned_comments": len(normalized),
            "hydrated_reply_threads": hydrated_count,
        },
        "comments": normalized,
        "citations": {
            "page": canonical_url,
            "comments": f"{canonical_url}#comments",
        },
    }
    if options.linked_entity_preview_limit > 0:
        payload["linked_entities"] = build_linked_entity_preview(
            extract_linked_entities_from_href(html, source_url=canonical_url)
            + extract_gatherer_entities(html, source_url=canonical_url),
            entity_type=plan.page_entity_type,
            entity_id=plan.page_entity_id,
            preview_limit=options.linked_entity_preview_limit,
            fetch_more_command_builder=lambda count: entity_page_fetch_more_command(
                entity_type, entity_id, count, expansion=cfg.expansion
            ),
        )
    if options.insights:
        payload["intelligence"] = build_comments_intelligence(
            page_url=canonical_url,
            embedded_total=embedded_total,
            filtered_comments=filtered_comments,
            filters=filter_metadata,
            insight_limit=options.insight_limit,
        )
    return payload


CommentsLimitOption = Annotated[int, typer.Option("--limit", min=1, max=500, help="Maximum number of top-level comments to return.")]
CommentsSortOption = Annotated[str, typer.Option("--sort", help="Sort mode for top-level comments: newest | oldest | rating.")]
CommentsMinRatingOption = Annotated[int | None, typer.Option("--min-rating", help="Filter out comments below this rating.")]
CommentsIncludeRepliesOption = Annotated[
    bool, typer.Option("--include-replies/--no-include-replies", help="Include reply objects for each comment.")
]
CommentsHydrateRepliesOption = Annotated[
    bool,
    typer.Option(
        "--hydrate-missing-replies/--no-hydrate-missing-replies",
        help="Fetch missing replies via /comment/show-replies when embedded data is incomplete.",
    ),
]
CommentsMaxConcurrencyOption = Annotated[
    int,
    typer.Option(
        "--max-concurrency",
        min=1,
        max=16,
        help="Maximum parallel reply hydration requests when --hydrate-missing-replies is enabled.",
    ),
]
CommentsLinkedPreviewLimitOption = Annotated[
    int,
    typer.Option(
        "--linked-entity-preview-limit",
        min=0,
        max=50,
        help="Maximum linked entities to include as a lightweight preview. Set to 0 to disable.",
    ),
]
CommentsDateFromOption = Annotated[
    str | None, typer.Option("--date-from", help="Retain comments on or after this ISO date (YYYY-MM-DD or full timestamp).")
]
CommentsDateToOption = Annotated[
    str | None, typer.Option("--date-to", help="Retain comments on or before this ISO date (YYYY-MM-DD or full timestamp).")
]
CommentsMinRepliesOption = Annotated[
    int | None, typer.Option("--min-replies", min=0, help="Retain only comments with at least this many replies.")
]
CommentsAuthorOption = Annotated[
    str | None,
    typer.Option("--author", help="Retain only comments whose author contains this substring (case-insensitive)."),
]
CommentsKeywordOption = Annotated[
    list[str] | None,
    typer.Option("--keyword", help="Retain only comments whose body contains every keyword. Repeatable or comma-separated."),
]
CommentsInsightsOption = Annotated[
    bool,
    typer.Option("--insights", help="Attach deterministic comment intelligence (freshness, near-duplicates, cited top insights)."),
]
CommentsInsightLimitOption = Annotated[
    int, typer.Option("--insight-limit", min=1, max=10, help="Maximum insight rows when --insights is enabled.")
]


@app.command("comments")
def comments(
    ctx: typer.Context,
    entity_type: str = typer.Argument(..., help="Wowhead entity type. Example: item, quest, npc."),
    entity_id: int = typer.Argument(..., help="Wowhead entity id."),
    limit: CommentsLimitOption = 25,
    sort: CommentsSortOption = "newest",
    min_rating: CommentsMinRatingOption = None,
    include_replies: CommentsIncludeRepliesOption = True,
    hydrate_missing_replies: CommentsHydrateRepliesOption = False,
    max_concurrency: CommentsMaxConcurrencyOption = 4,
    linked_entity_preview_limit: CommentsLinkedPreviewLimitOption = 5,
    date_from: CommentsDateFromOption = None,
    date_to: CommentsDateToOption = None,
    min_replies: CommentsMinRepliesOption = None,
    author: CommentsAuthorOption = None,
    keyword: CommentsKeywordOption = None,
    insights: CommentsInsightsOption = False,
    insight_limit: CommentsInsightLimitOption = 5,
) -> None:
    """Fetch and rank the comments on a Wowhead entity page."""
    if sort not in {"newest", "oldest", "rating"}:
        fail(ctx, "invalid_argument", "sort must be one of: newest, oldest, rating.")
    keyword_values = tuple(part.strip() for raw in keyword or [] for part in raw.split(",") if part.strip())
    _emit(
        ctx,
        _comments_payload(
            ctx,
            entity_type=entity_type,
            entity_id=entity_id,
            options=CommentsQueryOptions(
                limit=limit,
                sort=sort,
                min_rating=min_rating,
                include_replies=include_replies,
                hydrate_missing_replies=hydrate_missing_replies,
                max_concurrency=max_concurrency,
                linked_entity_preview_limit=linked_entity_preview_limit,
                date_from=date_from,
                date_to=date_to,
                min_replies=min_replies,
                author=author,
                keywords=keyword_values,
                insights=insights,
                insight_limit=insight_limit,
            ),
        ),
    )


def _compare_expansion_or_fail(ctx: typer.Context, entities: list[str]) -> None:
    """Auto-route to the expansion the refs point at, and refuse refs that span several of them."""
    if _cfg(ctx).expansion_explicit:
        return
    urls_with_expansion = [token for token in entities if detect_expansion_from_url(token) is not None]
    detected = sorted({profile.key for token in urls_with_expansion if (profile := detect_expansion_from_url(token))})
    if len(detected) > 1:
        fail(ctx, "invalid_argument", "Compare references span multiple expansions; pass explicit --expansion.")
    if urls_with_expansion:
        # Route off the first ref that actually names an expansion; `entities[0]` may be a bare
        # `<type>:<id>` ref, which would silently leave the run on the default profile.
        _apply_url_expansion(ctx, urls_with_expansion[0])


def _parsed_compare_refs(ctx: typer.Context, entities: list[str]) -> list[tuple[str, int, str]]:
    parsed_refs: list[tuple[str, int, str]] = []
    for token in entities:
        try:
            entity_type, entity_id = _parse_entity_ref_token(token)
        except ValueError as exc:
            fail(ctx, "invalid_argument", str(exc))
        parsed_refs.append((entity_type, entity_id, token))
    return parsed_refs


def _compare_tooltip(ctx: typer.Context, client: WowheadClient, *, entity_type: str, entity_id: int, token: str) -> dict[str, Any]:
    try:
        return client.tooltip(entity_type, entity_id)
    except httpx.HTTPStatusError as exc:
        _fail_http_status(ctx, exc, context=token)
    except httpx.HTTPError as exc:
        fail(ctx, "network_error", f"{token}: {exc}")
    except ValueError as exc:
        fail(ctx, "parse_error", f"{token}: {exc}")


def _compare_sampled_comments(
    raw_comments: list[dict[str, Any]], *, canonical_url: str, options: ResolvedCompareOptions
) -> list[dict[str, Any]]:
    if options.comment_sample <= 0 or not raw_comments:
        return []
    ranked = sort_comments(raw_comments, "rating")
    sampled = normalize_comments(ranked[: options.comment_sample], page_url=canonical_url, include_replies=False)
    return [
        {
            "id": row.get("id"),
            "user": row.get("user"),
            "rating": row.get("rating"),
            "date": row.get("date"),
            "body": truncate_text(row.get("body"), max_chars=options.comment_chars),
            "citation_url": row.get("citation_url"),
        }
        for row in sampled
    ]


def _compare_entity_record(
    ctx: typer.Context,
    client: WowheadClient,
    *,
    entity_type: str,
    entity_id: int,
    token: str,
    options: ResolvedCompareOptions,
) -> tuple[str, dict[str, Any], set[tuple[str, int]]]:
    cfg = _cfg(ctx)
    tooltip = _compare_tooltip(ctx, client, entity_type=entity_type, entity_id=entity_id, token=token)
    html, metadata = _fetch_entity_page(ctx, client, entity_type, entity_id)
    raw_canonical = metadata["canonical_url"] or entity_url(entity_type, entity_id, expansion=cfg.expansion)
    canonical_url = (
        _normalize_canonical_entity_url(raw_canonical, expansion=cfg.expansion, entity_type=entity_type, entity_id=entity_id)
        if cfg.normalize_canonical_to_expansion
        else raw_canonical
    )

    links = extract_linked_entities_from_href(html, source_url=canonical_url)
    if options.include_gatherer:
        links = links + extract_gatherer_entities(html, source_url=canonical_url)
    linked_entities = truncated_link_block(
        dedupe_links(links, entity_type=entity_type, entity_id=entity_id),
        max_links=options.max_links_per_entity,
    )

    try:
        raw_comments = extract_comments_dataset(html)
    except ValueError:
        raw_comments = []

    ref = f"{entity_type}:{entity_id}"
    record, link_set = comparison_entity_record(
        ref=ref,
        entity_type=entity_type,
        entity_id=entity_id,
        canonical_url=canonical_url,
        tooltip=tooltip,
        metadata=metadata,
        linked_entities=linked_entities,
        raw_comments=raw_comments,
        sampled_comments=_compare_sampled_comments(raw_comments, canonical_url=canonical_url, options=options),
    )
    return ref, record, link_set


def _compare_payload(
    cfg: WowheadConfig,
    *,
    options: ResolvedCompareOptions,
    entity_records: list[dict[str, Any]],
    entity_link_sets: dict[str, set[tuple[str, int]]],
) -> dict[str, Any]:
    refs_in_order = [row["ref"] for row in entity_records]
    return {
        "expansion": cfg.expansion.key,
        "normalize_canonical_to_expansion": cfg.normalize_canonical_to_expansion,
        "inputs": refs_in_order,
        "preset": {
            "key": options.preset.key,
            "label": options.preset.label,
            "comparable_fields": list(options.comparable_fields),
        },
        "comparison": {
            "fields": comparison_field_diffs(entity_records, comparable_fields=list(options.comparable_fields)),
            "linked_entities": comparison_linked_entities_summary(
                refs_in_order=refs_in_order,
                entity_link_sets=entity_link_sets,
                expansion=cfg.expansion,
                max_shared_links=options.max_shared_links,
                max_unique_links=options.max_unique_links,
            ),
        },
        "entities": entity_records,
    }


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
    """Compare two or more Wowhead entities field by field."""
    if len(entities) < 2:
        fail(ctx, "invalid_argument", "compare requires at least two entity references.")
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
        fail(ctx, "invalid_argument", str(exc))

    _compare_expansion_or_fail(ctx, entities)
    parsed_refs = _parsed_compare_refs(ctx, entities)
    client = _client(ctx)

    entity_records: list[dict[str, Any]] = []
    entity_link_sets: dict[str, set[tuple[str, int]]] = {}
    for entity_type, entity_id, token in parsed_refs:
        ref, record, link_set = _compare_entity_record(
            ctx, client, entity_type=entity_type, entity_id=entity_id, token=token, options=options
        )
        entity_link_sets[ref] = link_set
        entity_records.append(record)

    _emit(ctx, _compare_payload(_cfg(ctx), options=options, entity_records=entity_records, entity_link_sets=entity_link_sets))


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
    """Build a linked-entity graph rooted at one Wowhead entity."""
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
        fail(ctx, "invalid_argument", str(exc))

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
    guarded_run(app, provider=PROVIDER_NAME)


if __name__ == "__main__":
    run()

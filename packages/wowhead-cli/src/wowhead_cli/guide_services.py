"""Guide retrieval, parsing and default bundle export without CLI state or output."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from warcraft_content.guide_analysis import extract_section_chunk_analysis_surfaces
from warcraft_core.provider import ProviderError
from warcraft_core.timestamps import iso_now_utc

from wowhead_cli.entities import dedupe_links, truncated_link_block
from wowhead_cli.expansion_profiles import EXPANSION_PREFIXES, ExpansionProfile, expansion_url_policy_issues
from wowhead_cli.guide_builds import guide_build_references
from wowhead_cli.guides import (
    GuideExportOptions,
    GuideHydrationResult,
    default_guide_export_dir,
    guide_export_manifest,
    read_json_file,
    write_guide_export_assets,
    write_json_file,
)
from wowhead_cli.page_parser import (
    entity_names,
    extract_comments_dataset,
    extract_gatherer_entities,
    extract_guide_rating,
    extract_guide_section_chunks,
    extract_guide_sections,
    extract_json_ld,
    extract_json_script,
    extract_linked_entities_from_href,
    extract_markup_by_target,
    extract_markup_urls,
    guide_markup_text,
    normalize_comments,
    parse_page_meta_json,
    parse_page_metadata,
)
from wowhead_cli.ranking import page_path_parts
from wowhead_cli.wowhead_client import WOWHEAD_BASE_URL, WowheadClient, guide_url


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



def _is_guide_path(path: str) -> bool:
    """Whether a Wowhead path names a guide (``guide=<id>`` or ``guide/...``) after any expansion and locale prefixes.

    ``guides/...`` is a category listing, which the ``guides`` command reads.
    """
    segments = page_path_parts(path)
    return bool(segments) and (segments[0] == "guide" or segments[0].startswith("guide="))



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
        if not _is_guide_path(parsed.path):
            raise ValueError(f"{raw!r} is not a Wowhead guide URL; expected a /guide/... or guide=<id> path.")
        guide_id = _extract_guide_id_from_path(parsed.path)
        return raw, guide_id

    normalized = raw.lstrip("/")
    if not normalized:
        raise ValueError("Guide reference cannot be empty.")

    relative_id = _parse_guide_id_token(normalized)
    if relative_id is not None:
        return guide_url(relative_id, expansion=expansion), relative_id

    if not _is_guide_path(normalized):
        raise ValueError(f"{raw!r} is not a Wowhead guide path; expected guide/... or guide=<id>.")
    root_segment = normalized.split("/", 1)[0]
    lookup_url = f"{WOWHEAD_BASE_URL}/{normalized}" if root_segment in EXPANSION_PREFIXES else f"{expansion.wowhead_base}/{normalized}"
    guide_id = _extract_guide_id_from_path(f"/{normalized}")
    return lookup_url, guide_id



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
    client: WowheadClient,
    *,
    guide_ref: str,
) -> tuple[str, int | None, str, dict[str, str | None], str]:
    from wowhead_cli.provider import transport_errors

    try:
        lookup_url, guide_id = _resolve_guide_lookup_input(guide_ref, expansion=client.expansion)
    except ValueError as exc:
        raise ProviderError("invalid_argument", str(exc)) from exc

    default_lookup = guide_url(guide_id, expansion=client.expansion) if guide_id is not None else None
    try:
        with transport_errors():
            if guide_id is not None and lookup_url == default_lookup:
                html = client.guide_page_html(guide_id)
            else:
                html = client.page_html(lookup_url)
    except ProviderError as exc:
        # Wowhead answers an unknown /guide=<id> with HTTP 400, not 404.
        if guide_id is not None and (exc.details or {}).get("status_code") == 400:
            raise ProviderError("not_found", f"Wowhead has no guide {guide_id}.", details=exc.details) from exc
        raise

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



def _guide_body_block(guide_body_markup: str | None, names: dict[tuple[str, int], str]) -> dict[str, Any]:
    """Guide body markup plus its parsed sections, chunks, and leading summary text."""
    if not isinstance(guide_body_markup, str):
        return {"raw_markup": guide_body_markup, "sections": [], "section_chunks": [], "summary": None}
    return {
        "raw_markup": guide_body_markup,
        "sections": extract_guide_sections(guide_body_markup),
        "section_chunks": extract_guide_section_chunks(guide_body_markup, names),
        "summary": guide_markup_text(guide_body_markup[:2000], names),
    }



def _guide_navigation_block(guide_nav_markup: str | None, *, canonical_url: str) -> dict[str, Any]:
    return {
        "raw_markup": guide_nav_markup,
        "links": extract_markup_urls(guide_nav_markup, source_url=canonical_url)
        if isinstance(guide_nav_markup, str)
        else [],
    }



def build_guide_full_payload(
    client: WowheadClient,
    *,
    guide_ref: str,
    max_links: int,
    include_replies: bool,
) -> tuple[dict[str, Any], str]:
    html, guide_id, lookup_url, metadata, canonical_url = _fetch_guide_page(
        client,
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

    body = _guide_body_block(extract_markup_by_target(html, target="guide-body"), entity_names(gatherer_entities))
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
        "expansion": client.expansion.key,
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
        "build_references": guide_build_references(
            html, body["raw_markup"], source_url=canonical_url, expansion=client.expansion.key,
        ),
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
    policy_notes = expansion_url_policy_issues(canonical_url, profile=client.expansion)
    if policy_notes:
        payload["notes"] = policy_notes
    page_meta = _page_meta_block(parse_page_meta_json(html))
    if page_meta is not None:
        payload["page_meta"] = page_meta
    return payload, html



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
        except (OSError, ValueError):
            continue
        if not isinstance(manifest, dict):
            continue
        corpora.append(_bundle_index_row(child, manifest))
    corpora.sort(key=lambda row: ((row.get("title") or "").lower(), row["path"]))
    return corpora



def _holds_foreign_index(root: Path) -> bool:
    """Whether ``root/index.json`` is some other file than a bundle index, which writing one would destroy."""
    index_path = _guide_bundle_index_path(root)
    if not index_path.exists():
        return False
    try:
        payload = read_json_file(index_path)
    except (OSError, ValueError):
        return True
    return not (isinstance(payload, dict) and "index_version" in payload)



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


def export_guide_bundle(client: WowheadClient, *, guide_ref: str, out: Path | None = None,
                        max_links: int = 250, include_replies: bool = False) -> dict[str, Any]:
    """Write a complete unhydrated guide bundle; entity hydration remains a separate operation."""
    payload, html = build_guide_full_payload(client, guide_ref=guide_ref, max_links=max_links,
                                           include_replies=include_replies)
    export_dir = (out or default_guide_export_dir(payload)).expanduser()
    export_dir.mkdir(parents=True, exist_ok=True)
    files, assets = write_guide_export_assets(export_dir=export_dir, payload=payload, html=html)
    # A plain re-export must remove a previous hydration manifest as the CLI has always done.
    (export_dir / "entities" / "manifest.json").unlink(missing_ok=True)
    manifest = guide_export_manifest(export_dir=export_dir, payload=payload,
        options=GuideExportOptions(guide_ref=guide_ref, max_links=max_links, include_replies=include_replies),
        assets=assets, hydration=GuideHydrationResult(items=[], hydrated_at=None, files_written={}), files_written=files)
    manifest["files"]["manifest_json"] = "manifest.json"
    write_json_file(export_dir / "manifest.json", manifest)
    if not _holds_foreign_index(export_dir.parent):
        _write_guide_bundle_index(export_dir.parent)
    return manifest

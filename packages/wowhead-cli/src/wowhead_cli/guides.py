"""Guide listing and guide-export helpers for the Wowhead CLI.

This is the stated public seam for guide behavior: ``wowhead_cli.main`` composes these plain
functions inside its Typer commands, and the guide tests call them directly instead of reaching
into ``main``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from warcraft_core.timestamps import iso_now_utc, parse_iso8601_utc

from wowhead_cli.entity_types import DEFAULT_HYDRATE_ENTITY_TYPES
from wowhead_cli.expansion_profiles import ExpansionProfile
from wowhead_cli.listing_filters import (
    collect_timeline_facets,
    limited_result_block,
    normalize_text_filters,
    parse_date_bound,
    text_filter_match,
)
from wowhead_cli.ranking import listing_match_score
from wowhead_cli.wowhead_client import guide_category_url


def slugify_path_fragment(value: str) -> str:
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


def guide_export_root() -> Path:
    return Path.cwd() / "wowhead_exports"


def default_guide_export_dir(payload: dict[str, Any]) -> Path:
    guide = payload.get("guide")
    page = payload.get("page")
    guide_id = guide.get("id") if isinstance(guide, dict) else None
    title = page.get("title") if isinstance(page, dict) else None
    slug_source = title if isinstance(title, str) and title.strip() else str(guide_id or "guide")
    if isinstance(guide_id, int):
        name = f"guide-{guide_id}-{slugify_path_fragment(slug_source)}"
    else:
        name = f"guide-{slugify_path_fragment(slug_source)}"
    return guide_export_root() / name


def write_json_file(path: Path, payload: Any) -> None:
    rendered = json.dumps(payload, indent=2, sort_keys=True)
    path.write_text(f"{rendered}\n", encoding="utf-8")


def write_jsonl_file(path: Path, rows: list[Any]) -> None:
    content = "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows)
    path.write_text(content, encoding="utf-8")


def write_optional_text_file(path: Path, value: Any) -> bool:
    if not isinstance(value, str):
        return False
    path.write_text(value, encoding="utf-8")
    return True


def read_json_file(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl_file(path: Path) -> list[Any]:
    rows: list[Any] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rows.append(json.loads(line))
    return rows


def hydrate_source_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        source = row.get("storage_source")
        if not isinstance(source, str):
            continue
        counts[source] = counts.get(source, 0) + 1
    return counts


def normalize_guide_category_row(row: dict[str, Any]) -> dict[str, Any] | None:
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


def guide_sort_key(row: dict[str, Any], *, sort_by: str, fallback_index: int) -> tuple[Any, ...]:
    if sort_by == "updated":
        updated_at = parse_iso8601_utc(row.get("last_updated"))
        return (updated_at is None, -(updated_at.timestamp()) if updated_at is not None else 0.0, fallback_index)
    if sort_by == "published":
        published_at = parse_iso8601_utc(row.get("published_at"))
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


@dataclass(frozen=True, slots=True)
class GuideCategoryFilters:
    """Validated ``wowhead guides`` row filters: author, updated window, patch window, and sort."""

    authors: tuple[str, ...]
    updated_after: datetime | None
    updated_before: datetime | None
    patch_min: int | None
    patch_max: int | None
    sort_by: str



def validated_guides_filters(
    *,
    category: str,
    author: list[str],
    updated_after: str | None,
    updated_before: str | None,
    patch_min: int | None,
    patch_max: int | None,
    sort_by: str,
) -> tuple[str, GuideCategoryFilters]:
    if sort_by not in {"relevance", "updated", "published", "rating"}:
        raise ValueError("--sort must be one of: relevance, updated, published, rating.")
    selected_authors = normalize_text_filters(author)
    parsed_updated_after = parse_date_bound(updated_after, end_of_day=False)
    if updated_after is not None and parsed_updated_after is None:
        raise ValueError(f"Invalid --updated-after value {updated_after!r}.")
    parsed_updated_before = parse_date_bound(updated_before, end_of_day=True)
    if updated_before is not None and parsed_updated_before is None:
        raise ValueError(f"Invalid --updated-before value {updated_before!r}.")
    if parsed_updated_after is not None and parsed_updated_before is not None and parsed_updated_after > parsed_updated_before:
        raise ValueError("--updated-after must be <= --updated-before.")
    if patch_min is not None and patch_max is not None and patch_min > patch_max:
        raise ValueError("--patch-min must be <= --patch-max.")
    normalized_category = category.strip().strip("/")
    if not normalized_category:
        raise ValueError("Guide category cannot be empty.")
    return normalized_category, GuideCategoryFilters(
        authors=selected_authors,
        updated_after=parsed_updated_after,
        updated_before=parsed_updated_before,
        patch_min=patch_min,
        patch_max=patch_max,
        sort_by=sort_by,
    )



def guide_row_matches_filters(row: dict[str, Any], *, filters: GuideCategoryFilters) -> bool:
    if not text_filter_match(row.get("author"), filters.authors):
        return False
    updated_at = parse_iso8601_utc(row.get("last_updated"))
    if filters.updated_after is not None and (updated_at is None or updated_at < filters.updated_after):
        return False
    if filters.updated_before is not None and (updated_at is None or updated_at > filters.updated_before):
        return False
    patch_value = row.get("patch")
    if filters.patch_min is not None and (not isinstance(patch_value, int) or patch_value < filters.patch_min):
        return False
    return not (filters.patch_max is not None and (not isinstance(patch_value, int) or patch_value > filters.patch_max))



def filtered_guide_category_rows(
    rows: list[Any],
    *,
    query_text: str | None,
    filters: GuideCategoryFilters,
) -> list[dict[str, Any]]:
    normalized_rows: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        normalized_row = normalize_guide_category_row(row)
        if normalized_row is None:
            continue
        if not guide_row_matches_filters(normalized_row, filters=filters):
            continue
        if query_text is not None:
            # The URL slug carries what the title leaves out: a raid's boss guides are titled by boss
            # alone, but their slugs start with the raid ("venomous-abyss-ulatek-boss-strategy-abilities").
            score = listing_match_score(
                query_text,
                normalized_row.get("title"),
                normalized_row.get("name"),
                normalized_row.get("author"),
                normalized_row.get("category_path"),
                normalized_row["url"].rstrip("/").rsplit("/", 1)[-1].replace("-", " "),
            )
            if score <= 0:
                continue
            normalized_row["match_score"] = score
        normalized_row["_sort"] = guide_sort_key(normalized_row, sort_by=filters.sort_by, fallback_index=index)
        normalized_rows.append(normalized_row)
    normalized_rows.sort(key=lambda row: row.pop("_sort"))
    return normalized_rows



def guides_payload(
    *,
    expansion: ExpansionProfile,
    category: str,
    query: str | None,
    filters: GuideCategoryFilters,
    normalized_rows: list[dict[str, Any]],
    limit: int,
) -> dict[str, Any]:
    return {
        "query": query,
        "expansion": expansion.key,
        "category": category,
        "guides_url": guide_category_url(category, expansion=expansion),
        "filters": {
            "authors": list(filters.authors),
            "updated_after": filters.updated_after.isoformat() if filters.updated_after is not None else None,
            "updated_before": filters.updated_before.isoformat() if filters.updated_before is not None else None,
            "patch_min": filters.patch_min,
            "patch_max": filters.patch_max,
            "sort": filters.sort_by,
        },
        **limited_result_block(normalized_rows, limit=limit),
        "facets": collect_timeline_facets(
            normalized_rows,
            fields={"authors": "author", "category_paths": "category_path"},
        ),
    }



@dataclass(frozen=True, slots=True)
class GuideExportOptions:
    """What ``wowhead guide-export`` fetches, writes, and hydrates for one guide bundle."""

    guide_ref: str
    max_links: int
    include_replies: bool
    hydrate_linked_entities: bool = False
    hydrate_types: tuple[str, ...] = ()
    hydrate_limit: int = 0
    rehydrate_max_age_hours: int | None = None
    force_rehydrate: bool = False



@dataclass(frozen=True, slots=True)
class GuideExportAssets:
    """Row collections written into a bundle directory, kept for manifest counts and hydration."""

    sections: list[Any]
    navigation_links: list[Any]
    linked_items: list[Any]
    gatherer_items: list[Any]
    comment_items: list[Any]
    analysis_surfaces: list[Any]



@dataclass(frozen=True, slots=True)
class GuideHydrationResult:
    """Outcome of hydrating a bundle's linked entities into local entity JSON files."""

    items: list[dict[str, Any]]
    hydrated_at: str | None
    files_written: dict[str, str]
    failed: list[dict[str, Any]] = field(default_factory=list)



def write_guide_export_assets(
    *,
    export_dir: Path,
    payload: dict[str, Any],
    html: str,
) -> tuple[dict[str, str], GuideExportAssets]:
    files_written: dict[str, str] = {}

    guide_json_path = export_dir / "guide.json"
    write_json_file(guide_json_path, payload)
    files_written["guide_json"] = guide_json_path.name

    page_html_path = export_dir / "page.html"
    page_html_path.write_text(html, encoding="utf-8")
    files_written["page_html"] = page_html_path.name

    body = payload.get("body")
    if isinstance(body, dict) and write_optional_text_file(export_dir / "body.markup.txt", body.get("raw_markup")):
        files_written["body_markup"] = "body.markup.txt"

    navigation = payload.get("navigation")
    if isinstance(navigation, dict) and write_optional_text_file(
        export_dir / "navigation.markup.txt",
        navigation.get("raw_markup"),
    ):
        files_written["navigation_markup"] = "navigation.markup.txt"

    def write_jsonl_asset(asset_key: str, file_name: str, items: Any) -> list[Any]:
        rows = items if isinstance(items, list) else []
        write_jsonl_file(export_dir / file_name, rows)
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
        write_json_file(structured_data_path, structured_data)
        files_written["structured_data_json"] = structured_data_path.name

    return files_written, GuideExportAssets(
        sections=sections,
        navigation_links=nav_links,
        linked_items=linked_items,
        gatherer_items=gatherer_items,
        comment_items=comment_items,
        analysis_surfaces=analysis_surface_items,
    )


def guide_export_manifest(
    *,
    export_dir: Path,
    payload: dict[str, Any],
    options: GuideExportOptions,
    assets: GuideExportAssets,
    hydration: GuideHydrationResult,
    files_written: dict[str, str],
) -> dict[str, Any]:
    exported_at = iso_now_utc()
    return {
        "provider": "wowhead",
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
            "sections": len(assets.sections),
            "navigation_links": len(assets.navigation_links),
            "linked_entities": len(assets.linked_items),
            "gatherer_entities": len(assets.gatherer_items),
            "hydrated_entities": len(hydration.items),
            "comments": len(assets.comment_items),
            "analysis_surfaces": len(assets.analysis_surfaces),
        },
        "hydration": {
            "enabled": options.hydrate_linked_entities,
            "types": list(options.hydrate_types),
            "limit": options.hydrate_limit if options.hydrate_linked_entities else 0,
            "hydrated_at": hydration.hydrated_at,
            "source_counts": hydrate_source_counts(hydration.items),
            "failed": hydration.failed,
        },
        "export_options": {
            "guide_ref": options.guide_ref,
            "max_links": options.max_links,
            "include_replies": options.include_replies,
        },
        "files": files_written,
    }



def infer_guide_export_options(manifest: dict[str, Any]) -> GuideExportOptions:
    """Recover the export options recorded in a bundle manifest so a refresh can reproduce it."""
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

    return GuideExportOptions(
        guide_ref=guide_ref,
        max_links=max_links,
        include_replies=include_replies,
        hydrate_linked_entities=hydrate_enabled,
        hydrate_types=hydrate_types,
        hydrate_limit=hydrate_limit,
    )

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _iso_now_utc() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def default_article_export_root(provider: str, *, cwd: Path | None = None) -> Path:
    base = cwd if cwd is not None else Path.cwd()
    return base / f"{provider}_exports"


def default_article_export_dir(provider: str, ref_slug: str, *, prefix: str = "guide", cwd: Path | None = None) -> Path:
    return default_article_export_root(provider, cwd=cwd) / f"{prefix}-{ref_slug}"


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True))
            handle.write("\n")


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object in {path}")
    return value


def load_json_or_default(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return load_json(path)


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        value = json.loads(line)
        if isinstance(value, dict):
            rows.append(value)
    return rows


def write_article_bundle(
    full_payload: dict[str, Any],
    *,
    provider: str,
    export_dir: Path,
    resource_key: str = "guide",
    page_resource_key: str | None = None,
    content_key: str = "article",
) -> dict[str, Any]:
    resource = dict(full_payload[resource_key])
    normalized_page_resource_key = page_resource_key or resource_key
    navigation = list((full_payload.get("navigation") or {}).get("items") or [])
    pages = list(full_payload.get("pages") or [])
    linked_entities = list((full_payload.get("linked_entities") or {}).get("items") or [])
    build_references = list((full_payload.get("build_references") or {}).get("items") or [])
    analysis_surfaces = list((full_payload.get("analysis_surfaces") or {}).get("items") or [])
    sections: list[dict[str, Any]] = []
    page_rows: list[dict[str, Any]] = []
    page_files: list[dict[str, Any]] = []
    html_dir = export_dir / "pages"
    for page in pages:
        page_resource = dict(page[normalized_page_resource_key])
        page_meta = dict(page["page"])
        article = dict(page[content_key])
        page_slug = page_resource["section_slug"]
        html_path = html_dir / f"{page_slug}.html"
        html_path.parent.mkdir(parents=True, exist_ok=True)
        html_path.write_text(article["html"], encoding="utf-8")
        page_files.append(
            {
                "section_slug": page_slug,
                "path": str(html_path.relative_to(export_dir)),
                "page_url": page_resource["page_url"],
            }
        )
        page_rows.append(
            {
                "section_slug": page_slug,
                "section_title": page_resource["section_title"],
                "page_url": page_resource["page_url"],
                "title": page_meta["title"],
                "description": page_meta.get("description"),
                "text": article["text"],
                "heading_count": len(article.get("headings") or []),
            }
        )
        for section in article.get("sections") or []:
            sections.append(
                {
                    "page_url": page_resource["page_url"],
                    "section_slug": page_slug,
                    "page_title": page_meta["title"],
                    "title": section["title"],
                    "level": section["level"],
                    "ordinal": section["ordinal"],
                    "text": section["text"],
                    "html": section["html"],
                }
            )

    manifest = {
        "export_version": 1,
        "provider": provider,
        # Freshness anchor for downstream wrapper handoff/compare metadata (AUR-386).
        "exported_at": _iso_now_utc(),
        "resource_key": resource_key,
        "page_resource_key": normalized_page_resource_key,
        "content_key": content_key,
        "output_dir": str(export_dir),
        resource_key: resource,
        "counts": {
            "pages": len(page_rows),
            "sections": len(sections),
            "navigation_links": len(navigation),
            "linked_entities": len(linked_entities),
            "build_references": len(build_references),
            "analysis_surfaces": len(analysis_surfaces),
        },
        "files": {
            "guide_json": "guide.json",
            "page_files_json": "page-files.json",
            "pages_jsonl": "pages.jsonl",
            "sections_jsonl": "sections.jsonl",
            "navigation_links_jsonl": "navigation-links.jsonl",
            "linked_entities_jsonl": "linked-entities.jsonl",
            "build_references_jsonl": "build-references.jsonl",
            "analysis_surfaces_jsonl": "analysis-surfaces.jsonl",
            "page_html_dir": "pages",
        },
    }
    export_dir.mkdir(parents=True, exist_ok=True)
    _write_json(export_dir / "guide.json", full_payload)
    _write_json(export_dir / "manifest.json", manifest)
    _write_json(export_dir / "page-files.json", {"pages": page_files})
    _write_jsonl(export_dir / "pages.jsonl", page_rows)
    _write_jsonl(export_dir / "sections.jsonl", sections)
    _write_jsonl(export_dir / "navigation-links.jsonl", navigation)
    _write_jsonl(export_dir / "linked-entities.jsonl", linked_entities)
    _write_jsonl(export_dir / "build-references.jsonl", build_references)
    _write_jsonl(export_dir / "analysis-surfaces.jsonl", analysis_surfaces)
    return manifest


def load_article_bundle(export_dir: Path) -> dict[str, Any]:
    manifest = load_json(export_dir / "manifest.json")
    files = manifest.get("files") or {}
    page_files = load_json_or_default(export_dir / files.get("page_files_json", "page-files.json"), {"pages": []})
    return {
        "manifest": manifest,
        "page_files": list(page_files.get("pages") or []) if isinstance(page_files, dict) else [],
        "pages": load_jsonl(export_dir / files.get("pages_jsonl", "pages.jsonl")),
        "sections": load_jsonl(export_dir / files.get("sections_jsonl", "sections.jsonl")),
        "navigation": load_jsonl(export_dir / files.get("navigation_links_jsonl", "navigation-links.jsonl")),
        "linked_entities": load_jsonl(export_dir / files.get("linked_entities_jsonl", "linked-entities.jsonl")),
        "build_references": load_jsonl(export_dir / files.get("build_references_jsonl", "build-references.jsonl")),
        "analysis_surfaces": load_jsonl(export_dir / files.get("analysis_surfaces_jsonl", "analysis-surfaces.jsonl")),
    }


def _query_score(query: str, text: str) -> int:
    if not query or not text:
        return 0
    normalized_text = text.lower()
    score = 0
    if query in normalized_text:
        score += 10
    terms = [term for term in query.split() if term]
    if terms and all(term in normalized_text for term in terms):
        score += 6
    for term in terms:
        if term in normalized_text:
            score += 2
    return score


def _section_haystack(row: dict[str, Any]) -> str:
    title = str(row.get("title") or "")
    return f"{title} {row.get('text') or ''}"


def _navigation_haystack(row: dict[str, Any]) -> str:
    return f"{row.get('title') or ''} {row.get('section_slug') or ''}"


def _linked_entity_haystack(row: dict[str, Any]) -> str:
    return f"{row.get('name') or ''} {row.get('type') or ''} {row.get('id') or ''}"


def _build_reference_haystack(row: dict[str, Any]) -> str:
    build_identity = row.get("build_identity") or {}
    class_spec_identity = build_identity.get("class_spec_identity") or {}
    identity = class_spec_identity.get("identity") or {}
    return " ".join(
        part
        for part in (
            str(row.get("label") or ""),
            str(row.get("build_code") or ""),
            str(row.get("url") or ""),
            str(identity.get("actor_class") or ""),
            str(identity.get("spec") or ""),
        )
        if part
    )


def _analysis_surface_haystack(row: dict[str, Any]) -> str:
    return " ".join(
        part
        for part in (
            " ".join(str(tag) for tag in row.get("surface_tags") or []),
            str(row.get("section_title") or ""),
            str(row.get("page_title") or ""),
            str(row.get("content_family") or ""),
            str(row.get("text_preview") or ""),
        )
        if part
    )


def _section_title_predicate(normalized_filter: str | None):
    if not normalized_filter:
        return None

    def predicate(row: dict[str, Any]) -> bool:
        title = str(row.get("title") or "")
        return normalized_filter in title.lower()

    return predicate


def _collect_kind_matches(
    rows: list[dict[str, Any]],
    *,
    kind: str,
    query: str,
    haystack_fn,
    predicate=None,
) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for row in rows:
        if predicate is not None and not predicate(row):
            continue
        score = _query_score(query, haystack_fn(row))
        if score <= 0:
            continue
        matches.append({"kind": kind, "score": score, **row})
    return matches


def query_article_bundle(
    bundle: dict[str, Any],
    *,
    query: str,
    limit: int,
    kinds: set[str],
    section_title_filter: str | None,
) -> dict[str, Any]:
    normalized_query = query.lower().strip()
    normalized_section_title_filter = section_title_filter.lower().strip() if section_title_filter else None
    results_by_kind: dict[str, list[dict[str, Any]]] = {
        "sections": [],
        "navigation": [],
        "linked_entities": [],
        "build_references": [],
        "analysis_surfaces": [],
    }
    if "sections" in kinds:
        results_by_kind["sections"] = _collect_kind_matches(
            bundle["sections"],
            kind="section",
            query=normalized_query,
            haystack_fn=_section_haystack,
            predicate=_section_title_predicate(normalized_section_title_filter),
        )
    if "navigation" in kinds:
        results_by_kind["navigation"] = _collect_kind_matches(
            bundle["navigation"],
            kind="navigation",
            query=normalized_query,
            haystack_fn=_navigation_haystack,
        )
    if "linked_entities" in kinds:
        results_by_kind["linked_entities"] = _collect_kind_matches(
            bundle["linked_entities"],
            kind="linked_entity",
            query=normalized_query,
            haystack_fn=_linked_entity_haystack,
        )
    if "build_references" in kinds:
        results_by_kind["build_references"] = _collect_kind_matches(
            bundle["build_references"],
            kind="build_reference",
            query=normalized_query,
            haystack_fn=_build_reference_haystack,
        )
    if "analysis_surfaces" in kinds:
        results_by_kind["analysis_surfaces"] = _collect_kind_matches(
            bundle["analysis_surfaces"],
            kind="analysis_surface",
            query=normalized_query,
            haystack_fn=_analysis_surface_haystack,
        )
    for rows in results_by_kind.values():
        rows.sort(key=lambda row: (-row["score"], str(row.get("title") or row.get("name") or "")))
    top: list[dict[str, Any]] = []
    for rows in results_by_kind.values():
        top.extend(rows[:limit])
    top.sort(key=lambda row: (-row["score"], row["kind"], str(row.get("title") or row.get("name") or "")))
    return {
        "query": query,
        "count": sum(len(rows) for rows in results_by_kind.values()),
        "match_counts": {kind: len(rows) for kind, rows in results_by_kind.items()},
        "matches": {kind: rows[:limit] for kind, rows in results_by_kind.items()},
        "top": top[:limit],
    }


def _bundle_title(bundle: dict[str, Any]) -> str | None:
    manifest_raw = bundle.get("manifest")
    manifest: dict[str, Any] = manifest_raw if isinstance(manifest_raw, dict) else {}
    resource_key = manifest.get("resource_key")
    if isinstance(resource_key, str):
        resource = manifest.get(resource_key)
        if isinstance(resource, dict):
            for key in ("title", "name", "slug", "page_url"):
                value = resource.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    pages = bundle.get("pages")
    if isinstance(pages, list):
        for row in pages:
            if not isinstance(row, dict):
                continue
            value = row.get("title")
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _bundle_descriptor(bundle: dict[str, Any], *, path: Path) -> dict[str, Any]:
    manifest_raw = bundle.get("manifest")
    manifest: dict[str, Any] = manifest_raw if isinstance(manifest_raw, dict) else {}
    provider_value = manifest.get("provider")
    provider = provider_value if isinstance(provider_value, str) else None
    counts_value = manifest.get("counts")
    counts = counts_value if isinstance(counts_value, dict) else {}
    return {
        "provider": provider,
        "path": str(path),
        "title": _bundle_title(bundle),
        "resource_key": manifest.get("resource_key"),
        "exported_at": manifest.get("exported_at"),
        "counts": counts,
    }


def _unique_strings(values: list[Any]) -> list[str]:
    seen: set[str] = set()
    rows: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        normalized = value.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        rows.append(normalized)
    return rows


def _surface_bundle_entry(bundle_info: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    citations = [row.get("citation") for row in rows if isinstance(row.get("citation"), dict)]
    return {
        "provider": bundle_info.get("provider"),
        "path": bundle_info["path"],
        "title": bundle_info.get("title"),
        "entry_count": len(rows),
        "page_urls": _unique_strings([row.get("page_url") for row in rows]),
        "section_titles": _unique_strings([row.get("section_title") for row in rows]),
        "content_families": _unique_strings([row.get("content_family") for row in rows]),
        "source_kinds": _unique_strings([row.get("source_kind") for row in rows]),
        "confidences": _unique_strings([row.get("confidence") for row in rows]),
        "previews": _unique_strings([row.get("text_preview") for row in rows])[:3],
        "citations": citations[:5],
    }


def _section_title_key(row: dict[str, Any]) -> str:
    value = row.get("title")
    if not isinstance(value, str):
        return ""
    return " ".join(value.lower().split()).strip()


def _section_bundle_entry(bundle_info: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    citations = [
        {
            "page_url": row.get("page_url"),
            "page_title": row.get("page_title"),
            "section_slug": row.get("section_slug"),
            "section_title": row.get("title"),
            "section_ordinal": row.get("ordinal"),
        }
        for row in rows
    ]
    return {
        "provider": bundle_info.get("provider"),
        "path": bundle_info["path"],
        "title": bundle_info.get("title"),
        "entry_count": len(rows),
        "page_urls": _unique_strings([row.get("page_url") for row in rows]),
        "page_titles": _unique_strings([row.get("page_title") for row in rows]),
        "section_titles": _unique_strings([row.get("title") for row in rows]),
        "section_slugs": _unique_strings([row.get("section_slug") for row in rows]),
        "previews": _unique_strings([row.get("text") for row in rows])[:3],
        "citations": citations[:5],
    }


def _build_reference_identity(row: dict[str, Any]) -> dict[str, Any]:
    build_identity_raw = row.get("build_identity")
    build_identity: dict[str, Any] = build_identity_raw if isinstance(build_identity_raw, dict) else {}
    class_spec_identity_value = build_identity.get("class_spec_identity")
    class_spec_identity = class_spec_identity_value if isinstance(class_spec_identity_value, dict) else {}
    identity_value = class_spec_identity.get("identity")
    identity = identity_value if isinstance(identity_value, dict) else {}
    actor_class_value = identity.get("actor_class")
    actor_class = actor_class_value if isinstance(actor_class_value, str) else None
    spec_value = identity.get("spec")
    spec = spec_value if isinstance(spec_value, str) else None
    build_code = row.get("build_code") if isinstance(row.get("build_code"), str) else None
    url = row.get("url") if isinstance(row.get("url"), str) else None
    return {
        "actor_class": actor_class,
        "spec": spec,
        "build_code": build_code,
        "url": url,
    }


def _build_reference_key(row: dict[str, Any]) -> tuple[str, str, str, str]:
    identity = _build_reference_identity(row)
    return (
        str(identity.get("actor_class") or ""),
        str(identity.get("spec") or ""),
        str(identity.get("build_code") or ""),
        str(identity.get("url") or ""),
    )


def _build_bundle_entry(bundle_info: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    labels = _unique_strings([row.get("label") for row in rows])
    source_urls: list[str] = []
    for row in rows:
        for source_url in row.get("source_urls") or []:
            source_urls.append(source_url)
    return {
        "provider": bundle_info.get("provider"),
        "path": bundle_info["path"],
        "title": bundle_info.get("title"),
        "entry_count": len(rows),
        "labels": labels,
        "source_urls": _unique_strings(source_urls),
        "reference_types": _unique_strings([row.get("reference_type") for row in rows]),
        "urls": _unique_strings([row.get("url") for row in rows]),
    }


def _comparison_unique_rows(
    *,
    bundle_descriptors: list[dict[str, Any]],
    membership_by_key: dict[str, set[str]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for bundle_info in bundle_descriptors:
        bundle_path = str(bundle_info["path"])
        keys = sorted(key for key, members in membership_by_key.items() if members == {bundle_path})
        rows.append(
            {
                "provider": bundle_info.get("provider"),
                "path": bundle_path,
                "title": bundle_info.get("title"),
                "keys": keys,
            }
        )
    return rows


def _collect_bundle_evidence(
    bundle_inputs: list[tuple[Path, dict[str, Any]]],
) -> tuple[
    dict[str, dict[str, list[dict[str, Any]]]],
    dict[str, dict[str, list[dict[str, Any]]]],
    dict[tuple[str, str, str, str], dict[str, list[dict[str, Any]]]],
]:
    section_evidence: dict[str, dict[str, list[dict[str, Any]]]] = {}
    tag_evidence: dict[str, dict[str, list[dict[str, Any]]]] = {}
    build_evidence: dict[tuple[str, str, str, str], dict[str, list[dict[str, Any]]]] = {}
    for path, bundle in bundle_inputs:
        bundle_path = str(path)
        for row in bundle.get("sections") or []:
            if not isinstance(row, dict):
                continue
            title_key = _section_title_key(row)
            if not title_key:
                continue
            section_evidence.setdefault(title_key, {}).setdefault(bundle_path, []).append(row)
        for row in bundle.get("analysis_surfaces") or []:
            if not isinstance(row, dict):
                continue
            for tag in row.get("surface_tags") or []:
                if not isinstance(tag, str) or not tag.strip():
                    continue
                tag_evidence.setdefault(tag.strip(), {}).setdefault(bundle_path, []).append(row)
        for row in bundle.get("build_references") or []:
            if not isinstance(row, dict):
                continue
            build_evidence.setdefault(_build_reference_key(row), {}).setdefault(bundle_path, []).append(row)
    return section_evidence, tag_evidence, build_evidence


def _build_analysis_surface_rows(
    tag_evidence: dict[str, dict[str, list[dict[str, Any]]]],
    bundle_descriptors: list[dict[str, Any]],
    total: int,
) -> tuple[list[dict[str, Any]], dict[str, set[str]]]:
    rows: list[dict[str, Any]] = []
    membership: dict[str, set[str]] = {}
    for tag, bundle_rows in sorted(tag_evidence.items()):
        members = set(bundle_rows)
        membership[tag] = members
        rows.append(
            {
                "tag": tag,
                "bundle_count": len(members),
                "shared_across_all_bundles": len(members) == total,
                "bundles": [
                    _surface_bundle_entry(bundle_info, bundle_rows[str(bundle_info["path"])])
                    for bundle_info in bundle_descriptors
                    if str(bundle_info["path"]) in bundle_rows
                ],
            }
        )
    return rows, membership


def _build_section_evidence_rows(
    section_evidence: dict[str, dict[str, list[dict[str, Any]]]],
    bundle_descriptors: list[dict[str, Any]],
    total: int,
) -> tuple[list[dict[str, Any]], dict[str, set[str]]]:
    rows: list[dict[str, Any]] = []
    membership: dict[str, set[str]] = {}
    for title_key, bundle_rows in sorted(section_evidence.items()):
        members = set(bundle_rows)
        membership[title_key] = members
        title_variants = _unique_strings(
            [
                row.get("title")
                for rows_for_bundle in bundle_rows.values()
                for row in rows_for_bundle
                if isinstance(row, dict)
            ]
        )
        rows.append(
            {
                "section_title_key": title_key,
                "title_variants": title_variants,
                "bundle_count": len(members),
                "shared_across_all_bundles": len(members) == total,
                "bundles": [
                    _section_bundle_entry(bundle_info, bundle_rows[str(bundle_info["path"])])
                    for bundle_info in bundle_descriptors
                    if str(bundle_info["path"]) in bundle_rows
                ],
            }
        )
    return rows, membership


def _build_build_reference_rows(
    build_evidence: dict[tuple[str, str, str, str], dict[str, list[dict[str, Any]]]],
    bundle_descriptors: list[dict[str, Any]],
    total: int,
) -> tuple[list[dict[str, Any]], dict[str, set[str]]]:
    rows: list[dict[str, Any]] = []
    membership: dict[str, set[str]] = {}
    for key, bundle_rows in sorted(build_evidence.items()):
        members = set(bundle_rows)
        string_key = "::".join(key)
        membership[string_key] = members
        identity = _build_reference_identity(next(iter(next(iter(bundle_rows.values())))))
        rows.append(
            {
                "reference_key": string_key,
                "bundle_count": len(members),
                "shared_across_all_bundles": len(members) == total,
                "actor_class": identity.get("actor_class"),
                "spec": identity.get("spec"),
                "build_code": identity.get("build_code"),
                "url": identity.get("url"),
                "bundles": [
                    _build_bundle_entry(bundle_info, bundle_rows[str(bundle_info["path"])])
                    for bundle_info in bundle_descriptors
                    if str(bundle_info["path"]) in bundle_rows
                ],
            }
        )
    return rows, membership


def compare_article_bundles(bundle_inputs: list[tuple[Path, dict[str, Any]]]) -> dict[str, Any]:
    if len(bundle_inputs) < 2:
        raise ValueError("compare_article_bundles requires at least two bundles")

    bundle_descriptors = [_bundle_descriptor(bundle, path=path) for path, bundle in bundle_inputs]
    bundle_paths = [str(path) for path, _bundle in bundle_inputs]
    total = len(bundle_inputs)

    section_evidence, tag_evidence, build_evidence = _collect_bundle_evidence(bundle_inputs)
    analysis_rows, analysis_membership = _build_analysis_surface_rows(tag_evidence, bundle_descriptors, total)
    section_rows, section_membership = _build_section_evidence_rows(section_evidence, bundle_descriptors, total)
    build_rows, build_membership = _build_build_reference_rows(build_evidence, bundle_descriptors, total)

    return {
        "kind": "guide_bundle_comparison",
        "comparison_scope": ["section_evidence", "analysis_surfaces", "build_references"],
        "compared_bundle_count": len(bundle_inputs),
        "bundles": bundle_descriptors,
        "section_evidence": {
            "matching_rule": "exact_normalized_section_title",
            "count": len(section_rows),
            "shared": [row["section_title_key"] for row in section_rows if row["shared_across_all_bundles"]],
            "partial": [row["section_title_key"] for row in section_rows if not row["shared_across_all_bundles"]],
            "unique_by_bundle": _comparison_unique_rows(
                bundle_descriptors=bundle_descriptors,
                membership_by_key=section_membership,
            ),
            "items": section_rows,
        },
        "analysis_surface_tags": {
            "count": len(analysis_rows),
            "shared": [row["tag"] for row in analysis_rows if row["shared_across_all_bundles"]],
            "partial": [row["tag"] for row in analysis_rows if not row["shared_across_all_bundles"]],
            "unique_by_bundle": _comparison_unique_rows(
                bundle_descriptors=bundle_descriptors,
                membership_by_key=analysis_membership,
            ),
            "items": analysis_rows,
        },
        "build_references": {
            "count": len(build_rows),
            "shared": [row["reference_key"] for row in build_rows if row["shared_across_all_bundles"]],
            "partial": [row["reference_key"] for row in build_rows if not row["shared_across_all_bundles"]],
            "unique_by_bundle": _comparison_unique_rows(
                bundle_descriptors=bundle_descriptors,
                membership_by_key=build_membership,
            ),
            "items": build_rows,
        },
        "citations": {
            "bundle_paths": bundle_paths,
        },
    }

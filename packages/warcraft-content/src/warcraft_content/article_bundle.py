from __future__ import annotations

import json
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from warcraft_core.provider import ProviderError
from warcraft_core.shapes import as_dict, unique_strings
from warcraft_core.timestamps import iso_now_utc


def article_export_dir(out: Path | None, *, provider: str, ref_slug: str, prefix: str = "guide") -> Path:
    """Where an export writes: ``out``, or ``./<provider>_exports/<prefix>-<ref_slug>`` by default.

    Checked before anything is fetched, so an ``--out`` naming a file fails at once as a bad argument
    instead of after the whole guide has been downloaded.
    """
    export_dir = out.expanduser() if out is not None else Path.cwd() / f"{provider}_exports" / f"{prefix}-{ref_slug}"
    if export_dir.exists() and not export_dir.is_dir():
        raise ProviderError("invalid_argument", f"Export path is not a directory: {export_dir}")
    return export_dir


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


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"Expected a JSON object on every line of {path}")
        rows.append(value)
    return rows


def _failed_page_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Pages the provider could not fetch, from a guide payload or a bundle manifest.

    Both carry the same ``{"count": n, "items": [...]}`` block. An export that lost pages must stay
    visible to every bundle reader (docs/foundation/SAFE_ANALYTICS_RULES.md), so this never hides a
    malformed block: it returns the rows it finds and an empty list when there are none.
    """
    block = payload.get("failed_pages")
    if not isinstance(block, dict):
        return []
    return [row for row in block.get("items") or [] if isinstance(row, dict)]


@dataclass(frozen=True, slots=True)
class _PageExport:
    """Per-page HTML files plus the flattened page and section rows written to JSONL."""

    files: list[dict[str, Any]]
    rows: list[dict[str, Any]]
    sections: list[dict[str, Any]]


def _export_pages(
    pages: list[dict[str, Any]],
    *,
    export_dir: Path,
    page_resource_key: str,
) -> _PageExport:
    """Write one HTML file per page and collect the page/section rows describing them.

    Page files left by an earlier export into the same directory are removed first, so ``pages/``
    holds exactly the pages ``page-files.json`` lists. Only the bundle's own ``.html`` files go.
    """
    export = _PageExport(files=[], rows=[], sections=[])
    html_dir = export_dir / "pages"
    for stale in html_dir.glob("*.html"):
        stale.unlink()
    for page in pages:
        page_resource = dict(page[page_resource_key])
        page_meta = dict(page["page"])
        article = dict(page["article"])
        page_slug = page_resource["section_slug"]
        html_path = html_dir / f"{page_slug}.html"
        html_path.parent.mkdir(parents=True, exist_ok=True)
        html_path.write_text(article["html"], encoding="utf-8")
        export.files.append(
            {
                "section_slug": page_slug,
                "path": str(html_path.relative_to(export_dir)),
                "page_url": page_resource["page_url"],
            }
        )
        export.rows.append(
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
            export.sections.append(
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
    return export


def write_article_bundle(
    full_payload: dict[str, Any],
    *,
    provider: str,
    export_dir: Path,
    resource_key: str = "guide",
    page_resource_key: str | None = None,
) -> dict[str, Any]:
    resource = dict(full_payload[resource_key])
    normalized_page_resource_key = page_resource_key or resource_key
    navigation = list((full_payload.get("navigation") or {}).get("items") or [])
    linked_entities = list((full_payload.get("linked_entities") or {}).get("items") or [])
    build_references = list((full_payload.get("build_references") or {}).get("items") or [])
    analysis_surfaces = list((full_payload.get("analysis_surfaces") or {}).get("items") or [])
    failed_pages = _failed_page_rows(full_payload)
    pages = _export_pages(
        list(full_payload.get("pages") or []),
        export_dir=export_dir,
        page_resource_key=normalized_page_resource_key,
    )

    manifest = {
        "export_version": 1,
        "provider": provider,
        # Freshness anchor for downstream wrapper handoff/compare metadata (AUR-386).
        "exported_at": iso_now_utc(),
        "resource_key": resource_key,
        "page_resource_key": normalized_page_resource_key,
        "content_key": "article",
        "output_dir": str(export_dir),
        resource_key: resource,
        # Set when the site served another guide than the one asked for, so readers of the bundle see it too.
        "redirect": full_payload.get("redirect"),
        # Kept out of "counts", which describes what the bundle holds; this says what it is missing.
        "failed_pages": {"count": len(failed_pages), "items": failed_pages},
        "counts": {
            "pages": len(pages.rows),
            "sections": len(pages.sections),
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
    _write_json(export_dir / "page-files.json", {"pages": pages.files})
    _write_jsonl(export_dir / "pages.jsonl", pages.rows)
    _write_jsonl(export_dir / "sections.jsonl", pages.sections)
    _write_jsonl(export_dir / "navigation-links.jsonl", navigation)
    _write_jsonl(export_dir / "linked-entities.jsonl", linked_entities)
    _write_jsonl(export_dir / "build-references.jsonl", build_references)
    _write_jsonl(export_dir / "analysis-surfaces.jsonl", analysis_surfaces)
    return manifest


class ArticleBundleError(ProviderError, ValueError):
    """``export_dir`` cannot be read as an article bundle.

    ``not_found`` when the path is not there, ``invalid_argument`` when it is a file, and
    ``invalid_bundle`` when the directory is not a readable bundle. Also a ``ValueError`` so the
    per-bundle handlers in ``warcraft_cli`` keep turning one bad bundle into an error row instead of
    aborting a comparison.
    """


# Bundle row lists and the manifest ``files`` key naming each one. Article providers list all six; a
# wowhead guide-export lists every one but pages and build references. A manifest that lists none
# of them is not a bundle, and a listed file that is missing or corrupt makes the bundle unreadable.
_CONTENT_FILES: Final = {
    "pages": "pages_jsonl",
    "sections": "sections_jsonl",
    "navigation": "navigation_links_jsonl",
    "linked_entities": "linked_entities_jsonl",
    "build_references": "build_references_jsonl",
    "analysis_surfaces": "analysis_surfaces_jsonl",
}

# Row lists only a wowhead guide-export writes. They are read only when the manifest lists them, so a
# bundle without them has no such key and a query does not report those kinds for it.
_EXTRA_CONTENT_FILES: Final = {
    "gatherer_entities": "gatherer_entities_jsonl",
    "comments": "comments_jsonl",
}


def _object_field(row: dict[str, Any], key: str) -> dict[str, Any]:
    """``row[key]`` when it is an object, ``{}`` when absent; any other type is a corrupt bundle row."""
    value = row.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{key} is a {type(value).__name__}, not an object")
    return value


def _list_field(row: dict[str, Any], key: str) -> list[Any]:
    """``row[key]`` when it is a list, ``[]`` when absent; any other type is a corrupt bundle row."""
    value = row.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{key} is a {type(value).__name__}, not a list")
    return value


def _check_nested_fields(bundle: dict[str, Any]) -> None:
    """Reject rows whose nested fields the query and compare readers walk into have the wrong type."""
    for row in bundle["build_references"]:
        _build_reference_identity(row)
        _list_field(row, "source_urls")
    for row in bundle["analysis_surfaces"]:
        _list_field(row, "surface_tags")


def _read_bundle(export_dir: Path) -> dict[str, Any]:
    manifest = load_json(export_dir / "manifest.json")
    files = manifest.get("files")
    if not isinstance(files, dict) or not files.keys() & set(_CONTENT_FILES.values()):
        raise ValueError("its manifest lists no article content file")
    bundle: dict[str, Any] = {"manifest": manifest, "failed_pages": _failed_page_rows(manifest)}
    for name, key in _CONTENT_FILES.items():
        bundle[name] = load_jsonl(export_dir / files[key]) if key in files else []
    for name, key in _EXTRA_CONTENT_FILES.items():
        if key in files:
            bundle[name] = load_jsonl(export_dir / files[key])
    _check_nested_fields(bundle)
    return bundle


def load_article_bundle(export_dir: Path) -> dict[str, Any]:
    if not export_dir.exists():
        raise ArticleBundleError("not_found", f"Bundle directory not found: {export_dir}")
    if not export_dir.is_dir():
        raise ArticleBundleError("invalid_argument", f"Bundle path is not a directory: {export_dir}")
    try:
        return _read_bundle(export_dir)
    except (OSError, ValueError, TypeError) as exc:
        # OSError: no manifest.json (commonly the parent of a bundle) or a listed file is missing.
        # ValueError: corrupt JSON or JSONL, or a row with a wrongly typed nested field. TypeError: a
        # manifest entry of the wrong type.
        raise ArticleBundleError("invalid_bundle", f"Not a readable article bundle, {export_dir}: {exc}") from exc


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


def _row_name(row: dict[str, Any]) -> Any:
    """What a row is called: a section's or link's ``title``, an entity's ``name``, a wowhead link's ``label``."""
    return row.get("title") or row.get("name") or row.get("label")


def _section_text(row: dict[str, Any]) -> Any:
    """Section body: ``text`` in article bundles, ``content_text`` in wowhead guide-exports."""
    return row.get("text") or row.get("content_text")


def _section_haystack(row: dict[str, Any]) -> str:
    return f"{row.get('title') or ''} {_section_text(row) or ''}"


def _navigation_haystack(row: dict[str, Any]) -> str:
    # Article bundles title their navigation links and give their slug; wowhead guide-exports label
    # them with a short name and keep the topic words in the link URL.
    return f"{_row_name(row) or ''} {row.get('section_slug') or row.get('url') or ''}"


def _entity_type(row: dict[str, Any]) -> Any:
    """Entity type: ``type`` in article bundles, ``entity_type`` in wowhead guide-exports."""
    return row.get("type") or row.get("entity_type")


def _linked_entity_haystack(row: dict[str, Any]) -> str:
    return f"{row.get('name') or ''} {_entity_type(row) or ''} {row.get('id') or ''}"


def _comment_haystack(row: dict[str, Any]) -> str:
    return f"{row.get('user') or ''} {row.get('body') or ''}"


def _build_reference_haystack(row: dict[str, Any]) -> str:
    identity = _build_reference_identity(row)
    parts = (row.get("label"), identity["build_code"], identity["url"], identity["actor_class"], identity["spec"])
    return " ".join(str(part) for part in parts if part)


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


def _linked_source_predicate(linked_sources: Collection[str]):
    """Keep linked entities found by any of ``linked_sources``; ``multi`` keeps those found by more than one.

    Only wowhead guide-exports tag their linked entities with ``sources`` (``href``, ``gatherer``).
    """
    if not linked_sources:
        return None

    def predicate(row: dict[str, Any]) -> bool:
        sources = {value for value in row.get("sources") or [] if isinstance(value, str)}
        return bool(sources & set(linked_sources)) or ("multi" in linked_sources and len(sources) > 1)

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
        # A match in a row's name counts twice, so the section or entity named for the question
        # outranks every row that merely mentions it.
        score = _query_score(query, haystack_fn(row)) + _query_score(query, str(_row_name(row) or ""))
        if score <= 0:
            continue
        matches.append({**row, "kind": kind, "score": score})
    return matches


# Each searchable row list: the kind its matches carry and the text a query is scored against.
_QUERY_KINDS: Final = {
    "sections": ("section", _section_haystack),
    "navigation": ("navigation", _navigation_haystack),
    "linked_entities": ("linked_entity", _linked_entity_haystack),
    "build_references": ("build_reference", _build_reference_haystack),
    "analysis_surfaces": ("analysis_surface", _analysis_surface_haystack),
}

# Searched, and reported, only for a bundle that carries them (see ``_EXTRA_CONTENT_FILES``).
_EXTRA_QUERY_KINDS: Final = {
    "gatherer_entities": ("gatherer_entity", _linked_entity_haystack),
    "comments": ("comment", _comment_haystack),
}


def _top_matches(results_by_kind: dict[str, list[dict[str, Any]]], *, limit: int) -> list[dict[str, Any]]:
    # A wowhead gatherer record is usually also one of the guide's linked entities; the top list
    # shows that entity once, as the linked entity, which also says where it was found.
    linked = {(_entity_type(row), row.get("id")) for row in results_by_kind["linked_entities"]}
    top: list[dict[str, Any]] = []
    for kind, rows in results_by_kind.items():
        if kind == "gatherer_entities":
            rows = [row for row in rows if (_entity_type(row), row.get("id")) not in linked]
        top.extend(rows[:limit])
    top.sort(key=lambda row: (-row["score"], row["kind"], str(_row_name(row) or "")))
    return top[:limit]


def query_article_bundle(
    bundle: dict[str, Any],
    *,
    query: str,
    limit: int,
    kinds: set[str],
    section_title_filter: str | None,
    linked_sources: Collection[str] = (),
) -> dict[str, Any]:
    """Search a loaded bundle's rows of each kind in ``kinds`` for ``query``.

    ``match_counts`` and ``matches`` name every kind the bundle can hold, so a kind left out of
    ``kinds`` reads as zero matches. ``linked_sources`` narrows linked entities by where a wowhead
    guide-export found them.
    """
    normalized_query = query.lower().strip()
    normalized_section_title_filter = section_title_filter.lower().strip() if section_title_filter else None
    predicates = {
        "sections": _section_title_predicate(normalized_section_title_filter),
        "linked_entities": _linked_source_predicate(linked_sources),
    }
    searchable = {**_QUERY_KINDS, **{name: spec for name, spec in _EXTRA_QUERY_KINDS.items() if name in bundle}}
    results_by_kind: dict[str, list[dict[str, Any]]] = {
        name: _collect_kind_matches(
            bundle[name],
            kind=kind,
            query=normalized_query,
            haystack_fn=haystack_fn,
            predicate=predicates.get(name),
        )
        if name in kinds
        else []
        for name, (kind, haystack_fn) in searchable.items()
    }
    for rows in results_by_kind.values():
        rows.sort(key=lambda row: (-row["score"], str(_row_name(row) or "")))
    failed_pages = list(bundle.get("failed_pages") or [])
    return {
        "query": query,
        # A query answered from a partial bundle says so instead of reading as a complete answer.
        "failed_pages": {"count": len(failed_pages), "items": failed_pages},
        "count": sum(len(rows) for rows in results_by_kind.values()),
        "match_counts": {kind: len(rows) for kind, rows in results_by_kind.items()},
        "matches": {kind: rows[:limit] for kind, rows in results_by_kind.items()},
        "top": _top_matches(results_by_kind, limit=limit),
    }


def bundle_query_payload(
    bundle_ref: str | Path,
    query: str,
    *,
    limit: int,
    kinds: Collection[str] | None,
    allowed_kinds: Collection[str],
    section_title: str | None,
) -> dict[str, Any]:
    """``guide-query``/``article-query`` data: the bundle searched, the resource it holds, and its matches; no network.

    A blank query is ``invalid_query``: it would match nothing and read as "the guide doesn't say".
    """
    if not query.strip():
        raise ProviderError("invalid_query", "Query cannot be empty.")
    selected_kinds = set(kinds or allowed_kinds)
    invalid = sorted(selected_kinds - set(allowed_kinds))
    if invalid:
        raise ProviderError("invalid_argument", f"Unsupported query kinds: {', '.join(invalid)}")
    bundle = load_article_bundle(Path(bundle_ref).expanduser())
    result = query_article_bundle(bundle, query=query, limit=limit, kinds=selected_kinds, section_title_filter=section_title)
    resource_key = str(bundle["manifest"].get("resource_key") or "guide")
    return {"bundle": str(bundle_ref), resource_key: bundle["manifest"].get(resource_key), **result}


def _bundle_title(bundle: dict[str, Any]) -> str | None:
    manifest_raw = bundle.get("manifest")
    manifest: dict[str, Any] = manifest_raw if isinstance(manifest_raw, dict) else {}
    # A wowhead guide-export manifest has no resource_key; its title is under "page".
    resource_key = manifest.get("resource_key", "page")
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


def _content_updated_at(manifest: dict[str, Any]) -> str | None:
    """When the site last changed the guide itself, as opposed to ``exported_at`` (when it was read).

    A wowhead guide-export records Wowhead's JSON-LD ``dateModified`` as ``content_updated_at``; an
    article bundle keeps the guide's ``last_updated`` (Method, Icy Veins) in its resource block.
    """
    value = manifest.get("content_updated_at")
    if value is None:
        resource = manifest.get(manifest.get("resource_key") or "guide")
        value = resource.get("last_updated") if isinstance(resource, dict) else None
    return value if isinstance(value, str) and value.strip() else None


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
        "content_updated_at": _content_updated_at(manifest),
        "counts": counts,
        # A comparison that includes a partial export must not read as complete on both sides.
        "failed_page_count": len(_failed_page_rows(manifest)),
        "redirect": manifest.get("redirect"),
    }


def _surface_bundle_entry(bundle_info: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    citations = [row.get("citation") for row in rows if isinstance(row.get("citation"), dict)]
    return {
        "provider": bundle_info.get("provider"),
        "path": bundle_info["path"],
        "title": bundle_info.get("title"),
        "entry_count": len(rows),
        "page_urls": unique_strings([row.get("page_url") for row in rows]),
        "section_titles": unique_strings([row.get("section_title") for row in rows]),
        "content_families": unique_strings([row.get("content_family") for row in rows]),
        "source_kinds": unique_strings([row.get("source_kind") for row in rows]),
        "confidences": unique_strings([row.get("confidence") for row in rows]),
        "previews": unique_strings([row.get("text_preview") for row in rows])[:3],
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
        "page_urls": unique_strings([row.get("page_url") for row in rows]),
        "page_titles": unique_strings([row.get("page_title") for row in rows]),
        "section_titles": unique_strings([row.get("title") for row in rows]),
        "section_slugs": unique_strings([row.get("section_slug") for row in rows]),
        "previews": unique_strings([_section_text(row) for row in rows])[:3],
        "citations": citations[:5],
    }


def _build_reference_identity(row: dict[str, Any]) -> dict[str, Any]:
    identity = _object_field(_object_field(_object_field(row, "build_identity"), "class_spec_identity"), "identity")
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
    labels = unique_strings([row.get("label") for row in rows])
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
        "source_urls": unique_strings(source_urls),
        "reference_types": unique_strings([row.get("reference_type") for row in rows]),
        "urls": unique_strings([row.get("url") for row in rows]),
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
        title_variants = unique_strings(
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


def _build_reference_total(bundle_inputs: list[tuple[Path, dict[str, Any]]]) -> int:
    """How many bundles a build reference must be in to count as shared.

    Every bundle that can hold build references: a wowhead guide-export never lists a build
    references file, so it does not keep the others' builds partial. At least two, so a build only
    one bundle could hold is never shared.
    """
    holders = 0
    for _path, bundle in bundle_inputs:
        files = as_dict(bundle.get("manifest")).get("files")
        holders += isinstance(files, dict) and _CONTENT_FILES["build_references"] in files
    return max(2, holders)


def compare_article_bundles(bundle_inputs: list[tuple[Path, dict[str, Any]]]) -> dict[str, Any]:
    if len(bundle_inputs) < 2:
        raise ValueError("compare_article_bundles requires at least two bundles")
    # Membership is keyed by path, so a bundle given twice would read as disagreeing with itself.
    resolved = [path.resolve() for path, _bundle in bundle_inputs]
    duplicates = sorted({str(path) for path in resolved if resolved.count(path) > 1})
    if duplicates:
        raise ArticleBundleError(
            "invalid_argument",
            f"The same bundle was given more than once: {', '.join(duplicates)}",
            details={"duplicate_bundles": duplicates},
        )

    bundle_descriptors = [_bundle_descriptor(bundle, path=path) for path, bundle in bundle_inputs]
    bundle_paths = [str(path) for path, _bundle in bundle_inputs]
    total = len(bundle_inputs)

    section_evidence, tag_evidence, build_evidence = _collect_bundle_evidence(bundle_inputs)
    analysis_rows, analysis_membership = _build_analysis_surface_rows(tag_evidence, bundle_descriptors, total)
    section_rows, section_membership = _build_section_evidence_rows(section_evidence, bundle_descriptors, total)
    build_rows, build_membership = _build_build_reference_rows(
        build_evidence, bundle_descriptors, _build_reference_total(bundle_inputs)
    )

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

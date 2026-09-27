from __future__ import annotations

from typing import Any

from warcraft_core.citations import build_citation_pack


def _dict_field(payload: dict[str, Any], key: str) -> dict[str, Any]:
    """Read a nested object from an untyped payload, falling back to an empty dict."""
    value = payload.get(key)
    return value if isinstance(value, dict) else {}


def _list_field(payload: dict[str, Any], key: str) -> list[Any]:
    """Read a nested array from an untyped payload, falling back to an empty list."""
    value = payload.get(key)
    return value if isinstance(value, list) else []


def _add_source(sources: list[dict[str, Any]], *, key: str, url: str | None, kind: str) -> None:
    if isinstance(url, str) and url.strip():
        sources.append({"key": key, "url": url.strip(), "kind": kind})


def _add_claim(
    anchors: list[dict[str, Any]],
    *,
    claim: str,
    source_key: str,
    url: str | None,
    anchor: str | None = None,
) -> None:
    if not isinstance(url, str) or not url.strip():
        return
    entry: dict[str, Any] = {
        "claim": claim,
        "source_key": source_key,
        "url": url.strip(),
    }
    if isinstance(anchor, str) and anchor.strip():
        fragment = anchor.strip()
        entry["anchor"] = fragment
        if fragment.startswith("#"):
            base_url = url.strip().split("#", 1)[0]
            entry["url"] = f"{base_url}{fragment}"
    anchors.append(entry)


def _entity_page_url(payload: dict[str, Any]) -> str | None:
    entity = _dict_field(payload, "entity")
    return str(entity.get("page_url") or "").strip() or None


def _collect_entity_page_and_citation_sources(
    sources: list[dict[str, Any]],
    payload: dict[str, Any],
    page_url: str | None,
) -> None:
    _add_source(sources, key="page", url=page_url, kind="page")
    citations = _dict_field(payload, "citations")
    for label, url in sorted(citations.items()):
        if isinstance(url, str) and url.strip():
            _add_source(sources, key=f"citations.{label}", url=url, kind=label)


def _collect_tooltip_summary_claims(
    anchors: list[dict[str, Any]],
    payload: dict[str, Any],
    page_url: str | None,
) -> None:
    tooltip = _dict_field(payload, "tooltip")
    for field in ("name", "quality", "icon"):
        value = tooltip.get(field)
        if value is not None and page_url:
            _add_claim(anchors, claim=f"tooltip.{field}", source_key="page", url=page_url)

    summary = _dict_field(payload, "summary")
    for field in ("name", "quality", "icon", "title", "description"):
        value = summary.get(field)
        if value is not None and page_url:
            _add_claim(anchors, claim=f"summary.{field}", source_key="page", url=page_url)


def _collect_linked_entity_citations(
    sources: list[dict[str, Any]],
    anchors: list[dict[str, Any]],
    payload: dict[str, Any],
) -> None:
    linked = _dict_field(payload, "linked_entities")
    items = _list_field(linked, "items")
    for index, row in enumerate(items):
        if not isinstance(row, dict):
            continue
        link_url = row.get("url") if isinstance(row.get("url"), str) else row.get("href")
        source_key = f"linked_entities.items[{index}]"
        _add_source(sources, key=source_key, url=link_url if isinstance(link_url, str) else None, kind="linked_entity")
        name = row.get("name")
        if isinstance(name, str) and name.strip() and isinstance(link_url, str):
            _add_claim(
                anchors,
                claim=f"linked_entities.items[{index}].name",
                source_key=source_key,
                url=link_url,
            )


def _collect_comment_citations(
    sources: list[dict[str, Any]],
    anchors: list[dict[str, Any]],
    payload: dict[str, Any],
    page_url: str | None,
) -> None:
    comments = _dict_field(payload, "comments")
    for bucket in ("top", "items"):
        rows = _list_field(comments, bucket)
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            citation_url = row.get("citation_url")
            source_key = "comments" if bucket == "top" else f"comments.items[{index}]"
            if bucket == "top" and index == 0:
                _add_source(sources, key="comments", url=citation_url if isinstance(citation_url, str) else page_url, kind="comments")
            fragment = None
            if isinstance(citation_url, str) and "#" in citation_url:
                fragment = citation_url[citation_url.index("#"):]
            if isinstance(row.get("body"), str):
                _add_claim(
                    anchors,
                    claim=f"comments.{bucket}[{index}].body",
                    source_key=source_key if bucket != "top" else "comments",
                    url=citation_url if isinstance(citation_url, str) else page_url,
                    anchor=fragment,
                )


def citation_pack_from_entity(payload: dict[str, Any]) -> dict[str, Any]:
    sources: list[dict[str, Any]] = []
    anchors: list[dict[str, Any]] = []

    page_url = _entity_page_url(payload)
    _collect_entity_page_and_citation_sources(sources, payload, page_url)
    _collect_tooltip_summary_claims(anchors, payload, page_url)
    _collect_linked_entity_citations(sources, anchors, payload)
    _collect_comment_citations(sources, anchors, payload, page_url)

    return build_citation_pack(sources=sources, anchors=anchors)


def _merge_entity_citation_packs(
    sources: list[dict[str, Any]],
    anchors: list[dict[str, Any]],
    entities: list[Any],
) -> None:
    for entity_row in entities:
        if not isinstance(entity_row, dict):
            continue
        ref = str(entity_row.get("ref") or "").strip()
        if not ref:
            continue
        nested = citation_pack_from_entity(entity_row)
        for source in _list_field(nested, "sources"):
            if not isinstance(source, dict):
                continue
            sources.append(
                {
                    "key": f"{ref}.{source.get('key')}",
                    "url": source.get("url"),
                    "kind": source.get("kind"),
                }
            )
        for anchor in _list_field(nested, "anchors"):
            if not isinstance(anchor, dict):
                continue
            claim = str(anchor.get("claim") or "")
            anchors.append(
                {
                    "claim": f"{ref}.{claim}" if claim else claim,
                    "source_key": f"{ref}.{anchor.get('source_key')}",
                    "url": anchor.get("url"),
                    "anchor": anchor.get("anchor"),
                }
            )


def _collect_comparison_linked_entity_sources(
    sources: list[dict[str, Any]],
    comparison: dict[str, Any],
) -> None:
    linked = _dict_field(comparison, "linked_entities")
    # Two linked-entity shapes are emitted: list-shaped buckets (only shared_items today) here,
    # and the dict-shaped unique_by_entity (ref -> rows) in the loop just below — the latter is
    # NOT dropped. Behavior is byte-identical to the pre-extraction builder.
    for bucket in ("shared_items",):
        rows = _list_field(linked, bucket)
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            url = row.get("url")
            _add_source(sources, key=f"comparison.linked_entities.{bucket}[{index}]",
                        url=url if isinstance(url, str) else None, kind="linked_entity")
    for ref, rows in sorted((linked.get("unique_by_entity") or {}).items()):
        if not isinstance(rows, list):
            continue
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            url = row.get("url")
            _add_source(
                sources,
                key=f"comparison.linked_entities.unique_by_entity.{ref}[{index}]",
                url=url if isinstance(url, str) else None,
                kind="linked_entity",
            )


def _collect_comparison_field_claims(
    anchors: list[dict[str, Any]],
    comparison: dict[str, Any],
    entities: list[Any],
) -> None:
    fields = _dict_field(comparison, "fields")
    for field_name, field_row in sorted(fields.items()):
        if not isinstance(field_row, dict):
            continue
        values = _dict_field(field_row, "values")
        for ref, _value in sorted(values.items()):
            entity_row = next((row for row in entities if isinstance(row, dict) and row.get("ref") == ref), None)
            page_url = None
            if isinstance(entity_row, dict):
                entity = _dict_field(entity_row, "entity")
                page_url = entity.get("page_url")
            _add_claim(
                anchors,
                claim=f"comparison.fields.{field_name}.{ref}",
                source_key=f"{ref}.page",
                url=page_url if isinstance(page_url, str) else None,
            )


def citation_pack_from_compare(payload: dict[str, Any]) -> dict[str, Any]:
    sources: list[dict[str, Any]] = []
    anchors: list[dict[str, Any]] = []

    entities = _list_field(payload, "entities")
    _merge_entity_citation_packs(sources, anchors, entities)

    comparison = _dict_field(payload, "comparison")
    _collect_comparison_linked_entity_sources(sources, comparison)
    _collect_comparison_field_claims(anchors, comparison, entities)

    return build_citation_pack(sources=sources, anchors=anchors)

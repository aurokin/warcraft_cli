from __future__ import annotations

from typing import Any

from warcraft_core.citations import build_citation_pack


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


def citation_pack_from_entity(payload: dict[str, Any]) -> dict[str, Any]:
    sources: list[dict[str, Any]] = []
    anchors: list[dict[str, Any]] = []

    entity = payload.get("entity") if isinstance(payload.get("entity"), dict) else {}
    page_url = str(entity.get("page_url") or "").strip() or None
    _add_source(sources, key="page", url=page_url, kind="page")

    citations = payload.get("citations") if isinstance(payload.get("citations"), dict) else {}
    for label, url in sorted(citations.items()):
        if isinstance(url, str) and url.strip():
            _add_source(sources, key=f"citations.{label}", url=url, kind=label)

    tooltip = payload.get("tooltip") if isinstance(payload.get("tooltip"), dict) else {}
    for field in ("name", "quality", "icon"):
        value = tooltip.get(field)
        if value is not None and page_url:
            _add_claim(anchors, claim=f"tooltip.{field}", source_key="page", url=page_url)

    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    for field in ("name", "quality", "icon", "title", "description"):
        value = summary.get(field)
        if value is not None and page_url:
            _add_claim(anchors, claim=f"summary.{field}", source_key="page", url=page_url)

    linked = payload.get("linked_entities") if isinstance(payload.get("linked_entities"), dict) else {}
    items = linked.get("items") if isinstance(linked.get("items"), list) else []
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

    comments = payload.get("comments") if isinstance(payload.get("comments"), dict) else {}
    for bucket in ("top", "items"):
        rows = comments.get(bucket) if isinstance(comments.get(bucket), list) else []
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

    return build_citation_pack(sources=sources, anchors=anchors)


def citation_pack_from_compare(payload: dict[str, Any]) -> dict[str, Any]:
    sources: list[dict[str, Any]] = []
    anchors: list[dict[str, Any]] = []

    entities = payload.get("entities") if isinstance(payload.get("entities"), list) else []
    for entity_row in entities:
        if not isinstance(entity_row, dict):
            continue
        ref = str(entity_row.get("ref") or "").strip()
        if not ref:
            continue
        nested = citation_pack_from_entity(entity_row)
        for source in nested.get("sources") if isinstance(nested.get("sources"), list) else []:
            if not isinstance(source, dict):
                continue
            sources.append(
                {
                    "key": f"{ref}.{source.get('key')}",
                    "url": source.get("url"),
                    "kind": source.get("kind"),
                }
            )
        for anchor in nested.get("anchors") if isinstance(nested.get("anchors"), list) else []:
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

    comparison = payload.get("comparison") if isinstance(payload.get("comparison"), dict) else {}
    linked = comparison.get("linked_entities") if isinstance(comparison.get("linked_entities"), dict) else {}
    for bucket in ("shared_items",):
        rows = linked.get(bucket) if isinstance(linked.get(bucket), list) else []
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

    fields = comparison.get("fields") if isinstance(comparison.get("fields"), dict) else {}
    for field_name, field_row in sorted(fields.items()):
        if not isinstance(field_row, dict):
            continue
        values = field_row.get("values") if isinstance(field_row.get("values"), dict) else {}
        for ref, _value in sorted(values.items()):
            entity_row = next((row for row in entities if isinstance(row, dict) and row.get("ref") == ref), None)
            page_url = None
            if isinstance(entity_row, dict):
                entity = entity_row.get("entity") if isinstance(entity_row.get("entity"), dict) else {}
                page_url = entity.get("page_url")
            _add_claim(
                anchors,
                claim=f"comparison.fields.{field_name}.{ref}",
                source_key=f"{ref}.page",
                url=page_url if isinstance(page_url, str) else None,
            )

    return build_citation_pack(sources=sources, anchors=anchors)

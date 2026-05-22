from __future__ import annotations

from collections import deque
from typing import Any

from wowhead_cli.page_parser import extract_gatherer_entities, extract_linked_entities_from_href


def _normalize_relation_filter(values: list[str] | None) -> set[str]:
    normalized: set[str] = set()
    for raw in values or []:
        for part in raw.split(","):
            slug = part.strip().lower().replace(" ", "-")
            if slug:
                normalized.add(slug)
    return normalized


def _node_key(entity_type: str, entity_id: int) -> str:
    return f"{entity_type}:{entity_id}"


def _edge_row(*, from_key: str, to_key: str, relation: str, source_kind: str | None) -> dict[str, Any]:
    return {
        "from": from_key,
        "to": to_key,
        "relation": relation,
        "source_kind": source_kind,
    }


def _summarize_node(entity_type: str, entity_id: int, *, url: str | None, name: str | None) -> dict[str, Any]:
    return {
        "key": _node_key(entity_type, entity_id),
        "entity_type": entity_type,
        "id": entity_id,
        "name": name,
        "url": url,
    }


def build_linked_graph_payload(
    *,
    root_type: str,
    root_id: int,
    root_url: str,
    fetch_page,
    depth: int,
    relation_filter: set[str],
    node_limit: int,
    max_fetches: int,
    include_gatherer: bool,
) -> dict[str, Any]:
    if depth < 1 or depth > 2:
        raise ValueError("--depth must be 1 or 2 for the current linked-graph slice.")

    nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, Any]] = []
    seen_edges: set[tuple[str, str, str]] = set()
    fetch_count = 0
    truncated = False

    root_key = _node_key(root_type, root_id)
    nodes[root_key] = _summarize_node(root_type, root_id, url=root_url, name=None)

    queue: deque[tuple[str, int, str, int]] = deque()
    queue.append((root_type, root_id, root_url, 0))
    visited_pages: set[tuple[str, int]] = {(root_type, root_id)}

    while queue and fetch_count < max_fetches:
        entity_type, entity_id, page_url, current_depth = queue.popleft()
        if current_depth >= depth:
            continue

        html, _metadata = fetch_page(entity_type, entity_id)
        fetch_count += 1

        links = extract_linked_entities_from_href(html, source_url=page_url)
        if include_gatherer:
            links = links + extract_gatherer_entities(html, source_url=page_url)

        parent_key = _node_key(entity_type, entity_id)
        for link in links:
            if len(nodes) >= node_limit:
                truncated = True
                break
            link_type = str(link.get("entity_type") or "").strip().lower()
            link_id = link.get("id")
            if not link_type or not isinstance(link_id, int):
                continue
            if relation_filter and link_type not in relation_filter:
                continue

            child_key = _node_key(link_type, link_id)
            relation = link_type
            source_kind = link.get("source_kind") if isinstance(link.get("source_kind"), str) else None
            edge_identity = (parent_key, child_key, relation)
            if edge_identity not in seen_edges:
                seen_edges.add(edge_identity)
                edges.append(_edge_row(from_key=parent_key, to_key=child_key, relation=relation, source_kind=source_kind))

            if child_key not in nodes:
                nodes[child_key] = _summarize_node(
                    link_type,
                    link_id,
                    url=link.get("url") if isinstance(link.get("url"), str) else None,
                    name=link.get("name") if isinstance(link.get("name"), str) else None,
                )

            if current_depth + 1 < depth and (link_type, link_id) not in visited_pages:
                visited_pages.add((link_type, link_id))
                child_url = link.get("url") if isinstance(link.get("url"), str) else None
                if isinstance(child_url, str) and child_url.strip():
                    queue.append((link_type, link_id, child_url.strip(), current_depth + 1))

        if truncated:
            break

    return {
        "root": nodes[root_key],
        "depth": depth,
        "filters": {
            "relation": sorted(relation_filter),
            "node_limit": node_limit,
            "max_fetches": max_fetches,
            "include_gatherer": include_gatherer,
        },
        "graph": {
            "node_count": len(nodes),
            "edge_count": len(edges),
            "nodes": [nodes[key] for key in sorted(nodes)],
            "edges": sorted(edges, key=lambda row: (row["from"], row["to"], row["relation"])),
        },
        "sampling": {
            "pages_fetched": fetch_count,
            "truncated": truncated,
            "caveat": "Relations are entity-type edges parsed from href and gatherer links on fetched pages only.",
        },
    }


def normalize_relation_option(values: list[str] | None) -> set[str]:
    return _normalize_relation_filter(values)

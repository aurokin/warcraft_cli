from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
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


@dataclass(frozen=True, slots=True)
class LinkedGraphOptions:
    """Traversal limits for one ``linked-graph`` run."""

    depth: int
    relation_filter: set[str]
    node_limit: int
    max_fetches: int
    include_gatherer: bool


@dataclass(slots=True)
class _GraphState:
    """Nodes, edges and budget counters accumulated while walking linked entity pages."""

    nodes: dict[str, dict[str, Any]] = field(default_factory=dict)
    edges: list[dict[str, Any]] = field(default_factory=list)
    seen_edges: set[tuple[str, str, str]] = field(default_factory=set)
    fetch_count: int = 0
    truncated: bool = False


_QueueItem = tuple[str, int, str, int]


def _optional_str(link: dict[str, Any], key: str) -> str | None:
    value = link.get(key)
    return value if isinstance(value, str) else None


def _page_links(html: str, *, page_url: str, include_gatherer: bool) -> list[dict[str, Any]]:
    links = extract_linked_entities_from_href(html, source_url=page_url)
    if include_gatherer:
        links = links + extract_gatherer_entities(html, source_url=page_url)
    return links


def _link_ref(link: dict[str, Any], relation_filter: set[str]) -> tuple[str, int] | None:
    """Return the usable (type, id) for a parsed link, or None when it is malformed or filtered out."""
    link_type = str(link.get("entity_type") or "").strip().lower()
    link_id = link.get("id")
    if not link_type or not isinstance(link_id, int):
        return None
    if relation_filter and link_type not in relation_filter:
        return None
    return link_type, link_id


def _record_link(state: _GraphState, *, parent_key: str, link: dict[str, Any], link_type: str, link_id: int) -> None:
    child_key = _node_key(link_type, link_id)
    edge_identity = (parent_key, child_key, link_type)
    if edge_identity not in state.seen_edges:
        state.seen_edges.add(edge_identity)
        state.edges.append(
            _edge_row(from_key=parent_key, to_key=child_key, relation=link_type, source_kind=_optional_str(link, "source_kind"))
        )
    if child_key not in state.nodes:
        state.nodes[child_key] = _summarize_node(
            link_type, link_id, url=_optional_str(link, "url"), name=_optional_str(link, "name")
        )


def _enqueue_child(
    queue: deque[_QueueItem],
    visited_pages: set[tuple[str, int]],
    *,
    link: dict[str, Any],
    link_type: str,
    link_id: int,
    next_depth: int,
    depth: int,
) -> None:
    if next_depth >= depth or (link_type, link_id) in visited_pages:
        return
    child_url = _optional_str(link, "url")
    if not child_url or not child_url.strip():
        return
    visited_pages.add((link_type, link_id))
    queue.append((link_type, link_id, child_url.strip(), next_depth))


def _expand_page(
    state: _GraphState,
    options: LinkedGraphOptions,
    *,
    html: str,
    page_url: str,
    parent_key: str,
    current_depth: int,
    queue: deque[_QueueItem],
    visited_pages: set[tuple[str, int]],
) -> None:
    for link in _page_links(html, page_url=page_url, include_gatherer=options.include_gatherer):
        if len(state.nodes) >= options.node_limit:
            state.truncated = True
            return
        ref = _link_ref(link, options.relation_filter)
        if ref is None:
            continue
        link_type, link_id = ref
        _record_link(state, parent_key=parent_key, link=link, link_type=link_type, link_id=link_id)
        _enqueue_child(
            queue,
            visited_pages,
            link=link,
            link_type=link_type,
            link_id=link_id,
            next_depth=current_depth + 1,
            depth=options.depth,
        )


def _traverse(state: _GraphState, options: LinkedGraphOptions, *, fetch_page: Any, root: _QueueItem) -> None:
    root_type, root_id, _root_url, _root_depth = root
    queue: deque[_QueueItem] = deque([root])
    visited_pages: set[tuple[str, int]] = {(root_type, root_id)}
    while queue and state.fetch_count < options.max_fetches:
        entity_type, entity_id, page_url, current_depth = queue.popleft()
        if current_depth >= options.depth:
            continue
        html, _metadata = fetch_page(entity_type, entity_id)
        state.fetch_count += 1
        _expand_page(
            state,
            options,
            html=html,
            page_url=page_url,
            parent_key=_node_key(entity_type, entity_id),
            current_depth=current_depth,
            queue=queue,
            visited_pages=visited_pages,
        )
        if state.truncated:
            return


def _graph_payload(state: _GraphState, options: LinkedGraphOptions, *, root_key: str) -> dict[str, Any]:
    return {
        "root": state.nodes[root_key],
        "depth": options.depth,
        "filters": {
            "relation": sorted(options.relation_filter),
            "node_limit": options.node_limit,
            "max_fetches": options.max_fetches,
            "include_gatherer": options.include_gatherer,
        },
        "graph": {
            "node_count": len(state.nodes),
            "edge_count": len(state.edges),
            "nodes": [state.nodes[key] for key in sorted(state.nodes)],
            "edges": sorted(state.edges, key=lambda row: (row["from"], row["to"], row["relation"])),
        },
        "sampling": {
            "pages_fetched": state.fetch_count,
            "truncated": state.truncated,
            "caveat": "Relations are entity-type edges parsed from href and gatherer links on fetched pages only.",
        },
    }


def build_linked_graph_payload(
    *,
    root_type: str,
    root_id: int,
    root_url: str,
    fetch_page: Any,
    depth: int,
    relation_filter: set[str],
    node_limit: int,
    max_fetches: int,
    include_gatherer: bool,
) -> dict[str, Any]:
    """Walk linked entities out from one root page and return the bounded node/edge graph."""
    if depth < 1 or depth > 2:
        raise ValueError("--depth must be 1 or 2 for the current linked-graph slice.")
    options = LinkedGraphOptions(
        depth=depth,
        relation_filter=relation_filter,
        node_limit=node_limit,
        max_fetches=max_fetches,
        include_gatherer=include_gatherer,
    )
    state = _GraphState()
    root_key = _node_key(root_type, root_id)
    state.nodes[root_key] = _summarize_node(root_type, root_id, url=root_url, name=None)
    _traverse(state, options, fetch_page=fetch_page, root=(root_type, root_id, root_url, 0))
    return _graph_payload(state, options, root_key=root_key)


def normalize_relation_option(values: list[str] | None) -> set[str]:
    return _normalize_relation_filter(values)

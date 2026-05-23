"""Required top-level keys for Wowhead CLI JSON contracts."""

from __future__ import annotations

SEARCH_KEYS = frozenset({"expansion", "search_url", "count", "results", "query"})
ENTITY_KEYS = frozenset({"expansion", "entity", "linked_entities", "schema_version", "normalized"})
ENTITY_PAGE_KEYS = frozenset({"expansion", "entity", "linked_entities", "page", "schema_version", "normalized"})
COMMENTS_KEYS = frozenset({"expansion", "entity", "comments", "counts"})
COMPARE_KEYS = frozenset({"expansion", "entities", "comparison"})

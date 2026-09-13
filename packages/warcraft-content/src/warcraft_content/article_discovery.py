from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Any


def article_follow_up(
    provider_command: str,
    ref: str,
    *,
    surface: str = "guide",
    full_surface: str | None = None,
    export_surface: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    normalized_full_surface = full_surface or f"{surface}-full"
    normalized_export_surface = export_surface or f"{surface}-export"
    normalized_reason = reason or f"{surface}_summary"
    quoted_ref = shlex.quote(ref)
    return {
        "recommended_surface": surface,
        "recommended_command": f"{provider_command} {surface} {quoted_ref}",
        "reason": normalized_reason,
        "alternatives": [
            f"{provider_command} {normalized_full_surface} {quoted_ref}",
            f"{provider_command} {normalized_export_surface} {quoted_ref}",
        ],
    }


@dataclass(frozen=True, slots=True)
class ArticleKind:
    """How a provider labels its articles: the follow-up surface, display/entity type, and the metadata key that carries ``ref``."""

    surface: str = "guide"
    type_name: str = "Guide"
    entity_type: str = "guide"
    metadata_key: str = "slug"


GUIDE_KIND = ArticleKind()


def article_candidate(
    *,
    ref: str,
    name: str,
    url: str,
    score: int,
    reasons: list[str],
    provider_command: str,
    kind: ArticleKind = GUIDE_KIND,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload_metadata = {
        kind.metadata_key: ref,
    }
    if metadata:
        payload_metadata.update(metadata)
    return {
        "id": ref,
        "name": name,
        "type_name": kind.type_name,
        "entity_type": kind.entity_type,
        "url": url,
        "ranking": {
            "score": score,
            "match_reasons": reasons,
        },
        "metadata": payload_metadata,
        "follow_up": article_follow_up(provider_command, ref, surface=kind.surface),
    }


def sort_article_candidates(candidates: list[dict[str, Any]]) -> None:
    candidates.sort(key=lambda row: (-int(row["ranking"]["score"]), row["name"], row["id"]))


def article_search_payload(
    *,
    query: str,
    search_query: str,
    results: list[dict[str, Any]],
    total_count: int,
) -> dict[str, Any]:
    return {
        "query": query,
        "search_query": search_query,
        "count": total_count,
        "results": results,
    }


def article_resolve_payload(
    *,
    provider_command: str,
    query: str,
    search_query: str,
    results: list[dict[str, Any]],
    total_count: int,
    resolved: bool,
) -> dict[str, Any]:
    top = results[0] if results else None
    return {
        "query": query,
        "search_query": search_query,
        "resolved": resolved,
        "confidence": "high" if resolved else ("medium" if top else "none"),
        "match": top if top else None,
        "next_command": top["follow_up"]["recommended_command"] if resolved and top else None,
        "fallback_search_command": None if resolved else f"{provider_command} search {query!r}",
        "count": total_count,
        "candidates": results,
    }


def merge_article_linked_entities(pages: list[dict[str, Any]], *, page_key: str = "guide") -> list[dict[str, Any]]:
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for page in pages:
        page_url = page[page_key]["page_url"]
        for row in page["linked_entities"]:
            key = (str(row["type"]), str(row["id"]))
            record = merged.get(key)
            if record is None:
                merged[key] = {
                    "type": row["type"],
                    "id": row["id"],
                    "name": row.get("name"),
                    "url": row["url"],
                    "source_urls": [page_url],
                }
                continue
            if not record.get("name") and row.get("name"):
                record["name"] = row["name"]
            if page_url not in record["source_urls"]:
                record["source_urls"].append(page_url)
    return sorted(merged.values(), key=lambda row: (str(row["type"]), str(row["id"])))


def merge_article_build_references(pages: list[dict[str, Any]], *, page_key: str = "guide") -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for page in pages:
        page_url = page[page_key]["page_url"]
        for row in page.get("build_references") or []:
            reference_url = str(row["url"])
            record = merged.get(reference_url)
            if record is None:
                merged[reference_url] = {
                    "kind": row["kind"],
                    "reference_type": row["reference_type"],
                    "url": row["url"],
                    "label": row.get("label"),
                    "build_code": row.get("build_code"),
                    "build_identity": row["build_identity"],
                    "source_urls": [page_url],
                }
                if "source" in row:
                    merged[reference_url]["source"] = row["source"]
                continue
            if not record.get("label") and row.get("label"):
                record["label"] = row["label"]
            if page_url not in record["source_urls"]:
                record["source_urls"].append(page_url)
    return sorted(merged.values(), key=lambda row: str(row["url"]))

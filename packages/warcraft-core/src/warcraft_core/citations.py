from __future__ import annotations

from typing import Any


def _dedupe_sources(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for row in sorted(sources, key=lambda item: (str(item.get("key") or ""), str(item.get("url") or ""))):
        key = str(row.get("key") or "").strip()
        url = str(row.get("url") or "").strip()
        if not key or not url or key in seen_keys:
            continue
        seen_keys.add(key)
        deduped.append({"key": key, "url": url, "kind": str(row.get("kind") or "source")})
    return deduped


def _dedupe_anchors(anchors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen_claims: set[str] = set()
    for row in sorted(anchors, key=lambda item: str(item.get("claim") or "")):
        claim = str(row.get("claim") or "").strip()
        source_key = str(row.get("source_key") or "").strip()
        url = str(row.get("url") or "").strip()
        if not claim or not source_key or not url or claim in seen_claims:
            continue
        seen_claims.add(claim)
        anchor = row.get("anchor")
        entry: dict[str, Any] = {"claim": claim, "source_key": source_key, "url": url}
        if isinstance(anchor, str) and anchor.strip():
            entry["anchor"] = anchor.strip()
        deduped.append(entry)
    return deduped


def build_citation_pack(
    *,
    sources: list[dict[str, Any]],
    anchors: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build a deterministic citation pack from normalized source and anchor rows."""
    deduped_sources = _dedupe_sources(sources)
    deduped_anchors = _dedupe_anchors(anchors)
    return {
        "source_count": len(deduped_sources),
        "anchor_count": len(deduped_anchors),
        "sources": deduped_sources,
        "anchors": deduped_anchors,
    }

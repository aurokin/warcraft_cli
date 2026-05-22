from __future__ import annotations

from typing import Any


def build_citation_pack(
    *,
    sources: list[dict[str, Any]],
    anchors: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build a deterministic citation pack from normalized source and anchor rows."""
    deduped_sources: list[dict[str, Any]] = []
    seen_source_keys: set[str] = set()
    for row in sorted(sources, key=lambda item: (str(item.get("key") or ""), str(item.get("url") or ""))):
        key = str(row.get("key") or "").strip()
        url = str(row.get("url") or "").strip()
        if not key or not url or key in seen_source_keys:
            continue
        seen_source_keys.add(key)
        deduped_sources.append(
            {
                "key": key,
                "url": url,
                "kind": str(row.get("kind") or "source"),
            }
        )

    deduped_anchors: list[dict[str, Any]] = []
    seen_claims: set[str] = set()
    for row in sorted(anchors, key=lambda item: str(item.get("claim") or "")):
        claim = str(row.get("claim") or "").strip()
        source_key = str(row.get("source_key") or "").strip()
        url = str(row.get("url") or "").strip()
        if not claim or not source_key or not url or claim in seen_claims:
            continue
        seen_claims.add(claim)
        anchor = row.get("anchor")
        entry: dict[str, Any] = {
            "claim": claim,
            "source_key": source_key,
            "url": url,
        }
        if isinstance(anchor, str) and anchor.strip():
            entry["anchor"] = anchor.strip()
        deduped_anchors.append(entry)

    return {
        "source_count": len(deduped_sources),
        "anchor_count": len(deduped_anchors),
        "sources": deduped_sources,
        "anchors": deduped_anchors,
    }

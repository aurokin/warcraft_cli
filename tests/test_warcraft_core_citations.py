from __future__ import annotations

from warcraft_core.citations import build_citation_pack


def test_build_citation_pack_dedupes_and_sorts() -> None:
    pack = build_citation_pack(
        sources=[
            {"key": "page", "url": "https://example/page", "kind": "page"},
            {"key": "page", "url": "https://example/page", "kind": "page"},
            {"key": "comments", "url": "https://example/page#comments", "kind": "comments"},
        ],
        anchors=[
            {"claim": "tooltip.name", "source_key": "page", "url": "https://example/page"},
            {"claim": "comments.top[0].body", "source_key": "comments",
                "url": "https://example/page#comments:id=1", "anchor": "#comments:id=1"},
        ],
    )
    assert pack["source_count"] == 2
    assert pack["anchor_count"] == 2
    assert pack["sources"][0]["key"] == "comments"
    comment_anchor = next(row for row in pack["anchors"] if row["claim"] == "comments.top[0].body")
    assert comment_anchor["anchor"] == "#comments:id=1"

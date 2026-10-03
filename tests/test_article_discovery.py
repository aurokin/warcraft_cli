from __future__ import annotations

import shlex

from warcraft_content.article_discovery import (
    article_candidate,
    article_follow_up,
    article_resolve_payload,
    article_search_payload,
    merge_article_build_references,
    merge_article_linked_entities,
    sort_article_candidates,
)

from tests.discovery_contract import resolve_data_violations, row_violations, search_data_violations


def test_article_follow_up_uses_provider_command() -> None:
    follow_up = article_follow_up("method", "mistweaver-monk")

    assert follow_up == {
        "command": "method guide mistweaver-monk",
        "surface": "guide",
        "reason": "guide_summary",
        "alternative_commands": [
            "method guide-full mistweaver-monk",
            "method guide-export mistweaver-monk",
        ],
    }


def test_article_follow_up_supports_article_surfaces_and_quotes() -> None:
    follow_up = article_follow_up("warcraft-wiki", "World of Warcraft API", surface="article")

    assert follow_up == {
        "command": "warcraft-wiki article 'World of Warcraft API'",
        "surface": "article",
        "reason": "article_summary",
        "alternative_commands": [
            "warcraft-wiki article-full 'World of Warcraft API'",
            "warcraft-wiki article-export 'World of Warcraft API'",
        ],
    }


def test_article_candidate_builds_shared_shape() -> None:
    row = article_candidate(
        ref="mistweaver-monk",
        name="Mistweaver Monk",
        url="https://www.method.gg/guides/mistweaver-monk",
        score=33,
        reasons=["exact_name", "all_terms_match"],
        provider="method",
    )

    assert row_violations(row, provider="method") == []
    assert (row["kind"], row["id"]) == ("guide", "mistweaver-monk")
    assert row["ranking"]["score"] == 33
    assert row["follow_up"]["command"] == "method guide mistweaver-monk"
    assert row["metadata"] == {}


def test_sort_article_candidates_orders_by_score_then_name() -> None:
    rows = [
        article_candidate(
            ref="b",
            name="B Guide",
            url="https://example.invalid/b",
            score=10,
            reasons=["name_contains_query"],
            provider="method",
        ),
        article_candidate(
            ref="a",
            name="A Guide",
            url="https://example.invalid/a",
            score=30,
            reasons=["name_contains_query"],
            provider="method",
        ),
    ]

    sort_article_candidates(rows)

    assert rows[0]["id"] == "a"


def test_sort_article_candidates_breaks_score_ties_by_newest_lastmod_then_name_then_id() -> None:
    """Method's sitemap rows usually carry no lastmod, so name decides which tied row is the match."""

    def row(ref: str, name: str, lastmod: str | None = None) -> dict:
        return article_candidate(
            ref=ref,
            name=name,
            url=f"https://example.invalid/{ref}",
            score=20,
            reasons=["name_contains_query"],
            provider="method",
            metadata={"sitemap_lastmod": lastmod},
        )

    rows = [row("c", "Zeta Guide"), row("b2", "Alpha Guide"), row("dated", "Omega Guide", "2026-01-01"), row("b1", "Alpha Guide")]

    sort_article_candidates(rows)

    assert [candidate["id"] for candidate in rows] == ["dated", "b1", "b2", "c"]


def test_article_search_and_resolve_payloads_keep_contract_shape() -> None:
    rows = [
        article_candidate(
            ref="mistweaver-monk",
            name="Mistweaver Monk",
            url="https://www.method.gg/guides/mistweaver-monk",
            score=33,
            reasons=["exact_name"],
            provider="method",
        )
    ]

    search_payload = article_search_payload(query="mistweaver monk guide", search_query="mistweaver monk", matches=rows, limit=5)
    resolve_payload = article_resolve_payload(
        provider_command="method",
        query="mistweaver monk guide",
        search_query="mistweaver monk",
        matches=rows,
        limit=5,
        resolved=True,
    )

    assert search_payload["results"][0]["id"] == "mistweaver-monk"
    assert resolve_payload["resolved"] is True
    assert resolve_payload["next_command"] == "method guide mistweaver-monk"
    assert resolve_payload["confidence"] == "high"
    assert search_data_violations(search_payload, provider="method") == []
    assert resolve_data_violations(resolve_payload, provider="method") == []


def test_merge_article_linked_entities_dedupes_and_preserves_source_urls() -> None:
    pages = [
        {
            "guide": {"page_url": "https://example.invalid/intro"},
            "linked_entities": [
                {"type": "spell", "id": 123, "name": None, "url": "https://wowhead.com/spell=123"},
            ],
        },
        {
            "guide": {"page_url": "https://example.invalid/talents"},
            "linked_entities": [
                {"type": "spell", "id": 123, "name": "Example Spell", "url": "https://wowhead.com/spell=123"},
            ],
        },
    ]

    merged = merge_article_linked_entities(pages)

    assert merged == [
        {
            "type": "spell",
            "id": 123,
            "name": "Example Spell",
            "url": "https://wowhead.com/spell=123",
            "source_urls": [
                "https://example.invalid/intro",
                "https://example.invalid/talents",
            ],
        }
    ]


def test_merge_article_build_references_dedupes_and_preserves_source_urls() -> None:
    pages = [
        {
            "guide": {"page_url": "https://example.invalid/intro"},
            "build_references": [
                {
                    "kind": "build_reference",
                    "reference_type": "wowhead_talent_calc_url",
                    "url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123",
                    "label": None,
                    "build_code": "ABC123",
                    "build_identity": {"kind": "build_identity", "status": "inferred"},
                },
            ],
        },
        {
            "guide": {"page_url": "https://example.invalid/talents"},
            "build_references": [
                {
                    "kind": "build_reference",
                    "reference_type": "wowhead_talent_calc_url",
                    "url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123",
                    "label": "Raid Build",
                    "build_code": "ABC123",
                    "build_identity": {"kind": "build_identity", "status": "inferred"},
                },
            ],
        },
    ]

    merged = merge_article_build_references(pages)

    assert merged == [
        {
            "kind": "build_reference",
            "reference_type": "wowhead_talent_calc_url",
            "url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123",
            "label": "Raid Build",
            "build_code": "ABC123",
            "build_identity": {"kind": "build_identity", "status": "inferred"},
            "source_urls": [
                "https://example.invalid/intro",
                "https://example.invalid/talents",
            ],
        }
    ]


def _guide_row(ref: str, score: int) -> dict:
    return article_candidate(ref=ref, name=ref, url=f"https://example.test/{ref}", score=score, reasons=[], provider="method")


def test_article_payloads_count_the_rows_shown_and_total_every_match() -> None:
    rows = [_guide_row("a", 30), _guide_row("b", 20)]

    cut = article_search_payload(query="q", search_query="q", matches=rows, limit=1)
    upstream = article_search_payload(query="q", search_query="q", matches=rows, limit=5, total_matches=7)
    whole = article_resolve_payload(provider_command="method", query="q", search_query="q", matches=rows, limit=5, resolved=False)

    assert (cut["count"], cut["total_matches"], cut["truncated"]) == (1, 2, True)
    assert (upstream["count"], upstream["total_matches"], upstream["truncated"]) == (2, 7, True)
    assert (whole["count"], whole["total_matches"], whole["truncated"]) == (2, 2, False)


def test_article_resolve_reports_low_confidence_for_a_tie_the_limit_hides() -> None:
    def confidence(*scores: int, limit: int = 5) -> str:
        rows = [_guide_row(f"guide-{index}", score) for index, score in enumerate(scores)]
        payload = article_resolve_payload(
            provider_command="method", query="q", search_query="q", matches=rows, limit=limit, resolved=False
        )
        return str(payload["confidence"])

    assert confidence(9, 9, 9) == "low"
    assert confidence(9, 9, limit=1) == "low"
    assert confidence(20, 9) == "medium"
    assert confidence() == "none"


def test_article_resolve_fallback_search_command_is_valid_shell() -> None:
    query = """kil'jaeden "raid" $HOME guide"""
    match = article_candidate(ref="Kil'jaeden", name="Kil'jaeden", url="https://example.invalid/k", score=20, reasons=[], provider="warcraft-wiki")
    payload = article_resolve_payload(
        provider_command="warcraft-wiki", query=query, search_query=query, matches=[match], limit=5, resolved=False
    )

    assert shlex.split(payload["fallback_search_command"]) == ["warcraft-wiki", "search", query]


def test_compact_never_cuts_a_follow_up_command() -> None:
    """Every runnable hand-off sits under a ``*command``/``*commands`` key, which --compact keeps whole."""
    from warcraft_core.output import compact_value

    follow_up = article_follow_up(provider_command="method", surface="guide", ref="x" * 400)
    cut: list[str] = []
    assert compact_value(follow_up, max_chars=40, cut=cut) == follow_up
    assert cut == []

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


def test_article_follow_up_uses_provider_command() -> None:
    follow_up = article_follow_up("method", "mistweaver-monk")

    assert follow_up == {
        "recommended_surface": "guide",
        "command": "method guide mistweaver-monk",
        "reason": "guide_summary",
        "alternative_commands": [
            "method guide-full mistweaver-monk",
            "method guide-export mistweaver-monk",
        ],
    }


def test_article_follow_up_supports_article_surfaces_and_quotes() -> None:
    follow_up = article_follow_up("warcraft-wiki", "World of Warcraft API", surface="article")

    assert follow_up == {
        "recommended_surface": "article",
        "command": "warcraft-wiki article 'World of Warcraft API'",
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
        provider_command="method",
    )

    assert row["id"] == "mistweaver-monk"
    assert row["ranking"]["score"] == 33
    assert row["follow_up"]["command"] == "method guide mistweaver-monk"


def test_sort_article_candidates_orders_by_score_then_name() -> None:
    rows = [
        article_candidate(
            ref="b",
            name="B Guide",
            url="https://example.invalid/b",
            score=10,
            reasons=["name_contains_query"],
            provider_command="method",
        ),
        article_candidate(
            ref="a",
            name="A Guide",
            url="https://example.invalid/a",
            score=30,
            reasons=["name_contains_query"],
            provider_command="method",
        ),
    ]

    sort_article_candidates(rows)

    assert rows[0]["id"] == "a"


def test_article_search_and_resolve_payloads_keep_contract_shape() -> None:
    rows = [
        article_candidate(
            ref="mistweaver-monk",
            name="Mistweaver Monk",
            url="https://www.method.gg/guides/mistweaver-monk",
            score=33,
            reasons=["exact_name"],
            provider_command="method",
        )
    ]

    search_payload = article_search_payload(
        query="mistweaver monk guide",
        search_query="mistweaver monk",
        results=rows,
        total_count=1,
    )
    resolve_payload = article_resolve_payload(
        provider_command="method",
        query="mistweaver monk guide",
        search_query="mistweaver monk",
        results=rows,
        total_count=1,
        resolved=True,
    )

    assert search_payload["results"][0]["id"] == "mistweaver-monk"
    assert resolve_payload["resolved"] is True
    assert resolve_payload["next_command"] == "method guide mistweaver-monk"
    assert resolve_payload["confidence"] == "high"


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


def test_merge_article_linked_entities_supports_custom_page_key() -> None:
    pages = [
        {
            "article_meta": {"page_url": "https://warcraft.wiki.gg/wiki/World_of_Warcraft_API"},
            "linked_entities": [
                {"type": "wiki_article", "id": "UIOBJECT_Frame", "name": "UIOBJECT Frame", "url": "https://warcraft.wiki.gg/wiki/UIOBJECT_Frame"},
            ],
        }
    ]

    merged = merge_article_linked_entities(pages, page_key="article_meta")

    assert merged[0]["source_urls"] == ["https://warcraft.wiki.gg/wiki/World_of_Warcraft_API"]


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


def test_article_resolve_fallback_search_command_is_valid_shell() -> None:
    query = """kil'jaeden "raid" $HOME guide"""
    payload = article_resolve_payload(
        provider_command="warcraft-wiki", query=query, search_query=query, results=[], total_count=0, resolved=False
    )

    assert shlex.split(payload["fallback_search_command"]) == ["warcraft-wiki", "search", query]


def test_compact_never_cuts_a_follow_up_command() -> None:
    """Every runnable hand-off sits under a ``*command``/``*commands`` key, which --compact keeps whole."""
    from warcraft_core.output import compact_value

    follow_up = article_follow_up(provider_command="method", surface="guide", ref="x" * 400)
    cut: list[str] = []
    assert compact_value(follow_up, max_chars=40, cut=cut) == follow_up
    assert cut == []

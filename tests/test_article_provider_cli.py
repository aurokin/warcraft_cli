from __future__ import annotations

from warcraft_content.article_provider_cli import (
    build_article_resolve_response,
    build_article_search_response,
    unsupported_guide_surface_message,
)


def test_build_article_search_response_includes_scope_hint_when_present() -> None:
    payload = build_article_search_response(
        query="patch notes",
        search_query="patch notes",
        results=[],
        total_count=0,
        scope_hint={"code": "patch_notes", "message": "out of scope"},
    )

    assert payload["count"] == 0
    assert payload["results"] == []
    assert payload["scope_hint"]["code"] == "patch_notes"


def test_build_article_resolve_response_includes_scope_hint_when_present() -> None:
    payload = build_article_resolve_response(
        provider_command="icy-veins",
        query="latest class changes",
        search_query="latest class changes",
        results=[],
        total_count=0,
        resolved=False,
        scope_hint={"code": "class_changes", "message": "out of scope"},
    )

    assert payload["resolved"] is False
    assert payload["count"] == 0
    assert payload["scope_hint"]["code"] == "class_changes"
    assert payload["fallback_search_command"] == "icy-veins search 'latest class changes'"


def test_unsupported_guide_surface_message_is_provider_specific() -> None:
    message = unsupported_guide_surface_message(
        provider_name="Method",
        slug="tier-list",
        content_family="unsupported_index",
    )

    assert message == "Unsupported Method guide surface for slug='tier-list' family='unsupported_index'."

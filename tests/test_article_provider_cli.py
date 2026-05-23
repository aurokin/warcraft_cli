from __future__ import annotations

import pytest
import typer
from warcraft_content.article_provider_cli import (
    build_article_resolve_response,
    build_article_search_response,
    fail_with_error,
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


def test_fail_with_error_emits_error_payload_and_exits() -> None:
    captured: list[tuple[dict[str, object], bool]] = []

    with pytest.raises(typer.Exit) as exc_info:
        fail_with_error(
            lambda payload, err: captured.append((payload, err)),
            code="invalid_guide_ref",
            message="bad ref",
            status=2,
        )

    assert exc_info.value.exit_code == 2
    assert captured == [
        (
            {"ok": False, "error": {"code": "invalid_guide_ref", "message": "bad ref"}},
            True,
        )
    ]


def test_unsupported_guide_surface_message_is_provider_specific() -> None:
    message = unsupported_guide_surface_message(
        provider_name="Method",
        slug="tier-list",
        content_family="unsupported_index",
    )

    assert message == "Unsupported Method guide surface for slug='tier-list' family='unsupported_index'."

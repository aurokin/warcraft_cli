from __future__ import annotations

import json

import pytest
import typer
from icy_veins_cli.main import app as icy_veins_app
from lorrgs_cli.main import app as lorrgs_app
from method_cli.main import app as method_app
from raidbots_cli.main import app as raidbots_app
from typer.testing import CliRunner
from warcraft_content.article_provider_cli import (
    build_article_resolve_response,
    build_article_search_response,
    unsupported_guide_surface_message,
)
from warcraft_core.envelope import ENVELOPE_KEYS


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


@pytest.mark.parametrize(
    ("app", "failing_args"),
    [
        (icy_veins_app, ["guide-query", "/nonexistent/bundle", "talents"]),
        (method_app, ["guide-query", "/nonexistent/bundle", "talents"]),
        (lorrgs_app, ["report-overview", "not a report"]),
        (raidbots_app, ["explain-input", "--text", " "]),
    ],
)
def test_envelopes_carry_only_the_envelope_keys(app: typer.Typer, failing_args: list[str]) -> None:
    """Payload keys live under ``data`` only; the deprecated top-level copies are gone."""
    runner = CliRunner()
    success = runner.invoke(app, ["doctor"])
    failure = runner.invoke(app, failing_args)

    assert success.exit_code == 0
    assert set(json.loads(success.stdout)) == ENVELOPE_KEYS - {"error"}
    assert failure.exit_code != 0
    assert set(json.loads(failure.stderr)) == ENVELOPE_KEYS

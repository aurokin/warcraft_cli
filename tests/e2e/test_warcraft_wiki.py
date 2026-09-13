"""End-to-end journeys for the ``warcraft-wiki`` binary against the live warcraft.wiki.gg site.

Article titles are discovered from ``warcraft-wiki search``/``resolve`` rather than hard-coded, so
the file follows the wiki when it moves a page. (It did: the API reference moved out of the
main-namespace ``API Foo`` titles into the real ``API:`` namespace, which is why the API journeys
read the resolved title instead of asserting a literal one.) Every command in
``docs/reference/warcraft-wiki.md`` is exercised, plus the documented error journeys and the global
output flags.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

import pytest

from tests.e2e import pins
from tests.e2e.harness import (
    EXIT_GENERIC,
    EXIT_NETWORK,
    EXIT_NOT_FOUND,
    Result,
    dead_proxy_env,
    payload_or_legacy,
    run,
    run_raw,
)

BINARY = "warcraft-wiki"
PROVIDER = "warcraft-wiki"
# Widget script handlers are permanent UI vocabulary, like the ids in tests/e2e/pins.py.
UI_HANDLER_QUERY = "OnKeyDown"
API_REFERENCE_FAMILIES = {"api_function", "framework_page", "xml_schema", "cvar", "api_changes"}
EVENT_REFERENCE_FAMILIES = {"ui_handler", "framework_page"}


def assert_data_mirrors_legacy(result: Result, *keys: str) -> None:
    """``data`` carries the payload and the deprecated top-level copies agree with it."""
    assert result.data, f"data slot is empty\n{result.describe()}"
    for key in keys:
        assert key in result.data, f"data is missing {key!r}\n{result.describe()}"
        assert payload_or_legacy(result, key) == result.data[key], (
            f"legacy top-level {key!r} disagrees with data\n{result.describe()}"
        )


@cache
def api_search() -> Result:
    return run(BINARY, "search", pins.WIKI_API_FUNCTION, "--limit", "5")


@cache
def api_page_title() -> str:
    """The canonical title of the pinned API function page, whatever namespace it lives in."""
    results = api_search().data["results"]
    assert results, f"search found nothing for {pins.WIKI_API_FUNCTION!r}\n{api_search().describe()}"
    return str(results[0]["id"])


@cache
def lore_article() -> Result:
    return run(BINARY, "article", pins.WIKI_LORE_QUERY)


def test_doctor_reports_every_documented_command_ready(require) -> None:
    require(PROVIDER)
    result = run(BINARY, "doctor")

    assert result.data["status"] == "ready"
    capabilities = result.data["capabilities"]
    assert {
        "search",
        "resolve",
        "article",
        "article_full",
        "api",
        "api_full",
        "event",
        "event_full",
        "article_export",
        "article_query",
    } <= set(capabilities)
    assert set(capabilities.values()) == {"ready"}
    assert result.data["cache"]["enabled"] is True
    assert_data_mirrors_legacy(result, "capabilities", "cache")


def test_search_puts_the_api_page_at_the_top_for_an_api_query(require) -> None:
    require(PROVIDER)
    result = api_search()

    assert result.data["count"] >= 1
    first = result.data["results"][0]
    assert first["metadata"]["content_family"] == "api_function"
    assert pins.WIKI_API_FUNCTION.lower() in first["id"].lower()
    assert first["follow_up"]["recommended_command"].startswith(f"{BINARY} article ")
    assert_data_mirrors_legacy(result, "results", "count", "search_query")


def test_resolve_strips_the_family_hint_and_names_the_article_command(require) -> None:
    require(PROVIDER)
    result = run(BINARY, "resolve", f"lore {pins.WIKI_LORE_QUERY}", "--limit", "5")

    assert result.data["search_query"].lower() == pins.WIKI_LORE_QUERY.lower()
    assert result.data["excluded_terms"] == ["lore"]
    assert result.data["resolved"] is True
    assert result.data["match"]["id"] == pins.WIKI_LORE_QUERY
    assert result.data["next_command"] == f"{BINARY} article '{pins.WIKI_LORE_QUERY}'"


def test_article_returns_classified_text_headings_and_navigation(require) -> None:
    require(PROVIDER)
    result = lore_article()

    article = result.data["article"]
    assert article["title"] == pins.WIKI_LORE_QUERY
    assert article["content_family"] == "faction_reference"
    assert article["page_url"].endswith("/wiki/Argent_Dawn")
    content = result.data["content"]
    assert content["section_count"] >= 5
    assert content["text"].strip(), "article text is empty"
    # The wiki chrome must not leak into the extracted prose.
    assert "Main Menu" not in content["text"]
    assert result.data["navigation"]["count"] >= 3
    assert result.data["linked_entities"]["count"] >= 1
    assert all("action=edit" not in row["url"] for row in result.data["linked_entities"]["items"])
    assert result.data["reference"]["content_family"] == "faction_reference"
    assert result.payload["provenance"]["page"] == article["page_url"]
    assert_data_mirrors_legacy(result, "article", "content", "navigation", "reference")


def test_article_full_returns_every_section_not_just_the_preview(require) -> None:
    require(PROVIDER)
    result = run(BINARY, "article-full", pins.WIKI_LORE_QUERY)

    assert len(result.data["pages"]) == 1
    sections = result.data["pages"][0]["article"]["sections"]
    assert len(sections) == lore_article().data["content"]["section_count"]
    assert all(row["title"] for row in sections)
    assert any(row["text"].strip() for row in sections)


@pytest.mark.parametrize("command", ["api", "api-full"])
def test_api_commands_resolve_the_pinned_function(require, command: str) -> None:
    require(PROVIDER)
    result = run(BINARY, command, pins.WIKI_API_FUNCTION)

    assert result.data["article"]["title"] == api_page_title()
    assert result.data["article"]["content_family"] == "api_function"
    assert result.data["resolved_surface"] == "api"
    reference = result.data["reference"]
    assert reference["programming_reference"] is True
    assert pins.WIKI_API_FUNCTION in (reference["signature"] or "")
    assert reference["arguments"], "the API page lost its argument table"


def test_api_resolves_the_xml_schema_reference(require) -> None:
    require(PROVIDER)
    result = run(BINARY, "api", "XML schema")

    assert result.data["article"]["content_family"] == "xml_schema"
    assert result.data["resolved_surface"] == "api"
    assert result.data["reference"]["programming_reference"] is True


@pytest.mark.parametrize("command", ["event", "event-full"])
def test_event_commands_resolve_a_ui_handler(require, command: str) -> None:
    require(PROVIDER)
    result = run(BINARY, command, UI_HANDLER_QUERY)

    assert result.data["article"]["content_family"] == "ui_handler"
    assert result.data["resolved_surface"] == "event"
    assert UI_HANDLER_QUERY in result.data["article"]["title"]
    assert result.data["reference"]["programming_reference"] is True


def test_event_resolves_the_pinned_game_event_to_a_reference_page(require) -> None:
    require(PROVIDER)
    result = run(BINARY, "event", pins.WIKI_EVENT)

    assert result.data["resolved_surface"] == "event"
    assert result.data["article"]["content_family"] in EVENT_REFERENCE_FAMILIES
    assert result.data["reference"]["programming_reference"] is True
    assert result.data["content"]["text"].strip(), "the event reference page has no text"


def test_article_export_writes_a_bundle_that_article_query_answers_offline(require, out_dir: Path) -> None:
    require(PROVIDER)
    bundle = out_dir / "wiki-bundle"
    export = run(BINARY, "article-export", pins.WIKI_LORE_QUERY, "--out", str(bundle))

    assert Path(export.data["output_dir"]) == bundle
    counts = export.data["counts"]
    assert counts["pages"] == 1
    assert counts["sections"] >= 5
    for name in ("manifest.json", "guide.json", "pages.jsonl", "sections.jsonl", "linked-entities.jsonl"):
        assert (bundle / name).is_file(), f"{name} is missing from the export"
    assert sum(1 for _ in (bundle / "sections.jsonl").open()) == counts["sections"]

    query = run(BINARY, "article-query", str(bundle), "scarlet", "--limit", "3", env=dead_proxy_env())
    assert query.data["bundle"] == str(bundle)
    assert query.data["count"] >= 1
    sections = query.data["matches"]["sections"]
    assert sections, "no section matched a term that appears in the article"
    assert all("scarlet" in (row["text"] or row["html"]).lower() for row in sections)


def test_article_query_honours_kind_and_section_title_filters(require, out_dir: Path) -> None:
    require(PROVIDER)
    bundle = out_dir / "wiki-bundle"
    run(BINARY, "article-export", pins.WIKI_LORE_QUERY, "--out", str(bundle))

    only_sections = run(BINARY, "article-query", str(bundle), "reputation", "--kind", "sections", env=dead_proxy_env())
    assert only_sections.data["match_counts"]["sections"] >= 1
    assert only_sections.data["match_counts"]["linked_entities"] == 0

    titled = run(BINARY, "article-query", str(bundle), "argent", "--section-title", "organization", env=dead_proxy_env())
    assert titled.data["matches"]["sections"], "no section titled 'Organization' matched"
    assert all("organization" in row["title"].lower() for row in titled.data["matches"]["sections"])


def test_a_missing_article_is_a_not_found_envelope(require) -> None:
    require(PROVIDER)
    run(BINARY, "article", "Zzz No Such Warcraft Wiki Page 90210", expect=EXIT_NOT_FOUND, error_code="not_found")


@pytest.mark.parametrize(
    ("command", "error_code"),
    [("api", "invalid_api_ref"), ("event", "invalid_event_ref")],
)
def test_an_unresolvable_typed_reference_reports_its_documented_code(require, command: str, error_code: str) -> None:
    require(PROVIDER)
    run(BINARY, command, "zzz-not-a-real-reference-90210", expect=EXIT_GENERIC, error_code=error_code)


def test_network_failure_is_an_exit_5_envelope(require) -> None:
    require(PROVIDER)
    result = run(BINARY, "article", "Zzz Network Probe 90210", expect=EXIT_NETWORK, env=dead_proxy_env())

    assert result.error_code in {"network_error", "timeout", "upstream_error"}
    assert result.stdout == ""


def test_a_repeated_article_fetch_is_served_from_the_session_cache(require) -> None:
    require(PROVIDER)
    warm = lore_article()
    cached = run(BINARY, "article", pins.WIKI_LORE_QUERY, env=dead_proxy_env())

    assert cached.data["article"] == warm.data["article"]
    assert cached.data["content"]["section_count"] == warm.data["content"]["section_count"]


def test_fields_and_compact_shape_the_payload(require) -> None:
    require(PROVIDER)
    fields = run_raw(BINARY, "--fields", "data.article.title,data.content.section_count", "article", pins.WIKI_LORE_QUERY)
    assert fields.exit_code == 0, fields.describe()
    payload: dict[str, Any] = json.loads(fields.stdout)
    assert payload == {
        "data": {
            "article": {"title": pins.WIKI_LORE_QUERY},
            "content": {"section_count": lore_article().data["content"]["section_count"]},
        }
    }

    compact = run(BINARY, "--compact", "--compact-max-chars", "80", "article", pins.WIKI_LORE_QUERY)
    text = compact.data["content"]["text"]
    assert text.endswith("...")
    assert len(text) <= 90
    assert len(lore_article().data["content"]["text"]) > len(text)


def test_missing_argument_is_a_usage_error(require) -> None:
    require(PROVIDER)
    result = run_raw(BINARY, "article")

    assert result.exit_code == 2, result.describe()
    assert "Traceback" not in result.stderr

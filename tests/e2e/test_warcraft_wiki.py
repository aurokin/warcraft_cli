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
import shlex
from functools import cache
from pathlib import Path
from typing import Any

import pytest

from tests.e2e import pins
from tests.e2e.harness import (
    EXIT_NETWORK,
    EXIT_NOT_FOUND,
    Result,
    dead_proxy_env,
    run,
    run_raw,
)

BINARY = "warcraft-wiki"
PROVIDER = "warcraft-wiki"
# Widget script handlers are permanent UI vocabulary, like the ids in tests/e2e/pins.py.
UI_HANDLER_QUERY = "OnKeyDown"
# Game events and the wiki page that documents each one. These four are the events every addon
# registers first, and the wiki has carried their pages for a decade. COMBAT_LOG_EVENT_UNFILTERED
# keeps its pre-"UNFILTERED" title and serves the long name as a redirect, which is exactly the
# case a title-matching lookup has to get right.
GAME_EVENT_PAGES: tuple[tuple[str, str], ...] = (
    ("PLAYER_LOGIN", "Event:PLAYER LOGIN"),
    ("PLAYER_ENTERING_WORLD", "Event:PLAYER ENTERING WORLD"),
    ("COMBAT_LOG_EVENT_UNFILTERED", "Event:COMBAT LOG EVENT"),
    ("UNIT_HEALTH", "Event:UNIT HEALTH"),
)
# Query prefixes `resolve` strips, the article each cleaned query has to land on, the family the
# search row can claim from a title and a snippet alone, and the family the fetched page is
# classified as. The two differ where the title carries no signal: a zone or a character page reads
# as a plain article until the page itself is read, and saying so beats guessing.
RESOLVE_FAMILY_CASES: tuple[tuple[str, str, str, str, str], ...] = (
    ("class", "druid", "Druid", "class_reference", "class_reference"),
    ("zone", "elwynn forest", "Elwynn Forest", "general_article", "zone_reference"),
    ("profession", "alchemy", "Alchemy", "profession_reference", "profession_reference"),
    ("lore", "jaina proudmoore", "Jaina Proudmoore", "general_article", "lore_reference"),
)


def assert_data_holds(result: Result, *keys: str) -> None:
    """The envelope slot ``data`` carries the payload block each journey goes on to read."""
    assert result.data, f"data slot is empty\n{result.describe()}"
    for key in keys:
        assert key in result.data, f"data is missing {key!r}\n{result.describe()}"


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
    assert_data_holds(result, "capabilities", "cache")


def test_search_puts_the_api_page_at_the_top_for_an_api_query(require) -> None:
    require(PROVIDER)
    result = api_search()

    assert result.data["count"] >= 1
    first = result.data["results"][0]
    assert first["metadata"]["content_family"] == "api_function"
    assert pins.WIKI_API_FUNCTION.lower() in first["id"].lower()
    assert first["follow_up"]["recommended_command"].startswith(f"{BINARY} article ")
    assert_data_holds(result, "results", "count", "search_query")


@pytest.mark.parametrize(("hint", "name", "expected_title", "search_family", "article_family"), RESOLVE_FAMILY_CASES)
def test_resolve_strips_the_family_hint_and_its_article_command_returns_that_page(
    require, hint: str, name: str, expected_title: str, search_family: str, article_family: str
) -> None:
    """``resolve`` drops the family word, finds the page, and the command it prints fetches it."""
    require(PROVIDER)
    resolved = run(BINARY, "resolve", f"{hint} {name}", "--limit", "10")

    assert resolved.data["search_query"] == name
    assert resolved.data["excluded_terms"] == [hint]
    assert resolved.data["resolved"] is True
    assert resolved.data["match"]["id"] == expected_title, resolved.describe()
    assert resolved.data["match"]["metadata"]["content_family"] == search_family, resolved.describe()

    parts = shlex.split(resolved.data["next_command"])
    assert parts == [BINARY, "article", expected_title], resolved.describe()
    article = run(BINARY, *parts[1:])
    assert article.data["article"]["title"] == expected_title, article.describe()
    assert article.data["article"]["content_family"] == article_family, article.describe()
    assert article.data["reference"]["content_family"] == article_family, article.describe()


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
    assert_data_holds(result, "article", "content", "navigation", "reference")


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


@pytest.mark.parametrize(
    ("query", "expected_family"),
    [("XML schema", "xml_schema"), ("World of Warcraft API", "framework_page")],
)
def test_api_resolves_the_reference_pages_that_are_not_functions(require, query: str, expected_family: str) -> None:
    require(PROVIDER)
    result = run(BINARY, "api", query)

    assert result.data["article"]["content_family"] == expected_family, result.describe()
    assert result.data["resolved_surface"] == "api"
    assert result.data["reference"]["programming_reference"] is True


@pytest.mark.parametrize(
    ("article_ref", "expected_title", "expected_family"),
    [
        ("Patch 2.1.0/API changes", "Patch 2.1.0/API changes", "api_changes"),
        ("Create a WoW AddOn in 15 Minutes", "Create a WoW AddOn in 15 Minutes", "howto_programming"),
        # The wiki serves this one as a redirect; the payload must name the page it actually read.
        ("Legion", "World of Warcraft: Legion", "expansion_reference"),
    ],
)
def test_article_classifies_the_page_it_followed_a_ref_to(
    require, article_ref: str, expected_title: str, expected_family: str
) -> None:
    require(PROVIDER)
    result = run(BINARY, "article", article_ref)

    assert result.data["article"]["title"] == expected_title, result.describe()
    assert result.data["article"]["content_family"] == expected_family, result.describe()
    assert result.data["reference"]["content_family"] == expected_family, result.describe()
    assert result.data["content"]["text"].strip(), "the article has no extracted prose"
    assert "Main Menu" not in result.data["content"]["text"], "wiki chrome leaked into the prose"


@pytest.mark.parametrize("command", ["event", "event-full"])
def test_event_commands_resolve_a_ui_handler(require, command: str) -> None:
    require(PROVIDER)
    result = run(BINARY, command, UI_HANDLER_QUERY)

    assert result.data["article"]["content_family"] == "ui_handler"
    assert result.data["resolved_surface"] == "event"
    assert UI_HANDLER_QUERY in result.data["article"]["title"]
    assert result.data["reference"]["programming_reference"] is True


@pytest.mark.parametrize(("event_name", "expected_title"), GAME_EVENT_PAGES)
def test_event_returns_the_page_that_documents_that_event(require, event_name: str, expected_title: str) -> None:
    """``event <NAME>`` must land on that event's own page, not on whatever page mentions it.

    Ranking used to hand every upstream row a positional score, so ``event PLAYER_LOGIN`` answered
    with ``UIHANDLER OnEvent`` and ``event ENCOUNTER_START`` with the ``Events`` index — ok: true,
    wrong page. Asserting the title is the only thing that catches that.
    """
    require(PROVIDER)
    result = run(BINARY, "event", event_name)

    assert result.data["article"]["title"] == expected_title, result.describe()
    assert result.data["article"]["content_family"] == "event_reference", result.describe()
    assert result.data["resolved_surface"] == "event"
    assert result.data["resolved_from"] == "direct_fetch", "the exact page must be fetched, not searched for"
    assert result.data["article"]["page_url"].endswith(f"/wiki/{expected_title.replace(' ', '_')}")
    assert result.data["content"]["text"].strip(), "the event reference page has no text"


def test_event_full_returns_the_whole_event_page(require) -> None:
    require(PROVIDER)
    event_name, expected_title = GAME_EVENT_PAGES[0]
    result = run(BINARY, "event-full", event_name)

    assert result.data["article"]["title"] == expected_title, result.describe()
    assert result.data["resolved_surface"] == "event"
    sections = result.data["pages"][0]["article"]["sections"]
    assert sections and all(row["title"] for row in sections), result.describe()


def test_search_ranks_the_event_page_above_the_pages_that_merely_mention_it(require) -> None:
    require(PROVIDER)
    event_name, expected_title = GAME_EVENT_PAGES[0]
    result = run(BINARY, "search", event_name, "--limit", "5")

    top = result.data["results"][0]
    assert top["id"] == expected_title, result.describe()
    assert "exact_event_title" in top["ranking"]["match_reasons"], result.describe()
    # Whatever ranked below it does not answer the query; it must not come close on score.
    assert all(row["ranking"]["score"] < top["ranking"]["score"] - 18 for row in result.data["results"][1:])


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


@pytest.mark.parametrize("command", ["api", "event"])
@pytest.mark.parametrize("query", ["zzz-not-a-real-reference-90210", "NOT_A_REAL_EVENT_XYZ"])
def test_an_unresolvable_typed_reference_is_not_found(require, command: str, query: str) -> None:
    """A typed lookup that cannot find its page must fail, not fall back to an unrelated article."""
    require(PROVIDER)
    result = run(BINARY, command, query, expect=EXIT_NOT_FOUND, error_code="not_found")
    assert result.payload["error"]["details"]["surface"] == command, result.describe()


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

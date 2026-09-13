"""End-to-end journeys for the ``method`` binary against the live method.gg site.

The guide slug is discovered from ``method search`` on the shared guide query in
``tests/e2e/pins.py``, so nothing here rots when Method renames or retires a guide. Every command
in ``docs/reference/method.md`` is exercised, plus the documented error journeys and the global
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

BINARY = "method"
PROVIDER = "method"
# Method publishes no tier lists through the supported guide families; doctor lists the root as
# intentionally out of scope, so it is the documented way to reach the scope-hint branch.
OUT_OF_SCOPE_QUERY = "tier list"
UNSUPPORTED_SURFACE_SLUG = "tier-list"


def assert_data_mirrors_legacy(result: Result, *keys: str) -> None:
    """``data`` carries the payload and the deprecated top-level copies agree with it."""
    assert result.data, f"data slot is empty\n{result.describe()}"
    for key in keys:
        assert key in result.data, f"data is missing {key!r}\n{result.describe()}"
        assert payload_or_legacy(result, key) == result.data[key], (
            f"legacy top-level {key!r} disagrees with data\n{result.describe()}"
        )


@cache
def guide_search() -> Result:
    return run(BINARY, "search", pins.GUIDE_QUERY, "--limit", "5")


@cache
def guide_slug() -> str:
    results = guide_search().data["results"]
    assert results, f"search found no Method guide for {pins.GUIDE_QUERY!r}\n{guide_search().describe()}"
    return str(results[0]["id"])


@cache
def guide_page() -> Result:
    return run(BINARY, "guide", guide_slug())


def test_doctor_reports_every_documented_command_ready(require) -> None:
    require(PROVIDER)
    result = run(BINARY, "doctor")

    assert result.data["status"] == "ready"
    capabilities = result.data["capabilities"]
    assert {"search", "resolve", "guide", "guide_full", "guide_export", "guide_query"} <= set(capabilities)
    assert set(capabilities.values()) == {"ready"}
    scope = result.data["supported_scope"]
    assert "class_guide" in scope["content_families"]
    assert UNSUPPORTED_SURFACE_SLUG in scope["unsupported_roots"]
    assert_data_mirrors_legacy(result, "capabilities", "supported_scope")


def test_search_finds_a_real_guide_and_names_the_follow_up(require) -> None:
    require(PROVIDER)
    result = guide_search()

    assert result.data["count"] >= 1
    first = result.data["results"][0]
    assert first["entity_type"] == "guide"
    assert first["metadata"]["content_family"] in {"class_guide", "profession_guide", "delve_guide", "reputation_guide", "article_guide"}
    assert first["url"].startswith("https://www.method.gg/guides/")
    assert first["follow_up"]["recommended_command"] == f"{BINARY} guide {first['id']}"
    assert result.payload["provenance"]["sitemap_url"].endswith("sitemap.xml")
    assert_data_mirrors_legacy(result, "results", "count", "search_query")


def test_search_outside_the_supported_families_returns_a_scope_hint(require) -> None:
    require(PROVIDER)
    result = run(BINARY, "search", OUT_OF_SCOPE_QUERY, "--limit", "5")

    assert result.data["count"] == 0
    assert result.data["results"] == []
    assert result.data["scope_hint"]["code"]


def test_resolve_picks_the_search_winner_and_hands_over_the_next_command(require) -> None:
    require(PROVIDER)
    result = run(BINARY, "resolve", pins.GUIDE_QUERY, "--limit", "5")

    assert result.data["resolved"] is True
    assert result.data["match"]["id"] == guide_slug()
    assert result.data["next_command"] == f"{BINARY} guide {guide_slug()}"
    assert result.data["candidates"], "resolve dropped the candidate list"


def test_guide_returns_titled_sections_navigation_and_linked_entities(require) -> None:
    require(PROVIDER)
    result = guide_page()

    guide = result.data["guide"]
    assert guide["slug"] == guide_slug()
    assert guide["supported_surface"] is True
    assert guide["section_title"], "the active navigation page has no title"
    assert guide["page_url"].startswith("https://www.method.gg/guides/")
    # Method guides are multi-page: the family navigation is the only way to reach the other pages.
    assert result.data["navigation"]["count"] >= 2
    assert all(item["title"] and item["url"] for item in result.data["navigation"]["items"])
    article = result.data["article"]
    assert article["section_count"] >= 1
    assert article["text"].strip(), "article text is empty"
    assert result.data["linked_entities"]["count"] >= 1
    assert result.payload["provenance"]["page"] == guide["page_url"]
    assert_data_mirrors_legacy(result, "guide", "navigation", "article", "linked_entities")


def test_guide_full_fetches_and_merges_every_navigation_page(require) -> None:
    require(PROVIDER)
    result = run(BINARY, "guide-full", guide_slug())

    page_count = result.data["guide"]["page_count"]
    assert page_count >= 2
    assert len(result.data["pages"]) == page_count
    assert len(result.data["citations"]["pages"]) == page_count
    assert all(page["article"]["sections"] for page in result.data["pages"])
    # Merging every page can only add entities to what the first page alone carried.
    assert result.data["linked_entities"]["count"] >= guide_page().data["linked_entities"]["count"]


def test_guide_export_writes_a_bundle_that_guide_query_answers_offline(require, out_dir: Path) -> None:
    require(PROVIDER)
    bundle = out_dir / "method-bundle"
    export = run(BINARY, "guide-export", guide_slug(), "--out", str(bundle))

    assert Path(export.data["output_dir"]) == bundle
    counts = export.data["counts"]
    assert counts["pages"] >= 2
    assert counts["sections"] >= 1
    for name in ("manifest.json", "guide.json", "pages.jsonl", "sections.jsonl", "linked-entities.jsonl"):
        assert (bundle / name).is_file(), f"{name} is missing from the export"
    assert sum(1 for _ in (bundle / "sections.jsonl").open()) == counts["sections"]

    # The bundle has to answer without the network; a dead proxy proves nothing is fetched.
    query = run(BINARY, "guide-query", str(bundle), "renewing mist", "--limit", "3", env=dead_proxy_env())
    assert query.data["count"] >= 1
    sections = query.data["matches"]["sections"]
    assert sections, "no section matched a term that appears in the guide"
    assert all("renewing" in (row["text"] or row["html"]).lower() for row in sections)
    assert all(row["kind"] for row in query.data["top"])


def test_guide_query_honours_kind_and_section_title_filters(require, out_dir: Path) -> None:
    require(PROVIDER)
    bundle = out_dir / "method-bundle"
    run(BINARY, "guide-export", guide_slug(), "--out", str(bundle))

    only_navigation = run(BINARY, "guide-query", str(bundle), "talents", "--kind", "navigation", env=dead_proxy_env())
    assert only_navigation.data["match_counts"]["navigation"] >= 1
    assert only_navigation.data["matches"]["sections"] == []
    assert all(row["url"] for row in only_navigation.data["matches"]["navigation"])

    titled = run(BINARY, "guide-query", str(bundle), "mana", "--section-title", "rotation", env=dead_proxy_env())
    assert all("rotation" in row["title"].lower() for row in titled.data["matches"]["sections"])


def test_unknown_guide_slug_is_a_not_found_envelope(require) -> None:
    require(PROVIDER)
    result = run(BINARY, "guide", f"zzz-{guide_slug()}", expect=EXIT_NOT_FOUND, error_code="not_found")

    assert result.payload["error"]["details"]["status_code"] == 404


def test_unsupported_guide_surface_is_a_generic_failure(require) -> None:
    require(PROVIDER)
    run(BINARY, "guide", UNSUPPORTED_SURFACE_SLUG, expect=EXIT_GENERIC, error_code="unsupported_guide_surface")


def test_network_failure_is_an_exit_5_envelope(require) -> None:
    require(PROVIDER)
    # A guide page the session cache has never seen, so the fetch has to leave the process.
    result = run(BINARY, "guide", f"{guide_slug()}-network-probe", expect=EXIT_NETWORK, env=dead_proxy_env())

    assert result.error_code in {"network_error", "timeout", "upstream_error"}
    assert result.stdout == ""


def test_a_repeated_guide_fetch_is_served_from_the_session_cache(require) -> None:
    require(PROVIDER)
    warm = guide_page()
    cached = run(BINARY, "guide", guide_slug(), env=dead_proxy_env())

    assert cached.data["guide"] == warm.data["guide"]
    assert cached.data["article"]["section_count"] == warm.data["article"]["section_count"]


def test_fields_and_compact_shape_the_payload(require) -> None:
    require(PROVIDER)
    # --fields prunes the envelope down to the requested paths, so this journey reads the raw JSON
    # instead of the full-envelope contract the harness enforces elsewhere.
    fields = run_raw(BINARY, "--fields", "data.guide.slug,data.article.section_count", "guide", guide_slug())
    assert fields.exit_code == 0, fields.describe()
    payload: dict[str, Any] = json.loads(fields.stdout)
    assert payload == {"data": {"guide": {"slug": guide_slug()}, "article": {"section_count": guide_page().data["article"]["section_count"]}}}

    compact = run(BINARY, "--compact", "--compact-max-chars", "80", "guide", guide_slug())
    text = compact.data["article"]["text"]
    assert text.endswith("...")
    assert len(text) <= 90
    assert len(guide_page().data["article"]["text"]) > len(text)


def test_missing_argument_is_a_usage_error(require) -> None:
    require(PROVIDER)
    result = run_raw(BINARY, "guide")

    assert result.exit_code == 2, result.describe()
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize("command", ["guide", "guide-full", "guide-export"])
def test_every_guide_command_rejects_an_empty_reference(require, command: str) -> None:
    require(PROVIDER)
    run(BINARY, command, "   ", expect=EXIT_GENERIC, error_code="invalid_guide_ref")

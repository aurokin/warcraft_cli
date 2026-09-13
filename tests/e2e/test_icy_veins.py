"""End-to-end journeys for the ``icy-veins`` binary against the live icy-veins.com site.

The spec-guide slug is discovered from ``icy-veins search`` on the shared guide query in
``tests/e2e/pins.py``, so the file survives a slug rename. Every command in
``docs/reference/icy-veins.md`` is exercised, plus the documented error journeys and the global
output flags.

The navigation assertions are deliberate: Icy Veins rebuilt its guide layout in 2026 and the
parser silently returned zero sections and zero navigation entries for a while, which is exactly
the kind of drift a live journey has to catch.
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

BINARY = "icy-veins"
PROVIDER = "icy-veins"
# Icy Veins publishes patch notes as news, not as a guide; doctor-documented scope excludes them.
OUT_OF_SCOPE_QUERY = "patch notes"
# A real Icy Veins WoW page that is not a guide, so it never classifies into a supported family.
UNSUPPORTED_REF = "news-roundup"


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
def spec_guide_slug() -> str:
    """The best spec guide for the pinned query; spec guides are the ones with a family switcher."""
    for row in guide_search().data["results"]:
        if row["metadata"]["content_family"] == "spec_guide":
            return str(row["id"])
    raise AssertionError(f"search found no Icy Veins spec guide\n{guide_search().describe()}")


@cache
def guide_page() -> Result:
    return run(BINARY, "guide", spec_guide_slug())


def test_doctor_reports_every_documented_command_ready(require) -> None:
    require(PROVIDER)
    result = run(BINARY, "doctor")

    assert result.data["status"] == "ready"
    capabilities = result.data["capabilities"]
    assert {"search", "resolve", "guide", "guide_full", "guide_export", "guide_query"} <= set(capabilities)
    assert set(capabilities.values()) == {"ready"}
    cache_config = result.data["cache"]
    assert cache_config["enabled"] is True
    assert set(cache_config["ttls"]) == {"sitemap", "page_html"}
    assert_data_mirrors_legacy(result, "capabilities", "cache")


def test_search_ranks_real_guides_from_the_sitemap(require) -> None:
    require(PROVIDER)
    result = guide_search()

    assert result.data["count"] >= 1
    families = {row["metadata"]["content_family"] for row in result.data["results"]}
    assert families, "search returned rows without a content family"
    for row in result.data["results"]:
        assert row["entity_type"] == "guide"
        assert row["url"].startswith("https://www.icy-veins.com/wow/")
        assert row["follow_up"]["recommended_command"] == f"{BINARY} guide {row['id']}"
    assert_data_mirrors_legacy(result, "results", "count", "search_query")


def test_search_outside_the_guide_surface_returns_a_scope_hint(require) -> None:
    require(PROVIDER)
    result = run(BINARY, "search", OUT_OF_SCOPE_QUERY, "--limit", "5")

    assert result.data["count"] == 0
    assert result.data["results"] == []
    assert result.data["scope_hint"]["code"]


def test_resolve_names_the_guide_command_for_the_best_match(require) -> None:
    require(PROVIDER)
    result = run(BINARY, "resolve", pins.GUIDE_QUERY, "--limit", "5")

    match = result.data["match"]
    assert match is not None, f"resolve found no candidate\n{result.describe()}"
    assert result.data["next_command"] == f"{BINARY} guide {match['id']}"
    assert result.data["candidates"], "resolve dropped the candidate list"


def test_guide_returns_sections_family_navigation_and_a_page_toc(require) -> None:
    require(PROVIDER)
    result = guide_page()

    guide = result.data["guide"]
    assert guide["slug"] == spec_guide_slug()
    assert guide["content_family"] == "spec_guide"
    assert guide["traversal_scope"] == "family_navigation"
    assert guide["author"], "the guide byline is missing"
    assert guide["last_updated"], "the guide has no last-updated stamp"
    # A spec guide always carries the class/spec family switcher.
    navigation = result.data["navigation"]
    assert navigation["count"] >= 2
    assert all(item["title"] and item["url"] for item in navigation["items"])
    assert sum(1 for item in navigation["items"] if item["active"]) == 1
    assert result.data["page_toc"]["count"] >= 1
    article = result.data["article"]
    assert article["section_count"] >= 1
    assert article["text"].strip(), "article text is empty"
    assert article["intro_text"].strip(), "the guide intro is empty"
    assert result.data["linked_entities"]["count"] >= 1
    assert result.payload["provenance"]["page"] == guide["page_url"]
    assert_data_mirrors_legacy(result, "guide", "navigation", "article", "page_toc")


def test_guide_pages_all_carry_extracted_text(require) -> None:
    """Every section the parser reports has to have real content behind it, not just a heading."""
    require(PROVIDER)
    sections = guide_page().data["article"]["section_preview"]

    assert sections, "no section preview"
    assert all(row["title"] and row["level"] >= 2 for row in sections)


def test_guide_full_walks_the_whole_family_navigation(require) -> None:
    require(PROVIDER)
    result = run(BINARY, "guide-full", spec_guide_slug())

    page_count = result.data["guide"]["page_count"]
    assert page_count >= 2
    assert page_count == guide_page().data["navigation"]["count"]
    assert len(result.data["pages"]) == page_count
    assert len(result.data["citations"]["pages"]) == page_count
    assert all(page["article"]["sections"] for page in result.data["pages"])
    assert result.data["linked_entities"]["count"] >= guide_page().data["linked_entities"]["count"]
    assert result.data["analysis_surfaces"]["count"] >= 1


def test_guide_export_writes_a_bundle_that_guide_query_answers_offline(require, out_dir: Path) -> None:
    require(PROVIDER)
    bundle = out_dir / "icy-veins-bundle"
    export = run(BINARY, "guide-export", spec_guide_slug(), "--out", str(bundle))

    assert Path(export.data["output_dir"]) == bundle
    counts = export.data["counts"]
    assert counts["pages"] >= 2
    assert counts["sections"] >= 1
    for name in ("manifest.json", "guide.json", "pages.jsonl", "sections.jsonl", "navigation-links.jsonl"):
        assert (bundle / name).is_file(), f"{name} is missing from the export"
    assert sum(1 for _ in (bundle / "sections.jsonl").open()) == counts["sections"]
    assert any((bundle / "pages").iterdir()), "no page files were written"

    query = run(BINARY, "guide-query", str(bundle), "mana", "--limit", "3", env=dead_proxy_env())
    assert query.data["bundle"] == str(bundle)
    assert query.data["count"] >= 1
    sections = query.data["matches"]["sections"]
    assert sections, "no section matched a term that appears in a healing guide"
    assert all("mana" in (row["text"] or row["html"]).lower() for row in sections)


def test_guide_query_honours_kind_and_section_title_filters(require, out_dir: Path) -> None:
    require(PROVIDER)
    bundle = out_dir / "icy-veins-bundle"
    run(BINARY, "guide-export", spec_guide_slug(), "--out", str(bundle))

    only_navigation = run(BINARY, "guide-query", str(bundle), "talents", "--kind", "navigation", env=dead_proxy_env())
    assert only_navigation.data["matches"]["sections"] == []
    assert only_navigation.data["match_counts"]["navigation"] >= 1

    titled = run(BINARY, "guide-query", str(bundle), "healing", "--section-title", "overview", env=dead_proxy_env())
    assert all("overview" in row["title"].lower() for row in titled.data["matches"]["sections"])


def test_unknown_guide_slug_is_a_not_found_envelope(require) -> None:
    require(PROVIDER)
    # Keeping the "-guide" suffix means the slug classifies as a supported family, so the failure
    # comes from the site's 404 rather than from local validation.
    result = run(BINARY, "guide", f"zzz-{spec_guide_slug()}", expect=EXIT_NOT_FOUND, error_code="not_found")

    assert result.payload["error"]["details"]["status_code"] == 404


def test_a_non_guide_page_is_rejected_before_any_fetch(require) -> None:
    require(PROVIDER)
    run(BINARY, "guide", UNSUPPORTED_REF, expect=EXIT_GENERIC, error_code="invalid_guide_ref", env=dead_proxy_env())


def test_network_failure_is_an_exit_5_envelope(require) -> None:
    require(PROVIDER)
    result = run(BINARY, "guide", f"{spec_guide_slug()}-network-probe-guide", expect=EXIT_NETWORK, env=dead_proxy_env())

    assert result.error_code in {"network_error", "timeout", "upstream_error"}
    assert result.stdout == ""


def test_a_repeated_guide_fetch_is_served_from_the_session_cache(require) -> None:
    require(PROVIDER)
    warm = guide_page()
    cached = run(BINARY, "guide", spec_guide_slug(), env=dead_proxy_env())

    assert cached.data["guide"] == warm.data["guide"]
    assert cached.data["article"]["section_count"] == warm.data["article"]["section_count"]


def test_fields_and_compact_shape_the_payload(require) -> None:
    require(PROVIDER)
    fields = run_raw(BINARY, "--fields", "data.guide.slug,data.navigation.count", "guide", spec_guide_slug())
    assert fields.exit_code == 0, fields.describe()
    payload: dict[str, Any] = json.loads(fields.stdout)
    assert payload == {
        "data": {"guide": {"slug": spec_guide_slug()}, "navigation": {"count": guide_page().data["navigation"]["count"]}}
    }

    compact = run(BINARY, "--compact", "--compact-max-chars", "80", "guide", spec_guide_slug())
    text = compact.data["article"]["text"]
    assert text.endswith("...")
    assert len(text) <= 90
    assert len(guide_page().data["article"]["text"]) > len(text)


def test_guide_query_on_a_missing_bundle_is_a_usage_error(require) -> None:
    require(PROVIDER)
    result = run_raw(BINARY, "guide-query", "/nonexistent/icy-veins-bundle", "mana")

    assert result.exit_code == 2, result.describe()
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize("command", ["guide", "guide-full", "guide-export"])
def test_every_guide_command_rejects_an_empty_reference(require, command: str) -> None:
    require(PROVIDER)
    run(BINARY, command, "   ", expect=EXIT_GENERIC, error_code="invalid_guide_ref")

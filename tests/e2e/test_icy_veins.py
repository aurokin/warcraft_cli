"""End-to-end journeys for the ``icy-veins`` binary against the live icy-veins.com site.

The spec-guide slug is discovered from ``icy-veins search`` on the shared guide query in
``tests/e2e/pins.py``, so the file survives a slug rename. Every command in
``docs/reference/icy-veins.md`` is exercised, plus the documented error journeys.

The navigation and section assertions are deliberate: Icy Veins rebuilt its guide layout in 2026
and the parser silently returned zero sections and zero navigation entries for a while, which is
exactly the kind of drift a live journey has to catch. So a guide has to come back with a byline,
a last-updated stamp, sections that carry real prose on every page of the family walk, and build
references that ``warcraft guide-builds-simc`` can hand to SimulationCraft.
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
    EXIT_USAGE,
    Result,
    dead_proxy_env,
    run,
)

BINARY = "icy-veins"
PROVIDER = "icy-veins"
# Icy Veins publishes patch notes as news, not as a guide; doctor-documented scope excludes them.
OUT_OF_SCOPE_QUERY = "patch notes"
# A real Icy Veins WoW page that is not a guide, so it never classifies into a supported family.
UNSUPPORTED_REF = "news-roundup"
# A class hub is the one family that is deliberately not walked: it has no sibling pages to follow.
CLASS_HUB_QUERY = "monk guide"
# One search query per content family that the pinned spec guide's own family walk does not reach.
# The slugs age out with every expansion, so only the query and the family it must produce are
# fixed here; this is what the retired tests/test_icy_veins_live.py pinned by slug.
FAMILY_PROBES = (
    ("mistweaver monk pvp", "pvp", "family_navigation"),
    ("mistweaver monk raid guide", "raid_guide", "family_navigation"),
    ("healing guide", "role_guide", "current_page"),
    ("mistweaver monk the war within", "expansion_guide", "family_navigation"),
)
# A term the pinned healing guide uses throughout, for the offline bundle journeys.
BUNDLE_QUERY_TERM = "mana"


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


def _assert_page_is_split_into_real_sections(page: dict[str, Any], result: Result) -> None:
    """One page of a guide bundle has to be cut on its own headings, with prose under each one.

    Icy Veins wraps its headings in layout containers. When the parser stopped descending into them
    it found no heading at all and returned the whole page as a single fallback section -- still
    non-empty, still ``ok: true``. So the check is structural: every section title is one of the
    page's headings, and the page has about as many sections as it has headings (a heading with no
    content under it is dropped, which is the only legitimate way to have fewer).
    """
    where = page["guide"]["page_url"]
    sections = page["article"]["sections"]
    headings = page["article"]["headings"]
    assert sections, f"{where} parsed into zero sections\n{result.describe()}"
    assert all(row["title"].strip() for row in sections), f"{where} has untitled sections"
    empty = [row["title"] for row in sections if not (row["text"] or "").strip()]
    assert not empty, f"{where} has heading-only sections: {empty}"
    assert {row["title"] for row in sections} <= {row["title"] for row in headings}, f"{where} has sections that are not headings"
    assert len(sections) >= len(headings) - 2, f"{where} collapsed {len(headings)} headings into {len(sections)} sections"


def _exported_sections(bundle: Path) -> dict[tuple[str, int], dict[str, Any]]:
    """Every section the export wrote, keyed by the page it came from and its position on it."""
    lines = (bundle / "sections.jsonl").read_text(encoding="utf-8").splitlines()
    rows = [json.loads(line) for line in lines if line.strip()]
    return {(row["page_url"], row["ordinal"]): row for row in rows}


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


def test_search_ranks_real_guides_from_the_sitemap(require) -> None:
    require(PROVIDER)
    result = guide_search()

    assert result.data["count"] >= 1
    for row in result.data["results"]:
        assert row["entity_type"] == "guide"
        assert row["metadata"]["content_family"], "search returned a row without a content family"
        assert row["url"] == f"https://www.icy-veins.com/wow/{row['id']}"
        assert row["follow_up"]["recommended_command"] == f"{BINARY} guide {row['id']}"


def test_search_outside_the_guide_surface_returns_a_scope_hint(require) -> None:
    require(PROVIDER)
    result = run(BINARY, "search", OUT_OF_SCOPE_QUERY, "--limit", "5")

    assert result.data["count"] == 0
    assert result.data["results"] == []
    assert result.data["scope_hint"]["code"] == "patch_notes"


def test_resolve_hands_over_a_next_command_that_returns_the_same_guide(require) -> None:
    require(PROVIDER)
    result = run(BINARY, "resolve", pins.GUIDE_QUERY, "--limit", "5")

    match = result.data["match"]
    assert match is not None, f"resolve found no candidate\n{result.describe()}"
    assert result.data["candidates"], "resolve dropped the candidate list"

    # The whole point of next_command is that an agent can run it verbatim.
    next_command = result.data["next_command"]
    assert next_command == f"{BINARY} guide {match['id']}"
    binary, *args = next_command.split()
    assert binary == BINARY
    assert run(BINARY, *args).data["guide"]["slug"] == match["id"]


def test_guide_returns_attributed_sections_family_navigation_and_a_page_toc(require) -> None:
    require(PROVIDER)
    result = guide_page()

    guide = result.data["guide"]
    assert guide["slug"] == spec_guide_slug()
    assert guide["content_family"] == "spec_guide"
    assert guide["traversal_scope"] == "family_navigation"
    assert guide["page_url"] == f"https://www.icy-veins.com/wow/{spec_guide_slug()}"
    assert guide["author"].strip(), "the guide byline is missing"
    assert guide["last_updated"].strip(), "the guide has no last-updated stamp"
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
    assert article["section_preview"], "no section preview"
    assert all(row["title"].strip() and row["level"] >= 2 for row in article["section_preview"])
    assert result.data["linked_entities"]["count"] >= 1
    assert result.payload["provenance"]["page"] == guide["page_url"]


def test_guide_full_walks_the_family_and_publishes_build_references(require) -> None:
    require(PROVIDER)
    result = run(BINARY, "guide-full", spec_guide_slug())

    page_count = result.data["guide"]["page_count"]
    assert page_count == guide_page().data["navigation"]["count"]
    assert len(result.data["pages"]) == page_count
    assert len(result.data["citations"]["pages"]) == page_count
    # A skipped page is reported, never silently dropped; on the pinned guide none should be.
    assert result.data["failed_pages"] == {"count": 0, "items": []}, result.describe()

    for page in result.data["pages"]:
        _assert_page_is_split_into_real_sections(page, result)

    assert result.data["linked_entities"]["count"] >= guide_page().data["linked_entities"]["count"]
    assert result.data["analysis_surfaces"]["count"] >= 1

    # The builds/talents page is what feeds `warcraft guide-builds-simc`; zero build references
    # means the import-string markup moved and that handoff is silently empty.
    builds = result.data["build_references"]
    assert builds["count"] >= 1, result.describe()
    assert builds["count"] == len(builds["items"])
    assert {row["reference_type"] for row in builds["items"]} <= {"wow_talent_export", "wowhead_talent_calc_url"}
    assert all(row["build_code"] for row in builds["items"])


def _first_guide_of_family(query: str, family: str) -> str:
    result = run(BINARY, "search", query, "--limit", "5")
    for row in result.data["results"]:
        if row["metadata"]["content_family"] == family:
            return str(row["id"])
    raise AssertionError(f"Icy Veins search for {query!r} returned no {family}\n{result.describe()}")


@pytest.mark.parametrize(("query", "family", "traversal_scope"), FAMILY_PROBES)
def test_every_content_family_classifies_and_parses_with_a_byline(require, query: str, family: str, traversal_scope: str) -> None:
    """A guide from each remaining family, discovered live, has to classify and parse into prose."""
    require(PROVIDER)
    slug = _first_guide_of_family(query, family)
    result = run(BINARY, "guide", slug)

    guide = result.data["guide"]
    assert guide["slug"] == slug
    assert guide["content_family"] == family
    assert guide["traversal_scope"] == traversal_scope
    assert guide["supported_surface"] is True
    assert guide["author"].strip(), f"{slug} lost its byline"
    assert guide["last_updated"].strip(), f"{slug} lost its last-updated stamp"
    assert result.data["article"]["section_count"] >= 1
    assert len(result.data["article"]["text"].strip()) > 200, "the article parsed to almost nothing"


def test_a_class_hub_has_no_family_to_walk(require) -> None:
    """A class hub is scoped to the current page, so guide-full must stay on it rather than crawl."""
    require(PROVIDER)
    search = run(BINARY, "search", CLASS_HUB_QUERY, "--limit", "8")
    hubs = [row["id"] for row in search.data["results"] if row["metadata"]["content_family"] == "class_hub"]
    assert hubs, f"search found no Icy Veins class hub\n{search.describe()}"

    result = run(BINARY, "guide-full", hubs[0])
    assert result.data["guide"]["content_family"] == "class_hub"
    assert result.data["guide"]["traversal_scope"] == "current_page"
    assert result.data["guide"]["page_count"] == 1
    assert [page["guide"]["slug"] for page in result.data["pages"]] == [hubs[0]]
    assert result.data["pages"][0]["article"]["sections"], "the hub page parsed into zero sections"


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
    exported = _exported_sections(bundle)
    assert len(exported) == counts["sections"]
    assert any((bundle / "pages").iterdir()), "no page files were written"

    query = run(BINARY, "guide-query", str(bundle), BUNDLE_QUERY_TERM, "--limit", "3", env=dead_proxy_env())
    assert query.data["bundle"] == str(bundle)
    assert query.data["count"] >= 1
    sections = query.data["matches"]["sections"]
    assert sections, "no section matched a term that appears in a healing guide"
    for row in sections:
        # Matching is a substring search over title plus text, so the term has to be in one of them.
        assert BUNDLE_QUERY_TERM in f"{row['title']} {row['text']}".lower(), row["title"]
        # And the answer has to be the text the export wrote, not a re-derived or stale copy.
        assert row["text"] == exported[(row["page_url"], row["ordinal"])]["text"], row["title"]


def test_guide_query_honours_the_limit_kind_and_section_title_filters(require, out_dir: Path) -> None:
    require(PROVIDER)
    bundle = out_dir / "icy-veins-bundle"
    run(BINARY, "guide-export", spec_guide_slug(), "--out", str(bundle))

    def section_titles(*args: str) -> list[str]:
        result = run(BINARY, "guide-query", str(bundle), *args, env=dead_proxy_env())
        return [row["title"] for row in result.data["matches"]["sections"]]

    # --limit keeps the same ranking and cuts it off, rather than returning a different slice.
    wide = section_titles(BUNDLE_QUERY_TERM, "--limit", "20")
    assert len(wide) > 2, "the pinned guide needs more than two matches for --limit to mean anything"
    assert section_titles(BUNDLE_QUERY_TERM, "--limit", "2") == wide[:2]

    only_navigation = run(BINARY, "guide-query", str(bundle), "talents", "--kind", "navigation", env=dead_proxy_env())
    assert only_navigation.data["matches"]["sections"] == []
    assert only_navigation.data["match_counts"]["navigation"] >= 1

    # --section-title narrows that same ranking to the sections whose title contains the text. The
    # needle is the commonest word among the matched titles, so the expected subset is known exactly
    # and a filter that did nothing would return the whole list instead.
    all_titles = section_titles(BUNDLE_QUERY_TERM, "--limit", "50")
    words = [word for title in all_titles for word in title.lower().split() if len(word) > 3]
    needle = max(set(words), key=words.count)
    expected = [title for title in all_titles if needle in title.lower()]
    assert 0 < len(expected) < len(all_titles), f"{needle!r} has to select some titles but not all"
    assert section_titles(BUNDLE_QUERY_TERM, "--limit", "50", "--section-title", needle) == expected


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


def test_guide_query_on_a_missing_bundle_is_a_usage_error(require) -> None:
    require(PROVIDER)
    # The bundle argument is a Typer directory, so a missing path is rejected as bad input (exit 2).
    run(BINARY, "guide-query", "/nonexistent/icy-veins-bundle", "mana", expect=EXIT_USAGE, error_code="invalid_argument")


@pytest.mark.parametrize("command", ["guide", "guide-full", "guide-export"])
def test_every_guide_command_rejects_an_empty_reference(require, command: str) -> None:
    require(PROVIDER)
    run(BINARY, command, "   ", expect=EXIT_GENERIC, error_code="invalid_guide_ref")

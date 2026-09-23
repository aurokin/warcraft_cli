"""End-to-end journeys for the ``method`` binary against the live method.gg site.

The guide slug is discovered from ``method search`` on the shared guide query in
``tests/e2e/pins.py``, so nothing here rots when Method renames or retires a guide. Every command
in ``docs/reference/method.md`` is exercised, plus the documented error journeys.

The content assertions are the point of this file. Method's guide template moved once already and
the parser answered with an empty article and ``ok: true``; so a guide has to come back with a
byline, a last-updated stamp, sections that carry real prose, and build references that
``warcraft guide-builds-simc`` can hand to SimulationCraft. Each supported content family
(class, profession, reputation, article) is discovered at run time and held to the same bar,
which is what the retired ``tests/test_method_live.py`` pinned by slug.
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

BINARY = "method"
PROVIDER = "method"
# Method publishes no tier lists through the supported guide families; doctor lists the root as
# intentionally out of scope, so it is the documented way to reach the scope-hint branch.
OUT_OF_SCOPE_QUERY = "tier list"
UNSUPPORTED_SURFACE_SLUG = "tier-list"
# One search query per supported content family other than class_guide (which the pinned guide
# covers). The slugs themselves age out every patch, so only the query and the family are fixed.
FAMILY_PROBES = (
    ("alchemy profession", "profession_guide"),
    ("renown reputation", "reputation_guide"),
    ("dungeon locations", "article_guide"),
)
# A term that appears in the pinned mistweaver guide's prose, used to prove an exported bundle
# answers the same text offline that the export wrote.
BUNDLE_QUERY_TERM = "renewing mist"


@cache
def guide_search() -> Result:
    return run(BINARY, "search", pins.GUIDE_QUERY, "--limit", "5")


@cache
def guide_slug() -> str:
    """The pinned spec's Method guide, discovered rather than pinned but held to the pinned identity.

    A slug survives a rename this way, but it cannot quietly become another spec's guide: Method
    ranking brewmaster first for a mistweaver query would otherwise send every journey below to the
    wrong guide and still pass all of them.
    """
    result = guide_search()
    results = result.data["results"]
    assert results, f"search found no Method guide for {pins.GUIDE_QUERY!r}\n{result.describe()}"
    top = results[0]
    slug = str(top["id"])
    assert top["metadata"]["content_family"] == "class_guide", f"{slug} is not a class guide\n{result.describe()}"
    assert pins.GUIDE_CLASS in slug and pins.GUIDE_SPEC in slug, f"{slug} is not the pinned spec's guide"
    return slug


@cache
def guide_page() -> Result:
    return run(BINARY, "guide", guide_slug())


def _assert_page_is_split_into_real_sections(page: dict[str, Any], result: Result) -> None:
    """One page of a guide bundle has to be cut on its own headings, with prose under each one.

    Method wraps its ``h2`` headings in a layout container. When the parser stopped descending into
    it, most of a page merged into a single fallback section -- still non-empty, still ``ok: true``.
    So the check is structural: every section title is one of the page's headings, and the page has
    about as many sections as it has headings (a heading with no content under it is dropped, which
    is the only legitimate way to have fewer).
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


def _assert_summary_was_cut_on_its_headings(result: Result) -> None:
    """The one-page summary has about one section per heading, so a collapsed page fails.

    ``guide`` publishes the heading list and a section count rather than the sections themselves,
    and those two are enough: the parser's failure mode is merging a whole page into a single
    fallback section, which a page that reports many headings and one section cannot hide.
    """
    guide = result.data["guide"]
    article = result.data["article"]
    headings = {row["title"] for row in article["headings"]}
    slug = guide["slug"]
    assert headings, f"{slug} parsed into zero headings\n{result.describe()}"
    titles = [row["title"] for row in article["section_preview"]]
    assert titles and all(title.strip() for title in titles), result.describe()
    assert all(row["level"] >= 2 for row in article["section_preview"]), result.describe()
    # Prose above the first heading legitimately becomes one leading section named after the page;
    # every other section is one of the page's own headings.
    assert set(titles[1:]) <= headings, f"{slug} has sections that are not headings: {titles}\n{result.describe()}"
    assert titles[0] in headings | {guide["section_title"]}, f"{slug} opens on {titles[0]!r}\n{result.describe()}"
    # A page merged into that one fallback section still looks non-empty, so the count is what
    # catches it (a heading with nothing under it is dropped, which is the legitimate way to differ).
    assert article["section_count"] >= max(1, len(headings) // 2), (
        f"{slug} cut {len(headings)} headings into {article['section_count']} sections\n{result.describe()}"
    )


def _exported_sections(bundle: Path) -> dict[tuple[str, int], dict[str, Any]]:
    """Every section the export wrote, keyed by the page it came from and its position on it."""
    lines = (bundle / "sections.jsonl").read_text(encoding="utf-8").splitlines()
    rows = [json.loads(line) for line in lines if line.strip()]
    return {(row["page_url"], row["ordinal"]): row for row in rows}


def _first_guide_of_family(query: str, family: str) -> str:
    result = run(BINARY, "search", query, "--limit", "5")
    for row in result.data["results"]:
        if row["metadata"]["content_family"] == family:
            return str(row["id"])
    raise AssertionError(f"Method search for {query!r} returned no {family}\n{result.describe()}")


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


def test_search_finds_a_real_guide_and_names_the_follow_up(require) -> None:
    require(PROVIDER)
    result = guide_search()

    assert result.data["count"] >= 1
    # The query names a surface ("guide"); the search term it was reduced to must not.
    assert result.data["search_query"] == f"{pins.GUIDE_SPEC} {pins.GUIDE_CLASS}"
    rows = result.data["results"]
    assert rows == sorted(rows, key=lambda row: -row["ranking"]["score"]), "results must be ranked best first"
    first = rows[0]
    assert first["id"] == guide_slug(), "a spec query must rank that spec's class guide first"
    assert first["entity_type"] == "guide"
    assert first["url"] == f"https://www.method.gg/guides/{first['id']}"
    assert first["follow_up"]["recommended_command"] == f"{BINARY} guide {first['id']}"
    assert result.payload["provenance"]["sitemap_url"].endswith("sitemap.xml")


def test_search_outside_the_supported_families_returns_a_scope_hint(require) -> None:
    require(PROVIDER)
    result = run(BINARY, "search", OUT_OF_SCOPE_QUERY, "--limit", "5")

    assert result.data["count"] == 0
    assert result.data["results"] == []
    assert result.data["scope_hint"]["code"] == "tier_list"


def test_resolve_hands_over_a_next_command_that_returns_the_same_guide(require) -> None:
    require(PROVIDER)
    result = run(BINARY, "resolve", pins.GUIDE_QUERY, "--limit", "5")

    assert result.data["resolved"] is True
    assert result.data["confidence"] == "high"
    assert result.data["match"]["id"] == guide_slug(), "resolve must land on the pinned spec's guide"
    assert result.data["candidates"], "resolve dropped the candidate list"

    # The whole point of next_command is that an agent can run it verbatim.
    next_command = result.data["next_command"]
    assert next_command == f"{BINARY} guide {guide_slug()}"
    binary, *args = next_command.split()
    assert binary == BINARY
    assert run(BINARY, *args).data["guide"]["slug"] == guide_slug()


def test_guide_returns_titled_sections_navigation_and_linked_entities(require) -> None:
    require(PROVIDER)
    result = guide_page()

    guide = result.data["guide"]
    assert guide["slug"] == guide_slug()
    assert guide["supported_surface"] is True
    assert guide["content_family"] == "class_guide"
    assert guide["section_title"], "the active navigation page has no title"
    assert guide["page_url"] == f"https://www.method.gg/guides/{guide_slug()}"
    assert guide["author"].strip(), "the guide byline is missing"
    assert guide["last_updated"].strip(), "the guide has no last-updated stamp"
    # Method guides are multi-page: the family navigation is the only way to reach the other pages.
    assert result.data["navigation"]["count"] >= 2
    assert all(item["title"] and item["url"] for item in result.data["navigation"]["items"])
    assert result.data["article"]["text"].strip(), "article text is empty"
    _assert_summary_was_cut_on_its_headings(result)
    assert result.data["linked_entities"]["count"] >= 1
    assert result.payload["provenance"]["page"] == guide["page_url"]


@pytest.mark.parametrize(("query", "family"), FAMILY_PROBES)
def test_every_supported_guide_family_parses_with_a_byline(require, query: str, family: str) -> None:
    """A guide from each supported family, discovered live, has to parse into attributed prose."""
    require(PROVIDER)
    slug = _first_guide_of_family(query, family)
    result = run(BINARY, "guide", slug)

    guide = result.data["guide"]
    assert guide["slug"] == slug
    assert guide["content_family"] == family
    assert guide["supported_surface"] is True
    assert guide["author"].strip(), f"{slug} lost its byline"
    assert guide["last_updated"].strip(), f"{slug} lost its last-updated stamp"
    assert len(result.data["article"]["text"].strip()) > 200, "the article parsed to almost nothing"
    _assert_summary_was_cut_on_its_headings(result)


def test_guide_full_merges_every_page_and_publishes_build_references(require) -> None:
    require(PROVIDER)
    result = run(BINARY, "guide-full", guide_slug())

    page_count = result.data["guide"]["page_count"]
    assert page_count == guide_page().data["navigation"]["count"]
    assert len(result.data["pages"]) == page_count
    assert len(result.data["citations"]["pages"]) == page_count
    # A skipped page is reported, never silently dropped; on the pinned guide none should be.
    assert result.data["failed_pages"] == {"count": 0, "items": []}, result.describe()

    for page in result.data["pages"]:
        _assert_page_is_split_into_real_sections(page, result)

    # Merging every page can only add entities to what the first page alone carried.
    assert result.data["linked_entities"]["count"] >= guide_page().data["linked_entities"]["count"]

    # The talents page is what feeds `warcraft guide-builds-simc`; zero build references means the
    # import-string markup moved and that handoff is silently empty.
    builds = result.data["build_references"]
    assert builds["count"] == len(builds["items"])
    assert {row["reference_type"] for row in builds["items"]} <= {"wow_talent_export", "wowhead_talent_calc_url"}
    # A spec guide publishes a build per content type, so a parser that found only one has lost
    # most of them; the codes have to be distinct or the same build was collected repeatedly.
    codes = [row["build_code"] for row in builds["items"]]
    assert all(codes) and len(set(codes)) == len(codes) >= 2, result.describe()


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
    exported = _exported_sections(bundle)
    assert len(exported) == counts["sections"]

    # The bundle has to answer without the network; a dead proxy proves nothing is fetched.
    query = run(BINARY, "guide-query", str(bundle), BUNDLE_QUERY_TERM, "--limit", "3", env=dead_proxy_env())
    assert query.data["count"] >= 1
    sections = query.data["matches"]["sections"]
    assert sections, "no section matched a term that appears in the guide"
    for row in sections:
        # Matching is a substring search over title plus text, so the term has to be in one of them.
        assert BUNDLE_QUERY_TERM in f"{row['title']} {row['text']}".lower(), row["title"]
        # And the answer has to be the text the export wrote, not a re-derived or stale copy.
        assert row["text"] == exported[(row["page_url"], row["ordinal"])]["text"], row["title"]
    assert all(row["kind"] for row in query.data["top"])


def test_guide_query_honours_the_limit_kind_and_section_title_filters(require, out_dir: Path) -> None:
    require(PROVIDER)
    bundle = out_dir / "method-bundle"
    run(BINARY, "guide-export", guide_slug(), "--out", str(bundle))

    def section_titles(*args: str) -> list[str]:
        result = run(BINARY, "guide-query", str(bundle), *args, env=dead_proxy_env())
        return [row["title"] for row in result.data["matches"]["sections"]]

    # --limit keeps the same ranking and cuts it off, rather than returning a different slice.
    wide = section_titles(BUNDLE_QUERY_TERM, "--limit", "20")
    assert len(wide) > 2, "the pinned guide needs more than two matches for --limit to mean anything"
    assert section_titles(BUNDLE_QUERY_TERM, "--limit", "2") == wide[:2]

    # --kind drops the kinds that were not asked for. The same query without it has to match both
    # kinds first, or an empty section list would prove nothing about the filter.
    both = run(BINARY, "guide-query", str(bundle), "talents", env=dead_proxy_env())
    assert both.data["match_counts"]["sections"] >= 1, both.describe()
    assert both.data["match_counts"]["navigation"] >= 1, both.describe()

    only_navigation = run(BINARY, "guide-query", str(bundle), "talents", "--kind", "navigation", env=dead_proxy_env())
    assert only_navigation.data["matches"]["sections"] == []
    assert only_navigation.data["matches"]["navigation"] == both.data["matches"]["navigation"]
    assert all(row["url"] for row in only_navigation.data["matches"]["navigation"])

    # --section-title narrows that same ranking to the sections whose title contains the text. The
    # needle is the commonest word among the matched titles, so the expected subset is known exactly
    # and a filter that did nothing would return the whole list instead.
    all_titles = section_titles("mana", "--limit", "50")
    words = [word for title in all_titles for word in title.lower().split() if len(word) > 3]
    needle = max(set(words), key=words.count)
    expected = [title for title in all_titles if needle in title.lower()]
    assert 0 < len(expected) < len(all_titles), f"{needle!r} has to select some titles but not all"
    assert section_titles("mana", "--limit", "50", "--section-title", needle) == expected


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


def test_guide_query_rejects_a_bundle_path_that_is_missing_or_not_a_bundle(require, out_dir: Path) -> None:
    """The three ways the bundle argument can be wrong get the three answers the contract reserves.

    ``icy-veins guide-query`` answers identically; the pair used to disagree, so an agent that
    learned one provider's exit code got the other one wrong.
    """
    require(PROVIDER)
    run(BINARY, "guide-query", "/nonexistent/method-bundle", "mana", expect=EXIT_NOT_FOUND, error_code="not_found")

    not_a_directory = out_dir / "method-bundle.txt"
    not_a_directory.write_text("not a bundle", encoding="utf-8")
    run(BINARY, "guide-query", str(not_a_directory), "mana", expect=EXIT_USAGE, error_code="invalid_argument")

    # A directory with a manifest but no pages file (a wowhead guide-export bundle looks like this)
    # used to load as an empty bundle and answer ok:true with zero matches.
    not_a_bundle = out_dir / "method-not-a-bundle"
    not_a_bundle.mkdir()
    (not_a_bundle / "manifest.json").write_text(json.dumps({"files": {}}), encoding="utf-8")
    run(BINARY, "guide-query", str(not_a_bundle), "mana", expect=EXIT_GENERIC, error_code="invalid_bundle")

    # An unsupported --kind is refused with the code every article-bundle query shares.
    run(BINARY, "guide-query", str(not_a_bundle), "mana", "--kind", "bogus", expect=EXIT_GENERIC, error_code="invalid_query_kind")


@pytest.mark.parametrize("command", ["guide", "guide-full", "guide-export"])
def test_every_guide_command_rejects_an_empty_reference(require, command: str) -> None:
    require(PROVIDER)
    run(BINARY, command, "   ", expect=EXIT_GENERIC, error_code="invalid_guide_ref")

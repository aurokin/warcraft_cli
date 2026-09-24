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
import shlex
from datetime import date
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
    ("mistweaver monk spell summary", "spell_summary", "family_navigation"),
    ("mistweaver monk remix", "special_event_guide", "family_navigation"),
)
# A term the pinned healing guide uses throughout, for the offline bundle journeys.
BUNDLE_QUERY_TERM = "mana"


@cache
def guide_search() -> Result:
    return run(BINARY, "search", pins.GUIDE_QUERY, "--limit", "5")


@cache
def spec_guide_slug() -> str:
    """The pinned spec's guide, which a spec query has to rank first; it owns the family switcher.

    The slug is discovered rather than pinned so a rename cannot rot the file, but it has to name
    the pinned class and spec: a ranking regression that answered with another spec's guide would
    otherwise send every journey below to the wrong guide and still pass all of them.
    """
    result = guide_search()
    rows = result.data["results"]
    assert rows, f"search found no Icy Veins guide for {pins.GUIDE_QUERY!r}\n{result.describe()}"
    top = rows[0]
    slug = str(top["id"])
    assert top["metadata"]["content_family"] == "spec_guide", f"{slug} is not a spec guide\n{result.describe()}"
    assert pins.GUIDE_CLASS in slug and pins.GUIDE_SPEC in slug, f"{slug} is not the pinned spec's guide"
    return slug


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
    rows = result.data["results"]
    assert rows == sorted(rows, key=lambda row: -row["ranking"]["score"]), "results must be ranked best first"
    assert rows[0]["id"] == spec_guide_slug(), "a spec query must rank that spec's guide first"
    for row in rows:
        assert row["entity_type"] == "guide"
        assert row["metadata"]["content_family"], "search returned a row without a content family"
        assert row["url"] == f"https://www.icy-veins.com/wow/{row['id']}"
        assert row["follow_up"]["command"] == f"{BINARY} guide {row['id']}"


def test_search_outside_the_guide_surface_returns_a_scope_hint(require) -> None:
    require(PROVIDER)
    result = run(BINARY, "search", OUT_OF_SCOPE_QUERY, "--limit", "5")

    assert result.data["count"] == 0
    assert result.data["results"] == []
    assert result.data["scope_hint"]["code"] == "patch_notes"


def test_search_reads_mythic_plus_the_way_players_write_it(require) -> None:
    """``mythic+`` is how players type it and "Mythic Plus" is how Icy Veins names those pages.

    This exact query once answered ``ok: true`` with no rows.
    """
    require(PROVIDER)
    result = run(BINARY, "search", "mythic+", "--limit", "5")

    rows = result.data["results"]
    assert rows, result.describe()
    # Newer seasonal slugs drop "plus" (``midnight-mythic-season-2-guide``) and still answer the query.
    assert all("mythic-plus" in row["id"] or "-mythic-season-" in row["id"] for row in rows), result.describe()
    # The answer is the current season's page, not a guide the site stopped updating a year ago.
    dates = [date.fromisoformat(row["metadata"]["last_updated"]) for row in rows if row["metadata"]["last_updated"]]
    top_date = rows[0]["metadata"]["last_updated"]
    assert top_date and (max(dates) - date.fromisoformat(top_date)).days < 365, result.describe()


def test_resolve_hands_over_a_next_command_that_returns_the_same_guide(require) -> None:
    require(PROVIDER)
    result = run(BINARY, "resolve", pins.GUIDE_QUERY, "--limit", "5")

    assert result.data["resolved"] is True
    assert result.data["confidence"] == "high"
    match = result.data["match"]
    assert match is not None, f"resolve found no candidate\n{result.describe()}"
    assert match["id"] == spec_guide_slug(), "resolve must land on the pinned spec's guide"
    assert result.data["candidates"], "resolve dropped the candidate list"

    # The whole point of next_command is that an agent can run it verbatim.
    next_command = result.data["next_command"]
    assert next_command == f"{BINARY} guide {match['id']}"
    binary, *args = shlex.split(next_command)
    assert binary == BINARY
    assert run(BINARY, *args).data["guide"]["slug"] == match["id"]


@pytest.mark.parametrize(("query", "spec_slug"), [("frost mage", "frost-mage"), ("survival hunter guide", "survival-hunter")])
def test_resolve_lands_a_dps_spec_on_its_pve_dps_guide(require, query: str, spec_slug: str) -> None:
    """A damage spec's intro guide is ``<spec>-<class>-pve-dps-guide``; the PvP and pets pages are parts of it.

    Every DPS spec once tied its PvP guide and stayed unresolved, and the hunter specs resolved to
    their pets guide at high confidence. The expected slug is built from the query, and search has to
    list it, so the sitemap itself confirms the page exists.
    """
    require(PROVIDER)
    expected = f"{spec_slug}-pve-dps-guide"
    listed = run(BINARY, "search", query, "--limit", "10")
    assert expected in [row["id"] for row in listed.data["results"]], listed.describe()

    result = run(BINARY, "resolve", query)
    assert result.data["resolved"] is True, result.describe()
    assert result.data["match"]["id"] == expected, result.describe()
    binary, *args = shlex.split(result.data["next_command"])
    assert binary == BINARY
    page = run(BINARY, *args)
    assert (page.data["guide"]["slug"], page.data["guide"]["content_family"]) == (expected, "spec_guide"), page.describe()


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
    assert article["text"].strip(), "article text is empty"
    assert article["intro_text"].strip(), "the guide intro is empty"
    _assert_summary_was_cut_on_its_headings(result)
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

    # The walk covers the spec's talents, rotation, stat and gear pages, and the classifier gives each
    # of them its own family. A classifier that labelled every sub-page spec_guide would miss these.
    families = {page["guide"]["content_family"] for page in result.data["pages"]}
    core = {"spec_guide", "spec_builds_talents", "rotation_guide", "stat_priority", "gear_best_in_slot"}
    assert core <= families, f"the family walk classified {page_count} pages as {sorted(families)}"

    assert result.data["linked_entities"]["count"] >= guide_page().data["linked_entities"]["count"]
    # Each of those pages gets its own analysis surface citing it; a bare count cannot tell one
    # surface from one per page.
    cited = {row["page_url"] for row in result.data["analysis_surfaces"]["items"]}
    assert {page["guide"]["page_url"] for page in result.data["pages"] if page["guide"]["content_family"] in core} <= cited

    # The builds/talents page is what feeds `warcraft guide-builds-simc`; zero build references
    # means the import-string markup moved and that handoff is silently empty.
    builds = result.data["build_references"]
    assert builds["count"] == len(builds["items"])
    assert {row["reference_type"] for row in builds["items"]} <= {"wow_talent_export", "wowhead_talent_calc_url"}
    # A spec guide publishes a build per content type, so a parser that found only one has lost most
    # of them; the codes have to be distinct or the same build was collected repeatedly.
    codes = [row["build_code"] for row in builds["items"]]
    assert all(codes) and len(set(codes)) == len(codes) >= 2, result.describe()


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
    assert len(result.data["article"]["text"].strip()) > 200, "the article parsed to almost nothing"
    _assert_summary_was_cut_on_its_headings(result)


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

    # --kind drops the kinds that were not asked for. The same query without it has to match both
    # kinds first, or an empty section list would prove nothing about the filter.
    both = run(BINARY, "guide-query", str(bundle), "talents", env=dead_proxy_env())
    assert both.data["match_counts"]["sections"] >= 1, both.describe()
    assert both.data["match_counts"]["navigation"] >= 1, both.describe()

    only_navigation = run(BINARY, "guide-query", str(bundle), "talents", "--kind", "navigation", env=dead_proxy_env())
    assert only_navigation.data["matches"]["sections"] == []
    assert only_navigation.data["matches"]["navigation"] == both.data["matches"]["navigation"]

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


def test_guide_query_rejects_a_bundle_path_that_is_missing_or_not_a_bundle(require, out_dir: Path) -> None:
    """The three ways the bundle argument can be wrong get the three answers the contract reserves.

    ``method guide-query`` answers identically; the pair used to disagree, so an agent that learned
    one provider's exit code got the other one wrong.
    """
    require(PROVIDER)
    run(BINARY, "guide-query", "/nonexistent/icy-veins-bundle", "mana", expect=EXIT_NOT_FOUND, error_code="not_found")

    not_a_directory = out_dir / "icy-veins-bundle.txt"
    not_a_directory.write_text("not a bundle", encoding="utf-8")
    run(BINARY, "guide-query", str(not_a_directory), "mana", expect=EXIT_USAGE, error_code="invalid_argument")

    # A directory with a manifest but no pages file (a wowhead guide-export bundle looks like this)
    # used to load as an empty bundle and answer ok:true with zero matches.
    not_a_bundle = out_dir / "icy-veins-not-a-bundle"
    not_a_bundle.mkdir()
    (not_a_bundle / "manifest.json").write_text(json.dumps({"files": {}}), encoding="utf-8")
    run(BINARY, "guide-query", str(not_a_bundle), "mana", expect=EXIT_GENERIC, error_code="invalid_bundle")

    # An unsupported --kind is a bad flag: the usage code every provider gives that mistake, raised
    # before the bundle is read (so this one is not reported as the invalid bundle it also is).
    run(BINARY, "guide-query", str(not_a_bundle), "mana", "--kind", "bogus", expect=EXIT_USAGE, error_code="invalid_argument")


@pytest.mark.parametrize("command", ["guide", "guide-full", "guide-export"])
def test_every_guide_command_rejects_an_empty_reference(require, command: str) -> None:
    require(PROVIDER)
    run(BINARY, command, "   ", expect=EXIT_GENERIC, error_code="invalid_guide_ref")

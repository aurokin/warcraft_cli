"""The Icy Veins site index: page reading, the crawl policy behind ``index-refresh``, merging, and search over it."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from icy_veins_cli import site_index
from icy_veins_cli.client import (
    ICY_VEINS_SITEMAP_URL,
    INDEX_REFRESH_MIN_INTERVAL_SECONDS,
    INDEX_REFRESH_RATE_LIMITER,
    SITE_MENU_SEED_URL,
    IcyVeinsClient,
)
from icy_veins_cli.main import app
from icy_veins_cli.page_parser import classify_guide_slug, guide_url, parse_sitemap_guides, read_index_page
from icy_veins_cli.site_index import SiteIndex, load_site_index, local_index_path, merge_crawl, save_site_index
from typer.testing import CliRunner
from warcraft_content.site_crawler import CrawledPage, CrawlResult, FetchResult, PageRead
from warcraft_core.envelope import envelope_violations

from tests.article_provider_testkit import load_fixture_text

runner = CliRunner()
FIXTURE_DIR = Path(__file__).parent / "fixtures" / "icy_veins"
NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)


def iv_page(slug: str, *, title: str, published: str = "2026-03-12", menu: tuple[str, ...] = (), links: tuple[str, ...] = (), parent: str | None = None) -> str:
    """A synthetic Icy Veins page with the markup ``read_index_page`` reads: canonical, JSON-LD and the site menu."""
    crumbs = [{"@type": "ListItem", "position": 1, "item": {"@id": "https://www.icy-veins.com/wow/", "name": "World of Warcraft"}}]
    if parent:
        crumbs.append({"@type": "ListItem", "position": 2, "item": {"@id": guide_url(parent), "name": parent}})
    crumbs.append({"@type": "ListItem", "position": len(crumbs) + 1, "item": {"@id": guide_url(slug), "name": title}})
    article = {"@type": "Article", "headline": title, "datePublished": f"{published}T16:00:00+00:00", "dateModified": "2026-05-19T12:30:00+00:00"}
    menu_html = "".join(f'<a href="/wow/{target}">{target}</a>' for target in menu)
    body_html = "".join(f'<a href="https://www.icy-veins.com/wow/{target}">{target}</a>' for target in links)
    return (
        f'<html><head><link rel="canonical" href="{guide_url(slug)}">'
        f'<script type="application/ld+json">{json.dumps(article)}</script>'
        f'<script type="application/ld+json">{json.dumps({"@type": "BreadcrumbList", "itemListElement": crumbs})}</script>'
        f'</head><body><nav class="iv-subnav">{menu_html}</nav><main>{body_html}'
        '<a href="https://www.icy-veins.com/wow/news/some-post">news</a><a href="https://www.wowhead.com/spell=1">spell</a>'
        "</main></body></html>"
    )


# ---------------------------------------------------------------------------------------------------
# Reading pages


def test_read_index_page_reads_a_captured_boss_page_with_its_breadcrumb_parent() -> None:
    page = read_index_page("https://www.icy-veins.com/wow/vorasius-raid-guide", load_fixture_text(FIXTURE_DIR, "raid_boss_page.html"))

    assert page is not None
    assert page.row == {
        "slug": "vorasius-raid-guide",
        "url": "https://www.icy-veins.com/wow/vorasius-raid-guide",
        "title": "Vorasius Raid Guide in The Voidspire for Midnight Season 1",
        "date_published": "2026-03-12",
        "date_modified": "2026-05-19",
        "parent": "midnight-season-1-raid-guide",
    }
    links = {link.url.rsplit("/", 1)[1]: link.source for link in page.links}
    assert links["midnight-season-1-raid-guide"] == "page"
    assert links["death-knight-guide"] == "menu"
    assert "vorasius-raid-guide" not in links


@pytest.mark.parametrize(
    ("fixture", "slug", "linked"),
    [
        ("raid_hub_midnight_season_1.html", "midnight-season-1-raid-guide", {"chimaerus-raid-guide", "imperator-averzian-raid-guide", "midnight-falls-raid-guide"}),
        ("dungeon_hub.html", "dungeons-guide", {"windrunner-spire-dungeon-guide", "murder-row-dungeon-guide", "voidscar-arena-dungeon-guide"}),
    ],
)
def test_read_index_page_lists_the_pages_a_captured_hub_links_beside_the_site_menu(fixture: str, slug: str, linked: set[str]) -> None:
    page = read_index_page(guide_url(slug), load_fixture_text(FIXTURE_DIR, fixture))

    assert page is not None and page.row["slug"] == slug and page.row["parent"] == "midnight-expansion-guide"
    page_links = {link.url.rsplit("/", 1)[1] for link in page.links if link.source == "page"}
    assert linked <= page_links
    assert sum(link.source == "menu" for link in page.links) >= 100
    assert all(link.url.startswith("https://www.icy-veins.com/wow/") and link.url.count("/") == 4 for link in page.links)


def test_read_index_page_rejects_a_body_without_a_canonical_page() -> None:
    assert read_index_page(SITE_MENU_SEED_URL, "<html><title>Just a moment...</title></html>") is None


# ---------------------------------------------------------------------------------------------------
# Classification of the families the sitemap's other pages fall into


@pytest.mark.parametrize(
    ("slug", "family"),
    [
        ("drest-agath-normal-encounter-journal", "raid_encounter"),
        ("hellfire-high-council-strategy-guide-normal-heroic-mythic", "raid_encounter"),
        ("al-akir-healer-strategy", "raid_encounter"),
        ("broodtwister-ovi-nax-raid-guide-in-nerub-ar-palace", "raid_encounter"),
        ("orgozoa-strategy-guide-in-the-eternal-palace-raid", "raid_encounter"),
        ("vorasius-raid-guide", "raid_guide"),
        ("firelands-raid", "raid_guide"),
        ("ara-kara-city-of-echoes-dungeon-guide", "dungeon_guide"),
        ("dungeons-guide", "dungeon_guide"),
        ("the-sinkhole-delve-guide", "delve_guide"),
        ("brann-bronzebeard-delve-companion-guide", "delve_guide"),
        ("professions-alchemy", "profession"),
        ("professions", "profession"),
        ("mythic-dps-tier-list", "tier_list"),
        ("void-assaults-hub", "hub"),
        ("guides-for-legion", "hub"),
        ("weekly-to-do-list", "article_guide"),
        ("mistweaver-monk-pvp-talents-and-builds", "pvp"),
        ("transmogrification-priest-pvp-arena-season-10-set", "transmog"),
        ("koltiras-battlegear-dk-transmog-pve-tier-9-horde-set", "transmog"),
        ("transmogrification-guide", "article_guide"),
        ("arcane-mage-patch-9-1-changes-analysis", None),
        ("latest-mage-class-changes", None),
        ("midnight-talent-calculator", None),
        ("news-roundup", None),
    ],
)
def test_classify_guide_slug_gives_the_sitemaps_other_pages_their_families(slug: str, family: str | None) -> None:
    assert classify_guide_slug(slug) == family


# ---------------------------------------------------------------------------------------------------
# Merging a crawl into the previous index


def _row(slug: str, *, title: str, first_seen: str = "2026-09-01", last_seen: str = "2026-09-01", source: str = "menu") -> dict:
    return {
        "slug": slug,
        "url": guide_url(slug),
        "title": title,
        "date_published": "2026-03-01",
        "date_modified": "2026-05-19",
        "parent": None,
        "status": "ok",
        "redirect_to": None,
        "source": source,
        "first_seen": first_seen,
        "last_seen": last_seen,
    }


def _read(slug: str, title: str) -> PageRead:
    return PageRead(guide_url(slug), {"slug": slug, "url": guide_url(slug), "title": title, "date_published": "2026-09-30", "date_modified": None, "parent": None}, ())


def test_merge_keeps_pages_the_crawl_did_not_see_and_records_renames_and_removals() -> None:
    previous = SiteIndex(
        {
            "past-season-raid-guide": _row("past-season-raid-guide", title="Past Season Raid"),
            "current-raid-guide": _row("current-raid-guide", title="Old Title", source="page"),
            "retired-guide": _row("retired-guide", title="Retired"),
            "old-hunter-talents": _row("old-hunter-talents", title="Old Hunter Talents"),
        },
        "2026-09-01T00:00:00+00:00",
    )
    result = CrawlResult(
        pages=[
            CrawledPage(guide_url("current-raid-guide"), _read("current-raid-guide", "Current Raid"), "menu"),
            CrawledPage(guide_url("brand-new-guide"), _read("brand-new-guide", "Brand New"), "page"),
            CrawledPage(guide_url("old-hunter-talents"), _read("new-hunter-talents", "New Hunter Talents"), "page"),
        ],
        aliases={guide_url("old-hunter-talents"): guide_url("new-hunter-talents")},
        not_found=[guide_url("retired-guide")],
        frontier=[guide_url("not-fetched-yet")],
    )

    merged, counts = merge_crawl(previous, result, now=NOW)

    assert merged.pages["past-season-raid-guide"] == previous.pages["past-season-raid-guide"]
    current = merged.pages["current-raid-guide"]
    assert (current["title"], current["first_seen"], current["last_seen"], current["source"]) == ("Current Raid", "2026-09-01", "2026-10-02", "page")
    assert merged.pages["old-hunter-talents"]["status"] == "redirect"
    assert merged.pages["old-hunter-talents"]["redirect_to"] == "new-hunter-talents"
    assert merged.pages["old-hunter-talents"]["title"] is None
    assert "retired-guide" not in merged.pages
    assert counts.new == ["brand-new-guide", "new-hunter-talents"]
    assert counts.dropped == ["retired-guide"]
    assert merged.frontier == [guide_url("not-fetched-yet")]
    assert merged.refreshed_at == "2026-10-02T12:00:00+00:00"


def test_save_writes_sorted_rows_atomically_and_load_prefers_the_local_index(tmp_path: Path) -> None:
    index = SiteIndex({"b-guide": _row("b-guide", title="B"), "a-guide": _row("a-guide", title="A")}, "2026-10-01T00:00:00+00:00")

    path = save_site_index(index)

    assert path == local_index_path()
    assert [row["slug"] for row in json.loads(path.read_text())["pages"]] == ["a-guide", "b-guide"]
    assert [entry.name for entry in path.parent.iterdir()] == ["site_index.json"]
    loaded = load_site_index()
    assert loaded is not None and not loaded.bundled and set(loaded.pages) == {"a-guide", "b-guide"}


def test_load_falls_back_to_the_bundled_snapshot_when_the_local_index_is_missing_or_unreadable() -> None:
    """The package ships a snapshot so search finds the pages the sitemap lacks before any crawl."""
    bundled = load_site_index()
    assert bundled is not None and bundled.bundled
    assert len(bundled.pages) >= 100 and bundled.refreshed_at
    assert all(row["status"] in {"ok", "redirect"} and "html" not in row for row in bundled.pages.values())

    local_index_path().parent.mkdir(parents=True)
    local_index_path().write_text("{not json", encoding="utf-8")
    assert load_site_index().bundled is True

    # A local index in another format (an older or newer release's) is not read either.
    local_index_path().write_text(json.dumps({"format": 2, "pages": [_row("a-guide", title="A")]}), encoding="utf-8")
    assert load_site_index().bundled is True


def test_load_keeps_only_the_well_formed_rows_of_a_local_index() -> None:
    local_index_path().parent.mkdir(parents=True)
    pages = [_row("a-guide", title="A"), "not-a-row", {"title": "No slug"}, {"slug": 7}]
    local_index_path().write_text(json.dumps({"format": site_index.INDEX_FORMAT, "pages": pages}), encoding="utf-8")

    loaded = load_site_index()

    assert loaded is not None and not loaded.bundled and list(loaded.pages) == ["a-guide"]


# ---------------------------------------------------------------------------------------------------
# index-refresh


REFRESH_SITEMAP_XML = """
<urlset>
  <url><loc>https://www.icy-veins.com/wow/death-knight-guide</loc><lastmod>2025-10-05</lastmod></url>
  <url><loc>https://www.icy-veins.com/wow/old-sitemap-guide</loc><lastmod>2025-10-05</lastmod></url>
  <url><loc>https://www.icy-veins.com/wow/drest-agath-normal-encounter-journal</loc><lastmod>2020-01-01</lastmod></url>
</urlset>
"""


def _refresh_site() -> dict[str, str | FetchResult]:
    return {
        SITE_MENU_SEED_URL: iv_page("death-knight-guide", title="Death Knight Guide", menu=("midnight-season-2-raid-guide", "death-knight-guide")),
        guide_url("midnight-season-2-raid-guide"): iv_page(
            "midnight-season-2-raid-guide",
            title="Midnight Season 2 Raid Guide",
            menu=("midnight-season-2-raid-guide",),
            links=("old-sitemap-guide", "drest-agath-normal-encounter-journal", "nekzali-raid-guide", "renamed-guide", "deleted-guide"),
        ),
        guide_url("nekzali-raid-guide"): iv_page("nekzali-raid-guide", title="Nekzali Raid Guide in The Venomous Abyss", parent="midnight-season-2-raid-guide"),
        guide_url("renamed-guide"): iv_page("its-new-name-guide", title="Its New Name"),
    }


def _serve(monkeypatch, site: dict[str, str | FetchResult]) -> list[str]:
    fetched: list[str] = []

    def crawl_fetch(self, url: str) -> FetchResult:
        fetched.append(url)
        body = site.get(url)
        if body is None:
            return FetchResult(404)
        return body if isinstance(body, FetchResult) else FetchResult(200, body)

    monkeypatch.setattr(IcyVeinsClient, "sitemap_text", lambda self: REFRESH_SITEMAP_XML)
    monkeypatch.setattr(IcyVeinsClient, "crawl_fetch", crawl_fetch)
    monkeypatch.setattr("icy_veins_cli.site_index.BUNDLED_INDEX", Path("/nonexistent/site_index.json"))
    return fetched


def _refresh(*args: str) -> dict:
    result = runner.invoke(app, ["index-refresh", *args])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert envelope_violations(payload) == []
    return payload


def test_index_refresh_follows_the_menu_and_pages_the_sitemap_lacks_and_writes_the_index(monkeypatch) -> None:
    fetched = _serve(monkeypatch, _refresh_site())

    payload = _refresh()

    # The sitemap's pages are linked but never fetched; everything the sitemap lacks is.
    assert fetched == [SITE_MENU_SEED_URL] + [guide_url(slug) for slug in ("midnight-season-2-raid-guide", "nekzali-raid-guide", "renamed-guide", "deleted-guide")]
    data = payload["data"]
    assert (payload["command"], payload["kind"], payload["query"]) == ("index-refresh", "index_refresh", {"max_requests": 250})
    assert (data["partial"], data["stop_reason"]) == (False, None)
    assert data["counts"] == {"fetched": 5, "cached": 0, "pages": 4, "new": 4, "aliases": 1, "dropped": 0, "errors": 0, "frontier": 0, "total": 5}
    assert data["new_pages"] == ["death-knight-guide", "midnight-season-2-raid-guide", "nekzali-raid-guide", "its-new-name-guide"]
    assert payload["provenance"]["min_interval_seconds"] == INDEX_REFRESH_MIN_INTERVAL_SECONDS
    stored = json.loads(Path(data["index_path"]).read_text())
    rows = {row["slug"]: row for row in stored["pages"]}
    assert rows["nekzali-raid-guide"]["parent"] == "midnight-season-2-raid-guide"
    assert rows["nekzali-raid-guide"]["source"] == "page"
    assert rows["renamed-guide"]["redirect_to"] == "its-new-name-guide"
    assert "<html" not in json.dumps(stored)


def test_index_refresh_stops_at_the_cap_and_the_next_run_resumes_from_the_frontier(monkeypatch) -> None:
    fetched = _serve(monkeypatch, _refresh_site())

    first = _refresh("--max-requests", "2")["data"]
    assert (first["partial"], first["stop_reason"], first["counts"]["frontier"]) == (True, "max_requests", 3)

    fetched.clear()
    second = _refresh()["data"]
    assert fetched[:2] == [SITE_MENU_SEED_URL, guide_url("nekzali-raid-guide")]
    assert second["partial"] is False
    assert second["previous_index"]["bundled"] is False
    assert {"nekzali-raid-guide", "its-new-name-guide"} <= set(second["new_pages"])


def test_index_refresh_stops_at_a_challenge_and_keeps_every_page_of_the_previous_index(monkeypatch) -> None:
    site = _refresh_site()
    _serve(monkeypatch, site)
    _refresh()
    before = json.loads(local_index_path().read_text())
    before["refreshed_at"] = "2026-09-01T00:00:00+00:00"
    local_index_path().write_text(json.dumps(before))

    site[SITE_MENU_SEED_URL] = iv_page("death-knight-guide", title="Death Knight Guide", menu=("midnight-season-3-raid-guide",))
    site[guide_url("midnight-season-3-raid-guide")] = FetchResult(403, challenge=True)
    data = _refresh()["data"]

    assert (data["partial"], data["stop_reason"]) == (True, "blocked")
    assert data["blocked"] == {"url": guide_url("midnight-season-3-raid-guide"), "status": 403, "challenge": True}
    after = json.loads(local_index_path().read_text())
    assert {row["slug"] for row in after["pages"]} == {row["slug"] for row in before["pages"]}
    assert after["frontier"] == [guide_url("midnight-season-3-raid-guide")]
    # A blocked run is not a refresh: the index keeps its age, so its staleness warning stands.
    assert after["refreshed_at"] == before["refreshed_at"]


def test_index_refresh_stops_when_the_site_keeps_failing_and_keeps_the_index_age(monkeypatch) -> None:
    site = _refresh_site()
    _serve(monkeypatch, site)
    _refresh()
    before = json.loads(local_index_path().read_text())
    before["refreshed_at"] = "2026-09-01T00:00:00+00:00"
    local_index_path().write_text(json.dumps(before))

    down = [f"down-{index}-raid-guide" for index in range(5)]
    site[SITE_MENU_SEED_URL] = iv_page("death-knight-guide", title="Death Knight Guide", menu=tuple(down))
    site.update({guide_url(slug): FetchResult(503) for slug in down})
    data = _refresh()["data"]

    assert (data["partial"], data["stop_reason"], data["counts"]["fetched"]) == (True, "unavailable", 4)
    assert json.loads(local_index_path().read_text())["refreshed_at"] == before["refreshed_at"]


def test_index_refresh_blocked_before_reading_anything_does_not_copy_the_bundled_snapshot(monkeypatch) -> None:
    bundled = site_index.BUNDLED_INDEX
    _serve(monkeypatch, {SITE_MENU_SEED_URL: FetchResult(403, challenge=True)})
    monkeypatch.setattr("icy_veins_cli.site_index.BUNDLED_INDEX", bundled)

    data = _refresh()["data"]

    assert (data["stop_reason"], data["counts"]["pages"]) == ("blocked", 0)
    assert data["index_path"] == str(bundled)
    assert not local_index_path().exists()


def test_index_refresh_fails_when_the_seed_lists_no_links_and_leaves_the_index_alone(monkeypatch) -> None:
    site = _refresh_site()
    _serve(monkeypatch, site)
    _refresh()
    before = local_index_path().read_text()

    site[SITE_MENU_SEED_URL] = '<html><head><link rel="canonical" href="https://www.icy-veins.com/wow/death-knight-guide"></head></html>'
    result = runner.invoke(app, ["index-refresh"])

    assert result.exit_code == 1, result.output
    assert json.loads(result.stderr)["error"]["code"] == "parse_failed"
    assert local_index_path().read_text() == before


def test_index_refresh_reports_an_unreachable_seed_as_a_network_error(monkeypatch) -> None:
    _serve(monkeypatch, {SITE_MENU_SEED_URL: FetchResult(0, error="ConnectError: offline")})

    result = runner.invoke(app, ["index-refresh"])

    assert result.exit_code == 5, result.output
    assert json.loads(result.stderr)["error"]["code"] == "network_error"
    assert not local_index_path().exists()


def test_index_refresh_revisits_the_least_recently_seen_pages_first_and_never_a_redirect(monkeypatch) -> None:
    """A capped run must reach the stalest pages; re-reading the freshest ones would leave the rest stale for good."""
    rows = {
        "fresh-guide": _row("fresh-guide", title="Fresh", last_seen="2026-09-20"),
        "oldest-guide": _row("oldest-guide", title="Oldest", last_seen="2026-08-01"),
        "older-guide": _row("older-guide", title="Older", last_seen="2026-09-03"),
        "renamed-guide": {**_row("renamed-guide", title=None, last_seen="2026-07-01"), "status": "redirect", "redirect_to": "older-guide"},
    }
    site: dict[str, str | FetchResult] = {SITE_MENU_SEED_URL: iv_page("death-knight-guide", title="Death Knight Guide", menu=("fresh-guide",))}
    site.update({row["url"]: iv_page(slug, title=str(row["title"])) for slug, row in rows.items() if row["status"] == "ok"})
    fetched = _serve(monkeypatch, site)
    _write_index(*rows.values())

    data = _refresh("--max-requests", "3")["data"]

    assert fetched == [SITE_MENU_SEED_URL, guide_url("oldest-guide"), guide_url("older-guide")]
    assert (data["partial"], data["stop_reason"]) == (True, "max_requests")


def test_index_refresh_names_a_data_directory_it_cannot_write(monkeypatch, tmp_path: Path) -> None:
    """A regular file where the data directory belongs crashed the run as internal_error NotADirectoryError.

    It fails before the crawl: a default run would otherwise fetch for minutes before learning this.
    """
    data_home = tmp_path / "data-home"
    data_home.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    fetched = _serve(monkeypatch, _refresh_site())

    result = runner.invoke(app, ["index-refresh", "--max-requests", "1"])

    assert result.exit_code == 1, result.output
    error = json.loads(result.stderr)["error"]
    assert error["code"] == "invalid_data_dir"
    assert error["details"]["path"] == str(local_index_path())
    assert fetched == []


def test_index_refresh_names_a_data_directory_that_fails_at_save_time(monkeypatch) -> None:
    """A write that fails after the crawl (a full disk) is still invalid_data_dir, not internal_error."""
    _serve(monkeypatch, _refresh_site())

    def disk_full(_index: object) -> Path:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("icy_veins_cli.provider.save_site_index", disk_full)

    result = runner.invoke(app, ["index-refresh", "--max-requests", "1"])

    assert result.exit_code == 1, result.output
    assert json.loads(result.stderr)["error"]["code"] == "invalid_data_dir"


def _fake_request(monkeypatch, response: httpx.Response | Exception) -> list[dict]:
    calls: list[dict] = []

    def fake_request(_client, url: str, **kwargs) -> httpx.Response:
        calls.append({"url": url, **kwargs})
        if isinstance(response, Exception):
            raise response
        response.request = httpx.Request("GET", url)
        response.raise_for_status()
        return response

    monkeypatch.setattr("icy_veins_cli.client.request_with_retries", fake_request)
    return calls


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (httpx.Response(200, text="<html>page</html>"), FetchResult(200, "<html>page</html>")),
        (httpx.Response(403, headers={"cf-mitigated": "challenge"}), FetchResult(403, challenge=True)),
        (httpx.Response(429), FetchResult(429)),
        (httpx.ConnectError("offline"), FetchResult(0, error="ConnectError: offline")),
    ],
)
def test_crawl_fetch_makes_one_paced_request_and_never_retries(monkeypatch, response, expected: FetchResult) -> None:
    calls = _fake_request(monkeypatch, response)

    with IcyVeinsClient() as client:
        assert client.crawl_fetch(guide_url("vorasius-raid-guide")) == expected

    assert len(calls) == 1 and calls[0]["retry_attempts"] == 1
    assert calls[0]["rate_limiter"].min_interval_seconds == INDEX_REFRESH_MIN_INTERVAL_SECONDS


@pytest.mark.parametrize(("configured", "expected"), [("0", INDEX_REFRESH_MIN_INTERVAL_SECONDS), ("3", 3.0)])
def test_index_refresh_paces_at_the_configured_interval_when_it_is_longer(monkeypatch, configured: str, expected: float) -> None:
    monkeypatch.setenv("WARCRAFT_HTTP_MIN_INTERVAL_SECONDS", configured)

    assert INDEX_REFRESH_RATE_LIMITER.min_interval_seconds == expected


def test_crawl_fetch_caches_the_pages_it_reads(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ICY_VEINS_CACHE_BACKEND", "file")
    monkeypatch.setenv("ICY_VEINS_CACHE_DIR", str(tmp_path))
    calls = _fake_request(monkeypatch, httpx.Response(200, text="<html>page</html>"))

    with IcyVeinsClient() as client:
        client.crawl_fetch(SITE_MENU_SEED_URL)
    with IcyVeinsClient() as client:
        assert client.guide_page_html("death-knight-guide") == (SITE_MENU_SEED_URL, "<html>page</html>")
        assert client.crawl_fetch(SITE_MENU_SEED_URL).cached is True
    assert len(calls) == 1


def test_crawl_fetch_never_caches_a_challenge(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ICY_VEINS_CACHE_BACKEND", "file")
    monkeypatch.setenv("ICY_VEINS_CACHE_DIR", str(tmp_path))
    _fake_request(monkeypatch, httpx.Response(200, headers={"cf-mitigated": "challenge"}, text="Just a moment..."))

    with IcyVeinsClient() as client:
        assert client.crawl_fetch(SITE_MENU_SEED_URL).challenge is True
        assert client.cached_page_html(SITE_MENU_SEED_URL) is None


def test_crawl_fetch_serves_a_cached_guide_page_without_a_request(monkeypatch) -> None:
    calls = _fake_request(monkeypatch, httpx.Response(500))
    monkeypatch.setattr(IcyVeinsClient, "cached_page_html", lambda self, url: "<html>cached</html>")

    with IcyVeinsClient() as client:
        assert client.crawl_fetch(SITE_MENU_SEED_URL) == FetchResult(200, "<html>cached</html>", cached=True)
    assert calls == []


# ---------------------------------------------------------------------------------------------------
# Search over the sitemap, the menu and the index


FROZEN_SITEMAP_XML = """
<urlset>
  <url><loc>https://www.icy-veins.com/wow/frost-mage-pve-dps-guide</loc><lastmod>2025-10-05</lastmod></url>
  <url><loc>https://www.icy-veins.com/wow/mage-guide</loc><lastmod>2025-10-05</lastmod></url>
  <url><loc>https://www.icy-veins.com/wow/beast-mastery-hunter-pve-dps-talents</loc><lastmod>2025-09-01</lastmod></url>
  <url><loc>https://www.icy-veins.com/wow/transmogrification-mage-cloth-chest-item-model-list</loc><lastmod>2025-10-05</lastmod></url>
  <url><loc>https://www.icy-veins.com/wow/manaforge-omega-raid-guide</loc><lastmod>2025-09-30</lastmod></url>
</urlset>
"""


def _write_index(*rows: dict, refreshed: datetime = NOW) -> None:
    save_site_index(SiteIndex({row["slug"]: row for row in rows}, refreshed.isoformat(timespec="seconds")))


def _search(monkeypatch, query: str, *, menu: tuple[str, ...] = (), command: str = "search") -> dict:
    monkeypatch.setattr(IcyVeinsClient, "sitemap_guides", lambda self: parse_sitemap_guides(FROZEN_SITEMAP_XML))
    menu_rows = [{"slug": slug, "name": slug, "url": guide_url(slug), "content_family": classify_guide_slug(slug), "sitemap_lastmod": None} for slug in menu]
    monkeypatch.setattr(IcyVeinsClient, "site_menu_guides", lambda self: menu_rows)
    monkeypatch.setattr("icy_veins_cli.provider.date", type("FixedDate", (date,), {"today": classmethod(lambda cls: NOW.date())}))
    monkeypatch.setattr("icy_veins_cli.site_index.BUNDLED_INDEX", Path("/nonexistent/site_index.json"))
    result = runner.invoke(app, [command, query, "--limit", "10"])
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


VORASIUS = {**_row("vorasius-raid-guide", title="Vorasius Raid Guide in The Voidspire for Midnight Season 1", source="page"), "date_published": "2026-03-12"}
RAID_HUB = {**_row("midnight-season-1-raid-guide", title="Midnight Season 1 Raids - Dreamrift, Voidspire, March on Quel'Danas and Sporefall", source="page"), "date_published": "2026-03-03"}


def test_search_finds_a_page_only_the_index_knows_through_its_headline(monkeypatch) -> None:
    """``voidspire`` is in neither slug; the boss page and the raid hub are found by their headlines."""
    _write_index(VORASIUS, RAID_HUB)

    payload = _search(monkeypatch, "voidspire")

    rows = {row["id"]: row for row in payload["data"]["results"]}
    assert set(rows) == {"vorasius-raid-guide", "midnight-season-1-raid-guide"}
    boss = rows["vorasius-raid-guide"]
    assert boss["name"] == VORASIUS["title"]
    expected = {"source": "site_index", "content_family": "raid_guide", "sitemap_lastmod": None, "date_published": "2026-03-12"}
    assert expected.items() <= boss["metadata"].items()
    assert payload["provenance"]["site_index_path"] == str(local_index_path())
    assert "site_index_warning" not in payload["provenance"]


def test_search_ranks_a_renamed_page_once_under_its_new_slug(monkeypatch) -> None:
    renamed = {**_row("beast-mastery-hunter-pve-dps-talents", title=None), "status": "redirect", "redirect_to": "beast-mastery-hunter-pve-dps-spec-builds-talents"}
    current = _row("beast-mastery-hunter-pve-dps-spec-builds-talents", title="Beast Mastery Hunter Talents and Builds")
    _write_index(renamed, current)

    payload = _search(monkeypatch, "beast mastery hunter talents")

    ids = [row["id"] for row in payload["data"]["results"]]
    assert ids[0] == "beast-mastery-hunter-pve-dps-spec-builds-talents"
    assert "beast-mastery-hunter-pve-dps-talents" not in ids


def test_search_lends_an_index_headline_to_a_sitemap_page_and_keeps_its_source(monkeypatch) -> None:
    _write_index(_row("manaforge-omega-raid-guide", title="Manaforge Omega Raid Guide: Plexus Sentinel to Dimensius"))

    row = _search(monkeypatch, "dimensius")["data"]["results"][0]

    assert (row["id"], row["metadata"]["source"], row["metadata"]["sitemap_lastmod"]) == ("manaforge-omega-raid-guide", "sitemap", "2025-09-30")


def test_search_breaks_a_tie_toward_the_newest_published_index_page(monkeypatch) -> None:
    older = {**_row("rotmire-raid-guide", title="Rotmire Raid Guide"), "date_published": "2026-03-01"}
    newer = {**_row("sszorak-raid-guide", title="Sszorak Raid Guide"), "date_published": "2026-07-01"}
    _write_index(older, newer)

    ids = [row["id"] for row in _search(monkeypatch, "raid guide")["data"]["results"]]

    assert ids.index("sszorak-raid-guide") < ids.index("rotmire-raid-guide") < ids.index("manaforge-omega-raid-guide")


@pytest.mark.parametrize("query", ["midnight mythic plus", "midnight m+", "midnight mythic season"])
def test_resolve_scores_a_seasons_headline_and_slug_spellings_alike(monkeypatch, query: str) -> None:
    """Season 1's "Mythic+" headline earned the name prefix "plus"-less Season 2 missed, so S1 resolved at high confidence."""
    season_1 = {**_row("midnight-mythic-season-1-guide", title="Midnight Mythic+ Season 1 Guide", source="page"), "date_published": "2026-02-25"}
    season_2 = {**_row("midnight-mythic-season-2-guide", title="Midnight Mythic Season 2 Guide"), "date_published": "2026-08-03"}
    _write_index(season_1, season_2)

    data = _search(monkeypatch, query, command="resolve")["data"]

    top = [(row["id"], row["ranking"]["score"]) for row in data["candidates"][:2]]
    assert data["resolved"] is False
    assert [ref for ref, _ in top] == ["midnight-mythic-season-2-guide", "midnight-mythic-season-1-guide"]
    assert top[0][1] == top[1][1]


def test_search_ranks_transmog_pages_only_for_a_transmog_query(monkeypatch) -> None:
    """A transmog page names a class; only a query that asks for transmog may rank it."""
    _write_index(VORASIUS)

    plain = [row["id"] for row in _search(monkeypatch, "mage cloth chest")["data"]["results"]]
    transmog = [row["id"] for row in _search(monkeypatch, "transmog mage cloth chest")["data"]["results"]]

    assert "transmogrification-mage-cloth-chest-item-model-list" not in plain
    assert transmog[0] == "transmogrification-mage-cloth-chest-item-model-list"


def test_search_warns_when_the_local_index_is_over_a_week_old(monkeypatch) -> None:
    _write_index(VORASIUS, refreshed=NOW - timedelta(days=8))

    provenance = _search(monkeypatch, "voidspire")["provenance"]

    assert "last refreshed 2026-09-24" in provenance["site_index_warning"]
    assert "icy-veins index-refresh" in provenance["site_index_warning"]


def test_search_warns_when_the_live_menu_links_pages_the_index_lacks(monkeypatch) -> None:
    _write_index(VORASIUS)

    provenance = _search(monkeypatch, "voidspire", menu=("vorasius-raid-guide", "midnight-season-3-raid-guide"))["provenance"]

    assert "lacks 1 pages the live site menu links (midnight-season-3-raid-guide)" in provenance["site_index_warning"]


def test_search_without_a_local_index_says_where_its_pages_come_from(monkeypatch) -> None:
    payload = _search(monkeypatch, "frost mage", command="resolve")

    provenance = payload["provenance"]
    assert "No site index is available; run `icy-veins index-refresh`" in provenance["sitemap_warning"]
    assert "site_index_path" not in provenance and "site_index_warning" not in provenance
    assert payload["data"]["match"]["id"] == "frost-mage-pve-dps-guide"


def test_search_names_the_bundled_snapshot_until_the_user_refreshes(monkeypatch) -> None:
    bundled = load_site_index()
    assert bundled is not None and bundled.refreshed_at
    # Long past a week after the snapshot: its age is told in sitemap_warning, never as the local index's age warning.
    later = datetime.fromisoformat(bundled.refreshed_at).date() + timedelta(days=30)
    monkeypatch.setattr("icy_veins_cli.provider.date", type("FixedDate", (date,), {"today": classmethod(lambda cls: later)}))
    monkeypatch.setattr(IcyVeinsClient, "sitemap_guides", lambda self: parse_sitemap_guides(FROZEN_SITEMAP_XML))
    monkeypatch.setattr(IcyVeinsClient, "site_menu_guides", lambda self: [])

    provenance = json.loads(runner.invoke(app, ["search", "frost mage"]).stdout)["provenance"]

    assert provenance["site_index_path"] == bundled.path
    assert f"snapshot bundled with this release (refreshed {bundled.refreshed_at})" in provenance["sitemap_warning"]
    assert "site_index_warning" not in provenance


def test_search_does_not_read_the_index_while_the_sitemap_is_current(monkeypatch) -> None:
    current = FROZEN_SITEMAP_XML.replace("2025-10-05", datetime.now(UTC).date().isoformat())
    monkeypatch.setattr(IcyVeinsClient, "sitemap_guides", lambda self: parse_sitemap_guides(current))

    def must_not_read():
        raise AssertionError("the site index was read while the sitemap is current")

    monkeypatch.setattr("icy_veins_cli.search.load_site_index", must_not_read)
    payload = json.loads(runner.invoke(app, ["search", "frost mage"]).stdout)

    assert {row["metadata"]["source"] for row in payload["data"]["results"]} == {"sitemap"}
    assert not {"site_index_path", "sitemap_warning"} & set(payload["provenance"])


def test_doctor_reports_the_index_refresh_capability_and_the_index_in_use() -> None:
    data = json.loads(runner.invoke(app, ["doctor"]).stdout)["data"]

    assert data["capabilities"]["index_refresh"] == "ready"
    assert data["site_index"]["bundled"] is True and data["site_index"]["pages"] >= 100


def test_sitemap_text_is_the_body_sitemap_guides_ranks(monkeypatch) -> None:
    """The expansion rule needs every sitemap slug, classified or not, so index-refresh reads the body itself."""
    bodies = {ICY_VEINS_SITEMAP_URL: REFRESH_SITEMAP_XML}

    def fake_request(_client, url: str, **_kwargs) -> httpx.Response:
        return httpx.Response(200, text=bodies[url], request=httpx.Request("GET", url))

    monkeypatch.setattr("icy_veins_cli.client.request_with_retries", fake_request)
    with IcyVeinsClient() as client:
        assert client.sitemap_text() == REFRESH_SITEMAP_XML
        assert [row["slug"] for row in client.sitemap_guides()] == ["death-knight-guide", "drest-agath-normal-encounter-journal", "old-sitemap-guide"]

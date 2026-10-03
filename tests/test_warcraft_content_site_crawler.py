"""The pure site crawler over synthetic URL -> HTML maps: expansion, cap, aliases, blocks and a dead seed."""

from __future__ import annotations

import re

import pytest
from warcraft_content.site_crawler import FetchResult, PageLink, PageRead, crawl

SITE = "https://example.test"
LINK_RE = re.compile(r'<a href="(?P<href>[^"]+)"(?: class="(?P<cls>[^"]+)")?>')
CANONICAL_RE = re.compile(r'<link rel="canonical" href="(?P<href>[^"]+)">')


def page(*links: str, canonical: str | None = None, menu: tuple[str, ...] = ()) -> str:
    """A synthetic page: menu links carry class="menu", the rest are body links."""
    head = f'<link rel="canonical" href="{SITE}{canonical}">' if canonical else ""
    anchors = [f'<a href="{SITE}{href}" class="menu">' for href in menu] + [f'<a href="{SITE}{href}">' for href in links]
    return f"<html><head>{head}</head><body>{''.join(anchors)}</body></html>"


def read_page(url: str, html: str) -> PageRead | None:
    canonical = CANONICAL_RE.search(html)
    links = tuple(PageLink(match["href"], "menu" if match["cls"] == "menu" else "page") for match in LINK_RE.finditer(html))
    return PageRead(canonical["href"] if canonical else url, {"url": url}, links)


class Site:
    """Serves ``pages`` (path -> HTML or a ``FetchResult``) and records every fetch; unknown paths are 404s."""

    def __init__(self, pages: dict[str, str | FetchResult], *, cached: frozenset[str] = frozenset()) -> None:
        self.pages = pages
        self.cached = cached
        self.fetched: list[str] = []

    def fetch(self, url: str) -> FetchResult:
        path = url.removeprefix(SITE)
        self.fetched.append(path)
        body = self.pages.get(path)
        if body is None:
            return FetchResult(404)
        if isinstance(body, FetchResult):
            return body
        return FetchResult(200, body, cached=path in self.cached)


def run(site: Site, *, seeds: tuple[str, ...] = ("/seed",), max_requests: int = 50, known: frozenset[str] = frozenset(), revisit=()):
    """Crawl with the Icy Veins policy: follow menu links and any page link to a path not in ``known``."""
    return crawl(
        [SITE + path for path in seeds],
        fetch=site.fetch,
        read_page=read_page,
        should_expand=lambda link: link.source == "menu" or link.url.removeprefix(SITE) not in known,
        max_requests=max_requests,
        revisit=[SITE + path for path in revisit],
    )


def test_crawl_follows_menu_links_and_unknown_page_links_breadth_first() -> None:
    site = Site(
        {
            "/seed": page("/known", "/new-a", menu=("/menu-1",)),
            "/menu-1": page("/new-b", "/known"),
            "/new-a": page("/new-c"),
            "/new-b": page(),
            "/new-c": page(),
            "/known": page("/never"),
        }
    )

    result = run(site, known=frozenset({"/known"}))

    assert site.fetched == ["/seed", "/menu-1", "/new-a", "/new-b", "/new-c"]
    assert [crawled.source for crawled in result.pages] == ["seed", "menu", "page", "page", "page"]
    assert (result.partial, result.stop_reason, result.frontier) == (False, None, [])


def test_crawl_stops_at_the_request_cap_and_records_the_frontier_cached_pages_free() -> None:
    site = Site({"/seed": page("/a", "/b", "/c"), "/a": page(), "/b": page(), "/c": page()}, cached=frozenset({"/seed"}))

    result = run(site, max_requests=1)

    assert site.fetched == ["/seed", "/a"]
    assert (result.requests, result.cached) == (1, 1)
    assert result.stop_reason == "max_requests"
    assert result.frontier == [f"{SITE}/b", f"{SITE}/c"]


def test_crawl_records_a_redirect_as_an_alias_and_reports_404s() -> None:
    site = Site({"/seed": page("/old-name", "/gone"), "/old-name": page(canonical="/new-name")})

    result = run(site)

    assert result.aliases == {f"{SITE}/old-name": f"{SITE}/new-name"}
    assert result.not_found == [f"{SITE}/gone"]
    assert [crawled.page.url for crawled in result.pages] == [f"{SITE}/seed", f"{SITE}/new-name"]
    assert result.stop_reason is None


@pytest.mark.parametrize("blocked", [FetchResult(403), FetchResult(429), FetchResult(200, "<html>Just a moment</html>", challenge=True)])
def test_crawl_stops_at_the_first_block_and_never_retries_it(blocked: FetchResult) -> None:
    site = Site({"/seed": page("/a", "/b", "/c"), "/a": page(), "/b": blocked, "/c": page()})

    result = run(site)

    assert site.fetched == ["/seed", "/a", "/b"]
    assert result.stop_reason == "blocked"
    assert result.blocked == {"url": f"{SITE}/b", "status": blocked.status, "challenge": blocked.challenge}
    assert result.frontier == [f"{SITE}/b", f"{SITE}/c"]
    assert [crawled.requested_url for crawled in result.pages] == [f"{SITE}/seed", f"{SITE}/a"]


@pytest.mark.parametrize("seed", [page(), FetchResult(0, error="ConnectError: offline"), FetchResult(503)])
def test_crawl_fails_a_seed_it_cannot_read_or_that_lists_no_links(seed: str | FetchResult) -> None:
    """A seed page that lists no links means the reader no longer understands the site."""
    site = Site({"/seed": seed, "/other": page("/a")})

    result = run(site, seeds=("/seed", "/other"))

    assert result.stop_reason == "seed_failed"
    assert result.pages == []
    assert site.fetched == ["/seed"]
    assert len(result.errors) == 1


def test_crawl_revisits_known_pages_only_after_discovery_and_records_other_errors() -> None:
    site = Site({"/seed": page("/new"), "/new": page(), "/old-1": FetchResult(500), "/old-2": page()})

    result = run(site, revisit=("/old-1", "/old-2", "/new"))

    assert site.fetched == ["/seed", "/new", "/old-1", "/old-2"]
    assert [crawled.source for crawled in result.pages] == ["seed", "page", "revisit"]
    assert result.errors == [{"url": f"{SITE}/old-1", "status": 500, "error": "HTTP 500"}]
    assert result.stop_reason is None

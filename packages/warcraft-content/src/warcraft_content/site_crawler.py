"""Breadth-first crawl of one site for building a page index, with every side effect injected.

The provider supplies how a URL is fetched (its cache, pacing and HTTP client), how a page is read
(its identity, its index row and the links on it) and which links are worth following. This module
owns the walk and the rules that keep it polite: a request cap, a full stop at the first bot block,
and never a retry.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

# A site answers a crawler it has had enough of with one of these, or with a challenge page.
BLOCKED_STATUSES = frozenset({403, 429})
GONE_STATUSES = frozenset({404, 410})


@dataclass(frozen=True, slots=True)
class FetchResult:
    """One fetch: ``status`` is 0 when the request failed in transport (``error`` says why)."""

    status: int
    text: str = ""
    # A bot challenge (Cloudflare's ``cf-mitigated: challenge``), whatever the status says.
    challenge: bool = False
    # Served from the provider's page cache: no request was made, so it does not count against the cap.
    cached: bool = False
    error: str | None = None


@dataclass(frozen=True, slots=True)
class PageLink:
    url: str
    # Where on the page the link sits ("menu", "page"); the expansion predicate may weigh it.
    source: str


@dataclass(frozen=True, slots=True)
class PageRead:
    """What the provider read from one page; ``url`` is the page's own URL, which differs from the requested one after a redirect."""

    url: str
    row: dict[str, Any]
    links: tuple[PageLink, ...]


# Reads one fetched body: the page it is, its index row and its links; None when it is not a page of the site.
PageReader = Callable[[str, str], PageRead | None]


@dataclass(frozen=True, slots=True)
class CrawledPage:
    requested_url: str
    page: PageRead
    # Why the page was fetched: "seed", "revisit", or the source of the link that led to it.
    source: str


@dataclass(slots=True)
class CrawlResult:
    pages: list[CrawledPage] = field(default_factory=list)
    # Requested URL -> the URL the page says it is (a 301 rename).
    aliases: dict[str, str] = field(default_factory=dict)
    not_found: list[str] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    requests: int = 0
    cached: int = 0
    # None for a complete run; "blocked", "max_requests" or "seed_failed" for a partial one.
    stop_reason: str | None = None
    blocked: dict[str, Any] | None = None
    # Discovered URLs not fetched yet, for the next run to start from.
    frontier: list[str] = field(default_factory=list)

    @property
    def partial(self) -> bool:
        return self.stop_reason is not None


def crawl(
    seeds: Sequence[str],
    *,
    fetch: Callable[[str], FetchResult],
    read_page: PageReader,
    should_expand: Callable[[PageLink], bool],
    max_requests: int,
    revisit: Sequence[str] = (),
) -> CrawlResult:
    """Fetch ``seeds`` and every link ``should_expand`` accepts, breadth first, then ``revisit``.

    Discovery comes before revisits, so a capped run spends its requests on pages it has never seen.
    The first seed anchors the crawl: when it cannot be read, or reads with no links at all, the
    reader no longer understands the site and the run stops as ``seed_failed``. A 403, a 429 or a
    challenge stops the run at once (``blocked``) and nothing is retried; 404s are reported in
    ``not_found``; any other failure is recorded in ``errors`` and the walk goes on. ``read_page``
    returning None means the body is not one of the site's pages (a link that lands outside the
    indexed section); that is an error, not a block, so ``fetch`` must flag a challenge served as 200
    (``FetchResult.challenge``) for the run to stop on it.
    """
    result = CrawlResult()
    queue: deque[tuple[str, str]] = deque((url, "seed") for url in dict.fromkeys(seeds))
    # Queued or fetched; a revisit that discovery already fetched is skipped.
    seen = {url for url, _ in queue}
    revisits = deque(url for url in revisit if url not in seen)
    first_seed = seeds[0] if seeds else None
    while (item := _next_url(queue, revisits, seen)) is not None:
        url, source = item
        if result.requests >= max_requests:
            result.stop_reason = "max_requests"
            queue.appendleft(item)
            break
        page = _fetch_page(url, fetch, read_page, result)
        if result.stop_reason == "blocked":
            queue.appendleft(item)
            break
        if url == first_seed and (page is None or not page.links):
            if page is not None:
                result.errors.append({"url": url, "status": 200, "error": "the page listed no links"})
            result.stop_reason = "seed_failed"
            break
        if page is None:
            continue
        result.pages.append(CrawledPage(url, page, source))
        if page.url != url:
            result.aliases[url] = page.url
            seen.add(page.url)
        for link in page.links:
            if link.url not in seen and should_expand(link):
                seen.add(link.url)
                queue.append((link.url, link.source))
    result.frontier = [url for url, _ in queue]
    return result


def _next_url(queue: deque[tuple[str, str]], revisits: deque[str], seen: set[str]) -> tuple[str, str] | None:
    """The next discovered URL, else the next revisit nothing has fetched yet, else None."""
    while not queue and revisits:
        if (url := revisits.popleft()) not in seen:
            seen.add(url)
            return url, "revisit"
    return queue.popleft() if queue else None


def _fetch_page(url: str, fetch: Callable[[str], FetchResult], read_page: PageReader, result: CrawlResult) -> PageRead | None:
    """Fetch and read one URL, counting the request; a block sets ``result.stop_reason`` and returns None."""
    fetched = fetch(url)
    if fetched.cached:
        result.cached += 1
    else:
        result.requests += 1
    if fetched.challenge or fetched.status in BLOCKED_STATUSES:
        result.stop_reason = "blocked"
        result.blocked = {"url": url, "status": fetched.status, "challenge": fetched.challenge}
        return None
    return _read(url, fetched, read_page, result)


def _read(url: str, fetched: FetchResult, read_page: PageReader, result: CrawlResult) -> PageRead | None:
    """The page ``fetched`` holds, or None after recording why there is none."""
    if fetched.status in GONE_STATUSES:
        result.not_found.append(url)
        return None
    if fetched.status != 200:
        result.errors.append({"url": url, "status": fetched.status, "error": fetched.error or f"HTTP {fetched.status}"})
        return None
    page = read_page(url, fetched.text)
    if page is None:
        result.errors.append({"url": url, "status": fetched.status, "error": "the body is not a page of the site"})
    return page

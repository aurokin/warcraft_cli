from __future__ import annotations

from typing import Any

import httpx
from warcraft_content.site_client import GuideSite, GuideSiteClient


class _RecordingStore:
    def __init__(self) -> None:
        self.writes: dict[str, int] = {}
        self.values: dict[str, Any] = {}

    def get(self, key: str) -> Any | None:
        return self.values.get(key)

    def set(self, key: str, payload: Any, *, ttl_seconds: int) -> None:
        self.values[key] = payload
        self.writes[key.split(":", 1)[0]] = ttl_seconds


def test_guide_site_client_caches_sitemap_and_pages_with_their_own_ttls() -> None:
    store = _RecordingStore()
    fetched: list[str] = []

    def get_text(_client: httpx.Client, url: str) -> str:
        fetched.append(url)
        return f"body of {url}"

    site = GuideSite(
        label="Example",
        sitemap_url="https://example.test/sitemap.xml",
        parse_sitemap=lambda text: [{"slug": "a-guide", "text": text}],
        page_url=lambda ref: f"https://example.test/{ref}",
        parse_page=lambda html, *, source_url: {"html": html, "url": source_url},
    )
    with GuideSiteClient(
        site, cache_store=store, sitemap_ttl=86400, page_ttl=900, build_http_client=httpx.Client, get_text=get_text
    ) as client:
        client.sitemap_guides()
        assert client.fetch_guide_page("a-guide") == {"html": "body of https://example.test/a-guide", "url": "https://example.test/a-guide"}
        client.fetch_guide_page("a-guide")

    assert store.writes == {"sitemap": 86400, "guide_page_html": 900}
    assert fetched == ["https://example.test/sitemap.xml", "https://example.test/a-guide"]


def test_guide_site_client_hands_out_the_cached_sitemap_body_and_cached_pages_without_fetching() -> None:
    """``index-refresh`` reads every sitemap slug from the body, and reuses guide pages a ``guide`` call cached."""
    store = _RecordingStore()
    fetched: list[str] = []

    def get_text(_client: httpx.Client, url: str) -> str:
        fetched.append(url)
        return f"body of {url}"

    site = GuideSite(
        label="Example",
        sitemap_url="https://example.test/sitemap.xml",
        parse_sitemap=lambda text: [{"slug": "a-guide"}],
        page_url=lambda ref: f"https://example.test/{ref}",
        parse_page=lambda html, *, source_url: {"html": html},
    )
    with GuideSiteClient(
        site, cache_store=store, sitemap_ttl=86400, page_ttl=900, build_http_client=httpx.Client, get_text=get_text
    ) as client:
        assert client.cached_page_html("https://example.test/a-guide") is None
        client.guide_page_html("a-guide")
        assert client.sitemap_text() == "body of https://example.test/sitemap.xml"
        assert client.sitemap_guides() == [{"slug": "a-guide"}]
        assert client.cached_page_html("https://example.test/a-guide") == "body of https://example.test/a-guide"

    assert fetched == ["https://example.test/a-guide", "https://example.test/sitemap.xml"]

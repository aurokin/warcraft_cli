"""Cached sitemap and guide-page client shared by the sitemap-backed guide sites (Icy Veins, Method).

``warcraft_content`` may not import ``warcraft_api``, so each provider builds the cache store and the
HTTP calls from it and passes them in; this class owns the caching and the sitemap rules.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, Self

import httpx
from warcraft_core.provider import ProviderError


class CacheStore(Protocol):
    def get(self, key: str) -> Any | None: ...

    def set(self, key: str, payload: Any, *, ttl_seconds: int) -> None: ...


class GuidePageParser(Protocol):
    def __call__(self, html: str, *, source_url: str) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class GuideSite:
    """What differs between the guide sites: their sitemap, how a guide ref becomes a URL, and their parsers."""

    label: str
    sitemap_url: str
    parse_sitemap: Callable[[str], list[dict[str, Any]]]
    page_url: Callable[[str], str]
    parse_page: GuidePageParser


class GuideSiteClient:
    def __init__(
        self,
        site: GuideSite,
        *,
        cache_store: CacheStore | None,
        sitemap_ttl: int,
        page_ttl: int,
        build_http_client: Callable[[], httpx.Client],
        get_text: Callable[[httpx.Client, str], str],
    ) -> None:
        self._site = site
        self._cache_store = cache_store
        self._sitemap_ttl = sitemap_ttl
        self._page_ttl = page_ttl
        self._build_http_client = build_http_client
        self._get = get_text
        self._http_client: httpx.Client | None = None

    def close(self) -> None:
        if self._http_client is not None:
            self._http_client.close()
            self._http_client = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def _client(self) -> httpx.Client:
        if self._http_client is None:
            self._http_client = self._build_http_client()
        return self._http_client

    def _cache_key(self, namespace: str, url: str) -> str:
        raw = f"{namespace}|{url}".encode()
        return f"{namespace}:{hashlib.sha256(raw).hexdigest()}"

    def _read_cache(self, key: str) -> Any | None:
        if self._cache_store is None:
            return None
        return self._cache_store.get(key)

    def _write_cache(self, key: str, payload: Any, *, ttl_seconds: int) -> None:
        if self._cache_store is None:
            return
        self._cache_store.set(key, payload, ttl_seconds=ttl_seconds)

    def _fetch_text(self, url: str) -> str:
        return self._get(self._client(), url)

    def _get_text(self, url: str, *, namespace: str, ttl_seconds: int) -> str:
        key = self._cache_key(namespace, url)
        cached = self._read_cache(key)
        if isinstance(cached, str):
            return cached
        text = self._fetch_text(url)
        self._write_cache(key, text, ttl_seconds=ttl_seconds)
        return text

    def _sitemap(self) -> tuple[str, list[dict[str, Any]]]:
        sitemap_url = self._site.sitemap_url
        key = self._cache_key("sitemap", sitemap_url)
        cached = self._read_cache(key)
        if isinstance(cached, str) and (guides := self._site.parse_sitemap(cached)):
            return cached, guides
        text = self._fetch_text(sitemap_url)
        guides = self._site.parse_sitemap(text)
        if not guides:
            raise ProviderError(
                "parse_failed",
                f"The {self._site.label} sitemap listed no guide pages; its format has probably changed.",
                details={"sitemap_url": sitemap_url},
            )
        self._write_cache(key, text, ttl_seconds=self._sitemap_ttl)
        return text, guides

    def sitemap_guides(self) -> list[dict[str, Any]]:
        """Every supported guide the sitemap lists; a body that lists none fails as ``parse_failed`` and is not cached.

        A challenge page or a reshaped sitemap still answers 2xx, and ranking an empty list would
        report "no guide matches" for every query for as long as that body stayed cached.
        """
        return self._sitemap()[1]

    def sitemap_text(self) -> str:
        """The sitemap body ``sitemap_guides`` ranks, for callers that need the pages it does not classify."""
        return self._sitemap()[0]

    def cached_page_html(self, url: str) -> str | None:
        """The guide page body ``guide_page_html`` cached for ``url``, without making a request."""
        cached = self._read_cache(self._cache_key("guide_page_html", url))
        return cached if isinstance(cached, str) else None

    def guide_page_html(self, guide_ref: str) -> tuple[str, str]:
        url = self._site.page_url(guide_ref)
        html = self._get_text(url, namespace="guide_page_html", ttl_seconds=self._page_ttl)
        return url, html

    def fetch_guide_page(self, guide_ref: str) -> dict[str, Any]:
        url, html = self.guide_page_html(guide_ref)
        return self._site.parse_page(html, source_url=url)

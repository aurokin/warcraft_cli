from __future__ import annotations

import hashlib
from typing import Any

import httpx
from warcraft_api.cache import CacheSettings, CacheTTLConfig, build_cache_store, load_prefixed_cache_settings_from_env
from warcraft_api.http import DEFAULT_RETRY_ATTEMPTS, request_with_retries
from warcraft_content.paths import provider_cache_root

from method_cli.page_parser import guide_ref_parts, guide_url, parse_guide_page, parse_sitemap_guides

METHOD_BASE_URL = "https://www.method.gg"
METHOD_SITEMAP_URL = f"{METHOD_BASE_URL}/sitemap.xml"
DEFAULT_CACHE_DIR = provider_cache_root("method") / "http"


def load_method_cache_settings_from_env() -> tuple[CacheSettings, int, int]:
    settings = load_prefixed_cache_settings_from_env(
        env_prefix="METHOD",
        default_cache_dir=DEFAULT_CACHE_DIR,
        default_redis_prefix="method_cli",
        ttl_defaults=CacheTTLConfig(search_suggestions=86400, guide_page_html=3600, page_html=3600),
        ttl_env_overrides={
            "search_suggestions": "METHOD_SITEMAP_CACHE_TTL_SECONDS",
            "guide_page_html": "METHOD_PAGE_CACHE_TTL_SECONDS",
            "page_html": "METHOD_PAGE_CACHE_TTL_SECONDS",
        },
    )
    return settings, settings.ttls.search_suggestions, settings.ttls.page_html


class MethodClient:
    def __init__(
        self,
        *,
        timeout_seconds: float = 20.0,
        retry_attempts: int = DEFAULT_RETRY_ATTEMPTS,
    ) -> None:
        self._http_client: httpx.Client | None = None
        settings, sitemap_ttl, page_ttl = load_method_cache_settings_from_env()
        self._timeout_seconds = timeout_seconds
        self._retry_attempts = max(1, retry_attempts)
        self._cache_settings = settings
        self._cache_store = build_cache_store(settings) if settings.enabled else None
        self._sitemap_ttl = sitemap_ttl
        self._page_ttl = page_ttl

    def close(self) -> None:
        if self._http_client is not None:
            self._http_client.close()
            self._http_client = None

    def __enter__(self) -> MethodClient:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def _client(self) -> httpx.Client:
        if self._http_client is None:
            self._http_client = httpx.Client(timeout=self._timeout_seconds, follow_redirects=True)
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

    def _get_text(self, url: str, *, namespace: str, ttl_seconds: int) -> str:
        key = self._cache_key(namespace, url)
        cached = self._read_cache(key)
        if isinstance(cached, str):
            return cached
        response = request_with_retries(
            self._client(),
            url,
            retry_attempts=self._retry_attempts,
        )
        text = response.text
        self._write_cache(key, text, ttl_seconds=ttl_seconds)
        return text

    def sitemap_guides(self) -> list[dict[str, Any]]:
        xml_text = self._get_text(METHOD_SITEMAP_URL, namespace="sitemap", ttl_seconds=self._sitemap_ttl)
        return parse_sitemap_guides(xml_text)

    def guide_page_html(self, guide_ref: str) -> tuple[str, str]:
        slug, section_slug = guide_ref_parts(guide_ref)
        url = guide_url(slug, section_slug)
        html = self._get_text(url, namespace="guide_page_html", ttl_seconds=self._page_ttl)
        return url, html

    def fetch_guide_page(self, guide_ref: str) -> dict[str, Any]:
        url, html = self.guide_page_html(guide_ref)
        return parse_guide_page(html, source_url=url)

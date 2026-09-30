from __future__ import annotations

import hashlib
from typing import Any

import httpx
from warcraft_api.cache import CacheSettings, CacheTTLConfig, build_cache_store, load_prefixed_cache_settings_from_env
from warcraft_api.http import DEFAULT_RETRY_ATTEMPTS, build_client, request_with_retries
from warcraft_core.paths import provider_cache_root
from warcraft_core.provider import ProviderError

from icy_veins_cli.page_parser import guide_ref_parts, guide_url, parse_guide_page, parse_sitemap_guides

ICY_VEINS_BASE_URL = "https://www.icy-veins.com"
ICY_VEINS_SITEMAP_URL = f"{ICY_VEINS_BASE_URL}/sitemap.xml"
DEFAULT_CACHE_DIR = provider_cache_root("icy-veins") / "http"


def load_icy_veins_cache_settings_from_env() -> tuple[CacheSettings, int, int]:
    settings = load_prefixed_cache_settings_from_env(
        env_prefix="ICY_VEINS",
        default_cache_dir=DEFAULT_CACHE_DIR,
        default_redis_prefix="icy_veins_cli",
        ttl_defaults=CacheTTLConfig(search_suggestions=86400, page_html=3600),
        ttl_env_overrides={
            "search_suggestions": "ICY_VEINS_SITEMAP_CACHE_TTL_SECONDS",
            "page_html": "ICY_VEINS_PAGE_CACHE_TTL_SECONDS",
        },
    )
    return settings, settings.ttls.search_suggestions, settings.ttls.page_html


class IcyVeinsClient:
    def __init__(
        self,
        *,
        timeout_seconds: float = 20.0,
        retry_attempts: int = DEFAULT_RETRY_ATTEMPTS,
    ) -> None:
        self._http_client: httpx.Client | None = None
        settings, sitemap_ttl, page_ttl = load_icy_veins_cache_settings_from_env()
        self._timeout_seconds = timeout_seconds
        self._retry_attempts = max(1, retry_attempts)
        self._cache_store = build_cache_store(settings) if settings.enabled else None
        self._sitemap_ttl = sitemap_ttl
        self._page_ttl = page_ttl

    def close(self) -> None:
        if self._http_client is not None:
            self._http_client.close()
            self._http_client = None

    def __enter__(self) -> IcyVeinsClient:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def _client(self) -> httpx.Client:
        if self._http_client is None:
            self._http_client = build_client(timeout=self._timeout_seconds)
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
        return request_with_retries(self._client(), url, retry_attempts=self._retry_attempts).text

    def _get_text(self, url: str, *, namespace: str, ttl_seconds: int) -> str:
        key = self._cache_key(namespace, url)
        cached = self._read_cache(key)
        if isinstance(cached, str):
            return cached
        text = self._fetch_text(url)
        self._write_cache(key, text, ttl_seconds=ttl_seconds)
        return text

    def sitemap_guides(self) -> list[dict[str, Any]]:
        """Every supported guide the sitemap lists; a body that lists none fails as ``parse_failed`` and is not cached.

        A challenge page or a reshaped sitemap still answers 2xx, and ranking an empty list would
        report "no guide matches" for every query for as long as that body stayed cached.
        """
        key = self._cache_key("sitemap", ICY_VEINS_SITEMAP_URL)
        cached = self._read_cache(key)
        if isinstance(cached, str) and (guides := parse_sitemap_guides(cached)):
            return guides
        text = self._fetch_text(ICY_VEINS_SITEMAP_URL)
        guides = parse_sitemap_guides(text)
        if not guides:
            raise ProviderError(
                "parse_failed",
                "The Icy Veins sitemap listed no guide pages; its format has probably changed.",
                details={"sitemap_url": ICY_VEINS_SITEMAP_URL},
            )
        self._write_cache(key, text, ttl_seconds=self._sitemap_ttl)
        return guides

    def guide_page_html(self, guide_ref: str) -> tuple[str, str]:
        slug = guide_ref_parts(guide_ref)
        url = guide_url(slug)
        html = self._get_text(url, namespace="guide_page_html", ttl_seconds=self._page_ttl)
        return url, html

    def fetch_guide_page(self, guide_ref: str) -> dict[str, Any]:
        url, html = self.guide_page_html(guide_ref)
        return parse_guide_page(html, source_url=url)

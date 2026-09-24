from __future__ import annotations

import hashlib
import json
from typing import Any

import httpx
from warcraft_api.cache import CacheSettings, CacheTTLConfig, build_cache_store, load_prefixed_cache_settings_from_env
from warcraft_api.http import DEFAULT_RETRY_ATTEMPTS, build_client, request_with_retries
from warcraft_core.paths import provider_cache_root

from warcraft_wiki_cli.page_parser import normalize_article_ref, parse_article_page, parse_search_results

WIKI_API_URL = "https://warcraft.wiki.gg/api.php"
DEFAULT_CACHE_DIR = provider_cache_root("warcraft-wiki") / "http"
# MediaWiki searches the main namespace only by default. The wiki moved its API reference pages into
# the custom "API:" namespace (id 3000) and its game events into "Event:" (id 3004), leaving the old
# main-namespace titles as redirects, which list=search does not return. Without these ids search
# cannot see an API function or a game event at all.
SEARCH_NAMESPACES = "0|3000|3004"


class WarcraftWikiAPIError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def load_warcraft_wiki_cache_settings_from_env() -> tuple[CacheSettings, int, int]:
    settings = load_prefixed_cache_settings_from_env(
        env_prefix="WARCRAFT_WIKI",
        default_cache_dir=DEFAULT_CACHE_DIR,
        default_redis_prefix="warcraft_wiki_cli",
        ttl_defaults=CacheTTLConfig(search_suggestions=1800, page_html=3600),
        ttl_env_overrides={
            "search_suggestions": "WARCRAFT_WIKI_SEARCH_CACHE_TTL_SECONDS",
            "page_html": "WARCRAFT_WIKI_PAGE_CACHE_TTL_SECONDS",
        },
    )
    return settings, settings.ttls.search_suggestions, settings.ttls.page_html


class WarcraftWikiClient:
    def __init__(
        self,
        *,
        timeout_seconds: float = 20.0,
        retry_attempts: int = DEFAULT_RETRY_ATTEMPTS,
    ) -> None:
        self._http_client: httpx.Client | None = None
        settings, search_ttl, page_ttl = load_warcraft_wiki_cache_settings_from_env()
        self._timeout_seconds = timeout_seconds
        self._retry_attempts = max(1, retry_attempts)
        self._cache_store = build_cache_store(settings) if settings.enabled else None
        self._search_ttl = search_ttl
        self._page_ttl = page_ttl

    def close(self) -> None:
        if self._http_client is not None:
            self._http_client.close()
            self._http_client = None

    def __enter__(self) -> WarcraftWikiClient:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def _client(self) -> httpx.Client:
        if self._http_client is None:
            self._http_client = build_client(timeout=self._timeout_seconds)
        return self._http_client

    def _cache_key(self, namespace: str, params: dict[str, Any]) -> str:
        raw = json.dumps({"namespace": namespace, "params": params}, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return f"{namespace}:{hashlib.sha256(raw).hexdigest()}"

    def _read_cache(self, key: str) -> Any | None:
        if self._cache_store is None:
            return None
        return self._cache_store.get(key)

    def _write_cache(self, key: str, payload: Any, *, ttl_seconds: int) -> None:
        if self._cache_store is None:
            return
        self._cache_store.set(key, payload, ttl_seconds=ttl_seconds)

    def _api_json(self, *, namespace: str, ttl_seconds: int, params: dict[str, Any]) -> dict[str, Any]:
        key = self._cache_key(namespace, params)
        cached = self._read_cache(key)
        if isinstance(cached, dict):
            return cached
        response = request_with_retries(self._client(), WIKI_API_URL, params=params, retry_attempts=self._retry_attempts)
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            raise WarcraftWikiAPIError("upstream_error", "Warcraft Wiki API did not answer with a JSON object.")
        if isinstance(payload.get("error"), dict):
            error = payload["error"]
            raise WarcraftWikiAPIError(str(error.get("code") or "api_error"), str(error.get("info") or "Warcraft Wiki API error."))
        self._write_cache(key, payload, ttl_seconds=ttl_seconds)
        return payload

    def search_articles(self, query: str, *, limit: int) -> tuple[int, list[dict[str, Any]]]:
        payload = self._api_json(
            namespace="search",
            ttl_seconds=self._search_ttl,
            params={
                "action": "query",
                "list": "search",
                "srsearch": query,
                "srlimit": limit,
                "srnamespace": SEARCH_NAMESPACES,
                "format": "json",
            },
        )
        return parse_search_results(payload)

    def fetch_article_page(self, article_ref: str) -> dict[str, Any]:
        title = normalize_article_ref(article_ref)
        payload = self._api_json(
            namespace="article_parse",
            ttl_seconds=self._page_ttl,
            params={
                "action": "parse",
                "page": title,
                "prop": "text|sections|displaytitle",
                "format": "json",
                "redirects": 1,
            },
        )
        return parse_article_page(payload, source_title=title)

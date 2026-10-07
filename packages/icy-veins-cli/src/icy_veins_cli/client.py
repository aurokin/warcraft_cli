from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import httpx
from warcraft_api.cache import CacheSettings, CacheTTLConfig, build_cache_store, load_prefixed_cache_settings_from_env
from warcraft_api.http import DEFAULT_RATE_LIMITER, DEFAULT_RETRY_ATTEMPTS, HostRateLimiter, build_client, request_with_retries
from warcraft_content.site_client import GuideSite, GuideSiteClient
from warcraft_content.site_crawler import FetchResult
from warcraft_core.paths import provider_cache_root
from warcraft_core.provider import ProviderError

from icy_veins_cli.page_parser import guide_ref_parts, guide_url, parse_guide_page, parse_site_menu_guides, parse_sitemap_guides
from icy_veins_cli.talent_calculator import ConversionError, convert_calculator_builds

ICY_VEINS_BASE_URL = "https://www.icy-veins.com"
ICY_VEINS_SITEMAP_URL = f"{ICY_VEINS_BASE_URL}/sitemap.xml"
# Any WoW guide page carries the site-wide guide menu; a class hub's URL has outlived every expansion,
# where an expansion or season hub is retired when the next one ships.
SITE_MENU_SEED_URL = guide_url("death-knight-guide")
ICY_VEINS_SITE = GuideSite(
    label="Icy Veins",
    sitemap_url=ICY_VEINS_SITEMAP_URL,
    parse_sitemap=parse_sitemap_guides,
    page_url=lambda guide_ref: guide_url(guide_ref_parts(guide_ref)),
    parse_page=parse_guide_page,
)
# ``index-refresh`` requests a couple of hundred uncached pages from an origin the CDN does not cache,
# so it waits at least this long between requests, or WARCRAFT_HTTP_MIN_INTERVAL_SECONDS when that is longer.
INDEX_REFRESH_MIN_INTERVAL_SECONDS = 1.0


class _IndexRefreshRateLimiter(HostRateLimiter):
    @property
    def min_interval_seconds(self) -> float:
        return max(INDEX_REFRESH_MIN_INTERVAL_SECONDS, DEFAULT_RATE_LIMITER.min_interval_seconds)


INDEX_REFRESH_RATE_LIMITER = _IndexRefreshRateLimiter()


def _tree_json(text: str) -> Mapping[str, Any] | None:
    try:
        tree_data = json.loads(text)
    except ValueError:
        return None
    return tree_data if isinstance(tree_data, dict) and isinstance(tree_data.get("specs"), dict) else None


def _challenged(response: httpx.Response) -> bool:
    """Cloudflare marks a bot challenge with ``cf-mitigated: challenge``, whatever the status code."""
    return str(response.headers.get("cf-mitigated", "")).lower() == "challenge"


def load_icy_veins_cache_settings_from_env() -> tuple[CacheSettings, int, int]:
    settings = load_prefixed_cache_settings_from_env(
        env_prefix="ICY_VEINS",
        default_cache_dir=provider_cache_root("icy-veins") / "http",
        default_redis_prefix="icy_veins_cli",
        ttl_defaults=CacheTTLConfig(search_suggestions=86400, page_html=3600),
        ttl_env_overrides={
            "search_suggestions": "ICY_VEINS_SITEMAP_CACHE_TTL_SECONDS",
            "page_html": "ICY_VEINS_PAGE_CACHE_TTL_SECONDS",
        },
    )
    return settings, settings.ttls.search_suggestions, settings.ttls.page_html


class IcyVeinsClient(GuideSiteClient):
    def __init__(self) -> None:
        settings, sitemap_ttl, page_ttl = load_icy_veins_cache_settings_from_env()
        super().__init__(
            ICY_VEINS_SITE,
            cache_store=build_cache_store(settings) if settings.enabled else None,
            sitemap_ttl=sitemap_ttl,
            page_ttl=page_ttl,
            build_http_client=lambda: build_client(timeout=20.0),
            get_text=lambda client, url: request_with_retries(client, url, retry_attempts=DEFAULT_RETRY_ATTEMPTS).text,
        )
        self._tree_data: dict[str, Mapping[str, Any]] = {}
        # A failed read is not retried for the client's life: each calculator row asks for its class's
        # tree, and every request against a hanging static host costs a full retry cycle.
        self._tree_failures: dict[str, ConversionError] = {}

    def talent_tree_data(self, url: str) -> Mapping[str, Any]:
        """The talent calculator's tree data for one class, cached like a guide page and held for the client's life.

        Only a body that parses as the tree data is cached, so a challenge page is not served from the
        cache. Fails as ``ConversionError``, remembered for the client's life: a guide page whose builds
        cannot be converted is still read.
        """
        if (tree_data := self._tree_data.get(url)) is not None:
            return tree_data
        if (failure := self._tree_failures.get(url)) is not None:
            raise failure
        try:
            tree_data = self._read_tree_data(url)
        except ConversionError as exc:
            self._tree_failures[url] = exc
            raise
        self._tree_data[url] = tree_data
        return tree_data

    def _read_tree_data(self, url: str) -> Mapping[str, Any]:
        key = self._cache_key("talent_tree_json", url)
        cached = self._read_cache(key)
        tree_data = _tree_json(cached) if isinstance(cached, str) else None
        if tree_data is None:
            try:
                text = self._fetch_text(url)
            except httpx.HTTPError as exc:
                raise ConversionError(f"could not read the calculator's tree data from {url} ({type(exc).__name__}: {exc})") from exc
            if (tree_data := _tree_json(text)) is None:
                raise ConversionError(f"the calculator's tree data at {url} is not the JSON tree it used to be")
            self._write_cache(key, text, ttl_seconds=self._page_ttl)
        return tree_data

    def fetch_guide_page(self, guide_ref: str) -> dict[str, Any]:
        """The parsed guide page, with each talent calculator build converted to a WoW import string.

        A PvP page keeps the calculator URL next to the import string, since its hash also names the
        PvP talents; other pages keep it for a build whose hash names PvP talents or that could not be
        converted.
        """
        payload = super().fetch_guide_page(guide_ref)
        payload["build_references"] = convert_calculator_builds(
            payload["build_references"],
            load_tree=self.talent_tree_data,
            keep_calculator_urls=payload["guide"]["content_family"] == "pvp",
        )
        return payload

    def site_menu_guides(self) -> list[dict[str, Any]]:
        """Every supported guide the site-wide menu links; a page whose menu lists none fails as ``parse_failed``."""
        _, html = self.guide_page_html(SITE_MENU_SEED_URL)
        guides = parse_site_menu_guides(html)
        if not guides:
            raise ProviderError(
                "parse_failed",
                f"The Icy Veins guide menu on {SITE_MENU_SEED_URL} listed no guide pages; its layout has probably changed.",
                details={"page_url": SITE_MENU_SEED_URL},
            )
        return guides

    def crawl_fetch(self, url: str) -> FetchResult:
        """One ``index-refresh`` fetch: a cached guide page when there is one, else a single paced request.

        Never retried, 429 included: a site that has started refusing a crawler is left alone. A page
        read is cached like ``guide_page_html`` caches it, so search and guide reuse it.
        """
        cached = self.cached_page_html(url)
        if cached is not None:
            return FetchResult(200, cached, cached=True)
        try:
            response = request_with_retries(self._client(), url, retry_attempts=1, rate_limiter=INDEX_REFRESH_RATE_LIMITER)
        except httpx.HTTPStatusError as exc:
            return FetchResult(exc.response.status_code, challenge=_challenged(exc.response))
        except httpx.RequestError as exc:
            return FetchResult(0, error=f"{type(exc).__name__}: {exc}")
        challenge = _challenged(response)
        if response.status_code == 200 and not challenge:
            self.cache_page_html(url, response.text)
        return FetchResult(response.status_code, response.text, challenge=challenge)

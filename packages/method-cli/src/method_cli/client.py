from __future__ import annotations

from warcraft_api.cache import CacheSettings, CacheTTLConfig, build_cache_store, load_prefixed_cache_settings_from_env
from warcraft_api.http import DEFAULT_RETRY_ATTEMPTS, build_client, request_with_retries
from warcraft_content.site_client import GuideSite, GuideSiteClient
from warcraft_core.paths import provider_cache_root

from method_cli.page_parser import guide_ref_parts, guide_url, parse_guide_page, parse_sitemap_guides

METHOD_BASE_URL = "https://www.method.gg"
METHOD_SITEMAP_URL = f"{METHOD_BASE_URL}/sitemap.xml"
DEFAULT_CACHE_DIR = provider_cache_root("method") / "http"
METHOD_SITE = GuideSite(
    label="Method",
    sitemap_url=METHOD_SITEMAP_URL,
    parse_sitemap=parse_sitemap_guides,
    page_url=lambda guide_ref: guide_url(*guide_ref_parts(guide_ref)),
    parse_page=parse_guide_page,
)


def load_method_cache_settings_from_env() -> tuple[CacheSettings, int, int]:
    settings = load_prefixed_cache_settings_from_env(
        env_prefix="METHOD",
        default_cache_dir=DEFAULT_CACHE_DIR,
        default_redis_prefix="method_cli",
        ttl_defaults=CacheTTLConfig(search_suggestions=86400, page_html=3600),
        ttl_env_overrides={
            "search_suggestions": "METHOD_SITEMAP_CACHE_TTL_SECONDS",
            "page_html": "METHOD_PAGE_CACHE_TTL_SECONDS",
        },
    )
    return settings, settings.ttls.search_suggestions, settings.ttls.page_html


class MethodClient(GuideSiteClient):
    def __init__(self) -> None:
        settings, sitemap_ttl, page_ttl = load_method_cache_settings_from_env()
        super().__init__(
            METHOD_SITE,
            cache_store=build_cache_store(settings) if settings.enabled else None,
            sitemap_ttl=sitemap_ttl,
            page_ttl=page_ttl,
            build_http_client=lambda: build_client(timeout=20.0),
            get_text=lambda client, url: request_with_retries(client, url, retry_attempts=DEFAULT_RETRY_ATTEMPTS).text,
        )

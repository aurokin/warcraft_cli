from __future__ import annotations

from typing import Any

from warcraft_api.cache import CacheSettings, CacheTTLConfig, build_cache_store, load_prefixed_cache_settings_from_env
from warcraft_api.http import DEFAULT_RETRY_ATTEMPTS, build_client, request_with_retries
from warcraft_content.site_client import GuideSite, GuideSiteClient
from warcraft_core.paths import provider_cache_root
from warcraft_core.provider import ProviderError

from icy_veins_cli.page_parser import guide_ref_parts, guide_url, parse_guide_page, parse_site_menu_guides, parse_sitemap_guides

ICY_VEINS_BASE_URL = "https://www.icy-veins.com"
ICY_VEINS_SITEMAP_URL = f"{ICY_VEINS_BASE_URL}/sitemap.xml"
DEFAULT_CACHE_DIR = provider_cache_root("icy-veins") / "http"
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

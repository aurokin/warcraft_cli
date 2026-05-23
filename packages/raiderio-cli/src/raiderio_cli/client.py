from __future__ import annotations

import hashlib
import json
from typing import Any

import httpx
from warcraft_api.cache import CacheSettings, CacheTTLConfig, build_cache_store, load_prefixed_cache_settings_from_env
from warcraft_api.http import DEFAULT_RETRY_ATTEMPTS, request_with_retries
from warcraft_content.paths import provider_cache_root
from warcraft_core.wow_normalization import normalize_name, normalize_region, primary_realm_slug, realm_slug_variants

RAIDERIO_BASE_URL = "https://raider.io/api/v1"
RAIDERIO_SITE_BASE_URL = "https://raider.io"
DEFAULT_CACHE_DIR = provider_cache_root("raiderio") / "http"
DEFAULT_CHARACTER_FIELDS = ",".join(
    (
        "guild",
        "raid_progression",
        "mythic_plus_scores_by_season:current",
        "mythic_plus_ranks",
        "mythic_plus_recent_runs",
    )
)
DEFAULT_GUILD_FIELDS = ",".join(("raid_progression", "raid_rankings", "members"))


def load_raiderio_cache_settings_from_env() -> tuple[CacheSettings, int, int, int, int]:
    settings = load_prefixed_cache_settings_from_env(
        env_prefix="RAIDERIO",
        default_cache_dir=DEFAULT_CACHE_DIR,
        default_redis_prefix="raiderio_cli",
        ttl_defaults=CacheTTLConfig(
            search_suggestions=21600,
            entity_page_html=900,
            guide_page_html=900,
            page_html=300,
        ),
        ttl_env_overrides={
            "search_suggestions": "RAIDERIO_STATIC_CACHE_TTL_SECONDS",
            "entity_page_html": "RAIDERIO_CHARACTER_CACHE_TTL_SECONDS",
            "guide_page_html": "RAIDERIO_GUILD_CACHE_TTL_SECONDS",
            "page_html": "RAIDERIO_MPLUS_RUNS_CACHE_TTL_SECONDS",
        },
    )
    return (
        settings,
        settings.ttls.search_suggestions,
        settings.ttls.entity_page_html,
        settings.ttls.guide_page_html,
        settings.ttls.page_html,
    )


class RaiderIOClient:
    def __init__(
        self,
        *,
        timeout_seconds: float = 20.0,
        retry_attempts: int = DEFAULT_RETRY_ATTEMPTS,
    ) -> None:
        self._http_client: httpx.Client | None = None
        settings, static_ttl, character_ttl, guild_ttl, mplus_runs_ttl = load_raiderio_cache_settings_from_env()
        self._timeout_seconds = timeout_seconds
        self._retry_attempts = max(1, retry_attempts)
        self._cache_settings = settings
        self._cache_store = build_cache_store(settings) if settings.enabled else None
        self._static_ttl = static_ttl
        self._character_ttl = character_ttl
        self._guild_ttl = guild_ttl
        self._mplus_runs_ttl = mplus_runs_ttl

    def close(self) -> None:
        if self._http_client is not None:
            self._http_client.close()
            self._http_client = None

    def __enter__(self) -> RaiderIOClient:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def _client(self) -> httpx.Client:
        if self._http_client is None:
            self._http_client = httpx.Client(timeout=self._timeout_seconds, follow_redirects=True)
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

    def _get_json(self, path: str, *, params: dict[str, Any], namespace: str, ttl_seconds: int) -> dict[str, Any]:
        key = self._cache_key(namespace, params)
        cached = self._read_cache(key)
        if isinstance(cached, dict):
            return cached
        response = request_with_retries(
            self._client(),
            f"{RAIDERIO_BASE_URL}{path}",
            params=params,
            retry_attempts=self._retry_attempts,
        )
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError(f"Unexpected Raider.IO response shape for {path}.")
        self._write_cache(key, payload, ttl_seconds=ttl_seconds)
        return payload

    def _get_site_json(self, path: str, *, params: dict[str, Any], namespace: str, ttl_seconds: int) -> dict[str, Any]:
        key = self._cache_key(namespace, params)
        cached = self._read_cache(key)
        if isinstance(cached, dict):
            return cached
        response = request_with_retries(
            self._client(),
            f"{RAIDERIO_SITE_BASE_URL}{path}",
            params=params,
            retry_attempts=self._retry_attempts,
        )
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError(f"Unexpected Raider.IO response shape for {path}.")
        self._write_cache(key, payload, ttl_seconds=ttl_seconds)
        return payload

    def character_profile(self, *, region: str, realm: str, name: str, fields: str = DEFAULT_CHARACTER_FIELDS) -> dict[str, Any]:
        return self._get_json(
            "/characters/profile",
            params={"region": region, "realm": realm, "name": name, "fields": fields},
            namespace="character_profile",
            ttl_seconds=self._character_ttl,
        )

    def character_profile_variants(self, *, region: str, realm: str, name: str, fields: str = DEFAULT_CHARACTER_FIELDS) -> dict[str, Any]:
        normalized_region = normalize_region(region)
        normalized_name = normalize_name(name)
        variants = realm_slug_variants(realm) or [primary_realm_slug(realm)]
        last_error: httpx.HTTPStatusError | None = None
        for candidate in variants:
            try:
                return self.character_profile(region=normalized_region, realm=candidate, name=normalized_name, fields=fields)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 404:
                    raise
                last_error = exc
        if last_error is not None:
            raise last_error
        return self.character_profile(region=normalized_region, realm=primary_realm_slug(realm), name=normalized_name, fields=fields)

    def guild_profile(self, *, region: str, realm: str, name: str, fields: str = DEFAULT_GUILD_FIELDS) -> dict[str, Any]:
        return self._get_json(
            "/guilds/profile",
            params={"region": region, "realm": realm, "name": name, "fields": fields},
            namespace="guild_profile",
            ttl_seconds=self._guild_ttl,
        )

    def guild_profile_variants(self, *, region: str, realm: str, name: str, fields: str = DEFAULT_GUILD_FIELDS) -> dict[str, Any]:
        normalized_region = normalize_region(region)
        normalized_name = normalize_name(name)
        variants = realm_slug_variants(realm) or [primary_realm_slug(realm)]
        last_error: httpx.HTTPStatusError | None = None
        for candidate in variants:
            try:
                return self.guild_profile(region=normalized_region, realm=candidate, name=normalized_name, fields=fields)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 404:
                    raise
                last_error = exc
        if last_error is not None:
            raise last_error
        return self.guild_profile(region=normalized_region, realm=primary_realm_slug(realm), name=normalized_name, fields=fields)

    def mythic_plus_runs(
        self,
        *,
        season: str | None = None,
        region: str = "world",
        dungeon: str = "all",
        affixes: str | None = None,
        page: int = 0,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "region": region,
            "dungeon": dungeon,
            "page": page,
        }
        if season:
            params["season"] = season
        if affixes:
            params["affixes"] = affixes
        return self._get_json(
            "/mythic-plus/runs",
            params=params,
            namespace="mythic_plus_runs",
            ttl_seconds=self._mplus_runs_ttl,
        )

    def search(self, *, term: str, kind: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"term": term}
        if kind and kind != "all":
            params["type"] = kind
        return self._get_site_json(
            "/api/search",
            params=params,
            namespace="search",
            ttl_seconds=self._static_ttl,
        )

    @property
    def mythic_plus_runs_ttl_seconds(self) -> int:
        return self._mplus_runs_ttl

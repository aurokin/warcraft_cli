from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from warcraft_api.cache import CacheSettings, CacheTTLConfig, build_cache_store, load_prefixed_cache_settings_from_env
from warcraft_api.http import DEFAULT_RETRY_ATTEMPTS, build_client, request_with_retries
from warcraft_core.paths import provider_cache_root
from warcraft_core.provider import ProviderError
from warcraft_core.wow_normalization import normalize_name, normalize_region, primary_realm_slug

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


def load_raiderio_cache_settings_from_env() -> tuple[CacheSettings, int, int, int, int, int]:
    """Resolve cache settings plus the static, character, guild, M+ runs, and raid rankings TTLs."""
    settings = load_prefixed_cache_settings_from_env(
        env_prefix="RAIDERIO",
        default_cache_dir=DEFAULT_CACHE_DIR,
        default_redis_prefix="raiderio_cli",
        ttl_defaults=CacheTTLConfig(
            search_suggestions=21600,
            entity_page_html=900,
            guide_page_html=900,
            page_html=300,
            entity_response=900,
        ),
        ttl_env_overrides={
            "search_suggestions": "RAIDERIO_STATIC_CACHE_TTL_SECONDS",
            "entity_page_html": "RAIDERIO_CHARACTER_CACHE_TTL_SECONDS",
            "guide_page_html": "RAIDERIO_GUILD_CACHE_TTL_SECONDS",
            "page_html": "RAIDERIO_MPLUS_RUNS_CACHE_TTL_SECONDS",
            "entity_response": "RAIDERIO_RAID_RANKINGS_CACHE_TTL_SECONDS",
        },
    )
    return (
        settings,
        settings.ttls.search_suggestions,
        settings.ttls.entity_page_html,
        settings.ttls.guide_page_html,
        settings.ttls.page_html,
        settings.ttls.entity_response,
    )


@dataclass(frozen=True, slots=True)
class FetchedJson:
    """One Raider.IO response plus when it actually came off the wire.

    ``fetched_at`` is stored alongside the cached body, so a cache hit reports the age of the data
    it is replaying instead of the time the command happened to run.
    """

    payload: dict[str, Any]
    fetched_at: str
    cache_hit: bool


def combined_freshness(pages: Sequence[FetchedJson]) -> tuple[str, bool]:
    """The oldest fetch time across the pages a sample read, and whether any of them was replayed.

    A sample is only as fresh as its stalest page, so the oldest time is the honest one to report.
    """
    return min(page.fetched_at for page in pages), any(page.cache_hit for page in pages)


def page_freshness(fetched: FetchedJson, *, cache_ttl_seconds: int) -> dict[str, Any]:
    """The freshness block for one response: when it came off the wire, and whether it was replayed.

    ``fetched_at`` is the stored fetch time rather than the time the command ran, so a ``cache_hit``
    replay reports the age of the data it replayed. ``cache_ttl_seconds`` is how stale it may get.
    """
    return {
        "fetched_at": fetched.fetched_at,
        "cache_hit": fetched.cache_hit,
        "cache_ttl_seconds": cache_ttl_seconds,
    }


class RaiderIOClient:
    """Cached Raider.IO API access.

    Every endpoint a command reports provenance for returns :class:`FetchedJson`, so the command can
    quote the real fetch time; only site search, which carries no provenance, returns a bare body.
    """

    def __init__(
        self,
        *,
        timeout_seconds: float = 20.0,
        retry_attempts: int = DEFAULT_RETRY_ATTEMPTS,
    ) -> None:
        self._http_client: httpx.Client | None = None
        settings, static_ttl, character_ttl, guild_ttl, mplus_runs_ttl, raid_rankings_ttl = load_raiderio_cache_settings_from_env()
        self._timeout_seconds = timeout_seconds
        self._retry_attempts = max(1, retry_attempts)
        self._cache_store = build_cache_store(settings) if settings.enabled else None
        self._static_ttl = static_ttl
        self._character_ttl = character_ttl
        self._guild_ttl = guild_ttl
        self._mplus_runs_ttl = mplus_runs_ttl
        self._raid_rankings_ttl = raid_rankings_ttl

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
            self._http_client = build_client(timeout=self._timeout_seconds)
        return self._http_client

    def _cache_key(self, namespace: str, params: dict[str, Any]) -> str:
        raw = json.dumps({"namespace": namespace, "params": params}, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return f"{namespace}:{hashlib.sha256(raw).hexdigest()}"

    def _read_cache(self, key: str) -> FetchedJson | None:
        """Replay a cached response; an entry written before fetch times were stored is a miss."""
        cached = self._cache_store.get(key) if self._cache_store is not None else None
        if not isinstance(cached, dict):
            return None
        payload = cached.get("payload")
        fetched_at = cached.get("fetched_at")
        if not isinstance(payload, dict) or not isinstance(fetched_at, str):
            return None
        return FetchedJson(payload=payload, fetched_at=fetched_at, cache_hit=True)

    def _write_cache(self, key: str, fetched: FetchedJson, *, ttl_seconds: int) -> None:
        if self._cache_store is None:
            return
        self._cache_store.set(key, {"fetched_at": fetched.fetched_at, "payload": fetched.payload}, ttl_seconds=ttl_seconds)

    def _get_json(self, url: str, *, params: dict[str, Any], namespace: str, ttl_seconds: int) -> FetchedJson:
        key = self._cache_key(namespace, params)
        cached = self._read_cache(key)
        if cached is not None:
            return cached
        response = request_with_retries(self._client(), url, params=params, retry_attempts=self._retry_attempts)
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            raise ProviderError("upstream_error", f"Raider.IO did not answer {url} with a JSON object.", details={"url": url})
        fetched = FetchedJson(payload=payload, fetched_at=datetime.now(UTC).isoformat(), cache_hit=False)
        self._write_cache(key, fetched, ttl_seconds=ttl_seconds)
        return fetched

    def _profile(self, *, path: str, namespace: str, ttl_seconds: int, region: str, realm: str, name: str, fields: str) -> FetchedJson:
        """One profile plus its fetch time.

        Raider.IO accepts either slug spelling of a realm (``mal-ganis`` and ``malganis`` both find
        Mal'Ganis) and answers an unknown realm with HTTP 400 "Failed to find realm", so one request
        per lookup is enough.
        """
        params = {"region": normalize_region(region), "realm": primary_realm_slug(realm), "name": normalize_name(name), "fields": fields}
        return self._get_json(f"{RAIDERIO_BASE_URL}/{path}", params=params, namespace=namespace, ttl_seconds=ttl_seconds)

    def character_profile(self, *, region: str, realm: str, name: str, fields: str = DEFAULT_CHARACTER_FIELDS) -> FetchedJson:
        """One character profile plus its fetch time."""
        return self._profile(
            path="characters/profile",
            namespace="character_profile",
            ttl_seconds=self._character_ttl,
            region=region,
            realm=realm,
            name=name,
            fields=fields,
        )

    def character_profile_variants(self, *, region: str, realm: str, name: str, fields: str = DEFAULT_CHARACTER_FIELDS) -> dict[str, Any]:
        """The profile body alone, for the search/resolve probes that report no freshness."""
        return self.character_profile(region=region, realm=realm, name=name, fields=fields).payload

    def guild_profile(self, *, region: str, realm: str, name: str, fields: str = DEFAULT_GUILD_FIELDS) -> FetchedJson:
        """One guild profile plus its fetch time."""
        return self._profile(
            path="guilds/profile",
            namespace="guild_profile",
            ttl_seconds=self._guild_ttl,
            region=region,
            realm=realm,
            name=name,
            fields=fields,
        )

    def guild_profile_variants(self, *, region: str, realm: str, name: str, fields: str = DEFAULT_GUILD_FIELDS) -> dict[str, Any]:
        """The profile body alone, for the search/resolve probes that report no freshness."""
        return self.guild_profile(region=region, realm=realm, name=name, fields=fields).payload

    def mythic_plus_runs(
        self,
        *,
        season: str | None = None,
        region: str = "world",
        dungeon: str = "all",
        affixes: str | None = None,
        page: int = 0,
    ) -> FetchedJson:
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
            f"{RAIDERIO_BASE_URL}/mythic-plus/runs",
            params=params,
            namespace="mythic_plus_runs",
            ttl_seconds=self._mplus_runs_ttl,
        )

    def raid_rankings(
        self,
        *,
        raid: str,
        difficulty: str,
        region: str,
        realm: str | None = None,
        limit: int,
        page: int,
    ) -> FetchedJson:
        """One page of guild raid rankings; ``limit`` is the API page size (1-200)."""
        params: dict[str, Any] = {
            "raid": raid,
            "difficulty": difficulty,
            "region": region,
            "limit": limit,
            "page": page,
        }
        if realm:
            params["realm"] = realm
        return self._get_json(
            f"{RAIDERIO_BASE_URL}/raiding/raid-rankings",
            params=params,
            namespace="raid_rankings",
            ttl_seconds=self._raid_rankings_ttl,
        )

    def raid_static_data(self, *, expansion_id: int) -> FetchedJson:
        """Raid and encounter slugs for one expansion (11 = Midnight, 10 = The War Within, ...)."""
        return self._get_json(
            f"{RAIDERIO_BASE_URL}/raiding/static-data",
            params={"expansion_id": expansion_id},
            namespace="raid_static_data",
            ttl_seconds=self._static_ttl,
        )

    def search(self, *, term: str, kind: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"term": term}
        if kind and kind != "all":
            params["type"] = kind
        return self._get_json(
            f"{RAIDERIO_SITE_BASE_URL}/api/search",
            params=params,
            namespace="search",
            ttl_seconds=self._static_ttl,
        ).payload

    @property
    def character_profile_ttl_seconds(self) -> int:
        return self._character_ttl

    @property
    def guild_profile_ttl_seconds(self) -> int:
        return self._guild_ttl

    @property
    def static_data_ttl_seconds(self) -> int:
        return self._static_ttl

    @property
    def mythic_plus_runs_ttl_seconds(self) -> int:
        return self._mplus_runs_ttl

    @property
    def raid_rankings_ttl_seconds(self) -> int:
        return self._raid_rankings_ttl

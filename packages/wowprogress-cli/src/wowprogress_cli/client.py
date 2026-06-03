from __future__ import annotations

import hashlib
import json
import time
from typing import Any
from urllib.parse import urlencode, urlparse

from curl_cffi import requests
from warcraft_api.cache import CacheSettings, CacheTTLConfig, build_cache_store, load_prefixed_cache_settings_from_env
from warcraft_api.http import DEFAULT_RETRY_ATTEMPTS, RETRYABLE_STATUS_CODES, backoff_seconds
from warcraft_content.paths import provider_cache_root
from warcraft_core.wow_normalization import normalize_name, normalize_region, primary_realm_slug, realm_slug_variants

from wowprogress_cli.identity import _progress_snapshot
from wowprogress_cli.page_parser import (
    WOWPROGRESS_BASE_URL,
    character_url,
    guild_url,
    leaderboard_url,
    parse_character_page,
    parse_guild_page,
    parse_pve_leaderboard_page,
)

DEFAULT_CACHE_DIR = provider_cache_root("wowprogress") / "http"
DEFAULT_IMPERSONATE = "chrome136"


class WowProgressClientError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def load_wowprogress_cache_settings_from_env() -> tuple[CacheSettings, int, int, int]:
    settings = load_prefixed_cache_settings_from_env(
        env_prefix="WOWPROGRESS",
        default_cache_dir=DEFAULT_CACHE_DIR,
        default_redis_prefix="wowprogress_cli",
        ttl_defaults=CacheTTLConfig(entity_page_html=900, guide_page_html=900, page_html=300),
        ttl_env_overrides={
            "entity_page_html": "WOWPROGRESS_GUILD_CACHE_TTL_SECONDS",
            "guide_page_html": "WOWPROGRESS_CHARACTER_CACHE_TTL_SECONDS",
            "page_html": "WOWPROGRESS_LEADERBOARD_CACHE_TTL_SECONDS",
        },
    )
    return settings, settings.ttls.entity_page_html, settings.ttls.guide_page_html, settings.ttls.page_html


class WowProgressClient:
    def __init__(
        self,
        *,
        timeout_seconds: float = 20.0,
        retry_attempts: int = DEFAULT_RETRY_ATTEMPTS,
        impersonate: str = DEFAULT_IMPERSONATE,
    ) -> None:
        self._session: requests.Session | None = None
        settings, guild_ttl, character_ttl, leaderboard_ttl = load_wowprogress_cache_settings_from_env()
        self._timeout_seconds = timeout_seconds
        self._retry_attempts = max(1, retry_attempts)
        self._impersonate = impersonate
        self._cache_store = build_cache_store(settings) if settings.enabled else None
        self._guild_ttl = guild_ttl
        self._character_ttl = character_ttl
        self._leaderboard_ttl = leaderboard_ttl

    def close(self) -> None:
        if self._session is not None:
            self._session.close()
            self._session = None

    def __enter__(self) -> WowProgressClient:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def _client(self) -> requests.Session:
        if self._session is None:
            self._session = requests.Session()
        return self._session

    def _cache_key(self, namespace: str, params: dict[str, Any]) -> str:
        raw = json.dumps({"namespace": namespace, "params": params}, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return f"{namespace}:{hashlib.sha256(raw).hexdigest()}"

    def _read_cache(self, key: str) -> str | None:
        if self._cache_store is None:
            return None
        payload = self._cache_store.get(key)
        return payload if isinstance(payload, str) else None

    def _write_cache(self, key: str, payload: str, *, ttl_seconds: int) -> None:
        if self._cache_store is None:
            return
        self._cache_store.set(key, payload, ttl_seconds=ttl_seconds)

    def _fetch_html(self, url: str, *, namespace: str, ttl_seconds: int) -> str:
        key = self._cache_key(namespace, {"url": url, "impersonate": self._impersonate})
        cached = self._read_cache(key)
        if cached is not None:
            return cached
        attempts = max(1, self._retry_attempts)
        for attempt in range(1, attempts + 1):
            try:
                response = self._client().get(
                    url,
                    impersonate=self._impersonate,
                    timeout=self._timeout_seconds,
                    allow_redirects=True,
                )
            except Exception as exc:  # noqa: BLE001
                if attempt >= attempts:
                    raise WowProgressClientError("network_error", f"WowProgress request failed: {exc}") from exc
                time.sleep(backoff_seconds(attempt))
                continue

            status_code = int(response.status_code)
            final_url = str(response.url)
            html = str(response.text)
            title_probe = html[:512].lower()
            if status_code in RETRYABLE_STATUS_CODES and attempt < attempts:
                time.sleep(backoff_seconds(attempt))
                continue
            if status_code == 403 and "just a moment" in title_probe:
                raise WowProgressClientError("blocked", "WowProgress returned a bot-protection challenge page.")
            if status_code == 403 and "/search?" in final_url:
                raise WowProgressClientError("not_found", "WowProgress could not resolve that guild or character.")
            if status_code == 404:
                raise WowProgressClientError("not_found", "WowProgress could not find that page.")
            if status_code >= 400:
                raise WowProgressClientError("upstream_error", f"WowProgress request failed with HTTP {status_code}.")
            if "just a moment" in title_probe:
                raise WowProgressClientError("blocked", "WowProgress returned a bot-protection challenge page.")
            self._write_cache(key, html, ttl_seconds=ttl_seconds)
            return html
        raise AssertionError("Unreachable retry loop exit.")

    def _fetch_response(self, url: str, *, namespace: str, ttl_seconds: int) -> tuple[str, str]:
        key = self._cache_key(namespace, {"url": url, "impersonate": self._impersonate})
        cached = self._read_cache(key)
        if cached is not None:
            try:
                payload = json.loads(cached)
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict):
                html = payload.get("html")
                final_url = payload.get("final_url")
                if isinstance(html, str) and isinstance(final_url, str):
                    return html, final_url
            return cached, url
        attempts = max(1, self._retry_attempts)
        for attempt in range(1, attempts + 1):
            try:
                response = self._client().get(
                    url,
                    impersonate=self._impersonate,
                    timeout=self._timeout_seconds,
                    allow_redirects=True,
                )
            except Exception as exc:  # noqa: BLE001
                if attempt >= attempts:
                    raise WowProgressClientError("network_error", f"WowProgress request failed: {exc}") from exc
                time.sleep(backoff_seconds(attempt))
                continue

            status_code = int(response.status_code)
            final_url = str(response.url)
            html = str(response.text)
            title_probe = html[:512].lower()
            if status_code in RETRYABLE_STATUS_CODES and attempt < attempts:
                time.sleep(backoff_seconds(attempt))
                continue
            if status_code == 403 and "just a moment" in title_probe:
                raise WowProgressClientError("blocked", "WowProgress returned a bot-protection challenge page.")
            if status_code == 403 and "/search" in final_url:
                raise WowProgressClientError("blocked", "WowProgress blocked the search request.")
            if status_code >= 400:
                raise WowProgressClientError("upstream_error", f"WowProgress request failed with HTTP {status_code}.")
            if "just a moment" in title_probe:
                raise WowProgressClientError("blocked", "WowProgress returned a bot-protection challenge page.")
            self._write_cache(
                key,
                json.dumps({"html": html, "final_url": final_url}, sort_keys=True),
                ttl_seconds=ttl_seconds,
            )
            return html, final_url
        raise AssertionError("Unreachable retry loop exit.")

    def fetch_guild_page(self, *, region: str, realm: str, name: str) -> dict[str, Any]:
        region = normalize_region(region)
        realm = primary_realm_slug(realm)
        name = normalize_name(name)
        url = guild_url(region, realm, name)
        html = self._fetch_html(url, namespace="guild_page", ttl_seconds=self._guild_ttl)
        return parse_guild_page(html, url=url, region=region, realm=realm, name=name)

    def fetch_guild_page_variants(self, *, region: str, realm: str, name: str) -> dict[str, Any]:
        normalized_region = normalize_region(region)
        normalized_name = normalize_name(name)
        variants = realm_slug_variants(realm) or [primary_realm_slug(realm)]
        last_error: WowProgressClientError | None = None
        for candidate in variants:
            try:
                return self.fetch_guild_page(region=normalized_region, realm=candidate, name=normalized_name)
            except WowProgressClientError as exc:
                if exc.code != "not_found":
                    raise
                last_error = exc
        if last_error is not None:
            raise last_error
        return self.fetch_guild_page(region=normalized_region, realm=realm, name=normalized_name)

    def fetch_guild_page_url(self, url: str) -> dict[str, Any]:
        parsed = urlparse(url)
        parts = [part for part in parsed.path.split("/") if part]
        region = parts[1] if len(parts) > 1 else ""
        realm = parts[2] if len(parts) > 2 else ""
        name = parts[3] if len(parts) > 3 else ""
        html = self._fetch_html(url, namespace="guild_page", ttl_seconds=self._guild_ttl)
        return parse_guild_page(html, url=url, region=region, realm=realm, name=name)

    def fetch_character_page(self, *, region: str, realm: str, name: str) -> dict[str, Any]:
        region = normalize_region(region)
        realm = primary_realm_slug(realm)
        name = normalize_name(name)
        url = character_url(region, realm, name)
        html = self._fetch_html(url, namespace="character_page", ttl_seconds=self._character_ttl)
        return parse_character_page(html, url=url, region=region, realm=realm, name=name)

    def fetch_character_page_variants(self, *, region: str, realm: str, name: str) -> dict[str, Any]:
        normalized_region = normalize_region(region)
        normalized_name = normalize_name(name)
        variants = realm_slug_variants(realm) or [primary_realm_slug(realm)]
        last_error: WowProgressClientError | None = None
        for candidate in variants:
            try:
                return self.fetch_character_page(region=normalized_region, realm=candidate, name=normalized_name)
            except WowProgressClientError as exc:
                if exc.code != "not_found":
                    raise
                last_error = exc
        if last_error is not None:
            raise last_error
        return self.fetch_character_page(region=normalized_region, realm=realm, name=normalized_name)

    def fetch_pve_leaderboard(self, *, region: str, realm: str | None = None, limit: int = 25) -> dict[str, Any]:
        region = normalize_region(region)
        url = leaderboard_url(region, primary_realm_slug(realm) if realm else None)
        html = self._fetch_html(url, namespace="pve_leaderboard", ttl_seconds=self._leaderboard_ttl)
        return parse_pve_leaderboard_page(html, url=url, region=region, realm=primary_realm_slug(realm) if realm else None, limit=limit)

    @property
    def pve_leaderboard_ttl_seconds(self) -> int:
        return self._leaderboard_ttl

    @property
    def guild_page_ttl_seconds(self) -> int:
        return self._guild_ttl

    @property
    def cache_enabled(self) -> bool:
        return self._cache_store is not None

    def probe_search_route(self, *, region: str, realm: str, name: str, obj_type: str) -> dict[str, Any] | None:
        if obj_type not in {"char", "guild"}:
            raise WowProgressClientError("invalid_query", "WowProgress search probe supports only char or guild.")
        region = normalize_region(region)
        realm = primary_realm_slug(realm)
        name = normalize_name(name)
        query = urlencode({"name": name, "realm": realm, "area": region, "obj_type": obj_type})
        url = f"{WOWPROGRESS_BASE_URL}/u_search?{query}"
        html, final_url = self._fetch_response(url, namespace=f"search_probe_{obj_type}", ttl_seconds=self._character_ttl)
        if final_url.rstrip("/") == WOWPROGRESS_BASE_URL.rstrip("/"):
            return None
        if obj_type == "char" and "/character/" in final_url:
            try:
                payload = parse_character_page(html, url=final_url, region=region, realm=realm, name=name)
            except ValueError as exc:
                raise WowProgressClientError("upstream_error", "WowProgress returned an unexpected character profile page.") from exc
            payload["_search_kind"] = "character"
            return payload
        if obj_type == "guild" and "/guild/" in final_url:
            try:
                payload = parse_guild_page(html, url=final_url, region=region, realm=realm, name=name)
            except ValueError as exc:
                raise WowProgressClientError("upstream_error", "WowProgress returned an unexpected guild profile page.") from exc
            payload["_search_kind"] = "guild"
            return payload
        return None

    def fetch_guild_history(self, *, region: str, realm: str, name: str) -> dict[str, Any]:
        payload = self.fetch_guild_page_variants(region=region, realm=realm, name=name)
        history_links = payload.get("history_links") if isinstance(payload.get("history_links"), list) else []
        tiers: list[dict[str, Any]] = []
        for link in history_links:
            if not isinstance(link, dict):
                continue
            page_url = str(link.get("page_url") or "").strip()
            if not page_url:
                continue
            tier_payload = self.fetch_guild_page_url(page_url)
            progress = tier_payload.get("progress") if isinstance(tier_payload.get("progress"), dict) else {}
            item_level = tier_payload.get("item_level") if isinstance(tier_payload.get("item_level"), dict) else {}
            encounters = tier_payload.get("encounters") if isinstance(tier_payload.get("encounters"), dict) else {}
            items = encounters.get("items") if isinstance(encounters.get("items"), list) else []
            progress_summary = str(progress.get("summary") or "")
            progress_match = progress_summary.split(" ", 1)[0] if progress_summary else ""
            bosses_killed = None
            boss_count = None
            if "/" in progress_match:
                killed, total = progress_match.split("/", 1)
                if killed.isdigit() and total.isdigit():
                    bosses_killed = int(killed)
                    boss_count = int(total)
            tiers.append(
                {
                    "tier_key": link.get("tier_key"),
                    "raid": link.get("raid"),
                    "page_url": page_url,
                    "current": bool(link.get("current")),
                    "progress": progress_summary or None,
                    "progress_ranks": progress.get("ranks"),
                    "bosses_killed": bosses_killed,
                    "boss_count": boss_count,
                    # Parsed difficulty token (e.g. "M"), not the full "8/8 (M)" summary string.
                    "difficulty": _progress_snapshot(progress_summary)["difficulty"],
                    "item_level_average": item_level.get("average"),
                    "item_level_ranks": item_level.get("ranks"),
                    "first_kill_at": items[-1].get("first_kill_at") if items else None,
                    "last_kill_at": items[0].get("first_kill_at") if items else None,
                    "encounter_count": encounters.get("count"),
                    "encounters": items,
                }
            )
        return {
            "provider": "wowprogress",
            "kind": "guild_history",
            "guild": payload.get("guild"),
            "current_progress": payload.get("progress"),
            # Current-state item level + encounters from the main guild page, so consumers (e.g.
            # guild-snapshot) keep them even when history_links is empty/incomplete.
            "current_item_level": payload.get("item_level"),
            "current_encounters": payload.get("encounters"),
            "history": tiers,
            "citations": {
                "page": ((payload.get("citations") or {}).get("page") if isinstance(payload.get("citations"), dict) else None),
            },
        }

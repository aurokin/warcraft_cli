from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
from warcraft_api.cache import CacheSettings, CacheTTLConfig, build_cache_store, load_prefixed_cache_settings_from_env
from warcraft_api.http import DEFAULT_RETRY_ATTEMPTS, build_client, request_with_retries
from warcraft_core.paths import provider_cache_root

PROVIDER_NAME = "lorrgs"
API_HOST = "https://api2.lorrgs.io"
SITE_HOST = "https://lorrgs.io"
OPENAPI_URL = f"{API_HOST}/api/openapi.json"
# Lorrgs ranks Mythic and Heroic only; any other difficulty is a 404 "Not found." upstream.
DIFFICULTIES = ("mythic", "heroic")


def load_lorrgs_cache_settings_from_env() -> tuple[CacheSettings, int, int, int]:
    """Resolve cache settings plus the static metadata, ranking, and loaded-fight TTLs.

    Roster metadata (specs, bosses, zones, spells) changes with a patch, rankings when Lorrgs
    re-ranks, and a fight Lorrgs has loaded never changes.
    """
    settings = load_prefixed_cache_settings_from_env(
        env_prefix="LORRGS",
        # Resolved per call, not at import, so the cache root follows HOME/XDG as they are now.
        default_cache_dir=provider_cache_root("lorrgs") / "http",
        default_redis_prefix="lorrgs_cli",
        ttl_defaults=CacheTTLConfig(search_suggestions=43200, page_html=1800, report_finished=21600),
        ttl_env_overrides={
            "search_suggestions": "LORRGS_STATIC_CACHE_TTL_SECONDS",
            "page_html": "LORRGS_RANKING_CACHE_TTL_SECONDS",
            "report_finished": "LORRGS_REPORT_CACHE_TTL_SECONDS",
        },
    )
    return settings, settings.ttls.search_suggestions, settings.ttls.page_html, settings.ttls.report_finished


def _fights_loaded(payload: Any) -> bool:
    """Whether a fights answer holds loaded fights: Lorrgs answers an unloaded one with no players."""
    fights = payload.get("fights") if isinstance(payload, dict) else None
    return isinstance(fights, list) and bool(fights) and all(isinstance(fight, dict) and fight.get("players") for fight in fights)


class LorrgsClientError(RuntimeError):
    """Typed client error so the command layer can emit structured failures."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class LorrgsClient:
    def __init__(
        self,
        *,
        timeout_seconds: float = 20.0,
        retry_attempts: int = DEFAULT_RETRY_ATTEMPTS,
    ) -> None:
        settings, static_ttl, ranking_ttl, report_ttl = load_lorrgs_cache_settings_from_env()
        self._timeout_seconds = timeout_seconds
        self._retry_attempts = max(1, retry_attempts)
        self._http_client: httpx.Client | None = None
        self._cache_store = build_cache_store(settings) if settings.enabled else None
        self._static_ttl = static_ttl
        self._ranking_ttl = ranking_ttl
        self._report_ttl = report_ttl

    def close(self) -> None:
        if self._http_client is not None:
            self._http_client.close()
            self._http_client = None

    def __enter__(self) -> LorrgsClient:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def _client(self) -> httpx.Client:
        if self._http_client is None:
            self._http_client = build_client(timeout=self._timeout_seconds)
        return self._http_client

    @staticmethod
    def _decode_json(response: httpx.Response) -> Any:
        try:
            return response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise LorrgsClientError("invalid_response", "Lorrgs response was not valid JSON.") from exc

    def _get(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        ttl_seconds: int = 0,
        cacheable: Callable[[Any], bool] | None = None,
    ) -> dict[str, Any]:
        """GET one Lorrgs route, replaying a cached answer when ``ttl_seconds`` allows one.

        ``cacheable`` vetoes storing an answer that is not final yet (a fight Lorrgs has not loaded).
        The result carries ``fetched_at`` (when it came off the wire, also on a replay), ``cache_hit``
        and ``cache_ttl_seconds`` for provenance.
        """
        cleaned = _clean_params(params)
        key = f"lorrgs:{hashlib.sha256(json.dumps([path, cleaned], sort_keys=True).encode()).hexdigest()}"
        if ttl_seconds and self._cache_store is not None:
            cached = self._cache_store.get(key)
            if isinstance(cached, dict) and isinstance(cached.get("fetched_at"), str):
                return {**cached, "cache_hit": True, "cache_ttl_seconds": ttl_seconds}
        response = request_with_retries(
            self._client(),
            f"{API_HOST}{path}",
            params=cleaned,
            headers={"Accept": "application/json"},
            retry_attempts=self._retry_attempts,
        )
        result = {
            "payload": self._decode_json(response),
            "source_url": str(response.request.url),
            "fetched_at": datetime.now(UTC).isoformat(),
        }
        if ttl_seconds and self._cache_store is not None and (cacheable is None or cacheable(result["payload"])):
            self._cache_store.set(key, result, ttl_seconds=ttl_seconds)
        return {**result, "cache_hit": False, "cache_ttl_seconds": ttl_seconds}

    def roles(self) -> dict[str, Any]:
        return self._get("/api/roles", ttl_seconds=self._static_ttl)

    def classes(self) -> dict[str, Any]:
        return self._get("/api/classes", ttl_seconds=self._static_ttl)

    def specs(self) -> dict[str, Any]:
        return self._get("/api/specs", ttl_seconds=self._static_ttl)

    def spec(self, spec_slug: str) -> dict[str, Any]:
        return self._get(f"/api/specs/{spec_slug}", ttl_seconds=self._static_ttl)

    def spec_spells(self, spec_slug: str) -> dict[str, Any]:
        return self._get(f"/api/specs/{spec_slug}/spells", ttl_seconds=self._static_ttl)

    def zones(self) -> dict[str, Any]:
        return self._get("/api/zones", ttl_seconds=self._static_ttl)

    def season(self, season_slug: str = "current") -> dict[str, Any]:
        return self._get(f"/api/seasons/{season_slug}", ttl_seconds=self._static_ttl)

    def zone(self, zone_id: float) -> dict[str, Any]:
        return self._get(f"/api/zones/{zone_id:g}", ttl_seconds=self._static_ttl)

    def zone_bosses(self, zone_id: float) -> dict[str, Any]:
        return self._get(f"/api/zones/{zone_id:g}/bosses", ttl_seconds=self._static_ttl)

    def bosses(self) -> dict[str, Any]:
        return self._get("/api/bosses", ttl_seconds=self._static_ttl)

    def boss(self, boss_slug: str) -> dict[str, Any]:
        return self._get(f"/api/bosses/{boss_slug}", ttl_seconds=self._static_ttl)

    def boss_spells(self, boss_slug: str) -> dict[str, Any]:
        return self._get(f"/api/bosses/{boss_slug}/spells", ttl_seconds=self._static_ttl)

    def spell(self, spell_id: int) -> dict[str, Any]:
        return self._get(f"/api/spells/{spell_id}", ttl_seconds=self._static_ttl)

    def trinkets(self) -> dict[str, Any]:
        return self._get("/api/trinkets", ttl_seconds=self._static_ttl)

    def spec_ranking(
        self,
        *,
        spec_slug: str,
        boss_slug: str,
        difficulty: str = "mythic",
        metric: str | None = None,
    ) -> dict[str, Any]:
        return self._get(
            f"/api/spec_ranking/{spec_slug}/{boss_slug}",
            params={"difficulty": difficulty, "metric": metric},
            ttl_seconds=self._ranking_ttl,
        )

    def spec_ranking_info(
        self,
        *,
        spec_slug: str,
        boss_slug: str,
        difficulty: str = "mythic",
        metric: str | None = None,
    ) -> dict[str, Any]:
        return self._get(
            f"/api/spec_ranking/{spec_slug}/{boss_slug}/info",
            params={"difficulty": difficulty, "metric": metric},
            ttl_seconds=self._ranking_ttl,
        )

    def comp_ranking(
        self,
        *,
        boss_slug: str,
        limit: int = 20,
        roles: list[str] | None = None,
        specs: list[str] | None = None,
        killtime_min: int = 0,
        killtime_max: int = 0,
    ) -> dict[str, Any]:
        return self._get(
            f"/api/comp_ranking/{boss_slug}",
            params={
                "limit": limit,
                "role": roles or None,
                "spec": specs or None,
                "killtime_min": killtime_min or None,
                "killtime_max": killtime_max or None,
            },
            ttl_seconds=self._ranking_ttl,
        )

    def user_report(self, report_id: str) -> dict[str, Any]:
        return self._get(f"/api/user_reports/{report_id}")

    def report_overview(self, report_id: str, *, refresh: bool = False) -> dict[str, Any]:
        return self._get(f"/api/user_reports/{report_id}/load_overview", params={"refresh": refresh})

    def user_report_fights(
        self,
        *,
        report_id: str,
        fight: str,
        player: str | None = None,
        data_type: str | None = None,
    ) -> dict[str, Any]:
        return self._get(
            f"/api/user_reports/{report_id}/fights",
            params={"fight": fight, "player": player, "type": data_type},
            ttl_seconds=self._report_ttl,
            cacheable=_fights_loaded,
        )


def _clean_params(params: dict[str, Any] | None) -> dict[str, Any] | None:
    if params is None:
        return None
    cleaned = {key: value for key, value in params.items() if value not in (None, "")}
    return cleaned or None

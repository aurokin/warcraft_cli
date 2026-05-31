from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from typing import Any

import httpx
from warcraft_api.cache import CacheSettings, CacheTTLConfig, build_cache_store, load_prefixed_cache_settings_from_env
from warcraft_api.http import DEFAULT_RETRY_ATTEMPTS, request_with_retries
from warcraft_content.paths import provider_cache_root

DEFAULT_BASE_URL = "https://www.raidbots.com"
DEFAULT_REPORT_PATH_TEMPLATE = "/simbot/report/{id}"
DEFAULT_DATA_PATH_TEMPLATE = "/simbot/report/{id}/data.json"
DEFAULT_INPUT_PATH_TEMPLATE = "/simbot/report/{id}/simc"
DEFAULT_CACHE_DIR = provider_cache_root("raidbots") / "http"

# A report ID is the trailing path segment after `/report/`; accept the same
# characters Raidbots uses for its slugs (alphanumerics plus `-`/`_`).
_REPORT_ID_RE = re.compile(r"/report/([A-Za-z0-9_-]+)")
_BARE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class InvalidReportReference(ValueError):
    """Raised when a report URL or ID cannot be parsed into a report ID."""


def resolve_report_id(value: str) -> str:
    # Report references are normalized to a bare ID against the documented `/report/{ID}`
    # surface; all fetches are then rebuilt from the configured (env-overridable) base + path
    # templates. We deliberately do NOT honor an arbitrary host from a URL input — that keeps
    # the fetch target trusted/configured (avoids SSRF to arbitrary hosts) and matches the
    # documented contract (docs/raidbots/README.md: input is a bare ID or a `/report/{ID}` URL).
    # Env overrides adapt the *fetch* URLs to live drift with no code change; URL-*input* parsing
    # stays pinned to the public `/report/{ID}` shape, and a bare ID is always accepted — so URL
    # drift never blocks resolution (paste the ID). By design, not an oversight.
    candidate = (value or "").strip()
    if not candidate:
        raise InvalidReportReference("Report reference is empty.")
    match = _REPORT_ID_RE.search(candidate)
    if match:
        return match.group(1)
    if "/" not in candidate and _BARE_ID_RE.match(candidate):
        return candidate
    raise InvalidReportReference(f"Could not extract a report ID from {value!r}.")


@dataclass(frozen=True, slots=True)
class RaidbotsUrls:
    """Resolved Raidbots URL templates.

    These mirror the documented public report surface. They are constants so the
    parser/command contracts are the durable value, but each is env-overridable so
    a live URL correction needs no code change.
    """

    base_url: str
    report_path_template: str
    data_path_template: str
    input_path_template: str

    def report_url(self, report_id: str) -> str:
        return f"{self.base_url}{self.report_path_template.format(id=report_id)}"

    def data_url(self, report_id: str) -> str:
        return f"{self.base_url}{self.data_path_template.format(id=report_id)}"

    def input_url(self, report_id: str) -> str:
        return f"{self.base_url}{self.input_path_template.format(id=report_id)}"

    def templates(self) -> dict[str, str]:
        return {
            "base_url": self.base_url,
            "report": f"{self.base_url}{self.report_path_template}",
            "data_json": f"{self.base_url}{self.data_path_template}",
            "simc_input": f"{self.base_url}{self.input_path_template}",
            "note": "Documented Raidbots report surface; may need adjustment if Raidbots changes URLs.",
        }


def load_raidbots_urls_from_env() -> RaidbotsUrls:
    base = (os.getenv("RAIDBOTS_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    return RaidbotsUrls(
        base_url=base,
        report_path_template=os.getenv("RAIDBOTS_REPORT_PATH_TEMPLATE") or DEFAULT_REPORT_PATH_TEMPLATE,
        data_path_template=os.getenv("RAIDBOTS_DATA_PATH_TEMPLATE") or DEFAULT_DATA_PATH_TEMPLATE,
        input_path_template=os.getenv("RAIDBOTS_INPUT_PATH_TEMPLATE") or DEFAULT_INPUT_PATH_TEMPLATE,
    )


def load_raidbots_cache_settings_from_env() -> tuple[CacheSettings, int]:
    settings = load_prefixed_cache_settings_from_env(
        env_prefix="RAIDBOTS",
        default_cache_dir=DEFAULT_CACHE_DIR,
        default_redis_prefix="raidbots_cli",
        # Completed reports are immutable, so cache them for a full day by default.
        ttl_defaults=CacheTTLConfig(entity_response=86400),
        ttl_env_overrides={"entity_response": "RAIDBOTS_REPORT_CACHE_TTL_SECONDS"},
    )
    return settings, settings.ttls.entity_response


class RaidbotsClient:
    def __init__(
        self,
        *,
        timeout_seconds: float = 30.0,
        retry_attempts: int = DEFAULT_RETRY_ATTEMPTS,
    ) -> None:
        self._http_client: httpx.Client | None = None
        settings, report_ttl = load_raidbots_cache_settings_from_env()
        self._timeout_seconds = timeout_seconds
        self._retry_attempts = max(1, retry_attempts)
        self._cache_settings = settings
        self._cache_store = build_cache_store(settings) if settings.enabled else None
        self._report_ttl = report_ttl
        self._urls = load_raidbots_urls_from_env()
        self._last_from_cache = False

    @property
    def urls(self) -> RaidbotsUrls:
        return self._urls

    @property
    def report_ttl_seconds(self) -> int:
        return self._report_ttl

    @property
    def last_from_cache(self) -> bool:
        """Whether the most recent fetch was served from the local cache (may be stale)."""
        return self._last_from_cache

    def close(self) -> None:
        if self._http_client is not None:
            self._http_client.close()
            self._http_client = None

    def __enter__(self) -> RaidbotsClient:
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

    def report_data(self, report_id: str) -> dict[str, Any]:
        url = self._urls.data_url(report_id)
        # Key on the resolved URL, not just the ID, so changing the base/path overrides
        # busts the cache instead of returning data fetched from a different host.
        key = self._cache_key("report_data", {"url": url})
        cached = self._read_cache(key)
        if isinstance(cached, dict):
            self._last_from_cache = True
            return cached
        self._last_from_cache = False
        response = request_with_retries(self._client(), url, retry_attempts=self._retry_attempts)
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError(f"Unexpected Raidbots data.json shape for report {report_id}.")
        self._write_cache(key, payload, ttl_seconds=self._report_ttl)
        return payload

    def report_input(self, report_id: str) -> str:
        url = self._urls.input_url(report_id)
        key = self._cache_key("report_input", {"url": url})
        cached = self._read_cache(key)
        if isinstance(cached, str):
            self._last_from_cache = True
            return cached
        self._last_from_cache = False
        response = request_with_retries(self._client(), url, retry_attempts=self._retry_attempts)
        text = response.text
        self._write_cache(key, text, ttl_seconds=self._report_ttl)
        return text

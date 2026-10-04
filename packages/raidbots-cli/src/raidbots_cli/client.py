from __future__ import annotations

import re
from typing import Any

from warcraft_api.cache import CacheSettings, CacheTTLConfig, build_cache_store, load_prefixed_cache_settings_from_env
from warcraft_api.http import DEFAULT_RETRY_ATTEMPTS, CachedHttpClient, json_cache_key, request_with_retries
from warcraft_core.paths import provider_cache_root

BASE_URL = "https://www.raidbots.com"
REPORT_PATH_TEMPLATE = "/simbot/report/{id}"
DATA_PATH_TEMPLATE = "/simbot/report/{id}/data.json"
INPUT_PATH_TEMPLATE = "/simbot/report/{id}/simc"
URL_TEMPLATES: dict[str, str] = {
    "base_url": BASE_URL,
    "report": BASE_URL + REPORT_PATH_TEMPLATE,
    "data_json": BASE_URL + DATA_PATH_TEMPLATE,
    "simc_input": BASE_URL + INPUT_PATH_TEMPLATE,
}
DEFAULT_CACHE_DIR = provider_cache_root("raidbots") / "http"

# A report ID is the trailing path segment after `/report/`; accept the same
# characters Raidbots uses for its slugs (alphanumerics plus `-`/`_`).
_REPORT_ID_RE = re.compile(r"/report/([A-Za-z0-9_-]+)")
_BARE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class InvalidReportReference(ValueError):
    """Raised when a report URL or ID cannot be parsed into a report ID."""


class ReportNotAvailable(LookupError):
    """Raised when Raidbots answers a report fetch with its web page instead of report content."""


def _reject_web_page(text: str, *, report_id: str, url: str) -> None:
    """Raise when a 200 response carries the Raidbots single-page app instead of report content.

    Raidbots serves that page for a report id that does not exist, has expired, or is private. Both
    report fetches must treat it the same way, or the same missing report answers `not_found` on one
    command and a parse failure on the other.
    """
    if not text.lstrip()[:64].lower().startswith(("<!doctype html", "<html")):
        return
    raise ReportNotAvailable(
        f"Raidbots returned its web page instead of report content for report {report_id!r}: the "
        f"report id is wrong, or the report has expired or is private ({url})."
    )


def resolve_report_id(value: str) -> str:
    # Accept a `/report/{ID}` URL or a bare ID. The URL host is deliberately ignored: fetches are
    # always rebuilt from BASE_URL (SSRF-safe).
    candidate = (value or "").strip()
    if not candidate:
        raise InvalidReportReference("Report reference is empty.")
    match = _REPORT_ID_RE.search(candidate)
    if match:
        return match.group(1)
    if "/" not in candidate and _BARE_ID_RE.match(candidate):
        return candidate
    raise InvalidReportReference(f"Could not extract a report ID from {value!r}.")


def report_url(report_id: str) -> str:
    return BASE_URL + REPORT_PATH_TEMPLATE.format(id=report_id)


def data_url(report_id: str) -> str:
    return BASE_URL + DATA_PATH_TEMPLATE.format(id=report_id)


def input_url(report_id: str) -> str:
    return BASE_URL + INPUT_PATH_TEMPLATE.format(id=report_id)


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


class RaidbotsClient(CachedHttpClient):
    def __init__(
        self,
        *,
        timeout_seconds: float = 30.0,
        retry_attempts: int = DEFAULT_RETRY_ATTEMPTS,
    ) -> None:
        settings, report_ttl = load_raidbots_cache_settings_from_env()
        self._timeout_seconds = timeout_seconds
        self._retry_attempts = max(1, retry_attempts)
        self._cache_store = build_cache_store(settings) if settings.enabled else None
        self._report_ttl = report_ttl
        self._last_from_cache = False

    @property
    def report_ttl_seconds(self) -> int:
        return self._report_ttl

    @property
    def last_from_cache(self) -> bool:
        """Whether the most recent fetch was served from the local cache (may be stale)."""
        return self._last_from_cache

    def _cache_key(self, namespace: str, params: dict[str, Any]) -> str:
        return json_cache_key(namespace, {"namespace": namespace, "params": params})

    def report_data(self, report_id: str) -> dict[str, Any]:
        url = data_url(report_id)
        key = self._cache_key("report_data", {"url": url})
        cached = self._read_cache(key)
        if isinstance(cached, dict):
            self._last_from_cache = True
            return cached
        self._last_from_cache = False
        response = request_with_retries(self._client(), url, retry_attempts=self._retry_attempts)
        _reject_web_page(response.text, report_id=report_id, url=url)
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError(f"Unexpected Raidbots data.json shape for report {report_id}.")
        self._write_cache(key, payload, ttl_seconds=self._report_ttl)
        return payload

    def report_input(self, report_id: str) -> str:
        url = input_url(report_id)
        key = self._cache_key("report_input", {"url": url})
        cached = self._read_cache(key)
        if isinstance(cached, str):
            self._last_from_cache = True
            return cached
        self._last_from_cache = False
        response = request_with_retries(self._client(), url, retry_attempts=self._retry_attempts)
        text = response.text
        # Never cache the web page: reject it before the write below.
        _reject_web_page(text, report_id=report_id, url=url)
        self._write_cache(key, text, ttl_seconds=self._report_ttl)
        return text

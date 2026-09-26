from __future__ import annotations

import os
import random
import threading
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Final, cast
from urllib.parse import urlsplit

import httpx

DEFAULT_RETRY_ATTEMPTS = 3
RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
REPO_URL: Final = "https://github.com/aurokin/warcraft_cli"
# Upper bound on how long a server-sent Retry-After can stall a single attempt.
MAX_RETRY_AFTER_SECONDS: Final = 30.0
MIN_INTERVAL_ENV: Final = "WARCRAFT_HTTP_MIN_INTERVAL_SECONDS"
DEFAULT_MIN_INTERVAL_SECONDS: Final = 0.25


def default_user_agent() -> str:
    try:
        package_version = version("warcraft")
    except PackageNotFoundError:
        package_version = "dev"
    return f"warcraft-cli/{package_version} (+{REPO_URL})"


DEFAULT_USER_AGENT: Final = default_user_agent()


def backoff_seconds(attempt: int) -> float:
    base = 0.35 * (2 ** (attempt - 1))
    jitter = random.uniform(0.0, 0.12)
    return float(min(4.0, base + jitter))


def retry_after_seconds(response: httpx.Response) -> float | None:
    raw_value = response.headers.get("Retry-After")
    if raw_value is None:
        return None
    value = raw_value.strip()
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        seconds = (retry_at - datetime.now(UTC)).total_seconds()
    return min(max(0.0, seconds), MAX_RETRY_AFTER_SECONDS)


def _min_interval_from_env() -> float:
    raw_value = os.environ.get(MIN_INTERVAL_ENV)
    if raw_value is None or not raw_value.strip():
        return DEFAULT_MIN_INTERVAL_SECONDS
    try:
        return max(0.0, float(raw_value))
    except ValueError:
        return DEFAULT_MIN_INTERVAL_SECONDS


class HostRateLimiter:
    """Process-wide minimum interval between request starts, keyed per host.

    Thread-safe because wowhead fans requests out through a ThreadPoolExecutor.
    An interval of ``0`` disables waiting. When constructed without an explicit interval the
    ``WARCRAFT_HTTP_MIN_INTERVAL_SECONDS`` variable is read on every ``wait`` so the process-wide
    default limiter honours changes made after import (tests set it to ``0``).
    """

    def __init__(self, min_interval_seconds: float | None = None) -> None:
        self._configured_interval = None if min_interval_seconds is None else max(0.0, min_interval_seconds)
        self._lock = threading.Lock()
        self._next_allowed_at: dict[str, float] = {}

    @property
    def min_interval_seconds(self) -> float:
        if self._configured_interval is None:
            return _min_interval_from_env()
        return self._configured_interval

    def wait(self, url: str) -> None:
        interval = self.min_interval_seconds
        if interval <= 0:
            return
        host = urlsplit(url).netloc.lower() or url
        with self._lock:
            now = time.monotonic()
            start_at = max(now, self._next_allowed_at.get(host, now))
            self._next_allowed_at[host] = start_at + interval
        delay = start_at - now
        if delay > 0:
            time.sleep(delay)


DEFAULT_RATE_LIMITER = HostRateLimiter()


def build_client(
    *,
    timeout: float,
    headers: Mapping[str, str] | None = None,
    follow_redirects: bool = True,
    **kwargs: Any,
) -> httpx.Client:
    """Build an httpx client that identifies itself as warcraft-cli unless ``headers`` overrides User-Agent."""
    merged_headers: dict[str, str] = {"User-Agent": DEFAULT_USER_AGENT}
    if headers:
        merged_headers.update(headers)
    return httpx.Client(timeout=timeout, follow_redirects=follow_redirects, headers=merged_headers, **kwargs)


def request_with_retries(
    client: httpx.Client,
    url: str,
    *,
    method: str = "GET",
    params: dict[str, Any] | None = None,
    retry_attempts: int = DEFAULT_RETRY_ATTEMPTS,
    rate_limiter: HostRateLimiter | None = DEFAULT_RATE_LIMITER,
    **request_kwargs: Any,
) -> httpx.Response:
    attempts = max(1, retry_attempts)
    request_method = method.strip().upper() or "GET"
    for attempt in range(1, attempts + 1):
        if rate_limiter is not None:
            rate_limiter.wait(url)
        try:
            method_func = getattr(client, request_method.lower(), None)
            if callable(method_func):
                response = cast(httpx.Response, method_func(url, params=params, **request_kwargs))
            else:
                response = client.request(request_method, url, params=params, **request_kwargs)
            if not isinstance(response, httpx.Response):
                raise TypeError(f"HTTP client returned unexpected response type for {request_method} {url!r}.")
        except httpx.RequestError:
            if attempt >= attempts:
                raise
            time.sleep(backoff_seconds(attempt))
            continue

        if response.status_code in RETRYABLE_STATUS_CODES and attempt < attempts:
            sleep_seconds = retry_after_seconds(response) or backoff_seconds(attempt)
            response.close()
            time.sleep(sleep_seconds)
            continue

        response.raise_for_status()
        return response

    raise AssertionError("Unreachable retry loop exit.")

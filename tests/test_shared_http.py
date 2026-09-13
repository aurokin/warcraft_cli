from __future__ import annotations

import time

import httpx
from warcraft_api.http import (
    DEFAULT_USER_AGENT,
    MAX_RETRY_AFTER_SECONDS,
    HostRateLimiter,
    build_client,
    request_with_retries,
    retry_after_seconds,
)


def test_retry_after_seconds_is_capped() -> None:
    response = httpx.Response(429, headers={"Retry-After": "3600"})
    assert retry_after_seconds(response) == MAX_RETRY_AFTER_SECONDS


def test_host_rate_limiter_spaces_same_host_only() -> None:
    limiter = HostRateLimiter(0.05)
    limiter.wait("https://a.example/one")
    started = time.monotonic()
    limiter.wait("https://a.example/two")
    same_host_elapsed = time.monotonic() - started

    started = time.monotonic()
    limiter.wait("https://b.example/one")
    other_host_elapsed = time.monotonic() - started

    assert same_host_elapsed >= 0.05
    assert other_host_elapsed < 0.05


def test_host_rate_limiter_zero_interval_never_sleeps(monkeypatch) -> None:
    monkeypatch.setattr("warcraft_api.http.time.sleep", lambda _seconds: (_ for _ in ()).throw(AssertionError("slept")))
    limiter = HostRateLimiter(0)
    limiter.wait("https://a.example/one")
    limiter.wait("https://a.example/two")


def test_host_rate_limiter_reads_env_default(monkeypatch) -> None:
    monkeypatch.setenv("WARCRAFT_HTTP_MIN_INTERVAL_SECONDS", "1.5")
    assert HostRateLimiter().min_interval_seconds == 1.5
    monkeypatch.setenv("WARCRAFT_HTTP_MIN_INTERVAL_SECONDS", "bogus")
    assert HostRateLimiter().min_interval_seconds == 0.25


def test_request_with_retries_waits_on_rate_limiter_before_each_attempt(monkeypatch) -> None:
    waited: list[str] = []

    class RecordingLimiter:
        def wait(self, url: str) -> None:
            waited.append(url)

    responses = iter(
        [
            httpx.Response(503, request=httpx.Request("GET", "https://example.invalid")),
            httpx.Response(200, request=httpx.Request("GET", "https://example.invalid")),
        ]
    )

    class FakeClient:
        def get(self, url: str, params=None, **kwargs) -> httpx.Response:
            return next(responses)

    monkeypatch.setattr("warcraft_api.http.time.sleep", lambda _seconds: None)
    response = request_with_retries(
        FakeClient(),  # type: ignore[arg-type]
        "https://example.invalid",
        retry_attempts=2,
        rate_limiter=RecordingLimiter(),  # type: ignore[arg-type]
    )

    assert response.status_code == 200
    assert waited == ["https://example.invalid", "https://example.invalid"]


def test_build_client_sets_default_user_agent_and_allows_override() -> None:
    assert DEFAULT_USER_AGENT.startswith("warcraft-cli/")
    assert "(+https://github.com/aurokin/warcraft_cli)" in DEFAULT_USER_AGENT

    with build_client(timeout=1.0) as client:
        assert client.headers["User-Agent"] == DEFAULT_USER_AGENT
        assert client.follow_redirects is True

    with build_client(timeout=1.0, headers={"User-Agent": "custom/1", "Accept": "application/json"}) as client:
        assert client.headers["User-Agent"] == "custom/1"
        assert client.headers["Accept"] == "application/json"

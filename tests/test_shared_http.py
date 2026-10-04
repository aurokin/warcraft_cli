from __future__ import annotations

import hashlib
from typing import Any

import httpx
import pytest
from warcraft_api.http import (
    DEFAULT_USER_AGENT,
    MAX_RETRY_AFTER_SECONDS,
    CachedHttpClient,
    HostRateLimiter,
    build_client,
    json_cache_key,
    request_with_retries,
    retry_after_seconds,
)


def test_retry_after_seconds_is_capped() -> None:
    response = httpx.Response(429, headers={"Retry-After": "3600"})
    assert retry_after_seconds(response) == MAX_RETRY_AFTER_SECONDS


def test_host_rate_limiter_spaces_same_host_only(monkeypatch) -> None:
    class FakeClock:
        now = 100.0
        slept: list[float] = []

        def monotonic(self) -> float:
            return self.now

        def sleep(self, seconds: float) -> None:
            self.slept.append(seconds)
            self.now += seconds

    clock = FakeClock()
    monkeypatch.setattr("warcraft_api.http.time.monotonic", clock.monotonic)
    monkeypatch.setattr("warcraft_api.http.time.sleep", clock.sleep)

    limiter = HostRateLimiter(0.05)
    limiter.wait("https://a.example/one")
    limiter.wait("https://a.example/two")
    assert clock.slept == [pytest.approx(0.05)]

    limiter.wait("https://b.example/one")
    assert len(clock.slept) == 1


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

    statuses = iter([503, 200])
    client = httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(next(statuses))))

    monkeypatch.setattr("warcraft_api.http.time.sleep", lambda _seconds: None)
    response = request_with_retries(
        client,
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


def test_cached_http_client_builds_one_client_and_closes_it_on_exit() -> None:
    with CachedHttpClient() as client:
        first = client._client()
        assert client._client() is first
        assert first.headers["User-Agent"] == DEFAULT_USER_AGENT
    assert first.is_closed
    assert client._http_client is None


def test_cached_clients_own_separate_lazy_connections_and_reopen_after_close() -> None:
    with CachedHttpClient() as first, CachedHttpClient() as second:
        assert first._http_client is second._http_client is None
        connection = first._client()
        assert second._http_client is None
        assert second._client() is not connection
        first.close()
        first.close()
        assert connection.is_closed
        assert first._client() is not connection
        assert not first._client().is_closed


def test_cached_http_client_closes_after_context_exception() -> None:
    with pytest.raises(ValueError, match="test failure"), CachedHttpClient() as client:
        connection = client._client()
        raise ValueError("test failure")
    assert connection.is_closed
    assert client._http_client is None


def test_cached_http_client_cache_is_inert_without_a_store_and_used_with_one() -> None:
    class DictStore:
        def __init__(self) -> None:
            self.rows: dict[str, tuple[Any, int]] = {}

        def get(self, key: str) -> Any | None:
            row = self.rows.get(key)
            return row[0] if row else None

        def set(self, key: str, payload: Any, *, ttl_seconds: int) -> None:
            self.rows[key] = (payload, ttl_seconds)

    client = CachedHttpClient()
    client._write_cache("k", {"a": 1}, ttl_seconds=60)
    assert client._read_cache("k") is None

    store = DictStore()
    client._cache_store = store
    client._write_cache("k", {"a": 1}, ttl_seconds=60)
    assert store.rows == {"k": ({"a": 1}, 60)}
    assert client._read_cache("k") == {"a": 1}


def test_json_cache_key_keeps_the_key_format_clients_already_stored() -> None:
    # Clients moved onto this helper hashed exactly this compact, key-sorted JSON before, so their
    # cached entries stay hits.
    raw = b'{"namespace":"report_data","params":{"url":"u"}}'
    expected = f"report_data:{hashlib.sha256(raw).hexdigest()}"
    assert json_cache_key("report_data", {"params": {"url": "u"}, "namespace": "report_data"}) == expected

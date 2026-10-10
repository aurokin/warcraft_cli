"""Opt-in disposable loopback Redis contracts; provider HTTP stays synthetic and guarded.

Run with VERIFY_REDIS_URL=redis://127.0.0.1:<port>/0. No persistent Redis is required by the fast suite.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from urllib.parse import urlsplit

import httpx
import pytest
from raiderio_cli.client import RaiderIOClient
from warcraft_api.cache import RedisCacheStore, clear_redis_cache, inspect_redis_cache
from warcraft_core.cache_ledger import cache_ledger


@pytest.fixture
def redis_scope() -> Iterator[tuple[str, str]]:
    url = os.environ.get("VERIFY_REDIS_URL", "")
    if not url:
        pytest.skip("Set VERIFY_REDIS_URL to a disposable loopback Redis to run integration checks")
    assert urlsplit(url).hostname in {"127.0.0.1", "::1", "localhost"}, "Integration Redis must be loopback"
    prefix = f"warcraft_verify_{uuid.uuid4().hex}"
    try:
        yield url, prefix
    finally:
        clear_redis_cache(url, prefix=prefix)
        clear_redis_cache(url, prefix=f"{prefix}_other")


def test_real_redis_expires_and_zero_ttl_deletes_only_the_requested_key(redis_scope: tuple[str, str]) -> None:
    url, prefix = redis_scope
    store = RedisCacheStore(redis_url=url, prefix=prefix)
    store.set("entity:keep", {"value": "kept"}, ttl_seconds=60)
    store.set("entity:expire", {"value": "old"}, ttl_seconds=60)
    store.set("entity:expire", {"value": "new"}, ttl_seconds=0)
    assert store.get("entity:expire") is None
    assert store.get("entity:keep") == {"value": "kept"}
    store.set("entity:short", {"value": "temporary"}, ttl_seconds=1)
    assert store.get("entity:short") == {"value": "temporary"}
    deadline = time.monotonic() + 3
    while store.get("entity:short") is not None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert store.get("entity:short") is None
    assert store.get("entity:keep") == {"value": "kept"}


def test_real_redis_inspection_and_clear_preserve_other_namespaces_and_prefixes(redis_scope: tuple[str, str]) -> None:
    url, prefix = redis_scope
    store = RedisCacheStore(redis_url=url, prefix=prefix)
    other = RedisCacheStore(redis_url=url, prefix=f"{prefix}_other")
    store.set("entity:a", {"id": 1}, ttl_seconds=60)
    store.set("search:a", {"id": 2}, ttl_seconds=60)
    other.set("entity:a", {"id": 3}, ttl_seconds=60)
    summary = inspect_redis_cache(url, prefix=prefix)
    assert summary["available"] is True and summary["count"] == 2
    assert summary["namespaces"] == {"entity": 1, "search": 1}
    assert clear_redis_cache(url, prefix=prefix, namespaces=("entity",)) == {"total": 1, "namespaces": {"entity": 1}}
    assert store.get("entity:a") is None
    assert store.get("search:a") == {"id": 2}
    assert other.get("entity:a") == {"id": 3}


def test_provider_replays_from_real_redis_without_http_and_reports_hit_provenance(
    redis_scope: tuple[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    url, prefix = redis_scope
    monkeypatch.setenv("RAIDERIO_CACHE_BACKEND", "redis")
    monkeypatch.setenv("RAIDERIO_REDIS_URL", url)
    monkeypatch.setenv("RAIDERIO_REDIS_PREFIX", prefix)
    calls = []

    def source(request: httpx.Request) -> httpx.Response:
        calls.append(request.url)
        return httpx.Response(200, json={"name": "Synthetic", "region": "us"})

    with cache_ledger() as cold, RaiderIOClient() as client:
        client._http_client = httpx.Client(transport=httpx.MockTransport(source))
        first = client.character_profile(region="us", realm="illidan", name="Synthetic")
    assert first.cache_hit is False and len(calls) == 1
    assert cold.provenance()["hits"] == 0

    def forbidden(request: httpx.Request) -> httpx.Response:
        pytest.fail(f"Warm Redis read attempted provider HTTP: {request.url}")

    with cache_ledger() as warm, RaiderIOClient() as client:
        client._http_client = httpx.Client(transport=httpx.MockTransport(forbidden))
        second = client.character_profile(region="us", realm="illidan", name="Synthetic")
    assert second.cache_hit is True
    assert (second.payload, second.fetched_at) == (first.payload, first.fetched_at)
    provenance = warm.provenance()
    assert provenance is not None
    assert (provenance["backend"], provenance["hits"], provenance["errors"]) == ("redis", 1, 0)


def test_real_redis_outage_is_nonfatal_and_a_new_invocation_reconnects() -> None:
    """Pause only the explicitly named disposable container; the normal E2E Redis stays available."""
    url = os.environ.get("VERIFY_REDIS_OUTAGE_URL", "")
    container = os.environ.get("VERIFY_REDIS_OUTAGE_CONTAINER", "")
    if not url or not container:
        pytest.skip("Set VERIFY_REDIS_OUTAGE_URL and VERIFY_REDIS_OUTAGE_CONTAINER for a disposable Redis outage check")
    import redis

    assert container.startswith("warcraft-verify-outage-")
    assert urlsplit(url).hostname in {"127.0.0.1", "::1", "localhost"}
    docker = shutil.which("docker")
    assert docker is not None, "Disposable outage check requires Docker"
    prefix = f"warcraft_verify_{uuid.uuid4().hex}"
    store = RedisCacheStore(redis_url=url, prefix=prefix)
    store.set("entity:a", {"value": "before"}, ttl_seconds=60)
    assert store.get("entity:a") == {"value": "before"}
    subprocess.run([docker, "pause", container], check=True, capture_output=True, timeout=15)
    try:
        with cache_ledger() as ledger:
            assert store.get("entity:a") is None
            store.set("entity:b", {"value": "outage"}, ttl_seconds=60)
            assert store.get("entity:b") is None
        provenance = ledger.provenance()
        assert provenance is not None and provenance["errors"] == 3
    finally:
        subprocess.run([docker, "unpause", container], check=True, capture_output=True, timeout=15)
    direct = redis.Redis.from_url(url, decode_responses=True, socket_connect_timeout=1, socket_timeout=1)
    deadline = time.monotonic() + 5
    while True:
        try:
            assert direct.ping()
            break
        except redis.ConnectionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.05)
    direct.set(f"{prefix}:entity:a", '{"value":"restored"}', ex=60)
    # The breaker intentionally lasts for this process; each CLI invocation gets a fresh process.
    assert RedisCacheStore(redis_url=url, prefix=prefix).get("entity:a") is None
    source = "from warcraft_api.cache import RedisCacheStore; import sys; assert RedisCacheStore(redis_url=sys.argv[1], prefix=sys.argv[2]).get('entity:a') == {'value':'restored'}"
    subprocess.run([sys.executable, "-c", source, url, prefix], check=True, capture_output=True, text=True, timeout=15)
    direct.delete(f"{prefix}:entity:a")
    direct.close()

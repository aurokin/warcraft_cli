from __future__ import annotations

import hashlib
import importlib
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from blizzard_api_cli.auth import BlizzardAuthConfig
from blizzard_api_cli.client import BlizzardClient, BlizzardClientError, resolve_routing
from curseforge_cli.auth import CurseForgeAuthConfig
from curseforge_cli.client import CurseForgeClient, CurseForgeClientError
from lorrgs_cli.client import LorrgsClient
from raidbots_cli.client import RaidbotsClient
from raiderio_cli.client import RaiderIOClient
from warcraft_api import cache as cache_module
from warcraft_api.cache import (
    CacheSettings,
    CacheTTLConfig,
    FileCacheStore,
    RedisCacheStore,
    build_cache_store,
    cache_backend_health,
    clear_file_cache,
    clear_redis_cache,
    inspect_file_cache,
    inspect_redis_cache,
    load_cache_settings_from_env,
    redacted_redis_url,
)
from warcraft_core.cache_ledger import cache_ledger
from warcraft_wiki_cli.client import WarcraftWikiClient
from warcraftlogs_cli.client import WarcraftLogsClient
from wowhead_cli.wowhead_client import WowheadClient


def test_file_cache_store_roundtrips_and_expires(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = FileCacheStore(tmp_path)
    now = 1000.0
    monkeypatch.setattr("warcraft_api.cache.time.time", lambda: now)

    store.set("search_suggestions:abc123", {"query": "thunderfury"}, ttl_seconds=60)
    assert store.get("search_suggestions:abc123") == {"query": "thunderfury"}

    cache_file = tmp_path / "search_suggestions" / "abc123.json"
    assert cache_file.exists()

    monkeypatch.setattr("warcraft_api.cache.time.time", lambda: now + 61)
    assert store.get("search_suggestions:abc123") is None
    assert not cache_file.exists()


def test_provider_legacy_list_keys_replay_without_http_or_token_fetch_and_still_require_auth(tmp_path: Path) -> None:
    store = FileCacheStore(tmp_path)
    keys = {
        "blizzard": b'["https://us.api.blizzard.com", "/data/wow/realm/index", {"locale": "en_US", "namespace": "dynamic-us"}]',
        "curseforge": b'["/v1/mods", {"gameId": 1, "searchFilter": "synthetic addon"}]',
        "lorrgs": b'["/api/roles", {"id": "synthetic"}]',
    }
    for namespace, raw in keys.items():
        store.set(f"{namespace}:{hashlib.sha256(raw).hexdigest()}", {"payload": {"source": namespace}}, ttl_seconds=60)
    with (
        BlizzardClient(auth=BlizzardAuthConfig("synthetic-id", "synthetic-secret", None, None)) as blizzard,
        CurseForgeClient(auth=CurseForgeAuthConfig("synthetic-key", None)) as curseforge,
        LorrgsClient() as lorrgs,
    ):
        for client in (blizzard, curseforge, lorrgs):
            client._cache_store = store
        routing = resolve_routing(region_input="us", namespace_class="dynamic")
        assert blizzard._get(routing, "/data/wow/realm/index")["payload"] == {"source": "blizzard"}
        assert curseforge._get("/v1/mods", params={"searchFilter": "synthetic addon", "gameId": 1})["payload"] == {"source": "curseforge"}
        assert lorrgs._get("/api/roles", params={"id": "synthetic", "unused": None}, ttl_seconds=60)["payload"] == {"source": "lorrgs"}
        assert all(client._http_client is None for client in (blizzard, curseforge, lorrgs))
        blizzard._client_id = ""
        curseforge._api_key = ""
        with pytest.raises(BlizzardClientError) as missing_blizzard:
            blizzard._get(routing, "/data/wow/realm/index")
        with pytest.raises(CurseForgeClientError) as missing_curseforge:
            curseforge._get("/v1/mods", params={"searchFilter": "synthetic addon", "gameId": 1})
        assert missing_blizzard.value.code == "missing_client_credentials"
        assert missing_curseforge.value.code == "missing_api_key"


def test_file_cache_lookups_are_recorded_with_the_hit_age_from_the_entry_mtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("warcraft_api.cache.time.time", lambda: 1000.0)
    with cache_ledger() as ledger:
        store = FileCacheStore(tmp_path)
        assert store.get("search_suggestions:abc123") is None
        store.set("search_suggestions:abc123", {"query": "thunderfury"}, ttl_seconds=600)
        os.utime(tmp_path / "search_suggestions" / "abc123.json", (1000.0, 1000.0))
        monkeypatch.setattr("warcraft_api.cache.time.time", lambda: 1120.5)
        assert store.get("search_suggestions:abc123") == {"query": "thunderfury"}
    block = ledger.provenance()
    assert block is not None
    assert (block["backend"], block["lookups"], block["hits"], block["all_hits"]) == ("file", 2, 1, False)
    assert (block["oldest_hit_age_seconds"], block["oldest_hit_ttl_seconds"]) == (120, 600)


def test_a_truncated_cache_entry_is_a_miss_and_is_deleted(tmp_path: Path) -> None:
    """A half-written entry must not fail every command that reads it until the cache is cleared by hand."""
    entry = tmp_path / "search_suggestions" / "abc123.json"
    entry.parent.mkdir(parents=True)
    entry.write_text('{"expires_at":', encoding="utf-8")

    with cache_ledger() as ledger:
        assert FileCacheStore(tmp_path).get("search_suggestions:abc123") is None

    assert not entry.exists()
    block = ledger.provenance()
    assert block is not None
    assert (block["lookups"], block["hits"]) == (1, 0)


def test_inaccessible_cache_paths_allow_provider_fetches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    blocked = tmp_path / "inaccessible"
    original_exists = Path.exists

    def exists(path: Path) -> bool:
        if path.is_relative_to(blocked):
            raise PermissionError("synthetic inaccessible cache parent")
        return original_exists(path)

    monkeypatch.setattr(Path, "exists", exists)
    requests: list[str] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(200, json={"version": "synthetic-report"})

    with cache_ledger() as ledger, RaidbotsClient() as client:
        client._cache_store = FileCacheStore(blocked)
        client._http_client = httpx.Client(transport=httpx.MockTransport(upstream))
        assert client.report_data("synthetic-id") == {"version": "synthetic-report"}

    assert len(requests) == 1
    assert client.last_from_cache is False
    provenance = ledger.provenance()
    assert provenance is not None
    assert (provenance["lookups"], provenance["hits"]) == (1, 0)


class ExpiringRedis:
    """Synthetic Redis command semantics, including Redis's rejection of SET EX 0."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def set(self, key: str, value: str, *, ex: int) -> None:
        if ex <= 0:
            raise ValueError("invalid expire time in set")
        self.values[key] = value

    def delete(self, key: str) -> None:
        self.values.pop(key, None)


@pytest.fixture
def expiring_redis_store(monkeypatch: pytest.MonkeyPatch) -> RedisCacheStore:
    monkeypatch.setattr(cache_module, "_FAILED_REDIS_URLS", set())
    redis = ExpiringRedis()
    return RedisCacheStore(
        redis_url="redis://synthetic-cache:6379/0",
        prefix="synthetic",
        import_module_func=lambda name: SimpleNamespace(from_url=lambda *args, **kwargs: redis),
    )


@pytest.mark.parametrize("ttl", [0, -1])
def test_zero_or_negative_redis_ttl_expires_the_key_without_disabling_healthy_cache(
    expiring_redis_store: RedisCacheStore, ttl: int
) -> None:
    with cache_ledger() as ledger:
        store = expiring_redis_store
        store.set("keep", {"value": "old"}, ttl_seconds=60)
        store.set("expire", {"value": "old"}, ttl_seconds=60)
        store.set("expire", {"value": "new"}, ttl_seconds=ttl)
        assert store.get("expire") is None
        assert store.get("keep") == {"value": "old"}
        store.set("later", {"value": "new"}, ttl_seconds=60)
        assert store.get("later") == {"value": "new"}
    provenance = ledger.provenance()
    assert provenance is not None
    assert (provenance["hits"], provenance["errors"]) == (2, 0)


def test_zero_finished_report_ttl_preserves_other_redis_cache_families(
    expiring_redis_store: RedisCacheStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = {"reportData": {"report": {"code": "synthetic", "endTime": 999999, "fights": []}}}
    monkeypatch.setenv("WARCRAFTLOGS_FINISHED_REPORT_CACHE_TTL_SECONDS", "0")
    with cache_ledger() as ledger, WarcraftLogsClient() as client:
        client._cache_store = expiring_redis_store
        monkeypatch.setattr(client, "_has_user_token", lambda: False)
        monkeypatch.setattr(client, "_token", lambda: "synthetic-token")
        monkeypatch.setattr(client, "_post_graphql", lambda *args, **kwargs: httpx.Response(200, json={"data": report}))
        assert client._finished_report_ttl == 0
        resolver = client._report_finish_ttl_resolver()
        assert resolver(report) == 0
        assert client._graphql(
            operation_name="ReportFights", query="synthetic-query", variables={}, namespace="report_fights",
            ttl_seconds=60, ttl_resolver=resolver,
        ) == report
        client._write_cache("static", {"zones": []}, ttl_seconds=60)
        assert client._read_cache("static") == {"zones": []}
    provenance = ledger.provenance()
    assert provenance is not None
    assert provenance["errors"] == 0


def test_redis_cache_store_uses_prefix_and_roundtrips() -> None:
    class FakeRedisClient:
        def __init__(self) -> None:
            self.values: dict[str, str] = {}
            self.set_calls: list[tuple[str, str, int]] = []

        def get(self, key: str) -> str | None:
            return self.values.get(key)

        def set(self, key: str, value: str, ex: int) -> None:
            self.values[key] = value
            self.set_calls.append((key, value, ex))

    fake_client = FakeRedisClient()

    class FakeRedisModule:
        @staticmethod
        def from_url(url: str, decode_responses: bool = True, **_: object) -> FakeRedisClient:
            assert url == "redis://cache.example:6379/3"
            assert decode_responses is True
            return fake_client

    store = RedisCacheStore(
        redis_url="redis://cache.example:6379/3",
        prefix="wowhead_cli",
        import_module_func=lambda name: FakeRedisModule,
    )

    store.set("entity:abc123", {"entity": {"id": 19019}}, ttl_seconds=3600)
    assert fake_client.set_calls == [
        ("wowhead_cli:entity:abc123", json.dumps({"entity": {"id": 19019}}, separators=(",", ":")), 3600)
    ]
    with cache_ledger() as ledger:
        assert store.get("entity:abc123") == {"entity": {"id": 19019}}
        assert store.get("entity:missing") is None
    # Redis keeps no store time, so a hit is counted without an age.
    assert ledger.provenance() == {
        "backend": "redis",
        "lookups": 2,
        "hits": 1,
        "hit": True,
        "all_hits": False,
        "oldest_hit_age_seconds": None,
        "oldest_hit_ttl_seconds": None,
        "errors": 0,
    }


def test_a_failed_file_cache_write_leaves_no_temp_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = FileCacheStore(tmp_path)
    store.set("search_suggestions:abc123", {"query": "old"}, ttl_seconds=60)

    def fail(*_: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("os.replace", fail)
    monkeypatch.setattr("pathlib.Path.replace", fail)
    store.set("search_suggestions:abc123", {"query": "new"}, ttl_seconds=60)

    assert [path.name for path in (tmp_path / "search_suggestions").iterdir()] == ["abc123.json"]
    assert store.get("search_suggestions:abc123") == {"query": "old"}


def test_a_file_cache_write_does_not_share_a_fixed_temp_name_with_other_writers(tmp_path: Path) -> None:
    """Another writer's temp file under the old fixed name must not block or tear this write."""
    store = FileCacheStore(tmp_path)
    (tmp_path / "search_suggestions" / "abc123.tmp").mkdir(parents=True)

    store.set("search_suggestions:abc123", {"query": "thunderfury"}, ttl_seconds=60)

    assert store.get("search_suggestions:abc123") == {"query": "thunderfury"}


def test_a_file_cache_entry_follows_the_umask(tmp_path: Path) -> None:
    previous = os.umask(0o022)
    try:
        FileCacheStore(tmp_path).set("search_suggestions:abc123", {"query": "thunderfury"}, ttl_seconds=60)
    finally:
        os.umask(previous)

    assert (tmp_path / "search_suggestions" / "abc123.json").stat().st_mode & 0o777 == 0o644


def test_a_redis_write_of_a_payload_json_cannot_hold_is_dropped() -> None:
    class RecordingRedisClient:
        sets = 0

        def set(self, *_: object, **__: object) -> None:
            RecordingRedisClient.sets += 1

    _redis_store(RecordingRedisClient()).set("entity:a", {"raw": b"bytes"}, ttl_seconds=60)

    assert RecordingRedisClient.sets == 0


def _redis_store(client: object, url: str = "redis://cache.example:6379/3") -> RedisCacheStore:
    class FakeRedisModule:
        @staticmethod
        def from_url(url: str, **_: object) -> object:
            return client

    return RedisCacheStore(redis_url=url, prefix="wowhead_cli", import_module_func=lambda name: FakeRedisModule)


def test_an_unreachable_redis_is_skipped_after_its_first_failure_and_reported_as_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cache_module, "_FAILED_REDIS_URLS", set())

    class HungRedisClient:
        calls = 0

        def _fail(self, *_: object, **__: object) -> None:
            HungRedisClient.calls += 1
            raise TimeoutError("Timeout connecting to server")

        get = set = _fail

    with cache_ledger() as ledger:
        store = _redis_store(HungRedisClient())
        assert store.get("entity:a") is None
        store.set("entity:a", {"id": 1}, ttl_seconds=60)
        # A second store on the same URL (another provider in the same process) skips it too.
        assert _redis_store(HungRedisClient()).get("entity:b") is None

    assert HungRedisClient.calls == 1
    block = ledger.provenance()
    assert block is not None
    assert (block["backend"], block["lookups"], block["hits"], block["errors"]) == ("redis", 2, 0, 3)


def test_redis_clients_get_short_timeouts() -> None:
    pytest.importorskip("redis")
    client = cache_module._build_redis_client("redis://cache.example:6379/3")
    kwargs = client.connection_pool.connection_kwargs
    assert (kwargs["socket_connect_timeout"], kwargs["socket_timeout"]) == (1.0, 2.0)


def _redis_settings(url: str | None = "redis://cache.example:6379/3") -> CacheSettings:
    return CacheSettings(
        enabled=True, backend="redis", cache_dir=Path("unused"), redis_url=url, prefix="p", ttls=CacheTTLConfig()
    )


def test_the_redis_backend_without_the_redis_extra_is_a_config_error_naming_the_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "redis", None)
    with pytest.raises(ValueError, match=r"warcraft\[redis\]"):
        build_cache_store(_redis_settings())


def test_an_invalid_redis_url_error_never_quotes_the_userinfo() -> None:
    class StrictRedisModule:
        @staticmethod
        def from_url(url: str, **_: object) -> object:
            # redis-py reads a password holding '/' as the port and quotes it back.
            raise ValueError("Port could not be cast to integer value as 'FAKEPASS'")

    with pytest.raises(ValueError) as caught:
        cache_module._build_redis_client(
            "redis://user:FAKEPASS/x@cache.example:6379/0", import_module_func=lambda name: StrictRedisModule
        )
    assert "FAKEPASS" not in str(caught.value)
    assert "Invalid Redis URL redis://***@" in str(caught.value)


def test_cache_backend_health_pings_redis_and_reports_why_it_is_unavailable() -> None:
    class Module:
        def __init__(self, ping_error: Exception | None) -> None:
            self.ping_error = ping_error

        def from_url(self, url: str, **_: object) -> Module:
            return self

        def ping(self) -> bool:
            if self.ping_error is not None:
                raise self.ping_error
            return True

    def health(module: Module) -> object:
        return cache_backend_health(_redis_settings(), import_module_func=lambda name: module)

    assert health(Module(None)) == {"available": True, "error": None}
    assert health(Module(ConnectionError("invalid username-password pair"))) == {
        "available": False,
        "error": "invalid username-password pair",
    }
    file_settings = CacheSettings(
        enabled=True, backend="file", cache_dir=Path("unused"), redis_url=None, prefix="p", ttls=CacheTTLConfig()
    )
    assert cache_backend_health(file_settings) == {"available": True, "error": None}


def test_cache_backend_health_reports_a_missing_redis_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "redis", None)
    health = cache_backend_health(_redis_settings())
    assert health["available"] is False
    assert "warcraft[redis]" in str(health["error"])


def test_inspect_file_cache_summarizes_active_expired_and_invalid_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FileCacheStore(tmp_path)
    now = 1000.0
    monkeypatch.setattr("warcraft_api.cache.time.time", lambda: now)

    store.set("search_suggestions:active", {"query": "thunderfury"}, ttl_seconds=60)
    store.set("entity_response:expired", {"entity": {"id": 19019}}, ttl_seconds=10)
    invalid_path = tmp_path / "tooltip_meta" / "broken.json"
    invalid_path.parent.mkdir(parents=True)
    invalid_path.write_text("not-json", encoding="utf-8")

    monkeypatch.setattr("warcraft_api.cache.time.time", lambda: now + 20)
    summary = inspect_file_cache(tmp_path)

    assert summary["totals"] == {"active": 1, "expired": 1, "invalid": 1, "total": 3}
    assert summary["age_summary"]["oldest_entry_age_hours"] >= 0
    assert summary["age_summary"]["newest_entry_age_hours"] >= 0
    assert summary["namespaces"]["search_suggestions"] == {"active": 1, "expired": 0, "invalid": 0, "total": 1}
    assert summary["namespaces"]["entity_response"] == {"total": 1, "active": 0, "expired": 1, "invalid": 0}
    assert summary["namespaces"]["tooltip_meta"] == {"total": 1, "active": 0, "expired": 0, "invalid": 1}


def test_inspect_file_cache_groups_root_level_hashed_entries_under_legacy_namespace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 1000.0
    monkeypatch.setattr("warcraft_api.cache.time.time", lambda: now + 20)
    legacy_path = tmp_path / ("a" * 64 + ".json")
    legacy_path.write_text(json.dumps({"expires_at": now + 10, "payload": {}}), encoding="utf-8")

    summary = inspect_file_cache(tmp_path)

    assert summary["namespaces"] == {
        "legacy_unscoped": {"active": 0, "expired": 1, "invalid": 0, "total": 1}
    }
    assert summary["totals"] == {"active": 0, "expired": 1, "invalid": 0, "total": 1}


def test_clear_file_cache_supports_namespace_and_expired_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FileCacheStore(tmp_path)
    now = 1000.0
    monkeypatch.setattr("warcraft_api.cache.time.time", lambda: now)

    store.set("search_suggestions:active", {"query": "thunderfury"}, ttl_seconds=60)
    store.set("entity_response:expired", {"entity": {"id": 19019}}, ttl_seconds=10)
    store.set("entity_response:active", {"entity": {"id": 19020}}, ttl_seconds=60)

    monkeypatch.setattr("warcraft_api.cache.time.time", lambda: now + 20)
    removed = clear_file_cache(tmp_path, namespaces=("entity_response",), expired_only=True)
    assert removed == {"total": 1, "namespaces": {"entity_response": 1}}

    summary = inspect_file_cache(tmp_path)
    assert summary["totals"] == {"active": 2, "expired": 0, "invalid": 0, "total": 2}
    assert summary["namespaces"]["entity_response"] == {"active": 1, "expired": 0, "invalid": 0, "total": 1}
    assert summary["namespaces"]["search_suggestions"] == {"total": 1, "active": 1, "expired": 0, "invalid": 0}


def test_inspect_and_clear_redis_cache_support_prefix_and_namespaces() -> None:
    class FakeRedisClient:
        def __init__(self) -> None:
            self.values = {
                "wowhead_cli:search_suggestions:a": "{}",
                "wowhead_cli:entity_response:b": "{}",
                "wowhead_cli:entity_response:c": "{}",
                "other_app:entity_response:d": "{}",
            }
            self.deleted: list[str] = []

        def scan_iter(self, match: str):  # noqa: ANN202
            if match == "*":
                return list(self.values)
            if match.endswith(":*") and match.count(":") == 1:
                prefix = match[:-1]
                return [key for key in self.values if key.startswith(prefix)]
            prefix = match[:-1]
            return [key for key in self.values if key.startswith(prefix)]

        def delete(self, key: str) -> int:
            if key in self.values:
                self.deleted.append(key)
                del self.values[key]
                return 1
            return 0

    fake_client = FakeRedisClient()

    class FakeRedisModule:
        @staticmethod
        def from_url(url: str, decode_responses: bool = True, **_: object) -> FakeRedisClient:
            assert url == "redis://cache.example:6379/3"
            assert decode_responses is True
            return fake_client

    summary = inspect_redis_cache(
        "redis://cache.example:6379/3",
        prefix="wowhead_cli",
        import_module_func=lambda name: FakeRedisModule,
    )
    assert summary == {
        "kind": "redis",
        "available": True,
        "count": 3,
        "namespaces": {"entity_response": 2, "search_suggestions": 1},
        "error": None,
    }

    visibility_summary = inspect_redis_cache(
        "redis://cache.example:6379/3",
        prefix="wowhead_cli",
        include_prefix_visibility=True,
        prefix_limit=2,
        import_module_func=lambda name: FakeRedisModule,
    )
    assert visibility_summary["prefix_visibility"] == {
        "current_prefix": "wowhead_cli",
        "current_prefix_count": 3,
        "other_prefix_count": 1,
        "other_prefixes_present": True,
        "isolated": False,
        "total_prefixes": 2,
        "prefixes": [
            {"prefix": "wowhead_cli", "count": 3, "current": True},
            {"prefix": "other_app", "count": 1, "current": False},
        ],
        "truncated": False,
    }

    removed = clear_redis_cache(
        "redis://cache.example:6379/3",
        prefix="wowhead_cli",
        namespaces=("entity_response",),
        import_module_func=lambda name: FakeRedisModule,
    )
    assert removed == {"total": 2, "namespaces": {"entity_response": 2}}
    assert fake_client.deleted == [
        "wowhead_cli:entity_response:b",
        "wowhead_cli:entity_response:c",
    ]


def test_inspect_redis_cache_reports_a_redis_that_drops_between_its_two_scans() -> None:
    class DroppingRedisClient:
        def scan_iter(self, match: str) -> list[str]:
            if match == "*":
                raise ConnectionError("Connection reset by peer")
            return ["wowhead_cli:entity_response:a"]

    class FakeRedisModule:
        @staticmethod
        def from_url(url: str, **_: object) -> DroppingRedisClient:
            return DroppingRedisClient()

    summary = inspect_redis_cache(
        "redis://cache.example:6379/3",
        prefix="wowhead_cli",
        include_prefix_visibility=True,
        import_module_func=lambda name: FakeRedisModule,
    )

    assert (summary["available"], summary["error"]) == (False, "Connection reset by peer")


def test_load_cache_settings_from_env_supports_redis_and_ttl_overrides(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("WOWHEAD_CACHE_BACKEND", "redis")
    monkeypatch.setenv("WOWHEAD_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("WOWHEAD_REDIS_URL", "redis://cache.example:6379/4")
    monkeypatch.setenv("WOWHEAD_REDIS_PREFIX", "wowhead_cli_test")
    monkeypatch.setenv("WOWHEAD_SEARCH_CACHE_TTL_SECONDS", "1200")
    monkeypatch.setenv("WOWHEAD_TOOLTIP_CACHE_TTL_SECONDS", "5400")

    settings = load_cache_settings_from_env()

    assert settings.enabled is True
    assert settings.backend == "redis"
    assert settings.cache_dir == (tmp_path / "cache")
    assert settings.redis_url == "redis://cache.example:6379/4"
    assert settings.prefix == "wowhead_cli_test"
    assert settings.ttls.search_suggestions == 1200
    assert settings.ttls.tooltip_meta == 5400
    assert settings.ttls.entity_page_html == 3600


def test_load_cache_settings_from_env_uses_shared_xdg_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("WOWHEAD_CACHE_DIR", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    settings = load_cache_settings_from_env()

    assert settings.cache_dir == (tmp_path / "cache" / "warcraft" / "wowhead" / "http")


def test_wowhead_client_uses_updated_default_cache_ttls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WOWHEAD_CACHE_BACKEND", "none")
    client = WowheadClient()
    assert client._cache_ttls.search_suggestions == 900
    assert client._cache_ttls.tooltip_meta == 3600
    assert client._cache_ttls.entity_page_html == 3600
    assert client._cache_ttls.guide_page_html == 3600
    assert client._cache_ttls.comment_replies == 1800
    assert client._cache_ttls.entity_response == 3600


def test_entity_response_cache_roundtrips_with_shape_flags(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("WOWHEAD_CACHE_BACKEND", "file")
    monkeypatch.setenv("WOWHEAD_CACHE_DIR", str(tmp_path))
    client = WowheadClient()
    payload = {"entity": {"type": "item", "id": 19019, "name": "Thunderfury"}}

    client.set_cached_entity_response(
        payload,
        requested_type="item",
        requested_id=19019,
        data_env=None,
        include_comments=False,
        include_all_comments=False,
        linked_entity_preview_limit=0,
    )

    cached = client.get_cached_entity_response(
        requested_type="item",
        requested_id=19019,
        data_env=None,
        include_comments=False,
        include_all_comments=False,
        linked_entity_preview_limit=0,
    )
    assert cached == payload

    changed_shape = client.get_cached_entity_response(
        requested_type="item",
        requested_id=19019,
        data_env=None,
        include_comments=True,
        include_all_comments=False,
        linked_entity_preview_limit=0,
    )
    assert changed_shape is None
    assert (
        client.get_cached_entity_response(
            requested_type="item",
            requested_id=19019,
            data_env=None,
            include_comments=False,
            include_all_comments=True,
            linked_entity_preview_limit=0,
        )
        is None
    )


def _echo_value(request: httpx.Request) -> str | None:
    params = request.url.params
    return params.get("q") or params.get("name") or params.get("srsearch")


@pytest.mark.parametrize(
    ("env_prefix", "make_client", "fetch"),
    [
        ("WOWHEAD", WowheadClient, lambda client, value: client.search_suggestions(value)["echo"]),
        (
            "RAIDERIO",
            RaiderIOClient,
            lambda client, value: client.character_profile(region="us", realm="illidan", name=value).payload["echo"],
        ),
        ("WARCRAFT_WIKI", WarcraftWikiClient, lambda client, value: client.search_articles(value, limit=5)[1][0]["title"]),
    ],
)
def test_http_cache_keys_include_the_request_params(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    env_prefix: str,
    make_client: Callable[[], Any],
    fetch: Callable[[Any, str], str],
) -> None:
    """Two requests that differ only in one param must not share a cache entry (file or in-process)."""
    monkeypatch.setenv(f"{env_prefix}_CACHE_BACKEND", "file")
    monkeypatch.setenv(f"{env_prefix}_CACHE_DIR", str(tmp_path))
    requested: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        value = _echo_value(request)
        requested.append(value)
        body = {"echo": value, "query": {"searchinfo": {"totalhits": 1}, "search": [{"title": value}]}}
        return httpx.Response(200, json=body)

    def client_with_transport() -> Any:
        client = make_client()
        client._http_client = httpx.Client(transport=httpx.MockTransport(handler))
        return client

    client = client_with_transport()
    assert fetch(client, "thunderfury") == "thunderfury"
    assert fetch(client, "ashkandi") == "ashkandi"
    assert requested == ["thunderfury", "ashkandi"]

    # A fresh client replays both from the file cache, so the cache was really on above.
    replay = client_with_transport()
    assert fetch(replay, "thunderfury") == "thunderfury"
    assert fetch(replay, "ashkandi") == "ashkandi"
    assert requested == ["thunderfury", "ashkandi"]


def test_entity_response_cache_is_scoped_by_expansion(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("WOWHEAD_CACHE_BACKEND", "file")
    monkeypatch.setenv("WOWHEAD_CACHE_DIR", str(tmp_path))
    retail_client = WowheadClient(expansion="retail")
    classic_client = WowheadClient(expansion="classic")

    retail_payload = {"expansion": "retail", "entity": {"type": "item", "id": 19019, "name": "Thunderfury"}}
    classic_payload = {"expansion": "classic", "entity": {"type": "item", "id": 19019, "name": "Thunderfury"}}

    retail_client.set_cached_entity_response(
        retail_payload,
        requested_type="item",
        requested_id=19019,
        data_env=None,
        include_comments=False,
        include_all_comments=False,
        linked_entity_preview_limit=0,
    )

    assert classic_client.get_cached_entity_response(
        requested_type="item",
        requested_id=19019,
        data_env=None,
        include_comments=False,
        include_all_comments=False,
        linked_entity_preview_limit=0,
    ) is None

    classic_client.set_cached_entity_response(
        classic_payload,
        requested_type="item",
        requested_id=19019,
        data_env=None,
        include_comments=False,
        include_all_comments=False,
        linked_entity_preview_limit=0,
    )

    assert retail_client.get_cached_entity_response(
        requested_type="item",
        requested_id=19019,
        data_env=None,
        include_comments=False,
        include_all_comments=False,
        linked_entity_preview_limit=0,
    ) == retail_payload
    assert classic_client.get_cached_entity_response(
        requested_type="item",
        requested_id=19019,
        data_env=None,
        include_comments=False,
        include_all_comments=False,
        linked_entity_preview_limit=0,
    ) == classic_payload


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("redis://user:FAKEPASS@cache.example:6380/2?password=QUERYPASS", "redis://***@cache.example:6380/2"),
        ("redis://:FAKE@PASS@cache.example:6380/2", "redis://***@cache.example:6380/2"),
        ("redis://:FA[KE@PA]SS@cache.example:6380/2", "redis://***@cache.example:6380/2"),
        ("redis://user:FAKE/PASS@cache.example:6380/2", "redis://***@cache.example:6380/2"),
        ("redis://user:FAKE?PASS@cache.example:6380/2", "redis://***@cache.example:6380/2"),
        ("redis://:FAKE/PASS?x@cache.example:6380/2", "redis://***@cache.example:6380/2"),
        ("redis://:FAKE#PASS@cache.example:6380/2", "redis://***@cache.example:6380/2"),
        ("redis://cache.example:6379/0", "redis://cache.example:6379/0"),
        ("FAKEPASS@cache.example", "***"),
        (None, None),
    ],
)
def test_redacted_redis_url_hides_every_credential(url: str | None, expected: str | None) -> None:
    assert redacted_redis_url(url) == expected


@pytest.mark.parametrize(
    ("env_prefix", "provider_module"),
    [
        ("RAIDERIO", "raiderio_cli.provider"),
        ("RAIDBOTS", "raidbots_cli.provider"),
        ("WARCRAFT_WIKI", "warcraft_wiki_cli.provider"),
        ("ICY_VEINS", "icy_veins_cli.provider"),
        ("METHOD", "method_cli.provider"),
    ],
)
def test_no_provider_doctor_prints_the_redis_password(monkeypatch, env_prefix: str, provider_module: str) -> None:
    monkeypatch.setenv(f"{env_prefix}_REDIS_URL", "redis://:FAKE@PASS@cache.example:6380/2")
    provider = importlib.import_module(provider_module).PROVIDER
    payload = json.dumps(provider.doctor())
    assert "PASS" not in payload
    assert "redis://***@cache.example:6380/2" in payload


def test_wowhead_cache_settings_never_print_the_redis_password(monkeypatch) -> None:
    """Feeds wowhead doctor, cache-inspect and cache-clear (doctor itself probes live)."""
    from wowhead_cli.provider import cache_settings_payload

    monkeypatch.setenv("WOWHEAD_REDIS_URL", "redis://:FAKE@PASS@cache.example:6380/2")
    payload = cache_settings_payload(load_cache_settings_from_env())
    assert payload["redis_url"] == "redis://***@cache.example:6380/2"

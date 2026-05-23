from __future__ import annotations

import json
from pathlib import Path

import pytest
from wowhead_cli.cache import (
    CacheTTLConfig,
    FileCacheStore,
    RedisCacheStore,
    clear_file_cache,
    clear_redis_cache,
    inspect_file_cache,
    inspect_redis_cache,
    load_cache_settings_from_env,
    repair_file_cache,
)
from wowhead_cli.wowhead_client import WowheadClient


def test_file_cache_store_roundtrips_and_expires(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = FileCacheStore(tmp_path)
    now = 1000.0
    monkeypatch.setattr("wowhead_cli.cache.time.time", lambda: now)

    store.set("search_suggestions:abc123", {"query": "thunderfury"}, ttl_seconds=60)
    assert store.get("search_suggestions:abc123") == {"query": "thunderfury"}

    cache_file = tmp_path / "search_suggestions" / "abc123.json"
    assert cache_file.exists()

    monkeypatch.setattr("wowhead_cli.cache.time.time", lambda: now + 61)
    assert store.get("search_suggestions:abc123") is None
    assert not cache_file.exists()


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
        def from_url(url: str, decode_responses: bool = True) -> FakeRedisClient:
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
    assert store.get("entity:abc123") == {"entity": {"id": 19019}}


def test_inspect_file_cache_summarizes_active_expired_and_invalid_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FileCacheStore(tmp_path)
    now = 1000.0
    monkeypatch.setattr("wowhead_cli.cache.time.time", lambda: now)

    store.set("search_suggestions:active", {"query": "thunderfury"}, ttl_seconds=60)
    store.set("entity_response:expired", {"entity": {"id": 19019}}, ttl_seconds=10)
    invalid_path = tmp_path / "tooltip_meta" / "broken.json"
    invalid_path.parent.mkdir(parents=True)
    invalid_path.write_text("not-json", encoding="utf-8")

    monkeypatch.setattr("wowhead_cli.cache.time.time", lambda: now + 20)
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
    monkeypatch.setattr("wowhead_cli.cache.time.time", lambda: now + 20)
    legacy_path = tmp_path / ("a" * 64 + ".json")
    legacy_path.write_text(json.dumps({"expires_at": now + 10, "payload": {}}), encoding="utf-8")

    summary = inspect_file_cache(tmp_path)

    assert summary["namespaces"] == {
        "legacy_unscoped": {"active": 0, "expired": 1, "invalid": 0, "total": 1}
    }
    assert summary["totals"] == {"active": 0, "expired": 1, "invalid": 0, "total": 1}


def test_repair_file_cache_prunes_legacy_unscoped_entries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = 1000.0
    monkeypatch.setattr("wowhead_cli.cache.time.time", lambda: now + 20)
    legacy_path = tmp_path / ("a" * 64 + ".json")
    legacy_path.write_text(json.dumps({"expires_at": now + 10, "payload": {}}), encoding="utf-8")
    namespaced_path = tmp_path / "search_suggestions" / "active.json"
    namespaced_path.parent.mkdir(parents=True)
    namespaced_path.write_text(json.dumps({"expires_at": now + 120, "payload": {}}), encoding="utf-8")

    dry_run = repair_file_cache(tmp_path, apply=False, sample_limit=5)
    assert dry_run == {
        "mode": "legacy_unscoped",
        "apply": False,
        "expired_only": False,
        "candidates": 1,
        "removed": 0,
        "sample_paths": [str(legacy_path)],
        "truncated": False,
    }
    assert legacy_path.exists() is True

    applied = repair_file_cache(tmp_path, apply=True, expired_only=True, sample_limit=5)
    assert applied["expired_only"] is True
    assert applied["removed"] == 1
    assert legacy_path.exists() is False
    assert namespaced_path.exists() is True


def test_clear_file_cache_supports_namespace_and_expired_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FileCacheStore(tmp_path)
    now = 1000.0
    monkeypatch.setattr("wowhead_cli.cache.time.time", lambda: now)

    store.set("search_suggestions:active", {"query": "thunderfury"}, ttl_seconds=60)
    store.set("entity_response:expired", {"entity": {"id": 19019}}, ttl_seconds=10)
    store.set("entity_response:active", {"entity": {"id": 19020}}, ttl_seconds=60)

    monkeypatch.setattr("wowhead_cli.cache.time.time", lambda: now + 20)
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
        def from_url(url: str, decode_responses: bool = True) -> FakeRedisClient:
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


def test_wowhead_client_uses_updated_default_cache_ttls() -> None:
    client = WowheadClient(cache_enabled=False, cache_ttls=CacheTTLConfig())
    assert client._cache_ttls.search_suggestions == 900
    assert client._cache_ttls.tooltip_meta == 3600
    assert client._cache_ttls.entity_page_html == 3600
    assert client._cache_ttls.guide_page_html == 3600
    assert client._cache_ttls.comment_replies == 1800
    assert client._cache_ttls.entity_response == 3600


def test_entity_response_cache_roundtrips_with_shape_flags(tmp_path: Path) -> None:
    client = WowheadClient(cache_dir=tmp_path, cache_backend="file")
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


def test_entity_response_cache_is_scoped_by_expansion(tmp_path: Path) -> None:
    retail_client = WowheadClient(cache_dir=tmp_path, cache_backend="file", expansion="retail")
    classic_client = WowheadClient(cache_dir=tmp_path, cache_backend="file", expansion="classic")

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

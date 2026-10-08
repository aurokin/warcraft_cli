"""Client modules are imported before pytest changes HOME/XDG; roots must resolve on use."""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from icy_veins_cli.client import load_icy_veins_cache_settings_from_env
from method_cli.client import load_method_cache_settings_from_env
from raiderio_cli.client import load_raiderio_cache_settings_from_env
from warcraft_api.cache import CacheSettings, FileCacheStore, build_cache_store
from warcraft_wiki_cli.client import load_warcraft_wiki_cache_settings_from_env


@pytest.mark.parametrize(("provider", "prefix", "load_settings"), [
    ("method", "METHOD", load_method_cache_settings_from_env),
    ("icy-veins", "ICY_VEINS", load_icy_veins_cache_settings_from_env),
    ("raiderio", "RAIDERIO", load_raiderio_cache_settings_from_env),
    ("warcraft-wiki", "WARCRAFT_WIKI", load_warcraft_wiki_cache_settings_from_env),
])
def test_cache_root_follows_xdg_changes_after_module_import(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    provider: str, prefix: str, load_settings: Callable[[], tuple[CacheSettings, *tuple[int, ...]]],
) -> None:
    monkeypatch.setenv(f"{prefix}_CACHE_BACKEND", "file")
    for run in ("first", "second"):
        root = tmp_path / run
        monkeypatch.setenv("XDG_CACHE_HOME", str(root))
        settings = load_settings()[0]
        assert settings.cache_dir == root / "warcraft" / provider / "http"
        store = build_cache_store(settings)
        assert isinstance(store, FileCacheStore)
        store.set("test", {"run": run}, ttl_seconds=60)
        assert list(root.rglob("*.json")), "Enabled caches must write only below the current isolated root"

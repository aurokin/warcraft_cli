from __future__ import annotations

from warcraft_core.paths import provider_cache_root, provider_config_root, provider_data_root, worktree_root, worktree_runtime_details


def test_provider_roots_use_xdg_homes(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    assert provider_config_root("simc") == (tmp_path / "config" / "warcraft" / "simc")
    assert provider_data_root("simc") == (tmp_path / "data" / "warcraft" / "simc")
    assert provider_cache_root("simc") == (tmp_path / "cache" / "warcraft" / "simc")


def test_worktree_runtime_isolates_data_and_cache_but_keeps_config_shared(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    monkeypatch.setenv("WARCRAFT_WORKTREE_ROOT", str(tmp_path / "repo"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("XDG_DATA_HOME")
    monkeypatch.delenv("XDG_CACHE_HOME")

    assert provider_config_root("simc") == (tmp_path / "config" / "warcraft" / "simc")
    assert provider_data_root("simc") == (tmp_path / "repo" / ".warcraft" / "runtime" / "data" / "simc")
    assert provider_cache_root("simc") == (tmp_path / "repo" / ".warcraft" / "runtime" / "cache" / "simc")
    assert worktree_runtime_details() == {
        "active": True,
        "worktree_root": str((tmp_path / "repo").resolve()),
        "runtime_root": str((tmp_path / "repo" / ".warcraft" / "runtime").resolve()),
        "isolated_roots": ["data", "cache"],
        "shared_roots": ["config", "state"],
    }


def test_explicit_xdg_roots_override_worktree_runtime(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    monkeypatch.setenv("WARCRAFT_WORKTREE_ROOT", str(tmp_path / "repo"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    assert provider_data_root("simc") == (tmp_path / "data" / "warcraft" / "simc")
    assert provider_cache_root("simc") == (tmp_path / "cache" / "warcraft" / "simc")
    assert worktree_runtime_details() == {
        "active": True,
        "worktree_root": str((tmp_path / "repo").resolve()),
        "runtime_root": str((tmp_path / "repo" / ".warcraft" / "runtime").resolve()),
        "isolated_roots": [],
        "shared_roots": ["data", "cache", "config", "state"],
    }


def test_explicit_runtime_dir_activates_worktree_runtime_without_root(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    monkeypatch.setenv("WARCRAFT_WORKTREE_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.delenv("XDG_DATA_HOME")
    monkeypatch.delenv("XDG_CACHE_HOME")

    assert provider_data_root("simc") == (tmp_path / "runtime" / "data" / "simc")
    assert provider_cache_root("simc") == (tmp_path / "runtime" / "cache" / "simc")
    assert worktree_runtime_details() == {
        "active": True,
        "worktree_root": str(worktree_root()),
        "runtime_root": str((tmp_path / "runtime").resolve()),
        "isolated_roots": ["data", "cache"],
        "shared_roots": ["config", "state"],
    }

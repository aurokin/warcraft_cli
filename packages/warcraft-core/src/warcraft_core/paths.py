from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def _explicit_xdg_root(env_name: str) -> Path | None:
    value = os.getenv(env_name)
    if value and value.strip():
        return Path(value).expanduser() / "warcraft"
    return None


def _looks_like_worktree_root(candidate: Path) -> bool:
    return (
        (candidate / "pyproject.toml").is_file()
        and (candidate / "Makefile").is_file()
        and (candidate / "packages" / "warcraft-core" / "src" / "warcraft_core").is_dir()
        and ((candidate / ".git").is_dir() or (candidate / ".git").is_file())
    )


def worktree_root() -> Path | None:
    explicit_root = os.getenv("WARCRAFT_WORKTREE_ROOT")
    if explicit_root and explicit_root.strip():
        return Path(explicit_root).expanduser().resolve()

    module_path = Path(__file__).resolve()
    for candidate in module_path.parents:
        if _looks_like_worktree_root(candidate):
            return candidate
    return None


def worktree_runtime_root() -> Path | None:
    explicit_runtime_root = os.getenv("WARCRAFT_WORKTREE_RUNTIME_DIR")
    if explicit_runtime_root and explicit_runtime_root.strip():
        return Path(explicit_runtime_root).expanduser().resolve()
    root = worktree_root()
    if root is None:
        return None
    return root / ".warcraft" / "runtime"


def worktree_runtime_details() -> dict[str, Any]:
    root = worktree_root()
    runtime_root = worktree_runtime_root()
    isolated_roots: list[str] = []
    shared_roots: list[str] = []

    if runtime_root is not None:
        if _explicit_xdg_root("XDG_DATA_HOME") is None:
            isolated_roots.append("data")
        else:
            shared_roots.append("data")

        if _explicit_xdg_root("XDG_CACHE_HOME") is None:
            isolated_roots.append("cache")
        else:
            shared_roots.append("cache")

        shared_roots = [*shared_roots, "config", "state"]

    return {
        "active": runtime_root is not None,
        "worktree_root": str(root) if root is not None else None,
        "runtime_root": str(runtime_root) if runtime_root is not None else None,
        "isolated_roots": isolated_roots,
        "shared_roots": shared_roots,
    }


def _xdg_root(env_name: str, fallback_suffix: tuple[str, ...]) -> Path:
    explicit_root = _explicit_xdg_root(env_name)
    if explicit_root is not None:
        return explicit_root
    return Path.home().joinpath(*fallback_suffix, "warcraft")


def config_root() -> Path:
    return _xdg_root("XDG_CONFIG_HOME", (".config",))


def data_root() -> Path:
    explicit_root = _explicit_xdg_root("XDG_DATA_HOME")
    if explicit_root is not None:
        return explicit_root
    runtime_root = worktree_runtime_root()
    if runtime_root is not None:
        return runtime_root / "data"
    return _xdg_root("XDG_DATA_HOME", (".local", "share"))


def state_root() -> Path:
    return _xdg_root("XDG_STATE_HOME", (".local", "state"))


def cache_root() -> Path:
    explicit_root = _explicit_xdg_root("XDG_CACHE_HOME")
    if explicit_root is not None:
        return explicit_root
    runtime_root = worktree_runtime_root()
    if runtime_root is not None:
        return runtime_root / "cache"
    return _xdg_root("XDG_CACHE_HOME", (".cache",))


def provider_data_root(provider: str) -> Path:
    return data_root() / provider


def provider_config_root(provider: str) -> Path:
    return config_root() / provider


def provider_env_path(provider: str) -> Path:
    return config_root() / "providers" / f"{provider}.env"


def provider_state_path(provider: str) -> Path:
    return state_root() / "providers" / f"{provider}.json"


def provider_cache_root(provider: str) -> Path:
    return cache_root() / provider

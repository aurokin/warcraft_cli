from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from warcraft_core.paths import provider_config_root, provider_data_root

SIMC_REPO_URL = "https://github.com/simulationcraft/simc.git"


@dataclass(slots=True)
class RepoPaths:
    root: Path
    apl_default: Path
    apl_assisted: Path
    class_modules: Path
    spell_dump: Path
    build_dir: Path
    build_simc: Path


@dataclass(slots=True)
class RepoResolution:
    root: Path
    source: str
    config_path: Path
    configured_root: Path | None
    managed_root: Path
    managed_exists: bool


@dataclass(slots=True)
class CheckoutResult:
    status: str
    root: Path
    repo_url: str
    commands: list[list[str]]


def config_path() -> Path:
    return provider_config_root("simc") / "repo.json"


def managed_repo_root() -> Path:
    return provider_data_root("simc") / "repo"


def load_configured_repo_root() -> Path | None:
    path = config_path()
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text())
    except json.JSONDecodeError:
        return None
    value = payload.get("repo_root")
    if not isinstance(value, str) or not value.strip():
        return None
    return Path(value).expanduser()


def save_configured_repo_root(root: str | Path) -> Path:
    resolved = Path(root).expanduser().resolve()
    target = config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"repo_root": str(resolved)}, indent=2) + "\n")
    return resolved


def clear_configured_repo_root() -> bool:
    target = config_path()
    if not target.exists():
        return False
    target.unlink()
    return True


def default_repo_root() -> Path:
    configured = os.environ.get("SIMC_REPO_ROOT")
    if configured:
        return Path(configured).expanduser()
    configured_root = load_configured_repo_root()
    if configured_root is not None:
        return configured_root
    managed_root = managed_repo_root()
    return managed_root


def resolve_repo_root(root: str | Path | None = None) -> RepoResolution:
    config = config_path()
    configured_root = load_configured_repo_root()
    managed_root = managed_repo_root()
    managed_exists = managed_root.exists()
    if root:
        selected = Path(root).expanduser()
        source = "cli_override"
    elif os.environ.get("SIMC_REPO_ROOT"):
        selected = Path(os.environ["SIMC_REPO_ROOT"]).expanduser()
        source = "env"
    elif configured_root is not None:
        selected = configured_root
        source = "config"
    elif managed_exists:
        selected = managed_root
        source = "managed"
    else:
        selected = managed_root
        source = "unset"
    return RepoResolution(
        root=selected.resolve(),
        source=source,
        config_path=config,
        configured_root=configured_root.resolve() if configured_root is not None else None,
        managed_root=managed_root.resolve(),
        managed_exists=managed_exists,
    )


def discover_repo(root: str | Path | None = None) -> RepoPaths:
    repo_root = resolve_repo_root(root).root
    return RepoPaths(
        root=repo_root,
        apl_default=repo_root / "ActionPriorityLists" / "default",
        apl_assisted=repo_root / "ActionPriorityLists" / "assisted_combat",
        class_modules=repo_root / "engine" / "class_modules",
        spell_dump=repo_root / "SpellDataDump",
        build_dir=repo_root / "build",
        build_simc=repo_root / "build" / "simc",
    )


def validate_repo(paths: RepoPaths) -> list[str]:
    missing: list[str] = []
    for label, path in (
        ("repo root", paths.root),
        ("default APL dir", paths.apl_default),
        ("assisted APL dir", paths.apl_assisted),
        ("class modules dir", paths.class_modules),
        ("spell dump dir", paths.spell_dump),
    ):
        if not path.exists():
            missing.append(f"{label}: {path}")
    return missing


def validate_build(paths: RepoPaths) -> list[str]:
    missing: list[str] = []
    for label, path in (
        ("build dir", paths.build_dir),
        ("simc binary", paths.build_simc),
    ):
        if not path.exists():
            missing.append(f"{label}: {path}")
    return missing


def checkout_managed_repo(*, repo_url: str = SIMC_REPO_URL) -> CheckoutResult:
    root = managed_repo_root()
    root.parent.mkdir(parents=True, exist_ok=True)
    commands: list[list[str]] = []
    if not root.exists():
        clone_command = ["git", "clone", "--depth", "1", repo_url, str(root)]
        commands.append(clone_command)
        clone = subprocess.run(clone_command, capture_output=True, text=True, check=False)
        if clone.returncode != 0:
            message = clone.stderr.strip() or clone.stdout.strip() or "git clone failed"
            raise RuntimeError(message)
        return CheckoutResult(status="cloned", root=root.resolve(), repo_url=repo_url, commands=commands)

    fetch_command = ["git", "-C", str(root), "pull", "--ff-only"]
    commands.append(fetch_command)
    fetch = subprocess.run(fetch_command, capture_output=True, text=True, check=False)
    if fetch.returncode != 0:
        message = fetch.stderr.strip() or fetch.stdout.strip() or "git pull failed"
        raise RuntimeError(message)
    return CheckoutResult(status="updated", root=root.resolve(), repo_url=repo_url, commands=commands)

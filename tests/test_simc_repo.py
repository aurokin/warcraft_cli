from __future__ import annotations

from pathlib import Path

from simc_cli.repo import (
    checkout_managed_repo,
    clear_configured_repo_root,
    discover_repo,
    resolve_repo_root,
    save_configured_repo_root,
    validate_build,
    validate_repo,
)


def _make_repo(root: Path, *, with_binary: bool = True) -> None:
    (root / "ActionPriorityLists" / "default").mkdir(parents=True)
    (root / "ActionPriorityLists" / "assisted_combat").mkdir(parents=True)
    (root / "engine" / "class_modules").mkdir(parents=True)
    (root / "SpellDataDump").mkdir(parents=True)
    (root / "build").mkdir(parents=True)
    if with_binary:
        binary = root / "build" / "simc"
        binary.write_text("")
        binary.chmod(0o755)


def test_discover_repo_uses_override_path(tmp_path: Path) -> None:
    _make_repo(tmp_path)
    paths = discover_repo(tmp_path)
    assert paths.root == tmp_path.resolve()
    assert paths.apl_default.exists()
    assert paths.build_simc.exists()


def test_validate_repo_reports_missing_paths(tmp_path: Path) -> None:
    paths = discover_repo(tmp_path)
    issues = validate_repo(paths)
    assert issues
    assert any("default APL dir" in issue for issue in issues)


def test_validate_build_reports_missing_binary(tmp_path: Path) -> None:
    _make_repo(tmp_path, with_binary=False)
    paths = discover_repo(tmp_path)
    issues = validate_build(paths)
    assert any("simc binary" in issue for issue in issues)


def test_resolve_repo_root_prefers_config_then_managed(monkeypatch, tmp_path: Path) -> None:
    config_home = tmp_path / "config"
    data_home = tmp_path / "data"
    configured = tmp_path / "configured-simc"
    managed = data_home / "warcraft" / "simc" / "repo"
    configured.mkdir(parents=True)
    managed.mkdir(parents=True)

    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    monkeypatch.delenv("SIMC_REPO_ROOT", raising=False)

    save_configured_repo_root(configured)
    resolution = resolve_repo_root()
    assert resolution.source == "config"
    assert resolution.root == configured.resolve()

    clear_configured_repo_root()
    resolution = resolve_repo_root()
    assert resolution.source == "managed"
    assert resolution.root == managed.resolve()


def test_resolve_repo_root_reports_unset_when_managed_missing(monkeypatch, tmp_path: Path) -> None:
    config_home = tmp_path / "config"
    data_home = tmp_path / "data"
    managed = data_home / "warcraft" / "simc" / "repo"

    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    monkeypatch.delenv("SIMC_REPO_ROOT", raising=False)

    resolution = resolve_repo_root()
    assert resolution.source == "unset"
    assert resolution.root == managed.resolve()
    assert resolution.configured_root is None
    assert resolution.managed_root == managed.resolve()
    assert resolution.managed_exists is False


def test_resolve_repo_root_env_overrides_config(monkeypatch, tmp_path: Path) -> None:
    config_home = tmp_path / "config"
    data_home = tmp_path / "data"
    configured = tmp_path / "configured-simc"
    env_root = tmp_path / "env-simc"
    configured.mkdir(parents=True)
    env_root.mkdir(parents=True)

    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    monkeypatch.setenv("SIMC_REPO_ROOT", str(env_root))

    save_configured_repo_root(configured)
    resolution = resolve_repo_root()
    assert resolution.source == "env"
    assert resolution.root == env_root.resolve()


def test_checkout_managed_repo_clones_and_updates(monkeypatch, tmp_path: Path) -> None:
    config_home = tmp_path / "config"
    data_home = tmp_path / "data"
    root = data_home / "warcraft" / "simc" / "repo"

    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))

    calls: list[list[str]] = []

    def fake_run(command, capture_output, text, check):  # noqa: ANN001
        calls.append(command)
        if command[:2] == ["git", "clone"]:
            root.mkdir(parents=True, exist_ok=True)
        return type("Proc", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr("simc_cli.repo.subprocess.run", fake_run)

    first = checkout_managed_repo()
    assert first.status == "cloned"
    assert root.exists()
    assert calls[0][:2] == ["git", "clone"]

    second = checkout_managed_repo()
    assert second.status == "updated"
    assert calls[1][:4] == ["git", "-C", str(root), "pull"]

from __future__ import annotations

import os
import shutil
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


def test_build_repo_reconfigures_before_building_so_the_binary_revision_follows_the_checkout(
    monkeypatch, tmp_path: Path
) -> None:
    from simc_cli import run as run_module

    _make_repo(tmp_path, with_binary=True)
    paths = discover_repo(tmp_path)
    calls: list[list[str]] = []

    def fake_run(command: list[str], *, cwd: Path | None = None) -> run_module.CommandResult:
        calls.append(command)
        return run_module.CommandResult(command=command, cwd=cwd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(run_module, "_run", fake_run)
    monkeypatch.setattr(run_module.os, "cpu_count", lambda: 6)
    (paths.build_dir / "CMakeCache.txt").write_text("CMAKE_BUILD_TYPE:STRING=Debug\n")
    run_module.build_repo(paths, target="simc")
    assert calls == [
        ["cmake", "-S", str(paths.root), "-B", str(paths.build_dir)],
        ["cmake", "--build", str(paths.build_dir), "--parallel", "6", "--target", "simc"],
    ]


def test_build_repo_configures_a_fresh_build_optimized_and_without_the_gui(monkeypatch, tmp_path: Path) -> None:
    """SimC's CMake leaves CMAKE_BUILD_TYPE empty and defaults BUILD_GUI ON (which needs Qt); a cache keeps the user's choice.

    Runs a fake ``cmake`` on PATH that records its arguments and writes a cache on configure, as cmake does.
    """
    from simc_cli import run as run_module

    _make_repo(tmp_path, with_binary=True)
    paths = discover_repo(tmp_path)
    shutil.rmtree(paths.build_dir)
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    record = tmp_path / "cmake_calls.txt"
    fake_cmake = bin_dir / "cmake"
    fake_cmake.write_text(
        "#!/bin/sh\n"
        f'echo "$*" >> "{record}"\n'
        'if [ "$1" = "-S" ]; then mkdir -p "$4" && echo "CMAKE_BUILD_TYPE:STRING=Release" > "$4/CMakeCache.txt"; fi\n'
    )
    fake_cmake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    assert run_module.build_repo(paths, target="simc").returncode == 0
    assert run_module.build_repo(paths, target="simc").returncode == 0

    configures = [line.split() for line in record.read_text().splitlines() if line.startswith("-S")]
    assert configures[0][4:] == ["-DCMAKE_BUILD_TYPE=Release", "-DBUILD_GUI=OFF"]
    assert configures[1][4:] == []


def test_build_repo_stops_when_configure_fails(monkeypatch, tmp_path: Path) -> None:
    from simc_cli import run as run_module

    _make_repo(tmp_path, with_binary=True)
    paths = discover_repo(tmp_path)
    calls: list[list[str]] = []

    def fake_run(command: list[str], *, cwd: Path | None = None) -> run_module.CommandResult:
        calls.append(command)
        return run_module.CommandResult(command=command, cwd=cwd, returncode=1, stdout="", stderr="no CMakeLists")

    monkeypatch.setattr(run_module, "_run", fake_run)
    result = run_module.build_repo(paths, target="simc")
    assert result.returncode == 1
    assert len(calls) == 1

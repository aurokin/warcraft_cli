"""Static packaging-consistency guards for the warcraft-cli wrapper.

The wrapper imports every provider app at module load. The primary supported
install path (the ``warcraft`` umbrella wheel) bundles every provider ``src/``,
but a standalone ``pip install warcraft-cli`` only pulls in what the wrapper
declares as a runtime dependency. These tests keep the declared deps in sync
with what is actually imported so the standalone path cannot silently break.
"""

from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGES = REPO_ROOT / "packages"
WRAPPER_PKG = PACKAGES / "warcraft-cli"
WRAPPER_PYPROJECT = WRAPPER_PKG / "pyproject.toml"
WRAPPER_SOURCES = (
    WRAPPER_PKG / "src" / "warcraft_cli" / "main.py",
    WRAPPER_PKG / "src" / "warcraft_cli" / "providers.py",
)


def _dependency_names(pyproject: Path) -> set[str]:
    data = tomllib.loads(pyproject.read_text())
    names: set[str] = set()
    for dep in data["project"]["dependencies"]:
        name = re.split(r"[<>=!~ \[;]", dep, maxsplit=1)[0].strip().lower()
        names.add(name)
    return names


def _imported_provider_modules() -> set[str]:
    """Top-level ``*_cli`` modules imported by the wrapper (excluding itself)."""
    modules: set[str] = set()
    for source in WRAPPER_SOURCES:
        tree = ast.parse(source.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                modules.add(node.module.split(".")[0])
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    modules.add(alias.name.split(".")[0])
    return {m for m in modules if m.endswith("_cli") and m != "warcraft_cli"}


def test_every_imported_provider_app_is_a_declared_dependency() -> None:
    declared = _dependency_names(WRAPPER_PYPROJECT)
    missing = sorted(
        module.replace("_", "-")
        for module in _imported_provider_modules()
        if module.replace("_", "-") not in declared
    )
    assert not missing, (
        "warcraft-cli imports provider apps it does not declare as runtime deps: "
        f"{missing}. Add them to packages/warcraft-cli/pyproject.toml."
    )


def test_warcraftlogs_cli_pyproject_declares_console_script_and_deps() -> None:
    pyproject = PACKAGES / "warcraftlogs-cli" / "pyproject.toml"
    assert pyproject.exists(), "packages/warcraftlogs-cli/pyproject.toml must exist"
    data = tomllib.loads(pyproject.read_text())

    assert data["project"]["name"] == "warcraftlogs-cli"
    assert data["project"]["scripts"]["warcraftlogs"] == "warcraftlogs_cli.main:run"

    deps = _dependency_names(pyproject)
    # The warcraftlogs.main -> simc_cli.talent_transport edge is the single
    # import-linter ``ignore_imports`` exception; declare it as a real dep.
    assert "simc-cli" in deps
    assert {
        "typer",
        "httpx",
        "warcraft-core-cli",
        "warcraft-api-cli",
        "warcraft-content-cli",
    } <= deps

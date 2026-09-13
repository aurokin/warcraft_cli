"""Every package's declared dependencies must match what its source actually imports.

The supported install path is the root ``warcraft`` wheel, which bundles every ``src/``. The
per-package pyprojects still have to be truthful: CI proves one provider installs in isolation, and
an under-declared dependency only fails there. Over-declaration is equally a defect - it drags
packages into an install that never imports them.
"""

from __future__ import annotations

import ast
import re
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGES = REPO_ROOT / "packages"

# Import name -> distribution name, for the cases where they differ. Everything else is the module
# name with underscores turned into hyphens (``wowhead_cli`` -> ``wowhead-cli``, ``httpx``, ``typer``).
MODULE_TO_DIST = {
    "bs4": "beautifulsoup4",
    "curl_cffi": "curl-cffi",
    "warcraft_api": "warcraft-api-cli",
    "warcraft_content": "warcraft-content-cli",
    "warcraft_core": "warcraft-core-cli",
}

PACKAGE_DIRS = sorted(path.name for path in PACKAGES.iterdir() if (path / "pyproject.toml").exists())


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _pyproject(package_dir: str) -> dict[str, Any]:
    return tomllib.loads((PACKAGES / package_dir / "pyproject.toml").read_text())


def _declared_dependencies(data: dict[str, Any]) -> set[str]:
    return {_normalize(re.split(r"[<>=!~ \[;]", dep, maxsplit=1)[0]) for dep in data["project"].get("dependencies", [])}


def _wheel_module(data: dict[str, Any]) -> str:
    packages = data["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
    return Path(packages[0]).name


def _optional_modules(tree: ast.AST) -> set[str]:
    """Modules imported inside a ``try`` that catches ImportError: an extra, not a hard dependency."""
    optional: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        handled = {
            name.id
            for handler in node.handlers
            for name in ast.walk(handler.type)
            if handler.type is not None and isinstance(name, ast.Name)
        }
        if not handled & {"ImportError", "ModuleNotFoundError"}:
            continue
        optional.update(_top_level_imports(node))
    return optional


def _top_level_imports(tree: ast.AST) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module.split(".")[0])
    return modules


def _imported_distributions(source_dir: Path, own_module: str) -> set[str]:
    imported: set[str] = set()
    optional: set[str] = set()
    for path in sorted(source_dir.rglob("*.py")):
        tree = ast.parse(path.read_text())
        imported |= _top_level_imports(tree)
        optional |= _optional_modules(tree)
    third_party = imported - set(sys.stdlib_module_names) - optional - {own_module}
    return {MODULE_TO_DIST.get(module, _normalize(module)) for module in third_party}


@pytest.mark.parametrize("package_dir", PACKAGE_DIRS)
def test_declared_dependencies_match_imports(package_dir: str) -> None:
    data = _pyproject(package_dir)
    module = _wheel_module(data)
    declared = _declared_dependencies(data)
    imported = _imported_distributions(PACKAGES / package_dir / "src" / module, module)

    # Report both directions together: fixing one pyproject in two passes wastes a round trip.
    problems = [
        f"{label}: {names}"
        for label, names in (
            ("imports but does not declare", sorted(imported - declared)),
            ("declares but never imports", sorted(declared - imported)),
        )
        if names
    ]
    assert not problems, f"packages/{package_dir} " + "; ".join(problems)


@pytest.mark.parametrize("package_dir", [name for name in PACKAGE_DIRS if name.endswith("-cli")])
def test_provider_packages_expose_one_console_script(package_dir: str) -> None:
    data = _pyproject(package_dir)
    module = _wheel_module(data)
    scripts = data["project"].get("scripts", {})
    assert len(scripts) == 1, f"packages/{package_dir} should declare exactly one console script, got {sorted(scripts)}"
    binary, target = next(iter(scripts.items()))
    assert target == f"{module}.main:run", f"packages/{package_dir}: {binary} must point at {module}.main:run, got {target!r}"


def test_root_wheel_exposes_every_binary() -> None:
    """The root ``warcraft`` wheel is the distribution unit, so it must ship all 13 entry points."""
    root_scripts = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())["project"]["scripts"]
    package_scripts = {
        binary: target
        for package_dir in PACKAGE_DIRS
        if package_dir.endswith("-cli")
        for binary, target in _pyproject(package_dir)["project"].get("scripts", {}).items()
    }
    assert root_scripts == package_scripts, "Root pyproject [project.scripts] must match the per-package console scripts exactly."


def test_warcraftlogs_cli_does_not_depend_on_simc_cli() -> None:
    """Warcraft Logs delegates SimC work through the wrapper, never by importing the simc package."""
    assert "simc-cli" not in _declared_dependencies(_pyproject("warcraftlogs-cli"))

#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import site
import sys
import trace
from pathlib import Path

import pytest

os.environ.setdefault("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INCLUDE_DIRS = (
    REPO_ROOT / "packages" / "warcraft-core" / "src" / "warcraft_core",
    REPO_ROOT / "packages" / "warcraft-api" / "src" / "warcraft_api",
    REPO_ROOT / "packages" / "warcraft-content" / "src" / "warcraft_content",
)


def _site_packages_dirs() -> list[Path]:
    roots: list[Path] = []
    for raw_path in site.getsitepackages():
        roots.append(Path(raw_path))
    user_site = site.getusersitepackages()
    if user_site:
        roots.append(Path(user_site))
    return roots


def _ignored_dirs() -> list[str]:
    ignored = {
        Path(sys.prefix).resolve(),
        Path(sys.base_prefix).resolve(),
        (REPO_ROOT / ".venv").resolve(),
        (REPO_ROOT / "tests").resolve(),
        (REPO_ROOT / "scripts").resolve(),
    }
    ignored.update(path.resolve() for path in _site_packages_dirs())
    return [str(path) for path in sorted(ignored)]


def _included_python_files(include_dirs: tuple[Path, ...]) -> list[Path]:
    files: list[Path] = []
    for root in include_dirs:
        if not root.exists():
            continue
        files.extend(sorted(path for path in root.rglob("*.py") if path.is_file()))
    return files


def _executable_lines(path: Path) -> set[int]:
    return set(trace._find_executable_linenos(str(path)).keys())


def _covered_lines(results: trace.CoverageResults, path: Path) -> set[int]:
    covered: set[int] = set()
    resolved_path = str(path.resolve())
    for filename, line_no in results.counts:
        if str(Path(filename).resolve()) == resolved_path:
            covered.add(line_no)
    return covered


def _coverage_rows(include_dirs: tuple[Path, ...], results: trace.CoverageResults) -> list[tuple[str, int, int, float]]:
    rows: list[tuple[str, int, int, float]] = []
    for path in _included_python_files(include_dirs):
        executable = _executable_lines(path)
        if not executable:
            continue
        covered = _covered_lines(results, path) & executable
        percent = (len(covered) / len(executable)) * 100
        rows.append((str(path.relative_to(REPO_ROOT)), len(executable), len(covered), percent))
    return rows


def _print_summary(rows: list[tuple[str, int, int, float]]) -> None:
    total_lines = sum(line_count for _, line_count, _, _ in rows)
    total_hits = sum(hit_count for _, _, hit_count, _ in rows)
    overall_percent = (total_hits / total_lines) * 100 if total_lines else 0.0

    print("Coverage fallback: stdlib trace")
    print(f"Overall: {total_hits}/{total_lines} lines -> {overall_percent:.1f}%")
    print("lines   cov%   path")
    for path, line_count, _hit_count, percent in rows:
        print(f"{line_count:5d}   {percent:5.1f}%   {path}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run pytest under stdlib trace and summarize line coverage.",
        allow_abbrev=False,
    )
    parser.add_argument(
        "--include",
        action="append",
        default=[],
        help="Additional repo-relative directory to include in the coverage summary. Repeat as needed.",
    )
    args, pytest_args = parser.parse_known_args()

    include_dirs = DEFAULT_INCLUDE_DIRS + tuple((REPO_ROOT / item).resolve() for item in args.include)
    pytest_args = pytest_args or ["-q"]

    tracer = trace.Trace(count=1, trace=0, ignoredirs=_ignored_dirs())
    exit_code = tracer.runfunc(pytest.main, pytest_args)
    results = tracer.results()
    rows = _coverage_rows(include_dirs, results)
    _print_summary(rows)
    return int(exit_code)


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Bump the workspace version across every pyproject.toml in the repo.

Usage:
    python scripts/bump_version.py X.Y.Z

The workspace ships under a single version. This script enforces that
invariant: it errors if any pyproject.toml currently disagrees, and rewrites
all of them to the new version in one pass. It does not stage or commit.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
VERSION_LINE = re.compile(r'^(version\s*=\s*")([^"]+)(")', re.MULTILINE)


def find_pyprojects() -> list[Path]:
    files = [REPO_ROOT / "pyproject.toml"]
    files.extend(sorted((REPO_ROOT / "packages").glob("*/pyproject.toml")))
    return files


def read_version(path: Path) -> str:
    match = VERSION_LINE.search(path.read_text())
    if not match:
        raise SystemExit(f"error: no top-level version line in {path.relative_to(REPO_ROOT)}")
    return match.group(2)


def write_version(path: Path, new_version: str) -> None:
    text = path.read_text()
    new_text, count = VERSION_LINE.subn(rf'\g<1>{new_version}\g<3>', text, count=1)
    if count != 1:
        raise SystemExit(f"error: failed to rewrite version in {path.relative_to(REPO_ROOT)}")
    path.write_text(new_text)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: bump_version.py X.Y.Z", file=sys.stderr)
        return 2
    new_version = argv[1]
    if not SEMVER.match(new_version):
        print(f"error: {new_version!r} is not semver (expected X.Y.Z)", file=sys.stderr)
        return 2

    files = find_pyprojects()
    current = {path: read_version(path) for path in files}
    distinct = set(current.values())
    if len(distinct) != 1:
        print("error: workspace versions are out of sync:", file=sys.stderr)
        for path, version in current.items():
            print(f"  {version}  {path.relative_to(REPO_ROOT)}", file=sys.stderr)
        return 1
    [old_version] = distinct
    if old_version == new_version:
        print(f"workspace already at {new_version}; nothing to do")
        return 0

    for path in files:
        write_version(path, new_version)
    print(f"bumped {len(files)} files: {old_version} -> {new_version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

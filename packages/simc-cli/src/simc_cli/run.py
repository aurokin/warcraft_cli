from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from simc_cli.repo import RepoPaths

VERSION_RE = re.compile(r"(SimulationCraft[^\r\n]+)")


@dataclass(slots=True)
class CommandResult:
    command: list[str]
    cwd: Path | None
    returncode: int
    stdout: str
    stderr: str


@dataclass(slots=True)
class BinaryVersion:
    binary_path: Path
    available: bool
    version_line: str | None
    returncode: int | None


def _run(command: list[str], *, cwd: Path | None = None) -> CommandResult:
    # Fixed argv built from repo paths and CLI flags; never a shell string.
    proc = subprocess.run(command, cwd=str(cwd) if cwd else None, capture_output=True, text=True, check=False)  # noqa: S603
    return CommandResult(command=command, cwd=cwd, returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)


def _parse_version_line(text: str) -> str | None:
    match = VERSION_RE.search(text)
    if not match:
        return None
    return match.group(1).strip()


def binary_version(paths: RepoPaths) -> BinaryVersion:
    if not paths.build_simc.exists():
        return BinaryVersion(binary_path=paths.build_simc, available=False, version_line=None, returncode=None)
    result = _run([str(paths.build_simc)])
    return BinaryVersion(
        binary_path=paths.build_simc,
        available=True,
        version_line=_parse_version_line(result.stdout + result.stderr),
        returncode=result.returncode,
    )


def repo_git_status(paths: RepoPaths) -> dict[str, object]:
    if not (paths.root / ".git").exists():
        return {"git": False, "dirty": False, "branch": None, "head": None, "dirty_entries": []}
    branch = _run(["git", "-C", str(paths.root), "rev-parse", "--abbrev-ref", "HEAD"])
    head = _run(["git", "-C", str(paths.root), "rev-parse", "HEAD"])
    status = _run(["git", "-C", str(paths.root), "status", "--short"])
    dirty_entries = [line for line in status.stdout.splitlines() if line.strip()]
    return {
        "git": True,
        "dirty": bool(dirty_entries),
        "branch": branch.stdout.strip() or None,
        "head": head.stdout.strip() or None,
        "dirty_entries": dirty_entries,
    }


def sync_repo(paths: RepoPaths, *, allow_dirty: bool) -> CommandResult | None:
    status = repo_git_status(paths)
    if status.get("dirty") and not allow_dirty:
        return None
    return _run(["git", "-C", str(paths.root), "pull", "--ff-only"], cwd=paths.root)


def build_repo(paths: RepoPaths, *, target: str | None) -> CommandResult:
    command = ["cmake", "--build", str(paths.build_dir)]
    if target:
        command.extend(["--target", target])
    return _run(command, cwd=paths.root)


def run_profile(paths: RepoPaths, profile_path: str | Path, *, simc_args: list[str]) -> CommandResult:
    resolved = Path(profile_path).expanduser().resolve()
    command = [str(paths.build_simc), str(resolved), *simc_args]
    return _run(command, cwd=paths.root)

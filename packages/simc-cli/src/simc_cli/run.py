from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from simc_cli.repo import RepoPaths

VERSION_RE = re.compile(r"(SimulationCraft[^\r\n]+)")
GIT_BUILD_RE = re.compile(r"git build (?P<branch>\S+) (?P<revision>[0-9a-f]{7,40})")
# The cheapest invocation that makes SimC print its full build banner: the bare binary and a profile
# it cannot open both print a short banner that omits the git revision.
_BANNER_PROBE_ARG = "spell_query=spell.id=133"


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
    git_branch: str | None = None
    git_revision: str | None = None


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
    try:
        result = _run([str(paths.build_simc), _BANNER_PROBE_ARG])
    except OSError:
        # A file that exists but cannot be executed (interrupted build, missing +x) is as unusable as
        # a missing one, and callers on an error path must not crash while describing the failure.
        return BinaryVersion(binary_path=paths.build_simc, available=False, version_line=None, returncode=None)
    text = result.stdout + result.stderr
    git = GIT_BUILD_RE.search(text)
    return BinaryVersion(
        binary_path=paths.build_simc,
        available=True,
        version_line=_parse_version_line(text),
        returncode=result.returncode,
        git_branch=git.group("branch") if git else None,
        git_revision=git.group("revision") if git else None,
    )


def binary_matches_checkout(version: BinaryVersion, git_status: dict[str, object]) -> bool | None:
    """Whether the binary was built from the checkout's current HEAD.

    ``None`` when either side is unknown. A stale binary decodes talent hashes against older trait
    data, which is the usual cause of a checkout's own stock profiles being rejected.
    """
    head = git_status.get("head")
    if not version.git_revision or not isinstance(head, str) or not head:
        return None
    return head.startswith(version.git_revision)


@dataclass(frozen=True, slots=True)
class BinaryProvenance:
    """Which revision the binary was built from, next to the revision of the checkout it reads."""

    git_revision: str | None
    checkout_head: str | None
    matches_checkout: bool | None

    @property
    def stale_hint(self) -> str | None:
        """The sentence a decode failure should carry when the binary predates its own checkout."""
        if self.matches_checkout is not False:
            return None
        return (
            f"The SimC binary was built from {self.git_revision} but the checkout is at {self.checkout_head}; "
            "a binary older than its checkout rejects talent hashes built against newer trait data. "
            "Rebuild it with 'simc build' and confirm with 'simc doctor'."
        )


def binary_provenance(paths: RepoPaths) -> BinaryProvenance:
    version = binary_version(paths)
    git = repo_git_status(paths)
    head = git.get("head")
    return BinaryProvenance(
        git_revision=version.git_revision,
        checkout_head=head if isinstance(head, str) else None,
        matches_checkout=binary_matches_checkout(version, git),
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
    """Configure, then build. SimC bakes its git revision in at configure time, so building alone
    after a ``sync`` leaves a binary that ``doctor`` reports as built from the old commit."""
    configure = _run(["cmake", "-S", str(paths.root), "-B", str(paths.build_dir)], cwd=paths.root)
    if configure.returncode != 0:
        return configure
    command = ["cmake", "--build", str(paths.build_dir)]
    if target:
        command.extend(["--target", target])
    return _run(command, cwd=paths.root)


def run_profile(paths: RepoPaths, profile_path: str | Path, *, simc_args: list[str]) -> CommandResult:
    resolved = Path(profile_path).expanduser().resolve()
    command = [str(paths.build_simc), str(resolved), *simc_args]
    return _run(command, cwd=paths.root)

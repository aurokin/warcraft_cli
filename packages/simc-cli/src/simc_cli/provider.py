"""Pure SimC provider surface.

Functions here never print and never raise ``typer.Exit``: they return an ``Envelope``. ``simc_cli.main``
wraps them for the CLI and the ``warcraft`` wrapper can call ``PROVIDER`` in-process.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from warcraft_core.envelope import ENVELOPE_KEYS, Envelope, success_envelope
from warcraft_core.exit_codes import EXIT_USAGE
from warcraft_core.provider import ProviderError, ProviderSurface

from simc_cli.repo import RepoPaths, discover_repo, resolve_repo_root, validate_build, validate_repo
from simc_cli.run import binary_matches_checkout, binary_version, repo_git_status
from simc_cli.search import ripgrep_available

PROVIDER_NAME = "simc"

# Every command name the CLI exposes, with the state agents should expect from it.
CAPABILITIES: dict[str, str] = {
    "search": "not_supported",
    "resolve": "not_supported",
    "doctor": "ready",
    "repo": "ready",
    "checkout": "ready",
    "version": "ready",
    "sync": "ready",
    "build": "ready",
    "run": "ready",
    "sim": "ready",
    "inspect": "ready",
    "spec_files": "ready",
    "identify_build": "ready",
    "decode_build": "ready",
    "apply_build": "ready",
    "validate_talent_transport": "ready",
    "apl_lists": "ready",
    "apl_graph": "ready",
    "apl_talents": "ready",
    "find_action": "ready",
    "trace_action": "ready",
    "apl_prune": "ready",
    "apl_branch_trace": "ready",
    "apl_intent": "ready",
    "apl_intent_explain": "ready",
    "priority": "ready",
    "describe_build": "ready",
    "inactive_actions": "ready",
    "opener": "ready",
    "build_harness": "ready",
    "validate_apl": "ready",
    "compare_apls": "ready",
    "variant_report": "ready",
    "verify_clean": "ready",
    "apl_branch_compare": "ready",
    "analysis_packet": "ready",
    "first_cast": "ready",
    "log_actions": "ready",
    "compare_builds": "ready",
    "modify_build": "ready",
}

# Commands whose content search falls back to ripgrep when a file-name match comes up empty.
RIPGREP_COMMANDS = frozenset({"spec_files", "find_action", "trace_action"})

# Commands that cannot answer without executing the built SimC binary: they decode a talent hash or
# run a simulation. Without a usable binary they fail, so doctor must not advertise them as ready.
BINARY_COMMANDS = frozenset({
    "version",
    "run",
    "sim",
    "identify_build",
    "decode_build",
    "apply_build",
    "describe_build",
    "modify_build",
    "compare_builds",
    "validate_talent_transport",
    "validate_apl",
    "compare_apls",
    "first_cast",
    "priority",
    "inactive_actions",
    "opener",
    "analysis_packet",
    "apl_branch_compare",
    "apl_prune",
    "apl_branch_trace",
    "apl_intent",
    "apl_intent_explain",
})




def simc_envelope(command: str, payload: Mapping[str, Any]) -> Envelope:
    """Wrap a flat simc payload in the shared envelope; every non-envelope key goes under ``data``."""
    data = {key: value for key, value in payload.items() if key not in ENVELOPE_KEYS}
    executed = payload.get("command")
    if isinstance(executed, list):
        # The envelope's top-level ``command`` is the subcommand name; the executed SimC/git argv
        # stays at ``data.command`` so a run remains reproducible.
        data["command"] = executed
    kind = payload.get("kind")
    return success_envelope(
        provider=PROVIDER_NAME,
        command=command,
        kind=kind if isinstance(kind, str) else command.replace("-", "_"),
        data=data,
        query=payload.get("query"),
    )


def repo_payload(paths: RepoPaths) -> dict[str, Any]:
    """Describe the local SimulationCraft checkout: readiness, git state, and binary version."""
    repo_issues = validate_repo(paths)
    build_issues = validate_build(paths)
    version = binary_version(paths)
    git = repo_git_status(paths)
    matches_checkout = binary_matches_checkout(version, git)
    if matches_checkout is False:
        build_issues = [
            *build_issues,
            f"simc binary was built from {version.git_revision}, checkout HEAD is {git.get('head')}; rebuild with 'simc build'",
        ]
    return {
        "root": str(paths.root),
        "exists": paths.root.exists(),
        "repo_ready": not repo_issues,
        "build_ready": not build_issues,
        "repo_issues": repo_issues,
        "build_issues": build_issues,
        "git": git,
        "binary": {
            "path": str(paths.build_simc),
            "exists": paths.build_simc.exists(),
            "version_line": version.version_line,
            "available": version.available,
            "git_branch": version.git_branch,
            "git_revision": version.git_revision,
            "matches_checkout": matches_checkout,
        },
    }


def search(query: str, *, limit: int = 10, repo_root: str | Path | None = None, **options: Any) -> Envelope:
    """Reject free-text discovery; the local catalog is exposed by explicit inspection commands."""
    raise ProviderError(
        "unsupported_operation", "SimC does not support free-text discovery. Use spec-files <class> or inspect.",
        exit_code=EXIT_USAGE,
        details={"operation": "search", "available_commands": ["spec-files", "inspect", "identify-build"]},
    )


def resolve(target: str, *, repo_root: str | Path | None = None, **options: Any) -> Envelope:
    """Reject free-text resolution without suggesting an unrelated class."""
    raise ProviderError(
        "unsupported_operation", "SimC does not support free-text resolution. Use identify-build for an explicit build.",
        exit_code=EXIT_USAGE,
        details={"operation": "resolve", "available_commands": ["identify-build", "spec-files", "inspect"]},
    )


def doctor(*, repo_root: str | Path | None = None, **options: Any) -> Envelope:
    """Report repo/binary readiness, external dependencies, and the per-command capability map."""
    del options
    paths = discover_repo(repo_root)
    resolution = resolve_repo_root(repo_root)
    repo = repo_payload(paths)
    has_ripgrep = ripgrep_available()
    build_ready = bool(repo["build_ready"])
    unavailable = (set() if has_ripgrep else set(RIPGREP_COMMANDS)) | (set() if build_ready else set(BINARY_COMMANDS))
    capabilities = {name: "unavailable" if name in unavailable else state for name, state in CAPABILITIES.items()}
    payload = {
        "status": "ready" if repo["repo_ready"] and build_ready and has_ripgrep else "degraded",
        "command": "doctor",
        "installed": True,
        "language": "python",
        "auth": {"required": False, "deferred": False},
        "dependencies": {
            "ripgrep": {"required_by": sorted(RIPGREP_COMMANDS), "available": has_ripgrep},
            "simc_binary": {"required_by": sorted(BINARY_COMMANDS), "available": build_ready, "issues": repo["build_issues"]},
        },
        "capabilities": capabilities,
        "repo_resolution": {
            "source": resolution.source,
            "config_path": str(resolution.config_path),
            "configured_root": str(resolution.configured_root) if resolution.configured_root else None,
            "managed_root": str(resolution.managed_root),
            "managed_exists": resolution.managed_exists,
        },
        "repo": repo,
    }
    return simc_envelope("doctor", payload)


class _SimcProvider:
    """In-process surface the ``warcraft`` wrapper calls; the Typer commands wrap the same functions."""

    name = PROVIDER_NAME

    def search(self, query: str, *, limit: int = 10, **options: Any) -> Envelope:
        return search(query, limit=limit, **options)

    def resolve(self, target: str, **options: Any) -> Envelope:
        return resolve(target, **options)

    def doctor(self, **options: Any) -> Envelope:
        return doctor(**options)


PROVIDER: ProviderSurface = _SimcProvider()

"""Pure SimC provider surface.

Functions here never print and never raise ``typer.Exit``: they return an ``Envelope``. ``simc_cli.main``
wraps them for the CLI and the ``warcraft`` wrapper can call ``PROVIDER`` in-process.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from warcraft_core.envelope import ENVELOPE_KEYS, Envelope, success_envelope, with_legacy_keys
from warcraft_core.provider import ProviderSurface

from simc_cli.repo import RepoPaths, discover_repo, resolve_repo_root, validate_build, validate_repo
from simc_cli.run import binary_version, repo_git_status

PROVIDER_NAME = "simc"

# Every command name the CLI exposes, with the state agents should expect from it.
CAPABILITIES: dict[str, str] = {
    "search": "coming_soon",
    "resolve": "coming_soon",
    "doctor": "ready",
    "repo": "ready",
    "checkout": "ready",
    "version": "ready",
    "sync": "ready",
    "build": "ready",
    "run": "ready",
    "inspect": "ready",
    "spec_files": "ready",
    "identify_build": "ready",
    "decode_build": "ready",
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

COMING_SOON_MESSAGE = (
    "Free-text discovery is not implemented yet for simc phase 1. "
    "Use direct repo, spec-files, decode-build, or run commands."
)


def simc_envelope(command: str, payload: Mapping[str, Any]) -> Envelope:
    """Wrap a flat simc payload in the shared envelope, keeping the deprecated flat keys at the top level."""
    legacy = {key: value for key, value in payload.items() if key not in ENVELOPE_KEYS}
    data = dict(legacy)
    executed = payload.get("command")
    if isinstance(executed, list):
        # The envelope's top-level ``command`` is the subcommand name; the executed SimC/git argv
        # stays at ``data.command`` so a run remains reproducible.
        data["command"] = executed
    kind = payload.get("kind")
    envelope = success_envelope(
        provider=PROVIDER_NAME,
        command=command,
        kind=kind if isinstance(kind, str) else command.replace("-", "_"),
        data=data,
        query=payload.get("query"),
    )
    return cast(Envelope, with_legacy_keys(envelope, legacy))


def repo_payload(paths: RepoPaths) -> dict[str, Any]:
    """Describe the local SimulationCraft checkout: readiness, git state, and binary version."""
    repo_issues = validate_repo(paths)
    build_issues = validate_build(paths)
    version = binary_version(paths)
    return {
        "root": str(paths.root),
        "exists": paths.root.exists(),
        "repo_ready": not repo_issues,
        "build_ready": not build_issues,
        "repo_issues": repo_issues,
        "build_issues": build_issues,
        "git": repo_git_status(paths),
        "binary": {
            "path": str(paths.build_simc),
            "exists": paths.build_simc.exists(),
            "version_line": version.version_line,
            "available": version.available,
        },
    }


def coming_soon_payload(*, query: str, suggested_command: str) -> dict[str, Any]:
    """Structured stub for the discovery surfaces simc does not implement yet."""
    return {
        "provider": PROVIDER_NAME,
        "query": query,
        "search_query": query,
        "count": 0,
        "results": [],
        "candidates": [],
        "resolved": False,
        "confidence": "none",
        "match": None,
        "next_command": None,
        "fallback_search_command": None,
        "coming_soon": True,
        "message": COMING_SOON_MESSAGE,
        "suggested_command": suggested_command,
    }


def search(query: str, *, limit: int = 10, repo_root: str | Path | None = None, **options: Any) -> Envelope:
    """Free-text search is deferred; return the coming-soon stub instead of guessing."""
    del limit, repo_root, options
    return simc_envelope("search", coming_soon_payload(query=query, suggested_command="simc spec-files monk"))


def resolve(target: str, *, repo_root: str | Path | None = None, **options: Any) -> Envelope:
    """Free-text resolution is deferred; point at the direct decode path for the discovered repo."""
    del options
    example_apl = discover_repo(repo_root).apl_default / "monk_mistweaver.simc"
    payload = coming_soon_payload(query=target, suggested_command=f"simc decode-build --apl-path {example_apl}")
    return simc_envelope("resolve", payload)


def doctor(*, repo_root: str | Path | None = None, **options: Any) -> Envelope:
    """Report repo/binary readiness plus the per-command capability map."""
    del options
    paths = discover_repo(repo_root)
    resolution = resolve_repo_root(repo_root)
    repo = repo_payload(paths)
    payload = {
        "provider": PROVIDER_NAME,
        "status": "ready" if repo["repo_ready"] and repo["build_ready"] else "degraded",
        "command": "doctor",
        "installed": True,
        "language": "python",
        "auth": {"required": False, "deferred": False},
        "capabilities": dict(CAPABILITIES),
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

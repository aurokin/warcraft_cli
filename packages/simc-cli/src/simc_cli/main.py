from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import typer
from warcraft_core.identity import build_identity_payload, refresh_talent_transport_packet, validate_talent_transport_packet
from warcraft_core.output import emit

from simc_cli.apl import action_counts, group_entries, mermaid_graph, parse_apl, talent_refs, trace_action_entries
from simc_cli.branch import (
    active_priority_decisions,
    attach_focus_comparison,
    compare_branch_summaries,
    explain_intent,
    format_list_decision,
    inactive_priority_decisions,
    resolve_focus_list,
    summarize_branches,
    summarize_intent,
    trace_apl,
)
from simc_cli.build_input import (
    BuildResolution,
    BuildSpec,
    TreeDiff,
    build_profile_text,
    decode_build,
    diff_talent_trees,
    encode_build,
    extract_build_spec_from_text,
    identify_build,
    infer_actor_and_spec_from_apl,
    load_build_spec,
    tree_entries_string,
)
from simc_cli.compare import (
    build_variant_profile,
    compare_apl_variants,
    validate_profile_file,
    variant_report_payload,
    verify_clean_payload,
    write_harness,
)
from simc_cli.packet import build_analysis_packet
from simc_cli.prune import PruneContext, prune_entries, split_csv_values
from simc_cli.repo import (
    RepoPaths,
    checkout_managed_repo,
    clear_configured_repo_root,
    discover_repo,
    resolve_repo_root,
    save_configured_repo_root,
    validate_build,
    validate_repo,
)
from simc_cli.report import load_sim_report, sim_report_payload, summarize_sim_report
from simc_cli.run import binary_version, build_repo, repo_git_status, run_profile, sync_repo
from simc_cli.search import find_action, spec_file_search
from simc_cli.sim import first_action_hits, run_first_casts, summarize_first_casts
from simc_cli.talent_transport import validate_talent_tree_transport

app = typer.Typer(add_completion=False, help="SimulationCraft local workflow CLI.")


@dataclass(slots=True)
class RuntimeConfig:
    pretty: bool = False
    repo_root: str | None = None


def _is_transport_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _cfg(ctx: typer.Context) -> RuntimeConfig:
    obj = ctx.obj
    if isinstance(obj, RuntimeConfig):
        return obj
    return RuntimeConfig()


def _emit(ctx: typer.Context, payload: dict[str, Any], *, err: bool = False) -> None:
    emit(payload, pretty=_cfg(ctx).pretty, err=err)


def _fail(ctx: typer.Context, code: str, message: str, *, status: int = 1, extra: dict[str, Any] | None = None) -> None:
    payload: dict[str, Any] = {"ok": False, "error": {"code": code, "message": message}}
    if extra:
        payload.update(extra)
    _emit(ctx, payload, err=True)
    raise typer.Exit(status)


def _write_packet_json_or_fail(ctx: typer.Context, *, out: str | None, packet: dict[str, Any]) -> str | None:
    if not out:
        return None
    try:
        output_path = Path(out).expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(packet, indent=2) + "\n")
        return str(output_path)
    except OSError as exc:
        _fail(ctx, "transport_packet_write_failed", f"Failed to write talent transport packet: {exc}")
        raise AssertionError("unreachable") from None


def _repo_paths(ctx: typer.Context) -> RepoPaths:
    return discover_repo(_cfg(ctx).repo_root)


def _repo_resolution(ctx: typer.Context):
    return resolve_repo_root(_cfg(ctx).repo_root)


def _preview_text(text: str, *, max_lines: int = 20) -> tuple[list[str], bool]:
    lines = text.splitlines()
    return lines[:max_lines], len(lines) > max_lines


def _repo_payload(paths: RepoPaths) -> dict[str, Any]:
    repo_issues = validate_repo(paths)
    build_issues = validate_build(paths)
    git_status = repo_git_status(paths)
    version = binary_version(paths)
    return {
        "root": str(paths.root),
        "exists": paths.root.exists(),
        "repo_ready": not repo_issues,
        "build_ready": not build_issues,
        "repo_issues": repo_issues,
        "build_issues": build_issues,
        "git": git_status,
        "binary": {
            "path": str(paths.build_simc),
            "exists": paths.build_simc.exists(),
            "version_line": version.version_line,
            "available": version.available,
        },
    }


def _coming_soon_payload(*, query: str, suggested_command: str) -> dict[str, Any]:
    return {
        "provider": "simc",
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
        "message": (
            "Free-text discovery is not implemented yet for simc phase 1. "
            "Use direct repo, spec-files, decode-build, or run commands."
        ),
        "suggested_command": suggested_command,
    }


def _serialize_build_spec(spec: Any) -> dict[str, Any]:
    payload = {
        "actor_class": spec.actor_class,
        "spec": spec.spec,
        "talents": spec.talents,
        "class_talents": spec.class_talents,
        "spec_talents": spec.spec_talents,
        "hero_talents": spec.hero_talents,
        "source_kind": getattr(spec, "source_kind", None),
        "source_notes": spec.source_notes,
    }
    transport_source = getattr(spec, "transport_source", None)
    transport_form = getattr(spec, "transport_form", None)
    transport_status = getattr(spec, "transport_status", None)
    if transport_source or transport_form or transport_status:
        payload["transport_packet"] = {
            "path": transport_source,
            "transport_form": transport_form,
            "transport_status": transport_status,
        }
    return payload


def _serialize_build_identity(identity: Any) -> dict[str, Any]:
    return {
        "actor_class": identity.actor_class,
        "spec": identity.spec,
        "confidence": identity.confidence,
        "source": identity.source,
        "candidate_count": identity.candidate_count,
        "candidates": [{"actor_class": actor_class, "spec": spec} for actor_class, spec in identity.candidates],
        "source_notes": identity.source_notes,
        "identity_contract": build_identity_payload(
            actor_class=identity.actor_class,
            spec=identity.spec,
            confidence=identity.confidence,
            source=identity.source,
            candidates=list(identity.candidates),
            source_notes=identity.source_notes,
        ),
    }


def _resolve_path(paths: RepoPaths, value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = paths.root / path
    return path.resolve()


def _relative_to_repo(paths: RepoPaths, path: Path) -> str | None:
    return str(path.relative_to(paths.root)) if path.is_relative_to(paths.root) else None


def _load_transport_packet(path: str) -> tuple[dict[str, Any], str]:
    resolved = Path(path).expanduser().resolve()
    raw = json.loads(resolved.read_text())
    packet = validate_talent_transport_packet(raw)
    return packet, str(resolved)


def _packet_identity_value(packet: dict[str, Any], key: str) -> str | None:
    build_identity = packet.get("build_identity")
    if not isinstance(build_identity, dict):
        return None
    class_spec_identity = build_identity.get("class_spec_identity")
    if not isinstance(class_spec_identity, dict):
        return None
    identity = class_spec_identity.get("identity")
    if not isinstance(identity, dict):
        return None
    value = identity.get(key)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _packet_talent_tree_rows(packet: dict[str, Any]) -> list[dict[str, Any]]:
    raw_evidence = packet.get("raw_evidence")
    if not isinstance(raw_evidence, dict):
        return []
    rows = raw_evidence.get("talent_tree_entries")
    if not isinstance(rows, list) or not rows:
        return []
    normalized: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            return []
        entry = row.get("entry")
        node_id = row.get("node_id")
        rank = row.get("rank")
        normalized_row = {
            "entry": entry if _is_transport_int(entry) else None,
            "node_id": node_id if _is_transport_int(node_id) else None,
            "rank": rank if _is_transport_int(rank) else None,
        }
        if all(isinstance(normalized_row.get(key), int) for key in ("entry", "node_id", "rank")):
            normalized.append(normalized_row)
        else:
            return []
    return normalized


def _parse_talent_row(value: str) -> dict[str, int]:
    parts = [part.strip() for part in value.split(":", 2)]
    if len(parts) != 3:
        raise ValueError(f"Invalid talent row '{value}'. Expected entry_id:node_id:rank.")
    entry_text, node_text, rank_text = parts
    if not entry_text.isdigit() or not node_text.isdigit() or not rank_text.isdigit():
        raise ValueError(f"Invalid talent row '{value}'. Expected entry_id:node_id:rank.")
    return {
        "entry": int(entry_text),
        "node_id": int(node_text),
        "rank": int(rank_text),
    }


def _infer_default_apl_path(paths: RepoPaths, *, actor_class: str | None, spec: str | None) -> Path | None:
    if not actor_class or not spec:
        return None
    file_name = f"{actor_class}_{spec}.simc"
    for base in (paths.apl_default, paths.apl_assisted):
        candidate = (base / file_name).resolve()
        if candidate.exists():
            return candidate
    return None


def _write_temp_profile(*, source_name: str, text: str) -> Path:
    fd, raw_path = tempfile.mkstemp(suffix=".simc", prefix=f"{source_name}-")
    path = Path(raw_path).resolve()
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
        if not text.endswith("\n"):
            handle.write("\n")
    return path


def _sim_preset_settings(*, preset: str) -> tuple[int, int]:
    if preset == "high-accuracy":
        return (5000, 300)
    return (1000, 300)


def _build_option_values(
    *,
    profile_path: str | None,
    build_file: str | None,
    build_text: str | None,
    talents: str | None,
    class_talents: str | None,
    spec_talents: str | None,
    hero_talents: str | None,
    actor_class: str | None,
    spec_name: str | None,
    enable: list[str],
    disable: list[str],
    build_packet: str | None = None,
) -> dict[str, Any]:
    return {
        "profile_path": profile_path,
        "build_file": build_file,
        "build_packet": build_packet,
        "build_text": build_text,
        "talents": talents,
        "class_talents": class_talents,
        "spec_talents": spec_talents,
        "hero_talents": hero_talents,
        "actor_class": actor_class,
        "spec_name": spec_name,
        "enable": enable,
        "disable": disable,
    }


def _resolve_prune_context(paths: RepoPaths, apl_path: Path, option_values: dict[str, Any], targets: int) -> tuple[PruneContext, Any]:
    unresolved_spec = load_build_spec(
        apl_path=apl_path,
        profile_path=option_values["profile_path"],
        build_file=option_values["build_file"],
        build_packet=option_values["build_packet"],
        build_text=option_values["build_text"],
        talents=option_values["talents"],
        class_talents=option_values["class_talents"],
        spec_talents=option_values["spec_talents"],
        hero_talents=option_values["hero_talents"],
        actor_class=option_values["actor_class"],
        spec_name=option_values["spec_name"],
    )
    build_spec, _identity = identify_build(paths, unresolved_spec)
    resolution = decode_build(paths, build_spec)
    enabled = set(resolution.enabled_talents)
    enabled.update(split_csv_values(option_values["enable"]))
    disabled = split_csv_values(option_values["disable"])
    talent_sources = {
        talent.token: talent.tree
        for tree in ("class", "spec", "hero")
        for talent in resolution.talents_by_tree.get(tree, [])
    }
    for token in split_csv_values(option_values["enable"]):
        talent_sources[token] = "manual"
    context = PruneContext(
        enabled_talents=enabled,
        disabled_talents=disabled,
        targets=targets,
        talent_sources=talent_sources,
    )
    return context, resolution


def _load_identified_build_spec(
    paths: RepoPaths,
    *,
    apl_path: str | Path | None,
    profile_path: str | None,
    build_file: str | None,
    build_text: str | None,
    talents: str | None,
    class_talents: str | None,
    spec_talents: str | None,
    hero_talents: str | None,
    actor_class: str | None,
    spec_name: str | None,
    build_packet: str | None = None,
) -> tuple[Any, Any]:
    unresolved_spec = load_build_spec(
        apl_path=apl_path,
        profile_path=profile_path,
        build_file=build_file,
        build_packet=build_packet,
        build_text=build_text,
        talents=talents,
        class_talents=class_talents,
        spec_talents=spec_talents,
        hero_talents=hero_talents,
        actor_class=actor_class,
        spec_name=spec_name,
    )
    return identify_build(paths, unresolved_spec)


def _load_identified_build_spec_or_fail(
    ctx: typer.Context,
    paths: RepoPaths,
    *,
    apl_path: str | Path | None,
    profile_path: str | None,
    build_file: str | None,
    build_text: str | None,
    talents: str | None,
    class_talents: str | None,
    spec_talents: str | None,
    hero_talents: str | None,
    actor_class: str | None,
    spec_name: str | None,
    build_packet: str | None = None,
) -> tuple[Any, Any]:
    try:
        return _load_identified_build_spec(
            paths,
            apl_path=apl_path,
            profile_path=profile_path,
            build_file=build_file,
            build_packet=build_packet,
            build_text=build_text,
            talents=talents,
            class_talents=class_talents,
            spec_talents=spec_talents,
            hero_talents=hero_talents,
            actor_class=actor_class,
            spec_name=spec_name,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        if build_packet:
            _fail(ctx, "invalid_build_packet", str(exc))
            raise AssertionError("unreachable") from None
        _fail(ctx, "invalid_query", str(exc))
        raise AssertionError("unreachable") from None


def _prune_context_payload(resolution: Any, context: PruneContext) -> dict[str, Any]:
    return {
        "actor_class": resolution.actor_class,
        "spec": resolution.spec,
        "source_kind": getattr(resolution, "source_kind", None),
        "targets": context.targets,
        "enabled_talent_count": len(context.enabled_talents),
        "enabled_talents": sorted(context.enabled_talents),
        "source_notes": resolution.source_notes,
    }


def _talent_tree_payload(resolution: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for tree in ("class", "spec", "hero"):
        talents = resolution.talents_by_tree.get(tree, [])
        payload[tree] = {
            "selected": [
                {"name": talent.name, "token": talent.token, "rank": talent.rank, "max_rank": talent.max_rank}
                for talent in talents
                if talent.rank > 0
            ],
            "skipped": [
                {"name": talent.name, "token": talent.token, "rank": talent.rank, "max_rank": talent.max_rank}
                for talent in talents
                if talent.rank <= 0
            ],
        }
    return payload


def _priority_item(decision: Any) -> dict[str, Any]:
    return {
        "line_no": decision.line_no,
        "action": decision.action_name,
        "target_list": decision.target_list,
        "status": decision.status,
        "reason": decision.reason,
        "text": format_list_decision(decision),
    }


def _focus_list_summary(resolved: Path, context: PruneContext, *, start_list: str) -> tuple[Any, Any]:
    summary = summarize_branches(resolved, context, start_list=start_list)
    return summary, resolve_focus_list(resolved, context, start_list=start_list)


def _describe_target_payload(resolved: Path, context: PruneContext, *, start_list: str,
                             priority_limit: int, inactive_limit: int) -> dict[str, Any]:
    summary, focus = _focus_list_summary(resolved, context, start_list=start_list)
    active_all = active_priority_decisions(resolved, context, focus.focus_list)
    inactive_all = inactive_priority_decisions(resolved, context, focus.focus_list, talent_only=True)
    active = active_all[:priority_limit]
    inactive = inactive_all[:inactive_limit]
    explanation = explain_intent(resolved, context, focus.focus_list, limit=priority_limit)
    runtime_sensitive = [
        _priority_item(decision)
        for decision in active
        if decision.status == "possible" and decision.reason == "depends on runtime-only state"
    ]
    return {
        "targets": context.targets,
        "focus_list": focus.focus_list,
        "focus_path": focus.path,
        "focus_resolution": focus.reason,
        "dispatch_certainty": "guaranteed" if summary.guaranteed_dispatch else "unresolved",
        "branch_summary": {
            "start_list": summary.start_list,
            "guaranteed_dispatch": summary.guaranteed_dispatch,
            "guaranteed_dispatch_line": summary.guaranteed_dispatch_line,
            "guaranteed_dispatch_reason": summary.guaranteed_dispatch_reason,
            "dead_branches": summary.dead_branches,
            "unresolved_branches": summary.unresolved_branches,
            "shadowed_lines": summary.shadowed_lines,
        },
        "active_priority": [_priority_item(decision) for decision in active],
        "active_action_names": _action_names([_priority_item(decision) for decision in active_all]),
        "inactive_talent_branches": [_priority_item(decision) for decision in inactive],
        "explained_intent": {
            "setup": explanation.setup,
            "helpers": explanation.helpers,
            "burst": explanation.burst,
            "priorities": explanation.priorities,
        },
        "runtime_sensitive": runtime_sensitive,
    }


def _action_names(items: list[dict[str, Any]]) -> list[str]:
    return [str(item["action"]) for item in items if item.get("action")]


def _parse_variant_specs(values: list[str]) -> list[tuple[str, str]]:
    specs: list[tuple[str, str]] = []
    for value in values:
        label, sep, path = value.partition("=")
        label = label.strip()
        path = path.strip()
        if not sep or not label or not path:
            raise ValueError("Variants must use label=path format.")
        specs.append((label, path))
    return specs


@app.callback()
def main_callback(
    ctx: typer.Context,
    pretty: bool = typer.Option(False, "--pretty", help="Pretty-print JSON output."),
    repo_root: str | None = typer.Option(None, "--repo-root", help="Override the local SimulationCraft checkout path."),
) -> None:
    ctx.obj = RuntimeConfig(pretty=pretty, repo_root=repo_root)


@app.command("doctor")
def doctor(ctx: typer.Context) -> None:
    paths = _repo_paths(ctx)
    resolution = _repo_resolution(ctx)
    repo = _repo_payload(paths)
    status = "ready" if repo["repo_ready"] and repo["build_ready"] else "degraded"
    _emit(
        ctx,
        {
            "provider": "simc",
            "status": status,
            "command": "doctor",
            "installed": True,
            "language": "python",
            "auth": {
                "required": False,
                "deferred": False,
            },
            "capabilities": {
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
            },
            "repo_resolution": {
                "source": resolution.source,
                "config_path": str(resolution.config_path),
                "configured_root": str(resolution.configured_root) if resolution.configured_root else None,
                "managed_root": str(resolution.managed_root),
                "managed_exists": resolution.managed_exists,
            },
            "repo": repo,
        },
    )


@app.command("repo")
def repo_command(
    ctx: typer.Context,
    set_root: str | None = typer.Option(None, "--set-root", help="Persist an explicit SimulationCraft repo root."),
    clear_root: bool = typer.Option(False, "--clear-root", help="Clear the persisted explicit SimulationCraft repo root."),
) -> None:
    if set_root and clear_root:
        _fail(ctx, "invalid_query", "Use either --set-root or --clear-root, not both.")
        return
    changed = False
    action = "inspect"
    stored_root: str | None = None
    if set_root:
        resolved = Path(set_root).expanduser().resolve()
        if not resolved.exists():
            _fail(ctx, "not_found", f"Repo root not found: {resolved}")
            return
        save_configured_repo_root(resolved)
        changed = True
        action = "set_root"
        stored_root = str(resolved)
    elif clear_root:
        changed = clear_configured_repo_root()
        action = "clear_root"
    resolution = _repo_resolution(ctx)
    _emit(
        ctx,
        {
            "provider": "simc",
            "action": action,
            "changed": changed,
            "stored_root": stored_root,
            "resolution": {
                "root": str(resolution.root),
                "source": resolution.source,
                "config_path": str(resolution.config_path),
                "configured_root": str(resolution.configured_root) if resolution.configured_root else None,
                "managed_root": str(resolution.managed_root),
                "managed_exists": resolution.managed_exists,
            },
        },
    )


@app.command("checkout")
def checkout_command(ctx: typer.Context) -> None:
    try:
        result = checkout_managed_repo()
    except RuntimeError as exc:
        _fail(ctx, "checkout_failed", str(exc))
        return
    resolution = _repo_resolution(ctx)
    _emit(
        ctx,
        {
            "provider": "simc",
            "status": result.status,
            "repo_url": result.repo_url,
            "managed_root": str(result.root),
            "commands": result.commands,
            "active_resolution": {
                "root": str(resolution.root),
                "source": resolution.source,
                "configured_root": str(resolution.configured_root) if resolution.configured_root else None,
                "managed_root": str(resolution.managed_root),
            },
            "note": "Managed checkout becomes active when no CLI override, SIMC_REPO_ROOT, or configured explicit root is set.",
        },
    )


@app.command("search")
def search(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="Free-text query. Structured discovery is deferred for simc phase 1."),
    limit: int = typer.Option(5, "--limit", min=1, max=50, help="Unused in phase 1."),
) -> None:
    del limit
    _emit(ctx, _coming_soon_payload(query=query, suggested_command="simc spec-files monk"))


@app.command("resolve")
def resolve(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="Free-text query. Structured resolution is deferred for simc phase 1."),
    limit: int = typer.Option(5, "--limit", min=1, max=50, help="Unused in phase 1."),
) -> None:
    del limit
    paths = _repo_paths(ctx)
    example_apl = paths.apl_default / "monk_mistweaver.simc"
    _emit(
        ctx,
        _coming_soon_payload(
            query=query,
            suggested_command=f"simc decode-build --apl-path {example_apl}",
        ),
    )


@app.command("version")
def version(ctx: typer.Context) -> None:
    version_info = binary_version(_repo_paths(ctx))
    if not version_info.available:
        _fail(ctx, "missing_binary", f"SimC binary not found: {version_info.binary_path}")
        return
    _emit(
        ctx,
        {
            "provider": "simc",
            "binary": {
                "path": str(version_info.binary_path),
                "available": version_info.available,
                "returncode": version_info.returncode,
            },
            "version": version_info.version_line,
        },
    )


@app.command("inspect")
def inspect(
    ctx: typer.Context,
    target: str | None = typer.Argument(None, help="Optional file path to inspect. If omitted, inspect the repo."),
) -> None:
    paths = _repo_paths(ctx)
    if target is None:
        _emit(ctx, {"provider": "simc", "inspect": "repo", "repo": _repo_payload(paths)})
        return
    resolved = Path(target).expanduser().resolve()
    if not resolved.exists():
        _fail(ctx, "not_found", f"Inspect target not found: {resolved}")
        return
    payload: dict[str, Any] = {
        "provider": "simc",
        "inspect": "path",
        "target": {
            "path": str(resolved),
            "relative_to_repo": str(resolved.relative_to(paths.root)) if resolved.is_relative_to(paths.root) else None,
            "kind": "directory" if resolved.is_dir() else "file",
        },
    }
    if resolved.is_file():
        text = resolved.read_text()
        inferred_class, inferred_spec = infer_actor_and_spec_from_apl(resolved)
        payload["target"].update(
            {
                "suffix": resolved.suffix,
                "line_count": len(text.splitlines()),
                "inferred_actor_class": inferred_class,
                "inferred_spec": inferred_spec,
                "build_spec": _serialize_build_spec(extract_build_spec_from_text(text)),
            }
        )
    _emit(ctx, payload)


@app.command("spec-files")
def spec_files(
    ctx: typer.Context,
    query: str | None = typer.Argument(None, help="Optional substring to narrow APL and class-module files."),
    limit: int = typer.Option(25, "--limit", min=1, max=200, help="Maximum file rows to return per category."),
) -> None:
    paths = _repo_paths(ctx)
    matches = spec_file_search(paths, query)
    categories: dict[str, Any] = {}
    total = 0
    for category, rows in matches.items():
        items = [
            {
                "path": str(path),
                "relative_path": str(path.relative_to(paths.root)) if path.is_relative_to(paths.root) else str(path),
                "stem": path.stem,
            }
            for path in rows[:limit]
        ]
        categories[category] = {
            "count": len(rows),
            "items": items,
            "truncated": len(rows) > limit,
        }
        total += len(rows)
    _emit(ctx, {"provider": "simc", "query": query, "count": total, "categories": categories})


@app.command("decode-build")
def decode_build_command(
    ctx: typer.Context,
    apl_path: str | None = typer.Option(None, "--apl-path", help="Optional APL path used to infer actor class and spec."),
    profile_path: str | None = typer.Option(None, "--profile-path", help="Optional profile path containing build lines."),
    build_file: str | None = typer.Option(None, "--build-file", help="Optional plain text file with talents/spec lines."),
    build_packet: str | None = typer.Option(None, "--build-packet", help="Path to a talent transport packet JSON file."),
    build_text: str | None = typer.Option(
        None, "--build-text", help="Inline build text, talent hash, or Wowhead talent-calc URL with build code."),
    talents: str | None = typer.Option(
        None, "--talents", help="WoW export, Wowhead talent-calc URL with build code, SimC talents string, or talents=... line."),
    class_talents: str | None = typer.Option(None, "--class-talents", help="Split class talents string."),
    spec_talents: str | None = typer.Option(None, "--spec-talents", help="Split spec talents string."),
    hero_talents: str | None = typer.Option(None, "--hero-talents", help="Split hero talents string."),
    actor_class: str | None = typer.Option(None, "--actor-class", help="Actor class such as monk or evoker."),
    spec_name: str | None = typer.Option(None, "--spec", help="Spec name such as mistweaver."),
) -> None:
    paths = _repo_paths(ctx)
    build_spec, identity = _load_identified_build_spec_or_fail(
        ctx,
        paths,
        apl_path=apl_path,
        profile_path=profile_path,
        build_file=build_file,
        build_packet=build_packet,
        build_text=build_text,
        talents=talents,
        class_talents=class_talents,
        spec_talents=spec_talents,
        hero_talents=hero_talents,
        actor_class=actor_class,
        spec_name=spec_name,
    )
    if not build_spec.actor_class or not build_spec.spec:
        _fail(
            ctx,
            "invalid_query",
            "Could not determine actor class and spec for build decoding.",
            extra={"build_spec": _serialize_build_spec(build_spec), "identity": _serialize_build_identity(identity)},
        )
        return
    try:
        resolution = decode_build(paths, build_spec)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        extra = {"build_spec": _serialize_build_spec(build_spec), "identity": _serialize_build_identity(identity)}
        if build_spec.actor_class and build_spec.spec and any(
            [build_spec.talents, build_spec.class_talents, build_spec.spec_talents, build_spec.hero_talents]
        ):
            with contextlib.suppress(ValueError):
                extra["generated_profile"] = build_profile_text(build_spec)
        _fail(ctx, "decode_failed", str(exc), extra=extra)
        return
    _emit(
        ctx,
        {
            "provider": "simc",
            "build_spec": _serialize_build_spec(build_spec),
            "identity": _serialize_build_identity(identity),
            "decoded": {
                "actor_class": resolution.actor_class,
                "spec": resolution.spec,
                "source_kind": resolution.source_kind,
                "generated_profile": resolution.generated_profile_text,
                "enabled_talents": sorted(resolution.enabled_talents),
                "talents_by_tree": {
                    tree: [
                        {
                            "name": talent.name,
                            "token": talent.token,
                            "rank": talent.rank,
                            "max_rank": talent.max_rank,
                        }
                        for talent in talents
                    ]
                    for tree, talents in resolution.talents_by_tree.items()
                },
                "source_notes": resolution.source_notes,
            },
        },
    )


@app.command("identify-build")
def identify_build_command(
    ctx: typer.Context,
    apl_path: str | None = typer.Option(None, "--apl-path", help="Optional APL path used to infer actor class and spec."),
    profile_path: str | None = typer.Option(None, "--profile-path", help="Optional profile path containing build lines."),
    build_file: str | None = typer.Option(None, "--build-file", help="Optional plain text file with talents/spec lines."),
    build_packet: str | None = typer.Option(None, "--build-packet", help="Path to a talent transport packet JSON file."),
    build_text: str | None = typer.Option(
        None, "--build-text", help="Inline build text, talent hash, or Wowhead talent-calc URL with build code."),
    talents: str | None = typer.Option(
        None, "--talents", help="WoW export, Wowhead talent-calc URL with build code, SimC talents string, or talents=... line."),
    class_talents: str | None = typer.Option(None, "--class-talents", help="Split class talents string."),
    spec_talents: str | None = typer.Option(None, "--spec-talents", help="Split spec talents string."),
    hero_talents: str | None = typer.Option(None, "--hero-talents", help="Split hero talents string."),
    actor_class: str | None = typer.Option(None, "--actor-class", help="Actor class such as monk or evoker."),
    spec_name: str | None = typer.Option(None, "--spec", help="Spec name such as mistweaver."),
) -> None:
    paths = _repo_paths(ctx)
    build_spec, identity = _load_identified_build_spec_or_fail(
        ctx,
        paths,
        apl_path=apl_path,
        profile_path=profile_path,
        build_file=build_file,
        build_packet=build_packet,
        build_text=build_text,
        talents=talents,
        class_talents=class_talents,
        spec_talents=spec_talents,
        hero_talents=hero_talents,
        actor_class=actor_class,
        spec_name=spec_name,
    )
    _emit(
        ctx,
        {
            "provider": "simc",
            "kind": "identify_build",
            "build_spec": _serialize_build_spec(build_spec),
            "identity": _serialize_build_identity(identity),
        },
    )


@app.command("validate-talent-transport")
def validate_talent_transport_command(
    ctx: typer.Context,
    build_packet: str | None = typer.Option(None, "--build-packet", help="Path to a talent transport packet JSON file."),
    talent_row: list[str] = typer.Option([], "--talent-row", help="Raw talent row as entry_id:node_id:rank. Repeat as needed."),
    actor_class: str | None = typer.Option(None, "--actor-class", help="Actor class such as druid or paladin."),
    spec_name: str | None = typer.Option(None, "--spec", help="Spec name such as balance or retribution."),
    out: str | None = typer.Option(None, "--out", help="Optional path to write the upgraded packet JSON when --build-packet is used."),
) -> None:
    if build_packet and talent_row:
        _fail(ctx, "invalid_query", "Use either --build-packet or --talent-row, not both.")
        return
    if not build_packet and not talent_row:
        _fail(ctx, "invalid_query", "Provide either --build-packet or at least one --talent-row.")
        return
    if out and not build_packet:
        _fail(ctx, "invalid_query", "--out requires --build-packet.")
        return

    source = "talent_rows"
    resolved_packet_path: str | None = None
    packet_transport_status: str | None = None
    packet: dict[str, Any] | None = None
    rows: list[dict[str, Any]] = []
    resolved_actor_class = actor_class
    resolved_spec = spec_name

    if build_packet:
        try:
            packet, resolved_packet_path = _load_transport_packet(build_packet)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            _fail(ctx, "invalid_build_packet", str(exc))
            return
        source = "build_packet"
        packet_transport_status = packet.get("transport_status") if isinstance(packet.get("transport_status"), str) else None
        rows = _packet_talent_tree_rows(packet)
        if actor_class is None:
            resolved_actor_class = _packet_identity_value(packet, "actor_class")
        if spec_name is None:
            resolved_spec = _packet_identity_value(packet, "spec")
    else:
        try:
            rows = [_parse_talent_row(value) for value in talent_row]
        except ValueError as exc:
            _fail(ctx, "invalid_talent_row", str(exc))
            return

    if not rows:
        _fail(ctx, "invalid_query", "No raw talent rows were available to validate.")
        return
    if not resolved_actor_class or not resolved_spec:
        _fail(
            ctx,
            "invalid_query",
            (
                "Validate-talent-transport requires class/spec identity. "
                "Provide --actor-class and --spec, or use a build packet with packet identity."
            ),
        )
        return

    result = validate_talent_tree_transport(
        actor_class=resolved_actor_class,
        spec=resolved_spec,
        talent_tree_rows=rows,
        repo_root=_cfg(ctx).repo_root,
    )
    transport_forms = result.get("transport_forms") if isinstance(result.get("transport_forms"), dict) else {}
    validation = result.get("validation") if isinstance(result.get("validation"), dict) else {}
    transport_status = "validated" if transport_forms.get("simc_split_talents") else "raw_only"
    updated_packet: dict[str, Any] | None = None
    written_packet_path: str | None = None
    if packet is not None:
        try:
            updated_packet = refresh_talent_transport_packet(
                packet,
                transport_forms=transport_forms,
                validation=validation,
                build_identity=build_identity_payload(
                    actor_class=resolved_actor_class,
                    spec=resolved_spec,
                    confidence="high" if resolved_actor_class and resolved_spec else "none",
                    source="simc_validate_talent_transport",
                    candidates=[(resolved_actor_class, resolved_spec)] if resolved_actor_class and resolved_spec else None,
                    source_notes=[
                        "class/spec identity was refreshed from simc validate-talent-transport input"
                    ],
                ),
            )
        except ValueError as exc:
            _fail(ctx, "invalid_build_packet", str(exc))
            return
        transport_status = updated_packet["transport_status"] if isinstance(
            updated_packet.get("transport_status"), str) else transport_status
        written_packet_path = _write_packet_json_or_fail(ctx, out=out, packet=updated_packet)
    _emit(
        ctx,
        {
            "provider": "simc",
            "kind": "validate_talent_transport",
            "input": {
                "source": source,
                "build_packet": resolved_packet_path,
                "packet_transport_status": packet_transport_status,
                "actor_class": resolved_actor_class,
                "spec": resolved_spec,
                "talent_row_count": len(rows),
            },
            "raw_talent_tree_entries": rows,
            "transport_status": transport_status,
            "transport_forms": transport_forms,
            "validation": validation,
            "updated_packet": updated_packet,
            "written_packet_path": written_packet_path,
        },
    )


@app.command("build-harness")
def build_harness_command(
    ctx: typer.Context,
    out: str | None = typer.Option(None, "--out", help="Output harness profile path."),
    apl_path: str | None = typer.Option(None, "--apl-path", help="Optional APL path used to infer actor class and spec."),
    profile_path: str | None = typer.Option(None, "--profile-path", help="Optional profile path containing build lines."),
    build_file: str | None = typer.Option(None, "--build-file", help="Optional plain text file with talents/spec lines."),
    build_text: str | None = typer.Option(None, "--build-text", help="Inline build text or talent hash."),
    talents: str | None = typer.Option(
        None, "--talents", help="WoW export, Wowhead talent-calc URL with build code, SimC talents string, or talents=... line."),
    class_talents: str | None = typer.Option(None, "--class-talents", help="Split class talents string."),
    spec_talents: str | None = typer.Option(None, "--spec-talents", help="Split spec talents string."),
    hero_talents: str | None = typer.Option(None, "--hero-talents", help="Split hero talents string."),
    actor_class: str | None = typer.Option(None, "--actor-class", help="Actor class such as warlock."),
    spec_name: str | None = typer.Option(None, "--spec", help="Spec name such as demonology."),
    line: list[str] = typer.Option([], "--line", help="Extra profile line. Repeat as needed."),
) -> None:
    paths = _repo_paths(ctx)
    build_spec, identity = _load_identified_build_spec_or_fail(
        ctx,
        paths,
        apl_path=apl_path,
        profile_path=profile_path,
        build_file=build_file,
        build_text=build_text,
        talents=talents,
        class_talents=class_talents,
        spec_talents=spec_talents,
        hero_talents=hero_talents,
        actor_class=actor_class,
        spec_name=spec_name,
    )
    if not build_spec.actor_class or not build_spec.spec:
        _fail(
            ctx,
            "invalid_query",
            "Could not determine actor class and spec for harness generation.",
            extra={"build_spec": _serialize_build_spec(build_spec), "identity": _serialize_build_identity(identity)},
        )
        return
    try:
        target = write_harness(build_spec, lines=line, out_path=out)
    except ValueError as exc:
        _fail(ctx, "build_harness_failed", str(exc))
        return
    _emit(
        ctx,
        {
            "provider": "simc",
            "kind": "build_harness",
            "path": str(target),
            "build_spec": _serialize_build_spec(build_spec),
            "identity": _serialize_build_identity(identity),
            "extra_lines": line,
        },
    )


@app.command("validate-apl")
def validate_apl_command(
    ctx: typer.Context,
    harness_path: str = typer.Argument(..., help="Harness profile path without APL actions."),
    apl_path: str = typer.Argument(..., help="APL file to append to the harness."),
    label: str = typer.Option("variant", "--label", help="Variant label for the generated temporary profile."),
    out_dir: str | None = typer.Option(None, "--out-dir", help="Optional directory for the generated temporary profile."),
) -> None:
    paths = _repo_paths(ctx)
    try:
        profile_path = build_variant_profile(harness_path, apl_path, label=label, out_dir=out_dir)
        validation = validate_profile_file(paths, profile_path)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        _fail(ctx, "validate_apl_failed", str(exc))
        return
    _emit(
        ctx,
        {
            "provider": "simc",
            "kind": "validate_apl",
            "label": label,
            "apl_path": str(Path(apl_path).expanduser().resolve()),
            "profile_path": str(profile_path),
            "valid": validation.result.returncode == 0,
            "returncode": validation.result.returncode,
            "stdout_preview": _preview_text(validation.result.stdout)[0],
            "stderr_preview": _preview_text(validation.result.stderr)[0],
        },
    )


@app.command("compare-apls")
def compare_apls_command(
    ctx: typer.Context,
    harness_path: str = typer.Argument(..., help="Harness profile path without APL actions."),
    base_apl: str = typer.Option(..., "--base-apl", help="Base APL path."),
    base_label: str = typer.Option("base", "--base-label", help="Label for the base APL."),
    variant: list[str] = typer.Option([], "--variant", help="Variant in label=path form. Repeat as needed."),
    iterations: int = typer.Option(250, "--iterations", min=1, help="Iterations per variant."),
    threads: int = typer.Option(1, "--threads", min=1, help="Threads per variant."),
    out_dir: str | None = typer.Option(None, "--out-dir", help="Optional directory for generated profiles and JSON reports."),
    validate_first: bool = typer.Option(True, "--validate-first/--skip-validate",
                                        help="Validate each generated profile before the full comparison."),
    report_out: str | None = typer.Option(None, "--report-out", help="Optional path to save the structured comparison JSON."),
) -> None:
    paths = _repo_paths(ctx)
    try:
        variant_specs = _parse_variant_specs(variant)
        payload = compare_apl_variants(
            paths,
            harness_path=harness_path,
            base_label=base_label,
            base_apl_path=base_apl,
            variant_specs=variant_specs,
            iterations=iterations,
            threads=threads,
            out_dir=out_dir,
            validate_first=validate_first,
        )
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        _fail(ctx, "compare_apls_failed", str(exc))
        return
    if report_out:
        target = Path(report_out).expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, indent=2) + "\n")
        payload["report_path"] = str(target)
    _emit(ctx, {"provider": "simc", **payload})


@app.command("variant-report")
def variant_report_command(
    ctx: typer.Context,
    report_path: str = typer.Argument(..., help="Path to a saved compare-apls JSON report."),
) -> None:
    resolved = Path(report_path).expanduser().resolve()
    if not resolved.exists():
        _fail(ctx, "not_found", f"Report not found: {resolved}")
        return
    try:
        report = json.loads(resolved.read_text())
    except json.JSONDecodeError as exc:
        _fail(ctx, "invalid_report", str(exc))
        return
    _emit(
        ctx,
        {
            "provider": "simc",
            "report_path": str(resolved),
            **variant_report_payload(report),
        },
    )


@app.command("verify-clean")
def verify_clean_command(
    ctx: typer.Context,
    hash_binary: bool = typer.Option(False, "--hash-binary", help="Hash the local simc binary as part of the cleanliness report."),
) -> None:
    paths = _repo_paths(ctx)
    _emit(ctx, {"provider": "simc", **verify_clean_payload(paths, hash_binary=hash_binary)})


@app.command("apl-lists")
def apl_lists(
    ctx: typer.Context,
    apl_path: str = typer.Argument(..., help="Path to a .simc APL file."),
    list_name: str | None = typer.Option(None, "--list", help="Only return one action list."),
) -> None:
    paths = _repo_paths(ctx)
    resolved = _resolve_path(paths, apl_path)
    if not resolved.exists():
        _fail(ctx, "not_found", f"APL file not found: {resolved}")
        return
    entries = parse_apl(resolved)
    grouped = group_entries(entries)
    selected_names = [list_name] if list_name else sorted(grouped)
    lists_payload: list[dict[str, Any]] = []
    for current in selected_names:
        current_entries = grouped.get(current, [])
        lists_payload.append(
            {
                "list_name": current,
                "count": len(current_entries),
                "entries": [
                    {
                        "line_no": entry.line_no,
                        "action": entry.action,
                        "kind": entry.kind,
                        "target_list": entry.target_list,
                        "condition": entry.condition,
                        "raw": entry.raw,
                    }
                    for entry in current_entries
                ],
            }
        )
    _emit(
        ctx,
        {
            "provider": "simc",
            "apl": {
                "path": str(resolved),
                "relative_to_repo": str(resolved.relative_to(paths.root)) if resolved.is_relative_to(paths.root) else None,
                "entry_count": len(entries),
                "list_count": len(grouped),
            },
            "lists": lists_payload,
        },
    )


@app.command("apl-graph")
def apl_graph_command(
    ctx: typer.Context,
    apl_path: str = typer.Argument(..., help="Path to a .simc APL file."),
) -> None:
    paths = _repo_paths(ctx)
    resolved = _resolve_path(paths, apl_path)
    if not resolved.exists():
        _fail(ctx, "not_found", f"APL file not found: {resolved}")
        return
    entries = parse_apl(resolved)
    grouped = group_entries(entries)
    _emit(
        ctx,
        {
            "provider": "simc",
            "apl": {
                "path": str(resolved),
                "relative_to_repo": str(resolved.relative_to(paths.root)) if resolved.is_relative_to(paths.root) else None,
                "list_count": len(grouped),
            },
            "graph": {
                "format": "mermaid",
                "text": mermaid_graph(entries),
            },
        },
    )


@app.command("apl-talents")
def apl_talents_command(
    ctx: typer.Context,
    apl_path: str = typer.Argument(..., help="Path to a .simc APL file."),
) -> None:
    paths = _repo_paths(ctx)
    resolved = _resolve_path(paths, apl_path)
    if not resolved.exists():
        _fail(ctx, "not_found", f"APL file not found: {resolved}")
        return
    entries = parse_apl(resolved)
    refs = talent_refs(entries)
    counts = action_counts(entries)
    _emit(
        ctx,
        {
            "provider": "simc",
            "apl": {
                "path": str(resolved),
                "relative_to_repo": str(resolved.relative_to(paths.root)) if resolved.is_relative_to(paths.root) else None,
            },
            "count": len(refs),
            "talents": [{"token": token, "lines": lines} for token, lines in refs.items()],
            "action_counts": [{"name": name, "count": count} for name, count in counts.most_common(25)],
        },
    )


@app.command("find-action")
def find_action_command(
    ctx: typer.Context,
    action: str = typer.Argument(..., help="Action, buff, or token to search for."),
    wow_class: str | None = typer.Option(None, "--class", help="Optional class name to narrow code and spell dumps."),
    limit: int = typer.Option(25, "--limit", min=1, max=200, help="Maximum hits to return per bucket."),
) -> None:
    paths = _repo_paths(ctx)
    results = find_action(paths, action, wow_class)
    buckets: dict[str, Any] = {}
    total = 0
    for bucket, hits in results.items():
        items = [
            {
                "path": str(hit.path),
                "relative_to_repo": str(hit.path.relative_to(paths.root)) if hit.path.is_relative_to(paths.root) else str(hit.path),
                "line_no": hit.line_no,
                "text": hit.text,
            }
            for hit in hits[:limit]
        ]
        buckets[bucket] = {
            "count": len(hits),
            "items": items,
            "truncated": len(hits) > limit,
        }
        total += len(hits)
    _emit(ctx, {"provider": "simc", "action": action, "class_filter": wow_class, "count": total, "buckets": buckets})


@app.command("trace-action")
def trace_action_command(
    ctx: typer.Context,
    apl_path: str = typer.Argument(..., help="Path to a .simc APL file."),
    action: str = typer.Argument(..., help="Action name to trace."),
    wow_class: str | None = typer.Option(None, "--class", help="Optional class name to narrow code and spell dumps."),
    limit: int = typer.Option(25, "--limit", min=1, max=200, help="Maximum non-APL hits to return per bucket."),
) -> None:
    paths = _repo_paths(ctx)
    resolved = _resolve_path(paths, apl_path)
    if not resolved.exists():
        _fail(ctx, "not_found", f"APL file not found: {resolved}")
        return
    entries = trace_action_entries(parse_apl(resolved), action)
    search_hits = find_action(paths, action, wow_class)
    buckets: dict[str, Any] = {}
    total = 0
    for bucket, hits in search_hits.items():
        items = [
            {
                "path": str(hit.path),
                "relative_to_repo": str(hit.path.relative_to(paths.root)) if hit.path.is_relative_to(paths.root) else str(hit.path),
                "line_no": hit.line_no,
                "text": hit.text,
            }
            for hit in hits[:limit]
        ]
        buckets[bucket] = {
            "count": len(hits),
            "items": items,
            "truncated": len(hits) > limit,
        }
        total += len(hits)
    _emit(
        ctx,
        {
            "provider": "simc",
            "action": action,
            "class_filter": wow_class,
            "apl": {
                "path": str(resolved),
                "relative_to_repo": str(resolved.relative_to(paths.root)) if resolved.is_relative_to(paths.root) else None,
            },
            "apl_hits": {
                "count": len(entries),
                "items": [
                    {
                        "line_no": entry.line_no,
                        "list_name": entry.list_name,
                        "action": entry.action,
                        "kind": entry.kind,
                        "target_list": entry.target_list,
                        "condition": entry.condition,
                        "raw": entry.raw,
                    }
                    for entry in entries
                ],
            },
            "external_hit_count": total,
            "buckets": buckets,
        },
    )


@app.command("apl-prune")
def apl_prune_command(
    ctx: typer.Context,
    apl_path: str = typer.Argument(..., help="Path to a .simc APL file."),
    targets: int = typer.Option(1, "--targets", min=1, help="Active target count."),
    list_name: str | None = typer.Option(None, "--list", help="Only return one action list."),
    show: str = typer.Option("all", "--show", help="One of all, eligible, dead, or unknown."),
    profile_path: str | None = typer.Option(None, "--profile-path", help="Optional profile path containing build lines."),
    build_file: str | None = typer.Option(None, "--build-file", help="Optional plain text file with talents/spec lines."),
    build_text: str | None = typer.Option(None, "--build-text", help="Inline build text or talent hash."),
    talents: str | None = typer.Option(
        None, "--talents", help="WoW export, Wowhead talent-calc URL with build code, SimC talents string, or talents=... line."),
    class_talents: str | None = typer.Option(None, "--class-talents", help="Split class talents string."),
    spec_talents: str | None = typer.Option(None, "--spec-talents", help="Split spec talents string."),
    hero_talents: str | None = typer.Option(None, "--hero-talents", help="Split hero talents string."),
    actor_class: str | None = typer.Option(None, "--actor-class", help="Actor class such as monk or evoker."),
    spec_name: str | None = typer.Option(None, "--spec", help="Spec name such as mistweaver."),
    enable: list[str] = typer.Option([], "--enable", help="Enabled talent names. Repeat or pass comma-separated values."),
    disable: list[str] = typer.Option([], "--disable", help="Disabled talent names. Repeat or pass comma-separated values."),
) -> None:
    if show not in {"all", "eligible", "dead", "unknown"}:
        _fail(ctx, "invalid_query", "--show must be one of: all, eligible, dead, unknown")
        return
    paths = _repo_paths(ctx)
    resolved = _resolve_path(paths, apl_path)
    if not resolved.exists():
        _fail(ctx, "not_found", f"APL file not found: {resolved}")
        return
    option_values = _build_option_values(
        profile_path=profile_path,
        build_file=build_file,
        build_text=build_text,
        talents=talents,
        class_talents=class_talents,
        spec_talents=spec_talents,
        hero_talents=hero_talents,
        actor_class=actor_class,
        spec_name=spec_name,
        enable=enable,
        disable=disable,
    )
    try:
        context, resolution = _resolve_prune_context(paths, resolved, option_values, targets)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        _fail(ctx, "prune_context_failed", str(exc))
        return
    grouped: dict[str, list[Any]] = {}
    for pruned in prune_entries(parse_apl(resolved), context):
        grouped.setdefault(pruned.entry.list_name, []).append(pruned)
    selected_names = [list_name] if list_name else sorted(grouped)
    lists_payload: list[dict[str, Any]] = []
    for current in selected_names:
        current_entries = grouped.get(current, [])
        items = []
        for pruned in current_entries:
            if show != "all" and pruned.state.value != show:
                continue
            items.append(
                {
                    "line_no": pruned.entry.line_no,
                    "action": pruned.entry.action,
                    "target_list": pruned.entry.target_list,
                    "condition": pruned.entry.condition,
                    "state": pruned.state.value,
                    "reason": pruned.reason,
                    "raw": pruned.entry.raw,
                }
            )
        lists_payload.append({"list_name": current, "count": len(items), "items": items})
    _emit(
        ctx,
        {
            "provider": "simc",
            "apl": {
                "path": str(resolved),
                "relative_to_repo": str(resolved.relative_to(paths.root)) if resolved.is_relative_to(paths.root) else None,
            },
            "build": {
                "actor_class": resolution.actor_class,
                "spec": resolution.spec,
                "targets": context.targets,
                "enabled_talents": len(context.enabled_talents),
                "source_notes": resolution.source_notes,
            },
            "show": show,
            "lists": lists_payload,
        },
    )


@app.command("apl-branch-trace")
def apl_branch_trace_command(
    ctx: typer.Context,
    apl_path: str = typer.Argument(..., help="Path to a .simc APL file."),
    targets: int = typer.Option(1, "--targets", min=1, help="Active target count."),
    list_name: str = typer.Option("default", "--list", help="Starting action list."),
    max_depth: int = typer.Option(6, "--max-depth", min=1, max=20, help="Maximum recursive trace depth."),
    profile_path: str | None = typer.Option(None, "--profile-path", help="Optional profile path containing build lines."),
    build_file: str | None = typer.Option(None, "--build-file", help="Optional plain text file with talents/spec lines."),
    build_text: str | None = typer.Option(None, "--build-text", help="Inline build text or talent hash."),
    talents: str | None = typer.Option(
        None, "--talents", help="WoW export, Wowhead talent-calc URL with build code, SimC talents string, or talents=... line."),
    class_talents: str | None = typer.Option(None, "--class-talents", help="Split class talents string."),
    spec_talents: str | None = typer.Option(None, "--spec-talents", help="Split spec talents string."),
    hero_talents: str | None = typer.Option(None, "--hero-talents", help="Split hero talents string."),
    actor_class: str | None = typer.Option(None, "--actor-class", help="Actor class such as monk or evoker."),
    spec_name: str | None = typer.Option(None, "--spec", help="Spec name such as mistweaver."),
    enable: list[str] = typer.Option([], "--enable", help="Enabled talent names. Repeat or pass comma-separated values."),
    disable: list[str] = typer.Option([], "--disable", help="Disabled talent names. Repeat or pass comma-separated values."),
) -> None:
    paths = _repo_paths(ctx)
    resolved = _resolve_path(paths, apl_path)
    if not resolved.exists():
        _fail(ctx, "not_found", f"APL file not found: {resolved}")
        return
    option_values = _build_option_values(
        profile_path=profile_path,
        build_file=build_file,
        build_text=build_text,
        talents=talents,
        class_talents=class_talents,
        spec_talents=spec_talents,
        hero_talents=hero_talents,
        actor_class=actor_class,
        spec_name=spec_name,
        enable=enable,
        disable=disable,
    )
    try:
        context, resolution = _resolve_prune_context(paths, resolved, option_values, targets)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        _fail(ctx, "branch_trace_failed", str(exc))
        return
    summary = summarize_branches(resolved, context, start_list=list_name)
    trace_lines = trace_apl(resolved, context, start_list=list_name, max_depth=max_depth)
    _emit(
        ctx,
        {
            "provider": "simc",
            "apl": {
                "path": str(resolved),
                "relative_to_repo": str(resolved.relative_to(paths.root)) if resolved.is_relative_to(paths.root) else None,
            },
            "build": {
                "actor_class": resolution.actor_class,
                "spec": resolution.spec,
                "targets": context.targets,
                "enabled_talents": len(context.enabled_talents),
                "source_notes": resolution.source_notes,
            },
            "summary": {
                "start_list": summary.start_list,
                "guaranteed_dispatch": summary.guaranteed_dispatch,
                "guaranteed_dispatch_line": summary.guaranteed_dispatch_line,
                "guaranteed_dispatch_reason": summary.guaranteed_dispatch_reason,
                "dead_branches": summary.dead_branches,
                "unresolved_branches": summary.unresolved_branches,
                "shadowed_lines": summary.shadowed_lines,
            },
            "trace": [{"depth": line.depth, "text": line.text} for line in trace_lines],
        },
    )


@app.command("apl-intent")
def apl_intent_command(
    ctx: typer.Context,
    apl_path: str = typer.Argument(..., help="Path to a .simc APL file."),
    targets: int = typer.Option(1, "--targets", min=1, help="Active target count."),
    list_name: str = typer.Option("default", "--list", help="Starting action list."),
    limit: int = typer.Option(6, "--limit", min=1, max=50, help="Number of intent lines to return."),
    profile_path: str | None = typer.Option(None, "--profile-path", help="Optional profile path containing build lines."),
    build_file: str | None = typer.Option(None, "--build-file", help="Optional plain text file with talents/spec lines."),
    build_text: str | None = typer.Option(None, "--build-text", help="Inline build text or talent hash."),
    talents: str | None = typer.Option(
        None, "--talents", help="WoW export, Wowhead talent-calc URL with build code, SimC talents string, or talents=... line."),
    class_talents: str | None = typer.Option(None, "--class-talents", help="Split class talents string."),
    spec_talents: str | None = typer.Option(None, "--spec-talents", help="Split spec talents string."),
    hero_talents: str | None = typer.Option(None, "--hero-talents", help="Split hero talents string."),
    actor_class: str | None = typer.Option(None, "--actor-class", help="Actor class such as monk or evoker."),
    spec_name: str | None = typer.Option(None, "--spec", help="Spec name such as mistweaver."),
    enable: list[str] = typer.Option([], "--enable", help="Enabled talent names. Repeat or pass comma-separated values."),
    disable: list[str] = typer.Option([], "--disable", help="Disabled talent names. Repeat or pass comma-separated values."),
) -> None:
    paths = _repo_paths(ctx)
    resolved = _resolve_path(paths, apl_path)
    if not resolved.exists():
        _fail(ctx, "not_found", f"APL file not found: {resolved}")
        return
    option_values = _build_option_values(
        profile_path=profile_path,
        build_file=build_file,
        build_text=build_text,
        talents=talents,
        class_talents=class_talents,
        spec_talents=spec_talents,
        hero_talents=hero_talents,
        actor_class=actor_class,
        spec_name=spec_name,
        enable=enable,
        disable=disable,
    )
    try:
        context, resolution = _resolve_prune_context(paths, resolved, option_values, targets)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        _fail(ctx, "intent_failed", str(exc))
        return
    summary = summarize_branches(resolved, context, start_list=list_name)
    focus_list = summary.guaranteed_dispatch or list_name
    _emit(
        ctx,
        {
            "provider": "simc",
            "apl": {
                "path": str(resolved),
                "relative_to_repo": str(resolved.relative_to(paths.root)) if resolved.is_relative_to(paths.root) else None,
            },
            "build": {
                "actor_class": resolution.actor_class,
                "spec": resolution.spec,
                "targets": context.targets,
                "enabled_talents": len(context.enabled_talents),
                "source_notes": resolution.source_notes,
            },
            "focus_list": focus_list,
            "summary": {
                "start_list": summary.start_list,
                "guaranteed_dispatch": summary.guaranteed_dispatch,
                "guaranteed_dispatch_line": summary.guaranteed_dispatch_line,
                "guaranteed_dispatch_reason": summary.guaranteed_dispatch_reason,
                "dead_branches": summary.dead_branches,
                "unresolved_branches": summary.unresolved_branches,
                "shadowed_lines": summary.shadowed_lines,
            },
            "intent": summarize_intent(resolved, context, focus_list, limit=limit),
        },
    )


@app.command("apl-intent-explain")
def apl_intent_explain_command(
    ctx: typer.Context,
    apl_path: str = typer.Argument(..., help="Path to a .simc APL file."),
    targets: int = typer.Option(1, "--targets", min=1, help="Active target count."),
    list_name: str = typer.Option("default", "--list", help="Starting action list."),
    limit: int = typer.Option(8, "--limit", min=1, max=50, help="Maximum items per bucket."),
    profile_path: str | None = typer.Option(None, "--profile-path", help="Optional profile path containing build lines."),
    build_file: str | None = typer.Option(None, "--build-file", help="Optional plain text file with talents/spec lines."),
    build_text: str | None = typer.Option(None, "--build-text", help="Inline build text or talent hash."),
    talents: str | None = typer.Option(
        None, "--talents", help="WoW export, Wowhead talent-calc URL with build code, SimC talents string, or talents=... line."),
    class_talents: str | None = typer.Option(None, "--class-talents", help="Split class talents string."),
    spec_talents: str | None = typer.Option(None, "--spec-talents", help="Split spec talents string."),
    hero_talents: str | None = typer.Option(None, "--hero-talents", help="Split hero talents string."),
    actor_class: str | None = typer.Option(None, "--actor-class", help="Actor class such as monk or evoker."),
    spec_name: str | None = typer.Option(None, "--spec", help="Spec name such as mistweaver."),
    enable: list[str] = typer.Option([], "--enable", help="Enabled talent names. Repeat or pass comma-separated values."),
    disable: list[str] = typer.Option([], "--disable", help="Disabled talent names. Repeat or pass comma-separated values."),
) -> None:
    paths = _repo_paths(ctx)
    resolved = _resolve_path(paths, apl_path)
    if not resolved.exists():
        _fail(ctx, "not_found", f"APL file not found: {resolved}")
        return
    option_values = _build_option_values(
        profile_path=profile_path,
        build_file=build_file,
        build_text=build_text,
        talents=talents,
        class_talents=class_talents,
        spec_talents=spec_talents,
        hero_talents=hero_talents,
        actor_class=actor_class,
        spec_name=spec_name,
        enable=enable,
        disable=disable,
    )
    try:
        context, resolution = _resolve_prune_context(paths, resolved, option_values, targets)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        _fail(ctx, "intent_explain_failed", str(exc))
        return
    summary = summarize_branches(resolved, context, start_list=list_name)
    focus_list = summary.guaranteed_dispatch or list_name
    explanation = explain_intent(resolved, context, focus_list, limit=limit)
    _emit(
        ctx,
        {
            "provider": "simc",
            "apl": {
                "path": str(resolved),
                "relative_to_repo": str(resolved.relative_to(paths.root)) if resolved.is_relative_to(paths.root) else None,
            },
            "build": {
                "actor_class": resolution.actor_class,
                "spec": resolution.spec,
                "targets": context.targets,
                "enabled_talents": len(context.enabled_talents),
                "source_notes": resolution.source_notes,
            },
            "focus_list": focus_list,
            "summary": {
                "start_list": summary.start_list,
                "guaranteed_dispatch": summary.guaranteed_dispatch,
                "guaranteed_dispatch_line": summary.guaranteed_dispatch_line,
                "guaranteed_dispatch_reason": summary.guaranteed_dispatch_reason,
                "dead_branches": summary.dead_branches,
                "unresolved_branches": summary.unresolved_branches,
                "shadowed_lines": summary.shadowed_lines,
            },
            "explained_intent": {
                "setup": explanation.setup,
                "helpers": explanation.helpers,
                "burst": explanation.burst,
                "priorities": explanation.priorities,
            },
        },
    )


@app.command("priority")
def priority_command(
    ctx: typer.Context,
    apl_path: str = typer.Argument(..., help="Path to a .simc APL file."),
    targets: int = typer.Option(1, "--targets", min=1, help="Active target count."),
    list_name: str = typer.Option("default", "--list", help="Starting action list."),
    limit: int = typer.Option(12, "--limit", min=1, max=100, help="Maximum active priority rows to return."),
    profile_path: str | None = typer.Option(None, "--profile-path", help="Optional profile path containing build lines."),
    build_file: str | None = typer.Option(None, "--build-file", help="Optional plain text file with talents/spec lines."),
    build_text: str | None = typer.Option(None, "--build-text", help="Inline build text or talent hash."),
    talents: str | None = typer.Option(
        None, "--talents", help="WoW export, Wowhead talent-calc URL with build code, SimC talents string, or talents=... line."),
    class_talents: str | None = typer.Option(None, "--class-talents", help="Split class talents string."),
    spec_talents: str | None = typer.Option(None, "--spec-talents", help="Split spec talents string."),
    hero_talents: str | None = typer.Option(None, "--hero-talents", help="Split hero talents string."),
    actor_class: str | None = typer.Option(None, "--actor-class", help="Actor class such as monk or evoker."),
    spec_name: str | None = typer.Option(None, "--spec", help="Spec name such as mistweaver."),
    enable: list[str] = typer.Option([], "--enable", help="Enabled talent names. Repeat or pass comma-separated values."),
    disable: list[str] = typer.Option([], "--disable", help="Disabled talent names. Repeat or pass comma-separated values."),
) -> None:
    paths = _repo_paths(ctx)
    resolved = _resolve_path(paths, apl_path)
    if not resolved.exists():
        _fail(ctx, "not_found", f"APL file not found: {resolved}")
        return
    option_values = _build_option_values(
        profile_path=profile_path,
        build_file=build_file,
        build_text=build_text,
        talents=talents,
        class_talents=class_talents,
        spec_talents=spec_talents,
        hero_talents=hero_talents,
        actor_class=actor_class,
        spec_name=spec_name,
        enable=enable,
        disable=disable,
    )
    try:
        context, resolution = _resolve_prune_context(paths, resolved, option_values, targets)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        _fail(ctx, "priority_failed", str(exc))
        return
    summary, focus = _focus_list_summary(resolved, context, start_list=list_name)
    decisions = active_priority_decisions(resolved, context, focus.focus_list)[:limit]
    excluded = inactive_priority_decisions(resolved, context, focus.focus_list, talent_only=True)
    _emit(
        ctx,
        {
            "provider": "simc",
            "apl": {
                "path": str(resolved),
                "relative_to_repo": str(resolved.relative_to(paths.root)) if resolved.is_relative_to(paths.root) else None,
            },
            "build": _prune_context_payload(resolution, context),
            "priority": {
                "start_list": list_name,
                "focus_list": focus.focus_list,
                "focus_path": focus.path,
                "focus_resolution": focus.reason,
                "dispatch_certainty": "guaranteed" if summary.guaranteed_dispatch else "unresolved",
                "count": len(decisions),
                "items": [_priority_item(decision) for decision in decisions],
                "inactive_talent_branches": [
                    _priority_item(decision)
                    for decision in excluded[:limit]
                ],
                "note": "This is an exact-build static priority view. Inactive talent-gated actions are excluded from the active list.",
            },
        },
    )


@app.command("describe-build")
def describe_build_command(
    ctx: typer.Context,
    apl_path: str | None = typer.Option(
        None, "--apl-path", help="Optional APL path. If omitted, the CLI tries the default spec APL for the resolved build."),
    targets: int = typer.Option(1, "--targets", min=1, help="Primary target count for the base build summary."),
    aoe_targets: int = typer.Option(5, "--aoe-targets", min=2, help="Secondary target count used for the cleave/AoE comparison view."),
    list_name: str = typer.Option("default", "--list", help="Starting action list."),
    priority_limit: int = typer.Option(8, "--priority-limit", min=1, max=50,
                                       help="Maximum active priority rows to summarize per target view."),
    inactive_limit: int = typer.Option(8, "--inactive-limit", min=1, max=50,
                                       help="Maximum inactive talent-gated actions to summarize per target view."),
    profile_path: str | None = typer.Option(None, "--profile-path", help="Optional profile path containing build lines."),
    build_file: str | None = typer.Option(None, "--build-file", help="Optional plain text file with talents/spec lines."),
    build_packet: str | None = typer.Option(None, "--build-packet", help="Path to a talent transport packet JSON file."),
    build_text: str | None = typer.Option(
        None, "--build-text", help="Inline build text, talent hash, or Wowhead talent-calc URL with build code."),
    talents: str | None = typer.Option(
        None, "--talents", help="WoW export, Wowhead talent-calc URL with build code, SimC talents string, or talents=... line."),
    class_talents: str | None = typer.Option(None, "--class-talents", help="Split class talents string."),
    spec_talents: str | None = typer.Option(None, "--spec-talents", help="Split spec talents string."),
    hero_talents: str | None = typer.Option(None, "--hero-talents", help="Split hero talents string."),
    actor_class: str | None = typer.Option(None, "--actor-class", help="Actor class such as monk or evoker."),
    spec_name: str | None = typer.Option(None, "--spec", help="Spec name such as mistweaver."),
    enable: list[str] = typer.Option([], "--enable", help="Enabled talent names. Repeat or pass comma-separated values."),
    disable: list[str] = typer.Option([], "--disable", help="Disabled talent names. Repeat or pass comma-separated values."),
) -> None:
    paths = _repo_paths(ctx)
    build_spec, identity = _load_identified_build_spec_or_fail(
        ctx,
        paths,
        apl_path=apl_path,
        profile_path=profile_path,
        build_file=build_file,
        build_packet=build_packet,
        build_text=build_text,
        talents=talents,
        class_talents=class_talents,
        spec_talents=spec_talents,
        hero_talents=hero_talents,
        actor_class=actor_class,
        spec_name=spec_name,
    )
    if not build_spec.actor_class or not build_spec.spec:
        _fail(
            ctx,
            "invalid_query",
            "Could not determine actor class and spec for build description.",
            extra={"build_spec": _serialize_build_spec(build_spec), "identity": _serialize_build_identity(identity)},
        )
        return
    resolved = _resolve_path(paths, apl_path) if apl_path else _infer_default_apl_path(
        paths, actor_class=build_spec.actor_class, spec=build_spec.spec)
    if not resolved or not resolved.exists():
        _fail(
            ctx,
            "not_found",
            "Could not locate an APL file for the resolved build. Pass --apl-path explicitly.",
            extra={"build_spec": _serialize_build_spec(build_spec), "identity": _serialize_build_identity(identity)},
        )
        return
    option_values = _build_option_values(
        profile_path=profile_path,
        build_file=build_file,
        build_packet=build_packet,
        build_text=build_text,
        talents=talents,
        class_talents=class_talents,
        spec_talents=spec_talents,
        hero_talents=hero_talents,
        actor_class=actor_class,
        spec_name=spec_name,
        enable=enable,
        disable=disable,
    )
    try:
        primary_context, resolution = _resolve_prune_context(paths, resolved, option_values, targets)
        aoe_context, _ = _resolve_prune_context(paths, resolved, option_values, aoe_targets)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        _fail(ctx, "describe_build_failed", str(exc))
        return
    primary = _describe_target_payload(resolved, primary_context, start_list=list_name,
                                       priority_limit=priority_limit, inactive_limit=inactive_limit)
    aoe = _describe_target_payload(resolved, aoe_context, start_list=list_name,
                                   priority_limit=priority_limit, inactive_limit=inactive_limit)
    primary_actions = list(dict.fromkeys(primary.get("active_action_names") or _action_names(primary["active_priority"])))
    aoe_actions = list(dict.fromkeys(aoe.get("active_action_names") or _action_names(aoe["active_priority"])))
    _emit(
        ctx,
        {
            "provider": "simc",
            "kind": "describe_build",
            "apl": {
                "path": str(resolved),
                "relative_to_repo": _relative_to_repo(paths, resolved),
            },
            "build_spec": _serialize_build_spec(build_spec),
            "identity": _serialize_build_identity(identity),
            "build": {
                "actor_class": resolution.actor_class,
                "spec": resolution.spec,
                "source_kind": resolution.source_kind,
                "enabled_talents": sorted(resolution.enabled_talents),
                "talents_by_tree": _talent_tree_payload(resolution),
                "source_notes": resolution.source_notes,
            },
            "single_target": primary,
            "multi_target": aoe,
            "comparison": {
                "primary_targets": targets,
                "aoe_targets": aoe_targets,
                "new_active_actions_in_aoe": [action for action in aoe_actions if action not in primary_actions],
                "missing_active_actions_in_aoe": [action for action in primary_actions if action not in aoe_actions],
            },
        },
    )


@app.command("inactive-actions")
def inactive_actions_command(
    ctx: typer.Context,
    apl_path: str = typer.Argument(..., help="Path to a .simc APL file."),
    targets: int = typer.Option(1, "--targets", min=1, help="Active target count."),
    list_name: str = typer.Option("default", "--list", help="Starting action list."),
    limit: int = typer.Option(20, "--limit", min=1, max=200, help="Maximum inactive rows to return."),
    talent_only: bool = typer.Option(True, "--talent-only/--all-dead", help="Only return talent-gated dead actions by default."),
    profile_path: str | None = typer.Option(None, "--profile-path", help="Optional profile path containing build lines."),
    build_file: str | None = typer.Option(None, "--build-file", help="Optional plain text file with talents/spec lines."),
    build_text: str | None = typer.Option(None, "--build-text", help="Inline build text or talent hash."),
    talents: str | None = typer.Option(
        None, "--talents", help="WoW export, Wowhead talent-calc URL with build code, SimC talents string, or talents=... line."),
    class_talents: str | None = typer.Option(None, "--class-talents", help="Split class talents string."),
    spec_talents: str | None = typer.Option(None, "--spec-talents", help="Split spec talents string."),
    hero_talents: str | None = typer.Option(None, "--hero-talents", help="Split hero talents string."),
    actor_class: str | None = typer.Option(None, "--actor-class", help="Actor class such as monk or evoker."),
    spec_name: str | None = typer.Option(None, "--spec", help="Spec name such as mistweaver."),
    enable: list[str] = typer.Option([], "--enable", help="Enabled talent names. Repeat or pass comma-separated values."),
    disable: list[str] = typer.Option([], "--disable", help="Disabled talent names. Repeat or pass comma-separated values."),
) -> None:
    paths = _repo_paths(ctx)
    resolved = _resolve_path(paths, apl_path)
    if not resolved.exists():
        _fail(ctx, "not_found", f"APL file not found: {resolved}")
        return
    option_values = _build_option_values(
        profile_path=profile_path,
        build_file=build_file,
        build_text=build_text,
        talents=talents,
        class_talents=class_talents,
        spec_talents=spec_talents,
        hero_talents=hero_talents,
        actor_class=actor_class,
        spec_name=spec_name,
        enable=enable,
        disable=disable,
    )
    try:
        context, resolution = _resolve_prune_context(paths, resolved, option_values, targets)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        _fail(ctx, "inactive_actions_failed", str(exc))
        return
    summary, focus = _focus_list_summary(resolved, context, start_list=list_name)
    decisions = inactive_priority_decisions(resolved, context, focus.focus_list, talent_only=talent_only)
    _emit(
        ctx,
        {
            "provider": "simc",
            "apl": {
                "path": str(resolved),
                "relative_to_repo": str(resolved.relative_to(paths.root)) if resolved.is_relative_to(paths.root) else None,
            },
            "build": _prune_context_payload(resolution, context),
            "inactive_actions": {
                "start_list": list_name,
                "focus_list": focus.focus_list,
                "focus_path": focus.path,
                "focus_resolution": focus.reason,
                "dispatch_certainty": "guaranteed" if summary.guaranteed_dispatch else "unresolved",
                "talent_only": talent_only,
                "count": len(decisions),
                "items": [_priority_item(decision) for decision in decisions[:limit]],
            },
        },
    )


@app.command("opener")
def opener_command(
    ctx: typer.Context,
    apl_path: str = typer.Argument(..., help="Path to a .simc APL file."),
    targets: int = typer.Option(1, "--targets", min=1, help="Active target count."),
    list_name: str = typer.Option("default", "--list", help="Starting action list."),
    limit: int = typer.Option(10, "--limit", min=1, max=50, help="Maximum early actions to return."),
    profile_path: str | None = typer.Option(None, "--profile-path", help="Optional profile path containing build lines."),
    build_file: str | None = typer.Option(None, "--build-file", help="Optional plain text file with talents/spec lines."),
    build_text: str | None = typer.Option(None, "--build-text", help="Inline build text or talent hash."),
    talents: str | None = typer.Option(
        None, "--talents", help="WoW export, Wowhead talent-calc URL with build code, SimC talents string, or talents=... line."),
    class_talents: str | None = typer.Option(None, "--class-talents", help="Split class talents string."),
    spec_talents: str | None = typer.Option(None, "--spec-talents", help="Split spec talents string."),
    hero_talents: str | None = typer.Option(None, "--hero-talents", help="Split hero talents string."),
    actor_class: str | None = typer.Option(None, "--actor-class", help="Actor class such as monk or evoker."),
    spec_name: str | None = typer.Option(None, "--spec", help="Spec name such as mistweaver."),
    enable: list[str] = typer.Option([], "--enable", help="Enabled talent names. Repeat or pass comma-separated values."),
    disable: list[str] = typer.Option([], "--disable", help="Disabled talent names. Repeat or pass comma-separated values."),
) -> None:
    paths = _repo_paths(ctx)
    resolved = _resolve_path(paths, apl_path)
    if not resolved.exists():
        _fail(ctx, "not_found", f"APL file not found: {resolved}")
        return
    option_values = _build_option_values(
        profile_path=profile_path,
        build_file=build_file,
        build_text=build_text,
        talents=talents,
        class_talents=class_talents,
        spec_talents=spec_talents,
        hero_talents=hero_talents,
        actor_class=actor_class,
        spec_name=spec_name,
        enable=enable,
        disable=disable,
    )
    try:
        context, resolution = _resolve_prune_context(paths, resolved, option_values, targets)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        _fail(ctx, "opener_failed", str(exc))
        return
    summary, focus = _focus_list_summary(resolved, context, start_list=list_name)
    decisions = active_priority_decisions(resolved, context, focus.focus_list)[:limit]
    runtime_sensitive = [
        _priority_item(decision)
        for decision in decisions
        if decision.status == "possible" and decision.reason == "depends on runtime-only state"
    ]
    _emit(
        ctx,
        {
            "provider": "simc",
            "apl": {
                "path": str(resolved),
                "relative_to_repo": str(resolved.relative_to(paths.root)) if resolved.is_relative_to(paths.root) else None,
            },
            "build": _prune_context_payload(resolution, context),
            "opener": {
                "kind": "static_priority_preview",
                "start_list": list_name,
                "focus_list": focus.focus_list,
                "focus_path": focus.path,
                "focus_resolution": focus.reason,
                "dispatch_certainty": "guaranteed" if summary.guaranteed_dispatch else "unresolved",
                "count": len(decisions),
                "items": [_priority_item(decision) for decision in decisions],
                "runtime_sensitive": runtime_sensitive,
                "caveat": (
                    "This is a static exact-build opener preview. Use first-cast or log-actions "
                    "before treating it as a runtime-perfect opener."
                ),
            },
        },
    )


@app.command("apl-branch-compare")
def apl_branch_compare_command(
    ctx: typer.Context,
    apl_path: str = typer.Argument(..., help="Path to a .simc APL file."),
    left_targets: int = typer.Option(1, "--left-targets", min=1, help="Target count for the left context."),
    right_targets: int = typer.Option(1, "--right-targets", min=1, help="Target count for the right context."),
    list_name: str = typer.Option("default", "--list", help="Starting action list."),
    profile_path: str | None = typer.Option(None, "--profile-path", help="Optional left profile path containing build lines."),
    build_file: str | None = typer.Option(None, "--build-file", help="Optional left build file."),
    build_text: str | None = typer.Option(None, "--build-text", help="Inline left build text or talent hash."),
    talents: str | None = typer.Option(
        None, "--talents", help="Left WoW export, Wowhead talent-calc URL with build code, SimC talents string, or talents=... line."),
    class_talents: str | None = typer.Option(None, "--class-talents", help="Left split class talents string."),
    spec_talents: str | None = typer.Option(None, "--spec-talents", help="Left split spec talents string."),
    hero_talents: str | None = typer.Option(None, "--hero-talents", help="Left split hero talents string."),
    actor_class: str | None = typer.Option(None, "--actor-class", help="Left actor class such as monk or evoker."),
    spec_name: str | None = typer.Option(None, "--spec", help="Left spec name such as mistweaver."),
    enable: list[str] = typer.Option([], "--enable", help="Enabled left talent names. Repeat or pass comma-separated values."),
    disable: list[str] = typer.Option([], "--disable", help="Disabled left talent names. Repeat or pass comma-separated values."),
    right_profile_path: str | None = typer.Option(None, "--right-profile-path", help="Optional right profile path containing build lines."),
    right_build_file: str | None = typer.Option(None, "--right-build-file", help="Optional right build file."),
    right_build_text: str | None = typer.Option(None, "--right-build-text", help="Inline right build text or talent hash."),
    right_talents: str | None = typer.Option(None, "--right-talents", help="Right SimC talents string or talents=... line."),
    right_class_talents: str | None = typer.Option(None, "--right-class-talents", help="Right split class talents string."),
    right_spec_talents: str | None = typer.Option(None, "--right-spec-talents", help="Right split spec talents string."),
    right_hero_talents: str | None = typer.Option(None, "--right-hero-talents", help="Right split hero talents string."),
    right_actor_class: str | None = typer.Option(None, "--right-actor-class", help="Right actor class such as monk or evoker."),
    right_spec_name: str | None = typer.Option(None, "--right-spec", help="Right spec name such as mistweaver."),
    right_enable: list[str] = typer.Option([], "--right-enable", help="Enabled right talent names. Repeat or pass comma-separated values."),
    right_disable: list[str] = typer.Option(
        [], "--right-disable", help="Disabled right talent names. Repeat or pass comma-separated values."),
) -> None:
    paths = _repo_paths(ctx)
    resolved = _resolve_path(paths, apl_path)
    if not resolved.exists():
        _fail(ctx, "not_found", f"APL file not found: {resolved}")
        return
    left_values = _build_option_values(
        profile_path=profile_path,
        build_file=build_file,
        build_text=build_text,
        talents=talents,
        class_talents=class_talents,
        spec_talents=spec_talents,
        hero_talents=hero_talents,
        actor_class=actor_class,
        spec_name=spec_name,
        enable=enable,
        disable=disable,
    )
    right_values = _build_option_values(
        profile_path=right_profile_path if right_profile_path is not None else profile_path,
        build_file=right_build_file if right_build_file is not None else build_file,
        build_text=right_build_text if right_build_text is not None else build_text,
        talents=right_talents if right_talents is not None else talents,
        class_talents=right_class_talents if right_class_talents is not None else class_talents,
        spec_talents=right_spec_talents if right_spec_talents is not None else spec_talents,
        hero_talents=right_hero_talents if right_hero_talents is not None else hero_talents,
        actor_class=right_actor_class if right_actor_class is not None else actor_class,
        spec_name=right_spec_name if right_spec_name is not None else spec_name,
        enable=[*enable, *right_enable],
        disable=[*disable, *right_disable],
    )
    try:
        left_context, left_resolution = _resolve_prune_context(paths, resolved, left_values, left_targets)
        right_context, right_resolution = _resolve_prune_context(paths, resolved, right_values, right_targets)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        _fail(ctx, "branch_compare_failed", str(exc))
        return
    comparison = attach_focus_comparison(
        compare_branch_summaries(
            summarize_branches(resolved, left_context, start_list=list_name),
            summarize_branches(resolved, right_context, start_list=list_name),
        ),
        resolved,
        left_context,
        right_context,
    )
    _emit(
        ctx,
        {
            "provider": "simc",
            "apl": {
                "path": str(resolved),
                "relative_to_repo": str(resolved.relative_to(paths.root)) if resolved.is_relative_to(paths.root) else None,
            },
            "left": {
                "actor_class": left_resolution.actor_class,
                "spec": left_resolution.spec,
                "targets": left_context.targets,
                "enabled_talents": len(left_context.enabled_talents),
                "source_notes": left_resolution.source_notes,
            },
            "right": {
                "actor_class": right_resolution.actor_class,
                "spec": right_resolution.spec,
                "targets": right_context.targets,
                "enabled_talents": len(right_context.enabled_talents),
                "source_notes": right_resolution.source_notes,
            },
            "comparison": {
                "start_list": comparison.start_list,
                "left_dispatch": comparison.left_dispatch,
                "right_dispatch": comparison.right_dispatch,
                "dispatch_changed": comparison.dispatch_changed,
                "decision_changes": comparison.decision_changes,
                "left_focus_list": comparison.left_focus_list,
                "right_focus_list": comparison.right_focus_list,
                "focus_list_same": comparison.focus_list_same,
                "focus_changes": comparison.focus_changes,
                "left_focus_preview": comparison.left_focus_preview,
                "right_focus_preview": comparison.right_focus_preview,
                "left_focus_intent": comparison.left_focus_intent,
                "right_focus_intent": comparison.right_focus_intent,
            },
        },
    )


@app.command("analysis-packet")
def analysis_packet_command(
    ctx: typer.Context,
    apl_path: str = typer.Argument(..., help="Path to a .simc APL file."),
    targets: int = typer.Option(1, "--targets", min=1, help="Active target count."),
    list_name: str = typer.Option("default", "--list", help="Starting action list."),
    intent_limit: int = typer.Option(6, "--intent-limit", min=1, max=50, help="Number of intent lines to return."),
    explain_limit: int = typer.Option(8, "--explain-limit", min=1, max=50, help="Maximum items per explanation bucket."),
    runtime_scan_limit: int = typer.Option(8, "--runtime-scan-limit", min=1, max=50,
                                           help="How many early runtime-sensitive lines to report."),
    sim_profile: str | None = typer.Option(None, "--sim-profile", help="Optional profile path used for first-cast timing checks."),
    first_cast_action: list[str] = typer.Option([], "--first-cast-action", help="Action name to time with short sims. Repeat as needed."),
    seeds: int = typer.Option(5, "--seeds", min=1, max=100, help="Number of timing samples per first-cast action."),
    max_time: int = typer.Option(60, "--max-time", min=1, max=10000, help="Fight length for first-cast timing sims."),
    fight_style: str = typer.Option("Patchwerk", "--fight-style", help="Fight style for first-cast timing sims."),
    profile_path: str | None = typer.Option(None, "--profile-path", help="Optional profile path containing build lines."),
    build_file: str | None = typer.Option(None, "--build-file", help="Optional plain text file with talents/spec lines."),
    build_text: str | None = typer.Option(None, "--build-text", help="Inline build text or talent hash."),
    talents: str | None = typer.Option(
        None, "--talents", help="WoW export, Wowhead talent-calc URL with build code, SimC talents string, or talents=... line."),
    class_talents: str | None = typer.Option(None, "--class-talents", help="Split class talents string."),
    spec_talents: str | None = typer.Option(None, "--spec-talents", help="Split spec talents string."),
    hero_talents: str | None = typer.Option(None, "--hero-talents", help="Split hero talents string."),
    actor_class: str | None = typer.Option(None, "--actor-class", help="Actor class such as monk or evoker."),
    spec_name: str | None = typer.Option(None, "--spec", help="Spec name such as mistweaver."),
    enable: list[str] = typer.Option([], "--enable", help="Enabled talent names. Repeat or pass comma-separated values."),
    disable: list[str] = typer.Option([], "--disable", help="Disabled talent names. Repeat or pass comma-separated values."),
) -> None:
    paths = _repo_paths(ctx)
    resolved = _resolve_path(paths, apl_path)
    if not resolved.exists():
        _fail(ctx, "not_found", f"APL file not found: {resolved}")
        return
    option_values = _build_option_values(
        profile_path=profile_path,
        build_file=build_file,
        build_text=build_text,
        talents=talents,
        class_talents=class_talents,
        spec_talents=spec_talents,
        hero_talents=hero_talents,
        actor_class=actor_class,
        spec_name=spec_name,
        enable=enable,
        disable=disable,
    )
    try:
        context, resolution = _resolve_prune_context(paths, resolved, option_values, targets)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        _fail(ctx, "analysis_packet_failed", str(exc))
        return
    try:
        packet = build_analysis_packet(
            paths,
            resolved,
            context,
            start_list=list_name,
            intent_limit=intent_limit,
            explain_limit=explain_limit,
            runtime_scan_limit=runtime_scan_limit,
            first_cast_profile=sim_profile or profile_path,
            first_cast_actions=first_cast_action,
            first_cast_seeds=seeds,
            first_cast_max_time=max_time,
            first_cast_targets=targets,
            first_cast_fight_style=fight_style,
        )
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        _fail(ctx, "analysis_packet_failed", str(exc))
        return
    _emit(
        ctx,
        {
            "provider": "simc",
            "apl": {
                "path": str(packet.apl_path),
                "relative_to_repo": str(packet.apl_path.relative_to(paths.root)) if packet.apl_path.is_relative_to(paths.root) else None,
            },
            "build": {
                "actor_class": resolution.actor_class,
                "spec": resolution.spec,
                "targets": context.targets,
                "enabled_talents": len(context.enabled_talents),
                "source_notes": resolution.source_notes,
            },
            "packet": {
                "start_list": packet.start_list,
                "focus_list": packet.focus_list,
                "dispatch_certainty": packet.dispatch_certainty,
                "top_level_runtime_unresolved": packet.top_level_runtime_unresolved,
                "runtime_sensitive_priorities": packet.runtime_sensitive_priorities,
                "escalation_reasons": packet.escalation_reasons,
                "next_steps": packet.next_steps,
                "intent_lines": packet.intent_lines,
                "explained_intent": {
                    "setup": packet.explained_intent.setup,
                    "helpers": packet.explained_intent.helpers,
                    "burst": packet.explained_intent.burst,
                    "priorities": packet.explained_intent.priorities,
                },
                "first_casts": [
                    {
                        "action": item.action,
                        "samples": item.samples,
                        "found": item.found,
                        "min_time": item.min_time,
                        "avg_time": item.avg_time,
                        "max_time": item.max_time,
                        "results": [
                            {
                                "seed": result.seed,
                                "time": result.time,
                                "log_path": str(result.log_path),
                            }
                            for result in item.results
                        ],
                    }
                    for item in packet.first_casts
                ],
                "branch_summary": {
                    "start_list": packet.branch_summary.start_list,
                    "guaranteed_dispatch": packet.branch_summary.guaranteed_dispatch,
                    "guaranteed_dispatch_line": packet.branch_summary.guaranteed_dispatch_line,
                    "guaranteed_dispatch_reason": packet.branch_summary.guaranteed_dispatch_reason,
                    "dead_branches": packet.branch_summary.dead_branches,
                    "unresolved_branches": packet.branch_summary.unresolved_branches,
                    "shadowed_lines": packet.branch_summary.shadowed_lines,
                },
            },
        },
    )


@app.command("first-cast")
def first_cast_command(
    ctx: typer.Context,
    profile_path: str = typer.Argument(..., help="Path to a SimulationCraft profile to execute."),
    action: str = typer.Argument(..., help="Action name to time."),
    seeds: int = typer.Option(5, "--seeds", min=1, max=100, help="Number of timing samples."),
    max_time: int = typer.Option(60, "--max-time", min=1, max=10000, help="Fight length for each short sim."),
    targets: int = typer.Option(1, "--targets", min=1, help="Active target count."),
    fight_style: str = typer.Option("Patchwerk", "--fight-style", help="Fight style for the short sims."),
) -> None:
    paths = _repo_paths(ctx)
    resolved = Path(profile_path).expanduser().resolve()
    if not resolved.exists():
        _fail(ctx, "not_found", f"Profile not found: {resolved}")
        return
    try:
        results = run_first_casts(paths, resolved, action, seeds, max_time, targets, fight_style)
    except (FileNotFoundError, RuntimeError) as exc:
        _fail(ctx, "first_cast_failed", str(exc))
        return
    summary = summarize_first_casts(results)
    _emit(
        ctx,
        {
            "provider": "simc",
            "profile_path": str(resolved),
            "action": action,
            "targets": targets,
            "fight_style": fight_style,
            "seeds": seeds,
            "summary": summary,
            "results": [
                {
                    "seed": result.seed,
                    "time": result.time,
                    "log_path": str(result.log_path),
                }
                for result in results
            ],
        },
    )


@app.command("log-actions")
def log_actions_command(
    ctx: typer.Context,
    log_path: str = typer.Argument(..., help="Path to a SimulationCraft combat log."),
    actions: list[str] = typer.Argument(..., help="One or more action names to inspect."),
) -> None:
    resolved = Path(log_path).expanduser().resolve()
    if not resolved.exists():
        _fail(ctx, "not_found", f"Log file not found: {resolved}")
        return
    hits = first_action_hits(resolved, list(actions))
    _emit(
        ctx,
        {
            "provider": "simc",
            "log_path": str(resolved),
            "actions": list(actions),
            "count": len(hits),
            "hits": [
                {
                    "action": hit.action,
                    "scheduled_at": hit.scheduled_at,
                    "performed_at": hit.performed_at,
                }
                for hit in hits
            ],
        },
    )


@app.command("sync")
def sync(
    ctx: typer.Context,
    allow_dirty: bool = typer.Option(False, "--allow-dirty", help="Allow git pull even if the repo has local changes."),
) -> None:
    paths = _repo_paths(ctx)
    if not paths.root.exists():
        _fail(ctx, "missing_repo", f"SimulationCraft repo not found: {paths.root}")
        return
    result = sync_repo(paths, allow_dirty=allow_dirty)
    git_status = repo_git_status(paths)
    if result is None:
        _emit(
            ctx,
            {
                "provider": "simc",
                "status": "skipped",
                "reason": "dirty_worktree",
                "repo": str(paths.root),
                "git": git_status,
            },
        )
        return
    stdout_preview, stdout_truncated = _preview_text(result.stdout)
    stderr_preview, stderr_truncated = _preview_text(result.stderr)
    if result.returncode != 0:
        _fail(
            ctx,
            "sync_failed",
            "SimulationCraft git sync failed.",
            extra={
                "command": result.command,
                "stdout_preview": stdout_preview,
                "stdout_truncated": stdout_truncated,
                "stderr_preview": stderr_preview,
                "stderr_truncated": stderr_truncated,
            },
        )
        return
    _emit(
        ctx,
        {
            "provider": "simc",
            "status": "updated",
            "repo": str(paths.root),
            "command": result.command,
            "git": repo_git_status(paths),
            "stdout_preview": stdout_preview,
            "stdout_truncated": stdout_truncated,
            "stderr_preview": stderr_preview,
            "stderr_truncated": stderr_truncated,
        },
    )


@app.command("build")
def build(
    ctx: typer.Context,
    target: str | None = typer.Option(None, "--target", help="Optional build target passed to cmake."),
) -> None:
    paths = _repo_paths(ctx)
    if not paths.build_dir.exists():
        _fail(ctx, "missing_build_dir", f"SimulationCraft build dir not found: {paths.build_dir}")
        return
    result = build_repo(paths, target=target)
    stdout_preview, stdout_truncated = _preview_text(result.stdout)
    stderr_preview, stderr_truncated = _preview_text(result.stderr)
    if result.returncode != 0:
        _fail(
            ctx,
            "build_failed",
            "SimulationCraft build failed.",
            extra={
                "command": result.command,
                "stdout_preview": stdout_preview,
                "stdout_truncated": stdout_truncated,
                "stderr_preview": stderr_preview,
                "stderr_truncated": stderr_truncated,
            },
        )
        return
    _emit(
        ctx,
        {
            "provider": "simc",
            "status": "built",
            "command": result.command,
            "stdout_preview": stdout_preview,
            "stdout_truncated": stdout_truncated,
            "stderr_preview": stderr_preview,
            "stderr_truncated": stderr_truncated,
        },
    )


@app.command("sim")
def sim_command(
    ctx: typer.Context,
    profile_path: str | None = typer.Argument(None, help="Path to a SimulationCraft profile. Omit or pass '-' to read from stdin."),
    preset: str = typer.Option("quick", "--preset", help="Run preset: quick or high-accuracy."),
    iterations: int | None = typer.Option(None, "--iterations", min=1, help="Override the preset iteration count."),
    max_time: int | None = typer.Option(None, "--max-time", min=1, help="Override max fight length in seconds."),
    fight_style: str | None = typer.Option(None, "--fight-style", help="Optional fight style override."),
    threads: int | None = typer.Option(None, "--threads", min=1, help="Optional thread override. Leave unset to use SimC defaults."),
    targets: int | None = typer.Option(None, "--targets", min=1, help="Optional desired target count override."),
    vary_combat_length: float | None = typer.Option(None, "--vary-combat-length", min=0.0,
                                                    help="Optional combat length variance override."),
    profile_text: str | None = typer.Option(None, "--profile-text", help="Inline SimulationCraft profile text."),
    json_out: str | None = typer.Option(None, "--json-out", help="Optional path for the raw SimC JSON report."),
) -> None:
    if preset not in {"quick", "high-accuracy"}:
        _fail(ctx, "invalid_preset", f"Unsupported sim preset: {preset}")
        return
    paths = _repo_paths(ctx)
    default_iterations, default_max_time = _sim_preset_settings(preset=preset)
    requested_iterations = iterations or default_iterations
    requested_max_time = max_time or default_max_time

    input_source = "file"
    resolved_profile: Path
    cleanup_paths: list[Path] = []
    if profile_text is not None:
        resolved_profile = _write_temp_profile(source_name="simc-profile-text", text=profile_text)
        cleanup_paths.append(resolved_profile)
        input_source = "profile_text"
    elif profile_path in {None, "-"}:
        stdin_text = sys.stdin.read()
        if not stdin_text.strip():
            _fail(ctx, "missing_profile", "Provide a profile path, --profile-text, or pipe a profile into stdin.")
            return
        resolved_profile = _write_temp_profile(source_name="simc-stdin", text=stdin_text)
        cleanup_paths.append(resolved_profile)
        input_source = "stdin"
    else:
        resolved_profile = Path(profile_path).expanduser().resolve()
        if not resolved_profile.exists():
            _fail(ctx, "not_found", f"Profile not found: {resolved_profile}")
            return

    if json_out is not None:
        json_path = Path(json_out).expanduser().resolve()
        json_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        fd, raw_json_path = tempfile.mkstemp(suffix=".json", prefix="simc-run-")
        os.close(fd)
        json_path = Path(raw_json_path).resolve()
        cleanup_paths.append(json_path)

    simc_args = [
        f"iterations={requested_iterations}",
        "target_error=0",
        f"max_time={requested_max_time}",
        f"json2={json_path}",
    ]
    if fight_style:
        simc_args.append(f"fight_style={fight_style}")
    if threads is not None:
        simc_args.append(f"threads={threads}")
    if targets is not None:
        simc_args.append(f"desired_targets={targets}")
    if vary_combat_length is not None:
        simc_args.append(f"vary_combat_length={vary_combat_length}")

    result = run_profile(paths, resolved_profile, simc_args=simc_args)
    stdout_preview, stdout_truncated = _preview_text(result.stdout)
    stderr_preview, stderr_truncated = _preview_text(result.stderr)
    if result.returncode != 0:
        for path in cleanup_paths:
            path.unlink(missing_ok=True)
        _fail(
            ctx,
            "run_failed",
            "SimulationCraft sim failed.",
            extra={
                "command": result.command,
                "stdout_preview": stdout_preview,
                "stdout_truncated": stdout_truncated,
                "stderr_preview": stderr_preview,
                "stderr_truncated": stderr_truncated,
            },
        )
        return

    try:
        report = load_sim_report(json_path)
        summary = summarize_sim_report(report)
    except Exception as exc:
        for path in cleanup_paths:
            path.unlink(missing_ok=True)
        _fail(
            ctx,
            "invalid_report",
            f"SimulationCraft sim completed but the JSON report could not be parsed: {exc}",
            extra={"command": result.command, "json_report_path": str(json_path)},
        )
        return

    payload = sim_report_payload(
        summary,
        profile_path=str(resolved_profile) if input_source == "file" else None,
        preset=preset,
        input_source=input_source,
        json_report_path=str(json_path) if json_out is not None else None,
        command=result.command,
    )
    _emit(ctx, payload)
    for path in cleanup_paths:
        path.unlink(missing_ok=True)


def _tree_diff_payload(diff: TreeDiff) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "added": [
            {"name": t.name, "token": t.token, "rank": t.rank, "max_rank": t.max_rank, "entry": t.entry}
            for t in diff.added
        ],
        "removed": [
            {"name": t.name, "token": t.token, "rank": t.rank, "max_rank": t.max_rank, "entry": t.entry}
            for t in diff.removed
        ],
        "changed": [
            {
                "name": base.name,
                "token": base.token,
                "entry": base.entry,
                "base_rank": base.rank,
                "other_rank": other.rank,
                "max_rank": base.max_rank,
            }
            for base, other in diff.changed
        ],
    }
    payload["has_differences"] = bool(payload["added"] or payload["removed"] or payload["changed"])
    return payload


@app.command("compare-builds")
def compare_builds_command(
    ctx: typer.Context,
    base: str = typer.Option(..., "--base", help="Base build: WoW export, Wowhead talent-calc URL with build code, or talents=... line."),
    other: list[str] = typer.Option(..., "--other", help="Build to compare against base. Repeat for multiple builds."),
    tree: list[str] = typer.Option([], "--tree", help="Limit diff to specific trees (class, spec, hero). Omit for all."),
    actor_class: str | None = typer.Option(None, "--actor-class", help="Actor class such as druid."),
    spec_name: str | None = typer.Option(None, "--spec", help="Spec name such as balance."),
) -> None:
    paths = _repo_paths(ctx)
    trees = [t for t in tree] or ["class", "spec", "hero"]

    base_spec, base_identity = _load_identified_build_spec_or_fail(
        ctx,
        paths,
        apl_path=None,
        profile_path=None,
        build_file=None,
        build_text=None,
        talents=base,
        class_talents=None,
        spec_talents=None,
        hero_talents=None,
        actor_class=actor_class,
        spec_name=spec_name,
    )
    if not base_spec.actor_class or not base_spec.spec:
        _fail(ctx, "invalid_query", "Could not identify actor class and spec for base build.",
              extra={"build_spec": _serialize_build_spec(base_spec), "identity": _serialize_build_identity(base_identity)})
        return
    try:
        base_resolution = decode_build(paths, base_spec)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        _fail(ctx, "decode_failed", f"Failed to decode base build: {exc}")
        return

    comparisons: list[dict[str, Any]] = []
    for other_talents in other:
        try:
            other_spec = load_build_spec(
                apl_path=None, profile_path=None, build_file=None, build_text=None,
                talents=other_talents, class_talents=None, spec_talents=None, hero_talents=None,
                actor_class=base_spec.actor_class, spec_name=base_spec.spec,
            )
        except ValueError as exc:
            comparisons.append({"input": other_talents, "error": str(exc)})
            continue
        try:
            other_resolution = decode_build(paths, other_spec)
        except (FileNotFoundError, RuntimeError, ValueError) as exc:
            comparisons.append({"input": other_talents, "error": str(exc)})
            continue
        tree_diffs: dict[str, Any] = {}
        for t in trees:
            diff = diff_talent_trees(
                base_resolution.talents_by_tree.get(t, []),
                other_resolution.talents_by_tree.get(t, []),
            )
            tree_diffs[t] = _tree_diff_payload(diff)
        has_any = any(tree_diffs[t]["has_differences"] for t in trees)
        comparisons.append({
            "input": other_talents,
            "trees": tree_diffs,
            "has_differences": has_any,
        })

    _emit(ctx, {
        "provider": "simc",
        "kind": "compare_builds",
        "base": {
            "input": base,
            "actor_class": base_resolution.actor_class,
            "spec": base_resolution.spec,
            "enabled_talents": sorted(base_resolution.enabled_talents),
        },
        "trees_compared": trees,
        "comparisons": comparisons,
    })


def _resolve_modify_tree_entries(
    ctx: typer.Context,
    paths: RepoPaths,
    *,
    base_spec: BuildSpec,
    base_resolution: BuildResolution,
    swaps: list[tuple[str, str | None]],
    modifications: list[str],
) -> tuple[str | None, str | None, str | None]:
    class_entries: str | None = None
    spec_entries: str | None = None
    hero_entries: str | None = None
    for tree_name, swap_source in swaps:
        if not swap_source:
            continue
        swap_spec, _ = _load_identified_build_spec_or_fail(
            ctx,
            paths,
            apl_path=None,
            profile_path=None,
            build_file=None,
            build_text=None,
            talents=swap_source,
            class_talents=None,
            spec_talents=None,
            hero_talents=None,
            actor_class=base_spec.actor_class,
            spec_name=base_spec.spec,
        )
        try:
            swap_resolution = decode_build(paths, swap_spec)
        except (FileNotFoundError, RuntimeError, ValueError) as exc:
            _fail(ctx, "decode_failed", f"Failed to decode {tree_name} tree source: {exc}")
            return None, None, None
        entries_str = tree_entries_string(swap_resolution.talents_by_tree.get(tree_name, []))
        if tree_name == "class":
            class_entries = entries_str
        elif tree_name == "spec":
            spec_entries = entries_str
        else:
            hero_entries = entries_str
        modifications.append(f"swap_{tree_name}_tree")

    # Build the per-tree entry strings for trees that are NOT being swapped,
    # pulling from the base build.
    if any([class_entries, spec_entries, hero_entries]):
        if class_entries is None:
            class_entries = tree_entries_string(base_resolution.talents_by_tree.get("class", []))
        if spec_entries is None:
            spec_entries = tree_entries_string(base_resolution.talents_by_tree.get("spec", []))
        if hero_entries is None:
            hero_entries = tree_entries_string(base_resolution.talents_by_tree.get("hero", []))
    return class_entries, spec_entries, hero_entries


def _build_modify_overrides(
    ctx: typer.Context,
    *,
    base_resolution: BuildResolution,
    add: list[str],
    remove: list[str],
    modifications: list[str],
) -> list[str]:
    # SimC's parse_traits accepts both entry_id:rank and talent_name:rank,
    # so we pass values through directly and let SimC resolve names.
    overrides: list[str] = []
    # For --remove by name, we need to resolve to entry:0 using the base build
    # since SimC's rank-0 override requires an entry ID or known name.
    token_to_entry: dict[str, int] = {}
    for tree_talents in base_resolution.talents_by_tree.values():
        for t in tree_talents:
            if t.entry:
                token_to_entry[t.token] = t.entry
                token_to_entry[t.name.lower()] = t.entry

    for item in remove:
        token = item.strip()
        if token.isdigit():
            overrides.append(f"{token}:0")
        elif token.lower() in token_to_entry:
            overrides.append(f"{token_to_entry[token.lower()]}:0")
        else:
            msg = (
                f"Cannot resolve talent to remove: '{token}'. "
                "Use an entry ID or a name from the base build."
            )
            _fail(ctx, "unknown_talent", msg)
            return overrides
        modifications.append(f"remove:{token}")

    for item in add:
        parts = item.strip().split(":", 1)
        if len(parts) != 2:
            _fail(
                ctx, "invalid_add",
                f"--add requires 'name:rank' or 'entry_id:rank', got: '{item}'",
            )
            return overrides
        name_or_id, rank_str = parts
        if not rank_str.isdigit():
            _fail(ctx, "invalid_add", f"Rank must be a number in '{item}'")
            return overrides
        # Pass through as-is — SimC resolves both entry IDs and talent names.
        overrides.append(f"{name_or_id}:{rank_str}")
        modifications.append(f"add:{item}")
    return overrides


def _assemble_modified_spec(
    base_spec: BuildSpec,
    *,
    class_entries: str | None,
    spec_entries: str | None,
    hero_entries: str | None,
    overrides: list[str],
) -> BuildSpec | None:
    if class_entries is not None:
        # Tree-swap path: build from split trees.
        override_suffix = "/" + "/".join(overrides) if overrides else ""
        return BuildSpec(
            actor_class=base_spec.actor_class,
            spec=base_spec.spec,
            class_talents=class_entries + override_suffix if override_suffix else class_entries,
            spec_talents=spec_entries,
            hero_talents=hero_entries,
        )
    if overrides:
        # Individual override path: keep base talents, append overrides.
        return BuildSpec(
            actor_class=base_spec.actor_class,
            spec=base_spec.spec,
            talents=base_spec.talents,
            class_talents="/".join(overrides),
        )
    return None


def _modify_build_diff_payload(
    paths: RepoPaths,
    *,
    base_spec: BuildSpec,
    base_resolution: BuildResolution,
    encoded: str,
) -> dict[str, Any]:
    # Decode the result to produce the diff and verify.
    verify_spec = BuildSpec(
        actor_class=base_spec.actor_class,
        spec=base_spec.spec,
        talents=encoded,
    )
    try:
        result_resolution = decode_build(paths, verify_spec)
    except (FileNotFoundError, RuntimeError, ValueError):
        result_resolution = None

    diff_payload: dict[str, Any] = {}
    if result_resolution:
        for t in ("class", "spec", "hero"):
            diff = diff_talent_trees(
                base_resolution.talents_by_tree.get(t, []),
                result_resolution.talents_by_tree.get(t, []),
            )
            diff_payload[t] = _tree_diff_payload(diff)
    return diff_payload


@app.command("modify-build")
def modify_build_command(
    ctx: typer.Context,
    talents: str = typer.Option(..., "--talents",
                                help="Base build: WoW export, Wowhead talent-calc URL with build code, or talents=... line."),
    swap_class_tree_from: str | None = typer.Option(
        None, "--swap-class-tree-from", help="Replace class tree from this build.",
    ),
    swap_spec_tree_from: str | None = typer.Option(
        None, "--swap-spec-tree-from", help="Replace spec tree from this build.",
    ),
    swap_hero_tree_from: str | None = typer.Option(
        None, "--swap-hero-tree-from", help="Replace hero tree from this build.",
    ),
    add: list[str] = typer.Option([], "--add", help="Add or set talent: 'name:rank' or 'entry_id:rank'. Repeat as needed."),
    remove: list[str] = typer.Option([], "--remove", help="Remove talent by name or entry_id. Repeat as needed."),
    actor_class: str | None = typer.Option(None, "--actor-class", help="Actor class such as druid."),
    spec_name: str | None = typer.Option(None, "--spec", help="Spec name such as balance."),
) -> None:
    paths = _repo_paths(ctx)

    base_spec, base_identity = _load_identified_build_spec_or_fail(
        ctx,
        paths,
        apl_path=None,
        profile_path=None,
        build_file=None,
        build_text=None,
        talents=talents,
        class_talents=None,
        spec_talents=None,
        hero_talents=None,
        actor_class=actor_class,
        spec_name=spec_name,
    )
    if not base_spec.actor_class or not base_spec.spec:
        _fail(ctx, "invalid_query", "Could not identify actor class and spec for base build.",
              extra={"build_spec": _serialize_build_spec(base_spec), "identity": _serialize_build_identity(base_identity)})
        return

    try:
        base_resolution = decode_build(paths, base_spec)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        _fail(ctx, "decode_failed", f"Failed to decode base build: {exc}")
        return

    modifications: list[str] = []
    class_entries, spec_entries, hero_entries = _resolve_modify_tree_entries(
        ctx,
        paths,
        base_spec=base_spec,
        base_resolution=base_resolution,
        swaps=[
            ("class", swap_class_tree_from),
            ("spec", swap_spec_tree_from),
            ("hero", swap_hero_tree_from),
        ],
        modifications=modifications,
    )
    overrides = _build_modify_overrides(
        ctx,
        base_resolution=base_resolution,
        add=add,
        remove=remove,
        modifications=modifications,
    )

    modified_spec = _assemble_modified_spec(
        base_spec,
        class_entries=class_entries,
        spec_entries=spec_entries,
        hero_entries=hero_entries,
        overrides=overrides,
    )
    if modified_spec is None:
        _fail(
            ctx, "no_modifications",
            "No modifications specified. Use --swap-*-tree-from, --add, or --remove.",
        )
        return

    try:
        encoded = encode_build(paths, modified_spec)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        _fail(ctx, "encode_failed", f"Failed to encode modified build: {exc}")
        return

    diff_payload = _modify_build_diff_payload(
        paths, base_spec=base_spec, base_resolution=base_resolution, encoded=encoded
    )

    wowhead_url = f"https://www.wowhead.com/talent-calc/blizzard/{encoded}"

    _emit(ctx, {
        "provider": "simc",
        "kind": "modify_build",
        "base": {
            "input": talents,
            "actor_class": base_resolution.actor_class,
            "spec": base_resolution.spec,
        },
        "modifications": modifications,
        "result": {
            "talents_export": encoded,
            "wowhead_url": wowhead_url,
            "diff_from_base": diff_payload,
        },
    })


@app.command("run")
def run_command(
    ctx: typer.Context,
    profile_path: str = typer.Argument(..., help="Path to a SimulationCraft profile to execute."),
    simc_arg: list[str] = typer.Option([], "--arg", help="Additional raw SimulationCraft arg, repeatable."),
) -> None:
    paths = _repo_paths(ctx)
    resolved = Path(profile_path).expanduser().resolve()
    if not resolved.exists():
        _fail(ctx, "not_found", f"Profile not found: {resolved}")
        return
    result = run_profile(paths, resolved, simc_args=list(simc_arg))
    stdout_preview, stdout_truncated = _preview_text(result.stdout)
    stderr_preview, stderr_truncated = _preview_text(result.stderr)
    version_line = binary_version(paths).version_line
    if result.returncode != 0:
        _fail(
            ctx,
            "run_failed",
            "SimulationCraft run failed.",
            extra={
                "command": result.command,
                "stdout_preview": stdout_preview,
                "stdout_truncated": stdout_truncated,
                "stderr_preview": stderr_preview,
                "stderr_truncated": stderr_truncated,
                "version": version_line,
            },
        )
        return
    _emit(
        ctx,
        {
            "provider": "simc",
            "status": "completed",
            "profile_path": str(resolved),
            "command": result.command,
            "version": version_line,
            "stdout_preview": stdout_preview,
            "stdout_truncated": stdout_truncated,
            "stderr_preview": stderr_preview,
            "stderr_truncated": stderr_truncated,
        },
    )


def run() -> None:
    app()


if __name__ == "__main__":
    run()

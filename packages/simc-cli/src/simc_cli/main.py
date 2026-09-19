from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer
from warcraft_core.cli import (
    CompactMaxCharsOption,
    CompactOption,
    FieldsOption,
    FieldsStrictOption,
    PrettyOption,
    ProfileOption,
    RuntimeConfig,
    cfg_as,
    configure,
    emit,
    fail,
    guarded_run,
)
from warcraft_core.identity import build_identity_payload, refresh_talent_transport_packet, validate_talent_transport_packet
from warcraft_core.output import DEFAULT_COMPACT_MAX_CHARS
from warcraft_core.talent_transport import tokenize_talent_name

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
    SimcBuildError,
    TalentStrings,
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
from simc_cli.packet import FirstCastOptions, build_analysis_packet
from simc_cli.provider import PROVIDER, PROVIDER_NAME, repo_payload, simc_envelope
from simc_cli.prune import PruneContext, prune_entries, split_csv_values
from simc_cli.repo import (
    RepoPaths,
    checkout_managed_repo,
    clear_configured_repo_root,
    discover_repo,
    resolve_repo_root,
    save_configured_repo_root,
    validate_repo,
)
from simc_cli.report import load_sim_report, sim_report_payload, summarize_sim_report
from simc_cli.run import binary_provenance, binary_version, build_repo, repo_git_status, run_profile, sync_repo
from simc_cli.search import MissingRipgrepError, find_action, spec_file_search
from simc_cli.sim import first_action_hits, run_first_casts, summarize_first_casts
from simc_cli.talent_transport import validate_talent_tree_transport
from simc_cli.trait_data import TraitTable, load_trait_table

app = typer.Typer(add_completion=False, help="SimulationCraft local workflow CLI.")


@dataclass(slots=True)
class SimcConfig(RuntimeConfig):
    """Shared runtime config plus simc's global --repo-root override."""

    repo_root: str | None = None


def _is_transport_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _cfg(ctx: typer.Context) -> SimcConfig:
    """Narrow the shared config to simc's subclass; the callback always installs it."""
    return cfg_as(ctx, SimcConfig)


def _emit(ctx: typer.Context, payload: dict[str, Any]) -> None:
    """Emit the shared success envelope, keeping the deprecated flat payload keys at the top level."""
    emit(ctx, simc_envelope(ctx.info_name or "", payload))


def _write_packet_json_or_fail(ctx: typer.Context, *, out: str | None, packet: dict[str, Any]) -> str | None:
    if not out:
        return None
    try:
        output_path = Path(out).expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(packet, indent=2) + "\n")
        return str(output_path)
    except OSError as exc:
        fail(ctx, "transport_packet_write_failed", f"Failed to write talent transport packet: {exc}")


def _repo_paths(ctx: typer.Context) -> RepoPaths:
    return discover_repo(_cfg(ctx).repo_root)


def _repo_resolution(ctx: typer.Context):
    return resolve_repo_root(_cfg(ctx).repo_root)


def _preview_text(text: str, *, max_lines: int = 20) -> tuple[list[str], bool]:
    lines = text.splitlines()
    return lines[:max_lines], len(lines) > max_lines


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
    talents: TalentStrings,
    actor_class: str | None,
    spec_name: str | None,
    enable: list[str] | None = None,
    disable: list[str] | None = None,
    build_packet: str | None = None,
) -> dict[str, Any]:
    """Pack the shared build-input flag group so wide commands can hand it to one plain function."""
    return {
        "profile_path": profile_path,
        "build_file": build_file,
        "build_packet": build_packet,
        "build_text": build_text,
        "talents": talents,
        "actor_class": actor_class,
        "spec_name": spec_name,
        "enable": enable or [],
        "disable": disable or [],
    }


def _require_checkout(ctx: typer.Context, paths: RepoPaths) -> None:
    """Reject a search over a checkout that is not there; it otherwise reports zero hits as success."""
    missing = validate_repo(paths)
    if missing:
        fail(
            ctx,
            "not_found",
            f"SimulationCraft checkout is missing or incomplete: {'; '.join(missing)}. "
            "Run 'simc checkout', or point --repo-root at a checkout.",
            details={"repo_root": str(paths.root), "missing": missing},
        )


def _require_apl_path(ctx: typer.Context, paths: RepoPaths, apl_path: str | None) -> None:
    """Reject an --apl-path that does not exist: its file stem otherwise invents a class and spec."""
    if apl_path is None:
        return
    resolved = _resolve_path(paths, apl_path)
    if not resolved.exists():
        fail(ctx, "not_found", f"APL file not found: {resolved}")


def _identified_build_or_fail(
    ctx: typer.Context, paths: RepoPaths, *, apl_path: str | None, option_values: dict[str, Any]
) -> tuple[Any, Any]:
    _require_apl_path(ctx, paths, apl_path)
    return _load_identified_build_spec_or_fail(
        ctx,
        paths,
        apl_path=apl_path,
        profile_path=option_values["profile_path"],
        build_file=option_values["build_file"],
        build_packet=option_values["build_packet"],
        build_text=option_values["build_text"],
        talents=option_values["talents"],
        actor_class=option_values["actor_class"],
        spec_name=option_values["spec_name"],
    )


def _resolve_prune_context(paths: RepoPaths, apl_path: Path, option_values: dict[str, Any], targets: int) -> tuple[PruneContext, Any]:
    unresolved_spec = load_build_spec(
        apl_path=apl_path,
        profile_path=option_values["profile_path"],
        build_file=option_values["build_file"],
        build_packet=option_values["build_packet"],
        build_text=option_values["build_text"],
        talents=option_values["talents"],
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
    talents: TalentStrings,
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
    talents: TalentStrings,
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
            actor_class=actor_class,
            spec_name=spec_name,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        if build_packet:
            fail(ctx, "invalid_build_packet", str(exc))
        fail(ctx, "invalid_query", str(exc))


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
    return {
        tree: {
            "selected": [_talent_row(talent) for talent in resolution.talents_by_tree.get(tree, []) if talent.taken],
            "skipped": [_talent_row(talent) for talent in resolution.talents_by_tree.get(tree, []) if not talent.taken],
        }
        for tree in ("class", "spec", "hero")
    }


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
    """Name each priority row, keeping the target list on dispatch rows.

    Every ``call_action_list``/``run_action_list`` row would otherwise collapse to the same name, so
    a build that dispatches to a different list at another target count would look unchanged.
    """
    names: list[str] = []
    for item in items:
        action = item.get("action")
        if not action:
            continue
        target_list = item.get("target_list")
        names.append(f"{action} -> {target_list}" if target_list else str(action))
    return names


def _parse_variant_specs(values: list[str]) -> list[tuple[str, str | Path]]:
    specs: list[tuple[str, str | Path]] = []
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
    pretty: PrettyOption = False,
    compact: CompactOption = False,
    fields: FieldsOption = None,
    fields_strict: FieldsStrictOption = False,
    profile: ProfileOption = None,
    compact_max_chars: CompactMaxCharsOption = DEFAULT_COMPACT_MAX_CHARS,
    repo_root: Annotated[
        str | None, typer.Option("--repo-root", help="Override the local SimulationCraft checkout path.")
    ] = None,
) -> None:
    """Inspect, decode, and simulate builds against a local SimulationCraft checkout."""
    configure(
        ctx,
        provider=PROVIDER_NAME,
        pretty=pretty,
        compact=compact,
        fields=fields,
        fields_strict=fields_strict,
        profile=profile,
        compact_max_chars=compact_max_chars,
        config=SimcConfig(repo_root=repo_root),
    )


@app.command("doctor")
def doctor(ctx: typer.Context) -> None:
    """Report SimulationCraft repo readiness, binary version, and per-command capabilities."""
    emit(ctx, PROVIDER.doctor(repo_root=_cfg(ctx).repo_root))


@app.command("repo")
def repo_command(
    ctx: typer.Context,
    set_root: str | None = typer.Option(None, "--set-root", help="Persist an explicit SimulationCraft repo root."),
    clear_root: bool = typer.Option(False, "--clear-root", help="Clear the persisted explicit SimulationCraft repo root."),
) -> None:
    """Show or change which local SimulationCraft checkout the CLI uses."""
    if set_root and clear_root:
        fail(ctx, "invalid_query", "Use either --set-root or --clear-root, not both.")
    changed = False
    action = "inspect"
    stored_root: str | None = None
    if set_root:
        resolved = Path(set_root).expanduser().resolve()
        if not resolved.exists():
            fail(ctx, "not_found", f"Repo root not found: {resolved}")
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
    """Clone or update the managed SimulationCraft checkout."""
    try:
        result = checkout_managed_repo()
    except RuntimeError as exc:
        fail(ctx, "checkout_failed", str(exc))
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
    """Return the structured coming-soon stub for free-text search."""
    emit(ctx, PROVIDER.search(query, limit=limit, repo_root=_cfg(ctx).repo_root))


@app.command("resolve")
def resolve(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="Free-text query. Structured resolution is deferred for simc phase 1."),
    limit: int = typer.Option(5, "--limit", min=1, max=50, help="Unused in phase 1."),
) -> None:
    """Return the structured coming-soon stub for free-text resolution."""
    del limit
    emit(ctx, PROVIDER.resolve(query, repo_root=_cfg(ctx).repo_root))


@app.command("version")
def version(ctx: typer.Context) -> None:
    """Report the version reported by the local SimC binary."""
    version_info = binary_version(_repo_paths(ctx))
    if not version_info.available:
        fail(ctx, "missing_binary", f"SimC binary not found: {version_info.binary_path}")
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
    """Describe the repo, or one file inside it, including any build lines it carries."""
    paths = _repo_paths(ctx)
    if target is None:
        _emit(ctx, {"provider": "simc", "inspect": "repo", "repo": repo_payload(paths)})
        return
    resolved = Path(target).expanduser().resolve()
    if not resolved.exists():
        fail(ctx, "not_found", f"Inspect target not found: {resolved}")
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
    """List APL and class-module files in the checkout, optionally narrowed by a substring."""
    paths = _repo_paths(ctx)
    _require_checkout(ctx, paths)
    try:
        matches = spec_file_search(paths, query)
    except MissingRipgrepError as exc:
        fail(ctx, "missing_dependency", str(exc))
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


def _talent_row(talent: Any) -> dict[str, Any]:
    return {
        "name": talent.name,
        "token": talent.token,
        "entry": talent.entry,
        # SimC prints the leftover rank (0) for a tiered node decoded from a hash, so the talent is
        # taken but its rank is not recoverable from the decode output.
        "rank": talent.rank if talent.rank_known else None,
        "rank_known": talent.rank_known,
        "max_rank": talent.max_rank,
    }


def _hero_tree_payload(resolution: BuildResolution) -> dict[str, Any]:
    """The hero tree SimC activated, and the keystones of the tree it disabled."""
    return {
        "hero_tree": {"name": resolution.hero_tree.name, "id": resolution.hero_tree.id} if resolution.hero_tree else None,
        "inactive_hero_talents": [_talent_row(talent) for talent in resolution.inactive_hero_talents],
    }


def _decoded_payload(resolution: BuildResolution) -> dict[str, Any]:
    return {
        "actor_class": resolution.actor_class,
        "spec": resolution.spec,
        "source_kind": resolution.source_kind,
        "generated_profile": resolution.generated_profile_text,
        "enabled_talents": sorted(resolution.enabled_talents),
        **_hero_tree_payload(resolution),
        "talents_by_tree": {
            tree: [_talent_row(talent) for talent in talents]
            for tree, talents in resolution.talents_by_tree.items()
        },
        "source_notes": resolution.source_notes,
    }


def _fail_build_error(
    ctx: typer.Context, exc: Exception, *, code: str, prefix: str = "", details: dict[str, Any] | None = None
) -> NoReturn:
    """Report SimC's rejection of a build as ``invalid_build`` carrying only its error line.

    SimC is run with ``debug=1``, so its raw output is tens of thousands of lines; only the ``Error:``
    lines and a bounded tail belong in the envelope. The binary that rejected the build is named as
    well: a binary older than its checkout is the usual reason a valid hash comes back rejected.
    """
    extra = dict(details or {})
    if isinstance(exc, SimcBuildError):
        provenance = binary_provenance(_repo_paths(ctx))
        extra["simc_returncode"] = exc.returncode
        extra["simc_output_preview"] = exc.output_preview
        extra["simc_binary"] = {
            "git_revision": provenance.git_revision,
            "checkout_head": provenance.checkout_head,
            "matches_checkout": provenance.matches_checkout,
        }
        hint = provenance.stale_hint
        fail(ctx, "invalid_build", f"{prefix}{exc}{f' {hint}' if hint else ''}", details=extra or None)
    fail(ctx, code, f"{prefix}{exc}", details=extra or None)


def _decode_or_fail(
    ctx: typer.Context, paths: RepoPaths, build_spec: BuildSpec, *, identity: Any = None, prefix: str = ""
) -> BuildResolution:
    """Decode a build, turning SimC's rejection into an ``invalid_build`` envelope with its own message."""
    try:
        return decode_build(paths, build_spec)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        details: dict[str, Any] = {"build_spec": _serialize_build_spec(build_spec)}
        if identity is not None:
            details["identity"] = _serialize_build_identity(identity)
        with contextlib.suppress(ValueError):
            details["generated_profile"] = build_profile_text(build_spec)
        _fail_build_error(ctx, exc, code="decode_failed", prefix=prefix, details=details)


def _decode_build(ctx: typer.Context, *, apl_path: str | None, option_values: dict[str, Any]) -> None:
    paths = _repo_paths(ctx)
    build_spec, identity = _identified_build_or_fail(ctx, paths, apl_path=apl_path, option_values=option_values)
    if not build_spec.actor_class or not build_spec.spec:
        fail(
            ctx,
            "invalid_query",
            "Could not determine actor class and spec for build decoding.",
            details={"build_spec": _serialize_build_spec(build_spec), "identity": _serialize_build_identity(identity)},
        )
    resolution = _decode_or_fail(ctx, paths, build_spec, identity=identity)
    _emit(
        ctx,
        {
            "provider": "simc",
            "build_spec": _serialize_build_spec(build_spec),
            "identity": _serialize_build_identity(identity),
            "decoded": _decoded_payload(resolution),
        },
    )


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
    """Decode a talent build into per-tree talents using the local SimC binary."""
    option_values = _build_option_values(
        profile_path=profile_path,
        build_file=build_file,
        build_packet=build_packet,
        build_text=build_text,
        talents=TalentStrings(
            talents=talents,
            class_talents=class_talents,
            spec_talents=spec_talents,
            hero_talents=hero_talents,
        ),
        actor_class=actor_class,
        spec_name=spec_name,
    )
    _decode_build(ctx, apl_path=apl_path, option_values=option_values)


def _identify_build(ctx: typer.Context, *, apl_path: str | None, option_values: dict[str, Any]) -> None:
    paths = _repo_paths(ctx)
    build_spec, identity = _identified_build_or_fail(ctx, paths, apl_path=apl_path, option_values=option_values)
    _emit(
        ctx,
        {
            "provider": "simc",
            "kind": "identify_build",
            "build_spec": _serialize_build_spec(build_spec),
            "identity": _serialize_build_identity(identity),
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
    """Resolve class/spec identity for a build without decoding its talents."""
    option_values = _build_option_values(
        profile_path=profile_path,
        build_file=build_file,
        build_packet=build_packet,
        build_text=build_text,
        talents=TalentStrings(
            talents=talents,
            class_talents=class_talents,
            spec_talents=spec_talents,
            hero_talents=hero_talents,
        ),
        actor_class=actor_class,
        spec_name=spec_name,
    )
    _identify_build(ctx, apl_path=apl_path, option_values=option_values)


@dataclass(slots=True)
class _TransportInput:
    """Talent rows plus the identity and packet context the transport validator needs."""

    source: str
    rows: list[dict[str, Any]]
    actor_class: str | None
    spec: str | None
    packet: dict[str, Any] | None = None
    packet_path: str | None = None
    packet_transport_status: str | None = None


def _packet_transport_input(ctx: typer.Context, build_packet: str, actor_class: str | None, spec_name: str | None) -> _TransportInput:
    try:
        packet, resolved_packet_path = _load_transport_packet(build_packet)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        fail(ctx, "invalid_build_packet", str(exc))
    transport_status = packet.get("transport_status")
    return _TransportInput(
        source="build_packet",
        rows=_packet_talent_tree_rows(packet),
        actor_class=actor_class if actor_class is not None else _packet_identity_value(packet, "actor_class"),
        spec=spec_name if spec_name is not None else _packet_identity_value(packet, "spec"),
        packet=packet,
        packet_path=resolved_packet_path,
        packet_transport_status=transport_status if isinstance(transport_status, str) else None,
    )


def _transport_input_or_fail(
    ctx: typer.Context,
    *,
    build_packet: str | None,
    talent_row: list[str],
    actor_class: str | None,
    spec_name: str | None,
) -> _TransportInput:
    if build_packet:
        resolved = _packet_transport_input(ctx, build_packet, actor_class, spec_name)
    else:
        try:
            rows = [_parse_talent_row(value) for value in talent_row]
        except ValueError as exc:
            fail(ctx, "invalid_talent_row", str(exc))
        resolved = _TransportInput(source="talent_rows", rows=rows, actor_class=actor_class, spec=spec_name)
    if not resolved.rows:
        fail(ctx, "invalid_query", "No raw talent rows were available to validate.")
    if not resolved.actor_class or not resolved.spec:
        fail(
            ctx,
            "invalid_query",
            (
                "Validate-talent-transport requires class/spec identity. "
                "Provide --actor-class and --spec, or use a build packet with packet identity."
            ),
        )
    return resolved


def _refreshed_transport_packet(
    ctx: typer.Context,
    *,
    packet: dict[str, Any],
    resolved: _TransportInput,
    transport_forms: dict[str, Any],
    validation: dict[str, Any],
) -> dict[str, Any]:
    identity = build_identity_payload(
        actor_class=resolved.actor_class,
        spec=resolved.spec,
        confidence="high" if resolved.actor_class and resolved.spec else "none",
        source="simc_validate_talent_transport",
        candidates=[(resolved.actor_class, resolved.spec)] if resolved.actor_class and resolved.spec else None,
        source_notes=["class/spec identity was refreshed from simc validate-talent-transport input"],
    )
    try:
        return refresh_talent_transport_packet(
            packet,
            transport_forms=transport_forms,
            validation=validation,
            build_identity=identity,
        )
    except ValueError as exc:
        fail(ctx, "invalid_build_packet", str(exc))


@app.command("validate-talent-transport")
def validate_talent_transport_command(
    ctx: typer.Context,
    build_packet: str | None = typer.Option(None, "--build-packet", help="Path to a talent transport packet JSON file."),
    talent_row: list[str] = typer.Option([], "--talent-row", help="Raw talent row as entry_id:node_id:rank. Repeat as needed."),
    actor_class: str | None = typer.Option(None, "--actor-class", help="Actor class such as druid or paladin."),
    spec_name: str | None = typer.Option(None, "--spec", help="Spec name such as balance or retribution."),
    out: str | None = typer.Option(None, "--out", help="Optional path to write the upgraded packet JSON when --build-packet is used."),
) -> None:
    """Round-trip raw talent rows through SimulationCraft and report the validated transport forms."""
    if build_packet and talent_row:
        fail(ctx, "invalid_query", "Use either --build-packet or --talent-row, not both.")
    if not build_packet and not talent_row:
        fail(ctx, "invalid_query", "Provide either --build-packet or at least one --talent-row.")
    if out and not build_packet:
        fail(ctx, "invalid_query", "--out requires --build-packet.")

    resolved = _transport_input_or_fail(
        ctx, build_packet=build_packet, talent_row=talent_row, actor_class=actor_class, spec_name=spec_name
    )
    result = validate_talent_tree_transport(
        actor_class=resolved.actor_class,
        spec=resolved.spec,
        talent_tree_rows=resolved.rows,
        repo_root=_cfg(ctx).repo_root,
    )
    raw_forms = result.get("transport_forms")
    transport_forms: dict[str, Any] = raw_forms if isinstance(raw_forms, dict) else {}
    raw_validation = result.get("validation")
    validation: dict[str, Any] = raw_validation if isinstance(raw_validation, dict) else {}
    transport_status = "validated" if transport_forms.get("simc_split_talents") else "raw_only"
    updated_packet: dict[str, Any] | None = None
    written_packet_path: str | None = None
    if resolved.packet is not None:
        updated_packet = _refreshed_transport_packet(
            ctx,
            packet=resolved.packet,
            resolved=resolved,
            transport_forms=transport_forms,
            validation=validation,
        )
        packet_status = updated_packet.get("transport_status")
        transport_status = packet_status if isinstance(packet_status, str) else transport_status
        written_packet_path = _write_packet_json_or_fail(ctx, out=out, packet=updated_packet)
    _emit(
        ctx,
        {
            "provider": "simc",
            "kind": "validate_talent_transport",
            "input": {
                "source": resolved.source,
                "build_packet": resolved.packet_path,
                "packet_transport_status": resolved.packet_transport_status,
                "actor_class": resolved.actor_class,
                "spec": resolved.spec,
                "talent_row_count": len(resolved.rows),
            },
            "raw_talent_tree_entries": resolved.rows,
            "transport_status": transport_status,
            "transport_forms": transport_forms,
            "validation": validation,
            "updated_packet": updated_packet,
            "written_packet_path": written_packet_path,
        },
    )


def _build_harness(
    ctx: typer.Context,
    *,
    out: str | None,
    apl_path: str | None,
    line: list[str],
    option_values: dict[str, Any],
) -> None:
    paths = _repo_paths(ctx)
    build_spec, identity = _identified_build_or_fail(ctx, paths, apl_path=apl_path, option_values=option_values)
    if not build_spec.actor_class or not build_spec.spec:
        fail(
            ctx,
            "invalid_query",
            "Could not determine actor class and spec for harness generation.",
            details={"build_spec": _serialize_build_spec(build_spec), "identity": _serialize_build_identity(identity)},
        )
    try:
        target = write_harness(build_spec, lines=line, out_path=out)
    except ValueError as exc:
        fail(ctx, "build_harness_failed", str(exc))
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
    """Write a harness profile for the resolved build with no APL actions."""
    option_values = _build_option_values(
        profile_path=profile_path,
        build_file=build_file,
        build_text=build_text,
        talents=TalentStrings(
            talents=talents,
            class_talents=class_talents,
            spec_talents=spec_talents,
            hero_talents=hero_talents,
        ),
        actor_class=actor_class,
        spec_name=spec_name,
    )
    _build_harness(ctx, out=out, apl_path=apl_path, line=line, option_values=option_values)


@app.command("validate-apl")
def validate_apl_command(
    ctx: typer.Context,
    harness_path: str = typer.Argument(..., help="Harness profile path without APL actions."),
    apl_path: str = typer.Argument(..., help="APL file to append to the harness."),
    label: str = typer.Option("variant", "--label", help="Variant label for the generated temporary profile."),
    out_dir: str | None = typer.Option(None, "--out-dir", help="Optional directory for the generated temporary profile."),
) -> None:
    """Append an APL to a harness profile and check that SimC parses the result."""
    paths = _repo_paths(ctx)
    try:
        profile_path = build_variant_profile(harness_path, apl_path, label=label, out_dir=out_dir)
        validation = validate_profile_file(paths, profile_path)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        _fail_build_error(ctx, exc, code="validate_apl_failed")
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
    """Sim a base APL against labelled variants and rank them by DPS."""
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
        fail(ctx, "compare_apls_failed", str(exc))
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
    """Summarize a saved compare-apls JSON report."""
    resolved = Path(report_path).expanduser().resolve()
    if not resolved.exists():
        fail(ctx, "not_found", f"Report not found: {resolved}")
    try:
        report = json.loads(resolved.read_text())
    except json.JSONDecodeError as exc:
        fail(ctx, "invalid_report", str(exc))
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
    """Report whether the checkout and built binary are unmodified."""
    paths = _repo_paths(ctx)
    _emit(ctx, {"provider": "simc", **verify_clean_payload(paths, hash_binary=hash_binary)})


@app.command("apl-lists")
def apl_lists(
    ctx: typer.Context,
    apl_path: str = typer.Argument(..., help="Path to a .simc APL file."),
    list_name: str | None = typer.Option(None, "--list", help="Only return one action list."),
) -> None:
    """List the action lists in an APL file with their entries."""
    paths = _repo_paths(ctx)
    resolved = _resolve_path(paths, apl_path)
    if not resolved.exists():
        fail(ctx, "not_found", f"APL file not found: {resolved}")
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
    """Render the action-list call graph of an APL file as Mermaid text."""
    paths = _repo_paths(ctx)
    resolved = _resolve_path(paths, apl_path)
    if not resolved.exists():
        fail(ctx, "not_found", f"APL file not found: {resolved}")
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
    """List the talents an APL file references and the most common actions."""
    paths = _repo_paths(ctx)
    resolved = _resolve_path(paths, apl_path)
    if not resolved.exists():
        fail(ctx, "not_found", f"APL file not found: {resolved}")
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
    """Find an action, buff, or token across APLs, class modules, and spell dumps."""
    paths = _repo_paths(ctx)
    _require_checkout(ctx, paths)
    try:
        results = find_action(paths, action, wow_class)
    except MissingRipgrepError as exc:
        fail(ctx, "missing_dependency", str(exc))
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
    """Trace one action through an APL file and the surrounding source."""
    paths = _repo_paths(ctx)
    _require_checkout(ctx, paths)
    resolved = _resolve_path(paths, apl_path)
    if not resolved.exists():
        fail(ctx, "not_found", f"APL file not found: {resolved}")
    entries = trace_action_entries(parse_apl(resolved), action)
    try:
        search_hits = find_action(paths, action, wow_class)
    except MissingRipgrepError as exc:
        fail(ctx, "missing_dependency", str(exc))
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


def _apl_prune(
    ctx: typer.Context,
    *,
    apl_path: str,
    targets: int,
    list_name: str | None,
    show: str,
    option_values: dict[str, Any],
) -> None:
    if show not in {"all", "eligible", "dead", "unknown"}:
        fail(ctx, "invalid_query", "--show must be one of: all, eligible, dead, unknown")
    paths = _repo_paths(ctx)
    resolved = _resolve_path(paths, apl_path)
    if not resolved.exists():
        fail(ctx, "not_found", f"APL file not found: {resolved}")
    try:
        context, resolution = _resolve_prune_context(paths, resolved, option_values, targets)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        _fail_build_error(ctx, exc, code="prune_context_failed")
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
    """Classify APL entries as eligible, dead, or unknown for an exact build."""
    option_values = _build_option_values(
        profile_path=profile_path,
        build_file=build_file,
        build_text=build_text,
        talents=TalentStrings(
            talents=talents,
            class_talents=class_talents,
            spec_talents=spec_talents,
            hero_talents=hero_talents,
        ),
        actor_class=actor_class,
        spec_name=spec_name,
        enable=enable,
        disable=disable,
    )
    _apl_prune(ctx, apl_path=apl_path, targets=targets, list_name=list_name, show=show, option_values=option_values)


def _apl_branch_trace(
    ctx: typer.Context,
    *,
    apl_path: str,
    targets: int,
    list_name: str,
    max_depth: int,
    option_values: dict[str, Any],
) -> None:
    paths = _repo_paths(ctx)
    resolved = _resolve_path(paths, apl_path)
    if not resolved.exists():
        fail(ctx, "not_found", f"APL file not found: {resolved}")
    try:
        context, resolution = _resolve_prune_context(paths, resolved, option_values, targets)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        _fail_build_error(ctx, exc, code="branch_trace_failed")
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
    """Trace action-list dispatch for an exact build from a starting list."""
    option_values = _build_option_values(
        profile_path=profile_path,
        build_file=build_file,
        build_text=build_text,
        talents=TalentStrings(
            talents=talents,
            class_talents=class_talents,
            spec_talents=spec_talents,
            hero_talents=hero_talents,
        ),
        actor_class=actor_class,
        spec_name=spec_name,
        enable=enable,
        disable=disable,
    )
    _apl_branch_trace(
        ctx,
        apl_path=apl_path,
        targets=targets,
        list_name=list_name,
        max_depth=max_depth,
        option_values=option_values,
    )


def _apl_intent(
    ctx: typer.Context,
    *,
    apl_path: str,
    targets: int,
    list_name: str,
    limit: int,
    option_values: dict[str, Any],
) -> None:
    paths = _repo_paths(ctx)
    resolved = _resolve_path(paths, apl_path)
    if not resolved.exists():
        fail(ctx, "not_found", f"APL file not found: {resolved}")
    try:
        context, resolution = _resolve_prune_context(paths, resolved, option_values, targets)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        _fail_build_error(ctx, exc, code="intent_failed")
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
    """Summarize what the focus action list is trying to do for an exact build."""
    option_values = _build_option_values(
        profile_path=profile_path,
        build_file=build_file,
        build_text=build_text,
        talents=TalentStrings(
            talents=talents,
            class_talents=class_talents,
            spec_talents=spec_talents,
            hero_talents=hero_talents,
        ),
        actor_class=actor_class,
        spec_name=spec_name,
        enable=enable,
        disable=disable,
    )
    _apl_intent(ctx, apl_path=apl_path, targets=targets, list_name=list_name, limit=limit, option_values=option_values)


def _apl_intent_explain(
    ctx: typer.Context,
    *,
    apl_path: str,
    targets: int,
    list_name: str,
    limit: int,
    option_values: dict[str, Any],
) -> None:
    paths = _repo_paths(ctx)
    resolved = _resolve_path(paths, apl_path)
    if not resolved.exists():
        fail(ctx, "not_found", f"APL file not found: {resolved}")
    try:
        context, resolution = _resolve_prune_context(paths, resolved, option_values, targets)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        _fail_build_error(ctx, exc, code="intent_explain_failed")
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
    """Explain the focus list as setup, helper, burst, and priority buckets."""
    option_values = _build_option_values(
        profile_path=profile_path,
        build_file=build_file,
        build_text=build_text,
        talents=TalentStrings(
            talents=talents,
            class_talents=class_talents,
            spec_talents=spec_talents,
            hero_talents=hero_talents,
        ),
        actor_class=actor_class,
        spec_name=spec_name,
        enable=enable,
        disable=disable,
    )
    _apl_intent_explain(
        ctx,
        apl_path=apl_path,
        targets=targets,
        list_name=list_name,
        limit=limit,
        option_values=option_values,
    )


def _priority(
    ctx: typer.Context,
    *,
    apl_path: str,
    targets: int,
    list_name: str,
    limit: int,
    option_values: dict[str, Any],
) -> None:
    paths = _repo_paths(ctx)
    resolved = _resolve_path(paths, apl_path)
    if not resolved.exists():
        fail(ctx, "not_found", f"APL file not found: {resolved}")
    try:
        context, resolution = _resolve_prune_context(paths, resolved, option_values, targets)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        _fail_build_error(ctx, exc, code="priority_failed")
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
    """Return the static active priority for an exact build, excluding inactive talent branches."""
    option_values = _build_option_values(
        profile_path=profile_path,
        build_file=build_file,
        build_text=build_text,
        talents=TalentStrings(
            talents=talents,
            class_talents=class_talents,
            spec_talents=spec_talents,
            hero_talents=hero_talents,
        ),
        actor_class=actor_class,
        spec_name=spec_name,
        enable=enable,
        disable=disable,
    )
    _priority(ctx, apl_path=apl_path, targets=targets, list_name=list_name, limit=limit, option_values=option_values)


def _describe_build(
    ctx: typer.Context,
    *,
    apl_path: str | None,
    targets: int,
    aoe_targets: int,
    list_name: str,
    priority_limit: int,
    inactive_limit: int,
    option_values: dict[str, Any],
) -> None:
    paths = _repo_paths(ctx)
    build_spec, identity = _identified_build_or_fail(ctx, paths, apl_path=apl_path, option_values=option_values)
    if not build_spec.actor_class or not build_spec.spec:
        fail(
            ctx,
            "invalid_query",
            "Could not determine actor class and spec for build description.",
            details={"build_spec": _serialize_build_spec(build_spec), "identity": _serialize_build_identity(identity)},
        )
    resolved = _resolve_path(paths, apl_path) if apl_path else _infer_default_apl_path(
        paths, actor_class=build_spec.actor_class, spec=build_spec.spec)
    if not resolved or not resolved.exists():
        fail(
            ctx,
            "not_found",
            "Could not locate an APL file for the resolved build. Pass --apl-path explicitly.",
            details={"build_spec": _serialize_build_spec(build_spec), "identity": _serialize_build_identity(identity)},
        )
    try:
        primary_context, resolution = _resolve_prune_context(paths, resolved, option_values, targets)
        aoe_context, _ = _resolve_prune_context(paths, resolved, option_values, aoe_targets)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        _fail_build_error(ctx, exc, code="describe_build_failed")
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
                **_hero_tree_payload(resolution),
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
    """Describe a build end to end: talents, priority, and single-target versus AoE differences."""
    option_values = _build_option_values(
        profile_path=profile_path,
        build_file=build_file,
        build_packet=build_packet,
        build_text=build_text,
        talents=TalentStrings(
            talents=talents,
            class_talents=class_talents,
            spec_talents=spec_talents,
            hero_talents=hero_talents,
        ),
        actor_class=actor_class,
        spec_name=spec_name,
        enable=enable,
        disable=disable,
    )
    _describe_build(
        ctx,
        apl_path=apl_path,
        targets=targets,
        aoe_targets=aoe_targets,
        list_name=list_name,
        priority_limit=priority_limit,
        inactive_limit=inactive_limit,
        option_values=option_values,
    )


def _inactive_actions(
    ctx: typer.Context,
    *,
    apl_path: str,
    targets: int,
    list_name: str,
    limit: int,
    talent_only: bool,
    option_values: dict[str, Any],
) -> None:
    paths = _repo_paths(ctx)
    resolved = _resolve_path(paths, apl_path)
    if not resolved.exists():
        fail(ctx, "not_found", f"APL file not found: {resolved}")
    try:
        context, resolution = _resolve_prune_context(paths, resolved, option_values, targets)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        _fail_build_error(ctx, exc, code="inactive_actions_failed")
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
    """List the APL actions an exact build cannot use."""
    option_values = _build_option_values(
        profile_path=profile_path,
        build_file=build_file,
        build_text=build_text,
        talents=TalentStrings(
            talents=talents,
            class_talents=class_talents,
            spec_talents=spec_talents,
            hero_talents=hero_talents,
        ),
        actor_class=actor_class,
        spec_name=spec_name,
        enable=enable,
        disable=disable,
    )
    _inactive_actions(
        ctx,
        apl_path=apl_path,
        targets=targets,
        list_name=list_name,
        limit=limit,
        talent_only=talent_only,
        option_values=option_values,
    )


def _opener(
    ctx: typer.Context,
    *,
    apl_path: str,
    targets: int,
    list_name: str,
    limit: int,
    option_values: dict[str, Any],
) -> None:
    paths = _repo_paths(ctx)
    resolved = _resolve_path(paths, apl_path)
    if not resolved.exists():
        fail(ctx, "not_found", f"APL file not found: {resolved}")
    try:
        context, resolution = _resolve_prune_context(paths, resolved, option_values, targets)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        _fail_build_error(ctx, exc, code="opener_failed")
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
    """Preview the early priority for an exact build, flagging runtime-only conditions."""
    option_values = _build_option_values(
        profile_path=profile_path,
        build_file=build_file,
        build_text=build_text,
        talents=TalentStrings(
            talents=talents,
            class_talents=class_talents,
            spec_talents=spec_talents,
            hero_talents=hero_talents,
        ),
        actor_class=actor_class,
        spec_name=spec_name,
        enable=enable,
        disable=disable,
    )
    _opener(ctx, apl_path=apl_path, targets=targets, list_name=list_name, limit=limit, option_values=option_values)


def _apl_branch_compare(
    ctx: typer.Context,
    *,
    apl_path: str,
    list_name: str,
    left_targets: int,
    right_targets: int,
    left_values: dict[str, Any],
    right_values: dict[str, Any],
) -> None:
    paths = _repo_paths(ctx)
    resolved = _resolve_path(paths, apl_path)
    if not resolved.exists():
        fail(ctx, "not_found", f"APL file not found: {resolved}")
    try:
        left_context, left_resolution = _resolve_prune_context(paths, resolved, left_values, left_targets)
        right_context, right_resolution = _resolve_prune_context(paths, resolved, right_values, right_targets)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        _fail_build_error(ctx, exc, code="branch_compare_failed")
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
    """Compare branch dispatch between two builds or target counts on one APL."""
    left_values = _build_option_values(
        profile_path=profile_path,
        build_file=build_file,
        build_text=build_text,
        talents=TalentStrings(
            talents=talents,
            class_talents=class_talents,
            spec_talents=spec_talents,
            hero_talents=hero_talents,
        ),
        actor_class=actor_class,
        spec_name=spec_name,
        enable=enable,
        disable=disable,
    )
    right_values = _build_option_values(
        profile_path=right_profile_path if right_profile_path is not None else profile_path,
        build_file=right_build_file if right_build_file is not None else build_file,
        build_text=right_build_text if right_build_text is not None else build_text,
        talents=TalentStrings(
            talents=right_talents if right_talents is not None else talents,
            class_talents=right_class_talents if right_class_talents is not None else class_talents,
            spec_talents=right_spec_talents if right_spec_talents is not None else spec_talents,
            hero_talents=right_hero_talents if right_hero_talents is not None else hero_talents,
        ),
        actor_class=right_actor_class if right_actor_class is not None else actor_class,
        spec_name=right_spec_name if right_spec_name is not None else spec_name,
        enable=[*enable, *right_enable],
        disable=[*disable, *right_disable],
    )
    _apl_branch_compare(
        ctx,
        apl_path=apl_path,
        list_name=list_name,
        left_targets=left_targets,
        right_targets=right_targets,
        left_values=left_values,
        right_values=right_values,
    )


def _analysis_packet(
    ctx: typer.Context,
    *,
    apl_path: str,
    targets: int,
    list_name: str,
    intent_limit: int,
    explain_limit: int,
    runtime_scan_limit: int,
    first_cast: FirstCastOptions,
    option_values: dict[str, Any],
) -> None:
    paths = _repo_paths(ctx)
    resolved = _resolve_path(paths, apl_path)
    if not resolved.exists():
        fail(ctx, "not_found", f"APL file not found: {resolved}")
    try:
        context, resolution = _resolve_prune_context(paths, resolved, option_values, targets)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        _fail_build_error(ctx, exc, code="analysis_packet_failed")
    try:
        packet = build_analysis_packet(
            paths,
            resolved,
            context,
            start_list=list_name,
            intent_limit=intent_limit,
            explain_limit=explain_limit,
            runtime_scan_limit=runtime_scan_limit,
            first_cast=first_cast,
        )
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        _fail_build_error(ctx, exc, code="analysis_packet_failed")
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
    """Bundle branch, intent, and optional first-cast timing analysis into one payload."""
    option_values = _build_option_values(
        profile_path=profile_path,
        build_file=build_file,
        build_text=build_text,
        talents=TalentStrings(
            talents=talents,
            class_talents=class_talents,
            spec_talents=spec_talents,
            hero_talents=hero_talents,
        ),
        actor_class=actor_class,
        spec_name=spec_name,
        enable=enable,
        disable=disable,
    )
    _analysis_packet(
        ctx,
        apl_path=apl_path,
        targets=targets,
        list_name=list_name,
        intent_limit=intent_limit,
        explain_limit=explain_limit,
        runtime_scan_limit=runtime_scan_limit,
        first_cast=FirstCastOptions(
            profile=sim_profile or profile_path,
            actions=tuple(first_cast_action),
            seeds=seeds,
            max_time=max_time,
            targets=targets,
            fight_style=fight_style,
        ),
        option_values=option_values,
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
    """Time the first cast of an action across several short sims."""
    paths = _repo_paths(ctx)
    resolved = Path(profile_path).expanduser().resolve()
    if not resolved.exists():
        fail(ctx, "not_found", f"Profile not found: {resolved}")
    try:
        results = run_first_casts(paths, resolved, action, seeds, max_time, targets, fight_style)
    except (FileNotFoundError, RuntimeError) as exc:
        fail(ctx, "first_cast_failed", str(exc))
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
    """Report when actions were first scheduled and performed in a SimC combat log."""
    resolved = Path(log_path).expanduser().resolve()
    if not resolved.is_file():
        fail(ctx, "not_found", f"Log file not found: {resolved}")
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
    """Pull the latest SimulationCraft sources into the local checkout."""
    paths = _repo_paths(ctx)
    if not paths.root.exists():
        fail(ctx, "missing_repo", f"SimulationCraft repo not found: {paths.root}")
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
        fail(
            ctx,
            "sync_failed",
            "SimulationCraft git sync failed.",
            details={
                "command": result.command,
                "stdout_preview": stdout_preview,
                "stdout_truncated": stdout_truncated,
                "stderr_preview": stderr_preview,
                "stderr_truncated": stderr_truncated,
            },
        )
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
    """Build the local SimulationCraft binary with cmake."""
    paths = _repo_paths(ctx)
    if not paths.build_dir.exists():
        fail(ctx, "missing_build_dir", f"SimulationCraft build dir not found: {paths.build_dir}")
    result = build_repo(paths, target=target)
    stdout_preview, stdout_truncated = _preview_text(result.stdout)
    stderr_preview, stderr_truncated = _preview_text(result.stderr)
    if result.returncode != 0:
        fail(
            ctx,
            "build_failed",
            "SimulationCraft build failed.",
            details={
                "command": result.command,
                "stdout_preview": stdout_preview,
                "stdout_truncated": stdout_truncated,
                "stderr_preview": stderr_preview,
                "stderr_truncated": stderr_truncated,
            },
        )
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


@dataclass(slots=True)
class _SimProfileInput:
    """Where the sim profile came from, plus temp files the command must clean up."""

    path: Path
    source: str
    cleanup_paths: list[Path]


@dataclass(slots=True)
class _SimOverrides:
    """SimC engine settings from the command line; iterations/max_time are None until the preset default is applied."""

    iterations: int | None
    max_time: int | None
    fight_style: str | None
    threads: int | None
    targets: int | None
    vary_combat_length: float | None


def _sim_profile_input(ctx: typer.Context, *, profile_path: str | None, profile_text: str | None) -> _SimProfileInput:
    if profile_text is not None:
        written = _write_temp_profile(source_name="simc-profile-text", text=profile_text)
        return _SimProfileInput(path=written, source="profile_text", cleanup_paths=[written])
    if profile_path is None or profile_path == "-":
        stdin_text = sys.stdin.read()
        if not stdin_text.strip():
            fail(ctx, "missing_profile", "Provide a profile path, --profile-text, or pipe a profile into stdin.")
        written = _write_temp_profile(source_name="simc-stdin", text=stdin_text)
        return _SimProfileInput(path=written, source="stdin", cleanup_paths=[written])
    resolved = Path(profile_path).expanduser().resolve()
    if not resolved.exists():
        fail(ctx, "not_found", f"Profile not found: {resolved}")
    return _SimProfileInput(path=resolved, source="file", cleanup_paths=[])


def _sim_json_report_path(json_out: str | None, cleanup_paths: list[Path]) -> Path:
    """Where SimC writes its json2 report: the caller's path, or a temp file we delete afterwards."""
    if json_out is not None:
        json_path = Path(json_out).expanduser().resolve()
        json_path.parent.mkdir(parents=True, exist_ok=True)
        return json_path
    fd, raw_json_path = tempfile.mkstemp(suffix=".json", prefix="simc-run-")
    os.close(fd)
    json_path = Path(raw_json_path).resolve()
    cleanup_paths.append(json_path)
    return json_path


def _sim_engine_args(overrides: _SimOverrides, *, json_path: Path) -> list[str]:
    args = [
        f"iterations={overrides.iterations}",
        "target_error=0",
        f"max_time={overrides.max_time}",
        f"json2={json_path}",
    ]
    if overrides.fight_style:
        args.append(f"fight_style={overrides.fight_style}")
    if overrides.threads is not None:
        args.append(f"threads={overrides.threads}")
    if overrides.targets is not None:
        args.append(f"desired_targets={overrides.targets}")
    if overrides.vary_combat_length is not None:
        args.append(f"vary_combat_length={overrides.vary_combat_length}")
    return args


def _unlink_all(paths: list[Path]) -> None:
    for path in paths:
        path.unlink(missing_ok=True)


def _sim(
    ctx: typer.Context,
    *,
    profile_path: str | None,
    profile_text: str | None,
    preset: str,
    json_out: str | None,
    overrides: _SimOverrides,
) -> None:
    if preset not in {"quick", "high-accuracy"}:
        fail(ctx, "invalid_preset", f"Unsupported sim preset: {preset}")
    paths = _repo_paths(ctx)
    default_iterations, default_max_time = _sim_preset_settings(preset=preset)
    overrides.iterations = overrides.iterations or default_iterations
    overrides.max_time = overrides.max_time or default_max_time
    profile = _sim_profile_input(ctx, profile_path=profile_path, profile_text=profile_text)
    json_path = _sim_json_report_path(json_out, profile.cleanup_paths)

    result = run_profile(paths, profile.path, simc_args=_sim_engine_args(overrides, json_path=json_path))
    stdout_preview, stdout_truncated = _preview_text(result.stdout)
    stderr_preview, stderr_truncated = _preview_text(result.stderr)
    if result.returncode != 0:
        _unlink_all(profile.cleanup_paths)
        fail(
            ctx,
            "run_failed",
            "SimulationCraft sim failed.",
            details={
                "command": result.command,
                "stdout_preview": stdout_preview,
                "stdout_truncated": stdout_truncated,
                "stderr_preview": stderr_preview,
                "stderr_truncated": stderr_truncated,
            },
        )

    try:
        summary = summarize_sim_report(load_sim_report(json_path))
    except Exception as exc:
        _unlink_all(profile.cleanup_paths)
        fail(
            ctx,
            "invalid_report",
            f"SimulationCraft sim completed but the JSON report could not be parsed: {exc}",
            details={"command": result.command, "json_report_path": str(json_path)},
        )

    _emit(
        ctx,
        sim_report_payload(
            summary,
            profile_path=str(profile.path) if profile.source == "file" else None,
            preset=preset,
            input_source=profile.source,
            json_report_path=str(json_path) if json_out is not None else None,
            command=result.command,
        ),
    )
    _unlink_all(profile.cleanup_paths)


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
    """Run a profile through the local SimC binary and summarize the JSON report."""
    _sim(
        ctx,
        profile_path=profile_path,
        profile_text=profile_text,
        preset=preset,
        json_out=json_out,
        overrides=_SimOverrides(
            iterations=iterations,
            max_time=max_time,
            fight_style=fight_style,
            threads=threads,
            targets=targets,
            vary_combat_length=vary_combat_length,
        ),
    )


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
    """Diff a base talent build against one or more other builds, per tree."""
    paths = _repo_paths(ctx)
    trees = [t for t in tree] or ["class", "spec", "hero"]

    base_spec, base_identity = _load_identified_build_spec_or_fail(
        ctx,
        paths,
        apl_path=None,
        profile_path=None,
        build_file=None,
        build_text=None,
        talents=TalentStrings(talents=base),
        actor_class=actor_class,
        spec_name=spec_name,
    )
    if not base_spec.actor_class or not base_spec.spec:
        fail(ctx, "invalid_query", "Could not identify actor class and spec for base build.",
              details={"build_spec": _serialize_build_spec(base_spec), "identity": _serialize_build_identity(base_identity)})
    try:
        base_resolution = decode_build(paths, base_spec)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        _fail_build_error(ctx, exc, code="decode_failed", prefix="Failed to decode base build: ")

    comparisons: list[dict[str, Any]] = []
    for other_talents in other:
        try:
            other_spec = load_build_spec(
                apl_path=None, profile_path=None, build_file=None, build_text=None,
                talents=TalentStrings(talents=other_talents),
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
            talents=TalentStrings(talents=swap_source),
            actor_class=base_spec.actor_class,
            spec_name=base_spec.spec,
        )
        try:
            swap_resolution = decode_build(paths, swap_spec)
        except (FileNotFoundError, RuntimeError, ValueError) as exc:
            _fail_build_error(ctx, exc, code="decode_failed", prefix=f"Failed to decode {tree_name} tree source: ")
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


@dataclass(frozen=True, slots=True)
class _TalentEdit:
    """One ``--add``/``--remove`` edit, routed to the tree whose SimC option string must carry it."""

    tree: str
    value: str
    rank: int
    entry: int | None

    def as_option(self) -> str:
        return f"{self.value}:{self.rank}"


def _base_entry_index(base_resolution: BuildResolution) -> tuple[dict[str, tuple[str, int]], set[int]]:
    """Map every talent name/token in the base build to its (tree, entry), plus the set of entry ids."""
    by_name: dict[str, tuple[str, int]] = {}
    entries: set[int] = set()
    for tree, talents in base_resolution.talents_by_tree.items():
        for talent in talents:
            if not talent.entry:
                continue
            entries.add(talent.entry)
            by_name.setdefault(talent.token, (tree, talent.entry))
            by_name.setdefault(talent.name.lower(), (tree, talent.entry))
    return by_name, entries


def _resolve_edit(
    ctx: typer.Context,
    value: str,
    rank: int,
    *,
    table: TraitTable,
    by_name: dict[str, tuple[str, int]],
    class_id: int | None,
) -> _TalentEdit:
    """Decide which talent tree an edit belongs to; SimC resolves names per tree, not globally."""
    if value.isdigit():
        entry = int(value)
        tree = table.tree_for_entry(entry)
        if tree is None or tree == "selection":
            fail(ctx, "unknown_talent", f"Unknown talent entry id: '{value}'.")
        return _TalentEdit(tree=tree, value=value, rank=rank, entry=entry)
    # SimC tokenizes talent names when it matches them, and a profile line cannot contain spaces.
    token = tokenize_talent_name(value)
    known = by_name.get(value.lower()) or by_name.get(token)
    if known is not None:
        # Pass the name through so SimC spreads the rank over a tiered node's entries itself.
        return _TalentEdit(tree=known[0], value=token, rank=rank, entry=known[1])
    tree = table.tree_for_name(class_id, value) if class_id is not None else None
    if tree is None or tree == "selection":
        fail(
            ctx,
            "unknown_talent",
            f"Cannot resolve talent '{value}' to a talent tree. Use an entry id or a name from this class.",
        )
    return _TalentEdit(tree=tree, value=token, rank=rank, entry=None)


def _build_modify_edits(
    ctx: typer.Context,
    paths: RepoPaths,
    *,
    base_resolution: BuildResolution,
    add: list[str],
    remove: list[str],
    modifications: list[str],
) -> list[_TalentEdit]:
    table = load_trait_table(paths.root)
    by_name, base_entries = _base_entry_index(base_resolution)
    class_ids = {table.class_id_by_entry[entry] for entry in base_entries if entry in table.class_id_by_entry}
    class_id = next(iter(class_ids)) if len(class_ids) == 1 else None

    edits: list[_TalentEdit] = []
    for item in remove:
        value = item.strip()
        edits.append(_resolve_edit(ctx, value, 0, table=table, by_name=by_name, class_id=class_id))
        modifications.append(f"remove:{value}")
    for item in add:
        name_or_id, _, rank_str = item.strip().partition(":")
        if not rank_str.isdigit() or not name_or_id:
            fail(ctx, "invalid_add", f"--add requires 'name:rank' or 'entry_id:rank', got: '{item}'")
        edits.append(_resolve_edit(ctx, name_or_id, int(rank_str), table=table, by_name=by_name, class_id=class_id))
        modifications.append(f"add:{item}")
    return edits


def _join_tree_option(entries: str | None, edits: list[_TalentEdit], tree: str) -> str | None:
    parts = [part for part in [entries] if part]
    parts.extend(edit.as_option() for edit in edits if edit.tree == tree)
    return "/".join(parts) or None


def _assemble_modified_spec(
    base_spec: BuildSpec,
    *,
    class_entries: str | None,
    spec_entries: str | None,
    hero_entries: str | None,
    edits: list[_TalentEdit],
) -> BuildSpec | None:
    """Apply the edits to the build, keeping every edit in the tree string SimC resolves it against."""
    if class_entries is None and not edits:
        return None
    swapping = class_entries is not None
    return BuildSpec(
        actor_class=base_spec.actor_class,
        spec=base_spec.spec,
        # Without a tree swap the base hash stays the foundation: it carries per-entry ranks that the
        # decode output cannot reproduce (tiered nodes) and freely granted traits.
        talents=None if swapping else base_spec.talents,
        class_talents=_join_tree_option(class_entries, edits, "class"),
        spec_talents=_join_tree_option(spec_entries, edits, "spec"),
        hero_talents=_join_tree_option(hero_entries, edits, "hero"),
    )


ACTIVE_TREES = ("class", "spec", "hero")
# SimC freely grants every hero tree's keystone whenever it regenerates a hash (player.cpp
# parse_traits), so a re-encoded export always carries the unselected tree's keystone even when the
# input hash did not. Those talents are disabled in the sim, so they are disclosed rather than
# treated as an edit that failed verification.
INACTIVE_HERO_TREE = "inactive_hero"
REENCODE_KEYSTONE_DISCLOSURE = (
    "SimC regenerated the talent hash and freely granted the keystone of every hero tree, so the "
    "export carries hero talents outside the selected tree. They are inactive in the sim and are "
    "listed under result.diff_from_base.inactive_hero."
)


def _unrequested_changes(diff_payload: dict[str, Any], edits: list[_TalentEdit], swapped_trees: set[str]) -> list[dict[str, Any]]:
    """Differences between the re-encoded build and the base that nobody asked for.

    SimC re-serializes the whole build when it is handed split talent strings, so an edit can drag
    unrelated talents along. Anything the caller did not name is reported instead of shipped.
    Only the active trees gate the export; ``inactive_hero`` is disclosed instead (see above).
    """
    requested_entries = {edit.entry for edit in edits if edit.entry is not None}
    requested_names = {tokenize_talent_name(edit.value) for edit in edits if edit.entry is None}
    unrequested: list[dict[str, Any]] = []
    for tree in ACTIVE_TREES:
        if tree in swapped_trees:
            continue
        tree_diff = diff_payload[tree]
        for change, rows in tree_diff.items():
            if change == "has_differences":
                continue
            for row in rows:
                if row.get("entry") in requested_entries or row.get("token") in requested_names:
                    continue
                unrequested.append({"tree": tree, "change": change, **row})
    return unrequested


def _modify_build_diff_payload(
    paths: RepoPaths,
    *,
    base_spec: BuildSpec,
    base_resolution: BuildResolution,
    encoded: str,
) -> dict[str, Any]:
    """Decode the re-encoded build and diff it against the base, per tree.

    The fourth key, ``inactive_hero``, covers the hero talents SimC granted for the tree the build
    did not select. Without it the export could differ from the input hash with nothing in the
    payload saying so.
    """
    verify_spec = BuildSpec(
        actor_class=base_spec.actor_class,
        spec=base_spec.spec,
        talents=encoded,
    )
    result_resolution = decode_build(paths, verify_spec)
    diff = {
        tree: _tree_diff_payload(
            diff_talent_trees(
                base_resolution.talents_by_tree.get(tree, []),
                result_resolution.talents_by_tree.get(tree, []),
            )
        )
        for tree in ACTIVE_TREES
    }
    diff[INACTIVE_HERO_TREE] = _tree_diff_payload(
        diff_talent_trees(base_resolution.inactive_hero_talents, result_resolution.inactive_hero_talents)
    )
    return diff


@dataclass(frozen=True, slots=True)
class _ModifyBuildOptions:
    """The modify-build flag group: one base build plus the tree swaps and per-talent edits applied to it."""

    talents: str
    swap_class_tree_from: str | None
    swap_spec_tree_from: str | None
    swap_hero_tree_from: str | None
    add: list[str]
    remove: list[str]
    actor_class: str | None
    spec_name: str | None


def _modify_build(ctx: typer.Context, options: _ModifyBuildOptions) -> None:
    paths = _repo_paths(ctx)

    base_spec, base_identity = _load_identified_build_spec_or_fail(
        ctx,
        paths,
        apl_path=None,
        profile_path=None,
        build_file=None,
        build_text=None,
        talents=TalentStrings(talents=options.talents),
        actor_class=options.actor_class,
        spec_name=options.spec_name,
    )
    if not base_spec.actor_class or not base_spec.spec:
        fail(ctx, "invalid_query", "Could not identify actor class and spec for base build.",
              details={"build_spec": _serialize_build_spec(base_spec), "identity": _serialize_build_identity(base_identity)})

    try:
        base_resolution = decode_build(paths, base_spec)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        _fail_build_error(ctx, exc, code="decode_failed", prefix="Failed to decode base build: ")

    modifications: list[str] = []
    class_entries, spec_entries, hero_entries = _resolve_modify_tree_entries(
        ctx,
        paths,
        base_spec=base_spec,
        base_resolution=base_resolution,
        swaps=[
            ("class", options.swap_class_tree_from),
            ("spec", options.swap_spec_tree_from),
            ("hero", options.swap_hero_tree_from),
        ],
        modifications=modifications,
    )
    edits = _build_modify_edits(
        ctx,
        paths,
        base_resolution=base_resolution,
        add=options.add,
        remove=options.remove,
        modifications=modifications,
    )

    modified_spec = _assemble_modified_spec(
        base_spec,
        class_entries=class_entries,
        spec_entries=spec_entries,
        hero_entries=hero_entries,
        edits=edits,
    )
    if modified_spec is None:
        fail(
            ctx, "no_modifications",
            "No modifications specified. Use --swap-*-tree-from, --add, or --remove.",
        )

    try:
        encoded = encode_build(paths, modified_spec)
        diff_payload = _modify_build_diff_payload(
            paths, base_spec=base_spec, base_resolution=base_resolution, encoded=encoded
        )
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        _fail_build_error(ctx, exc, code="encode_failed", prefix="Failed to encode modified build: ")

    swapped_trees = {
        tree
        for tree, source in (
            ("class", options.swap_class_tree_from),
            ("spec", options.swap_spec_tree_from),
            ("hero", options.swap_hero_tree_from),
        )
        if source
    }
    unrequested = _unrequested_changes(diff_payload, edits, swapped_trees)
    if unrequested:
        fail(
            ctx,
            "encode_mismatch",
            "The re-encoded build differs from the requested build; no export was emitted.",
            details={"modifications": modifications, "unrequested_changes": unrequested, "diff_from_base": diff_payload},
        )

    _emit(ctx, {
        "provider": "simc",
        "kind": "modify_build",
        "base": {
            "input": options.talents,
            "actor_class": base_resolution.actor_class,
            "spec": base_resolution.spec,
        },
        "modifications": modifications,
        "result": {
            "talents_export": encoded,
            "wowhead_url": f"https://www.wowhead.com/talent-calc/blizzard/{encoded}",
            "diff_from_base": diff_payload,
            "disclosures": [REENCODE_KEYSTONE_DISCLOSURE] if diff_payload[INACTIVE_HERO_TREE]["has_differences"] else [],
            # The active trees carry exactly the requested edits; see disclosures for the rest.
            "verified": True,
        },
    })


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
    """Apply talent swaps, additions, and removals to a build and re-encode it."""
    _modify_build(
        ctx,
        _ModifyBuildOptions(
            talents=talents,
            swap_class_tree_from=swap_class_tree_from,
            swap_spec_tree_from=swap_spec_tree_from,
            swap_hero_tree_from=swap_hero_tree_from,
            add=add,
            remove=remove,
            actor_class=actor_class,
            spec_name=spec_name,
        ),
    )


@app.command("run")
def run_command(
    ctx: typer.Context,
    profile_path: str = typer.Argument(..., help="Path to a SimulationCraft profile to execute."),
    simc_arg: list[str] = typer.Option([], "--arg", help="Additional raw SimulationCraft arg, repeatable."),
) -> None:
    """Run a profile through the local SimC binary with raw SimC arguments."""
    paths = _repo_paths(ctx)
    resolved = Path(profile_path).expanduser().resolve()
    if not resolved.exists():
        fail(ctx, "not_found", f"Profile not found: {resolved}")
    result = run_profile(paths, resolved, simc_args=list(simc_arg))
    stdout_preview, stdout_truncated = _preview_text(result.stdout)
    stderr_preview, stderr_truncated = _preview_text(result.stderr)
    version_line = binary_version(paths).version_line
    if result.returncode != 0:
        fail(
            ctx,
            "run_failed",
            "SimulationCraft run failed.",
            details={
                "command": result.command,
                "stdout_preview": stdout_preview,
                "stdout_truncated": stdout_truncated,
                "stderr_preview": stderr_preview,
                "stderr_truncated": stderr_truncated,
                "version": version_line,
            },
        )
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
    guarded_run(app, provider=PROVIDER_NAME)


if __name__ == "__main__":
    run()

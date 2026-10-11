"""Output-free exact-build operations shared by the SimC CLI and wrapper."""

from __future__ import annotations

import contextlib
import dataclasses
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from warcraft_core.exit_codes import EXIT_USAGE
from warcraft_core.identity import (
    build_identity_payload,
    is_transport_int,
    refresh_talent_transport_packet,
    validate_talent_transport_packet,
)
from warcraft_core.provider import ProviderError
from warcraft_core.talent_transport import CLASS_ID_BY_ACTOR_CLASS, specialization_ids, tokenize_talent_name

from simc_cli.apl import group_entries, parse_apl
from simc_cli.branch import (
    BranchSummary,
    FocusResolution,
    active_priority_decisions,
    explain_intent,
    format_list_decision,
    inactive_priority_decisions,
    resolve_focus_list,
    summarize_branches,
)
from simc_cli.build_input import (
    BuildIdentity,
    BuildResolution,
    BuildSpec,
    PacketInput,
    SimcBuildError,
    TalentStrings,
    UnknownClassSpecError,
    UnsupportedBuildReference,
    build_profile_text,
    decode_build,
    has_talent_data,
    identify_build,
    load_build_spec,
    packet_identity_value,
)
from simc_cli.prune import RUNTIME_ONLY, PruneContext, split_csv_values
from simc_cli.repo import (
    RepoPaths,
    discover_repo,
)
from simc_cli.run import (
    binary_provenance,
)
from simc_cli.talent_transport import validate_talent_tree_transport
from simc_cli.trait_data import (
    SimcNotReadyError,
    UnknownTalentError,
    load_trait_table,
    resolve_talent_tokens,
)


def _serialize_build_spec(spec: BuildSpec) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "actor_class": spec.actor_class,
        "spec": spec.spec,
        "talents": spec.talents,
        "class_talents": spec.class_talents,
        "spec_talents": spec.spec_talents,
        "hero_talents": spec.hero_talents,
        "source_kind": spec.source_kind,
        "source_notes": spec.source_notes,
    }
    if spec.transport_source or spec.transport_form or spec.transport_status:
        payload["transport_packet"] = {
            # A packet handed over in memory (``PacketInput`` without a path) has no file to name.
            **({"path": spec.transport_source} if spec.transport_source else {}),
            "transport_form": spec.transport_form,
            "transport_status": spec.transport_status,
        }
    return payload

def _serialize_build_identity(identity: BuildIdentity) -> dict[str, Any]:
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
    """Resolve an APL path: relative to the current directory when that file exists, else to the checkout.

    A bare file name the checkout root does not hold names a spec APL in ActionPriorityLists/default.
    """
    path = Path(value).expanduser()
    if not path.is_absolute() and not path.exists():
        path = paths.root / path
        if not path.exists() and Path(value).name == value:
            path = paths.apl_default / value
    return path.resolve()

def _relative_to_repo(paths: RepoPaths, path: Path) -> str | None:
    return str(path.relative_to(paths.root)) if path.is_relative_to(paths.root) else None

def _packet_talent_tree_rows(packet: dict[str, Any]) -> list[dict[str, Any]]:
    raw_evidence = packet.get("raw_evidence")
    if not isinstance(raw_evidence, dict):
        return []
    rows = raw_evidence.get("talent_tree_entries")
    if not isinstance(rows, list) or not rows:
        return []
    keys = ("entry", "node_id", "rank")
    if not all(isinstance(row, dict) and all(is_transport_int(row.get(key)) for key in keys) for row in rows):
        return []
    return [{key: row[key] for key in keys} for row in rows]

def _infer_default_apl_path(paths: RepoPaths, *, actor_class: str | None, spec: str | None) -> Path | None:
    if not actor_class or not spec:
        return None
    file_name = f"{actor_class}_{spec}.simc"
    for base in (paths.apl_default, paths.apl_assisted):
        candidate = (base / file_name).resolve()
        if candidate.exists():
            return candidate
    return None

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
    packet: PacketInput | None = None,
) -> dict[str, Any]:
    """Pack the shared build-input flag group so wide commands can hand it to one plain function."""
    return {
        "profile_path": profile_path,
        "build_file": build_file,
        "build_packet": build_packet,
        "packet": packet,
        "build_text": build_text,
        "talents": talents,
        "actor_class": actor_class,
        "spec_name": spec_name,
        "enable": enable or [],
        "disable": disable or [],
    }

def _require_apl_path(paths: RepoPaths, apl_path: str | None) -> None:
    """Reject an --apl-path that does not exist: its file stem otherwise invents a class and spec."""
    if apl_path is not None:
        _apl_or_raise(paths, apl_path)

def _apl_or_raise(paths: RepoPaths, apl_path: str, *, list_name: str | None = None) -> Path:
    """Resolve an APL file, raising ``not_found`` when it or the requested action list is not there.

    An unknown list would otherwise read as a list with no actions.
    """
    resolved = _resolve_path(paths, apl_path)
    if not resolved.exists():
        raise ProviderError("not_found", f"APL file not found: {resolved}")
    if list_name is not None:
        lists = sorted(group_entries(parse_apl(resolved)))
        if list_name not in lists:
            raise ProviderError(
                "not_found",
                f"Action list '{list_name}' is not in {resolved.name}. Its lists are: {', '.join(lists)}.",
                details={"available_lists": lists},
            )
    return resolved

def _identified_build_or_raise(
    paths: RepoPaths, *, apl_path: str | None, option_values: dict[str, Any]
) -> tuple[BuildSpec, BuildIdentity]:
    _require_apl_path(paths, apl_path)
    return _load_identified_build_spec_or_raise(
        paths,
        apl_path=apl_path,
        profile_path=option_values["profile_path"],
        build_file=option_values["build_file"],
        build_packet=option_values["build_packet"],
        packet=option_values["packet"],
        build_text=option_values["build_text"],
        talents=option_values["talents"],
        actor_class=option_values["actor_class"],
        spec_name=option_values["spec_name"],
    )

def _prune_context(
    paths: RepoPaths, build_spec: BuildSpec, option_values: dict[str, Any], targets: int
) -> tuple[PruneContext, BuildResolution]:
    """Decode an identified build into the talents, ranks and hero tree its APL conditions test."""
    resolution = decode_build(paths, build_spec)
    # `--enable`/`--disable` name talents; a value the class has no talent for used to be dropped in
    # silence, so the command answered as if the flag had never been passed.
    enable_tokens = resolve_talent_tokens(paths.root, build_spec.actor_class, split_csv_values(option_values["enable"]))
    disabled = resolve_talent_tokens(paths.root, build_spec.actor_class, split_csv_values(option_values["disable"]))
    decoded = [talent for tree in ("class", "spec", "hero") for talent in resolution.talents_by_tree.get(tree, [])]
    enabled = set(resolution.enabled_talents) | enable_tokens
    class_id = CLASS_ID_BY_ACTOR_CLASS.get(resolution.actor_class or "")
    spec_id = specialization_ids(paths.root).get((resolution.actor_class or "", resolution.spec or ""))
    untaken: set[str] = set()
    if resolution.enabled_talents and class_id is not None and spec_id is not None:
        untaken = load_trait_table(paths.root).untaken_talents(
            enabled, class_id=class_id, spec_id=spec_id, include_hero=resolution.hero_tree is not None
        )
    context = PruneContext(
        enabled_talents=enabled,
        disabled_talents=disabled,
        targets=targets,
        talent_sources={talent.token: talent.tree for talent in decoded} | dict.fromkeys(enable_tokens, "manual"),
        talent_ranks={talent.token: talent.rank for talent in decoded if talent.rank_known and talent.rank > 0},
        hero_tree=tokenize_talent_name(resolution.hero_tree.name) if resolution.hero_tree else None,
        untaken_talents=untaken | disabled,
    )
    return context, resolution

def _apl_payload(paths: RepoPaths, apl: Path) -> dict[str, Any]:
    return {"path": str(apl), "relative_to_repo": _relative_to_repo(paths, apl)}

def _branch_summary_payload(summary: BranchSummary) -> dict[str, Any]:
    return {
        "start_list": summary.start_list,
        "guaranteed_dispatch": summary.guaranteed_dispatch,
        "guaranteed_dispatch_line": summary.guaranteed_dispatch_line,
        "guaranteed_dispatch_reason": summary.guaranteed_dispatch_reason,
        "dead_branches": summary.dead_branches,
        "unresolved_branches": summary.unresolved_branches,
        "shadowed_lines": summary.shadowed_lines,
    }

def _focus_payload(summary: BranchSummary, focus: FocusResolution) -> dict[str, Any]:
    return {
        "focus_list": focus.focus_list,
        "focus_path": focus.path,
        "focus_resolution": focus.reason,
        "dispatch_certainty": "guaranteed" if summary.guaranteed_dispatch else "unresolved",
    }

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
    packet: PacketInput | None = None,
) -> tuple[BuildSpec, BuildIdentity]:
    unresolved_spec = load_build_spec(
        profile_path=profile_path,
        build_file=build_file,
        build_packet=build_packet,
        packet=packet,
        build_text=build_text,
        talents=talents,
        actor_class=actor_class,
        spec_name=spec_name,
    )
    return identify_build(paths, unresolved_spec, apl_path=apl_path)

def _load_identified_build_spec_or_raise(
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
    packet: PacketInput | None = None,
) -> tuple[BuildSpec, BuildIdentity]:
    try:
        return _load_identified_build_spec(
            paths,
            apl_path=apl_path,
            profile_path=profile_path,
            build_file=build_file,
            build_packet=build_packet,
            packet=packet,
            build_text=build_text,
            talents=talents,
            actor_class=actor_class,
            spec_name=spec_name,
        )
    except SimcNotReadyError as exc:
        # The checkout, not the caller's input, is what failed; this is not a usage error.
        raise ProviderError("identify_failed", str(exc)) from exc
    except SimcBuildError as exc:
        raise _build_error(paths, exc, code="invalid_build") from exc
    except UnsupportedBuildReference as exc:
        raise ProviderError(
            "unsupported_build_reference",
            str(exc),
            exit_code=EXIT_USAGE,
            details={"reference_type": exc.reference_type},
        ) from exc
    except UnknownClassSpecError as exc:
        # The class, spec or APL named beside the build is wrong, not a build packet.
        raise ProviderError("invalid_query", str(exc)) from exc
    except FileNotFoundError as exc:
        raise ProviderError("not_found", f"File not found: {exc.filename}") from exc
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        code = "invalid_build_packet" if build_packet or packet is not None else "invalid_query"
        raise ProviderError(code, str(exc)) from exc

def _unidentified_build_error(*, purpose: str, build_spec: BuildSpec, identity: BuildIdentity) -> ProviderError:
    """Ask for an explicit class and spec when decoding the build as every candidate spec found no single match."""
    if identity.source == "missing_build_data":
        reason = "no talent build was supplied"
    elif identity.candidates:
        found = ", ".join(f"{actor_class} {spec}" for actor_class, spec in identity.candidates)
        reason = f"it decodes as {len(identity.candidates)} specs ({found})"
    else:
        reason = f"it decodes as none of {identity.probe_scope}"
    return ProviderError(
        "invalid_query",
        f"Could not determine actor class and spec for {purpose}: {reason}. Pass --actor-class and --spec.",
        details={"build_spec": _serialize_build_spec(build_spec), "identity": _serialize_build_identity(identity)},
    )

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

def _focus_list_summary(resolved: Path, context: PruneContext, *, start_list: str) -> tuple[BranchSummary, FocusResolution]:
    summary = summarize_branches(resolved, context, start_list=start_list)
    return summary, resolve_focus_list(resolved, context, start_list=start_list)

def _describe_target_payload(resolved: Path, context: PruneContext, *, start_list: str,
                             priority_limit: int, inactive_limit: int) -> dict[str, Any]:
    summary, focus = _focus_list_summary(resolved, context, start_list=start_list)
    active_all = active_priority_decisions(resolved, context, focus.focus_list)
    inactive_all = inactive_priority_decisions(resolved, context, focus.focus_list, talent_only=True)
    active = active_all[:priority_limit]
    explanation = explain_intent(resolved, context, focus.focus_list, limit=priority_limit)
    runtime_sensitive = [
        _priority_item(decision)
        for decision in active
        if decision.status == "possible" and decision.reason == RUNTIME_ONLY
    ]
    return {
        "targets": context.targets,
        **_focus_payload(summary, focus),
        "branch_summary": _branch_summary_payload(summary),
        "active_priority": [_priority_item(decision) for decision in active],
        "active_priority_total": len(active_all),
        "active_priority_truncated": len(active_all) > priority_limit,
        "active_action_names": _action_names([_priority_item(decision) for decision in active_all]),
        "inactive_talent_branches": [_priority_item(decision) for decision in inactive_all[:inactive_limit]],
        "inactive_talent_branch_total": len(inactive_all),
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

def _build_error(
    paths: RepoPaths, exc: Exception, *, code: str, prefix: str = "", details: dict[str, Any] | None = None
) -> ProviderError:
    """Report SimC's rejection of a build as ``invalid_build`` carrying only its error line.

    SimC is run with ``debug=1``, so its raw output is tens of thousands of lines; only the ``Error:``
    lines and a bounded tail belong in the envelope. The binary that rejected the build is named as
    well: a binary older than its checkout is the usual reason a valid hash comes back rejected.
    """
    extra = dict(details or {})
    if isinstance(exc, UnknownTalentError):
        # A bad --enable/--disable value is the caller's typo, not SimC rejecting the build.
        return ProviderError("unknown_talent", str(exc), exit_code=EXIT_USAGE, details={"unknown_talents": exc.values})
    if isinstance(exc, UnknownClassSpecError):
        return ProviderError("invalid_query", str(exc))
    if isinstance(exc, SimcBuildError):
        provenance = binary_provenance(paths)
        extra["simc_returncode"] = exc.returncode
        extra["simc_output_preview"] = exc.output_preview
        extra["simc_binary"] = {
            "git_revision": provenance.git_revision,
            "checkout_head": provenance.checkout_head,
            "matches_checkout": provenance.matches_checkout,
        }
        hint = provenance.stale_hint
        return ProviderError("invalid_build", f"{prefix}{exc}{f' {hint}' if hint else ''}", details=extra or None)
    return ProviderError(code, f"{prefix}{exc}", details=extra or None)

def _decode_or_raise(
    paths: RepoPaths, build_spec: BuildSpec, *, identity: BuildIdentity | None = None, prefix: str = ""
) -> BuildResolution:
    """Decode a build, turning SimC's rejection into an ``invalid_build`` failure with its own message."""
    try:
        return decode_build(paths, build_spec)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        details: dict[str, Any] = {"build_spec": _serialize_build_spec(build_spec)}
        if identity is not None:
            details["identity"] = _serialize_build_identity(identity)
        with contextlib.suppress(ValueError):
            details["generated_profile"] = build_profile_text(build_spec)
        raise _build_error(paths, exc, code="decode_failed", prefix=prefix, details=details) from exc

def _decode_build_payload(paths: RepoPaths, *, apl_path: str | None, option_values: dict[str, Any]) -> dict[str, Any]:
    build_spec, identity = _identified_build_or_raise(paths, apl_path=apl_path, option_values=option_values)
    if not has_talent_data(build_spec):
        raise ProviderError(
            "invalid_query",
            "No talent build was supplied to decode. Pass --talents, --build-text, --build-file, --build-packet, "
            "--profile-path, or --class-talents/--spec-talents/--hero-talents.",
        )
    if not build_spec.actor_class or not build_spec.spec:
        raise _unidentified_build_error(purpose="build decoding", build_spec=build_spec, identity=identity)
    resolution = _decode_or_raise(paths, build_spec, identity=identity)
    return {
        "build_spec": _serialize_build_spec(build_spec),
        "identity": _serialize_build_identity(identity),
        "decoded": _decoded_payload(resolution),
    }

def _in_memory_build_option_values(build: PacketInput | str) -> dict[str, Any]:
    """The build-input options for one build handed over in memory: a packet, or build text such as a talent hash."""
    packet, build_text = (build, None) if isinstance(build, PacketInput) else (None, build)
    return _build_option_values(
        profile_path=None,
        build_file=None,
        build_text=build_text,
        talents=TalentStrings(),
        actor_class=None,
        spec_name=None,
        packet=packet,
    )

def decode_build_payload(build: PacketInput | str) -> dict[str, Any]:
    """What ``simc decode-build`` reports for one in-memory build. Raises ``ProviderError`` instead of exiting."""
    return _decode_build_payload(discover_repo(), apl_path=None, option_values=_in_memory_build_option_values(build))

def _identify_build_payload(paths: RepoPaths, *, apl_path: str | None, option_values: dict[str, Any]) -> dict[str, Any]:
    build_spec, identity = _identified_build_or_raise(paths, apl_path=apl_path, option_values=option_values)
    if identity.source == "missing_build_data":
        raise ProviderError(
            "invalid_query",
            "No build was supplied to identify. Pass --talents, --build-text, --build-file, --build-packet, "
            "--profile-path, --apl-path, --class-talents/--spec-talents/--hero-talents, or --actor-class with --spec.",
        )
    return {
        "kind": "identify_build",
        "build_spec": _serialize_build_spec(build_spec),
        "identity": _serialize_build_identity(identity),
    }

def identify_build_payload(build: PacketInput | str) -> dict[str, Any]:
    """What ``simc identify-build`` reports for one in-memory build. Raises ``ProviderError`` instead of exiting."""
    return _identify_build_payload(discover_repo(), apl_path=None, option_values=_in_memory_build_option_values(build))

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

def _packet_transport_input(
    packet: dict[str, Any], packet_path: str | None, actor_class: str | None, spec_name: str | None
) -> _TransportInput:
    transport_status = packet.get("transport_status")
    return _TransportInput(
        source="build_packet",
        rows=_packet_talent_tree_rows(packet),
        actor_class=actor_class if actor_class is not None else packet_identity_value(packet, "actor_class"),
        spec=spec_name if spec_name is not None else packet_identity_value(packet, "spec"),
        packet=packet,
        packet_path=packet_path,
        packet_transport_status=transport_status if isinstance(transport_status, str) else None,
    )

def _checked_transport_input(resolved: _TransportInput) -> _TransportInput:
    """Raise unless the input has talent rows and a class/spec identity to validate them against."""
    if not resolved.rows:
        raise ProviderError("invalid_query", "No raw talent rows were available to validate.")
    if not resolved.actor_class or not resolved.spec:
        raise ProviderError(
            "invalid_query",
            (
                "Validate-talent-transport requires class/spec identity. "
                "Provide --actor-class and --spec, or use a build packet with packet identity."
            ),
        )
    return resolved

def _refreshed_transport_packet(
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
        raise ProviderError("invalid_build_packet", str(exc)) from exc

def _validate_talent_transport_payload(resolved: _TransportInput, *, repo_root: str | None) -> dict[str, Any]:
    """Round-trip the input's talent rows through SimC; ``written_packet_path`` is left for ``--out`` to fill."""
    result = validate_talent_tree_transport(
        actor_class=resolved.actor_class,
        spec=resolved.spec,
        talent_tree_rows=resolved.rows,
        repo_root=repo_root,
    )
    raw_forms = result.get("transport_forms")
    transport_forms: dict[str, Any] = raw_forms if isinstance(raw_forms, dict) else {}
    raw_validation = result.get("validation")
    validation: dict[str, Any] = raw_validation if isinstance(raw_validation, dict) else {}
    transport_status = "validated" if transport_forms.get("simc_split_talents") else "raw_only"
    updated_packet: dict[str, Any] | None = None
    if resolved.packet is not None:
        updated_packet = _refreshed_transport_packet(
            packet=resolved.packet,
            resolved=resolved,
            transport_forms=transport_forms,
            validation=validation,
        )
        packet_status = updated_packet.get("transport_status")
        transport_status = packet_status if isinstance(packet_status, str) else transport_status
    return {
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
        "written_packet_path": None,
    }

def validate_transport_packet_payload(packet: PacketInput) -> dict[str, Any]:
    """What ``simc validate-talent-transport --build-packet`` reports for an in-memory packet.

    ``input.build_packet`` is ``packet.path``, so it is null for a packet that never touched disk.
    Raises ``ProviderError`` instead of exiting.
    """
    try:
        validated = validate_talent_transport_packet(packet.packet)
    except ValueError as exc:
        raise ProviderError("invalid_build_packet", str(exc)) from exc
    resolved = _checked_transport_input(_packet_transport_input(validated, packet.path, None, None))
    return _validate_talent_transport_payload(resolved, repo_root=None)

@dataclass(frozen=True, slots=True)
class DescribeOptions:
    """The APL view ``describe-build`` reports; the defaults are the command's own flag defaults."""

    apl_path: str | None = None
    targets: int = 1
    aoe_targets: int = 5
    list_name: str = "default"
    priority_limit: int = 8
    inactive_limit: int = 8

def _describe_build_payload(paths: RepoPaths, options: DescribeOptions, option_values: dict[str, Any]) -> dict[str, Any]:
    build_spec, identity = _identified_build_or_raise(paths, apl_path=options.apl_path, option_values=option_values)
    if not build_spec.actor_class or not build_spec.spec:
        raise _unidentified_build_error(purpose="build description", build_spec=build_spec, identity=identity)
    resolved = _resolve_path(paths, options.apl_path) if options.apl_path else _infer_default_apl_path(
        paths, actor_class=build_spec.actor_class, spec=build_spec.spec)
    if not resolved:
        raise ProviderError(
            "not_found",
            "Could not locate an APL file for the resolved build. Pass --apl-path explicitly.",
            details={"build_spec": _serialize_build_spec(build_spec), "identity": _serialize_build_identity(identity)},
        )
    _apl_or_raise(paths, str(resolved), list_name=options.list_name)
    try:
        primary_context, resolution = _prune_context(paths, build_spec, option_values, options.targets)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        raise _build_error(paths, exc, code="describe_build_failed") from exc
    aoe_context = dataclasses.replace(primary_context, targets=options.aoe_targets)
    primary = _describe_target_payload(resolved, primary_context, start_list=options.list_name,
                                       priority_limit=options.priority_limit, inactive_limit=options.inactive_limit)
    aoe = _describe_target_payload(resolved, aoe_context, start_list=options.list_name,
                                   priority_limit=options.priority_limit, inactive_limit=options.inactive_limit)
    primary_actions = list(dict.fromkeys(primary.get("active_action_names") or _action_names(primary["active_priority"])))
    aoe_actions = list(dict.fromkeys(aoe.get("active_action_names") or _action_names(aoe["active_priority"])))
    return {
        "kind": "describe_build",
        "apl": _apl_payload(paths, resolved),
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
            "primary_targets": options.targets,
            "aoe_targets": options.aoe_targets,
            "new_active_actions_in_aoe": [action for action in aoe_actions if action not in primary_actions],
            "missing_active_actions_in_aoe": [action for action in primary_actions if action not in aoe_actions],
        },
    }

def describe_build_payload(build: PacketInput | str, options: DescribeOptions) -> dict[str, Any]:
    """What ``simc describe-build`` reports for one in-memory build. Raises ``ProviderError`` instead of exiting."""
    return _describe_build_payload(discover_repo(), options, _in_memory_build_option_values(build))

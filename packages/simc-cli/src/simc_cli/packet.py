from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from simc_cli.branch import (
    IntentExplanation,
    explain_intent,
    is_helper_decision,
    summarize_branches,
    summarize_intent,
    summarize_list_decisions,
)
from simc_cli.prune import PruneContext
from simc_cli.sim import FirstCastResult, run_first_casts, summarize_first_casts


@dataclass(slots=True)
class FirstCastPacket:
    action: str
    samples: int
    found: int
    min_time: float | None
    avg_time: float | None
    max_time: float | None
    results: list[FirstCastResult]


@dataclass(slots=True)
class AnalysisPacket:
    apl_path: Path
    start_list: str
    focus_list: str
    dispatch_certainty: str
    top_level_runtime_unresolved: bool
    runtime_sensitive_priorities: list[str]
    escalation_reasons: list[str]
    next_steps: list[str]
    intent_lines: list[str]
    explained_intent: IntentExplanation
    first_casts: list[FirstCastPacket]
    branch_summary: object


def build_analysis_packet(
    paths,
    apl_path: str | Path,
    context: PruneContext,
    *,
    start_list: str = "default",
    intent_limit: int = 6,
    explain_limit: int = 8,
    runtime_scan_limit: int = 8,
    first_cast_profile: str | Path | None = None,
    first_cast_actions: list[str] | None = None,
    first_cast_seeds: int = 5,
    first_cast_max_time: int = 60,
    first_cast_targets: int | None = None,
    first_cast_fight_style: str = "Patchwerk",
) -> AnalysisPacket:
    normalized_path = Path(apl_path).expanduser().resolve()
    summary = summarize_branches(normalized_path, context, start_list=start_list)
    focus_list = summary.guaranteed_dispatch or start_list
    intent_lines = summarize_intent(normalized_path, context, focus_list, limit=intent_limit)
    explained = explain_intent(normalized_path, context, focus_list, limit=explain_limit)

    early_decisions = summarize_list_decisions(normalized_path, context, focus_list)
    runtime_sensitive = [
        f"L{decision.line_no} {decision.action_label}"
        for decision in early_decisions
        if decision.status == "possible"
        and decision.reason == "depends on runtime-only state"
        and not is_helper_decision(decision)
    ][:runtime_scan_limit]

    escalation_reasons: list[str] = []
    if summary.unresolved_branches:
        escalation_reasons.append("top-level dispatch still has unresolved branch candidates")
    if summary.branch_decisions and not summary.guaranteed_dispatch:
        escalation_reasons.append("no guaranteed run_action_list dispatch from the starting list")
    if runtime_sensitive:
        escalation_reasons.append(f"early priorities in {focus_list} depend on runtime-only state")
    first_casts = collect_first_cast_packets(
        paths,
        first_cast_profile,
        first_cast_actions or [],
        first_cast_seeds,
        first_cast_max_time,
        first_cast_targets if first_cast_targets is not None else context.targets,
        first_cast_fight_style,
    )

    return AnalysisPacket(
        apl_path=normalized_path,
        start_list=start_list,
        focus_list=focus_list,
        dispatch_certainty="guaranteed" if summary.guaranteed_dispatch else "unresolved",
        top_level_runtime_unresolved=bool(summary.unresolved_branches),
        runtime_sensitive_priorities=runtime_sensitive,
        escalation_reasons=escalation_reasons,
        next_steps=recommended_next_steps(start_list, focus_list, bool(summary.unresolved_branches), bool(runtime_sensitive)),
        intent_lines=intent_lines,
        explained_intent=explained,
        first_casts=first_casts,
        branch_summary=summary,
    )


def recommended_next_steps(start_list: str, focus_list: str, has_unresolved_branches: bool,
                           has_runtime_sensitive_priorities: bool) -> list[str]:
    steps: list[str] = []
    steps.append(f"run priority on `{focus_list}` with the exact talent string before summarizing the rotation")
    if has_unresolved_branches:
        steps.append(f"run apl-branch-trace from `{start_list}` to inspect unresolved branch paths")
    if has_runtime_sensitive_priorities:
        steps.append(f"use trace-action on early `{focus_list}` actions before making runtime claims")
        steps.append(f"use inactive-actions on `{focus_list}` to confirm talent-gated branches are excluded")
        steps.append("validate timing with first-cast or log-actions before treating priorities as an opener")
    else:
        steps.append(f"use opener on `{focus_list}` for a static early-action preview")
    if not steps:
        steps.append("use trace-action for any action-specific implementation questions")
    return steps


def collect_first_cast_packets(
    paths,
    profile: str | Path | None,
    actions: list[str],
    seeds: int,
    max_time: int,
    targets: int,
    fight_style: str,
) -> list[FirstCastPacket]:
    if not profile or not actions:
        return []
    packets: list[FirstCastPacket] = []
    for action in actions:
        results = run_first_casts(paths, profile, action, seeds, max_time, targets, fight_style)
        summary = summarize_first_casts(results)
        packets.append(
            FirstCastPacket(
                action=action,
                samples=int(summary["samples"]),
                found=int(summary["found"]),
                min_time=summary.get("min"),
                avg_time=summary.get("avg"),
                max_time=summary.get("max"),
                results=results,
            )
        )
    return packets

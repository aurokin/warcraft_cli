from __future__ import annotations

from dataclasses import dataclass

from simc_cli.apl import AplEntry, group_entries, parse_apl
from simc_cli.prune import ConditionOutcome, PruneContext, evaluate_condition_outcome, explanation_for_condition

LIST_NAME_ALIASES = {
    "trinkets": "trinket helper",
    "cooldowns": "cooldown helper",
    "precombat": "precombat setup",
    "math_for_wizards": "build logic helper",
    "illicit_doping": "burst items helper",
    "es": "eternity surge helper",
    "fb": "fire breath helper",
}

TOKEN_ALIASES = {
    "st": "single-target",
    "aoe": "aoe",
    "sc": "scalecommander",
    "fs": "flameshaper",
    "es": "eternity surge",
    "fb": "fire breath",
}

ACTION_ROLE_ALIASES = {
    "metamorphosis": "burst",
    "dragonrage": "burst",
    "tip_the_scales": "burst_setup",
}


@dataclass(slots=True)
class TraceLine:
    depth: int
    text: str


@dataclass(slots=True)
class BranchDecision:
    target_list: str
    line_no: int
    status: str
    reason: str


@dataclass(slots=True)
class BranchSummary:
    start_list: str
    guaranteed_dispatch: str | None
    guaranteed_dispatch_line: int | None
    guaranteed_dispatch_reason: str | None
    dead_branches: list[str]
    unresolved_branches: list[str]
    shadowed_lines: list[str]
    branch_decisions: dict[str, BranchDecision]


@dataclass(slots=True)
class ListDecision:
    list_name: str
    line_no: int
    action_label: str
    action_name: str
    target_list: str | None
    status: str
    reason: str


@dataclass(slots=True)
class IntentExplanation:
    focus_list: str
    setup: list[str]
    helpers: list[str]
    burst: list[str]
    priorities: list[str]


@dataclass(slots=True)
class BranchComparison:
    start_list: str
    left_dispatch: str | None
    right_dispatch: str | None
    dispatch_changed: bool
    decision_changes: list[str]
    left_focus_list: str
    right_focus_list: str
    focus_list_same: bool
    focus_changes: list[str]
    left_focus_preview: list[str]
    right_focus_preview: list[str]
    left_focus_intent: list[str]
    right_focus_intent: list[str]


@dataclass(slots=True)
class FocusResolution:
    start_list: str
    focus_list: str
    path: list[str]
    reason: str


def trace_apl(apl_path, context: PruneContext, start_list: str = "default", max_depth: int = 6) -> list[TraceLine]:
    entries = parse_apl(apl_path)
    grouped = group_entries(entries)
    lines: list[TraceLine] = []
    _trace_list(grouped, start_list, context, lines, depth=0, max_depth=max_depth, visited=[])
    return lines


def summarize_branches(apl_path, context: PruneContext, start_list: str = "default") -> BranchSummary:
    entries = parse_apl(apl_path)
    grouped = group_entries(entries)
    branch_entries = [entry for entry in grouped.get(start_list, []) if entry.kind == "run_action_list" and entry.target_list]

    guaranteed_dispatch = None
    guaranteed_dispatch_line = None
    guaranteed_dispatch_reason = None
    dead_branches: list[str] = []
    unresolved_branches: list[str] = []
    shadowed_lines: list[str] = []
    branch_decisions: dict[str, BranchDecision] = {}
    stop_here = False

    for entry in branch_entries:
        if stop_here:
            shadowed_lines.append(f"L{entry.line_no} -> {entry.target_list}")
            branch_decisions[entry.target_list] = BranchDecision(
                target_list=entry.target_list,
                line_no=entry.line_no,
                status="shadowed",
                reason="shadowed by earlier guaranteed dispatch",
            )
            continue
        outcome = _entry_outcome(entry, context)
        reason = explanation_for_condition(entry.condition, context, outcome) if entry.condition else "no condition"
        if outcome.guaranteed_true:
            guaranteed_dispatch = entry.target_list
            guaranteed_dispatch_line = entry.line_no
            guaranteed_dispatch_reason = reason
            branch_decisions[entry.target_list] = BranchDecision(
                target_list=entry.target_list,
                line_no=entry.line_no,
                status="guaranteed",
                reason=reason,
            )
            stop_here = True
        elif outcome.guaranteed_false:
            dead_branches.append(f"L{entry.line_no} -> {entry.target_list}: {reason}")
            branch_decisions[entry.target_list] = BranchDecision(
                target_list=entry.target_list,
                line_no=entry.line_no,
                status="dead",
                reason=reason,
            )
        else:
            unresolved_branches.append(f"L{entry.line_no} -> {entry.target_list}: {reason}")
            branch_decisions[entry.target_list] = BranchDecision(
                target_list=entry.target_list,
                line_no=entry.line_no,
                status="possible",
                reason=reason,
            )

    return BranchSummary(
        start_list=start_list,
        guaranteed_dispatch=guaranteed_dispatch,
        guaranteed_dispatch_line=guaranteed_dispatch_line,
        guaranteed_dispatch_reason=guaranteed_dispatch_reason,
        dead_branches=dead_branches,
        unresolved_branches=unresolved_branches,
        shadowed_lines=shadowed_lines,
        branch_decisions=branch_decisions,
    )


def resolve_focus_list(apl_path, context: PruneContext, start_list: str = "default", max_depth: int = 8) -> FocusResolution:
    current = start_list
    path = [start_list]
    seen = {start_list}
    reason = "start_list"

    for _ in range(max_depth):
        summary = summarize_branches(apl_path, context, start_list=current)
        if summary.guaranteed_dispatch and summary.guaranteed_dispatch not in seen:
            current = summary.guaranteed_dispatch
            path.append(current)
            seen.add(current)
            reason = "guaranteed_run_dispatch"
            continue

        decisions = summarize_list_decisions(apl_path, context, current)
        non_helper_active = [
            decision
            for decision in decisions
            if decision.status != "dead" and not is_helper_decision(decision)
        ]
        if len(non_helper_active) == 1:
            only = non_helper_active[0]
            if only.action_name == "call_action_list" and only.status == "guaranteed" and only.target_list and only.target_list not in seen:
                current = only.target_list
                path.append(current)
                seen.add(current)
                reason = "guaranteed_call_leaf"
                continue
        break

    return FocusResolution(start_list=start_list, focus_list=current, path=path, reason=reason)


def summarize_list_decisions(apl_path, context: PruneContext, list_name: str) -> list[ListDecision]:
    entries = parse_apl(apl_path)
    grouped = group_entries(entries)
    decisions: list[ListDecision] = []
    for entry in grouped.get(list_name, []):
        outcome = _entry_outcome(entry, context)
        reason = explanation_for_condition(entry.condition, context, outcome) if entry.condition else "no condition"
        target = f" -> {entry.target_list}" if entry.target_list else ""
        decisions.append(
            ListDecision(
                list_name=list_name,
                line_no=entry.line_no,
                action_label=f"{entry.action}{target}",
                action_name=entry.action,
                target_list=entry.target_list,
                status=_status_text(outcome),
                reason=reason,
            )
        )
    return decisions


def active_priority_decisions(apl_path, context: PruneContext, list_name: str, *, include_helpers: bool = False) -> list[ListDecision]:
    decisions = summarize_list_decisions(apl_path, context, list_name)
    rows = [decision for decision in decisions if decision.status != "dead"]
    if not include_helpers:
        rows = [decision for decision in rows if not is_helper_decision(decision)]
    return rows


def inactive_priority_decisions(apl_path, context: PruneContext, list_name: str, *, talent_only: bool = False) -> list[ListDecision]:
    decisions = summarize_list_decisions(apl_path, context, list_name)
    rows = [decision for decision in decisions if decision.status == "dead"]
    if talent_only:
        rows = [decision for decision in rows if "talent." in decision.reason]
    return rows


def summarize_intent(apl_path, context: PruneContext, list_name: str, limit: int = 6) -> list[str]:
    decisions = summarize_list_decisions(apl_path, context, list_name)
    primary = [decision for decision in decisions if not is_helper_decision(decision)]
    helper = [decision for decision in decisions if is_helper_decision(decision)]
    lines: list[str] = []
    seen: set[str] = set()
    for decision in [*primary, *helper]:
        if decision.status == "dead":
            continue
        qualifier = "always" if decision.status == "guaranteed" else "situational"
        label = humanize_action_label(decision.action_label)
        if decision.reason in {"no condition", "depends on runtime-only state"}:
            text = f"{qualifier}: {label}"
        else:
            text = f"{qualifier}: {label} [{decision.reason}]"
        if text not in seen:
            lines.append(text)
            seen.add(text)
        if len(lines) >= limit:
            break
    return lines


def explain_intent(apl_path, context: PruneContext, list_name: str, limit: int = 8) -> IntentExplanation:
    decisions = summarize_list_decisions(apl_path, context, list_name)
    setup: list[str] = []
    helpers: list[str] = []
    burst: list[str] = []
    priorities: list[str] = []
    seen: set[str] = set()

    for decision in decisions:
        if decision.status == "dead":
            continue
        line = intent_line_for_decision(decision)
        if line in seen:
            continue
        seen.add(line)

        role = classify_decision_role(decision)
        if role == "setup" and len(setup) < limit:
            setup.append(line)
        elif role in {"helper", "burst_helper"} and len(helpers) < limit:
            helpers.append(line)
        elif role in {"burst", "burst_setup"} and len(burst) < limit:
            burst.append(line)
        elif len(priorities) < limit:
            priorities.append(line)

    return IntentExplanation(
        focus_list=list_name,
        setup=setup[:limit],
        helpers=helpers[:limit],
        burst=burst[:limit],
        priorities=priorities[:limit],
    )


def compare_branch_summaries(left: BranchSummary, right: BranchSummary) -> BranchComparison:
    targets = sorted(set(left.branch_decisions) | set(right.branch_decisions))
    decision_changes: list[str] = []
    for target in targets:
        left_decision = left.branch_decisions.get(target)
        right_decision = right.branch_decisions.get(target)
        if left_decision and right_decision:
            if left_decision.status != right_decision.status or left_decision.reason != right_decision.reason:
                decision_changes.append(
                    f"{target}: {left_decision.status} -> {right_decision.status}"
                    f" | left={left_decision.reason}"
                    f" | right={right_decision.reason}"
                )
        elif left_decision:
            decision_changes.append(f"{target}: only in left ({left_decision.status})")
        elif right_decision:
            decision_changes.append(f"{target}: only in right ({right_decision.status})")

    left_focus_list = left.guaranteed_dispatch or left.start_list
    right_focus_list = right.guaranteed_dispatch or right.start_list
    return BranchComparison(
        start_list=left.start_list,
        left_dispatch=left.guaranteed_dispatch,
        right_dispatch=right.guaranteed_dispatch,
        dispatch_changed=left.guaranteed_dispatch != right.guaranteed_dispatch,
        decision_changes=decision_changes,
        left_focus_list=left_focus_list,
        right_focus_list=right_focus_list,
        focus_list_same=left_focus_list == right_focus_list,
        focus_changes=[],
        left_focus_preview=[],
        right_focus_preview=[],
        left_focus_intent=[],
        right_focus_intent=[],
    )


def attach_focus_comparison(comparison: BranchComparison, apl_path, left_context: PruneContext,
                            right_context: PruneContext, max_changes: int = 8) -> BranchComparison:
    left_decisions = summarize_list_decisions(apl_path, left_context, comparison.left_focus_list)
    right_decisions = summarize_list_decisions(apl_path, right_context, comparison.right_focus_list)
    left_preview = [format_list_decision(decision) for decision in left_decisions[:max_changes]]
    right_preview = [format_list_decision(decision) for decision in right_decisions[:max_changes]]

    focus_changes: list[str] = []
    if comparison.focus_list_same:
        right_by_line = {decision.line_no: decision for decision in right_decisions}
        for left_decision in left_decisions:
            right_decision = right_by_line.get(left_decision.line_no)
            if not right_decision:
                continue
            if left_decision.status != right_decision.status or left_decision.reason != right_decision.reason:
                focus_changes.append(
                    f"L{left_decision.line_no} {left_decision.action_label}: "
                    f"{left_decision.status} -> {right_decision.status}"
                    f" | left={left_decision.reason}"
                    f" | right={right_decision.reason}"
                )
            if len(focus_changes) >= max_changes:
                break

    comparison.focus_changes = focus_changes
    comparison.left_focus_preview = left_preview
    comparison.right_focus_preview = right_preview
    comparison.left_focus_intent = summarize_intent(apl_path, left_context, comparison.left_focus_list)
    comparison.right_focus_intent = summarize_intent(apl_path, right_context, comparison.right_focus_list)
    return comparison


def humanize_action_label(label: str) -> str:
    if " -> " in label:
        action, target = label.split(" -> ", 1)
        target_text = humanize_list_name(target)
        if action == "call_action_list":
            return f"run {target_text}"
        if action == "run_action_list":
            return f"enter {target_text}"
        return f"{action.replace('_', ' ')} -> {target_text}"
    return label.replace("_", " ")


def humanize_list_name(list_name: str) -> str:
    if list_name in LIST_NAME_ALIASES:
        return LIST_NAME_ALIASES[list_name]
    parts = list_name.split("_")
    return " ".join(TOKEN_ALIASES.get(part, part) for part in parts)


def is_helper_decision(decision: ListDecision) -> bool:
    if " -> " not in decision.action_label:
        return False
    action, target = decision.action_label.split(" -> ", 1)
    if action != "call_action_list":
        return False
    return target in LIST_NAME_ALIASES or target.endswith("_variables") or target.endswith("_helper")


def intent_line_for_decision(decision: ListDecision) -> str:
    qualifier = "always" if decision.status == "guaranteed" else "situational"
    label = humanize_action_label(decision.action_label)
    if decision.reason in {"no condition", "depends on runtime-only state"}:
        return f"{qualifier}: {label}"
    return f"{qualifier}: {label} [{decision.reason}]"


def classify_decision_role(decision: ListDecision) -> str | None:
    if decision.action_name == "call_action_list" and decision.target_list:
        if decision.target_list in LIST_NAME_ALIASES:
            return "helper"
        if decision.target_list.endswith("_variables") or decision.target_list.endswith("_helper"):
            return "helper"
    return ACTION_ROLE_ALIASES.get(decision.action_name)


def format_list_decision(decision: ListDecision) -> str:
    return f"L{decision.line_no} {decision.action_label}: {decision.status} ({decision.reason})"


def _trace_list(grouped, list_name: str, context: PruneContext,
                lines: list[TraceLine], depth: int, max_depth: int, visited: list[str]) -> None:
    if depth > max_depth:
        lines.append(TraceLine(depth=depth, text=f"[{list_name}] max depth reached"))
        return
    if list_name in visited:
        lines.append(TraceLine(depth=depth, text=f"[{list_name}] recursion detected"))
        return
    entries = grouped.get(list_name, [])
    lines.append(TraceLine(depth=depth, text=f"[{list_name}]"))
    local_visited = visited + [list_name]
    stop_here = False
    for entry in entries:
        if stop_here:
            lines.append(TraceLine(depth=depth + 1, text=f"L{entry.line_no}: shadowed by earlier guaranteed run_action_list"))
            continue
        outcome = _entry_outcome(entry, context)
        lines.append(TraceLine(depth=depth + 1, text=_label_for_entry(entry, outcome)))
        if entry.condition:
            reason = explanation_for_condition(entry.condition, context, outcome)
            if reason != "depends on runtime-only state":
                lines.append(TraceLine(depth=depth + 2, text=f"because: {reason}"))
        if entry.target_list and entry.kind == "call_action_list" and outcome.can_be_true:
            _trace_list(grouped, entry.target_list, context, lines, depth + 2, max_depth, local_visited)
        if entry.target_list and entry.kind == "run_action_list":
            if outcome.guaranteed_true:
                _trace_list(grouped, entry.target_list, context, lines, depth + 2, max_depth, local_visited)
                stop_here = True
            elif outcome.can_be_true:
                lines.append(TraceLine(depth=depth + 2, text=f"possible path into [{entry.target_list}]"))
                _trace_list(grouped, entry.target_list, context, lines, depth + 3, max_depth, local_visited)
                lines.append(TraceLine(depth=depth + 2, text="fallthrough remains possible if condition is false"))


def _entry_outcome(entry: AplEntry, context: PruneContext) -> ConditionOutcome:
    if not entry.condition:
        return ConditionOutcome(can_be_true=True, can_be_false=False)
    return evaluate_condition_outcome(entry.condition, context)


def _label_for_entry(entry: AplEntry, outcome: ConditionOutcome) -> str:
    target = f" -> {entry.target_list}" if entry.target_list else ""
    condition = f" if={entry.condition}" if entry.condition else ""
    return f"L{entry.line_no}: {_status_text(outcome):10} {entry.action}{target}{condition}"


def _status_text(outcome: ConditionOutcome) -> str:
    if outcome.guaranteed_true:
        return "guaranteed"
    if outcome.guaranteed_false:
        return "dead"
    return "possible"

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

ACTION_RE = re.compile(r"^actions(?:\.([A-Za-z0-9_]+))?(\+?=)(.*)$")
TALENT_RE = re.compile(r"talent\.([A-Za-z0-9_]+)")


@dataclass(slots=True)
class AplEntry:
    line_no: int
    list_name: str
    op: str
    action: str
    raw_args: str
    condition: str | None
    raw: str
    target_list: str | None
    kind: str
    talent_source_lines: tuple[tuple[str, int], ...] | None = None


@dataclass(frozen=True, slots=True)
class _ActionSource:
    text: str
    line_no: int
    op: str
    raw: str


def _strip_comment(line: str) -> str:
    comment = line.find("#")
    if comment != -1:
        line = line[:comment]
    return line.strip()


def parse_apl(path: str | Path) -> list[AplEntry]:
    """Parse effective action lists, retaining each action's source line and assignment.

    SimC replaces a list on ``=`` and concatenates text on ``+=``. A slash
    starts another action; an append without one continues the previous action.
    Conditions use SimC's ``%`` division operator.
    """
    lists: dict[str, list[list[_ActionSource]]] = {}
    separator_at_end: dict[str, bool] = {}
    apl_path = Path(path)
    for line_no, raw_line in enumerate(apl_path.read_text().splitlines(), start=1):
        line = _strip_comment(raw_line)
        if not line:
            continue
        match = ACTION_RE.match(line)
        if not match:
            continue
        list_name = match.group(1) or "default"
        op = match.group(2)
        if op == "=":
            lists[list_name] = []
            separator_at_end[list_name] = False
        actions = lists.setdefault(list_name, [])
        value = match.group(3)
        continues_previous = op == "+=" and not separator_at_end.get(list_name, False)
        for index, body in enumerate(value.split("/")):
            if not body:
                continue
            source = _ActionSource(text=body, line_no=line_no, op=op, raw=line)
            if index == 0 and continues_previous and actions:
                actions[-1].append(source)
            else:
                actions.append([source])
        if value:
            separator_at_end[list_name] = value.endswith("/")
    entries = [_parse_action(sources, list_name=list_name) for list_name, actions in lists.items() for sources in actions]
    return sorted(entries, key=lambda entry: entry.line_no)


def _parse_action(sources: list[_ActionSource], *, list_name: str) -> AplEntry:
    body = "".join(source.text for source in sources)
    action, _, raw_args = body.partition(",")
    action, raw_args = action.strip(), raw_args.strip()
    options: dict[str, str] = {}
    for part in raw_args.split(","):
        key, separator, value = part.partition("=")
        if separator:
            options[key.strip()] = value.strip()
    kind = action if action in {"call_action_list", "run_action_list"} else "action"
    return AplEntry(
        line_no=sources[0].line_no, list_name=list_name, op=sources[0].op, action=action, raw_args=raw_args,
        condition=options.get("if"), raw="\n".join(source.raw for source in sources),
        target_list=options.get("name") if kind != "action" else None, kind=kind,
        talent_source_lines=_talent_source_lines(body, sources),
    )


def _talent_source_lines(body: str, sources: list[_ActionSource]) -> tuple[tuple[str, int], ...]:
    """Locate each effective talent reference in the source fragment that starts it."""
    references: list[tuple[str, int]] = []
    for match in TALENT_RE.finditer(body):
        offset = 0
        for source in sources:
            offset += len(source.text)
            if match.start() < offset:
                references.append((match.group(1), source.line_no))
                break
    return tuple(references)


def group_entries(entries: list[AplEntry]) -> dict[str, list[AplEntry]]:
    grouped: dict[str, list[AplEntry]] = defaultdict(list)
    for entry in entries:
        grouped[entry.list_name].append(entry)
    return dict(grouped)


def talent_refs(entries: list[AplEntry]) -> dict[str, list[int]]:
    refs: dict[str, set[int]] = defaultdict(set)
    for entry in entries:
        sources = entry.talent_source_lines
        if sources is None:
            sources = tuple((talent, entry.line_no) for talent in TALENT_RE.findall(entry.raw_args))
        for talent, line_no in sources:
            refs[talent].add(line_no)
    return {talent: sorted(lines) for talent, lines in sorted(refs.items())}


def action_counts(entries: list[AplEntry]) -> Counter[str]:
    counter: Counter[str] = Counter()
    for entry in entries:
        key = entry.target_list if entry.target_list else entry.action
        counter[key] += 1
    return counter


def mermaid_graph(entries: list[AplEntry]) -> str:
    grouped = group_entries(entries)
    lines = ["flowchart TD"]
    seen_edges: set[tuple[str, str, str]] = set()
    for list_name, list_entries in grouped.items():
        node_name = "default" if list_name == "default" else list_name
        lines.append(f"  {node_name}[{list_name}]")
        for entry in list_entries:
            if not entry.target_list:
                continue
            edge = (list_name, entry.target_list, entry.kind)
            if edge in seen_edges:
                continue
            seen_edges.add(edge)
            label = "call" if entry.kind == "call_action_list" else "run"
            lines.append(f"  {node_name} -->|{label}| {entry.target_list}")
    return "\n".join(lines)


def trace_action_entries(entries: list[AplEntry], action: str) -> list[AplEntry]:
    return [entry for entry in entries if entry.action == action]

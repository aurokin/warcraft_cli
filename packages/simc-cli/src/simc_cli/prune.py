from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from simc_cli.apl import AplEntry

TOKEN_RE = re.compile(r"\s*(>=|<=|!=|=|<|>|\(|\)|!|&|\||[A-Za-z0-9_.-]+)")
COMPARISON_RE = re.compile(r"^([A-Za-z0-9_.]+)\s*(>=|<=|!=|=|<|>)\s*([A-Za-z0-9_.-]+)$")


class TruthValue(StrEnum):
    TRUE = "eligible"
    FALSE = "dead"
    UNKNOWN = "unknown"


@dataclass(slots=True)
class PruneContext:
    enabled_talents: set[str]
    disabled_talents: set[str]
    targets: int
    talent_sources: dict[str, str] | None = None


@dataclass(slots=True)
class PrunedEntry:
    entry: AplEntry
    state: TruthValue
    reason: str


@dataclass(slots=True)
class ConditionOutcome:
    can_be_true: bool
    can_be_false: bool

    @property
    def state(self) -> TruthValue:
        if self.can_be_true and not self.can_be_false:
            return TruthValue.TRUE
        if not self.can_be_true and self.can_be_false:
            return TruthValue.FALSE
        return TruthValue.UNKNOWN

    @property
    def guaranteed_true(self) -> bool:
        return self.can_be_true and not self.can_be_false

    @property
    def guaranteed_false(self) -> bool:
        return not self.can_be_true and self.can_be_false


def split_csv_values(values: list[str]) -> set[str]:
    result: set[str] = set()
    for value in values:
        for piece in value.split(","):
            piece = piece.strip()
            if piece:
                result.add(piece)
    return result


def prune_entries(entries: list[AplEntry], context: PruneContext) -> list[PrunedEntry]:
    pruned: list[PrunedEntry] = []
    for entry in entries:
        if not entry.condition:
            pruned.append(PrunedEntry(entry=entry, state=TruthValue.TRUE, reason="no condition"))
            continue
        outcome = evaluate_condition_outcome(entry.condition, context)
        pruned.append(
            PrunedEntry(
                entry=entry,
                state=outcome.state,
                reason=explanation_for_condition(entry.condition, context, outcome),
            )
        )
    return pruned


def evaluate_condition_outcome(condition: str, context: PruneContext) -> ConditionOutcome:
    parser = ConditionParser(condition, context)
    if not parser.tokens:
        return ConditionOutcome(can_be_true=True, can_be_false=True)
    return parser.parse()


def explanation_for_condition(condition: str, context: PruneContext, outcome: ConditionOutcome) -> str:
    if outcome.state == TruthValue.UNKNOWN:
        return "depends on runtime-only state"
    atoms = extract_known_atoms(condition, context)
    if atoms:
        return "; ".join(atoms)
    return "resolved from known inputs"


def extract_known_atoms(condition: str, context: PruneContext) -> list[str]:
    explanations: list[str] = []
    seen: set[str] = set()
    for atom in TOKEN_RE.findall(condition):
        if atom in {"(", ")", "!", "&", "|", "=", "!=", "<", ">", "<=", ">="}:
            continue
        if atom.startswith("talent."):
            talent = atom.split(".", 1)[1]
            enabled = talent in context.enabled_talents and talent not in context.disabled_talents
            source = ""
            if context.talent_sources and talent in context.talent_sources:
                source = f" [{context.talent_sources[talent]}]"
            elif talent in context.disabled_talents:
                source = " [manual]"
            text = f"{atom}={'true' if enabled else 'false'}{source}"
        elif atom == "active_enemies":
            text = f"active_enemies={context.targets}"
        elif atom.startswith("spell_targets."):
            text = f"{atom}={context.targets}"
        else:
            continue
        if text not in seen:
            explanations.append(text)
            seen.add(text)
    return explanations


class ConditionParser:
    def __init__(self, condition: str, context: PruneContext):
        self.tokens = [token for token in TOKEN_RE.findall(condition) if token.strip()]
        self.index = 0
        self.context = context

    def parse(self) -> ConditionOutcome:
        return self.parse_or()

    def parse_or(self) -> ConditionOutcome:
        result = self.parse_and()
        while self.peek() == "|":
            self.consume("|")
            result = or_value(result, self.parse_and())
        return result

    def parse_and(self) -> ConditionOutcome:
        result = self.parse_unary()
        while self.peek() == "&":
            self.consume("&")
            result = and_value(result, self.parse_unary())
        return result

    def parse_unary(self) -> ConditionOutcome:
        if self.peek() == "!":
            self.consume("!")
            return not_value(self.parse_unary())
        return self.parse_primary()

    def parse_primary(self) -> ConditionOutcome:
        if self.peek() == "(":
            self.consume("(")
            result = self.parse_or()
            if self.peek() == ")":
                self.consume(")")
            return result
        atom_tokens: list[str] = []
        while self.peek() not in {None, "&", "|", ")"}:
            atom_tokens.append(self.consume())
        return eval_atom("".join(atom_tokens), self.context)

    def peek(self) -> str | None:
        if self.index >= len(self.tokens):
            return None
        return self.tokens[self.index]

    def consume(self, expected: str | None = None) -> str:
        token = self.peek()
        if token is None:
            raise ValueError("Unexpected end of expression")
        if expected is not None and token != expected:
            raise ValueError(f"Expected {expected}, got {token}")
        self.index += 1
        return token


def eval_atom(atom: str, context: PruneContext) -> ConditionOutcome:
    if not atom:
        return ConditionOutcome(can_be_true=True, can_be_false=True)
    if atom.startswith("talent."):
        talent = atom.split(".", 1)[1]
        if talent in context.disabled_talents:
            return ConditionOutcome(can_be_true=False, can_be_false=True)
        if talent in context.enabled_talents:
            return ConditionOutcome(can_be_true=True, can_be_false=False)
        return ConditionOutcome(can_be_true=False, can_be_false=True)
    match = COMPARISON_RE.match(atom)
    if match:
        left, op, right = match.groups()
        left_value = resolve_value(left, context)
        right_value = resolve_value(right, context)
        if left_value is None or right_value is None:
            return ConditionOutcome(can_be_true=True, can_be_false=True)
        if compare_values(left_value, op, right_value):
            return ConditionOutcome(can_be_true=True, can_be_false=False)
        return ConditionOutcome(can_be_true=False, can_be_false=True)
    if atom.isdigit():
        return ConditionOutcome(can_be_true=int(atom) != 0, can_be_false=int(atom) == 0)
    return ConditionOutcome(can_be_true=True, can_be_false=True)


def resolve_value(token: str, context: PruneContext) -> int | None:
    if token.isdigit():
        return int(token)
    if token == "active_enemies":
        return context.targets
    if token.startswith("spell_targets."):
        return context.targets
    return None


def compare_values(left: int, op: str, right: int) -> bool:
    if op == "=":
        return left == right
    if op == "!=":
        return left != right
    if op == "<":
        return left < right
    if op == "<=":
        return left <= right
    if op == ">":
        return left > right
    if op == ">=":
        return left >= right
    raise ValueError(f"Unsupported operator: {op}")


def not_value(value: ConditionOutcome) -> ConditionOutcome:
    return ConditionOutcome(can_be_true=value.can_be_false, can_be_false=value.can_be_true)


def and_value(left: ConditionOutcome, right: ConditionOutcome) -> ConditionOutcome:
    return ConditionOutcome(
        can_be_true=left.can_be_true and right.can_be_true,
        can_be_false=left.can_be_false or right.can_be_false,
    )


def or_value(left: ConditionOutcome, right: ConditionOutcome) -> ConditionOutcome:
    return ConditionOutcome(
        can_be_true=left.can_be_true or right.can_be_true,
        can_be_false=left.can_be_false and right.can_be_false,
    )

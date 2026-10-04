"""Static evaluation of SimC APL conditions against a known build and target count.

A condition is parsed with SimC's own token set and operator precedence (engine/sim/expressions.cpp).
Each sub-expression evaluates to the range of values it can take at runtime: build facts (talents,
hero tree, target count) are exact, everything else spans every number. A condition is eligible or
dead only when that range proves it; anything the parser cannot read is unknown, never dead.
"""

from __future__ import annotations

import math
import operator
import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from enum import StrEnum

from simc_cli.apl import AplEntry

# One SimC expression token: a number, an identifier, or an operator. Anything else fails the parse.
TOKEN_RE = re.compile(
    r"\s*(\d+(?:\.\d*)?|[A-Za-z][A-Za-z0-9_.]*|~!=|~<=|~>=|~=|~<|~>|!~|<=|>=|<\?|>\?|!=|%%|&&|\|\||\^\^|==|[()+\-*%@&|^~=!<>])"
)
# A talent atom that names one entry of a node, as in `talent.hand_of_frost_4`.
INDEXED_TALENT_RE = re.compile(r"^(.+)_\d+$")
RUNTIME_ONLY = "depends on runtime-only state"


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
    # Exact ranks for taken talents whose rank the decode reported; other taken talents are rank >= 1.
    talent_ranks: dict[str, int] = field(default_factory=dict)
    # The build's hero tree as a SimC token (`shadopan`), or None when it is not known.
    hero_tree: str | None = None
    # Talents the spec can take but the build did not. SimC creates no action named after one.
    untaken_talents: set[str] = field(default_factory=set)
    # False makes every talent atom span every rank: see without_talents.
    talents_known: bool = True


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


@dataclass(frozen=True, slots=True)
class Span:
    """The closed range of values an expression can take."""

    lo: float
    hi: float

    @property
    def point(self) -> bool:
        return self.lo == self.hi

    def outcome(self) -> ConditionOutcome:
        return ConditionOutcome(can_be_true=not (self.lo == 0 == self.hi), can_be_false=self.lo <= 0 <= self.hi)


ANY = Span(-math.inf, math.inf)
FALSE = Span(0, 0)
TRUE = Span(1, 1)


def _from_outcome(outcome: ConditionOutcome) -> Span:
    return Span(0 if outcome.can_be_false else 1, 1 if outcome.can_be_true else 0)


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
        outcome, reason = entry_verdict(entry, context)
        pruned.append(PrunedEntry(entry=entry, state=outcome.state, reason=reason))
    return pruned


def entry_verdict(entry: AplEntry, context: PruneContext) -> tuple[ConditionOutcome, str]:
    """Whether an APL row can run for this build, and why.

    A row whose action is a talent the build did not take is dead whatever its condition says.
    """
    if entry.action in context.untaken_talents:
        return ConditionOutcome(can_be_true=False, can_be_false=True), f"talent.{entry.action}=false [action]"
    if not entry.condition:
        return ConditionOutcome(can_be_true=True, can_be_false=False), "no condition"
    outcome = evaluate_condition_outcome(entry.condition, context)
    return outcome, explanation_for_condition(entry.condition, context, outcome)


def evaluate_condition_outcome(condition: str, context: PruneContext) -> ConditionOutcome:
    tokens = tokenize_condition(condition)
    if not tokens:
        return ConditionOutcome(can_be_true=True, can_be_false=True)
    parser = ConditionParser(tokens, context)
    try:
        value = parser.parse_binary(1)
    except ValueError:
        return ConditionOutcome(can_be_true=True, can_be_false=True)
    if parser.index != len(tokens):
        # Trailing tokens mean the condition was not read in full, so no verdict on it can be trusted.
        return ConditionOutcome(can_be_true=True, can_be_false=True)
    return value.outcome()


def tokenize_condition(condition: str) -> list[str] | None:
    """Split a condition into SimC tokens, or None when it holds a character SimC would not read."""
    tokens: list[str] = []
    position = 0
    text = condition.rstrip()
    while position < len(text):
        match = TOKEN_RE.match(text, position)
        if not match:
            return None
        tokens.append(match.group(1))
        position = match.end()
    return tokens


def explanation_for_condition(condition: str, context: PruneContext, outcome: ConditionOutcome) -> str:
    if outcome.state == TruthValue.UNKNOWN:
        return RUNTIME_ONLY
    atoms = extract_known_atoms(condition, context)
    if atoms:
        return "; ".join(atoms)
    return "resolved from known inputs"


def extract_known_atoms(condition: str, context: PruneContext) -> list[str]:
    explanations: list[str] = []
    for token in tokenize_condition(condition) or []:
        text = _known_atom_text(token, context)
        if text and text not in explanations:
            explanations.append(text)
    return explanations


def _known_atom_text(atom: str, context: PruneContext) -> str | None:
    parts = atom.split(".")
    if parts[0] == "talent" and len(parts) >= 2:
        name = parts[1]
        rank = _talent_rank(name, context)
        if not rank.point and rank.lo == 0:
            return None
        source = ""
        if context.talent_sources and name in context.talent_sources:
            source = f" [{context.talent_sources[name]}]"
        elif name in context.disabled_talents:
            source = " [manual]"
        if parts[2:] == ["rank"] and rank.point:
            return f"talent.{name}.rank={int(rank.lo)}{source}"
        return f"talent.{name}={'true' if rank.lo > 0 else 'false'}{source}"
    if parts[0] == "hero_tree" and len(parts) == 2 and context.hero_tree:
        return f"{atom}={'true' if parts[1] == context.hero_tree else 'false'}"
    if atom == "active_enemies" or parts[0] == "spell_targets":
        return f"{atom}={context.targets}"
    return None


def without_talents(context: PruneContext) -> PruneContext:
    """The same build with its talents and hero tree unknown; a row dead under the build but not here is talent-gated."""
    return replace(context, talents_known=False, hero_tree=None, untaken_talents=set())


def _talent_rank(name: str, context: PruneContext) -> Span:
    if not context.talents_known:
        return Span(0, math.inf)
    if name in context.disabled_talents:
        return FALSE
    if name in context.enabled_talents:
        rank = context.talent_ranks.get(name)
        return Span(rank, rank) if rank else Span(1, math.inf)
    indexed = INDEXED_TALENT_RE.match(name)
    if indexed and indexed.group(1) in context.enabled_talents:
        # SimC picks one entry of a taken node by index; which entry the build took is not tracked.
        return Span(0, math.inf)
    return FALSE


def eval_atom(atom: str, context: PruneContext) -> Span:
    """Value of one identifier, following SimC's player expression rules for the build facts it knows."""
    parts = atom.split(".")
    if parts[0] == "talent" and len(parts) in (2, 3):
        rank = _talent_rank(parts[1], context)
        suffix = parts[2] if len(parts) == 3 else "enabled"
        if suffix == "rank":
            return rank
        enabled = _from_outcome(rank.outcome())
        if suffix == "enabled":
            return enabled
        if suffix == "disabled":
            return _from_outcome(_not(enabled.outcome()))
        return ANY
    if parts[0] == "hero_tree" and len(parts) == 2 and context.hero_tree:
        return TRUE if parts[1] == context.hero_tree else FALSE
    if atom == "active_enemies" or (parts[0] == "spell_targets" and len(parts) == 2):
        return Span(context.targets, context.targets)
    return ANY


def _not(outcome: ConditionOutcome) -> ConditionOutcome:
    return ConditionOutcome(can_be_true=outcome.can_be_false, can_be_false=outcome.can_be_true)


def _compare(left: Span, op: str, right: Span) -> Span:
    """Whether ``left op right`` holds for every, no, or only some values in the two ranges."""
    if op in {"=", "==", "~="}:
        if left.point and right.point:
            return TRUE if left.lo == right.lo else FALSE
        return FALSE if left.hi < right.lo or left.lo > right.hi else Span(0, 1)
    if op in {"!=", "~!="}:
        return _from_outcome(_not(_compare(left, "=", right).outcome()))
    if op in {"<", "~<"}:
        return _compare(right, ">", left)
    if op in {"<=", "~<="}:
        return _compare(right, ">=", left)
    if op in {">", "~>"}:
        return TRUE if left.lo > right.hi else FALSE if left.hi <= right.lo else Span(0, 1)
    if op in {">=", "~>="}:
        return TRUE if left.lo >= right.hi else FALSE if left.hi < right.lo else Span(0, 1)
    return Span(0, 1)  # `~` and `!~` test membership in a runtime spell list


def _logic(left: Span, op: str, right: Span) -> Span:
    a, b = left.outcome(), right.outcome()
    if op in {"&", "&&"}:
        return _from_outcome(ConditionOutcome(a.can_be_true and b.can_be_true, a.can_be_false or b.can_be_false))
    if op in {"|", "||"}:
        return _from_outcome(ConditionOutcome(a.can_be_true or b.can_be_true, a.can_be_false and b.can_be_false))
    same = (a.can_be_true and b.can_be_true) or (a.can_be_false and b.can_be_false)
    differ = (a.can_be_true and b.can_be_false) or (a.can_be_false and b.can_be_true)
    return _from_outcome(ConditionOutcome(can_be_true=differ, can_be_false=same))


ARITHMETIC: dict[str, Callable[[float, float], float]] = {
    "+": operator.add,
    "-": operator.sub,
    "*": operator.mul,
    "<?": max,
    ">?": min,
}


def _arithmetic(left: Span, op: str, right: Span) -> Span:
    # Only exact operands are folded; SimC's `%` (divide) and `%%` (modulus) are left to runtime.
    if op in ARITHMETIC and left.point and right.point and math.isfinite(left.lo) and math.isfinite(right.lo):
        value = ARITHMETIC[op](left.lo, right.lo)
        return Span(value, value)
    return ANY


# SimC operator precedence: higher binds tighter.
PRECEDENCE = {
    "|": 1, "||": 1,
    "^": 2, "^^": 2,
    "&": 3, "&&": 3,
    **dict.fromkeys(["=", "==", "!=", "<", "<=", ">", ">=", "~", "!~", "~=", "~!=", "~<", "~<=", "~>", "~>="], 4),
    "<?": 5, ">?": 5,
    "+": 6, "-": 6,
    "*": 7, "%": 7, "%%": 7,
}


class ConditionParser:
    """Precedence-climbing parser that evaluates as it reads."""

    def __init__(self, tokens: list[str], context: PruneContext):
        self.tokens = tokens
        self.index = 0
        self.context = context

    def parse_binary(self, min_precedence: int) -> Span:
        left = self.parse_unary()
        while (op := self.peek()) in PRECEDENCE and PRECEDENCE[op] >= min_precedence:
            self.index += 1
            right = self.parse_binary(PRECEDENCE[op] + 1)
            level = PRECEDENCE[op]
            if level <= 3:
                left = _logic(left, op, right)
            elif level == 4:
                left = _compare(left, op, right)
            else:
                left = _arithmetic(left, op, right)
        return left

    def parse_unary(self) -> Span:
        symbol = self.consume()
        if symbol == "!":
            return _from_outcome(_not(self.parse_unary().outcome()))
        if symbol == "-":
            value = self.parse_unary()
            return Span(-value.hi, -value.lo)
        if symbol == "+":
            return self.parse_unary()
        if symbol == "@":
            self.parse_unary()
            return ANY
        if symbol == "(":
            value = self.parse_binary(1)
            if self.consume() != ")":
                raise ValueError("unbalanced parenthesis")
            return value
        if symbol[0].isdigit():
            number = float(symbol)
            return Span(number, number)
        if not symbol[0].isalpha():
            raise ValueError(f"unexpected symbol {symbol}")
        if symbol.lower() in {"floor", "ceil"} and self.peek() == "(":
            self.parse_unary()
            return ANY
        return eval_atom(symbol, self.context)

    def peek(self) -> str | None:
        return self.tokens[self.index] if self.index < len(self.tokens) else None

    def consume(self) -> str:
        token = self.peek()
        if token is None:
            raise ValueError("Unexpected end of expression")
        self.index += 1
        return token

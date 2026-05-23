from __future__ import annotations

from simc_cli.apl import AplEntry
from simc_cli.prune import PruneContext, TruthValue, evaluate_condition_outcome, explanation_for_condition, prune_entries


def test_target_count_comparison() -> None:
    context = PruneContext(enabled_talents=set(), disabled_talents=set(), targets=3)
    outcome = evaluate_condition_outcome("active_enemies>=3", context)
    assert outcome.guaranteed_true


def test_talent_negation_false_when_enabled() -> None:
    context = PruneContext(enabled_talents={"voidfall"}, disabled_talents=set(), targets=1)
    outcome = evaluate_condition_outcome("!talent.voidfall", context)
    assert outcome.guaranteed_false


def test_mixed_known_and_unknown_condition_is_unknown() -> None:
    context = PruneContext(enabled_talents={"mass_disintegrate"}, disabled_talents=set(), targets=3)
    outcome = evaluate_condition_outcome("active_enemies>=3&talent.mass_disintegrate&buff.dragonrage.up", context)
    assert outcome.state == TruthValue.UNKNOWN


def test_explanation_includes_talent_source() -> None:
    context = PruneContext(
        enabled_talents={"mass_disintegrate"},
        disabled_talents=set(),
        targets=3,
        talent_sources={"mass_disintegrate": "hero"},
    )
    outcome = evaluate_condition_outcome("active_enemies>=3&talent.mass_disintegrate", context)
    assert explanation_for_condition("active_enemies>=3&talent.mass_disintegrate", context,
                                     outcome) == "active_enemies=3; talent.mass_disintegrate=true [hero]"


def test_prune_entries_marks_unconditional_action_as_eligible() -> None:
    entry = AplEntry(
        line_no=10,
        list_name="default",
        op="+=",
        action="void_ray",
        raw_args="",
        condition=None,
        raw="actions+=/void_ray",
        target_list=None,
        kind="action",
    )
    pruned = prune_entries([entry], PruneContext(enabled_talents=set(), disabled_talents=set(), targets=1))
    assert pruned[0].state == TruthValue.TRUE
    assert pruned[0].reason == "no condition"

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


def _state(condition: str, context: PruneContext) -> str:
    return evaluate_condition_outcome(condition, context).state.value


def test_talent_suffixes_follow_simc_for_a_taken_talent() -> None:
    """`talent.X.enabled` and comparisons used to look up `X.enabled` / `X>=1` and call a taken talent dead."""
    context = PruneContext(enabled_talents={"surging_totem"}, disabled_talents=set(), targets=1)

    assert _state("talent.surging_totem.enabled", context) == "eligible"
    assert _state("!talent.surging_totem.enabled", context) == "dead"
    assert _state("talent.surging_totem>=1", context) == "eligible"
    assert _state("talent.surging_totem.disabled", context) == "dead"
    assert _state("talent.lashing_flames.enabled", context) == "dead"
    assert _state("active_enemies=1&!talent.surging_totem.enabled", context) == "dead"


def test_talent_rank_is_exact_only_when_the_decode_reported_it() -> None:
    unknown_rank = PruneContext(enabled_talents={"x"}, disabled_talents=set(), targets=1)
    known_rank = PruneContext(enabled_talents={"x"}, disabled_talents=set(), targets=1, talent_ranks={"x": 1})

    assert _state("talent.x.rank>=2", unknown_rank) == "unknown"
    assert _state("talent.x.rank>=1", unknown_rank) == "eligible"
    assert _state("talent.x.rank>=2", known_rank) == "dead"
    assert explanation_for_condition("talent.x.rank>=2", known_rank, evaluate_condition_outcome("talent.x.rank>=2", known_rank)) == (
        "talent.x.rank=1"
    )


def test_an_indexed_talent_of_a_taken_node_is_unknown_not_dead() -> None:
    context = PruneContext(enabled_talents={"hand_of_frost"}, disabled_talents=set(), targets=1)

    assert _state("!talent.hand_of_frost_4", context) == "unknown"
    assert _state("talent.frostfire_bolt_2", context) == "dead"


def test_hero_tree_resolves_from_the_decoded_tree() -> None:
    deathbringer = PruneContext(enabled_talents=set(), disabled_talents=set(), targets=1, hero_tree="deathbringer")
    no_tree = PruneContext(enabled_talents=set(), disabled_talents=set(), targets=1)

    assert _state("hero_tree.deathbringer", deathbringer) == "eligible"
    assert _state("hero_tree.sanlayn", deathbringer) == "dead"
    assert _state("hero_tree.sanlayn", no_tree) == "unknown"
    outcome = evaluate_condition_outcome("hero_tree.deathbringer", deathbringer)
    assert explanation_for_condition("hero_tree.deathbringer", deathbringer, outcome) == "hero_tree.deathbringer=true"


def test_arithmetic_inside_a_condition_is_read_in_full() -> None:
    """Hellcaller's wither line: the parser used to stop at the first arithmetic `)` and drop the
    `|refreshable` alternative, so it called the line dead on a talent the alternative does not need."""
    context = PruneContext(enabled_talents={"soul_fire"}, disabled_talents=set(), targets=1)
    wither = (
        "(((dot.wither.remains-5*(action.chaos_bolt.in_flight&talent.internal_combustion))<dot.wither.duration*0.3)"
        "|refreshable|(dot.wither.remains-action.chaos_bolt.execute_time)<5&action.chaos_bolt.in_flight)"
        "&(!talent.soul_fire|cooldown.soul_fire.remains+action.soul_fire.cast_time>(dot.wither.remains-5))"
        "&target.time_to_die>8&!action.soul_fire.in_flight_to_target"
    )

    assert _state(wither, context) == "unknown"
    assert _state("time>=(8*(1))|buff.combustion.remains>6", context) == "unknown"
    assert _state("active_enemies>=2+1", PruneContext(enabled_talents=set(), disabled_talents=set(), targets=3)) == "eligible"


def test_arithmetic_follows_simc_precedence_and_operators() -> None:
    """`*` binds tighter than `+`, which binds tighter than SimC's max `<?` and min `>?`; unary minus negates."""
    context = PruneContext(enabled_talents=set(), disabled_talents=set(), targets=3)

    for condition in ("active_enemies=1+1*2", "active_enemies=2+1<?3", "active_enemies=(1<?3)", "active_enemies=(5>?3)", "active_enemies=-1+4"):
        assert _state(condition, context) == "eligible", condition


def test_a_condition_that_does_not_parse_in_full_is_unknown() -> None:
    context = PruneContext(enabled_talents=set(), disabled_talents=set(), targets=1)

    assert _state("talent.x)|active_enemies>0", context) == "unknown"
    assert _state("talent.x&(active_enemies>1", context) == "unknown"
    assert _state("talent.x$1", context) == "unknown"

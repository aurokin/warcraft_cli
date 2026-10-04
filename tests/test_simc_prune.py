from __future__ import annotations

from simc_cli.apl import AplEntry
from simc_cli.prune import PruneContext, TruthValue, evaluate_condition_outcome, explanation_for_condition, prune_entries
from simc_cli.trait_data import parse_trait_table


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

    for condition in ("active_enemies=1+1*2", "active_enemies=2+1<?3", "active_enemies=3<?1+1", "active_enemies=(1<?3)", "active_enemies=(5>?3)", "active_enemies=-1+4"):
        assert _state(condition, context) == "eligible", condition


def test_a_condition_that_does_not_parse_in_full_is_unknown() -> None:
    context = PruneContext(enabled_talents=set(), disabled_talents=set(), targets=1)

    assert _state("talent.x)|active_enemies>0", context) == "unknown"
    assert _state("talent.x&(active_enemies>1", context) == "unknown"
    assert _state("talent.x$1", context) == "unknown"


def _entry(action: str, condition: str | None = None) -> AplEntry:
    raw = f"actions+=/{action}" + (f",if={condition}" if condition else "")
    return AplEntry(line_no=1, list_name="default", op="+=", action=action, raw_args="", condition=condition, raw=raw,
                    target_list=None, kind="action")


def test_an_action_that_is_an_untaken_talent_is_dead_whatever_its_condition() -> None:
    """Frost's `comet_storm` row has no condition and was reported guaranteed for builds without Comet Storm,
    which SimC never creates an action for."""
    context = PruneContext(enabled_talents={"flurry"}, disabled_talents=set(), targets=5, untaken_talents={"comet_storm"})

    pruned = prune_entries([_entry("comet_storm"), _entry("comet_storm", "active_enemies>=3"), _entry("flurry")], context)

    assert [(row.state, row.reason) for row in pruned] == [
        (TruthValue.FALSE, "talent.comet_storm=false [action]"),
        (TruthValue.FALSE, "talent.comet_storm=false [action]"),
        (TruthValue.TRUE, "no condition"),
    ]


# Rows copied from SimC's trait_data.inc: Frost's Flurry and Comet Storm, and Brewmaster's Keg Smash and
# the Celestial Brew / Celestial Infusion choice node.
SYNTHETIC_TRAIT_ROWS = """
  { 2,  8,  80243,  62178, 1,  0,  85246,   44614,      0,      0,  4,  2, 100,  "Flurry", {   64,    0,    0,    0 }, {    0,    0,    0,    0 },   0, 0 },
  { 2,  8,  80251,  62185, 1, 20,  85254, 1247777,      0,      0, 10,  6, 100,  "Comet Storm", {   64,    0,    0,    0 }, {    0,    0,    0,    0 },   0, 0 },
  { 2, 10, 124865, 101088, 1,  0, 129703,  121253,      0,      0,  1,  5, 100,  "Keg Smash", {  268,    0,    0,    0 }, {    0,    0,    0,    0 },   0, 0 },
  { 2, 10, 124841, 101067, 1,  8, 129679,  322507,      0,      0,  5,  3, 100,  "Celestial Brew", {  268,    0,    0,    0 }, {    0,    0,    0,    0 },   0, 2 },
  { 2, 10, 136146, 101067, 1,  8, 140901, 1241059,      0,      0,  5,  3, 200,  "Celestial Infusion", {  268,    0,    0,    0 }, {    0,    0,    0,    0 },   0, 2 },
"""


def test_untaken_talents_leave_out_the_other_entry_of_a_choice_the_build_made() -> None:
    """SimC turns Brewmaster's `celestial_brew` action into Celestial Infusion when that choice is taken."""
    table = parse_trait_table(SYNTHETIC_TRAIT_ROWS)

    frost = table.untaken_talents({"flurry"}, class_id=8, spec_id=64, include_hero=False)
    brewmaster = table.untaken_talents({"keg_smash", "celestial_infusion"}, class_id=10, spec_id=268, include_hero=False)

    assert frost == {"comet_storm"}
    assert brewmaster == set()
    assert table.untaken_talents({"keg_smash"}, class_id=10, spec_id=268, include_hero=False) == {
        "celestial_brew", "celestial_infusion"
    }

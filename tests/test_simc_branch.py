from __future__ import annotations

from pathlib import Path

from simc_cli.branch import (
    compare_branches,
    explain_intent,
    inactive_priority_decisions,
    resolve_focus_list,
    summarize_branches,
    summarize_intent,
)
from simc_cli.packet import build_analysis_packet
from simc_cli.prune import PruneContext


def _sample_apl(tmp_path: Path) -> Path:
    apl = tmp_path / "evoker_devastation.simc"
    apl.write_text(
        "\n".join(
            [
                "actions+=/run_action_list,name=aoe,if=active_enemies>=3",
                "actions+=/run_action_list,name=st,if=active_enemies<3",
                "actions.aoe+=/fire_breath",
                "actions.st+=/disintegrate,if=talent.mass_disintegrate",
            ]
        )
        + "\n"
    )
    return apl


def test_summarize_branches_and_intent(tmp_path: Path) -> None:
    apl = _sample_apl(tmp_path)
    summary = summarize_branches(apl, PruneContext(enabled_talents={"mass_disintegrate"}, disabled_talents=set(), targets=3))
    intent = summarize_intent(apl, PruneContext(enabled_talents={"mass_disintegrate"}, disabled_talents=set(), targets=1), "st")
    explained = explain_intent(apl, PruneContext(enabled_talents={"mass_disintegrate"}, disabled_talents=set(), targets=1), "st")
    assert summary.guaranteed_dispatch == "aoe"
    assert "always: disintegrate [talent.mass_disintegrate=true]" in intent
    assert explained.priorities


def test_branch_analysis_uses_replaced_inline_list_and_order_independent_dispatch(tmp_path: Path) -> None:
    apl = tmp_path / "replace.simc"
    apl.write_text(
        "actions=run_action_list,name=obsolete\n"
        "actions=run_action_list,if=active_enemies>=3,name=aoe/run_action_list,if=active_enemies<3,name=st\n"
        "actions.aoe=fireball\n"
        "actions.st=frostbolt/ice_lance,if=talent.shatter\n"
    )
    context = PruneContext(enabled_talents={"shatter"}, disabled_talents=set(), targets=1)
    summary = summarize_branches(apl, context)
    assert summary.guaranteed_dispatch == "st"
    assert "obsolete" not in summary.branch_decisions
    assert resolve_focus_list(apl, context).focus_list == "st"
    assert len(summarize_intent(apl, context, "st")) == 2


def test_inline_actions_are_distinguished_in_talent_filter_and_build_comparison(tmp_path: Path) -> None:
    apl = tmp_path / "inline.simc"
    apl.write_text("actions=fireball,if=talent.hot_streak/frostbolt,if=active_enemies>2/ice_lance\n")
    without = PruneContext(enabled_talents=set(), disabled_talents={"hot_streak"}, targets=1)
    with_talent = PruneContext(enabled_talents={"hot_streak"}, disabled_talents=set(), targets=1)
    inactive = inactive_priority_decisions(apl, without, "default", talent_only=True)
    assert [row.action_name for row in inactive] == ["fireball"]
    comparison = compare_branches(apl, without, with_talent)
    assert len(comparison.focus_changes) == 1
    assert "fireball: dead -> guaranteed" in comparison.focus_changes[0]


def test_appended_condition_prevents_false_static_dispatch(tmp_path: Path) -> None:
    apl = tmp_path / "append.simc"
    apl.write_text("actions=run_action_list,name=aoe\nactions+=,if=0/run_action_list,name=st\n")
    context = PruneContext(enabled_talents=set(), disabled_talents=set(), targets=1)
    summary = summarize_branches(apl, context)
    assert summary.guaranteed_dispatch == "st"
    assert summary.branch_decisions["aoe"].status == "dead"


def test_compare_branch_summaries_and_focus_comparison(tmp_path: Path) -> None:
    apl = _sample_apl(tmp_path)
    left_context = PruneContext(enabled_talents={"mass_disintegrate"}, disabled_talents=set(), targets=3)
    right_context = PruneContext(enabled_talents=set(), disabled_talents=set(), targets=1)
    comparison = compare_branches(apl, left_context, right_context)
    assert comparison.dispatch_changed is True
    assert comparison.left_focus_intent
    assert comparison.right_focus_intent == []


def test_focus_descends_past_utility_actions_and_helper_lists(tmp_path: Path) -> None:
    """Retribution's default list is `auto_attack, rebuke, call cooldowns, call generators`; the plain
    actions used to count as rival rotation rows, so priority stopped at `default`."""
    apl = tmp_path / "paladin_retribution.simc"
    apl.write_text(
        "actions=auto_attack\n"
        "actions+=/rebuke\n"
        "actions+=/call_action_list,name=cooldowns\n"
        "actions+=/call_action_list,name=generators\n"
        "actions.generators=blade_of_justice\n"
    )

    focus = resolve_focus_list(apl, PruneContext(enabled_talents=set(), disabled_talents=set(), targets=1))

    assert (focus.focus_list, focus.path, focus.reason) == ("generators", ["default", "generators"], "guaranteed_call_leaf")


def test_focus_stays_put_while_two_rotation_lists_can_run(tmp_path: Path) -> None:
    apl = tmp_path / "monk_windwalker.simc"
    apl.write_text(
        "actions=call_action_list,name=default_st,if=active_enemies=1\n"
        "actions+=/call_action_list,name=fallback\n"
        "actions.default_st=tiger_palm\n"
        "actions.fallback=blackout_kick\n"
    )

    assert resolve_focus_list(apl, PruneContext(enabled_talents=set(), disabled_talents=set(), targets=1)).focus_list == "default"


def test_analysis_packet_and_priority_agree_on_a_two_level_focus(tmp_path: Path) -> None:
    """analysis-packet followed only the run_action_list level and told agents to read `st`, while
    priority resolved `st_core`."""
    apl = tmp_path / "two_level.simc"
    apl.write_text(
        "actions=run_action_list,name=st,if=active_enemies=1\n"
        "actions.st=call_action_list,name=st_core\n"
        "actions.st_core=tiger_palm\n"
    )
    context = PruneContext(enabled_talents=set(), disabled_talents=set(), targets=1)

    packet = build_analysis_packet(None, apl, context)

    assert packet.focus_list == resolve_focus_list(apl, context).focus_list == "st_core"


def test_focus_stays_on_a_rotation_list_that_ends_by_calling_a_smaller_one(tmp_path: Path) -> None:
    """Frost's `spellslinger` list is the rotation and falls back to `call movement` at the end."""
    apl = tmp_path / "mage_frost.simc"
    apl.write_text(
        "actions=run_action_list,name=spellslinger\n"
        "actions.spellslinger=comet_storm\n"
        "actions.spellslinger+=/frozen_orb\n"
        "actions.spellslinger+=/frostbolt\n"
        "actions.spellslinger+=/call_action_list,name=movement\n"
        "actions.movement=ice_lance\n"
    )

    assert resolve_focus_list(apl, PruneContext(enabled_talents=set(), disabled_talents=set(), targets=1)).focus_list == "spellslinger"


def test_talent_only_rows_are_the_ones_the_build_kills_not_the_ones_that_name_a_talent(tmp_path: Path) -> None:
    """The filter used to match "talent." in the reason: it dropped a row the hero tree kills and kept a
    row only the target count kills because its reason also listed a taken talent."""
    apl = tmp_path / "warlock_destruction.simc"
    apl.write_text(
        "actions=call_action_list,name=soul_harvester,if=hero_tree.soul_harvester\n"
        "actions+=/call_action_list,name=aoe_hc,if=active_enemies>=2&talent.wither\n"
        "actions+=/cataclysm\n"
        "actions+=/chaos_bolt\n"
    )
    context = PruneContext(enabled_talents={"wither"}, disabled_talents=set(), targets=1, hero_tree="hellcaller",
                           untaken_talents={"cataclysm"})

    rows = inactive_priority_decisions(apl, context, "default", talent_only=True)

    assert [row.action_label for row in rows] == ["call_action_list -> soul_harvester", "cataclysm"]


def test_branch_compare_sees_a_rotation_switch_made_through_call_action_list(tmp_path: Path) -> None:
    """Beast Mastery dispatches with call_action_list; only run_action_list rows used to count as dispatch."""
    apl = tmp_path / "hunter_beast_mastery.simc"
    apl.write_text(
        "actions=call_action_list,name=cds\n"
        "actions+=/call_action_list,name=st,if=active_enemies<2\n"
        "actions+=/call_action_list,name=cleave,if=active_enemies>1\n"
        # Dead on both sides, for different reasons: no change.
        "actions+=/call_action_list,name=aoe,if=active_enemies>5\n"
        "actions.st=kill_command\nactions.st+=/cobra_shot\n"
        "actions.cleave=multishot\nactions.cleave+=/kill_command\n"
        "actions.aoe=multishot\n"
    )
    left = PruneContext(enabled_talents=set(), disabled_talents=set(), targets=1)
    right = PruneContext(enabled_talents=set(), disabled_talents=set(), targets=3)

    comparison = compare_branches(apl, left, right)

    assert comparison.dispatch_changed is True
    assert [change.split(" |")[0] for change in comparison.decision_changes] == [
        "cleave: dead -> guaranteed", "st: guaranteed -> dead"
    ]

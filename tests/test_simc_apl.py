from __future__ import annotations

from pathlib import Path

from simc_cli.apl import action_counts, group_entries, mermaid_graph, parse_apl, talent_refs, trace_action_entries


def _sample_apl(tmp_path: Path) -> Path:
    apl = tmp_path / "monk_mistweaver.simc"
    apl.write_text(
        "\n".join(
            [
                "actions.precombat=flask",
                "actions=auto_attack",
                "actions+=/run_action_list,name=aoe,if=active_enemies>2",
                "actions+=/rising_sun_kick,if=talent.rising_mist.enabled",
                "actions.aoe=spinning_crane_kick",
                "actions.aoe+=/call_action_list,name=cds",
                "actions.cds=invoke_chiji,if=talent.invoke_chiji_the_red_crane.enabled",
            ]
        )
        + "\n"
    )
    return apl


def test_parse_apl_and_group_entries(tmp_path: Path) -> None:
    apl = _sample_apl(tmp_path)
    entries = parse_apl(apl)
    grouped = group_entries(entries)
    assert len(entries) == 7
    assert sorted(grouped) == ["aoe", "cds", "default", "precombat"]
    assert grouped["default"][1].target_list == "aoe"


def test_talent_refs_and_action_counts(tmp_path: Path) -> None:
    apl = _sample_apl(tmp_path)
    entries = parse_apl(apl)
    refs = talent_refs(entries)
    counts = action_counts(entries)
    assert refs["invoke_chiji_the_red_crane"] == [7]
    assert refs["rising_mist"] == [4]
    assert counts["aoe"] == 1
    assert counts["rising_sun_kick"] == 1


def test_mermaid_graph_and_trace_action(tmp_path: Path) -> None:
    apl = _sample_apl(tmp_path)
    entries = parse_apl(apl)
    graph = mermaid_graph(entries)
    traced = trace_action_entries(entries, "rising_sun_kick")
    assert "default -->|run| aoe" in graph
    assert "aoe -->|call| cds" in graph
    assert len(traced) == 1
    assert traced[0].line_no == 4


def test_replacement_discards_superseded_actions_and_empty_assignment_clears_list(tmp_path: Path) -> None:
    apl = tmp_path / "replacement.simc"
    apl.write_text(
        "actions=run_action_list,name=old,if=talent.old\n"
        "actions.old=fireball\n"
        "actions.precombat=flask\n"
        "actions+=/frostbolt\n"
        "actions=run_action_list,name=new\n"
        "actions.new=ice_lance\n"
        "actions.precombat=\n"
    )
    entries = parse_apl(apl)
    assert [entry.action for entry in group_entries(entries)["default"]] == ["run_action_list"]
    assert group_entries(entries)["default"][0].line_no == 5
    assert "precombat" not in group_entries(entries)
    assert talent_refs(entries) == {}
    assert "default -->|run| new" in mermaid_graph(entries)
    assert "default -->|run| old" not in mermaid_graph(entries)


def test_inline_actions_keep_individual_conditions_dispatch_targets_and_source(tmp_path: Path) -> None:
    apl = tmp_path / "inline.simc"
    raw = "actions=/frostbolt,if=talent.frost/call_action_list,if=talent.fire,name=burst/fireball"
    apl.write_text(raw + "\nactions+=/run_action_list,if=active_enemies>2,name=aoe\n")
    entries = parse_apl(apl)
    assert [entry.action for entry in entries] == ["frostbolt", "call_action_list", "fireball", "run_action_list"]
    assert [entry.condition for entry in entries] == ["talent.frost", "talent.fire", None, "active_enemies>2"]
    assert [entry.target_list for entry in entries] == [None, "burst", None, "aoe"]
    assert [entry.line_no for entry in entries] == [1, 1, 1, 2]
    assert entries[0].raw == raw
    assert "default -->|call| burst" in mermaid_graph(entries)
    assert "default -->|run| aoe" in mermaid_graph(entries)


def test_append_without_separator_continues_action_and_tracks_source_fragments(tmp_path: Path) -> None:
    apl = tmp_path / "fragmented.simc"
    apl.write_text(
        "actions=fireball\n"
        "actions+=,if=talent.hot_streak\n"
        "actions+=/frostbolt,if=talent.\n"
        "actions+=shatter/ice_lance\n"
    )
    entries = parse_apl(apl)
    assert [entry.action for entry in entries] == ["fireball", "frostbolt", "ice_lance"]
    assert [entry.condition for entry in entries] == ["talent.hot_streak", "talent.shatter", None]
    assert [entry.line_no for entry in entries] == [1, 3, 4]
    assert entries[0].raw == "actions=fireball\nactions+=,if=talent.hot_streak"
    assert entries[1].raw == "actions+=/frostbolt,if=talent.\nactions+=shatter/ice_lance"
    assert talent_refs(entries) == {"hot_streak": [2], "shatter": [3]}


def test_named_list_reset_removes_appended_source_fragments(tmp_path: Path) -> None:
    apl = tmp_path / "reset.simc"
    apl.write_text("actions.st=fire\nactions.st+=ball,if=talent.old\nactions.st=ice_lance\nactions.st+=\n")
    entries = parse_apl(apl)
    assert len(entries) == 1
    assert entries[0].action == "ice_lance"
    assert entries[0].raw == "actions.st=ice_lance"
    assert entries[0].line_no == 3
    assert talent_refs(entries) == {}


def test_separator_can_be_at_end_of_previous_assignment(tmp_path: Path) -> None:
    apl = tmp_path / "separator.simc"
    apl.write_text("actions=fireball/\nactions+=\nactions+=frostbolt/\nactions+=ice_lance\n")
    entries = parse_apl(apl)
    assert [entry.action for entry in entries] == ["fireball", "frostbolt", "ice_lance"]
    assert [entry.line_no for entry in entries] == [1, 3, 4]

from __future__ import annotations

from pathlib import Path

from simc_cli.packet import FirstCastOptions, build_analysis_packet
from simc_cli.prune import PruneContext


def test_build_analysis_packet_reports_guaranteed_dispatch(tmp_path: Path) -> None:
    apl_path = tmp_path / "evoker_devastation.simc"
    apl_path.write_text(
        "\n".join(
            [
                "actions+=/run_action_list,name=aoe,if=active_enemies>=3",
                "actions+=/run_action_list,name=st,if=active_enemies<3",
                "actions.aoe+=/fire_breath",
                "actions.st+=/disintegrate",
            ]
        )
        + "\n"
    )
    packet = build_analysis_packet(None, apl_path, PruneContext(enabled_talents=set(), disabled_talents=set(), targets=3))
    assert packet.focus_list == "aoe"
    assert packet.dispatch_certainty == "guaranteed"
    assert packet.escalation_reasons == []
    assert packet.first_casts == []
    assert "always: fire breath" in packet.intent_lines


def test_build_analysis_packet_flags_runtime_sensitive_priorities(tmp_path: Path) -> None:
    apl_path = tmp_path / "evoker_devastation.simc"
    apl_path.write_text(
        "\n".join(
            [
                "actions+=/run_action_list,name=st,if=active_enemies<3",
                "actions.st+=/disintegrate,if=buff.dragonrage.up",
            ]
        )
        + "\n"
    )
    packet = build_analysis_packet(None, apl_path, PruneContext(enabled_talents=set(), disabled_talents=set(), targets=1))
    assert packet.focus_list == "st"
    assert "early priorities in st depend on runtime-only state" in packet.escalation_reasons
    assert packet.runtime_sensitive_priorities == ["L2 disintegrate"]
    assert "validate timing with first-cast or log-actions before treating priorities as an opener" in packet.next_steps


def test_build_analysis_packet_can_include_first_casts(monkeypatch, tmp_path: Path) -> None:
    apl_path = tmp_path / "evoker_devastation.simc"
    apl_path.write_text("actions.st+=/disintegrate\n")

    monkeypatch.setattr(
        "simc_cli.packet.run_first_casts",
        lambda paths, profile, action, seeds, max_time, targets, fight_style: [
            type("Result", (), {"seed": 1, "time": 0.4, "log_path": tmp_path / "seed_1.log"})(),
        ],
    )

    packet = build_analysis_packet(
        object(),
        apl_path,
        PruneContext(enabled_talents=set(), disabled_talents=set(), targets=1),
        start_list="st",
        first_cast=FirstCastOptions(profile=tmp_path / "profile.simc", actions=("disintegrate",)),
    )
    assert len(packet.first_casts) == 1
    assert packet.first_casts[0].action == "disintegrate"
    assert packet.first_casts[0].found == 1

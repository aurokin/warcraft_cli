"""The arithmetic `compare-apls` and `variant-report` hand back: summaries, deltas, ranking, sampling.

Every number here comes from `compare.py` running over a captured SimC `json2` report, so a change in
how a summary is read or a delta is computed shows up as a wrong number rather than a passing stub.
"""

from __future__ import annotations

import json
from pathlib import Path

from simc_cli.compare import (
    ACTION_SAMPLE_ITERATIONS,
    VariantSummary,
    _extract_summary,
    comparison_report,
    variant_report_payload,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "simc"
# A real 4-iteration Arcane Mage run, trimmed to the fields compare.py reads.
CAPTURED_REPORT = json.loads((FIXTURES / "captured_arcane_mage_json2_report.json").read_text())


def _summary(label: str, *, report: dict[str, object] | None = None) -> VariantSummary:
    return _extract_summary(
        label=label,
        apl_path=Path(f"/repo/{label}.simc"),
        profile_path=Path(f"/tmp/{label}.simc"),
        json_path=Path(f"/tmp/{label}.json"),
        report=report or CAPTURED_REPORT,
    )


def test_extract_summary_reads_the_means_and_counts_the_recorded_action_sequence() -> None:
    summary = _summary("base")

    assert summary.dps == 85305.07794238535
    assert summary.fight_length == 30.666666666666668
    # 26 recorded rows, one of which is a `wait` with no action name.
    assert sum(summary.action_counts.values()) == 25
    assert summary.action_counts["arcane_missiles"] == 9
    assert "wait" not in summary.action_counts


def test_action_cpm_scales_the_recorded_counts_by_the_mean_fight_length() -> None:
    summary = _summary("base")

    assert summary.action_cpm["arcane_missiles"] == round(9 * 60.0 / 30.666666666666668, 2)
    assert summary.action_cpm["arcane_missiles"] == 17.61


def _variant(label: str, dps: float, action_counts: dict[str, int]) -> VariantSummary:
    return VariantSummary(
        label=label,
        apl_path=Path(f"/repo/{label}.simc"),
        profile_path=Path(f"/tmp/{label}.simc"),
        json_path=Path(f"/tmp/{label}.json"),
        dps=dps,
        dps_error=10.0,
        fight_length=60.0,
        action_counts=action_counts,
        action_cpm={name: round(count * 1.0, 2) for name, count in action_counts.items()},
    )


def _report() -> dict[str, object]:
    return comparison_report(
        [
            _variant("base", 1000.0, {"arcane_blast": 10, "arcane_barrage": 5}),
            _variant("slower", 950.0, {"arcane_blast": 8, "arcane_barrage": 5}),
            _variant("faster", 1100.0, {"arcane_blast": 14, "arcane_missiles": 2}),
        ],
        compare_dir=Path("/tmp/compare"),
        harness_path=Path("/tmp/harness.simc"),
        iterations=250,
        threads=4,
        validations=[],
    )


def test_comparison_report_ranks_by_dps_and_measures_every_variant_against_the_first() -> None:
    report = _report()

    assert [row["label"] for row in report["ranking"]] == ["faster", "base", "slower"]
    assert report["base"]["label"] == "base"
    deltas = {row["label"]: (row["dps_delta"], row["percent_delta"]) for row in report["comparisons"]}
    assert deltas == {"slower": (-50.0, -5.0), "faster": (100.0, 10.0)}


def test_comparison_report_ranks_action_deltas_by_magnitude_not_by_sign() -> None:
    faster = next(row for row in _report()["comparisons"] if row["label"] == "faster")

    # arcane_barrage lost 5 casts, arcane_blast gained 4, arcane_missiles gained 2.
    assert [row["action"] for row in faster["top_action_deltas"]] == ["arcane_barrage", "arcane_blast", "arcane_missiles"]
    assert faster["top_action_deltas"][0] == {
        "action": "arcane_barrage",
        "base_cpm": 5.0,
        "current_cpm": 0.0,
        "delta_cpm": -5.0,
    }


def test_comparison_report_says_the_action_numbers_come_from_one_recorded_iteration() -> None:
    """SimC records an action sequence for a single iteration; the payload must not hide that."""
    report = _report()

    assert report["sampling"]["iterations_simulated"] == 250
    assert report["sampling"]["action_sequence_iterations"] == ACTION_SAMPLE_ITERATIONS == 1
    assert "not from an iteration mean" in report["sampling"]["note"]
    assert all(row["action_sequence_iterations"] == 1 for row in report["ranking"])
    assert all(row["action_sequence_iterations"] == 1 for row in report["comparisons"])


def test_variant_report_carries_each_variant_delta_and_the_sampling_note() -> None:
    summary = variant_report_payload(_report())

    assert (summary["base_label"], summary["best_label"], summary["best_dps"]) == ("base", "faster", 1100.0)
    assert {row["label"]: row["delta_vs_base"] for row in summary["ranking"]} == {
        "faster": 100.0,
        "base": 0.0,
        "slower": -50.0,
    }
    assert summary["sampling"]["action_sequence_iterations"] == 1

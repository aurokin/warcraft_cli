"""The arithmetic `compare-apls` and `variant-report` hand back: summaries, deltas, ranking, sampling.

Every number here comes from `compare.py` running over a captured SimC `json2` report, so a change in
how a summary is read or a delta is computed shows up as a wrong number rather than a passing stub.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from simc_cli.compare import (
    OutputPathConflict,
    VariantSummary,
    _extract_summary,
    build_variant_profile,
    comparison_report,
    preflight_output_paths,
    variant_report_payload,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "simc"
# A real 4-iteration, 30-second MID2 Arcane Mage run (SimC 1210-01), trimmed to the fields compare.py
# and report.py read. SimC printed `DPS=362851.8712606381 DPS-Error=38374.55027921543` for it.
CAPTURED_REPORT = json.loads((FIXTURES / "captured_arcane_mage_json2_report.json").read_text())


@pytest.mark.parametrize("alias_kind", ["symlink", "hardlink"])
def test_profile_generation_rejects_aliases_to_source_files(tmp_path: Path, alias_kind: str) -> None:
    harness = tmp_path / "harness.simc"
    harness.write_text('mage="h"\nspec=frost\n')
    apl = tmp_path / "apl.simc"
    apl.write_text("actions=frostbolt\n")
    output = tmp_path / "variant.simc"
    if alias_kind == "symlink":
        output.symlink_to(apl)
    else:
        output.hardlink_to(apl)
    with pytest.raises(OutputPathConflict, match="would overwrite"):
        build_variant_profile(harness, apl, label="variant", out_dir=tmp_path)
    assert apl.read_text() == "actions=frostbolt\n"


def test_preflight_rejects_output_aliases_to_each_other(tmp_path: Path) -> None:
    output = tmp_path / "report.json"
    output.write_text("original")
    alias = tmp_path / "other.json"
    alias.hardlink_to(output)
    with pytest.raises(OutputPathConflict):
        preflight_output_paths([], [output, alias])


def test_profile_generation_writes_merged_profile_without_changing_sources(tmp_path: Path) -> None:
    harness = tmp_path / "harness.simc"
    harness.write_text('mage="h"\nspec=frost\n')
    apl = tmp_path / "apl.simc"
    apl.write_text("actions=frostbolt\n")
    output = build_variant_profile(harness, apl, label="base", out_dir=tmp_path / "out")
    assert output.read_text() == 'mage="h"\nspec=frost\nactions=frostbolt\n'
    assert harness.read_text() == 'mage="h"\nspec=frost\n'
    assert apl.read_text() == "actions=frostbolt\n"


def _summary(label: str, *, report: dict[str, object] | None = None) -> VariantSummary:
    return _extract_summary(
        label=label,
        apl_path=Path(f"/repo/{label}.simc"),
        profile_path=Path(f"/tmp/{label}.simc"),
        json_path=Path(f"/tmp/{label}.json"),
        report=report or CAPTURED_REPORT,
    )


def test_extract_summary_reads_the_dps_and_fight_length_means() -> None:
    summary = _summary("base")

    assert summary.dps == 362851.8712606381
    assert summary.fight_length == 31.0


def _report_with_stats() -> dict[str, object]:
    """The captured report plus a synthetic ``stats`` block whose means disagree with its one recorded sequence."""
    report = json.loads(json.dumps(CAPTURED_REPORT))
    report["sim"]["players"][0]["stats"] = [
        {"name": "arcane_missiles", "num_executes": {"sum": 30.0, "count": 4, "mean": 7.5}},
        {"name": "arcane_blast", "num_executes": {"sum": 46.0, "count": 4, "mean": 11.5}},
        {"name": "touch_of_the_magi", "num_executes": {"sum": 0.0, "count": 4, "mean": 0.0}},
    ]
    return report


def test_action_counts_are_simc_execute_means_over_every_iteration_not_the_one_recorded_sequence() -> None:
    """The captured action_sequence holds 6 arcane_missiles from its one iteration; the mean over all four is 7.5."""
    summary = _summary("base", report=_report_with_stats())

    assert summary.action_counts == {"arcane_missiles": 7.5, "arcane_blast": 11.5}


def test_extract_summary_reports_the_dps_error_simc_prints_not_effective_dps() -> None:
    """`collected_data.dpse` is effective DPS; SimC's DPS error is dps.mean_std_dev * confidence_estimator."""
    summary = _summary("base")

    assert summary.dps_error == 38374.55027921543


def test_action_cpm_scales_the_execute_means_by_the_mean_fight_length() -> None:
    summary = _summary("base", report=_report_with_stats())

    assert summary.action_cpm["arcane_missiles"] == round(7.5 * 60.0 / 31.0, 2) == 14.52


def _variant(label: str, dps: float, action_counts: dict[str, float]) -> VariantSummary:
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
        disclosures=[],
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


def test_comparison_report_says_where_the_action_numbers_come_from() -> None:
    report = _report()

    assert report["sampling"]["iterations_simulated"] == 250
    assert "mean executes per iteration" in report["sampling"]["note"]


def test_variant_report_carries_each_variant_delta_and_the_sampling_note() -> None:
    summary = variant_report_payload(_report())

    assert (summary["base_label"], summary["best_label"], summary["best_dps"]) == ("base", "faster", 1100.0)
    assert {row["label"]: row["delta_vs_base"] for row in summary["ranking"]} == {
        "faster": 100.0,
        "base": 0.0,
        "slower": -50.0,
    }
    assert {row["label"]: row["percent_vs_base"] for row in summary["ranking"]} == {
        "faster": 10.0,
        "base": 0.0,
        "slower": -5.0,
    }
    assert summary["sampling"]["iterations_simulated"] == 250

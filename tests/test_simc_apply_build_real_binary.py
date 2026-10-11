"""An explicit checkout and real SimC binary verify the geared build handoff."""

from __future__ import annotations

import json

from simc_cli.profile_build import apply_build_payload
from simc_cli.run import run_profile

from tests.test_simc_real_binary import repo as repo


def test_real_geared_profile_accepts_the_applied_stock_build(repo, tmp_path):
    stock = repo.root / "profiles" / "MID2" / "MID2_Mage_Frost.simc"
    text = stock.read_text()
    talents = next(line.partition("=")[2] for line in text.splitlines() if line.startswith("talents="))
    source = tmp_path / "geared.simc"
    destination = tmp_path / "verified.simc"
    source.write_text(
        text.replace(f"talents={talents}", "talents=INVALID")
        + "\nload_default_talents=1\nenable_all_talents=1\nclass_talents+=999999:1\nspec_talents+=999999:1\nhero_talents+=999999:1\n"
    )
    source_before = source.read_bytes()
    result = apply_build_payload(
        repo,
        profile_path=source,
        build=f"mage=Guide\nspec=frost\ntalents={talents}",
        out=destination,
    )
    assert source.read_bytes() == source_before
    output = destination.read_text()
    gear = [line for line in text.splitlines() if line.startswith(("head=", "neck=", "trinket1=", "main_hand="))]
    assert gear and all(line in output for line in gear)
    assert f"talents={talents}" in output and "load_default_talents" not in output
    assert result["validation"]["status"] == "decoded"
    report = tmp_path / "simulation.json"
    run = run_profile(
        repo,
        destination,
        simc_args=[
            "iterations=1",
            "threads=1",
            "target_error=0",
            "max_time=3",
            "fixed_time=1",
            f"json2={report}",
        ],
    )
    assert run.returncode == 0, run.stdout + run.stderr
    player = json.loads(report.read_text())["sim"]["players"][0]
    assert player["specialization"] == "Frost Mage"
    assert player["talents"] == talents
    assert player["collected_data"]["dps"]["mean"] > 0

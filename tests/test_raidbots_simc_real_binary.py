"""Compare Raidbots' first-actor handoff with the talents real SimC actually selected."""

from __future__ import annotations

import json
import os
import re
import shlex
from pathlib import Path

import pytest
from raidbots_cli.simc_input import classify_simc_input, simc_handoff
from simc_cli.build_input import BuildSpec, decode_build, tree_entries_string
from simc_cli.main import app
from simc_cli.repo import RepoPaths, discover_repo
from simc_cli.run import run_profile
from typer.testing import CliRunner


@pytest.fixture(scope="module")
def real_repo() -> RepoPaths:
    root = os.environ.get("WARCRAFT_SIMC_TESTS_REPO", "").strip()
    if not root:
        pytest.skip("Set WARCRAFT_SIMC_TESTS_REPO to exercise the real multi-actor SimC oracle")
    paths = discover_repo(root)
    assert paths.build_simc.is_file(), f"No built binary at {paths.build_simc}"
    return paths


def _talents(repo: RepoPaths, name: str) -> str:
    text = (repo.root / "profiles" / "MID1" / f"MID1_Death_Knight_{name}.simc").read_text()
    return next(line.partition("=")[2] for line in text.splitlines() if line.startswith("talents="))


@pytest.mark.parametrize("case", ["last_assignments", "second_actor", "copy_active", "split_override"])
def test_first_actor_handoff_matches_simcs_selected_build(real_repo: RepoPaths, tmp_path: Path, case: str) -> None:
    original = _talents(real_repo, "Frost")
    replacement = _talents(real_repo, "Frost_Rider")
    unholy = _talents(real_repo, "Unholy")
    text = f'deathknight="Main"\nspec=blood\nspec=frost\nlevel=90\nload_default_gear=1\ntalents={original}\n'
    if case == "last_assignments":
        text += f"talents={replacement}\n"
    elif case == "second_actor":
        text += f'deathknight="Other"\nspec=unholy\nlevel=90\nload_default_gear=1\ntalents={unholy}\n'
    elif case == "copy_active":
        text += f"copy=Other\ntalents={replacement}\nactive=Main\ntalents={replacement}\n"
    else:
        decoded = decode_build(real_repo, BuildSpec(actor_class="deathknight", spec="frost", talents=replacement))
        hero = tree_entries_string(decoded.talents_by_tree["hero"])
        assert hero, "The replacement stock profile must supply a hero-tree override"
        text += f"hero_talents={hero}\n"

    handoff = simc_handoff(text, classify_simc_input(text))
    assert handoff["classification"]["spec"] == "frost"
    command = next(row["command"] for row in handoff["suggested_simc_commands"] if shlex.split(row["command"])[1] == "decode-build")
    result = CliRunner().invoke(app, ["--repo-root", str(real_repo.root), *shlex.split(command)[1:]])
    assert result.exit_code == 0, result.stdout + result.stderr
    decoded_handoff = json.loads(result.stdout)["data"]["decoded"]
    candidates = set(decoded_handoff["enabled_talents"])
    for talents in (original, replacement):
        candidates.update(decode_build(real_repo, BuildSpec(actor_class="deathknight", spec="frost", talents=talents)).enabled_talents)
    # Evaluate each talent directly inside Main's real APL. json2 and save= retain the original
    # hash even when split-tree assignments change the talents, so neither is a state oracle.
    probes = [f"variable,name=probe_{token},value=talent.{token}" for token in sorted(candidates)]
    actions = "actions=" + "/".join([*probes, "wait,sec=1"])
    profile = tmp_path / "actors.simc"
    report_path = tmp_path / "actors.json"
    profile.write_text(text + f"active=Main\n{actions}\n")
    oracle = run_profile(real_repo, profile, simc_args=[
        "iterations=1", "threads=1", "max_time=1", "target_error=0", "debug=1", f"json2={report_path}",
    ])
    assert oracle.returncode == 0, oracle.stdout + oracle.stderr
    players = json.loads(report_path.read_text())["sim"]["players"]
    main = next(player for player in players if player["name"] == "Main")
    assert main["specialization"] == "Frost Death Knight"
    if case in {"second_actor", "copy_active"}:
        assert {player["name"] for player in players} == {"Main", "Other"}

    values = {
        token: float(value) for token, value in re.findall(
            r"Player 'Main' variable name=probe_([a-z0-9_]+) op=\d+ value=([0-9.]+)", oracle.stdout,
        )
    }
    assert set(values) == candidates, "Every candidate talent must be evaluated by the real APL"
    enabled = {token for token, value in values.items() if value > 0}
    assert len(enabled) > 30
    assert set(decoded_handoff["enabled_talents"]) == enabled

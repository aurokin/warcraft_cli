from __future__ import annotations

from pathlib import Path

from simc_cli.run import BinaryVersion, binary_matches_checkout
from simc_cli.sim import first_action_hits, first_action_time, primary_actor_name, summarize_first_casts


def _version(revision: str | None) -> BinaryVersion:
    return BinaryVersion(
        binary_path=Path("/repo/build/simc"),
        available=True,
        version_line="SimulationCraft 1210-01",
        returncode=0,
        git_branch="midnight",
        git_revision=revision,
    )


def test_binary_matches_checkout_compares_the_abbreviated_build_revision_with_head() -> None:
    """SimC's banner carries an abbreviated revision; git reports the full hash."""
    assert binary_matches_checkout(_version("3377576e3b"), {"head": "3377576e3b1122334455"}) is True
    assert binary_matches_checkout(_version("3377576e3b"), {"head": "0908ace08c9b22638fcd"}) is False


def test_binary_matches_checkout_is_unknown_when_either_side_is_missing() -> None:
    assert binary_matches_checkout(_version(None), {"head": "0908ace08c"}) is None
    assert binary_matches_checkout(_version("3377576e3b"), {"head": None}) is None


# Lines in SimC's `log=1` shape, from a real MID2 Beast Mastery run: the pets are actors named `<owner>_<pet>`.
_BM_LOG = "\n".join(
    [
        "0.000 Player 'MID2_Hunter_Beast_Mastery' schedules execute for Action 'kill_command' (0)",
        "1.871 Player 'MID2_Hunter_Beast_Mastery_duck' performs Action 'bloodshed' (321538) (82.98794782599431)",
        "1.871 Player 'MID2_Hunter_Beast_Mastery_duck' Action 'bloodshed' (321538) hits Enemy 'Fluffy_Pillow' for 0.0",
        "2.807 Player 'MID2_Hunter_Beast_Mastery' performs Action 'kill_command' (34026) (100)",
        "5.610 Player 'MID2_Hunter_Beast_Mastery' performs Action 'kill_command' (34026) (100)",
    ]
)


def test_first_action_time_extracts_the_actors_first_performed_timestamp() -> None:
    assert first_action_time(_BM_LOG, "kill_command", "MID2_Hunter_Beast_Mastery") == 2.807
    assert first_action_time(_BM_LOG, "multi_shot", "MID2_Hunter_Beast_Mastery") is None


def test_first_action_time_does_not_count_a_pet_cast_as_the_players() -> None:
    """Only the duck casts bloodshed; the BM APL has no bloodshed line."""
    assert first_action_time(_BM_LOG, "bloodshed", "MID2_Hunter_Beast_Mastery") is None
    assert first_action_time(_BM_LOG, "bloodshed", None) == 1.871


def test_primary_actor_name_is_the_first_actor_line() -> None:
    profile = '# comment\nhunter="MID2_Hunter_Beast_Mastery"\nspec=beast_mastery\nwarrior="Second"\n'
    assert primary_actor_name(profile) == "MID2_Hunter_Beast_Mastery"
    assert primary_actor_name("iterations=10\n") is None


def test_first_action_hits_extracts_scheduled_and_performed_times_and_the_actor(tmp_path: Path) -> None:
    log_path = tmp_path / "combat.log"
    log_path.write_text(_BM_LOG + "\n")

    hits = first_action_hits(log_path, ["kill_command", "bloodshed"])
    assert (hits[0].scheduled_at, hits[0].performed_at, hits[0].actor) == (0.0, 2.807, "MID2_Hunter_Beast_Mastery")
    assert (hits[1].scheduled_at, hits[1].performed_at, hits[1].actor) == (None, 1.871, "MID2_Hunter_Beast_Mastery_duck")

    filtered = first_action_hits(log_path, ["bloodshed"], "MID2_Hunter_Beast_Mastery")
    assert (filtered[0].performed_at, filtered[0].actor) == (None, None)


def test_summarize_first_casts_handles_missing_times(tmp_path: Path) -> None:
    from simc_cli.sim import FirstCastResult

    results = [
        FirstCastResult(seed=1, time=0.4, log_path=tmp_path / "seed_1.log"),
        FirstCastResult(seed=2, time=None, log_path=tmp_path / "seed_2.log"),
        FirstCastResult(seed=3, time=0.7, log_path=tmp_path / "seed_3.log"),
    ]
    summary = summarize_first_casts(results)
    assert summary["samples"] == 3
    assert summary["found"] == 2
    assert summary["min"] == 0.4
    assert summary["max"] == 0.7

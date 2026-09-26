from __future__ import annotations

from pathlib import Path

from simc_cli.run import BinaryVersion, binary_matches_checkout
from simc_cli.sim import first_action_hits, first_action_time, summarize_first_casts


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


def test_first_action_time_extracts_first_performed_timestamp() -> None:
    log_text = "\n".join(
        [
            "0.100 schedules execute for Action 'rising_sun_kick'",
            "0.250 performs Action 'rising_sun_kick'",
            "0.400 performs Action 'blackout_kick'",
        ]
    )
    assert first_action_time(log_text, "rising_sun_kick") == 0.25
    assert first_action_time(log_text, "vivify") is None


def test_first_action_hits_extracts_scheduled_and_performed_times(tmp_path: Path) -> None:
    log_path = tmp_path / "combat.log"
    log_path.write_text(
        "\n".join(
            [
                "0.100 schedules execute for Action 'rising_sun_kick'",
                "0.250 performs Action 'rising_sun_kick'",
                "0.500 performs Action 'blackout_kick'",
            ]
        )
        + "\n"
    )
    hits = first_action_hits(log_path, ["rising_sun_kick", "blackout_kick"])
    assert hits[0].scheduled_at == 0.1
    assert hits[0].performed_at == 0.25
    assert hits[1].scheduled_at is None
    assert hits[1].performed_at == 0.5


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

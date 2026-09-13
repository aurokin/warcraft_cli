"""The SimC-backed talent transport backend simc_cli supplies to warcraft_core."""

from __future__ import annotations

from pathlib import Path

import pytest
from simc_cli import build_input
from simc_cli.repo import RepoPaths
from simc_cli.talent_transport import simc_backend
from warcraft_core.talent_transport import BuildSpec, RoundTripError


def _resolution(entry: int, rank: int) -> build_input.BuildResolution:
    return build_input.BuildResolution(
        actor_class="druid",
        spec="balance",
        enabled_talents={"moonkin_form"},
        talents_by_tree={
            "class": [build_input.DecodedTalent(tree="class", name="Moonkin Form", token="moonkin_form", rank=rank,
                                                max_rank=1, entry=entry)],
            "spec": [build_input.DecodedTalent(tree="spec", name="Skipped", token="skipped", rank=0, max_rank=1, entry=999)],
        },
        source_kind="simc_split_talents",
        generated_profile_text=None,
        source_notes=[],
    )


def test_round_trip_returns_the_export_and_only_ranked_entries(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(build_input, "encode_build", lambda paths, spec: "EXPORT")
    monkeypatch.setattr(build_input, "decode_build", lambda paths, spec: _resolution(entry=101, rank=1))

    backend = simc_backend(tmp_path)
    result = backend.round_trip(BuildSpec(actor_class="druid", spec="balance", class_talents="101:1"))

    assert backend.trait_data_root == tmp_path.resolve()
    assert result.wow_talent_export == "EXPORT"
    # Rank-0 talents are not selections, so they never reach the expected-entry comparison.
    assert result.entries_by_tree == {"class": {101: 1}, "spec": {}, "hero": {}}


def test_encode_failure_becomes_a_round_trip_error(monkeypatch, tmp_path: Path) -> None:
    def boom(paths: RepoPaths, spec: build_input.BuildSpec) -> str:
        raise RuntimeError("simc binary missing")

    monkeypatch.setattr(build_input, "encode_build", boom)

    backend = simc_backend(tmp_path)
    with pytest.raises(RoundTripError, match="simc binary missing"):
        backend.round_trip(BuildSpec(actor_class="druid", spec="balance", class_talents="101:1"))

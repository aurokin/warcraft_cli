"""SimulationCraft-backed executor for warcraft_core's talent transport validation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from warcraft_core.talent_transport import (
    BuildSpec,
    RoundTripError,
    RoundTripResult,
    TalentTransportBackend,
)
from warcraft_core.talent_transport import (
    validate_talent_tree_transport as _validate_with_backend,
)

from simc_cli import build_input
from simc_cli.repo import discover_repo


def simc_backend(repo_root: str | Path | None = None) -> TalentTransportBackend:
    """Build a backend that round-trips builds through the local SimC binary in the discovered repo."""
    repo = discover_repo(repo_root)

    def round_trip(build_spec: BuildSpec) -> RoundTripResult:
        simc_spec = build_input.BuildSpec(
            actor_class=build_spec.actor_class,
            spec=build_spec.spec,
            class_talents=build_spec.class_talents,
            spec_talents=build_spec.spec_talents,
            hero_talents=build_spec.hero_talents,
            source_kind=build_spec.source_kind,
        )
        try:
            export = build_input.encode_build(repo, simc_spec)
            resolution = build_input.decode_build(
                repo,
                build_input.BuildSpec(actor_class=build_spec.actor_class, spec=build_spec.spec, talents=export),
            )
        except (FileNotFoundError, RuntimeError, ValueError) as exc:
            raise RoundTripError(str(exc)) from exc
        # Tiered nodes arrive with their real per-entry ranks. One whose ranks the decoder could not
        # read back stays a single rank-0 entry, which core's rank comparison rejects.
        entries_by_tree: dict[str, dict[int, int]] = {"class": {}, "spec": {}, "hero": {}}
        for tree in entries_by_tree:
            for talent in resolution.talents_by_tree.get(tree, []):
                if talent.entry:
                    entries_by_tree[tree][talent.entry] = talent.rank
        return RoundTripResult(wow_talent_export=export, entries_by_tree=entries_by_tree)

    return TalentTransportBackend(trait_data_root=repo.root, round_trip=round_trip)


def validate_talent_tree_transport(
    *,
    actor_class: str | None,
    spec: str | None,
    talent_tree_rows: list[dict[str, Any]],
    repo_root: str | Path | None = None,
) -> dict[str, Any]:
    return _validate_with_backend(
        actor_class=actor_class,
        spec=spec,
        talent_tree_rows=talent_tree_rows,
        backend=simc_backend(repo_root),
    )

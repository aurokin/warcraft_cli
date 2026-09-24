from __future__ import annotations

from pathlib import Path

import pytest
from warcraft_core.talent_transport import (
    BuildSpec,
    RoundTripError,
    RoundTripResult,
    TalentTransportBackend,
    validate_talent_tree_transport,
)


def _fake_backend(root: Path, *, export: str, entries_by_tree: dict[str, dict[int, int]]) -> TalentTransportBackend:
    def round_trip(build_spec: BuildSpec) -> RoundTripResult:
        return RoundTripResult(wow_talent_export=export, entries_by_tree=entries_by_tree)

    return TalentTransportBackend(trait_data_root=root, round_trip=round_trip)


def _unreachable_backend(root: Path) -> TalentTransportBackend:
    """Backend whose executor must never run; earlier validation stages should short-circuit first."""

    def round_trip(build_spec: BuildSpec) -> RoundTripResult:
        raise AssertionError("round trip should not run")

    return TalentTransportBackend(trait_data_root=root, round_trip=round_trip)


def _write_fake_generated_repo(root: Path) -> None:
    generated = root / "engine" / "dbc" / "generated"
    generated.mkdir(parents=True)
    (generated / "sc_specialization_data.inc").write_text(
        """enum specialization_e {
  SPEC_NONE              = 0,
  DRUID_BALANCE          = 102,
  DRUID_FERAL            = 103,
};
"""
    )
    (generated / "trait_data.inc").write_text(
        "static constexpr std::array<trait_data_t, 8> __trait_data_data { {\n"
        '  { 2, 11, 120001, 98001, 1, 0, 0, 0, 0, 0, 5, 5, 100, "Tiered Growth", '
        "{ 102, 0, 0, 0 }, { 0, 0, 0, 0 }, 0, 1 },\n"
        '  { 2, 11, 120002, 98001, 2, 0, 0, 0, 0, 0, 5, 5, 200, "Tiered Growth", '
        "{ 102, 0, 0, 0 }, { 0, 0, 0, 0 }, 0, 1 },\n"
        '  { 3, 11, 117999, 94999, 1, 0, 0, 0, 0, 0, 1, 3, 100, "Other Keystone", '
        "{ 102, 103, 0, 0 }, { 102, 103, 0, 0 }, 25, 0 },\n"
        '  { 4, 11, 123400, 99900, 1, 0, 0, 0, 0, 0, 1, 1, 200, "0", '
        "{ 102, 0, 0, 0 }, { 0, 0, 0, 0 }, 24, 3 },\n"
        '  { 1, 11, 103324, 82244, 1, 23, 108329, 29166, 0, 0, 10, 8, 100, "Innervate", '
        "{ 0, 0, 0, 0 }, { 0, 0, 0, 0 }, 0, 0 },\n"
        '  { 2, 11, 109839, 88206, 1, 20, 114844, 394013, 0, 102560, 9, 4, 100, '
        '"Incarnation: Chosen of Elune", { 102, 0, 0, 0 }, { 0, 0, 0, 0 }, 0, 2 },\n'
        '  { 3, 11, 117176, 94585, 1, 0, 122188, 428655, 0, 0, 4, 2, 100, "The Light of Elune", '
        "{ 102, 104, 0, 0 }, { 0, 0, 0, 0 }, 24, 2 },\n"
        '  { 2, 11, 118888, 95555, 1, 0, 122199, 428700, 0, 0, 4, 2, 100, "Feral Only Talent", '
        "{ 103, 0, 0, 0 }, { 0, 0, 0, 0 }, 0, 2 },\n"
        "} };\n"
        "static constexpr std::array<std::tuple<unsigned, const char*, unsigned>, 1> __trait_sub_tree_data { {\n"
        '  { 24, "Elune\'s Chosen", 11 },\n'
        '  { 25, "Other Tree", 11 },\n'
        "} };\n"
    )


def test_validate_talent_tree_transport_builds_validated_split_forms(tmp_path: Path) -> None:
    _write_fake_generated_repo(tmp_path)
    seen_specs: list[BuildSpec] = []

    def round_trip(build_spec: BuildSpec) -> RoundTripResult:
        seen_specs.append(build_spec)
        return RoundTripResult(
            wow_talent_export="ENCODED123",
            entries_by_tree={"class": {103324: 1}, "spec": {109839: 1}, "hero": {117176: 1}},
        )

    payload = validate_talent_tree_transport(
        actor_class="Druid",
        spec="Balance",
        talent_tree_rows=[
            {"entry": 103324, "node_id": 82244, "rank": 1},
            {"entry": 109839, "node_id": 88206, "rank": 1},
            {"entry": 117176, "node_id": 94585, "rank": 1},
        ],
        backend=TalentTransportBackend(trait_data_root=tmp_path, round_trip=round_trip),
    )

    assert payload["transport_forms"]["simc_split_talents"] == {
        "class_talents": "103324:1",
        "spec_talents": "109839:1",
        "hero_talents": "117176:1",
    }
    assert payload["validation"]["status"] == "validated"
    assert payload["validation"]["actor_class"] == "druid"
    assert payload["validation"]["spec"] == "balance"
    assert payload["validation"]["round_trip"]["wow_talent_export"] == "ENCODED123"
    assert payload["validation"]["resolved_entries"][2]["hero_tree"] == "Elune's Chosen"
    assert seen_specs == [
        BuildSpec(
            actor_class="druid",
            spec="balance",
            class_talents="103324:1",
            spec_talents="109839:1",
            hero_talents="117176:1",
            source_kind="simc_split_talents",
        )
    ]


def test_validate_talent_tree_transport_resolves_hero_selection_nodes_outside_transport_forms(tmp_path: Path) -> None:
    """Warcraft Logs lists the hero-tree selection node as a talent; SimC keeps it under tree index 4."""
    _write_fake_generated_repo(tmp_path)

    def round_trip(build_spec: BuildSpec) -> RoundTripResult:
        return RoundTripResult(wow_talent_export="ENCODED123", entries_by_tree={"class": {}, "spec": {}, "hero": {117176: 1}})

    payload = validate_talent_tree_transport(
        actor_class="druid",
        spec="balance",
        talent_tree_rows=[
            {"entry": 117176, "node_id": 94585, "rank": 1},
            {"entry": 123400, "node_id": 99900, "rank": 1},
        ],
        backend=TalentTransportBackend(trait_data_root=tmp_path, round_trip=round_trip),
    )

    assert payload["validation"]["status"] == "validated", payload["validation"]
    assert payload["transport_forms"]["simc_split_talents"] == {"class_talents": None, "spec_talents": None, "hero_talents": "117176:1"}
    selection = payload["validation"]["resolved_entries"][1]
    assert selection["tree"] == "selection"
    assert selection["name"] == "Elune's Chosen"
    assert selection["hero_tree"] == "Elune's Chosen"


def _tiered_and_hero_rows() -> list[dict[str, int]]:
    return [
        {"entry": 117176, "node_id": 94585, "rank": 1},
        {"entry": 123400, "node_id": 99900, "rank": 1},
        {"entry": 120001, "node_id": 98001, "rank": 1},
        {"entry": 120002, "node_id": 98001, "rank": 2},
    ]


def test_validate_talent_tree_transport_compares_tiered_node_entries_by_rank(tmp_path: Path) -> None:
    """The executor reads a tiered node's per-entry ranks back, so they are compared like any entry."""
    _write_fake_generated_repo(tmp_path)
    backend = _fake_backend(
        tmp_path, export="ENCODED123", entries_by_tree={"class": {}, "spec": {120001: 1, 120002: 2}, "hero": {117176: 1}}
    )

    payload = validate_talent_tree_transport(actor_class="druid", spec="balance", talent_tree_rows=_tiered_and_hero_rows(), backend=backend)

    assert payload["validation"]["status"] == "validated", payload["validation"]
    assert payload["transport_forms"]["simc_split_talents"]["spec_talents"] == "120001:1/120002:2"


@pytest.mark.parametrize(
    "spec_entries",
    [
        # The node's total survived but its split did not.
        {120001: 1, 120002: 1},
        # The executor could not read the node's ranks back: one entry at rank 0.
        {120001: 0},
        # The node is missing from the round trip altogether.
        {},
    ],
)
def test_validate_talent_tree_transport_rejects_a_tiered_node_the_round_trip_did_not_reproduce(
    tmp_path: Path, spec_entries: dict[int, int]
) -> None:
    _write_fake_generated_repo(tmp_path)
    backend = _fake_backend(tmp_path, export="ENCODED123", entries_by_tree={"class": {}, "spec": spec_entries, "hero": {117176: 1}})

    payload = validate_talent_tree_transport(actor_class="druid", spec="balance", talent_tree_rows=_tiered_and_hero_rows(), backend=backend)

    assert payload["transport_forms"] == {}
    assert payload["validation"]["reason"] == "simc_round_trip_mismatch"
    assert payload["validation"]["expected_entries_by_tree"]["spec"] == {"120001": 1, "120002": 2}


def test_validate_talent_tree_transport_rejects_a_rank_above_the_entry_max_rank(tmp_path: Path) -> None:
    """SimC clamps rank to max_ranks, so an over-rank row would build a different character."""
    _write_fake_generated_repo(tmp_path)

    payload = validate_talent_tree_transport(
        actor_class="druid",
        spec="balance",
        talent_tree_rows=[{"entry": 120001, "node_id": 98001, "rank": 9}],
        backend=_unreachable_backend(tmp_path),
    )

    assert payload["transport_forms"] == {}
    assert payload["validation"]["status"] == "not_validated"
    assert payload["validation"]["reason"] == "simc_trait_resolution_incomplete"
    assert payload["validation"]["unresolved_entries"] == [
        {"entry": 120001, "node_id": 98001, "rank": 9, "max_rank": 1, "name": "Tiered Growth", "reason": "rank_exceeds_max_rank"}
    ]


def test_validate_talent_tree_transport_rejects_a_repeated_entry(tmp_path: Path) -> None:
    """SimC keeps the last rank it is given for an entry, so a repeated row must not validate either copy."""
    _write_fake_generated_repo(tmp_path)

    payload = validate_talent_tree_transport(
        actor_class="druid",
        spec="balance",
        talent_tree_rows=[
            {"entry": 120001, "node_id": 98001, "rank": 1},
            {"entry": 120002, "node_id": 98001, "rank": 2},
            {"entry": 120002, "node_id": 98001, "rank": 1},
        ],
        backend=_unreachable_backend(tmp_path),
    )

    assert payload["validation"]["reason"] == "simc_trait_resolution_incomplete"
    assert payload["validation"]["unresolved_entries"] == [
        {"entry": 120002, "node_id": 98001, "rank": 1, "reason": "duplicate_entry"}
    ]


def test_validate_talent_tree_transport_ignores_entries_from_unselected_hero_trees(tmp_path: Path) -> None:
    """SimC grants entries from every hero tree the spec can pick; only the selected tree counts."""
    _write_fake_generated_repo(tmp_path)
    backend = _fake_backend(tmp_path, export="ENCODED123", entries_by_tree={"class": {}, "spec": {}, "hero": {117176: 1, 117999: 1}})

    payload = validate_talent_tree_transport(
        actor_class="druid",
        spec="balance",
        talent_tree_rows=[{"entry": 117176, "node_id": 94585, "rank": 1}, {"entry": 123400, "node_id": 99900, "rank": 1}],
        backend=backend,
    )

    assert payload["validation"]["status"] == "validated", payload["validation"]
    assert payload["validation"]["round_trip"]["ignored_unselected_hero_entries"] == [
        {"entry": 117999, "name": "Other Keystone", "hero_tree": "Other Tree", "hero_tree_id": 25}
    ]


def test_validate_talent_tree_transport_reports_round_trip_mismatch(tmp_path: Path) -> None:
    _write_fake_generated_repo(tmp_path)

    payload = validate_talent_tree_transport(
        actor_class="Druid",
        spec="Balance",
        talent_tree_rows=[{"entry": 103324, "node_id": 82244, "rank": 1}],
        backend=_fake_backend(tmp_path, export="ENCODED123", entries_by_tree={"class": {}, "spec": {}, "hero": {}}),
    )

    assert payload["transport_forms"] == {}
    assert payload["validation"]["reason"] == "simc_round_trip_mismatch"
    assert payload["validation"]["expected_entries_by_tree"] == {"class": {"103324": 1}, "spec": {}, "hero": {}}


def test_validate_talent_tree_transport_reports_round_trip_failure(tmp_path: Path) -> None:
    _write_fake_generated_repo(tmp_path)

    def round_trip(build_spec: BuildSpec) -> RoundTripResult:
        raise RoundTripError("SimC binary not found: /nowhere/simc")

    payload = validate_talent_tree_transport(
        actor_class="Druid",
        spec="Balance",
        talent_tree_rows=[{"entry": 103324, "node_id": 82244, "rank": 1}],
        backend=TalentTransportBackend(trait_data_root=tmp_path, round_trip=round_trip),
    )

    assert payload["transport_forms"] == {}
    assert payload["validation"]["reason"] == "simc_round_trip_failed"
    assert payload["validation"]["message"] == "SimC binary not found: /nowhere/simc"


def test_validate_talent_tree_transport_without_backend_is_not_validated() -> None:
    payload = validate_talent_tree_transport(
        actor_class="Druid",
        spec="Balance",
        talent_tree_rows=[{"entry": 103324, "node_id": 82244, "rank": 1}],
        backend=None,
    )

    assert payload == {
        "transport_forms": {},
        "validation": {"status": "not_validated", "reason": "simc_backend_unavailable"},
    }


def test_validate_talent_tree_transport_stays_unvalidated_when_rows_do_not_resolve(tmp_path: Path) -> None:
    _write_fake_generated_repo(tmp_path)

    payload = validate_talent_tree_transport(
        actor_class="Druid",
        spec="Balance",
        talent_tree_rows=[
            {"entry": 103324, "node_id": 99999, "rank": 1},
        ],
        backend=_unreachable_backend(tmp_path),
    )

    assert payload["transport_forms"] == {}
    assert payload["validation"]["status"] == "not_validated"
    assert payload["validation"]["reason"] == "simc_trait_resolution_incomplete"
    assert payload["validation"]["unresolved_entries"][0]["reason"] == "trait_not_found"


def test_validate_talent_tree_transport_rejects_zero_rank_only_rows(tmp_path: Path) -> None:
    _write_fake_generated_repo(tmp_path)

    payload = validate_talent_tree_transport(
        actor_class="Druid",
        spec="Balance",
        talent_tree_rows=[
            {"entry": 103324, "node_id": 82244, "rank": 0},
        ],
        backend=_unreachable_backend(tmp_path),
    )

    assert payload["transport_forms"] == {}
    assert payload["validation"]["status"] == "not_validated"
    assert payload["validation"]["reason"] == "no_ranked_talent_entries"
    assert payload["validation"]["resolved_entries"] == [
        {
            "entry": 103324,
            "node_id": 82244,
            "rank": 0,
            "tree": "class",
            "name": "Innervate",
            "token": "innervate",
            "max_rank": 1,
            "hero_tree_id": None,
            "hero_tree": None,
            "node_type": 0,
            "selection_index": 100,
        }
    ]


def test_validate_talent_tree_transport_rejects_rows_for_other_specs(tmp_path: Path) -> None:
    _write_fake_generated_repo(tmp_path)

    payload = validate_talent_tree_transport(
        actor_class="Druid",
        spec="Balance",
        talent_tree_rows=[
            {"entry": 118888, "node_id": 95555, "rank": 1},
        ],
        backend=_unreachable_backend(tmp_path),
    )

    assert payload["transport_forms"] == {}
    assert payload["validation"]["status"] == "not_validated"
    assert payload["validation"]["reason"] == "simc_trait_resolution_incomplete"
    assert payload["validation"]["unresolved_entries"][0]["reason"] == "trait_not_found"


def test_validate_talent_tree_transport_resolves_hero_entries_tagged_for_a_sibling_spec(tmp_path: Path) -> None:
    """SimC ignores id_spec on hero entries, so a hero talent tagged for Balance resolves for Feral."""
    _write_fake_generated_repo(tmp_path)

    def round_trip(build_spec: BuildSpec) -> RoundTripResult:
        return RoundTripResult(wow_talent_export="ENCODED123", entries_by_tree={"class": {}, "spec": {}, "hero": {117176: 1}})

    payload = validate_talent_tree_transport(
        actor_class="Druid",
        spec="Feral",
        talent_tree_rows=[{"entry": 117176, "node_id": 94585, "rank": 1}],
        backend=TalentTransportBackend(trait_data_root=tmp_path, round_trip=round_trip),
    )

    assert payload["validation"]["status"] == "validated", payload["validation"]
    assert payload["transport_forms"]["simc_split_talents"]["hero_talents"] == "117176:1"


def test_validate_talent_tree_transport_supports_specs_with_underscores(tmp_path: Path) -> None:
    generated = tmp_path / "engine" / "dbc" / "generated"
    generated.mkdir(parents=True)
    (generated / "sc_specialization_data.inc").write_text(
        """enum specialization_e {
  SPEC_NONE              = 0,
  HUNTER_BEAST_MASTERY   = 253,
};
"""
    )
    (generated / "trait_data.inc").write_text(
        "static constexpr std::array<trait_data_t, 1> __trait_data_data { {\n"
        '  { 2, 3, 200001, 80001, 1, 0, 0, 0, 0, 0, 4, 2, 100, "Bestial Wrath", '
        "{ 253, 0, 0, 0 }, { 0, 0, 0, 0 }, 0, 2 },\n"
        "} };\n"
    )

    payload = validate_talent_tree_transport(
        actor_class="Hunter",
        spec="Beast Mastery",
        talent_tree_rows=[{"entry": 200001, "node_id": 80001, "rank": 1}],
        backend=_fake_backend(tmp_path, export="XYZ987", entries_by_tree={"class": {}, "spec": {200001: 1}, "hero": {}}),
    )

    assert payload["validation"]["status"] == "validated"
    assert payload["validation"]["actor_class"] == "hunter"
    assert payload["validation"]["spec"] == "beast_mastery"
    assert payload["transport_forms"]["simc_split_talents"]["spec_talents"] == "200001:1"


def test_validate_talent_tree_transport_supports_multiword_class_enums(tmp_path: Path) -> None:
    generated = tmp_path / "engine" / "dbc" / "generated"
    generated.mkdir(parents=True)
    (generated / "sc_specialization_data.inc").write_text(
        """enum specialization_e {
  SPEC_NONE              = 0,
  DEATH_KNIGHT_BLOOD     = 250,
};
"""
    )
    (generated / "trait_data.inc").write_text(
        "static constexpr std::array<trait_data_t, 1> __trait_data_data { {\n"
        '  { 2, 6, 300001, 81001, 1, 0, 0, 0, 0, 0, 4, 2, 100, "Heartbreaker", '
        "{ 250, 0, 0, 0 }, { 0, 0, 0, 0 }, 0, 2 },\n"
        "} };\n"
    )

    payload = validate_talent_tree_transport(
        actor_class="Death Knight",
        spec="Blood",
        talent_tree_rows=[{"entry": 300001, "node_id": 81001, "rank": 1}],
        backend=_fake_backend(tmp_path, export="DK123", entries_by_tree={"class": {}, "spec": {300001: 1}, "hero": {}}),
    )

    assert payload["validation"]["status"] == "validated"
    assert payload["validation"]["actor_class"] == "deathknight"
    assert payload["validation"]["spec"] == "blood"
    assert payload["transport_forms"]["simc_split_talents"]["spec_talents"] == "300001:1"


def test_validate_talent_tree_transport_parses_single_line_generated_files(tmp_path: Path) -> None:
    generated = tmp_path / "engine" / "dbc" / "generated"
    generated.mkdir(parents=True)
    (generated / "sc_specialization_data.inc").write_text(
        "enum specialization_e { SPEC_NONE = 0, DEATH_KNIGHT_BLOOD = 250, HUNTER_BEAST_MASTERY = 253, };"
    )
    (generated / "trait_data.inc").write_text(
        'static constexpr std::array<trait_data_t, 2> __trait_data_data { {'
        ' { 2, 6, 300001, 81001, 1, 0, 0, 0, 0, 0, 4, 2, 100, "Heartbreaker", { 250, 0, 0, 0 }, { 0, 0, 0, 0 }, 0, 2 },'
        ' { 3, 6, 300002, 81002, 1, 0, 0, 0, 0, 0, 4, 2, 100, "Sanlayn", { 250, 0, 0, 0 }, { 0, 0, 0, 0 }, 24, 2 },'
        ' } };'
        'static constexpr std::array<std::tuple<unsigned, const char*, unsigned>, 1> __trait_sub_tree_data { {'
        ' { 24, "Sanlayn", 6 },'
        ' } };'
    )

    payload = validate_talent_tree_transport(
        actor_class="Death Knight",
        spec="Blood",
        talent_tree_rows=[
            {"entry": 300001, "node_id": 81001, "rank": 1},
            {"entry": 300002, "node_id": 81002, "rank": 1},
        ],
        backend=_fake_backend(
            tmp_path, export="DK123", entries_by_tree={"class": {}, "spec": {300001: 1}, "hero": {300002: 1}}
        ),
    )

    assert payload["validation"]["status"] == "validated"
    assert payload["validation"]["actor_class"] == "deathknight"
    assert payload["validation"]["spec"] == "blood"
    assert payload["validation"]["resolved_entries"][1]["hero_tree"] == "Sanlayn"
    assert payload["transport_forms"]["simc_split_talents"]["spec_talents"] == "300001:1"
    assert payload["transport_forms"]["simc_split_talents"]["hero_talents"] == "300002:1"

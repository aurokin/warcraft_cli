"""``specialization_ids`` is the one parser of SimulationCraft's specialization enum.

It is public so callers that probe a build against every spec SimC knows (simc-cli's build
identification) read the same list core validates against, healers included.

The validation tests below cover source rows no real character can have; each must fail closed
before the round trip, which would otherwise echo them back as validated.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from warcraft_core.talent_transport import (
    BuildSpec,
    RoundTripResult,
    TalentTransportBackend,
    specialization_ids,
    validate_talent_tree_transport,
)

# Trimmed shape of engine/dbc/generated/sc_specialization_data.inc: one enum entry per line, plus
# rows that name no player spec (pets) and a class whose enum name carries an underscore.
SPECIALIZATION_ENUM = """
enum specialization : unsigned {
  SPEC_NONE = 0,
  WARRIOR_ARMS = 71,
  PALADIN_HOLY = 65,
  MONK_MISTWEAVER = 270,
  DEATH_KNIGHT_BLOOD = 250,
  PET_FEROCITY = 537,
};
"""


def _repo_root(tmp_path: Path) -> Path:
    generated = tmp_path / "engine" / "dbc" / "generated"
    generated.mkdir(parents=True)
    (generated / "sc_specialization_data.inc").write_text(SPECIALIZATION_ENUM)
    return tmp_path


def test_specialization_ids_covers_healers_and_multi_word_class_names(tmp_path: Path) -> None:
    """Healer specs have no APL file of their own; a caller that skips them cannot identify them."""
    ids = specialization_ids(_repo_root(tmp_path))

    assert ids[("paladin", "holy")] == 65
    assert ids[("monk", "mistweaver")] == 270
    assert ids[("deathknight", "blood")] == 250
    assert ids[("warrior", "arms")] == 71
    # Rows that name no player spec stay out, so the caller probes real specs only.
    assert set(ids) == {("warrior", "arms"), ("paladin", "holy"), ("monk", "mistweaver"), ("deathknight", "blood")}


def test_specialization_ids_is_empty_without_a_simulationcraft_checkout(tmp_path: Path) -> None:
    """A missing checkout yields no specs rather than an exception; callers report it themselves."""
    assert specialization_ids(tmp_path) == {}


# Trimmed trait_data.inc rows (tree, class, entry, node, ..., max_rank, ..., name, specs, ..., hero tree,
# selection index): one class talent, a keystone from each of two Balance hero trees, and the hero
# tree selection node's entry for tree 24.
TRAIT_DATA = (
    "static constexpr std::array<trait_data_t, 4> __trait_data_data { {\n"
    '  { 1, 11, 103324, 82244, 1, 23, 108329, 29166, 0, 0, 10, 8, 100, "Innervate", '
    "{ 0, 0, 0, 0 }, { 0, 0, 0, 0 }, 0, 0 },\n"
    '  { 3, 11, 117176, 94585, 1, 0, 122188, 428655, 0, 0, 4, 2, 100, "The Light of Elune", '
    "{ 102, 104, 0, 0 }, { 0, 0, 0, 0 }, 24, 2 },\n"
    '  { 3, 11, 117999, 94999, 1, 0, 0, 0, 0, 0, 1, 3, 100, "Other Keystone", '
    "{ 102, 103, 0, 0 }, { 102, 103, 0, 0 }, 25, 0 },\n"
    '  { 4, 11, 123400, 99900, 1, 0, 0, 0, 0, 0, 1, 1, 200, "0", '
    "{ 102, 0, 0, 0 }, { 0, 0, 0, 0 }, 24, 3 },\n"
    "} };\n"
)
INNERVATE = {"entry": 103324, "node_id": 82244, "rank": 1}


def _echo_backend(tmp_path: Path, rows: list[dict[str, Any]]) -> TalentTransportBackend:
    """A checkout holding ``TRAIT_DATA`` and a round trip that decodes exactly the ranked rows it was given."""
    generated = tmp_path / "engine" / "dbc" / "generated"
    generated.mkdir(parents=True)
    (generated / "sc_specialization_data.inc").write_text("enum specialization_e {\n  DRUID_BALANCE = 102,\n};\n")
    (generated / "trait_data.inc").write_text(TRAIT_DATA)
    tree_by_entry = {103324: "class", 117176: "hero", 117999: "hero"}
    entries: dict[str, dict[int, int]] = {"class": {}, "spec": {}, "hero": {}}
    for row in rows:
        if row["entry"] in tree_by_entry and row["rank"] > 0:
            entries[tree_by_entry[row["entry"]]][row["entry"]] = row["rank"]

    def round_trip(build_spec: BuildSpec) -> RoundTripResult:
        return RoundTripResult(wow_talent_export="EXPORT", entries_by_tree=entries)

    return TalentTransportBackend(trait_data_root=tmp_path, round_trip=round_trip)


def _validate(tmp_path: Path, rows: list[dict[str, Any]]) -> dict[str, Any]:
    backend = _echo_backend(tmp_path, rows)
    return validate_talent_tree_transport(actor_class="druid", spec="balance", talent_tree_rows=rows, backend=backend)


def test_a_negative_rank_is_unresolved_instead_of_dropped(tmp_path: Path) -> None:
    result = _validate(tmp_path, [INNERVATE, {"entry": 117176, "node_id": 94585, "rank": -1}])

    assert result["transport_forms"] == {}
    assert result["validation"]["reason"] == "simc_trait_resolution_incomplete"
    assert [row["reason"] for row in result["validation"]["unresolved_entries"]] == ["negative_rank"]


@pytest.mark.parametrize(
    "hero_rows",
    [
        [{"entry": 117176, "node_id": 94585, "rank": 1}, {"entry": 117999, "node_id": 94999, "rank": 1}],
        [{"entry": 123400, "node_id": 99900, "rank": 1}, {"entry": 117999, "node_id": 94999, "rank": 1}],
    ],
    ids=["talents_from_two_hero_trees", "selection_of_one_tree_with_a_talent_from_another"],
)
def test_rows_from_two_hero_trees_are_not_validated(tmp_path: Path, hero_rows: list[dict[str, Any]]) -> None:
    result = _validate(tmp_path, [INNERVATE, *hero_rows])

    assert result["transport_forms"] == {}
    assert result["validation"]["reason"] == "multiple_hero_trees"
    assert result["validation"]["hero_tree_ids"] == [24, 25]


def test_rows_from_one_hero_tree_still_validate(tmp_path: Path) -> None:
    rows = [INNERVATE, {"entry": 123400, "node_id": 99900, "rank": 1}, {"entry": 117176, "node_id": 94585, "rank": 1}]

    result = _validate(tmp_path, rows)

    assert result["validation"]["status"] == "validated"
    assert result["transport_forms"]["simc_split_talents"]["hero_talents"] == "117176:1"

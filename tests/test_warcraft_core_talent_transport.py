"""``specialization_ids`` is the one parser of SimulationCraft's specialization enum.

It is public so callers that probe a build against every spec SimC knows (simc-cli's build
identification) read the same list core validates against, healers included.
"""

from __future__ import annotations

from pathlib import Path

from warcraft_core.talent_transport import specialization_ids

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

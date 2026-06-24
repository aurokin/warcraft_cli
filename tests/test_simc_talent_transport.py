from __future__ import annotations

from pathlib import Path

from warcraft_core.talent_transport import BuildResolution, _decoded_talent, validate_talent_tree_transport


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
        "static constexpr std::array<trait_data_t, 4> __trait_data_data { {\n"
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
        "} };\n"
    )


def test_validate_talent_tree_transport_builds_validated_split_forms(monkeypatch, tmp_path: Path) -> None:
    _write_fake_generated_repo(tmp_path)

    monkeypatch.setattr("warcraft_core.talent_transport.encode_build", lambda repo, build_spec: "ENCODED123")
    monkeypatch.setattr(
        "warcraft_core.talent_transport.decode_build",
        lambda repo, build_spec: BuildResolution(
            actor_class="druid",
            spec="balance",
            enabled_talents={"innervate", "incarnation_chosen_of_elune", "the_light_of_elune"},
            talents_by_tree={
                "class": [_decoded_talent(tree="class", entry=103324, rank=1, name="Innervate")],
                "spec": [_decoded_talent(tree="spec", entry=109839, rank=1, name="Incarnation: Chosen of Elune")],
                "hero": [_decoded_talent(tree="hero", entry=117176, rank=1, name="The Light of Elune")],
                "selection": [],
            },
            source_kind="wow_talent_export",
            generated_profile_text=None,
            source_notes=[],
        ),
    )

    payload = validate_talent_tree_transport(
        actor_class="Druid",
        spec="Balance",
        talent_tree_rows=[
            {"entry": 103324, "node_id": 82244, "rank": 1},
            {"entry": 109839, "node_id": 88206, "rank": 1},
            {"entry": 117176, "node_id": 94585, "rank": 1},
        ],
        repo_root=tmp_path,
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


def test_validate_talent_tree_transport_stays_unvalidated_when_rows_do_not_resolve(tmp_path: Path) -> None:
    _write_fake_generated_repo(tmp_path)

    payload = validate_talent_tree_transport(
        actor_class="Druid",
        spec="Balance",
        talent_tree_rows=[
            {"entry": 103324, "node_id": 99999, "rank": 1},
        ],
        repo_root=tmp_path,
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
        repo_root=tmp_path,
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
        repo_root=tmp_path,
    )

    assert payload["transport_forms"] == {}
    assert payload["validation"]["status"] == "not_validated"
    assert payload["validation"]["reason"] == "simc_trait_resolution_incomplete"
    assert payload["validation"]["unresolved_entries"][0]["reason"] == "trait_not_found"


def test_validate_talent_tree_transport_supports_specs_with_underscores(monkeypatch, tmp_path: Path) -> None:
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

    monkeypatch.setattr("warcraft_core.talent_transport.encode_build", lambda repo, build_spec: "XYZ987")
    monkeypatch.setattr(
        "warcraft_core.talent_transport.decode_build",
        lambda repo, build_spec: BuildResolution(
            actor_class="hunter",
            spec="beast_mastery",
            enabled_talents={"bestial_wrath"},
            talents_by_tree={
                "class": [],
                "spec": [_decoded_talent(tree="spec", entry=200001, rank=1, name="Bestial Wrath")],
                "hero": [],
                "selection": [],
            },
            source_kind="wow_talent_export",
            generated_profile_text=None,
            source_notes=[],
        ),
    )

    payload = validate_talent_tree_transport(
        actor_class="Hunter",
        spec="Beast Mastery",
        talent_tree_rows=[{"entry": 200001, "node_id": 80001, "rank": 1}],
        repo_root=tmp_path,
    )

    assert payload["validation"]["status"] == "validated"
    assert payload["validation"]["actor_class"] == "hunter"
    assert payload["validation"]["spec"] == "beast_mastery"
    assert payload["transport_forms"]["simc_split_talents"]["spec_talents"] == "200001:1"


def test_validate_talent_tree_transport_supports_multiword_class_enums(monkeypatch, tmp_path: Path) -> None:
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

    monkeypatch.setattr("warcraft_core.talent_transport.encode_build", lambda repo, build_spec: "DK123")
    monkeypatch.setattr(
        "warcraft_core.talent_transport.decode_build",
        lambda repo, build_spec: BuildResolution(
            actor_class="deathknight",
            spec="blood",
            enabled_talents={"heartbreaker"},
            talents_by_tree={
                "class": [],
                "spec": [_decoded_talent(tree="spec", entry=300001, rank=1, name="Heartbreaker")],
                "hero": [],
                "selection": [],
            },
            source_kind="wow_talent_export",
            generated_profile_text=None,
            source_notes=[],
        ),
    )

    payload = validate_talent_tree_transport(
        actor_class="Death Knight",
        spec="Blood",
        talent_tree_rows=[{"entry": 300001, "node_id": 81001, "rank": 1}],
        repo_root=tmp_path,
    )

    assert payload["validation"]["status"] == "validated"
    assert payload["validation"]["actor_class"] == "deathknight"
    assert payload["validation"]["spec"] == "blood"
    assert payload["transport_forms"]["simc_split_talents"]["spec_talents"] == "300001:1"


def test_validate_talent_tree_transport_parses_single_line_generated_files(monkeypatch, tmp_path: Path) -> None:
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

    monkeypatch.setattr("warcraft_core.talent_transport.encode_build", lambda repo, build_spec: "DK123")
    monkeypatch.setattr(
        "warcraft_core.talent_transport.decode_build",
        lambda repo, build_spec: BuildResolution(
            actor_class="deathknight",
            spec="blood",
            enabled_talents={"heartbreaker", "sanlayn"},
            talents_by_tree={
                "class": [],
                "spec": [_decoded_talent(tree="spec", entry=300001, rank=1, name="Heartbreaker")],
                "hero": [_decoded_talent(tree="hero", entry=300002, rank=1, name="Sanlayn")],
                "selection": [],
            },
            source_kind="wow_talent_export",
            generated_profile_text=None,
            source_notes=[],
        ),
    )

    payload = validate_talent_tree_transport(
        actor_class="Death Knight",
        spec="Blood",
        talent_tree_rows=[
            {"entry": 300001, "node_id": 81001, "rank": 1},
            {"entry": 300002, "node_id": 81002, "rank": 1},
        ],
        repo_root=tmp_path,
    )

    assert payload["validation"]["status"] == "validated"
    assert payload["validation"]["actor_class"] == "deathknight"
    assert payload["validation"]["spec"] == "blood"
    assert payload["validation"]["resolved_entries"][1]["hero_tree"] == "Sanlayn"
    assert payload["transport_forms"]["simc_split_talents"]["spec_talents"] == "300001:1"
    assert payload["transport_forms"]["simc_split_talents"]["hero_talents"] == "300002:1"

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from simc_cli.build_input import (
    BuildSpec,
    DecodedTalent,
    SimcBuildError,
    TalentStrings,
    UnsupportedBuildReference,
    bounded_output_preview,
    build_profile_text,
    decode_build,
    detect_build_text_source_kind,
    detect_talents_option_source_kind,
    diff_talent_trees,
    encode_build,
    extract_build_spec_from_text,
    identify_build,
    infer_actor_and_spec_from_apl,
    load_build_spec,
    merge_build_specs,
    normalize_talents_input,
    parse_debug_talents,
    parse_wowhead_talent_calc_ref,
    tokenize_talent_name,
    tree_entries_string,
)
from simc_cli.repo import RepoPaths
from simc_cli.trait_data import SimcNotReadyError

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "simc"
# Real `simc ... debug=1` output, trimmed to the talent block plus the line that ends it.
CAPTURED_HASH_REJECTED = FIXTURES / "captured_paladin_retribution_hash_error_debug.txt"
CAPTURED_GEARLESS_ACTOR = FIXTURES / "captured_deathknight_blood_no_weapon_debug.txt"
CAPTURED_TWO_HERO_TREES = FIXTURES / "captured_mage_arcane_sunfury_debug.txt"
# The checkout's trait table, trimmed to the entries the captured debug output mentions.
CAPTURED_TRAIT_DATA = FIXTURES / "captured_trait_data.inc"
CAPTURED_SPECIALIZATION_DATA = FIXTURES / "captured_sc_specialization_data.inc"


def _repo(tmp_path: Path, *, with_trait_data: bool = False) -> RepoPaths:
    """A checkout stub whose every directory is ``tmp_path``, with SimC's spec table and a binary that merely exists."""
    binary = tmp_path / "simc"
    binary.write_text("")
    generated = tmp_path / "engine" / "dbc" / "generated"
    generated.mkdir(parents=True, exist_ok=True)
    (generated / "sc_specialization_data.inc").write_text(CAPTURED_SPECIALIZATION_DATA.read_text())
    if with_trait_data:
        (generated / "trait_data.inc").write_text(CAPTURED_TRAIT_DATA.read_text())
    return RepoPaths(
        root=tmp_path,
        apl_default=tmp_path,
        apl_assisted=tmp_path,
        class_modules=tmp_path,
        spell_dump=tmp_path,
        build_dir=tmp_path,
        build_simc=binary,
    )


def test_tokenize_talent_name_normalizes_text() -> None:
    assert tokenize_talent_name("Devourer's Bite") == "devourers_bite"
    assert tokenize_talent_name("Tip the Scales") == "tip_the_scales"


def test_extract_build_spec_from_plain_hash() -> None:
    spec = extract_build_spec_from_text("ABC123")
    assert spec.talents == "ABC123"
    assert spec.source_kind == "wow_talent_export"
    assert "single-line talent export" in spec.source_notes


def test_extract_build_spec_from_wowhead_talent_calc_url() -> None:
    spec = extract_build_spec_from_text("https://www.wowhead.com/talent-calc/demon-hunter/devourer/ABC123")
    assert spec.actor_class == "demonhunter"
    assert spec.spec == "devourer"
    assert spec.talents == "ABC123"
    assert spec.source_kind == "wowhead_talent_calc_url"


def test_parse_wowhead_talent_calc_ref_supports_relative_paths() -> None:
    spec = parse_wowhead_talent_calc_ref("/talent-calc/hunter/beast-mastery/XYZ987")
    assert spec is not None
    assert spec.actor_class == "hunter"
    assert spec.spec == "beast_mastery"
    assert spec.talents == "XYZ987"


def test_parse_wowhead_talent_calc_ref_supports_scheme_less_wowhead_urls() -> None:
    spec = parse_wowhead_talent_calc_ref("wowhead.com/talent-calc/demon-hunter/devourer/ABC123")
    assert spec is not None
    assert spec.actor_class == "demonhunter"
    assert spec.spec == "devourer"
    assert spec.talents == "ABC123"


def test_parse_wowhead_talent_calc_ref_rejects_empty_or_extra_path_segments() -> None:
    assert parse_wowhead_talent_calc_ref("https://www.wowhead.com/talent-calc/druid//balance/ABC123") is None
    assert parse_wowhead_talent_calc_ref("https://www.wowhead.com/talent-calc/druid/balance/ABC123/extra") is None


def test_extract_build_spec_from_profile_lines() -> None:
    text = """
    demonhunter="example"
    spec=devourer
    talents=ABC123
    class_talents=class-string
    hero_talents=hero-string
    """
    spec = extract_build_spec_from_text(text)
    assert spec.actor_class == "demonhunter"
    assert spec.spec == "devourer"
    assert spec.talents == "ABC123"
    assert spec.class_talents == "class-string"
    assert spec.hero_talents == "hero-string"
    assert spec.source_kind == "simc_split_talents"


def test_merge_build_specs_prefers_later_values() -> None:
    left = BuildSpec(actor_class="evoker", spec="devastation", talents="left")
    right = BuildSpec(talents="right", hero_talents="hero")
    merged = merge_build_specs(left, right)
    assert merged.actor_class == "evoker"
    assert merged.spec == "devastation"
    assert merged.talents == "right"
    assert merged.hero_talents == "hero"


def test_infer_actor_and_spec_from_apl() -> None:
    actor_class, spec = infer_actor_and_spec_from_apl("ActionPriorityLists/default/evoker_devastation.simc")
    assert actor_class == "evoker"
    assert spec == "devastation"


def test_normalize_talents_input() -> None:
    assert normalize_talents_input("talents=ABC123") == "ABC123"
    assert normalize_talents_input("ABC123") == "ABC123"
    assert normalize_talents_input("https://www.wowhead.com/talent-calc/demon-hunter/devourer/ABC123") == "ABC123"
    assert normalize_talents_input(None) is None


def test_detect_build_text_source_kind() -> None:
    assert detect_build_text_source_kind("ABC123") == "wow_talent_export"
    assert detect_build_text_source_kind("https://www.wowhead.com/talent-calc/demon-hunter/devourer/ABC123") == "wowhead_talent_calc_url"
    assert detect_build_text_source_kind('warlock="probe"\nspec=demonology\ntalents=ABC123\n') == "simc_profile"
    assert detect_build_text_source_kind("class_talents=AAA\nspec_talents=BBB\nhero_talents=CCC\n") == "simc_split_talents"


def test_detect_talents_option_source_kind() -> None:
    assert (
        detect_talents_option_source_kind(
            talents=TalentStrings(talents="ABC123"),
        )
        == "wow_talent_export"
    )
    assert (
        detect_talents_option_source_kind(
            talents=TalentStrings(talents="https://www.wowhead.com/talent-calc/demon-hunter/devourer/ABC123"),
        )
        == "wowhead_talent_calc_url"
    )
    assert (
        detect_talents_option_source_kind(
            talents=TalentStrings(talents="talents=ABC123"),
        )
        == "simc_profile"
    )
    assert (
        detect_talents_option_source_kind(
            talents=TalentStrings(
                talents=None,
                class_talents="AAA",
                spec_talents="BBB",
                hero_talents="CCC",
            ),
        )
        == "simc_split_talents"
    )


def test_load_build_spec_extracts_class_and_spec_from_talents_url() -> None:
    spec = load_build_spec(
        profile_path=None,
        build_file=None,
        build_text=None,
        talents=TalentStrings(talents="https://www.wowhead.com/talent-calc/demon-hunter/devourer/ABC123"),
        actor_class=None,
        spec_name=None,
    )

    assert spec.actor_class == "demonhunter"
    assert spec.spec == "devourer"
    assert spec.talents == "ABC123"
    assert spec.source_kind == "wowhead_talent_calc_url"


def test_load_build_spec_extracts_exact_transport_form_from_packet(tmp_path: Path) -> None:
    packet_path = tmp_path / "build-packet.json"
    packet_path.write_text(
        """
        {
          "kind": "talent_transport_packet",
          "transport_status": "exact",
          "build_identity": {
            "class_spec_identity": {
              "identity": {
                "actor_class": "druid",
                "spec": "balance"
              }
            }
          },
          "transport_forms": {
            "wowhead_talent_calc_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123"
          },
          "raw_evidence": {
            "reference_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123"
          },
          "validation": {},
          "scope": {}
        }
        """.strip()
    )

    spec = load_build_spec(
        profile_path=None,
        build_file=None,
        build_text=None,
        talents=TalentStrings(),
        actor_class=None,
        spec_name=None,
        build_packet=str(packet_path),
    )

    assert spec.actor_class == "druid"
    assert spec.spec == "balance"
    assert spec.talents == "ABC123"
    assert spec.source_kind == "wowhead_talent_calc_url"
    assert spec.transport_form == "wowhead_talent_calc_url"
    assert any("talent transport packet" in note for note in spec.source_notes)


def test_load_build_spec_rejects_packet_that_mixes_exact_and_split_forms(tmp_path: Path) -> None:
    packet_path = tmp_path / "build-packet.json"
    packet_path.write_text(
        """
        {
          "kind": "talent_transport_packet",
          "transport_status": "exact",
          "build_identity": {
            "class_spec_identity": {
              "identity": {
                "actor_class": "druid",
                "spec": "balance"
              }
            }
          },
          "transport_forms": {
            "wowhead_talent_calc_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123",
            "simc_split_talents": {
              "class_talents": "103324:1"
            }
          },
          "raw_evidence": {
            "reference_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123"
          },
          "validation": {
            "status": "validated",
            "actor_class": "druid",
            "spec": "balance"
          },
          "scope": {}
        }
        """.strip()
    )

    with pytest.raises(ValueError, match="must not mix exact transport forms with simc_split_talents"):
        load_build_spec(
            profile_path=None,
            build_file=None,
            build_text=None,
            talents=TalentStrings(),
            actor_class=None,
            spec_name=None,
            build_packet=str(packet_path),
        )


def test_load_build_spec_rejects_buildless_wowhead_talent_calc_url() -> None:
    with pytest.raises(UnsupportedBuildReference) as excinfo:
        load_build_spec(
            profile_path=None,
            build_file=None,
            build_text="https://www.wowhead.com/talent-calc/druid/balance",
            talents=TalentStrings(),
            actor_class=None,
            spec_name=None,
        )

    assert excinfo.value.reference_type == "wowhead_talent_calc_url"
    assert "no build code" in str(excinfo.value)


def _loaded(**overrides: Any) -> BuildSpec:
    options: dict[str, Any] = {
        "profile_path": None,
        "build_file": None,
        "build_text": None,
        "talents": TalentStrings(),
        "actor_class": None,
        "spec_name": None,
    }
    return load_build_spec(**{**options, **overrides})


@pytest.mark.parametrize("actor_class", ["Death Knight", "death_knight", "DeathKnight"])
def test_load_build_spec_reads_every_death_knight_spelling_as_simc_deathknight(actor_class: str) -> None:
    loaded = _loaded(actor_class=actor_class, spec_name="Frost")

    assert (loaded.actor_class, loaded.spec) == ("deathknight", "frost")


def test_load_build_spec_rejects_a_build_option_given_an_empty_value() -> None:
    """An empty option is not an omitted one; it used to resolve to a build with no talents."""
    with pytest.raises(ValueError, match=r"--talents"):
        _loaded(talents=TalentStrings(talents="  "), actor_class="monk", spec_name="mistweaver")


def test_load_build_spec_reads_the_blizzard_talent_calc_url_modify_build_publishes() -> None:
    """`modify-build` publishes /talent-calc/blizzard/<hash>; that hash is a plain WoW export."""
    spec = _loaded(build_text="https://www.wowhead.com/talent-calc/blizzard/C4QAAAAA")

    assert (spec.talents, spec.source_kind) == ("C4QAAAAA", "wow_talent_export")
    assert spec.actor_class is None and spec.spec is None


def test_load_build_spec_reads_the_blizzard_talent_calc_path_only_on_wowhead() -> None:
    """A look-alike host is not Wowhead, so its /talent-calc/blizzard/<hash> is no build reference."""
    with pytest.raises(UnsupportedBuildReference):
        _loaded(build_text="https://notwowhead.com/talent-calc/blizzard/C4QAAAAA")


def test_load_build_spec_rejects_a_page_url_instead_of_treating_it_as_a_hash() -> None:
    """A guide URL reached SimC as a talent hash, so the envelope blamed the build."""
    with pytest.raises(UnsupportedBuildReference) as excinfo:
        _loaded(talents=TalentStrings(talents="https://www.icy-veins.com/wow/mistweaver-monk-pve-healing-guide"))

    assert excinfo.value.reference_type == "url"


# One published build reference from each captured guide fixture: tests/fixtures/method's
# captured_talents_page.html and tests/fixtures/icy_veins' astro_spec_builds_talents.html. Both guide
# providers publish only `wow_talent_export` strings, which name no class or spec.
GUIDE_BUILD_REFERENCES = [
    "C4QAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAM2mB2sYGzMbzYDzMDzsstMzYhZ0MmBMYwYWmZmZY2GmhZZmAAAAAz20ysNzysBAAAAwMzAADwiMAA",
    "C4QAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAghx2YZYzixMzyyM2wYGmZZbbmxCzoZMDYwAsMzMzwsBDWmJAAAAAAYxyMLzyMDAAMgBYGwYYsMZMDA",
]


@pytest.mark.parametrize("reference", GUIDE_BUILD_REFERENCES)
def test_a_guide_build_reference_reads_as_a_wow_talent_export(reference: str) -> None:
    """Guide references carry the talents but no identity; the SimC probe or --actor-class/--spec supplies it."""
    spec = _loaded(build_text=reference)

    assert (spec.talents, spec.source_kind) == (reference, "wow_talent_export")
    assert spec.actor_class is None and spec.spec is None


def test_load_build_spec_extracts_split_transport_form_from_packet(tmp_path: Path) -> None:
    packet_path = tmp_path / "build-packet.json"
    packet_path.write_text(
        """
        {
          "kind": "talent_transport_packet",
          "transport_status": "validated",
          "build_identity": {
            "class_spec_identity": {
              "identity": {
                "actor_class": "druid",
                "spec": "balance"
              }
            }
          },
          "transport_forms": {
            "simc_split_talents": {
              "class_talents": "103324:1",
              "spec_talents": "109839:1",
              "hero_talents": "117176:1"
            }
          },
          "raw_evidence": {
            "talent_tree_entries": [
              {"entry": 103324, "node_id": 82244, "rank": 1}
            ]
          },
          "validation": {
            "status": "validated",
            "actor_class": "druid",
            "spec": "balance"
          },
          "scope": {}
        }
        """.strip()
    )

    spec = load_build_spec(
        profile_path=None,
        build_file=None,
        build_text=None,
        talents=TalentStrings(),
        actor_class=None,
        spec_name=None,
        build_packet=str(packet_path),
    )

    assert spec.actor_class == "druid"
    assert spec.spec == "balance"
    assert spec.class_talents == "103324:1"
    assert spec.spec_talents == "109839:1"
    assert spec.hero_talents == "117176:1"
    assert spec.source_kind == "simc_split_talents"
    assert spec.transport_form == "simc_split_talents"
    assert "class/spec metadata came from packet contents and was validated with the split transport" in spec.source_notes


def test_load_build_spec_accepts_normalized_validated_split_transport_identity_from_packet(tmp_path: Path) -> None:
    packet_path = tmp_path / "build-packet.json"
    packet_path.write_text(
        """
        {
          "kind": "talent_transport_packet",
          "transport_status": "validated",
          "build_identity": {
            "class_spec_identity": {
              "identity": {
                "actor_class": "Druid",
                "spec": "Balance"
              }
            }
          },
          "transport_forms": {
            "simc_split_talents": {
              "class_talents": "103324:1",
              "spec_talents": "109839:1"
            }
          },
          "raw_evidence": {
            "talent_tree_entries": [
              {"entry": 103324, "node_id": 82244, "rank": 1}
            ]
          },
          "validation": {
            "status": "validated",
            "actor_class": "druid",
            "spec": "balance"
          },
          "scope": {}
        }
        """.strip()
    )

    spec = load_build_spec(
        profile_path=None,
        build_file=None,
        build_text=None,
        talents=TalentStrings(),
        actor_class=None,
        spec_name=None,
        build_packet=str(packet_path),
    )

    assert spec.actor_class == "druid"
    assert spec.spec == "balance"
    assert spec.class_talents == "103324:1"
    assert spec.spec_talents == "109839:1"
    assert spec.source_kind == "simc_split_talents"


def test_load_build_spec_rejects_unvalidated_split_transport_form_from_packet(tmp_path: Path) -> None:
    packet_path = tmp_path / "build-packet.json"
    packet_path.write_text(
        """
        {
          "kind": "talent_transport_packet",
          "transport_status": "raw_only",
          "build_identity": {
            "class_spec_identity": {
              "identity": {
                "actor_class": "druid",
                "spec": "balance"
              }
            }
          },
          "transport_forms": {
            "simc_split_talents": {
              "class_talents": "103324:1",
              "spec_talents": "109839:1"
            }
          },
          "raw_evidence": {
            "talent_tree_entries": [
              {"entry": 103324, "node_id": 82244, "rank": 1}
            ]
          },
          "validation": {
            "status": "not_validated"
          },
          "scope": {}
        }
        """.strip()
    )

    with pytest.raises(ValueError, match="simc_split_talents transport form requires a validated packet identity"):
        load_build_spec(
            profile_path=None,
            build_file=None,
            build_text=None,
            talents=TalentStrings(),
            actor_class=None,
            spec_name=None,
            build_packet=str(packet_path),
        )


def test_load_build_spec_extracts_wow_export_transport_form_from_packet(tmp_path: Path) -> None:
    packet_path = tmp_path / "build-packet.json"
    packet_path.write_text(
        """
        {
          "kind": "talent_transport_packet",
          "transport_status": "exact",
          "build_identity": {
            "class_spec_identity": {
              "identity": {
                "actor_class": "druid",
                "spec": "balance"
              }
            }
          },
          "transport_forms": {
            "wow_talent_export": "ABC123"
          },
          "raw_evidence": {
            "reference_type": "wow_talent_export"
          },
          "validation": {},
          "scope": {}
        }
        """.strip()
    )

    spec = load_build_spec(
        profile_path=None,
        build_file=None,
        build_text=None,
        talents=TalentStrings(),
        actor_class=None,
        spec_name=None,
        build_packet=str(packet_path),
    )

    assert spec.actor_class is None
    assert spec.spec is None
    assert spec.talents == "ABC123"
    assert spec.source_kind == "wow_talent_export"
    assert spec.transport_form == "wow_talent_export"
    assert "class/spec metadata came from packet contents and was not independently validated" in spec.source_notes


def test_identify_build_downgrades_wow_export_packet_metadata_confidence(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    build_spec = BuildSpec(
        actor_class="priest",
        spec="shadow",
        talents="ABC123",
        source_kind="wow_talent_export",
        source_notes=["build packet: /tmp/forged.json", "talent transport packet"],
    )

    with patch("simc_cli.build_input.decode_build", return_value=type("Resolution", (), {"enabled_talents": {"mind_blast"}})()):
        identified, identity = identify_build(repo, build_spec)

    assert identified.actor_class == "priest"
    assert identified.spec == "shadow"
    assert identity.source == "wow_talent_export"
    assert identity.confidence == "medium"


def test_parse_wowhead_talent_calc_ref_rejects_nested_talent_calc_segments() -> None:
    assert parse_wowhead_talent_calc_ref("talent-calc/foo/talent-calc/druid/balance/ABC123") is None


def test_identify_build_narrows_an_unverified_packet_to_the_apl_name_spec(tmp_path: Path) -> None:
    """The packet's unvalidated druid balance is not a hint; the APL name's evoker devastation is."""
    repo = _repo(tmp_path)
    packet_path = tmp_path / "build-packet.json"
    packet_path.write_text(
        """
        {
          "kind": "talent_transport_packet",
          "transport_status": "exact",
          "build_identity": {"class_spec_identity": {"identity": {"actor_class": "druid", "spec": "balance"}}},
          "transport_forms": {"wow_talent_export": "ABC123"},
          "raw_evidence": {"reference_type": "wow_talent_export"},
          "validation": {},
          "scope": {}
        }
        """.strip()
    )
    loaded = _loaded(build_packet=str(packet_path))
    tried: list[tuple[str | None, str | None]] = []

    def fake_decode(_repo: RepoPaths, build_spec: BuildSpec) -> Any:
        tried.append((build_spec.actor_class, build_spec.spec))
        return type("Resolution", (), {"enabled_talents": {"disintegrate"}})()

    with patch("simc_cli.build_input.decode_build", side_effect=fake_decode):
        identified, identity = identify_build(repo, loaded, apl_path=tmp_path / "evoker_devastation.simc")

    assert (loaded.actor_class, loaded.spec) == (None, None)
    assert tried == [("evoker", "devastation")]
    assert (identified.actor_class, identified.spec, identified.talents) == ("evoker", "devastation", "ABC123")
    assert "inferred from apl: evoker_devastation" in identity.source_notes


@pytest.mark.parametrize(
    ("apl_name", "actor_class", "probed"),
    [
        # A renamed copy of mage_arcane.simc: 'arcane_variant' is no mage spec.
        ("mage_arcane_variant", None, 40),
        # The caller's mage does not pair with the file's fury; only --actor-class narrows the probe.
        ("warrior_fury", "mage", 3),
    ],
)
def test_identify_build_drops_an_apl_name_guess_that_names_no_simc_spec(
    tmp_path: Path, apl_name: str, actor_class: str | None, probed: int
) -> None:
    """The file-name guess is not the caller's hint: it used to fail as invalid_query naming a spec nobody passed."""
    repo = _repo(tmp_path)
    tried: list[tuple[str | None, str | None]] = []

    def fake_decode(_repo: RepoPaths, build_spec: BuildSpec) -> Any:
        tried.append((build_spec.actor_class, build_spec.spec))
        if (build_spec.actor_class, build_spec.spec) != ("mage", "arcane"):
            raise SimcBuildError("Selected node is not available to player's spec", output_preview=[], returncode=1)
        return type("Resolution", (), {"enabled_talents": {"arcane_blast"}})()

    build_spec = BuildSpec(actor_class=actor_class, talents="HASH", source_kind="wow_talent_export")
    with patch("simc_cli.build_input.decode_build", side_effect=fake_decode):
        identified, identity = identify_build(repo, build_spec, apl_path=tmp_path / f"{apl_name}.simc")

    assert len(tried) == probed
    assert (identified.actor_class, identified.spec) == ("mage", "arcane")
    assert identity.source == "simc_probe"
    assert f"ignored apl name: {apl_name} does not complete a SimC class/spec pair" in identity.source_notes


def test_load_build_spec_rejects_conflicting_exact_transport_forms(tmp_path: Path) -> None:
    packet_path = tmp_path / "build-packet.json"
    packet_path.write_text(
        """
        {
          "kind": "talent_transport_packet",
          "transport_status": "exact",
          "build_identity": {
            "class_spec_identity": {
              "identity": {
                "actor_class": "druid",
                "spec": "balance"
              }
            }
          },
          "transport_forms": {
            "wowhead_talent_calc_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123",
            "wow_talent_export": "XYZ789"
          },
          "raw_evidence": {
            "reference_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123"
          },
          "validation": {},
          "scope": {}
        }
        """.strip()
    )

    try:
        load_build_spec(
            profile_path=None,
            build_file=None,
            build_text=None,
            talents=TalentStrings(),
            actor_class=None,
            spec_name=None,
            build_packet=str(packet_path),
        )
    except ValueError as exc:
        assert "exact transport forms must agree" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_parse_debug_talents_ignores_selection_tree() -> None:
    output = (FIXTURES / "dh_decode_debug.txt").read_text()
    talents = parse_debug_talents(output)
    assert [talent.token for talent in talents["class"]] == ["voidblade"]
    assert [talent.token for talent in talents["spec"]] == ["void_ray", "devourers_bite"]
    assert [talent.token for talent in talents["hero"]] == ["voidsurge"]
    assert talents["selection"] == []


def test_build_profile_text_contains_expected_lines() -> None:
    text = build_profile_text(BuildSpec(actor_class="evoker", spec="devastation", talents="ABC123"))
    assert 'evoker="simc_decode"' in text
    assert "race=dracthyr" in text
    assert "talents=ABC123" in text


def test_decode_build_uses_debug_output(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    fake_output = (FIXTURES / "dh_decode_debug.txt").read_text()

    with patch("simc_cli.build_input.subprocess.run") as mocked_run:
        mocked_run.return_value = subprocess.CompletedProcess([], 0, stdout=fake_output, stderr="")
        result = decode_build(repo, BuildSpec(actor_class="demonhunter", spec="devourer", talents="ABC123"))

    assert result.actor_class == "demonhunter"
    assert result.spec == "devourer"
    assert result.source_kind is None
    assert 'demonhunter="simc_decode"' in (result.generated_profile_text or "")
    assert "voidblade" in result.enabled_talents
    assert "devourers_bite" in result.enabled_talents
    assert "midnight" not in result.enabled_talents
    assert any("decoded via" in note for note in result.source_notes)


def _decode(repo: RepoPaths, output: str, returncode: int, **spec_fields: str) -> Any:
    with patch("simc_cli.build_input.subprocess.run") as mocked_run:
        mocked_run.return_value = subprocess.CompletedProcess([], returncode, stdout=output, stderr="")
        return decode_build(repo, BuildSpec(**spec_fields))


def test_decode_build_rejects_a_hash_simc_refused_even_after_printing_talents(tmp_path: Path) -> None:
    """SimC prints the freely granted talents before it reports the bad hash; that is not a build."""
    output = CAPTURED_HASH_REJECTED.read_text()
    assert "adding spec talent Wake of Ashes" in output

    with pytest.raises(SimcBuildError) as caught:
        _decode(_repo(tmp_path), output, 81, actor_class="paladin", spec="retribution", talents="CYEAAA")

    assert "Node 81527 is not a choice node but has index selection" in str(caught.value)
    assert caught.value.returncode == 81


def test_decode_build_error_message_is_the_simc_error_line_not_its_option_dump(tmp_path: Path) -> None:
    """`debug=1` makes SimC print tens of thousands of lines; only its error belongs in the envelope."""
    dump = "World of Warcraft Raid Simulator Options:\n" + "".join(f"option_{i}=0\n" for i in range(700))

    with pytest.raises(SimcBuildError) as caught:
        _decode(
            _repo(tmp_path),
            dump + CAPTURED_HASH_REJECTED.read_text(),
            81,
            actor_class="paladin",
            spec="retribution",
            talents="CYEAAA",
        )

    assert "option_0=0" not in str(caught.value)
    assert len(str(caught.value)) < 500
    assert len(caught.value.output_preview) == 20


def test_decode_build_keeps_a_build_whose_only_error_is_the_gearless_decode_actor(tmp_path: Path) -> None:
    """The decode profile carries no gear on purpose, so SimC's weapon complaint is not a rejection."""
    output = CAPTURED_GEARLESS_ACTOR.read_text()
    assert "has no weapon equipped" in output

    result = _decode(
        _repo(tmp_path, with_trait_data=True), output, 80,
        actor_class="deathknight", spec="blood", talents="CoEAAA",
    )

    assert result.hero_tree is not None
    assert result.hero_tree.name == "San'layn"
    assert "coagulopathy" in result.enabled_talents


@pytest.mark.parametrize("returncode", [139, -11])
def test_decode_build_refuses_the_talents_of_a_simc_run_that_crashed(tmp_path: Path, returncode: int) -> None:
    """SimC killed part-way through the talent block used to decode as a whole build with ok: true."""
    partial = "\n".join(CAPTURED_TWO_HERO_TREES.read_text().splitlines()[:20])
    assert "adding class talent" in partial

    with pytest.raises(SimcNotReadyError, match=f"SimC exited {returncode} before finishing the decode"):
        _decode(_repo(tmp_path, with_trait_data=True), partial, returncode, actor_class="mage", spec="arcane", talents="C4DAAA")


def test_decode_build_blames_the_checkout_for_a_binary_it_cannot_run(tmp_path: Path) -> None:
    """A non-executable binary used to escape as a bare PermissionError (internal_error)."""
    with (
        patch("simc_cli.build_input.subprocess.run", side_effect=PermissionError(13, "Permission denied")),
        pytest.raises(SimcNotReadyError, match="Cannot run the SimC binary .*: Permission denied"),
    ):
        decode_build(_repo(tmp_path), BuildSpec(actor_class="mage", spec="arcane", talents="C4DAAA"))


def test_decode_build_drops_hero_talents_from_the_hero_tree_simc_did_not_activate(tmp_path: Path) -> None:
    """A hash grants both keystones; keeping the unselected one flips hero-gated APL branches."""
    result = _decode(
        _repo(tmp_path, with_trait_data=True), CAPTURED_TWO_HERO_TREES.read_text(), 0,
        actor_class="mage", spec="arcane", talents="C4DAAA",
    )

    assert result.hero_tree is not None
    assert (result.hero_tree.name, result.hero_tree.id) == ("Sunfury", 39)
    assert [talent.name for talent in result.inactive_hero_talents] == ["Splintering Sorcery"]
    assert "splintering_sorcery" not in result.enabled_talents
    assert "ashes_of_inspiration" in result.enabled_talents
    assert all(talent.tree != "hero" or talent.entry != 117267 for talent in result.talents_by_tree["hero"])


def test_decode_build_counts_a_tiered_node_printed_at_rank_zero_as_taken(tmp_path: Path) -> None:
    """SimC spreads a tiered node's ranks over its entries and prints the leftover, always 0."""
    output = CAPTURED_TWO_HERO_TREES.read_text()
    assert "Prismatic Bolt (node=110420 entry=137028 rank=0/1)" in output

    result = _decode(
        _repo(tmp_path, with_trait_data=True), output, 0,
        actor_class="mage", spec="arcane", talents="C4DAAA",
    )

    assert "prismatic_bolt" in result.enabled_talents
    tiered = next(talent for talent in result.talents_by_tree["spec"] if talent.entry == 137028)
    assert (tiered.rank, tiered.rank_known, tiered.taken) == (0, False, True)


# SimC's `log=1` answer when a talent option overwrites ranks the hash allocated to Prismatic Bolt.
CAPTURED_TIERED_OVERWRITE_LOG = "\n".join(
    [
        "0.000 Overwriting talent Prismatic Bolt (137028), rank 1 -> 0",
        "0.000 Overwriting talent Prismatic Bolt (137027), rank 2 -> 0",
        "0.000 Overwriting talent Prismatic Bolt (137026), rank 1 -> 0",
    ]
)


def test_decode_build_reads_back_the_per_entry_ranks_of_a_tiered_node(tmp_path: Path) -> None:
    """Without the read-back a tiered node cannot be re-serialized, so every tree swap dropped it."""
    decode_output = CAPTURED_TWO_HERO_TREES.read_text()
    profiles: list[str] = []

    def fake_run(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        profile = Path(str(cmd[1])).read_text()
        profiles.append(profile)
        answer = CAPTURED_TIERED_OVERWRITE_LOG if "spec_talents=" in profile else decode_output
        return subprocess.CompletedProcess(cmd, 0, stdout=answer, stderr="")

    with patch("simc_cli.build_input.subprocess.run", side_effect=fake_run):
        result = decode_build(
            _repo(tmp_path, with_trait_data=True),
            BuildSpec(actor_class="mage", spec="arcane", talents="C4DAAA"),
        )

    # The probe keeps the build and adds every entry of the tiered node at rank 0.
    assert "spec_talents=137028:0/137027:0/137026:0" in profiles[1]
    assert "talents=C4DAAA" in profiles[1]
    tiered = [talent for talent in result.talents_by_tree["spec"] if talent.name == "Prismatic Bolt"]
    assert [(talent.entry, talent.rank, talent.max_rank) for talent in tiered] == [
        (137028, 1, 1),
        (137027, 2, 2),
        (137026, 1, 1),
    ]
    assert all(talent.rank_known for talent in tiered)
    assert "137028:1/137027:2/137026:1" in tree_entries_string(result.talents_by_tree["spec"])


def test_decode_build_removes_the_profile_directory_it_wrote(tmp_path: Path) -> None:
    written: list[Path] = []

    def fake_run(cmd, **kwargs):  # noqa: ANN001, ANN003
        written.append(Path(str(cmd[1])))
        return subprocess.CompletedProcess(cmd, 0, stdout=(FIXTURES / "dh_decode_debug.txt").read_text(), stderr="")

    with patch("simc_cli.build_input.subprocess.run", side_effect=fake_run):
        decode_build(_repo(tmp_path), BuildSpec(actor_class="demonhunter", spec="devourer", talents="ABC123"))

    assert written and not written[0].parent.exists()


def test_identify_build_probes_healer_specs_that_ship_no_apl(tmp_path: Path) -> None:
    """The probe draws on SimC's specialization data, not on APL files, so a healer build identifies."""
    repo = _repo(tmp_path)
    (tmp_path / "engine" / "dbc" / "generated" / "sc_specialization_data.inc").write_text("  PALADIN_HOLY = 65,\n  PALADIN_RETRIBUTION = 70,\n")
    tried: list[tuple[str | None, str | None]] = []

    def fake_decode(_repo: RepoPaths, build_spec: BuildSpec) -> Any:
        tried.append((build_spec.actor_class, build_spec.spec))
        if build_spec.spec != "holy":
            raise SimcBuildError("Selected node is not available to player's spec", output_preview=[], returncode=1)
        return type("Resolution", (), {"enabled_talents": {"holy_shock"}})()

    with patch("simc_cli.build_input.decode_build", side_effect=fake_decode):
        identified, identity = identify_build(repo, BuildSpec(talents="HOLY_EXPORT", source_kind="wow_talent_export"))

    assert tried == [("paladin", "holy"), ("paladin", "retribution")]
    assert (identified.actor_class, identified.spec) == ("paladin", "holy")
    assert (identity.source, identity.confidence) == ("simc_probe", "high")


def _decodes_only_as(actor_class: str, spec: str, tried: list[tuple[str | None, str | None]]) -> Any:
    """A decode stub that accepts the hash as one spec and rejects it, as SimC does, as every other."""

    def fake_decode(_repo: RepoPaths, build_spec: BuildSpec) -> Any:
        tried.append((build_spec.actor_class, build_spec.spec))
        if (build_spec.actor_class, build_spec.spec) != (actor_class, spec):
            raise SimcBuildError("Wrong specialization.", output_preview=[], returncode=81)
        return type("Resolution", (), {"enabled_talents": {"a_talent"}})()

    return fake_decode


def test_identify_build_confirms_direct_metadata_with_one_decode(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    tried: list[tuple[str | None, str | None]] = []
    build_spec = BuildSpec(actor_class="demonhunter", spec="devourer", talents="ABC123", source_kind="wowhead_talent_calc_url")

    with patch("simc_cli.build_input.decode_build", side_effect=_decodes_only_as("demonhunter", "devourer", tried)):
        identified, identity = identify_build(repo, build_spec)

    assert tried == [("demonhunter", "devourer")]
    assert (identified.actor_class, identified.spec) == ("demonhunter", "devourer")
    assert (identity.source, identity.confidence) == ("wowhead_talent_calc_url", "high")


@pytest.mark.parametrize(
    ("guessed", "apl_path", "note"),
    [
        # A Beast Mastery hash pasted into a /talent-calc/hunter/marksmanship/ URL used to come back as
        # hunter marksmanship with high confidence.
        (BuildSpec(actor_class="hunter", spec="marksmanship", talents="BM_HASH", source_kind="wowhead_talent_calc_url"),
         None, "ignored talent-calc url path: the build does not decode as hunter marksmanship"),
        # The file name of the APL the build is read against is no statement about the build.
        (BuildSpec(talents="BM_HASH", source_kind="wow_talent_export"),
         "mage_fire.simc", "ignored apl name: the build does not decode as mage fire"),
    ],
    ids=["url-path", "apl-name"],
)
def test_identify_build_drops_a_guessed_spec_the_hash_does_not_decode_as(
    tmp_path: Path, guessed: BuildSpec, apl_path: str | None, note: str
) -> None:
    repo = _repo(tmp_path)
    tried: list[tuple[str | None, str | None]] = []

    with patch("simc_cli.build_input.decode_build", side_effect=_decodes_only_as("hunter", "beast_mastery", tried)):
        identified, identity = identify_build(repo, guessed, apl_path=tmp_path / apl_path if apl_path else None)

    assert (identified.actor_class, identified.spec) == ("hunter", "beast_mastery")
    assert (identity.source, identity.confidence) == ("simc_probe", "high")
    assert note in identity.source_notes


def test_identify_build_does_not_confirm_a_caller_spec_the_hash_does_not_decode_as(tmp_path: Path) -> None:
    """The caller's class and spec are kept, but identification no longer reports them as the build's."""
    repo = _repo(tmp_path)
    tried: list[tuple[str | None, str | None]] = []
    build_spec = BuildSpec(
        actor_class="mage", spec="fire", talents="SHADOW_HASH", source_kind="wow_talent_export",
        source_notes=["command-line build options"],
    )

    with patch("simc_cli.build_input.decode_build", side_effect=_decodes_only_as("priest", "shadow", tried)):
        identified, identity = identify_build(repo, build_spec)

    assert set(tried) == {("mage", "fire")}
    assert (identified.actor_class, identified.spec) == ("mage", "fire")
    assert (identity.confidence, identity.candidates) == ("none", [])


def test_identify_build_keeps_the_one_spec_the_build_decodes_as(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    build_spec = BuildSpec(talents="ABC123", source_kind="wow_talent_export")

    with (
        patch("simc_cli.build_input.specialization_ids", return_value={("demonhunter", "devourer"): 1480, ("monk", "mistweaver"): 270}),
        patch("simc_cli.build_input.decode_build") as mocked_decode,
    ):
        mocked_decode.side_effect = [
            type("Resolution", (), {"enabled_talents": {"void_ray"}})(),
            SimcBuildError("failed", output_preview=[], returncode=1),
        ]
        identified, identity = identify_build(repo, build_spec)

    assert identified.actor_class == "demonhunter"
    assert identified.spec == "devourer"
    assert "identified by SimC probe" in identified.source_notes
    assert identity.source == "simc_probe"
    assert identity.candidates == [("demonhunter", "devourer")]


def test_identify_build_probes_simc_split_talent_packets_instead_of_trusting_packet_metadata(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    build_spec = BuildSpec(
        actor_class=None,
        spec=None,
        class_talents="103324:1",
        spec_talents="109839:1",
        hero_talents="117176:1",
        source_kind="simc_split_talents",
        source_notes=["talent transport packet"],
    )

    with (
        patch("simc_cli.build_input.specialization_ids", return_value={("druid", "balance"): 102, ("monk", "mistweaver"): 270}),
        patch("simc_cli.build_input.decode_build") as mocked_decode,
    ):
        mocked_decode.side_effect = [
            type("Resolution", (), {"enabled_talents": {"stellar_flare"}})(),
            SimcBuildError("failed", output_preview=[], returncode=1),
        ]
        identified, identity = identify_build(repo, build_spec)

    assert identified.actor_class == "druid"
    assert identified.spec == "balance"
    assert identity.source == "simc_probe"
    assert identity.confidence == "high"
    assert identity.candidates == [("druid", "balance")]


def test_identify_build_returns_none_when_probe_finds_no_matches(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    build_spec = BuildSpec(talents="ABC123", source_kind="wow_talent_export")

    with (
        patch("simc_cli.build_input.specialization_ids", return_value={("demonhunter", "devourer"): 1480, ("monk", "mistweaver"): 270}),
        patch("simc_cli.build_input.decode_build", side_effect=SimcBuildError("failed", output_preview=[], returncode=1)),
    ):
        identified, identity = identify_build(repo, build_spec)

    assert identified.actor_class is None
    assert identified.spec is None
    assert identity.confidence == "none"
    assert identity.candidate_count == 0


def test_identify_build_reports_ambiguous_probe_matches(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    build_spec = BuildSpec(talents="ABC123", source_kind="wow_talent_export")

    with (
        patch("simc_cli.build_input.specialization_ids", return_value={("demonhunter", "devourer"): 1480, ("monk", "mistweaver"): 270}),
        patch("simc_cli.build_input.decode_build") as mocked_decode,
    ):
        mocked_decode.side_effect = [
            type("Resolution", (), {"enabled_talents": {"void_ray"}})(),
            type("Resolution", (), {"enabled_talents": {"ancient_teachings"}})(),
        ]
        identified, identity = identify_build(repo, build_spec)

    assert identified.actor_class is None
    assert identified.spec is None
    assert identity.confidence == "low"
    assert identity.candidates == [("demonhunter", "devourer"), ("monk", "mistweaver")]


def test_identify_build_does_not_echo_unverified_packet_identity_when_probe_fails(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    build_spec = BuildSpec(
        actor_class="priest",
        spec="shadow",
        talents="ABC123",
        source_kind="wow_talent_export",
        source_notes=["inferred from apl: /tmp/priest_shadow.simc"],
        transport_form="wow_talent_export",
        transport_status="exact",
    )

    with (
        patch("simc_cli.build_input.specialization_ids", return_value={("druid", "balance"): 102, ("priest", "shadow"): 258}),
        patch("simc_cli.build_input.decode_build", side_effect=SimcBuildError("failed", output_preview=[], returncode=1)),
    ):
        identified, identity = identify_build(repo, build_spec)

    assert identified.actor_class is None
    assert identified.spec is None
    assert identity.actor_class is None
    assert identity.spec is None
    assert identity.confidence == "none"
    assert identity.candidate_count == 0


def test_identify_build_preserves_apl_inferred_scope_for_wow_export_probe(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    build_spec = BuildSpec(
        actor_class="priest",
        spec="shadow",
        talents="ABC123",
        source_kind="wow_talent_export",
        source_notes=["inferred from apl: priest_shadow"],
        transport_form="wow_talent_export",
        transport_status="exact",
    )

    with (
        patch("simc_cli.build_input.specialization_ids", return_value={("druid", "balance"): 102, ("priest", "shadow"): 258}),
        patch("simc_cli.build_input.decode_build") as mocked_decode,
    ):
        mocked_decode.side_effect = [
            type("Resolution", (), {"enabled_talents": {"mind_blast"}})(),
        ]
        identified, identity = identify_build(repo, build_spec)

    assert identified.actor_class == "priest"
    assert identified.spec == "shadow"
    assert identity.source == "simc_probe"
    assert identity.candidates == [("priest", "shadow")]
    assert mocked_decode.call_count == 1


# --- tree_entries_string ---


def test_tree_entries_string_formats_entry_rank_pairs() -> None:
    talents = [
        DecodedTalent(tree="class", name="Thick Hide", token="thick_hide", rank=1, max_rank=1, entry=103306),
        DecodedTalent(tree="class", name="Nurturing Instinct", token="nurturing_instinct", rank=2, max_rank=2, entry=103292),
    ]
    assert tree_entries_string(talents) == "103306:1/103292:2"


def test_tree_entries_string_skips_zero_rank_and_zero_entry() -> None:
    talents = [
        DecodedTalent(tree="spec", name="Active", token="active", rank=1, max_rank=1, entry=100),
        DecodedTalent(tree="spec", name="Skipped", token="skipped", rank=0, max_rank=1, entry=200),
        DecodedTalent(tree="spec", name="NoEntry", token="no_entry", rank=1, max_rank=1, entry=0),
    ]
    assert tree_entries_string(talents) == "100:1"


def test_tree_entries_string_empty_list() -> None:
    assert tree_entries_string([]) == ""


# --- diff_talent_trees ---


def _talent(name: str, entry: int, rank: int = 1, max_rank: int = 1) -> DecodedTalent:
    return DecodedTalent(
        tree="class", name=name, token=name.lower().replace(" ", "_"),
        rank=rank, max_rank=max_rank, entry=entry,
    )


def test_diff_talent_trees_detects_added_and_removed() -> None:
    base = [_talent("Thick Hide", 100), _talent("Innervate", 200)]
    other = [_talent("Thick Hide", 100), _talent("Forestwalk", 300, rank=2, max_rank=2)]
    diff = diff_talent_trees(base, other)
    assert [t.name for t in diff.added] == ["Forestwalk"]
    assert [t.name for t in diff.removed] == ["Innervate"]
    assert diff.changed == []


def test_diff_talent_trees_detects_rank_changes() -> None:
    base = [_talent("Nurturing Instinct", 100, rank=1, max_rank=2)]
    other = [_talent("Nurturing Instinct", 100, rank=2, max_rank=2)]
    diff = diff_talent_trees(base, other)
    assert diff.added == []
    assert diff.removed == []
    assert len(diff.changed) == 1
    assert diff.changed[0][0].rank == 1
    assert diff.changed[0][1].rank == 2


def test_diff_talent_trees_identical_returns_empty() -> None:
    talents = [_talent("Thick Hide", 100), _talent("Innervate", 200)]
    diff = diff_talent_trees(talents, talents)
    assert diff.added == []
    assert diff.removed == []
    assert diff.changed == []


def test_diff_talent_trees_skips_zero_rank_entries() -> None:
    base = [
        _talent("Active", 100),
        DecodedTalent(tree="class", name="Zero", token="zero", rank=0, max_rank=1, entry=200),
    ]
    other = [_talent("Active", 100)]
    diff = diff_talent_trees(base, other)
    assert diff.added == []
    assert diff.removed == []
    assert diff.changed == []


def test_diff_talent_trees_reports_a_lost_tiered_node_whose_rank_was_never_readable() -> None:
    """A tiered node decodes at rank 0 with an unknown rank; dropping it is still a removal."""
    tiered = DecodedTalent(
        tree="spec", name="Prismatic Bolt", token="prismatic_bolt", rank=0, max_rank=1, entry=137028, rank_known=False,
    )
    diff = diff_talent_trees([_talent("Active", 100), tiered], [_talent("Active", 100)])
    assert [t.name for t in diff.removed] == ["Prismatic Bolt"]


# --- encode_build ---


def test_encode_build_extracts_talents_from_save_output(tmp_path: Path) -> None:
    repo = _repo(tmp_path)

    def fake_run(cmd, **kwargs):  # noqa: ANN001, ANN003
        # SimC writes a save file; simulate that by writing to the save= path.
        profile_text = Path(str(cmd[1])).read_text()
        for line in profile_text.splitlines():
            if line.startswith("save="):
                Path(line.split("=", 1)[1]).write_text("talents=ENCODED_RESULT_123\n")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    spec = BuildSpec(actor_class="druid", spec="balance", talents="ORIGINAL")
    with patch("simc_cli.build_input.subprocess.run", side_effect=fake_run):
        result = encode_build(repo, spec)

    assert result == "ENCODED_RESULT_123"


def test_encode_build_profile_loads_default_gear(tmp_path: Path) -> None:
    """A gearless actor is dropped before SimC generates profiles, so the save file never appears."""
    repo = _repo(tmp_path)
    seen: dict[str, str] = {}

    def fake_run(cmd, **kwargs):  # noqa: ANN001, ANN003
        profile_path = Path(str(cmd[1]))
        profile_text = profile_path.read_text()
        seen["profile"] = profile_text
        if "load_default_gear=1" not in profile_text:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="No active players in sim!")
        for line in profile_text.splitlines():
            if line.startswith("save="):
                Path(line.split("=", 1)[1]).write_text("talents=ENCODED_WITH_GEAR\n")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    spec = BuildSpec(actor_class="monk", spec="windwalker", talents="ORIGINAL", class_talents="tigers_lust:0")
    with patch("simc_cli.build_input.subprocess.run", side_effect=fake_run):
        result = encode_build(repo, spec)

    assert result == "ENCODED_WITH_GEAR"
    assert "load_default_gear=1" in seen["profile"]


def test_encode_build_raises_when_save_file_missing(tmp_path: Path) -> None:
    repo = _repo(tmp_path)

    stderr = "Error: Generating profiles: Player 'simc_decode': Invalid 'class_talents': Unable to find class talent 'bogus'.\n"
    with patch("simc_cli.build_input.subprocess.run") as mocked_run:
        mocked_run.return_value = subprocess.CompletedProcess([], 1, stdout="banner\n" * 900, stderr=stderr)
        with pytest.raises(SimcBuildError) as caught:
            encode_build(repo, BuildSpec(actor_class="druid", spec="balance", talents="ABC"))

    assert str(caught.value) == "Generating profiles: Player 'simc_decode': Invalid 'class_talents': Unable to find class talent 'bogus'."
    assert caught.value.returncode == 1


def test_encode_build_raises_without_class_or_spec(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    with pytest.raises(ValueError, match="actor class and spec"):
        encode_build(repo, BuildSpec(talents="ABC"))


def test_bounded_output_preview_clips_a_single_enormous_debug_line() -> None:
    """SimC writes the enemy's stat block on one ~4 KB line, so a 20-line tail is not a bound."""
    preview = bounded_output_preview("\n".join(["short"] * 30 + ["x" * 5000]))

    assert len(preview) == 20
    assert preview[:-1] == ["short"] * 19
    assert preview[-1].startswith("x" * 200)
    assert preview[-1].endswith("... (5000 chars, truncated)")
    assert max(len(line) for line in preview) < 250

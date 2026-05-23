from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from simc_cli.build_input import (
    BuildSpec,
    DecodedTalent,
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
    supported_specs,
    tokenize_talent_name,
    tree_entries_string,
)
from simc_cli.repo import RepoPaths

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "simc"


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
            talents="ABC123",
            class_talents=None,
            spec_talents=None,
            hero_talents=None,
        )
        == "wow_talent_export"
    )
    assert (
        detect_talents_option_source_kind(
            talents="https://www.wowhead.com/talent-calc/demon-hunter/devourer/ABC123",
            class_talents=None,
            spec_talents=None,
            hero_talents=None,
        )
        == "wowhead_talent_calc_url"
    )
    assert (
        detect_talents_option_source_kind(
            talents="talents=ABC123",
            class_talents=None,
            spec_talents=None,
            hero_talents=None,
        )
        == "simc_profile"
    )
    assert (
        detect_talents_option_source_kind(
            talents=None,
            class_talents="AAA",
            spec_talents="BBB",
            hero_talents="CCC",
        )
        == "simc_split_talents"
    )


def test_load_build_spec_extracts_class_and_spec_from_talents_url() -> None:
    spec = load_build_spec(
        apl_path=None,
        profile_path=None,
        build_file=None,
        build_text=None,
        talents="https://www.wowhead.com/talent-calc/demon-hunter/devourer/ABC123",
        class_talents=None,
        spec_talents=None,
        hero_talents=None,
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
        apl_path=None,
        profile_path=None,
        build_file=None,
        build_text=None,
        talents=None,
        class_talents=None,
        spec_talents=None,
        hero_talents=None,
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
            apl_path=None,
            profile_path=None,
            build_file=None,
            build_text=None,
            talents=None,
            class_talents=None,
            spec_talents=None,
            hero_talents=None,
            actor_class=None,
            spec_name=None,
            build_packet=str(packet_path),
        )


def test_load_build_spec_rejects_buildless_wowhead_talent_calc_url() -> None:
    try:
        load_build_spec(
            apl_path=None,
            profile_path=None,
            build_file=None,
            build_text="https://www.wowhead.com/talent-calc/druid/balance",
            talents=None,
            class_talents=None,
            spec_talents=None,
            hero_talents=None,
            actor_class=None,
            spec_name=None,
        )
    except ValueError as exc:
        assert "must include a build code" in str(exc)
    else:
        raise AssertionError("expected ValueError")


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
        apl_path=None,
        profile_path=None,
        build_file=None,
        build_text=None,
        talents=None,
        class_talents=None,
        spec_talents=None,
        hero_talents=None,
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
        apl_path=None,
        profile_path=None,
        build_file=None,
        build_text=None,
        talents=None,
        class_talents=None,
        spec_talents=None,
        hero_talents=None,
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
            apl_path=None,
            profile_path=None,
            build_file=None,
            build_text=None,
            talents=None,
            class_talents=None,
            spec_talents=None,
            hero_talents=None,
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
        apl_path=None,
        profile_path=None,
        build_file=None,
        build_text=None,
        talents=None,
        class_talents=None,
        spec_talents=None,
        hero_talents=None,
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
    repo = RepoPaths(
        root=tmp_path,
        apl_default=tmp_path,
        apl_assisted=tmp_path,
        class_modules=tmp_path,
        spell_dump=tmp_path,
        build_dir=tmp_path,
        build_simc=tmp_path / "simc",
    )
    build_spec = BuildSpec(
        actor_class="priest",
        spec="shadow",
        talents="ABC123",
        source_kind="wow_talent_export",
        source_notes=["build packet: /tmp/forged.json", "talent transport packet"],
    )

    identified, identity = identify_build(repo, build_spec)

    assert identified.actor_class == "priest"
    assert identified.spec == "shadow"
    assert identity.source == "wow_talent_export"
    assert identity.confidence == "medium"


def test_parse_wowhead_talent_calc_ref_rejects_nested_talent_calc_segments() -> None:
    assert parse_wowhead_talent_calc_ref("talent-calc/foo/talent-calc/druid/balance/ABC123") is None


def test_load_build_spec_uses_apl_inference_when_packet_identity_is_missing(tmp_path: Path) -> None:
    packet_path = tmp_path / "build-packet.json"
    apl_path = tmp_path / "druid_balance.simc"
    apl_path.write_text("actions=wrath\n")
    packet_path.write_text(
        """
        {
          "kind": "talent_transport_packet",
          "transport_status": "exact",
          "build_identity": {
            "class_spec_identity": {
              "identity": {}
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
        apl_path=str(apl_path),
        profile_path=None,
        build_file=None,
        build_text=None,
        talents=None,
        class_talents=None,
        spec_talents=None,
        hero_talents=None,
        actor_class=None,
        spec_name=None,
        build_packet=str(packet_path),
    )

    assert spec.actor_class == "druid"
    assert spec.spec == "balance"
    assert spec.talents == "ABC123"
    assert any(note.startswith("inferred from apl:") for note in spec.source_notes)


def test_load_build_spec_lets_apl_inference_override_unverified_wow_export_packet_identity(tmp_path: Path) -> None:
    packet_path = tmp_path / "build-packet.json"
    apl_path = tmp_path / "evoker_devastation.simc"
    apl_path.write_text("actions=disintegrate\n")
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
        apl_path=str(apl_path),
        profile_path=None,
        build_file=None,
        build_text=None,
        talents=None,
        class_talents=None,
        spec_talents=None,
        hero_talents=None,
        actor_class=None,
        spec_name=None,
        build_packet=str(packet_path),
    )

    assert spec.actor_class == "evoker"
    assert spec.spec == "devastation"
    assert spec.talents == "ABC123"
    assert any(note.startswith("inferred from apl:") for note in spec.source_notes)


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
            apl_path=None,
            profile_path=None,
            build_file=None,
            build_text=None,
            talents=None,
            class_talents=None,
            spec_talents=None,
            hero_talents=None,
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
    binary = tmp_path / "simc"
    binary.write_text("")
    repo = RepoPaths(
        root=tmp_path,
        apl_default=tmp_path,
        apl_assisted=tmp_path,
        class_modules=tmp_path,
        spell_dump=tmp_path,
        build_dir=tmp_path,
        build_simc=binary,
    )
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


def test_supported_specs_collects_unique_apl_specs(tmp_path: Path) -> None:
    default_dir = tmp_path / "default"
    assisted_dir = tmp_path / "assisted"
    default_dir.mkdir()
    assisted_dir.mkdir()
    (default_dir / "demonhunter_devourer.simc").write_text("")
    (assisted_dir / "demonhunter_devourer.simc").write_text("")
    (default_dir / "warlock_demonology.simc").write_text("")
    repo = RepoPaths(
        root=tmp_path,
        apl_default=default_dir,
        apl_assisted=assisted_dir,
        class_modules=tmp_path,
        spell_dump=tmp_path,
        build_dir=tmp_path,
        build_simc=tmp_path / "simc",
    )
    assert supported_specs(repo) == [("demonhunter", "devourer"), ("warlock", "demonology")]


def test_identify_build_uses_direct_metadata_without_probe(tmp_path: Path) -> None:
    repo = RepoPaths(
        root=tmp_path,
        apl_default=tmp_path,
        apl_assisted=tmp_path,
        class_modules=tmp_path,
        spell_dump=tmp_path,
        build_dir=tmp_path,
        build_simc=tmp_path / "simc",
    )
    build_spec = BuildSpec(actor_class="demonhunter", spec="devourer", talents="ABC123", source_kind="wowhead_talent_calc_url")
    identified, identity = identify_build(repo, build_spec)
    assert identified.actor_class == "demonhunter"
    assert identified.spec == "devourer"
    assert identity.source == "wowhead_talent_calc_url"
    assert identity.confidence == "high"


def test_identify_build_probes_supported_specs(tmp_path: Path) -> None:
    repo = RepoPaths(
        root=tmp_path,
        apl_default=tmp_path,
        apl_assisted=tmp_path,
        class_modules=tmp_path,
        spell_dump=tmp_path,
        build_dir=tmp_path,
        build_simc=tmp_path / "simc",
    )
    build_spec = BuildSpec(talents="ABC123", source_kind="wow_talent_export")

    with (
        patch("simc_cli.build_input.supported_specs", return_value=[("monk", "mistweaver"), ("demonhunter", "devourer")]),
        patch("simc_cli.build_input.decode_build") as mocked_decode,
    ):
        mocked_decode.side_effect = [
            RuntimeError("failed"),
            type("Resolution", (), {"enabled_talents": {"void_ray"}})(),
        ]
        identified, identity = identify_build(repo, build_spec)

    assert identified.actor_class == "demonhunter"
    assert identified.spec == "devourer"
    assert "identified by SimC probe" in identified.source_notes
    assert identity.source == "simc_probe"
    assert identity.candidates == [("demonhunter", "devourer")]


def test_identify_build_probes_simc_split_talent_packets_instead_of_trusting_packet_metadata(tmp_path: Path) -> None:
    repo = RepoPaths(
        root=tmp_path,
        apl_default=tmp_path,
        apl_assisted=tmp_path,
        class_modules=tmp_path,
        spell_dump=tmp_path,
        build_dir=tmp_path,
        build_simc=tmp_path / "simc",
    )
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
        patch("simc_cli.build_input.supported_specs", return_value=[("monk", "mistweaver"), ("druid", "balance")]),
        patch("simc_cli.build_input.decode_build") as mocked_decode,
    ):
        mocked_decode.side_effect = [
            RuntimeError("failed"),
            type("Resolution", (), {"enabled_talents": {"stellar_flare"}})(),
        ]
        identified, identity = identify_build(repo, build_spec)

    assert identified.actor_class == "druid"
    assert identified.spec == "balance"
    assert identity.source == "simc_probe"
    assert identity.confidence == "high"
    assert identity.candidates == [("druid", "balance")]


def test_identify_build_returns_none_when_probe_finds_no_matches(tmp_path: Path) -> None:
    repo = RepoPaths(
        root=tmp_path,
        apl_default=tmp_path,
        apl_assisted=tmp_path,
        class_modules=tmp_path,
        spell_dump=tmp_path,
        build_dir=tmp_path,
        build_simc=tmp_path / "simc",
    )
    build_spec = BuildSpec(talents="ABC123", source_kind="wow_talent_export")

    with (
        patch("simc_cli.build_input.supported_specs", return_value=[("monk", "mistweaver"), ("demonhunter", "devourer")]),
        patch("simc_cli.build_input.decode_build", side_effect=RuntimeError("failed")),
    ):
        identified, identity = identify_build(repo, build_spec)

    assert identified.actor_class is None
    assert identified.spec is None
    assert identity.confidence == "none"
    assert identity.candidate_count == 0


def test_identify_build_reports_ambiguous_probe_matches(tmp_path: Path) -> None:
    repo = RepoPaths(
        root=tmp_path,
        apl_default=tmp_path,
        apl_assisted=tmp_path,
        class_modules=tmp_path,
        spell_dump=tmp_path,
        build_dir=tmp_path,
        build_simc=tmp_path / "simc",
    )
    build_spec = BuildSpec(talents="ABC123", source_kind="wow_talent_export")

    with (
        patch("simc_cli.build_input.supported_specs", return_value=[("monk", "mistweaver"), ("demonhunter", "devourer")]),
        patch("simc_cli.build_input.decode_build") as mocked_decode,
    ):
        mocked_decode.side_effect = [
            type("Resolution", (), {"enabled_talents": {"ancient_teachings"}})(),
            type("Resolution", (), {"enabled_talents": {"void_ray"}})(),
        ]
        identified, identity = identify_build(repo, build_spec)

    assert identified.actor_class is None
    assert identified.spec is None
    assert identity.confidence == "low"
    assert identity.candidates == [("monk", "mistweaver"), ("demonhunter", "devourer")]


def test_identify_build_does_not_echo_unverified_packet_identity_when_probe_fails(tmp_path: Path) -> None:
    repo = RepoPaths(
        root=tmp_path,
        apl_default=tmp_path,
        apl_assisted=tmp_path,
        class_modules=tmp_path,
        spell_dump=tmp_path,
        build_dir=tmp_path,
        build_simc=tmp_path / "simc",
    )
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
        patch("simc_cli.build_input.supported_specs", return_value=[("priest", "shadow"), ("druid", "balance")]),
        patch("simc_cli.build_input.decode_build", side_effect=RuntimeError("failed")),
    ):
        identified, identity = identify_build(repo, build_spec)

    assert identified.actor_class is None
    assert identified.spec is None
    assert identity.actor_class is None
    assert identity.spec is None
    assert identity.confidence == "none"
    assert identity.candidate_count == 0


def test_identify_build_preserves_apl_inferred_scope_for_wow_export_probe(tmp_path: Path) -> None:
    repo = RepoPaths(
        root=tmp_path,
        apl_default=tmp_path,
        apl_assisted=tmp_path,
        class_modules=tmp_path,
        spell_dump=tmp_path,
        build_dir=tmp_path,
        build_simc=tmp_path / "simc",
    )
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
        patch("simc_cli.build_input.supported_specs", return_value=[("priest", "shadow"), ("druid", "balance")]),
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


# --- encode_build ---


def test_encode_build_extracts_talents_from_save_output(tmp_path: Path) -> None:
    binary = tmp_path / "simc"
    binary.write_text("")
    repo = RepoPaths(
        root=tmp_path,
        apl_default=tmp_path,
        apl_assisted=tmp_path,
        class_modules=tmp_path,
        spell_dump=tmp_path,
        build_dir=tmp_path,
        build_simc=binary,
    )

    def fake_run(cmd, **kwargs):  # noqa: ANN001, ANN003
        # SimC writes a save file; simulate that by writing to the save= path.
        for arg in cmd:
            arg_str = str(arg)
            if "encode.simc" in arg_str:
                # Read the profile to find the save= path.
                profile_text = Path(arg_str).read_text()
                for line in profile_text.splitlines():
                    if line.startswith("save="):
                        save_path = line.split("=", 1)[1]
                        Path(save_path).write_text("talents=ENCODED_RESULT_123\n")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    spec = BuildSpec(actor_class="druid", spec="balance", talents="ORIGINAL")
    with patch("simc_cli.build_input.subprocess.run", side_effect=fake_run):
        result = encode_build(repo, spec)

    assert result == "ENCODED_RESULT_123"


def test_encode_build_raises_when_save_file_missing(tmp_path: Path) -> None:
    binary = tmp_path / "simc"
    binary.write_text("")
    repo = RepoPaths(
        root=tmp_path,
        apl_default=tmp_path,
        apl_assisted=tmp_path,
        class_modules=tmp_path,
        spell_dump=tmp_path,
        build_dir=tmp_path,
        build_simc=binary,
    )

    with patch("simc_cli.build_input.subprocess.run") as mocked_run:
        mocked_run.return_value = subprocess.CompletedProcess([], 1, stdout="", stderr="error msg")
        with pytest.raises(RuntimeError, match="error msg"):
            encode_build(repo, BuildSpec(actor_class="druid", spec="balance", talents="ABC"))


def test_encode_build_raises_without_class_or_spec(tmp_path: Path) -> None:
    repo = RepoPaths(
        root=tmp_path,
        apl_default=tmp_path,
        apl_assisted=tmp_path,
        class_modules=tmp_path,
        spell_dump=tmp_path,
        build_dir=tmp_path,
        build_simc=tmp_path / "simc",
    )
    with pytest.raises(ValueError, match="actor class and spec"):
        encode_build(repo, BuildSpec(talents="ABC"))

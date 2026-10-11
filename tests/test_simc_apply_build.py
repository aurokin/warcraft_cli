"""Geared profile handoffs preserve caller inputs and require verified build identity."""

from __future__ import annotations

import json
import os
from dataclasses import replace

import pytest
from simc_cli.build_input import BuildIdentity, BuildResolution, BuildSpec
from simc_cli.main import app
from simc_cli.profile_build import apply_build_payload
from simc_cli.repo import discover_repo
from typer.testing import CliRunner
from warcraft_core.provider import ProviderError

PROFILE = """# supplied equipment and settings
iterations=1200
mage=Example
level=90
spec=frost
race=blood_elf
talents=OLD
class_talents=1:1
head=id=123,bonus_id=456
trinket1=id=789
max_time=300
# talents=COMMENT
"""


@pytest.fixture
def handoff(tmp_path, monkeypatch):
    source = tmp_path / "geared.simc"
    source.write_text(PROFILE)
    destination = tmp_path / "variant.simc"
    spec = BuildSpec(actor_class="mage", spec="frost", talents="VERIFIED", source_kind="inline")
    identity = BuildIdentity(actor_class="mage", spec="frost", confidence="high", source="decoded", candidate_count=1)
    resolution = BuildResolution(
        actor_class="mage",
        spec="frost",
        enabled_talents=set(),
        talents_by_tree={},
        source_kind="inline",
        generated_profile_text=None,
        source_notes=[],
    )
    monkeypatch.setattr("simc_cli.build_services._identified_build_or_raise", lambda *_args, **_kwargs: (spec, identity))
    monkeypatch.setattr("simc_cli.build_services._decode_or_raise", lambda *_args, **_kwargs: resolution)
    return source, destination, discover_repo(tmp_path / "checkout"), spec, identity, resolution


def apply(handoff, *, overwrite=False):
    source, destination, paths, *_rest = handoff
    return apply_build_payload(
        paths, profile_path=source, build="mage=Example\nspec=frost\ntalents=VERIFIED", out=destination, overwrite=overwrite
    )


def test_combined_talents_preserve_equipment_settings_and_source(handoff):
    source, destination, *_rest = handoff
    original = source.read_bytes()
    result = apply(handoff)
    assert source.read_bytes() == original
    output = destination.read_text()
    for line in ["head=id=123,bonus_id=456", "trinket1=id=789", "iterations=1200", "max_time=300", "race=blood_elf", "# talents=COMMENT"]:
        assert line in output
    assert "talents=VERIFIED\n" in output and "talents=OLD" not in output and "class_talents=1:1" not in output
    assert result["validation"] == {"status": "decoded", "actor_class": "mage", "spec": "frost"}
    assert "not been simulated" in " ".join(result["notes"])


def test_split_talents_replace_all_previous_forms(handoff):
    source, destination, _paths, spec, *_rest = handoff
    spec.talents = None
    spec.class_talents, spec.spec_talents, spec.hero_talents = "10:1", "20:2", "30:1"
    source.write_text(PROFILE + "hero_talents=OLDHERO\nspec_talents=OLDSPEC\n")
    apply(handoff)
    output = destination.read_text()
    assert "class_talents=10:1\nspec_talents=20:2\nhero_talents=30:1\n" in output
    assert "OLD" not in output


def test_existing_destination_requires_explicit_overwrite(handoff):
    _source, destination, *_rest = handoff
    destination.write_text("KEEP")
    with pytest.raises(ProviderError, match="already exists") as failure:
        apply(handoff)
    assert failure.value.code == "output_exists" and destination.read_text() == "KEEP"
    apply(handoff, overwrite=True)
    assert "talents=VERIFIED" in destination.read_text()


@pytest.mark.parametrize("alias", ["same", "symlink", "hardlink"])
def test_source_aliases_are_rejected_even_with_overwrite(handoff, alias):
    source, destination, paths, *_rest = handoff
    if alias == "same":
        destination = source
    elif alias == "symlink":
        destination.symlink_to(source)
    else:
        os.link(source, destination)
    with pytest.raises(ProviderError) as failure:
        apply_build_payload(paths, profile_path=source, build="build", out=destination, overwrite=True)
    assert failure.value.code == "output_path_conflict"
    assert source.read_text() == PROFILE


@pytest.mark.parametrize("extra", ["mage=Second\nspec=frost\n", "input=other.simc\n", "copy=Second\n", "profileset.A+=talents=OTHER\n"])
def test_multiactor_or_indirect_profiles_rejected_before_decoding(handoff, monkeypatch, extra):
    source, destination, *_rest = handoff
    source.write_text(PROFILE + extra)
    monkeypatch.setattr(
        "simc_cli.build_services._identified_build_or_raise", lambda *_args, **_kwargs: pytest.fail("Decoded invalid profile")
    )
    with pytest.raises(ProviderError) as failure:
        apply(handoff)
    assert failure.value.code == "invalid_profile" and not destination.exists()


def test_class_spec_mismatch_never_publishes(handoff):
    _source, destination, _paths, spec, *_rest = handoff
    spec.spec = "arcane"
    with pytest.raises(ProviderError) as failure:
        apply(handoff)
    assert failure.value.code == "build_identity_mismatch" and not destination.exists()


@pytest.mark.parametrize("directive", ["armory", "guild", "local_json", "player_simplified", "pet", "guardian", "active"])
def test_unexpanded_player_controls_are_rejected_before_build_work(handoff, monkeypatch, directive):
    source, destination, *_rest = handoff
    source.write_text(PROFILE + f"{directive}=Other\n")
    monkeypatch.setattr(
        "simc_cli.build_services._identified_build_or_raise", lambda *_args, **_kwargs: pytest.fail("Read unexpanded player")
    )
    with pytest.raises(ProviderError) as failure:
        apply(handoff)
    assert failure.value.code == "unsupported_profile" and not destination.exists()


def test_enemy_settings_remain_after_applying_the_player_build(handoff):
    source, destination, *_rest = handoff
    source.write_text(PROFILE + "enemy=Boss\nhealth=10000000\n")
    apply(handoff)
    assert "enemy=Boss\nhealth=10000000\n" in destination.read_text()


@pytest.mark.parametrize("profile", ["spec=frost\nmage=Example\n", "mage=Example\nenemy=Boss\nspec=frost\n"])
def test_spec_must_belong_to_the_declared_player(handoff, profile):
    source, destination, *_rest = handoff
    source.write_text(profile)
    with pytest.raises(ProviderError) as failure:
        apply(handoff)
    assert failure.value.code == "invalid_profile" and not destination.exists()


def test_decoder_identity_mismatch_never_publishes(handoff, monkeypatch):
    _source, destination, _paths, _spec, _identity, resolution = handoff
    monkeypatch.setattr("simc_cli.build_services._decode_or_raise", lambda *_args, **_kwargs: replace(resolution, spec="arcane"))
    with pytest.raises(ProviderError) as failure:
        apply(handoff)
    assert failure.value.code == "build_identity_mismatch" and not destination.exists()


def test_rejected_talent_transport_never_publishes(handoff, monkeypatch):
    _source, destination, *_rest = handoff

    def invalid(*_args, **_kwargs):
        raise ProviderError("invalid_build", "SimC rejected the supplied talents.", exit_code=2)

    monkeypatch.setattr("simc_cli.build_services._decode_or_raise", invalid)
    with pytest.raises(ProviderError) as failure:
        apply(handoff)
    assert failure.value.code == "invalid_build" and not destination.exists()


def test_cli_reports_written_profile_and_requires_one_build_input(handoff):
    source, destination, *_rest = handoff
    runner = CliRunner()
    result = runner.invoke(
        app, ["apply-build", str(source), "--build-text", "mage=Example\nspec=frost\ntalents=VERIFIED", "--out", str(destination)]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["command"] == "apply-build" and payload["data"]["written_profile_path"] == str(destination)
    conflict = runner.invoke(app, ["apply-build", str(source), "--out", str(destination)])
    assert conflict.exit_code == 2 and json.loads(conflict.stderr)["error"]["code"] == "invalid_query"


@pytest.mark.parametrize(
    "override",
    ["load_default_talents=1", "enable_all_talents=1", "class_talents+=10:0", "spec_talents+=20:0", "hero_talents+=30:0"],
)
def test_source_talent_overrides_cannot_change_the_verified_build(handoff, override):
    source, destination, *_rest = handoff
    source.write_text(PROFILE + override + "\nomnium_talents=expansion_effect:1\nomnium_talents+=other_effect:1\n")
    apply(handoff)
    output = destination.read_text()
    assert "talents=VERIFIED" in output
    assert override not in output
    assert "omnium_talents=expansion_effect:1\nomnium_talents+=other_effect:1\n" in output


def test_build_without_talents_does_not_publish(handoff):
    _source, destination, _paths, spec, *_rest = handoff
    spec.talents = None
    with pytest.raises(ProviderError) as failure:
        apply(handoff)
    assert failure.value.code == "invalid_query" and not destination.exists()


def test_malformed_packet_fails_without_writing_profile(handoff, tmp_path):
    source, destination, *_rest = handoff
    packet = tmp_path / "broken-packet.json"
    packet.write_text('{"kind":"talent_transport_packet","transport_forms":false}')
    result = CliRunner().invoke(app, ["apply-build", str(source), "--build-packet", str(packet), "--out", str(destination)])
    assert result.exit_code == 2
    assert json.loads(result.stderr)["error"]["code"] == "invalid_build_packet"
    assert not destination.exists()

"""The SimC-backed talent transport backend simc_cli supplies to warcraft_core, and the pure
build commands the ``warcraft`` wrapper calls with a packet held in memory."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from simc_cli import build_input
from simc_cli.build_services import identify_build_payload, validate_transport_packet_payload
from simc_cli.main import app as simc_app
from simc_cli.provider import simc_envelope
from simc_cli.repo import RepoPaths
from simc_cli.talent_transport import simc_backend
from typer.testing import CliRunner
from warcraft_core.provider import ProviderError
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


def test_round_trip_returns_the_export_and_every_decoded_entry(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(build_input, "encode_build", lambda paths, spec: "EXPORT")
    monkeypatch.setattr(build_input, "decode_build", lambda paths, spec: _resolution(entry=101, rank=1))

    backend = simc_backend(tmp_path)
    result = backend.round_trip(BuildSpec(actor_class="druid", spec="balance", class_talents="101:1"))

    assert backend.trait_data_root == tmp_path.resolve()
    assert result.wow_talent_export == "EXPORT"
    # SimC reports a tiered node as one rank-0 line, so rank-0 entries stay visible for core to
    # compare by node presence.
    assert result.entries_by_tree == {"class": {101: 1}, "spec": {999: 0}, "hero": {}}


def test_encode_failure_becomes_a_round_trip_error(monkeypatch, tmp_path: Path) -> None:
    def boom(paths: RepoPaths, spec: build_input.BuildSpec) -> str:
        raise RuntimeError("simc binary missing")

    monkeypatch.setattr(build_input, "encode_build", boom)

    backend = simc_backend(tmp_path)
    with pytest.raises(RoundTripError, match="simc binary missing"):
        backend.round_trip(BuildSpec(actor_class="druid", spec="balance", class_talents="101:1"))


# --- pure build commands over an in-memory packet --------------------------------------------------

_RAW_PACKET: dict[str, Any] = {
    "kind": "talent_transport_packet",
    "transport_status": "raw_only",
    "build_identity": {"class_spec_identity": {"identity": {"actor_class": "druid", "spec": "balance"}}},
    "transport_forms": {},
    "validation": {},
    "scope": {},
    "raw_evidence": {"talent_tree_entries": [{"entry": 103324, "node_id": 82244, "rank": 1}]},
}
_EXACT_PACKET: dict[str, Any] = {
    "kind": "talent_transport_packet",
    "transport_status": "exact",
    "build_identity": {"class_spec_identity": {"identity": {"actor_class": "druid", "spec": "balance"}}},
    "transport_forms": {"wowhead_talent_calc_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123"},
    "validation": {},
    "scope": {},
    "raw_evidence": {"reference_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123"},
}


def test_an_in_memory_packet_validates_exactly_as_the_cli_validates_its_file(monkeypatch, tmp_path: Path) -> None:
    """The wrapper's in-process call and `simc validate-talent-transport` share one function."""
    monkeypatch.setattr(
        "simc_cli.build_services.validate_talent_tree_transport",
        lambda **kwargs: {
            "transport_forms": {"simc_split_talents": {"class_talents": "103324:1"}},
            "validation": {"status": "validated", "actor_class": "druid", "spec": "balance"},
        },
    )
    packet_path = tmp_path / "packet.json"
    packet_path.write_text(json.dumps(_RAW_PACKET))

    cli = CliRunner().invoke(simc_app, ["validate-talent-transport", "--build-packet", str(packet_path)])
    pure = validate_transport_packet_payload(build_input.PacketInput(_RAW_PACKET, str(packet_path.resolve())))

    assert cli.exit_code == 0, cli.output
    assert simc_envelope("validate-talent-transport", pure) == json.loads(cli.stdout)
    assert validate_transport_packet_payload(build_input.PacketInput(_RAW_PACKET))["input"]["build_packet"] is None


def test_an_in_memory_packet_cites_only_the_file_its_caller_names(monkeypatch) -> None:
    monkeypatch.setattr(
        "simc_cli.build_services.identify_build",
        lambda _paths, spec, *, apl_path: (
            spec,
            build_input.BuildIdentity(actor_class=spec.actor_class, spec=spec.spec, confidence="high", source="direct",
                                      candidate_count=1),
        ),
    )

    unfiled = identify_build_payload(build_input.PacketInput(_EXACT_PACKET))["build_spec"]
    assert "path" not in unfiled["transport_packet"]
    assert not any(note.startswith("build packet:") for note in unfiled["source_notes"])

    filed = identify_build_payload(build_input.PacketInput(_EXACT_PACKET, "/packets/balance.json"))["build_spec"]
    assert filed["transport_packet"]["path"] == "/packets/balance.json"
    assert filed["source_notes"][0] == "build packet: /packets/balance.json"


def test_the_pure_build_commands_raise_instead_of_exiting() -> None:
    with pytest.raises(ProviderError) as excinfo:
        # An exact packet carries no raw talent rows, so there is nothing to round-trip.
        validate_transport_packet_payload(build_input.PacketInput(_EXACT_PACKET))
    assert excinfo.value.code == "invalid_query"

    with pytest.raises(ProviderError) as excinfo:
        validate_transport_packet_payload(build_input.PacketInput({"kind": "not_a_packet"}))
    assert excinfo.value.code == "invalid_build_packet"

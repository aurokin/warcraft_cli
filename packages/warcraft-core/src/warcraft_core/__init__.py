"""Shared Warcraft core utilities."""

from warcraft_core.cli import RuntimeConfig, cfg, configure, emit, fail, guarded_run, install_common_callback
from warcraft_core.env import find_env_file, load_env_file, load_explicit_env_file
from warcraft_core.envelope import Envelope, error_envelope, success_envelope
from warcraft_core.exit_codes import exit_code_for
from warcraft_core.expansions import expansion_keys, list_expansions, resolve_expansion
from warcraft_core.identity import (
    ability_identity_payload,
    build_identity_payload,
    build_reference_payload,
    build_reference_transport_packet_payload,
    class_spec_identity_payload,
    encounter_identity_payload,
    normalize_ability_name,
    normalize_actor_class,
    normalize_encounter_name,
    normalize_spec_name,
    parse_wowhead_talent_calc_ref,
    refresh_talent_transport_packet,
    report_actor_identity_payload,
    talent_transport_packet_payload,
    validate_talent_transport_packet,
)
from warcraft_core.provider import ProviderError, ProviderSurface
from warcraft_core.talent_transport import validate_talent_tree_transport

__all__ = [
    "Envelope",
    "ProviderError",
    "ProviderSurface",
    "RuntimeConfig",
    "ability_identity_payload",
    "build_identity_payload",
    "build_reference_payload",
    "build_reference_transport_packet_payload",
    "cfg",
    "class_spec_identity_payload",
    "configure",
    "emit",
    "encounter_identity_payload",
    "error_envelope",
    "exit_code_for",
    "fail",
    "expansion_keys",
    "find_env_file",
    "guarded_run",
    "install_common_callback",
    "list_expansions",
    "load_env_file",
    "load_explicit_env_file",
    "normalize_actor_class",
    "normalize_ability_name",
    "normalize_encounter_name",
    "normalize_spec_name",
    "parse_wowhead_talent_calc_ref",
    "refresh_talent_transport_packet",
    "report_actor_identity_payload",
    "success_envelope",
    "resolve_expansion",
    "talent_transport_packet_payload",
    "validate_talent_transport_packet",
    "validate_talent_tree_transport",
]

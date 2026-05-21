"""Warcraft Logs CLI payload envelope keys and dual-emit shims (v0.4.0)."""

from __future__ import annotations

import copy
from typing import Any

# Keys that stay at the top level and are not duplicated under the command envelope.
META_KEYS = frozenset(
    {
        "ok",
        "provider",
        "notes",
        "graphql_warnings",
        "command",
        "deprecated_keys",
    }
)

# Legacy primary payload key per command (hyphenated CLI name). When different from the
# canonical command key, the legacy value is dual-emitted for one minor release.
LEGACY_PRIMARY_KEYS: dict[str, str] = {
    "search": "results",
    "resolve": "match",
    "encounter-rankings": "rankings",
    "guild-reports": "reports",
    "boss-kills": "kills",
    "top-kills": "kills",
    "boss-spec-usage": "spec_usage",
    "ability-usage-summary": "usage",
    "comp-samples": "kills",
    "kill-time-distribution": "distribution",
    "report-encounter-casts": "casts",
    "report-encounter-buffs": "buffs",
    "report-encounter-aura-summary": "aura_summary",
    "report-encounter-aura-compare": "comparison",
    "report-encounter-damage-source-summary": "damage_summary",
    "report-encounter-damage-target-summary": "damage_summary",
    "report-encounter-damage-breakdown": "table",
    "report-encounter-players": "player_details",
    "report-player-talents": "talent_transport_packet",
    "report-events": "events",
    "report-table": "table",
    "report-graph": "graph",
    "report-master-data": "master_data",
    "report-player-details": "player_details",
    "report-rankings": "rankings",
    "graphql": "data",
}

# Encounter-scoped commands include these keys in the canonical envelope body.
ENCOUNTER_SCOPE_KEYS = frozenset(
    {
        "reference",
        "report",
        "fight",
        "encounter",
        "encounter_identity",
        "stability",
        "kind",
        "query",
    }
)

ENCOUNTER_COMMANDS = frozenset(
    {
        "report-encounter",
        "report-encounter-players",
        "report-player-talents",
        "report-encounter-casts",
        "report-encounter-buffs",
        "report-encounter-aura-summary",
        "report-encounter-aura-compare",
        "report-encounter-damage-source-summary",
        "report-encounter-damage-target-summary",
        "report-encounter-damage-breakdown",
    }
)


def command_to_canonical_key(command: str) -> str:
    return command.replace("-", "_")


def canonical_key_for_command(command: str) -> str:
    return command_to_canonical_key(command)


def documented_payload_keys() -> list[tuple[str, str, str | None]]:
    """Return (command, canonical_key, legacy_primary_key) for docs and tests."""
    from warcraftlogs_cli.payload_keys_registry import ALL_COMMANDS

    rows: list[tuple[str, str, str | None]] = []
    for command in ALL_COMMANDS:
        canonical = canonical_key_for_command(command)
        legacy = LEGACY_PRIMARY_KEYS.get(command)
        if legacy == canonical:
            legacy = None
        rows.append((command, canonical, legacy))
    return rows


def _encounter_envelope_body(payload: dict[str, Any], *, legacy_primary: str) -> dict[str, Any]:
    body: dict[str, Any] = {}
    for key in ENCOUNTER_SCOPE_KEYS:
        if key in payload:
            body[key] = copy.deepcopy(payload[key])
    if legacy_primary in payload:
        body[legacy_primary] = copy.deepcopy(payload[legacy_primary])
    for key, value in payload.items():
        if key in META_KEYS or key in ENCOUNTER_SCOPE_KEYS or key == legacy_primary:
            continue
        if key == command_to_canonical_key(""):  # pragma: no cover
            continue
        body[key] = copy.deepcopy(value)
    return body


def apply_payload_envelope(command: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Add canonical command key and dual-emit legacy primary keys when applicable."""
    if payload.get("ok") is False:
        return payload

    canonical = canonical_key_for_command(command)
    legacy_primary = LEGACY_PRIMARY_KEYS.get(command)
    out = copy.deepcopy(payload)
    out["command"] = command

    deprecated: list[str] = []

    if command == "graphql":
        primary = "introspection" if "introspection" in out else "data"
        out[canonical] = copy.deepcopy(out[primary])
        if primary != canonical:
            deprecated.append(primary)
        if deprecated:
            out["deprecated_keys"] = sorted(set(deprecated))
        return out

    if command in ENCOUNTER_COMMANDS and legacy_primary:
        body = _encounter_envelope_body(out, legacy_primary=legacy_primary)
        out[canonical] = body
        if legacy_primary != canonical:
            deprecated.append(legacy_primary)
    elif legacy_primary and legacy_primary in out and legacy_primary != canonical:
        out[canonical] = copy.deepcopy(out[legacy_primary])
        deprecated.append(legacy_primary)
    elif canonical not in out or command in {"doctor", "report-encounter"}:
        out[canonical] = {
            key: copy.deepcopy(value)
            for key, value in out.items()
            if key not in META_KEYS and key != canonical
        }

    if deprecated:
        out["deprecated_keys"] = sorted(set(deprecated))

    return out

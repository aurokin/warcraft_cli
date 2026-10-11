"""Source-backed player and talent normalization shared by CLI and composite operations."""

from __future__ import annotations

from typing import Any

from warcraft_core.identity import (
    IdentityConfidence,
    class_spec_identity_payload,
    report_actor_identity_payload,
    talent_transport_packet_payload,
)
from warcraft_core.talent_transport import validate_talent_tree_transport

from warcraftlogs_cli.boss_kills import player_details_roles
from warcraftlogs_cli.report_payloads import report_brief_payload as _report_brief_payload
from warcraftlogs_cli.sampling_utils import dict_at, list_at


def _all_player_detail_rows_from_roles(details: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for role, actors in details.items():
        for actor in actors:
            if isinstance(actor, dict):
                rows.append({"role": role, **actor})
    return rows


def _player_detail_actor(details_payload: dict[str, Any], actor_id: int) -> dict[str, Any] | None:
    player_details = dict_at(details_payload, "player_details")
    roles = dict_at(player_details, "roles")
    return next((row for row in _all_player_detail_rows_from_roles(roles) if row.get("id") == actor_id), None)


def _normalized_talent_tree_rows(actor: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    combatant_info = dict_at(actor, "combatant_info")
    rows = list_at(combatant_info, "talentTree")
    normalized_rows: list[dict[str, Any]] = []
    had_invalid_rows = False
    for row in rows:
        if not isinstance(row, dict):
            had_invalid_rows = True
            continue
        normalized_row = {
            "entry": row.get("id") if isinstance(row.get("id"), int) and not isinstance(row.get("id"), bool) else None,
            "node_id": row.get("nodeID") if isinstance(row.get("nodeID"), int) and not isinstance(row.get("nodeID"), bool) else None,
            "rank": row.get("rank") if isinstance(row.get("rank"), int) and not isinstance(row.get("rank"), bool) else None,
        }
        if all(isinstance(normalized_row.get(key), int) for key in ("entry", "node_id", "rank")):
            normalized_rows.append(normalized_row)
        else:
            had_invalid_rows = True
    return normalized_rows, had_invalid_rows


def _player_talent_transport_identity(actor: dict[str, Any]) -> tuple[str | None, str | None]:
    class_spec_identity = dict_at(actor, "class_spec_identity")
    identity = dict_at(class_spec_identity, "identity")
    actor_class = identity.get("actor_class") if isinstance(identity.get("actor_class"), str) else None
    spec = identity.get("spec") if isinstance(identity.get("spec"), str) else None
    return actor_class, spec


def _player_talent_transport_packet(
    actor: dict[str, Any],
    *,
    report_code: str,
    fight_id: int,
    actor_id: int,
    raw_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    actor_class, spec = _player_talent_transport_identity(actor)
    # Warcraft Logs has no SimulationCraft backend: validation stops at the identity checks or at
    # `simc_backend_unavailable`, and `simc validate-talent-transport` does the real validation.
    validation_result = validate_talent_tree_transport(actor_class=actor_class, spec=spec, talent_tree_rows=raw_rows, backend=None)
    return talent_transport_packet_payload(
        actor_class=actor_class,
        spec=spec,
        confidence="high" if actor_class and spec else "none",
        source="warcraftlogs_talent_tree",
        provider="warcraftlogs",
        source_notes=["raw talents came from combatant_info.talentTree", "one report, one fight, one actor scope"],
        transport_forms=dict_at(validation_result, "transport_forms"),
        raw_evidence={
            "source_contract": "warcraftlogs_combatant_info_talentTree",
            "talent_tree_entries": raw_rows,
        },
        validation=dict_at(validation_result, "validation"),
        scope={
            "type": "report_fight_actor",
            "report_code": report_code,
            "fight_id": fight_id,
            "actor_id": actor_id,
        },
    )


def _fight_identity_confidence(actor: dict[str, Any], *, spec_count: int) -> IdentityConfidence:
    """Class and spec come from the fight itself: one spec is high confidence, several are low."""
    if not isinstance(actor.get("type"), str) or spec_count == 0:
        return "none"
    return "high" if spec_count == 1 else "low"


def _player_detail_actor_payload(actor: dict[str, Any], *, report_code: str | None = None, fight_id: int | None = None) -> dict[str, Any]:
    specs = list_at(actor, "specs")
    normalized_specs = [{"spec": spec.get("spec"), "count": spec.get("count")} for spec in specs if isinstance(spec, dict)]
    return {
        "name": actor.get("name"),
        "id": actor.get("id"),
        "guid": actor.get("guid"),
        "type": actor.get("type"),
        "server": actor.get("server"),
        "region": actor.get("region"),
        "icon": actor.get("icon"),
        "specs": normalized_specs,
        "min_item_level": actor.get("minItemLevel"),
        "max_item_level": actor.get("maxItemLevel"),
        # potionUse/healthstoneUse are left out: Warcraft Logs reports 0 for players whose casts show
        # both (use report-encounter-casts for real counts).
        "combatant_info": actor.get("combatantInfo"),
        # Class and spec come from the fight itself: one spec is a high-confidence identity, several
        # are low-confidence candidates.
        "class_spec_identity": class_spec_identity_payload(
            actor_class=actor.get("type") if isinstance(actor.get("type"), str) else None,
            spec=normalized_specs[0].get("spec") if len(normalized_specs) == 1 else None,
            provider="warcraftlogs",
            source="report_player_details",
            confidence=_fight_identity_confidence(actor, spec_count=len(normalized_specs)),
            candidates=(
                [(actor.get("type") if isinstance(actor.get("type"), str) else None, spec.get("spec")) for spec in normalized_specs]
                if len(normalized_specs) > 1
                else None
            ),
        ),
        "identity_contract": report_actor_identity_payload(
            report_code=report_code,
            fight_id=fight_id,
            actor_id=actor.get("id") if isinstance(actor.get("id"), int) else None,
            name=actor.get("name") if isinstance(actor.get("name"), str) else None,
            actor_class=actor.get("type") if isinstance(actor.get("type"), str) else None,
            spec=normalized_specs[0].get("spec") if len(normalized_specs) == 1 else None,
            provider="warcraftlogs",
            source="report_player_details",
            notes=["canonical only when one report and one fight are both explicit"],
        ),
    }


def _report_player_details_payload(
    report: dict[str, Any], *, report_code: str | None = None, fight_id: int | None = None
) -> dict[str, Any]:
    roles = {
        role: [_player_detail_actor_payload(row, report_code=report_code, fight_id=fight_id) for row in rows]
        for role, rows in player_details_roles(report).items()
    }
    counts = {role: len(rows) for role, rows in roles.items()}
    counts["total"] = sum(counts.values())
    return {
        "report": _report_brief_payload(report),
        "player_details": {
            "counts": counts,
            "roles": roles,
        },
    }

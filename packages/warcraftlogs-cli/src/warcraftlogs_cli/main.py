from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import shlex
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import typer
from simc_cli.talent_transport import validate_talent_tree_transport
from warcraft_core.analytics import numeric_summary
from warcraft_core.auth import (
    delete_provider_auth_state,
    load_provider_auth_state,
    provider_auth_status,
    save_provider_auth_state,
)
from warcraft_core.identity import (
    ability_identity_payload,
    class_spec_identity_payload,
    encounter_identity_payload,
    normalize_actor_class,
    normalize_spec_name,
    report_actor_identity_payload,
    talent_transport_packet_payload,
    validate_talent_transport_packet,
)
from warcraft_core.output import emit
from warcraft_core.paths import provider_state_path
from warcraft_core.wow_normalization import normalize_region

from warcraftlogs_cli.client import (
    RETAIL_PROFILE,
    EncounterRankingsOptions,
    ReportFilterOptions,
    ReportPlayerDetailsOptions,
    ReportRankingsOptions,
    WarcraftLogsClient,
    WarcraftLogsClientError,
    load_warcraftlogs_auth_config,
    warcraftlogs_provider_env_path,
)

app = typer.Typer(add_completion=False, help="Warcraft Logs official API CLI.")
auth_app = typer.Typer(add_completion=False, help="Warcraft Logs authentication helpers.")
app.add_typer(auth_app, name="auth")

FIGHT_ID_OPTION = typer.Option(None, "--fight-id", help="Optional fight ID filter. Repeat as needed.")
REPORT_CODE_PATTERN = re.compile(r"^(?=.*[A-Za-z])(?=.*\d)[A-Za-z0-9]{8,32}$")


@dataclass(slots=True)
class RuntimeConfig:
    pretty: bool = False


@dataclass(frozen=True, slots=True)
class ReportReference:
    code: str
    fight_id: int | None
    source_url: str | None = None


def _cfg(ctx: typer.Context) -> RuntimeConfig:
    obj = ctx.obj
    if isinstance(obj, RuntimeConfig):
        return obj
    return RuntimeConfig()


def _emit(ctx: typer.Context, payload: dict[str, Any], *, err: bool = False, client: Any = None) -> None:
    if client is not None:
        payload = _with_warnings(payload, client)
    emit(payload, pretty=_cfg(ctx).pretty, err=err)


def _with_warnings(payload: dict[str, Any], client: Any) -> dict[str, Any]:
    warnings = list(getattr(client, "last_warnings", []) or [])
    if not warnings:
        return payload
    notes = list(payload.get("notes") or [])
    notes.extend(f"warcraft logs returned partial errors: {w.get('message', '')}" for w in warnings if isinstance(w, dict))
    return {**payload, "notes": notes, "graphql_warnings": warnings}


def _fail(ctx: typer.Context, code: str, message: str, *, status: int = 1) -> None:
    _emit(ctx, {"ok": False, "error": {"code": code, "message": message}}, err=True)
    raise typer.Exit(status)


def _validated_transport_packet(ctx: typer.Context, packet: Any, *, command_name: str) -> dict[str, Any]:
    try:
        return validate_talent_transport_packet(packet)
    except ValueError as exc:
        _fail(ctx, "invalid_transport_packet", f"{command_name} produced an invalid talent transport packet: {exc}")
        raise AssertionError("unreachable") from exc


def _utc_now_z() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _normalize_graphql_enum(value: str | None) -> str | None:
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    if any(sep in text for sep in ("-", "_", " ")):
        parts = [part for part in re.split(r"[-_\s]+", text) if part]
        return "".join(part[:1].upper() + part[1:] for part in parts)
    if text.islower():
        return text[:1].upper() + text[1:]
    return text


def _normalize_hard_mode_level_rank_filter(value: str | None) -> str | None:
    normalized = _normalize_graphql_enum(value)
    if normalized == "NoHardMode":
        return "NormalMode"
    return normalized


_WARCRAFTLOGS_ENCOUNTER_RANKING_CLASS_DISPLAY_NAMES = {
    "deathknight": "Death Knight",
    "demonhunter": "Demon Hunter",
    "druid": "Druid",
    "evoker": "Evoker",
    "hunter": "Hunter",
    "mage": "Mage",
    "monk": "Monk",
    "paladin": "Paladin",
    "priest": "Priest",
    "rogue": "Rogue",
    "shaman": "Shaman",
    "warlock": "Warlock",
    "warrior": "Warrior",
}

_WARCRAFTLOGS_ENCOUNTER_RANKING_SPEC_DISPLAY_NAMES = {
    "affliction": "Affliction",
    "arcane": "Arcane",
    "arms": "Arms",
    "assassination": "Assassination",
    "augmentation": "Augmentation",
    "balance": "Balance",
    "beast_mastery": "Beast Mastery",
    "blood": "Blood",
    "brewmaster": "Brewmaster",
    "destruction": "Destruction",
    "devastation": "Devastation",
    "demonology": "Demonology",
    "discipline": "Discipline",
    "elemental": "Elemental",
    "enhancement": "Enhancement",
    "feral": "Feral",
    "fire": "Fire",
    "frost": "Frost",
    "fury": "Fury",
    "guardian": "Guardian",
    "havoc": "Havoc",
    "holy": "Holy",
    "marksmanship": "Marksmanship",
    "mistweaver": "Mistweaver",
    "outlaw": "Outlaw",
    "preservation": "Preservation",
    "protection": "Protection",
    "restoration": "Restoration",
    "retribution": "Retribution",
    "shadow": "Shadow",
    "subtlety": "Subtlety",
    "survival": "Survival",
    "unholy": "Unholy",
    "vengeance": "Vengeance",
    "windwalker": "Windwalker",
}


def _normalize_encounter_ranking_class_name(value: str | None) -> str | None:
    normalized = normalize_actor_class(value)
    if normalized and normalized in _WARCRAFTLOGS_ENCOUNTER_RANKING_CLASS_DISPLAY_NAMES:
        return _WARCRAFTLOGS_ENCOUNTER_RANKING_CLASS_DISPLAY_NAMES[normalized]
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _normalize_encounter_ranking_spec_name(value: str | None) -> str | None:
    normalized = normalize_spec_name(value)
    if normalized and normalized in _WARCRAFTLOGS_ENCOUNTER_RANKING_SPEC_DISPLAY_NAMES:
        return _WARCRAFTLOGS_ENCOUNTER_RANKING_SPEC_DISPLAY_NAMES[normalized]
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _client(ctx: typer.Context) -> WarcraftLogsClient:
    try:
        return WarcraftLogsClient()
    except Exception as exc:  # noqa: BLE001
        _fail(ctx, "invalid_runtime_config", _runtime_error_message(str(exc)))
        raise AssertionError("unreachable") from exc


def _handle_client_error(ctx: typer.Context, exc: WarcraftLogsClientError) -> None:
    _fail(ctx, exc.code, exc.message)


def _saved_user_token_ready(state: dict[str, Any]) -> bool:
    return bool(
        state.get("has_access_token")
        and state.get("auth_mode") in {"authorization_code", "pkce"}
        and not state.get("expired")
    )


REQUIRED_USER_SCOPE = "view-user-profile"
PRIVATE_REPORTS_SCOPE = "view-private-reports"


def _missing_view_user_profile_warning() -> str:
    return (
        f"Saved token is missing the {REQUIRED_USER_SCOPE!r} scope; "
        "user-endpoint queries (currentUser, user guilds, claimed characters) will fail. "
        f"Re-run `warcraftlogs auth login --scope {REQUIRED_USER_SCOPE} ...`."
    )


def _missing_view_private_reports_warning() -> str:
    return (
        f"Saved token is missing the {PRIVATE_REPORTS_SCOPE!r} scope; "
        "private and guild-stealth reports will return 'permission denied'. "
        f"Re-run `warcraftlogs auth login --scope {REQUIRED_USER_SCOPE} --scope {PRIVATE_REPORTS_SCOPE} ...`."
    )


def _decode_jwt_scopes(access_token: Any) -> list[str]:
    if not isinstance(access_token, str):
        return []
    parts = access_token.split(".")
    if len(parts) < 2:
        return []
    body = parts[1]
    pad = "=" * (-len(body) % 4)
    try:
        decoded = base64.urlsafe_b64decode(body + pad)
        payload = json.loads(decoded)
    except (ValueError, json.JSONDecodeError):
        return []
    if not isinstance(payload, dict):
        return []
    raw = payload.get("scopes")
    if isinstance(raw, list):
        return [str(item) for item in raw if isinstance(item, str) and item.strip()]
    if isinstance(raw, str) and raw.strip():
        return [item for item in raw.replace(",", " ").split() if item]
    return []


def _scope_breakdown(
    *,
    granted_scope: Any,
    requested_scopes: Any,
    access_token: Any = None,
) -> dict[str, Any]:
    granted: list[str] = []
    if isinstance(granted_scope, str) and granted_scope.strip():
        granted = [item for item in granted_scope.replace(",", " ").split() if item]
    elif isinstance(granted_scope, list):
        granted = [str(item) for item in granted_scope if isinstance(item, str) and item.strip()]
    if not granted:
        granted = _decode_jwt_scopes(access_token)
    requested: list[str] = []
    if isinstance(requested_scopes, list):
        requested = [str(item) for item in requested_scopes if isinstance(item, str) and item.strip()]
    has_view_user_profile = REQUIRED_USER_SCOPE in granted
    has_view_private_reports = PRIVATE_REPORTS_SCOPE in granted
    if not has_view_user_profile:
        warning = _missing_view_user_profile_warning()
    elif not has_view_private_reports:
        warning = _missing_view_private_reports_warning()
    else:
        warning = None
    return {
        "granted": granted,
        "requested": requested,
        "has_view_user_profile": has_view_user_profile,
        "has_view_private_reports": has_view_private_reports,
        "warning": warning,
    }


def _saved_user_token_scope_summary() -> dict[str, Any]:
    payload = load_provider_auth_state("warcraftlogs") or {}
    breakdown = _scope_breakdown(
        granted_scope=payload.get("scope"),
        requested_scopes=payload.get("requested_scopes"),
        access_token=payload.get("access_token"),
    )
    return {
        "granted": breakdown["granted"],
        "requested": breakdown["requested"],
        "has_view_user_profile": breakdown["has_view_user_profile"],
        "has_view_private_reports": breakdown["has_view_private_reports"],
    }


def _runtime_error_message(message: str) -> str:
    return message.replace("WOWHEAD_", "WARCRAFTLOGS_")


def _probe_failed_payload(*, mode: str | None, validation: str, probe: str, message: str) -> dict[str, Any]:
    return {
        "ready": False,
        "mode": mode,
        "reason": "probe_failed",
        "message": _runtime_error_message(message),
        "validation": validation,
        "probe": probe,
    }


def _runtime_access_payload() -> dict[str, Any]:
    try:
        client = WarcraftLogsClient()
    except Exception as exc:  # noqa: BLE001
        return {
            "ready": False,
            "reason": "invalid_runtime_config",
            "message": _runtime_error_message(str(exc)),
        }
    client.close()
    return {
        "ready": True,
    }


def _public_api_access_payload(*, auth_configured: bool, runtime_access: dict[str, Any], live: bool) -> dict[str, Any]:
    if not runtime_access["ready"]:
        return {
            "ready": False,
            "mode": None,
            "reason": str(runtime_access["reason"]),
            "message": runtime_access["message"],
            "validation": "local",
        }
    if auth_configured:
        if not live:
            return {
                "ready": True,
                "mode": "client_credentials",
                "validation": "skipped",
                "probe": "rate_limit",
                "live_validated": False,
            }
        client: WarcraftLogsClient | None = None
        try:
            client = WarcraftLogsClient()
            client.probe_live_public_api()
        except WarcraftLogsClientError as exc:
            return {
                "ready": False,
                "mode": "client_credentials",
                "reason": exc.code,
                "message": exc.message,
                "validation": "live",
                "probe": "rate_limit",
            }
        except Exception as exc:  # noqa: BLE001
            return _probe_failed_payload(
                mode="client_credentials",
                validation="live",
                probe="rate_limit",
                message=str(exc),
            )
        finally:
            if client is not None:
                client.close()
        return {
            "ready": True,
            "mode": "client_credentials",
            "validation": "live",
            "probe": "rate_limit",
            "live_validated": True,
        }
    return {
        "ready": False,
        "mode": None,
        "reason": "requires_client_credentials",
        "validation": "local",
    }


def _user_api_access_payload(state: dict[str, Any], *, runtime_access: dict[str, Any], live: bool) -> dict[str, Any]:
    if not runtime_access["ready"]:
        return {
            "ready": False,
            "mode": None,
            "reason": str(runtime_access["reason"]),
            "message": runtime_access["message"],
            "validation": "local",
        }
    if _saved_user_token_ready(state):
        auth_mode = state.get("auth_mode")
        scopes = _saved_user_token_scope_summary()
        scope_warning: str | None
        if not scopes["has_view_user_profile"]:
            scope_warning = _missing_view_user_profile_warning()
        elif not scopes["has_view_private_reports"]:
            scope_warning = _missing_view_private_reports_warning()
        else:
            scope_warning = None
        if not live:
            return {
                "ready": True,
                "mode": auth_mode,
                "validation": "skipped",
                "probe": "current_user",
                "live_validated": False,
                "scopes": scopes,
                "scope_warning": scope_warning,
            }
        client: WarcraftLogsClient | None = None
        try:
            client = WarcraftLogsClient()
            client.probe_live_user_api()
        except WarcraftLogsClientError as exc:
            return {
                "ready": False,
                "mode": auth_mode,
                "reason": exc.code,
                "message": exc.message,
                "validation": "live",
                "probe": "current_user",
                "scopes": scopes,
                "scope_warning": scope_warning,
            }
        except Exception as exc:  # noqa: BLE001
            failure = _probe_failed_payload(
                mode=str(auth_mode) if isinstance(auth_mode, str) else None,
                validation="live",
                probe="current_user",
                message=str(exc),
            )
            failure["scopes"] = scopes
            failure["scope_warning"] = scope_warning
            return failure
        finally:
            if client is not None:
                client.close()
        return {
            "ready": True,
            "mode": auth_mode,
            "validation": "live",
            "probe": "current_user",
            "live_validated": True,
            "scopes": scopes,
            "scope_warning": scope_warning,
        }
    return {
        "ready": False,
        "mode": None,
        "reason": "requires_saved_user_token",
        "validation": "local",
    }


def _user_auth_capability(*, auth_configured: bool, runtime_access: dict[str, Any], user_api_access: dict[str, Any]) -> str:
    if user_api_access["ready"]:
        return "ready"
    reason = str(user_api_access.get("reason") or "")
    if not runtime_access["ready"] or reason == "invalid_runtime_config":
        return "invalid_runtime_config"
    if reason in {"auth_failed", "probe_failed", "skipped_no_live_probe"}:
        return reason
    if auth_configured:
        return "ready_manual_exchange"
    return "requires_client_credentials"


def _grant_statuses(*, auth_configured: bool, runtime_access: dict[str, Any]) -> dict[str, str]:
    if not runtime_access["ready"]:
        status = str(runtime_access["reason"])
        return {
            "client_credentials": status,
            "authorization_code": status,
            "pkce": status,
        }
    if auth_configured:
        return {
            "client_credentials": "ready",
            "authorization_code": "ready_manual_exchange",
            "pkce": "ready_manual_exchange",
        }
    return {
        "client_credentials": "requires_client_credentials",
        "authorization_code": "requires_client_credentials",
        "pkce": "requires_client_credentials",
    }


def _capability_status(*, ready: bool, reason: str) -> str:
    return "ready" if ready else reason


def _public_capability_status(public_api_access: dict[str, Any]) -> str:
    return _capability_status(
        ready=bool(public_api_access["ready"]),
        reason=str(public_api_access.get("reason") or "requires_client_credentials"),
    )


def _doctor_payload(*, live: bool) -> dict[str, Any]:
    auth = load_warcraftlogs_auth_config()
    credential_source = auth.env_file if auth.env_file is not None else ("environment" if auth.configured else None)
    state = provider_auth_status("warcraftlogs")
    runtime_access = _runtime_access_payload()
    public_api_access = _public_api_access_payload(
        auth_configured=auth.configured,
        runtime_access=runtime_access,
        live=live,
    )
    user_api_access = _user_api_access_payload(
        state,
        runtime_access=runtime_access,
        live=live,
    )
    return {
        "ok": True,
        "provider": "warcraftlogs",
        "status": "ready",
        "site_profile": {
            "key": RETAIL_PROFILE.key,
            "label": RETAIL_PROFILE.label,
            "root_url": RETAIL_PROFILE.root_url,
            "api_url": RETAIL_PROFILE.api_url,
        },
        "auth": {
            "required": True,
            "configured": auth.configured,
            "client_credentials_configured": auth.configured,
            "flow": "oauth_client_credentials",
            "active_mode": (
                state.get("auth_mode")
                if state.get("has_access_token") and isinstance(state.get("auth_mode"), str)
                else "client_credentials"
            ),
            "endpoint_family": (
                "user"
                if state.get("has_access_token") and state.get("auth_mode") in {"authorization_code", "pkce"}
                else "client"
            ),
            "credential_source": credential_source,
            "lookup_order": [".env.local", warcraftlogs_provider_env_path(), "environment"],
            "state": state,
            "state_path": str(provider_state_path("warcraftlogs")),
            "redirect_flow_deferred": True,
            "runtime_access": runtime_access,
            "public_api_access": public_api_access,
            "user_api_access": user_api_access,
        },
        "capabilities": {
            "doctor": "ready",
            "search": "ready_explicit_report_only",
            "resolve": "ready_explicit_report_only",
            "rate_limit": _public_capability_status(public_api_access),
            "regions": _public_capability_status(public_api_access),
            "expansions": _public_capability_status(public_api_access),
            "server": _public_capability_status(public_api_access),
            "zone": _public_capability_status(public_api_access),
            "zones": _public_capability_status(public_api_access),
            "encounter": _public_capability_status(public_api_access),
            "guild": _public_capability_status(public_api_access),
            "guild_members": _public_capability_status(public_api_access),
            "guild_attendance": _public_capability_status(public_api_access),
            "guild_rankings": _public_capability_status(public_api_access),
            "boss_kills": _public_capability_status(public_api_access),
            "top_kills": _public_capability_status(public_api_access),
            "kill_time_distribution": _public_capability_status(public_api_access),
            "boss_spec_usage": _public_capability_status(public_api_access),
            "comp_samples": _public_capability_status(public_api_access),
            "ability_usage_summary": _public_capability_status(public_api_access),
            "report_encounter": _public_capability_status(public_api_access),
            "report_encounter_players": _public_capability_status(public_api_access),
            "report_encounter_casts": _public_capability_status(public_api_access),
            "report_encounter_buffs": _public_capability_status(public_api_access),
            "report_encounter_aura_summary": _public_capability_status(public_api_access),
            "report_encounter_aura_compare": _public_capability_status(public_api_access),
            "report_encounter_damage_source_summary": _public_capability_status(public_api_access),
            "report_encounter_damage_target_summary": _public_capability_status(public_api_access),
            "report_encounter_damage_breakdown": _public_capability_status(public_api_access),
            "character": _public_capability_status(public_api_access),
            "character_rankings": _public_capability_status(public_api_access),
            "report": _public_capability_status(public_api_access),
            "reports": _public_capability_status(public_api_access),
            "report_fights": _public_capability_status(public_api_access),
            "report_events": _public_capability_status(public_api_access),
            "report_table": _public_capability_status(public_api_access),
            "report_graph": _public_capability_status(public_api_access),
            "report_master_data": _public_capability_status(public_api_access),
            "report_player_details": _public_capability_status(public_api_access),
            "report_rankings": _public_capability_status(public_api_access),
            "user_auth": _user_auth_capability(
                auth_configured=auth.configured,
                runtime_access=runtime_access,
                user_api_access=user_api_access,
            ),
        },
    }


def _region_payload(region: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": region.get("id"),
        "name": region.get("name"),
        "compact_name": region.get("compactName"),
        "slug": normalize_region(str(region.get("slug", ""))),
    }


def _server_payload(server: dict[str, Any]) -> dict[str, Any]:
    region = server.get("region") if isinstance(server.get("region"), dict) else {}
    subregion = server.get("subregion") if isinstance(server.get("subregion"), dict) else {}
    return {
        "id": server.get("id"),
        "name": server.get("name"),
        "normalized_name": server.get("normalizedName"),
        "slug": server.get("slug"),
        "region": _region_payload(region) if region else None,
        "subregion": {
            "id": subregion.get("id"),
            "name": subregion.get("name"),
        }
        if subregion
        else None,
        "connected_realm_id": server.get("connectedRealmID"),
        "season_id": server.get("seasonID"),
    }


def _zone_payload(zone: dict[str, Any]) -> dict[str, Any]:
    expansion = zone.get("expansion") if isinstance(zone.get("expansion"), dict) else {}
    difficulties = zone.get("difficulties")
    encounters = zone.get("encounters")
    return {
        "id": zone.get("id"),
        "name": zone.get("name"),
        "frozen": zone.get("frozen"),
        "expansion": {
            "id": expansion.get("id"),
            "name": expansion.get("name"),
        }
        if expansion
        else None,
        "difficulties": [
            {"id": difficulty.get("id"), "name": difficulty.get("name"), "sizes": difficulty.get("sizes", [])}
            for difficulty in difficulties
            if isinstance(difficulty, dict)
        ]
        if isinstance(difficulties, list)
        else [],
        "encounters": [
            {"id": encounter.get("id"), "name": encounter.get("name"), "journal_id": encounter.get("journalID")}
            for encounter in encounters
            if isinstance(encounter, dict)
        ]
        if isinstance(encounters, list)
        else [],
        "partitions": [
            {
                "id": partition.get("id"),
                "name": partition.get("name"),
                "compact_name": partition.get("compactName"),
                "default": partition.get("default"),
            }
            for partition in (zone.get("partitions") if isinstance(zone.get("partitions"), list) else [])
            if isinstance(partition, dict)
        ],
    }


def _encounter_payload(encounter: dict[str, Any]) -> dict[str, Any]:
    zone = encounter.get("zone") if isinstance(encounter.get("zone"), dict) else {}
    expansion = zone.get("expansion") if isinstance(zone.get("expansion"), dict) else {}
    return {
        "id": encounter.get("id"),
        "name": encounter.get("name"),
        "journal_id": encounter.get("journalID"),
        "zone": {
            "id": zone.get("id"),
            "name": zone.get("name"),
            "expansion": {"id": expansion.get("id"), "name": expansion.get("name")} if expansion else None,
        }
        if zone
        else None,
    }


def _expansion_payload(expansion: dict[str, Any]) -> dict[str, Any]:
    zones = expansion.get("zones")
    zone_rows = [zone for zone in zones if isinstance(zone, dict)] if isinstance(zones, list) else []
    return {
        "id": expansion.get("id"),
        "name": expansion.get("name"),
        "zone_count": len(zone_rows),
        "zones": [
            {
                "id": zone.get("id"),
                "name": zone.get("name"),
                "frozen": zone.get("frozen"),
            }
            for zone in zone_rows
        ],
    }


def _rank_payload(rank: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(rank, dict):
        return None
    return {
        "number": rank.get("number"),
        "color": rank.get("color"),
        "percentile": rank.get("percentile"),
    }


def _guild_payload(guild: dict[str, Any]) -> dict[str, Any]:
    faction = guild.get("faction") if isinstance(guild.get("faction"), dict) else {}
    server = guild.get("server") if isinstance(guild.get("server"), dict) else {}
    zone_ranking = guild.get("zoneRanking") if isinstance(guild.get("zoneRanking"), dict) else {}
    progress = zone_ranking.get("progress") if isinstance(zone_ranking.get("progress"), dict) else {}
    tags = guild.get("tags")
    return {
        "id": guild.get("id"),
        "name": guild.get("name"),
        "description": guild.get("description"),
        "competition_mode": guild.get("competitionMode"),
        "stealth_mode": guild.get("stealthMode"),
        "tags": [
            {"id": tag.get("id"), "name": tag.get("name")}
            for tag in tags
            if isinstance(tag, dict)
        ]
        if isinstance(tags, list)
        else [],
        "faction": {
            "id": faction.get("id"),
            "name": faction.get("name"),
        }
        if faction
        else None,
        "server": _server_payload(server) if server else None,
        "zone_ranking": {
            "progress": {
                "world": _rank_payload(progress.get("worldRank")),
                "region": _rank_payload(progress.get("regionRank")),
                "server": _rank_payload(progress.get("serverRank")),
            }
        }
        if zone_ranking
        else None,
    }


def _rank_positions_payload(positions: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(positions, dict):
        return None
    return {
        "world": _rank_payload(positions.get("worldRank")),
        "region": _rank_payload(positions.get("regionRank")),
        "server": _rank_payload(positions.get("serverRank")),
    }


def _guild_rankings_payload(guild: dict[str, Any]) -> dict[str, Any]:
    server = guild.get("server") if isinstance(guild.get("server"), dict) else {}
    zone_ranking = guild.get("zoneRanking") if isinstance(guild.get("zoneRanking"), dict) else {}
    return {
        "id": guild.get("id"),
        "name": guild.get("name"),
        "server": _server_payload(server) if server else None,
        "zone_ranking": {
            "progress": _rank_positions_payload(zone_ranking.get("progress")),
            "speed": _rank_positions_payload(zone_ranking.get("speed")),
            "complete_raid_speed": _rank_positions_payload(zone_ranking.get("completeRaidSpeed")),
        }
        if zone_ranking
        else None,
    }


def _pagination_payload(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return {
        "total": value.get("total"),
        "per_page": value.get("per_page"),
        "current_page": value.get("current_page"),
        "from": value.get("from"),
        "to": value.get("to"),
        "last_page": value.get("last_page"),
        "has_more_pages": value.get("has_more_pages"),
    }


def _guild_member_payload(character: dict[str, Any]) -> dict[str, Any]:
    faction = character.get("faction") if isinstance(character.get("faction"), dict) else {}
    server = character.get("server") if isinstance(character.get("server"), dict) else {}
    return {
        "id": character.get("id"),
        "canonical_id": character.get("canonicalID"),
        "name": character.get("name"),
        "level": character.get("level"),
        "class_id": character.get("classID"),
        "hidden": character.get("hidden"),
        "guild_rank": character.get("guildRank"),
        "faction": {"id": faction.get("id"), "name": faction.get("name")} if faction else None,
        "server": _server_payload(server) if server else None,
    }


def _guild_members_payload(guild: dict[str, Any]) -> dict[str, Any]:
    server = guild.get("server") if isinstance(guild.get("server"), dict) else {}
    members = guild.get("members") if isinstance(guild.get("members"), dict) else {}
    rows = [row for row in (members.get("data") if isinstance(members.get("data"), list) else []) if isinstance(row, dict)]
    return {
        "id": guild.get("id"),
        "name": guild.get("name"),
        "server": _server_payload(server) if server else None,
        "pagination": _pagination_payload(members),
        "count": len(rows),
        "members": [_guild_member_payload(row) for row in rows],
    }


def _presence_label(value: int | None) -> str | None:
    if value == 1:
        return "present"
    if value == 2:
        return "benched"
    return None


def _attendance_player_payload(player: dict[str, Any]) -> dict[str, Any]:
    presence = player.get("presence")
    return {
        "name": player.get("name"),
        "type": player.get("type"),
        "presence": presence,
        "presence_label": _presence_label(presence if isinstance(presence, int) else None),
    }


def _guild_attendance_payload(guild: dict[str, Any]) -> dict[str, Any]:
    server = guild.get("server") if isinstance(guild.get("server"), dict) else {}
    attendance = guild.get("attendance") if isinstance(guild.get("attendance"), dict) else {}
    rows = [row for row in (attendance.get("data") if isinstance(attendance.get("data"), list) else []) if isinstance(row, dict)]
    attendance_rows = []
    for row in rows:
        zone = row.get("zone") if isinstance(row.get("zone"), dict) else {}
        players = [player for player in (row.get("players") if isinstance(row.get("players"), list) else []) if isinstance(player, dict)]
        attendance_rows.append(
            {
                "code": row.get("code"),
                "start_time": row.get("startTime"),
                "zone": {
                    "id": zone.get("id"),
                    "name": zone.get("name"),
                    "frozen": zone.get("frozen"),
                }
                if zone
                else None,
                "player_count": len(players),
                "players": [_attendance_player_payload(player) for player in players],
            }
        )
    return {
        "id": guild.get("id"),
        "name": guild.get("name"),
        "server": _server_payload(server) if server else None,
        "pagination": _pagination_payload(attendance),
        "count": len(attendance_rows),
        "attendance": attendance_rows,
    }


def _archive_status_payload(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return {
        "is_archived": value.get("isArchived"),
        "is_accessible": value.get("isAccessible"),
        "archive_date": value.get("archiveDate"),
    }


def _character_payload(character: dict[str, Any]) -> dict[str, Any]:
    faction = character.get("faction") if isinstance(character.get("faction"), dict) else {}
    server = character.get("server") if isinstance(character.get("server"), dict) else {}
    guilds = character.get("guilds")
    normalized_guilds: list[dict[str, Any]] = []
    if isinstance(guilds, list):
        for guild in guilds:
            if not isinstance(guild, dict):
                continue
            normalized_guilds.append(
                {
                    "id": guild.get("id"),
                    "name": guild.get("name"),
                    "server": _server_payload(guild.get("server")) if isinstance(guild.get("server"), dict) else None,
                }
            )
    return {
        "id": character.get("id"),
        "canonical_id": character.get("canonicalID"),
        "name": character.get("name"),
        "level": character.get("level"),
        "class_id": character.get("classID"),
        "hidden": character.get("hidden"),
        "server": _server_payload(server) if server else None,
        "guild_rank": character.get("guildRank"),
        "faction": {"id": faction.get("id"), "name": faction.get("name")} if faction else None,
        "guilds": normalized_guilds,
    }


def _report_payload(report: dict[str, Any]) -> dict[str, Any]:
    zone = report.get("zone") if isinstance(report.get("zone"), dict) else {}
    guild = report.get("guild") if isinstance(report.get("guild"), dict) else {}
    return {
        "code": report.get("code"),
        "title": report.get("title"),
        "start_time": report.get("startTime"),
        "end_time": report.get("endTime"),
        "visibility": report.get("visibility"),
        "archive_status": _archive_status_payload(report.get("archiveStatus")),
        "segments": report.get("segments"),
        "exported_segments": report.get("exportedSegments"),
        "zone": {"id": zone.get("id"), "name": zone.get("name")} if zone else None,
        "guild": {
            "id": guild.get("id"),
            "name": guild.get("name"),
            "server": _server_payload(guild.get("server")) if isinstance(guild.get("server"), dict) else None,
        }
        if guild
        else None,
    }


def _report_brief_payload(report: dict[str, Any]) -> dict[str, Any]:
    zone = report.get("zone") if isinstance(report.get("zone"), dict) else {}
    return {
        "code": report.get("code"),
        "title": report.get("title"),
        "zone": {"id": zone.get("id"), "name": zone.get("name")} if zone else None,
    }


def _report_url(code: str | None, *, fight_id: int | None = None) -> str | None:
    if not isinstance(code, str) or not code.strip():
        return None
    base = f"https://www.warcraftlogs.com/reports/{code}"
    if isinstance(fight_id, int):
        return f"{base}#fight={fight_id}"
    return base


def _report_discovery_hint(query: str) -> dict[str, Any]:
    return {
        "provider": "warcraftlogs",
        "query": query,
        "search_query": query,
        "count": 0,
        "results": [],
        "resolved": False,
        "confidence": "none",
        "match": None,
        "next_command": None,
        "fallback_search_command": None,
        "message": (
            "Warcraft Logs discovery is intentionally narrow for now. "
            "Use an explicit report URL or a bare report code."
        ),
        "supported_inputs": [
            "https://www.warcraftlogs.com/reports/<code>#fight=<id>",
            "<report_code>",
        ],
        "suggested_commands": [
            "warcraftlogs report <report_code>",
            "warcraftlogs report-encounter <report_code> --fight-id <id>",
        ],
    }


def _fight_payload(fight: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": fight.get("id"),
        "name": fight.get("name"),
        "encounter_id": fight.get("encounterID"),
        "difficulty": fight.get("difficulty"),
        "kill": fight.get("kill"),
        "complete_raid": fight.get("completeRaid"),
        "start_time": fight.get("startTime"),
        "end_time": fight.get("endTime"),
        "fight_percentage": fight.get("fightPercentage"),
        "boss_percentage": fight.get("bossPercentage"),
        "average_item_level": fight.get("averageItemLevel"),
        "size": fight.get("size"),
    }


def _parse_report_reference(reference: str, *, explicit_fight_id: int | None) -> ReportReference:
    text = reference.strip()
    if not text:
        raise ValueError("Report reference is required.")
    source_url: str | None = None
    code = text
    parsed = urlparse(text)
    parsed_fight_id: int | None = None
    if parsed.scheme and parsed.netloc:
        source_url = text
        parts = [part for part in parsed.path.strip("/").split("/") if part]
        try:
            reports_index = parts.index("reports")
            code = parts[reports_index + 1]
        except (ValueError, IndexError):
            raise ValueError("Could not extract a Warcraft Logs report code from the provided URL.") from None
        fragments = parse_qs(parsed.fragment)
        fight_values = fragments.get("fight") or []
        if fight_values:
            try:
                parsed_fight_id = int(fight_values[0])
            except ValueError:
                parsed_fight_id = None
    fight_id = explicit_fight_id if explicit_fight_id is not None else parsed_fight_id
    return ReportReference(code=code, fight_id=fight_id, source_url=source_url)


def _explicit_report_reference(query: str) -> ReportReference | None:
    text = query.strip()
    if not text:
        return None
    if " " in text and not text.startswith("http://") and not text.startswith("https://"):
        return None
    try:
        ref = _parse_report_reference(text, explicit_fight_id=None)
    except ValueError:
        return None
    if not REPORT_CODE_PATTERN.fullmatch(ref.code):
        return None
    return ref


def _report_discovery_candidate(ref: ReportReference) -> dict[str, Any]:
    quoted_reference = shlex.quote(ref.code)
    if ref.fight_id is None:
        kind = "report"
        next_command = f"warcraftlogs report {quoted_reference}"
        score = 92
        reasons = ["explicit_report_reference", "report_code"]
    else:
        kind = "report_encounter"
        next_command = f"warcraftlogs report-encounter {quoted_reference} --fight-id {ref.fight_id}"
        score = 96
        reasons = ["explicit_report_reference", "fight_scope_present"]
    return {
        "provider": "warcraftlogs",
        "kind": kind,
        "id": f"warcraftlogs:{kind}:{ref.code}:{ref.fight_id or ''}",
        "name": f"Warcraft Logs report {ref.code}",
        "report_reference": _report_reference_payload(ref),
        "ranking": {"score": score, "match_reasons": reasons},
        "follow_up": {
            "provider": "warcraftlogs",
            "kind": kind,
            "surface": kind,
            "command": next_command,
        },
    }


def _report_search_payload(query: str, *, ref: ReportReference | None) -> dict[str, Any]:
    if ref is None:
        return _report_discovery_hint(query)
    candidate = _report_discovery_candidate(ref)
    return {
        "provider": "warcraftlogs",
        "query": query,
        "search_query": query,
        "count": 1,
        "results": [candidate],
        "truncated": False,
        "discovery_scope": "explicit_report_reference",
        "message": "Matched an explicit Warcraft Logs report reference.",
    }


def _report_resolve_payload(query: str, *, ref: ReportReference | None) -> dict[str, Any]:
    if ref is None:
        hint = _report_discovery_hint(query)
        return {
            "provider": "warcraftlogs",
            "query": query,
            "search_query": query,
            "resolved": False,
            "confidence": "none",
            "match": None,
            "next_command": None,
            "fallback_search_command": None,
            "message": hint["message"],
            "supported_inputs": hint["supported_inputs"],
            "suggested_commands": hint["suggested_commands"],
        }
    candidate = _report_discovery_candidate(ref)
    follow_up = candidate["follow_up"]
    return {
        "provider": "warcraftlogs",
        "query": query,
        "search_query": query,
        "resolved": True,
        "confidence": "high" if ref.fight_id is not None or ref.source_url is not None else "medium",
        "match": candidate,
        "next_command": follow_up["command"],
        "fallback_search_command": None,
    }


def _kill_type_for_fight(fight: dict[str, Any]) -> str:
    return "Kills" if fight.get("kill") else "Wipes"


def _resolve_encounter_scope(
    ctx: typer.Context,
    *,
    client: WarcraftLogsClient,
    reference: str,
    fight_id: int | None,
    allow_unlisted: bool,
) -> tuple[ReportReference, dict[str, Any], dict[str, Any], dict[str, Any] | None]:
    try:
        ref = _parse_report_reference(reference, explicit_fight_id=fight_id)
    except ValueError as exc:
        _fail(ctx, "invalid_query", str(exc))
        raise AssertionError("unreachable") from exc
    report = client.report(code=ref.code, allow_unlisted=allow_unlisted)
    fights_report = client.report_fights(code=ref.code, difficulty=None, allow_unlisted=allow_unlisted)
    fights = fights_report.get("fights") if isinstance(fights_report.get("fights"), list) else []
    fight_rows = [row for row in fights if isinstance(row, dict)]
    selected: dict[str, Any] | None = None
    if ref.fight_id is not None:
        selected = next((row for row in fight_rows if row.get("id") == ref.fight_id), None)
        if selected is None:
            _fail(ctx, "not_found", f"Fight {ref.fight_id} was not found in report {ref.code!r}.")
    elif len(fight_rows) == 1:
        selected = fight_rows[0]
    else:
        _fail(ctx, "missing_scope", "Provide --fight-id or a report URL with a numeric #fight=... fragment for encounter-scoped analysis.")
    encounter_id = selected.get("encounterID")
    encounter = None
    if isinstance(encounter_id, int):
        try:
            encounter = client.encounter(encounter_id=encounter_id)
        except WarcraftLogsClientError:
            encounter = None
    return ref, report, selected, encounter


def _report_reference_payload(ref: ReportReference) -> dict[str, Any]:
    return {"code": ref.code, "fight_id": ref.fight_id, "source_url": ref.source_url}


def _encounter_summary_payload(*, ref: ReportReference, report: dict[str, Any], fight: dict[str, Any], encounter: dict[str, Any] | None) -> dict[str, Any]:
    encounter_payload = None
    encounter_identity = encounter_identity_payload(
        encounter_id=fight.get("encounterID") if isinstance(fight.get("encounterID"), int) else None,
        name=fight.get("name") if isinstance(fight.get("name"), str) else None,
        provider="warcraftlogs",
        source="report_encounter",
        notes=["canonical only within explicit encounter metadata returned by Warcraft Logs"],
    )
    if isinstance(encounter, dict):
        zone = encounter.get("zone") if isinstance(encounter.get("zone"), dict) else {}
        encounter_identity = encounter_identity_payload(
            encounter_id=encounter.get("id") if isinstance(encounter.get("id"), int) else None,
            journal_id=encounter.get("journalID") if isinstance(encounter.get("journalID"), int) else None,
            name=encounter.get("name") if isinstance(encounter.get("name"), str) else None,
            zone_id=zone.get("id") if isinstance(zone.get("id"), int) else None,
            provider="warcraftlogs",
            source="report_encounter",
        )
        encounter_payload = _encounter_payload(encounter)
    return {
        "reference": _report_reference_payload(ref),
        "report": _report_payload(report),
        "fight": _fight_payload(fight),
        "encounter": encounter_payload,
        "encounter_identity": encounter_identity,
        "stability": {
            "report_finished": _report_is_finished(report),
            "cache_safe": _report_is_finished(report),
            "live": not _report_is_finished(report),
        },
    }


def _encounter_window_bounds(
    ctx: typer.Context,
    *,
    fight: dict[str, Any],
    window_start_ms: float | None,
    window_end_ms: float | None,
) -> tuple[float | None, float | None]:
    fight_start = fight.get("startTime")
    if (window_start_ms is not None or window_end_ms is not None) and not isinstance(fight_start, (int, float)):
        _fail(ctx, "invalid_response", "Selected fight did not include a start timestamp for encounter windowing.")
    absolute_start = float(fight_start) + float(window_start_ms) if window_start_ms is not None and isinstance(fight_start, (int, float)) else None
    absolute_end = float(fight_start) + float(window_end_ms) if window_end_ms is not None and isinstance(fight_start, (int, float)) else None
    if absolute_start is not None and absolute_end is not None and absolute_end < absolute_start:
        _fail(ctx, "invalid_query", "--window-end-ms must be greater than or equal to --window-start-ms.")
    return absolute_start, absolute_end


def _encounter_filter_options(
    ctx: typer.Context,
    *,
    fight: dict[str, Any],
    ability_id: float | None,
    data_type: str,
    source_id: int | None,
    target_id: int | None,
    hostility_type: str | None,
    translate: bool | None,
    view_by: str | None = None,
    limit: int | None = None,
    wipe_cutoff: int | None = None,
    window_start_ms: float | None = None,
    window_end_ms: float | None = None,
) -> tuple[ReportFilterOptions, dict[str, Any]]:
    start_time, end_time = _encounter_window_bounds(
        ctx,
        fight=fight,
        window_start_ms=window_start_ms,
        window_end_ms=window_end_ms,
    )
    encounter_id = fight.get("encounterID") if isinstance(fight.get("encounterID"), int) else None
    fight_ids = [int(fight["id"])] if isinstance(fight.get("id"), int) else None
    options = ReportFilterOptions(
        ability_id=ability_id,
        data_type=data_type,
        encounter_id=encounter_id,
        end_time=end_time,
        fight_ids=fight_ids,
        hostility_type=hostility_type,
        kill_type=_kill_type_for_fight(fight),
        limit=limit,
        source_id=source_id,
        start_time=start_time,
        target_id=target_id,
        translate=translate,
        view_by=view_by,
        wipe_cutoff=wipe_cutoff,
    )
    query = {
        "ability_id": ability_id,
        "data_type": data_type,
        "encounter_id": encounter_id,
        "fight_ids": fight_ids,
        "hostility_type": hostility_type,
        "kill_type": _kill_type_for_fight(fight),
        "limit": limit,
        "source_id": source_id,
        "target_id": target_id,
        "translate": translate,
        "view_by": view_by,
        "wipe_cutoff": wipe_cutoff,
        "window_start_ms": window_start_ms,
        "window_end_ms": window_end_ms,
        "start_time": start_time,
        "end_time": end_time,
    }
    return options, query


def _require_explicit_window(ctx: typer.Context, *, name: str, start_ms: float | None, end_ms: float | None) -> None:
    if start_ms is None or end_ms is None:
        _fail(ctx, "invalid_query", f"{name} requires both a start and end window offset in milliseconds.")


def _master_data_indexes(report: dict[str, Any]) -> tuple[dict[int, dict[str, Any]], dict[int, dict[str, Any]]]:
    payload = _report_master_data_payload(report)["master_data"]
    actor_index = {
        int(row["id"]): row
        for row in payload["actors"]
        if isinstance(row, dict) and isinstance(row.get("id"), int)
    }
    ability_index = {
        int(row["game_id"]): row
        for row in payload["abilities"]
        if isinstance(row, dict) and isinstance(row.get("game_id"), int)
    }
    return actor_index, ability_index


def _named_actor(
    actor_index: dict[int, dict[str, Any]],
    actor_id: int | None,
    *,
    report_code: str | None = None,
    fight_id: int | None = None,
    source: str,
) -> dict[str, Any] | None:
    if actor_id is None:
        return None
    actor = actor_index.get(actor_id)
    if not isinstance(actor, dict):
        return {
            "id": actor_id,
            "name": f"actor:{actor_id}",
            "identity_contract": report_actor_identity_payload(
                report_code=report_code,
                fight_id=fight_id,
                actor_id=actor_id,
                name=f"actor:{actor_id}",
                provider="warcraftlogs",
                source=source,
                notes=["actor id was present, but no master-data actor row was available"],
            ),
        }
    actor_name = actor.get("name") if isinstance(actor.get("name"), str) else None
    actor_sub_type = actor.get("sub_type")
    return {
        "id": actor_id,
        "name": actor_name,
        "type": actor.get("type"),
        "sub_type": actor_sub_type,
        "identity_contract": report_actor_identity_payload(
            report_code=report_code,
            fight_id=fight_id,
            actor_id=actor_id,
            name=actor_name,
            actor_class=actor_sub_type if actor.get("type") == "Player" else None,
            provider="warcraftlogs",
            source=source,
        ),
    }


def _named_ability(ability_index: dict[int, dict[str, Any]], ability_id: int | None, *, source: str) -> dict[str, Any] | None:
    if ability_id is None:
        return None
    ability = ability_index.get(ability_id)
    if not isinstance(ability, dict):
        return {
            "game_id": ability_id,
            "name": f"ability:{ability_id}",
            "identity_contract": ability_identity_payload(
                game_id=ability_id,
                name=f"ability:{ability_id}",
                provider="warcraftlogs",
                source=source,
                notes=["ability id was present, but no master-data ability row was available"],
            ),
        }
    ability_name = ability.get("name") if isinstance(ability.get("name"), str) else None
    return {
        "game_id": ability_id,
        "name": ability_name,
        "type": ability.get("type"),
        "icon": ability.get("icon"),
        "identity_contract": ability_identity_payload(
            game_id=ability_id,
            name=ability_name,
            provider="warcraftlogs",
            source=source,
        ),
    }


def _event_id(value: Any) -> int | None:
    return int(value) if isinstance(value, (int, float)) else None


def _encounter_cast_rows_payload(*, report: dict[str, Any], fight: dict[str, Any], events_report: dict[str, Any], master_report: dict[str, Any], preview_limit: int) -> dict[str, Any]:
    actor_index, ability_index = _master_data_indexes(master_report)
    paginator = events_report.get("events") if isinstance(events_report.get("events"), dict) else {}
    rows = paginator.get("data") if isinstance(paginator.get("data"), list) else []
    cast_rows = [row for row in rows if isinstance(row, dict)]
    by_source: dict[tuple[int | None, str | None], int] = {}
    by_target: dict[tuple[int | None, str | None], int] = {}
    by_ability: dict[tuple[int | None, str | None], int] = {}
    by_source_ability: dict[tuple[int | None, int | None], int] = {}
    by_source_target: dict[tuple[int | None, int | None], int] = {}
    preview: list[dict[str, Any]] = []
    fight_start = fight.get("startTime") if isinstance(fight.get("startTime"), (int, float)) else None
    report_code = report.get("code") if isinstance(report.get("code"), str) else None
    selected_fight_id = fight.get("id") if isinstance(fight.get("id"), int) else None

    for row in cast_rows:
        source_id = _event_id(row.get("sourceID"))
        target_id = _event_id(row.get("targetID"))
        ability_id = _event_id(row.get("abilityGameID"))
        source = _named_actor(
            actor_index,
            source_id,
            report_code=report_code,
            fight_id=selected_fight_id,
            source="report_encounter_casts",
        )
        target = _named_actor(
            actor_index,
            target_id,
            report_code=report_code,
            fight_id=selected_fight_id,
            source="report_encounter_casts",
        )
        ability = _named_ability(ability_index, ability_id, source="report_encounter_casts")
        source_key = (source_id, str(source.get("name") if source else None))
        target_key = (target_id, str(target.get("name") if target else None))
        ability_key = (ability_id, str(ability.get("name") if ability else None))
        by_source[source_key] = by_source.get(source_key, 0) + 1
        by_target[target_key] = by_target.get(target_key, 0) + 1
        by_ability[ability_key] = by_ability.get(ability_key, 0) + 1
        by_source_ability[(source_id, ability_id)] = by_source_ability.get((source_id, ability_id), 0) + 1
        by_source_target[(source_id, target_id)] = by_source_target.get((source_id, target_id), 0) + 1
        if len(preview) < preview_limit:
            timestamp = row.get("timestamp")
            relative_ms = None
            if isinstance(timestamp, (int, float)) and isinstance(fight_start, (int, float)):
                relative_ms = float(timestamp) - float(fight_start)
            preview.append(
                {
                    "timestamp": timestamp,
                    "relative_time_ms": relative_ms,
                    "source": source,
                    "target": target,
                    "ability": ability,
                    "type": row.get("type"),
                }
            )

    def _sorted_rows(counts: dict[tuple[Any, Any], int], *, field: str) -> list[dict[str, Any]]:
        rows_out: list[dict[str, Any]] = []
        for (numeric_id, name), count in sorted(counts.items(), key=lambda item: (-item[1], str(item[0][1] or ""))):
            base = {"count": count}
            if field == "source":
                base["source"] = _named_actor(
                    actor_index,
                    numeric_id if isinstance(numeric_id, int) else None,
                    report_code=report_code,
                    fight_id=selected_fight_id,
                    source="report_encounter_casts",
                ) or {"id": numeric_id, "name": name}
            elif field == "target":
                base["target"] = _named_actor(
                    actor_index,
                    numeric_id if isinstance(numeric_id, int) else None,
                    report_code=report_code,
                    fight_id=selected_fight_id,
                    source="report_encounter_casts",
                ) or {"id": numeric_id, "name": name}
            else:
                base["ability"] = _named_ability(
                    ability_index,
                    numeric_id if isinstance(numeric_id, int) else None,
                    source="report_encounter_casts",
                ) or {"game_id": numeric_id, "name": name}
            rows_out.append(base)
        return rows_out

    combo_rows = []
    for (source_id, ability_id), count in sorted(by_source_ability.items(), key=lambda item: (-item[1], item[0])):
        combo_rows.append(
            {
                "count": count,
                "source": _named_actor(
                    actor_index,
                    source_id,
                    report_code=report_code,
                    fight_id=selected_fight_id,
                    source="report_encounter_casts",
                ),
                "ability": _named_ability(ability_index, ability_id, source="report_encounter_casts"),
            }
        )

    source_target_rows = []
    for (source_id, target_id), count in sorted(by_source_target.items(), key=lambda item: (-item[1], item[0])):
        source_target_rows.append(
            {
                "count": count,
                "source": _named_actor(
                    actor_index,
                    source_id,
                    report_code=report_code,
                    fight_id=selected_fight_id,
                    source="report_encounter_casts",
                ),
                "target": _named_actor(
                    actor_index,
                    target_id,
                    report_code=report_code,
                    fight_id=selected_fight_id,
                    source="report_encounter_casts",
                ),
            }
        )

    return {
        "report": _report_brief_payload(report),
        "fight": _fight_payload(fight),
        "casts": {
            "event_count": len(cast_rows),
            "next_page_timestamp": paginator.get("nextPageTimestamp"),
            "by_source": _sorted_rows(by_source, field="source"),
            "by_target": _sorted_rows(by_target, field="target"),
            "by_ability": _sorted_rows(by_ability, field="ability"),
            "by_source_ability": combo_rows,
            "by_source_target": source_target_rows,
            "preview": preview,
        },
    }


def _encounter_buff_rows_payload(
    *,
    report: dict[str, Any],
    fight: dict[str, Any],
    table_report: dict[str, Any],
    master_report: dict[str, Any],
    preview_limit: int,
    view_by: str | None,
) -> dict[str, Any]:
    actor_index, ability_index = _master_data_indexes(master_report)
    report_code = report.get("code") if isinstance(report.get("code"), str) else None
    selected_fight_id = fight.get("id") if isinstance(fight.get("id"), int) else None
    actor_field = "target" if isinstance(view_by, str) and view_by.lower() == "target" else "source"
    rows_out: list[dict[str, Any]] = []
    for entry in _report_table_entries(table_report):
        actor_id = entry.get("id") if isinstance(entry.get("id"), int) else None
        if actor_id is not None:
            actor_payload = _named_actor(
                actor_index,
                actor_id,
                report_code=report_code,
                fight_id=selected_fight_id,
                source="report_encounter_buffs",
            )
        else:
            # Live WCL returns per-aura aggregate rows (id=null, name=aura) when no --ability-id
            # is set — there is no source/target actor to attach. Emit a placeholder that keeps
            # the row shape uniform and makes the missing actor scope explicit.
            actor_payload = {
                "id": None,
                "name": None,
                "identity_contract": report_actor_identity_payload(
                    report_code=report_code,
                    fight_id=selected_fight_id,
                    actor_id=None,
                    name=None,
                    provider="warcraftlogs",
                    source="report_encounter_buffs",
                    notes=["row is not actor-scoped; pass --ability-id (or use report-encounter-aura-summary) to get per-actor rows"],
                ),
            }
        aura_guid = entry.get("guid") if isinstance(entry.get("guid"), int) else None
        aura_name = entry.get("name") if isinstance(entry.get("name"), str) else None
        ability_meta = ability_index.get(aura_guid) if aura_guid is not None else None
        aura_payload: dict[str, Any] = {
            "game_id": aura_guid,
            "name": aura_name,
            "type": ability_meta.get("type") if isinstance(ability_meta, dict) else None,
            "icon": ability_meta.get("icon") if isinstance(ability_meta, dict) else None,
            "identity_contract": ability_identity_payload(
                game_id=aura_guid,
                name=aura_name,
                provider="warcraftlogs",
                source="report_encounter_buffs",
            ),
        }
        reported_total_uptime = (
            entry.get("totalUptime")
            if isinstance(entry.get("totalUptime"), (int, float))
            else entry.get("totalTime")
        )
        rows_out.append(
            {
                actor_field: actor_payload,
                "aura": aura_payload,
                "reported_total_uptime": reported_total_uptime,
                "reported_total_uses": entry.get("totalUses"),
                "reported_bands": entry.get("bands"),
                "reported_total": entry.get("total"),
                "reported_active_time": entry.get("activeTime"),
                "reported_total_time": entry.get("totalTime"),
            }
        )
    rows_out.sort(
        key=lambda row: (
            -(float(row["reported_total_uptime"]) if isinstance(row.get("reported_total_uptime"), (int, float)) else float("-inf")),
            str(((row.get(actor_field) or {}).get("name") or "")),
        )
    )
    total = len(rows_out)
    preview = rows_out[:preview_limit]
    return {
        "report": _report_brief_payload(report),
        "fight": _fight_payload(fight),
        "buffs": {
            "total": total,
            "preview": preview,
            "preview_truncated": total > preview_limit,
            "view_by": view_by,
        },
    }


def _normalize_match_text(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _report_is_finished(report: dict[str, Any]) -> bool:
    end_time = report.get("endTime")
    return isinstance(end_time, (int, float)) and float(end_time) > 0


def _fight_duration_ms(fight: dict[str, Any]) -> float | None:
    start_time = fight.get("startTime")
    end_time = fight.get("endTime")
    if not isinstance(start_time, (int, float)) or not isinstance(end_time, (int, float)):
        return None
    duration = float(end_time) - float(start_time)
    if duration <= 0:
        return None
    return duration


def _boss_matches(fight: dict[str, Any], *, boss_id: int | None, boss_name: str | None) -> bool:
    if boss_id is not None and fight.get("encounterID") != boss_id:
        return False
    if boss_name is None:
        return True
    actual = _normalize_match_text(str(fight.get("name") or ""))
    query = _normalize_match_text(boss_name)
    if not query:
        return True
    return query in actual or actual in query


def _resolve_encounter(
    ctx: typer.Context,
    *,
    client: WarcraftLogsClient,
    zone_id: int,
    boss_id: int | None,
    boss_name: str | None,
) -> dict[str, Any]:
    zone = client.zone(zone_id=zone_id)
    encounters = [row for row in (zone.get("encounters") if isinstance(zone.get("encounters"), list) else []) if isinstance(row, dict)]

    if boss_id is not None:
        zone_match = next((row for row in encounters if row.get("id") == boss_id), None)
        if zone_match is None:
            _fail(ctx, "not_found", f"Encounter {boss_id} was not found in zone {zone_id}.")
        if boss_name and not _boss_matches(zone_match, boss_id=None, boss_name=boss_name):
            _fail(ctx, "boss_scope_mismatch", f"Encounter {boss_id} in zone {zone_id} does not match boss name {boss_name!r}.")
        encounter = client.encounter(encounter_id=boss_id)
        encounter_zone = encounter.get("zone") if isinstance(encounter.get("zone"), dict) else {}
        if encounter_zone.get("id") != zone_id:
            _fail(ctx, "boss_scope_mismatch", f"Encounter {boss_id} does not belong to zone {zone_id}.")
        return encounter

    query = _normalize_match_text(boss_name)
    if not query:
        _fail(ctx, "missing_boss", "Provide --boss-id or --boss-name.")

    exact_matches = [row for row in encounters if _normalize_match_text(str(row.get("name") or "")) == query]
    fuzzy_matches = [row for row in encounters if _boss_matches(row, boss_id=None, boss_name=boss_name)]
    candidates = exact_matches or fuzzy_matches
    if not candidates:
        _fail(ctx, "not_found", f"No encounter named {boss_name!r} was found in zone {zone_id}.")
    if len(candidates) > 1:
        names = ", ".join(sorted({str(row.get("name") or "") for row in candidates if row.get("name")}))
        _fail(ctx, "ambiguous_boss", f"Boss name {boss_name!r} matched multiple encounters in zone {zone_id}: {names}")
    encounter_id = candidates[0].get("id")
    if not isinstance(encounter_id, int):
        _fail(ctx, "invalid_provider_payload", f"Encounter rows for zone {zone_id} did not include a stable encounter id.")
    return client.encounter(encounter_id=encounter_id)


def _encounter_rankings_rows(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [row for row in value if isinstance(row, dict)]
    if not isinstance(value, dict):
        return []
    for key in ("rankings", "data", "entries", "rows"):
        rows = value.get(key)
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    return []


_WARCRAFTLOGS_RANKINGS_PAGE_SIZE = 100
_WARCRAFTLOGS_ENCOUNTER_RANKING_COMBATANT_INFO_KEYS = (
    "talents",
    "gear",
    "externalBuffs",
    "itemLevel",
    "legendaryEffects",
    "artifactTraits",
    "covenantID",
    "soulbindID",
    "conduitIDs",
)


def _encounter_ranking_position(*, row: dict[str, Any], page: int, row_index: int) -> int | None:
    provider_rank = row.get("rank")
    if isinstance(provider_rank, int) and provider_rank > 0:
        return provider_rank
    if page < 1:
        return None
    return ((page - 1) * _WARCRAFTLOGS_RANKINGS_PAGE_SIZE) + row_index + 1


def _encounter_ranking_has_combatant_info(row: dict[str, Any]) -> bool:
    if isinstance(row.get("combatantInfo"), dict):
        return True
    return any(key in row for key in _WARCRAFTLOGS_ENCOUNTER_RANKING_COMBATANT_INFO_KEYS)


def _encounter_ranking_other_players_count(row: dict[str, Any]) -> int:
    if isinstance(row.get("otherPlayers"), list):
        return len(row.get("otherPlayers"))
    if isinstance(row.get("allCharacters"), list):
        return max(len(row.get("allCharacters")) - 1, 0)
    return 0


def _encounter_ranking_row_payload(row: dict[str, Any], *, page: int, row_index: int) -> dict[str, Any]:
    report = row.get("report") if isinstance(row.get("report"), dict) else {}
    server = row.get("server") if isinstance(row.get("server"), dict) else {}
    guild = row.get("guild") if isinstance(row.get("guild"), dict) else {}
    class_name = row.get("className") if isinstance(row.get("className"), str) else row.get("class")
    spec_name = row.get("spec") if isinstance(row.get("spec"), str) else row.get("specName")
    report_code = next(
        (
            value
            for value in (
                row.get("reportCode"),
                row.get("reportID"),
                row.get("code"),
                report.get("code"),
            )
            if isinstance(value, str) and value.strip()
        ),
        None,
    )
    fight_id = next(
        (
            value
            for value in (
                row.get("fightID"),
                row.get("fightId"),
                report.get("fightID"),
                report.get("fightId"),
            )
            if isinstance(value, int)
        ),
        None,
    )
    return {
        "name": row.get("name"),
        "server_name": next(
            (
                value
                for value in (
                    row.get("serverName"),
                    server.get("name"),
                )
                if isinstance(value, str) and value.strip()
            ),
            None,
        ),
        "server_region": next(
            (
                value
                for value in (
                    row.get("serverRegion"),
                    server.get("region"),
                )
                if isinstance(value, str) and value.strip()
            ),
            None,
        ),
        "guild_name": next(
            (
                value
                for value in (
                    row.get("guildName"),
                    guild.get("name"),
                )
                if isinstance(value, str) and value.strip()
            ),
            None,
        ),
        "class_name": class_name,
        "spec_name": spec_name,
        "class_spec_identity": class_spec_identity_payload(
            actor_class=class_name if isinstance(class_name, str) else None,
            spec=spec_name if isinstance(spec_name, str) else None,
            provider="warcraftlogs",
            source="encounter_rankings",
            confidence="high",
        ),
        "rank": _encounter_ranking_position(row=row, page=page, row_index=row_index),
        "out_of": row.get("outOf") if isinstance(row.get("outOf"), int) else None,
        "rank_percent": (
            row.get("rankPercent")
            if isinstance(row.get("rankPercent"), (int, float))
            else row.get("percentile") if isinstance(row.get("percentile"), (int, float)) else None
        ),
        "amount": row.get("amount"),
        "total": row.get("total"),
        "duration": row.get("duration"),
        "start_time": row.get("startTime") if row.get("startTime") is not None else report.get("startTime"),
        "report_code": report_code,
        "fight_id": fight_id,
        "report_url": _report_url(report_code, fight_id=fight_id),
        "has_combatant_info": _encounter_ranking_has_combatant_info(row),
        "other_players_count": _encounter_ranking_other_players_count(row),
    }


def _encounter_rankings_payload(
    *,
    encounter: dict[str, Any],
    rankings: Any,
    query: dict[str, Any],
    top: int,
) -> dict[str, Any]:
    page = rankings.get("page") if isinstance(rankings, dict) and isinstance(rankings.get("page"), int) else query.get("page")
    if not isinstance(page, int) or page < 1:
        page = 1
    normalized_rows = [
        _encounter_ranking_row_payload(row, page=page, row_index=row_index)
        for row_index, row in enumerate(_encounter_rankings_rows(rankings))
    ]
    returned = normalized_rows[:top]
    page_count = rankings.get("count") if isinstance(rankings, dict) and isinstance(rankings.get("count"), int) else len(normalized_rows)
    return {
        "ok": True,
        "provider": "warcraftlogs",
        "kind": "encounter_rankings",
        "ranking_basis": "encounter_character_rankings",
        "query": query,
        "encounter": _encounter_payload(encounter),
        "encounter_identity": encounter_identity_payload(
            encounter_id=encounter.get("id") if isinstance(encounter.get("id"), int) else None,
            journal_id=encounter.get("journalID") if isinstance(encounter.get("journalID"), int) else None,
            name=encounter.get("name") if isinstance(encounter.get("name"), str) else None,
            zone_id=((encounter.get("zone") or {}).get("id") if isinstance(encounter.get("zone"), dict) else None),
            provider="warcraftlogs",
            source="encounter_rankings",
        ),
        "rankings": {
            "count": len(returned),
            "page_count": page_count,
            "excluded_count": max(0, page_count - len(returned)),
            "truncated": page_count > top,
            "page": page,
            "has_more_pages": rankings.get("hasMorePages") if isinstance(rankings, dict) else None,
            "rows": returned,
        },
        "raw": rankings,
    }


def _sampled_spec_filter_notes(spec_name: str | None) -> list[str]:
    if not spec_name:
        return []
    return [
        (
            "spec_name filters sampled fights by matching participant specs before aggregation; "
            "these results are not a global spec ranking leaderboard"
        )
    ]


def _player_spec_matches(actor: dict[str, Any], spec_name: str) -> bool:
    wanted = _normalize_match_text(spec_name)
    for spec in actor.get("specs") if isinstance(actor.get("specs"), list) else []:
        if not isinstance(spec, dict):
            continue
        if _normalize_match_text(str(spec.get("spec") or "")) == wanted:
            return True
    return False


def _matching_spec_players(report: dict[str, Any], *, spec_name: str) -> list[dict[str, Any]]:
    details = _report_player_details_payload(report)["player_details"]["roles"]
    matches: list[dict[str, Any]] = []
    for role, rows in details.items():
        for row in rows:
            if _player_spec_matches(row, spec_name):
                matches.append(
                    {
                        "name": row.get("name"),
                        "id": row.get("id"),
                        "role": role,
                        "type": row.get("type"),
                        "matching_specs": [
                            spec
                            for spec in (row.get("specs") if isinstance(row.get("specs"), list) else [])
                            if _normalize_match_text(str(spec.get("spec") or "")) == _normalize_match_text(spec_name)
                        ],
                    }
                )
    return matches


def _all_player_detail_rows(report: dict[str, Any]) -> list[dict[str, Any]]:
    details = _report_player_details_payload(report)["player_details"]["roles"]
    return _all_player_detail_rows_from_roles(details)


def _all_player_detail_rows_from_roles(details: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for role, actors in details.items():
        for actor in actors:
            if isinstance(actor, dict):
                rows.append({"role": role, **actor})
    return rows


def _player_detail_actor(details_payload: dict[str, Any], actor_id: int) -> dict[str, Any] | None:
    player_details = details_payload.get("player_details") if isinstance(details_payload.get("player_details"), dict) else {}
    roles = player_details.get("roles") if isinstance(player_details.get("roles"), dict) else {}
    return next((row for row in _all_player_detail_rows_from_roles(roles) if row.get("id") == actor_id), None)


def _normalized_talent_tree_rows(actor: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    combatant_info = actor.get("combatant_info") if isinstance(actor.get("combatant_info"), dict) else {}
    rows = combatant_info.get("talentTree") if isinstance(combatant_info.get("talentTree"), list) else []
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
    class_spec_identity = actor.get("class_spec_identity") if isinstance(actor.get("class_spec_identity"), dict) else {}
    identity = class_spec_identity.get("identity") if isinstance(class_spec_identity.get("identity"), dict) else {}
    actor_class = identity.get("actor_class") if isinstance(identity.get("actor_class"), str) else None
    spec = identity.get("spec") if isinstance(identity.get("spec"), str) else None
    return actor_class, spec


def _player_talent_transport_validation(
    actor: dict[str, Any],
    *,
    raw_rows: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    if raw_rows is None:
        raw_rows, _ = _normalized_talent_tree_rows(actor)
    actor_class, spec = _player_talent_transport_identity(actor)
    validation_result = validate_talent_tree_transport(
        actor_class=actor_class,
        spec=spec,
        talent_tree_rows=raw_rows,
    )
    transport_forms = validation_result.get("transport_forms") if isinstance(validation_result.get("transport_forms"), dict) else {}
    validation = validation_result.get("validation") if isinstance(validation_result.get("validation"), dict) else {}
    return raw_rows, transport_forms, validation


def _player_talent_source_notes(transport_forms: dict[str, Any]) -> list[str]:
    source_notes = [
        "raw talents came from combatant_info.talentTree",
        "one report, one fight, one actor scope",
    ]
    if transport_forms.get("simc_split_talents"):
        source_notes.append("validated simc_split_talents via local SimulationCraft trait data")
    return source_notes


def _write_transport_packet_json(out: str | None, transport_packet: dict[str, Any]) -> str | None:
    if not out:
        return None
    try:
        output_path = Path(out).expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(transport_packet, indent=2) + "\n")
        return str(output_path)
    except OSError as exc:
        raise WarcraftLogsClientError("transport_packet_write_failed", str(exc)) from exc


def _player_talent_transport_packet(
    actor: dict[str, Any],
    *,
    report_code: str,
    fight_id: int,
    actor_id: int,
    raw_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    actor_class, spec = _player_talent_transport_identity(actor)
    raw_rows, transport_forms, validation = _player_talent_transport_validation(actor, raw_rows=raw_rows)
    return talent_transport_packet_payload(
        actor_class=actor_class,
        spec=spec,
        confidence="high" if actor_class and spec else "none",
        source="warcraftlogs_talent_tree",
        provider="warcraftlogs",
        source_notes=_player_talent_source_notes(transport_forms),
        transport_forms=transport_forms,
        raw_evidence={
            "source_contract": "warcraftlogs_combatant_info_talentTree",
            "talent_tree_entries": raw_rows,
        },
        validation=validation,
        scope={
            "type": "report_fight_actor",
            "report_code": report_code,
            "fight_id": fight_id,
            "actor_id": actor_id,
        },
    )


def _duration_bucket_rows(values: list[float], *, bucket_seconds: int) -> list[dict[str, Any]]:
    if not values:
        return []
    counts: dict[int, int] = {}
    for value in values:
        bucket_start = int(value // bucket_seconds) * bucket_seconds
        counts[bucket_start] = counts.get(bucket_start, 0) + 1
    rows = []
    total = len(values)
    for bucket_start, count in sorted(counts.items()):
        bucket_end = bucket_start + bucket_seconds
        rows.append(
            {
                "start_seconds": bucket_start,
                "end_seconds": bucket_end,
                "count": count,
                "percent": round((count / total) * 100, 2),
            }
        )
    return rows


def _boss_kill_row(*, report: dict[str, Any], fight: dict[str, Any], matching_players: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    duration_ms = _fight_duration_ms(fight)
    return {
        "report": _report_brief_payload(report),
        "report_finished": _report_is_finished(report),
        "guild": _report_payload(report).get("guild"),
        "fight": _fight_payload(fight),
        "duration_ms": duration_ms,
        "duration_seconds": round(duration_ms / 1000, 2) if duration_ms is not None else None,
        "matching_players": matching_players or [],
    }


def _sampled_cross_report_freshness() -> dict[str, Any]:
    return {
        "sampled_at": _utc_now_z(),
        "cache_ttl_seconds": None,
    }


def _sampled_cross_report_citations(rows: list[dict[str, Any]], *, limit: int = 20) -> dict[str, Any]:
    sample_reports: list[dict[str, Any]] = []
    seen: set[tuple[str, int | None]] = set()
    for row in rows:
        report = row.get("report") if isinstance(row.get("report"), dict) else {}
        fight = row.get("fight") if isinstance(row.get("fight"), dict) else {}
        report_code = report.get("code") if isinstance(report.get("code"), str) else None
        fight_id = fight.get("id") if isinstance(fight.get("id"), int) else None
        if report_code is None:
            continue
        key = (report_code, fight_id)
        if key in seen:
            continue
        seen.add(key)
        sample_reports.append(
            {
                "report_code": report_code,
                "fight_id": fight_id,
                "report_url": _report_url(report_code, fight_id=fight_id),
            }
        )
        if len(sample_reports) >= limit:
            break
    return {
        "sample_reports": sample_reports,
    }


def _collect_boss_kill_rows(
    *,
    client: WarcraftLogsClient,
    zone_id: int,
    boss_id: int | None,
    boss_name: str | None,
    difficulty: int | None,
    spec_name: str | None,
    kill_time_min: float | None,
    kill_time_max: float | None,
    report_pages: int,
    reports_per_page: int,
    start_time: float | None,
    end_time: float | None,
    guild_region: str | None,
    guild_realm: str | None,
    guild_name: str | None,
) -> dict[str, Any]:
    report_rows: list[dict[str, Any]] = []
    for page in range(1, report_pages + 1):
        pagination = client.reports(
            guild_region=guild_region,
            guild_realm=guild_realm,
            guild_name=guild_name,
            limit=reports_per_page,
            page=page,
            start_time=start_time,
            end_time=end_time,
            zone_id=zone_id,
            game_zone_id=None,
        )
        page_rows = pagination.get("data") if isinstance(pagination.get("data"), list) else []
        report_rows.extend([row for row in page_rows if isinstance(row, dict)])
        if not pagination.get("has_more_pages"):
            break

    live_reports = [row for row in report_rows if not _report_is_finished(row)]
    finished_reports = [row for row in report_rows if _report_is_finished(row)]

    boss_kills: list[dict[str, Any]] = []
    scanned_fight_count = 0
    matched_boss_kill_count = 0

    for report in finished_reports:
        fights_payload = client.report_fights(code=str(report.get("code") or ""), difficulty=difficulty, allow_unlisted=False, ttl_override=client._guild_ttl)
        fights = fights_payload.get("fights") if isinstance(fights_payload.get("fights"), list) else []
        for fight in fights:
            if not isinstance(fight, dict):
                continue
            scanned_fight_count += 1
            if not fight.get("kill"):
                continue
            if not _boss_matches(fight, boss_id=boss_id, boss_name=boss_name):
                continue
            duration_ms = _fight_duration_ms(fight)
            if duration_ms is None:
                continue
            duration_seconds = duration_ms / 1000
            if kill_time_min is not None and duration_seconds < kill_time_min:
                continue
            if kill_time_max is not None and duration_seconds > kill_time_max:
                continue
            matched_boss_kill_count += 1
            matching_players: list[dict[str, Any]] = []
            if spec_name:
                details_report = client.report_player_details(
                    code=str(report.get("code") or ""),
                    allow_unlisted=False,
                    options=ReportPlayerDetailsOptions(
                        difficulty=difficulty,
                        encounter_id=fight.get("encounterID") if isinstance(fight.get("encounterID"), int) else None,
                        fight_ids=[int(fight["id"])] if isinstance(fight.get("id"), int) else None,
                        include_combatant_info=True,
                        kill_type="Kills",
                    ),
                    ttl_override=client._guild_ttl,
                )
                matching_players = _matching_spec_players(details_report, spec_name=spec_name)
                if not matching_players:
                    continue
            boss_kills.append(_boss_kill_row(report=report, fight=fight, matching_players=matching_players))

    boss_kills.sort(
        key=lambda row: (
            row.get("duration_ms") if isinstance(row.get("duration_ms"), (int, float)) else float("inf"),
            str((row.get("report") or {}).get("code") or ""),
            int(((row.get("fight") or {}).get("id") or 0)),
        )
    )

    return {
        "rows": boss_kills,
        "sample": {
            "source_report_count": len(report_rows),
            "finished_report_count": len(finished_reports),
            "skipped_live_report_count": len(live_reports),
            "scanned_fight_count": scanned_fight_count,
            "matched_boss_kill_count": matched_boss_kill_count,
        },
    }


def _boss_kills_payload(
    *,
    kind: str,
    rows: list[dict[str, Any]],
    sample: dict[str, Any],
    query: dict[str, Any],
    top: int,
) -> dict[str, Any]:
    returned = rows[:top]
    return {
        "ok": True,
        "provider": "warcraftlogs",
        "kind": kind,
        "ranking_basis": "sampled_fastest_kills",
        "matching_rule": "sampled_zone_reports_filtered_by_optional_boss_difficulty_spec_and_kill_time",
        "query": query,
        "notes": _sampled_spec_filter_notes(query.get("spec_name") if isinstance(query, dict) else None),
        "freshness": _sampled_cross_report_freshness(),
        "citations": _sampled_cross_report_citations(rows),
        "sample": {
            **sample,
            "filtered_kill_count": len(rows),
            "returned_kill_count": len(returned),
            "excluded_kill_count": max(0, len(rows) - len(returned)),
            "truncated": len(rows) > top,
            "stable_source_only": True,
        },
        "count": len(returned),
        "kills": returned,
    }


def _kill_time_distribution_payload(*, rows: list[dict[str, Any]], sample: dict[str, Any], query: dict[str, Any], bucket_seconds: int) -> dict[str, Any]:
    durations = [
        float(duration)
        for duration in (row.get("duration_seconds") for row in rows)
        if isinstance(duration, (int, float))
    ]
    return {
        "ok": True,
        "provider": "warcraftlogs",
        "kind": "kill_time_distribution",
        "matching_rule": "sampled_zone_reports_filtered_by_optional_boss_difficulty_spec_and_kill_time",
        "query": query,
        "notes": _sampled_spec_filter_notes(query.get("spec_name") if isinstance(query, dict) else None),
        "freshness": _sampled_cross_report_freshness(),
        "citations": _sampled_cross_report_citations(rows),
        "sample": {
            **sample,
            "filtered_kill_count": len(rows),
            "stable_source_only": True,
        },
        "distribution": {
            "unit": "seconds",
            "bucket_seconds": bucket_seconds,
            "statistics": numeric_summary(durations),
            "rows": _duration_bucket_rows(durations, bucket_seconds=bucket_seconds),
        },
        "fastest_kills_preview": rows[: min(5, len(rows))],
    }


def _boss_spec_usage_payload(
    *,
    rows: list[dict[str, Any]],
    sample: dict[str, Any],
    query: dict[str, Any],
    top: int,
) -> dict[str, Any]:
    spec_counts: dict[tuple[str, str], dict[str, Any]] = {}
    sampled_player_rows = 0

    for row in rows:
        code = str(((row.get("report") or {}).get("code") or ""))
        fight_id = int(((row.get("fight") or {}).get("id") or 0))
        player_rows = row.get("player_details") if isinstance(row.get("player_details"), list) else []
        seen_specs_for_fight: set[tuple[str, str]] = set()
        for player in player_rows:
            if not isinstance(player, dict):
                continue
            sampled_player_rows += 1
            role = str(player.get("role") or "unknown")
            specs = player.get("specs") if isinstance(player.get("specs"), list) else []
            for spec in specs:
                if not isinstance(spec, dict):
                    continue
                spec_name = str(spec.get("spec") or "").strip()
                if not spec_name:
                    continue
                count = int(spec.get("count") or 0)
                key = (spec_name, role)
                entry = spec_counts.setdefault(
                    key,
                    {
                        "spec_name": spec_name,
                        "role": role,
                        "appearance_count": 0,
                        "kill_presence_count": 0,
                        "sample_fights": [],
                    },
                )
                entry["appearance_count"] += count if count > 0 else 1
                fight_key = (code, fight_id, spec_name, role)
                if fight_key not in seen_specs_for_fight:
                    seen_specs_for_fight.add(fight_key)
                    entry["kill_presence_count"] += 1
                    if len(entry["sample_fights"]) < 3:
                        entry["sample_fights"].append({"report_code": code, "fight_id": fight_id})

    normalized_rows = sorted(
        [
            {
                **entry,
                "percent_of_kills": round((entry["kill_presence_count"] / len(rows)) * 100, 2) if rows else 0.0,
            }
            for entry in spec_counts.values()
        ],
        key=lambda entry: (
            -int(entry["kill_presence_count"]),
            -int(entry["appearance_count"]),
            str(entry["spec_name"]).lower(),
        ),
    )
    returned = normalized_rows[:top]
    return {
        "ok": True,
        "provider": "warcraftlogs",
        "kind": "boss_spec_usage",
        "ranking_basis": "sampled_finished_kill_cohort_spec_presence",
        "matching_rule": "spec_presence_across_sampled_finished_kills_with_player_details",
        "query": query,
        "notes": _sampled_spec_filter_notes(query.get("spec_name") if isinstance(query, dict) else None),
        "freshness": _sampled_cross_report_freshness(),
        "citations": _sampled_cross_report_citations(rows),
        "sample": {
            **sample,
            "filtered_kill_count": len(rows),
            "sampled_player_row_count": sampled_player_rows,
            "distinct_spec_count": len(normalized_rows),
            "returned_spec_count": len(returned),
            "excluded_spec_count": max(0, len(normalized_rows) - len(returned)),
            "truncated": len(normalized_rows) > top,
            "stable_source_only": True,
        },
        "count": len(returned),
        "spec_usage": returned,
    }


def _composition_sample_row(row: dict[str, Any], *, details_report: dict[str, Any]) -> dict[str, Any]:
    details_payload = _report_player_details_payload(
        details_report,
        report_code=((row.get("report") or {}).get("code") if isinstance(row.get("report"), dict) else None),
        fight_id=((row.get("fight") or {}).get("id") if isinstance(row.get("fight"), dict) else None),
    )
    role_rows = details_payload["player_details"]["roles"]
    flattened_players: list[dict[str, Any]] = []
    class_counts: dict[str, int] = {}
    for role, players in role_rows.items():
        for player in players:
            if not isinstance(player, dict):
                continue
            flattened_players.append({"role": role, **player})
            actor_class = str(player.get("type") or "").strip()
            if actor_class:
                class_counts[actor_class] = class_counts.get(actor_class, 0) + 1
    class_count_rows = [
        {"class_name": class_name, "count": count}
        for class_name, count in sorted(class_counts.items(), key=lambda item: (-item[1], item[0].lower()))
    ]
    class_signature = "|".join(f"{row['class_name']}x{row['count']}" for row in class_count_rows) if class_count_rows else None
    return {
        **row,
        "composition": {
            "player_count": details_payload["player_details"]["counts"]["total"],
            "role_counts": {
                "tanks": details_payload["player_details"]["counts"]["tanks"],
                "healers": details_payload["player_details"]["counts"]["healers"],
                "dps": details_payload["player_details"]["counts"]["dps"],
            },
            "class_counts": class_count_rows,
            "class_signature": class_signature,
        },
        "player_details": {
            "counts": details_payload["player_details"]["counts"],
            "players": flattened_players,
        },
    }


def _collect_comp_sample_rows(
    *,
    client: WarcraftLogsClient,
    zone_id: int,
    boss_id: int | None,
    boss_name: str | None,
    difficulty: int | None,
    spec_name: str | None,
    kill_time_min: float | None,
    kill_time_max: float | None,
    report_pages: int,
    reports_per_page: int,
    start_time: float | None,
    end_time: float | None,
    guild_region: str | None,
    guild_realm: str | None,
    guild_name: str | None,
) -> dict[str, Any]:
    analytics = _collect_boss_kill_rows(
        client=client,
        zone_id=zone_id,
        boss_id=boss_id,
        boss_name=boss_name,
        difficulty=difficulty,
        spec_name=spec_name,
        kill_time_min=kill_time_min,
        kill_time_max=kill_time_max,
        report_pages=report_pages,
        reports_per_page=reports_per_page,
        start_time=start_time,
        end_time=end_time,
        guild_region=guild_region,
        guild_realm=guild_realm,
        guild_name=guild_name,
    )
    composed_rows: list[dict[str, Any]] = []
    for row in analytics["rows"]:
        report_payload = row.get("report") if isinstance(row.get("report"), dict) else {}
        fight_payload = row.get("fight") if isinstance(row.get("fight"), dict) else {}
        report_code = report_payload.get("code")
        fight_id = fight_payload.get("id")
        encounter_id = fight_payload.get("encounter_id")
        if not isinstance(report_code, str) or not isinstance(fight_id, int):
            continue
        details_report = client.report_player_details(
            code=report_code,
            allow_unlisted=False,
            options=ReportPlayerDetailsOptions(
                difficulty=int(fight_payload["difficulty"]) if isinstance(fight_payload.get("difficulty"), int) else difficulty,
                encounter_id=int(encounter_id) if isinstance(encounter_id, int) else None,
                fight_ids=[fight_id],
                include_combatant_info=True,
                kill_type="Kills",
            ),
            ttl_override=client._guild_ttl,
        )
        composed_rows.append(_composition_sample_row(row, details_report=details_report))
    return {
        "rows": composed_rows,
        "sample": analytics["sample"],
    }


def _comp_samples_payload(
    *,
    rows: list[dict[str, Any]],
    sample: dict[str, Any],
    query: dict[str, Any],
    top: int,
) -> dict[str, Any]:
    class_presence: dict[str, dict[str, Any]] = {}
    signature_counts: dict[str, dict[str, Any]] = {}
    sampled_player_count = 0

    for row in rows:
        report_code = str(((row.get("report") or {}).get("code") or ""))
        fight_id = int(((row.get("fight") or {}).get("id") or 0))
        player_details = row.get("player_details") if isinstance(row.get("player_details"), dict) else {}
        players = player_details.get("players") if isinstance(player_details.get("players"), list) else []
        sampled_player_count += len([player for player in players if isinstance(player, dict)])
        composition = row.get("composition") if isinstance(row.get("composition"), dict) else {}
        class_rows = composition.get("class_counts") if isinstance(composition.get("class_counts"), list) else []
        seen_classes: set[str] = set()
        for class_row in class_rows:
            if not isinstance(class_row, dict):
                continue
            class_name = str(class_row.get("class_name") or "").strip()
            if not class_name:
                continue
            count = int(class_row.get("count") or 0)
            entry = class_presence.setdefault(
                class_name,
                {
                    "class_name": class_name,
                    "appearance_count": 0,
                    "kill_presence_count": 0,
                    "sample_fights": [],
                },
            )
            entry["appearance_count"] += count if count > 0 else 1
            if class_name not in seen_classes:
                seen_classes.add(class_name)
                entry["kill_presence_count"] += 1
                if len(entry["sample_fights"]) < 3:
                    entry["sample_fights"].append({"report_code": report_code, "fight_id": fight_id})
        class_signature = composition.get("class_signature")
        if isinstance(class_signature, str) and class_signature:
            signature_entry = signature_counts.setdefault(
                class_signature,
                {
                    "class_signature": class_signature,
                    "kill_count": 0,
                    "sample_fights": [],
                },
            )
            signature_entry["kill_count"] += 1
            if len(signature_entry["sample_fights"]) < 3:
                signature_entry["sample_fights"].append({"report_code": report_code, "fight_id": fight_id})

    normalized_class_rows = sorted(
        [
            {
                **entry,
                "percent_of_kills": round((entry["kill_presence_count"] / len(rows)) * 100, 2) if rows else 0.0,
            }
            for entry in class_presence.values()
        ],
        key=lambda entry: (-int(entry["kill_presence_count"]), -int(entry["appearance_count"]), str(entry["class_name"]).lower()),
    )
    normalized_signatures = sorted(
        signature_counts.values(),
        key=lambda entry: (-int(entry["kill_count"]), str(entry["class_signature"]).lower()),
    )
    returned = rows[:top]
    return {
        "ok": True,
        "provider": "warcraftlogs",
        "kind": "comp_samples",
        "ranking_basis": "sampled_fastest_kills",
        "matching_rule": "class_roster_composition_across_sampled_finished_kills_with_player_details",
        "query": query,
        "notes": _sampled_spec_filter_notes(query.get("spec_name") if isinstance(query, dict) else None),
        "freshness": _sampled_cross_report_freshness(),
        "citations": _sampled_cross_report_citations(rows),
        "sample": {
            **sample,
            "filtered_kill_count": len(rows),
            "returned_kill_count": len(returned),
            "excluded_kill_count": max(0, len(rows) - len(returned)),
            "truncated": len(rows) > top,
            "sampled_player_count": sampled_player_count,
            "distinct_class_count": len(normalized_class_rows),
            "distinct_class_signature_count": len(normalized_signatures),
            "stable_source_only": True,
        },
        "class_presence": normalized_class_rows,
        "composition_signatures": normalized_signatures[: min(10, len(normalized_signatures))],
        "kills": returned,
    }


def _collect_ability_usage_rows(
    *,
    client: WarcraftLogsClient,
    zone_id: int,
    boss_id: int | None,
    boss_name: str | None,
    difficulty: int | None,
    spec_name: str | None,
    kill_time_min: float | None,
    kill_time_max: float | None,
    report_pages: int,
    reports_per_page: int,
    start_time: float | None,
    end_time: float | None,
    guild_region: str | None,
    guild_realm: str | None,
    guild_name: str | None,
    ability_id: int,
    event_limit: int,
) -> dict[str, Any]:
    analytics = _collect_boss_kill_rows(
        client=client,
        zone_id=zone_id,
        boss_id=boss_id,
        boss_name=boss_name,
        difficulty=difficulty,
        spec_name=spec_name,
        kill_time_min=kill_time_min,
        kill_time_max=kill_time_max,
        report_pages=report_pages,
        reports_per_page=reports_per_page,
        start_time=start_time,
        end_time=end_time,
        guild_region=guild_region,
        guild_realm=guild_realm,
        guild_name=guild_name,
    )
    master_cache: dict[str, dict[str, Any]] = {}
    usage_rows: list[dict[str, Any]] = []
    resolved_ability: dict[str, Any] | None = None

    for row in analytics["rows"]:
        report_payload = row.get("report") if isinstance(row.get("report"), dict) else {}
        fight_payload = row.get("fight") if isinstance(row.get("fight"), dict) else {}
        report_code = report_payload.get("code")
        fight_id = fight_payload.get("id")
        encounter_id = fight_payload.get("encounter_id")
        if not isinstance(report_code, str) or not isinstance(fight_id, int):
            continue
        master_report = master_cache.get(report_code)
        if master_report is None:
            master_report = client.report_master_data(code=report_code, allow_unlisted=False, actor_type="Player")
            master_cache[report_code] = master_report
        actor_index, ability_index = _master_data_indexes(master_report)
        if resolved_ability is None:
            resolved_ability = _named_ability(ability_index, ability_id, source="ability_usage_summary")
        events_report = client.report_events(
            code=report_code,
            allow_unlisted=False,
            options=ReportFilterOptions(
                ability_id=float(ability_id),
                data_type="Casts",
                encounter_id=int(encounter_id) if isinstance(encounter_id, int) else None,
                fight_ids=[fight_id],
                kill_type="Kills",
                limit=event_limit,
            ),
        )
        paginator = events_report.get("events") if isinstance(events_report.get("events"), dict) else {}
        event_rows = paginator.get("data") if isinstance(paginator.get("data"), list) else []
        source_counts: dict[int, int] = {}
        for event in event_rows:
            if not isinstance(event, dict):
                continue
            source_id = _event_id(event.get("sourceID"))
            if isinstance(source_id, int):
                source_counts[source_id] = source_counts.get(source_id, 0) + 1
        usage_rows.append(
            {
                **row,
                "casts": {
                    "count": len([event for event in event_rows if isinstance(event, dict)]),
                    "next_page_timestamp": paginator.get("nextPageTimestamp"),
                    "sources": [
                        {
                            "count": count,
                            "source": _named_actor(
                                actor_index,
                                source_id,
                                report_code=report_code,
                                fight_id=fight_id,
                                source="ability_usage_summary",
                            ),
                        }
                        for source_id, count in sorted(source_counts.items(), key=lambda item: (-item[1], item[0]))
                    ],
                },
            }
        )

    if resolved_ability is None:
        resolved_ability = {
            "game_id": ability_id,
            "name": f"ability:{ability_id}",
            "identity_contract": ability_identity_payload(
                game_id=ability_id,
                name=f"ability:{ability_id}",
                provider="warcraftlogs",
                source="ability_usage_summary",
                notes=["ability id was filtered explicitly but was not present in sampled master data"],
            ),
        }
    return {
        "rows": usage_rows,
        "sample": analytics["sample"],
        "ability": resolved_ability,
    }


def _ability_usage_summary_payload(
    *,
    rows: list[dict[str, Any]],
    sample: dict[str, Any],
    query: dict[str, Any],
    ability: dict[str, Any],
    preview_limit: int,
    event_limit: int,
) -> dict[str, Any]:
    cast_counts = [
        int(casts.get("count"))
        for casts in (row.get("casts") for row in rows)
        if isinstance(casts, dict) and isinstance(casts.get("count"), int)
    ]
    used_counts = [count for count in cast_counts if count > 0]
    preview = rows[:preview_limit]
    return {
        "ok": True,
        "provider": "warcraftlogs",
        "kind": "ability_usage_summary",
        "ranking_basis": "sampled_fastest_kills",
        "matching_rule": "ability_casts_across_sampled_finished_kills_with_event_limit",
        "query": {
            **query,
            "event_limit": event_limit,
            "preview_limit": preview_limit,
        },
        "notes": _sampled_spec_filter_notes(query.get("spec_name") if isinstance(query, dict) else None),
        "freshness": _sampled_cross_report_freshness(),
        "citations": _sampled_cross_report_citations(rows),
        "sample": {
            **sample,
            "filtered_kill_count": len(rows),
            "preview_kill_count": len(preview),
            "excluded_preview_kill_count": max(0, len(rows) - len(preview)),
            "preview_truncated": len(rows) > preview_limit,
            "stable_source_only": True,
        },
        "ability": ability,
        "usage": {
            "total_casts": sum(cast_counts),
            "kills_with_any_usage_count": len(used_counts),
            "kills_with_any_usage_percent": round((len(used_counts) / len(rows)) * 100, 2) if rows else 0.0,
            "casts_per_kill": numeric_summary([float(count) for count in cast_counts]),
            "casts_per_used_kill": numeric_summary([float(count) for count in used_counts]),
        },
        "kills_preview": preview,
    }


def _report_filter_query_payload(
    *,
    ability_id: float | None,
    data_type: str | None,
    difficulty: int | None,
    encounter_id: int | None,
    end_time: float | None,
    fight_ids: list[int] | None,
    filter_expression: str | None,
    hostility_type: str | None,
    kill_type: str | None,
    limit: int | None,
    source_id: int | None,
    start_time: float | None,
    target_id: int | None,
    translate: bool | None,
    view_by: str | None,
    wipe_cutoff: int | None,
) -> dict[str, Any]:
    return {
        "ability_id": ability_id,
        "data_type": data_type,
        "difficulty": difficulty,
        "encounter_id": encounter_id,
        "end_time": end_time,
        "fight_ids": fight_ids,
        "filter_expression": filter_expression,
        "hostility_type": hostility_type,
        "kill_type": kill_type,
        "limit": limit,
        "source_id": source_id,
        "start_time": start_time,
        "target_id": target_id,
        "translate": translate,
        "view_by": view_by,
        "wipe_cutoff": wipe_cutoff,
    }


def _report_filter_options(
    *,
    ability_id: float | None,
    data_type: str | None,
    difficulty: int | None,
    encounter_id: int | None,
    end_time: float | None,
    fight_ids: list[int] | None,
    filter_expression: str | None,
    hostility_type: str | None,
    kill_type: str | None,
    limit: int | None,
    source_id: int | None,
    start_time: float | None,
    target_id: int | None,
    translate: bool | None,
    view_by: str | None,
    wipe_cutoff: int | None,
) -> ReportFilterOptions:
    return ReportFilterOptions(
        ability_id=ability_id,
        data_type=data_type,
        difficulty=difficulty,
        encounter_id=encounter_id,
        end_time=end_time,
        fight_ids=fight_ids or None,
        filter_expression=filter_expression,
        hostility_type=hostility_type,
        kill_type=kill_type,
        limit=limit,
        source_id=source_id,
        start_time=start_time,
        target_id=target_id,
        translate=translate,
        view_by=view_by,
        wipe_cutoff=wipe_cutoff,
    )


def _report_events_payload(report: dict[str, Any]) -> dict[str, Any]:
    paginator = report.get("events") if isinstance(report.get("events"), dict) else {}
    return {
        "report": _report_brief_payload(report),
        "next_page_timestamp": paginator.get("nextPageTimestamp"),
        "events": paginator.get("data"),
    }


def _report_json_payload(report: dict[str, Any], *, field: str) -> dict[str, Any]:
    return {
        "report": _report_brief_payload(report),
        field: report.get(field),
    }


def _report_table_entries(report: dict[str, Any]) -> list[dict[str, Any]]:
    table = report.get("table") if isinstance(report.get("table"), dict) else {}
    container = table.get("data") if isinstance(table.get("data"), dict) else table
    rows = container.get("entries")
    if not isinstance(rows, list):
        rows = container.get("auras") if isinstance(container.get("auras"), list) else []
    return [row for row in rows if isinstance(row, dict)]


def _report_master_data_payload(report: dict[str, Any]) -> dict[str, Any]:
    master_data = report.get("masterData") if isinstance(report.get("masterData"), dict) else {}
    abilities = master_data.get("abilities") if isinstance(master_data.get("abilities"), list) else []
    actors = master_data.get("actors") if isinstance(master_data.get("actors"), list) else []
    report_code = report.get("code") if isinstance(report.get("code"), str) else None
    return {
        "report": _report_brief_payload(report),
        "master_data": {
            "log_version": master_data.get("logVersion"),
            "game_version": master_data.get("gameVersion"),
            "lang": master_data.get("lang"),
            "ability_count": len([row for row in abilities if isinstance(row, dict)]),
            "actor_count": len([row for row in actors if isinstance(row, dict)]),
            "abilities": [
                {
                    "game_id": row.get("gameID"),
                    "icon": row.get("icon"),
                    "name": row.get("name"),
                    "type": row.get("type"),
                    "identity_contract": ability_identity_payload(
                        game_id=row.get("gameID") if isinstance(row.get("gameID"), int) else None,
                        name=row.get("name") if isinstance(row.get("name"), str) else None,
                        provider="warcraftlogs",
                        source="report_master_data",
                    ),
                }
                for row in abilities
                if isinstance(row, dict)
            ],
            "actors": [
                {
                    "game_id": row.get("gameID"),
                    "icon": row.get("icon"),
                    "id": row.get("id"),
                    "name": row.get("name"),
                    "pet_owner": row.get("petOwner"),
                    "server": row.get("server"),
                    "sub_type": row.get("subType"),
                    "type": row.get("type"),
                    "identity_contract": report_actor_identity_payload(
                        report_code=report_code,
                        fight_id=None,
                        actor_id=row.get("id") if isinstance(row.get("id"), int) else None,
                        name=row.get("name") if isinstance(row.get("name"), str) else None,
                        actor_class=row.get("subType") if row.get("type") == "Player" and isinstance(row.get("subType"), str) else None,
                        provider="warcraftlogs",
                        source="report_master_data",
                        notes=["canonical only when narrowed to a specific fight scope"],
                    ),
                }
                for row in actors
                if isinstance(row, dict)
            ],
        },
    }


def _player_detail_actor_payload(actor: dict[str, Any], *, report_code: str | None = None, fight_id: int | None = None) -> dict[str, Any]:
    specs = actor.get("specs") if isinstance(actor.get("specs"), list) else []
    normalized_specs = [
        {"spec": spec.get("spec"), "count": spec.get("count")}
        for spec in specs
        if isinstance(spec, dict)
    ]
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
        "potion_use": actor.get("potionUse"),
        "healthstone_use": actor.get("healthstoneUse"),
        "combatant_info": actor.get("combatantInfo"),
        "class_spec_identity": class_spec_identity_payload(
            actor_class=actor.get("type") if isinstance(actor.get("type"), str) else None,
            spec=normalized_specs[0].get("spec") if len(normalized_specs) == 1 else None,
            provider="warcraftlogs",
            source="report_player_details",
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


def _report_player_details_payload(report: dict[str, Any], *, report_code: str | None = None, fight_id: int | None = None) -> dict[str, Any]:
    details = report.get("playerDetails") if isinstance(report.get("playerDetails"), dict) else {}
    data = details.get("data") if isinstance(details.get("data"), dict) else {}
    role_data = data.get("playerDetails") if isinstance(data.get("playerDetails"), dict) else data
    roles: dict[str, list[dict[str, Any]]] = {}
    counts: dict[str, int] = {}
    for role in ("tanks", "healers", "dps"):
        rows = role_data.get(role) if isinstance(role_data.get(role), list) else []
        normalized_rows = [
            _player_detail_actor_payload(row, report_code=report_code, fight_id=fight_id)
            for row in rows
            if isinstance(row, dict)
        ]
        roles[role] = normalized_rows
        counts[role] = len(normalized_rows)
    counts["total"] = counts["tanks"] + counts["healers"] + counts["dps"]
    return {
        "report": _report_brief_payload(report),
        "player_details": {
            "counts": counts,
            "roles": roles,
        },
    }


def _report_rankings_payload(report: dict[str, Any]) -> dict[str, Any]:
    rankings = report.get("rankings")
    rows = rankings.get("data") if isinstance(rankings, dict) else []
    normalized_rows = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
    return {
        "report": _report_brief_payload(report),
        "rankings": {
            "count": len(normalized_rows),
            "rows": normalized_rows,
        },
    }


def _report_encounter_aura_summary_payload(
    *,
    report: dict[str, Any],
    fight: dict[str, Any],
    table_report: dict[str, Any],
    master_report: dict[str, Any],
    ability_id: int,
) -> dict[str, Any]:
    actor_index, ability_index = _master_data_indexes(master_report)
    rows_out: list[dict[str, Any]] = []
    for entry in _report_table_entries(table_report):
        source_id = entry.get("id") if isinstance(entry.get("id"), int) else None
        reported_total_uptime = entry.get("totalUptime") if isinstance(entry.get("totalUptime"), (int, float)) else entry.get("totalTime")
        rows_out.append(
            {
                "source": _named_actor(
                    actor_index,
                    source_id,
                    report_code=report.get("code") if isinstance(report.get("code"), str) else None,
                    fight_id=fight.get("id") if isinstance(fight.get("id"), int) else None,
                    source="report_encounter_aura_summary",
                ) if source_id is not None else {"id": None, "name": entry.get("name")},
                "reported_total": entry.get("total"),
                "reported_active_time": entry.get("activeTime"),
                "reported_total_time": entry.get("totalTime"),
                "reported_total_uptime": reported_total_uptime,
                "reported_total_uses": entry.get("totalUses"),
                "reported_bands": entry.get("bands"),
                "raw_entry": entry,
            }
        )
    rows_out.sort(
        key=lambda row: (
            -(float(row["reported_total_uptime"]) if isinstance(row.get("reported_total_uptime"), (int, float)) else
              float(row["reported_total"]) if isinstance(row.get("reported_total"), (int, float)) else float("-inf")),
            str(((row.get("source") or {}).get("name") or "")),
        )
    )
    return {
        "report": _report_brief_payload(table_report),
        "aura": _named_ability(ability_index, ability_id, source="report_encounter_aura_summary")
        or {
            "game_id": ability_id,
            "name": f"ability:{ability_id}",
            "identity_contract": ability_identity_payload(
                game_id=ability_id,
                name=f"ability:{ability_id}",
                provider="warcraftlogs",
                source="report_encounter_aura_summary",
                notes=["ability id was requested explicitly but no matching master-data ability row was found"],
            ),
        },
        "aura_summary": {
            "entry_count": len(rows_out),
            "rows": rows_out,
        },
    }


def _report_encounter_damage_source_summary_payload(
    *,
    report: dict[str, Any],
    fight: dict[str, Any],
    table_report: dict[str, Any],
    master_report: dict[str, Any],
) -> dict[str, Any]:
    actor_index, _ability_index = _master_data_indexes(master_report)
    rows_out: list[dict[str, Any]] = []
    for entry in _report_table_entries(table_report):
        source_id = entry.get("id") if isinstance(entry.get("id"), int) else None
        rows_out.append(
            {
                "source": _named_actor(
                    actor_index,
                    source_id,
                    report_code=report.get("code") if isinstance(report.get("code"), str) else None,
                    fight_id=fight.get("id") if isinstance(fight.get("id"), int) else None,
                    source="report_encounter_damage_source_summary",
                ) if source_id is not None else {"id": None, "name": entry.get("name")},
                "reported_total": entry.get("total"),
                "raw_entry": entry,
            }
        )
    rows_out.sort(
        key=lambda row: (
            -(float(row["reported_total"]) if isinstance(row.get("reported_total"), (int, float)) else float("-inf")),
            str(((row.get("source") or {}).get("name") or "")),
        )
    )
    return {
        "report": _report_brief_payload(table_report),
        "damage_summary": {
            "entry_count": len(rows_out),
            "rows": rows_out,
        },
    }


def _report_encounter_damage_target_summary_payload(
    *,
    report: dict[str, Any],
    fight: dict[str, Any],
    table_report: dict[str, Any],
    master_report: dict[str, Any],
) -> dict[str, Any]:
    actor_index, _ability_index = _master_data_indexes(master_report)
    rows_out: list[dict[str, Any]] = []
    for entry in _report_table_entries(table_report):
        target_id = entry.get("id") if isinstance(entry.get("id"), int) else None
        rows_out.append(
            {
                "target": _named_actor(
                    actor_index,
                    target_id,
                    report_code=report.get("code") if isinstance(report.get("code"), str) else None,
                    fight_id=fight.get("id") if isinstance(fight.get("id"), int) else None,
                    source="report_encounter_damage_target_summary",
                ) if target_id is not None else {"id": None, "name": entry.get("name")},
                "reported_total": entry.get("total"),
                "raw_entry": entry,
            }
        )
    rows_out.sort(
        key=lambda row: (
            -(float(row["reported_total"]) if isinstance(row.get("reported_total"), (int, float)) else float("-inf")),
            str(((row.get("target") or {}).get("name") or "")),
        )
    )
    return {
        "report": _report_brief_payload(table_report),
        "damage_summary": {
            "entry_count": len(rows_out),
            "rows": rows_out,
        },
    }


def _aura_compare_rows(
    *,
    left_rows: list[dict[str, Any]],
    right_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    def _row_key(row: dict[str, Any]) -> tuple[int | None, str]:
        source = row.get("source") if isinstance(row.get("source"), dict) else {}
        source_id = source.get("id") if isinstance(source.get("id"), int) else None
        source_name = str(source.get("name") or "")
        return source_id, source_name

    left_index = {_row_key(row): row for row in left_rows if isinstance(row, dict)}
    right_index = {_row_key(row): row for row in right_rows if isinstance(row, dict)}
    combined_keys = sorted(set(left_index) | set(right_index), key=lambda item: (str(item[1]).lower(), item[0] or 0))
    compared: list[dict[str, Any]] = []
    for key in combined_keys:
        left = left_index.get(key)
        right = right_index.get(key)
        left_total = left.get("reported_total") if isinstance(left, dict) else None
        right_total = right.get("reported_total") if isinstance(right, dict) else None
        left_active = left.get("reported_active_time") if isinstance(left, dict) else None
        right_active = right.get("reported_active_time") if isinstance(right, dict) else None
        compared.append(
            {
                "source": (
                    left.get("source")
                    if isinstance(left, dict) and isinstance(left.get("source"), dict)
                    else (right.get("source") if isinstance(right, dict) and isinstance(right.get("source"), dict) else {"id": key[0], "name": key[1]})
                ),
                "left_reported_total": left_total,
                "right_reported_total": right_total,
                "reported_total_delta": (
                    round(float(right_total) - float(left_total), 2)
                    if isinstance(left_total, (int, float)) and isinstance(right_total, (int, float))
                    else None
                ),
                "left_reported_active_time": left_active,
                "right_reported_active_time": right_active,
                "reported_active_time_delta": (
                    int(right_active) - int(left_active)
                    if isinstance(left_active, (int, float)) and isinstance(right_active, (int, float))
                    else None
                ),
                "left_row": left,
                "right_row": right,
            }
        )
    compared.sort(
        key=lambda row: (
            -abs(float(row["reported_total_delta"])) if isinstance(row.get("reported_total_delta"), (int, float)) else -1.0,
            str(((row.get("source") or {}).get("name") or "")),
        )
    )
    return compared


def _character_rankings_payload(character: dict[str, Any], *, top: int) -> dict[str, Any]:
    server = character.get("server") if isinstance(character.get("server"), dict) else {}
    faction = character.get("faction") if isinstance(character.get("faction"), dict) else {}
    rankings = character.get("zoneRankings") if isinstance(character.get("zoneRankings"), dict) else {}
    rankings_error = rankings.get("error") if isinstance(rankings.get("error"), str) else None
    all_stars = rankings.get("allStars") if isinstance(rankings.get("allStars"), list) else []
    ranking_rows = rankings.get("rankings") if isinstance(rankings.get("rankings"), list) else []
    return {
        "id": character.get("id"),
        "canonical_id": character.get("canonicalID"),
        "name": character.get("name"),
        "level": character.get("level"),
        "class_id": character.get("classID"),
        "server": _server_payload(server) if server else None,
        "faction": {"id": faction.get("id"), "name": faction.get("name")} if faction else None,
        "summary": {
            "zone": rankings.get("zone"),
            "difficulty": rankings.get("difficulty"),
            "metric": rankings.get("metric"),
            "partition": rankings.get("partition"),
            "size": rankings.get("size"),
            "best_performance_average": rankings.get("bestPerformanceAverage"),
            "median_performance_average": rankings.get("medianPerformanceAverage"),
        }
        if not rankings_error
        else None,
        "error": rankings_error,
        "all_stars": [
            {
                "spec": row.get("spec"),
                "points": row.get("points"),
                "possible_points": row.get("possiblePoints"),
                "rank": row.get("rank"),
                "rank_percent": row.get("rankPercent"),
                "region_rank": row.get("regionRank"),
                "server_rank": row.get("serverRank"),
                "total": row.get("total"),
            }
            for row in all_stars[:top]
            if isinstance(row, dict)
        ],
        "rankings": [
            {
                "encounter": row.get("encounter"),
                "spec": row.get("spec"),
                "best_spec": row.get("bestSpec"),
                "rank_percent": row.get("rankPercent"),
                "median_percent": row.get("medianPercent"),
                "total_kills": row.get("totalKills"),
                "all_stars": row.get("allStars"),
                "best_rank": row.get("bestRank"),
                "best_amount": row.get("bestAmount"),
                "fastest_kill": row.get("fastestKill"),
            }
            for row in ranking_rows[:top]
            if isinstance(row, dict)
        ],
        "raw": rankings,
    }


def _reports_payload(pagination: dict[str, Any]) -> dict[str, Any]:
    rows = pagination.get("data") if isinstance(pagination.get("data"), list) else []
    return {
        "pagination": {
            "total": pagination.get("total"),
            "per_page": pagination.get("per_page"),
            "current_page": pagination.get("current_page"),
            "from": pagination.get("from"),
            "to": pagination.get("to"),
            "last_page": pagination.get("last_page"),
            "has_more_pages": pagination.get("has_more_pages"),
        },
        "reports": [_report_payload(report) for report in rows if isinstance(report, dict)],
    }


@app.callback()
def main(
    ctx: typer.Context,
    pretty: bool = typer.Option(False, "--pretty", help="Pretty-print JSON output."),
) -> None:
    ctx.obj = RuntimeConfig(pretty=pretty)


@app.command("search")
def search(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="Explicit Warcraft Logs report URL or report code."),
    limit: int = typer.Option(5, "--limit", min=1, max=50, help="Accepted for wrapper compatibility; explicit report discovery returns at most one result."),
) -> None:
    del limit
    _emit(ctx, _report_search_payload(query, ref=_explicit_report_reference(query)))


@app.command("resolve")
def resolve(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="Explicit Warcraft Logs report URL or report code."),
    limit: int = typer.Option(5, "--limit", min=1, max=50, help="Accepted for wrapper compatibility; explicit report resolution returns at most one match."),
) -> None:
    del limit
    _emit(ctx, _report_resolve_payload(query, ref=_explicit_report_reference(query)))


@app.command("doctor")
def doctor(
    ctx: typer.Context,
    no_live: bool = typer.Option(False, "--no-live", help="Skip live Warcraft Logs auth probes and report local/runtime readiness only."),
) -> None:
    _emit(ctx, _doctor_payload(live=not no_live))


def _random_state_token() -> str:
    return secrets.token_urlsafe(32)


def _pkce_verifier() -> str:
    return secrets.token_urlsafe(72)[:96]


def _pkce_challenge(code_verifier: str) -> str:
    digest = hashlib.sha256(code_verifier.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _token_payload_summary(payload: dict[str, Any], *, auth_mode: str, redirect_uri: str) -> dict[str, Any]:
    expires_in = payload.get("expires_in", 0)
    expires_at = time.time() + int(expires_in) if isinstance(expires_in, (int, float)) else None
    return {
        "auth_mode": auth_mode,
        "access_token": payload.get("access_token"),
        "refresh_token": payload.get("refresh_token"),
        "token_type": payload.get("token_type"),
        "scope": payload.get("scope"),
        "redirect_uri": redirect_uri,
        "expires_at": expires_at,
    }


def _active_auth_mode_from_state(state: dict[str, Any]) -> str:
    if state.get("has_access_token") and isinstance(state.get("auth_mode"), str):
        return str(state["auth_mode"])
    return "client_credentials"


def _endpoint_family_from_state(state: dict[str, Any]) -> str:
    if state.get("has_access_token") and state.get("auth_mode") in {"authorization_code", "pkce"}:
        return "user"
    return "client"


@auth_app.command("status")
def auth_status(
    ctx: typer.Context,
    no_live: bool = typer.Option(False, "--no-live", help="Skip live Warcraft Logs auth probes and report local/runtime readiness only."),
) -> None:
    auth = load_warcraftlogs_auth_config()
    credential_source = auth.env_file if auth.env_file is not None else ("environment" if auth.configured else None)
    state = provider_auth_status("warcraftlogs")
    runtime_access = _runtime_access_payload()
    public_api_access = _public_api_access_payload(
        auth_configured=auth.configured,
        runtime_access=runtime_access,
        live=not no_live,
    )
    user_api_access = _user_api_access_payload(
        state,
        runtime_access=runtime_access,
        live=not no_live,
    )
    _emit(
        ctx,
        {
            "ok": True,
            "provider": "warcraftlogs",
            "auth": {
                "configured": auth.configured,
                "client_credentials_configured": auth.configured,
                "flow": "oauth_client_credentials",
                "active_mode": _active_auth_mode_from_state(state),
                "endpoint_family": _endpoint_family_from_state(state),
                "credential_source": credential_source,
                "lookup_order": [".env.local", warcraftlogs_provider_env_path(), "environment"],
                "state": state,
                "runtime_access": runtime_access,
                "public_api_access": public_api_access,
                "user_api_access": user_api_access,
                "grants": _grant_statuses(auth_configured=auth.configured, runtime_access=runtime_access),
            },
        },
    )


@auth_app.command("client")
def auth_client(ctx: typer.Context) -> None:
    auth = load_warcraftlogs_auth_config()
    credential_source = auth.env_file if auth.env_file is not None else ("environment" if auth.configured else None)
    client_id = auth.client_id or ""
    display_client_id = f"{client_id[:8]}..." if len(client_id) > 8 else client_id
    _emit(
        ctx,
        {
            "ok": True,
            "provider": "warcraftlogs",
            "client": {
                "configured": auth.configured,
                "credential_source": credential_source,
                "client_id": display_client_id or None,
                "site_profile": RETAIL_PROFILE.key,
                "authorize_url": RETAIL_PROFILE.oauth_authorize_url,
                "token_url": RETAIL_PROFILE.oauth_token_url,
                "client_api_url": RETAIL_PROFILE.api_url,
                "user_api_url": RETAIL_PROFILE.user_api_url,
            },
        },
    )


@auth_app.command("token")
def auth_token(ctx: typer.Context) -> None:
    state = provider_auth_status("warcraftlogs")
    payload = load_provider_auth_state("warcraftlogs") or {}
    scopes = _scope_breakdown(
        granted_scope=payload.get("scope"),
        requested_scopes=payload.get("requested_scopes"),
        access_token=payload.get("access_token"),
    )
    scope_warning = scopes["warning"] if _saved_user_token_ready(state) else None
    _emit(
        ctx,
        {
            "ok": True,
            "provider": "warcraftlogs",
            "token": {
                "active_mode": _active_auth_mode_from_state(state),
                "endpoint_family": _endpoint_family_from_state(state),
                "state": state,
                "scopes": {
                    "granted": scopes["granted"],
                    "requested": scopes["requested"],
                    "has_view_user_profile": scopes["has_view_user_profile"],
                    "has_view_private_reports": scopes["has_view_private_reports"],
                },
                "scope_warning": scope_warning,
            },
        },
    )


@auth_app.command("login")
def auth_login(
    ctx: typer.Context,
    redirect_uri: str = typer.Option(..., "--redirect-uri", help="Registered redirect URI for the Warcraft Logs OAuth client."),
    code: str | None = typer.Option(None, "--code", help="Authorization code returned by the redirect callback."),
    state: str | None = typer.Option(None, "--state", help="State value returned by the redirect callback."),
    scope: list[str] = typer.Option([], "--scope", help="OAuth scope. Use view-user-profile for currentUser/user data and view-private-reports for private/guild-stealth reports. Repeatable."),
) -> None:
    if not code:
        client = _client(ctx)
        try:
            pending_state = _random_state_token()
            scopes = [item.strip() for item in scope if item.strip()]
            authorize_url = client.authorization_code_url(redirect_uri=redirect_uri, state=pending_state)
            if scopes:
                joiner = "&" if "?" in authorize_url else "?"
                authorize_url = f"{authorize_url}{joiner}scope={'+'.join(scopes)}"
            saved_path = save_provider_auth_state(
                "warcraftlogs",
                {
                    "pending_auth_mode": "authorization_code",
                    "pending_state": pending_state,
                    "redirect_uri": redirect_uri,
                    "requested_scopes": scopes,
                },
            )
        except WarcraftLogsClientError as exc:
            _handle_client_error(ctx, exc)
            return
        finally:
            client.close()
        _emit(
            ctx,
            {
                "ok": True,
                "provider": "warcraftlogs",
                "mode": "authorization_code",
                "step": "authorize",
                "authorize_url": authorize_url,
                "redirect_uri": redirect_uri,
                "state": pending_state,
                "requested_scopes": scopes,
                "state_path": str(saved_path),
            },
            client=client,
        )
        return

    pending = load_provider_auth_state("warcraftlogs") or {}
    expected_state = pending.get("pending_state")
    if isinstance(expected_state, str) and expected_state and not state:
        _fail(ctx, "missing_state", "Missing callback state. Re-run the login URL step and provide the returned state value.")
    if isinstance(expected_state, str) and expected_state and state and state != expected_state:
        _fail(ctx, "state_mismatch", "Callback state did not match the pending authorization flow.")
    if isinstance(pending.get("redirect_uri"), str) and pending.get("redirect_uri") != redirect_uri:
        _fail(ctx, "redirect_uri_mismatch", "Redirect URI did not match the pending authorization flow.")

    client = _client(ctx)
    try:
        payload = client.exchange_authorization_code(code=code, redirect_uri=redirect_uri)
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)
        return
    finally:
        client.close()
    token_summary = _token_payload_summary(payload, auth_mode="authorization_code", redirect_uri=redirect_uri)
    if isinstance(pending.get("requested_scopes"), list):
        token_summary["requested_scopes"] = pending.get("requested_scopes")
    saved_path = save_provider_auth_state("warcraftlogs", token_summary)
    scopes = _scope_breakdown(
        granted_scope=token_summary.get("scope"),
        requested_scopes=token_summary.get("requested_scopes"),
        access_token=token_summary.get("access_token"),
    )
    _emit(
        ctx,
        {
            "ok": True,
            "provider": "warcraftlogs",
            "mode": "authorization_code",
            "step": "token_exchanged",
            "endpoint_family": "user",
            "state_path": str(saved_path),
            "token": {
                "token_type": token_summary.get("token_type"),
                "scope": token_summary.get("scope"),
                "expires_at": token_summary.get("expires_at"),
                "has_refresh_token": bool(token_summary.get("refresh_token")),
            },
            "scopes": {
                "granted": scopes["granted"],
                "requested": scopes["requested"],
                "has_view_user_profile": scopes["has_view_user_profile"],
                "has_view_private_reports": scopes["has_view_private_reports"],
            },
            "scope_warning": scopes["warning"],
        },
        client=client,
    )


@auth_app.command("pkce-login")
def auth_pkce_login(
    ctx: typer.Context,
    redirect_uri: str = typer.Option(..., "--redirect-uri", help="Registered redirect URI for the Warcraft Logs OAuth client."),
    code: str | None = typer.Option(None, "--code", help="Authorization code returned by the redirect callback."),
    state: str | None = typer.Option(None, "--state", help="State value returned by the redirect callback."),
    scope: list[str] = typer.Option([], "--scope", help="OAuth scope. Use view-user-profile for currentUser/user data and view-private-reports for private/guild-stealth reports. Repeatable."),
) -> None:
    if not code:
        client = _client(ctx)
        try:
            pending_state = _random_state_token()
            code_verifier = _pkce_verifier()
            code_challenge = _pkce_challenge(code_verifier)
            scopes = [item.strip() for item in scope if item.strip()]
            authorize_url = client.pkce_code_url(
                redirect_uri=redirect_uri,
                state=pending_state,
                code_challenge=code_challenge,
            )
            if scopes:
                joiner = "&" if "?" in authorize_url else "?"
                authorize_url = f"{authorize_url}{joiner}scope={'+'.join(scopes)}"
            saved_path = save_provider_auth_state(
                "warcraftlogs",
                {
                    "pending_auth_mode": "pkce",
                    "pending_state": pending_state,
                    "redirect_uri": redirect_uri,
                    "code_verifier": code_verifier,
                    "requested_scopes": scopes,
                },
            )
        except WarcraftLogsClientError as exc:
            _handle_client_error(ctx, exc)
            return
        finally:
            client.close()
        _emit(
            ctx,
            {
                "ok": True,
                "provider": "warcraftlogs",
                "mode": "pkce",
                "step": "authorize",
                "authorize_url": authorize_url,
                "redirect_uri": redirect_uri,
                "state": pending_state,
                "requested_scopes": scopes,
                "state_path": str(saved_path),
            },
            client=client,
        )
        return

    pending = load_provider_auth_state("warcraftlogs") or {}
    expected_state = pending.get("pending_state")
    code_verifier = pending.get("code_verifier")
    if not isinstance(code_verifier, str) or not code_verifier:
        _fail(ctx, "missing_code_verifier", "Missing pending PKCE verifier. Re-run `warcraftlogs auth pkce-login --redirect-uri ...` first.")
    if isinstance(expected_state, str) and expected_state and not state:
        _fail(ctx, "missing_state", "Missing callback state. Re-run the PKCE login URL step and provide the returned state value.")
    if isinstance(expected_state, str) and expected_state and state and state != expected_state:
        _fail(ctx, "state_mismatch", "Callback state did not match the pending PKCE flow.")
    if isinstance(pending.get("redirect_uri"), str) and pending.get("redirect_uri") != redirect_uri:
        _fail(ctx, "redirect_uri_mismatch", "Redirect URI did not match the pending PKCE flow.")

    client = _client(ctx)
    try:
        payload = client.exchange_pkce_code(code=code, redirect_uri=redirect_uri, code_verifier=code_verifier)
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)
        return
    finally:
        client.close()
    token_summary = _token_payload_summary(payload, auth_mode="pkce", redirect_uri=redirect_uri)
    if isinstance(pending.get("requested_scopes"), list):
        token_summary["requested_scopes"] = pending.get("requested_scopes")
    saved_path = save_provider_auth_state("warcraftlogs", token_summary)
    scopes = _scope_breakdown(
        granted_scope=token_summary.get("scope"),
        requested_scopes=token_summary.get("requested_scopes"),
        access_token=token_summary.get("access_token"),
    )
    _emit(
        ctx,
        {
            "ok": True,
            "provider": "warcraftlogs",
            "mode": "pkce",
            "step": "token_exchanged",
            "endpoint_family": "user",
            "state_path": str(saved_path),
            "token": {
                "token_type": token_summary.get("token_type"),
                "scope": token_summary.get("scope"),
                "expires_at": token_summary.get("expires_at"),
                "has_refresh_token": bool(token_summary.get("refresh_token")),
            },
            "scopes": {
                "granted": scopes["granted"],
                "requested": scopes["requested"],
                "has_view_user_profile": scopes["has_view_user_profile"],
                "has_view_private_reports": scopes["has_view_private_reports"],
            },
            "scope_warning": scopes["warning"],
        },
        client=client,
    )


@auth_app.command("logout")
def auth_logout(ctx: typer.Context) -> None:
    removed = delete_provider_auth_state("warcraftlogs")
    _emit(
        ctx,
        {
            "ok": True,
            "provider": "warcraftlogs",
            "auth": {
                "state_path": str(provider_state_path("warcraftlogs")),
                "removed": removed,
            },
        },
    )


@auth_app.command("whoami")
def auth_whoami(ctx: typer.Context) -> None:
    client = _client(ctx)
    try:
        payload = client.current_user()
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)
        return
    finally:
        client.close()
    _emit(
        ctx,
        {
            "ok": True,
            "provider": "warcraftlogs",
            "endpoint_family": "user",
            "user": {
                "id": payload.get("id"),
                "name": payload.get("name"),
                "avatar": payload.get("avatar"),
            },
        },
        client=client,
    )


@app.command("rate-limit")
def rate_limit(ctx: typer.Context) -> None:
    client = _client(ctx)
    try:
        payload = client.rate_limit()
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)
        return
    finally:
        client.close()
    _emit(
        ctx,
        {
            "ok": True,
            "provider": "warcraftlogs",
            "rate_limit": {
                "limit_per_hour": payload.get("limitPerHour"),
                "points_spent_this_hour": payload.get("pointsSpentThisHour"),
                "points_reset_in": payload.get("pointsResetIn"),
            },
        },
        client=client,
    )


@app.command("regions")
def regions(ctx: typer.Context) -> None:
    client = _client(ctx)
    try:
        rows = client.regions()
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)
        return
    finally:
        client.close()
    regions_payload = [_region_payload(region) for region in rows]
    _emit(ctx, {"ok": True, "provider": "warcraftlogs", "count": len(regions_payload), "regions": regions_payload}, client=client)


@app.command("expansions")
def expansions(ctx: typer.Context) -> None:
    client = _client(ctx)
    try:
        rows = client.expansions()
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)
        return
    finally:
        client.close()
    expansions_payload = [_expansion_payload(row) for row in rows]
    _emit(ctx, {"ok": True, "provider": "warcraftlogs", "count": len(expansions_payload), "expansions": expansions_payload}, client=client)


@app.command("server")
def server(
    ctx: typer.Context,
    region: str,
    slug: str,
) -> None:
    client = _client(ctx)
    try:
        payload = client.server(region=region, slug=slug)
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)
        return
    finally:
        client.close()
    _emit(ctx, {"ok": True, "provider": "warcraftlogs", "server": _server_payload(payload)}, client=client)


@app.command("zones")
def zones(
    ctx: typer.Context,
    expansion_id: int | None = typer.Option(None, "--expansion-id", help="Optional Warcraft Logs expansion ID filter."),
) -> None:
    client = _client(ctx)
    try:
        rows = client.zones(expansion_id=expansion_id)
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)
        return
    finally:
        client.close()
    zones_payload = [_zone_payload(zone) for zone in rows]
    _emit(
        ctx,
        {
            "ok": True,
            "provider": "warcraftlogs",
            "expansion_id": expansion_id,
            "count": len(zones_payload),
            "zones": zones_payload,
        },
        client=client,
    )


@app.command("zone")
def zone(ctx: typer.Context, zone_id: int) -> None:
    client = _client(ctx)
    try:
        payload = client.zone(zone_id=zone_id)
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)
        return
    finally:
        client.close()
    _emit(ctx, {"ok": True, "provider": "warcraftlogs", "zone": _zone_payload(payload)}, client=client)


@app.command("encounter")
def encounter(ctx: typer.Context, encounter_id: int) -> None:
    client = _client(ctx)
    try:
        payload = client.encounter(encounter_id=encounter_id)
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)
        return
    finally:
        client.close()
    zone = payload.get("zone") if isinstance(payload.get("zone"), dict) else {}
    _emit(
        ctx,
        {
            "ok": True,
            "provider": "warcraftlogs",
            "encounter": _encounter_payload(payload),
            "encounter_identity": encounter_identity_payload(
                encounter_id=payload.get("id") if isinstance(payload.get("id"), int) else None,
                journal_id=payload.get("journalID") if isinstance(payload.get("journalID"), int) else None,
                name=payload.get("name") if isinstance(payload.get("name"), str) else None,
                zone_id=zone.get("id") if isinstance(zone.get("id"), int) else None,
                provider="warcraftlogs",
                source="encounter",
            ),
        },
        client=client,
    )


@app.command("encounter-rankings")
def encounter_rankings(
    ctx: typer.Context,
    zone_id: int = typer.Option(..., "--zone-id", help="Warcraft Logs zone ID that contains the encounter."),
    boss_id: int | None = typer.Option(None, "--boss-id", help="Encounter ID to rank."),
    boss_name: str | None = typer.Option(None, "--boss-name", help="Encounter name to resolve within the selected zone."),
    bracket: int | None = typer.Option(None, "--bracket", help="Optional Warcraft Logs bracket filter."),
    difficulty: int | None = typer.Option(None, "--difficulty", help="Optional difficulty ID filter."),
    class_name: str | None = typer.Option(None, "--class-name", help="Optional class slug or class name filter."),
    spec_name: str | None = typer.Option(None, "--spec-name", help="Optional spec slug or spec name filter."),
    metric: str | None = typer.Option(None, "--metric", help="Optional ranking metric such as dps, hps, or bossdps."),
    page: int | None = typer.Option(None, "--page", min=1, help="Optional rankings page number."),
    partition: int | None = typer.Option(None, "--partition", help="Optional Warcraft Logs partition filter."),
    size: int | None = typer.Option(None, "--size", help="Optional raid size filter."),
    server_region: str | None = typer.Option(None, "--server-region", help="Optional server region filter."),
    server_slug: str | None = typer.Option(None, "--server-slug", help="Optional server slug filter."),
    leaderboard: str | None = typer.Option(None, "--leaderboard", help="Optional leaderboard enum filter."),
    hard_mode_level: str | None = typer.Option(None, "--hard-mode-level", help="Optional hard-mode-level enum filter."),
    filter_text: str | None = typer.Option(None, "--filter", help="Optional Warcraft Logs advanced encounter ranking filter string."),
    include_combatant_info: bool | None = typer.Option(
        None,
        "--include-combatant-info/--no-include-combatant-info",
        help="Optional combatant info toggle.",
    ),
    include_other_players: bool | None = typer.Option(
        None,
        "--include-other-players/--no-include-other-players",
        help="Optional toggle for other players in the clear.",
    ),
    top: int = typer.Option(10, "--top", min=1, max=100, help="Maximum returned ranking rows after normalization."),
) -> None:
    normalized_leaderboard = _normalize_graphql_enum(leaderboard)
    normalized_hard_mode_level = _normalize_hard_mode_level_rank_filter(hard_mode_level)
    normalized_class_name = _normalize_encounter_ranking_class_name(class_name)
    normalized_spec_name = _normalize_encounter_ranking_spec_name(spec_name)
    client = _client(ctx)
    try:
        encounter_payload = _resolve_encounter(
            ctx,
            client=client,
            zone_id=zone_id,
            boss_id=boss_id,
            boss_name=boss_name,
        )
        options = EncounterRankingsOptions(
            bracket=bracket,
            difficulty=difficulty,
            page=page,
            partition=partition,
            size=size,
            server_region=server_region,
            server_slug=server_slug,
            leaderboard=normalized_leaderboard,
            hard_mode_level=normalized_hard_mode_level,
            metric=metric,
            filter=filter_text,
            include_combatant_info=include_combatant_info,
            include_other_players=include_other_players,
            class_name=normalized_class_name,
            spec_name=normalized_spec_name,
        )
        rankings_payload = client.encounter_rankings(
            encounter_id=int(encounter_payload["id"]),
            options=options,
        )
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)
        return
    finally:
        client.close()
    rankings = rankings_payload.get("characterRankings")
    rankings_error = rankings.get("error") if isinstance(rankings, dict) and isinstance(rankings.get("error"), str) else None
    if rankings_error:
        _fail(ctx, "invalid_query", rankings_error)
    _emit(
        ctx,
        _encounter_rankings_payload(
            encounter=rankings_payload,
            rankings=rankings,
            query={
                "zone_id": zone_id,
                "boss_id": encounter_payload.get("id"),
                "boss_name": encounter_payload.get("name"),
                "bracket": bracket,
                "difficulty": difficulty,
                "class_name": class_name,
                "spec_name": spec_name,
                "metric": metric,
                "page": page,
                "partition": partition,
                "size": size,
                "server_region": normalize_region(server_region) if server_region else None,
                "server_slug": server_slug,
                "leaderboard": normalized_leaderboard,
                "hard_mode_level": normalized_hard_mode_level,
                "filter": filter_text,
                "include_combatant_info": include_combatant_info,
                "include_other_players": include_other_players,
                "top": top,
            },
            top=top,
        ),
        client=client,
    )


@app.command("guild")
def guild(
    ctx: typer.Context,
    region: str,
    realm: str,
    name: str,
    zone_id: int | None = typer.Option(None, "--zone-id", help="Optional Warcraft Logs zone ID for current guild ranking context."),
) -> None:
    client = _client(ctx)
    try:
        payload = client.guild(region=region, realm=realm, name=name, zone_id=zone_id)
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)
        return
    finally:
        client.close()
    _emit(
        ctx,
        {
            "ok": True,
            "provider": "warcraftlogs",
            "query": {"region": region, "realm": realm, "name": name, "zone_id": zone_id},
            "guild": _guild_payload(payload),
        },
        client=client,
    )


@app.command("guild-rankings")
def guild_rankings(
    ctx: typer.Context,
    region: str,
    realm: str,
    name: str,
    zone_id: int | None = typer.Option(None, "--zone-id", help="Optional Warcraft Logs zone ID."),
    size: int | None = typer.Option(None, "--size", help="Optional raid size."),
    difficulty: int | None = typer.Option(None, "--difficulty", help="Optional difficulty ID for speed ranks."),
) -> None:
    client = _client(ctx)
    try:
        payload = client.guild_rankings(region=region, realm=realm, name=name, zone_id=zone_id, size=size, difficulty=difficulty)
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)
        return
    finally:
        client.close()
    _emit(
        ctx,
        {
            "ok": True,
            "provider": "warcraftlogs",
            "query": {"region": region, "realm": realm, "name": name, "zone_id": zone_id, "size": size, "difficulty": difficulty},
            "guild_rankings": _guild_rankings_payload(payload),
        },
        client=client,
    )


@app.command("guild-members")
def guild_members(
    ctx: typer.Context,
    region: str,
    realm: str,
    name: str,
    limit: int = typer.Option(100, "--limit", min=1, max=100, help="Roster rows per page."),
    page: int = typer.Option(1, "--page", min=1, help="Page number."),
) -> None:
    client = _client(ctx)
    try:
        payload = client.guild_members(region=region, realm=realm, name=name, limit=limit, page=page)
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)
        return
    finally:
        client.close()
    _emit(
        ctx,
        {
            "ok": True,
            "provider": "warcraftlogs",
            "query": {"region": region, "realm": realm, "name": name, "limit": limit, "page": page},
            "guild_members": _guild_members_payload(payload),
            "notes": [
                "Guild roster queries only work for games where Warcraft Logs can verify guild membership.",
            ],
        },
        client=client,
    )


@app.command("guild-attendance")
def guild_attendance(
    ctx: typer.Context,
    region: str,
    realm: str,
    name: str,
    guild_tag_id: int | None = typer.Option(None, "--guild-tag-id", help="Optional guild tag filter."),
    limit: int = typer.Option(16, "--limit", min=1, max=25, help="Attendance rows per page."),
    page: int = typer.Option(1, "--page", min=1, help="Page number."),
    zone_id: int | None = typer.Option(None, "--zone-id", help="Optional zone filter."),
) -> None:
    client = _client(ctx)
    try:
        payload = client.guild_attendance(
            region=region,
            realm=realm,
            name=name,
            guild_tag_id=guild_tag_id,
            limit=limit,
            page=page,
            zone_id=zone_id,
        )
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)
        return
    finally:
        client.close()
    _emit(
        ctx,
        {
            "ok": True,
            "provider": "warcraftlogs",
            "query": {
                "region": region,
                "realm": realm,
                "name": name,
                "guild_tag_id": guild_tag_id,
                "limit": limit,
                "page": page,
                "zone_id": zone_id,
            },
            "guild_attendance": _guild_attendance_payload(payload),
        },
        client=client,
    )


@app.command("character")
def character(ctx: typer.Context, region: str, realm: str, name: str) -> None:
    client = _client(ctx)
    try:
        payload = client.character(region=region, realm=realm, name=name)
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)
        return
    finally:
        client.close()
    _emit(
        ctx,
        {
            "ok": True,
            "provider": "warcraftlogs",
            "query": {"region": region, "realm": realm, "name": name},
            "character": _character_payload(payload),
        },
        client=client,
    )


@app.command("character-rankings")
def character_rankings(
    ctx: typer.Context,
    region: str,
    realm: str,
    name: str,
    zone_id: int | None = typer.Option(None, "--zone-id", help="Optional Warcraft Logs zone ID."),
    difficulty: int | None = typer.Option(None, "--difficulty", help="Optional difficulty ID."),
    metric: str | None = typer.Option(None, "--metric", help="Optional ranking metric such as dps, hps, or tankhps."),
    size: int | None = typer.Option(None, "--size", help="Optional raid size."),
    spec_name: str | None = typer.Option(None, "--spec-name", help="Optional spec slug filter."),
    top: int = typer.Option(5, "--top", min=1, max=20, help="Number of top ranking rows to keep in the summary."),
) -> None:
    client = _client(ctx)
    try:
        payload = client.character_rankings(
            region=region,
            realm=realm,
            name=name,
            zone_id=zone_id,
            difficulty=difficulty,
            metric=metric,
            size=size,
            spec_name=spec_name,
        )
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)
        return
    finally:
        client.close()
    _emit(
        ctx,
        {
            "ok": True,
            "provider": "warcraftlogs",
            "query": {
                "region": region,
                "realm": realm,
                "name": name,
                "zone_id": zone_id,
                "difficulty": difficulty,
                "metric": metric,
                "size": size,
                "spec_name": spec_name,
                "top": top,
            },
            "character_rankings": _character_rankings_payload(payload, top=top),
        },
        client=client,
    )


@app.command("report")
def report(
    ctx: typer.Context,
    code: str,
    allow_unlisted: bool = typer.Option(False, "--allow-unlisted", help="Allow lookup of unlisted reports."),
) -> None:
    client = _client(ctx)
    try:
        payload = client.report(code=code, allow_unlisted=allow_unlisted)
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)
        return
    finally:
        client.close()
    _emit(
        ctx,
        {
            "ok": True,
            "provider": "warcraftlogs",
            "report": _report_payload(payload),
        },
        client=client,
    )


@app.command("reports")
def reports(
    ctx: typer.Context,
    guild_region: str | None = typer.Option(None, "--guild-region", help="Optional guild region for guild-scoped report queries."),
    guild_realm: str | None = typer.Option(None, "--guild-realm", help="Optional guild realm for guild-scoped report queries."),
    guild_name: str | None = typer.Option(None, "--guild-name", help="Optional guild name for guild-scoped report queries."),
    limit: int = typer.Option(25, "--limit", min=1, max=100, help="Reports per page."),
    page: int = typer.Option(1, "--page", min=1, help="Page number."),
    start_time: float | None = typer.Option(None, "--start-time", help="Optional report-range start time in milliseconds."),
    end_time: float | None = typer.Option(None, "--end-time", help="Optional report-range end time in milliseconds."),
    zone_id: int | None = typer.Option(None, "--zone-id", help="Optional Warcraft Logs zone filter."),
    game_zone_id: int | None = typer.Option(None, "--game-zone-id", help="Optional game zone filter."),
) -> None:
    client = _client(ctx)
    try:
        payload = client.reports(
            guild_region=guild_region,
            guild_realm=guild_realm,
            guild_name=guild_name,
            limit=limit,
            page=page,
            start_time=start_time,
            end_time=end_time,
            zone_id=zone_id,
            game_zone_id=game_zone_id,
        )
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)
        return
    finally:
        client.close()
    report_payload = _reports_payload(payload)
    _emit(
        ctx,
        {
            "ok": True,
            "provider": "warcraftlogs",
            "query": {
                "guild_region": guild_region,
                "guild_realm": guild_realm,
                "guild_name": guild_name,
                "limit": limit,
                "page": page,
                "start_time": start_time,
                "end_time": end_time,
                "zone_id": zone_id,
                "game_zone_id": game_zone_id,
            },
            "count": len(report_payload["reports"]),
            **report_payload,
        },
        client=client,
    )


@app.command("guild-reports")
def guild_reports(
    ctx: typer.Context,
    region: str,
    realm: str,
    name: str,
    limit: int = typer.Option(25, "--limit", min=1, max=100, help="Reports per page."),
    page: int = typer.Option(1, "--page", min=1, help="Page number."),
    start_time: float | None = typer.Option(None, "--start-time", help="Optional report-range start time in milliseconds."),
    end_time: float | None = typer.Option(None, "--end-time", help="Optional report-range end time in milliseconds."),
    zone_id: int | None = typer.Option(None, "--zone-id", help="Optional Warcraft Logs zone filter."),
    game_zone_id: int | None = typer.Option(None, "--game-zone-id", help="Optional game zone filter."),
) -> None:
    client = _client(ctx)
    try:
        payload = client.reports(
            guild_region=region,
            guild_realm=realm,
            guild_name=name,
            limit=limit,
            page=page,
            start_time=start_time,
            end_time=end_time,
            zone_id=zone_id,
            game_zone_id=game_zone_id,
        )
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)
        return
    finally:
        client.close()
    report_payload = _reports_payload(payload)
    _emit(
        ctx,
        {
            "ok": True,
            "provider": "warcraftlogs",
            "query": {
                "region": region,
                "realm": realm,
                "name": name,
                "limit": limit,
                "page": page,
                "start_time": start_time,
                "end_time": end_time,
                "zone_id": zone_id,
                "game_zone_id": game_zone_id,
            },
            "guild": {"region": region, "realm": realm, "name": name},
            "count": len(report_payload["reports"]),
            **report_payload,
        },
        client=client,
    )


def _cross_report_query(
    *,
    zone_id: int,
    boss_id: int | None,
    boss_name: str | None,
    difficulty: int | None,
    spec_name: str | None,
    kill_time_min: float | None,
    kill_time_max: float | None,
    top: int,
    report_pages: int,
    reports_per_page: int,
    start_time: float | None,
    end_time: float | None,
    guild_region: str | None,
    guild_realm: str | None,
    guild_name: str | None,
) -> dict[str, Any]:
    return {
        "zone_id": zone_id,
        "boss_id": boss_id,
        "boss_name": boss_name,
        "difficulty": difficulty,
        "spec_name": spec_name,
        "kill_time_min": kill_time_min,
        "kill_time_max": kill_time_max,
        "top": top,
        "report_pages": report_pages,
        "reports_per_page": reports_per_page,
        "start_time": start_time,
        "end_time": end_time,
        "guild_region": guild_region,
        "guild_realm": guild_realm,
        "guild_name": guild_name,
    }


def _require_boss_scope(ctx: typer.Context, *, boss_id: int | None, boss_name: str | None) -> None:
    if boss_id is None and not boss_name:
        _fail(ctx, "missing_boss", "Provide --boss-id or --boss-name for cross-report boss analytics.")


@app.command("boss-kills")
def boss_kills(
    ctx: typer.Context,
    zone_id: int = typer.Option(..., "--zone-id", help="Warcraft Logs zone ID to sample reports from."),
    boss_id: int | None = typer.Option(None, "--boss-id", help="Encounter ID to match."),
    boss_name: str | None = typer.Option(None, "--boss-name", help="Boss name to match within sampled fights."),
    difficulty: int | None = typer.Option(None, "--difficulty", help="Optional difficulty ID filter."),
    spec_name: str | None = typer.Option(
        None,
        "--spec-name",
        help="Optional sampled participant spec filter applied before ranking sampled kills.",
    ),
    kill_time_min: float | None = typer.Option(None, "--kill-time-min", help="Optional minimum kill time in seconds."),
    kill_time_max: float | None = typer.Option(None, "--kill-time-max", help="Optional maximum kill time in seconds."),
    top: int = typer.Option(10, "--top", min=1, max=100, help="Maximum returned kill rows after ranking."),
    report_pages: int = typer.Option(1, "--report-pages", min=1, max=10, help="How many report-list pages to sample."),
    reports_per_page: int = typer.Option(25, "--reports-per-page", min=1, max=100, help="Reports to fetch per sampled page."),
    start_time: float | None = typer.Option(None, "--start-time", help="Optional report-range start time in milliseconds."),
    end_time: float | None = typer.Option(None, "--end-time", help="Optional report-range end time in milliseconds."),
    guild_region: str | None = typer.Option(None, "--guild-region", help="Optional guild-region scope for report discovery."),
    guild_realm: str | None = typer.Option(None, "--guild-realm", help="Optional guild-realm scope for report discovery."),
    guild_name: str | None = typer.Option(None, "--guild-name", help="Optional guild-name scope for report discovery."),
) -> None:
    _require_boss_scope(ctx, boss_id=boss_id, boss_name=boss_name)
    client = _client(ctx)
    try:
        analytics = _collect_boss_kill_rows(
            client=client,
            zone_id=zone_id,
            boss_id=boss_id,
            boss_name=boss_name,
            difficulty=difficulty,
            spec_name=spec_name,
            kill_time_min=kill_time_min,
            kill_time_max=kill_time_max,
            report_pages=report_pages,
            reports_per_page=reports_per_page,
            start_time=start_time,
            end_time=end_time,
            guild_region=guild_region,
            guild_realm=guild_realm,
            guild_name=guild_name,
        )
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)
        return
    finally:
        client.close()
    _emit(
        ctx,
        _boss_kills_payload(
            kind="boss_kills",
            rows=analytics["rows"],
            sample=analytics["sample"],
            query=_cross_report_query(
                zone_id=zone_id,
                boss_id=boss_id,
                boss_name=boss_name,
                difficulty=difficulty,
                spec_name=spec_name,
                kill_time_min=kill_time_min,
                kill_time_max=kill_time_max,
                top=top,
                report_pages=report_pages,
                reports_per_page=reports_per_page,
                start_time=start_time,
                end_time=end_time,
                guild_region=guild_region,
                guild_realm=guild_realm,
                guild_name=guild_name,
            ),
            top=top,
        ),
        client=client,
    )


@app.command("top-kills")
def top_kills(
    ctx: typer.Context,
    zone_id: int = typer.Option(..., "--zone-id", help="Warcraft Logs zone ID to sample reports from."),
    boss_id: int | None = typer.Option(None, "--boss-id", help="Encounter ID to match."),
    boss_name: str | None = typer.Option(None, "--boss-name", help="Boss name to match within sampled fights."),
    difficulty: int | None = typer.Option(None, "--difficulty", help="Optional difficulty ID filter."),
    spec_name: str | None = typer.Option(
        None,
        "--spec-name",
        help="Optional sampled participant spec filter applied before ranking sampled kills.",
    ),
    kill_time_min: float | None = typer.Option(None, "--kill-time-min", help="Optional minimum kill time in seconds."),
    kill_time_max: float | None = typer.Option(None, "--kill-time-max", help="Optional maximum kill time in seconds."),
    top: int = typer.Option(10, "--top", min=1, max=100, help="Maximum returned kill rows after ranking."),
    report_pages: int = typer.Option(1, "--report-pages", min=1, max=10, help="How many report-list pages to sample."),
    reports_per_page: int = typer.Option(25, "--reports-per-page", min=1, max=100, help="Reports to fetch per sampled page."),
    start_time: float | None = typer.Option(None, "--start-time", help="Optional report-range start time in milliseconds."),
    end_time: float | None = typer.Option(None, "--end-time", help="Optional report-range end time in milliseconds."),
    guild_region: str | None = typer.Option(None, "--guild-region", help="Optional guild-region scope for report discovery."),
    guild_realm: str | None = typer.Option(None, "--guild-realm", help="Optional guild-realm scope for report discovery."),
    guild_name: str | None = typer.Option(None, "--guild-name", help="Optional guild-name scope for report discovery."),
) -> None:
    _require_boss_scope(ctx, boss_id=boss_id, boss_name=boss_name)
    client = _client(ctx)
    try:
        analytics = _collect_boss_kill_rows(
            client=client,
            zone_id=zone_id,
            boss_id=boss_id,
            boss_name=boss_name,
            difficulty=difficulty,
            spec_name=spec_name,
            kill_time_min=kill_time_min,
            kill_time_max=kill_time_max,
            report_pages=report_pages,
            reports_per_page=reports_per_page,
            start_time=start_time,
            end_time=end_time,
            guild_region=guild_region,
            guild_realm=guild_realm,
            guild_name=guild_name,
        )
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)
        return
    finally:
        client.close()
    _emit(
        ctx,
        _boss_kills_payload(
            kind="top_kills",
            rows=analytics["rows"],
            sample=analytics["sample"],
            query=_cross_report_query(
                zone_id=zone_id,
                boss_id=boss_id,
                boss_name=boss_name,
                difficulty=difficulty,
                spec_name=spec_name,
                kill_time_min=kill_time_min,
                kill_time_max=kill_time_max,
                top=top,
                report_pages=report_pages,
                reports_per_page=reports_per_page,
                start_time=start_time,
                end_time=end_time,
                guild_region=guild_region,
                guild_realm=guild_realm,
                guild_name=guild_name,
            ),
            top=top,
        ),
        client=client,
    )


@app.command("boss-spec-usage")
def boss_spec_usage(
    ctx: typer.Context,
    zone_id: int = typer.Option(..., "--zone-id", help="Warcraft Logs zone ID to sample reports from."),
    boss_id: int | None = typer.Option(None, "--boss-id", help="Encounter ID to match."),
    boss_name: str | None = typer.Option(None, "--boss-name", help="Boss name to match within sampled fights."),
    difficulty: int | None = typer.Option(None, "--difficulty", help="Optional difficulty ID filter."),
    spec_name: str | None = typer.Option(
        None,
        "--spec-name",
        help="Optional sampled participant spec filter applied before aggregation.",
    ),
    kill_time_min: float | None = typer.Option(None, "--kill-time-min", help="Optional minimum kill time in seconds."),
    kill_time_max: float | None = typer.Option(None, "--kill-time-max", help="Optional maximum kill time in seconds."),
    top: int = typer.Option(10, "--top", min=1, max=100, help="Maximum returned spec rows after ranking."),
    report_pages: int = typer.Option(1, "--report-pages", min=1, max=10, help="How many report-list pages to sample."),
    reports_per_page: int = typer.Option(25, "--reports-per-page", min=1, max=100, help="Reports to fetch per sampled page."),
    start_time: float | None = typer.Option(None, "--start-time", help="Optional report-range start time in milliseconds."),
    end_time: float | None = typer.Option(None, "--end-time", help="Optional report-range end time in milliseconds."),
    guild_region: str | None = typer.Option(None, "--guild-region", help="Optional guild-region scope for report discovery."),
    guild_realm: str | None = typer.Option(None, "--guild-realm", help="Optional guild-realm scope for report discovery."),
    guild_name: str | None = typer.Option(None, "--guild-name", help="Optional guild-name scope for report discovery."),
) -> None:
    _require_boss_scope(ctx, boss_id=boss_id, boss_name=boss_name)
    client = _client(ctx)
    try:
        analytics = _collect_boss_kill_rows(
            client=client,
            zone_id=zone_id,
            boss_id=boss_id,
            boss_name=boss_name,
            difficulty=difficulty,
            spec_name=spec_name,
            kill_time_min=kill_time_min,
            kill_time_max=kill_time_max,
            report_pages=report_pages,
            reports_per_page=reports_per_page,
            start_time=start_time,
            end_time=end_time,
            guild_region=guild_region,
            guild_realm=guild_realm,
            guild_name=guild_name,
        )
        enriched_rows: list[dict[str, Any]] = []
        for row in analytics["rows"]:
            report_payload = row.get("report") if isinstance(row.get("report"), dict) else {}
            fight_payload = row.get("fight") if isinstance(row.get("fight"), dict) else {}
            code = str(report_payload.get("code") or "")
            fight_id_value = fight_payload.get("id")
            encounter_id_value = fight_payload.get("encounter_id")
            detail_report = client.report_player_details(
                code=code,
                allow_unlisted=False,
                options=ReportPlayerDetailsOptions(
                    difficulty=int(fight_payload["difficulty"]) if isinstance(fight_payload.get("difficulty"), int) else difficulty,
                    encounter_id=int(encounter_id_value) if isinstance(encounter_id_value, int) else None,
                    fight_ids=[int(fight_id_value)] if isinstance(fight_id_value, int) else None,
                    include_combatant_info=True,
                    kill_type="Kills",
                ),
                ttl_override=client._guild_ttl,
            )
            enriched_rows.append({**row, "player_details": _all_player_detail_rows(detail_report)})
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)
        return
    finally:
        client.close()
    _emit(
        ctx,
        _boss_spec_usage_payload(
            rows=enriched_rows,
            sample=analytics["sample"],
            query=_cross_report_query(
                zone_id=zone_id,
                boss_id=boss_id,
                boss_name=boss_name,
                difficulty=difficulty,
                spec_name=spec_name,
                kill_time_min=kill_time_min,
                kill_time_max=kill_time_max,
                top=top,
                report_pages=report_pages,
                reports_per_page=reports_per_page,
                start_time=start_time,
                end_time=end_time,
                guild_region=guild_region,
                guild_realm=guild_realm,
                guild_name=guild_name,
            ),
            top=top,
        ),
        client=client,
    )


@app.command("ability-usage-summary")
def ability_usage_summary(
    ctx: typer.Context,
    zone_id: int = typer.Option(..., "--zone-id", help="Warcraft Logs zone ID to sample reports from."),
    ability_id: int = typer.Option(..., "--ability-id", help="Ability game ID to summarize across the sampled kill cohort."),
    boss_id: int | None = typer.Option(None, "--boss-id", help="Encounter ID to match."),
    boss_name: str | None = typer.Option(None, "--boss-name", help="Boss name to match within sampled fights."),
    difficulty: int | None = typer.Option(None, "--difficulty", help="Optional difficulty ID filter."),
    spec_name: str | None = typer.Option(
        None,
        "--spec-name",
        help="Optional sampled participant spec filter applied before aggregation.",
    ),
    kill_time_min: float | None = typer.Option(None, "--kill-time-min", help="Optional minimum kill time in seconds."),
    kill_time_max: float | None = typer.Option(None, "--kill-time-max", help="Optional maximum kill time in seconds."),
    preview_limit: int = typer.Option(10, "--preview-limit", min=1, max=100, help="Maximum sampled kill rows to include in the preview payload."),
    event_limit: int = typer.Option(200, "--event-limit", min=1, max=5000, help="Maximum cast events to request per sampled kill."),
    report_pages: int = typer.Option(1, "--report-pages", min=1, max=10, help="How many report-list pages to sample."),
    reports_per_page: int = typer.Option(25, "--reports-per-page", min=1, max=100, help="Reports to fetch per sampled page."),
    start_time: float | None = typer.Option(None, "--start-time", help="Optional report-range start time in milliseconds."),
    end_time: float | None = typer.Option(None, "--end-time", help="Optional report-range end time in milliseconds."),
    guild_region: str | None = typer.Option(None, "--guild-region", help="Optional guild-region scope for report discovery."),
    guild_realm: str | None = typer.Option(None, "--guild-realm", help="Optional guild-realm scope for report discovery."),
    guild_name: str | None = typer.Option(None, "--guild-name", help="Optional guild-name scope for report discovery."),
) -> None:
    _require_boss_scope(ctx, boss_id=boss_id, boss_name=boss_name)
    client = _client(ctx)
    try:
        analytics = _collect_ability_usage_rows(
            client=client,
            zone_id=zone_id,
            boss_id=boss_id,
            boss_name=boss_name,
            difficulty=difficulty,
            spec_name=spec_name,
            kill_time_min=kill_time_min,
            kill_time_max=kill_time_max,
            report_pages=report_pages,
            reports_per_page=reports_per_page,
            start_time=start_time,
            end_time=end_time,
            guild_region=guild_region,
            guild_realm=guild_realm,
            guild_name=guild_name,
            ability_id=ability_id,
            event_limit=event_limit,
        )
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)
        return
    finally:
        client.close()
    _emit(
        ctx,
        _ability_usage_summary_payload(
            rows=analytics["rows"],
            sample=analytics["sample"],
            query={
                "zone_id": zone_id,
                "ability_id": ability_id,
                "boss_id": boss_id,
                "boss_name": boss_name,
                "difficulty": difficulty,
                "spec_name": spec_name,
                "kill_time_min": kill_time_min,
                "kill_time_max": kill_time_max,
                "report_pages": report_pages,
                "reports_per_page": reports_per_page,
                "start_time": start_time,
                "end_time": end_time,
                "guild_region": guild_region,
                "guild_realm": guild_realm,
                "guild_name": guild_name,
            },
            ability=analytics["ability"],
            preview_limit=preview_limit,
            event_limit=event_limit,
        ),
        client=client,
    )


@app.command("comp-samples")
def comp_samples(
    ctx: typer.Context,
    zone_id: int = typer.Option(..., "--zone-id", help="Warcraft Logs zone ID to sample reports from."),
    boss_id: int | None = typer.Option(None, "--boss-id", help="Encounter ID to match."),
    boss_name: str | None = typer.Option(None, "--boss-name", help="Boss name to match within sampled fights."),
    difficulty: int | None = typer.Option(None, "--difficulty", help="Optional difficulty ID filter."),
    spec_name: str | None = typer.Option(
        None,
        "--spec-name",
        help="Optional sampled participant spec filter applied before aggregation.",
    ),
    kill_time_min: float | None = typer.Option(None, "--kill-time-min", help="Optional minimum kill time in seconds."),
    kill_time_max: float | None = typer.Option(None, "--kill-time-max", help="Optional maximum kill time in seconds."),
    top: int = typer.Option(10, "--top", min=1, max=100, help="Maximum returned sampled kill rows after ranking."),
    report_pages: int = typer.Option(1, "--report-pages", min=1, max=10, help="How many report-list pages to sample."),
    reports_per_page: int = typer.Option(25, "--reports-per-page", min=1, max=100, help="Reports to fetch per sampled page."),
    start_time: float | None = typer.Option(None, "--start-time", help="Optional report-range start time in milliseconds."),
    end_time: float | None = typer.Option(None, "--end-time", help="Optional report-range end time in milliseconds."),
    guild_region: str | None = typer.Option(None, "--guild-region", help="Optional guild-region scope for report discovery."),
    guild_realm: str | None = typer.Option(None, "--guild-realm", help="Optional guild-realm scope for report discovery."),
    guild_name: str | None = typer.Option(None, "--guild-name", help="Optional guild-name scope for report discovery."),
) -> None:
    _require_boss_scope(ctx, boss_id=boss_id, boss_name=boss_name)
    client = _client(ctx)
    try:
        analytics = _collect_comp_sample_rows(
            client=client,
            zone_id=zone_id,
            boss_id=boss_id,
            boss_name=boss_name,
            difficulty=difficulty,
            spec_name=spec_name,
            kill_time_min=kill_time_min,
            kill_time_max=kill_time_max,
            report_pages=report_pages,
            reports_per_page=reports_per_page,
            start_time=start_time,
            end_time=end_time,
            guild_region=guild_region,
            guild_realm=guild_realm,
            guild_name=guild_name,
        )
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)
        return
    finally:
        client.close()
    _emit(
        ctx,
        _comp_samples_payload(
            rows=analytics["rows"],
            sample=analytics["sample"],
            query=_cross_report_query(
                zone_id=zone_id,
                boss_id=boss_id,
                boss_name=boss_name,
                difficulty=difficulty,
                spec_name=spec_name,
                kill_time_min=kill_time_min,
                kill_time_max=kill_time_max,
                top=top,
                report_pages=report_pages,
                reports_per_page=reports_per_page,
                start_time=start_time,
                end_time=end_time,
                guild_region=guild_region,
                guild_realm=guild_realm,
                guild_name=guild_name,
            ),
            top=top,
        ),
        client=client,
    )


@app.command("report-encounter")
def report_encounter(
    ctx: typer.Context,
    reference: str,
    fight_id: int | None = typer.Option(None, "--fight-id", help="Override or supply a fight ID when the report reference does not include one."),
    allow_unlisted: bool = typer.Option(False, "--allow-unlisted", help="Allow lookup of unlisted reports."),
) -> None:
    client = _client(ctx)
    try:
        ref, report, fight, encounter = _resolve_encounter_scope(
            ctx,
            client=client,
            reference=reference,
            fight_id=fight_id,
            allow_unlisted=allow_unlisted,
        )
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)
        return
    finally:
        client.close()
    _emit(
        ctx,
        {
            "ok": True,
            "provider": "warcraftlogs",
            "kind": "report_encounter",
            **_encounter_summary_payload(ref=ref, report=report, fight=fight, encounter=encounter),
        },
        client=client,
    )


@app.command("report-encounter-players")
def report_encounter_players(
    ctx: typer.Context,
    reference: str,
    fight_id: int | None = typer.Option(None, "--fight-id", help="Override or supply a fight ID when the report reference does not include one."),
    include_combatant_info: bool | None = typer.Option(
        None,
        "--include-combatant-info/--no-include-combatant-info",
        help="Optional combatant detail toggle.",
    ),
    translate: bool | None = typer.Option(None, "--translate/--no-translate", help="Optional translation toggle."),
    allow_unlisted: bool = typer.Option(False, "--allow-unlisted", help="Allow lookup of unlisted reports."),
) -> None:
    client = _client(ctx)
    try:
        ref, report, fight, encounter = _resolve_encounter_scope(
            ctx,
            client=client,
            reference=reference,
            fight_id=fight_id,
            allow_unlisted=allow_unlisted,
        )
        payload = client.report_player_details(
            code=ref.code,
            allow_unlisted=allow_unlisted,
            options=ReportPlayerDetailsOptions(
                encounter_id=fight.get("encounterID") if isinstance(fight.get("encounterID"), int) else None,
                fight_ids=[int(fight["id"])] if isinstance(fight.get("id"), int) else None,
                include_combatant_info=include_combatant_info,
                kill_type=_kill_type_for_fight(fight),
                translate=translate,
            ),
        )
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)
        return
    finally:
        client.close()
    _emit(
        ctx,
        {
            "ok": True,
            "provider": "warcraftlogs",
            "kind": "report_encounter_players",
            **_encounter_summary_payload(ref=ref, report=report, fight=fight, encounter=encounter),
            **_report_player_details_payload(
                payload,
                report_code=ref.code,
                fight_id=fight.get("id") if isinstance(fight.get("id"), int) else None,
            ),
        },
        client=client,
    )


@app.command("report-player-talents")
def report_player_talents(
    ctx: typer.Context,
    reference: str,
    actor_id: int = typer.Option(..., "--actor-id", help="Report-local actor ID scoped to the selected fight."),
    fight_id: int | None = typer.Option(None, "--fight-id", help="Override or supply a fight ID when the report reference does not include one."),
    allow_unlisted: bool = typer.Option(False, "--allow-unlisted", help="Allow lookup of unlisted reports."),
    out: str | None = typer.Option(None, "--out", help="Optional path to write the scoped talent transport packet JSON."),
) -> None:
    client = _client(ctx)
    try:
        ref, report, fight, encounter = _resolve_encounter_scope(
            ctx,
            client=client,
            reference=reference,
            fight_id=fight_id,
            allow_unlisted=allow_unlisted,
        )
        payload = client.report_player_details(
            code=ref.code,
            allow_unlisted=allow_unlisted,
            options=ReportPlayerDetailsOptions(
                encounter_id=fight.get("encounterID") if isinstance(fight.get("encounterID"), int) else None,
                fight_ids=[int(fight["id"])] if isinstance(fight.get("id"), int) else None,
                include_combatant_info=True,
                kill_type=_kill_type_for_fight(fight),
            ),
        )
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)
        return
    finally:
        client.close()

    details_payload = _report_player_details_payload(
        payload,
        report_code=ref.code,
        fight_id=fight.get("id") if isinstance(fight.get("id"), int) else None,
    )
    actor = _player_detail_actor(details_payload, actor_id)
    if not isinstance(actor, dict):
        _fail(ctx, "not_found", f"Actor ID {actor_id} was not present in the selected fight.")
        return

    talent_rows, had_invalid_talent_rows = _normalized_talent_tree_rows(actor)
    if not talent_rows:
        _fail(ctx, "missing_talent_tree", f"Actor ID {actor_id} did not include combatant_info.talentTree in the selected fight.")
        return
    if had_invalid_talent_rows:
        _fail(
            ctx,
            "missing_talent_tree",
            f"Actor ID {actor_id} included incomplete combatant_info.talentTree rows in the selected fight.",
        )
        return

    transport_packet = _validated_transport_packet(
        ctx,
        _player_talent_transport_packet(
            actor,
            report_code=ref.code,
            fight_id=int(fight["id"]),
            actor_id=actor_id,
            raw_rows=talent_rows,
        ),
        command_name="warcraftlogs report-player-talents",
    )
    try:
        written_packet_path = _write_transport_packet_json(out, transport_packet)
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)
        return

    _emit(
        ctx,
        {
            "ok": True,
            "provider": "warcraftlogs",
            "kind": "report_player_talents",
            **_encounter_summary_payload(ref=ref, report=report, fight=fight, encounter=encounter),
            "player": actor,
            "talent_transport_packet": transport_packet,
            "written_packet_path": written_packet_path,
        },
        client=client,
    )


@app.command("report-encounter-casts")
def report_encounter_casts(
    ctx: typer.Context,
    reference: str,
    fight_id: int | None = typer.Option(None, "--fight-id", help="Override or supply a fight ID when the report reference does not include one."),
    source_id: int | None = typer.Option(None, "--source-id", help="Optional source actor filter."),
    target_id: int | None = typer.Option(None, "--target-id", help="Optional target actor filter."),
    ability_id: float | None = typer.Option(None, "--ability-id", help="Optional ability game ID filter."),
    hostility_type: str | None = typer.Option(None, "--hostility-type", help="Optional hostility filter."),
    limit: int = typer.Option(200, "--limit", min=1, max=10000, help="Maximum cast events to request from Warcraft Logs."),
    preview_limit: int = typer.Option(20, "--preview-limit", min=1, max=200, help="Maximum preview cast rows to return."),
    window_start_ms: float | None = typer.Option(None, "--window-start-ms", help="Optional encounter-relative start offset in milliseconds."),
    window_end_ms: float | None = typer.Option(None, "--window-end-ms", help="Optional encounter-relative end offset in milliseconds."),
    translate: bool | None = typer.Option(None, "--translate/--no-translate", help="Optional translation toggle."),
    allow_unlisted: bool = typer.Option(False, "--allow-unlisted", help="Allow lookup of unlisted reports."),
) -> None:
    client = _client(ctx)
    try:
        ref, report, fight, encounter = _resolve_encounter_scope(
            ctx,
            client=client,
            reference=reference,
            fight_id=fight_id,
            allow_unlisted=allow_unlisted,
        )
        normalized_hostility_type = _normalize_graphql_enum(hostility_type)
        options, query = _encounter_filter_options(
            ctx,
            fight=fight,
            ability_id=ability_id,
            data_type="Casts",
            source_id=source_id,
            target_id=target_id,
            hostility_type=normalized_hostility_type,
            translate=translate,
            limit=limit,
            window_start_ms=window_start_ms,
            window_end_ms=window_end_ms,
        )
        events_report = client.report_events(
            code=ref.code,
            allow_unlisted=allow_unlisted,
            options=options,
        )
        master_report = client.report_master_data(code=ref.code, allow_unlisted=allow_unlisted, actor_type="Player")
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)
        return
    finally:
        client.close()
    _emit(
        ctx,
        {
            "ok": True,
            "provider": "warcraftlogs",
            "kind": "report_encounter_casts",
            "query": {
                "preview_limit": preview_limit,
                **query,
            },
            **_encounter_summary_payload(ref=ref, report=report, fight=fight, encounter=encounter),
            **_encounter_cast_rows_payload(
                report=report,
                fight=fight,
                events_report=events_report,
                master_report=master_report,
                preview_limit=preview_limit,
            ),
        },
        client=client,
    )


@app.command("report-encounter-buffs")
def report_encounter_buffs(
    ctx: typer.Context,
    reference: str,
    fight_id: int | None = typer.Option(None, "--fight-id", help="Override or supply a fight ID when the report reference does not include one."),
    source_id: int | None = typer.Option(None, "--source-id", help="Optional source actor filter."),
    target_id: int | None = typer.Option(None, "--target-id", help="Optional target actor filter."),
    ability_id: float | None = typer.Option(None, "--ability-id", help="Optional ability game ID filter."),
    hostility_type: str | None = typer.Option(None, "--hostility-type", help="Optional hostility filter."),
    view_by: str | None = typer.Option("source", "--view-by", help="Optional table view grouping."),
    wipe_cutoff: int | None = typer.Option(None, "--wipe-cutoff", help="Optional wipe cutoff."),
    preview_limit: int = typer.Option(20, "--preview-limit", min=1, max=200, help="Maximum preview buff rows to return."),
    window_start_ms: float | None = typer.Option(None, "--window-start-ms", help="Optional encounter-relative start offset in milliseconds."),
    window_end_ms: float | None = typer.Option(None, "--window-end-ms", help="Optional encounter-relative end offset in milliseconds."),
    translate: bool | None = typer.Option(None, "--translate/--no-translate", help="Optional translation toggle."),
    allow_unlisted: bool = typer.Option(False, "--allow-unlisted", help="Allow lookup of unlisted reports."),
) -> None:
    client = _client(ctx)
    try:
        ref, report, fight, encounter = _resolve_encounter_scope(
            ctx,
            client=client,
            reference=reference,
            fight_id=fight_id,
            allow_unlisted=allow_unlisted,
        )
        normalized_hostility_type = _normalize_graphql_enum(hostility_type)
        normalized_view_by = _normalize_graphql_enum(view_by)
        options, query = _encounter_filter_options(
            ctx,
            fight=fight,
            ability_id=ability_id,
            data_type="Buffs",
            source_id=source_id,
            target_id=target_id,
            hostility_type=normalized_hostility_type,
            translate=translate,
            view_by=normalized_view_by,
            wipe_cutoff=wipe_cutoff,
            window_start_ms=window_start_ms,
            window_end_ms=window_end_ms,
        )
        table_report = client.report_table(code=ref.code, allow_unlisted=allow_unlisted, options=options)
        master_report = client.report_master_data(code=ref.code, allow_unlisted=allow_unlisted, actor_type="Player")
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)
        return
    finally:
        client.close()
    _emit(
        ctx,
        {
            "ok": True,
            "provider": "warcraftlogs",
            "kind": "report_encounter_buffs",
            "query": {
                "preview_limit": preview_limit,
                **query,
            },
            **_encounter_summary_payload(ref=ref, report=report, fight=fight, encounter=encounter),
            **_encounter_buff_rows_payload(
                report=report,
                fight=fight,
                table_report=table_report,
                master_report=master_report,
                preview_limit=preview_limit,
                view_by=normalized_view_by,
            ),
        },
        client=client,
    )


@app.command("report-encounter-aura-summary")
def report_encounter_aura_summary(
    ctx: typer.Context,
    reference: str,
    ability_id: int = typer.Option(..., "--ability-id", help="Required aura ability game ID."),
    fight_id: int | None = typer.Option(None, "--fight-id", help="Override or supply a fight ID when the report reference does not include one."),
    source_id: int | None = typer.Option(None, "--source-id", help="Optional source actor filter."),
    target_id: int | None = typer.Option(None, "--target-id", help="Optional target actor filter."),
    hostility_type: str | None = typer.Option(None, "--hostility-type", help="Optional hostility filter."),
    wipe_cutoff: int | None = typer.Option(None, "--wipe-cutoff", help="Optional wipe cutoff."),
    window_start_ms: float | None = typer.Option(None, "--window-start-ms", help="Optional encounter-relative start offset in milliseconds."),
    window_end_ms: float | None = typer.Option(None, "--window-end-ms", help="Optional encounter-relative end offset in milliseconds."),
    translate: bool | None = typer.Option(None, "--translate/--no-translate", help="Optional translation toggle."),
    allow_unlisted: bool = typer.Option(False, "--allow-unlisted", help="Allow lookup of unlisted reports."),
) -> None:
    client = _client(ctx)
    try:
        ref, report, fight, encounter = _resolve_encounter_scope(
            ctx,
            client=client,
            reference=reference,
            fight_id=fight_id,
            allow_unlisted=allow_unlisted,
        )
        normalized_hostility_type = _normalize_graphql_enum(hostility_type)
        options, query = _encounter_filter_options(
            ctx,
            fight=fight,
            ability_id=float(ability_id),
            data_type="Buffs",
            source_id=source_id,
            target_id=target_id,
            hostility_type=normalized_hostility_type,
            translate=translate,
            view_by="Source",
            wipe_cutoff=wipe_cutoff,
            window_start_ms=window_start_ms,
            window_end_ms=window_end_ms,
        )
        table_payload = client.report_table(code=ref.code, allow_unlisted=allow_unlisted, options=options)
        master_report = client.report_master_data(code=ref.code, allow_unlisted=allow_unlisted, actor_type="Player")
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)
        return
    finally:
        client.close()
    _emit(
        ctx,
        {
            "ok": True,
            "provider": "warcraftlogs",
            "kind": "report_encounter_aura_summary",
            "query": query,
            **_encounter_summary_payload(ref=ref, report=report, fight=fight, encounter=encounter),
            **_report_encounter_aura_summary_payload(
                report=report,
                fight=fight,
                table_report=table_payload,
                master_report=master_report,
                ability_id=ability_id,
            ),
        },
        client=client,
    )


@app.command("report-encounter-aura-compare")
def report_encounter_aura_compare(
    ctx: typer.Context,
    reference: str,
    ability_id: int = typer.Option(..., "--ability-id", help="Required aura ability game ID."),
    fight_id: int | None = typer.Option(None, "--fight-id", help="Override or supply a fight ID when the report reference does not include one."),
    left_window_start_ms: float | None = typer.Option(None, "--left-window-start-ms", help="Encounter-relative start offset for the left comparison window."),
    left_window_end_ms: float | None = typer.Option(None, "--left-window-end-ms", help="Encounter-relative end offset for the left comparison window."),
    right_window_start_ms: float | None = typer.Option(None, "--right-window-start-ms", help="Encounter-relative start offset for the right comparison window."),
    right_window_end_ms: float | None = typer.Option(None, "--right-window-end-ms", help="Encounter-relative end offset for the right comparison window."),
    left_label: str = typer.Option("left", "--left-label", help="Label for the left comparison window."),
    right_label: str = typer.Option("right", "--right-label", help="Label for the right comparison window."),
    source_id: int | None = typer.Option(None, "--source-id", help="Optional source actor filter applied to both windows."),
    target_id: int | None = typer.Option(None, "--target-id", help="Optional target actor filter applied to both windows."),
    hostility_type: str | None = typer.Option(None, "--hostility-type", help="Optional hostility filter applied to both windows."),
    wipe_cutoff: int | None = typer.Option(None, "--wipe-cutoff", help="Optional wipe cutoff applied to both windows."),
    translate: bool | None = typer.Option(None, "--translate/--no-translate", help="Optional translation toggle."),
    allow_unlisted: bool = typer.Option(False, "--allow-unlisted", help="Allow lookup of unlisted reports."),
) -> None:
    _require_explicit_window(ctx, name="--left-window", start_ms=left_window_start_ms, end_ms=left_window_end_ms)
    _require_explicit_window(ctx, name="--right-window", start_ms=right_window_start_ms, end_ms=right_window_end_ms)
    client = _client(ctx)
    try:
        ref, report, fight, encounter = _resolve_encounter_scope(
            ctx,
            client=client,
            reference=reference,
            fight_id=fight_id,
            allow_unlisted=allow_unlisted,
        )
        normalized_hostility_type = _normalize_graphql_enum(hostility_type)
        left_options, left_query = _encounter_filter_options(
            ctx,
            fight=fight,
            ability_id=float(ability_id),
            data_type="Buffs",
            source_id=source_id,
            target_id=target_id,
            hostility_type=normalized_hostility_type,
            translate=translate,
            view_by="Source",
            wipe_cutoff=wipe_cutoff,
            window_start_ms=left_window_start_ms,
            window_end_ms=left_window_end_ms,
        )
        right_options, right_query = _encounter_filter_options(
            ctx,
            fight=fight,
            ability_id=float(ability_id),
            data_type="Buffs",
            source_id=source_id,
            target_id=target_id,
            hostility_type=normalized_hostility_type,
            translate=translate,
            view_by="Source",
            wipe_cutoff=wipe_cutoff,
            window_start_ms=right_window_start_ms,
            window_end_ms=right_window_end_ms,
        )
        left_table = client.report_table(code=ref.code, allow_unlisted=allow_unlisted, options=left_options)
        right_table = client.report_table(code=ref.code, allow_unlisted=allow_unlisted, options=right_options)
        master_report = client.report_master_data(code=ref.code, allow_unlisted=allow_unlisted, actor_type="Player")
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)
        return
    finally:
        client.close()

    left_payload = _report_encounter_aura_summary_payload(
        report=report,
        fight=fight,
        table_report=left_table,
        master_report=master_report,
        ability_id=ability_id,
    )
    right_payload = _report_encounter_aura_summary_payload(
        report=report,
        fight=fight,
        table_report=right_table,
        master_report=master_report,
        ability_id=ability_id,
    )

    _emit(
        ctx,
        {
            "ok": True,
            "provider": "warcraftlogs",
            "kind": "report_encounter_aura_compare",
            "query": {
                "ability_id": float(ability_id),
                "source_id": source_id,
                "target_id": target_id,
                "hostility_type": normalized_hostility_type,
                "translate": translate,
                "wipe_cutoff": wipe_cutoff,
            },
            **_encounter_summary_payload(ref=ref, report=report, fight=fight, encounter=encounter),
            "aura": left_payload.get("aura"),
            "windows": [
                {
                    "label": left_label,
                    "query": left_query,
                    "aura_summary": left_payload.get("aura_summary"),
                },
                {
                    "label": right_label,
                    "query": right_query,
                    "aura_summary": right_payload.get("aura_summary"),
                },
            ],
            "comparison": {
                "matching_rule": "same_report_same_fight_same_ability_explicit_windows",
                "rows": _aura_compare_rows(
                    left_rows=(left_payload.get("aura_summary") or {}).get("rows") if isinstance(left_payload.get("aura_summary"), dict) else [],
                    right_rows=(right_payload.get("aura_summary") or {}).get("rows") if isinstance(right_payload.get("aura_summary"), dict) else [],
                ),
            },
        },
        client=client,
    )


@app.command("report-encounter-damage-source-summary")
def report_encounter_damage_source_summary(
    ctx: typer.Context,
    reference: str,
    fight_id: int | None = typer.Option(None, "--fight-id", help="Override or supply a fight ID when the report reference does not include one."),
    source_id: int | None = typer.Option(None, "--source-id", help="Optional source actor filter."),
    target_id: int | None = typer.Option(None, "--target-id", help="Optional target actor filter."),
    ability_id: float | None = typer.Option(None, "--ability-id", help="Optional ability game ID filter."),
    hostility_type: str | None = typer.Option(None, "--hostility-type", help="Optional hostility filter."),
    wipe_cutoff: int | None = typer.Option(None, "--wipe-cutoff", help="Optional wipe cutoff."),
    window_start_ms: float | None = typer.Option(None, "--window-start-ms", help="Optional encounter-relative start offset in milliseconds."),
    window_end_ms: float | None = typer.Option(None, "--window-end-ms", help="Optional encounter-relative end offset in milliseconds."),
    translate: bool | None = typer.Option(None, "--translate/--no-translate", help="Optional translation toggle."),
    allow_unlisted: bool = typer.Option(False, "--allow-unlisted", help="Allow lookup of unlisted reports."),
) -> None:
    client = _client(ctx)
    try:
        ref, report, fight, encounter = _resolve_encounter_scope(
            ctx,
            client=client,
            reference=reference,
            fight_id=fight_id,
            allow_unlisted=allow_unlisted,
        )
        normalized_hostility_type = _normalize_graphql_enum(hostility_type)
        options, query = _encounter_filter_options(
            ctx,
            fight=fight,
            ability_id=ability_id,
            data_type="DamageDone",
            source_id=source_id,
            target_id=target_id,
            hostility_type=normalized_hostility_type,
            translate=translate,
            view_by="Source",
            wipe_cutoff=wipe_cutoff,
            window_start_ms=window_start_ms,
            window_end_ms=window_end_ms,
        )
        table_payload = client.report_table(code=ref.code, allow_unlisted=allow_unlisted, options=options)
        master_report = client.report_master_data(code=ref.code, allow_unlisted=allow_unlisted, actor_type="Player")
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)
        return
    finally:
        client.close()
    _emit(
        ctx,
        {
            "ok": True,
            "provider": "warcraftlogs",
            "kind": "report_encounter_damage_source_summary",
            "query": query,
            **_encounter_summary_payload(ref=ref, report=report, fight=fight, encounter=encounter),
            **_report_encounter_damage_source_summary_payload(
                report=report,
                fight=fight,
                table_report=table_payload,
                master_report=master_report,
            ),
        },
        client=client,
    )


@app.command("report-encounter-damage-target-summary")
def report_encounter_damage_target_summary(
    ctx: typer.Context,
    reference: str,
    fight_id: int | None = typer.Option(None, "--fight-id", help="Override or supply a fight ID when the report reference does not include one."),
    source_id: int | None = typer.Option(None, "--source-id", help="Optional source actor filter."),
    target_id: int | None = typer.Option(None, "--target-id", help="Optional target actor filter."),
    ability_id: float | None = typer.Option(None, "--ability-id", help="Optional ability game ID filter."),
    hostility_type: str | None = typer.Option(None, "--hostility-type", help="Optional hostility filter."),
    wipe_cutoff: int | None = typer.Option(None, "--wipe-cutoff", help="Optional wipe cutoff."),
    window_start_ms: float | None = typer.Option(None, "--window-start-ms", help="Optional encounter-relative start offset in milliseconds."),
    window_end_ms: float | None = typer.Option(None, "--window-end-ms", help="Optional encounter-relative end offset in milliseconds."),
    translate: bool | None = typer.Option(None, "--translate/--no-translate", help="Optional translation toggle."),
    allow_unlisted: bool = typer.Option(False, "--allow-unlisted", help="Allow lookup of unlisted reports."),
) -> None:
    client = _client(ctx)
    try:
        ref, report, fight, encounter = _resolve_encounter_scope(
            ctx,
            client=client,
            reference=reference,
            fight_id=fight_id,
            allow_unlisted=allow_unlisted,
        )
        normalized_hostility_type = _normalize_graphql_enum(hostility_type)
        options, query = _encounter_filter_options(
            ctx,
            fight=fight,
            ability_id=ability_id,
            data_type="DamageDone",
            source_id=source_id,
            target_id=target_id,
            hostility_type=normalized_hostility_type,
            translate=translate,
            view_by="Target",
            wipe_cutoff=wipe_cutoff,
            window_start_ms=window_start_ms,
            window_end_ms=window_end_ms,
        )
        table_payload = client.report_table(code=ref.code, allow_unlisted=allow_unlisted, options=options)
        master_report = client.report_master_data(code=ref.code, allow_unlisted=allow_unlisted)
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)
        return
    finally:
        client.close()
    _emit(
        ctx,
        {
            "ok": True,
            "provider": "warcraftlogs",
            "kind": "report_encounter_damage_target_summary",
            "query": query,
            **_encounter_summary_payload(ref=ref, report=report, fight=fight, encounter=encounter),
            **_report_encounter_damage_target_summary_payload(
                report=report,
                fight=fight,
                table_report=table_payload,
                master_report=master_report,
            ),
        },
        client=client,
    )


@app.command("report-encounter-damage-breakdown")
def report_encounter_damage_breakdown(
    ctx: typer.Context,
    reference: str,
    fight_id: int | None = typer.Option(None, "--fight-id", help="Override or supply a fight ID when the report reference does not include one."),
    source_id: int | None = typer.Option(None, "--source-id", help="Optional source actor filter."),
    target_id: int | None = typer.Option(None, "--target-id", help="Optional target actor filter."),
    ability_id: float | None = typer.Option(None, "--ability-id", help="Optional ability game ID filter."),
    hostility_type: str | None = typer.Option(None, "--hostility-type", help="Optional hostility filter."),
    view_by: str | None = typer.Option("source", "--view-by", help="Optional table view grouping."),
    wipe_cutoff: int | None = typer.Option(None, "--wipe-cutoff", help="Optional wipe cutoff."),
    window_start_ms: float | None = typer.Option(None, "--window-start-ms", help="Optional encounter-relative start offset in milliseconds."),
    window_end_ms: float | None = typer.Option(None, "--window-end-ms", help="Optional encounter-relative end offset in milliseconds."),
    translate: bool | None = typer.Option(None, "--translate/--no-translate", help="Optional translation toggle."),
    allow_unlisted: bool = typer.Option(False, "--allow-unlisted", help="Allow lookup of unlisted reports."),
) -> None:
    client = _client(ctx)
    try:
        ref, report, fight, encounter = _resolve_encounter_scope(
            ctx,
            client=client,
            reference=reference,
            fight_id=fight_id,
            allow_unlisted=allow_unlisted,
        )
        normalized_hostility_type = _normalize_graphql_enum(hostility_type)
        normalized_view_by = _normalize_graphql_enum(view_by)
        options, query = _encounter_filter_options(
            ctx,
            fight=fight,
            ability_id=ability_id,
            data_type="DamageDone",
            source_id=source_id,
            target_id=target_id,
            hostility_type=normalized_hostility_type,
            translate=translate,
            view_by=normalized_view_by,
            wipe_cutoff=wipe_cutoff,
            window_start_ms=window_start_ms,
            window_end_ms=window_end_ms,
        )
        payload = client.report_table(code=ref.code, allow_unlisted=allow_unlisted, options=options)
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)
        return
    finally:
        client.close()
    _emit(
        ctx,
        {
            "ok": True,
            "provider": "warcraftlogs",
            "kind": "report_encounter_damage_breakdown",
            "query": query,
            **_encounter_summary_payload(ref=ref, report=report, fight=fight, encounter=encounter),
            **_report_json_payload(payload, field="table"),
        },
        client=client,
    )


@app.command("kill-time-distribution")
def kill_time_distribution(
    ctx: typer.Context,
    zone_id: int = typer.Option(..., "--zone-id", help="Warcraft Logs zone ID to sample reports from."),
    boss_id: int | None = typer.Option(None, "--boss-id", help="Encounter ID to match."),
    boss_name: str | None = typer.Option(None, "--boss-name", help="Boss name to match within sampled fights."),
    difficulty: int | None = typer.Option(None, "--difficulty", help="Optional difficulty ID filter."),
    spec_name: str | None = typer.Option(
        None,
        "--spec-name",
        help="Optional sampled participant spec filter applied before aggregation.",
    ),
    kill_time_min: float | None = typer.Option(None, "--kill-time-min", help="Optional minimum kill time in seconds."),
    kill_time_max: float | None = typer.Option(None, "--kill-time-max", help="Optional maximum kill time in seconds."),
    report_pages: int = typer.Option(1, "--report-pages", min=1, max=10, help="How many report-list pages to sample."),
    reports_per_page: int = typer.Option(25, "--reports-per-page", min=1, max=100, help="Reports to fetch per sampled page."),
    start_time: float | None = typer.Option(None, "--start-time", help="Optional report-range start time in milliseconds."),
    end_time: float | None = typer.Option(None, "--end-time", help="Optional report-range end time in milliseconds."),
    guild_region: str | None = typer.Option(None, "--guild-region", help="Optional guild-region scope for report discovery."),
    guild_realm: str | None = typer.Option(None, "--guild-realm", help="Optional guild-realm scope for report discovery."),
    guild_name: str | None = typer.Option(None, "--guild-name", help="Optional guild-name scope for report discovery."),
    bucket_seconds: int = typer.Option(30, "--bucket-seconds", min=5, max=600, help="Bucket size in seconds for the returned histogram."),
) -> None:
    _require_boss_scope(ctx, boss_id=boss_id, boss_name=boss_name)
    client = _client(ctx)
    try:
        analytics = _collect_boss_kill_rows(
            client=client,
            zone_id=zone_id,
            boss_id=boss_id,
            boss_name=boss_name,
            difficulty=difficulty,
            spec_name=spec_name,
            kill_time_min=kill_time_min,
            kill_time_max=kill_time_max,
            report_pages=report_pages,
            reports_per_page=reports_per_page,
            start_time=start_time,
            end_time=end_time,
            guild_region=guild_region,
            guild_realm=guild_realm,
            guild_name=guild_name,
        )
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)
        return
    finally:
        client.close()
    _emit(
        ctx,
        _kill_time_distribution_payload(
            rows=analytics["rows"],
            sample=analytics["sample"],
            query=_cross_report_query(
                zone_id=zone_id,
                boss_id=boss_id,
                boss_name=boss_name,
                difficulty=difficulty,
                spec_name=spec_name,
                kill_time_min=kill_time_min,
                kill_time_max=kill_time_max,
                top=len(analytics["rows"]),
                report_pages=report_pages,
                reports_per_page=reports_per_page,
                start_time=start_time,
                end_time=end_time,
                guild_region=guild_region,
                guild_realm=guild_realm,
                guild_name=guild_name,
            ),
            bucket_seconds=bucket_seconds,
        ),
        client=client,
    )


@app.command("report-fights")
def report_fights(
    ctx: typer.Context,
    code: str,
    difficulty: int | None = typer.Option(None, "--difficulty", help="Optional difficulty ID filter."),
    allow_unlisted: bool = typer.Option(False, "--allow-unlisted", help="Allow lookup of unlisted reports."),
) -> None:
    client = _client(ctx)
    try:
        payload = client.report_fights(code=code, difficulty=difficulty, allow_unlisted=allow_unlisted)
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)
        return
    finally:
        client.close()
    fights = payload.get("fights") if isinstance(payload.get("fights"), list) else []
    _emit(
        ctx,
        {
            "ok": True,
            "provider": "warcraftlogs",
            "report": _report_brief_payload(payload),
            "difficulty": difficulty,
            "count": len(fights),
            "fights": [_fight_payload(fight) for fight in fights if isinstance(fight, dict)],
        },
        client=client,
    )


@app.command("report-events")
def report_events(
    ctx: typer.Context,
    code: str,
    ability_id: float | None = typer.Option(None, "--ability-id", help="Optional ability game ID filter."),
    data_type: str | None = typer.Option(None, "--data-type", help="Event data type (e.g. casts, damage-done, healing). Strongly recommended; without it Warcraft Logs returns events.data: null even on valid scoped slices."),
    difficulty: int | None = typer.Option(None, "--difficulty", help="Optional difficulty ID filter."),
    encounter_id: int | None = typer.Option(None, "--encounter-id", help="Optional encounter ID filter."),
    end_time: float | None = typer.Option(None, "--end-time", help="Optional event-range end timestamp."),
    fight_id: list[int] | None = FIGHT_ID_OPTION,
    filter_expression: str | None = typer.Option(None, "--filter-expression", help="Optional Warcraft Logs filter expression."),
    hostility_type: str | None = typer.Option(None, "--hostility-type", help="Optional hostility filter."),
    kill_type: str | None = typer.Option(None, "--kill-type", help="Optional kill filter."),
    limit: int | None = typer.Option(None, "--limit", min=1, max=10000, help="Optional page event limit."),
    source_id: int | None = typer.Option(None, "--source-id", help="Optional source actor ID filter."),
    start_time: float | None = typer.Option(None, "--start-time", help="Optional event-range start timestamp."),
    target_id: int | None = typer.Option(None, "--target-id", help="Optional target actor ID filter."),
    translate: bool | None = typer.Option(None, "--translate/--no-translate", help="Optional translation toggle."),
    allow_unlisted: bool = typer.Option(False, "--allow-unlisted", help="Allow lookup of unlisted reports."),
) -> None:
    normalized_data_type = _normalize_graphql_enum(data_type)
    normalized_hostility_type = _normalize_graphql_enum(hostility_type)
    normalized_kill_type = _normalize_graphql_enum(kill_type)
    if not any(value is not None for value in (fight_id, encounter_id, start_time, end_time)):
        _fail(
            ctx,
            "missing_scope",
            "report-events requires a narrowed slice. Provide --fight-id, --encounter-id, --start-time, or --end-time.",
        )
        return
    options = _report_filter_options(
        ability_id=ability_id,
        data_type=normalized_data_type,
        difficulty=difficulty,
        encounter_id=encounter_id,
        end_time=end_time,
        fight_ids=fight_id,
        filter_expression=filter_expression,
        hostility_type=normalized_hostility_type,
        kill_type=normalized_kill_type,
        limit=limit,
        source_id=source_id,
        start_time=start_time,
        target_id=target_id,
        translate=translate,
        view_by=None,
        wipe_cutoff=None,
    )
    client = _client(ctx)
    try:
        payload = client.report_events(code=code, allow_unlisted=allow_unlisted, options=options)
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)
        return
    finally:
        client.close()
    result_payload = _report_events_payload(payload)
    notes: list[str] = []
    if result_payload.get("events") is None and normalized_data_type is None:
        notes.append(
            "events.data is null. Warcraft Logs requires --data-type "
            "(e.g. casts, damage-done, healing) for non-null event slices."
        )
    emitted: dict[str, Any] = {
        "ok": True,
        "provider": "warcraftlogs",
        "query": _report_filter_query_payload(
            ability_id=ability_id,
            data_type=normalized_data_type,
            difficulty=difficulty,
            encounter_id=encounter_id,
            end_time=end_time,
            fight_ids=fight_id,
            filter_expression=filter_expression,
            hostility_type=normalized_hostility_type,
            kill_type=normalized_kill_type,
            limit=limit,
            source_id=source_id,
            start_time=start_time,
            target_id=target_id,
            translate=translate,
            view_by=None,
            wipe_cutoff=None,
        ),
        **result_payload,
    }
    if notes:
        emitted["notes"] = notes
    _emit(ctx, emitted, client=client)


@app.command("report-table")
def report_table(
    ctx: typer.Context,
    code: str,
    ability_id: float | None = typer.Option(None, "--ability-id", help="Optional ability game ID filter."),
    data_type: str | None = typer.Option(None, "--data-type", help="Optional table data type."),
    difficulty: int | None = typer.Option(None, "--difficulty", help="Optional difficulty ID filter."),
    encounter_id: int | None = typer.Option(None, "--encounter-id", help="Optional encounter ID filter."),
    end_time: float | None = typer.Option(None, "--end-time", help="Optional event-range end timestamp."),
    fight_id: list[int] | None = FIGHT_ID_OPTION,
    filter_expression: str | None = typer.Option(None, "--filter-expression", help="Optional Warcraft Logs filter expression."),
    hostility_type: str | None = typer.Option(None, "--hostility-type", help="Optional hostility filter."),
    kill_type: str | None = typer.Option(None, "--kill-type", help="Optional kill filter."),
    source_id: int | None = typer.Option(None, "--source-id", help="Optional source actor ID filter."),
    start_time: float | None = typer.Option(None, "--start-time", help="Optional event-range start timestamp."),
    target_id: int | None = typer.Option(None, "--target-id", help="Optional target actor ID filter."),
    translate: bool | None = typer.Option(None, "--translate/--no-translate", help="Optional translation toggle."),
    view_by: str | None = typer.Option(None, "--view-by", help="Optional view grouping."),
    wipe_cutoff: int | None = typer.Option(None, "--wipe-cutoff", help="Optional wipe cutoff."),
    allow_unlisted: bool = typer.Option(False, "--allow-unlisted", help="Allow lookup of unlisted reports."),
) -> None:
    normalized_data_type = _normalize_graphql_enum(data_type)
    normalized_hostility_type = _normalize_graphql_enum(hostility_type)
    normalized_kill_type = _normalize_graphql_enum(kill_type)
    normalized_view_by = _normalize_graphql_enum(view_by)
    options = _report_filter_options(
        ability_id=ability_id,
        data_type=normalized_data_type,
        difficulty=difficulty,
        encounter_id=encounter_id,
        end_time=end_time,
        fight_ids=fight_id,
        filter_expression=filter_expression,
        hostility_type=normalized_hostility_type,
        kill_type=normalized_kill_type,
        limit=None,
        source_id=source_id,
        start_time=start_time,
        target_id=target_id,
        translate=translate,
        view_by=normalized_view_by,
        wipe_cutoff=wipe_cutoff,
    )
    client = _client(ctx)
    try:
        payload = client.report_table(code=code, allow_unlisted=allow_unlisted, options=options)
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)
        return
    finally:
        client.close()
    result_payload = _report_json_payload(payload, field="table")
    _emit(
        ctx,
        {
            "ok": True,
            "provider": "warcraftlogs",
            "query": _report_filter_query_payload(
                ability_id=ability_id,
                data_type=normalized_data_type,
                difficulty=difficulty,
                encounter_id=encounter_id,
                end_time=end_time,
                fight_ids=fight_id,
                filter_expression=filter_expression,
                hostility_type=normalized_hostility_type,
                kill_type=normalized_kill_type,
                limit=None,
                source_id=source_id,
                start_time=start_time,
                target_id=target_id,
                translate=translate,
                view_by=normalized_view_by,
                wipe_cutoff=wipe_cutoff,
            ),
            **result_payload,
        },
        client=client,
    )


@app.command("report-graph")
def report_graph(
    ctx: typer.Context,
    code: str,
    ability_id: float | None = typer.Option(None, "--ability-id", help="Optional ability game ID filter."),
    data_type: str | None = typer.Option(None, "--data-type", help="Optional graph data type."),
    difficulty: int | None = typer.Option(None, "--difficulty", help="Optional difficulty ID filter."),
    encounter_id: int | None = typer.Option(None, "--encounter-id", help="Optional encounter ID filter."),
    end_time: float | None = typer.Option(None, "--end-time", help="Optional event-range end timestamp."),
    fight_id: list[int] | None = FIGHT_ID_OPTION,
    filter_expression: str | None = typer.Option(None, "--filter-expression", help="Optional Warcraft Logs filter expression."),
    hostility_type: str | None = typer.Option(None, "--hostility-type", help="Optional hostility filter."),
    kill_type: str | None = typer.Option(None, "--kill-type", help="Optional kill filter."),
    source_id: int | None = typer.Option(None, "--source-id", help="Optional source actor ID filter."),
    start_time: float | None = typer.Option(None, "--start-time", help="Optional event-range start timestamp."),
    target_id: int | None = typer.Option(None, "--target-id", help="Optional target actor ID filter."),
    translate: bool | None = typer.Option(None, "--translate/--no-translate", help="Optional translation toggle."),
    view_by: str | None = typer.Option(None, "--view-by", help="Optional view grouping."),
    wipe_cutoff: int | None = typer.Option(None, "--wipe-cutoff", help="Optional wipe cutoff."),
    allow_unlisted: bool = typer.Option(False, "--allow-unlisted", help="Allow lookup of unlisted reports."),
) -> None:
    normalized_data_type = _normalize_graphql_enum(data_type)
    normalized_hostility_type = _normalize_graphql_enum(hostility_type)
    normalized_kill_type = _normalize_graphql_enum(kill_type)
    normalized_view_by = _normalize_graphql_enum(view_by)
    options = _report_filter_options(
        ability_id=ability_id,
        data_type=normalized_data_type,
        difficulty=difficulty,
        encounter_id=encounter_id,
        end_time=end_time,
        fight_ids=fight_id,
        filter_expression=filter_expression,
        hostility_type=normalized_hostility_type,
        kill_type=normalized_kill_type,
        limit=None,
        source_id=source_id,
        start_time=start_time,
        target_id=target_id,
        translate=translate,
        view_by=normalized_view_by,
        wipe_cutoff=wipe_cutoff,
    )
    client = _client(ctx)
    try:
        payload = client.report_graph(code=code, allow_unlisted=allow_unlisted, options=options)
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)
        return
    finally:
        client.close()
    result_payload = _report_json_payload(payload, field="graph")
    _emit(
        ctx,
        {
            "ok": True,
            "provider": "warcraftlogs",
            "query": _report_filter_query_payload(
                ability_id=ability_id,
                data_type=normalized_data_type,
                difficulty=difficulty,
                encounter_id=encounter_id,
                end_time=end_time,
                fight_ids=fight_id,
                filter_expression=filter_expression,
                hostility_type=normalized_hostility_type,
                kill_type=normalized_kill_type,
                limit=None,
                source_id=source_id,
                start_time=start_time,
                target_id=target_id,
                translate=translate,
                view_by=normalized_view_by,
                wipe_cutoff=wipe_cutoff,
            ),
            **result_payload,
        },
        client=client,
    )


@app.command("report-master-data")
def report_master_data(
    ctx: typer.Context,
    code: str,
    actor_type: str | None = typer.Option(None, "--actor-type", help="Optional actor type filter."),
    actor_sub_type: str | None = typer.Option(None, "--actor-sub-type", help="Optional actor sub-type filter."),
    translate: bool | None = typer.Option(None, "--translate/--no-translate", help="Optional translation toggle."),
    allow_unlisted: bool = typer.Option(False, "--allow-unlisted", help="Allow lookup of unlisted reports."),
) -> None:
    client = _client(ctx)
    try:
        payload = client.report_master_data(
            code=code,
            allow_unlisted=allow_unlisted,
            translate=translate,
            actor_type=actor_type,
            actor_sub_type=actor_sub_type,
        )
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)
        return
    finally:
        client.close()
    _emit(
        ctx,
        {
            "ok": True,
            "provider": "warcraftlogs",
            "query": {"actor_type": actor_type, "actor_sub_type": actor_sub_type, "translate": translate},
            **_report_master_data_payload(payload),
        },
        client=client,
    )


@app.command("report-player-details")
def report_player_details(
    ctx: typer.Context,
    code: str,
    difficulty: int | None = typer.Option(None, "--difficulty", help="Optional difficulty ID filter."),
    encounter_id: int | None = typer.Option(None, "--encounter-id", help="Optional encounter ID filter."),
    end_time: float | None = typer.Option(None, "--end-time", help="Optional event-range end timestamp."),
    fight_id: list[int] | None = FIGHT_ID_OPTION,
    include_combatant_info: bool | None = typer.Option(
        None,
        "--include-combatant-info/--no-include-combatant-info",
        help="Optional combatant detail toggle.",
    ),
    kill_type: str | None = typer.Option(None, "--kill-type", help="Optional kill filter."),
    start_time: float | None = typer.Option(None, "--start-time", help="Optional event-range start timestamp."),
    translate: bool | None = typer.Option(None, "--translate/--no-translate", help="Optional translation toggle."),
    allow_unlisted: bool = typer.Option(False, "--allow-unlisted", help="Allow lookup of unlisted reports."),
) -> None:
    normalized_kill_type = _normalize_graphql_enum(kill_type)
    options = ReportPlayerDetailsOptions(
        difficulty=difficulty,
        encounter_id=encounter_id,
        end_time=end_time,
        fight_ids=fight_id or None,
        include_combatant_info=include_combatant_info,
        kill_type=normalized_kill_type,
        start_time=start_time,
        translate=translate,
    )
    client = _client(ctx)
    try:
        payload = client.report_player_details(code=code, allow_unlisted=allow_unlisted, options=options)
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)
        return
    finally:
        client.close()
    _emit(
        ctx,
        {
            "ok": True,
            "provider": "warcraftlogs",
            "query": {
                "difficulty": difficulty,
                "encounter_id": encounter_id,
                "end_time": end_time,
                "fight_ids": fight_id,
                "include_combatant_info": include_combatant_info,
                "kill_type": normalized_kill_type,
                "start_time": start_time,
                "translate": translate,
            },
            **_report_player_details_payload(
                payload,
                report_code=code,
                fight_id=fight_id[0] if fight_id and len(fight_id) == 1 else None,
            ),
        },
        client=client,
    )


@app.command("report-rankings")
def report_rankings(
    ctx: typer.Context,
    code: str,
    compare: str | None = typer.Option(None, "--compare", help="Optional compare mode such as rankings or parses."),
    difficulty: int | None = typer.Option(None, "--difficulty", help="Optional difficulty ID filter."),
    encounter_id: int | None = typer.Option(None, "--encounter-id", help="Optional encounter ID filter."),
    fight_id: list[int] | None = FIGHT_ID_OPTION,
    player_metric: str | None = typer.Option(None, "--player-metric", help="Optional player metric such as dps or hps."),
    timeframe: str | None = typer.Option(None, "--timeframe", help="Optional timeframe such as today or historical."),
    allow_unlisted: bool = typer.Option(False, "--allow-unlisted", help="Allow lookup of unlisted reports."),
) -> None:
    normalized_compare = _normalize_graphql_enum(compare)
    normalized_timeframe = _normalize_graphql_enum(timeframe)
    options = ReportRankingsOptions(
        compare=normalized_compare,
        difficulty=difficulty,
        encounter_id=encounter_id,
        fight_ids=fight_id or None,
        player_metric=player_metric,
        timeframe=normalized_timeframe,
    )
    client = _client(ctx)
    try:
        payload = client.report_rankings(code=code, allow_unlisted=allow_unlisted, options=options)
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)
        return
    finally:
        client.close()
    _emit(
        ctx,
        {
            "ok": True,
            "provider": "warcraftlogs",
            "query": {
                "compare": normalized_compare,
                "difficulty": difficulty,
                "encounter_id": encounter_id,
                "fight_ids": fight_id,
                "player_metric": player_metric,
                "timeframe": normalized_timeframe,
            },
            **_report_rankings_payload(payload),
        },
        client=client,
    )


def run() -> None:
    app()

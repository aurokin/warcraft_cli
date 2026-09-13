from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import shlex
import sys
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Literal, NoReturn
from urllib.parse import parse_qs, urlparse

import typer
from warcraft_core.analytics import numeric_summary
from warcraft_core.auth import (
    delete_provider_auth_state,
    load_provider_auth_state,
    provider_auth_status,
    save_provider_auth_state,
)
from warcraft_core.cli import (
    CompactMaxCharsOption,
    CompactOption,
    FieldsOption,
    FieldsStrictOption,
    PrettyOption,
    ProfileOption,
    cfg_as,
    configure,
    emit,
    fail,
    guarded_run,
)
from warcraft_core.cli import (
    RuntimeConfig as BaseRuntimeConfig,
)
from warcraft_core.envelope import ENVELOPE_KEYS, SCHEMA_VERSION, Envelope
from warcraft_core.exit_codes import EXIT_AUTH, EXIT_USAGE, exit_code_for
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
from warcraft_core.output import DEFAULT_COMPACT_MAX_CHARS
from warcraft_core.paths import provider_state_path
from warcraft_core.talent_transport import TalentTransportBackend, validate_talent_tree_transport
from warcraft_core.wow_normalization import normalize_region

from warcraftlogs_cli.boss_kills import (
    CrossReportScope,
)
from warcraftlogs_cli.boss_kills import (
    boss_kills_payload as _boss_kills_payload,
)
from warcraftlogs_cli.boss_kills import (
    collect_boss_kill_rows as _collect_boss_kill_rows,
)
from warcraftlogs_cli.boss_kills import (
    kill_time_distribution_payload as _kill_time_distribution_payload,
)
from warcraftlogs_cli.boss_kills import (
    sampled_cache_provenance as _sampled_cache_provenance,
)
from warcraftlogs_cli.boss_kills import (
    sampled_cross_report_citations as _sampled_cross_report_citations,
)
from warcraftlogs_cli.boss_kills import (
    sampled_cross_report_freshness as _sampled_cross_report_freshness,
)
from warcraftlogs_cli.boss_kills import (
    sampled_sample_scope as _sampled_sample_scope,
)
from warcraftlogs_cli.boss_kills import (
    spec_filtered_kill_samples_payload as _spec_filtered_kill_samples_payload,
)
from warcraftlogs_cli.client import (
    GRAPHQL_WARNINGS_KEY,
    RETAIL_PROFILE,
    EncounterRankingsOptions,
    ReportFilterOptions,
    ReportPlayerDetailsOptions,
    ReportRankingsOptions,
    WarcraftLogsClient,
    WarcraftLogsClientError,
    WarcraftLogsSiteProfile,
    load_warcraftlogs_auth_config,
    resolve_site_profile,
    saved_user_token_site_key,
    warcraftlogs_provider_env_path,
)
from warcraftlogs_cli.payload_envelope import apply_payload_envelope, canonical_key_for_command
from warcraftlogs_cli.payload_keys_registry import ALL_COMMANDS
from warcraftlogs_cli.provider import doctor as provider_doctor
from warcraftlogs_cli.provider import resolve as provider_resolve
from warcraftlogs_cli.provider import search as provider_search
from warcraftlogs_cli.report_payloads import (
    fight_payload as _fight_payload,
)
from warcraftlogs_cli.report_payloads import (
    region_payload as _region_payload,
)
from warcraftlogs_cli.report_payloads import (
    report_brief_payload as _report_brief_payload,
)
from warcraftlogs_cli.report_payloads import (
    report_payload as _report_payload,
)
from warcraftlogs_cli.report_payloads import (
    report_url as _report_url,
)
from warcraftlogs_cli.report_payloads import (
    server_payload as _server_payload,
)
from warcraftlogs_cli.sampling_utils import (
    boss_matches as _boss_matches,
)
from warcraftlogs_cli.sampling_utils import dict_at, list_at
from warcraftlogs_cli.sampling_utils import (
    normalize_match_text as _normalize_match_text,
)
from warcraftlogs_cli.sampling_utils import (
    report_cache_provenance as _report_cache_provenance,
)
from warcraftlogs_cli.sampling_utils import (
    report_is_finished as _report_is_finished,
)
from warcraftlogs_cli.sampling_utils import (
    sampled_spec_filter_notes as _sampled_spec_filter_notes,
)

app = typer.Typer(add_completion=False, help="Warcraft Logs official API CLI.")
auth_app = typer.Typer(add_completion=False, help="Warcraft Logs authentication helpers.")
app.add_typer(auth_app, name="auth")

FIGHT_ID_OPTION = typer.Option(None, "--fight-id", help="Optional fight ID filter. Repeat as needed.")
REPORT_CODE_PATTERN = re.compile(r"^(?=.*[A-Za-z])(?=.*\d)[A-Za-z0-9]{8,32}$")
RAW_GRAPHQL_VAR_OPTION = typer.Option(
    [],
    "--var",
    help="GraphQL variable in key=value form. Value is JSON-coerced when possible.",
)
GRAPHQL_VARIABLE_DECLARATION_PATTERN = re.compile(
    r"\$(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*:\s*(?P<type>\[[^\]]+\]!?|[A-Za-z_][A-Za-z0-9_]*!?)"
)
GRAPHQL_OPERATION_NAME_PATTERN = re.compile(
    r"\b(?P<kind>query|mutation|subscription)\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)",
    re.MULTILINE,
)

WARCRAFTLOGS_INTROSPECTION_QUERY = """
query IntrospectionQuery {
  __schema {
    queryType { name }
    mutationType { name }
    subscriptionType { name }
    types {
      ...FullType
    }
    directives {
      name
      description
      locations
      args {
        ...InputValue
      }
    }
  }
}

fragment FullType on __Type {
  kind
  name
  description
  fields(includeDeprecated: true) {
    name
    description
    args {
      ...InputValue
    }
    type {
      ...TypeRef
    }
    isDeprecated
    deprecationReason
  }
  inputFields {
    ...InputValue
  }
  interfaces {
    ...TypeRef
  }
  enumValues(includeDeprecated: true) {
    name
    description
    isDeprecated
    deprecationReason
  }
  possibleTypes {
    ...TypeRef
  }
}

fragment InputValue on __InputValue {
  name
  description
  type { ...TypeRef }
  defaultValue
}

fragment TypeRef on __Type {
  kind
  name
  ofType {
    kind
    name
    ofType {
      kind
      name
      ofType {
        kind
        name
        ofType {
          kind
          name
          ofType {
            kind
            name
            ofType {
              kind
              name
              ofType {
                kind
                name
              }
            }
          }
        }
      }
    }
  }
}
"""


@dataclass(slots=True)
class RuntimeConfig(BaseRuntimeConfig):
    """Shared runtime config plus the Warcraft Logs ``--site`` profile."""

    site_profile: WarcraftLogsSiteProfile = RETAIL_PROFILE


@dataclass(frozen=True, slots=True)
class ReportReference:
    code: str
    fight_id: int | None
    source_url: str | None = None


def _cfg(ctx: typer.Context) -> RuntimeConfig:
    return cfg_as(ctx, RuntimeConfig)


def _envelope_defaults(command: str | None) -> dict[str, Any]:
    canonical = canonical_key_for_command(command) if command else ""
    return {
        "ok": True,
        "provider": "warcraftlogs",
        "command": command or "",
        "kind": canonical,
        "schema_version": SCHEMA_VERSION,
        "query": None,
        "provenance": {},
        "data": {},
    }


def _with_envelope_keys(payload: dict[str, Any], *, command: str | None) -> dict[str, Any]:
    """Add the shared envelope keys this payload is missing, never overwriting what a command set.

    Warcraft Logs payloads stay flat (plus the deprecated canonical command key); ``data`` mirrors
    those keys so agents can read the envelope slot everywhere.
    """
    missing = {key: value for key, value in _envelope_defaults(command).items() if key not in payload}
    if "data" in missing:
        missing["data"] = {key: value for key, value in payload.items() if key not in ENVELOPE_KEYS}
    if not missing:
        return payload
    return {**payload, **missing}


def _emit(ctx: typer.Context, payload: dict[str, Any], *, client: Any = None, command: str | None = None) -> None:
    """Emit a success payload: client warnings, then the canonical command key, then envelope keys.

    Error envelopes go to stderr through ``_fail``, never here.
    """
    if client is not None:
        payload = _with_warnings(payload, client)
    ctx_command = getattr(ctx, "command", None)
    command_name = command or (ctx_command.name if ctx_command is not None else None)
    if command_name in ALL_COMMANDS:
        payload = apply_payload_envelope(command_name, payload)
    emit(ctx, _with_envelope_keys(payload, command=command_name))


def _with_warnings(payload: dict[str, Any], client: Any) -> dict[str, Any]:
    warnings = list(getattr(client, "last_warnings", []) or [])
    if not warnings:
        return payload
    notes = list(payload.get("notes") or [])
    notes.extend(f"warcraft logs returned partial errors: {w.get('message', '')}" for w in warnings if isinstance(w, dict))
    return {**payload, "notes": notes, "graphql_warnings": warnings}


# Warcraft Logs error codes that mean "the caller is not authorised", on top of the shared vocabulary.
_AUTH_ERROR_CODES = frozenset({"missing_client_credentials", "missing_public_auth", "missing_user_auth", "user_token_expired"})


# Missing required options are usage errors (exit 2), like Click's own parse failures.
_USAGE_ERROR_CODES = frozenset({"missing_boss", "missing_query"})


def _fail(ctx: typer.Context, code: str, message: str) -> NoReturn:
    if code in _AUTH_ERROR_CODES:
        exit_code = EXIT_AUTH
    elif code in _USAGE_ERROR_CODES:
        exit_code = EXIT_USAGE
    else:
        exit_code = exit_code_for(code)
    fail(ctx, code, message, exit_code=exit_code)


def _load_graphql_query(ctx: typer.Context, query: str | None, *, introspect: bool) -> str:
    if introspect:
        return WARCRAFTLOGS_INTROSPECTION_QUERY
    if query is None or not query.strip():
        _fail(ctx, "missing_query", "warcraftlogs graphql requires --query unless --introspect is set.")
    if query == "-":
        loaded = sys.stdin.read()
    elif query.startswith("@"):
        path_text = query[1:]
        if not path_text:
            _fail(ctx, "invalid_query", "--query @path requires a non-empty path.")
        path = Path(path_text).expanduser()
        try:
            loaded = path.read_text(encoding="utf-8")
        except OSError as exc:
            _fail(ctx, "invalid_query", f"Could not read GraphQL query file {str(path)!r}: {exc}")
    else:
        loaded = query
    if not loaded.strip():
        _fail(ctx, "invalid_query", "GraphQL query text must not be empty.")
    return loaded


def _json_coerced_var_value(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _parse_graphql_variables_json(ctx: typer.Context, variables_json: str | None) -> dict[str, Any]:
    if variables_json is None or not variables_json.strip():
        return {}
    try:
        parsed = json.loads(variables_json)
    except json.JSONDecodeError as exc:
        _fail(ctx, "invalid_variables", f"--variables-json must be valid JSON: {exc.msg}.")
    if not isinstance(parsed, dict):
        _fail(ctx, "invalid_variables", "--variables-json must decode to a JSON object.")
    return dict(parsed)


def _parse_graphql_var_options(ctx: typer.Context, raw_vars: list[str]) -> dict[str, Any]:
    variables: dict[str, Any] = {}
    for raw in raw_vars:
        key, separator, value = raw.partition("=")
        if not separator or not key.strip():
            _fail(ctx, "invalid_variables", "--var values must use key=value form.")
        variables[key.strip()] = _json_coerced_var_value(value)
    return variables


def _graphql_balanced_parenthesized_block(text: str, start: int) -> str | None:
    if start >= len(text) or text[start] != "(":
        return None
    depth = 0
    index = start
    in_string = False
    in_block_string = False
    escaped = False
    while index < len(text):
        if in_block_string:
            if text.startswith('"""', index):
                in_block_string = False
                index += 3
                continue
            index += 1
            continue
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if text.startswith('"""', index):
            in_block_string = True
            index += 3
            continue
        if char == '"':
            in_string = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return text[start: index + 1]
        index += 1
    return None


def _graphql_position_in_line_comment(text: str, position: int) -> bool:
    line_start = text.rfind("\n", 0, position) + 1
    comment_start = text.find("#", line_start, position)
    return comment_start != -1


def _selected_graphql_operation_variables(query: str, operation_name: str) -> str:
    for match in GRAPHQL_OPERATION_NAME_PATTERN.finditer(query):
        if _graphql_position_in_line_comment(query, match.start()):
            continue
        if match.group("name") != operation_name:
            continue
        index = match.end()
        while index < len(query) and query[index].isspace():
            index += 1
        return _graphql_balanced_parenthesized_block(query, index) or ""
    return ""


def _declared_graphql_variables(query: str, *, operation_name: str | None = None) -> dict[str, str]:
    signature = _selected_graphql_operation_variables(query, operation_name) if operation_name else query
    return {match.group("name"): match.group("type") for match in GRAPHQL_VARIABLE_DECLARATION_PATTERN.finditer(signature)}


def _graphql_variable_is_list(graphql_type: str | None) -> bool:
    return isinstance(graphql_type, str) and graphql_type.strip().startswith("[")


@dataclass(frozen=True, slots=True)
class _GraphqlScope:
    """Scope values a raw GraphQL call can inject into the variables its operation declares."""

    report_code: str | None = None
    fight_ids: list[int] | None = None
    encounter_id: int | None = None
    start_time: float | None = None
    end_time: float | None = None
    difficulty: int | None = None
    zone_id: int | None = None
    source_id: int | None = None
    target_id: int | None = None
    ability_id: int | None = None
    allow_unlisted: bool = False


def _inject_graphql_scope_helpers(
    variables: dict[str, Any],
    *,
    declared_variables: dict[str, str],
    scope: _GraphqlScope,
) -> dict[str, Any]:
    merged = dict(variables)

    def inject(name: str, value: Any) -> None:
        if name in declared_variables and name not in merged and value is not None:
            merged[name] = value

    inject("code", scope.report_code)
    if scope.fight_ids:
        if "fightIDs" in declared_variables and "fightIDs" not in merged:
            merged["fightIDs"] = list(scope.fight_ids)
        elif "fightID" in declared_variables and "fightID" not in merged:
            merged["fightID"] = scope.fight_ids[0]
    inject("encounterID", scope.encounter_id)
    inject("startTime", scope.start_time)
    inject("endTime", scope.end_time)
    inject("difficulty", scope.difficulty)
    inject("zoneID", scope.zone_id)
    inject("sourceID", scope.source_id)
    inject("targetID", scope.target_id)
    inject("abilityID", scope.ability_id)
    if scope.allow_unlisted:
        inject("allowUnlisted", True)

    for name, graphql_type in declared_variables.items():
        if name in merged and name == "fightIDs" and _graphql_variable_is_list(graphql_type) and isinstance(merged[name], int):
            merged[name] = [merged[name]]
    return merged


def _validated_transport_packet(ctx: typer.Context, packet: Any, *, command_name: str) -> dict[str, Any]:
    try:
        return validate_talent_transport_packet(packet)
    except ValueError as exc:
        _fail(ctx, "invalid_transport_packet", f"{command_name} produced an invalid talent transport packet: {exc}")



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
        return WarcraftLogsClient(site=_cfg(ctx).site_profile)
    except Exception as exc:
        _fail(ctx, "invalid_runtime_config", _runtime_error_message(str(exc)))


def _handle_client_error(ctx: typer.Context, exc: WarcraftLogsClientError) -> NoReturn:
    _fail(ctx, exc.code, exc.message)


def _saved_user_token_ready(state: dict[str, Any], *, site: WarcraftLogsSiteProfile | None = None) -> bool:
    ready = bool(
        state.get("has_access_token")
        and state.get("auth_mode") in {"authorization_code", "pkce"}
        and not state.get("expired")
    )
    if not ready or site is None:
        return ready
    return _saved_user_token_matches_site(site, state=state)


def _saved_provider_auth_payload(state: dict[str, Any] | None = None) -> dict[str, Any]:
    state_path = state.get("path") if state is not None else None
    if isinstance(state_path, str) and state_path.strip():
        payload = load_provider_auth_state("warcraftlogs", path=state_path)
    else:
        payload = load_provider_auth_state("warcraftlogs")
    return payload or {}


def _saved_user_token_site_key(state: dict[str, Any] | None = None) -> str:
    payload = _saved_provider_auth_payload(state)
    return saved_user_token_site_key(payload)


def _saved_user_token_matches_site(site: WarcraftLogsSiteProfile, *, state: dict[str, Any] | None = None) -> bool:
    return _saved_user_token_site_key(state) == site.key


def _site_profile_mismatch_message(*, token_site: str, selected_site: WarcraftLogsSiteProfile) -> str:
    return (
        f"Saved Warcraft Logs user token is for site profile {token_site!r}, not {selected_site.key!r}. "
        f"Re-run `warcraftlogs --site {selected_site.key} auth login` for user-endpoint requests on this site."
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


def _saved_user_token_scope_summary(state: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = _saved_provider_auth_payload(state)
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


def _site_profile_payload(site: WarcraftLogsSiteProfile) -> dict[str, Any]:
    return {
        "key": site.key,
        "label": site.label,
        "root_url": site.root_url,
        "api_url": site.api_url,
        "user_api_url": site.user_api_url,
    }


def _runtime_access_payload(site: WarcraftLogsSiteProfile) -> dict[str, Any]:
    try:
        client = WarcraftLogsClient(site=site)
    except Exception as exc:
        return {
            "ready": False,
            "reason": "invalid_runtime_config",
            "message": _runtime_error_message(str(exc)),
        }
    client.close()
    return {
        "ready": True,
    }


def _public_api_access_payload(
    *,
    auth_configured: bool,
    runtime_access: dict[str, Any],
    live: bool,
    site: WarcraftLogsSiteProfile,
) -> dict[str, Any]:
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
            client = WarcraftLogsClient(site=site)
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
        except Exception as exc:
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


def _user_api_access_payload(
    state: dict[str, Any],
    *,
    runtime_access: dict[str, Any],
    live: bool,
    site: WarcraftLogsSiteProfile,
) -> dict[str, Any]:
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
        scopes = _saved_user_token_scope_summary(state)
        scope_warning: str | None
        if not scopes["has_view_user_profile"]:
            scope_warning = _missing_view_user_profile_warning()
        elif not scopes["has_view_private_reports"]:
            scope_warning = _missing_view_private_reports_warning()
        else:
            scope_warning = None
        token_site = _saved_user_token_site_key(state)
        if token_site != site.key:
            return {
                "ready": False,
                "mode": auth_mode,
                "reason": "site_profile_mismatch",
                "message": _site_profile_mismatch_message(token_site=token_site, selected_site=site),
                "validation": "local",
                "selected_site_profile": site.key,
                "token_site_profile": token_site,
                "scopes": scopes,
                "scope_warning": scope_warning,
            }
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
            client = WarcraftLogsClient(site=site)
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
        except Exception as exc:
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
    if reason == "site_profile_mismatch":
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


def _doctor_payload(*, live: bool, site: WarcraftLogsSiteProfile) -> dict[str, Any]:
    auth = load_warcraftlogs_auth_config()
    credential_source = auth.env_file if auth.env_file is not None else ("environment" if auth.configured else None)
    state = provider_auth_status("warcraftlogs")
    runtime_access = _runtime_access_payload(site)
    public_api_access = _public_api_access_payload(
        auth_configured=auth.configured,
        runtime_access=runtime_access,
        live=live,
        site=site,
    )
    user_api_access = _user_api_access_payload(
        state,
        runtime_access=runtime_access,
        live=live,
        site=site,
    )
    return {
        "ok": True,
        "provider": "warcraftlogs",
        "status": "ready",
        "site_profile": _site_profile_payload(site),
        "auth": {
            "required": True,
            "configured": auth.configured,
            "client_credentials_configured": auth.configured,
            "flow": "oauth_client_credentials",
            "active_mode": _active_auth_mode_from_state(state, site=site),
            "endpoint_family": _endpoint_family_from_state(state, site=site),
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
            "spec_kill_samples": _public_capability_status(public_api_access),
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




def _zone_payload(zone: dict[str, Any]) -> dict[str, Any]:
    expansion = dict_at(zone, "expansion")
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
            for partition in (list_at(zone, "partitions"))
            if isinstance(partition, dict)
        ],
    }


def _encounter_payload(encounter: dict[str, Any]) -> dict[str, Any]:
    zone = dict_at(encounter, "zone")
    expansion = dict_at(zone, "expansion")
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
    faction = dict_at(guild, "faction")
    server = dict_at(guild, "server")
    zone_ranking = dict_at(guild, "zoneRanking")
    progress = dict_at(zone_ranking, "progress")
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
    server = dict_at(guild, "server")
    zone_ranking = dict_at(guild, "zoneRanking")
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
    faction = dict_at(character, "faction")
    server = dict_at(character, "server")
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
    server = dict_at(guild, "server")
    members = dict_at(guild, "members")
    rows = [row for row in (list_at(members, "data")) if isinstance(row, dict)]
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
    server = dict_at(guild, "server")
    attendance = dict_at(guild, "attendance")
    rows = [row for row in (list_at(attendance, "data")) if isinstance(row, dict)]
    attendance_rows = []
    for row in rows:
        zone = dict_at(row, "zone")
        players = [player for player in (list_at(row, "players")) if isinstance(player, dict)]
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



def _character_payload(character: dict[str, Any]) -> dict[str, Any]:
    faction = dict_at(character, "faction")
    server = dict_at(character, "server")
    guilds = character.get("guilds")
    normalized_guilds: list[dict[str, Any]] = []
    if isinstance(guilds, list):
        for guild in guilds:
            if not isinstance(guild, dict):
                continue
            guild_server = dict_at(guild, "server")
            normalized_guilds.append(
                {
                    "id": guild.get("id"),
                    "name": guild.get("name"),
                    "server": _server_payload(guild_server) if guild_server else None,
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





def _warcraftlogs_command_prefix(site: WarcraftLogsSiteProfile) -> str:
    if site.key == RETAIL_PROFILE.key:
        return "warcraftlogs"
    return f"warcraftlogs --site {shlex.quote(site.key)}"


def _report_discovery_hint(query: str, *, site: WarcraftLogsSiteProfile) -> dict[str, Any]:
    command_prefix = _warcraftlogs_command_prefix(site)
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
            f"{site.root_url}/reports/<code>#fight=<id>",
            "<report_code>",
        ],
        "suggested_commands": [
            f"{command_prefix} report <report_code>",
            f"{command_prefix} report-encounter <report_code> --fight-id <id>",
        ],
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


def _report_discovery_candidate(ref: ReportReference, *, site: WarcraftLogsSiteProfile) -> dict[str, Any]:
    quoted_reference = shlex.quote(ref.code)
    command_prefix = _warcraftlogs_command_prefix(site)
    if ref.fight_id is None:
        kind = "report"
        next_command = f"{command_prefix} report {quoted_reference}"
        score = 92
        reasons = ["explicit_report_reference", "report_code"]
    else:
        kind = "report_encounter"
        next_command = f"{command_prefix} report-encounter {quoted_reference} --fight-id {ref.fight_id}"
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


def _report_search_payload(query: str, *, ref: ReportReference | None, site: WarcraftLogsSiteProfile) -> dict[str, Any]:
    if ref is None:
        return _report_discovery_hint(query, site=site)
    candidate = _report_discovery_candidate(ref, site=site)
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


def _report_resolve_payload(query: str, *, ref: ReportReference | None, site: WarcraftLogsSiteProfile) -> dict[str, Any]:
    if ref is None:
        hint = _report_discovery_hint(query, site=site)
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
    candidate = _report_discovery_candidate(ref, site=site)
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


# Report reference, report, selected fight, and the encounter the fight belongs to (when known).
_EncounterScope = tuple[ReportReference, dict[str, Any], dict[str, Any], dict[str, Any] | None]


def _resolve_encounter_scope(
    ctx: typer.Context,
    *,
    client: WarcraftLogsClient,
    reference: str,
    fight_id: int | None,
    allow_unlisted: bool,
) -> _EncounterScope:
    try:
        ref = _parse_report_reference(reference, explicit_fight_id=fight_id)
    except ValueError as exc:
        _fail(ctx, "invalid_query", str(exc))
    report = client.report(code=ref.code, allow_unlisted=allow_unlisted)
    fights_report = client.report_fights(code=ref.code, difficulty=None, allow_unlisted=allow_unlisted)
    fights = list_at(fights_report, "fights")
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


def _emitted_finished_report_ttl(client: WarcraftLogsClient) -> int | None:
    """Finished-report TTL as it should appear in emitted provenance/freshness.

    Returns ``None`` when caching is disabled (``WARCRAFTLOGS_CACHE_BACKEND=none|off|disabled``),
    so the trust metadata never claims a TTL for data that is never stored.
    """
    return client._finished_report_ttl if client._cache_store is not None else None


def _emitted_report_ttl(client: WarcraftLogsClient) -> int | None:
    """Short report TTL as it should appear in emitted provenance; ``None`` when caching is off."""
    return client._report_ttl if client._cache_store is not None else None


def _encounter_summary_payload(*, ref: ReportReference, report: dict[str, Any],
                               fight: dict[str, Any], encounter: dict[str, Any] | None,
                               finished_report_ttl: int | None = 86400, report_ttl: int | None = 60) -> dict[str, Any]:
    encounter_payload = None
    encounter_identity = encounter_identity_payload(
        encounter_id=fight.get("encounterID") if isinstance(fight.get("encounterID"), int) else None,
        name=fight.get("name") if isinstance(fight.get("name"), str) else None,
        provider="warcraftlogs",
        source="report_encounter",
        notes=["canonical only within explicit encounter metadata returned by Warcraft Logs"],
    )
    if isinstance(encounter, dict):
        zone = dict_at(encounter, "zone")
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
        # cache_provenance describes the *report's* finish state, resolved from the report
        # metadata lookup (client.report(); REPORT_QUERY selects report-level endTime). It is a
        # property of the log, not a per-namespace cache-entry audit: an encounter command may
        # also return data from other namespaces (report_fights/report_player_details/...), and
        # during the bounded live->finished window (<= the live TTL) those entries can briefly
        # disagree with the report's resolved finish state. See docs/warcraftlogs/CACHING.md.
        "cache_provenance": _report_cache_provenance(
            report,
            finished_ttl=finished_report_ttl,
            live_ttl=report_ttl,
            source="report_detail",
        ),
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
    absolute_start = float(fight_start) + \
        float(window_start_ms) if window_start_ms is not None and isinstance(fight_start, (int, float)) else None
    absolute_end = float(fight_start) + \
        float(window_end_ms) if window_end_ms is not None and isinstance(fight_start, (int, float)) else None
    if absolute_start is not None and absolute_end is not None and absolute_end < absolute_start:
        _fail(ctx, "invalid_query", "--window-end-ms must be greater than or equal to --window-start-ms.")
    return absolute_start, absolute_end


@dataclass(frozen=True, slots=True)
class _EncounterFilters:
    """Per-command filter inputs for one encounter slice, before fight-relative resolution.

    Window offsets are encounter-relative milliseconds; ``_encounter_filter_options``
    turns them into the absolute report timestamps the transport wants.
    """

    data_type: str
    ability_id: float | None = None
    source_id: int | None = None
    target_id: int | None = None
    hostility_type: str | None = None
    translate: bool | None = None
    view_by: str | None = None
    limit: int | None = None
    wipe_cutoff: int | None = None
    window_start_ms: float | None = None
    window_end_ms: float | None = None


def _encounter_filter_options(
    ctx: typer.Context,
    fight: dict[str, Any],
    filters: _EncounterFilters,
) -> tuple[ReportFilterOptions, dict[str, Any]]:
    start_time, end_time = _encounter_window_bounds(
        ctx,
        fight=fight,
        window_start_ms=filters.window_start_ms,
        window_end_ms=filters.window_end_ms,
    )
    encounter_id = fight.get("encounterID") if isinstance(fight.get("encounterID"), int) else None
    fight_ids = [int(fight["id"])] if isinstance(fight.get("id"), int) else None
    options = ReportFilterOptions(
        ability_id=filters.ability_id,
        data_type=filters.data_type,
        encounter_id=encounter_id,
        end_time=end_time,
        fight_ids=fight_ids,
        hostility_type=filters.hostility_type,
        kill_type=_kill_type_for_fight(fight),
        limit=filters.limit,
        source_id=filters.source_id,
        start_time=start_time,
        target_id=filters.target_id,
        translate=filters.translate,
        view_by=filters.view_by,
        wipe_cutoff=filters.wipe_cutoff,
    )
    query = {
        "ability_id": filters.ability_id,
        "data_type": filters.data_type,
        "encounter_id": encounter_id,
        "fight_ids": fight_ids,
        "hostility_type": filters.hostility_type,
        "kill_type": _kill_type_for_fight(fight),
        "limit": filters.limit,
        "source_id": filters.source_id,
        "target_id": filters.target_id,
        "translate": filters.translate,
        "view_by": filters.view_by,
        "wipe_cutoff": filters.wipe_cutoff,
        "window_start_ms": filters.window_start_ms,
        "window_end_ms": filters.window_end_ms,
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


@dataclass(frozen=True, slots=True)
class _CastNaming:
    """Master-data lookups plus the report scope every cast identity in one fight is stamped with."""

    actor_index: dict[int, dict[str, Any]]
    ability_index: dict[int, dict[str, Any]]
    report_code: str | None
    fight_id: int | None

    def actor(self, actor_id: int | None) -> dict[str, Any] | None:
        return _named_actor(
            self.actor_index,
            actor_id,
            report_code=self.report_code,
            fight_id=self.fight_id,
            source="report_encounter_casts",
        )

    def ability(self, ability_id: int | None) -> dict[str, Any] | None:
        return _named_ability(self.ability_index, ability_id, source="report_encounter_casts")


@dataclass(slots=True)
class _CastTallies:
    """Cast counts for one fight, keyed by identity pair, plus the bounded event preview."""

    by_source: dict[tuple[int | None, str | None], int]
    by_target: dict[tuple[int | None, str | None], int]
    by_ability: dict[tuple[int | None, str | None], int]
    by_source_ability: dict[tuple[int | None, int | None], int]
    by_source_target: dict[tuple[int | None, int | None], int]
    preview: list[dict[str, Any]]


def _cast_preview_row(row: dict[str, Any], *, named: dict[str, Any], fight_start: float | None) -> dict[str, Any]:
    timestamp = row.get("timestamp")
    relative_ms = None
    if isinstance(timestamp, (int, float)) and isinstance(fight_start, (int, float)):
        relative_ms = float(timestamp) - float(fight_start)
    return {
        "timestamp": timestamp,
        "relative_time_ms": relative_ms,
        **named,
        "type": row.get("type"),
    }


def _tally_cast_events(
    cast_rows: list[dict[str, Any]],
    *,
    naming: _CastNaming,
    fight_start: float | None,
    preview_limit: int,
) -> _CastTallies:
    tallies = _CastTallies({}, {}, {}, {}, {}, [])
    for row in cast_rows:
        source_id = _event_id(row.get("sourceID"))
        target_id = _event_id(row.get("targetID"))
        ability_id = _event_id(row.get("abilityGameID"))
        source = naming.actor(source_id)
        target = naming.actor(target_id)
        ability = naming.ability(ability_id)
        source_key = (source_id, str(source.get("name") if source else None))
        target_key = (target_id, str(target.get("name") if target else None))
        ability_key = (ability_id, str(ability.get("name") if ability else None))
        tallies.by_source[source_key] = tallies.by_source.get(source_key, 0) + 1
        tallies.by_target[target_key] = tallies.by_target.get(target_key, 0) + 1
        tallies.by_ability[ability_key] = tallies.by_ability.get(ability_key, 0) + 1
        tallies.by_source_ability[(source_id, ability_id)] = tallies.by_source_ability.get((source_id, ability_id), 0) + 1
        tallies.by_source_target[(source_id, target_id)] = tallies.by_source_target.get((source_id, target_id), 0) + 1
        if len(tallies.preview) < preview_limit:
            tallies.preview.append(
                _cast_preview_row(
                    row,
                    named={"source": source, "target": target, "ability": ability},
                    fight_start=fight_start,
                )
            )
    return tallies


def _sorted_cast_rows(
    counts: dict[tuple[Any, Any], int],
    *,
    naming: _CastNaming,
    field: Literal["source", "target", "ability"],
) -> list[dict[str, Any]]:
    """Counts sorted by descending count then name, each re-resolved to a full identity payload."""
    rows_out: list[dict[str, Any]] = []
    for (numeric_id, name), count in sorted(counts.items(), key=lambda item: (-item[1], str(item[0][1] or ""))):
        identity_id = numeric_id if isinstance(numeric_id, int) else None
        if field == "ability":
            named = naming.ability(identity_id) or {"game_id": numeric_id, "name": name}
        else:
            named = naming.actor(identity_id) or {"id": numeric_id, "name": name}
        rows_out.append({"count": count, field: named})
    return rows_out


def _cast_pair_rows(
    counts: dict[tuple[int | None, int | None], int],
    *,
    naming: _CastNaming,
    second: Literal["target", "ability"],
) -> list[dict[str, Any]]:
    """Source-keyed pair counts, sorted by descending count then by the raw id pair."""
    return [
        {
            "count": count,
            "source": naming.actor(source_id),
            second: naming.actor(other_id) if second == "target" else naming.ability(other_id),
        }
        for (source_id, other_id), count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]


def _encounter_cast_rows_payload(
    *,
    report: dict[str, Any],
    fight: dict[str, Any],
    events_report: dict[str, Any],
    master_report: dict[str, Any],
    preview_limit: int,
) -> dict[str, Any]:
    actor_index, ability_index = _master_data_indexes(master_report)
    naming = _CastNaming(
        actor_index=actor_index,
        ability_index=ability_index,
        report_code=report.get("code") if isinstance(report.get("code"), str) else None,
        fight_id=fight.get("id") if isinstance(fight.get("id"), int) else None,
    )
    paginator = dict_at(events_report, "events")
    cast_rows = [row for row in list_at(paginator, "data") if isinstance(row, dict)]
    tallies = _tally_cast_events(
        cast_rows,
        naming=naming,
        fight_start=fight.get("startTime") if isinstance(fight.get("startTime"), (int, float)) else None,
        preview_limit=preview_limit,
    )
    return {
        "report": _report_brief_payload(report),
        "fight": _fight_payload(fight),
        "casts": {
            "event_count": len(cast_rows),
            "next_page_timestamp": paginator.get("nextPageTimestamp"),
            "by_source": _sorted_cast_rows(tallies.by_source, naming=naming, field="source"),
            "by_target": _sorted_cast_rows(tallies.by_target, naming=naming, field="target"),
            "by_ability": _sorted_cast_rows(tallies.by_ability, naming=naming, field="ability"),
            "by_source_ability": _cast_pair_rows(tallies.by_source_ability, naming=naming, second="ability"),
            "by_source_target": _cast_pair_rows(tallies.by_source_target, naming=naming, second="target"),
            "preview": tallies.preview,
        },
    }


def _buff_aura_payload(
    entry: dict[str, Any],
    *,
    ability_index: dict[int, dict[str, Any]],
    ability_id: int | None,
) -> dict[str, Any]:
    # Two row shapes: aura-aggregate rows (table.data.auras) carry their own `guid`/`name`;
    # actor-scoped rows under an --ability-id filter (table.data.entries) carry the actor name
    # in `name` and no `guid` — the aura there is the requested filter, not the row.
    aura_guid = entry.get("guid") if isinstance(entry.get("guid"), int) else None
    if aura_guid is not None:
        aura_name = entry.get("name") if isinstance(entry.get("name"), str) else None
        ability_meta = ability_index.get(aura_guid)
        return {
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
    if ability_id is not None:
        return _named_ability(ability_index, ability_id, source="report_encounter_buffs") or {
            "game_id": ability_id,
            "name": f"ability:{ability_id}",
            "identity_contract": ability_identity_payload(
                game_id=ability_id,
                name=f"ability:{ability_id}",
                provider="warcraftlogs",
                source="report_encounter_buffs",
                notes=["ability id was requested explicitly but no matching master-data ability row was found"],
            ),
        }
    return {
        "game_id": None,
        "name": None,
        "identity_contract": ability_identity_payload(
            game_id=None,
            name=None,
            provider="warcraftlogs",
            source="report_encounter_buffs",
            notes=["row carried no aura identity; pass --ability-id to scope to a specific aura"],
        ),
    }


def _encounter_buff_rows_payload(
    *,
    report: dict[str, Any],
    fight: dict[str, Any],
    table_report: dict[str, Any],
    master_report: dict[str, Any],
    preview_limit: int,
    view_by: str | None,
    ability_id: int | None,
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
        aura_payload = _buff_aura_payload(entry, ability_index=ability_index, ability_id=ability_id)
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
            str((row.get(actor_field) or {}).get("name") or ""),
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






def _resolve_encounter_by_id(
    ctx: typer.Context,
    *,
    client: WarcraftLogsClient,
    zone_id: int,
    boss_id: int,
    boss_name: str | None,
    encounters: list[dict[str, Any]],
) -> dict[str, Any]:
    zone_match = next((row for row in encounters if row.get("id") == boss_id), None)
    if zone_match is None:
        _fail(ctx, "not_found", f"Encounter {boss_id} was not found in zone {zone_id}.")
    if boss_name and not _boss_matches(zone_match, boss_id=None, boss_name=boss_name):
        _fail(ctx, "boss_scope_mismatch", f"Encounter {boss_id} in zone {zone_id} does not match boss name {boss_name!r}.")
    encounter = client.encounter(encounter_id=boss_id)
    encounter_zone = dict_at(encounter, "zone")
    if encounter_zone.get("id") != zone_id:
        _fail(ctx, "boss_scope_mismatch", f"Encounter {boss_id} does not belong to zone {zone_id}.")
    return encounter


def _resolve_encounter_by_name(
    ctx: typer.Context,
    *,
    client: WarcraftLogsClient,
    zone_id: int,
    boss_name: str | None,
    encounters: list[dict[str, Any]],
) -> dict[str, Any]:
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


def _resolve_encounter(
    ctx: typer.Context,
    *,
    client: WarcraftLogsClient,
    zone_id: int,
    boss_id: int | None,
    boss_name: str | None,
) -> dict[str, Any]:
    zone = client.zone(zone_id=zone_id)
    encounters = [row for row in (list_at(zone, "encounters")) if isinstance(row, dict)]
    if boss_id is not None:
        return _resolve_encounter_by_id(
            ctx, client=client, zone_id=zone_id, boss_id=boss_id, boss_name=boss_name, encounters=encounters
        )
    return _resolve_encounter_by_name(
        ctx, client=client, zone_id=zone_id, boss_name=boss_name, encounters=encounters
    )


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
    other_players = row.get("otherPlayers")
    if isinstance(other_players, list):
        return len(other_players)
    all_characters = row.get("allCharacters")
    if isinstance(all_characters, list):
        return max(len(all_characters) - 1, 0)
    return 0


def _first_non_empty_str(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value
    return None


def _first_int(*values: Any) -> int | None:
    for value in values:
        if isinstance(value, int):
            return value
    return None


def _encounter_ranking_row_payload(
    row: dict[str, Any],
    *,
    page: int,
    row_index: int,
    site: WarcraftLogsSiteProfile,
) -> dict[str, Any]:
    report = dict_at(row, "report")
    server = dict_at(row, "server")
    guild = dict_at(row, "guild")
    class_name = row.get("className") if isinstance(row.get("className"), str) else row.get("class")
    spec_name = row.get("spec") if isinstance(row.get("spec"), str) else row.get("specName")
    report_code = _first_non_empty_str(
        row.get("reportCode"), row.get("reportID"), row.get("code"), report.get("code")
    )
    fight_id = _first_int(
        row.get("fightID"), row.get("fightId"), report.get("fightID"), report.get("fightId")
    )
    return {
        "name": row.get("name"),
        "server_name": _first_non_empty_str(row.get("serverName"), server.get("name")),
        "server_region": _first_non_empty_str(row.get("serverRegion"), server.get("region")),
        "guild_name": _first_non_empty_str(row.get("guildName"), guild.get("name")),
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
        "report_url": _report_url(report_code, fight_id=fight_id, root_url=site.root_url),
        "has_combatant_info": _encounter_ranking_has_combatant_info(row),
        "other_players_count": _encounter_ranking_other_players_count(row),
    }


def _encounter_rankings_payload(
    *,
    encounter: dict[str, Any],
    rankings: Any,
    query: dict[str, Any],
    top: int,
    site: WarcraftLogsSiteProfile,
) -> dict[str, Any]:
    page = rankings.get("page") if isinstance(rankings, dict) and isinstance(rankings.get("page"), int) else query.get("page")
    if not isinstance(page, int) or page < 1:
        page = 1
    normalized_rows = [
        _encounter_ranking_row_payload(row, page=page, row_index=row_index, site=site)
        for row_index, row in enumerate(_encounter_rankings_rows(rankings))
    ]
    returned = normalized_rows[:top]
    page_count: int = rankings["count"] if isinstance(rankings, dict) and isinstance(rankings.get("count"), int) else len(normalized_rows)
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


def _player_talent_transport_validation(
    actor: dict[str, Any],
    *,
    raw_rows: list[dict[str, Any]] | None = None,
    backend: TalentTransportBackend | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    if raw_rows is None:
        raw_rows, _ = _normalized_talent_tree_rows(actor)
    actor_class, spec = _player_talent_transport_identity(actor)
    validation_result = validate_talent_tree_transport(
        actor_class=actor_class,
        spec=spec,
        talent_tree_rows=raw_rows,
        backend=backend,
    )
    transport_forms = dict_at(validation_result, "transport_forms")
    validation = dict_at(validation_result, "validation")
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
    backend: TalentTransportBackend | None = None,
) -> dict[str, Any]:
    actor_class, spec = _player_talent_transport_identity(actor)
    raw_rows, transport_forms, validation = _player_talent_transport_validation(actor, raw_rows=raw_rows, backend=backend)
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









def _accumulate_boss_spec_counts(
    rows: list[dict[str, Any]],
) -> tuple[dict[tuple[str, str], dict[str, Any]], int]:
    spec_counts: dict[tuple[str, str], dict[str, Any]] = {}
    sampled_player_rows = 0
    for row in rows:
        code = str((row.get("report") or {}).get("code") or "")
        fight_id = int((row.get("fight") or {}).get("id") or 0)
        player_rows = list_at(row, "player_details")
        seen_specs_for_fight: set[tuple[str, int, str, str]] = set()
        for player in player_rows:
            if not isinstance(player, dict):
                continue
            sampled_player_rows += 1
            role = str(player.get("role") or "unknown")
            specs = list_at(player, "specs")
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
    return spec_counts, sampled_player_rows


def _boss_spec_usage_payload(
    *,
    rows: list[dict[str, Any]],
    sample: dict[str, Any],
    query: dict[str, Any],
    top: int,
    cache_ttl_seconds: int | None = None,
    root_url: str = "https://www.warcraftlogs.com",
) -> dict[str, Any]:
    spec_counts, sampled_player_rows = _accumulate_boss_spec_counts(rows)

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
        "freshness": _sampled_cross_report_freshness(cache_ttl_seconds),
        "cache_provenance": _sampled_cache_provenance(cache_ttl_seconds),
        "sample_scope": _sampled_sample_scope(
            ranking_basis="sampled_finished_kill_cohort_spec_presence",
            query=query,
            returned=len(returned),
            excluded=max(0, len(normalized_rows) - len(returned)),
            truncated=len(normalized_rows) > top,
        ),
        "citations": _sampled_cross_report_citations(rows, root_url=root_url),
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


def _sampled_kill_row_scope(row: dict[str, Any]) -> tuple[str, int] | None:
    """Report code and fight ID of one sampled kill row, or ``None`` when the row cannot be scoped."""
    report_code = dict_at(row, "report").get("code")
    fight_id = dict_at(row, "fight").get("id")
    if not isinstance(report_code, str) or not isinstance(fight_id, int):
        return None
    return report_code, fight_id


def _fight_player_details(client: WarcraftLogsClient, row: dict[str, Any], *, difficulty: int | None) -> dict[str, Any]:
    """Combatant-info player details for the fight behind one sampled kill row."""
    fight_payload = dict_at(row, "fight")
    fight_id = fight_payload.get("id")
    encounter_id = fight_payload.get("encounter_id")
    return client.report_player_details(
        code=str(dict_at(row, "report").get("code") or ""),
        allow_unlisted=False,
        options=ReportPlayerDetailsOptions(
            difficulty=int(fight_payload["difficulty"]) if isinstance(fight_payload.get("difficulty"), int) else difficulty,
            encounter_id=int(encounter_id) if isinstance(encounter_id, int) else None,
            fight_ids=[int(fight_id)] if isinstance(fight_id, int) else None,
            include_combatant_info=True,
            kill_type="Kills",
        ),
        ttl_override=client._finished_report_ttl,
    )


def _collect_comp_sample_rows(client: WarcraftLogsClient, scope: CrossReportScope) -> dict[str, Any]:
    analytics = _collect_boss_kill_rows(client, scope)
    composed_rows = [
        _composition_sample_row(row, details_report=_fight_player_details(client, row, difficulty=scope.difficulty))
        for row in analytics["rows"]
        if _sampled_kill_row_scope(row) is not None
    ]
    return {
        "rows": composed_rows,
        "sample": analytics["sample"],
    }


def _record_comp_class_presence(
    class_presence: dict[str, dict[str, Any]],
    class_rows: list[Any],
    *,
    report_code: str,
    fight_id: int,
) -> None:
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


def _record_comp_signature(
    signature_counts: dict[str, dict[str, Any]],
    class_signature: Any,
    *,
    report_code: str,
    fight_id: int,
) -> None:
    if not (isinstance(class_signature, str) and class_signature):
        return
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


def _accumulate_comp_presence(
    rows: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], int]:
    class_presence: dict[str, dict[str, Any]] = {}
    signature_counts: dict[str, dict[str, Any]] = {}
    sampled_player_count = 0
    for row in rows:
        report_code = str((row.get("report") or {}).get("code") or "")
        fight_id = int((row.get("fight") or {}).get("id") or 0)
        player_details = dict_at(row, "player_details")
        players = list_at(player_details, "players")
        sampled_player_count += len([player for player in players if isinstance(player, dict)])
        composition = dict_at(row, "composition")
        class_rows = list_at(composition, "class_counts")
        _record_comp_class_presence(class_presence, class_rows, report_code=report_code, fight_id=fight_id)
        _record_comp_signature(
            signature_counts, composition.get("class_signature"), report_code=report_code, fight_id=fight_id
        )
    return class_presence, signature_counts, sampled_player_count


def _comp_samples_payload(
    *,
    rows: list[dict[str, Any]],
    sample: dict[str, Any],
    query: dict[str, Any],
    top: int,
    cache_ttl_seconds: int | None = None,
    root_url: str = "https://www.warcraftlogs.com",
) -> dict[str, Any]:
    class_presence, signature_counts, sampled_player_count = _accumulate_comp_presence(rows)

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
        "freshness": _sampled_cross_report_freshness(cache_ttl_seconds),
        "cache_provenance": _sampled_cache_provenance(cache_ttl_seconds),
        "sample_scope": _sampled_sample_scope(
            ranking_basis="sampled_fastest_kills",
            query=query,
            returned=len(returned),
            excluded=max(0, len(rows) - len(returned)),
            truncated=len(rows) > top,
        ),
        "citations": _sampled_cross_report_citations(rows, root_url=root_url),
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


def _ability_cast_summary(
    events_report: dict[str, Any],
    *,
    actor_index: dict[int, dict[str, Any]],
    report_code: str,
    fight_id: int,
) -> dict[str, Any]:
    """Cast totals and per-source rows for one sampled kill's ability event slice."""
    paginator = dict_at(events_report, "events")
    event_rows = [event for event in list_at(paginator, "data") if isinstance(event, dict)]
    source_counts: dict[int, int] = {}
    for event in event_rows:
        source_id = _event_id(event.get("sourceID"))
        if isinstance(source_id, int):
            source_counts[source_id] = source_counts.get(source_id, 0) + 1
    return {
        "count": len(event_rows),
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
    }


def _unresolved_ability_identity(ability_id: int) -> dict[str, Any]:
    """Ability identity for an explicitly filtered ability that never appeared in sampled master data."""
    return {
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


def _collect_ability_usage_rows(
    client: WarcraftLogsClient,
    scope: CrossReportScope,
    *,
    ability_id: int,
    event_limit: int,
) -> dict[str, Any]:
    analytics = _collect_boss_kill_rows(client, scope)
    master_cache: dict[str, dict[str, Any]] = {}
    usage_rows: list[dict[str, Any]] = []
    resolved_ability: dict[str, Any] | None = None

    for row in analytics["rows"]:
        row_scope = _sampled_kill_row_scope(row)
        if row_scope is None:
            continue
        report_code, fight_id = row_scope
        encounter_id = dict_at(row, "fight").get("encounter_id")
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
        usage_rows.append(
            {
                **row,
                "casts": _ability_cast_summary(
                    events_report,
                    actor_index=actor_index,
                    report_code=report_code,
                    fight_id=fight_id,
                ),
            }
        )

    return {
        "rows": usage_rows,
        "sample": analytics["sample"],
        "ability": resolved_ability or _unresolved_ability_identity(ability_id),
    }


def _ability_usage_summary_payload(
    *,
    rows: list[dict[str, Any]],
    sample: dict[str, Any],
    query: dict[str, Any],
    ability: dict[str, Any],
    preview_limit: int,
    event_limit: int,
    cache_ttl_seconds: int | None = None,
    root_url: str = "https://www.warcraftlogs.com",
) -> dict[str, Any]:
    cast_counts = [
        int(casts["count"])
        for casts in (row.get("casts") for row in rows)
        if isinstance(casts, dict) and isinstance(casts.get("count"), int)
    ]
    used_counts = [count for count in cast_counts if count > 0]
    preview = rows[:preview_limit]
    # The emitted `query` block widens the caller's query with the event/preview limits;
    # sample_scope.filters must project from the SAME widened query so the two cannot drift
    # (the contract in docs/warcraftlogs/CACHING.md).
    scoped_query = {
        **query,
        "event_limit": event_limit,
        "preview_limit": preview_limit,
    }
    return {
        "ok": True,
        "provider": "warcraftlogs",
        "kind": "ability_usage_summary",
        "ranking_basis": "sampled_fastest_kills",
        "matching_rule": "ability_casts_across_sampled_finished_kills_with_event_limit",
        "query": scoped_query,
        "notes": _sampled_spec_filter_notes(query.get("spec_name") if isinstance(query, dict) else None),
        "freshness": _sampled_cross_report_freshness(cache_ttl_seconds),
        "cache_provenance": _sampled_cache_provenance(cache_ttl_seconds),
        "sample_scope": _sampled_sample_scope(
            ranking_basis="sampled_fastest_kills",
            query=scoped_query,
            returned=len(preview),
            excluded=max(0, len(rows) - len(preview)),
            truncated=len(rows) > preview_limit,
        ),
        "citations": _sampled_cross_report_citations(rows, root_url=root_url),
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


def _report_events_payload(report: dict[str, Any]) -> dict[str, Any]:
    paginator = dict_at(report, "events")
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
    table = dict_at(report, "table")
    container = dict_at(table, "data") or table
    rows = container.get("entries")
    if not isinstance(rows, list):
        rows = list_at(container, "auras")
    return [row for row in rows if isinstance(row, dict)]


def _report_master_data_payload(report: dict[str, Any]) -> dict[str, Any]:
    master_data = dict_at(report, "masterData")
    abilities = list_at(master_data, "abilities")
    actors = list_at(master_data, "actors")
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
    specs = list_at(actor, "specs")
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


def _report_player_details_payload(report: dict[str, Any], *, report_code: str |
                                   None = None, fight_id: int | None = None) -> dict[str, Any]:
    details = dict_at(report, "playerDetails")
    data = dict_at(details, "data")
    role_data = dict_at(data, "playerDetails") or data
    roles: dict[str, list[dict[str, Any]]] = {}
    counts: dict[str, int] = {}
    for role in ("tanks", "healers", "dps"):
        rows = list_at(role_data, role)
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
            str((row.get("source") or {}).get("name") or ""),
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
            str((row.get("source") or {}).get("name") or ""),
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
            str((row.get("target") or {}).get("name") or ""),
        )
    )
    return {
        "report": _report_brief_payload(table_report),
        "damage_summary": {
            "entry_count": len(rows_out),
            "rows": rows_out,
        },
    }


def _aura_summary_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Rows of an aura-summary payload, or an empty list when the summary is missing."""
    return [row for row in list_at(dict_at(payload, "aura_summary"), "rows") if isinstance(row, dict)]


def _aura_compare_rows(
    *,
    left_rows: list[dict[str, Any]],
    right_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    def _row_key(row: dict[str, Any]) -> tuple[int | None, str]:
        source = dict_at(row, "source")
        source_id = source.get("id") if isinstance(source.get("id"), int) else None
        source_name = str(source.get("name") or "")
        return source_id, source_name

    def _compare_row(
        key: tuple[int | None, str],
        left: dict[str, Any] | None,
        right: dict[str, Any] | None,
    ) -> dict[str, Any]:
        left_total = left.get("reported_total") if isinstance(left, dict) else None
        right_total = right.get("reported_total") if isinstance(right, dict) else None
        left_active = left.get("reported_active_time") if isinstance(left, dict) else None
        right_active = right.get("reported_active_time") if isinstance(right, dict) else None
        return {
            "source": (
                left.get("source")
                if isinstance(left, dict) and isinstance(left.get("source"), dict)
                else (
                    right.get("source")
                    if isinstance(right, dict) and isinstance(right.get("source"), dict)
                    else {"id": key[0], "name": key[1]}
                )
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

    left_index = {_row_key(row): row for row in left_rows if isinstance(row, dict)}
    right_index = {_row_key(row): row for row in right_rows if isinstance(row, dict)}
    combined_keys = sorted(set(left_index) | set(right_index), key=lambda item: (str(item[1]).lower(), item[0] or 0))
    compared: list[dict[str, Any]] = [
        _compare_row(key, left_index.get(key), right_index.get(key)) for key in combined_keys
    ]
    compared.sort(
        key=lambda row: (
            -abs(float(row["reported_total_delta"])) if isinstance(row.get("reported_total_delta"), (int, float)) else -1.0,
            str((row.get("source") or {}).get("name") or ""),
        )
    )
    return compared


def _character_rankings_payload(character: dict[str, Any], *, top: int) -> dict[str, Any]:
    server = dict_at(character, "server")
    faction = dict_at(character, "faction")
    rankings = dict_at(character, "zoneRankings")
    rankings_error = rankings.get("error") if isinstance(rankings.get("error"), str) else None
    all_stars = list_at(rankings, "allStars")
    ranking_rows = list_at(rankings, "rankings")
    all_star_specs = [
        row.get("spec")
        for row in all_stars
        if isinstance(row, dict) and isinstance(row.get("spec"), str) and row.get("spec")
    ]
    unique_specs = list(dict.fromkeys(all_star_specs))
    source_character_identity = class_spec_identity_payload(
        actor_class=None,
        spec=unique_specs[0] if len(unique_specs) == 1 else None,
        provider="warcraftlogs",
        source="character_rankings",
        candidates=[(None, spec) for spec in unique_specs] if len(unique_specs) > 1 else None,
        notes=[
            "warcraftlogs character-rankings exposes class only as an internal classID enum; "
            "class name is not normalized here"
        ],
    )
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
        "trust": {
            "ranking_basis": "public_character_zone_rankings",
            "scope": {
                "zone": rankings.get("zone"),
                "difficulty": rankings.get("difficulty"),
                "metric": rankings.get("metric"),
                "partition": rankings.get("partition"),
                "size": rankings.get("size"),
            },
            "freshness": _sampled_cross_report_freshness(),
            "source_character_identity": source_character_identity,
        },
        "raw": rankings,
    }


def _reports_payload(pagination: dict[str, Any]) -> dict[str, Any]:
    rows = list_at(pagination, "data")
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
    site: str = typer.Option(
        "retail",
        "--site",
        help="Warcraft Logs site profile: retail, classic, or fresh.",
    ),
    pretty: PrettyOption = False,
    compact: CompactOption = False,
    fields: FieldsOption = None,
    fields_strict: FieldsStrictOption = False,
    profile: ProfileOption = None,
    compact_max_chars: CompactMaxCharsOption = DEFAULT_COMPACT_MAX_CHARS,
) -> None:
    """Global options for every warcraftlogs command."""
    try:
        site_profile = resolve_site_profile(site)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--site") from exc
    configure(
        ctx,
        provider="warcraftlogs",
        pretty=pretty,
        compact=compact,
        fields=fields,
        fields_strict=fields_strict,
        profile=profile,
        compact_max_chars=compact_max_chars,
        config=RuntimeConfig(site_profile=site_profile),
    )


def _emit_surface(ctx: typer.Context, envelope: Envelope) -> None:
    """Emit a pure-surface envelope, keeping its body flattened at the top level for older agents."""
    _emit(ctx, {**envelope, **envelope["data"]})


@app.command("search")
def search(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="Explicit Warcraft Logs report URL or report code."),
    limit: int = typer.Option(5, "--limit", min=1, max=50,
                              help="Accepted for wrapper compatibility; explicit report discovery returns at most one result."),
) -> None:
    """Match an explicit Warcraft Logs report URL or code; free text returns a discovery hint."""
    _emit_surface(ctx, provider_search(query, limit=limit, site=_cfg(ctx).site_profile))


@app.command("resolve")
def resolve(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="Explicit Warcraft Logs report URL or report code."),
    limit: int = typer.Option(5, "--limit", min=1, max=50,
                              help="Accepted for wrapper compatibility; explicit report resolution returns at most one match."),
) -> None:
    """Resolve an explicit Warcraft Logs report URL or code to a single report reference."""
    del limit
    _emit_surface(ctx, provider_resolve(query, site=_cfg(ctx).site_profile))


@app.command("doctor")
def doctor(
    ctx: typer.Context,
    no_live: bool = typer.Option(False, "--no-live", help="Skip live Warcraft Logs auth probes and report local/runtime readiness only."),
) -> None:
    """Report Warcraft Logs auth, site profile, and per-command readiness."""
    _emit_surface(ctx, provider_doctor(live=not no_live, site=_cfg(ctx).site_profile))


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


def _validate_pending_site_profile(ctx: typer.Context, pending: dict[str, Any]) -> None:
    pending_site = pending.get("site_profile")
    selected_site = _cfg(ctx).site_profile.key
    if isinstance(pending_site, str) and pending_site and pending_site != selected_site:
        _fail(
            ctx,
            "site_profile_mismatch",
            (
                "Callback site profile did not match the pending authorization flow. "
                f"Re-run the callback command with `--site {pending_site}`."
            ),
        )


def _requested_scopes(scope: list[str]) -> list[str]:
    return [item.strip() for item in scope if item.strip()]


def _authorize_url_with_scopes(authorize_url: str, scopes: list[str]) -> str:
    if not scopes:
        return authorize_url
    joiner = "&" if "?" in authorize_url else "?"
    return f"{authorize_url}{joiner}scope={'+'.join(scopes)}"


def _emit_authorize_step(ctx: typer.Context, *, mode: str, redirect_uri: str, scope: list[str]) -> None:
    """Persist the pending OAuth state and emit the authorize URL for ``auth login``/``auth pkce-login``."""
    client = _client(ctx)
    try:
        pending_state = _random_state_token()
        scopes = _requested_scopes(scope)
        pending: dict[str, Any] = {
            "pending_auth_mode": mode,
            "pending_state": pending_state,
            "redirect_uri": redirect_uri,
            "requested_scopes": scopes,
            "site_profile": client.site.key,
        }
        if mode == "pkce":
            code_verifier = _pkce_verifier()
            pending["code_verifier"] = code_verifier
            authorize_url = client.pkce_code_url(
                redirect_uri=redirect_uri,
                state=pending_state,
                code_challenge=_pkce_challenge(code_verifier),
            )
        else:
            authorize_url = client.authorization_code_url(redirect_uri=redirect_uri, state=pending_state)
        authorize_url = _authorize_url_with_scopes(authorize_url, scopes)
        saved_path = save_provider_auth_state("warcraftlogs", pending)
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)
    finally:
        client.close()
    _emit(
        ctx,
        {
            "ok": True,
            "provider": "warcraftlogs",
            "mode": mode,
            "step": "authorize",
            "authorize_url": authorize_url,
            "redirect_uri": redirect_uri,
            "state": pending_state,
            "requested_scopes": scopes,
            "site_profile": _site_profile_payload(client.site),
            "state_path": str(saved_path),
        },
        client=client,
    )


def _validate_oauth_callback(
    ctx: typer.Context,
    pending: dict[str, Any],
    *,
    state: str | None,
    redirect_uri: str,
    flow_label: str,
    authorize_step_label: str,
) -> None:
    """Reject a callback that does not match the pending flow before any code is exchanged."""
    expected_state = pending.get("pending_state")
    if isinstance(expected_state, str) and expected_state:
        if not state:
            _fail(ctx, "missing_state", f"Missing callback state. Re-run {authorize_step_label} and provide the returned state value.")
        if state != expected_state:
            _fail(ctx, "state_mismatch", f"Callback state did not match the pending {flow_label}.")
    if isinstance(pending.get("redirect_uri"), str) and pending.get("redirect_uri") != redirect_uri:
        _fail(ctx, "redirect_uri_mismatch", f"Redirect URI did not match the pending {flow_label}.")


def _emit_token_step(
    ctx: typer.Context,
    *,
    mode: str,
    pending: dict[str, Any],
    payload: dict[str, Any],
    redirect_uri: str,
    client: WarcraftLogsClient,
) -> None:
    """Save the exchanged user token and emit the granted-scope summary."""
    token_summary = _token_payload_summary(payload, auth_mode=mode, redirect_uri=redirect_uri)
    token_summary["site_profile"] = _cfg(ctx).site_profile.key
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
            "mode": mode,
            "step": "token_exchanged",
            "endpoint_family": "user",
            "site_profile": _site_profile_payload(_cfg(ctx).site_profile),
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


def _active_auth_mode_from_state(state: dict[str, Any], *, site: WarcraftLogsSiteProfile | None = None) -> str:
    auth_mode = state.get("auth_mode")
    if state.get("has_access_token") and isinstance(auth_mode, str):
        if site is not None and auth_mode in {"authorization_code", "pkce"} and not _saved_user_token_matches_site(site, state=state):
            return "client_credentials"
        return auth_mode
    return "client_credentials"


def _endpoint_family_from_state(state: dict[str, Any], *, site: WarcraftLogsSiteProfile | None = None) -> str:
    if (
        state.get("has_access_token")
        and state.get("auth_mode") in {"authorization_code", "pkce"}
        and (site is None or _saved_user_token_matches_site(site, state=state))
    ):
        return "user"
    return "client"


@auth_app.command("status")
def auth_status(
    ctx: typer.Context,
    no_live: bool = typer.Option(False, "--no-live", help="Skip live Warcraft Logs auth probes and report local/runtime readiness only."),
) -> None:
    """Report saved Warcraft Logs credentials, token state, and public/user API readiness."""
    site = _cfg(ctx).site_profile
    auth = load_warcraftlogs_auth_config()
    credential_source = auth.env_file if auth.env_file is not None else ("environment" if auth.configured else None)
    state = provider_auth_status("warcraftlogs")
    runtime_access = _runtime_access_payload(site)
    public_api_access = _public_api_access_payload(
        auth_configured=auth.configured,
        runtime_access=runtime_access,
        live=not no_live,
        site=site,
    )
    user_api_access = _user_api_access_payload(
        state,
        runtime_access=runtime_access,
        live=not no_live,
        site=site,
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
                "site_profile": _site_profile_payload(site),
                "active_mode": _active_auth_mode_from_state(state, site=site),
                "endpoint_family": _endpoint_family_from_state(state, site=site),
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
    """Show the configured OAuth client and the endpoints of the selected site profile."""
    site = _cfg(ctx).site_profile
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
                "site_profile": site.key,
                "site": _site_profile_payload(site),
                "authorize_url": site.oauth_authorize_url,
                "token_url": site.oauth_token_url,
                "client_api_url": site.api_url,
                "user_api_url": site.user_api_url,
            },
        },
    )


@auth_app.command("token")
def auth_token(ctx: typer.Context) -> None:
    """Summarize the saved user token: type, expiry, and granted OAuth scopes."""
    site = _cfg(ctx).site_profile
    state = provider_auth_status("warcraftlogs")
    payload = _saved_provider_auth_payload(state)
    scopes = _scope_breakdown(
        granted_scope=payload.get("scope"),
        requested_scopes=payload.get("requested_scopes"),
        access_token=payload.get("access_token"),
    )
    scope_warning = scopes["warning"] if _saved_user_token_ready(state, site=site) else None
    _emit(
        ctx,
        {
            "ok": True,
            "provider": "warcraftlogs",
            "token": {
                "active_mode": _active_auth_mode_from_state(state, site=site),
                "endpoint_family": _endpoint_family_from_state(state, site=site),
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
    scope: list[str] = typer.Option(
        [],
        "--scope",
        help=(
            "OAuth scope. Use view-user-profile for currentUser/user data and "
            "view-private-reports for private/guild-stealth reports. Repeatable."
        ),
    ),
) -> None:
    """Start (or complete with --code) the authorization-code login that grants a user token."""
    if not code:
        _emit_authorize_step(ctx, mode="authorization_code", redirect_uri=redirect_uri, scope=scope)
        return

    pending = load_provider_auth_state("warcraftlogs") or {}
    _validate_pending_site_profile(ctx, pending)
    _validate_oauth_callback(
        ctx,
        pending,
        state=state,
        redirect_uri=redirect_uri,
        flow_label="authorization flow",
        authorize_step_label="the login URL step",
    )

    client = _client(ctx)
    try:
        payload = client.exchange_authorization_code(code=code, redirect_uri=redirect_uri)
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)
    finally:
        client.close()
    _emit_token_step(ctx, mode="authorization_code", pending=pending, payload=payload, redirect_uri=redirect_uri, client=client)


@auth_app.command("pkce-login")
def auth_pkce_login(
    ctx: typer.Context,
    redirect_uri: str = typer.Option(..., "--redirect-uri", help="Registered redirect URI for the Warcraft Logs OAuth client."),
    code: str | None = typer.Option(None, "--code", help="Authorization code returned by the redirect callback."),
    state: str | None = typer.Option(None, "--state", help="State value returned by the redirect callback."),
    scope: list[str] = typer.Option(
        [],
        "--scope",
        help=(
            "OAuth scope. Use view-user-profile for currentUser/user data and "
            "view-private-reports for private/guild-stealth reports. Repeatable."
        ),
    ),
) -> None:
    """Start (or complete with --code) the PKCE login that grants a user token without a client secret."""
    if not code:
        _emit_authorize_step(ctx, mode="pkce", redirect_uri=redirect_uri, scope=scope)
        return

    pending = load_provider_auth_state("warcraftlogs") or {}
    _validate_pending_site_profile(ctx, pending)
    code_verifier = pending.get("code_verifier")
    if not isinstance(code_verifier, str) or not code_verifier:
        _fail(
            ctx,
            "missing_code_verifier",
            "Missing pending PKCE verifier. Re-run `warcraftlogs auth pkce-login --redirect-uri ...` first.",
        )
    _validate_oauth_callback(
        ctx,
        pending,
        state=state,
        redirect_uri=redirect_uri,
        flow_label="PKCE flow",
        authorize_step_label="the PKCE login URL step",
    )

    client = _client(ctx)
    try:
        payload = client.exchange_pkce_code(code=code, redirect_uri=redirect_uri, code_verifier=code_verifier)
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)
    finally:
        client.close()
    _emit_token_step(ctx, mode="pkce", pending=pending, payload=payload, redirect_uri=redirect_uri, client=client)


@auth_app.command("logout")
def auth_logout(ctx: typer.Context) -> None:
    """Delete the saved Warcraft Logs user token from local state."""
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
    """Show the Warcraft Logs account the saved user token belongs to."""
    client = _client(ctx)
    try:
        payload = client.current_user()
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)
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
    """Show the remaining hourly Warcraft Logs API points for the configured client."""
    client = _client(ctx)
    try:
        payload = client.rate_limit()
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)
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
    """List Warcraft Logs regions and their realms."""
    client = _client(ctx)
    try:
        rows = client.regions()
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)
    finally:
        client.close()
    regions_payload = [_region_payload(region) for region in rows]
    _emit(ctx, {"ok": True, "provider": "warcraftlogs", "count": len(regions_payload), "regions": regions_payload}, client=client)


@app.command("expansions")
def expansions(ctx: typer.Context) -> None:
    """List Warcraft Logs expansions and their zones."""
    client = _client(ctx)
    try:
        rows = client.expansions()
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)
    finally:
        client.close()
    expansions_payload = [_expansion_payload(row) for row in rows]
    _emit(ctx, {"ok": True, "provider": "warcraftlogs", "count": len(expansions_payload), "expansions": expansions_payload}, client=client)


@app.command("server")
def server(
    ctx: typer.Context,
    region: str = typer.Argument(..., help="Realm region slug, for example us or eu."),
    slug: str = typer.Argument(..., help="Realm slug, for example illidan."),
) -> None:
    """Look up a realm by region and slug."""
    client = _client(ctx)
    try:
        payload = client.server(region=region, slug=slug)
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)
    finally:
        client.close()
    _emit(ctx, {"ok": True, "provider": "warcraftlogs", "server": _server_payload(payload)}, client=client)


@app.command("zones")
def zones(
    ctx: typer.Context,
    expansion_id: int | None = typer.Option(None, "--expansion-id", help="Optional Warcraft Logs expansion ID filter."),
) -> None:
    """List zones, optionally filtered to one expansion."""
    client = _client(ctx)
    try:
        rows = client.zones(expansion_id=expansion_id)
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)
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
def zone(
    ctx: typer.Context,
    zone_id: int = typer.Argument(..., help="Warcraft Logs zone ID."),
) -> None:
    """Show one zone with its difficulties, encounters, and partitions."""
    client = _client(ctx)
    try:
        payload = client.zone(zone_id=zone_id)
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)
    finally:
        client.close()
    _emit(ctx, {"ok": True, "provider": "warcraftlogs", "zone": _zone_payload(payload)}, client=client)


@app.command("encounter")
def encounter(
    ctx: typer.Context,
    encounter_id: int = typer.Argument(..., help="Warcraft Logs encounter ID."),
) -> None:
    """Show one encounter and the zone it belongs to."""
    client = _client(ctx)
    try:
        payload = client.encounter(encounter_id=encounter_id)
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)
    finally:
        client.close()
    zone = dict_at(payload, "zone")
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


@dataclass(frozen=True, slots=True)
class _EncounterRankingsRequest:
    """One encounter-rankings run: transport options plus the raw scope echoed back in ``query``."""

    zone_id: int
    boss_id: int | None
    boss_name: str | None
    class_name: str | None
    spec_name: str | None
    server_region: str | None
    top: int
    options: EncounterRankingsOptions


def _encounter_rankings_query(request: _EncounterRankingsRequest, encounter: dict[str, Any]) -> dict[str, Any]:
    options = request.options
    return {
        "zone_id": request.zone_id,
        "boss_id": encounter.get("id"),
        "boss_name": encounter.get("name"),
        "bracket": options.bracket,
        "difficulty": options.difficulty,
        "class_name": request.class_name,
        "spec_name": request.spec_name,
        "metric": options.metric,
        "page": options.page,
        "partition": options.partition,
        "size": options.size,
        "server_region": normalize_region(request.server_region) if request.server_region else None,
        "server_slug": options.server_slug,
        "leaderboard": options.leaderboard,
        "hard_mode_level": options.hard_mode_level,
        "filter": options.filter,
        "include_combatant_info": options.include_combatant_info,
        "include_other_players": options.include_other_players,
        "top": request.top,
    }


def _run_encounter_rankings(ctx: typer.Context, request: _EncounterRankingsRequest) -> None:
    client = _client(ctx)
    try:
        encounter_payload = _resolve_encounter(
            ctx,
            client=client,
            zone_id=request.zone_id,
            boss_id=request.boss_id,
            boss_name=request.boss_name,
        )
        rankings_payload = client.encounter_rankings(
            encounter_id=int(encounter_payload["id"]),
            options=request.options,
        )
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)
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
            query=_encounter_rankings_query(request, encounter_payload),
            top=request.top,
            site=client.site,
        ),
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
    """Rank characters on one encounter, filtered by class, spec, difficulty, and server."""
    _run_encounter_rankings(
        ctx,
        _EncounterRankingsRequest(
            zone_id=zone_id,
            boss_id=boss_id,
            boss_name=boss_name,
            class_name=class_name,
            spec_name=spec_name,
            server_region=server_region,
            top=top,
            options=EncounterRankingsOptions(
                bracket=bracket,
                difficulty=difficulty,
                page=page,
                partition=partition,
                size=size,
                server_region=server_region,
                server_slug=server_slug,
                leaderboard=_normalize_graphql_enum(leaderboard),
                hard_mode_level=_normalize_hard_mode_level_rank_filter(hard_mode_level),
                metric=metric,
                filter=filter_text,
                include_combatant_info=include_combatant_info,
                include_other_players=include_other_players,
                class_name=_normalize_encounter_ranking_class_name(class_name),
                spec_name=_normalize_encounter_ranking_spec_name(spec_name),
            ),
        ),
    )


@app.command("guild")
def guild(
    ctx: typer.Context,
    region: str = typer.Argument(..., help="Guild region slug, for example us or eu."),
    realm: str = typer.Argument(..., help="Guild realm slug or name."),
    name: str = typer.Argument(..., help="Guild name."),
    zone_id: int | None = typer.Option(None, "--zone-id", help="Optional Warcraft Logs zone ID for current guild ranking context."),
) -> None:
    """Look up a guild by region, realm, and name."""
    client = _client(ctx)
    try:
        payload = client.guild(region=region, realm=realm, name=name, zone_id=zone_id)
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)
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
    region: str = typer.Argument(..., help="Guild region slug, for example us or eu."),
    realm: str = typer.Argument(..., help="Guild realm slug or name."),
    name: str = typer.Argument(..., help="Guild name."),
    zone_id: int | None = typer.Option(None, "--zone-id", help="Optional Warcraft Logs zone ID."),
    size: int | None = typer.Option(None, "--size", help="Optional raid size."),
    difficulty: int | None = typer.Option(None, "--difficulty", help="Optional difficulty ID for speed ranks."),
) -> None:
    """Show a guild's progress and speed rankings for one zone."""
    client = _client(ctx)
    try:
        payload = client.guild_rankings(region=region, realm=realm, name=name, zone_id=zone_id, size=size, difficulty=difficulty)
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)
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
    region: str = typer.Argument(..., help="Guild region slug, for example us or eu."),
    realm: str = typer.Argument(..., help="Guild realm slug or name."),
    name: str = typer.Argument(..., help="Guild name."),
    limit: int = typer.Option(100, "--limit", min=1, max=100, help="Roster rows per page."),
    page: int = typer.Option(1, "--page", min=1, help="Page number."),
) -> None:
    """List a guild's roster."""
    client = _client(ctx)
    try:
        payload = client.guild_members(region=region, realm=realm, name=name, limit=limit, page=page)
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)
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
    region: str = typer.Argument(..., help="Guild region slug, for example us or eu."),
    realm: str = typer.Argument(..., help="Guild realm slug or name."),
    name: str = typer.Argument(..., help="Guild name."),
    guild_tag_id: int | None = typer.Option(None, "--guild-tag-id", help="Optional guild tag filter."),
    limit: int = typer.Option(16, "--limit", min=1, max=25, help="Attendance rows per page."),
    page: int = typer.Option(1, "--page", min=1, help="Page number."),
    zone_id: int | None = typer.Option(None, "--zone-id", help="Optional zone filter."),
) -> None:
    """Show a guild's raid attendance by report."""
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
def character(
    ctx: typer.Context,
    region: str = typer.Argument(..., help="Character region slug, for example us or eu."),
    realm: str = typer.Argument(..., help="Character realm slug or name."),
    name: str = typer.Argument(..., help="Character name."),
) -> None:
    """Look up a character by region, realm, and name."""
    client = _client(ctx)
    try:
        payload = client.character(region=region, realm=realm, name=name)
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)
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
    region: str = typer.Argument(..., help="Character region slug, for example us or eu."),
    realm: str = typer.Argument(..., help="Character realm slug or name."),
    name: str = typer.Argument(..., help="Character name."),
    zone_id: int | None = typer.Option(None, "--zone-id", help="Optional Warcraft Logs zone ID."),
    difficulty: int | None = typer.Option(None, "--difficulty", help="Optional difficulty ID."),
    metric: str | None = typer.Option(None, "--metric", help="Optional ranking metric such as dps, hps, or tankhps."),
    size: int | None = typer.Option(None, "--size", help="Optional raid size."),
    spec_name: str | None = typer.Option(None, "--spec-name", help="Optional spec slug filter."),
    top: int = typer.Option(5, "--top", min=1, max=20, help="Number of top ranking rows to keep in the summary."),
) -> None:
    """Show a character's encounter rankings for one zone."""
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
    code: str = typer.Argument(..., help="Warcraft Logs report code."),
    allow_unlisted: bool = typer.Option(False, "--allow-unlisted", help="Allow lookup of unlisted reports."),
) -> None:
    """Show one report's metadata, zone, and owning guild."""
    client = _client(ctx)
    try:
        payload = client.report(code=code, allow_unlisted=allow_unlisted)
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)
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
    """List reports for a guild, optionally narrowed by zone and time window."""
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
    region: str = typer.Argument(..., help="Guild region slug, for example us or eu."),
    realm: str = typer.Argument(..., help="Guild realm slug or name."),
    name: str = typer.Argument(..., help="Guild name."),
    limit: int = typer.Option(25, "--limit", min=1, max=100, help="Reports per page."),
    page: int = typer.Option(1, "--page", min=1, help="Page number."),
    start_time: float | None = typer.Option(None, "--start-time", help="Optional report-range start time in milliseconds."),
    end_time: float | None = typer.Option(None, "--end-time", help="Optional report-range end time in milliseconds."),
    zone_id: int | None = typer.Option(None, "--zone-id", help="Optional Warcraft Logs zone filter."),
    game_zone_id: int | None = typer.Option(None, "--game-zone-id", help="Optional game zone filter."),
) -> None:
    """List a guild's reports by region, realm, and name."""
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


def _sampled_kill_cohort(ctx: typer.Context, client: WarcraftLogsClient, scope: CrossReportScope) -> dict[str, Any]:
    """Collect the sampled boss-kill cohort and close the client, mapping transport errors to the CLI contract."""
    try:
        return _collect_boss_kill_rows(client, scope)
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)
    finally:
        client.close()


def _sampled_comp_cohort(ctx: typer.Context, client: WarcraftLogsClient, scope: CrossReportScope) -> dict[str, Any]:
    """Collect the sampled kill cohort with raid compositions attached, then close the client."""
    try:
        return _collect_comp_sample_rows(client, scope)
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)
    finally:
        client.close()


def _sampled_spec_usage_cohort(
    ctx: typer.Context,
    client: WarcraftLogsClient,
    scope: CrossReportScope,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Sampled kill cohort plus its per-kill player-detail rows, then close the client."""
    try:
        analytics = _collect_boss_kill_rows(client, scope)
        enriched_rows = [
            {
                **row,
                "player_details": _all_player_detail_rows(_fight_player_details(client, row, difficulty=scope.difficulty)),
            }
            for row in analytics["rows"]
        ]
        return analytics, enriched_rows
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)
    finally:
        client.close()


def _sampled_ability_usage_cohort(
    ctx: typer.Context,
    client: WarcraftLogsClient,
    scope: CrossReportScope,
    *,
    ability_id: int,
    event_limit: int,
) -> dict[str, Any]:
    """Collect the sampled kill cohort with per-kill ability casts attached, then close the client."""
    try:
        return _collect_ability_usage_rows(client, scope, ability_id=ability_id, event_limit=event_limit)
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)
    finally:
        client.close()


def _ability_usage_query(scope: CrossReportScope, *, ability_id: int) -> dict[str, Any]:
    """Cross-report scope echo for ability-usage-summary: ability id after the zone, no ``top``."""
    scoped = asdict(scope)
    del scoped["top"]
    return {"zone_id": scoped.pop("zone_id"), "ability_id": ability_id, **scoped}


def _require_boss_scope(ctx: typer.Context, *, boss_id: int | None, boss_name: str | None) -> None:
    if boss_id is None and not boss_name:
        _fail(ctx, "missing_boss", "Provide --boss-id or --boss-name for cross-report boss analytics.")


def _require_spec_scope(ctx: typer.Context, *, spec_name: str | None) -> None:
    if not spec_name:
        _fail(ctx, "missing_spec", "Provide --spec-name to build a spec-filtered participant kill cohort.")


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
    """Sample recent kills of one boss across reports and summarize them."""
    _require_boss_scope(ctx, boss_id=boss_id, boss_name=boss_name)
    scope = CrossReportScope(
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
    )
    client = _client(ctx)
    analytics = _sampled_kill_cohort(ctx, client, scope)
    _emit(
        ctx,
        _boss_kills_payload(
            cache_ttl_seconds=_emitted_finished_report_ttl(client),
            kind="boss_kills",
            rows=analytics["rows"],
            sample=analytics["sample"],
            query=asdict(scope),
            top=top,
            root_url=client.site.root_url,
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
    """Sample recent kills of one boss and return the fastest ones."""
    _require_boss_scope(ctx, boss_id=boss_id, boss_name=boss_name)
    scope = CrossReportScope(
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
    )
    client = _client(ctx)
    analytics = _sampled_kill_cohort(ctx, client, scope)
    _emit(
        ctx,
        _boss_kills_payload(
            cache_ttl_seconds=_emitted_finished_report_ttl(client),
            kind="top_kills",
            rows=analytics["rows"],
            sample=analytics["sample"],
            query=asdict(scope),
            top=top,
            root_url=client.site.root_url,
        ),
        client=client,
    )


@app.command("spec-kill-samples")
def spec_kill_samples(
    ctx: typer.Context,
    zone_id: int = typer.Option(..., "--zone-id", help="Warcraft Logs zone ID to sample reports from."),
    spec_name: str | None = typer.Option(
        None,
        "--spec-name",
        help="Required participant spec slug. Sampled kills are filtered to fights containing this spec.",
    ),
    boss_id: int | None = typer.Option(None, "--boss-id", help="Encounter ID to match."),
    boss_name: str | None = typer.Option(None, "--boss-name", help="Boss name to match within sampled fights."),
    difficulty: int | None = typer.Option(None, "--difficulty", help="Optional difficulty ID filter."),
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
    """Sample recent kills of one boss that include a given spec."""
    _require_boss_scope(ctx, boss_id=boss_id, boss_name=boss_name)
    _require_spec_scope(ctx, spec_name=spec_name)
    scope = CrossReportScope(
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
    )
    client = _client(ctx)
    analytics = _sampled_kill_cohort(ctx, client, scope)
    _emit(
        ctx,
        _spec_filtered_kill_samples_payload(
            cache_ttl_seconds=_emitted_finished_report_ttl(client),
            rows=analytics["rows"],
            sample=analytics["sample"],
            query=asdict(scope),
            top=top,
            root_url=client.site.root_url,
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
    """Count spec usage across a sample of recent kills of one boss."""
    _require_boss_scope(ctx, boss_id=boss_id, boss_name=boss_name)
    scope = CrossReportScope(
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
    )
    client = _client(ctx)
    analytics, enriched_rows = _sampled_spec_usage_cohort(ctx, client, scope)
    _emit(
        ctx,
        _boss_spec_usage_payload(
            cache_ttl_seconds=_emitted_finished_report_ttl(client),
            rows=enriched_rows,
            sample=analytics["sample"],
            query=asdict(scope),
            top=top,
            root_url=client.site.root_url,
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
    preview_limit: int = typer.Option(10, "--preview-limit", min=1, max=100,
                                      help="Maximum sampled kill rows to include in the preview payload."),
    event_limit: int = typer.Option(200, "--event-limit", min=1, max=5000, help="Maximum cast events to request per sampled kill."),
    report_pages: int = typer.Option(1, "--report-pages", min=1, max=10, help="How many report-list pages to sample."),
    reports_per_page: int = typer.Option(25, "--reports-per-page", min=1, max=100, help="Reports to fetch per sampled page."),
    start_time: float | None = typer.Option(None, "--start-time", help="Optional report-range start time in milliseconds."),
    end_time: float | None = typer.Option(None, "--end-time", help="Optional report-range end time in milliseconds."),
    guild_region: str | None = typer.Option(None, "--guild-region", help="Optional guild-region scope for report discovery."),
    guild_realm: str | None = typer.Option(None, "--guild-realm", help="Optional guild-realm scope for report discovery."),
    guild_name: str | None = typer.Option(None, "--guild-name", help="Optional guild-name scope for report discovery."),
) -> None:
    """Summarize how often one ability is cast across a sample of recent kills."""
    _require_boss_scope(ctx, boss_id=boss_id, boss_name=boss_name)
    scope = CrossReportScope(
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
    client = _client(ctx)
    analytics = _sampled_ability_usage_cohort(ctx, client, scope, ability_id=ability_id, event_limit=event_limit)
    _emit(
        ctx,
        _ability_usage_summary_payload(
            cache_ttl_seconds=_emitted_finished_report_ttl(client),
            rows=analytics["rows"],
            sample=analytics["sample"],
            query=_ability_usage_query(scope, ability_id=ability_id),
            ability=analytics["ability"],
            preview_limit=preview_limit,
            event_limit=event_limit,
            root_url=client.site.root_url,
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
    """Sample raid compositions from recent kills of one boss."""
    _require_boss_scope(ctx, boss_id=boss_id, boss_name=boss_name)
    scope = CrossReportScope(
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
    )
    client = _client(ctx)
    analytics = _sampled_comp_cohort(ctx, client, scope)
    _emit(
        ctx,
        _comp_samples_payload(
            cache_ttl_seconds=_emitted_finished_report_ttl(client),
            rows=analytics["rows"],
            sample=analytics["sample"],
            query=asdict(scope),
            top=top,
            root_url=client.site.root_url,
        ),
        client=client,
    )


@app.command("report-encounter")
def report_encounter(
    ctx: typer.Context,
    reference: str = typer.Argument(..., help="Warcraft Logs report URL or report code, optionally with a #fight=N fragment."),
    fight_id: int | None = typer.Option(
        None, "--fight-id", help="Override or supply a fight ID when the report reference does not include one."),
    allow_unlisted: bool = typer.Option(False, "--allow-unlisted", help="Allow lookup of unlisted reports."),
) -> None:
    """Show one report fight with its report, fight, and encounter identity."""
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
    finally:
        client.close()
    _emit(
        ctx,
        {
            "ok": True,
            "provider": "warcraftlogs",
            "kind": "report_encounter",
            **_encounter_summary_payload(
                ref=ref,
                report=report,
                fight=fight,
                encounter=encounter,
                finished_report_ttl=_emitted_finished_report_ttl(client),
                report_ttl=_emitted_report_ttl(client),
            ),
        },
        client=client,
    )


@app.command("report-encounter-players")
def report_encounter_players(
    ctx: typer.Context,
    reference: str = typer.Argument(..., help="Warcraft Logs report URL or report code, optionally with a #fight=N fragment."),
    fight_id: int | None = typer.Option(
        None, "--fight-id", help="Override or supply a fight ID when the report reference does not include one."),
    include_combatant_info: bool | None = typer.Option(
        None,
        "--include-combatant-info/--no-include-combatant-info",
        help="Optional combatant detail toggle.",
    ),
    translate: bool | None = typer.Option(None, "--translate/--no-translate", help="Optional translation toggle."),
    allow_unlisted: bool = typer.Option(False, "--allow-unlisted", help="Allow lookup of unlisted reports."),
) -> None:
    """List the players in one report fight, by role and spec."""
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
    finally:
        client.close()
    _emit(
        ctx,
        {
            "ok": True,
            "provider": "warcraftlogs",
            "kind": "report_encounter_players",
            **_encounter_summary_payload(
                ref=ref,
                report=report,
                fight=fight,
                encounter=encounter,
                finished_report_ttl=_emitted_finished_report_ttl(client),
                report_ttl=_emitted_report_ttl(client),
            ),
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
    reference: str = typer.Argument(..., help="Warcraft Logs report URL or report code, optionally with a #fight=N fragment."),
    actor_id: int = typer.Option(..., "--actor-id", help="Report-local actor ID scoped to the selected fight."),
    fight_id: int | None = typer.Option(
        None, "--fight-id", help="Override or supply a fight ID when the report reference does not include one."),
    allow_unlisted: bool = typer.Option(False, "--allow-unlisted", help="Allow lookup of unlisted reports."),
    out: str | None = typer.Option(None, "--out", help="Optional path to write the scoped talent transport packet JSON."),
) -> None:
    """Emit one player's talent tree from a report fight as a talent transport packet."""
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

    _emit(
        ctx,
        {
            "ok": True,
            "provider": "warcraftlogs",
            "kind": "report_player_talents",
            **_encounter_summary_payload(
                ref=ref,
                report=report,
                fight=fight,
                encounter=encounter,
                finished_report_ttl=_emitted_finished_report_ttl(client),
                report_ttl=_emitted_report_ttl(client),
            ),
            "player": actor,
            "talent_transport_packet": transport_packet,
            "written_packet_path": written_packet_path,
        },
        client=client,
    )


@app.command("report-encounter-casts")
def report_encounter_casts(
    ctx: typer.Context,
    reference: str = typer.Argument(..., help="Warcraft Logs report URL or report code, optionally with a #fight=N fragment."),
    fight_id: int | None = typer.Option(
        None, "--fight-id", help="Override or supply a fight ID when the report reference does not include one."),
    source_id: int | None = typer.Option(None, "--source-id", help="Optional source actor filter."),
    target_id: int | None = typer.Option(None, "--target-id", help="Optional target actor filter."),
    ability_id: float | None = typer.Option(None, "--ability-id", help="Optional ability game ID filter."),
    hostility_type: str | None = typer.Option(None, "--hostility-type", help="Optional hostility filter."),
    limit: int = typer.Option(200, "--limit", min=1, max=10000, help="Maximum cast events to request from Warcraft Logs."),
    preview_limit: int = typer.Option(20, "--preview-limit", min=1, max=200, help="Maximum preview cast rows to return."),
    window_start_ms: float | None = typer.Option(
        None, "--window-start-ms", help="Optional encounter-relative start offset in milliseconds."),
    window_end_ms: float | None = typer.Option(None, "--window-end-ms", help="Optional encounter-relative end offset in milliseconds."),
    translate: bool | None = typer.Option(None, "--translate/--no-translate", help="Optional translation toggle."),
    allow_unlisted: bool = typer.Option(False, "--allow-unlisted", help="Allow lookup of unlisted reports."),
) -> None:
    """Summarize casts in one report fight, grouped by ability, source, or target."""
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
            fight,
            _EncounterFilters(
                ability_id=ability_id,
                data_type="Casts",
                source_id=source_id,
                target_id=target_id,
                hostility_type=normalized_hostility_type,
                translate=translate,
                limit=limit,
                window_start_ms=window_start_ms,
                window_end_ms=window_end_ms,
            ),
        )
        events_report = client.report_events(
            code=ref.code,
            allow_unlisted=allow_unlisted,
            options=options,
        )
        master_report = client.report_master_data(code=ref.code, allow_unlisted=allow_unlisted, actor_type="Player")
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)
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
            **_encounter_summary_payload(
                ref=ref,
                report=report,
                fight=fight,
                encounter=encounter,
                finished_report_ttl=_emitted_finished_report_ttl(client),
                report_ttl=_emitted_report_ttl(client),
            ),
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
    reference: str = typer.Argument(..., help="Warcraft Logs report URL or report code, optionally with a #fight=N fragment."),
    fight_id: int | None = typer.Option(
        None, "--fight-id", help="Override or supply a fight ID when the report reference does not include one."),
    source_id: int | None = typer.Option(None, "--source-id", help="Optional source actor filter."),
    target_id: int | None = typer.Option(None, "--target-id", help="Optional target actor filter."),
    ability_id: float | None = typer.Option(None, "--ability-id", help="Optional ability game ID filter."),
    hostility_type: str | None = typer.Option(None, "--hostility-type", help="Optional hostility filter."),
    view_by: str | None = typer.Option("source", "--view-by", help="Optional table view grouping."),
    wipe_cutoff: int | None = typer.Option(None, "--wipe-cutoff", help="Optional wipe cutoff."),
    preview_limit: int = typer.Option(20, "--preview-limit", min=1, max=200, help="Maximum preview buff rows to return."),
    window_start_ms: float | None = typer.Option(
        None, "--window-start-ms", help="Optional encounter-relative start offset in milliseconds."),
    window_end_ms: float | None = typer.Option(None, "--window-end-ms", help="Optional encounter-relative end offset in milliseconds."),
    translate: bool | None = typer.Option(None, "--translate/--no-translate", help="Optional translation toggle."),
    allow_unlisted: bool = typer.Option(False, "--allow-unlisted", help="Allow lookup of unlisted reports."),
) -> None:
    """Summarize buffs applied during one report fight."""
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
            fight,
            _EncounterFilters(
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
            ),
        )
        table_report = client.report_table(code=ref.code, allow_unlisted=allow_unlisted, options=options)
        # Unfiltered actor master data: buff targets (and `--view-by target`) can be pets or NPCs,
        # which an actor_type="Player" fetch would drop, degrading their identities to `actor:<id>`.
        master_report = client.report_master_data(code=ref.code, allow_unlisted=allow_unlisted)
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)
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
            **_encounter_summary_payload(
                ref=ref,
                report=report,
                fight=fight,
                encounter=encounter,
                finished_report_ttl=_emitted_finished_report_ttl(client),
                report_ttl=_emitted_report_ttl(client),
            ),
            **_encounter_buff_rows_payload(
                report=report,
                fight=fight,
                table_report=table_report,
                master_report=master_report,
                preview_limit=preview_limit,
                view_by=normalized_view_by,
                ability_id=int(ability_id) if ability_id is not None else None,
            ),
        },
        client=client,
    )


@app.command("report-encounter-aura-summary")
def report_encounter_aura_summary(
    ctx: typer.Context,
    reference: str = typer.Argument(..., help="Warcraft Logs report URL or report code, optionally with a #fight=N fragment."),
    ability_id: int = typer.Option(..., "--ability-id", help="Required aura ability game ID."),
    fight_id: int | None = typer.Option(
        None, "--fight-id", help="Override or supply a fight ID when the report reference does not include one."),
    source_id: int | None = typer.Option(None, "--source-id", help="Optional source actor filter."),
    target_id: int | None = typer.Option(None, "--target-id", help="Optional target actor filter."),
    hostility_type: str | None = typer.Option(None, "--hostility-type", help="Optional hostility filter."),
    wipe_cutoff: int | None = typer.Option(None, "--wipe-cutoff", help="Optional wipe cutoff."),
    window_start_ms: float | None = typer.Option(
        None, "--window-start-ms", help="Optional encounter-relative start offset in milliseconds."),
    window_end_ms: float | None = typer.Option(None, "--window-end-ms", help="Optional encounter-relative end offset in milliseconds."),
    translate: bool | None = typer.Option(None, "--translate/--no-translate", help="Optional translation toggle."),
    allow_unlisted: bool = typer.Option(False, "--allow-unlisted", help="Allow lookup of unlisted reports."),
) -> None:
    """Summarize aura uptime in one report fight, optionally over an explicit window."""
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
            fight,
            _EncounterFilters(
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
            ),
        )
        table_payload = client.report_table(code=ref.code, allow_unlisted=allow_unlisted, options=options)
        master_report = client.report_master_data(code=ref.code, allow_unlisted=allow_unlisted, actor_type="Player")
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)
    finally:
        client.close()
    _emit(
        ctx,
        {
            "ok": True,
            "provider": "warcraftlogs",
            "kind": "report_encounter_aura_summary",
            "query": query,
            **_encounter_summary_payload(
                ref=ref,
                report=report,
                fight=fight,
                encounter=encounter,
                finished_report_ttl=_emitted_finished_report_ttl(client),
                report_ttl=_emitted_report_ttl(client),
            ),
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


@dataclass(frozen=True, slots=True)
class _AuraWindow:
    """One labelled side of an aura comparison, as encounter-relative millisecond offsets."""

    label: str
    start_ms: float | None
    end_ms: float | None


@dataclass(frozen=True, slots=True)
class _AuraWindowResult:
    """A fetched aura window: its label, the echoed slice query, and the summary payload it produced."""

    label: str
    query: dict[str, Any]
    payload: dict[str, Any]


def _aura_compare_windows(
    ctx: typer.Context,
    client: WarcraftLogsClient,
    *,
    reference: str,
    fight_id: int | None,
    allow_unlisted: bool,
    ability_id: int,
    filters: _EncounterFilters,
    left: _AuraWindow,
    right: _AuraWindow,
) -> tuple[_EncounterScope, _AuraWindowResult, _AuraWindowResult]:
    """Resolve the fight, fetch one aura table per window, and summarize both against shared master data."""
    try:
        scope = _resolve_encounter_scope(
            ctx,
            client=client,
            reference=reference,
            fight_id=fight_id,
            allow_unlisted=allow_unlisted,
        )
        fight = scope[2]
        left_options, left_query = _encounter_filter_options(
            ctx, fight, replace(filters, window_start_ms=left.start_ms, window_end_ms=left.end_ms)
        )
        right_options, right_query = _encounter_filter_options(
            ctx, fight, replace(filters, window_start_ms=right.start_ms, window_end_ms=right.end_ms)
        )
        code = scope[0].code
        left_table = client.report_table(code=code, allow_unlisted=allow_unlisted, options=left_options)
        right_table = client.report_table(code=code, allow_unlisted=allow_unlisted, options=right_options)
        master_report = client.report_master_data(code=code, allow_unlisted=allow_unlisted, actor_type="Player")
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)
    finally:
        client.close()

    def summarize(window: _AuraWindow, query: dict[str, Any], table: dict[str, Any]) -> _AuraWindowResult:
        return _AuraWindowResult(
            label=window.label,
            query=query,
            payload=_report_encounter_aura_summary_payload(
                report=scope[1],
                fight=fight,
                table_report=table,
                master_report=master_report,
                ability_id=ability_id,
            ),
        )

    return scope, summarize(left, left_query, left_table), summarize(right, right_query, right_table)


def _aura_compare_payload(
    *,
    scope: _EncounterScope,
    filters: _EncounterFilters,
    left: _AuraWindowResult,
    right: _AuraWindowResult,
    finished_report_ttl: int | None,
    report_ttl: int | None,
) -> dict[str, Any]:
    ref, report, fight, encounter = scope
    return {
        "ok": True,
        "provider": "warcraftlogs",
        "kind": "report_encounter_aura_compare",
        "query": {
            "ability_id": filters.ability_id,
            "source_id": filters.source_id,
            "target_id": filters.target_id,
            "hostility_type": filters.hostility_type,
            "translate": filters.translate,
            "wipe_cutoff": filters.wipe_cutoff,
        },
        **_encounter_summary_payload(
            ref=ref,
            report=report,
            fight=fight,
            encounter=encounter,
            finished_report_ttl=finished_report_ttl,
            report_ttl=report_ttl,
        ),
        "aura": left.payload.get("aura"),
        "windows": [
            {"label": window.label, "query": window.query, "aura_summary": window.payload.get("aura_summary")}
            for window in (left, right)
        ],
        "comparison": {
            "matching_rule": "same_report_same_fight_same_ability_explicit_windows",
            "rows": _aura_compare_rows(
                left_rows=_aura_summary_rows(left.payload),
                right_rows=_aura_summary_rows(right.payload),
            ),
        },
    }


@app.command("report-encounter-aura-compare")
def report_encounter_aura_compare(
    ctx: typer.Context,
    reference: str = typer.Argument(..., help="Warcraft Logs report URL or report code, optionally with a #fight=N fragment."),
    ability_id: int = typer.Option(..., "--ability-id", help="Required aura ability game ID."),
    fight_id: int | None = typer.Option(
        None, "--fight-id", help="Override or supply a fight ID when the report reference does not include one."),
    left_window_start_ms: float | None = typer.Option(
        None, "--left-window-start-ms", help="Encounter-relative start offset for the left comparison window."),
    left_window_end_ms: float | None = typer.Option(
        None, "--left-window-end-ms", help="Encounter-relative end offset for the left comparison window."),
    right_window_start_ms: float | None = typer.Option(
        None, "--right-window-start-ms", help="Encounter-relative start offset for the right comparison window."),
    right_window_end_ms: float | None = typer.Option(
        None, "--right-window-end-ms", help="Encounter-relative end offset for the right comparison window."),
    left_label: str = typer.Option("left", "--left-label", help="Label for the left comparison window."),
    right_label: str = typer.Option("right", "--right-label", help="Label for the right comparison window."),
    source_id: int | None = typer.Option(None, "--source-id", help="Optional source actor filter applied to both windows."),
    target_id: int | None = typer.Option(None, "--target-id", help="Optional target actor filter applied to both windows."),
    hostility_type: str | None = typer.Option(None, "--hostility-type", help="Optional hostility filter applied to both windows."),
    wipe_cutoff: int | None = typer.Option(None, "--wipe-cutoff", help="Optional wipe cutoff applied to both windows."),
    translate: bool | None = typer.Option(None, "--translate/--no-translate", help="Optional translation toggle."),
    allow_unlisted: bool = typer.Option(False, "--allow-unlisted", help="Allow lookup of unlisted reports."),
) -> None:
    """Compare aura uptime between two explicit windows of the same report fight."""
    _require_explicit_window(ctx, name="--left-window", start_ms=left_window_start_ms, end_ms=left_window_end_ms)
    _require_explicit_window(ctx, name="--right-window", start_ms=right_window_start_ms, end_ms=right_window_end_ms)
    filters = _EncounterFilters(
        ability_id=float(ability_id),
        data_type="Buffs",
        source_id=source_id,
        target_id=target_id,
        hostility_type=_normalize_graphql_enum(hostility_type),
        translate=translate,
        view_by="Source",
        wipe_cutoff=wipe_cutoff,
    )
    client = _client(ctx)
    scope, left, right = _aura_compare_windows(
        ctx,
        client,
        reference=reference,
        fight_id=fight_id,
        allow_unlisted=allow_unlisted,
        ability_id=ability_id,
        filters=filters,
        left=_AuraWindow(left_label, left_window_start_ms, left_window_end_ms),
        right=_AuraWindow(right_label, right_window_start_ms, right_window_end_ms),
    )
    _emit(
        ctx,
        _aura_compare_payload(
            scope=scope,
            filters=filters,
            left=left,
            right=right,
            finished_report_ttl=_emitted_finished_report_ttl(client),
            report_ttl=_emitted_report_ttl(client),
        ),
        client=client,
    )


@app.command("report-encounter-damage-source-summary")
def report_encounter_damage_source_summary(
    ctx: typer.Context,
    reference: str = typer.Argument(..., help="Warcraft Logs report URL or report code, optionally with a #fight=N fragment."),
    fight_id: int | None = typer.Option(
        None, "--fight-id", help="Override or supply a fight ID when the report reference does not include one."),
    source_id: int | None = typer.Option(None, "--source-id", help="Optional source actor filter."),
    target_id: int | None = typer.Option(None, "--target-id", help="Optional target actor filter."),
    ability_id: float | None = typer.Option(None, "--ability-id", help="Optional ability game ID filter."),
    hostility_type: str | None = typer.Option(None, "--hostility-type", help="Optional hostility filter."),
    wipe_cutoff: int | None = typer.Option(None, "--wipe-cutoff", help="Optional wipe cutoff."),
    window_start_ms: float | None = typer.Option(
        None, "--window-start-ms", help="Optional encounter-relative start offset in milliseconds."),
    window_end_ms: float | None = typer.Option(None, "--window-end-ms", help="Optional encounter-relative end offset in milliseconds."),
    translate: bool | None = typer.Option(None, "--translate/--no-translate", help="Optional translation toggle."),
    allow_unlisted: bool = typer.Option(False, "--allow-unlisted", help="Allow lookup of unlisted reports."),
) -> None:
    """Summarize damage in one report fight by source actor."""
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
            fight,
            _EncounterFilters(
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
            ),
        )
        table_payload = client.report_table(code=ref.code, allow_unlisted=allow_unlisted, options=options)
        master_report = client.report_master_data(code=ref.code, allow_unlisted=allow_unlisted, actor_type="Player")
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)
    finally:
        client.close()
    _emit(
        ctx,
        {
            "ok": True,
            "provider": "warcraftlogs",
            "kind": "report_encounter_damage_source_summary",
            "query": query,
            **_encounter_summary_payload(
                ref=ref,
                report=report,
                fight=fight,
                encounter=encounter,
                finished_report_ttl=_emitted_finished_report_ttl(client),
                report_ttl=_emitted_report_ttl(client),
            ),
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
    reference: str = typer.Argument(..., help="Warcraft Logs report URL or report code, optionally with a #fight=N fragment."),
    fight_id: int | None = typer.Option(
        None, "--fight-id", help="Override or supply a fight ID when the report reference does not include one."),
    source_id: int | None = typer.Option(None, "--source-id", help="Optional source actor filter."),
    target_id: int | None = typer.Option(None, "--target-id", help="Optional target actor filter."),
    ability_id: float | None = typer.Option(None, "--ability-id", help="Optional ability game ID filter."),
    hostility_type: str | None = typer.Option(None, "--hostility-type", help="Optional hostility filter."),
    wipe_cutoff: int | None = typer.Option(None, "--wipe-cutoff", help="Optional wipe cutoff."),
    window_start_ms: float | None = typer.Option(
        None, "--window-start-ms", help="Optional encounter-relative start offset in milliseconds."),
    window_end_ms: float | None = typer.Option(None, "--window-end-ms", help="Optional encounter-relative end offset in milliseconds."),
    translate: bool | None = typer.Option(None, "--translate/--no-translate", help="Optional translation toggle."),
    allow_unlisted: bool = typer.Option(False, "--allow-unlisted", help="Allow lookup of unlisted reports."),
) -> None:
    """Summarize damage in one report fight by target actor."""
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
            fight,
            _EncounterFilters(
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
            ),
        )
        table_payload = client.report_table(code=ref.code, allow_unlisted=allow_unlisted, options=options)
        master_report = client.report_master_data(code=ref.code, allow_unlisted=allow_unlisted)
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)
    finally:
        client.close()
    _emit(
        ctx,
        {
            "ok": True,
            "provider": "warcraftlogs",
            "kind": "report_encounter_damage_target_summary",
            "query": query,
            **_encounter_summary_payload(
                ref=ref,
                report=report,
                fight=fight,
                encounter=encounter,
                finished_report_ttl=_emitted_finished_report_ttl(client),
                report_ttl=_emitted_report_ttl(client),
            ),
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
    reference: str = typer.Argument(..., help="Warcraft Logs report URL or report code, optionally with a #fight=N fragment."),
    fight_id: int | None = typer.Option(
        None, "--fight-id", help="Override or supply a fight ID when the report reference does not include one."),
    source_id: int | None = typer.Option(None, "--source-id", help="Optional source actor filter."),
    target_id: int | None = typer.Option(None, "--target-id", help="Optional target actor filter."),
    ability_id: float | None = typer.Option(None, "--ability-id", help="Optional ability game ID filter."),
    hostility_type: str | None = typer.Option(None, "--hostility-type", help="Optional hostility filter."),
    view_by: str | None = typer.Option("source", "--view-by", help="Optional table view grouping."),
    wipe_cutoff: int | None = typer.Option(None, "--wipe-cutoff", help="Optional wipe cutoff."),
    window_start_ms: float | None = typer.Option(
        None, "--window-start-ms", help="Optional encounter-relative start offset in milliseconds."),
    window_end_ms: float | None = typer.Option(None, "--window-end-ms", help="Optional encounter-relative end offset in milliseconds."),
    translate: bool | None = typer.Option(None, "--translate/--no-translate", help="Optional translation toggle."),
    allow_unlisted: bool = typer.Option(False, "--allow-unlisted", help="Allow lookup of unlisted reports."),
) -> None:
    """Return the raw damage table for one report fight."""
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
            fight,
            _EncounterFilters(
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
            ),
        )
        payload = client.report_table(code=ref.code, allow_unlisted=allow_unlisted, options=options)
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)
    finally:
        client.close()
    _emit(
        ctx,
        {
            "ok": True,
            "provider": "warcraftlogs",
            "kind": "report_encounter_damage_breakdown",
            "query": query,
            **_encounter_summary_payload(
                ref=ref,
                report=report,
                fight=fight,
                encounter=encounter,
                finished_report_ttl=_emitted_finished_report_ttl(client),
                report_ttl=_emitted_report_ttl(client),
            ),
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
    """Bucket kill durations across a sample of recent kills of one boss."""
    _require_boss_scope(ctx, boss_id=boss_id, boss_name=boss_name)
    scope = CrossReportScope(
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
    client = _client(ctx)
    analytics = _sampled_kill_cohort(ctx, client, scope)
    _emit(
        ctx,
        _kill_time_distribution_payload(
            cache_ttl_seconds=_emitted_finished_report_ttl(client),
            rows=analytics["rows"],
            sample=analytics["sample"],
            query={**asdict(scope), "top": len(analytics["rows"])},
            bucket_seconds=bucket_seconds,
            root_url=client.site.root_url,
        ),
        client=client,
    )


@app.command("report-fights")
def report_fights(
    ctx: typer.Context,
    code: str = typer.Argument(..., help="Warcraft Logs report code."),
    difficulty: int | None = typer.Option(None, "--difficulty", help="Optional difficulty ID filter."),
    allow_unlisted: bool = typer.Option(False, "--allow-unlisted", help="Allow lookup of unlisted reports."),
) -> None:
    """List the fights in one report."""
    client = _client(ctx)
    try:
        payload = client.report_fights(code=code, difficulty=difficulty, allow_unlisted=allow_unlisted)
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)
    finally:
        client.close()
    fights = list_at(payload, "fights")
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


@dataclass(frozen=True, slots=True)
class _GraphqlRequest:
    """A resolved raw-GraphQL invocation: operation text, merged variables, endpoint, and cache budget."""

    operation_name: str | None
    query: str
    variables: dict[str, Any]
    endpoint: str
    cache_ttl_seconds: int
    introspect: bool


def _run_graphql(ctx: typer.Context, request: _GraphqlRequest) -> None:
    client = _client(ctx)
    try:
        payload, effective_endpoint = client.raw_graphql(
            operation_name=request.operation_name,
            query=request.query,
            variables=request.variables,
            endpoint=request.endpoint,
            cache_ttl_seconds=request.cache_ttl_seconds,
        )
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)
    finally:
        client.close()
    data = dict(payload) if isinstance(payload, dict) else payload
    if isinstance(data, dict):
        data.pop(GRAPHQL_WARNINGS_KEY, None)
    emitted: dict[str, Any] = {
        "ok": True,
        "provider": "warcraftlogs",
        "query": {
            "operation_name": request.operation_name,
            "variables": request.variables,
            "endpoint": effective_endpoint,
            "requested_endpoint": request.endpoint,
            "cache_ttl_seconds": request.cache_ttl_seconds,
        },
    }
    if request.introspect:
        emitted["introspection"] = data.get("__schema") if isinstance(data, dict) else None
    else:
        emitted["data"] = data
    _emit(ctx, emitted, client=client)


@app.command("graphql")
def graphql(
    ctx: typer.Context,
    query_text: str | None = typer.Option(None, "--query", help="GraphQL operation text, @path, or - for stdin."),
    raw_var: list[str] = RAW_GRAPHQL_VAR_OPTION,
    variables_json: str | None = typer.Option(None, "--variables-json", help="JSON object of GraphQL variables."),
    operation_name: str | None = typer.Option(None, "--operation-name", help="Optional GraphQL operation name."),
    endpoint: str = typer.Option("auto", "--endpoint", help="Endpoint family: auto, client, or user."),
    cache_ttl: int = typer.Option(0, "--cache-ttl", min=0, help="Opt-in cache TTL in seconds. Defaults to 0/off."),
    introspect: bool = typer.Option(False, "--introspect", help="Run a GraphQL introspection query."),
    allow_unlisted: bool = typer.Option(
        False,
        "--allow-unlisted",
        help="Inject allowUnlisted=true when the query declares $allowUnlisted.",
    ),
    report_code: str | None = typer.Option(None, "--report-code", help="Inject report code into declared $code variables."),
    fight_id: list[int] | None = FIGHT_ID_OPTION,
    encounter_id: int | None = typer.Option(None, "--encounter-id", help="Inject declared $encounterID variables."),
    start_time: float | None = typer.Option(None, "--start-time", help="Inject declared $startTime variables."),
    end_time: float | None = typer.Option(None, "--end-time", help="Inject declared $endTime variables."),
    difficulty: int | None = typer.Option(None, "--difficulty", help="Inject declared $difficulty variables."),
    zone_id: int | None = typer.Option(None, "--zone-id", help="Inject declared $zoneID variables."),
    source_id: int | None = typer.Option(None, "--source-id", help="Inject declared $sourceID variables."),
    target_id: int | None = typer.Option(None, "--target-id", help="Inject declared $targetID variables."),
    ability_id: int | None = typer.Option(None, "--ability-id", help="Inject declared $abilityID variables."),
) -> None:
    """Run a raw Warcraft Logs GraphQL query, or introspect the schema with --introspect."""
    query = _load_graphql_query(ctx, query_text, introspect=introspect)
    effective_operation_name = "IntrospectionQuery" if introspect else operation_name
    variables = _parse_graphql_variables_json(ctx, variables_json)
    variables.update(_parse_graphql_var_options(ctx, raw_var))
    _run_graphql(
        ctx,
        _GraphqlRequest(
            operation_name=effective_operation_name,
            query=query,
            variables=_inject_graphql_scope_helpers(
                variables,
                declared_variables=_declared_graphql_variables(query, operation_name=effective_operation_name),
                scope=_GraphqlScope(
                    report_code=report_code,
                    fight_ids=fight_id,
                    encounter_id=encounter_id,
                    start_time=start_time,
                    end_time=end_time,
                    difficulty=difficulty,
                    zone_id=zone_id,
                    source_id=source_id,
                    target_id=target_id,
                    ability_id=ability_id,
                    allow_unlisted=allow_unlisted,
                ),
            ),
            endpoint=endpoint,
            cache_ttl_seconds=cache_ttl,
            introspect=introspect,
        ),
    )


def _emit_report_events_slice(
    ctx: typer.Context,
    *,
    code: str,
    allow_unlisted: bool,
    options: ReportFilterOptions,
) -> None:
    """Fetch one raw event slice and emit it with the filter options echoed back as the query."""
    client = _client(ctx)
    try:
        payload = client.report_events(code=code, allow_unlisted=allow_unlisted, options=options)
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)
    finally:
        client.close()
    result_payload = _report_events_payload(payload)
    emitted: dict[str, Any] = {
        "ok": True,
        "provider": "warcraftlogs",
        "query": asdict(options),
        **result_payload,
    }
    if result_payload.get("events") is None and options.data_type is None:
        emitted["notes"] = [
            "events.data is null. Warcraft Logs requires --data-type "
            "(e.g. casts, damage-done, healing) for non-null event slices."
        ]
    _emit(ctx, emitted, client=client)


def _emit_report_json_slice(
    ctx: typer.Context,
    *,
    code: str,
    allow_unlisted: bool,
    options: ReportFilterOptions,
    field: Literal["table", "graph"],
) -> None:
    """Fetch one raw report ``table`` or ``graph`` slice and emit it under the matching payload key."""
    client = _client(ctx)
    try:
        payload = (
            client.report_table(code=code, allow_unlisted=allow_unlisted, options=options)
            if field == "table"
            else client.report_graph(code=code, allow_unlisted=allow_unlisted, options=options)
        )
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)
    finally:
        client.close()
    _emit(
        ctx,
        {
            "ok": True,
            "provider": "warcraftlogs",
            "query": asdict(options),
            **_report_json_payload(payload, field=field),
        },
        client=client,
    )


@app.command("report-events")
def report_events(
    ctx: typer.Context,
    code: str = typer.Argument(..., help="Warcraft Logs report code."),
    ability_id: float | None = typer.Option(None, "--ability-id", help="Optional ability game ID filter."),
    data_type: str | None = typer.Option(
        None,
        "--data-type",
        help=(
            "Event data type (e.g. casts, damage-done, healing). Strongly recommended; "
            "without it Warcraft Logs returns events.data: null even on valid scoped slices."
        ),
    ),
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
    """Return raw report events for one narrowed slice of a report."""
    if not any(value is not None for value in (fight_id, encounter_id, start_time, end_time)):
        _fail(
            ctx,
            "missing_scope",
            "report-events requires a narrowed slice. Provide --fight-id, --encounter-id, --start-time, or --end-time.",
        )
    _emit_report_events_slice(
        ctx,
        code=code,
        allow_unlisted=allow_unlisted,
        options=ReportFilterOptions(
            ability_id=ability_id,
            data_type=_normalize_graphql_enum(data_type),
            difficulty=difficulty,
            encounter_id=encounter_id,
            end_time=end_time,
            fight_ids=fight_id,
            filter_expression=filter_expression,
            hostility_type=_normalize_graphql_enum(hostility_type),
            kill_type=_normalize_graphql_enum(kill_type),
            limit=limit,
            source_id=source_id,
            start_time=start_time,
            target_id=target_id,
            translate=translate,
        ),
    )


@app.command("report-table")
def report_table(
    ctx: typer.Context,
    code: str = typer.Argument(..., help="Warcraft Logs report code."),
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
    """Return a raw report table for one narrowed slice of a report."""
    _emit_report_json_slice(
        ctx,
        code=code,
        allow_unlisted=allow_unlisted,
        options=ReportFilterOptions(
            ability_id=ability_id,
            data_type=_normalize_graphql_enum(data_type),
            difficulty=difficulty,
            encounter_id=encounter_id,
            end_time=end_time,
            fight_ids=fight_id,
            filter_expression=filter_expression,
            hostility_type=_normalize_graphql_enum(hostility_type),
            kill_type=_normalize_graphql_enum(kill_type),
            source_id=source_id,
            start_time=start_time,
            target_id=target_id,
            translate=translate,
            view_by=_normalize_graphql_enum(view_by),
            wipe_cutoff=wipe_cutoff,
        ),
        field="table",
    )


@app.command("report-graph")
def report_graph(
    ctx: typer.Context,
    code: str = typer.Argument(..., help="Warcraft Logs report code."),
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
    """Return a raw report graph series for one narrowed slice of a report."""
    _emit_report_json_slice(
        ctx,
        code=code,
        allow_unlisted=allow_unlisted,
        options=ReportFilterOptions(
            ability_id=ability_id,
            data_type=_normalize_graphql_enum(data_type),
            difficulty=difficulty,
            encounter_id=encounter_id,
            end_time=end_time,
            fight_ids=fight_id,
            filter_expression=filter_expression,
            hostility_type=_normalize_graphql_enum(hostility_type),
            kill_type=_normalize_graphql_enum(kill_type),
            source_id=source_id,
            start_time=start_time,
            target_id=target_id,
            translate=translate,
            view_by=_normalize_graphql_enum(view_by),
            wipe_cutoff=wipe_cutoff,
        ),
        field="graph",
    )


@app.command("report-master-data")
def report_master_data(
    ctx: typer.Context,
    code: str = typer.Argument(..., help="Warcraft Logs report code."),
    actor_type: str | None = typer.Option(None, "--actor-type", help="Optional actor type filter."),
    actor_sub_type: str | None = typer.Option(None, "--actor-sub-type", help="Optional actor sub-type filter."),
    translate: bool | None = typer.Option(None, "--translate/--no-translate", help="Optional translation toggle."),
    allow_unlisted: bool = typer.Option(False, "--allow-unlisted", help="Allow lookup of unlisted reports."),
) -> None:
    """Return a report's master data: actors and abilities."""
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
    code: str = typer.Argument(..., help="Warcraft Logs report code."),
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
    """Return a report's player details, by role and spec."""
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
    code: str = typer.Argument(..., help="Warcraft Logs report code."),
    compare: str | None = typer.Option(None, "--compare", help="Optional compare mode such as rankings or parses."),
    difficulty: int | None = typer.Option(None, "--difficulty", help="Optional difficulty ID filter."),
    encounter_id: int | None = typer.Option(None, "--encounter-id", help="Optional encounter ID filter."),
    fight_id: list[int] | None = FIGHT_ID_OPTION,
    player_metric: str | None = typer.Option(None, "--player-metric", help="Optional player metric such as dps or hps."),
    timeframe: str | None = typer.Option(None, "--timeframe", help="Optional timeframe such as today or historical."),
    allow_unlisted: bool = typer.Option(False, "--allow-unlisted", help="Allow lookup of unlisted reports."),
) -> None:
    """Return the rankings attached to one report's fights."""
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
    """Console-script entry point: never let an exception escape as a traceback."""
    guarded_run(app, provider="warcraftlogs")

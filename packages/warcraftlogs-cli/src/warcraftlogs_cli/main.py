from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import secrets
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, NoReturn

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
    command_path,
    configure,
    emit,
    fail,
    guarded_run,
)
from warcraft_core.cli import (
    RuntimeConfig as BaseRuntimeConfig,
)
from warcraft_core.envelope import success_envelope
from warcraft_core.exit_codes import EXIT_AUTH, EXIT_USAGE, exit_code_for
from warcraft_core.identity import (
    IdentityConfidence,
    ability_identity_payload,
    class_spec_identity_payload,
    encounter_identity_payload,
    report_actor_identity_payload,
    talent_transport_packet_payload,
    validate_talent_transport_packet,
)
from warcraft_core.output import DEFAULT_COMPACT_MAX_CHARS
from warcraft_core.paths import provider_state_path
from warcraft_core.talent_transport import validate_talent_tree_transport
from warcraft_core.wow_normalization import profile_region
from warcraft_core.wow_specs import WOW_CLASS_NAMES, lookup_class, warcraftlogs_class_slug

from warcraftlogs_cli.boss_kills import (
    CrossReportScope,
    player_details_roles,
    retail_specs_named,
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
    sampled_cohort_notes as _sampled_cohort_notes,
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
    validated_region,
    warcraftlogs_provider_env_path,
)
from warcraftlogs_cli.provider import doctor as provider_doctor
from warcraftlogs_cli.provider import payload_body
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
from warcraftlogs_cli.services import (
    ReportReference,
    _active_auth_mode_from_state,
    _endpoint_family_from_state,
    _parse_report_reference,
    _public_api_access_payload,
    _report_reference_payload,
    _runtime_access_payload,
    _runtime_error_message,
    _saved_provider_auth_payload,
    _saved_user_token_ready,
    _scope_breakdown,
    _site_profile_payload,
    _user_api_access_payload,
    _warcraftlogs_command_prefix,
)

app = typer.Typer(add_completion=False, help="Warcraft Logs official API CLI.")
auth_app = typer.Typer(add_completion=False, help="Warcraft Logs authentication helpers.")
app.add_typer(auth_app, name="auth")

FIGHT_ID_OPTION = typer.Option(None, "--fight-id", help="Optional fight ID filter. Repeat as needed.")
_INCLUDE_RAW_HELP = (
    "Attach the untyped Warcraft Logs table entry to every row. Off by default: the raw entries "
    "carry full gear/pet/ability detail and dominate the payload size."
)
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


def _cfg(ctx: typer.Context) -> RuntimeConfig:
    return cfg_as(ctx, RuntimeConfig)


def _emit(ctx: typer.Context, payload: dict[str, Any], *, client: Any = None) -> None:
    """Emit a command's flat payload as the envelope: its fields go under ``data``, once.

    A payload may set the envelope's ``kind``, ``query`` and ``provenance``; ``kind`` defaults to
    the leaf command (``auth status`` is kind ``status``). ``command`` is the full subcommand path,
    the same label a failure carries. Error envelopes go to stderr through ``_fail``, never here.
    """
    if client is not None:
        payload = _with_warnings(payload, client)
    payload = _with_window_clamp_notes(payload)
    command = command_path(ctx)
    envelope = success_envelope(
        provider="warcraftlogs",
        command=command,
        kind=payload.get("kind") or command.rsplit(" ", 1)[-1].replace("-", "_"),
        data=payload_body(payload),
        query=payload.get("query"),
        provenance=payload.get("provenance"),
    )
    emit(ctx, envelope)


def _with_warnings(payload: dict[str, Any], client: Any) -> dict[str, Any]:
    warnings = list(getattr(client, "graphql_warnings", []) or [])
    if not warnings:
        return payload
    notes = list(payload.get("notes") or [])
    notes.extend(f"warcraft logs returned partial errors: {w.get('message', '')}" for w in warnings if isinstance(w, dict))
    return {**payload, "notes": notes, "graphql_warnings": warnings}


# Warcraft Logs error codes that mean "the caller is not authorised", on top of the shared vocabulary.
# `site_profile_mismatch` belongs here: the saved token exists but is not usable for the selected site.
_AUTH_ERROR_CODES = frozenset(
    {
        "missing_client_credentials",
        "missing_public_auth",
        "missing_user_auth",
        "site_profile_mismatch",
        "user_token_expired",
    }
)


# Rejected or contradictory command input is a usage error (exit 2), like Click's own parse failures.
# Keep every locally-raised input code here: an omission silently downgrades the command to exit 1.
_USAGE_ERROR_CODES = frozenset(
    {
        "ambiguous_boss",
        "boss_scope_mismatch",
        "invalid_variables",
        "missing_boss",
        "missing_query",
        "missing_scope",
        "missing_spec",
        "missing_state",
        "redirect_uri_mismatch",
        "state_mismatch",
    }
)


def _fail(ctx: typer.Context, code: str, message: str, *, details: dict[str, Any] | None = None) -> NoReturn:
    """Fail with the Warcraft Logs exit-code mapping."""
    if code in _AUTH_ERROR_CODES:
        exit_code = EXIT_AUTH
    elif code in _USAGE_ERROR_CODES:
        exit_code = EXIT_USAGE
    else:
        exit_code = exit_code_for(code)
    fail(ctx, code, message, exit_code=exit_code, details=details)


def _finite_float(value: str) -> float:
    """A float option's value; nan and inf fail as usage errors instead of breaking the JSON request and envelope."""
    try:
        number = float(value)
    except ValueError:
        number = math.nan
    if not math.isfinite(number):
        raise typer.BadParameter(f"{value!r} is not a finite number.")
    return number


def _float_option(*param_decls: str, help: str) -> Any:
    """An optional float flag that rejects nan and inf."""
    return typer.Option(None, *param_decls, help=help, parser=_finite_float, metavar="FLOAT")


def _epoch_ms(value: str) -> float:
    """A report-range bound: an ISO-8601 date or time (UTC unless it carries an offset), or UNIX epoch milliseconds.

    The date is tried first so a compact ``20260901`` is that day, not 20260901 ms after 1970.
    """
    try:
        moment = datetime.fromisoformat(value.strip())
    except ValueError:
        try:
            return _finite_float(value)
        except typer.BadParameter:
            raise typer.BadParameter(f"{value!r} is neither UNIX epoch milliseconds nor an ISO-8601 date.") from None
    return (moment if moment.tzinfo else moment.replace(tzinfo=UTC)).timestamp() * 1000


def _epoch_ms_option(*param_decls: str, help: str) -> Any:
    return typer.Option(None, *param_decls, help=help, parser=_epoch_ms, metavar="EPOCH_MS|DATE")


# Warcraft Logs' retail raid difficulty ids; `warcraftlogs zone <id>` lists the ones a zone has.
_DIFFICULTY_IDS = {"lfr": 1, "normal": 3, "heroic": 4, "mythic": 5}


def _difficulty_id(value: str) -> int:
    """A ``--difficulty`` value: a Warcraft Logs difficulty id, or lfr/normal/heroic/mythic for its retail id."""
    text = value.strip().lower()
    if text in _DIFFICULTY_IDS:
        return _DIFFICULTY_IDS[text]
    try:
        return int(text)
    except ValueError:
        raise typer.BadParameter(
            f"{value!r} is not a difficulty: use an id or lfr (1), normal (3), heroic (4) or mythic (5)."
        ) from None


def _difficulty_option(help: str = "Optional difficulty filter.") -> Any:
    return typer.Option(
        None,
        "--difficulty",
        help=f"{help} An id or name: lfr = 1, normal = 3, heroic = 4, mythic = 5 (`warcraftlogs zone <id>` lists a zone's).",
        parser=_difficulty_id,
        metavar="DIFFICULTY",
    )


def _graphql_enum_option(
    flag: str, values: tuple[str, ...], *, help: str, default: str | None = None, aliases: dict[str, str] | None = None
) -> Any:
    """An optional flag for one Warcraft Logs GraphQL enum, checked locally so a typo fails with the valid values.

    Matching ignores case, hyphens, underscores and spaces (``damage-done`` is ``DamageDone``).
    """
    by_key = {value.lower(): value for value in values} | (aliases or {})

    def parse(text: str) -> str:
        key = re.sub(r"[-_\s]+", "", text).lower()
        if key not in by_key:
            raise typer.BadParameter(f"{text!r} is not one of: {', '.join(values)}.")
        return by_key[key]

    return typer.Option(default, flag, help=f"{help} One of: {', '.join(values)}.", parser=parse, metavar="TEXT")


_HOSTILITY_TYPES = ("Friendlies", "Enemies")
_KILL_TYPES = ("All", "Encounters", "Kills", "Trash", "Wipes")
_VIEW_TYPES = ("Default", "Ability", "Source", "Target")
# TableDataType and GraphDataType share these values; EventDataType swaps Summary and Survivability for All and CombatantInfo.
_TABLE_DATA_TYPES = (
    "Summary", "Buffs", "Casts", "DamageDone", "DamageTaken", "Deaths", "Debuffs", "Dispels", "Healing", "Interrupts",
    "Resources", "Summons", "Survivability", "Threat",
)
_EVENT_DATA_TYPES = (
    "All", "Buffs", "Casts", "CombatantInfo", "DamageDone", "DamageTaken", "Deaths", "Debuffs", "Dispels", "Healing",
    "Interrupts", "Resources", "Summons", "Threat",
)
_HOSTILITY_OPTION = _graphql_enum_option("--hostility-type", _HOSTILITY_TYPES, help="Optional hostility filter.")
_KILL_TYPE_OPTION = _graphql_enum_option("--kill-type", _KILL_TYPES, help="Optional kill filter.")
_VIEW_BY_OPTION = _graphql_enum_option("--view-by", _VIEW_TYPES, help="Optional view grouping.")
# Specs Warcraft Logs ranks on healing; every one of these names is a healer for each class that has it.
_HEALER_SPEC_SLUGS = frozenset({"Discipline", "Holy", "Mistweaver", "Preservation", "Restoration"})
# Warcraft Logs' difficulty id for a Mythic+ (keystone dungeon) zone or fight.
_DUNGEON_DIFFICULTY = 10


def _default_ranking_metric(zone: dict[str, Any], spec_slug: str | None) -> str:
    """The metric encounter-rankings sends when ``--metric`` is omitted, so ``query.metric`` names what ranked the rows.

    Warcraft Logs' own default is score in a Mythic+ zone and dps in a raid zone, healers included, so a healer spec
    is ranked on hps only in a raid zone.
    """
    if any(isinstance(row, dict) and row.get("id") == _DUNGEON_DIFFICULTY for row in list_at(zone, "difficulties")):
        return "playerscore"
    return "hps" if spec_slug in _HEALER_SPEC_SLUGS else "dps"


def _load_graphql_query(ctx: typer.Context, query: str | None, *, introspect: bool) -> str:
    if introspect:
        return WARCRAFTLOGS_INTROSPECTION_QUERY
    if query is None or not query.strip():
        _fail(ctx, "missing_query", "warcraftlogs graphql requires --query unless --introspect is set.")
    if query == "-":
        try:
            loaded = sys.stdin.buffer.read().decode("utf-8")
        except UnicodeDecodeError as exc:
            _fail(ctx, "invalid_query", f"GraphQL query on stdin is not UTF-8 text: {exc}")
    elif query.startswith("@"):
        path_text = query[1:]
        if not path_text:
            _fail(ctx, "invalid_query", "--query @path requires a non-empty path.")
        path = Path(path_text).expanduser()
        try:
            loaded = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            _fail(ctx, "not_found", f"GraphQL query file {str(path)!r} does not exist.")
        except (OSError, UnicodeDecodeError) as exc:
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


# characterRankings takes Warcraft Logs' own CamelCase slugs ("DeathKnight", "BeastMastery") and
# answers "Invalid class and spec specified." for the spaced display names, and a character's
# zoneRankings silently ignores any other spec spelling (both checked live 2026-09-30).
def _warcraftlogs_class_slug(ctx: typer.Context, value: str | None) -> str | None:
    """The Warcraft Logs class slug for any provider's class spelling ("death-knight", "Death Knight", "dk").

    Warcraft Logs answers an unknown class name unfiltered instead of rejecting it, so one fails ``invalid_query``.
    """
    text = (value or "").strip()
    if not text:
        return None
    class_key = lookup_class(text)
    if class_key is None:
        classes = ", ".join(warcraftlogs_class_slug(key) for key in WOW_CLASS_NAMES)
        _fail(ctx, "invalid_query", f"Unknown --class-name {text!r}; expected one of: {classes}.")
    return warcraftlogs_class_slug(class_key)


_SPEC_NAME_HINT = "name a spec (Frost), a class and spec (Frost Mage) or shorthand (bm)."


def _warcraftlogs_spec_slug(ctx: typer.Context, value: str | None, *, strict: bool) -> str | None:
    """The Warcraft Logs spec slug for any provider's spec spelling ("beast-mastery", "bm hunter", "hunter-beastmastery").

    An unrecognised value fails ``invalid_query`` when ``strict``; otherwise it passes through trimmed,
    for a value Warcraft Logs rejects itself or a site whose specs the retail table does not cover.
    """
    text = (value or "").strip()
    if not text:
        return None
    # Each spec a spelling names shares one slug: a bare "Frost" is the Death Knight's and the Mage's.
    slugs = {spec.warcraftlogs_spec_slug for spec in retail_specs_named(text)}
    if not slugs and strict:
        _fail(ctx, "invalid_query", f"Unknown --spec-name {text!r}; {_SPEC_NAME_HINT}")
    return slugs.pop() if slugs else text


def _ranking_class_and_spec(ctx: typer.Context, class_name: str | None, spec_name: str | None) -> tuple[str | None, str | None]:
    """The class and spec slugs for encounterRankings, which needs a className with a specName.

    A spec spelling that names one class (bm hunter, fdk, ret) supplies the class when ``--class-name`` is absent,
    and one that names a different class than ``--class-name`` fails ``invalid_query``.
    """
    class_slug = _warcraftlogs_class_slug(ctx, class_name)
    spec_slug = _warcraftlogs_spec_slug(ctx, spec_name, strict=False)
    named = {warcraftlogs_class_slug(spec.class_key) for spec in retail_specs_named((spec_name or "").strip())}
    if len(named) == 1:
        (spec_class,) = named
        if class_slug is not None and class_slug != spec_class:
            _fail(ctx, "invalid_query", f"--spec-name {spec_name!r} is a {spec_class} spec, but --class-name is {class_name!r}.")
        class_slug = spec_class
    return class_slug, spec_slug


def _client(ctx: typer.Context) -> WarcraftLogsClient:
    try:
        return WarcraftLogsClient(site=_cfg(ctx).site_profile)
    except Exception as exc:
        _fail(ctx, "invalid_runtime_config", _runtime_error_message(str(exc)))


def _handle_client_error(ctx: typer.Context, exc: WarcraftLogsClientError) -> NoReturn:
    _fail(ctx, exc.code, exc.message)


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


# Warcraft Logs numbers classes itself (gameData.classes, the same on every site), not the way Blizzard does:
# its class 5 is Monk, Blizzard's is Priest.
_WARCRAFTLOGS_CLASS_KEYS = (
    "deathknight", "druid", "hunter", "mage", "monk", "paladin", "priest",
    "rogue", "shaman", "warlock", "warrior", "demonhunter", "evoker",
)


def _class_name(class_id: object) -> str | None:
    """The class a Warcraft Logs ``classID`` names, or ``None`` for an unknown id."""
    if not isinstance(class_id, int) or not 1 <= class_id <= len(_WARCRAFTLOGS_CLASS_KEYS):
        return None
    return WOW_CLASS_NAMES[_WARCRAFTLOGS_CLASS_KEYS[class_id - 1]]


def _guild_member_payload(character: dict[str, Any]) -> dict[str, Any]:
    faction = dict_at(character, "faction")
    server = dict_at(character, "server")
    return {
        "id": character.get("id"),
        "canonical_id": character.get("canonicalID"),
        "name": character.get("name"),
        "level": character.get("level"),
        "class_id": character.get("classID"),
        "class_name": _class_name(character.get("classID")),
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
        "class_name": _class_name(character.get("classID")),
        "hidden": character.get("hidden"),
        "server": _server_payload(server) if server else None,
        "guild_rank": character.get("guildRank"),
        "faction": {"id": faction.get("id"), "name": faction.get("name")} if faction else None,
        "guilds": normalized_guilds,
    }


def _report_reference(ctx: typer.Context, reference: str, *, fight_id: int | None = None) -> ReportReference:
    """A report URL or bare report code on the selected site; anything else fails ``invalid_query`` before a request."""
    site = _cfg(ctx).site_profile
    try:
        ref = _parse_report_reference(reference, explicit_fight_id=fight_id)
    except ValueError as exc:
        _fail(ctx, "invalid_query", str(exc))
    if ref.site is not None and ref.site.key != site.key:
        _fail(
            ctx,
            "invalid_query",
            f"{reference!r} is a {ref.site.label} Warcraft Logs report, but the selected site is {site.key!r}. "
            f"Re-run with `{_warcraftlogs_command_prefix(ref.site)} ...`.",
        )
    return ref


def _report_code_and_fights(ctx: typer.Context, reference: str, fight_ids: list[int] | None) -> tuple[str, list[int] | None]:
    """A report command's code and fights: a URL's ``#fight=N`` scopes it when ``--fight-id`` is absent."""
    ref = _report_reference(ctx, reference)
    return ref.code, fight_ids or ([ref.fight_id] if ref.fight_id is not None else None)


def _fight_encounter_id(fight: dict[str, Any]) -> int | None:
    """The fight's boss encounter ID; ``None`` for a trash fight, which Warcraft Logs reports as 0."""
    encounter_id = fight.get("encounterID")
    return encounter_id if isinstance(encounter_id, int) and encounter_id > 0 else None


def _kill_type_for_fight(fight: dict[str, Any]) -> str | None:
    """Kills or Wipes for a boss fight. A trash fight is neither, so its slice carries no kill filter."""
    if _fight_encounter_id(fight) is None:
        return None
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
    ref = _report_reference(ctx, reference, fight_id=fight_id)
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
        _fail(
            ctx,
            "missing_scope",
            "Provide --fight-id or a report URL with a numeric ?fight=... or #fight=... for encounter-scoped analysis.",
        )
    encounter_id = _fight_encounter_id(selected)
    encounter = None
    if encounter_id is not None:
        try:
            encounter = client.encounter(encounter_id=encounter_id)
        except WarcraftLogsClientError:
            encounter = None
    return ref, report, selected, encounter


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
        encounter_id=_fight_encounter_id(fight),
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
    flag: str,
) -> tuple[float | None, float | None]:
    """Absolute report timestamps for an encounter-relative window, clamped to the fight.

    ``flag`` names the window's options. A window that runs past the pull keeps only the part
    inside it; ``_effective_window`` reports what was kept.
    """
    if window_start_ms is None and window_end_ms is None:
        return None, None
    fight_start = fight.get("startTime")
    if not isinstance(fight_start, (int, float)):
        _fail(ctx, "invalid_response", "Selected fight did not include a start timestamp for encounter windowing.")
    if window_start_ms is not None and window_end_ms is not None and window_end_ms < window_start_ms:
        _fail(ctx, "invalid_query", f"{flag}-end-ms must be greater than or equal to {flag}-start-ms.")
    fight_end = fight.get("endTime")
    # A window that opens after the pull ended holds no events; answering zero would read as "none happened".
    if window_start_ms is not None and isinstance(fight_end, (int, float)) and fight_start + window_start_ms >= fight_end:
        _fail(
            ctx,
            "invalid_query",
            f"{flag}-start-ms {window_start_ms:g} is at or past the end of the fight ({fight_end - fight_start:g} ms long).",
        )
    absolute_start = float(fight_start) + max(float(window_start_ms), 0.0) if window_start_ms is not None else None
    absolute_end = float(fight_start) + float(window_end_ms) if window_end_ms is not None else None
    if absolute_end is not None and isinstance(fight_end, (int, float)):
        absolute_end = min(absolute_end, float(fight_end))
    return absolute_start, absolute_end


def _effective_window(
    fight: dict[str, Any],
    *,
    window_start_ms: float | None,
    window_end_ms: float | None,
) -> dict[str, Any]:
    """The encounter-relative window a query really covers, and whether the request was clamped to the fight.

    Computed from the requested offsets, not from the absolute bounds, so a fractional offset that
    round-trips through a report timestamp inexactly is not reported as clamped.
    """
    fight_start = fight.get("startTime")
    fight_end = fight.get("endTime")
    if (window_start_ms is None and window_end_ms is None) or not isinstance(fight_start, (int, float)):
        return {}
    fight_length = float(fight_end - fight_start) if isinstance(fight_end, (int, float)) else None
    start_ms = max(window_start_ms, 0.0) if window_start_ms is not None else 0.0
    end_ms = window_end_ms if window_end_ms is not None else fight_length
    if end_ms is not None and fight_length is not None:
        end_ms = min(end_ms, fight_length)
    clamped = end_ms is not None and (
        (window_start_ms is not None and window_start_ms < 0)
        or (window_end_ms is not None and fight_length is not None and window_end_ms > fight_length)
    )
    return {
        "effective_window_start_ms": start_ms,
        "effective_window_end_ms": end_ms,
        "effective_window_duration_ms": end_ms - start_ms if end_ms is not None else None,
        "window_clamped": clamped,
    }


def _with_window_clamp_notes(payload: dict[str, Any]) -> dict[str, Any]:
    """Add a note for each encounter window (the query's, or each compared window's) that ran outside the fight."""

    def requested(offset: float | None, default: str) -> str:
        return default if offset is None else f"{offset:g}"

    queries = [dict_at(payload, "query"), *(dict_at(window, "query") for window in list_at(payload, "windows"))]
    notes = [
        f"The window requested as {requested(query['window_start_ms'], 'start')}..{requested(query['window_end_ms'], 'end')} "
        f"ms ran outside the fight and was clamped to {query['effective_window_start_ms']:g}.."
        f"{query['effective_window_end_ms']:g} ms; compare uptime and counts against effective_window_duration_ms "
        f"({query['effective_window_duration_ms']:g} ms), not the requested length."
        for query in queries
        if query.get("window_clamped")
    ]
    return {**payload, "notes": [*(payload.get("notes") or []), *notes]} if notes else payload


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
    # The option prefix the window came from, so a rejected window names the caller's own flag.
    window_flag: str = "--window"


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
        flag=filters.window_flag,
    )
    encounter_id = _fight_encounter_id(fight)
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
        **_effective_window(fight, window_start_ms=filters.window_start_ms, window_end_ms=filters.window_end_ms),
    }
    return options, query


def _require_report_slice(
    ctx: typer.Context,
    *,
    command: str,
    fight_id: list[int] | None,
    start_time: float | None,
    end_time: float | None,
) -> None:
    """Reject a query Warcraft Logs answers with an empty payload plus a GraphQL warning.

    Warcraft Logs accepts exactly two slice shapes here: fight IDs, or a start time AND an end
    time. ``--encounter-id`` and a half-open time range narrow nothing on their own, so they are
    not accepted as a slice.
    """
    if fight_id or (start_time is not None and end_time is not None):
        return
    _fail(
        ctx,
        "missing_scope",
        f"{command} requires --fight-id, or both --start-time and --end-time. "
        "Warcraft Logs answers any wider query with an empty payload; --encounter-id filters "
        "the slice but does not define one.",
    )


def _require_matching_fight(
    ctx: typer.Context,
    client: WarcraftLogsClient,
    *,
    code: str,
    allow_unlisted: bool,
    fight_ids: list[int] | None,
    encounter_id: int | None,
    difficulty: int | None,
    start_time: float | None = None,
    end_time: float | None = None,
) -> float | None:
    """Reject a fight-scoped request naming a fight the report does not have, or an empty window.

    Warcraft Logs answers an unknown ``--fight-id``, an ``--encounter-id`` the report never
    pulled, or a ``--difficulty`` those fights were not on with an empty or null slice and HTTP
    200, which reads as "that fight had no data" instead of "no such fight". Every requested fight
    ID has to exist and match the other filters: one missing ID fails the request and is named in
    ``error.details.missing_fight_ids``, instead of the answer silently covering only the others.
    Requests that name no fight at all are left alone: a report-wide slice is a legitimate query,
    and an empty answer to one is a real answer. An inverted window, or one starting after every
    selected fight ended, is answered with an empty slice too, so both are ``invalid_query``.
    Returns the latest end time of the selected fights, or None when no fight was named.
    """
    _require_ordered_window(ctx, start_time=start_time, end_time=end_time)
    if not fight_ids and encounter_id is None and difficulty is None:
        return None
    fights_report = client.report_fights(code=code, difficulty=difficulty, allow_unlisted=allow_unlisted)
    matching = [
        row
        for row in list_at(fights_report, "fights")
        if isinstance(row, dict) and (encounter_id is None or row.get("encounterID") == encounter_id)
    ]
    matching_ids = {row.get("id") for row in matching}
    missing = [fight_id for fight_id in fight_ids or [] if fight_id not in matching_ids]
    if not matching_ids or missing:
        scope = {"fight_ids": missing or None, "encounter_id": encounter_id, "difficulty": difficulty}
        _fail(
            ctx,
            "not_found",
            f"Warcraft Logs report {code} has no fight matching {_described_slice(scope)}.",
            details={"missing_fight_ids": missing} if missing else None,
        )
    fights_end = _selected_fights_end(matching, fight_ids=fight_ids)
    if start_time is not None and fights_end is not None and start_time > fights_end:
        _fail(ctx, "invalid_query", f"--start-time {start_time:.0f} is after the selected fights end at {fights_end:.0f}.")
    return fights_end


def _selected_fights_end(fights: list[dict[str, Any]], *, fight_ids: list[int] | None) -> float | None:
    return max(
        (
            float(row["endTime"])
            for row in fights
            if (not fight_ids or row.get("id") in fight_ids) and isinstance(row.get("endTime"), (int, float))
        ),
        default=None,
    )


def _require_ordered_window(ctx: typer.Context, *, start_time: float | None, end_time: float | None) -> None:
    if start_time is not None and end_time is not None and start_time > end_time:
        _fail(ctx, "invalid_query", "--start-time must not be after --end-time.")


def _described_slice(query: dict[str, Any]) -> str:
    """Echo the filters a report query actually carried, so a not-found message names the scope."""
    carried = [f"{name}={value!r}" for name, value in sorted(query.items()) if value is not None]
    return ", ".join(carried) if carried else "no filters"


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


def _completed_casts(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep only the ``cast`` events of a Casts slice: one per completed cast.

    The Casts data type also returns ``begincast`` (a cast bar starting, including casts later
    cancelled) and ``empowerstart``/``empowerend`` (an empowered spell's charge). Warcraft Logs records
    ``cast`` once per press of an empowered spell, once per finished cast-time spell, and once when a
    channel starts, so counting only ``cast`` counts each use exactly once.
    """
    return [event for event in events if event.get("type") == "cast"]


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
    event_rows = [row for row in list_at(paginator, "data") if isinstance(row, dict)]
    cast_rows = _completed_casts(event_rows)
    tallies = _tally_cast_events(
        cast_rows,
        naming=naming,
        fight_start=fight.get("startTime") if isinstance(fight.get("startTime"), (int, float)) else None,
        preview_limit=preview_limit,
    )
    next_page_timestamp = paginator.get("nextPageTimestamp")
    truncated = next_page_timestamp is not None
    notes = (
        [
            f"Warcraft Logs returned next_page_timestamp: every aggregate below covers only the {len(cast_rows)} "
            f"casts in the first {len(event_rows)} events of the selected fight/window, not the whole fight. "
            "Raise --limit or narrow the window before treating these counts as complete."
        ]
        if truncated
        else []
    )
    return {
        "report": _report_brief_payload(report),
        "fight": _fight_payload(fight),
        "notes": notes,
        "casts": {
            "event_count": len(event_rows),
            "cast_count": len(cast_rows),
            "truncated": truncated,
            "next_page_timestamp": next_page_timestamp,
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
    # Two row shapes: aura-aggregate rows carry no actor `id` and their own aura `guid`/`name`;
    # actor rows under an --ability-id filter carry the actor's `id`, `name` and GUID in `guid`,
    # so the aura there is the requested filter, not the row.
    is_actor_row = isinstance(entry.get("id"), int)
    aura_guid = entry.get("guid") if isinstance(entry.get("guid"), int) and not is_actor_row else None
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


def _buff_row_actor(table_report: dict[str, Any], *, view_by: str | None) -> Literal["aura_holder", "applied_by"]:
    """Which actor a Buffs-table row names.

    viewBy Source groups rows by the actor that has the aura, viewBy Target by the actor that
    applied it. When a filter pins that actor (``--source-id``), Warcraft Logs groups by the other
    one instead and says so with ``useTargets: true``.
    """
    holder = not (isinstance(view_by, str) and view_by.lower() == "target")
    if dict_at(dict_at(table_report, "table"), "data").get("useTargets") is True:
        holder = not holder
    return "aura_holder" if holder else "applied_by"


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
    actor_field = _buff_row_actor(table_report, view_by=view_by)
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
            # is set — there is no actor to attach. Emit a placeholder that keeps the row shape
            # uniform and makes the missing actor scope explicit.
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
        # The fields Warcraft Logs' Buffs table carries per aura row (see the captured fixture).
        rows_out.append(
            {
                actor_field: actor_payload,
                "aura": _buff_aura_payload(entry, ability_index=ability_index, ability_id=ability_id),
                "reported_total_uptime": entry.get("totalUptime"),
                "reported_total_uses": entry.get("totalUses"),
                "reported_bands": entry.get("bands"),
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
            "row_actor": actor_field,
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
    zone: dict[str, Any],
    zone_id: int,
    boss_id: int | None,
    boss_name: str | None,
) -> dict[str, Any]:
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
    raw_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    actor_class, spec = _player_talent_transport_identity(actor)
    # Warcraft Logs has no SimulationCraft backend: validation stops at the identity checks or at
    # `simc_backend_unavailable`, and `simc validate-talent-transport` does the real validation.
    validation_result = validate_talent_tree_transport(
        actor_class=actor_class, spec=spec, talent_tree_rows=raw_rows, backend=None
    )
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


def _accumulate_boss_spec_counts(
    rows: list[dict[str, Any]],
) -> tuple[dict[tuple[str, str, str], dict[str, Any]], int]:
    spec_counts: dict[tuple[str, str, str], dict[str, Any]] = {}
    sampled_player_rows = 0
    for row in rows:
        code = str((row.get("report") or {}).get("code") or "")
        fight_id = int((row.get("fight") or {}).get("id") or 0)
        player_rows = list_at(row, "player_details")
        seen_specs_for_fight: set[tuple[str, int, str, str, str]] = set()
        for player in player_rows:
            if not isinstance(player, dict):
                continue
            sampled_player_rows += 1
            role = str(player.get("role") or "unknown")
            specs = list_at(player, "specs")
            # Spec names repeat across classes (Frost Mage, Frost Death Knight), so a spec is class + spec.
            class_name = str(player.get("type") or "").strip()
            for spec in specs:
                if not isinstance(spec, dict):
                    continue
                spec_name = str(spec.get("spec") or "").strip()
                if not spec_name:
                    continue
                count = int(spec.get("count") or 0)
                key = (class_name, spec_name, role)
                entry = spec_counts.setdefault(
                    key,
                    {
                        "class_name": class_name or None,
                        "spec_name": spec_name,
                        "role": role,
                        "appearance_count": 0,
                        "kill_presence_count": 0,
                        "sample_fights": [],
                    },
                )
                entry["appearance_count"] += count if count > 0 else 1
                fight_key = (code, fight_id, *key)
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
    transport_counts: dict[str, int],
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
            str(entry["class_name"] or "").lower(),
        ),
    )
    returned = normalized_rows[:top]
    return {
        "kind": "boss_spec_usage",
        "ranking_basis": "sampled_kill_cohort_spec_presence",
        "matching_rule": "spec_presence_across_sampled_kills_with_player_details",
        "query": query,
        "notes": [
            *_sampled_spec_filter_notes(query.get("spec_name") if isinstance(query, dict) else None, sample),
            *_sampled_cohort_notes(sample),
        ],
        "freshness": _sampled_cross_report_freshness(cache_ttl_seconds, transport_counts=transport_counts),
        "cache_provenance": _sampled_cache_provenance(cache_ttl_seconds, rows),
        "sample_scope": _sampled_sample_scope(
            ranking_basis="sampled_kill_cohort_spec_presence",
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
    transport_counts: dict[str, int],
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
        "kind": "comp_samples",
        "ranking_basis": "sampled_fastest_kills",
        "matching_rule": "class_roster_composition_across_sampled_kills_with_player_details",
        "query": query,
        "notes": [
            *_sampled_spec_filter_notes(query.get("spec_name") if isinstance(query, dict) else None, sample),
            *_sampled_cohort_notes(sample),
        ],
        "freshness": _sampled_cross_report_freshness(cache_ttl_seconds, transport_counts=transport_counts),
        "cache_provenance": _sampled_cache_provenance(cache_ttl_seconds, rows),
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
    event_rows = _completed_casts([event for event in list_at(paginator, "data") if isinstance(event, dict)])
    source_counts: dict[int, int] = {}
    for event in event_rows:
        source_id = _event_id(event.get("sourceID"))
        if isinstance(source_id, int):
            source_counts[source_id] = source_counts.get(source_id, 0) + 1
    next_page_timestamp = paginator.get("nextPageTimestamp")
    return {
        "count": len(event_rows),
        # The kill's cast events did not fit in one --event-limit page, so `count` is a lower bound.
        "truncated": next_page_timestamp is not None,
        "next_page_timestamp": next_page_timestamp,
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


def _event_limit_truncation_notes(truncated_kill_count: int, *, event_limit: int) -> list[str]:
    """Say so when sampled kills hit ``--event-limit``, so no total reads as a complete count."""
    if truncated_kill_count <= 0:
        return []
    return [
        f"{truncated_kill_count} sampled kill(s) returned more cast events than --event-limit={event_limit}; "
        "their cast counts, and every total derived from them, are lower bounds. Raise --event-limit for exact totals"
    ]


def _ability_usage_summary_payload(
    *,
    rows: list[dict[str, Any]],
    sample: dict[str, Any],
    query: dict[str, Any],
    ability: dict[str, Any],
    preview_limit: int,
    event_limit: int,
    transport_counts: dict[str, int],
    cache_ttl_seconds: int | None = None,
    root_url: str = "https://www.warcraftlogs.com",
) -> dict[str, Any]:
    row_casts = [casts for casts in (row.get("casts") for row in rows) if isinstance(casts, dict)]
    cast_counts = [int(casts["count"]) for casts in row_casts if isinstance(casts.get("count"), int)]
    used_counts = [count for count in cast_counts if count > 0]
    # A kill whose cast events did not fit in one --event-limit page contributes a lower bound,
    # so every total derived from it is a lower bound too (SAFE_ANALYTICS_RULES.md).
    truncated_kill_count = sum(1 for casts in row_casts if casts.get("truncated"))
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
        "kind": "ability_usage_summary",
        "ranking_basis": "sampled_fastest_kills",
        "matching_rule": "ability_casts_across_sampled_kills_with_event_limit",
        "query": scoped_query,
        "notes": [
            *_sampled_spec_filter_notes(query.get("spec_name") if isinstance(query, dict) else None, sample),
            *_sampled_cohort_notes(sample),
            *_event_limit_truncation_notes(truncated_kill_count, event_limit=event_limit),
        ],
        "freshness": _sampled_cross_report_freshness(cache_ttl_seconds, transport_counts=transport_counts),
        "cache_provenance": _sampled_cache_provenance(cache_ttl_seconds, rows),
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
            "kills_with_truncated_events_count": truncated_kill_count,
        },
        "ability": ability,
        "usage": {
            "total_casts": sum(cast_counts),
            "total_casts_is_lower_bound": truncated_kill_count > 0,
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
    container = dict_at(dict_at(report, "table"), "data")
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


def _fight_identity_confidence(actor: dict[str, Any], *, spec_count: int) -> IdentityConfidence:
    """Class and spec come from the fight itself: one spec is high confidence, several are low."""
    if not isinstance(actor.get("type"), str) or spec_count == 0:
        return "none"
    return "high" if spec_count == 1 else "low"


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


def _report_player_details_payload(report: dict[str, Any], *, report_code: str |
                                   None = None, fight_id: int | None = None) -> dict[str, Any]:
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


def _raid_fight_ids(report: dict[str, Any]) -> set[Any]:
    """The ranked fights of ``report`` that are not Mythic+ runs."""
    return {
        row.get("fightID")
        for row in list_at(dict_at(report, "rankings"), "data")
        if isinstance(row, dict) and row.get("difficulty") != _DUNGEON_DIFFICULTY
    }


def _with_healers_from(report: dict[str, Any], healer_report: dict[str, Any]) -> dict[str, Any]:
    """``report``'s rankings with each raid fight's healer role taken from the same fight in ``healer_report``."""
    raid_fights = _raid_fight_ids(report)
    healers_by_fight = {
        row.get("fightID"): dict_at(row, "roles").get("healers")
        for row in list_at(dict_at(healer_report, "rankings"), "data")
        if isinstance(row, dict) and row.get("fightID") in raid_fights
    }
    rows = [
        {**row, "roles": {**dict_at(row, "roles"), "healers": healers_by_fight[row.get("fightID")]}}
        if isinstance(row, dict) and healers_by_fight.get(row.get("fightID")) is not None
        else row
        for row in list_at(dict_at(report, "rankings"), "data")
    ]
    return {**report, "rankings": {**dict_at(report, "rankings"), "data": rows}}


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
    include_raw: bool,
) -> dict[str, Any]:
    actor_index, ability_index = _master_data_indexes(master_report)
    actor_field = _buff_row_actor(table_report, view_by="Source")
    rows_out: list[dict[str, Any]] = []
    for entry in _report_table_entries(table_report):
        actor_id = entry.get("id") if isinstance(entry.get("id"), int) else None
        rows_out.append(
            {
                actor_field: _named_actor(
                    actor_index,
                    actor_id,
                    report_code=report.get("code") if isinstance(report.get("code"), str) else None,
                    fight_id=fight.get("id") if isinstance(fight.get("id"), int) else None,
                    source="report_encounter_aura_summary",
                ) if actor_id is not None else {"id": None, "name": entry.get("name")},
                # An ability-scoped Buffs table reports per-actor uptime (ms), uses and bands only.
                "reported_total_uptime": entry.get("totalUptime"),
                "reported_total_uses": entry.get("totalUses"),
                "reported_bands": entry.get("bands"),
                **({"raw_entry": entry} if include_raw else {}),
            }
        )
    rows_out.sort(
        key=lambda row: (
            -(float(row["reported_total_uptime"]) if isinstance(row.get("reported_total_uptime"), (int, float)) else float("-inf")),
            str((row.get(actor_field) or {}).get("name") or ""),
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
            "row_actor": actor_field,
            "rows": rows_out,
        },
    }


def _report_encounter_damage_summary_payload(
    *,
    report: dict[str, Any],
    fight: dict[str, Any],
    table_report: dict[str, Any],
    master_report: dict[str, Any],
    actor_field: Literal["source", "target"],
    include_raw: bool,
) -> dict[str, Any]:
    """Damage-table rows keyed by the grouping actor, typed and sorted by reported total."""
    actor_index, _ability_index = _master_data_indexes(master_report)
    identity_source = f"report_encounter_damage_{actor_field}_summary"
    rows_out: list[dict[str, Any]] = []
    for entry in _report_table_entries(table_report):
        actor_id = entry.get("id") if isinstance(entry.get("id"), int) else None
        rows_out.append(
            {
                actor_field: _named_actor(
                    actor_index,
                    actor_id,
                    report_code=report.get("code") if isinstance(report.get("code"), str) else None,
                    fight_id=fight.get("id") if isinstance(fight.get("id"), int) else None,
                    source=identity_source,
                ) if actor_id is not None else {"id": None, "name": entry.get("name")},
                "reported_total": entry.get("total"),
                **({"raw_entry": entry} if include_raw else {}),
            }
        )
    rows_out.sort(
        key=lambda row: (
            -(float(row["reported_total"]) if isinstance(row.get("reported_total"), (int, float)) else float("-inf")),
            str((row.get(actor_field) or {}).get("name") or ""),
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


# The fields a Warcraft Logs Buffs table actually carries per source (totalUptime ms, totalUses).
_AURA_COMPARE_FIELDS = ("reported_total_uptime", "reported_total_uses")


def _aura_compare_rows(
    *,
    left_rows: list[dict[str, Any]],
    right_rows: list[dict[str, Any]],
    actor_field: str,
) -> list[dict[str, Any]]:
    """One row per aura-summary actor, with right-minus-left deltas of uptime and uses, largest uptime change first."""

    def _row_key(row: dict[str, Any]) -> tuple[int | None, str]:
        actor = dict_at(row, actor_field)
        actor_id = actor.get("id") if isinstance(actor.get("id"), int) else None
        return actor_id, str(actor.get("name") or "")

    def _compare_row(key: tuple[int | None, str], left: dict[str, Any] | None, right: dict[str, Any] | None) -> dict[str, Any]:
        compared: dict[str, Any] = {
            actor_field: dict_at(left or {}, actor_field) or dict_at(right or {}, actor_field) or {"id": key[0], "name": key[1]},
        }
        for field in _AURA_COMPARE_FIELDS:
            left_value = (left or {}).get(field)
            right_value = (right or {}).get(field)
            compared[f"left_{field}"] = left_value
            compared[f"right_{field}"] = right_value
            compared[f"{field}_delta"] = (
                right_value - left_value
                if isinstance(left_value, (int, float)) and isinstance(right_value, (int, float))
                else None
            )
        return {**compared, "left_row": left, "right_row": right}

    left_index = {_row_key(row): row for row in left_rows}
    right_index = {_row_key(row): row for row in right_rows}
    compared_rows = [_compare_row(key, left_index.get(key), right_index.get(key)) for key in set(left_index) | set(right_index)]
    compared_rows.sort(
        key=lambda row: (
            -abs(row["reported_total_uptime_delta"]) if row["reported_total_uptime_delta"] is not None else 1,
            str(row[actor_field].get("name") or "").lower(),
        )
    )
    return compared_rows


def _character_rankings_payload(character: dict[str, Any], *, top: int, transport_counts: dict[str, int]) -> dict[str, Any]:
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
    class_name = _class_name(character.get("classID"))
    source_character_identity = class_spec_identity_payload(
        actor_class=class_name,
        spec=unique_specs[0] if len(unique_specs) == 1 else None,
        provider="warcraftlogs",
        source="character_rankings",
        candidates=[(class_name, spec) for spec in unique_specs] if len(unique_specs) > 1 else None,
    )
    return {
        "id": character.get("id"),
        "canonical_id": character.get("canonicalID"),
        "name": character.get("name"),
        "level": character.get("level"),
        "class_id": character.get("classID"),
        "class_name": class_name,
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
            "freshness": _sampled_cross_report_freshness(transport_counts=transport_counts),
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


@app.command("search")
def search(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="Explicit Warcraft Logs report URL or report code."),
    limit: int = typer.Option(5, "--limit", min=1, max=50, help="Maximum rows to return; an explicit reference yields at most one."),
) -> None:
    """Match an explicit Warcraft Logs report URL or code; free text returns a discovery hint."""
    emit(ctx, provider_search(query, limit=limit, site=_cfg(ctx).site_profile))


@app.command("resolve")
def resolve(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="Explicit Warcraft Logs report URL or report code."),
    limit: int = typer.Option(5, "--limit", min=1, max=50, help="Maximum candidates to list; an explicit reference yields at most one."),
) -> None:
    """Resolve an explicit Warcraft Logs report URL or code to a single report reference."""
    del limit
    emit(ctx, provider_resolve(query, site=_cfg(ctx).site_profile))


@app.command("doctor")
def doctor(
    ctx: typer.Context,
    no_live: bool = typer.Option(False, "--no-live", help="Skip live Warcraft Logs auth probes and report local/runtime readiness only."),
) -> None:
    """Report Warcraft Logs auth, site profile, and per-command readiness."""
    emit(ctx, provider_doctor(live=not no_live, site=_cfg(ctx).site_profile))


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
            "mode": mode,
            "step": "token_exchanged",
            "endpoint_family": "user",
            "site_profile": _site_profile_payload(_cfg(ctx).site_profile),
            "state_path": str(saved_path),
            "token": {
                "token_type": token_summary.get("token_type"),
                "scope": token_summary.get("scope"),
                "expires_at": token_summary.get("expires_at"),
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
    authorization_code: str | None = typer.Option(None, "--code", help="Authorization code returned by the redirect callback."),
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
    if not authorization_code:
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
        payload = client.exchange_authorization_code(code=authorization_code, redirect_uri=redirect_uri)
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)
    finally:
        client.close()
    _emit_token_step(ctx, mode="authorization_code", pending=pending, payload=payload, redirect_uri=redirect_uri, client=client)


@auth_app.command("pkce-login")
def auth_pkce_login(
    ctx: typer.Context,
    redirect_uri: str = typer.Option(..., "--redirect-uri", help="Registered redirect URI for the Warcraft Logs OAuth client."),
    authorization_code: str | None = typer.Option(None, "--code", help="Authorization code returned by the redirect callback."),
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
    if not authorization_code:
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
        payload = client.exchange_pkce_code(code=authorization_code, redirect_uri=redirect_uri, code_verifier=code_verifier)
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
    _emit(ctx, {"count": len(regions_payload), "regions": regions_payload}, client=client)


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
    _emit(ctx, {"count": len(expansions_payload), "expansions": expansions_payload}, client=client)


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
    _emit(ctx, {"server": _server_payload(payload)}, client=client)


@app.command("zones")
def zones(
    ctx: typer.Context,
    expansion_id: int | None = typer.Option(
        None,
        "--expansion-id",
        help=(
            "Optional Warcraft Logs expansion ID filter: 7 = Midnight, 6 = The War Within "
            "(`warcraftlogs expansions` lists them; Raider.IO numbers expansions differently)."
        ),
    ),
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
    _emit(ctx, {"zone": _zone_payload(payload)}, client=client)


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
        "server_region": profile_region(request.server_region) if request.server_region else None,
        "server_slug": options.server_slug,
        "leaderboard": options.leaderboard,
        "hard_mode_level": options.hard_mode_level,
        "filter": options.filter,
        "include_combatant_info": options.include_combatant_info,
        "include_other_players": options.include_other_players,
        "top": request.top,
    }


def _run_encounter_rankings(ctx: typer.Context, request: _EncounterRankingsRequest) -> None:
    if request.server_region:
        _check_region(ctx, request.server_region)
    client = _client(ctx)
    try:
        zone = client.zone(zone_id=request.zone_id)
        encounter_payload = _resolve_encounter(
            ctx,
            client=client,
            zone=zone,
            zone_id=request.zone_id,
            boss_id=request.boss_id,
            boss_name=request.boss_name,
        )
        if request.options.metric is None:
            request = replace(
                request, options=replace(request.options, metric=_default_ranking_metric(zone, request.options.spec_name))
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
    difficulty: int | None = _difficulty_option(),
    class_name: str | None = typer.Option(None, "--class-name", help="Optional class filter (Death Knight, death-knight, dk)."),
    spec_name: str | None = typer.Option(None, "--spec-name", help="Optional spec filter (Beast Mastery, beast-mastery, bm)."),
    metric: str | None = typer.Option(
        None,
        "--metric",
        help="Ranking metric such as dps, hps, or bossdps. Defaults to playerscore in a Mythic+ zone; "
        "in a raid zone hps for a healer --spec-name, else dps.",
    ),
    page: int | None = typer.Option(None, "--page", min=1, help="Optional rankings page number."),
    partition: int | None = typer.Option(None, "--partition", help="Optional Warcraft Logs partition filter."),
    size: int | None = typer.Option(None, "--size", help="Optional raid size filter."),
    server_region: str | None = typer.Option(None, "--server-region", help="Optional server region filter."),
    server_slug: str | None = typer.Option(None, "--server-slug", help="Optional server slug filter."),
    leaderboard: str | None = _graphql_enum_option("--leaderboard", ("Any", "LogsOnly"), help="Optional leaderboard filter."),
    hard_mode_level: str | None = _graphql_enum_option(
        "--hard-mode-level",
        ("Any", "Highest", "NormalMode", "Level0", "Level1", "Level2", "Level3", "Level4"),
        help="Optional hard-mode-level filter (no-hard-mode is NormalMode).",
        aliases={"nohardmode": "NormalMode"},
    ),
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
    top: int = typer.Option(10, "--limit", "--top", min=1, max=100, help="Maximum returned ranking rows after normalization."),
) -> None:
    """Rank characters on one encounter, filtered by class, spec, difficulty, and server."""
    class_slug, spec_slug = _ranking_class_and_spec(ctx, class_name, spec_name)
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
                leaderboard=leaderboard,
                hard_mode_level=hard_mode_level,
                metric=metric,
                filter=filter_text,
                include_combatant_info=include_combatant_info,
                include_other_players=include_other_players,
                class_name=class_slug,
                # Warcraft Logs rejects an unknown spec here itself ("Invalid class and spec specified.").
                spec_name=spec_slug,
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
    difficulty: int | None = _difficulty_option("Optional difficulty for speed ranks."),
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
    difficulty: int | None = _difficulty_option(),
    metric: str | None = typer.Option(None, "--metric", help="Optional ranking metric such as dps, hps, or tankhps."),
    size: int | None = typer.Option(None, "--size", help="Optional raid size."),
    spec_name: str | None = typer.Option(None, "--spec-name", help="Optional spec filter (Beast Mastery, beast-mastery, bm)."),
    top: int = typer.Option(5, "--limit", "--top", min=1, max=20, help="Number of top ranking rows to keep in the summary."),
) -> None:
    """Show a character's encounter rankings for one zone."""
    # The spec list is retail's; a classic site has specs it lacks (Combat), so only retail is checked.
    retail = _cfg(ctx).site_profile.key == RETAIL_PROFILE.key
    spec_slug = _warcraftlogs_spec_slug(ctx, spec_name, strict=retail)
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
            spec_name=spec_slug,
        )
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)
    finally:
        client.close()
    _emit(
        ctx,
        {
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
            "character_rankings": _character_rankings_payload(payload, top=top, transport_counts=client.transport_counts),
        },
        client=client,
    )


@app.command("report")
def report(
    ctx: typer.Context,
    code: str = typer.Argument(..., help="Warcraft Logs report URL or report code."),
    allow_unlisted: bool = typer.Option(False, "--allow-unlisted", help="Allow lookup of unlisted reports."),
) -> None:
    """Show one report's metadata, zone, and owning guild."""
    code = _report_reference(ctx, code).code
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
    start_time: float | None = _epoch_ms_option("--start-time", help="Report-range start: UNIX epoch ms or ISO-8601 date (UTC)."),
    end_time: float | None = _epoch_ms_option("--end-time", help="Report-range end: UNIX epoch ms or ISO-8601 date (UTC)."),
    zone_id: int | None = typer.Option(None, "--zone-id", help="Optional Warcraft Logs zone filter."),
    game_zone_id: int | None = typer.Option(None, "--game-zone-id", help="Optional game zone filter."),
) -> None:
    """List reports for a guild, optionally narrowed by zone and time window."""
    _require_complete_guild_scope(ctx, guild_region=guild_region, guild_realm=guild_realm, guild_name=guild_name)
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
    start_time: float | None = _epoch_ms_option("--start-time", help="Report-range start: UNIX epoch ms or ISO-8601 date (UTC)."),
    end_time: float | None = _epoch_ms_option("--end-time", help="Report-range end: UNIX epoch ms or ISO-8601 date (UTC)."),
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


def _validate_cohort_scope(ctx: typer.Context, client: WarcraftLogsClient, scope: CrossReportScope) -> CrossReportScope:
    """Reject a partial guild scope or unknown spec, and resolve the zone/boss filter to one encounter id.

    Without this an unknown zone id or misspelled boss name scans an empty cohort and returns
    ``ok: true`` with ``count: 0``, which is indistinguishable from "nobody killed it recently".
    The scan then matches fights on that encounter id alone, so a loose ``--boss-name`` cannot pull
    in another encounter whose name merely contains it.
    """
    _require_complete_guild_scope(
        ctx, guild_region=scope.guild_region, guild_realm=scope.guild_realm, guild_name=scope.guild_name
    )
    # A misspelled spec matches no player and reads as "nobody played it". Only retail is checked:
    # a classic site has specs the retail list lacks (Combat).
    if scope.spec_name and client.site.key == RETAIL_PROFILE.key and not retail_specs_named(scope.spec_name):
        _fail(ctx, "invalid_query", f"Unknown --spec-name {scope.spec_name!r}; {_SPEC_NAME_HINT}")
    encounter = _resolve_encounter(
        ctx,
        client=client,
        zone=client.zone(zone_id=scope.zone_id),
        zone_id=scope.zone_id,
        boss_id=scope.boss_id,
        boss_name=scope.boss_name,
    )
    return replace(scope, boss_id=encounter["id"], boss_name=None)


def _sampled_kill_cohort(ctx: typer.Context, client: WarcraftLogsClient, scope: CrossReportScope) -> dict[str, Any]:
    """Collect the sampled boss-kill cohort and close the client, mapping transport errors to the CLI contract."""
    try:
        return _collect_boss_kill_rows(client, _validate_cohort_scope(ctx, client, scope))
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)
    finally:
        client.close()


def _sampled_comp_cohort(ctx: typer.Context, client: WarcraftLogsClient, scope: CrossReportScope) -> dict[str, Any]:
    """Collect the sampled kill cohort with raid compositions attached, then close the client."""
    try:
        return _collect_comp_sample_rows(client, _validate_cohort_scope(ctx, client, scope))
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
        scope = _validate_cohort_scope(ctx, client, scope)
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
        return _collect_ability_usage_rows(
            client, _validate_cohort_scope(ctx, client, scope), ability_id=ability_id, event_limit=event_limit
        )
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)
    finally:
        client.close()


def _ability_usage_query(scope: CrossReportScope, *, ability_id: int) -> dict[str, Any]:
    """Cross-report scope echo for ability-usage-summary: ability id after the zone, no ``top``."""
    scoped = asdict(scope)
    del scoped["top"]
    return {"zone_id": scoped.pop("zone_id"), "ability_id": ability_id, **scoped}


def _require_complete_guild_scope(
    ctx: typer.Context, *, guild_region: str | None, guild_realm: str | None, guild_name: str | None
) -> None:
    """Warcraft Logs drops a guild filter missing any of its three parts and answers with every guild's reports.

    The region is checked here too, so a typo is a usage error before the first request.
    """
    given = [value for value in (guild_region, guild_realm, guild_name) if value]
    if given and len(given) < 3:
        _fail(ctx, "invalid_query", "Pass --guild-region, --guild-realm and --guild-name together, or none of them.")
    if guild_region:
        _check_region(ctx, guild_region)


def _check_region(ctx: typer.Context, region: str) -> None:
    try:
        validated_region(region)
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)


def _require_boss_scope(ctx: typer.Context, *, boss_id: int | None, boss_name: str | None) -> None:
    if boss_id is None and not boss_name:
        _fail(ctx, "missing_boss", "Provide --boss-id or --boss-name for cross-report boss analytics.")


def _require_spec_scope(ctx: typer.Context, *, spec_name: str | None) -> None:
    if not spec_name:
        _fail(ctx, "missing_spec", "Provide --spec-name to build a spec-filtered participant kill cohort.")


def _fastest_kills_command(kind: str, summary: str) -> Callable[..., None]:
    """Build ``boss-kills`` and ``top-kills``: one sampled fastest-kill cohort under two names and kinds."""

    def command(
        ctx: typer.Context,
        zone_id: int = typer.Option(..., "--zone-id", help="Warcraft Logs zone ID to sample reports from."),
        boss_id: int | None = typer.Option(None, "--boss-id", help="Encounter ID to match."),
        boss_name: str | None = typer.Option(None, "--boss-name", help="Boss name to match within sampled fights."),
        difficulty: int | None = _difficulty_option(),
        spec_name: str | None = typer.Option(
            None,
            "--spec-name",
            help="Optional sampled participant spec filter applied before ranking sampled kills.",
        ),
        kill_time_min: float | None = _float_option("--kill-time-min", help="Optional minimum kill time in seconds."),
        kill_time_max: float | None = _float_option("--kill-time-max", help="Optional maximum kill time in seconds."),
        top: int = typer.Option(10, "--limit", "--top", min=1, max=100, help="Maximum returned kill rows after ranking."),
        report_pages: int = typer.Option(1, "--report-pages", min=1, max=10, help="How many report-list pages to sample."),
        reports_per_page: int = typer.Option(25, "--reports-per-page", min=1, max=100, help="Reports to fetch per sampled page."),
        start_time: float | None = _epoch_ms_option("--start-time", help="Report-range start: UNIX epoch ms or ISO-8601 date (UTC)."),
        end_time: float | None = _epoch_ms_option("--end-time", help="Report-range end: UNIX epoch ms or ISO-8601 date (UTC)."),
        guild_region: str | None = typer.Option(None, "--guild-region", help="Optional guild-region scope for report discovery."),
        guild_realm: str | None = typer.Option(None, "--guild-realm", help="Optional guild-realm scope for report discovery."),
        guild_name: str | None = typer.Option(None, "--guild-name", help="Optional guild-name scope for report discovery."),
    ) -> None:
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
                transport_counts=client.transport_counts,
                kind=kind,
                rows=analytics["rows"],
                sample=analytics["sample"],
                query=asdict(scope),
                top=top,
                root_url=client.site.root_url,
            ),
            client=client,
        )

    command.__doc__ = summary
    return command


app.command("boss-kills")(
    _fastest_kills_command("boss_kills", "Sample recent kills of one boss across reports and summarize them.")
)
app.command("top-kills")(_fastest_kills_command("top_kills", "Sample recent kills of one boss and return the fastest ones."))


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
    difficulty: int | None = _difficulty_option(),
    kill_time_min: float | None = _float_option("--kill-time-min", help="Optional minimum kill time in seconds."),
    kill_time_max: float | None = _float_option("--kill-time-max", help="Optional maximum kill time in seconds."),
    top: int = typer.Option(10, "--limit", "--top", min=1, max=100, help="Maximum returned kill rows after ranking."),
    report_pages: int = typer.Option(1, "--report-pages", min=1, max=10, help="How many report-list pages to sample."),
    reports_per_page: int = typer.Option(25, "--reports-per-page", min=1, max=100, help="Reports to fetch per sampled page."),
    start_time: float | None = _epoch_ms_option("--start-time", help="Report-range start: UNIX epoch ms or ISO-8601 date (UTC)."),
    end_time: float | None = _epoch_ms_option("--end-time", help="Report-range end: UNIX epoch ms or ISO-8601 date (UTC)."),
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
            transport_counts=client.transport_counts,
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
    difficulty: int | None = _difficulty_option(),
    spec_name: str | None = typer.Option(
        None,
        "--spec-name",
        help="Optional sampled participant spec filter applied before aggregation.",
    ),
    kill_time_min: float | None = _float_option("--kill-time-min", help="Optional minimum kill time in seconds."),
    kill_time_max: float | None = _float_option("--kill-time-max", help="Optional maximum kill time in seconds."),
    top: int = typer.Option(10, "--limit", "--top", min=1, max=100, help="Maximum returned spec rows after ranking."),
    report_pages: int = typer.Option(1, "--report-pages", min=1, max=10, help="How many report-list pages to sample."),
    reports_per_page: int = typer.Option(25, "--reports-per-page", min=1, max=100, help="Reports to fetch per sampled page."),
    start_time: float | None = _epoch_ms_option("--start-time", help="Report-range start: UNIX epoch ms or ISO-8601 date (UTC)."),
    end_time: float | None = _epoch_ms_option("--end-time", help="Report-range end: UNIX epoch ms or ISO-8601 date (UTC)."),
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
            transport_counts=client.transport_counts,
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
    difficulty: int | None = _difficulty_option(),
    spec_name: str | None = typer.Option(
        None,
        "--spec-name",
        help="Optional sampled participant spec filter applied before aggregation.",
    ),
    kill_time_min: float | None = _float_option("--kill-time-min", help="Optional minimum kill time in seconds."),
    kill_time_max: float | None = _float_option("--kill-time-max", help="Optional maximum kill time in seconds."),
    preview_limit: int = typer.Option(10, "--preview-limit", min=1, max=100,
                                      help="Maximum sampled kill rows to include in the preview payload."),
    event_limit: int = typer.Option(200, "--event-limit", min=1, max=5000, help="Maximum cast events to request per sampled kill."),
    report_pages: int = typer.Option(1, "--report-pages", min=1, max=10, help="How many report-list pages to sample."),
    reports_per_page: int = typer.Option(25, "--reports-per-page", min=1, max=100, help="Reports to fetch per sampled page."),
    start_time: float | None = _epoch_ms_option("--start-time", help="Report-range start: UNIX epoch ms or ISO-8601 date (UTC)."),
    end_time: float | None = _epoch_ms_option("--end-time", help="Report-range end: UNIX epoch ms or ISO-8601 date (UTC)."),
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
            transport_counts=client.transport_counts,
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
    difficulty: int | None = _difficulty_option(),
    spec_name: str | None = typer.Option(
        None,
        "--spec-name",
        help="Optional sampled participant spec filter applied before aggregation.",
    ),
    kill_time_min: float | None = _float_option("--kill-time-min", help="Optional minimum kill time in seconds."),
    kill_time_max: float | None = _float_option("--kill-time-max", help="Optional maximum kill time in seconds."),
    top: int = typer.Option(10, "--limit", "--top", min=1, max=100, help="Maximum returned sampled kill rows after ranking."),
    report_pages: int = typer.Option(1, "--report-pages", min=1, max=10, help="How many report-list pages to sample."),
    reports_per_page: int = typer.Option(25, "--reports-per-page", min=1, max=100, help="Reports to fetch per sampled page."),
    start_time: float | None = _epoch_ms_option("--start-time", help="Report-range start: UNIX epoch ms or ISO-8601 date (UTC)."),
    end_time: float | None = _epoch_ms_option("--end-time", help="Report-range end: UNIX epoch ms or ISO-8601 date (UTC)."),
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
            transport_counts=client.transport_counts,
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
                encounter_id=_fight_encounter_id(fight),
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
                encounter_id=_fight_encounter_id(fight),
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
    ability_id: float | None = _float_option("--ability-id", help="Optional ability game ID filter."),
    hostility_type: str | None = _HOSTILITY_OPTION,
    limit: int = typer.Option(
        200, "--event-limit", "--limit", min=1, max=10000, help="Maximum cast events to request from Warcraft Logs."
    ),
    preview_limit: int = typer.Option(20, "--preview-limit", min=1, max=200, help="Maximum preview cast rows to return."),
    window_start_ms: float | None = _float_option("--window-start-ms", help="Optional encounter-relative start offset in milliseconds."),
    window_end_ms: float | None = _float_option("--window-end-ms", help="Optional encounter-relative end offset in milliseconds."),
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
        options, query = _encounter_filter_options(
            ctx,
            fight,
            _EncounterFilters(
                ability_id=ability_id,
                data_type="Casts",
                source_id=source_id,
                target_id=target_id,
                hostility_type=hostility_type,
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
        # Unfiltered master data: cast targets are usually NPCs, and a Player-only actor index
        # leaves every boss and add in `by_target` as an anonymous `actor:<id>` placeholder.
        master_report = client.report_master_data(code=ref.code, allow_unlisted=allow_unlisted)
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)
    finally:
        client.close()
    _emit(
        ctx,
        {
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
    ability_id: float | None = _float_option("--ability-id", help="Optional ability game ID filter."),
    hostility_type: str | None = _HOSTILITY_OPTION,
    view_by: str = _graphql_enum_option("--view-by", _VIEW_TYPES, help="Table view grouping.", default="Source"),
    wipe_cutoff: int | None = typer.Option(None, "--wipe-cutoff", help="Optional wipe cutoff."),
    preview_limit: int = typer.Option(20, "--preview-limit", min=1, max=200, help="Maximum preview buff rows to return."),
    window_start_ms: float | None = _float_option("--window-start-ms", help="Optional encounter-relative start offset in milliseconds."),
    window_end_ms: float | None = _float_option("--window-end-ms", help="Optional encounter-relative end offset in milliseconds."),
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
        options, query = _encounter_filter_options(
            ctx,
            fight,
            _EncounterFilters(
                ability_id=ability_id,
                data_type="Buffs",
                source_id=source_id,
                target_id=target_id,
                hostility_type=hostility_type,
                translate=translate,
                view_by=view_by,
                wipe_cutoff=wipe_cutoff,
                window_start_ms=window_start_ms,
                window_end_ms=window_end_ms,
            ),
        )
        table_report = client.report_table(code=ref.code, allow_unlisted=allow_unlisted, options=options)
        master_report = client.report_master_data(code=ref.code, allow_unlisted=allow_unlisted)
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)
    finally:
        client.close()
    _emit(
        ctx,
        {
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
                view_by=view_by,
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
    hostility_type: str | None = _HOSTILITY_OPTION,
    wipe_cutoff: int | None = typer.Option(None, "--wipe-cutoff", help="Optional wipe cutoff."),
    window_start_ms: float | None = _float_option("--window-start-ms", help="Optional encounter-relative start offset in milliseconds."),
    window_end_ms: float | None = _float_option("--window-end-ms", help="Optional encounter-relative end offset in milliseconds."),
    translate: bool | None = typer.Option(None, "--translate/--no-translate", help="Optional translation toggle."),
    include_raw: bool = typer.Option(False, "--include-raw", help=_INCLUDE_RAW_HELP),
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
        options, query = _encounter_filter_options(
            ctx,
            fight,
            _EncounterFilters(
                ability_id=float(ability_id),
                data_type="Buffs",
                source_id=source_id,
                target_id=target_id,
                hostility_type=hostility_type,
                translate=translate,
                view_by="Source",
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
                include_raw=include_raw,
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
            ctx, fight, replace(filters, window_start_ms=left.start_ms, window_end_ms=left.end_ms, window_flag="--left-window")
        )
        right_options, right_query = _encounter_filter_options(
            ctx, fight, replace(filters, window_start_ms=right.start_ms, window_end_ms=right.end_ms, window_flag="--right-window")
        )
        code = scope[0].code
        left_table = client.report_table(code=code, allow_unlisted=allow_unlisted, options=left_options)
        right_table = client.report_table(code=code, allow_unlisted=allow_unlisted, options=right_options)
        master_report = client.report_master_data(code=code, allow_unlisted=allow_unlisted)
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
                include_raw=False,
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
    # Both windows share every filter, so Warcraft Logs groups their rows by the same actor.
    row_actor = str(left.payload["aura_summary"]["row_actor"])
    return {
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
            "row_actor": row_actor,
            "rows": _aura_compare_rows(
                left_rows=_aura_summary_rows(left.payload),
                right_rows=_aura_summary_rows(right.payload),
                actor_field=row_actor,
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
    left_window_start_ms: float | None = _float_option(
        "--left-window-start-ms", help="Encounter-relative start offset for the left comparison window."
    ),
    left_window_end_ms: float | None = _float_option(
        "--left-window-end-ms", help="Encounter-relative end offset for the left comparison window."
    ),
    right_window_start_ms: float | None = _float_option(
        "--right-window-start-ms", help="Encounter-relative start offset for the right comparison window."
    ),
    right_window_end_ms: float | None = _float_option(
        "--right-window-end-ms", help="Encounter-relative end offset for the right comparison window."
    ),
    left_label: str = typer.Option("left", "--left-label", help="Label for the left comparison window."),
    right_label: str = typer.Option("right", "--right-label", help="Label for the right comparison window."),
    source_id: int | None = typer.Option(None, "--source-id", help="Optional source actor filter applied to both windows."),
    target_id: int | None = typer.Option(None, "--target-id", help="Optional target actor filter applied to both windows."),
    hostility_type: str | None = _graphql_enum_option(
        "--hostility-type", _HOSTILITY_TYPES, help="Optional hostility filter applied to both windows."
    ),
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
        hostility_type=hostility_type,
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


def _damage_summary_command(actor_field: Literal["source", "target"]) -> Callable[..., None]:
    """Build the source- and target-grouped damage summary commands, which differ only in grouping."""

    def command(
        ctx: typer.Context,
        reference: str = typer.Argument(..., help="Warcraft Logs report URL or report code, optionally with a #fight=N fragment."),
        fight_id: int | None = typer.Option(
            None, "--fight-id", help="Override or supply a fight ID when the report reference does not include one."),
        source_id: int | None = typer.Option(None, "--source-id", help="Optional source actor filter."),
        target_id: int | None = typer.Option(None, "--target-id", help="Optional target actor filter."),
        ability_id: float | None = _float_option("--ability-id", help="Optional ability game ID filter."),
        hostility_type: str | None = _HOSTILITY_OPTION,
        wipe_cutoff: int | None = typer.Option(None, "--wipe-cutoff", help="Optional wipe cutoff."),
        window_start_ms: float | None = _float_option(
            "--window-start-ms", help="Optional encounter-relative start offset in milliseconds."
        ),
        window_end_ms: float | None = _float_option("--window-end-ms", help="Optional encounter-relative end offset in milliseconds."),
        translate: bool | None = typer.Option(None, "--translate/--no-translate", help="Optional translation toggle."),
        include_raw: bool = typer.Option(False, "--include-raw", help=_INCLUDE_RAW_HELP),
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
            options, query = _encounter_filter_options(
                ctx,
                fight,
                _EncounterFilters(
                    ability_id=ability_id,
                    data_type="DamageDone",
                    source_id=source_id,
                    target_id=target_id,
                    hostility_type=hostility_type,
                    translate=translate,
                    view_by=actor_field.capitalize(),
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
                "kind": f"report_encounter_damage_{actor_field}_summary",
                "query": query,
                **_encounter_summary_payload(
                    ref=ref,
                    report=report,
                    fight=fight,
                    encounter=encounter,
                    finished_report_ttl=_emitted_finished_report_ttl(client),
                    report_ttl=_emitted_report_ttl(client),
                ),
                **_report_encounter_damage_summary_payload(
                    report=report,
                    fight=fight,
                    table_report=table_payload,
                    master_report=master_report,
                    actor_field=actor_field,
                    include_raw=include_raw,
                ),
            },
            client=client,
        )

    command.__doc__ = f"Summarize damage in one report fight by {actor_field} actor."
    return command


app.command("report-encounter-damage-source-summary")(_damage_summary_command("source"))
app.command("report-encounter-damage-target-summary")(_damage_summary_command("target"))


@app.command("report-encounter-damage-breakdown")
def report_encounter_damage_breakdown(
    ctx: typer.Context,
    reference: str = typer.Argument(..., help="Warcraft Logs report URL or report code, optionally with a #fight=N fragment."),
    fight_id: int | None = typer.Option(
        None, "--fight-id", help="Override or supply a fight ID when the report reference does not include one."),
    source_id: int | None = typer.Option(None, "--source-id", help="Optional source actor filter."),
    target_id: int | None = typer.Option(None, "--target-id", help="Optional target actor filter."),
    ability_id: float | None = _float_option("--ability-id", help="Optional ability game ID filter."),
    hostility_type: str | None = _HOSTILITY_OPTION,
    view_by: str = _graphql_enum_option("--view-by", _VIEW_TYPES, help="Table view grouping.", default="Source"),
    wipe_cutoff: int | None = typer.Option(None, "--wipe-cutoff", help="Optional wipe cutoff."),
    window_start_ms: float | None = _float_option("--window-start-ms", help="Optional encounter-relative start offset in milliseconds."),
    window_end_ms: float | None = _float_option("--window-end-ms", help="Optional encounter-relative end offset in milliseconds."),
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
        options, query = _encounter_filter_options(
            ctx,
            fight,
            _EncounterFilters(
                ability_id=ability_id,
                data_type="DamageDone",
                source_id=source_id,
                target_id=target_id,
                hostility_type=hostility_type,
                translate=translate,
                view_by=view_by,
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
    difficulty: int | None = _difficulty_option(),
    spec_name: str | None = typer.Option(
        None,
        "--spec-name",
        help="Optional sampled participant spec filter applied before aggregation.",
    ),
    kill_time_min: float | None = _float_option("--kill-time-min", help="Optional minimum kill time in seconds."),
    kill_time_max: float | None = _float_option("--kill-time-max", help="Optional maximum kill time in seconds."),
    report_pages: int = typer.Option(1, "--report-pages", min=1, max=10, help="How many report-list pages to sample."),
    reports_per_page: int = typer.Option(25, "--reports-per-page", min=1, max=100, help="Reports to fetch per sampled page."),
    start_time: float | None = _epoch_ms_option("--start-time", help="Report-range start: UNIX epoch ms or ISO-8601 date (UTC)."),
    end_time: float | None = _epoch_ms_option("--end-time", help="Report-range end: UNIX epoch ms or ISO-8601 date (UTC)."),
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
            transport_counts=client.transport_counts,
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
    code: str = typer.Argument(..., help="Warcraft Logs report URL or report code."),
    difficulty: int | None = _difficulty_option(),
    allow_unlisted: bool = typer.Option(False, "--allow-unlisted", help="Allow lookup of unlisted reports."),
) -> None:
    """List the fights in one report."""
    code = _report_reference(ctx, code).code
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
    # ``data`` is the GraphQL result's own ``data`` object (``__schema`` under --introspect), built
    # directly so no field of it, whatever its alias, becomes an envelope key or is rewritten. Partial
    # errors go under ``provenance`` for the same reason.
    data = dict(payload or {})
    warnings = data.pop(GRAPHQL_WARNINGS_KEY, None)
    query = {
        "operation_name": request.operation_name,
        "variables": request.variables,
        "endpoint": effective_endpoint,
        "requested_endpoint": request.endpoint,
        "cache_ttl_seconds": request.cache_ttl_seconds,
    }
    provenance = {"graphql_warnings": warnings} if warnings else {}
    emit(
        ctx,
        success_envelope(
            provider="warcraftlogs", command="graphql", kind="graphql", data=data, query=query, provenance=provenance
        ),
    )


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
    start_time: float | None = _float_option("--start-time", help="Inject declared $startTime variables."),
    end_time: float | None = _float_option("--end-time", help="Inject declared $endTime variables."),
    difficulty: int | None = _difficulty_option("Inject declared $difficulty variables."),
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
        fights_end = _require_matching_fight(
            ctx,
            client,
            code=code,
            allow_unlisted=allow_unlisted,
            fight_ids=options.fight_ids,
            encounter_id=options.encounter_id,
            difficulty=options.difficulty,
            start_time=options.start_time,
            end_time=options.end_time,
        )
        # Warcraft Logs answers a start time without an end time with no events, so a next-page
        # request (--start-time only) runs to the end of the selected fights instead.
        if options.start_time is not None and options.end_time is None and fights_end is not None:
            options = replace(options, end_time=fights_end)
        payload = client.report_events(code=code, allow_unlisted=allow_unlisted, options=options)
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)
    finally:
        client.close()
    result_payload = _report_events_payload(payload)
    emitted: dict[str, Any] = {
        "query": asdict(options),
        **result_payload,
    }
    if result_payload.get("events") is None and options.data_type is None:
        emitted["notes"] = [
            "events.data is null. Warcraft Logs requires --data-type "
            "(e.g. casts, damage-done, healing) for non-null event slices."
        ]
    next_page = result_payload.get("next_page_timestamp")
    if next_page is not None:
        slice_end = options.end_time if options.end_time is not None else fights_end
        end_flag = f" --end-time {slice_end:.0f}" if slice_end is not None else ""
        emitted["notes"] = [
            f"This is one page: events stop at timestamp {next_page:.0f}, before the end of the slice. Fetch the next "
            f"page with the same filters plus --start-time {next_page:.0f}{end_flag} before counting events over "
            "the whole slice."
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
        _require_matching_fight(
            ctx,
            client,
            code=code,
            allow_unlisted=allow_unlisted,
            fight_ids=options.fight_ids,
            encounter_id=options.encounter_id,
            difficulty=options.difficulty,
            start_time=options.start_time,
            end_time=options.end_time,
        )
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
            "query": asdict(options),
            **_report_json_payload(payload, field=field),
        },
        client=client,
    )


@app.command("report-events")
def report_events(
    ctx: typer.Context,
    code: str = typer.Argument(..., help="Warcraft Logs report URL or report code."),
    ability_id: float | None = _float_option("--ability-id", help="Optional ability game ID filter."),
    data_type: str | None = _graphql_enum_option(
        "--data-type",
        _EVENT_DATA_TYPES,
        help=(
            "Event data type. Strongly recommended; "
            "without it Warcraft Logs returns events.data: null even on valid scoped slices."
        ),
    ),
    difficulty: int | None = _difficulty_option(),
    encounter_id: int | None = typer.Option(None, "--encounter-id", help="Optional encounter ID filter."),
    end_time: float | None = _float_option("--end-time", help="Optional event-range end, in milliseconds from the report start."),
    fight_id: list[int] | None = FIGHT_ID_OPTION,
    filter_expression: str | None = typer.Option(None, "--filter-expression", help="Optional Warcraft Logs filter expression."),
    hostility_type: str | None = _HOSTILITY_OPTION,
    kill_type: str | None = _KILL_TYPE_OPTION,
    limit: int | None = typer.Option(None, "--limit", min=1, max=10000, help="Optional page event limit."),
    source_id: int | None = typer.Option(None, "--source-id", help="Optional source actor ID filter."),
    start_time: float | None = _float_option("--start-time", help="Optional event-range start, in milliseconds from the report start."),
    target_id: int | None = typer.Option(None, "--target-id", help="Optional target actor ID filter."),
    translate: bool | None = typer.Option(None, "--translate/--no-translate", help="Optional translation toggle."),
    allow_unlisted: bool = typer.Option(False, "--allow-unlisted", help="Allow lookup of unlisted reports."),
) -> None:
    """Return raw report events for one fight (--fight-id) or one explicit --start-time/--end-time window."""
    code, fight_id = _report_code_and_fights(ctx, code, fight_id)
    options = ReportFilterOptions(
        ability_id=ability_id,
        data_type=data_type,
        difficulty=difficulty,
        encounter_id=encounter_id,
        end_time=end_time,
        fight_ids=fight_id,
        filter_expression=filter_expression,
        hostility_type=hostility_type,
        kill_type=kill_type,
        limit=limit,
        source_id=source_id,
        start_time=start_time,
        target_id=target_id,
        translate=translate,
    )
    _require_report_slice(
        ctx,
        command="report-events",
        fight_id=fight_id,
        start_time=start_time,
        end_time=end_time,
    )
    _emit_report_events_slice(ctx, code=code, allow_unlisted=allow_unlisted, options=options)


@app.command("report-table")
def report_table(
    ctx: typer.Context,
    code: str = typer.Argument(..., help="Warcraft Logs report URL or report code."),
    ability_id: float | None = _float_option("--ability-id", help="Optional ability game ID filter."),
    data_type: str | None = _graphql_enum_option("--data-type", _TABLE_DATA_TYPES, help="Optional table data type."),
    difficulty: int | None = _difficulty_option(),
    encounter_id: int | None = typer.Option(None, "--encounter-id", help="Optional encounter ID filter."),
    end_time: float | None = _float_option("--end-time", help="Optional event-range end, in milliseconds from the report start."),
    fight_id: list[int] | None = FIGHT_ID_OPTION,
    filter_expression: str | None = typer.Option(None, "--filter-expression", help="Optional Warcraft Logs filter expression."),
    hostility_type: str | None = _HOSTILITY_OPTION,
    kill_type: str | None = _KILL_TYPE_OPTION,
    source_id: int | None = typer.Option(None, "--source-id", help="Optional source actor ID filter."),
    start_time: float | None = _float_option("--start-time", help="Optional event-range start, in milliseconds from the report start."),
    target_id: int | None = typer.Option(None, "--target-id", help="Optional target actor ID filter."),
    translate: bool | None = typer.Option(None, "--translate/--no-translate", help="Optional translation toggle."),
    view_by: str | None = _VIEW_BY_OPTION,
    wipe_cutoff: int | None = typer.Option(None, "--wipe-cutoff", help="Optional wipe cutoff."),
    allow_unlisted: bool = typer.Option(False, "--allow-unlisted", help="Allow lookup of unlisted reports."),
) -> None:
    """Return a raw report table for one narrowed slice of a report."""
    code, fight_id = _report_code_and_fights(ctx, code, fight_id)
    _emit_report_json_slice(
        ctx,
        code=code,
        allow_unlisted=allow_unlisted,
        options=ReportFilterOptions(
            ability_id=ability_id,
            data_type=data_type,
            difficulty=difficulty,
            encounter_id=encounter_id,
            end_time=end_time,
            fight_ids=fight_id,
            filter_expression=filter_expression,
            hostility_type=hostility_type,
            kill_type=kill_type,
            source_id=source_id,
            start_time=start_time,
            target_id=target_id,
            translate=translate,
            view_by=view_by,
            wipe_cutoff=wipe_cutoff,
        ),
        field="table",
    )


@app.command("report-graph")
def report_graph(
    ctx: typer.Context,
    code: str = typer.Argument(..., help="Warcraft Logs report URL or report code."),
    ability_id: float | None = _float_option("--ability-id", help="Optional ability game ID filter."),
    data_type: str | None = _graphql_enum_option("--data-type", _TABLE_DATA_TYPES, help="Optional graph data type."),
    difficulty: int | None = _difficulty_option(),
    encounter_id: int | None = typer.Option(None, "--encounter-id", help="Optional encounter ID filter."),
    end_time: float | None = _float_option("--end-time", help="Optional event-range end, in milliseconds from the report start."),
    fight_id: list[int] | None = FIGHT_ID_OPTION,
    filter_expression: str | None = typer.Option(None, "--filter-expression", help="Optional Warcraft Logs filter expression."),
    hostility_type: str | None = _HOSTILITY_OPTION,
    kill_type: str | None = _KILL_TYPE_OPTION,
    source_id: int | None = typer.Option(None, "--source-id", help="Optional source actor ID filter."),
    start_time: float | None = _float_option("--start-time", help="Optional event-range start, in milliseconds from the report start."),
    target_id: int | None = typer.Option(None, "--target-id", help="Optional target actor ID filter."),
    translate: bool | None = typer.Option(None, "--translate/--no-translate", help="Optional translation toggle."),
    view_by: str | None = _VIEW_BY_OPTION,
    wipe_cutoff: int | None = typer.Option(None, "--wipe-cutoff", help="Optional wipe cutoff."),
    allow_unlisted: bool = typer.Option(False, "--allow-unlisted", help="Allow lookup of unlisted reports."),
) -> None:
    """Return a raw report graph series for one narrowed slice of a report."""
    code, fight_id = _report_code_and_fights(ctx, code, fight_id)
    _emit_report_json_slice(
        ctx,
        code=code,
        allow_unlisted=allow_unlisted,
        options=ReportFilterOptions(
            ability_id=ability_id,
            data_type=data_type,
            difficulty=difficulty,
            encounter_id=encounter_id,
            end_time=end_time,
            fight_ids=fight_id,
            filter_expression=filter_expression,
            hostility_type=hostility_type,
            kill_type=kill_type,
            source_id=source_id,
            start_time=start_time,
            target_id=target_id,
            translate=translate,
            view_by=view_by,
            wipe_cutoff=wipe_cutoff,
        ),
        field="graph",
    )


@app.command("report-master-data")
def report_master_data(
    ctx: typer.Context,
    code: str = typer.Argument(..., help="Warcraft Logs report URL or report code."),
    actor_type: str | None = typer.Option(None, "--actor-type", help="Optional actor type filter."),
    actor_sub_type: str | None = typer.Option(None, "--actor-sub-type", help="Optional actor sub-type filter."),
    translate: bool | None = typer.Option(None, "--translate/--no-translate", help="Optional translation toggle."),
    allow_unlisted: bool = typer.Option(False, "--allow-unlisted", help="Allow lookup of unlisted reports."),
) -> None:
    """Return a report's master data: actors and abilities."""
    code = _report_reference(ctx, code).code
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
            "query": {"actor_type": actor_type, "actor_sub_type": actor_sub_type, "translate": translate},
            **_report_master_data_payload(payload),
        },
        client=client,
    )


@app.command("report-player-details")
def report_player_details(
    ctx: typer.Context,
    code: str = typer.Argument(..., help="Warcraft Logs report URL or report code."),
    difficulty: int | None = _difficulty_option(),
    encounter_id: int | None = typer.Option(None, "--encounter-id", help="Optional encounter ID filter."),
    end_time: float | None = _float_option("--end-time", help="Optional event-range end, in milliseconds from the report start."),
    fight_id: list[int] | None = FIGHT_ID_OPTION,
    include_combatant_info: bool | None = typer.Option(
        None,
        "--include-combatant-info/--no-include-combatant-info",
        help="Optional combatant detail toggle.",
    ),
    kill_type: str | None = _KILL_TYPE_OPTION,
    start_time: float | None = _float_option("--start-time", help="Optional event-range start, in milliseconds from the report start."),
    translate: bool | None = typer.Option(None, "--translate/--no-translate", help="Optional translation toggle."),
    allow_unlisted: bool = typer.Option(False, "--allow-unlisted", help="Allow lookup of unlisted reports."),
) -> None:
    """Return a report's player details for one fight (--fight-id) or one explicit --start-time/--end-time window."""
    code, fight_id = _report_code_and_fights(ctx, code, fight_id)
    # Warcraft Logs answers a wider playerDetails query with an empty roster plus a GraphQL
    # warning, which reads as "this report has no players". Reject it here like report-events does.
    options = ReportPlayerDetailsOptions(
        difficulty=difficulty,
        encounter_id=encounter_id,
        end_time=end_time,
        fight_ids=fight_id or None,
        include_combatant_info=include_combatant_info,
        kill_type=kill_type,
        start_time=start_time,
        translate=translate,
    )
    query = {
        "difficulty": difficulty,
        "encounter_id": encounter_id,
        "end_time": end_time,
        "fight_ids": fight_id,
        "include_combatant_info": include_combatant_info,
        "kill_type": kill_type,
        "start_time": start_time,
        "translate": translate,
    }
    _require_report_slice(
        ctx,
        command="report-player-details",
        fight_id=fight_id,
        start_time=start_time,
        end_time=end_time,
    )
    client = _client(ctx)
    try:
        _require_matching_fight(
            ctx,
            client,
            code=code,
            allow_unlisted=allow_unlisted,
            fight_ids=fight_id,
            encounter_id=encounter_id,
            difficulty=difficulty,
            start_time=start_time,
            end_time=end_time,
        )
        payload = client.report_player_details(code=code, allow_unlisted=allow_unlisted, options=options)
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)
    finally:
        client.close()
    details = _report_player_details_payload(
        payload,
        report_code=code,
        fight_id=fight_id[0] if fight_id and len(fight_id) == 1 else None,
    )
    # A fight Warcraft Logs actually has always has a roster. An empty one means the slice matched
    # no fight (an unknown fight ID, a mismatched --encounter-id/--difficulty, an empty window),
    # which must not read as "this report has no players".
    if details["player_details"]["counts"]["total"] == 0:
        _fail(
            ctx,
            "not_found",
            f"Warcraft Logs report {code} has no fight matching {_described_slice(query)}, so the roster is empty.",
        )
    _emit(
        ctx,
        {
            "query": query,
            **details,
        },
        client=client,
    )


@app.command("report-rankings")
def report_rankings(
    ctx: typer.Context,
    code: str = typer.Argument(..., help="Warcraft Logs report URL or report code."),
    compare: str | None = _graphql_enum_option("--compare", ("Rankings", "Parses"), help="Optional compare mode."),
    difficulty: int | None = _difficulty_option(),
    encounter_id: int | None = typer.Option(None, "--encounter-id", help="Optional encounter ID filter."),
    fight_id: list[int] | None = FIGHT_ID_OPTION,
    player_metric: str | None = typer.Option(
        None,
        "--player-metric",
        help="Player metric such as dps or hps for every role. Defaults to Warcraft Logs' default "
        "(dps for raid fights, score for Mythic+), with healers on hps in raid fights.",
    ),
    timeframe: str | None = _graphql_enum_option("--timeframe", ("Today", "Historical"), help="Optional ranking timeframe."),
    allow_unlisted: bool = typer.Option(False, "--allow-unlisted", help="Allow lookup of unlisted reports."),
) -> None:
    """Return the rankings attached to one report's fights."""
    code, fight_id = _report_code_and_fights(ctx, code, fight_id)
    options = ReportRankingsOptions(
        compare=compare,
        difficulty=difficulty,
        encounter_id=encounter_id,
        fight_ids=fight_id or None,
        # Warcraft Logs' "default" is dps for a raid fight and score for a Mythic+ run, for every role.
        player_metric=player_metric or "default",
        timeframe=timeframe,
    )
    client = _client(ctx)
    try:
        _require_matching_fight(
            ctx,
            client,
            code=code,
            allow_unlisted=allow_unlisted,
            fight_ids=fight_id,
            encounter_id=encounter_id,
            difficulty=difficulty,
        )
        payload = client.report_rankings(code=code, allow_unlisted=allow_unlisted, options=options)
        healer_metric = options.player_metric
        # That default ranks raid healers on dps, so their hps ranking costs a second request.
        if player_metric is None and _raid_fight_ids(payload):
            healer_metric = "hps"
            healer_payload = client.report_rankings(
                code=code, allow_unlisted=allow_unlisted, options=replace(options, player_metric=healer_metric)
            )
            payload = _with_healers_from(payload, healer_payload)
    except WarcraftLogsClientError as exc:
        _handle_client_error(ctx, exc)
    finally:
        client.close()
    _emit(
        ctx,
        {
            "query": {
                "compare": compare,
                "difficulty": difficulty,
                "encounter_id": encounter_id,
                "fight_ids": fight_id,
                "player_metric": options.player_metric,
                "healer_metric": healer_metric,
                "timeframe": timeframe,
            },
            **_report_rankings_payload(payload),
        },
        client=client,
    )


def run() -> None:
    """Console-script entry point: never let an exception escape as a traceback."""
    guarded_run(app, provider="warcraftlogs")

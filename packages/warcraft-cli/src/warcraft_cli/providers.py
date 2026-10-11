"""The single place the wrapper imports provider packages.

Every ``warcraft`` composite command reaches a provider through this registry: the pure
``PROVIDER`` surfaces for search/resolve/doctor, typed guide exports, and typed report/profile/catalog
operations. The provider Typer app is only for passthrough. No other ``warcraft_cli`` module imports a
provider package.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

import typer
from blizzard_api_cli.main import app as blizzard_app
from blizzard_api_cli.provider import PROVIDER as blizzard_provider
from curseforge_cli.main import app as curseforge_app
from curseforge_cli.provider import PROVIDER as curseforge_provider
from icy_veins_cli import provider as icy_veins_operations
from icy_veins_cli.main import app as icy_veins_app
from icy_veins_cli.provider import PROVIDER as icy_veins_provider
from lorrgs_cli import operations as lorrgs_operations
from lorrgs_cli.main import app as lorrgs_app
from lorrgs_cli.provider import PROVIDER as lorrgs_provider
from lorrgs_cli.search import parse_report_reference as parse_lorrgs_report_reference
from method_cli import provider as method_operations
from method_cli.main import app as method_app
from method_cli.provider import PROVIDER as method_provider
from raidbots_cli.main import app as raidbots_app
from raidbots_cli.provider import PROVIDER as raidbots_provider
from raiderio_cli import profiles as raiderio_operations
from raiderio_cli.main import app as raiderio_app
from raiderio_cli.provider import PROVIDER as raiderio_provider
from simc_cli.build_input import PacketInput
from simc_cli.build_services import (
    DescribeOptions,
    decode_build_payload,
    describe_build_payload,
    identify_build_payload,
    validate_transport_packet_payload,
)
from simc_cli.main import app as simc_app
from simc_cli.provider import PROVIDER as simc_provider
from simc_cli.provider import simc_envelope
from warcraft_core.cache_ledger import cache_ledger, with_cache_provenance
from warcraft_core.cli import command_path_from_args, error_envelope_for
from warcraft_core.envelope import Envelope, error_envelope
from warcraft_core.exit_codes import EXIT_USAGE
from warcraft_core.expansions import list_expansions, resolve_expansion, warcraftlogs_site_for_expansion
from warcraft_core.output import to_json
from warcraft_core.paths import cache_root, config_root, data_root, state_root, worktree_runtime_details
from warcraft_core.provider import ProviderError, ProviderSurface
from warcraft_core.shapes import as_dict
from warcraft_wiki_cli.main import app as warcraft_wiki_app
from warcraft_wiki_cli.provider import PROVIDER as warcraft_wiki_provider
from warcraft_wiki_cli.search import QUERY_COVERAGE_REASONS as WIKI_QUERY_COVERAGE_REASONS
from warcraftlogs_cli import operations as wcl_operations
from warcraftlogs_cli.client import (
    ReportFilterOptions,
    ReportPlayerDetailsOptions,
    WarcraftLogsClient,
    WarcraftLogsClientError,
    resolve_site_profile,
)
from warcraftlogs_cli.errors import client_error_exit_code
from warcraftlogs_cli.main import app as warcraftlogs_app
from warcraftlogs_cli.provider import PROVIDER as warcraftlogs_provider
from wowhead_cli import provider as wowhead_operations
from wowhead_cli.entity_types import RESOLVE_ENTITY_TYPES
from wowhead_cli.main import app as wowhead_app
from wowhead_cli.provider import PROVIDER as wowhead_provider
from wowhead_cli.ranking import STALE_GUIDE_REASON

from warcraft_cli.discovery_scope import filter_candidates
from warcraft_cli.operation_args import OperationArgs
from warcraft_cli.provider_calls import (
    ProviderCalls,
    ProviderFetch,
    ProviderGuideExport,
    ProviderInvoke,
    SimcCall,
    SimcCommand,
    failed_call,
    provider_payload_data,
    shared_failure,
    source_exit_code,
    wrapper_envelope,
)

__all__ = [
    "PROVIDERS",
    "normalize_discovery_scope",
    "discovery_filtered_providers",
    "STALE_GUIDE_REASON",
    "WIKI_QUERY_COVERAGE_REASONS",
    "wrapper_envelope",
    "DescribeOptions",
    "PacketInput",
    "ProviderCalls",
    "ProviderFetch",
    "ProviderGuideExport",
    "ProviderRegistration",
    "ProviderInvoke",
    "SimcCall",
    "source_exit_code",
    "expansion_filtered_providers",
    "expansion_support_snapshot",
    "failed_call",
    "get_provider",
    "global_doctor_payload",
    "invoke_provider_command",
    "list_providers",
    "parse_lorrgs_report_reference",
    "provider_doctor",
    "provider_expansion_args",
    "provider_expansion_exclusion_reason",
    "provider_expansion_options",
    "provider_expansion_support",
    "provider_invoke",
    "provider_guide_export",
    "provider_payload_data",
    "provider_resolve",
    "provider_search",
    "provider_supports_surface",
    "provider_tiers",
    "provider_surface_status",
    "provider_surface_support",
    "resolve_wrapper_expansion_key",
    "shared_failure",
    "simc_call",
    "surface_filtered_providers",
    "warcraftlogs_site_for_expansion",
]

WOWHEAD_SUPPORTED_EXPANSIONS: tuple[str, ...] = tuple(
    expansion.key for expansion in list_expansions() if expansion.wowhead_path_prefix is not None
)
WARCRAFTLOGS_SUPPORTED_EXPANSIONS: tuple[str, ...] = tuple(
    expansion.key for expansion in list_expansions() if expansion.warcraftlogs_site is not None
)


def resolve_wrapper_expansion_key(value: str | None) -> str:
    return resolve_expansion(value).key


# Readiness of one wrapper surface for one provider, as advertised by `warcraft doctor`.
SurfaceStatus = Literal["ready", "ready_explicit_report_only", "not_supported"]
ProviderTier = Literal["core", "supported", "experimental"]


@dataclass(frozen=True, slots=True)
class ProviderRegistration:
    name: str
    command: str
    status: str
    description: str
    auth_required: bool
    expansion_mode: str
    supported_expansions: tuple[str, ...]
    expansion_review_status: str
    expansion_policy_note: str
    # Readiness of the three surfaces the wrapper itself routes through. Everything else a provider
    # can do is reported by that provider's own `doctor`; duplicating it here only lets the two drift.
    wrapper_capabilities: dict[str, SurfaceStatus]
    # Pure in-process surface (search/resolve/doctor); the wrapper never shells out to a binary.
    surface: ProviderSurface
    # Support level agents should expect. See docs/warcraft/README.md.
    tier: ProviderTier
    # Keyword name the surface takes for the wrapper's --expansion value, when it takes one at all.
    expansion_option: str | None
    app: typer.Typer
    # Extra keyword arguments for surface.doctor(), e.g. skipping wowhead's live probes.
    doctor_options: dict[str, Any] = field(default_factory=dict)


PROVIDERS: tuple[ProviderRegistration, ...] = (
    ProviderRegistration(
        name="wowhead",
        command="wowhead",
        status="ready",
        description="Structured Wowhead provider with live search, resolve, and retrieval commands.",
        auth_required=False,
        expansion_mode="profiled",
        supported_expansions=WOWHEAD_SUPPORTED_EXPANSIONS,
        expansion_review_status="reviewed",
        expansion_policy_note="Provider has first-class expansion profiles and real version-specific routing.",
        wrapper_capabilities={
            "doctor": "ready",
            "search": "ready",
            "resolve": "ready",
        },
        surface=wowhead_provider,
        tier="core",
        expansion_option="expansion",
        app=wowhead_app,
        doctor_options={"live": False},
    ),
    ProviderRegistration(
        name="method",
        command="method",
        status="ready",
        description="Method.gg article provider with sitemap-backed search and guide bundle export/query.",
        auth_required=False,
        expansion_mode="fixed",
        supported_expansions=("retail",),
        expansion_review_status="reviewed",
        expansion_policy_note=(
            "Current supported live guide/article families are retail-focused and do not expose reliable non-retail routing."
        ),
        wrapper_capabilities={
            "doctor": "ready",
            "search": "ready",
            "resolve": "ready",
        },
        surface=method_provider,
        tier="supported",
        expansion_option=None,
        app=method_app,
    ),
    ProviderRegistration(
        name="icy-veins",
        command="icy-veins",
        status="ready",
        description="Icy Veins article provider with sitemap-backed search, resolve, and guide bundle export/query.",
        auth_required=False,
        expansion_mode="fixed",
        supported_expansions=("retail",),
        expansion_review_status="reviewed",
        expansion_policy_note=(
            "Current supported guide families are retail-focused and do not provide a reliable wrapper-level non-retail split."
        ),
        wrapper_capabilities={
            "doctor": "ready",
            "search": "ready",
            "resolve": "ready",
        },
        surface=icy_veins_provider,
        tier="supported",
        expansion_option=None,
        app=icy_veins_app,
    ),
    ProviderRegistration(
        name="raiderio",
        command="raiderio",
        status="partial",
        description="Raider.IO API provider with search, resolve, character, guild, and mythic-plus runs lookups.",
        auth_required=False,
        expansion_mode="fixed",
        supported_expansions=("retail",),
        expansion_review_status="reviewed",
        expansion_policy_note=(
            "Current provider surface is retail-first profile and leaderboard data; "
            "non-retail semantics are not part of the supported contract."
        ),
        wrapper_capabilities={
            "doctor": "ready",
            "search": "ready",
            "resolve": "ready",
        },
        surface=raiderio_provider,
        tier="supported",
        expansion_option=None,
        app=raiderio_app,
    ),
    ProviderRegistration(
        name="warcraftlogs",
        command="warcraftlogs",
        status="partial",
        description="Warcraft Logs API provider with explicit report discovery plus guild, character, and report analytics commands.",
        auth_required=True,
        expansion_mode="profiled",
        supported_expansions=WARCRAFTLOGS_SUPPORTED_EXPANSIONS,
        expansion_review_status="reviewed",
        expansion_policy_note=(
            "Warcraft Logs has first-class site profiles: retail routes to www.warcraftlogs.com, "
            "classic-family keys route to classic.warcraftlogs.com, and fresh routes to "
            "fresh.warcraftlogs.com. Discovery remains intentionally limited to explicit report references."
        ),
        wrapper_capabilities={
            "doctor": "ready",
            "search": "ready_explicit_report_only",
            "resolve": "ready_explicit_report_only",
        },
        surface=warcraftlogs_provider,
        tier="core",
        expansion_option="site",
        app=warcraftlogs_app,
        doctor_options={"live": False},
    ),
    ProviderRegistration(
        name="warcraft-wiki",
        command="warcraft-wiki",
        status="ready",
        description="Warcraft Wiki reference provider with MediaWiki-backed search, resolve, article export, and local query.",
        auth_required=False,
        expansion_mode="fixed",
        supported_expansions=("retail",),
        expansion_review_status="reviewed",
        expansion_policy_note=(
            "Reference articles are expansion-agnostic, but wrapper fanout treats warcraft-wiki "
            "as retail-only until explicit classic/fresh routing exists."
        ),
        wrapper_capabilities={
            "doctor": "ready",
            "search": "ready",
            "resolve": "ready",
        },
        surface=warcraft_wiki_provider,
        tier="supported",
        expansion_option=None,
        app=warcraft_wiki_app,
    ),
    ProviderRegistration(
        name="simc",
        command="simc",
        status="partial",
        description="SimulationCraft local provider with repo inspection, build decoding, and local run workflows.",
        auth_required=False,
        expansion_mode="none",
        supported_expansions=(),
        expansion_review_status="deferred",
        expansion_policy_note=(
            "Local repo analysis is versioned differently from wrapper content providers and should not join expansion fanout yet."
        ),
        wrapper_capabilities={
            "doctor": "ready",
            "search": "not_supported",
            "resolve": "not_supported",
        },
        surface=simc_provider,
        tier="core",
        expansion_option=None,
        app=simc_app,
    ),
    ProviderRegistration(
        name="raidbots",
        command="raidbots",
        status="partial",
        description="Raidbots report consumption provider: parse public reports and bridge SimC input to local simc.",
        auth_required=False,
        expansion_mode="fixed",
        supported_expansions=("retail",),
        expansion_review_status="reviewed",
        expansion_policy_note=("Raidbots reports are retail-focused SimulationCraft runs; report consumption stays fixed to retail."),
        wrapper_capabilities={
            "doctor": "ready",
            "search": "not_supported",
            "resolve": "not_supported",
        },
        surface=raidbots_provider,
        tier="experimental",
        expansion_option=None,
        app=raidbots_app,
    ),
    ProviderRegistration(
        name="blizzard-api",
        command="blizzard",
        status="partial",
        description=(
            "Official Blizzard Battle.net WoW API provider: realm, item, PvP seasons/leaderboards, "
            "auctions, commodities, character profiles, PvP ratings, and collections reads."
        ),
        auth_required=True,
        # Blizzard routes by region + namespace class, not the wrapper's expansion axis. Passthrough
        # with --expansion runs with an advisory; see docs/architecture/EXPANSION_FILTERING.md.
        expansion_mode="none",
        supported_expansions=(),
        expansion_review_status="reviewed",
        expansion_policy_note=(
            "Region- and namespace-aware routing is honored by the Game Data/Profile commands, but "
            "Blizzard's region/namespace model is not the wrapper's expansion axis, so this provider "
            "stays out of expansion fanout (expansion_mode='none')."
        ),
        wrapper_capabilities={
            "doctor": "ready",
            "search": "not_supported",
            "resolve": "not_supported",
        },
        surface=blizzard_provider,
        tier="supported",
        expansion_option=None,
        app=blizzard_app,
    ),
    ProviderRegistration(
        name="curseforge",
        command="curseforge",
        status="partial",
        description="CurseForge addon provider: doctor + addon lookup (metadata, latest files, changelog) over the public CurseForge API.",
        auth_required=True,
        # Addon game-version compatibility lives in file records, not the wrapper's expansion axis.
        # Passthrough with --expansion runs with an advisory; see docs/architecture/EXPANSION_FILTERING.md.
        expansion_mode="none",
        supported_expansions=(),
        expansion_review_status="reviewed",
        expansion_policy_note=(
            "CurseForge addon metadata is not tied to the wrapper's expansion axis (game-version "
            "compatibility lives inside addon file records), so this provider stays out of expansion "
            "fanout (expansion_mode='none')."
        ),
        wrapper_capabilities={
            "doctor": "ready",
            "search": "not_supported",
            "resolve": "not_supported",
        },
        surface=curseforge_provider,
        tier="experimental",
        expansion_option=None,
        app=curseforge_app,
    ),
    ProviderRegistration(
        name="lorrgs",
        command="lorrgs",
        status="partial",
        description=(
            "Lorrgs public API provider: cooldown timeline rankings by spec/boss, composition rankings, "
            "report overview handoffs, and static class/spec/boss/spell metadata."
        ),
        auth_required=False,
        expansion_mode="fixed",
        supported_expansions=("retail",),
        expansion_review_status="reviewed",
        expansion_policy_note=(
            "Lorrgs data is current retail Warcraft Logs-derived raid ranking data and does not expose "
            "a classic/fresh selector, so wrapper expansion fanout treats it as retail-only."
        ),
        wrapper_capabilities={
            "doctor": "ready",
            "search": "ready",
            "resolve": "ready",
        },
        surface=lorrgs_provider,
        tier="supported",
        expansion_option=None,
        app=lorrgs_app,
    ),
)


def list_providers() -> tuple[ProviderRegistration, ...]:
    return PROVIDERS


def get_provider(provider: str) -> ProviderRegistration:
    for registration in PROVIDERS:
        if registration.name == provider:
            return registration
    raise ValueError(f"Unknown provider: {provider}")


def provider_expansion_support(registration: ProviderRegistration, *, requested_expansion: str | None = None) -> dict[str, Any]:
    allowed = provider_supports_expansion(registration, requested_expansion=requested_expansion)
    payload: dict[str, Any] = {
        "mode": registration.expansion_mode,
        "supported_expansions": list(registration.supported_expansions),
        "requested_expansion": requested_expansion,
        "allowed": allowed,
        "review_status": registration.expansion_review_status,
        "policy_note": registration.expansion_policy_note,
    }
    reason = provider_expansion_exclusion_reason(registration, requested_expansion=requested_expansion)
    if reason is not None:
        payload["exclusion_reason"] = reason
    return payload


def provider_supports_expansion(registration: ProviderRegistration, *, requested_expansion: str | None) -> bool:
    if requested_expansion is None:
        return True
    if registration.expansion_mode in {"profiled", "fixed"}:
        return requested_expansion in registration.supported_expansions
    return False


def provider_expansion_exclusion_reason(
    registration: ProviderRegistration,
    *,
    requested_expansion: str | None,
) -> str | None:
    if requested_expansion is None or provider_supports_expansion(registration, requested_expansion=requested_expansion):
        return None
    if registration.expansion_mode == "fixed":
        return "provider_fixed_to_other_expansion"
    if registration.expansion_mode == "profiled":
        return "provider_does_not_support_requested_expansion"
    return "provider_has_no_expansion_support"


def expansion_filtered_providers(
    *,
    requested_expansion: str | None,
) -> tuple[list[ProviderRegistration], list[dict[str, Any]]]:
    included: list[ProviderRegistration] = []
    excluded: list[dict[str, Any]] = []
    for registration in PROVIDERS:
        if provider_supports_expansion(registration, requested_expansion=requested_expansion):
            included.append(registration)
            continue
        excluded.append(
            {
                "provider": registration.name,
                "command": registration.command,
                # Same top-level `reason` key the surface-readiness exclusions carry, so an agent can
                # read one field regardless of why a provider was left out of the fanout.
                "reason": provider_expansion_exclusion_reason(
                    registration,
                    requested_expansion=requested_expansion,
                ),
                "expansion_support": provider_expansion_support(
                    registration,
                    requested_expansion=requested_expansion,
                ),
            }
        )
    return included, excluded


def expansion_support_snapshot(*, requested_expansion: str | None) -> list[dict[str, Any]]:
    return [
        {
            "provider": registration.name,
            "command": registration.command,
            "expansion_support": provider_expansion_support(
                registration,
                requested_expansion=requested_expansion,
            ),
        }
        for registration in PROVIDERS
    ]


def provider_surface_status(registration: ProviderRegistration, surface: str) -> str:
    return registration.wrapper_capabilities.get(surface, "unsupported")


def provider_supports_surface(registration: ProviderRegistration, surface: str) -> bool:
    return provider_surface_status(registration, surface).startswith("ready")


def provider_surface_support(registration: ProviderRegistration, surface: str) -> dict[str, Any]:
    status = provider_surface_status(registration, surface)
    return {
        "surface": surface,
        "status": status,
        "ready": provider_supports_surface(registration, surface),
    }


def surface_filtered_providers(
    registrations: list[ProviderRegistration],
    *,
    surface: str,
    requested_expansion: str | None,
) -> tuple[list[ProviderRegistration], list[dict[str, Any]]]:
    included: list[ProviderRegistration] = []
    excluded: list[dict[str, Any]] = []
    for registration in registrations:
        if provider_supports_surface(registration, surface):
            included.append(registration)
            continue
        excluded.append(
            {
                "provider": registration.name,
                "command": registration.command,
                "reason": "provider_surface_not_ready",
                "surface_support": provider_surface_support(registration, surface),
                "expansion_support": provider_expansion_support(
                    registration,
                    requested_expansion=requested_expansion,
                ),
            }
        )
    return included, excluded


def provider_expansion_options(registration: ProviderRegistration, expansion: str | None) -> dict[str, str]:
    """Keyword arguments that pin a pure surface call to the wrapper's requested expansion."""
    option = registration.expansion_option
    if expansion is None or option is None:
        return {}
    if option == "site":
        return {option: warcraftlogs_site_for_expansion(expansion)}
    return {option: expansion}


def provider_expansion_args(registration: ProviderRegistration, expansion: str | None) -> list[str]:
    """The same pin as ``provider_expansion_options``, in provider CLI flag form for passthrough."""
    return [arg for key, value in provider_expansion_options(registration, expansion).items() for arg in (f"--{key}", value)]


def _unsupported_expansion_result(
    registration: ProviderRegistration, expansion: str | None, *, command: str, query: str | None = None
) -> dict[str, Any] | None:
    """Early return for a provider that cannot honour the requested expansion; ``None`` means proceed."""
    if (
        expansion is None
        or registration.expansion_mode == "none"
        or provider_expansion_exclusion_reason(registration, requested_expansion=expansion) is None
    ):
        return None
    envelope = error_envelope(
        provider=registration.name,
        command=command,
        code="unsupported_provider_expansion",
        message=f"Provider {registration.name!r} does not support wrapper expansion {expansion!r}.",
        query=query,
        details={
            "requested_expansion": expansion,
            "expansion_support": provider_expansion_support(registration, requested_expansion=expansion),
        },
    )
    return {"provider": registration.name, "exit_code": EXIT_USAGE, "payload": dict(envelope)}


def _call_surface(provider: str, command: str, call: Callable[[], Mapping[str, Any]], *, query: Any = None) -> tuple[int, dict[str, Any]]:
    """Run one pure surface call, returning ``(exit_code, envelope)`` and never raising.

    A raised failure echoes ``query``, the input the wrapper handed the surface. The call runs in
    its own cache ledger, so a success envelope carries this provider's own ``provenance.cache``
    while the wrapper's ledger still receives the counts.
    """
    with cache_ledger() as ledger:
        try:
            envelope = call()
        except Exception as exc:
            failure, exit_code = error_envelope_for(provider, command, exc)
            return exit_code, {**failure, "query": query}
    # Surfaces raise on failure, so an envelope that comes back is a success.
    return 0, with_cache_provenance(envelope, ledger)


DISCOVERY_ENTITY_TYPES: dict[str, frozenset[str]] = {
    "wowhead": RESOLVE_ENTITY_TYPES,
    "method": frozenset({"guide"}),
    "icy-veins": frozenset({"guide"}),
    "warcraft-wiki": frozenset({"article"}),
    "raiderio": frozenset({"character", "guild"}),
    "warcraftlogs": frozenset({"report", "report_encounter"}),
    "lorrgs": frozenset({"spec", "boss", "spec_ranking", "comp_ranking", "report_overview", "user_report_fights"}),
}


def normalize_discovery_scope(providers: list[str] | None, entity_types: list[str] | None) -> tuple[tuple[str, ...], tuple[str, ...]]:
    names = tuple(dict.fromkeys(providers or []))
    kinds = tuple(dict.fromkeys(entity_types or []))
    unknown = set(names) - {row.name for row in PROVIDERS}
    invalid = set(kinds) - set().union(*DISCOVERY_ENTITY_TYPES.values())
    if unknown or invalid:
        raise ProviderError(
            "invalid_query",
            "Unknown discovery scope.",
            details={
                "unknown_providers": sorted(unknown),
                "unknown_entity_types": sorted(invalid),
                "providers": [row.name for row in PROVIDERS],
                "entity_types": sorted(set().union(*DISCOVERY_ENTITY_TYPES.values())),
            },
        )
    return names, kinds


def discovery_filtered_providers(
    registrations: list[ProviderRegistration], *, providers: tuple[str, ...], entity_types: tuple[str, ...]
) -> tuple[list[ProviderRegistration], list[dict[str, Any]]]:
    included = []
    excluded = []
    for registration in registrations:
        reason = None
        if providers and registration.name not in providers:
            reason = "provider_not_selected"
        elif entity_types and not set(entity_types) & DISCOVERY_ENTITY_TYPES.get(registration.name, frozenset()):
            reason = "provider_has_no_requested_entity_type"
        if reason is None:
            included.append(registration)
        else:
            excluded.append({"provider": registration.name, "command": registration.command, "reason": reason})
    return included, excluded


def _discovery_options(registration: ProviderRegistration, expansion: str | None, entity_types: tuple[str, ...]) -> dict[str, Any]:
    options: dict[str, Any] = dict(provider_expansion_options(registration, expansion))
    if entity_types and registration.name == "wowhead":
        options["entity_types"] = tuple(kind for kind in entity_types if kind in RESOLVE_ENTITY_TYPES)
    elif entity_types and registration.name == "raiderio":
        selected = set(entity_types) & DISCOVERY_ENTITY_TYPES["raiderio"]
        options["kind"] = next(iter(selected)) if len(selected) == 1 else "all"
    return options


def _scope_result(payload: dict[str, Any], *, provider: str, entity_types: tuple[str, ...], surface: str, limit: int) -> dict[str, Any]:
    if not entity_types or payload.get("ok") is False:
        return payload
    if provider in {"wowhead", "raiderio"}:
        data = as_dict(payload.get("data"))
        return {
            **payload,
            "data": {**data, "entity_scope": {"mode": "native", "entity_types": list(entity_types), "candidate_limit": limit}},
        }
    return filter_candidates(payload, entity_types=entity_types, surface=surface, limit=limit)


def provider_search(
    provider: str, query: str, *, limit: int = 5, expansion: str | None = None, entity_types: tuple[str, ...] = ()
) -> dict[str, Any]:
    """One provider's search. ``entity_types`` restricts the kinds a provider that can filter returns."""
    registration = get_provider(provider)
    unsupported = _unsupported_expansion_result(registration, expansion, command="search", query=query)
    if unsupported is not None:
        return unsupported
    options = _discovery_options(registration, expansion, entity_types)
    code, payload = _call_surface(provider, "search", lambda: registration.surface.search(query, limit=limit, **options), query=query)
    return {
        "provider": provider,
        "exit_code": code,
        "payload": _scope_result(payload, provider=provider, entity_types=entity_types, surface="search", limit=limit),
    }


def provider_resolve(
    provider: str, query: str, *, limit: int = 5, expansion: str | None = None, entity_types: tuple[str, ...] = ()
) -> dict[str, Any]:
    """One provider's resolve. ``entity_types`` restricts the kinds a provider that can filter returns."""
    registration = get_provider(provider)
    unsupported = _unsupported_expansion_result(registration, expansion, command="resolve", query=query)
    if unsupported is not None:
        return unsupported
    options = _discovery_options(registration, expansion, entity_types)
    code, payload = _call_surface(provider, "resolve", lambda: registration.surface.resolve(query, limit=limit, **options), query=query)
    return {
        "provider": provider,
        "exit_code": code,
        "payload": _scope_result(payload, provider=provider, entity_types=entity_types, surface="resolve", limit=limit),
    }


def parse_json_object(text: str) -> dict[str, Any] | None:
    """``text`` as one JSON object, or ``None`` when it is empty, not JSON, or not an object."""
    raw = text.strip()
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def invoke_provider_command(app: typer.Typer, *, args: list[str], prog_name: str) -> None:
    """Run a provider Typer app with its output streaming straight through to the caller's stdout."""
    command = typer.main.get_command(app)
    try:
        # standalone_mode=False makes click *return* the exit code for typer.Exit/click.Exit
        # rather than calling sys.exit, so a provider's `raise typer.Exit(1)` would otherwise
        # be silently swallowed to exit 0. Re-raise it so passthrough propagates failures.
        exit_code = command.main(args=args, prog_name=prog_name, standalone_mode=False)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
        raise typer.Exit(code) from exc
    except typer.Abort:
        raise
    except Exception as exc:
        # A usage error escapes non-standalone mode; label it as the provider's own binary does.
        envelope, code = error_envelope_for(prog_name, command_path_from_args(app, args), exc)
        typer.echo(to_json(envelope, pretty=False), err=True)
        raise typer.Exit(code) from exc
    if isinstance(exit_code, int) and exit_code != 0:
        raise typer.Exit(exit_code)


def provider_guide_export(provider: str, guide_ref: str, *, out: Path, expansion: str | None = None) -> dict[str, Any]:
    """Export one provider guide in-process through a typed, output-free operation."""
    registration = get_provider(provider)
    unsupported = _unsupported_expansion_result(registration, expansion, command="guide-export", query=guide_ref)
    if unsupported is not None:
        return unsupported

    def export() -> Envelope:
        if provider == "method":
            return method_operations.guide_export(guide_ref, out=out)
        if provider == "icy-veins":
            return icy_veins_operations.guide_export(guide_ref, out=out)
        if provider == "wowhead":
            return wowhead_operations.guide_export(guide_ref, out=out, expansion=expansion)
        raise ProviderError("invalid_argument", f"{provider} does not export guide bundles.")

    code, payload = _call_surface(provider, "guide-export", export, query={"guide_ref": guide_ref, "out": str(out)})
    return {"provider": provider, "exit_code": code, "payload": payload}


def _lorrgs_operation(command: str, args: list[str]) -> Envelope:
    if command == "bosses":
        OperationArgs.parse(args, count=0)
        return lorrgs_operations.bosses()
    if command in {"spec-spells", "boss-spells"}:
        parsed = OperationArgs.parse(args, count=1)
        return (
            lorrgs_operations.spec_spells(parsed.positionals[0])
            if command == "spec-spells"
            else lorrgs_operations.boss_spells(parsed.positionals[0])
        )
    if command == "spec-ranking":
        parsed = OperationArgs.parse(args, count=2, values=frozenset({"--difficulty", "--metric"}))
        return lorrgs_operations.spec_ranking(
            *parsed.positionals, difficulty=parsed.text("--difficulty") or "mythic", metric=parsed.text("--metric")
        )
    if command == "user-report-fights":
        parsed = OperationArgs.parse(
            args, count=1, values=frozenset({"--fight", "--fight-id", "--player", "--type"}), repeated=frozenset({"--fight-id"})
        )
        return lorrgs_operations.user_report_fights(
            parsed.positionals[0],
            fight=parsed.text("--fight"),
            fight_ids=parsed.integers("--fight-id"),
            player=parsed.text("--player"),
            data_type=parsed.text("--type"),
        )
    raise ProviderError("invalid_argument", f"Lorrgs operation {command!r} is not used by wrapper composites.")


def _wcl_report_operation(client: WarcraftLogsClient, command: str, args: list[str], *, endpoint: str) -> Envelope:
    if command == "report-fights":
        parsed = OperationArgs.parse(args, count=1, switches=frozenset({"--allow-unlisted"}))
        return wcl_operations.report_fights(client, reference=parsed.positionals[0], allow_unlisted="--allow-unlisted" in parsed.switches)
    if command == "report-player-details":
        parsed = OperationArgs.parse(
            args, count=1, values=frozenset({"--fight-id"}), repeated=frozenset({"--fight-id"}), switches=frozenset({"--allow-unlisted"})
        )
        return wcl_operations.report_player_details(
            client,
            reference=parsed.positionals[0],
            options=ReportPlayerDetailsOptions(fight_ids=parsed.integers("--fight-id")),
            allow_unlisted="--allow-unlisted" in parsed.switches,
        )
    if command == "report-player-talents":
        parsed = OperationArgs.parse(
            args, count=1, values=frozenset({"--actor-id", "--fight-id"}), switches=frozenset({"--allow-unlisted"})
        )
        actor = parsed.integer("--actor-id")
        if actor is None:
            raise ProviderError("invalid_argument", "Actor ID is required.")
        return wcl_operations.report_player_talents(
            client,
            reference=parsed.positionals[0],
            actor_id=actor,
            fight_id=parsed.integer("--fight-id"),
            allow_unlisted="--allow-unlisted" in parsed.switches,
        )
    if command == "report-events":
        parsed = OperationArgs.parse(
            args,
            count=1,
            values=frozenset({"--fight-id", "--source-id", "--data-type", "--limit"}),
            repeated=frozenset({"--fight-id"}),
            switches=frozenset({"--allow-unlisted"}),
        )
        options = ReportFilterOptions(
            fight_ids=parsed.integers("--fight-id"),
            source_id=parsed.integer("--source-id"),
            data_type=(parsed.text("--data-type") or "casts").title(),
            limit=parsed.integer("--limit", 300),
        )
        return wcl_operations.report_events(
            client, reference=parsed.positionals[0], options=options, allow_unlisted="--allow-unlisted" in parsed.switches
        )
    if command == "graphql":
        parsed = OperationArgs.parse(
            args,
            count=0,
            values=frozenset({"--query", "--report-code", "--variables-json", "--fight-id"}),
            repeated=frozenset({"--fight-id"}),
            switches=frozenset({"--allow-unlisted"}),
        )
        query = parsed.text("--query")
        if not query:
            raise ProviderError("invalid_argument", "GraphQL query is required.")
        try:
            variables = json.loads(parsed.text("--variables-json") or "{}")
        except json.JSONDecodeError as exc:
            raise ProviderError("invalid_argument", "GraphQL variables must be JSON.") from exc
        if not isinstance(variables, dict):
            raise ProviderError("invalid_argument", "GraphQL variables must be an object.")
        variables = _graphql_fight_variables(query, variables, parsed.integers("--fight-id"))
        return wcl_operations.raw_graphql(
            client,
            query=query,
            variables=variables,
            report_code=parsed.text("--report-code"),
            allow_unlisted="--allow-unlisted" in parsed.switches,
            endpoint=endpoint,
        )
    raise ProviderError("invalid_argument", f"WCL operation {command!r} is not used by wrapper composites.")


def _graphql_fight_variables(query: str, variables: dict[str, Any], fight_ids: list[int] | None) -> dict[str, Any]:
    declared = set(re.findall(r"\$([A-Za-z_][A-Za-z0-9_]*)\s*:", query))
    merged = dict(variables)
    if fight_ids:
        if "fightIDs" in declared and "fightIDs" not in merged:
            merged["fightIDs"] = fight_ids
        elif "fightID" in declared and "fightID" not in merged:
            merged["fightID"] = fight_ids[0]
    return merged


def _wcl_operation(command: str, args: list[str], *, expansion: str | None, endpoint: str) -> Envelope:
    try:
        with WarcraftLogsClient(
            site=resolve_site_profile(warcraftlogs_site_for_expansion(expansion) if expansion else None), endpoint=endpoint
        ) as client:
            return _wcl_report_operation(client, command, args, endpoint=endpoint)
    except WarcraftLogsClientError as exc:
        raise ProviderError(exc.code, exc.message, exit_code=client_error_exit_code(exc.code)) from exc


def _composite_operation(provider: str, command: str, args: list[str], *, expansion: str | None, warcraftlogs_endpoint: str) -> Envelope:
    if provider == "lorrgs":
        return _lorrgs_operation(command, args)
    if provider == "warcraftlogs":
        return _wcl_operation(command, args, expansion=expansion, endpoint=warcraftlogs_endpoint)
    if provider == "raiderio" and command in {"character", "guild"}:
        parsed = OperationArgs.parse(args, count=3)
        return (
            raiderio_operations.character_profile(*parsed.positionals)
            if command == "character"
            else raiderio_operations.guild_profile(*parsed.positionals)
        )
    if provider == "wowhead" and command == "talent-calc-packet":
        parsed = OperationArgs.parse(args, count=1, values=frozenset({"--listed-build-limit"}))
        return wowhead_operations.talent_calc_packet(
            parsed.positionals[0], listed_build_limit=parsed.integer("--listed-build-limit", 20), expansion=expansion
        )
    raise ProviderError("invalid_argument", f"No typed wrapper operation for {provider} {command}; use provider passthrough.")


def provider_invoke(
    provider: str, args: list[str], *, expansion: str | None = None, warcraftlogs_endpoint: str = "client"
) -> dict[str, Any]:
    """Adapt the existing composite seam to a finite family of typed, output-free operations.

    Ordinary provider passthrough is handled separately and retains its full CLI flag surface.
    """
    if len(args) == 4 and args[0] == "guide-export" and args[2] == "--out":
        return provider_guide_export(provider, args[1], out=Path(args[3]), expansion=expansion)
    registration = get_provider(provider)
    command = args[0] if args else ""
    unsupported = _unsupported_expansion_result(registration, expansion, command=command)
    if unsupported is not None:
        return unsupported
    code, payload = _call_surface(
        provider,
        command,
        lambda: _composite_operation(provider, command, args[1:], expansion=expansion, warcraftlogs_endpoint=warcraftlogs_endpoint),
        query={"args": args},
    )
    return {"provider": provider, "exit_code": code, "payload": payload}


def _simc_payload(command: SimcCommand, build: PacketInput | str, describe: DescribeOptions | None) -> dict[str, Any]:
    if command == "identify-build":
        return identify_build_payload(build)
    if command == "decode-build":
        return decode_build_payload(build)
    if command == "describe-build":
        return describe_build_payload(build, describe or DescribeOptions())
    if isinstance(build, str):
        raise TypeError("simc validate-talent-transport reads a talent transport packet, not build text")
    return validate_transport_packet_payload(build)


def simc_call(command: SimcCommand, build: PacketInput | str, *, describe: DescribeOptions | None = None) -> dict[str, Any]:
    """Run one simc build command in-process on a build held in memory: a packet, or build text.

    Returns ``provider_invoke``'s ``{provider, exit_code, payload}`` with the envelope the simc command
    prints. Nothing is written to disk, so the payload cites ``build.path`` for a packet and no file
    when that is ``None``. A failure echoes these inputs as its ``query``.
    """
    query: dict[str, Any] = {"build_packet": build.path} if isinstance(build, PacketInput) else {"build_text": build}
    if describe is not None:
        query.update(asdict(describe))

    def run() -> Envelope:
        return simc_envelope(command, _simc_payload(command, build, describe))

    code, payload = _call_surface("simc", command, run, query=query)
    return {"provider": "simc", "exit_code": code, "payload": payload}


def _doctor_cache_error(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    """A provider doctor's cache failure as ``{code, message}``, or ``None``.

    A broken cache config or an unreachable Redis fails every read the provider makes. Some doctors
    report it in ``data.cache.error`` (a Redis ping's message, or an ``invalid_cache_config`` object);
    others fail outright with ``invalid_cache_config`` as their envelope error.
    """
    raw = as_dict(as_dict(payload.get("data")).get("cache")).get("error")
    if isinstance(raw, str) and raw:
        return {"code": "cache_unavailable", "message": raw}
    error = as_dict(raw) or as_dict(payload.get("error"))
    if raw or error.get("code") == "invalid_cache_config":
        return {"code": error.get("code"), "message": error.get("message")}
    return None


def provider_doctor(provider: str, *, requested_expansion: str | None = None, warcraftlogs_endpoint: str = "client") -> dict[str, Any]:
    registration = get_provider(provider)
    expansion_options: dict[str, str] = {"endpoint": warcraftlogs_endpoint} if provider == "warcraftlogs" else {}
    if provider_expansion_exclusion_reason(registration, requested_expansion=requested_expansion) is None:
        expansion_options.update(provider_expansion_options(registration, requested_expansion))
    code, payload = _call_surface(
        provider, "doctor", lambda: registration.surface.doctor(**registration.doctor_options, **expansion_options)
    )
    data = as_dict(payload.get("data"))
    raw_auth = data.get("auth")
    auth_details = raw_auth if isinstance(raw_auth, dict) else None
    cache_error = _doctor_cache_error(payload)
    return {
        "provider": registration.name,
        "status": "error" if code != 0 else "degraded" if cache_error else registration.status,
        "cache_error": cache_error,
        "command": registration.command,
        "tier": registration.tier,
        # The provider package imported, so the surface is always reachable in-process.
        "installed": True,
        "auth": auth_details
        or {
            "required": registration.auth_required,
            "configured": None,
        },
        "expansion_support": provider_expansion_support(registration, requested_expansion=requested_expansion),
        "wrapper_surfaces": {surface: provider_surface_support(registration, surface) for surface in ("doctor", "search", "resolve")},
        "details": payload,
    }


def provider_tiers() -> dict[str, list[str]]:
    """Provider names grouped by support tier, in registry order."""
    tiers: dict[str, list[str]] = {"core": [], "supported": [], "experimental": []}
    for registration in PROVIDERS:
        tiers[registration.tier].append(registration.name)
    return tiers


def global_doctor_payload(*, requested_expansion: str | None = None, warcraftlogs_endpoint: str = "client") -> dict[str, Any]:
    included, excluded = expansion_filtered_providers(requested_expansion=requested_expansion)
    return {
        "wrapper": {
            "provider_count": len(PROVIDERS),
            "tiers": provider_tiers(),
            "requested_expansion": requested_expansion,
            "expansion_filter_active": requested_expansion is not None,
            "included_provider_count": len(included),
            "excluded_provider_count": len(excluded),
        },
        "paths": {
            "config_root": str(config_root()),
            "data_root": str(data_root()),
            "cache_root": str(cache_root()),
            "state_root": str(state_root()),
            "worktree_runtime": worktree_runtime_details(),
        },
        "providers": [
            provider_doctor(provider.name, requested_expansion=requested_expansion, warcraftlogs_endpoint=warcraftlogs_endpoint)
            for provider in PROVIDERS
        ],
        "included_providers": [provider.name for provider in included],
        "excluded_providers": excluded,
    }

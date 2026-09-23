"""The single place the wrapper imports provider packages.

Every ``warcraft`` composite command reaches a provider through this registry: the pure
``PROVIDER`` surfaces for search/resolve/doctor, and the provider Typer app for passthrough and
for the commands that have no surface method yet. No other ``warcraft_cli`` module imports a
provider package.
"""

from __future__ import annotations

import io
import json
from collections.abc import Callable, Mapping
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass, field
from typing import Any, Literal

import click
import typer
from blizzard_api_cli.main import app as blizzard_app
from blizzard_api_cli.provider import PROVIDER as blizzard_provider
from curseforge_cli.main import app as curseforge_app
from curseforge_cli.provider import PROVIDER as curseforge_provider
from icy_veins_cli.main import app as icy_veins_app
from icy_veins_cli.provider import PROVIDER as icy_veins_provider
from lorrgs_cli.main import app as lorrgs_app
from lorrgs_cli.provider import PROVIDER as lorrgs_provider
from lorrgs_cli.search import parse_report_reference as parse_lorrgs_report_reference
from method_cli.main import app as method_app
from method_cli.provider import PROVIDER as method_provider
from raidbots_cli.main import app as raidbots_app
from raidbots_cli.provider import PROVIDER as raidbots_provider
from raiderio_cli.main import app as raiderio_app
from raiderio_cli.provider import PROVIDER as raiderio_provider
from simc_cli.main import app as simc_app
from simc_cli.provider import PROVIDER as simc_provider
from warcraft_core.cli import error_envelope_for
from warcraft_core.envelope import ENVELOPE_KEYS, SCHEMA_VERSION, error_envelope
from warcraft_core.exit_codes import EXIT_GENERIC, EXIT_USAGE, exit_code_for
from warcraft_core.expansions import list_expansions, resolve_expansion, warcraftlogs_site_for_expansion
from warcraft_core.paths import cache_root, config_root, data_root, state_root, worktree_runtime_details
from warcraft_core.provider import ProviderSurface
from warcraft_core.shapes import as_dict
from warcraft_wiki_cli.main import app as warcraft_wiki_app
from warcraft_wiki_cli.provider import PROVIDER as warcraft_wiki_provider
from warcraftlogs_cli.main import app as warcraftlogs_app
from warcraftlogs_cli.provider import PROVIDER as warcraftlogs_provider
from wowhead_cli.main import app as wowhead_app
from wowhead_cli.provider import PROVIDER as wowhead_provider
from wowhead_cli.ranking import STALE_GUIDE_REASON

__all__ = [
    "PROVIDERS",
    "STALE_GUIDE_REASON",
    "wrapper_envelope",
    "ProviderRegistration",
    "expansion_filtered_providers",
    "expansion_support_snapshot",
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
    "provider_payload_data",
    "provider_resolve",
    "provider_search",
    "provider_supports_surface",
    "provider_tiers",
    "provider_surface_status",
    "provider_surface_support",
    "resolve_wrapper_expansion_key",
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
SurfaceStatus = Literal["ready", "ready_explicit_report_only", "coming_soon", "not_supported"]
ProviderTier = Literal["core", "supported", "experimental"]


@dataclass(frozen=True, slots=True)
class ProviderRegistration:
    name: str
    command: str
    language: str
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
    # Argument vector for `<provider> doctor`, used by the registry/CLI parity tests.
    doctor_args: tuple[str, ...]
    # Extra keyword arguments for surface.doctor(), e.g. skipping wowhead's live probes.
    doctor_options: dict[str, Any] = field(default_factory=dict)


PROVIDERS: tuple[ProviderRegistration, ...] = (
    ProviderRegistration(
        name="wowhead",
        command="wowhead",
        language="python",
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
        doctor_args=("doctor", "--no-live"),
        doctor_options={"live": False},
    ),
    ProviderRegistration(
        name="method",
        command="method",
        language="python",
        status="ready",
        description="Method.gg article provider with sitemap-backed search and guide bundle export/query.",
        auth_required=False,
        expansion_mode="fixed",
        supported_expansions=("retail",),
        expansion_review_status="reviewed",
        expansion_policy_note=(
            "Current supported live guide/article families are retail-focused "
            "and do not expose reliable non-retail routing."
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
        doctor_args=("doctor",),
    ),
    ProviderRegistration(
        name="icy-veins",
        command="icy-veins",
        language="python",
        status="ready",
        description="Icy Veins article provider with sitemap-backed search, resolve, and guide bundle export/query.",
        auth_required=False,
        expansion_mode="fixed",
        supported_expansions=("retail",),
        expansion_review_status="reviewed",
        expansion_policy_note=(
            "Current supported guide families are retail-focused "
            "and do not provide a reliable wrapper-level non-retail split."
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
        doctor_args=("doctor",),
    ),
    ProviderRegistration(
        name="raiderio",
        command="raiderio",
        language="python",
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
        doctor_args=("doctor",),
    ),
    ProviderRegistration(
        name="warcraftlogs",
        command="warcraftlogs",
        language="python",
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
        doctor_args=("doctor", "--no-live"),
        doctor_options={"live": False},
    ),
    ProviderRegistration(
        name="warcraft-wiki",
        command="warcraft-wiki",
        language="python",
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
        doctor_args=("doctor",),
    ),
    ProviderRegistration(
        name="simc",
        command="simc",
        language="python",
        status="partial",
        description="SimulationCraft local provider with repo inspection, build decoding, and local run workflows.",
        auth_required=False,
        expansion_mode="none",
        supported_expansions=(),
        expansion_review_status="deferred",
        expansion_policy_note=(
            "Local repo analysis is versioned differently from wrapper content providers "
            "and should not join expansion fanout yet."
        ),
        wrapper_capabilities={
            "doctor": "ready",
            "search": "coming_soon",
            "resolve": "coming_soon",
        },
        surface=simc_provider,
        tier="core",
        expansion_option=None,
        app=simc_app,
        doctor_args=("doctor",),
    ),
    ProviderRegistration(
        name="raidbots",
        command="raidbots",
        language="python",
        status="partial",
        description="Raidbots report consumption provider: parse public reports and bridge SimC input to local simc.",
        auth_required=False,
        expansion_mode="fixed",
        supported_expansions=("retail",),
        expansion_review_status="reviewed",
        expansion_policy_note=(
            "Raidbots reports are retail-focused SimulationCraft runs; "
            "report consumption stays fixed to retail."
        ),
        wrapper_capabilities={
            "doctor": "ready",
            "search": "not_supported",
            "resolve": "not_supported",
        },
        surface=raidbots_provider,
        tier="experimental",
        expansion_option=None,
        app=raidbots_app,
        doctor_args=("doctor",),
    ),
    ProviderRegistration(
        name="blizzard-api",
        command="blizzard",
        language="python",
        status="partial",
        description="Official Blizzard Battle.net WoW API provider: doctor + auth, Game Data (realm, item) and Profile (character) reads.",
        auth_required=True,
        # expansion_mode="none" mirrors simc: Blizzard routes by region + namespace class
        # (dynamic/static/profile), which is not the wrapper's expansion axis, so there is no honest
        # expansion to advertise. A side effect (shared with simc) is that `warcraft --expansion <x>
        # blizzard ...` is rejected by the wrapper passthrough; plain `warcraft blizzard ...` works.
        # Relaxing expansion-pinned passthrough for none-expansion providers is wrapper-wide policy
        # (AUR-384/AUR-389), not part of this provider.
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
            "search": "coming_soon",
            "resolve": "coming_soon",
        },
        surface=blizzard_provider,
        tier="experimental",
        expansion_option=None,
        app=blizzard_app,
        doctor_args=("doctor",),
    ),
    ProviderRegistration(
        name="curseforge",
        command="curseforge",
        language="python",
        status="partial",
        description="CurseForge addon provider: doctor + addon lookup (metadata, latest files, changelog) over the public CurseForge API.",
        auth_required=True,
        # expansion_mode="none" mirrors blizzard-api/simc: addon game-version compatibility lives
        # inside individual file records, not the wrapper's expansion axis, so there is no honest
        # expansion to advertise. As with those providers, `warcraft --expansion <x> curseforge ...`
        # is rejected by the wrapper passthrough (relaxed to passthrough per AUR-384/AUR-389 policy),
        # while plain `warcraft curseforge ...` works.
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
            "search": "coming_soon",
            "resolve": "coming_soon",
        },
        surface=curseforge_provider,
        tier="experimental",
        expansion_option=None,
        app=curseforge_app,
        doctor_args=("doctor",),
    ),
    ProviderRegistration(
        name="lorrgs",
        command="lorrgs",
        language="python",
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
        doctor_args=("doctor",),
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


def source_exit_code(source_result: Mapping[str, Any]) -> int:
    """Exit with the failing source's own code (blocked -> 5, not_found -> 4) instead of a flat 1."""
    code = source_result.get("exit_code")
    if isinstance(code, int) and code != 0:
        return code
    error = source_result.get("error")
    error_code = error.get("code") if isinstance(error, dict) else None
    return exit_code_for(error_code) if isinstance(error_code, str) else EXIT_GENERIC


def wrapper_envelope(command: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Shape a wrapper-built payload as the contract envelope: exactly the envelope keys, nothing else.

    Envelope keys the payload sets win over the defaults, except that a failure's ``kind`` is always
    ``error``. Every other key is payload content: it goes under ``data`` on success, and under
    ``error.details`` on failure, where ``data`` is ``{}``.
    """
    ok = bool(payload.get("ok", "error" not in payload))
    body = {key: value for key, value in payload.items() if key not in ENVELOPE_KEYS}
    envelope: dict[str, Any] = {
        "ok": ok,
        "provider": "warcraft",
        "command": command,
        "kind": command,
        "schema_version": SCHEMA_VERSION,
        "query": None,
        "provenance": {},
        **{key: value for key, value in payload.items() if key in ENVELOPE_KEYS},
    }
    if ok:
        envelope["data"] = {**body, **as_dict(payload.get("data"))}
        return envelope
    error = dict(as_dict(payload.get("error")))
    details = {**body, **as_dict(error.get("details"))}
    if details:
        error["details"] = details
    envelope.update(ok=False, kind="error", data={}, error=error)
    return envelope


def provider_payload_data(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    """The ``data`` body of a provider envelope: the only place a wrapper composite reads its fields."""
    return as_dict(as_dict(payload).get("data"))


def _call_surface(
    provider: str, command: str, call: Callable[[], Mapping[str, Any]], *, query: str | None = None
) -> tuple[int, dict[str, Any]]:
    """Run one pure surface call, returning ``(exit_code, envelope)`` and never raising.

    A raised failure echoes ``query``, the input the wrapper handed the surface.
    """
    try:
        envelope = call()
    except Exception as exc:
        failure, exit_code = error_envelope_for(provider, command, exc)
        return exit_code, {**failure, "query": query}
    payload = dict(envelope)
    if payload.get("ok") is False:
        error = payload.get("error")
        code = error.get("code") if isinstance(error, dict) else None
        return exit_code_for(code) if isinstance(code, str) else EXIT_GENERIC, payload
    return 0, payload


def provider_search(provider: str, query: str, *, limit: int = 5, expansion: str | None = None) -> dict[str, Any]:
    registration = get_provider(provider)
    unsupported = _unsupported_expansion_result(registration, expansion, command="search", query=query)
    if unsupported is not None:
        return unsupported
    options = provider_expansion_options(registration, expansion)
    code, payload = _call_surface(
        provider, "search", lambda: registration.surface.search(query, limit=limit, **options), query=query
    )
    return {"provider": provider, "exit_code": code, "payload": payload}


def provider_resolve(provider: str, query: str, *, limit: int = 5, expansion: str | None = None) -> dict[str, Any]:
    registration = get_provider(provider)
    unsupported = _unsupported_expansion_result(registration, expansion, command="resolve", query=query)
    if unsupported is not None:
        return unsupported
    options = provider_expansion_options(registration, expansion)
    code, payload = _call_surface(
        provider, "resolve", lambda: registration.surface.resolve(query, limit=limit, **options), query=query
    )
    return {"provider": provider, "exit_code": code, "payload": payload}


def _capture_command(app: typer.Typer, args: list[str], *, prog_name: str) -> tuple[int, dict[str, Any] | None, str]:
    """Run a provider Typer app in-process, capturing its exit code, JSON payload, and raw output."""
    command = typer.main.get_command(app)
    out, err = io.StringIO(), io.StringIO()
    exit_code = 0
    failure: dict[str, Any] | None = None
    try:
        with redirect_stdout(out), redirect_stderr(err):
            returned = command.main(args=args, prog_name=prog_name, standalone_mode=False)
        if isinstance(returned, int):
            exit_code = returned
    except click.ClickException as exc:
        # standalone_mode=False raises usage errors instead of printing them.
        exit_code = EXIT_USAGE
        failure = dict(error_envelope(provider=prog_name, command=prog_name, code="usage_error", message=exc.format_message()))
    except SystemExit as exc:
        exit_code = exc.code if isinstance(exc.code, int) else EXIT_GENERIC
    except Exception as exc:
        # A provider crash must reach the agent as an envelope, never a traceback.
        envelope, exit_code = error_envelope_for(prog_name, prog_name, exc)
        failure = dict(envelope)
    text = out.getvalue() + err.getvalue()
    if failure is not None:
        return exit_code, failure, text
    return exit_code, _first_json_object(out.getvalue()) or _first_json_object(err.getvalue()), text


def _first_json_object(text: str) -> dict[str, Any] | None:
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
    if isinstance(exit_code, int) and exit_code != 0:
        raise typer.Exit(exit_code)


def provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, Any]:
    registration = get_provider(provider)
    unsupported = _unsupported_expansion_result(registration, expansion, command=" ".join(args[:1]))
    if unsupported is not None:
        return {**unsupported, "stdout": ""}
    normalized_args = [*provider_expansion_args(registration, expansion), *args]
    code, payload, stdout = _capture_command(registration.app, normalized_args, prog_name=registration.command)
    return {
        "provider": provider,
        "exit_code": code,
        "payload": payload,
        "stdout": stdout,
    }


def provider_doctor(provider: str, *, requested_expansion: str | None = None) -> dict[str, Any]:
    registration = get_provider(provider)
    expansion_options: dict[str, str] = {}
    if provider_expansion_exclusion_reason(registration, requested_expansion=requested_expansion) is None:
        expansion_options = provider_expansion_options(registration, requested_expansion)
    code, payload = _call_surface(
        provider, "doctor", lambda: registration.surface.doctor(**registration.doctor_options, **expansion_options)
    )
    raw_auth = as_dict(payload.get("data")).get("auth")
    auth_details = raw_auth if isinstance(raw_auth, dict) else None
    return {
        "provider": registration.name,
        "status": registration.status if code == 0 else "error",
        "command": registration.command,
        "language": registration.language,
        "tier": registration.tier,
        # The provider package imported, so the surface is always reachable in-process.
        "installed": True,
        "invocation_mode": "python_entrypoint",
        "auth": auth_details
        or {
            "required": registration.auth_required,
            "configured": None,
        },
        "expansion_support": provider_expansion_support(registration, requested_expansion=requested_expansion),
        "wrapper_surfaces": {
            surface: provider_surface_support(registration, surface)
            for surface in ("doctor", "search", "resolve")
        },
        "details": payload,
    }


def provider_tiers() -> dict[str, list[str]]:
    """Provider names grouped by support tier, in registry order."""
    tiers: dict[str, list[str]] = {"core": [], "supported": [], "experimental": []}
    for registration in PROVIDERS:
        tiers[registration.tier].append(registration.name)
    return tiers


def global_doctor_payload(*, requested_expansion: str | None = None) -> dict[str, Any]:
    included, excluded = expansion_filtered_providers(requested_expansion=requested_expansion)
    return {
        "wrapper": {
            "provider_count": len(PROVIDERS),
            "python_first": True,
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
        "providers": [provider_doctor(provider.name, requested_expansion=requested_expansion) for provider in PROVIDERS],
        "included_providers": [provider.name for provider in included],
        "excluded_providers": excluded,
    }

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from blizzard_api_cli.main import app as blizzard_app
from curseforge_cli.main import app as curseforge_app
from icy_veins_cli.main import app as icy_veins_app
from method_cli.main import app as method_app
from raidbots_cli.main import app as raidbots_app
from raiderio_cli.main import app as raiderio_app
from simc_cli.main import app as simc_app
from typer.testing import CliRunner
from warcraft_content.paths import cache_root, config_root, data_root, state_root, worktree_runtime_details
from warcraft_wiki_cli.main import app as warcraft_wiki_app
from warcraftlogs_cli.main import app as warcraftlogs_app
from wowhead_cli.expansion_profiles import list_profiles
from wowhead_cli.main import app as wowhead_app
from wowprogress_cli.main import app as wowprogress_app

runner = CliRunner()


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
    wrapper_capabilities: dict[str, str]
    app: Any
    doctor_args: tuple[str, ...]


PROVIDERS: tuple[ProviderRegistration, ...] = (
    ProviderRegistration(
        name="wowhead",
        command="wowhead",
        language="python",
        status="ready",
        description="Structured Wowhead provider with live search, resolve, and retrieval commands.",
        auth_required=False,
        expansion_mode="profiled",
        supported_expansions=tuple(profile.key for profile in list_profiles()),
        expansion_review_status="reviewed",
        expansion_policy_note="Provider has first-class expansion profiles and real version-specific routing.",
        wrapper_capabilities={
            "doctor": "ready",
            "search": "ready",
            "resolve": "ready",
        },
        app=wowhead_app,
        doctor_args=("doctor", "--no-live"),
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
        expansion_mode="fixed",
        supported_expansions=("retail",),
        expansion_review_status="reviewed",
        expansion_policy_note=(
            "Current supported Warcraft Logs routing is retail-only "
            "and discovery is intentionally limited to explicit report references."
        ),
        wrapper_capabilities={
            "doctor": "ready",
            "search": "ready_explicit_report_only",
            "resolve": "ready_explicit_report_only",
        },
        app=warcraftlogs_app,
        doctor_args=("doctor",),
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
        app=warcraft_wiki_app,
        doctor_args=("doctor",),
    ),
    ProviderRegistration(
        name="wowprogress",
        command="wowprogress",
        language="python",
        status="partial",
        description=(
            "WowProgress rankings provider with structured search, conservative resolve, "
            "guild, character, and PvE leaderboard lookups."
        ),
        auth_required=False,
        expansion_mode="fixed",
        supported_expansions=("retail",),
        expansion_review_status="reviewed",
        expansion_policy_note=(
            "Current supported guild, character, and PvE leaderboard surfaces are retail-focused "
            "and should stay fixed to retail for now."
        ),
        wrapper_capabilities={
            "doctor": "ready",
            "search": "ready",
            "resolve": "ready",
        },
        app=wowprogress_app,
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
            "inspect_report": "ready",
            "input": "ready",
            "explain_input": "ready",
        },
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
            "game_data": "ready",
            "profile": "ready",
        },
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
            "addon": "ready",
        },
        app=curseforge_app,
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


def _invoke_provider_app(app: Any, args: list[str]) -> tuple[int, dict[str, Any] | None, str]:
    result = runner.invoke(app, args)
    payload: dict[str, Any] | None = None
    for raw in (result.stdout.strip(), getattr(result, "stderr", "").strip(), result.output.strip()):
        if not raw:
            continue
        try:
            maybe_payload = json.loads(raw)
        except json.JSONDecodeError:
            maybe_payload = None
        if isinstance(maybe_payload, dict):
            payload = maybe_payload
            break
    return result.exit_code, payload, result.output


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


def provider_search(provider: str, query: str, *, limit: int = 5, expansion: str | None = None) -> dict[str, Any]:
    registration = get_provider(provider)
    args = ["search", query, "--limit", str(limit)]
    if expansion is not None and registration.name == "wowhead":
        args = ["--expansion", expansion, *args]
    code, payload, _stdout = _invoke_provider_app(registration.app, args)
    return {
        "provider": provider,
        "exit_code": code,
        "payload": payload,
    }


def provider_resolve(provider: str, query: str, *, limit: int = 5, expansion: str | None = None) -> dict[str, Any]:
    registration = get_provider(provider)
    args = ["resolve", query, "--limit", str(limit)]
    if expansion is not None and registration.name == "wowhead":
        args = ["--expansion", expansion, *args]
    code, payload, _stdout = _invoke_provider_app(registration.app, args)
    return {
        "provider": provider,
        "exit_code": code,
        "payload": payload,
    }


def provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, Any]:
    registration = get_provider(provider)
    normalized_args = list(args)
    if expansion is not None and registration.name == "wowhead":
        normalized_args = ["--expansion", expansion, *normalized_args]
    code, payload, stdout = _invoke_provider_app(registration.app, normalized_args)
    return {
        "provider": provider,
        "exit_code": code,
        "payload": payload,
        "stdout": stdout,
    }


def provider_doctor(provider: str, *, requested_expansion: str | None = None) -> dict[str, Any]:
    registration = get_provider(provider)
    code, payload, _stdout = _invoke_provider_app(registration.app, list(registration.doctor_args))
    installed = payload is not None or code == 0
    auth_details = payload.get("auth") if isinstance(payload, dict) and isinstance(payload.get("auth"), dict) else None
    return {
        "provider": registration.name,
        "status": registration.status if code == 0 else "error",
        "command": registration.command,
        "language": registration.language,
        "installed": installed,
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


def global_doctor_payload(*, requested_expansion: str | None = None) -> dict[str, Any]:
    included, excluded = expansion_filtered_providers(requested_expansion=requested_expansion)
    return {
        "wrapper": {
            "provider_count": len(PROVIDERS),
            "python_first": True,
            "shell_fallback": True,
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

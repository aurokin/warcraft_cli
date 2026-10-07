"""Discovery and readiness services shared by the provider surface and CLI.

These functions return data without importing Typer or the command module. Report-scoped
commands also use the same reference parsing and saved-auth semantics.
"""

from __future__ import annotations

import base64
import json
import re
import shlex
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

from warcraft_api.cache import cache_backend_health, redacted_redis_url
from warcraft_core.auth import load_provider_auth_state, provider_auth_status
from warcraft_core.discovery import ResolveConfidence, discovery_row, resolve_data, search_data
from warcraft_core.identity import is_warcraftlogs_report_code
from warcraft_core.paths import provider_state_path

from warcraftlogs_cli.client import (
    CLASSIC_PROFILE,
    FRESH_PROFILE,
    RETAIL_PROFILE,
    WarcraftLogsClient,
    WarcraftLogsClientError,
    WarcraftLogsSiteProfile,
    load_warcraftlogs_auth_config,
    load_warcraftlogs_cache_settings_from_env,
    saved_user_token_site_key,
    warcraftlogs_provider_env_path,
)
from warcraftlogs_cli.report_payloads import report_url as _report_url


@dataclass(frozen=True, slots=True)
class ReportReference:
    code: str
    fight_id: int | None
    source_url: str | None = None
    # The site a report URL's host names; None for a bare code or a host that names no known site.
    site: WarcraftLogsSiteProfile | None = None


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
            client.current_user()
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


def _capability_status(*, ready: bool, reason: str) -> str:
    return "ready" if ready else reason


def _public_capability_status(public_api_access: dict[str, Any]) -> str:
    return _capability_status(
        ready=bool(public_api_access["ready"]),
        reason=str(public_api_access.get("reason") or "requires_client_credentials"),
    )


def _doctor_cache_payload() -> dict[str, Any]:
    """The resolved cache configuration, the block every provider doctor carries."""
    try:
        settings, guild_ttl, static_ttl, report_ttl, finished_report_ttl = load_warcraftlogs_cache_settings_from_env()
    except ValueError as exc:
        # Every cached read fails on this config, so doctor must not report the provider ready.
        return {"available": False, "error": {"code": "invalid_cache_config", "message": str(exc)}}
    return {
        "enabled": settings.enabled,
        "backend": settings.backend,
        "cache_dir": str(settings.cache_dir),
        "redis_url": redacted_redis_url(settings.redis_url),
        "prefix": settings.prefix,
        "ttls": {"guild": guild_ttl, "static": static_ttl, "reports": report_ttl, "finished_report": finished_report_ttl},
        **cache_backend_health(settings),
    }


def doctor_payload(*, live: bool, site: WarcraftLogsSiteProfile) -> dict[str, Any]:
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
    cache = _doctor_cache_payload()
    return {
        # Every data command needs the public API, so without it the provider is degraded, as it is
        # when a Redis cache does not answer or the cache config does not parse.
        "status": "ready" if public_api_access["ready"] and cache.get("available") is not False else "degraded",
        "installed": True,
        "language": "python",
        "cache": cache,
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


def _warcraftlogs_command_prefix(site: WarcraftLogsSiteProfile) -> str:
    if site.key == RETAIL_PROFILE.key:
        return "warcraftlogs"
    return f"warcraftlogs --site {shlex.quote(site.key)}"


def _report_discovery_hint(site: WarcraftLogsSiteProfile) -> dict[str, Any]:
    """What a free-text search or resolve answers with: discovery only takes explicit report references."""
    command_prefix = _warcraftlogs_command_prefix(site)
    return {
        "message": (
            "Warcraft Logs discovery is intentionally narrow for now. "
            "Use an explicit report URL or a bare report code."
        ),
        "supported_inputs": [
            f"{site.root_url}/reports/<code>?fight=<id> (or #fight=<id>)",
            "<report_code>",
        ],
        # Placeholders are bare words, so each command stays a valid shell line once filled in.
        "suggested_commands": [
            f"{command_prefix} report REPORT_CODE",
            f"{command_prefix} report-encounter REPORT_CODE --fight-id FIGHT_ID",
        ],
    }


def _url_site_profile(hostname: str | None) -> WarcraftLogsSiteProfile | None:
    """The site profile a report URL's host belongs to: a report code only exists on its own site."""
    labels = (hostname or "").split(".")
    if labels[-2:] != ["warcraftlogs", "com"]:
        return None
    subdomains = labels[:-2]
    if subdomains in ([], ["www"]):
        return RETAIL_PROFILE
    return next((profile for profile in (CLASSIC_PROFILE, FRESH_PROFILE) if profile.key in subdomains), None)


def _parse_report_reference(reference: str, *, explicit_fight_id: int | None) -> ReportReference:
    text = reference.strip()
    if not text:
        raise ValueError("Report reference is required.")
    source_url: str | None = None
    parsed = urlparse(text)
    # A bare code may carry the fight the way a URL does: ``CODE#fight=N`` or ``CODE?fight=N``.
    code = text if parsed.scheme or parsed.netloc else parsed.path
    parsed_fight_id: int | None = None
    if parsed.scheme and parsed.netloc:
        # Any warcraftlogs.com host (de., ko.classic., vanilla., ...); site detection is separate.
        if (parsed.hostname or "").split(".")[-2:] != ["warcraftlogs", "com"]:
            raise ValueError("Report URL must point to warcraftlogs.com or one of its subdomains.")
        source_url = text
        parts = [part for part in parsed.path.strip("/").split("/") if part]
        try:
            reports_index = parts.index("reports")
            code = parts[reports_index + 1]
        except (ValueError, IndexError):
            raise ValueError("Could not extract a Warcraft Logs report code from the provided URL.") from None
    # Warcraft Logs writes the fight as ``?fight=N`` or ``#fight=N``; the query string wins.
    fight_values = parse_qs(parsed.query).get("fight") or parse_qs(parsed.fragment).get("fight") or []
    if fight_values:
        try:
            parsed_fight_id = int(fight_values[0])
        except ValueError:
            parsed_fight_id = None
    if not re.fullmatch(r"[A-Za-z0-9]+", code):
        raise ValueError(f"{text!r} is not a Warcraft Logs report code or report URL.")
    fight_id = explicit_fight_id if explicit_fight_id is not None else parsed_fight_id
    return ReportReference(code=code, fight_id=fight_id, source_url=source_url, site=_url_site_profile(parsed.hostname))


def explicit_report_reference(query: str) -> ReportReference | None:
    text = query.strip()
    if not text:
        return None
    if " " in text and not text.startswith("http://") and not text.startswith("https://"):
        return None
    try:
        ref = _parse_report_reference(text, explicit_fight_id=None)
    except ValueError:
        return None
    return ref if is_warcraftlogs_report_code(ref.code, from_url=ref.source_url is not None) else None


def _report_discovery_candidate(ref: ReportReference, *, site: WarcraftLogsSiteProfile) -> dict[str, Any]:
    quoted_reference = shlex.quote(ref.code)
    # A report URL names its own site, which the follow-up command and the report URL have to use.
    report_site = ref.site or site
    command_prefix = _warcraftlogs_command_prefix(report_site)
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
    return discovery_row(
        provider="warcraftlogs",
        kind=kind,
        id=f"warcraftlogs:{kind}:{ref.code}:{ref.fight_id or ''}",
        name=f"Warcraft Logs report {ref.code}",
        url=_report_url(ref.code, fight_id=ref.fight_id, root_url=report_site.root_url),
        score=score,
        match_reasons=reasons,
        command=next_command,
        surface=kind,
        report_reference=_report_reference_payload(ref),
    )


def report_search_payload(query: str, *, ref: ReportReference | None, site: WarcraftLogsSiteProfile, limit: int) -> dict[str, Any]:
    if ref is None:
        return search_data(search_query=query, ranked=[], limit=limit, **_report_discovery_hint(site))
    return search_data(
        search_query=query,
        ranked=[_report_discovery_candidate(ref, site=site)],
        limit=limit,
        discovery_scope="explicit_report_reference",
        message="Matched an explicit Warcraft Logs report reference.",
    )


def report_resolve_payload(query: str, *, ref: ReportReference | None, site: WarcraftLogsSiteProfile) -> dict[str, Any]:
    if ref is None:
        return resolve_data(
            search_query=query, ranked=[], limit=1, confidence="none", fallback_search_command=None, **_report_discovery_hint(site)
        )
    # A URL or a fight ID makes the reference certain. A bare code is only judged by its shape, so it
    # stays unresolved and its command waits in match.follow_up.command.
    confidence: ResolveConfidence = "high" if ref.fight_id is not None or ref.source_url is not None else "medium"
    # Free-text search adds nothing here, so there is no fallback search to offer.
    return resolve_data(
        search_query=query,
        ranked=[_report_discovery_candidate(ref, site=site)],
        limit=1,
        confidence=confidence,
        fallback_search_command=None,
    )


def _report_reference_payload(ref: ReportReference) -> dict[str, Any]:
    return {"code": ref.code, "fight_id": ref.fight_id, "source_url": ref.source_url}


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

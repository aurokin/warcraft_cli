"""Provider orchestration behind `warcraft actor-profile`.

The Typer command owns the flag surface; this module reads a Warcraft Logs report actor and joins it
to a Raider.IO character. Provider calls arrive as an injected ``fetch`` callable, as in
``cooldown_packet_flow``, so the command keeps its single seam in ``warcraft_cli.main``.
"""

from __future__ import annotations

import re
from typing import Any, NoReturn
from urllib.parse import urlparse

import typer
from warcraft_core.cli import fail
from warcraft_core.exit_codes import EXIT_GENERIC, EXIT_NOT_FOUND, EXIT_USAGE
from warcraft_core.shapes import as_dict, as_list

from warcraft_cli.crosswalk import (
    actor_lookup_identity,
    actor_spec_ambiguous,
    distinct_actor_targets,
    find_report_actors,
    reconcile_class_spec,
    report_actor_names,
)
from warcraft_cli.provider_calls import ProviderFetch, provider_payload_data, source_exit_code
from warcraft_cli.providers import parse_lorrgs_report_reference

_ACTOR_PROFILE_JOIN_RULE = "soft match on region + realm + character name; not a canonical cross-provider actor id"


def _fail_actor_profile(
    ctx: typer.Context,
    *,
    query: dict[str, Any],
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
    sources: dict[str, Any] | None = None,
    exit_code: int = EXIT_GENERIC,
) -> NoReturn:
    """Emit the crosswalk failure envelope. Structured context goes under ``error.details``."""
    context = {**({"sources": sources} if sources is not None else {}), **(details or {})}
    fail(ctx, code, message, exit_code=exit_code, query=query, details=context)


# A whole-report crosswalk names the fights it reads, and Warcraft Logs takes one --fight-id flag
# per fight. A wipe night can hold fifty of them, so the scope is bounded: kills carry the roster
# the caller means, and the bound keeps one query from turning into a fifty-flag command line.
ACTOR_PROFILE_MAX_SCOPED_FIGHTS = 10


def _actor_profile_fight_scope(fights: list[Any]) -> tuple[list[int], dict[str, Any]]:
    """Pick the fights to read the roster from, kills first, and describe what was left out."""
    rows = [fight for fight in fights if isinstance(fight, dict) and isinstance(fight.get("id"), int)]
    kills: list[int] = [fight["id"] for fight in rows if fight.get("kill") is True]
    others: list[int] = [fight["id"] for fight in rows if fight.get("kill") is not True]
    scoped = [*kills, *others][:ACTOR_PROFILE_MAX_SCOPED_FIGHTS]
    return scoped, {
        "rule": "kills_first_then_report_order",
        "report_fight_count": len(rows),
        "kill_fight_count": len(kills),
        "scoped_fight_count": len(scoped),
        "max_scoped_fights": ACTOR_PROFILE_MAX_SCOPED_FIGHTS,
        "truncated": len(rows) > len(scoped),
    }


def _actor_profile_fight_ids(
    ctx: typer.Context,
    *,
    query: dict[str, Any],
    code: str,
    allow_unlisted: bool,
    expansion: str | None,
    fetch: ProviderFetch,
) -> tuple[list[int], dict[str, Any]]:
    """The fights an unscoped ``--fight-id`` crosswalk reads, plus the scope it applied.

    Warcraft Logs only answers ``playerDetails`` for an explicit fight list or time window; an
    unscoped query comes back as an empty roster. The crosswalk's whole-report default therefore has
    to enumerate the fights itself rather than omit the slice.
    """
    args = ["report-fights", code]
    if allow_unlisted:
        args.append("--allow-unlisted")
    result = fetch("warcraftlogs", args, expansion=expansion)
    if result.get("status") != "ok":
        _fail_actor_profile(
            ctx,
            query=query,
            code="warcraftlogs_lookup_failed",
            message="Warcraft Logs fight lookup failed, so the report roster cannot be scoped.",
            details={"source": result.get("error"), "provider": "warcraftlogs"},
            exit_code=source_exit_code(result),
        )
    fight_ids, scope = _actor_profile_fight_scope(as_list(provider_payload_data(result.get("payload")).get("fights")))
    if not fight_ids:
        _fail_actor_profile(
            ctx,
            query=query,
            code="report_has_no_fights",
            message=f"Report {code!r} contains no fights, so it has no roster to cross-walk.",
            details={"hint": "Check the report code, or pass --fight-id if you know the fight."},
            exit_code=EXIT_NOT_FOUND,
        )
    return fight_ids, scope


def _actor_profile_log_payload(
    ctx: typer.Context,
    *,
    query: dict[str, Any],
    code: str,
    fight_ids: list[int],
    allow_unlisted: bool,
    expansion: str | None,
    fetch: ProviderFetch,
) -> dict[str, Any]:
    """Fetch the Warcraft Logs report-player-details payload the crosswalk reads its actor from."""
    wcl_args = ["report-player-details", code]
    for fight_id in fight_ids:
        wcl_args += ["--fight-id", str(fight_id)]
    if allow_unlisted:
        wcl_args.append("--allow-unlisted")
    log_result = fetch("warcraftlogs", wcl_args, expansion=expansion)
    if log_result.get("status") != "ok":
        _fail_actor_profile(
            ctx,
            query=query,
            code="warcraftlogs_lookup_failed",
            message="Warcraft Logs report lookup failed.",
            details={"source": log_result.get("error"), "provider": "warcraftlogs"},
            exit_code=source_exit_code(log_result),
        )
    return provider_payload_data(log_result.get("payload"))


def _actor_profile_actor(
    ctx: typer.Context,
    *,
    query: dict[str, Any],
    log_payload: dict[str, Any],
    code: str,
    name: str,
) -> dict[str, Any]:
    """Resolve ``name`` to exactly one report actor, or fail with the ambiguity that blocks the join."""
    matches = find_report_actors(log_payload, name)
    if not matches:
        fight_scope = query["fight_scope"]
        message = f"No actor named {name!r} in report {code!r}."
        if fight_scope["truncated"]:
            # A miss inside a sampled scope is not a miss in the report.
            message = (
                f"No actor named {name!r} in the {fight_scope['scoped_fight_count']} of "
                f"{fight_scope['report_fight_count']} fights read from report {code!r}; the other fights "
                "were not searched. Pass --fight-id to read one of them."
            )
        _fail_actor_profile(
            ctx,
            query=query,
            code="actor_not_found",
            message=message,
            details={"available_actors": report_actor_names(log_payload), "fight_scope": fight_scope},
            exit_code=EXIT_NOT_FOUND,
        )
    targets = distinct_actor_targets(matches)
    if len(targets) > 1:
        _fail_actor_profile(
            ctx,
            query=query,
            code="ambiguous_actor",
            message=(
                f"Report {code!r} has {len(targets)} characters named {name!r} on "
                "different realms/regions; cannot pick one safely."
            ),
            details={
                "candidates": targets,
                "hint": (
                    "Narrow to a single fight with --fight-id, or query the realm directly "
                    "with 'raiderio character <region> <realm> <name>'."
                ),
            },
        )
    if actor_spec_ambiguous(matches):
        _fail_actor_profile(
            ctx,
            query=query,
            code="ambiguous_actor_spec",
            message=(
                f"Actor {name!r} appears in report {code!r} with more than one class/spec "
                "across fights; cannot pick one to reconcile."
            ),
            details={"hint": "Narrow to a single fight with --fight-id so the actor resolves to one spec."},
        )
    return matches[0]


def _actor_profile_identity(
    ctx: typer.Context,
    *,
    query: dict[str, Any],
    actor: dict[str, Any],
    log_side: dict[str, Any],
    region: str | None,
) -> dict[str, Any]:
    """Region/realm/name for the Raider.IO lookup, or fail naming the field the report never carried."""
    lookup = actor_lookup_identity(actor, region_override=region)
    if not lookup["ok"]:
        missing = lookup["missing"]
        hint = (
            "Re-run with --region <slug> to enable the Raider.IO profile lookup."
            if missing == "region"
            else f"The report actor is missing its {missing}; the Raider.IO profile lookup cannot proceed."
        )
        _fail_actor_profile(
            ctx,
            query=query,
            code=f"actor_{missing}_unknown",
            message=f"The report actor has no {missing}, so the Raider.IO profile lookup cannot proceed.",
            details={"missing_field": missing, "hint": hint},
            sources={"warcraftlogs": log_side},
        )
    identity: dict[str, Any] = lookup["identity"]
    return identity


def _actor_profile_character(
    ctx: typer.Context,
    *,
    query: dict[str, Any],
    identity: dict[str, Any],
    log_side: dict[str, Any],
    expansion: str | None,
    fetch: ProviderFetch,
) -> dict[str, Any]:
    """Fetch the Raider.IO character the report actor soft-matches to."""
    profile_result = fetch(
        "raiderio",
        ["character", identity["region"], identity["realm"], identity["name"]],
        expansion=expansion,
    )
    if profile_result.get("status") != "ok":
        _fail_actor_profile(
            ctx,
            query=query,
            code="profile_lookup_failed",
            message="Raider.IO character profile lookup failed for the report actor.",
            details={"source": profile_result.get("error"), "provider": "raiderio"},
            sources={
                "warcraftlogs": log_side,
                "raiderio": {"status": "error", "error": profile_result.get("error")},
            },
            exit_code=source_exit_code(profile_result),
        )
    return as_dict(provider_payload_data(profile_result.get("payload")).get("character"))


def _report_realm_slug(
    *, code: str, server: Any, allow_unlisted: bool, expansion: str | None, fetch: ProviderFetch
) -> str | None:
    """The realm slug of a localized server name, from the report's ranked characters.

    Warcraft Logs names an actor's server by its space-stripped name (``Ревущийфьорд``). Raider.IO
    takes a run-together Latin name (``wyrmrestaccord``) but not a localized one, so for a non-ASCII
    name the report's own realm rows supply the slug (``howling-fjord``). ``None`` when they do not.
    """
    if not isinstance(server, str) or server.isascii():
        return None
    args = ["graphql", "--query", _REPORT_REALMS_QUERY, "--report-code", code]
    if allow_unlisted:
        args.append("--allow-unlisted")
    result = fetch("warcraftlogs", args, expansion=expansion)
    report = as_dict(as_dict(provider_payload_data(result.get("payload")).get("reportData")).get("report"))
    realms = (as_dict(row).get("server") for row in as_list(report.get("rankedCharacters")))
    slug = next((realm["slug"] for realm in realms if isinstance(realm, dict) and realm.get("normalizedName") == server), None)
    return slug if isinstance(slug, str) and slug else None


_REPORT_REALMS_QUERY = (
    "query ActorProfileRealms($code: String!, $allowUnlisted: Boolean) {"
    " reportData { report(code: $code, allowUnlisted: $allowUnlisted) {"
    " rankedCharacters { server { slug normalizedName } } } } }"
)


def _report_code_and_fight(ctx: typer.Context, reference: str, *, name: str, fight_id: int | None) -> tuple[str, int | None]:
    """The report code a Warcraft Logs report URL or bare code names, and the URL's fight when ``--fight-id`` is absent.

    The same rule ``warcraftlogs`` report commands apply: a URL on a host other than warcraftlogs.com
    or its subdomains, a URL with no report code, and a blank or non-alphanumeric code are
    ``invalid_query`` before any request.
    """
    text = reference.strip()
    parsed = urlparse(text)
    if parsed.scheme and parsed.netloc:
        ref = parse_lorrgs_report_reference(text) if (parsed.hostname or "").split(".")[-2:] == ["warcraftlogs", "com"] else None
        if ref is not None:
            return ref.code, fight_id if fight_id is not None else ref.fight_id
    elif re.fullmatch(r"[A-Za-z0-9]+", text):
        return text, fight_id
    _fail_actor_profile(
        ctx,
        query={"report_code": reference, "actor_name": name, "fight_id": fight_id},
        code="invalid_query",
        message=f"{reference!r} is not a Warcraft Logs report code or a warcraftlogs.com report URL.",
        exit_code=EXIT_USAGE,
    )


def actor_profile_payload(
    ctx: typer.Context,
    *,
    code: str,
    name: str,
    fight_id: int | None,
    region: str | None,
    allow_unlisted: bool,
    expansion: str | None,
    fetch: ProviderFetch,
) -> dict[str, Any]:
    """Cross-walk a Warcraft Logs report actor to a Raider.IO profile, failing on any break in the join."""
    code, fight_id = _report_code_and_fight(ctx, code, name=name, fight_id=fight_id)
    query: dict[str, Any] = {"report_code": code, "actor_name": name, "fight_id": fight_id}
    scoped_fight_ids, fight_scope = (
        ([fight_id], {"rule": "explicit_fight_id", "scoped_fight_count": 1, "truncated": False})
        if fight_id is not None
        else _actor_profile_fight_ids(
            ctx, query=query, code=code, allow_unlisted=allow_unlisted, expansion=expansion, fetch=fetch
        )
    )
    query["scoped_fight_ids"] = scoped_fight_ids
    query["fight_scope"] = fight_scope
    log_payload = _actor_profile_log_payload(
        ctx,
        query=query,
        code=code,
        fight_ids=scoped_fight_ids,
        allow_unlisted=allow_unlisted,
        expansion=expansion,
        fetch=fetch,
    )
    actor = _actor_profile_actor(ctx, query=query, log_payload=log_payload, code=code, name=name)
    log_side = {
        "status": "ok",
        "role": actor.get("role"),
        "server": actor.get("server"),
        "region": actor.get("region"),
        "class_spec_identity": actor.get("class_spec_identity"),
        "report_actor_identity": actor.get("identity_contract"),
    }
    identity = _actor_profile_identity(ctx, query=query, actor=actor, log_side=log_side, region=region)
    identity["realm"] = _report_realm_slug(
        code=code, server=actor.get("server"), allow_unlisted=allow_unlisted, expansion=expansion, fetch=fetch
    ) or identity["realm"]
    query.update(identity)
    character = _actor_profile_character(
        ctx,
        query=query,
        identity=identity,
        log_side=log_side,
        expansion=expansion,
        fetch=fetch,
    )
    profile_identity = character.get("class_spec_identity")
    return {
        "ok": True,
        "provider": "warcraft",
        "kind": "actor_profile_crosswalk",
        "query": query,
        "join_rule": _ACTOR_PROFILE_JOIN_RULE,
        "sources": {
            "warcraftlogs": log_side,
            "raiderio": {
                "status": "ok",
                "class_spec_identity": profile_identity,
                # Raider.IO's spec is the one the character last logged out in, so an off-spec log
                # reports spec_mismatch; only class_mismatch casts doubt on the join.
                "spec_source": "active_spec",
                "profile_url": character.get("profile_url"),
            },
        },
        "reconciliation": reconcile_class_spec(actor.get("class_spec_identity"), profile_identity),
    }

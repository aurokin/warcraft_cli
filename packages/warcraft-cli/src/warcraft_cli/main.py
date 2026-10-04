from __future__ import annotations

import contextvars
import io
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import typer
from warcraft_core.cli import (
    CompactMaxCharsOption,
    CompactOption,
    FieldsOption,
    FieldsStrictOption,
    PrettyOption,
    ProfileOption,
    RuntimeConfig,
    cfg_as,
    configure,
    emit,
    fail,
    guarded_run,
)
from warcraft_core.exit_codes import EXIT_GENERIC, EXIT_USAGE
from warcraft_core.output import DEFAULT_COMPACT_MAX_CHARS
from warcraft_core.shapes import as_dict, as_list

from warcraft_cli.actor_profile import actor_profile_payload
from warcraft_cli.cooldown_packet_flow import CooldownRequest, emit_cooldown_packet
from warcraft_cli.guide_compare import (
    GuideCompareQueryOptions,
    default_guide_compare_query_root,
    guide_builds_simc_payload,
    guide_compare_payload,
    guide_compare_query_payload,
    load_guide_build_source,
    normalize_guide_compare_providers,
    simc_handoff_failure,
)
from warcraft_cli.guild import guild_merge_payload, normalized_identity, raiderio_guild_summary
from warcraft_cli.provider_contract import (
    compact_resolve_match,
    compact_wrapper_candidate,
    confidence_rank,
    decorate_resolve_payload,
    decorate_search_result,
    merged_search_page,
    provider_max_candidate_score,
    resolve_answer_accepted,
    resolve_payload_sort_key,
)
from warcraft_cli.providers import (
    DescribeOptions,
    ProviderCalls,
    ProviderRegistration,
    expansion_filtered_providers,
    expansion_support_snapshot,
    failed_call,
    get_provider,
    global_doctor_payload,
    invoke_provider_command,
    list_providers,
    parse_json_object,
    provider_expansion_args,
    provider_expansion_exclusion_reason,
    provider_expansion_support,
    provider_invoke,
    provider_payload_data,
    provider_resolve,
    provider_search,
    provider_surface_status,
    resolve_wrapper_expansion_key,
    shared_failure,
    simc_call,
    source_exit_code,
    surface_filtered_providers,
    wrapper_envelope,
)
from warcraft_cli.schema import envelope_json_schema
from warcraft_cli.talent_routing import TalentSource, talent_describe_payload, talent_packet_payload

PROVIDER_NAME = "warcraft"
app = typer.Typer(add_completion=False, help="Warcraft wrapper CLI for routing to service-specific Warcraft CLIs.")


def _emit(ctx: typer.Context, payload: Mapping[str, Any], *, err: bool = False) -> None:
    emit(ctx, wrapper_envelope(ctx.info_name or "", payload), err=err)
GUIDE_COMPARE_BUNDLES_ARGUMENT = typer.Argument(
    ...,
    help="Two or more exported guide bundle directories from wowhead, method, or icy-veins.",
)


@dataclass(slots=True)
class WrapperConfig(RuntimeConfig):
    """Shared runtime config plus the wrapper's one extra global flag."""

    requested_expansion: str | None = None


@app.callback()
def main_callback(
    ctx: typer.Context,
    pretty: PrettyOption = False,
    compact: CompactOption = False,
    fields: FieldsOption = None,
    fields_strict: FieldsStrictOption = False,
    profile: ProfileOption = None,
    compact_max_chars: CompactMaxCharsOption = DEFAULT_COMPACT_MAX_CHARS,
    expansion: str | None = typer.Option(
        None,
        "--expansion",
        help="Filter wrapper search/resolve to a specific expansion profile. Passed through to expansion-aware providers like wowhead.",
    ),
) -> None:
    requested_expansion: str | None = None
    if expansion is not None:
        try:
            requested_expansion = resolve_wrapper_expansion_key(expansion)
        except ValueError as exc:
            raise typer.BadParameter(str(exc), param_hint="--expansion") from exc
    configure(
        ctx,
        provider=PROVIDER_NAME,
        pretty=pretty,
        compact=compact,
        fields=fields,
        fields_strict=fields_strict,
        profile=profile,
        compact_max_chars=compact_max_chars,
        config=WrapperConfig(requested_expansion=requested_expansion),
    )


def _requested_expansion(ctx: typer.Context) -> str | None:
    return cfg_as(ctx, WrapperConfig).requested_expansion


def _expansion_passthrough_advisory(ctx: typer.Context, *, provider_name: str) -> dict[str, Any] | None:
    """Decide expansion policy for a ``warcraft <provider> ...`` proxy.

    Returns ``None`` for a normal passthrough (no expansion requested, or the provider
    supports it). For a provider with ``expansion_mode == "none"`` requested with an
    expansion it returns an advisory dict (relax-to-passthrough): the provider has no
    expansion semantics to honor, so the command runs unchanged with the note attached.
    For a ``fixed``/``profiled`` provider asked for an unsupported expansion this is a
    genuine mismatch — it emits ``unsupported_provider_expansion`` and exits 2 (a usage error).
    """
    requested_expansion = _requested_expansion(ctx)
    if requested_expansion is None:
        return None
    registration = get_provider(provider_name)
    reason = provider_expansion_exclusion_reason(registration, requested_expansion=requested_expansion)
    if reason is None:
        return None
    if registration.expansion_mode == "none":
        return {
            "expansion_filter": "passthrough_no_expansion_semantics",
            "requested_expansion": requested_expansion,
            "provider_expansion_mode": "none",
            "note": (
                f"Provider {provider_name!r} has no expansion semantics (expansion_mode=none); "
                f"the wrapper --expansion {requested_expansion!r} flag was not applied and the "
                "command was passed through unchanged."
            ),
        }
    fail(
        ctx,
        "unsupported_provider_expansion",
        f"Provider {provider_name!r} does not support wrapper expansion {requested_expansion!r}.",
        exit_code=EXIT_USAGE,
        query=_passthrough_query(provider_name, requested_expansion),
        details={
            "provider": provider_name,
            "requested_expansion": requested_expansion,
            "expansion_support": provider_expansion_support(registration, requested_expansion=requested_expansion),
        },
    )


def _passthrough_query(provider_name: str, requested_expansion: str) -> dict[str, str]:
    """What a passthrough refusal echoes: the wrapper's own parsed input, never the provider's argv."""
    return {"provider": provider_name, "expansion": requested_expansion}


def _has_option(args: list[str], flags: set[str]) -> bool:
    return any(arg in flags or any(arg.startswith(f"{flag}=") for flag in flags) for arg in args)


def _forwarded_output_args(ctx: typer.Context) -> list[str]:
    """The wrapper's global output flags, restated for the provider CLI behind a passthrough.

    Every binary accepts the same common options, so `warcraft --pretty wowhead search x` must
    shape the provider's payload the same way `warcraft --pretty search x` shapes the wrapper's.
    """
    output = cfg_as(ctx, WrapperConfig).output
    args: list[str] = []
    if output.pretty:
        args.append("--pretty")
    if output.compact:
        args.append("--compact")
    if output.compact_max_chars != DEFAULT_COMPACT_MAX_CHARS:
        args += ["--compact-max-chars", str(output.compact_max_chars)]
    for path in output.fields:
        args += ["--fields", path]
    if output.fields_strict:
        args.append("--fields-strict")
    if output.profile is not None:
        args += ["--profile", output.profile]
    return args


def _passthrough_args(ctx: typer.Context, *, provider_name: str, forward_output: bool = True) -> list[str]:
    """Provider argv for a passthrough; ``forward_output=False`` leaves output shaping to the wrapper."""
    args = [*(_forwarded_output_args(ctx) if forward_output else []), *ctx.args]
    requested_expansion = _requested_expansion(ctx)
    if requested_expansion is None:
        return args
    registration = get_provider(provider_name)
    if provider_expansion_exclusion_reason(registration, requested_expansion=requested_expansion) is not None:
        return args
    expansion_args = provider_expansion_args(registration, requested_expansion)
    if expansion_args:
        duplicate_flags = {f"--{registration.expansion_option}"}
        if _has_option(args, duplicate_flags):
            flag_text = " or ".join(sorted(duplicate_flags))
            fail(
                ctx,
                "duplicate_expansion_argument",
                f"Do not pass both warcraft --expansion and provider-level {flag_text} in the same command.",
                exit_code=EXIT_USAGE,
                query=_passthrough_query(provider_name, requested_expansion),
                details={"provider": provider_name, "requested_expansion": requested_expansion},
            )
        return [*expansion_args, *args]
    return args


def _run_passthrough(ctx: typer.Context, sub_app: typer.Typer, *, provider_name: str, prog_name: str) -> None:
    """Proxy ``warcraft <provider> ...`` to a provider CLI, applying expansion policy.

    Normal case: invoke the sub-app directly so its payload reaches stdout untouched.
    none-expansion relax case: capture the provider envelope and attach the advisory note inside it
    (``data`` on success, ``error.details`` on failure), then re-emit.
    """
    advisory = _expansion_passthrough_advisory(ctx, provider_name=provider_name)
    # In the capture path the wrapper shapes the annotated payload itself (through ``_emit``), so
    # the output flags are not forwarded; otherwise ``--fields data.expansion_advisory`` could never match.
    args = _passthrough_args(ctx, provider_name=provider_name, forward_output=advisory is None)
    if advisory is None:
        invoke_provider_command(sub_app, args=args, prog_name=prog_name)
        return
    # The provider emits its payload to stdout (ok) or stderr (error). Capture both so the
    # advisory note can be attached regardless of which stream carried the JSON payload.
    # This buffers instead of streaming, but only on the explicit `warcraft --expansion <key>
    # <none-provider> ...` combination — normal `warcraft <provider> ...` returns at the
    # advisory-is-None branch above and streams untouched. There is no incremental streaming to
    # lose here regardless: provider commands emit a single JSON envelope all at once, and
    # attaching the advisory inside the envelope requires the whole payload to parse it,
    # so the buffer just holds that one envelope momentarily (non-JSON output like --help is
    # small and handled by the passthrough branch below). The buffering is intrinsic to the
    # annotate-the-payload feature, not an incidental regression of normal simc/report workflows.
    out_buf, err_buf = io.StringIO(), io.StringIO()
    exit_code = 0
    try:
        with redirect_stdout(out_buf), redirect_stderr(err_buf):
            invoke_provider_command(sub_app, args=args, prog_name=prog_name)
    except typer.Exit as exc:
        exit_code = exc.exit_code if isinstance(exc.exit_code, int) else 1
    out_text, err_text = out_buf.getvalue(), err_buf.getvalue()
    out_json, err_json = parse_json_object(out_text), parse_json_object(err_text)
    if out_json is not None:
        _emit(ctx, _with_expansion_advisory(out_json, advisory))
    elif err_json is not None:
        _emit(ctx, _with_expansion_advisory(err_json, advisory), err=True)
    else:
        # Non-JSON provider output (e.g. --help text): surface the advisory on its own so the
        # relax is never silent, then pass the raw output through below.
        _emit(ctx, {"expansion_advisory": advisory}, err=True)
    # Preserve any non-payload stream content verbatim (provenance is never hidden).
    if out_json is None and out_text:
        typer.echo(out_text, nl=False)
    if err_json is None and err_text:
        typer.echo(err_text, nl=False, err=True)
    if exit_code:
        raise typer.Exit(exit_code)


def _with_expansion_advisory(payload: dict[str, Any], advisory: dict[str, Any]) -> dict[str, Any]:
    """Attach the advisory inside the provider envelope: ``data`` on success, ``error.details`` on failure."""
    if payload.get("ok") is False:
        error = as_dict(payload.get("error"))
        return {**payload, "error": {**error, "details": {**as_dict(error.get("details")), "expansion_advisory": advisory}}}
    return {**payload, "data": {**as_dict(payload.get("data")), "expansion_advisory": advisory}}


def _provider_payload_result(
    provider: str,
    args: list[str],
    *,
    expansion: str | None,
) -> dict[str, Any]:
    result = provider_invoke(provider, args, expansion=expansion)
    payload = result.get("payload") if isinstance(result.get("payload"), dict) else None
    failure = failed_call(result)
    if failure is not None:
        return {"provider": provider, "status": "error", "error": failure[0], "payload": payload, "exit_code": failure[1]}
    return {"provider": provider, "status": "ok", "payload": payload, "exit_code": 0}


def _provider_calls() -> ProviderCalls:
    """The provider seams, read from this module at call time so a test can replace any one of them."""
    return ProviderCalls(invoke=provider_invoke, resolve=provider_resolve, search=provider_search, simc=simc_call)


def _provider_outcome(result: Mapping[str, Any]) -> dict[str, Any]:
    """Per-provider fanout outcome: ``status`` is registry readiness, this is what the call did."""
    payload = result.get("payload")
    exit_code = result.get("exit_code")
    if not isinstance(payload, dict):
        return {
            "ok": False,
            "exit_code": exit_code or EXIT_GENERIC,
            "error": {"code": "missing_provider_payload", "message": "Provider returned no JSON payload."},
        }
    error = payload.get("error")
    return {"ok": bool(payload.get("ok", True)), "exit_code": exit_code, "error": error if isinstance(error, dict) else None}


def _provider_answered(registration: ProviderRegistration, surface: str, provider_row: dict[str, Any]) -> bool:
    """Whether the provider actually looked the query up.

    An explicit-report-only provider (Warcraft Logs) answers free text with a hint it builds
    locally and no rows by construction. Counting that as an answer would turn an outage of every
    provider that does search into an ok:true empty page.
    """
    if not provider_row["ok"]:
        return False
    if provider_surface_status(registration, surface) != "ready_explicit_report_only":
        return True
    data = provider_payload_data(provider_row.get("payload"))
    return bool(as_list(data.get("results")) or data.get("match"))


def _failed_provider_rows(providers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One compact row per provider that did not answer, kept in the payload even under ``--brief``."""
    rows: list[dict[str, Any]] = []
    for provider_row in providers:
        if provider_row.get("ok"):
            continue
        error = as_dict(provider_row.get("error"))
        rows.append(
            {
                "provider": provider_row.get("provider"),
                "code": error.get("code"),
                "message": error.get("message"),
                "exit_code": provider_row.get("exit_code"),
            }
        )
    return rows


def _unresolved_reason(top: dict[str, Any]) -> str:
    """Why the top-ranked resolve answer is not the wrapper's answer, read from its provider's payload.

    A provider that capped a one-word query's answer at ``medium`` says so in ``confidence_cap``; the
    wrapper reports that rule rather than recomputing it.
    """
    if top.get("resolved"):
        return "provider_family_ranked_down_by_query_intent"
    if as_dict(top.get("confidence_cap")).get("rule") == "single_word_query":
        return "single_word_query_not_named_exactly"
    return "provider_did_not_resolve"


def _unresolved_next_steps(candidates: list[dict[str, Any]], *, resolved: bool) -> dict[str, Any]:
    """What an agent should do next when the top-ranked candidate is not a resolved answer.

    Providers that decline to resolve still report a best candidate and their own
    ``fallback_search_command``; without these the wrapper's resolve is a dead end even when a
    provider clearly found the thing. ``candidates`` holds only the providers that returned a match,
    so a provider that found nothing never hands over a search certain to come back empty. They are
    in ranking order (wrapper score, intent fit included; the provider's confidence only breaks a
    tie), so ``best_unresolved_candidate`` (which names why it is not the answer) and the first
    fallback search are the match ``warcraft search`` ranks first. A provider's ``low`` often means
    two right pages tied, so it does not push an on-intent match behind an off-intent ``medium`` one
    here, although it does keep that match from being the answer. A match its provider resolved but
    a better-ranked unresolved match blocked is listed in ``provider_resolved_candidates`` with its
    ``next_command``, because a resolved answer has no fallback search.
    """
    if resolved:
        return {
            "fallback_search_command": None,
            "fallback_search_commands": [],
            "best_unresolved_candidate": None,
            "provider_resolved_candidates": [],
        }
    fallbacks = [
        {"provider": as_dict(row.get("match")).get("provider"), "command": command}
        for row in candidates
        if isinstance(command := row.get("fallback_search_command"), str) and command.strip()
    ]
    top = candidates[0] if candidates else None
    best = compact_resolve_match(top)
    if best is not None and top is not None:
        best["resolved"] = False
        best["unresolved_reason"] = _unresolved_reason(top)
    return {
        "fallback_search_command": fallbacks[0]["command"] if fallbacks else None,
        "fallback_search_commands": fallbacks,
        "best_unresolved_candidate": best,
        "provider_resolved_candidates": [compact_resolve_match(row) for row in candidates[1:] if row.get("resolved")],
    }


def _provider_warnings(providers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every ``*_warning`` a provider put in its provenance (Icy Veins' stale-sitemap warning), kept under ``--brief``."""
    return [
        {"provider": row.get("provider"), "key": key, "warning": value}
        for row in providers
        for key, value in as_dict(as_dict(row.get("payload")).get("provenance")).items()
        if key.endswith("_warning") and isinstance(value, str) and value
    ]


def _fan_out(
    registrations: list[ProviderRegistration], call: Callable[[ProviderRegistration], dict[str, Any]]
) -> list[dict[str, Any]]:
    """``call`` for every provider at once, so a query waits for the slowest provider rather than all of
    them in turn; the results come back in ``registrations`` order.

    Each worker runs in its own copy of this context, so the provider's cache lookups still reach the
    command's cache ledger (``warcraft_core.cache_ledger``).
    """
    with ThreadPoolExecutor(max_workers=max(1, len(registrations))) as pool:
        futures = [pool.submit(contextvars.copy_context().run, call, registration) for registration in registrations]
        return [future.result() for future in futures]


def _fanout_health(providers: list[dict[str, Any]]) -> dict[str, Any]:
    """Answered/failed counts, failure rows and provider warnings, so nothing a provider flagged is silent."""
    failed_rows = _failed_provider_rows(providers)
    return {
        "answered_provider_count": sum(1 for row in providers if row["answered"]),
        "failed_provider_count": len(failed_rows),
        "failed_providers": failed_rows,
        "provider_warnings": _provider_warnings(providers),
    }


def _require_query(ctx: typer.Context, query: str) -> None:
    """Reject a blank query before the fanout: every provider would refuse it, and one would send it upstream."""
    if not query.strip():
        fail(ctx, "invalid_query", "Query cannot be empty.")


def _emit_fanout(ctx: typer.Context, payload: dict[str, Any]) -> None:
    """Emit a search/resolve payload, failing with the providers' own error when none answered."""
    failed_rows = payload["failed_providers"]
    if failed_rows and not payload["answered_provider_count"]:
        code, exit_code = shared_failure(failed_rows)
        message = (
            f"No provider answered: {len(failed_rows)} providers failed and no other included provider "
            "searched this query."
        )
        error = {"code": code, "message": message, "details": {"failed_providers": failed_rows}}
        _emit(ctx, {"ok": False, "query": payload["query"], "error": error}, err=True)
        raise typer.Exit(exit_code)
    if not payload["answered_provider_count"]:
        # Only explicit-report providers (or none) were included, so nobody looked the text up.
        message = "No included provider searches this query; see error.details for the providers excluded and why."
        details = {key: payload[key] for key in ("requested_expansion", "included_providers", "excluded_providers")}
        error = {"code": "no_searching_provider", "message": message, "details": details}
        _emit(ctx, {"ok": False, "query": payload["query"], "error": error}, err=True)
        raise typer.Exit(EXIT_USAGE)
    _emit(ctx, payload)


def _raiderio_source(identity: dict[str, str], *, expansion: str | None) -> dict[str, Any]:
    """The Raider.IO guild call, summarized, with the envelope's provenance.

    The summary carries every data field the snapshot uses, so the raw envelope is not repeated
    beside it; `warcraft raiderio guild` returns that envelope.
    """
    result = _provider_payload_result(
        "raiderio",
        ["guild", identity["region"], identity["realm"], identity["name"]],
        expansion=expansion,
    )
    if result.get("status") != "ok":
        return result
    payload = as_dict(result.pop("payload"))
    return {**result, "provenance": payload.get("provenance"), "summary": raiderio_guild_summary(provider_payload_data(payload))}


@app.command("doctor")
def doctor(ctx: typer.Context) -> None:
    """Report wrapper and per-provider readiness: tiers, auth, expansion support, and runtime paths."""
    _emit(ctx, global_doctor_payload(requested_expansion=_requested_expansion(ctx)))


@app.command("schema")
def schema(ctx: typer.Context) -> None:
    """Print the JSON Schema (draft 2020-12) that every envelope this repo emits conforms to.

    The same document is checked in at `schemas/envelope.schema.json` for agents that cannot run the
    CLI; see `docs/foundation/ERROR_CONTRACT.md` for what the keys mean.
    """
    _emit(ctx, {"kind": "envelope_schema", "schema": envelope_json_schema()})


@app.command("search")
def search(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="Search across available providers."),
    limit: int = typer.Option(
        5,
        "--limit",
        min=1,
        max=50,
        help="Results to request from each provider, and the size of the merged result list.",
    ),
    brief: bool = typer.Option(
        False,
        "--brief",
        help="Return a smaller wrapper payload: compact candidate rows and no per-provider payloads.",
    ),
    ranking_debug: bool = typer.Option(
        False, "--ranking-debug", help="Include compact wrapper ranking summaries for the returned candidates."),
    expansion_debug: bool = typer.Option(
        False,
        "--expansion-debug",
        help="Include a compact expansion support snapshot for all providers.",
    ),
) -> None:
    """Fan out a free-text query to every search-ready provider and rank the merged candidates."""
    _require_query(ctx, query)
    requested_expansion = _requested_expansion(ctx)
    expansion_included, excluded_providers = expansion_filtered_providers(requested_expansion=requested_expansion)
    included_registrations, surface_excluded = surface_filtered_providers(
        expansion_included,
        surface="search",
        requested_expansion=requested_expansion,
    )
    excluded_providers = [*excluded_providers, *surface_excluded]
    providers: list[dict[str, Any]] = []
    flattened: list[dict[str, Any]] = []
    results = _fan_out(
        included_registrations,
        lambda registration: provider_search(registration.name, query, limit=limit, expansion=requested_expansion),
    )
    for registration, result in zip(included_registrations, results, strict=True):
        provider_payload = result.get("payload")
        provider_row = {
            "provider": registration.name,
            "status": registration.status,
            **_provider_outcome(result),
            "expansion_support": provider_expansion_support(
                registration,
                requested_expansion=requested_expansion,
            ),
            "payload": provider_payload,
        }
        provider_row["answered"] = _provider_answered(registration, "search", provider_row)
        providers.append(provider_row)
        if isinstance(provider_payload, dict):
            provider_data = provider_payload_data(provider_payload)
            provider_results = [row for row in as_list(provider_data.get("results")) if isinstance(row, dict)]
            # Scores are normalized against this provider's own best row before the merge so a
            # provider with an inflated local scale cannot own every slot in the merged list.
            provider_max_score = provider_max_candidate_score(provider_results)
            for index, row in enumerate(provider_results):
                flattened.append(
                    decorate_search_result(
                        query,
                        {
                            "provider_expansion": provider_expansion_support(
                                registration,
                                requested_expansion=requested_expansion,
                            ),
                            **row,
                        },
                        provider_max_score=provider_max_score,
                        provider_top_row=index == 0,
                    )
                )
    ranked, merge_policy = merged_search_page(flattened, limit=limit)
    merge_policy["provider_total_matches"] = {
        row["provider"]: provider_payload_data(row["payload"]).get("total_matches") for row in providers
    }
    top = [compact_wrapper_candidate(row) for row in ranked] if brief else ranked
    payload: dict[str, Any] = {
        "query": query,
        "provider_count": len(list_providers()),
        "requested_expansion": requested_expansion,
        "expansion_filter_active": requested_expansion is not None,
        "included_providers": [registration.name for registration in included_registrations],
        "excluded_providers": excluded_providers,
        "included_provider_count": len(included_registrations),
        "excluded_provider_count": len(excluded_providers),
        **_fanout_health(providers),
        "providers": [] if brief else providers,
        "count": len(top),
        "truncated": len(flattened) > len(top),
        "merge_policy": merge_policy,
        "results": top,
    }
    if ranking_debug:
        payload["ranking_debug"] = [compact_wrapper_candidate(row) for row in ranked]
    if expansion_debug:
        payload["expansion_debug"] = expansion_support_snapshot(requested_expansion=requested_expansion)
    _emit_fanout(ctx, payload)


@app.command("resolve")
def resolve(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="Resolve a query across available providers."),
    limit: int = typer.Option(5, "--limit", min=1, max=50, help="Ranked candidates to list under --ranking-debug."),
    brief: bool = typer.Option(
        False,
        "--brief",
        help="Return a smaller wrapper payload: a compact match summary and no per-provider payloads.",
    ),
    ranking_debug: bool = typer.Option(
        False, "--ranking-debug", help="Include the first --limit providers' matches in ranking order, each with its resolved flag."
    ),
    expansion_debug: bool = typer.Option(
        False,
        "--expansion-debug",
        help="Include a compact expansion support snapshot for all providers.",
    ),
) -> None:
    """Fan out a query to every resolve-ready provider and return the single best match plus its follow-up command.

    The answer is the candidate `warcraft search` would rank first, skipping any its own provider
    rated `low`, and only when that provider resolved it at `high` confidence; otherwise the command
    reports `resolved: false` with the top-ranked candidate as `best_unresolved_candidate` and lists
    any lower match a provider resolved under `provider_resolved_candidates`.
    """
    _require_query(ctx, query)
    requested_expansion = _requested_expansion(ctx)
    expansion_included, excluded_providers = expansion_filtered_providers(requested_expansion=requested_expansion)
    included_registrations, surface_excluded = surface_filtered_providers(
        expansion_included,
        surface="resolve",
        requested_expansion=requested_expansion,
    )
    excluded_providers = [*excluded_providers, *surface_excluded]
    providers: list[dict[str, Any]] = []
    ranked: list[dict[str, Any]] = []
    results = _fan_out(
        included_registrations, lambda registration: provider_resolve(registration.name, query, expansion=requested_expansion)
    )
    for registration, result in zip(included_registrations, results, strict=True):
        provider_payload = result.get("payload")
        provider_row = {
            "provider": registration.name,
            "status": registration.status,
            **_provider_outcome(result),
            "expansion_support": provider_expansion_support(
                registration,
                requested_expansion=requested_expansion,
            ),
            "payload": provider_payload,
        }
        provider_row["answered"] = _provider_answered(registration, "resolve", provider_row)
        providers.append(provider_row)
        resolve_data = provider_payload_data(provider_payload)
        if isinstance(resolve_data.get("match"), dict):
            ranked.append(decorate_resolve_payload(query, resolve_data))
    ranked.sort(key=resolve_payload_sort_key)
    # A match its own provider rated low (a tie it could not break, a weak guess) never stands in
    # front of another provider's answer; a medium one still does: it found something unconfirmed.
    contenders = [row for row in ranked if confidence_rank(row.get("confidence")) > confidence_rank("low")]
    top = contenders[0] if contenders else None
    best = top if top is not None and resolve_answer_accepted(top) else None
    match = compact_resolve_match(best) if brief else as_dict(best).get("match")
    payload: dict[str, Any] = {
        "query": query,
        "provider_count": len(list_providers()),
        "requested_expansion": requested_expansion,
        "expansion_filter_active": requested_expansion is not None,
        "included_providers": [registration.name for registration in included_registrations],
        "excluded_providers": excluded_providers,
        "included_provider_count": len(included_registrations),
        "excluded_provider_count": len(excluded_providers),
        "resolved": best is not None,
        "selected_provider": as_dict(as_dict(best).get("match")).get("provider"),
        "match": match or None,
        "next_command": as_dict(best).get("next_command"),
        "confidence": as_dict(best).get("confidence"),
        **_fanout_health(providers),
        **_unresolved_next_steps(ranked, resolved=best is not None),
        "providers": [] if brief else providers,
    }
    if ranking_debug:
        payload["ranking_debug"] = [
            {**as_dict(compact_resolve_match(row)), "resolved": bool(row.get("resolved"))} for row in ranked[:limit]
        ]
    if expansion_debug:
        payload["expansion_debug"] = expansion_support_snapshot(requested_expansion=requested_expansion)
    _emit_fanout(ctx, payload)


@app.command("guild")
def guild(
    ctx: typer.Context,
    region: str = typer.Argument(..., help="Region slug such as us or eu."),
    realm: str = typer.Argument(..., help="Realm title or slug."),
    name: str = typer.Argument(..., help="Guild name."),
) -> None:
    """Return one guild identity's Raider.IO snapshot: identity, every raid's progression and ranks, roster preview, citations.

    Raider.IO orders its progression and rankings rows by raid slug and reports no raid start/end
    window, so the snapshot names no "active" raid; cross-reference `warcraft raiderio raids` for
    the tier that is currently running.
    """
    identity = normalized_identity(region, realm, name)
    source = _raiderio_source(identity, expansion=_requested_expansion(ctx))
    payload = guild_merge_payload(identity, raiderio=source)
    _emit(ctx, payload, err=not payload.get("ok"))
    if not payload.get("ok"):
        raise typer.Exit(source_exit_code(source))


@app.command("actor-profile")
def actor_profile(
    ctx: typer.Context,
    code: str = typer.Argument(..., help="Warcraft Logs report URL or report code."),
    name: str = typer.Argument(..., help="Character (actor) name within the report."),
    fight_id: int | None = typer.Option(
        None, "--fight-id", help="Narrow to one fight (makes the log actor identity canonical). Defaults to fight=<id> from the URL."
    ),
    region: str | None = typer.Option(None, "--region", help="Override the actor region for the Raider.IO lookup."),
    allow_unlisted: bool = typer.Option(False, "--allow-unlisted", help="Allow lookup of unlisted Warcraft Logs reports."),
) -> None:
    """Cross-walk a Warcraft Logs report actor to a Raider.IO profile (log actor -> profile handoff)."""
    payload = actor_profile_payload(
        ctx,
        code=code,
        name=name,
        fight_id=fight_id,
        region=region,
        allow_unlisted=allow_unlisted,
        expansion=_requested_expansion(ctx),
        fetch=_provider_payload_result,
    )
    _emit(ctx, payload)


@app.command("cooldown-packet")
def cooldown_packet(
    ctx: typer.Context,
    report_ref: str = typer.Argument(..., help="Warcraft Logs report URL/code or Lorrgs user_report URL."),
    fight_id: int | None = typer.Option(None, "--fight-id", help="Fight id. Defaults to fight=<id> from the URL."),
    actor_id: int | None = typer.Option(None, "--actor-id", help="Report-local source/actor id for the player to analyze."),
    actor_name: str | None = typer.Option(
        None,
        "--actor-name",
        help="Player name within the selected fight, used when --actor-id is omitted.",
    ),
    phase: int = typer.Option(..., "--phase", min=1, help="One-based phase index to analyze, e.g. --phase 2 for P2."),
    spec_slug: str | None = typer.Option(
        None,
        "--spec-slug",
        help="Override the Lorrgs spec slug, e.g. mage-frost; any provider's spelling (frost-mage, Frost Mage) is translated.",
    ),
    boss_slug: str | None = typer.Option(None, "--boss-slug", help="Override Lorrgs boss slug, e.g. lura."),
    difficulty: str | None = typer.Option(
        None,
        "--difficulty",
        help="Lorrgs difficulty for the top-parse comparison. Defaults to the Warcraft Logs fight's own difficulty.",
    ),
    metric: str | None = typer.Option(None, "--metric", help="Optional Lorrgs ranking metric, e.g. dps or hps."),
    sample_limit: int = typer.Option(5, "--sample-limit", min=0, max=20, help="Top-parse samples to include; 0 disables comparison."),
    event_limit: int = typer.Option(5000, "--event-limit", min=1, max=10000, help="Warcraft Logs cast events to request."),
    spell_id: list[int] | None = typer.Option(
        None,
        "--spell-id",
        help="Restrict tracked cooldown spell ids. Repeatable. Defaults to Lorrgs query/show spells for the spec.",
    ),
    allow_unlisted: bool = typer.Option(False, "--allow-unlisted", help="Allow lookup of unlisted Warcraft Logs reports."),
) -> None:
    """Build an evidence packet for phase-scoped cooldown analysis."""
    request = CooldownRequest(
        report_ref=report_ref,
        fight_id=fight_id,
        actor_id=actor_id,
        actor_name=actor_name,
        phase=phase,
        # Lorrgs spec slugs are lowercase, so ``WARRIOR-arms`` is the same spec as ``warrior-arms``.
        spec_slug=spec_slug.lower() if spec_slug else spec_slug,
        boss_slug=boss_slug,
        difficulty=difficulty,
        metric=metric,
        sample_limit=sample_limit,
        event_limit=event_limit,
        spell_ids=spell_id,
        allow_unlisted=allow_unlisted,
        expansion=_requested_expansion(ctx),
    )
    emit_cooldown_packet(ctx, request, fetch=_provider_payload_result)


@app.command("guide-compare")
def guide_compare(
    ctx: typer.Context,
    bundles: list[Path] = GUIDE_COMPARE_BUNDLES_ARGUMENT,
    max_age_hours: int = typer.Option(
        24,
        "--max-age-hours",
        min=1,
        max=24 * 30,
        help="Freshness threshold (hours) for each compared bundle's exported_at.",
    ),
) -> None:
    """Compare two or more already-exported guide bundles from wowhead, method, or icy-veins."""
    _emit(ctx, guide_compare_payload(ctx, bundles, max_age_hours=max_age_hours))


@app.command("guide-compare-query")
def guide_compare_query(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="Guide query to resolve across supported guide providers."),
    provider: list[str] = typer.Option(
        [],
        "--provider",
        help="Restrict orchestration to one or more providers from: wowhead, method, icy-veins.",
    ),
    out_root: Path | None = typer.Option(
        None,
        "--out-root",
        file_okay=False,
        dir_okay=True,
        writable=True,
        resolve_path=True,
        help=(
            "Directory root where orchestrated guide bundles should be written. "
            "Defaults to <data root>/guide_compare/<query-slug>, with the data root `warcraft doctor` "
            "reports as paths.data_root; nothing is written to the current directory."
        ),
    ),
    max_age_hours: int = typer.Option(
        24,
        "--max-age-hours",
        min=1,
        max=24 * 30,
        help="Reuse existing orchestrated guide bundles only when they are newer than this many hours.",
    ),
    force_refresh: bool = typer.Option(
        False,
        "--force-refresh/--no-force-refresh",
        help="Re-export selected guide bundles even when a fresh orchestrated bundle already exists.",
    ),
    simc_build_handoff: bool = typer.Option(
        False,
        "--simc-build-handoff/--no-simc-build-handoff",
        help="Also emit an explicit guide-build-to-simc evidence packet from the exported bundles.",
    ),
    simc_apl_path: str | None = typer.Option(
        None,
        "--simc-apl-path",
        help="Optional SimC APL path used to add exact-build describe-build output when simc build handoff is enabled.",
    ),
    simc_decode: bool = typer.Option(
        True,
        "--simc-decode/--no-simc-decode",
        help="Also run simc decode-build for each explicit guide build reference when simc build handoff is enabled.",
    ),
    simc_build_limit: int = typer.Option(
        20,
        "--simc-build-limit",
        min=1,
        max=200,
        help="Maximum unique explicit build references to hand off to simc when simc build handoff is enabled.",
    ),
) -> None:
    """Resolve a guide query across wowhead, method, and icy-veins, export the bundles, and compare them."""
    try:
        selected_providers = normalize_guide_compare_providers(provider)
    except ValueError as exc:
        fail(ctx, "invalid_argument", str(exc))

    payload, exit_code = guide_compare_query_payload(
        GuideCompareQueryOptions(
            query=query,
            providers=selected_providers,
            orchestration_root=(out_root or default_guide_compare_query_root(query)).expanduser(),
            requested_expansion=_requested_expansion(ctx),
            max_age_hours=max_age_hours,
            force_refresh=force_refresh,
            simc_build_handoff=simc_build_handoff,
            simc_apl_path=simc_apl_path,
            simc_decode=simc_decode,
            simc_build_limit=simc_build_limit,
        ),
        _provider_calls(),
    )
    _emit(ctx, payload, err=exit_code != 0)
    if exit_code != 0:
        raise typer.Exit(exit_code)


@app.command("talent-packet")
def talent_packet(
    ctx: typer.Context,
    source: str = typer.Argument(
        ...,
        help=(
            "Explicit Wowhead talent-calc ref with build code, explicit Warcraft Logs report ref "
            "with --actor-id, or a talent transport packet JSON path."
        ),
    ),
    actor_id: int | None = typer.Option(None, "--actor-id", help="Required for Warcraft Logs report sources; report-local actor ID."),
    fight_id: int | None = typer.Option(None, "--fight-id", help="Optional explicit fight id for Warcraft Logs report sources."),
    allow_unlisted: bool = typer.Option(False, "--allow-unlisted", help="Allow lookup of unlisted Warcraft Logs reports."),
    listed_build_limit: int = typer.Option(
        10,
        "--listed-build-limit",
        min=1,
        max=100,
        help="Maximum embedded Wowhead listed builds to keep when using a talent-calc ref.",
    ),
    validate: bool = typer.Option(
        True,
        "--validate/--no-validate",
        help="Upgrade raw packet inputs through simc validation when possible.",
    ),
    out: str | None = typer.Option(None, "--out", help="Optional path to write the final talent transport packet JSON."),
) -> None:
    """Build a validated talent transport packet from a Wowhead talent-calc or Warcraft Logs reference."""
    request = TalentSource(
        source=source,
        actor_id=actor_id,
        fight_id=fight_id,
        allow_unlisted=allow_unlisted,
        listed_build_limit=listed_build_limit,
        validate=validate,
        expansion=_requested_expansion(ctx),
    )
    _emit(ctx, talent_packet_payload(ctx, request, out=out, calls=_provider_calls()))


@app.command("talent-describe")
def talent_describe(
    ctx: typer.Context,
    source: str = typer.Argument(
        ...,
        help=(
            "Explicit Wowhead talent-calc ref with build code, explicit Warcraft Logs report ref "
            "with --actor-id, or a talent transport packet JSON path."
        ),
    ),
    actor_id: int | None = typer.Option(None, "--actor-id", help="Required for Warcraft Logs report sources; report-local actor ID."),
    fight_id: int | None = typer.Option(None, "--fight-id", help="Optional explicit fight id for Warcraft Logs report sources."),
    allow_unlisted: bool = typer.Option(False, "--allow-unlisted", help="Allow lookup of unlisted Warcraft Logs reports."),
    listed_build_limit: int = typer.Option(
        10,
        "--listed-build-limit",
        min=1,
        max=100,
        help="Maximum embedded Wowhead listed builds to keep when using a talent-calc ref.",
    ),
    validate: bool = typer.Option(
        True,
        "--validate/--no-validate",
        help="Upgrade raw packet inputs through simc validation when possible.",
    ),
    packet_out: str | None = typer.Option(
        None,
        "--packet-out",
        help="Optional path to write the final routed talent transport packet JSON.",
    ),
    apl_path: str | None = typer.Option(
        None,
        "--apl-path",
        help="Optional SimC APL path. If omitted, simc tries the default APL for the resolved build.",
    ),
    targets: int = typer.Option(1, "--targets", min=1, help="Primary target count for the base build summary."),
    aoe_targets: int = typer.Option(5, "--aoe-targets", min=2, help="Secondary target count used for the cleave/AoE comparison view."),
    list_name: str = typer.Option("default", "--list", help="Starting action list."),
    priority_limit: int = typer.Option(
        8,
        "--priority-limit",
        min=1,
        max=50,
        help="Maximum active priority rows to summarize per target view.",
    ),
    inactive_limit: int = typer.Option(
        8,
        "--inactive-limit",
        min=1,
        max=50,
        help="Maximum inactive talent-gated actions to summarize per target view.",
    ),
) -> None:
    """Build a talent transport packet and add simc describe-build output for the decoded build."""
    request = TalentSource(
        source=source,
        actor_id=actor_id,
        fight_id=fight_id,
        allow_unlisted=allow_unlisted,
        listed_build_limit=listed_build_limit,
        validate=validate,
        expansion=_requested_expansion(ctx),
    )
    describe = DescribeOptions(
        # A blank --apl-path means no APL, as it does for guide-builds-simc.
        apl_path=apl_path if apl_path and apl_path.strip() else None,
        targets=targets,
        aoe_targets=aoe_targets,
        list_name=list_name,
        priority_limit=priority_limit,
        inactive_limit=inactive_limit,
    )
    _emit(ctx, talent_describe_payload(ctx, request, packet_out=packet_out, describe=describe, calls=_provider_calls()))


@app.command("guide-builds-simc")
def guide_builds_simc(
    ctx: typer.Context,
    source: Path = typer.Argument(
        ...,
        exists=True,
        file_okay=False,
        dir_okay=True,
        readable=True,
        resolve_path=True,
        help="Exported guide bundle directory or guide-compare-query output root.",
    ),
    decode: bool = typer.Option(
        True,
        "--decode/--no-decode",
        help="Also run simc decode-build for each unique explicit build reference.",
    ),
    apl_path: str | None = typer.Option(
        None,
        "--apl-path",
        help="Optional SimC APL path used to add exact-build describe-build output for each explicit guide build ref.",
    ),
    limit: int = typer.Option(
        20,
        "--limit",
        min=1,
        max=200,
        help="Maximum unique explicit build references to hand off to simc.",
    ),
) -> None:
    """Turn the explicit build references in exported guide bundles into a simc evidence packet."""
    try:
        source_kind, bundle_inputs, source_manifest = load_guide_build_source(source)
    except ValueError as exc:
        fail(ctx, "invalid_bundle_source", str(exc), details={"source": str(source)})

    payload = guide_builds_simc_payload(
        source_path=source,
        source_kind=source_kind,
        source_manifest=source_manifest,
        bundle_inputs=bundle_inputs,
        decode=decode,
        apl_path=apl_path,
        limit=limit,
        simc=simc_call,
    )
    if payload["summary"]["simc_handoff_status"] == "all_handoffs_failed":
        # The packet's provenance stays the envelope's; the rest of it, per-build failure codes
        # included, becomes `error.details`.
        _emit(ctx, {**payload, "ok": False, "error": simc_handoff_failure(payload)}, err=True)
        raise typer.Exit(EXIT_GENERIC)
    _emit(ctx, payload)


def _register_passthrough(registration: ProviderRegistration) -> None:
    """Register `warcraft <provider> ...` as a proxy to that provider's own CLI."""

    def passthrough(ctx: typer.Context) -> None:
        _run_passthrough(ctx, registration.app, provider_name=registration.name, prog_name=registration.command)

    app.command(
        registration.command,
        help=(
            f"Proxy to the {registration.command} CLI ({registration.tier} tier). "
            "Remaining arguments are passed through unchanged."
        ),
        context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
    )(passthrough)


for _registration in list_providers():
    _register_passthrough(_registration)


def run() -> None:
    guarded_run(app, provider=PROVIDER_NAME)


if __name__ == "__main__":
    run()

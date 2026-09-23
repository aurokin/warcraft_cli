from __future__ import annotations

import io
import json
import tempfile
from collections.abc import Mapping
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import urlparse

import typer
from warcraft_content.article_bundle import compare_article_bundles, load_article_bundle
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
    guarded_run,
)
from warcraft_core.exit_codes import EXIT_GENERIC, EXIT_NETWORK, EXIT_NOT_FOUND, exit_code_for
from warcraft_core.expansions import wowhead_path_prefixes
from warcraft_core.identity import (
    build_reference_transport_packet_payload,
    parse_wowhead_talent_calc_ref,
    validate_talent_transport_packet,
)
from warcraft_core.output import DEFAULT_COMPACT_MAX_CHARS
from warcraft_core.paths import data_root
from warcraft_core.shapes import as_dict, as_list

from warcraft_cli.cooldown_packet_flow import CooldownRequest, emit_cooldown_packet
from warcraft_cli.crosswalk import (
    actor_lookup_identity,
    actor_spec_ambiguous,
    distinct_actor_targets,
    find_report_actors,
    reconcile_class_spec,
    report_actor_names,
)
from warcraft_cli.guild import guild_merge_payload, guild_rank_rows, normalized_identity, raiderio_guild_summary
from warcraft_cli.provider_contract import (
    compact_resolve_match,
    compact_wrapper_candidate,
    decorate_resolve_payload,
    decorate_search_result,
    merged_search_page,
    provider_max_candidate_score,
    resolve_payload_sort_key,
)
from warcraft_cli.providers import (
    ProviderRegistration,
    expansion_filtered_providers,
    expansion_support_snapshot,
    get_provider,
    global_doctor_payload,
    invoke_provider_command,
    list_providers,
    provider_expansion_args,
    provider_expansion_exclusion_reason,
    provider_expansion_support,
    provider_invoke,
    provider_payload_data,
    provider_resolve,
    provider_search,
    provider_surface_status,
    resolve_wrapper_expansion_key,
    source_exit_code,
    surface_filtered_providers,
    wrapper_envelope,
)
from warcraft_cli.schema import envelope_json_schema

PROVIDER_NAME = "warcraft"
app = typer.Typer(add_completion=False, help="Warcraft wrapper CLI for routing to service-specific Warcraft CLIs.")


def _emit(ctx: typer.Context, payload: Mapping[str, Any], *, err: bool = False) -> None:
    emit(ctx, wrapper_envelope(ctx.info_name or "", payload), err=err)
GUIDE_COMPARE_BUNDLES_ARGUMENT = typer.Argument(
    ...,
    help="Two or more exported guide bundle directories from wowhead, method, or icy-veins.",
)
GUIDE_COMPARE_QUERY_PROVIDERS = ("wowhead", "method", "icy-veins")


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
    genuine mismatch — it emits ``unsupported_provider_expansion`` and exits 1.
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
    _emit(ctx,
        {
            "ok": False,
            "error": {
                "code": "unsupported_provider_expansion",
                "message": (
                    f"Provider {provider_name!r} does not support wrapper expansion "
                    f"{requested_expansion!r}."
                ),
                "details": {
                    "provider": provider_name,
                    "requested_expansion": requested_expansion,
                    "expansion_support": provider_expansion_support(
                        registration,
                        requested_expansion=requested_expansion,
                    ),
                },
            },
        },
        err=True,
    )
    raise typer.Exit(1)


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
            _emit(ctx,
                {
                    "ok": False,
                    "error": {
                        "code": "duplicate_expansion_argument",
                        "message": (
                            f"Do not pass both warcraft --expansion and provider-level {flag_text} "
                            "in the same command."
                        ),
                        "details": {"provider": provider_name, "requested_expansion": requested_expansion},
                    },
                },
                err=True,
            )
            raise typer.Exit(1)
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
    out_json, err_json = _parse_json_object(out_text), _parse_json_object(err_text)
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


def _parse_json_object(text: str) -> dict[str, Any] | None:
    try:
        value = json.loads(text.strip())
    except (json.JSONDecodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _with_expansion_advisory(payload: dict[str, Any], advisory: dict[str, Any]) -> dict[str, Any]:
    """Attach the advisory inside the provider envelope: ``data`` on success, ``error.details`` on failure."""
    if payload.get("ok") is False:
        error = as_dict(payload.get("error"))
        return {**payload, "error": {**error, "details": {**as_dict(error.get("details")), "expansion_advisory": advisory}}}
    return {**payload, "data": {**as_dict(payload.get("data")), "expansion_advisory": advisory}}


def _slugify_path_fragment(value: str) -> str:
    parts = [
        part
        for part in "".join(
            character.lower() if character.isalnum() else " "
            for character in value.strip()
        ).split()
        if part
    ]
    if not parts:
        return "query"
    return "-".join(parts[:12])


def _default_guide_compare_query_root(query: str) -> Path:
    """Where exported bundles land without ``--out-root``: the XDG data dir, never the caller's CWD."""
    return data_root() / "guide_compare" / _slugify_path_fragment(query)


def _guide_compare_manifest_path(root: Path) -> Path:
    return root / "manifest.json"


def _iso_now_utc() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_iso8601_utc(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _guide_compare_freshness(exported_at: Any, *, max_age_hours: int) -> dict[str, Any]:
    parsed = _parse_iso8601_utc(exported_at)
    if parsed is None:
        return {"status": "stale", "reason": "missing_exported_at", "age_hours": None, "max_age_hours": max_age_hours}
    age_hours = round((datetime.now(UTC) - parsed).total_seconds() / 3600, 2)
    if age_hours > max_age_hours:
        return {"status": "stale", "reason": "max_age_exceeded", "age_hours": age_hours, "max_age_hours": max_age_hours}
    return {"status": "fresh", "reason": "within_max_age", "age_hours": age_hours, "max_age_hours": max_age_hours}


def _guide_build_handoff_freshness(source_kind: str, source_manifest: dict[str, Any] | None) -> dict[str, Any]:
    updated_at = source_manifest.get("updated_at") if isinstance(source_manifest, dict) else None
    parsed_updated_at = _parse_iso8601_utc(updated_at)
    if source_kind == "orchestration_root":
        if parsed_updated_at is None:
            return {
                "status": "unknown",
                "reason": "missing_orchestration_updated_at",
                "sampled_at": None,
                "cache_ttl_seconds": None,
            }
        return {
            "status": "known",
            "reason": "orchestration_manifest_updated_at",
            "sampled_at": parsed_updated_at.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "cache_ttl_seconds": None,
        }
    exported_at = source_manifest.get("exported_at") if isinstance(source_manifest, dict) else None
    parsed_exported_at = _parse_iso8601_utc(exported_at)
    if parsed_exported_at is None:
        return {
            "status": "unknown",
            "reason": "bundle_manifest_has_no_export_timestamp",
            "sampled_at": None,
            "cache_ttl_seconds": None,
        }
    return {
        "status": "known",
        "reason": "bundle_manifest_exported_at",
        "sampled_at": parsed_exported_at.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "cache_ttl_seconds": None,
    }


def _guide_compare_freshness_rollup(
    bundle_freshness: list[dict[str, Any]],
    *,
    max_age_hours: int,
) -> dict[str, Any]:
    statuses = [row["freshness"]["status"] for row in bundle_freshness]
    ages = [
        row["freshness"]["age_hours"]
        for row in bundle_freshness
        if isinstance(row["freshness"].get("age_hours"), (int, float))
    ]
    if not bundle_freshness:
        status = "unknown"
    elif all(status == "fresh" for status in statuses):
        status = "fresh"
    else:
        status = "stale"
    return {
        "status": status,
        "max_age_hours": max_age_hours,
        "bundle_count": len(bundle_freshness),
        "fresh_count": sum(1 for status_value in statuses if status_value == "fresh"),
        "stale_count": sum(1 for status_value in statuses if status_value == "stale"),
        "oldest_age_hours": max(ages) if ages else None,
        "newest_age_hours": min(ages) if ages else None,
    }


def _guide_comparison_packet(
    bundle_inputs: list[tuple[Path, dict[str, Any]]],
    *,
    max_age_hours: int,
) -> dict[str, Any]:
    """Build the guide-bundle comparison object with additive freshness + scope evidence.

    Shared by `guide-compare` and `guide-compare-query` so both emit the same comparison
    packet (raw evidence + `freshness` rollup + `comparison_evidence`).
    """
    comparison = compare_article_bundles(bundle_inputs)
    bundle_descriptors = [
        descriptor for descriptor in (comparison.get("bundles") or []) if isinstance(descriptor, dict)
    ]
    bundle_freshness = [
        {
            "provider": descriptor.get("provider"),
            "path": descriptor.get("path"),
            "exported_at": descriptor.get("exported_at"),
            "freshness": _guide_compare_freshness(descriptor.get("exported_at"), max_age_hours=max_age_hours),
        }
        for descriptor in bundle_descriptors
    ]
    freshness_rollup = _guide_compare_freshness_rollup(bundle_freshness, max_age_hours=max_age_hours)
    return {
        **comparison,
        "freshness": freshness_rollup,
        "comparison_evidence": {
            "compared_bundle_count": comparison.get("compared_bundle_count"),
            "providers": [descriptor.get("provider") for descriptor in bundle_descriptors],
            "matching_rules": {
                "section_evidence": comparison.get("section_evidence", {}).get("matching_rule"),
                "analysis_surface_tags": "exact_normalized_tag",
                "build_references": "exact_build_reference_key",
            },
            "freshness": freshness_rollup,
            "bundle_freshness": bundle_freshness,
        },
    }


def _load_guide_compare_manifest(root: Path) -> dict[str, Any] | None:
    path = _guide_compare_manifest_path(root)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_guide_compare_manifest(
    *,
    root: Path,
    query: str,
    requested_expansion: str | None,
    max_age_hours: int,
    provider_results: list[dict[str, Any]],
) -> dict[str, Any]:
    providers: list[dict[str, Any]] = []
    for row in provider_results:
        if row.get("status") not in {"exported", "reused"}:
            continue
        candidate = as_dict(row.get("candidate"))
        freshness = as_dict(row.get("freshness"))
        providers.append(
            {
                "provider": row.get("provider"),
                "bundle_path": row.get("bundle_path"),
                "candidate_ref": candidate.get("ref"),
                "candidate_name": candidate.get("name"),
                "selection_source": candidate.get("selection_source"),
                "exported_at": row.get("exported_at"),
                "freshness": freshness,
            }
        )
    payload = {
        "kind": "guide_compare_orchestration_manifest",
        "updated_at": _iso_now_utc(),
        "query": query,
        "requested_expansion": requested_expansion,
        "max_age_hours": max_age_hours,
        "providers": providers,
    }
    root.mkdir(parents=True, exist_ok=True)
    _guide_compare_manifest_path(root).write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return payload


def _provider_error_payload(provider: str, result: dict[str, Any]) -> dict[str, Any]:
    payload = result.get("payload")
    if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
        return dict(payload["error"])
    return {
        "code": "provider_command_failed",
        "message": f"{provider} command failed.",
        "exit_code": result.get("exit_code"),
    }


def _provider_result_failed(result: dict[str, Any]) -> bool:
    payload = result.get("payload")
    return result.get("exit_code") != 0 or (isinstance(payload, dict) and payload.get("ok") is False)


def _provider_payload_result(
    provider: str,
    args: list[str],
    *,
    expansion: str | None,
) -> dict[str, Any]:
    result = provider_invoke(provider, args, expansion=expansion)
    payload = result.get("payload") if isinstance(result.get("payload"), dict) else None
    if _provider_result_failed(result):
        return {
            "provider": provider,
            "status": "error",
            "error": _provider_error_payload(provider, result),
            "payload": payload,
            "exit_code": result.get("exit_code"),
        }
    return {
        "provider": provider,
        "status": "ok",
        "payload": payload,
        "exit_code": result.get("exit_code"),
    }


def _load_guide_build_source(source_path: Path) -> tuple[str, list[tuple[Path, dict[str, Any]]], dict[str, Any] | None]:
    manifest_path = source_path / "manifest.json"
    if not manifest_path.exists():
        raise ValueError(f"Missing manifest file under {source_path}.")
    try:
        raw_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid manifest under {source_path}: {exc}") from exc
    if not isinstance(raw_manifest, dict):
        raise ValueError(f"Manifest under {source_path} is not a JSON object.")
    if raw_manifest.get("kind") == "guide_compare_orchestration_manifest":
        bundle_inputs: list[tuple[Path, dict[str, Any]]] = []
        providers = raw_manifest.get("providers")
        if not isinstance(providers, list):
            raise ValueError(f"Orchestration manifest under {source_path} is missing providers.")
        for row in providers:
            if not isinstance(row, dict):
                continue
            bundle_path_raw = row.get("bundle_path")
            if not isinstance(bundle_path_raw, str) or not bundle_path_raw.strip():
                continue
            bundle_path = Path(bundle_path_raw).expanduser()
            bundle_inputs.append((bundle_path, load_article_bundle(bundle_path)))
        return "orchestration_root", bundle_inputs, raw_manifest
    return "bundle", [(source_path, load_article_bundle(source_path))], raw_manifest


def _collect_build_reference_handoff_rows(
    bundle_inputs: list[tuple[Path, dict[str, Any]]],
) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for bundle_path, bundle in bundle_inputs:
        manifest = as_dict(bundle.get("manifest"))
        provider = manifest.get("provider") if isinstance(manifest.get("provider"), str) else None
        for row in bundle.get("build_references") or []:
            if not isinstance(row, dict):
                continue
            ref_url = row.get("url")
            if not isinstance(ref_url, str) or not ref_url.strip():
                continue
            record = grouped.get(ref_url)
            source_entry = {
                "provider": provider,
                "bundle_path": str(bundle_path),
                "label": row.get("label"),
                "source_urls": list(row.get("source_urls") or []),
                "build_identity": row.get("build_identity"),
            }
            if record is None:
                grouped[ref_url] = {
                    "reference": {
                        "url": ref_url,
                        "reference_type": row.get("reference_type"),
                        "build_code": row.get("build_code"),
                        "label": row.get("label"),
                        "build_identity": row.get("build_identity"),
                    },
                    "sources": [source_entry],
                }
                continue
            record["sources"].append(source_entry)
    return sorted(grouped.values(), key=lambda row: str(((row.get("reference") or {}).get("url")) or ""))


def _unique_non_empty_strings(values: list[Any]) -> list[str]:
    seen: set[str] = set()
    rows: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        text = value.strip()
        if not text or text in seen:
            continue
        seen.add(text)
        rows.append(text)
    return rows


def _resolve_handoff_build_code(reference: dict[str, Any], build_url: str) -> str | None:
    build_code = reference.get("build_code")
    parsed_ref = parse_wowhead_talent_calc_ref(build_url)
    if not isinstance(build_code, str) or not build_code.strip():
        build_code = parsed_ref.get("build_code") if isinstance(parsed_ref, dict) else None
    if not isinstance(build_code, str) or not build_code.strip():
        return None
    return build_code


def _build_handoff_transport_packet(
    build_url: str,
    normalized_reference: dict[str, Any],
    sources: list[Any],
) -> Any:
    return build_reference_transport_packet_payload(
        ref=build_url,
        provider="warcraft",
        source="guide_build_reference_handoff",
        label=normalized_reference.get("label") if isinstance(normalized_reference.get("label"), str) else None,
        source_urls=_unique_non_empty_strings(
            [
                url
                for source_row in sources
                if isinstance(source_row, dict)
                for url in (source_row.get("source_urls") or [])
            ]
        ),
        notes=[
            "exact build reference came from exported guide bundles",
            "transport packet preserves the same explicit wowhead ref used for simc handoff",
        ],
        scope={"type": "guide_build_reference_handoff"},
    )


def _invoke_simc_for_handoff(
    build_input_args: list[str],
    *,
    decode: bool,
    apl_path: str | None,
    expansion: str | None,
) -> dict[str, Any | None]:
    identify_result = provider_invoke("simc", ["identify-build", *build_input_args], expansion=expansion)
    decode_result = (
        provider_invoke("simc", ["decode-build", *build_input_args], expansion=expansion)
        if decode
        else None
    )
    describe_result = (
        provider_invoke(
            "simc",
            ["describe-build", "--apl-path", apl_path, *build_input_args],
            expansion=expansion,
        )
        if isinstance(apl_path, str) and apl_path.strip()
        else None
    )
    identify_result = _normalize_simc_transport_packet_path(identify_result, stable_packet_path=None)
    decode_result = (
        _normalize_simc_transport_packet_path(decode_result, stable_packet_path=None)
        if isinstance(decode_result, dict)
        else None
    )
    describe_result = (
        _normalize_simc_transport_packet_path(describe_result, stable_packet_path=None)
        if isinstance(describe_result, dict)
        else None
    )
    return {"identify": identify_result, "decode": decode_result, "describe": describe_result}


def _handoff_evidence_section(sources: list[Any]) -> dict[str, Any]:
    return {
        "explicit_build_reference_only": True,
        "source_count": len([item for item in sources if isinstance(item, dict)]),
        "provider_count": len(
            {
                provider
                for provider in (
                    source_row.get("provider") if isinstance(source_row, dict) else None
                    for source_row in sources
                )
                if isinstance(provider, str) and provider
            }
        ),
        "providers": _unique_non_empty_strings(
            [source_row.get("provider") for source_row in sources if isinstance(source_row, dict)]
        ),
        "bundle_paths": _unique_non_empty_strings(
            [source_row.get("bundle_path") for source_row in sources if isinstance(source_row, dict)]
        ),
        "source_urls": _unique_non_empty_strings(
            [
                url
                for source_row in sources
                if isinstance(source_row, dict)
                for url in (source_row.get("source_urls") or [])
            ]
        ),
    }


def _handoff_leg(result: Any) -> dict[str, Any] | None:
    """One simc leg's outcome, with its failure reason beside its payload rather than buried in it."""
    if not isinstance(result, dict):
        return None
    payload = as_dict(result.get("payload"))
    error = as_dict(payload.get("error"))
    return {
        "exit_code": result.get("exit_code"),
        "ok": result.get("exit_code") == 0,
        "error": {"code": error.get("code"), "message": error.get("message")} if error else None,
        "payload": result.get("payload"),
    }


def _handoff_simc_section(simc_results: dict[str, Any | None]) -> dict[str, Any]:
    return {leg: _handoff_leg(simc_results[leg]) for leg in ("identify", "decode", "describe")}


def _handoff_build_input(
    reference: dict[str, Any],
    sources: list[Any],
) -> tuple[list[str], dict[str, Any] | None, dict[str, Any], str | None]:
    """The form simc accepts for this reference type, or the reason the reference cannot be handed over.

    A Wowhead talent-calc URL carries class and spec in its path, so it travels as a talent transport
    packet. A guide-published ``wow_talent_export`` string *is* the build code and is what
    ``simc --build-text`` consumes; sending the raw string as a wowhead reference would fail to parse.

    Returns the simc argv, the transport packet when the reference needs one, the reference with its
    resolved ``build_code``, and the reason it is unusable (``None`` when it is usable).
    """
    build_url = reference.get("url")
    if not isinstance(build_url, str) or not build_url.strip():
        return [], None, reference, "missing_reference_url"
    build_code = _resolve_handoff_build_code(reference, build_url)
    if build_code is None:
        return [], None, reference, "missing_build_code"
    normalized_reference = {**reference, "build_code": build_code}
    reference_type = str(reference.get("reference_type") or "").strip()
    if reference_type == "wow_talent_export":
        return ["--build-text", build_code], None, normalized_reference, None
    transport_packet = _build_handoff_transport_packet(build_url, normalized_reference, sources)
    if isinstance(transport_packet, dict):
        return [], transport_packet, normalized_reference, None
    return [], None, normalized_reference, f"unsupported_reference_type:{reference_type or 'unknown'}"


def _build_simc_handoff_row(
    row: dict[str, Any],
    *,
    decode: bool,
    apl_path: str | None,
    expansion: str | None,
) -> dict[str, Any]:
    """Hand one build reference to simc, or return the excluded row naming why it could not be."""
    reference = as_dict(row.get("reference"))
    sources = as_list(row.get("sources"))
    build_input_args, transport_packet, normalized_reference, unusable_reason = _handoff_build_input(reference, sources)
    if unusable_reason is not None:
        return {
            "status": "excluded",
            "reference": reference,
            "reason": unusable_reason,
            "sources": sources,
        }
    packet_path: Path | None = None
    if transport_packet is not None:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".json",
            prefix="guide-build-packet-",
            delete=False,
        ) as handle:
            json.dump(transport_packet, handle, indent=2)
            handle.write("\n")
            packet_path = Path(handle.name).resolve()
        build_input_args = ["--build-packet", str(packet_path)]
    try:
        simc_results = _invoke_simc_for_handoff(
            build_input_args, decode=decode, apl_path=apl_path, expansion=expansion
        )
    finally:
        packet_path.unlink(missing_ok=True) if packet_path is not None else None
    simc_section = _handoff_simc_section(simc_results)
    return {
        "status": "handed_off",
        "reference": normalized_reference,
        "talent_transport_packet": transport_packet,
        "sources": sources,
        "evidence": _handoff_evidence_section(sources),
        "simc": simc_section,
        "failures": [
            {"leg": leg, **as_dict(section.get("error") or {"code": "simc_leg_failed", "message": None})}
            for leg, section in simc_section.items()
            if isinstance(section, dict) and not section.get("ok")
        ],
    }


def _count_simc_handoff_successes(build_rows: list[dict[str, Any]]) -> tuple[int, int, int]:
    def _success(section_key: str) -> int:
        return len(
            [
                row
                for row in build_rows
                if isinstance(((row.get("simc") or {}).get(section_key)), dict)
                and ((row.get("simc") or {}).get(section_key) or {}).get("exit_code") == 0
            ]
        )

    return _success("identify"), _success("decode"), _success("describe")


def _simc_handoff_status(
    *,
    returned_build_count: int,
    requested_leg_count: int,
    empty_requested_legs: list[str],
    partial_requested_legs: list[str],
) -> str:
    """Whether the simc leg of the handoff produced anything, as one field an agent can branch on.

    ``ok`` means every requested leg succeeded for every build. ``partial`` means a leg worked for
    some builds and not others. A leg that was requested and produced nothing at all - zero decodes
    out of ten - is ``failed``: calling that ``partial`` reads as "most of it worked", which is the
    wrong-answer-with-``ok: true`` shape this field exists to prevent. When every requested leg
    produced nothing the packet has no simc output at all: ``all_handoffs_failed``, an error.
    """
    if returned_build_count == 0:
        return "no_build_references"
    if len(empty_requested_legs) == requested_leg_count:
        return "all_handoffs_failed"
    if empty_requested_legs:
        return "failed"
    return "partial" if partial_requested_legs else "ok"


def _bundle_health(bundle_inputs: list[tuple[Path, dict[str, Any]]]) -> dict[str, Any]:
    """Pages the exports could not fetch, so a handoff built from a partial bundle says so.

    ``load_article_bundle`` reports the pages a guide export failed on; a build set read from a
    bundle that lost pages is incomplete evidence, not a complete answer.
    """
    rows: list[tuple[str, Any, int]] = [
        (
            str(bundle_path),
            as_dict(bundle.get("manifest")).get("provider"),
            len(as_list(bundle.get("failed_pages"))),
        )
        for bundle_path, bundle in bundle_inputs
    ]
    return {
        "bundle_count": len(rows),
        "failed_page_count": sum(failed for _path, _provider, failed in rows),
        "bundles": [
            {"bundle_path": path, "provider": provider, "failed_page_count": failed}
            for path, provider, failed in rows
        ],
    }


def _handoff_citations(
    selected_rows: list[dict[str, Any]],
    bundle_inputs: list[tuple[Path, dict[str, Any]]],
) -> dict[str, Any]:
    return {
        "bundle_paths": [str(path) for path, _bundle in bundle_inputs],
        "build_reference_urls": _unique_non_empty_strings(
            [((row.get("reference") or {}).get("url")) for row in selected_rows if isinstance(row, dict)]
        ),
        "source_urls": _unique_non_empty_strings(
            [
                url
                for row in selected_rows
                if isinstance(row, dict)
                for source_row in (row.get("sources") or [])
                if isinstance(source_row, dict)
                for url in (source_row.get("source_urls") or [])
            ]
        ),
    }


def _guide_builds_simc_payload(
    *,
    source_path: Path,
    source_kind: str,
    source_manifest: dict[str, Any] | None,
    bundle_inputs: list[tuple[Path, dict[str, Any]]],
    decode: bool,
    apl_path: str | None,
    limit: int,
    expansion: str | None,
) -> dict[str, Any]:
    handoff_rows = _collect_build_reference_handoff_rows(bundle_inputs)
    selected_rows = handoff_rows[:limit]
    bundle_health = _bundle_health(bundle_inputs)
    build_rows: list[dict[str, Any]] = []
    source_providers = sorted(
        {
            provider
            for _bundle_path, bundle in bundle_inputs
            for provider in [((bundle.get("manifest") or {}).get("provider") if isinstance(bundle.get("manifest"), dict) else None)]
            if isinstance(provider, str) and provider
        }
    )
    excluded_rows: list[dict[str, Any]] = []
    for row in selected_rows:
        build_row = _build_simc_handoff_row(row, decode=decode, apl_path=apl_path, expansion=expansion)
        (excluded_rows if build_row["status"] == "excluded" else build_rows).append(build_row)

    identify_success_count, decode_success_count, describe_success_count = _count_simc_handoff_successes(
        build_rows
    )
    requested_legs = [
        (leg, success_count)
        for leg, requested, success_count in (
            ("identify", True, identify_success_count),
            ("decode", decode, decode_success_count),
            ("describe", bool((apl_path or "").strip()), describe_success_count),
        )
        if requested
    ]
    empty_requested_legs = [leg for leg, success_count in requested_legs if success_count == 0]
    partial_requested_legs = [
        leg for leg, success_count in requested_legs if 0 < success_count < len(build_rows)
    ]
    return {
        "provider": "warcraft",
        "kind": "guide_builds_simc_handoff",
        "source": {
            "path": str(source_path),
            "kind": source_kind,
            "manifest_kind": source_manifest.get("kind") if isinstance(source_manifest, dict) else None,
            "query": source_manifest.get("query") if isinstance(source_manifest, dict) else None,
        },
        "provenance": {
            "explicit_build_reference_only": True,
            "selection_contract": "embedded_build_references_only",
            "source_providers": source_providers,
        },
        "freshness": _guide_build_handoff_freshness(source_kind, source_manifest),
        "citations": _handoff_citations(selected_rows, bundle_inputs),
        "bundle_count": len(bundle_inputs),
        "bundle_health": bundle_health,
        "build_reference_count": len(handoff_rows),
        "truncated": len(handoff_rows) > len(selected_rows),
        "decode_enabled": decode,
        "apl_path": apl_path,
        "summary": {
            "returned_build_count": len(build_rows),
            "excluded_build_count": max(0, len(handoff_rows) - len(selected_rows)) + len(excluded_rows),
            "identify_success_count": identify_success_count,
            "decode_success_count": decode_success_count,
            "describe_success_count": describe_success_count,
            # Which requested legs produced nothing at all (`failed`) and which worked for only
            # some builds (`partial`), so the status always names its own cause.
            "empty_requested_legs": empty_requested_legs,
            "partial_requested_legs": partial_requested_legs,
            "failed_page_count": bundle_health["failed_page_count"],
            "simc_handoff_status": _simc_handoff_status(
                returned_build_count=len(build_rows),
                requested_leg_count=len(requested_legs),
                empty_requested_legs=empty_requested_legs,
                partial_requested_legs=partial_requested_legs,
            ),
        },
        "excluded_builds": excluded_rows,
        "builds": build_rows,
    }


# Wowhead class path segments that precede /talent-calc in a bare (non-URL) reference.
_WOWHEAD_CLASS_SLUGS = frozenset(
    {
        "deathknight",
        "death-knight",
        "demonhunter",
        "demon-hunter",
        "druid",
        "evoker",
        "hunter",
        "mage",
        "monk",
        "paladin",
        "priest",
        "rogue",
        "shaman",
        "warlock",
        "warrior",
    }
)
# Expansion path prefixes Wowhead puts in front of a talent-calc path.
_WOWHEAD_EXPANSION_PREFIXES = wowhead_path_prefixes()


def _wowhead_url_targets_talent_calc(url: str) -> bool:
    parsed = urlparse(url)
    hostname = parsed.hostname.lower() if isinstance(parsed.hostname, str) else ""
    path_parts = [part for part in parsed.path.split("/") if part]
    return (hostname == "wowhead.com" or hostname.endswith(".wowhead.com")) and "talent-calc" in path_parts


def _talent_calc_path_parts(text: str) -> list[str]:
    """Path segments of a bare Wowhead reference with any leading expansion prefix removed."""
    parts = [part for part in text.split("/") if part]
    if parts and parts[0] in _WOWHEAD_EXPANSION_PREFIXES:
        parts = parts[1:]
    return parts


def _looks_like_wowhead_talent_calc_reference(value: str) -> bool:
    text = value.strip()
    if not text:
        return False
    lowered = text.lower()
    if "://" in text:
        return _wowhead_url_targets_talent_calc(text)
    if lowered.startswith(("www.wowhead.com/", "wowhead.com/")):
        return _wowhead_url_targets_talent_calc(f"https://{text}")
    parts = _talent_calc_path_parts(text)
    if not parts:
        return False
    if parts[0].strip() in _WOWHEAD_CLASS_SLUGS or parts[0].strip() == "talent-calc":
        return True
    return len(parts) >= 2 and parts[1].strip() == "talent-calc"


def _looks_like_warcraftlogs_report_reference(value: str) -> bool:
    text = value.strip()
    if not text:
        return False
    if "warcraftlogs.com/reports/" in text:
        return True
    return 8 <= len(text) <= 32 and text.isalnum() and any(ch.isalpha() for ch in text)


def _normalize_warcraftlogs_report_reference(value: str) -> str:
    text = value.strip()
    if not text:
        return text
    parsed = urlparse(text)
    if parsed.scheme or parsed.netloc:
        return text
    if text.startswith(("warcraftlogs.com/", "www.warcraftlogs.com/")):
        return f"https://{text}"
    return text


def _looks_like_transport_packet_path_input(value: str) -> bool:
    text = value.strip()
    if not text:
        return False
    lowered = text.lower()
    if lowered.endswith(".json"):
        return True
    if text.startswith(("./", "../", "~/")) or "\\" in text:
        return True
    return "/" in text and "://" not in text and not _looks_like_wowhead_talent_calc_reference(text)


def _load_transport_packet_file(source: str) -> tuple[dict[str, Any], str] | None:
    path = Path(source).expanduser()
    if not path.exists() or not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid talent transport packet file {path}: {exc}") from exc
    try:
        packet = validate_talent_transport_packet(payload)
    except ValueError as exc:
        raise ValueError(f"Invalid talent transport packet file {path}: {exc}") from exc
    return packet, str(path.resolve())


def _write_transport_packet(path_value: str, packet: dict[str, Any]) -> str:
    output_path = Path(path_value).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(packet, indent=2) + "\n", encoding="utf-8")
    return str(output_path)


def _write_transport_packet_or_fail(
    ctx: typer.Context,
    *,
    path_value: str | None,
    packet: dict[str, Any],
    source: str,
    kind: str,
    route: dict[str, Any] | None = None,
    provider_result: dict[str, Any] | None = None,
) -> str | None:
    if not isinstance(path_value, str) or not path_value.strip():
        return None
    try:
        return _write_transport_packet(path_value, packet)
    except OSError as exc:
        _fail_talent_route(
            ctx,
            code="transport_packet_write_failed",
            message=f"Failed to write talent transport packet: {exc}",
            source=source,
            kind=kind,
            route=route,
            provider_result=provider_result,
        )


def _stable_transport_packet_path(
    *,
    route: dict[str, Any],
    written_packet_path: str | None,
    upgraded: bool,
) -> str | None:
    if isinstance(written_packet_path, str) and written_packet_path.strip():
        return written_packet_path
    if upgraded:
        return None
    packet_path = route.get("packet_path")
    return packet_path if isinstance(packet_path, str) and packet_path.strip() else None


def _normalize_simc_transport_packet_path(
    result: dict[str, Any],
    *,
    stable_packet_path: str | None,
) -> dict[str, Any]:
    """Point ``data.build_spec.transport_packet.path`` at the stable packet (or drop a temporary one)."""
    payload = result.get("payload")
    if not isinstance(payload, dict):
        return result
    data = as_dict(payload.get("data"))
    build_spec = _normalize_build_spec_packet_path(data.get("build_spec"), stable_packet_path=stable_packet_path)
    if build_spec is None:
        return result
    return {**result, "payload": {**payload, "data": {**data, "build_spec": build_spec}}}


def _normalize_build_spec_packet_path(build_spec: Any, *, stable_packet_path: str | None) -> dict[str, Any] | None:
    if not isinstance(build_spec, dict):
        return None
    transport_packet = build_spec.get("transport_packet")
    if not isinstance(transport_packet, dict):
        return None
    normalized_build_spec = dict(build_spec)
    source_notes = build_spec.get("source_notes")
    if isinstance(source_notes, list):
        normalized_source_notes: list[str] = []
        for note in source_notes:
            if not isinstance(note, str):
                continue
            if not note.startswith("build packet: "):
                normalized_source_notes.append(note)
                continue
            if stable_packet_path is not None:
                normalized_source_notes.append(f"build packet: {stable_packet_path}")
        normalized_build_spec["source_notes"] = normalized_source_notes
    normalized_transport_packet = dict(transport_packet)
    if stable_packet_path is not None:
        normalized_transport_packet["path"] = stable_packet_path
    else:
        normalized_transport_packet.pop("path", None)
    normalized_build_spec["transport_packet"] = normalized_transport_packet
    return normalized_build_spec


def _normalize_upgrade_result_build_packet_path(
    upgrade_result: dict[str, Any] | None,
    *,
    stable_packet_path: str | None,
) -> dict[str, Any] | None:
    if not isinstance(upgrade_result, dict):
        return upgrade_result
    payload = upgrade_result.get("payload")
    data = as_dict(payload.get("data")) if isinstance(payload, dict) else {}
    if not isinstance(payload, dict) or not isinstance(data.get("input"), dict):
        return upgrade_result
    # The temporary packet file simc read is gone by now, so its path is not evidence.
    trimmed_input = {key: value for key, value in data["input"].items() if key != "build_packet"}
    return {**upgrade_result, "payload": {**payload, "data": {**data, "input": trimmed_input}}}


def _invoke_simc_with_transport_packet(
    packet: dict[str, Any],
    args: list[str],
    *,
    expansion: str | None,
    prefix: str,
) -> dict[str, Any]:
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        suffix=".json",
        prefix=prefix,
        delete=False,
    ) as handle:
        json.dump(packet, handle, indent=2)
        handle.write("\n")
        packet_path = Path(handle.name).resolve()
    try:
        return provider_invoke("simc", [*args, "--build-packet", str(packet_path)], expansion=expansion)
    finally:
        packet_path.unlink(missing_ok=True)


def _upgrade_transport_packet_with_simc(
    packet: dict[str, Any],
    *,
    expansion: str | None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    result = _invoke_simc_with_transport_packet(
        packet,
        ["validate-talent-transport"],
        expansion=expansion,
        prefix="warcraft-talent-packet-",
    )
    if _provider_result_failed(result):
        return result, None
    payload = provider_payload_data(result.get("payload"))
    updated_packet = payload.get("updated_packet") if isinstance(payload.get("updated_packet"), dict) else None
    if updated_packet is None:
        return result, None
    return result, validate_talent_transport_packet(updated_packet)


_TRANSPORT_STATUS_RANKS = {"unknown": 0, "raw_only": 1, "validated": 2, "exact": 3}


def _transport_status_rank(status: str | None) -> int:
    return _TRANSPORT_STATUS_RANKS.get(status or "", -1)


def _non_empty_transport_form_names(packet: dict[str, Any]) -> set[str]:
    transport_forms = packet.get("transport_forms")
    if not isinstance(transport_forms, dict):
        return set()
    form_names: set[str] = set()
    for name, value in transport_forms.items():
        if not isinstance(name, str) or not name.strip():
            continue
        if isinstance(value, str):
            if value.strip():
                form_names.add(name)
            continue
        if value:
            form_names.add(name)
    return form_names


def _transport_packet_upgraded(previous_packet: dict[str, Any], updated_packet: dict[str, Any]) -> bool:
    previous_status = previous_packet.get("transport_status") if isinstance(previous_packet.get("transport_status"), str) else None
    updated_status = updated_packet.get("transport_status") if isinstance(updated_packet.get("transport_status"), str) else None
    if _transport_status_rank(updated_status) > _transport_status_rank(previous_status):
        return True
    previous_forms = _non_empty_transport_form_names(previous_packet)
    updated_forms = _non_empty_transport_form_names(updated_packet)
    return updated_forms > previous_forms


def _describe_transport_packet_with_simc(
    packet: dict[str, Any],
    *,
    expansion: str | None,
    apl_path: str | None,
    targets: int,
    aoe_targets: int,
    list_name: str,
    priority_limit: int,
    inactive_limit: int,
) -> dict[str, Any]:
    args = [
        "describe-build",
        "--targets",
        str(targets),
        "--aoe-targets",
        str(aoe_targets),
        "--list",
        list_name,
        "--priority-limit",
        str(priority_limit),
        "--inactive-limit",
        str(inactive_limit),
    ]
    if isinstance(apl_path, str) and apl_path.strip():
        args.extend(["--apl-path", apl_path])
    return _invoke_simc_with_transport_packet(
        packet,
        args,
        expansion=expansion,
        prefix="warcraft-talent-describe-",
    )


def _fail_talent_route(
    ctx: typer.Context,
    *,
    code: str,
    message: str,
    source: str,
    kind: str,
    route: dict[str, Any] | None = None,
    provider_result: dict[str, Any] | None = None,
) -> NoReturn:
    payload: dict[str, Any] = {
        "ok": False,
        "error": {"code": code, "message": message},
        "provider": "warcraft",
        "kind": kind,
        "source": source,
    }
    if route is not None:
        payload["route"] = route
    if provider_result is not None:
        payload["provider_result"] = provider_result
    _emit(ctx, payload, err=True)
    raise typer.Exit(1)


def _transport_packet_from_provider_result(
    ctx: typer.Context,
    *,
    source: str,
    route: dict[str, Any],
    provider_result: dict[str, Any],
    command_name: str,
    kind: str,
) -> dict[str, Any]:
    producer_payload = provider_payload_data(provider_result.get("payload"))
    provider_name = route.get("provider")
    provider_label = provider_name if isinstance(provider_name, str) and provider_name else "provider"
    if _provider_result_failed(provider_result):
        error_payload = _provider_error_payload(provider_label, provider_result)
        _fail_talent_route(
            ctx,
            code=str(error_payload.get("code") or "provider_command_failed"),
            message=str(error_payload.get("message") or f"{command_name} failed."),
            source=source,
            kind=kind,
            route=route,
            provider_result=provider_result,
        )
    packet_value = producer_payload.get("talent_transport_packet")
    packet = as_dict(packet_value)
    if not packet:
        _fail_talent_route(
            ctx,
            code="missing_transport_packet",
            message=f"{command_name} did not return a talent transport packet.",
            source=source,
            kind=kind,
            route=route,
            provider_result=provider_result,
        )
    try:
        return validate_talent_transport_packet(packet)
    except ValueError as exc:
        _fail_talent_route(
            ctx,
            code="invalid_transport_packet",
            message=f"{command_name} returned an invalid talent transport packet: {exc}",
            source=source,
            kind=kind,
            route=route,
            provider_result=provider_result,
        )


def _wowhead_transport_packet(
    ctx: typer.Context,
    *,
    source: str,
    listed_build_limit: int,
    requested_expansion: str | None,
    kind: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    route = {"kind": "wowhead_talent_calc", "provider": "wowhead"}
    producer_result = provider_invoke(
        "wowhead",
        ["talent-calc-packet", source, "--listed-build-limit", str(listed_build_limit)],
        expansion=requested_expansion,
    )
    packet = _transport_packet_from_provider_result(
        ctx,
        source=source,
        route=route,
        provider_result=producer_result,
        command_name="wowhead talent-calc-packet",
        kind=kind,
    )
    return route, producer_result, packet


def _warcraftlogs_transport_packet(
    ctx: typer.Context,
    *,
    source: str,
    actor_id: int,
    fight_id: int | None,
    allow_unlisted: bool,
    requested_expansion: str | None,
    kind: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    normalized_source = _normalize_warcraftlogs_report_reference(source)
    route = {
        "kind": "warcraftlogs_report_actor",
        "provider": "warcraftlogs",
        "actor_id": actor_id,
        "fight_id": fight_id,
        "allow_unlisted": allow_unlisted,
    }
    args = ["report-player-talents", normalized_source, "--actor-id", str(actor_id)]
    if fight_id is not None:
        args.extend(["--fight-id", str(fight_id)])
    if allow_unlisted:
        args.append("--allow-unlisted")
    producer_result = provider_invoke("warcraftlogs", args, expansion=requested_expansion)
    packet = _transport_packet_from_provider_result(
        ctx,
        source=source,
        route=route,
        provider_result=producer_result,
        command_name="warcraftlogs report-player-talents",
        kind=kind,
    )
    return route, producer_result, packet


def _maybe_upgrade_transport_packet(
    ctx: typer.Context,
    *,
    source: str,
    route: dict[str, Any],
    packet: dict[str, Any],
    validate: bool,
    requested_expansion: str | None,
    kind: str,
) -> tuple[str | None, bool, bool, dict[str, Any] | None, dict[str, Any]]:
    source_status = packet.get("transport_status") if isinstance(packet.get("transport_status"), str) else None
    upgrade_result: dict[str, Any] | None = None
    packet_changed = False
    upgrade_attempted = bool(validate and source_status == "raw_only")
    if upgrade_attempted:
        original_packet = packet
        try:
            upgrade_result, upgraded_packet = _upgrade_transport_packet_with_simc(packet, expansion=requested_expansion)
        except ValueError as exc:
            _fail_talent_route(
                ctx,
                code="packet_upgrade_failed",
                message=f"simc validate-talent-transport returned an invalid upgraded packet: {exc}",
                source=source,
                kind=kind,
                route=route,
            )
        if _provider_result_failed(upgrade_result):
            error_payload = _provider_error_payload("simc", upgrade_result)
            _fail_talent_route(
                ctx,
                code=str(error_payload.get("code") or "packet_upgrade_failed"),
                message=str(error_payload.get("message") or "simc validate-talent-transport failed while upgrading the packet."),
                source=source,
                kind=kind,
                route=route,
                provider_result=upgrade_result,
            )
        if upgraded_packet is None:
            _fail_talent_route(
                ctx,
                code="packet_upgrade_failed",
                message="simc validate-talent-transport did not return an upgraded talent transport packet.",
                source=source,
                kind=kind,
                route=route,
                provider_result=upgrade_result,
            )
        packet = upgraded_packet
        packet_changed = _transport_packet_upgraded(original_packet, packet)
    return source_status, upgrade_attempted, packet_changed, upgrade_result, packet


def _resolve_talent_transport(
    ctx: typer.Context,
    *,
    source: str,
    actor_id: int | None,
    fight_id: int | None,
    allow_unlisted: bool,
    listed_build_limit: int,
    validate: bool,
    kind: str = "talent_transport",
) -> dict[str, Any]:
    requested_expansion = _requested_expansion(ctx)
    route: dict[str, Any]
    producer_result: dict[str, Any] | None = None
    try:
        packet_file = _load_transport_packet_file(source)
    except ValueError as exc:
        _fail_talent_route(
            ctx,
            code="invalid_transport_packet",
            message=str(exc),
            source=source,
            kind=kind,
        )
    if packet_file is not None:
        packet, packet_path = packet_file
        route = {
            "kind": "packet_file",
            "provider": None,
            "packet_path": packet_path,
        }
    elif source.strip().lower().endswith(".json") and _looks_like_transport_packet_path_input(source):
        _fail_talent_route(
            ctx,
            code="invalid_transport_packet",
            message=f"Talent transport packet file was not found: {source}",
            source=source,
            kind=kind,
        )
    elif _looks_like_wowhead_talent_calc_reference(source):
        route, producer_result, packet = _wowhead_transport_packet(
            ctx,
            source=source,
            listed_build_limit=listed_build_limit,
            requested_expansion=requested_expansion,
            kind=kind,
        )
    elif actor_id is not None and _looks_like_warcraftlogs_report_reference(source):
        route, producer_result, packet = _warcraftlogs_transport_packet(
            ctx,
            source=source,
            actor_id=actor_id,
            fight_id=fight_id,
            allow_unlisted=allow_unlisted,
            requested_expansion=requested_expansion,
            kind=kind,
        )
    elif _looks_like_transport_packet_path_input(source):
        _fail_talent_route(
            ctx,
            code="invalid_transport_packet",
            message=f"Talent transport packet file was not found: {source}",
            source=source,
            kind=kind,
        )
    else:
        _fail_talent_route(
            ctx,
            code="unsupported_talent_source",
            message=(
                "Use an explicit Wowhead talent-calc ref, an explicit Warcraft Logs report ref with --actor-id, "
                "or a local talent transport packet JSON path."
            ),
            source=source,
            kind=kind,
        )

    source_status, upgrade_attempted, packet_changed, upgrade_result, packet = _maybe_upgrade_transport_packet(
        ctx,
        source=source,
        route=route,
        packet=packet,
        validate=validate,
        requested_expansion=requested_expansion,
        kind=kind,
    )
    return {
        "source": source,
        "route": route,
        "requested_expansion": requested_expansion,
        "source_packet_status": source_status,
        "upgrade_attempted": upgrade_attempted,
        "upgraded": packet_changed,
        "producer_result": producer_result,
        "upgrade_result": upgrade_result,
        "talent_transport_packet": packet,
    }


def _normalize_guide_compare_providers(values: list[str]) -> tuple[str, ...]:
    selected = [value.strip() for raw in values for value in raw.split(",") if value.strip()]
    if not selected:
        return GUIDE_COMPARE_QUERY_PROVIDERS
    invalid = sorted(provider for provider in selected if provider not in GUIDE_COMPARE_QUERY_PROVIDERS)
    if invalid:
        supported = ", ".join(GUIDE_COMPARE_QUERY_PROVIDERS)
        raise ValueError(
            f"Unsupported guide comparison providers: {', '.join(invalid)}. Supported providers: {supported}."
        )
    deduped: list[str] = []
    for provider in selected:
        if provider not in deduped:
            deduped.append(provider)
    return tuple(deduped)


def _resolved_guide_match(provider: str, payload: dict[str, Any] | None) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(payload, dict):
        return None, "missing_payload"
    if not payload.get("resolved"):
        return None, "provider_did_not_resolve_query"
    match = payload.get("match")
    if not isinstance(match, dict):
        return None, "missing_resolved_match"
    entity_type = match.get("entity_type")
    if entity_type != "guide":
        return None, f"resolved_non_guide:{entity_type}"
    raw_ref = match.get("id")
    if raw_ref is None:
        metadata = match.get("metadata")
        if isinstance(metadata, dict):
            raw_ref = metadata.get("slug")
    if raw_ref is None:
        return None, "resolved_guide_missing_ref"
    ref = str(raw_ref)
    return {
        "provider": provider,
        "ref": ref,
        "name": match.get("name"),
        "url": match.get("url"),
        "confidence": payload.get("confidence"),
        "next_command": payload.get("next_command"),
        "selection_source": "resolve",
    }, None


# Thresholds for accepting a search top hit as a guide selection when resolve did not fire.
_SEARCH_FALLBACK_MIN_TOP_SCORE = 50
_SEARCH_FALLBACK_MIN_MARGIN = 25
_SEARCH_FALLBACK_MIN_SINGLE_SCORE = 70


def _top_guide_result(payload: dict[str, Any] | None) -> tuple[list[dict[str, Any]] | None, str | None]:
    """The search result rows, only when the top row is a usable guide candidate."""
    if not isinstance(payload, dict):
        return None, "missing_search_payload"
    results = payload.get("results")
    if not isinstance(results, list) or not results:
        return None, "provider_search_returned_no_results"
    top = results[0]
    if not isinstance(top, dict):
        return None, "invalid_search_top_candidate"
    if top.get("entity_type") != "guide":
        return None, f"search_top_non_guide:{top.get('entity_type')}"
    return [row for row in results if isinstance(row, dict)], None


def _search_scores(results: list[dict[str, Any]]) -> tuple[int, int | None]:
    """Ranking scores of the top row and its runner-up (``None`` when there is only one row)."""

    def score(row: dict[str, Any]) -> int:
        ranking = row.get("ranking")
        return int(ranking.get("score") or 0) if isinstance(ranking, dict) else 0

    return score(results[0]), score(results[1]) if len(results) > 1 else None


def _search_fallback_rejection(top_score: int, second_score: int | None) -> str | None:
    if top_score < _SEARCH_FALLBACK_MIN_TOP_SCORE:
        return f"search_top_guide_score_too_low:{top_score}"
    if second_score is not None and top_score < second_score + _SEARCH_FALLBACK_MIN_MARGIN:
        return "search_results_not_decisive"
    if second_score is None and top_score < _SEARCH_FALLBACK_MIN_SINGLE_SCORE:
        return "search_single_result_not_strong_enough"
    return None


def _search_candidate_ref(top: dict[str, Any]) -> str | None:
    raw_ref = top.get("id")
    if raw_ref is None:
        metadata = top.get("metadata")
        if isinstance(metadata, dict):
            raw_ref = metadata.get("slug")
    return None if raw_ref is None else str(raw_ref)


def _search_fallback_guide_match(
    provider: str,
    payload: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, str | None]:
    results, reason = _top_guide_result(payload)
    if results is None:
        return None, reason
    top = results[0]
    top_score, second_score = _search_scores(results)
    rejection = _search_fallback_rejection(top_score, second_score)
    if rejection is not None:
        return None, rejection
    ref = _search_candidate_ref(top)
    if ref is None:
        return None, "search_guide_missing_ref"
    follow_up = top.get("follow_up")
    return {
        "provider": provider,
        "ref": ref,
        "name": top.get("name"),
        "url": top.get("url"),
        "confidence": "medium",
        "next_command": follow_up.get("command") if isinstance(follow_up, dict) else None,
        "selection_source": "search_fallback",
        "search_ranking": top.get("ranking"),
        "selection_contract": {
            "rule": "top_guide_result_with_strong_score_and_clear_margin",
            "top_score": top_score,
            "second_score": second_score,
            "minimum_top_score": _SEARCH_FALLBACK_MIN_TOP_SCORE,
            "minimum_margin_over_runner_up": _SEARCH_FALLBACK_MIN_MARGIN if second_score is not None else None,
            "single_result_minimum_score": None if second_score is not None else _SEARCH_FALLBACK_MIN_SINGLE_SCORE,
        },
    }, None


def _provider_outcome(payload: Any) -> dict[str, Any]:
    """Per-provider fanout outcome: ``status`` is registry readiness, this is what the call did."""
    if not isinstance(payload, dict):
        return {"ok": False, "error": {"code": "missing_provider_payload", "message": "Provider returned no JSON payload."}}
    error = payload.get("error")
    return {"ok": bool(payload.get("ok", True)), "error": error if isinstance(error, dict) else None}


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
    return bool(as_list(data.get("results")) or data.get("resolved"))


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
            }
        )
    return rows


def _fanout_failure_error(failed_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Top-level error for a fanout where no provider answered and at least one failed.

    The code is the providers' shared failure code when they agree so the exit code the contract
    derives from ``error.code`` stays true; a mixed set of failures degrades to ``upstream_error``.
    """
    codes = {row["code"] for row in failed_rows if isinstance(row.get("code"), str)}
    code = codes.pop() if len(codes) == 1 else "upstream_error"
    return {
        "code": code,
        "message": (
            f"No provider answered: {len(failed_rows)} providers failed and no other included provider "
            "searched this query."
        ),
        "details": {"failed_providers": failed_rows},
    }


def _unresolved_next_steps(query: str, providers: list[dict[str, Any]], *, resolved: bool) -> dict[str, Any]:
    """What an agent should do next when no provider resolved the query.

    Providers that decline to resolve still report a best candidate and their own
    ``fallback_search_command``; without these the wrapper's resolve is a dead end even when a
    provider clearly found the thing.
    """
    if resolved:
        return {"fallback_search_command": None, "fallback_search_commands": [], "best_unresolved_candidate": None}
    fallbacks: list[dict[str, Any]] = []
    candidates: list[tuple[str, dict[str, Any]]] = []
    for provider_row in providers:
        provider_data = provider_payload_data(provider_row.get("payload"))
        provider_name = str(provider_row.get("provider") or "")
        command = provider_data.get("fallback_search_command")
        if isinstance(command, str) and command.strip():
            fallbacks.append({"provider": provider_name, "command": command})
        if isinstance(provider_data.get("match"), dict):
            candidates.append((provider_name, decorate_resolve_payload(query, provider_name, provider_data)))
    candidates.sort(key=lambda row: resolve_payload_sort_key(row[0], row[1]))
    best = compact_resolve_match(candidates[0][1]) if candidates else None
    if best is not None:
        best["provider"] = candidates[0][0]
        best["resolved"] = False
    return {
        "fallback_search_command": fallbacks[0]["command"] if fallbacks else None,
        "fallback_search_commands": fallbacks,
        "best_unresolved_candidate": best,
    }


def _fanout_health(providers: list[dict[str, Any]]) -> dict[str, Any]:
    """Answered/failed counts plus the failure rows, so partial and total failure are never silent."""
    failed_rows = _failed_provider_rows(providers)
    return {
        "answered_provider_count": sum(1 for row in providers if row["answered"]),
        "failed_provider_count": len(failed_rows),
        "failed_providers": failed_rows,
    }


def _emit_fanout(ctx: typer.Context, payload: dict[str, Any]) -> None:
    """Emit a search/resolve payload, failing with the providers' own error when none answered."""
    failed_rows = payload["failed_providers"]
    if failed_rows and not payload["answered_provider_count"]:
        error = _fanout_failure_error(failed_rows)
        _emit(ctx, {"ok": False, "query": payload["query"], "error": error}, err=True)
        raise typer.Exit(exit_code_for(str(error["code"]), EXIT_NETWORK))
    _emit(ctx, payload)


def _raiderio_source(identity: dict[str, str], *, expansion: str | None) -> dict[str, Any]:
    result = _provider_payload_result(
        "raiderio",
        ["guild", identity["region"], identity["realm"], identity["name"]],
        expansion=expansion,
    )
    if result.get("status") != "ok":
        return result
    return {
        **result,
        "summary": raiderio_guild_summary(provider_payload_data(result.get("payload"))),
    }


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
    for registration in included_registrations:
        result = provider_search(registration.name, query, limit=limit, expansion=requested_expansion)
        provider_payload = result.get("payload")
        provider_row = {
            "provider": registration.name,
            "status": registration.status,
            **_provider_outcome(provider_payload),
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
                            "provider": registration.name,
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
        "count": len(flattened),
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
    limit: int = typer.Option(5, "--limit", min=1, max=50, help="Maximum provider-local candidates to request."),
    brief: bool = typer.Option(
        False,
        "--brief",
        help="Return a smaller wrapper payload: a compact match summary and no per-provider payloads.",
    ),
    ranking_debug: bool = typer.Option(False, "--ranking-debug", help="Include compact wrapper ranking summaries for resolved candidates."),
    expansion_debug: bool = typer.Option(
        False,
        "--expansion-debug",
        help="Include a compact expansion support snapshot for all providers.",
    ),
) -> None:
    """Fan out a query to every resolve-ready provider and return the single best match plus its follow-up command."""
    requested_expansion = _requested_expansion(ctx)
    expansion_included, excluded_providers = expansion_filtered_providers(requested_expansion=requested_expansion)
    included_registrations, surface_excluded = surface_filtered_providers(
        expansion_included,
        surface="resolve",
        requested_expansion=requested_expansion,
    )
    excluded_providers = [*excluded_providers, *surface_excluded]
    providers: list[dict[str, Any]] = []
    resolved_candidates: list[tuple[str, dict[str, Any]]] = []
    for registration in included_registrations:
        result = provider_resolve(registration.name, query, limit=limit, expansion=requested_expansion)
        provider_payload = result.get("payload")
        provider_row = {
            "provider": registration.name,
            "status": registration.status,
            **_provider_outcome(provider_payload),
            "expansion_support": provider_expansion_support(
                registration,
                requested_expansion=requested_expansion,
            ),
            "payload": provider_payload,
        }
        provider_row["answered"] = _provider_answered(registration, "resolve", provider_row)
        providers.append(provider_row)
        resolve_data = provider_payload_data(provider_payload)
        if resolve_data.get("resolved"):
            resolved_candidates.append(
                (registration.name, decorate_resolve_payload(query, registration.name, resolve_data))
            )
    resolved_candidates.sort(key=lambda row: resolve_payload_sort_key(row[0], row[1]))
    best_provider = resolved_candidates[0][0] if resolved_candidates else None
    best_payload = resolved_candidates[0][1] if resolved_candidates else None
    match = compact_resolve_match(best_payload) if brief else (best_payload.get("match") if isinstance(best_payload, dict) else None)
    payload: dict[str, Any] = {
        "query": query,
        "provider_count": len(list_providers()),
        "requested_expansion": requested_expansion,
        "expansion_filter_active": requested_expansion is not None,
        "included_providers": [registration.name for registration in included_registrations],
        "excluded_providers": excluded_providers,
        "included_provider_count": len(included_registrations),
        "excluded_provider_count": len(excluded_providers),
        "resolved": best_payload is not None,
        # The envelope is attributed to the provider whose match it carries; `selected_provider` is
        # the same choice, nullable, inside data.
        "provider": best_provider or "warcraft",
        "selected_provider": best_provider,
        "match": match,
        "next_command": best_payload.get("next_command") if isinstance(best_payload, dict) else None,
        "confidence": best_payload.get("confidence") if isinstance(best_payload, dict) else None,
        **_fanout_health(providers),
        **_unresolved_next_steps(query, providers, resolved=best_payload is not None),
        "providers": [] if brief else providers,
    }
    if ranking_debug:
        payload["ranking_debug"] = [compact_resolve_match(row[1])
                                    for row in resolved_candidates[:limit] if compact_resolve_match(row[1]) is not None]
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


@app.command("guild-ranks")
def guild_ranks(
    ctx: typer.Context,
    region: str = typer.Argument(..., help="Region slug such as us or eu."),
    realm: str = typer.Argument(..., help="Realm title or slug."),
    name: str = typer.Argument(..., help="Guild name."),
) -> None:
    """Report a guild's per-raid progression with normal/heroic/mythic world, region, and realm ranks from Raider.IO."""
    identity = normalized_identity(region, realm, name)
    source_result = _provider_payload_result(
        "raiderio",
        ["guild", identity["region"], identity["realm"], identity["name"]],
        expansion=_requested_expansion(ctx),
    )
    if source_result.get("status") != "ok":
        _emit(ctx, {"ok": False, "error": source_result.get("error"), "query": identity, "source": "raiderio"}, err=True)
        raise typer.Exit(source_exit_code(source_result))
    payload = provider_payload_data(source_result.get("payload"))
    raids = guild_rank_rows(payload)
    _emit(ctx,
        {
            "ok": True,
            "provider": "warcraft",
            "kind": "guild_ranks",
            "query": identity,
            "source": "raiderio",
            "guild": payload.get("guild"),
            "count": len(raids),
            "raids": raids,
            "citations": payload.get("citations"),
            "provider_payload": source_result.get("payload"),
        },
    )


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
    error: dict[str, Any] = {"code": code, "message": message}
    if details:
        error["details"] = details
    payload: dict[str, Any] = {
        "ok": False,
        "provider": "warcraft",
        "kind": "actor_profile_crosswalk",
        "query": query,
        "error": error,
    }
    if sources is not None:
        payload["sources"] = sources
    _emit(ctx, payload, err=True)
    raise typer.Exit(exit_code)


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
) -> tuple[list[int], dict[str, Any]]:
    """The fights an unscoped ``--fight-id`` crosswalk reads, plus the scope it applied.

    Warcraft Logs only answers ``playerDetails`` for an explicit fight list or time window; an
    unscoped query comes back as an empty roster. The crosswalk's whole-report default therefore has
    to enumerate the fights itself rather than omit the slice.
    """
    args = ["report-fights", code]
    if allow_unlisted:
        args.append("--allow-unlisted")
    result = _provider_payload_result("warcraftlogs", args, expansion=expansion)
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
) -> dict[str, Any]:
    """Fetch the Warcraft Logs report-player-details payload the crosswalk reads its actor from."""
    wcl_args = ["report-player-details", code]
    for fight_id in fight_ids:
        wcl_args += ["--fight-id", str(fight_id)]
    if allow_unlisted:
        wcl_args.append("--allow-unlisted")
    log_result = _provider_payload_result("warcraftlogs", wcl_args, expansion=expansion)
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
) -> dict[str, Any]:
    """Fetch the Raider.IO character the report actor soft-matches to."""
    profile_result = _provider_payload_result(
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


@app.command("actor-profile")
def actor_profile(
    ctx: typer.Context,
    code: str = typer.Argument(..., help="Warcraft Logs report code."),
    name: str = typer.Argument(..., help="Character (actor) name within the report."),
    fight_id: int | None = typer.Option(None, "--fight-id", help="Narrow to one fight (makes the log actor identity canonical)."),
    region: str | None = typer.Option(None, "--region", help="Override the actor region for the Raider.IO lookup."),
    allow_unlisted: bool = typer.Option(False, "--allow-unlisted", help="Allow lookup of unlisted Warcraft Logs reports."),
) -> None:
    """Cross-walk a Warcraft Logs report actor to a Raider.IO profile (log actor -> profile handoff)."""
    requested_expansion = _requested_expansion(ctx)
    query: dict[str, Any] = {"report_code": code, "actor_name": name, "fight_id": fight_id}
    scoped_fight_ids, fight_scope = (
        ([fight_id], {"rule": "explicit_fight_id", "scoped_fight_count": 1, "truncated": False})
        if fight_id is not None
        else _actor_profile_fight_ids(
            ctx, query=query, code=code, allow_unlisted=allow_unlisted, expansion=requested_expansion
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
        expansion=requested_expansion,
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
    query.update(identity)
    character = _actor_profile_character(
        ctx,
        query=query,
        identity=identity,
        log_side=log_side,
        expansion=requested_expansion,
    )
    profile_identity = character.get("class_spec_identity")
    _emit(ctx,
        {
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
                    "profile_url": character.get("profile_url"),
                },
            },
            "reconciliation": reconcile_class_spec(actor.get("class_spec_identity"), profile_identity),
        },
    )


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
    spec_slug: str | None = typer.Option(None, "--spec-slug", help="Override Lorrgs spec slug, e.g. mage-frost."),
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
        spec_slug=spec_slug,
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
    if len(bundles) < 2:
        _emit(ctx,
            {
                "ok": False,
                "error": {
                    "code": "invalid_argument",
                    "message": "guide-compare requires at least two exported guide bundles.",
                },
            },
            err=True,
        )
        raise typer.Exit(1)

    bundle_inputs: list[tuple[Path, dict[str, Any]]] = []
    for bundle_path in bundles:
        resolved_path = bundle_path.expanduser()
        if not resolved_path.exists():
            _emit(ctx,
                {
                    "ok": False,
                    "error": {
                        "code": "invalid_bundle",
                        "message": f"Bundle directory not found: {resolved_path}",
                    },
                },
                err=True,
            )
            raise typer.Exit(1)
        try:
            bundle_inputs.append((resolved_path, load_article_bundle(resolved_path)))
        except (ValueError, OSError) as exc:
            _emit(ctx,
                {
                    "ok": False,
                    "error": {
                        "code": "invalid_bundle",
                        "message": str(exc),
                    },
                    "bundle": str(resolved_path),
                },
                err=True,
            )
            raise typer.Exit(1) from exc

    payload = {
        "provider": "warcraft",
        **_guide_comparison_packet(bundle_inputs, max_age_hours=max_age_hours),
    }
    _emit(ctx, payload)


def _resolve_guide_compare_candidate(
    provider_name: str,
    query: str,
    *,
    limit: int,
    expansion: str | None,
) -> tuple[dict[str, Any] | None, str | None, dict[str, Any] | None, dict[str, Any] | None]:
    resolved = provider_resolve(provider_name, query, limit=limit, expansion=expansion)
    candidate, candidate_reason = _resolved_guide_match(
        provider_name,
        provider_payload_data(resolved.get("payload")),
    )
    search_payload: dict[str, Any] | None = None
    if candidate is None:
        searched = provider_search(provider_name, query, limit=limit, expansion=expansion)
        search_payload = searched.get("payload") if isinstance(searched, dict) else None
        fallback_candidate, fallback_reason = _search_fallback_guide_match(
            provider_name, provider_payload_data(search_payload)
        )
        if fallback_candidate is not None:
            candidate = fallback_candidate
            candidate_reason = None
        else:
            candidate_reason = fallback_reason if fallback_reason is not None else candidate_reason
    resolve_payload = resolved.get("payload") if isinstance(resolved, dict) else None
    return candidate, candidate_reason, resolve_payload, search_payload


def _guide_compare_existing_freshness(existing_row: dict[str, Any] | None, *, max_age_hours: int) -> dict[str, Any]:
    if existing_row is None:
        return {"status": "stale", "reason": "missing_manifest_row", "age_hours": None, "max_age_hours": max_age_hours}
    return _guide_compare_freshness(existing_row.get("exported_at"), max_age_hours=max_age_hours)


def _guide_compare_reusable(
    existing_row: dict[str, Any] | None,
    *,
    candidate: dict[str, Any],
    export_dir: Path,
    freshness: dict[str, Any],
    force_refresh: bool,
) -> bool:
    """An exported bundle is reusable only when the manifest row names this candidate and is still fresh."""
    if existing_row is None or force_refresh:
        return False
    same_candidate = (
        str(existing_row.get("candidate_ref") or "") == str(candidate["ref"])
        and str(existing_row.get("bundle_path") or "") == str(export_dir)
    )
    return same_candidate and freshness.get("status") == "fresh" and export_dir.exists()


def _guide_compare_invalid_bundle_row(
    provider_name: str,
    *,
    candidate: dict[str, Any],
    export_dir: Path,
    freshness: dict[str, Any],
    error: str,
) -> dict[str, Any]:
    return {
        "provider": provider_name,
        "status": "error",
        "reason": "invalid_exported_bundle",
        "candidate": candidate,
        "bundle_path": str(export_dir),
        "freshness": freshness,
        "error": error,
    }


def _guide_compare_reuse_row(
    provider_name: str,
    *,
    candidate: dict[str, Any],
    export_dir: Path,
    existing_row: dict[str, Any] | None,
    freshness: dict[str, Any],
) -> tuple[dict[str, Any], tuple[Path, dict[str, Any]] | None]:
    """Load the already-exported bundle instead of re-exporting it."""
    try:
        bundle = load_article_bundle(export_dir)
    except (ValueError, OSError) as exc:
        return _guide_compare_invalid_bundle_row(
            provider_name,
            candidate=candidate,
            export_dir=export_dir,
            freshness=freshness,
            error=str(exc),
        ), None
    return {
        "provider": provider_name,
        "status": "reused",
        "candidate": candidate,
        "bundle_path": str(export_dir),
        "freshness": freshness,
        "exported_at": existing_row.get("exported_at") if existing_row is not None else None,
    }, (export_dir, bundle)


def _guide_compare_export_row(
    provider_name: str,
    *,
    candidate: dict[str, Any],
    export_dir: Path,
    freshness: dict[str, Any],
    expansion: str | None,
    max_age_hours: int,
) -> tuple[dict[str, Any], tuple[Path, dict[str, Any]] | None]:
    """Run the provider's guide-export into ``export_dir`` and load the bundle it wrote."""
    export_result = provider_invoke(
        provider_name,
        ["guide-export", candidate["ref"], "--out", str(export_dir)],
        expansion=expansion,
    )
    if export_result.get("exit_code") != 0:
        return {
            "provider": provider_name,
            "status": "error",
            "reason": "guide_export_failed",
            "candidate": candidate,
            "bundle_path": str(export_dir),
            "freshness": freshness,
            "export": export_result.get("payload"),
        }, None
    try:
        bundle = load_article_bundle(export_dir)
    except (ValueError, OSError) as exc:
        return _guide_compare_invalid_bundle_row(
            provider_name,
            candidate=candidate,
            export_dir=export_dir,
            freshness=freshness,
            error=str(exc),
        ), None

    # exported_at comes from the manifest the provider's guide-export just stamped, so the
    # orchestration row, the reuse check, and _guide_comparison_packet share one timestamp (they
    # cannot disagree on freshness). The `or _iso_now_utc()` is an unreachable safety net, NOT a
    # freshness fabricator: every guide-compare provider stamps exported_at on export (wowhead via
    # _guide_export_manifest, method/icy-veins via write_article_bundle), and this branch runs only
    # after guide-export above re-wrote the bundle now — so "now" would reflect a real just-happened
    # export, never a stale reuse. A timestamp-less bundle is also never *reused*: the reuse gate
    # requires freshness "fresh" and _guide_compare_freshness(None) is always "stale", which forces a
    # re-export (re-stamping a real anchor). So a bundle lacking a real anchor cannot be stamped here
    # and then treated as freshly exported on a later run.
    bundle_manifest = as_dict(bundle.get("manifest"))
    exported_at = bundle_manifest.get("exported_at") or _iso_now_utc()
    return {
        "provider": provider_name,
        "status": "exported",
        "candidate": candidate,
        "bundle_path": str(export_dir),
        "freshness": _guide_compare_freshness(exported_at, max_age_hours=max_age_hours),
        "exported_at": exported_at,
        "export": export_result.get("payload"),
    }, (export_dir, bundle)


def _process_guide_compare_provider(
    provider_name: str,
    *,
    query: str,
    requested_expansion: str | None,
    limit: int,
    max_age_hours: int,
    force_refresh: bool,
    orchestration_root: Path,
    manifest_by_provider: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], tuple[Path, dict[str, Any]] | None]:
    registration = get_provider(provider_name)
    exclusion_reason = provider_expansion_exclusion_reason(
        registration,
        requested_expansion=requested_expansion,
    )
    if exclusion_reason is not None:
        return {
            "provider": provider_name,
            "status": "skipped",
            "reason": exclusion_reason,
            "expansion_support": provider_expansion_support(
                registration,
                requested_expansion=requested_expansion,
            ),
        }, None

    candidate, candidate_reason, resolve_payload, search_payload = _resolve_guide_compare_candidate(
        provider_name,
        query,
        limit=limit,
        expansion=requested_expansion,
    )
    if candidate is None:
        return {
            "provider": provider_name,
            "status": "skipped",
            "reason": candidate_reason,
            "resolve": resolve_payload,
            "search": search_payload,
        }, None

    export_dir = orchestration_root / provider_name
    existing_row = manifest_by_provider.get(provider_name)
    existing_freshness = _guide_compare_existing_freshness(existing_row, max_age_hours=max_age_hours)
    if _guide_compare_reusable(
        existing_row,
        candidate=candidate,
        export_dir=export_dir,
        freshness=existing_freshness,
        force_refresh=force_refresh,
    ):
        return _guide_compare_reuse_row(
            provider_name,
            candidate=candidate,
            export_dir=export_dir,
            existing_row=existing_row,
            freshness=existing_freshness,
        )
    return _guide_compare_export_row(
        provider_name,
        candidate=candidate,
        export_dir=export_dir,
        freshness=existing_freshness,
        expansion=requested_expansion,
        max_age_hours=max_age_hours,
    )


@dataclass(frozen=True, slots=True)
class GuideCompareQueryOptions:
    """One orchestration run of `warcraft guide-compare-query`, after its flags are validated."""

    query: str
    providers: tuple[str, ...]
    orchestration_root: Path
    requested_expansion: str | None
    limit: int
    max_age_hours: int
    force_refresh: bool
    simc_build_handoff: bool
    simc_apl_path: str | None
    simc_decode: bool
    simc_build_limit: int


def _guide_compare_manifest_index(root: Path) -> dict[str, dict[str, Any]]:
    """Provider rows from a previous orchestration manifest, keyed by provider name."""
    manifest = _load_guide_compare_manifest(root) or {}
    rows = manifest.get("providers")
    if not isinstance(rows, list):
        return {}
    return {row["provider"]: row for row in rows if isinstance(row, dict) and isinstance(row.get("provider"), str)}


def _guide_compare_decline_row(provider_row: dict[str, Any]) -> dict[str, Any]:
    """Why one provider did not contribute a bundle, small enough to live inside ``error.details``."""
    return {
        "provider": provider_row.get("provider"),
        "status": provider_row.get("status"),
        "reason": provider_row.get("reason"),
        "candidate_ref": as_dict(provider_row.get("candidate")).get("ref"),
        "bundle_path": provider_row.get("bundle_path"),
    }


def _guide_compare_query_payload(options: GuideCompareQueryOptions) -> dict[str, Any]:
    """Export each selected provider's guide bundle and compare them.

    Returns the payload to emit; ``ok: False`` with ``insufficient_guides`` when fewer than two
    bundles exported, which the command turns into exit 1.
    """
    manifest_by_provider = _guide_compare_manifest_index(options.orchestration_root)
    provider_rows: list[dict[str, Any]] = []
    bundle_inputs: list[tuple[Path, dict[str, Any]]] = []
    for provider_name in options.providers:
        provider_row, bundle_input = _process_guide_compare_provider(
            provider_name,
            query=options.query,
            requested_expansion=options.requested_expansion,
            limit=options.limit,
            max_age_hours=options.max_age_hours,
            force_refresh=options.force_refresh,
            orchestration_root=options.orchestration_root,
            manifest_by_provider=manifest_by_provider,
        )
        provider_rows.append(provider_row)
        if bundle_input is not None:
            bundle_inputs.append(bundle_input)

    if len(bundle_inputs) < 2:
        # The manifest describes a completed comparison, so a failed run writes none: it must not
        # leave a `providers: []` manifest behind for the next run to reuse.
        return {
            "ok": False,
            "query": options.query,
            "error": {
                "code": "insufficient_guides",
                "message": "Need at least two exported guide bundles to compare.",
                "details": {
                    "exported_bundle_count": len(bundle_inputs),
                    "required_bundle_count": 2,
                    "selected_providers": list(options.providers),
                    "provider_results": [_guide_compare_decline_row(row) for row in provider_rows],
                },
            },
        }

    manifest = _write_guide_compare_manifest(
        root=options.orchestration_root,
        query=options.query,
        requested_expansion=options.requested_expansion,
        max_age_hours=options.max_age_hours,
        provider_results=provider_rows,
    )
    include_simc_build_handoff = options.simc_build_handoff or (
        isinstance(options.simc_apl_path, str) and bool(options.simc_apl_path.strip())
    )
    return {
        "provider": "warcraft",
        "kind": "guide_bundle_comparison_orchestration",
        "query": options.query,
        "requested_expansion": options.requested_expansion,
        "selected_providers": list(options.providers),
        "output_root": str(options.orchestration_root),
        "max_age_hours": options.max_age_hours,
        "force_refresh": options.force_refresh,
        "provider_results": provider_rows,
        "exported_bundle_count": len(bundle_inputs),
        "manifest": manifest,
        "comparison": {
            "provider": "warcraft",
            **_guide_comparison_packet(bundle_inputs, max_age_hours=options.max_age_hours),
        },
        "simc_build_handoff": _guide_builds_simc_payload(
            source_path=options.orchestration_root,
            source_kind="orchestration_root",
            source_manifest=manifest,
            bundle_inputs=bundle_inputs,
            decode=options.simc_decode,
            apl_path=options.simc_apl_path,
            limit=options.simc_build_limit,
            expansion=options.requested_expansion,
        )
        if include_simc_build_handoff
        else None,
    }


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
            "Defaults to <XDG data dir>/warcraft/guide_compare/<query-slug>; nothing is written to "
            "the current directory."
        ),
    ),
    limit: int = typer.Option(
        5,
        "--limit",
        min=1,
        max=20,
        help="Maximum provider-local resolve candidates to request before selecting one guide match.",
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
        selected_providers = _normalize_guide_compare_providers(provider)
    except ValueError as exc:
        _emit(ctx,
            {
                "ok": False,
                "error": {"code": "invalid_argument", "message": str(exc)},
            },
            err=True,
        )
        raise typer.Exit(1) from exc

    payload = _guide_compare_query_payload(
        GuideCompareQueryOptions(
            query=query,
            providers=selected_providers,
            orchestration_root=(out_root or _default_guide_compare_query_root(query)).expanduser(),
            requested_expansion=_requested_expansion(ctx),
            limit=limit,
            max_age_hours=max_age_hours,
            force_refresh=force_refresh,
            simc_build_handoff=simc_build_handoff,
            simc_apl_path=simc_apl_path,
            simc_decode=simc_decode,
            simc_build_limit=simc_build_limit,
        )
    )
    if payload.get("ok") is False:
        _emit(ctx, payload, err=True)
        raise typer.Exit(1)
    _emit(ctx, payload)


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
    resolved = _resolve_talent_transport(
        ctx,
        source=source,
        actor_id=actor_id,
        fight_id=fight_id,
        allow_unlisted=allow_unlisted,
        listed_build_limit=listed_build_limit,
        validate=validate,
        kind="talent_transport",
    )
    packet = resolved["talent_transport_packet"]
    written_packet_path = _write_transport_packet_or_fail(
        ctx,
        path_value=out,
        packet=packet,
        source=source,
        kind="talent_transport",
        route=resolved["route"],
        provider_result=resolved["producer_result"],
    )
    stable_packet_path = _stable_transport_packet_path(
        route=resolved["route"],
        written_packet_path=written_packet_path,
        upgraded=bool(resolved["upgraded"]),
    )
    upgrade_result = _normalize_upgrade_result_build_packet_path(
        resolved.get("upgrade_result") if isinstance(resolved, dict) else None,
        stable_packet_path=stable_packet_path,
    )
    _emit(ctx,
        {
            "provider": "warcraft",
            "kind": "talent_transport",
            **resolved,
            "upgrade_result": upgrade_result,
            "written_packet_path": written_packet_path,
        },
    )


@dataclass(frozen=True, slots=True)
class TalentDescribeOptions:
    """One `warcraft talent-describe` run: how to route the packet, and how to describe the build."""

    source: str
    actor_id: int | None
    fight_id: int | None
    allow_unlisted: bool
    listed_build_limit: int
    validate: bool
    packet_out: str | None
    apl_path: str | None
    targets: int
    aoe_targets: int
    list_name: str
    priority_limit: int
    inactive_limit: int


def _talent_describe_payload(ctx: typer.Context, options: TalentDescribeOptions) -> dict[str, Any]:
    """Route the source to a talent transport packet and attach simc describe-build output for it."""
    resolved = _resolve_talent_transport(
        ctx,
        source=options.source,
        actor_id=options.actor_id,
        fight_id=options.fight_id,
        allow_unlisted=options.allow_unlisted,
        listed_build_limit=options.listed_build_limit,
        validate=options.validate,
        kind="talent_describe",
    )
    packet = resolved["talent_transport_packet"]
    describe_result = _describe_transport_packet_with_simc(
        packet,
        expansion=resolved["requested_expansion"],
        apl_path=options.apl_path,
        targets=options.targets,
        aoe_targets=options.aoe_targets,
        list_name=options.list_name,
        priority_limit=options.priority_limit,
        inactive_limit=options.inactive_limit,
    )
    if _provider_result_failed(describe_result):
        error_payload = _provider_error_payload("simc", describe_result)
        _fail_talent_route(
            ctx,
            code=str(error_payload.get("code") or "describe_build_failed"),
            message=str(error_payload.get("message") or "simc describe-build failed for the routed talent transport packet."),
            source=options.source,
            kind="talent_describe",
            route=resolved["route"],
            provider_result=describe_result,
        )
    written_packet_path = _write_transport_packet_or_fail(
        ctx,
        path_value=options.packet_out,
        packet=packet,
        source=options.source,
        kind="talent_describe",
        route=resolved["route"],
        provider_result=describe_result,
    )
    stable_packet_path = _stable_transport_packet_path(
        route=resolved["route"],
        written_packet_path=written_packet_path,
        upgraded=bool(resolved["upgraded"]),
    )
    return {
        "provider": "warcraft",
        "kind": "talent_describe",
        **resolved,
        "upgrade_result": _normalize_upgrade_result_build_packet_path(
            resolved.get("upgrade_result"),
            stable_packet_path=stable_packet_path,
        ),
        "written_packet_path": written_packet_path,
        "describe_result": _normalize_simc_transport_packet_path(
            describe_result,
            stable_packet_path=stable_packet_path,
        ),
    }


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
    _emit(ctx,
        _talent_describe_payload(
            ctx,
            TalentDescribeOptions(
                source=source,
                actor_id=actor_id,
                fight_id=fight_id,
                allow_unlisted=allow_unlisted,
                listed_build_limit=listed_build_limit,
                validate=validate,
                packet_out=packet_out,
                apl_path=apl_path,
                targets=targets,
                aoe_targets=aoe_targets,
                list_name=list_name,
                priority_limit=priority_limit,
                inactive_limit=inactive_limit,
            ),
        ),
    )


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
    requested_expansion = _requested_expansion(ctx)
    try:
        source_kind, bundle_inputs, source_manifest = _load_guide_build_source(source)
    except ValueError as exc:
        _emit(ctx,
            {
                "ok": False,
                "error": {"code": "invalid_bundle_source", "message": str(exc)},
                "source": str(source),
            },
            err=True,
        )
        raise typer.Exit(1) from exc

    payload = _guide_builds_simc_payload(
        source_path=source,
        source_kind=source_kind,
        source_manifest=source_manifest,
        bundle_inputs=bundle_inputs,
        decode=decode,
        apl_path=apl_path,
        limit=limit,
        expansion=requested_expansion,
    )
    summary = payload["summary"]
    if summary["simc_handoff_status"] == "all_handoffs_failed":
        # The packet's provenance stays the envelope's; the rest of it, per-build failure codes
        # included, becomes `error.details`.
        _emit(ctx,
            {
                **{key: value for key, value in payload.items() if key != "kind"},
                "ok": False,
                "error": {
                    "code": "simc_handoff_failed",
                    "message": (
                        f"Every requested simc leg ({', '.join(summary['empty_requested_legs'])}) failed for all "
                        f"{summary['returned_build_count']} build references; the packet carries no usable simc "
                        "output. Each build's `failures` names the simc error; check `warcraft simc doctor`."
                    ),
                },
            },
            err=True,
        )
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

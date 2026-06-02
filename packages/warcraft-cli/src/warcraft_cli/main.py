from __future__ import annotations

import io
import json
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import urlparse

import typer
from blizzard_api_cli.main import app as blizzard_app
from curseforge_cli.main import app as curseforge_app
from icy_veins_cli.main import app as icy_veins_app
from method_cli.main import app as method_app
from raidbots_cli.main import app as raidbots_app
from raiderio_cli.main import app as raiderio_app
from simc_cli.main import app as simc_app
from typer.main import get_command
from warcraft_content.article_bundle import compare_article_bundles, load_article_bundle
from warcraft_core.identity import (
    build_reference_transport_packet_payload,
    parse_wowhead_talent_calc_ref,
    validate_talent_transport_packet,
)
from warcraft_core.output import emit
from warcraft_core.provider_contract import (
    compact_resolve_match,
    compact_wrapper_candidate,
    decorate_resolve_payload,
    decorate_search_result,
    resolve_payload_sort_key,
    search_result_sort_key,
    synthetic_resolve_payloads,
    synthetic_search_candidates,
)
from warcraft_wiki_cli.main import app as warcraft_wiki_app
from warcraftlogs_cli.main import app as warcraftlogs_app
from wowhead_cli.expansion_profiles import resolve_expansion
from wowhead_cli.main import app as wowhead_app
from wowprogress_cli.main import app as wowprogress_app

from warcraft_cli.crosswalk import (
    actor_lookup_identity,
    actor_spec_ambiguous,
    distinct_actor_targets,
    find_report_actors,
    reconcile_class_spec,
    report_actor_names,
)
from warcraft_cli.guild import guild_merge_payload, normalized_identity, raiderio_guild_summary, wowprogress_guild_summary
from warcraft_cli.providers import (
    expansion_filtered_providers,
    expansion_support_snapshot,
    get_provider,
    global_doctor_payload,
    list_providers,
    provider_expansion_exclusion_reason,
    provider_expansion_support,
    provider_invoke,
    provider_resolve,
    provider_search,
    surface_filtered_providers,
)

app = typer.Typer(add_completion=False, help="Warcraft wrapper CLI for routing to service-specific Warcraft CLIs.")
GUIDE_COMPARE_BUNDLES_ARGUMENT = typer.Argument(
    ...,
    help="Two or more exported guide bundle directories from wowhead, method, or icy-veins.",
)
GUIDE_COMPARE_QUERY_PROVIDERS = ("wowhead", "method", "icy-veins")


def _emit(payload: Any, *, pretty: bool, err: bool = False) -> None:
    emit(payload, pretty=pretty, err=err)


def _invoke_sub_app(sub_app: typer.Typer, *, args: list[str], prog_name: str) -> None:
    command = get_command(sub_app)
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


@app.callback()
def main_callback(
    ctx: typer.Context,
    pretty: bool = typer.Option(False, "--pretty", help="Pretty-print JSON output."),
    expansion: str | None = typer.Option(
        None,
        "--expansion",
        help="Filter wrapper search/resolve to a specific expansion profile. Passed through to expansion-aware providers like wowhead.",
    ),
) -> None:
    requested_expansion: str | None = None
    if expansion is not None:
        try:
            requested_expansion = resolve_expansion(expansion).key
        except ValueError as exc:
            raise typer.BadParameter(str(exc), param_hint="--expansion") from exc
    ctx.obj = {"pretty": pretty, "requested_expansion": requested_expansion}


def _pretty(ctx: typer.Context) -> bool:
    obj = ctx.obj
    if isinstance(obj, dict):
        return bool(obj.get("pretty"))
    return False


def _requested_expansion(ctx: typer.Context) -> str | None:
    obj = ctx.obj
    if isinstance(obj, dict):
        value = obj.get("requested_expansion")
        if isinstance(value, str) and value:
            return value
    return None


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
    _emit(
        {
            "ok": False,
            "error": {
                "code": "unsupported_provider_expansion",
                "message": (
                    f"Provider {provider_name!r} does not support wrapper expansion "
                    f"{requested_expansion!r}."
                ),
            },
            "provider": provider_name,
            "requested_expansion": requested_expansion,
            "expansion_support": provider_expansion_support(
                registration,
                requested_expansion=requested_expansion,
            ),
        },
        pretty=_pretty(ctx),
        err=True,
    )
    raise typer.Exit(1)


def _passthrough_args(ctx: typer.Context, *, provider_name: str) -> list[str]:
    args = list(ctx.args)
    requested_expansion = _requested_expansion(ctx)
    if requested_expansion is None:
        return args
    if provider_name == "wowhead":
        if "--expansion" in args:
            _emit(
                {
                    "ok": False,
                    "error": {
                        "code": "duplicate_expansion_argument",
                        "message": "Do not pass both warcraft --expansion and wowhead --expansion in the same command.",
                    },
                    "provider": provider_name,
                    "requested_expansion": requested_expansion,
                },
                pretty=_pretty(ctx),
                err=True,
            )
            raise typer.Exit(1)
        return ["--expansion", requested_expansion, *args]
    return args


def _run_passthrough(ctx: typer.Context, sub_app: typer.Typer, *, provider_name: str, prog_name: str) -> None:
    """Proxy ``warcraft <provider> ...`` to a provider CLI, applying expansion policy.

    Normal case: invoke the sub-app directly so its payload reaches stdout untouched.
    none-expansion relax case: capture the provider payload and attach the advisory note
    as an additive sibling key (provider payload preserved), then re-emit.
    """
    advisory = _expansion_passthrough_advisory(ctx, provider_name=provider_name)
    args = _passthrough_args(ctx, provider_name=provider_name)
    if advisory is None:
        _invoke_sub_app(sub_app, args=args, prog_name=prog_name)
        return
    # The provider emits its payload to stdout (ok) or stderr (error). Capture both so the
    # advisory note can be attached regardless of which stream carried the JSON payload.
    # This buffers the provider's output instead of streaming it; that only affects the
    # unusual `warcraft --expansion <key> <none-provider> ...` combination (expansion has no
    # meaning for a none-expansion provider) and is the cost of annotating the payload.
    out_buf, err_buf = io.StringIO(), io.StringIO()
    exit_code = 0
    try:
        with redirect_stdout(out_buf), redirect_stderr(err_buf):
            _invoke_sub_app(sub_app, args=args, prog_name=prog_name)
    except typer.Exit as exc:
        exit_code = exc.exit_code if isinstance(exc.exit_code, int) else 1
    out_text, err_text = out_buf.getvalue(), err_buf.getvalue()
    out_json, err_json = _parse_json_object(out_text), _parse_json_object(err_text)
    if out_json is not None:
        _emit(_with_expansion_advisory(out_json, advisory), pretty=_pretty(ctx))
    elif err_json is not None:
        _emit(_with_expansion_advisory(err_json, advisory), pretty=_pretty(ctx), err=True)
    else:
        # Non-JSON provider output (e.g. --help text): surface the advisory on its own so the
        # relax is never silent, then pass the raw output through below.
        _emit(advisory, pretty=_pretty(ctx), err=True)
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
    annotated = {**payload, "expansion_advisory": advisory}
    annotated.setdefault("expansion_filter", advisory["expansion_filter"])
    return annotated


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
    return Path.cwd() / "warcraft_guide_compare" / _slugify_path_fragment(query)


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
        candidate = row.get("candidate") if isinstance(row.get("candidate"), dict) else {}
        freshness = row.get("freshness") if isinstance(row.get("freshness"), dict) else {}
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
        manifest = bundle.get("manifest") if isinstance(bundle.get("manifest"), dict) else {}
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


def _handoff_simc_section(simc_results: dict[str, Any | None]) -> dict[str, Any]:
    identify_result = simc_results["identify"]
    decode_result = simc_results["decode"]
    describe_result = simc_results["describe"]
    return {
        "identify": {
            "exit_code": identify_result.get("exit_code"),
            "payload": identify_result.get("payload"),
        },
        "decode": (
            {"exit_code": decode_result.get("exit_code"), "payload": decode_result.get("payload")}
            if isinstance(decode_result, dict)
            else None
        ),
        "describe": (
            {"exit_code": describe_result.get("exit_code"), "payload": describe_result.get("payload")}
            if isinstance(describe_result, dict)
            else None
        ),
    }


def _build_simc_handoff_row(
    row: dict[str, Any],
    *,
    decode: bool,
    apl_path: str | None,
    expansion: str | None,
) -> dict[str, Any] | None:
    reference = row.get("reference") if isinstance(row.get("reference"), dict) else {}
    build_url = reference.get("url")
    if not isinstance(build_url, str) or not build_url.strip():
        return None
    build_code = _resolve_handoff_build_code(reference, build_url)
    if build_code is None:
        return None
    normalized_reference = dict(reference)
    normalized_reference["build_code"] = build_code
    sources = row.get("sources") if isinstance(row.get("sources"), list) else []
    transport_packet = _build_handoff_transport_packet(build_url, normalized_reference, sources)
    packet_path: Path | None = None
    build_input_args = ["--build-text", build_url]
    if isinstance(transport_packet, dict):
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
    return {
        "reference": normalized_reference,
        "talent_transport_packet": transport_packet,
        "sources": sources,
        "evidence": _handoff_evidence_section(sources),
        "simc": _handoff_simc_section(simc_results),
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
    build_rows: list[dict[str, Any]] = []
    source_providers = sorted(
        {
            provider
            for _bundle_path, bundle in bundle_inputs
            for provider in [((bundle.get("manifest") or {}).get("provider") if isinstance(bundle.get("manifest"), dict) else None)]
            if isinstance(provider, str) and provider
        }
    )
    for row in selected_rows:
        build_row = _build_simc_handoff_row(row, decode=decode, apl_path=apl_path, expansion=expansion)
        if build_row is not None:
            build_rows.append(build_row)

    identify_success_count, decode_success_count, describe_success_count = _count_simc_handoff_successes(
        build_rows
    )
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
        "build_reference_count": len(handoff_rows),
        "truncated": len(handoff_rows) > len(selected_rows),
        "decode_enabled": decode,
        "apl_path": apl_path,
        "summary": {
            "returned_build_count": len(build_rows),
            "excluded_build_count": max(0, len(handoff_rows) - len(selected_rows)) + max(0, len(selected_rows) - len(build_rows)),
            "identify_success_count": identify_success_count,
            "decode_success_count": decode_success_count,
            "describe_success_count": describe_success_count,
        },
        "builds": build_rows,
    }


def _looks_like_wowhead_talent_calc_reference(value: str) -> bool:
    text = value.strip()
    if not text:
        return False
    lowered = text.lower()
    url_candidate: str | None = None
    if "://" in text:
        url_candidate = text
    elif lowered.startswith(("www.wowhead.com/", "wowhead.com/")):
        url_candidate = f"https://{text}"
    if url_candidate is not None:
        parsed = urlparse(url_candidate)
        hostname = parsed.hostname.lower() if isinstance(parsed.hostname, str) else ""
        path_parts = [part for part in parsed.path.split("/") if part]
        return (
            (hostname == "wowhead.com" or hostname.endswith(".wowhead.com"))
            and "talent-calc" in path_parts
        )
    parts = text.split("/")
    if parts and parts[0] == "":
        parts = parts[1:]
    if parts and parts[-1] == "":
        parts = parts[:-1]
    if not parts:
        return False
    if parts and parts[0] in {"classic", "tbc", "wotlk", "cata", "mop-classic", "ptr", "beta", "classic-ptr"}:
        parts = parts[1:]
        if not parts:
            return False
    known_classes = {
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
    if parts[0].strip() in known_classes:
        return True
    if parts[0].strip() == "talent-calc":
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
    payload = result.get("payload")
    if not isinstance(payload, dict):
        return result
    build_spec = payload.get("build_spec")
    if not isinstance(build_spec, dict):
        return result
    transport_packet = build_spec.get("transport_packet")
    if not isinstance(transport_packet, dict):
        return result
    normalized_result = dict(result)
    normalized_payload = dict(payload)
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
    normalized_payload["build_spec"] = normalized_build_spec
    normalized_result["payload"] = normalized_payload
    return normalized_result


def _normalize_upgrade_result_build_packet_path(
    upgrade_result: dict[str, Any] | None,
    *,
    stable_packet_path: str | None,
) -> dict[str, Any] | None:
    if not isinstance(upgrade_result, dict):
        return upgrade_result
    payload = upgrade_result.get("payload")
    if not isinstance(payload, dict):
        return upgrade_result
    input_payload = payload.get("input")
    if not isinstance(input_payload, dict):
        return upgrade_result
    normalized_result = dict(upgrade_result)
    normalized_payload = dict(payload)
    normalized_input = dict(input_payload)
    normalized_input.pop("build_packet", None)
    normalized_payload["input"] = normalized_input
    normalized_result["payload"] = normalized_payload
    return normalized_result


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
    payload = result.get("payload") if isinstance(result.get("payload"), dict) else {}
    updated_packet = payload.get("updated_packet") if isinstance(payload.get("updated_packet"), dict) else None
    if updated_packet is None:
        return result, None
    return result, validate_talent_transport_packet(updated_packet)


def _transport_status_rank(status: str | None) -> int:
    return {
        "unknown": 0,
        "raw_only": 1,
        "validated": 2,
        "exact": 3,
    }.get(status, -1)


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
    _emit(payload, pretty=_pretty(ctx), err=True)
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
    producer_payload = provider_result.get("payload") if isinstance(provider_result.get("payload"), dict) else {}
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
    packet = packet_value if isinstance(packet_value, dict) else {}
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
            upgrade_result, packet = _upgrade_transport_packet_with_simc(packet, expansion=requested_expansion)
        except ValueError as exc:
            _fail_talent_route(
                ctx,
                code="packet_upgrade_failed",
                message=f"simc validate-talent-transport returned an invalid upgraded packet: {exc}",
                source=source,
                kind=kind,
                route=route,
            )
        if upgrade_result is not None and _provider_result_failed(upgrade_result):
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
        if packet is None:
            _fail_talent_route(
                ctx,
                code="packet_upgrade_failed",
                message="simc validate-talent-transport did not return an upgraded talent transport packet.",
                source=source,
                kind=kind,
                route=route,
                provider_result=upgrade_result,
            )
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


def _search_fallback_guide_match(
    provider: str,
    payload: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, str | None]:
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

    ranking = top.get("ranking")
    top_score = int(ranking.get("score") or 0) if isinstance(ranking, dict) else 0
    second_score = 0
    has_second = False
    if len(results) > 1 and isinstance(results[1], dict):
        second_ranking = results[1].get("ranking")
        second_score = int(second_ranking.get("score") or 0) if isinstance(second_ranking, dict) else 0
        has_second = True
    if top_score < 50:
        return None, f"search_top_guide_score_too_low:{top_score}"
    if has_second and top_score < second_score + 25:
        return None, "search_results_not_decisive"
    if not has_second and top_score < 70:
        return None, "search_single_result_not_strong_enough"

    raw_ref = top.get("id")
    if raw_ref is None:
        metadata = top.get("metadata")
        if isinstance(metadata, dict):
            raw_ref = metadata.get("slug")
    if raw_ref is None:
        return None, "search_guide_missing_ref"

    return {
        "provider": provider,
        "ref": str(raw_ref),
        "name": top.get("name"),
        "url": top.get("url"),
        "confidence": "medium",
        "next_command": (
            top.get("follow_up", {}).get("recommended_command")
            if isinstance(top.get("follow_up"), dict)
            else None
        ),
        "selection_source": "search_fallback",
        "search_ranking": ranking,
        "selection_contract": {
            "rule": "top_guide_result_with_strong_score_and_clear_margin",
            "top_score": top_score,
            "second_score": second_score if has_second else None,
            "minimum_top_score": 50,
            "minimum_margin_over_runner_up": 25 if has_second else None,
            "single_result_minimum_score": None if has_second else 70,
        },
    }, None


def _raiderio_source(identity: dict[str, str], *, expansion: str | None) -> dict[str, Any]:
    result = _provider_payload_result(
        "raiderio",
        ["guild", identity["region"], identity["realm"], identity["name"]],
        expansion=expansion,
    )
    payload = result.get("payload")
    if result.get("status") != "ok" or not isinstance(payload, dict):
        return result
    return {
        **result,
        "summary": raiderio_guild_summary(payload),
    }


def _wowprogress_source(identity: dict[str, str], *, expansion: str | None) -> dict[str, Any]:
    result = _provider_payload_result(
        "wowprogress",
        ["guild", identity["region"], identity["realm"], identity["name"]],
        expansion=expansion,
    )
    payload = result.get("payload")
    if result.get("status") != "ok" or not isinstance(payload, dict):
        return result
    return {
        **result,
        "summary": wowprogress_guild_summary(payload),
    }


@app.command("doctor")
def doctor(ctx: typer.Context) -> None:
    _emit(global_doctor_payload(requested_expansion=_requested_expansion(ctx)), pretty=_pretty(ctx))


@app.command("search")
def search(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="Search across available providers."),
    limit: int = typer.Option(5, "--limit", min=1, max=50, help="Maximum provider-local results to request."),
    compact: bool = typer.Option(False, "--compact", help="Return a smaller wrapper payload with compact candidates."),
    ranking_debug: bool = typer.Option(
        False, "--ranking-debug", help="Include compact wrapper ranking summaries for the returned candidates."),
    expansion_debug: bool = typer.Option(
        False,
        "--expansion-debug",
        help="Include a compact expansion support snapshot for all providers.",
    ),
) -> None:
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
        payload = result.get("payload")
        provider_row = {
            "provider": registration.name,
            "status": registration.status,
            "expansion_support": provider_expansion_support(
                registration,
                requested_expansion=requested_expansion,
            ),
            "payload": payload,
        }
        providers.append(provider_row)
        if isinstance(payload, dict):
            for row in payload.get("results", []) or []:
                if isinstance(row, dict):
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
                        )
                    )
    for row in synthetic_search_candidates(query):
        registration = get_provider(str(row.get("provider") or ""))
        if provider_expansion_exclusion_reason(registration, requested_expansion=requested_expansion) is not None:
            continue
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
            )
        )
    flattened.sort(key=search_result_sort_key)
    top = flattened[:limit]
    if compact:
        top = [compact_wrapper_candidate(row) for row in top]
    payload: dict[str, Any] = {
        "query": query,
        "provider_count": len(list_providers()),
        "requested_expansion": requested_expansion,
        "expansion_filter_active": requested_expansion is not None,
        "included_providers": [registration.name for registration in included_registrations],
        "excluded_providers": excluded_providers,
        "included_provider_count": len(included_registrations),
        "excluded_provider_count": len(excluded_providers),
        "providers": [] if compact else providers,
        "count": len(flattened),
        "results": top,
    }
    if ranking_debug:
        payload["ranking_debug"] = [compact_wrapper_candidate(row) for row in flattened[:limit]]
    if expansion_debug:
        payload["expansion_debug"] = expansion_support_snapshot(requested_expansion=requested_expansion)
    _emit(
        payload,
        pretty=_pretty(ctx),
    )


@app.command("resolve")
def resolve(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="Resolve a query across available providers."),
    limit: int = typer.Option(5, "--limit", min=1, max=50, help="Maximum provider-local candidates to request."),
    compact: bool = typer.Option(False, "--compact", help="Return a smaller wrapper payload with a compact match summary."),
    ranking_debug: bool = typer.Option(False, "--ranking-debug", help="Include compact wrapper ranking summaries for resolved candidates."),
    expansion_debug: bool = typer.Option(
        False,
        "--expansion-debug",
        help="Include a compact expansion support snapshot for all providers.",
    ),
) -> None:
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
        payload = result.get("payload")
        providers.append(
            {
                "provider": registration.name,
                "status": registration.status,
                "expansion_support": provider_expansion_support(
                    registration,
                    requested_expansion=requested_expansion,
                ),
                "payload": payload,
            }
        )
        if isinstance(payload, dict) and payload.get("resolved"):
            resolved_candidates.append((registration.name, decorate_resolve_payload(query, registration.name, payload)))
    for provider_name, payload in synthetic_resolve_payloads(query):
        registration = get_provider(provider_name)
        if provider_expansion_exclusion_reason(registration, requested_expansion=requested_expansion) is not None:
            continue
        resolved_candidates.append((provider_name, payload))
    resolved_candidates.sort(key=lambda row: resolve_payload_sort_key(row[0], row[1]))
    best_provider = resolved_candidates[0][0] if resolved_candidates else None
    best_payload = resolved_candidates[0][1] if resolved_candidates else None
    match = compact_resolve_match(best_payload) if compact else (best_payload.get("match") if isinstance(best_payload, dict) else None)
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
        "provider": best_provider,
        "match": match,
        "next_command": best_payload.get("next_command") if isinstance(best_payload, dict) else None,
        "confidence": best_payload.get("confidence") if isinstance(best_payload, dict) else None,
        "providers": [] if compact else providers,
    }
    if ranking_debug:
        payload["ranking_debug"] = [compact_resolve_match(row[1])
                                    for row in resolved_candidates[:limit] if compact_resolve_match(row[1]) is not None]
    if expansion_debug:
        payload["expansion_debug"] = expansion_support_snapshot(requested_expansion=requested_expansion)
    _emit(
        payload,
        pretty=_pretty(ctx),
    )


@app.command("guild")
def guild(
    ctx: typer.Context,
    region: str = typer.Argument(..., help="Region slug such as us or eu."),
    realm: str = typer.Argument(..., help="Realm title or slug."),
    name: str = typer.Argument(..., help="Guild name."),
) -> None:
    identity = normalized_identity(region, realm, name)
    requested_expansion = _requested_expansion(ctx)
    payload = guild_merge_payload(
        identity,
        raiderio=_raiderio_source(identity, expansion=requested_expansion),
        wowprogress=_wowprogress_source(identity, expansion=requested_expansion),
    )
    _emit(payload, pretty=_pretty(ctx), err=not payload.get("ok"))
    if not payload.get("ok"):
        raise typer.Exit(1)


@app.command("guild-history")
def guild_history(
    ctx: typer.Context,
    region: str = typer.Argument(..., help="Region slug such as us or eu."),
    realm: str = typer.Argument(..., help="Realm title or slug."),
    name: str = typer.Argument(..., help="Guild name."),
) -> None:
    identity = normalized_identity(region, realm, name)
    requested_expansion = _requested_expansion(ctx)
    source_result = _provider_payload_result(
        "wowprogress",
        ["guild-history", identity["region"], identity["realm"], identity["name"]],
        expansion=requested_expansion,
    )
    payload = source_result.get("payload") if isinstance(source_result.get("payload"), dict) else {}
    history = payload.get("tiers") if isinstance(payload.get("tiers"), list) else []
    if source_result.get("status") != "ok":
        _emit(
            {
                "ok": False,
                "error": source_result.get("error"),
                "query": identity,
                "source": "wowprogress",
                "provider_payload": payload,
            },
            pretty=_pretty(ctx),
            err=True,
        )
        raise typer.Exit(1)
    _emit(
        {
            "ok": True,
            "provider": "warcraft",
            "kind": "guild_history",
            "query": identity,
            "source": "wowprogress",
            "guild": payload.get("guild"),
            "count": len(history),
            "tiers": history,
            "citations": payload.get("citations"),
            "provider_payload": payload,
        },
        pretty=_pretty(ctx),
    )


@app.command("guild-ranks")
def guild_ranks(
    ctx: typer.Context,
    region: str = typer.Argument(..., help="Region slug such as us or eu."),
    realm: str = typer.Argument(..., help="Realm title or slug."),
    name: str = typer.Argument(..., help="Guild name."),
) -> None:
    identity = normalized_identity(region, realm, name)
    requested_expansion = _requested_expansion(ctx)
    source_result = _provider_payload_result(
        "wowprogress",
        ["guild-ranks", identity["region"], identity["realm"], identity["name"]],
        expansion=requested_expansion,
    )
    payload = source_result.get("payload") if isinstance(source_result.get("payload"), dict) else {}
    history = payload.get("tiers") if isinstance(payload.get("tiers"), list) else []
    if source_result.get("status") != "ok":
        _emit(
            {
                "ok": False,
                "error": source_result.get("error"),
                "query": identity,
                "source": "wowprogress",
                "provider_payload": payload,
            },
            pretty=_pretty(ctx),
            err=True,
        )
        raise typer.Exit(1)
    _emit(
        {
            "ok": True,
            "provider": "warcraft",
            "kind": "guild_ranks",
            "query": identity,
            "source": "wowprogress",
            "guild": payload.get("guild"),
            "count": len(history),
            "tiers": history,
            "citations": payload.get("citations"),
            "provider_payload": payload,
        },
        pretty=_pretty(ctx),
    )


_ACTOR_PROFILE_JOIN_RULE = "soft match on region + realm + character name; not a canonical cross-provider actor id"


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
    wcl_args = ["report-player-details", code]
    if fight_id is not None:
        wcl_args += ["--fight-id", str(fight_id)]
    if allow_unlisted:
        wcl_args.append("--allow-unlisted")
    log_result = _provider_payload_result("warcraftlogs", wcl_args, expansion=requested_expansion)
    if log_result.get("status") != "ok":
        _emit(
            {
                "ok": False,
                "provider": "warcraft",
                "kind": "actor_profile_crosswalk",
                "query": query,
                "error": {
                    "code": "warcraftlogs_lookup_failed",
                    "message": "Warcraft Logs report lookup failed.",
                    "source": log_result.get("error"),
                },
            },
            pretty=_pretty(ctx),
            err=True,
        )
        raise typer.Exit(1)
    log_payload = log_result.get("payload") if isinstance(log_result.get("payload"), dict) else {}
    matches = find_report_actors(log_payload, name)
    if not matches:
        _emit(
            {
                "ok": False,
                "provider": "warcraft",
                "kind": "actor_profile_crosswalk",
                "query": query,
                "error": {
                    "code": "actor_not_found",
                    "message": f"No actor named {name!r} in report {code!r}.",
                    "available_actors": report_actor_names(log_payload),
                },
            },
            pretty=_pretty(ctx),
            err=True,
        )
        raise typer.Exit(1)
    targets = distinct_actor_targets(matches)
    if len(targets) > 1:
        _emit(
            {
                "ok": False,
                "provider": "warcraft",
                "kind": "actor_profile_crosswalk",
                "query": query,
                "error": {
                    "code": "ambiguous_actor",
                    "message": (
                        f"Report {code!r} has {len(targets)} characters named {name!r} on "
                        "different realms/regions; cannot pick one safely."
                    ),
                    "candidates": targets,
                    "hint": (
                        "Narrow to a single fight with --fight-id, or query the realm directly "
                        "with 'raiderio character <region> <realm> <name>'."
                    ),
                },
            },
            pretty=_pretty(ctx),
            err=True,
        )
        raise typer.Exit(1)
    if actor_spec_ambiguous(matches):
        _emit(
            {
                "ok": False,
                "provider": "warcraft",
                "kind": "actor_profile_crosswalk",
                "query": query,
                "error": {
                    "code": "ambiguous_actor_spec",
                    "message": (
                        f"Actor {name!r} appears in report {code!r} with more than one class/spec "
                        "across fights; cannot pick one to reconcile."
                    ),
                    "hint": "Narrow to a single fight with --fight-id so the actor resolves to one spec.",
                },
            },
            pretty=_pretty(ctx),
            err=True,
        )
        raise typer.Exit(1)
    actor = matches[0]
    log_side = {
        "status": "ok",
        "role": actor.get("role"),
        "server": actor.get("server"),
        "region": actor.get("region"),
        "class_spec_identity": actor.get("class_spec_identity"),
        "report_actor_identity": actor.get("identity_contract"),
    }
    lookup = actor_lookup_identity(actor, region_override=region)
    if not lookup["ok"]:
        missing = lookup["missing"]
        hint = (
            "Re-run with --region <slug> to enable the Raider.IO profile lookup."
            if missing == "region"
            else f"The report actor is missing its {missing}; the Raider.IO profile lookup cannot proceed."
        )
        _emit(
            {
                "ok": False,
                "provider": "warcraft",
                "kind": "actor_profile_crosswalk",
                "query": query,
                "error": {"code": f"actor_{missing}_unknown", "missing_field": missing, "hint": hint},
                "sources": {"warcraftlogs": log_side},
            },
            pretty=_pretty(ctx),
            err=True,
        )
        raise typer.Exit(1)
    identity = lookup["identity"]
    query.update(identity)
    profile_result = _provider_payload_result(
        "raiderio",
        ["character", identity["region"], identity["realm"], identity["name"]],
        expansion=requested_expansion,
    )
    if profile_result.get("status") != "ok":
        _emit(
            {
                "ok": False,
                "provider": "warcraft",
                "kind": "actor_profile_crosswalk",
                "query": query,
                "error": {"code": "profile_lookup_failed", "source": profile_result.get("error")},
                "sources": {"warcraftlogs": log_side, "raiderio": {"status": "error", "error": profile_result.get("error")}},
            },
            pretty=_pretty(ctx),
            err=True,
        )
        raise typer.Exit(1)
    profile_payload = profile_result.get("payload") if isinstance(profile_result.get("payload"), dict) else {}
    character = profile_payload.get("character") if isinstance(profile_payload.get("character"), dict) else {}
    profile_identity = character.get("class_spec_identity")
    _emit(
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
        pretty=_pretty(ctx),
    )


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
    if len(bundles) < 2:
        _emit(
            {
                "ok": False,
                "error": {
                    "code": "invalid_argument",
                    "message": "guide-compare requires at least two exported guide bundles.",
                },
            },
            pretty=_pretty(ctx),
            err=True,
        )
        raise typer.Exit(1)

    bundle_inputs: list[tuple[Path, dict[str, Any]]] = []
    for bundle_path in bundles:
        resolved_path = bundle_path.expanduser()
        if not resolved_path.exists():
            _emit(
                {
                    "ok": False,
                    "error": {
                        "code": "invalid_bundle",
                        "message": f"Bundle directory not found: {resolved_path}",
                    },
                },
                pretty=_pretty(ctx),
                err=True,
            )
            raise typer.Exit(1)
        try:
            bundle_inputs.append((resolved_path, load_article_bundle(resolved_path)))
        except (ValueError, OSError) as exc:
            _emit(
                {
                    "ok": False,
                    "error": {
                        "code": "invalid_bundle",
                        "message": str(exc),
                    },
                    "bundle": str(resolved_path),
                },
                pretty=_pretty(ctx),
                err=True,
            )
            raise typer.Exit(1) from exc

    payload = {
        "provider": "warcraft",
        **_guide_comparison_packet(bundle_inputs, max_age_hours=max_age_hours),
    }
    _emit(payload, pretty=_pretty(ctx))


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
        resolved.get("payload") if isinstance(resolved, dict) else None,
    )
    search_payload: dict[str, Any] | None = None
    if candidate is None:
        searched = provider_search(provider_name, query, limit=limit, expansion=expansion)
        search_payload = searched.get("payload") if isinstance(searched, dict) else None
        fallback_candidate, fallback_reason = _search_fallback_guide_match(provider_name, search_payload)
        if fallback_candidate is not None:
            candidate = fallback_candidate
            candidate_reason = None
        else:
            candidate_reason = fallback_reason if fallback_reason is not None else candidate_reason
    resolve_payload = resolved.get("payload") if isinstance(resolved, dict) else None
    return candidate, candidate_reason, resolve_payload, search_payload


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
    existing_freshness = (
        _guide_compare_freshness(existing_row.get("exported_at"), max_age_hours=max_age_hours)
        if isinstance(existing_row, dict)
        else {"status": "stale", "reason": "missing_manifest_row", "age_hours": None, "max_age_hours": max_age_hours}
    )
    same_candidate = (
        isinstance(existing_row, dict)
        and str(existing_row.get("candidate_ref") or "") == str(candidate["ref"])
        and str(existing_row.get("bundle_path") or "") == str(export_dir)
    )
    can_reuse = (
        not force_refresh
        and same_candidate
        and existing_freshness.get("status") == "fresh"
        and export_dir.exists()
    )
    if can_reuse:
        try:
            bundle = load_article_bundle(export_dir)
        except (ValueError, OSError) as exc:
            return {
                "provider": provider_name,
                "status": "error",
                "reason": "invalid_exported_bundle",
                "candidate": candidate,
                "bundle_path": str(export_dir),
                "freshness": existing_freshness,
                "error": str(exc),
            }, None
        return {
            "provider": provider_name,
            "status": "reused",
            "candidate": candidate,
            "bundle_path": str(export_dir),
            "freshness": existing_freshness,
            "exported_at": existing_row.get("exported_at") if isinstance(existing_row, dict) else None,
        }, (export_dir, bundle)

    export_result = provider_invoke(
        provider_name,
        ["guide-export", candidate["ref"], "--out", str(export_dir)],
        expansion=requested_expansion,
    )
    if export_result.get("exit_code") != 0:
        return {
            "provider": provider_name,
            "status": "error",
            "reason": "guide_export_failed",
            "candidate": candidate,
            "bundle_path": str(export_dir),
            "freshness": existing_freshness,
            "export": export_result.get("payload"),
        }, None
    try:
        bundle = load_article_bundle(export_dir)
    except (ValueError, OSError) as exc:
        return {
            "provider": provider_name,
            "status": "error",
            "reason": "invalid_exported_bundle",
            "candidate": candidate,
            "bundle_path": str(export_dir),
            "freshness": existing_freshness,
            "error": str(exc),
        }, None

    # Source exported_at from the bundle manifest write_article_bundle just stamped, so the
    # orchestration row, the reuse check, and _guide_comparison_packet all read one timestamp
    # (they cannot disagree on freshness). Fall back to now only for legacy timestamp-less bundles.
    bundle_manifest = bundle.get("manifest") if isinstance(bundle.get("manifest"), dict) else {}
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
        help="Directory root where orchestrated guide bundles should be written.",
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
    requested_expansion = _requested_expansion(ctx)
    try:
        selected_providers = _normalize_guide_compare_providers(provider)
    except ValueError as exc:
        _emit(
            {
                "ok": False,
                "error": {"code": "invalid_argument", "message": str(exc)},
            },
            pretty=_pretty(ctx),
            err=True,
        )
        raise typer.Exit(1) from exc

    orchestration_root = (out_root or _default_guide_compare_query_root(query)).expanduser()
    orchestration_manifest = _load_guide_compare_manifest(orchestration_root) or {}
    manifest_rows = orchestration_manifest.get("providers")
    manifest_by_provider: dict[str, dict[str, Any]] = {}
    if isinstance(manifest_rows, list):
        for row in manifest_rows:
            if not isinstance(row, dict):
                continue
            provider_name = row.get("provider")
            if isinstance(provider_name, str):
                manifest_by_provider[provider_name] = row
    provider_rows: list[dict[str, Any]] = []
    bundle_inputs: list[tuple[Path, dict[str, Any]]] = []

    for provider_name in selected_providers:
        provider_row, bundle_input = _process_guide_compare_provider(
            provider_name,
            query=query,
            requested_expansion=requested_expansion,
            limit=limit,
            max_age_hours=max_age_hours,
            force_refresh=force_refresh,
            orchestration_root=orchestration_root,
            manifest_by_provider=manifest_by_provider,
        )
        provider_rows.append(provider_row)
        if bundle_input is not None:
            bundle_inputs.append(bundle_input)

    payload: dict[str, Any] = {
        "provider": "warcraft",
        "kind": "guide_bundle_comparison_orchestration",
        "query": query,
        "requested_expansion": requested_expansion,
        "selected_providers": list(selected_providers),
        "output_root": str(orchestration_root),
        "max_age_hours": max_age_hours,
        "force_refresh": force_refresh,
        "provider_results": provider_rows,
        "exported_bundle_count": len(bundle_inputs),
        "comparison": None,
        "simc_build_handoff": None,
    }
    payload["manifest"] = _write_guide_compare_manifest(
        root=orchestration_root,
        query=query,
        requested_expansion=requested_expansion,
        max_age_hours=max_age_hours,
        provider_results=provider_rows,
    )
    if len(bundle_inputs) >= 2:
        payload["comparison"] = {
            "provider": "warcraft",
            **_guide_comparison_packet(bundle_inputs, max_age_hours=max_age_hours),
        }
        include_simc_build_handoff = simc_build_handoff or (
            isinstance(simc_apl_path, str) and bool(simc_apl_path.strip())
        )
        if include_simc_build_handoff:
            payload["simc_build_handoff"] = _guide_builds_simc_payload(
                source_path=orchestration_root,
                source_kind="orchestration_root",
                source_manifest=payload["manifest"] if isinstance(payload.get("manifest"), dict) else None,
                bundle_inputs=bundle_inputs,
                decode=simc_decode,
                apl_path=simc_apl_path,
                limit=simc_build_limit,
                expansion=requested_expansion,
            )
        _emit(payload, pretty=_pretty(ctx))
        return

    payload["ok"] = False
    payload["error"] = {
        "code": "insufficient_guides",
        "message": "Need at least two exported guide bundles to compare.",
    }
    _emit(payload, pretty=_pretty(ctx), err=True)
    raise typer.Exit(1)


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
    _emit(
        {
            "provider": "warcraft",
            "kind": "talent_transport",
            **resolved,
            "upgrade_result": upgrade_result,
            "written_packet_path": written_packet_path,
        },
        pretty=_pretty(ctx),
    )


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
    resolved = _resolve_talent_transport(
        ctx,
        source=source,
        actor_id=actor_id,
        fight_id=fight_id,
        allow_unlisted=allow_unlisted,
        listed_build_limit=listed_build_limit,
        validate=validate,
        kind="talent_describe",
    )
    packet = resolved["talent_transport_packet"]
    describe_result = _describe_transport_packet_with_simc(
        packet,
        expansion=resolved["requested_expansion"],
        apl_path=apl_path,
        targets=targets,
        aoe_targets=aoe_targets,
        list_name=list_name,
        priority_limit=priority_limit,
        inactive_limit=inactive_limit,
    )
    if _provider_result_failed(describe_result):
        error_payload = _provider_error_payload("simc", describe_result)
        _fail_talent_route(
            ctx,
            code=str(error_payload.get("code") or "describe_build_failed"),
            message=str(error_payload.get("message") or "simc describe-build failed for the routed talent transport packet."),
            source=source,
            kind="talent_describe",
            route=resolved["route"],
            provider_result=describe_result,
        )
    written_packet_path = _write_transport_packet_or_fail(
        ctx,
        path_value=packet_out,
        packet=packet,
        source=source,
        kind="talent_describe",
        route=resolved["route"],
        provider_result=describe_result,
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
    describe_result = _normalize_simc_transport_packet_path(
        describe_result,
        stable_packet_path=stable_packet_path,
    )
    _emit(
        {
            "provider": "warcraft",
            "kind": "talent_describe",
            **resolved,
            "upgrade_result": upgrade_result,
            "packet_written_path": written_packet_path,
            "describe_result": describe_result,
        },
        pretty=_pretty(ctx),
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
    requested_expansion = _requested_expansion(ctx)
    try:
        source_kind, bundle_inputs, source_manifest = _load_guide_build_source(source)
    except ValueError as exc:
        _emit(
            {
                "ok": False,
                "error": {"code": "invalid_bundle_source", "message": str(exc)},
                "source": str(source),
            },
            pretty=_pretty(ctx),
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
    _emit(payload, pretty=_pretty(ctx))


@app.command(
    "wowhead",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def wowhead_passthrough(ctx: typer.Context) -> None:
    _run_passthrough(ctx, wowhead_app, provider_name="wowhead", prog_name="wowhead")


@app.command(
    "icy-veins",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def icy_veins_passthrough(ctx: typer.Context) -> None:
    _run_passthrough(ctx, icy_veins_app, provider_name="icy-veins", prog_name="icy-veins")


@app.command(
    "method",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def method_passthrough(ctx: typer.Context) -> None:
    _run_passthrough(ctx, method_app, provider_name="method", prog_name="method")


@app.command(
    "raiderio",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def raiderio_passthrough(ctx: typer.Context) -> None:
    _run_passthrough(ctx, raiderio_app, provider_name="raiderio", prog_name="raiderio")


@app.command(
    "warcraftlogs",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def warcraftlogs_passthrough(ctx: typer.Context) -> None:
    _run_passthrough(ctx, warcraftlogs_app, provider_name="warcraftlogs", prog_name="warcraftlogs")


@app.command(
    "warcraft-wiki",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def warcraft_wiki_passthrough(ctx: typer.Context) -> None:
    _run_passthrough(ctx, warcraft_wiki_app, provider_name="warcraft-wiki", prog_name="warcraft-wiki")


@app.command(
    "wowprogress",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def wowprogress_passthrough(ctx: typer.Context) -> None:
    _run_passthrough(ctx, wowprogress_app, provider_name="wowprogress", prog_name="wowprogress")


@app.command(
    "simc",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def simc_passthrough(ctx: typer.Context) -> None:
    _run_passthrough(ctx, simc_app, provider_name="simc", prog_name="simc")


@app.command(
    "raidbots",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def raidbots_passthrough(ctx: typer.Context) -> None:
    _run_passthrough(ctx, raidbots_app, provider_name="raidbots", prog_name="raidbots")


@app.command(
    "blizzard",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def blizzard_passthrough(ctx: typer.Context) -> None:
    _run_passthrough(ctx, blizzard_app, provider_name="blizzard-api", prog_name="blizzard")


@app.command(
    "curseforge",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def curseforge_passthrough(ctx: typer.Context) -> None:
    _run_passthrough(ctx, curseforge_app, provider_name="curseforge", prog_name="curseforge")


def run() -> None:
    app()


if __name__ == "__main__":
    run()

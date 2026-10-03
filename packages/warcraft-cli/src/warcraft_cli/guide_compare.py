"""Guide comparison behind `warcraft guide-compare`, `guide-compare-query` and `guide-builds-simc`.

The Typer commands own the flag surface; this module compares exported guide bundles, orchestrates
the per-provider guide exports a query needs, and hands the bundles' explicit build references to
simc. Provider calls arrive as injected ``ProviderCalls`` so the commands keep their seams in
``warcraft_cli.main``. simc reads each build in memory, so no temp packet file is written.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import typer
from warcraft_content.article_bundle import ArticleBundleError, compare_article_bundles, load_article_bundle
from warcraft_core.cli import fail
from warcraft_core.exit_codes import EXIT_GENERIC, EXIT_USAGE
from warcraft_core.identity import build_reference_transport_packet_payload, parse_wowhead_talent_calc_ref
from warcraft_core.paths import data_root
from warcraft_core.shapes import as_dict, as_list, unique_strings
from warcraft_core.timestamps import iso_now_utc, parse_iso8601_utc

from warcraft_cli.provider_contract import candidate_score
from warcraft_cli.providers import (
    DescribeOptions,
    PacketInput,
    ProviderCalls,
    ProviderInvoke,
    SimcCall,
    failed_call,
    get_provider,
    provider_expansion_exclusion_reason,
    provider_expansion_support,
    provider_payload_data,
    shared_failure,
)

GUIDE_COMPARE_QUERY_PROVIDERS = ("wowhead", "method", "icy-veins")


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


def default_guide_compare_query_root(query: str) -> Path:
    """Where exported bundles land without ``--out-root``: the data root, never the caller's CWD."""
    return data_root() / "guide_compare" / _slugify_path_fragment(query)


def _guide_compare_manifest_path(root: Path) -> Path:
    return root / "manifest.json"


def _guide_compare_freshness(exported_at: Any, *, max_age_hours: int) -> dict[str, Any]:
    parsed = parse_iso8601_utc(exported_at)
    if parsed is None:
        return {"status": "stale", "reason": "missing_exported_at", "age_hours": None, "max_age_hours": max_age_hours}
    age_hours = round((datetime.now(UTC) - parsed).total_seconds() / 3600, 2)
    if age_hours > max_age_hours:
        return {"status": "stale", "reason": "max_age_exceeded", "age_hours": age_hours, "max_age_hours": max_age_hours}
    return {"status": "fresh", "reason": "within_max_age", "age_hours": age_hours, "max_age_hours": max_age_hours}


def _guide_build_handoff_freshness(source_kind: str, source_manifest: dict[str, Any] | None) -> dict[str, Any]:
    updated_at = source_manifest.get("updated_at") if isinstance(source_manifest, dict) else None
    parsed_updated_at = parse_iso8601_utc(updated_at)
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
    parsed_exported_at = parse_iso8601_utc(exported_at)
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
                # Saved so a later run that reuses this bundle still reports the redirect.
                "redirect": row.get("redirect"),
            }
        )
    payload = {
        "kind": "guide_compare_orchestration_manifest",
        "updated_at": iso_now_utc(),
        "query": query,
        "requested_expansion": requested_expansion,
        "max_age_hours": max_age_hours,
        "providers": providers,
    }
    root.mkdir(parents=True, exist_ok=True)
    _guide_compare_manifest_path(root).write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return payload




def load_guide_build_source(source_path: Path) -> tuple[str, list[tuple[Path, dict[str, Any]]], dict[str, Any] | None]:
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
        source_urls=unique_strings(
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
    build: PacketInput | str,
    *,
    decode: bool,
    apl_path: str | None,
    simc: SimcCall,
) -> dict[str, Any | None]:
    identify_result = simc("identify-build", build)
    decode_result = simc("decode-build", build) if decode else None
    describe_result = (
        simc("describe-build", build, describe=DescribeOptions(apl_path=apl_path))
        if isinstance(apl_path, str) and apl_path.strip()
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
        "providers": unique_strings(
            [source_row.get("provider") for source_row in sources if isinstance(source_row, dict)]
        ),
        "bundle_paths": unique_strings(
            [source_row.get("bundle_path") for source_row in sources if isinstance(source_row, dict)]
        ),
        "source_urls": unique_strings(
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
) -> tuple[PacketInput | str | None, dict[str, Any], str | None]:
    """The form simc accepts for this reference type, or the reason the reference cannot be handed over.

    A Wowhead talent-calc URL carries class and spec in its path, so it travels as a talent transport
    packet. A guide-published ``wow_talent_export`` string *is* the build code and is what
    ``simc --build-text`` consumes; sending the raw string as a wowhead reference would fail to parse.

    Returns the build simc reads (an in-memory packet, which names no file, or the build text), the
    reference with its resolved ``build_code``, and the reason it is unusable (``None`` when usable).
    """
    build_url = reference.get("url")
    if not isinstance(build_url, str) or not build_url.strip():
        return None, reference, "missing_reference_url"
    build_code = _resolve_handoff_build_code(reference, build_url)
    if build_code is None:
        return None, reference, "missing_build_code"
    normalized_reference = {**reference, "build_code": build_code}
    reference_type = str(reference.get("reference_type") or "").strip()
    if reference_type == "wow_talent_export":
        return build_code, normalized_reference, None
    transport_packet = _build_handoff_transport_packet(build_url, normalized_reference, sources)
    if isinstance(transport_packet, dict):
        return PacketInput(transport_packet), normalized_reference, None
    return None, normalized_reference, f"unsupported_reference_type:{reference_type or 'unknown'}"


def _build_simc_handoff_row(
    row: dict[str, Any],
    *,
    decode: bool,
    apl_path: str | None,
    simc: SimcCall,
) -> dict[str, Any]:
    """Hand one build reference to simc, or return the excluded row naming why it could not be."""
    reference = as_dict(row.get("reference"))
    sources = as_list(row.get("sources"))
    build, normalized_reference, unusable_reason = _handoff_build_input(reference, sources)
    if build is None or unusable_reason is not None:
        return {
            "status": "excluded",
            "reference": reference,
            "reason": unusable_reason,
            "sources": sources,
        }
    simc_section = _handoff_simc_section(_invoke_simc_for_handoff(build, decode=decode, apl_path=apl_path, simc=simc))
    return {
        "status": "handed_off",
        "reference": normalized_reference,
        "talent_transport_packet": build.packet if isinstance(build, PacketInput) else None,
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
    build_reference_count: int,
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
    ``all_references_excluded`` means the guide had build references but none could be handed to
    simc (``excluded_builds`` names why), which is not the same as a guide with no builds.
    """
    if build_reference_count == 0:
        return "no_build_references"
    if returned_build_count == 0:
        return "all_references_excluded"
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
    rows = [
        {
            "bundle_path": str(bundle_path),
            "provider": as_dict(bundle.get("manifest")).get("provider"),
            "failed_page_count": len(as_list(bundle.get("failed_pages"))),
            # A bundle of a retired guide holds the builds of the guide the site served instead.
            "redirect": as_dict(bundle.get("manifest")).get("redirect"),
        }
        for bundle_path, bundle in bundle_inputs
    ]
    return {
        "bundle_count": len(rows),
        "failed_page_count": sum(row["failed_page_count"] for row in rows),
        "bundles": rows,
    }


def _handoff_citations(
    selected_rows: list[dict[str, Any]],
    bundle_inputs: list[tuple[Path, dict[str, Any]]],
) -> dict[str, Any]:
    return {
        "bundle_paths": [str(path) for path, _bundle in bundle_inputs],
        "build_reference_urls": unique_strings(
            [((row.get("reference") or {}).get("url")) for row in selected_rows if isinstance(row, dict)]
        ),
        "source_urls": unique_strings(
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


def guide_builds_simc_payload(
    *,
    source_path: Path,
    source_kind: str,
    source_manifest: dict[str, Any] | None,
    bundle_inputs: list[tuple[Path, dict[str, Any]]],
    decode: bool,
    apl_path: str | None,
    limit: int,
    simc: SimcCall,
) -> dict[str, Any]:
    """The guide-build-to-simc evidence packet for the explicit build references in ``bundle_inputs``."""
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
        build_row = _build_simc_handoff_row(row, decode=decode, apl_path=apl_path, simc=simc)
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
                build_reference_count=len(handoff_rows),
                returned_build_count=len(build_rows),
                requested_leg_count=len(requested_legs),
                empty_requested_legs=empty_requested_legs,
                partial_requested_legs=partial_requested_legs,
            ),
        },
        "excluded_builds": excluded_rows,
        "builds": build_rows,
    }


def normalize_guide_compare_providers(values: list[str]) -> tuple[str, ...]:
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
    ref = _guide_ref(match)
    if ref is None:
        return None, "resolved_guide_missing_ref"
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
    return candidate_score(results[0]), candidate_score(results[1]) if len(results) > 1 else None


def _search_fallback_rejection(top_score: int, second_score: int | None) -> str | None:
    if top_score < _SEARCH_FALLBACK_MIN_TOP_SCORE:
        return f"search_top_guide_score_too_low:{top_score}"
    if second_score is not None and top_score < second_score + _SEARCH_FALLBACK_MIN_MARGIN:
        return "search_results_not_decisive"
    if second_score is None and top_score < _SEARCH_FALLBACK_MIN_SINGLE_SCORE:
        return "search_single_result_not_strong_enough"
    return None


def _guide_ref(row: dict[str, Any]) -> str | None:
    """The ref a guide provider's guide-export takes: the row's ``id``, else its ``metadata.slug``."""
    raw_ref = row.get("id")
    if raw_ref is None:
        raw_ref = as_dict(row.get("metadata")).get("slug")
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
    ref = _guide_ref(top)
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


def _resolve_guide_compare_candidate(
    provider_name: str,
    query: str,
    *,
    expansion: str | None,
    calls: ProviderCalls,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """The provider's guide for ``query``: its resolved guide, else a decisive search top hit.

    Returns ``(candidate, decline)``; ``decline`` is the provider row when there is no candidate,
    naming why each step declined. Neither call gets a --limit: the resolve confidence and the
    search margin both need the rivals a small limit would hide.
    """
    resolved = calls.resolve(provider_name, query, expansion=expansion)
    candidate, resolve_reason = _resolved_guide_match(provider_name, provider_payload_data(resolved.get("payload")))
    if candidate is not None:
        return candidate, {}
    searched = calls.search(provider_name, query, expansion=expansion)
    candidate, search_reason = _search_fallback_guide_match(provider_name, provider_payload_data(searched.get("payload")))
    if candidate is not None:
        return candidate, {}
    resolve_failure = failed_call(resolved)
    decline: dict[str, Any] = {
        "provider": provider_name,
        "status": "skipped",
        "reason": search_reason,
        "resolve_reason": "provider_failed" if resolve_failure is not None else resolve_reason,
        "resolve": resolved.get("payload"),
        "search": searched.get("payload"),
    }
    # A failed call means the provider was never asked properly, so "no guide" would be a guess.
    # Search is the last step, so its failure is the one reported when both failed.
    failure = failed_call(searched) or resolve_failure
    if failure is not None:
        decline.update(status="error", reason="provider_failed", error=failure[0], exit_code=failure[1])
    return None, decline


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
        "error": {"code": "invalid_bundle", "message": error},
        "exit_code": EXIT_GENERIC,
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
        "redirect": existing_row.get("redirect") if existing_row is not None else None,
    }, (export_dir, bundle)


def _guide_compare_export_row(
    provider_name: str,
    *,
    candidate: dict[str, Any],
    export_dir: Path,
    freshness: dict[str, Any],
    expansion: str | None,
    max_age_hours: int,
    invoke: ProviderInvoke,
) -> tuple[dict[str, Any], tuple[Path, dict[str, Any]] | None]:
    """Run the provider's guide-export into ``export_dir`` and load the bundle it wrote."""
    export_result = invoke(
        provider_name,
        ["guide-export", candidate["ref"], "--out", str(export_dir)],
        expansion=expansion,
    )
    failure = failed_call(export_result)
    if failure is not None:
        return {
            "provider": provider_name,
            "status": "error",
            "reason": "guide_export_failed",
            "candidate": candidate,
            # The export wrote nothing usable, so there is no bundle to point at.
            "bundle_path": None,
            "freshness": freshness,
            "error": failure[0],
            "exit_code": failure[1],
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
    # cannot disagree on freshness). The `or iso_now_utc()` is an unreachable safety net, NOT a
    # freshness fabricator: every guide-compare provider stamps exported_at on export (wowhead via
    # _guide_export_manifest, method/icy-veins via write_article_bundle), and this branch runs only
    # after guide-export above re-wrote the bundle now — so "now" would reflect a real just-happened
    # export, never a stale reuse. A timestamp-less bundle is also never *reused*: the reuse gate
    # requires freshness "fresh" and _guide_compare_freshness(None) is always "stale", which forces a
    # re-export (re-stamping a real anchor). So a bundle lacking a real anchor cannot be stamped here
    # and then treated as freshly exported on a later run.
    bundle_manifest = as_dict(bundle.get("manifest"))
    exported_at = bundle_manifest.get("exported_at") or iso_now_utc()
    return {
        "provider": provider_name,
        "status": "exported",
        "candidate": candidate,
        "bundle_path": str(export_dir),
        "freshness": _guide_compare_freshness(exported_at, max_age_hours=max_age_hours),
        "exported_at": exported_at,
        # Set when the provider served another guide than the candidate (a retired, redirected page).
        "redirect": as_dict(as_dict(export_result.get("payload")).get("data")).get("redirect"),
        "export": export_result.get("payload"),
    }, (export_dir, bundle)


def _process_guide_compare_provider(
    provider_name: str,
    *,
    query: str,
    requested_expansion: str | None,
    max_age_hours: int,
    force_refresh: bool,
    orchestration_root: Path,
    manifest_by_provider: dict[str, dict[str, Any]],
    calls: ProviderCalls,
) -> tuple[dict[str, Any], tuple[Path, dict[str, Any]] | None]:
    candidate, decline = _resolve_guide_compare_candidate(provider_name, query, expansion=requested_expansion, calls=calls)
    if candidate is None:
        return decline, None

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
        invoke=calls.invoke,
    )


@dataclass(frozen=True, slots=True)
class GuideCompareQueryOptions:
    """One orchestration run of `warcraft guide-compare-query`, after its flags are validated."""

    query: str
    providers: tuple[str, ...]
    orchestration_root: Path
    requested_expansion: str | None
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


def _guide_compare_expansion_skips(providers: tuple[str, ...], requested_expansion: str | None) -> list[dict[str, Any]]:
    """A ``skipped`` row for each selected provider that does not serve the requested expansion."""
    rows: list[dict[str, Any]] = []
    for provider_name in providers:
        registration = get_provider(provider_name)
        reason = provider_expansion_exclusion_reason(registration, requested_expansion=requested_expansion)
        if reason is not None:
            support = provider_expansion_support(registration, requested_expansion=requested_expansion)
            rows.append({"provider": provider_name, "status": "skipped", "reason": reason, "expansion_support": support})
    return rows


def _guide_compare_decline_row(provider_row: dict[str, Any]) -> dict[str, Any]:
    """Why one provider did not contribute a bundle, small enough to live inside ``error.details``."""
    return {
        "provider": provider_row.get("provider"),
        "status": provider_row.get("status"),
        "reason": provider_row.get("reason"),
        "resolve_reason": provider_row.get("resolve_reason"),
        "candidate_ref": as_dict(provider_row.get("candidate")).get("ref"),
        "bundle_path": provider_row.get("bundle_path"),
        "error": provider_row.get("error"),
    }


def _insufficient_guides_error(provider_rows: list[dict[str, Any]]) -> tuple[dict[str, Any], int]:
    """The failure for a run that exported fewer than two bundles, and its exit code.

    When every provider that contributed nothing failed (an outage, not a missing guide) the run
    fails with those providers' shared code and exit code, so an agent retries instead of concluding
    no guide exists. Otherwise it is ``insufficient_guides``, exit 1.
    """
    failed = [row for row in provider_rows if row.get("status") == "error"]
    empty = [row for row in provider_rows if row.get("status") not in ("exported", "reused")]
    if failed and len(failed) == len(empty):
        code, exit_code = shared_failure(
            [{**as_dict(row.get("error")), "exit_code": row.get("exit_code")} for row in failed]
        )
        message = f"{len(failed)} guide providers failed, so fewer than two guide bundles exported."
        return {"code": code, "message": message}, exit_code
    return {"code": "insufficient_guides", "message": "Need at least two exported guide bundles to compare."}, EXIT_GENERIC


def simc_handoff_failure(summary: Mapping[str, Any]) -> dict[str, Any]:
    """The error for a simc build handoff whose every requested leg failed for every build."""
    return {
        "code": "simc_handoff_failed",
        "message": (
            f"Every requested simc leg ({', '.join(summary['empty_requested_legs'])}) failed for all "
            f"{summary['returned_build_count']} build references; the packet carries no usable simc "
            "output. Each build's `failures` names the simc error; check `warcraft simc doctor`."
        ),
    }


def guide_compare_query_payload(options: GuideCompareQueryOptions, calls: ProviderCalls) -> tuple[dict[str, Any], int]:
    """Export each selected provider's guide bundle and compare them.

    Returns the payload to emit and its exit code. The payload is ``ok: False`` when fewer than two
    bundles exported, or when the requested simc handoff failed for every leg; the comparison then
    rides along in ``error.details``.
    """
    provider_rows = _guide_compare_expansion_skips(options.providers, options.requested_expansion)
    skipped = {row["provider"] for row in provider_rows}
    eligible = [provider_name for provider_name in options.providers if provider_name not in skipped]
    if len(eligible) < 2:
        # Decided before any provider call: this flag combination can never export two guides.
        message = (
            f"guide-compare-query needs at least two guide providers, but only {len(eligible)} of the selected "
            f"providers ({', '.join(options.providers)}) serve expansion {options.requested_expansion or 'retail'}."
        )
        details = {"selected_providers": list(options.providers), "provider_results": provider_rows}
        return {"ok": False, "query": options.query, "error": {"code": "invalid_argument", "message": message,
                                                                "details": details}}, EXIT_USAGE
    manifest_by_provider = _guide_compare_manifest_index(options.orchestration_root)
    bundle_inputs: list[tuple[Path, dict[str, Any]]] = []
    for provider_name in eligible:
        provider_row, bundle_input = _process_guide_compare_provider(
            provider_name,
            query=options.query,
            requested_expansion=options.requested_expansion,
            max_age_hours=options.max_age_hours,
            force_refresh=options.force_refresh,
            orchestration_root=options.orchestration_root,
            manifest_by_provider=manifest_by_provider,
            calls=calls,
        )
        provider_rows.append(provider_row)
        if bundle_input is not None:
            bundle_inputs.append(bundle_input)

    if len(bundle_inputs) < 2:
        # The manifest describes a completed comparison, so a failed run writes none: it must not
        # leave a `providers: []` manifest behind for the next run to reuse.
        error, exit_code = _insufficient_guides_error(provider_rows)
        error["details"] = {
            "exported_bundle_count": len(bundle_inputs),
            "required_bundle_count": 2,
            "selected_providers": list(options.providers),
            "provider_results": [_guide_compare_decline_row(row) for row in provider_rows],
        }
        return {"ok": False, "query": options.query, "error": error}, exit_code

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
    payload = {
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
        "simc_build_handoff": guide_builds_simc_payload(
            source_path=options.orchestration_root,
            source_kind="orchestration_root",
            source_manifest=manifest,
            bundle_inputs=bundle_inputs,
            decode=options.simc_decode,
            apl_path=options.simc_apl_path,
            limit=options.simc_build_limit,
            simc=calls.simc,
        )
        if include_simc_build_handoff
        else None,
    }
    summary = as_dict(as_dict(payload["simc_build_handoff"]).get("summary"))
    if summary.get("simc_handoff_status") == "all_handoffs_failed":
        return {**payload, "ok": False, "error": simc_handoff_failure(summary)}, EXIT_GENERIC
    return payload, 0


def guide_compare_payload(ctx: typer.Context, bundles: list[Path], *, max_age_hours: int) -> dict[str, Any]:
    """Compare two or more already-exported guide bundles, failing on a bundle that does not load."""
    if len(bundles) < 2:
        fail(ctx, "invalid_argument", "guide-compare requires at least two exported guide bundles.")

    bundle_inputs: list[tuple[Path, dict[str, Any]]] = []
    for bundle_path in bundles:
        resolved_path = bundle_path.expanduser()
        try:
            bundle_inputs.append((resolved_path, load_article_bundle(resolved_path)))
        except ArticleBundleError as exc:
            fail(ctx, exc.code, exc.message, details={"bundle": str(resolved_path)})

    try:
        packet = _guide_comparison_packet(bundle_inputs, max_age_hours=max_age_hours)
    except ArticleBundleError as exc:  # the same bundle given twice
        fail(ctx, exc.code, exc.message, details=exc.details)
    return {"provider": "warcraft", **packet}

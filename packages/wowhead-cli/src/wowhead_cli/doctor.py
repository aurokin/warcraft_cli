"""Wowhead endpoint preflight checks for `wowhead doctor`."""

from __future__ import annotations

import time
from typing import Any

import httpx

from wowhead_cli.expansion_profiles import (
    ExpansionProfile,
    build_entity_url,
    build_search_suggestions_url,
    build_tooltip_url,
)
from wowhead_cli.page_parser import (
    extract_comments_dataset,
    extract_linked_entities_from_href,
    parse_page_meta_json,
    parse_page_metadata,
)

DOCTOR_QUERY = "thunderfury"
DOCTOR_ENTITY_TYPE = "item"
DOCTOR_ENTITY_ID = 19019


def _latency_bucket(latency_ms: float) -> str:
    if latency_ms < 500:
        return "fast"
    if latency_ms < 2000:
        return "moderate"
    return "slow"


def _probe_result(
    *,
    ok: bool,
    latency_ms: float,
    status_code: int | None = None,
    error: str | None = None,
    shape: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": ok,
        "latency_ms": round(latency_ms, 1),
        "latency_bucket": _latency_bucket(latency_ms),
    }
    if status_code is not None:
        payload["status_code"] = status_code
    if error is not None:
        payload["error"] = error
    if shape is not None:
        payload["shape"] = shape
    return payload


def _probe_search_suggestions(profile: ExpansionProfile, *, timeout_seconds: float) -> dict[str, Any]:
    url = build_search_suggestions_url(profile)
    started = time.perf_counter()
    try:
        with httpx.Client(timeout=timeout_seconds, follow_redirects=True) as client:
            response = client.get(url, params={"q": DOCTOR_QUERY})
            latency_ms = (time.perf_counter() - started) * 1000
            response.raise_for_status()
            payload = response.json()
            results = payload.get("results") if isinstance(payload, dict) else None
            shape_ok = isinstance(results, list) and len(results) > 0
            return _probe_result(
                ok=shape_ok,
                latency_ms=latency_ms,
                status_code=response.status_code,
                error=None if shape_ok else "search results missing or empty",
                shape={"result_count": len(results) if isinstance(results, list) else 0},
            )
    except Exception as exc:  # noqa: BLE001
        latency_ms = (time.perf_counter() - started) * 1000
        return _probe_result(ok=False, latency_ms=latency_ms, error=str(exc))


def _probe_tooltip(profile: ExpansionProfile, *, timeout_seconds: float) -> dict[str, Any]:
    url = build_tooltip_url(profile, DOCTOR_ENTITY_TYPE, DOCTOR_ENTITY_ID)
    started = time.perf_counter()
    try:
        with httpx.Client(timeout=timeout_seconds, follow_redirects=True) as client:
            response = client.get(url, params={"dataEnv": profile.data_env})
            latency_ms = (time.perf_counter() - started) * 1000
            response.raise_for_status()
            payload = response.json()
            name = payload.get("name") if isinstance(payload, dict) else None
            tooltip = payload.get("tooltip") if isinstance(payload, dict) else None
            shape_ok = isinstance(name, str) and tooltip is not None
            return _probe_result(
                ok=shape_ok,
                latency_ms=latency_ms,
                status_code=response.status_code,
                error=None if shape_ok else "tooltip payload missing name or tooltip",
                shape={"has_name": isinstance(name, str), "has_tooltip": tooltip is not None},
            )
    except Exception as exc:  # noqa: BLE001
        latency_ms = (time.perf_counter() - started) * 1000
        return _probe_result(ok=False, latency_ms=latency_ms, error=str(exc))


def _probe_entity_page(profile: ExpansionProfile, *, timeout_seconds: float) -> dict[str, Any]:
    url = build_entity_url(profile, DOCTOR_ENTITY_TYPE, DOCTOR_ENTITY_ID)
    started = time.perf_counter()
    try:
        with httpx.Client(timeout=timeout_seconds, follow_redirects=True) as client:
            response = client.get(url)
            latency_ms = (time.perf_counter() - started) * 1000
            response.raise_for_status()
            html = response.text
            meta = parse_page_metadata(html, fallback_url=url)
            page_meta = parse_page_meta_json(html)
            linked = extract_linked_entities_from_href(html, source_url=meta.get("canonical_url") or url)
            comments = extract_comments_dataset(html)
            data_env = page_meta.get("dataEnv") if isinstance(page_meta, dict) else None
            env_ok = isinstance(data_env, dict) and data_env.get("env") == profile.data_env
            shape_ok = isinstance(meta.get("canonical_url"), str) and len(linked) > 0 and env_ok
            return _probe_result(
                ok=shape_ok,
                latency_ms=latency_ms,
                status_code=response.status_code,
                error=None if shape_ok else "entity page parser checks failed",
                shape={
                    "linked_entity_count": len(linked),
                    "comment_count": len(comments) if isinstance(comments, list) else 0,
                    "data_env_match": env_ok,
                },
            )
    except Exception as exc:  # noqa: BLE001
        latency_ms = (time.perf_counter() - started) * 1000
        return _probe_result(ok=False, latency_ms=latency_ms, error=str(exc))


def build_doctor_payload(
    profile: ExpansionProfile,
    *,
    live: bool,
    cache: dict[str, Any],
    timeout_seconds: float = 20.0,
) -> dict[str, Any]:
    endpoints: dict[str, Any] = {
        "search_suggestions": {"ok": None, "skipped": True, "reason": "live probes disabled"},
        "tooltip": {"ok": None, "skipped": True, "reason": "live probes disabled"},
        "entity_page": {"ok": None, "skipped": True, "reason": "live probes disabled"},
    }
    if live:
        endpoints = {
            "search_suggestions": _probe_search_suggestions(profile, timeout_seconds=timeout_seconds),
            "tooltip": _probe_tooltip(profile, timeout_seconds=timeout_seconds),
            "entity_page": _probe_entity_page(profile, timeout_seconds=timeout_seconds),
        }

    probe_results = [row for row in endpoints.values() if row.get("skipped") is not True]
    failures = [name for name, row in endpoints.items() if row.get("skipped") is not True and not row.get("ok")]
    status = "ready"
    if live and failures:
        status = "degraded" if len(failures) < len(probe_results) else "error"

    return {
        "provider": "wowhead",
        "status": status,
        "command": "doctor",
        "installed": True,
        "language": "python",
        "expansion": profile.key,
        "capabilities": {
            "search": "ready",
            "resolve": "ready",
            "entity": "ready",
            "entity_page": "ready",
            "comments": "ready",
            "compare": "ready",
        },
        "cache": cache,
        "endpoints": endpoints,
        "failed_probes": failures,
    }

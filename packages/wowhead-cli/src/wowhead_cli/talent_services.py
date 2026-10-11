"""Output-free Wowhead talent reference parsing, page enrichment and transport packets."""

from __future__ import annotations

import json
import re
from typing import Any

from warcraft_core.identity import (
    RETAIL_TALENT_CALCULATORS,
    TIERED_TALENT_CALCULATORS,
    WowheadTalentCalcRef,
    WowheadTalentCalcRefError,
    build_identity_payload,
    build_reference_transport_packet_payload,
    normalize_actor_class,
    parse_wowhead_talent_calc,
    validate_talent_transport_packet,
)
from warcraft_core.provider import ProviderError
from warcraft_core.wow_specs import WOW_SPECS

from wowhead_cli import provider
from wowhead_cli.classic_talents import CLASSIC_CALCULATORS, decode_build, talent_data_url, undecodable_reason
from wowhead_cli.expansion_profiles import ExpansionProfile
from wowhead_cli.listing_filters import absolute_wowhead_url
from wowhead_cli.page_parser import extract_json_script, parse_page_metadata
from wowhead_cli.wowhead_client import WowheadClient

# Retail build codes are Blizzard loadout strings (base64 letters, digits, ``+``); classic ones are digits and ``-``.
_TALENT_CALC_BUILD_CODE_RE = re.compile(r"[A-Za-z0-9+_-]+")
# After a classic build code: its selection order (TalentCalcClassic's two rank alphabets); after a MoP one, glyphs.
_TALENT_CALC_EXTRA_SEGMENT_RE = re.compile(r"[A-Za-z0-9.!_~^-]+")
# Blizzard specialization ids by class and spec key. Wowhead's listed builds carry one as ``spec``, and
# a retail build code's loadout header encodes one.
_WOW_SPEC_IDS: dict[tuple[str, str], int] = {(spec.class_key, spec.key): spec.spec_id for spec in WOW_SPECS}
_WOW_SPEC_BY_ID = {spec_id: class_spec for class_spec, spec_id in _WOW_SPEC_IDS.items()}
_LOADOUT_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
_RETAIL_BUILD_CODE_REASON = (
    "Retail build codes are Blizzard loadout strings; `warcraft talent-describe` or `simc describe-build` decodes them."
)


def _talent_calc_state(ref: str, *, default_expansion: str) -> dict[str, Any]:
    """Split a talent-calc ref into its URL, calculator, class, spec and build code; raises ValueError.

    The shared parser reads the path; Wowhead adds the build code checks: the characters a code uses
    and, on a retail calculator, that the loadout header names the path's spec.
    """
    parsed = parse_wowhead_talent_calc(ref, default_expansion=default_expansion)
    if isinstance(parsed, WowheadTalentCalcRefError):
        raise ValueError(parsed.message)
    build_code = parsed.build_code
    spec_id: int | None = None
    if parsed.spec is not None and parsed.expansion in RETAIL_TALENT_CALCULATORS:
        spec_id = _talent_calc_spec_id(parsed, build_code)
    if build_code is not None and _TALENT_CALC_BUILD_CODE_RE.fullmatch(build_code) is None:
        raise ValueError(f"Talent calculator build code {build_code!r} holds characters no build code uses.")
    extra = parsed.extra_segment
    if extra is not None and _TALENT_CALC_EXTRA_SEGMENT_RE.fullmatch(extra) is None:
        raise ValueError(f"Talent calculator segment {extra!r} after the build code holds characters no calculator uses.")
    return {
        "state_url": parsed.reference_url,
        "expansion": parsed.expansion,
        "actor_class": parsed.actor_class,
        "class_slug": parsed.class_slug,
        "spec_slug": parsed.spec_slug,
        "spec_id": spec_id,
        "build_code": build_code,
        "extra_segment": extra,
        "path_segments": list(parsed.path_segments),
        "has_build_code": build_code is not None,
    }


def _loadout_spec_id(build_code: str) -> int | None:
    """The spec id in a Blizzard loadout string's header, an 8-bit version then a 16-bit spec id.

    The string packs 6 bits per character, least significant first, so the first four characters
    hold the header. Returns None for a code that is not a known spec's loadout string.
    """
    header = build_code[:4]
    if len(header) < 4 or any(char not in _LOADOUT_ALPHABET for char in header):
        return None
    bits = sum(_LOADOUT_ALPHABET.index(char) << (6 * index) for index, char in enumerate(header))
    spec_id = bits >> 8
    return spec_id if spec_id in _WOW_SPEC_BY_ID else None


def _talent_calc_spec_id(parsed: WowheadTalentCalcRef, build_code: str | None) -> int:
    """The spec id a retail talent-calc path names; raises when the build code is another spec's loadout."""
    spec_id = _WOW_SPEC_IDS[(parsed.actor_class, parsed.spec or "")]
    encoded = _loadout_spec_id(build_code) if build_code else None
    if encoded is not None and encoded != spec_id:
        encoded_class, encoded_spec = _WOW_SPEC_BY_ID[encoded]
        raise ValueError(f"Build code is a {encoded_class}/{encoded_spec} loadout, not {parsed.class_slug}/{parsed.spec_slug}.")
    return spec_id


def _extract_talent_calc_listed_builds(html: str, *, spec_id: int, limit: int) -> dict[str, Any] | None:
    """The builds Wowhead lists for ``spec_id``; the page embeds every spec's builds in one block."""
    try:
        payload = extract_json_script(html, "data.wow.talentCalcDragonflight.live.talentBuilds")
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    rows: list[dict[str, Any]] = []
    for raw_id, raw_row in payload.items():
        if not isinstance(raw_row, dict) or raw_row.get("spec") != spec_id:
            continue
        row_id = raw_row.get("id")
        if not isinstance(row_id, int):
            try:
                row_id = int(raw_id)
            except ValueError:
                row_id = None
        row = {
            "id": row_id,
            "name": raw_row.get("name"),
            "hash": raw_row.get("hash"),
            "spec_id": raw_row.get("spec"),
            "listed": raw_row.get("isListed"),
        }
        rows.append(row)
    rows.sort(key=lambda row: (row.get("name") or "", row.get("id") or 0))
    return {
        "count": len(rows),
        "items": rows[:limit],
    }


def base_talent_calc_payload(
    expansion: ExpansionProfile,
    *,
    ref: str,
) -> dict[str, Any]:
    try:
        state = _talent_calc_state(ref, default_expansion=expansion.key)
    except ValueError as exc:
        raise ProviderError("invalid_tool_ref", str(exc)) from exc
    state_url = state.pop("state_url")
    actor_class = state.pop("actor_class")
    named = bool(state.get("spec_slug"))
    return {
        "expansion": state["expansion"],
        "tool": {
            "kind": "talent-calc",
            "input": ref,
            "state_url": state_url,
            "page_url": state_url,
            **state,
        },
        "build_identity": build_identity_payload(
            actor_class=actor_class,
            spec=state.get("spec_slug"),
            confidence="high" if named else "none",
            source="wowhead_talent_calc_url",
            candidates=[(actor_class, state.get("spec_slug"))] if named else None,
            source_notes=["class/spec came from the explicit Wowhead talent-calc URL path"],
        ),
        "page": {
            "title": None,
            "description": None,
            "canonical_url": None,
        },
        "citations": {
            "page": state_url,
        },
    }


def _talents_block(client: WowheadClient, html: str, tool: dict[str, Any]) -> dict[str, Any]:
    """The decoded ``talents`` of a classic calculator build, or ``decoded: false`` with the reason it is not decoded."""
    calculator = str(tool["expansion"])
    if calculator not in CLASSIC_CALCULATORS:
        return {"decoded": False, "reason": _RETAIL_BUILD_CODE_REASON}
    extra_key = "glyphs_code" if calculator in TIERED_TALENT_CALCULATORS else "selection_order"
    extra = {extra_key: tool["extra_segment"]} if tool["extra_segment"] else {}
    reason = undecodable_reason(calculator, spec_named=tool["spec_slug"] is not None)
    if reason is not None:
        return {"decoded": False, "reason": reason, **extra}
    data_url = talent_data_url(html)
    if data_url is None:
        return {"decoded": False, "reason": "The calculator page names no talent data file.", **extra}
    try:
        with provider.transport_errors():
            data = client.talent_calc_data(data_url)
    except ProviderError as exc:
        return {
            "decoded": False,
            "reason": "The talent data file could not be fetched.",
            "fetch_error": {"code": exc.code, "message": exc.message},
            "data_url": data_url,
            **extra,
        }
    except ValueError as exc:
        return {"decoded": False, "reason": f"The talent data file did not parse: {exc}", "data_url": data_url, **extra}
    actor_class = normalize_actor_class(tool["class_slug"]) or ""
    decoded = decode_build(data, calculator=calculator, actor_class=actor_class, build_code=str(tool["build_code"]))
    return {**decoded, **extra, "data_url": data_url}


def enrich_talent_calc_payload(
    client: WowheadClient,
    payload: dict[str, Any],
    *,
    listed_build_limit: int,
    fail_on_fetch_error: bool,
    decode_talents: bool = False,
) -> dict[str, Any]:
    state_url = str(payload["tool"]["state_url"])
    try:
        with provider.transport_errors():
            html = client.page_html(state_url)
    except ProviderError as exc:
        if fail_on_fetch_error:
            raise
        # The build code in the URL still answers; the page metadata and listed builds are what is missing.
        return {**payload, "page": {**payload["page"], "fetch_error": {"code": exc.code, "message": exc.message}}}
    metadata = parse_page_metadata(html, fallback_url=state_url)
    page_url = absolute_wowhead_url(metadata.get("canonical_url"), fallback=state_url) or state_url
    enriched_payload = dict(payload)
    enriched_tool = dict(payload["tool"])
    enriched_tool["page_url"] = page_url
    enriched_payload["tool"] = enriched_tool
    enriched_payload["page"] = {
        "title": metadata.get("title"),
        "description": metadata.get("description"),
        "canonical_url": page_url,
    }
    spec_id = payload["tool"]["spec_id"]
    listed_builds = None if spec_id is None else _extract_talent_calc_listed_builds(html, spec_id=spec_id, limit=listed_build_limit)
    if listed_builds is not None:
        enriched_payload["listed_builds"] = listed_builds
    if decode_talents and enriched_tool["build_code"] is not None:
        enriched_payload["talents"] = _talents_block(client, html, enriched_tool)
    return enriched_payload


def require_packet_reference(payload: dict[str, Any]) -> None:
    if not payload["tool"].get("has_build_code"):
        raise ProviderError("invalid_tool_ref", "talent-calc packet refs must include an explicit build code.")
    if payload["tool"].get("spec_slug") is None:
        raise ProviderError("invalid_tool_ref", "talent-calc packet refs must name a spec: /talent-calc/<class>/<spec>/<build-code>.")


def talent_packet_payload(client: WowheadClient, payload: dict[str, Any], *, listed_build_limit: int) -> dict[str, Any]:
    require_packet_reference(payload)
    payload = enrich_talent_calc_payload(client, payload, listed_build_limit=listed_build_limit, fail_on_fetch_error=False)
    packet = build_reference_transport_packet_payload(
        ref=str(payload["tool"]["state_url"]),
        provider="wowhead",
        source="wowhead_talent_calc_url",
        source_url=str(payload["page"]["canonical_url"] or payload["tool"]["state_url"]),
        notes=["exact transport packet came from an explicit Wowhead talent-calc ref"],
        scope={"type": "wowhead_talent_calc", "expansion": str(payload["tool"]["expansion"])},
    )
    try:
        packet = validate_talent_transport_packet(packet)
    except ValueError as exc:
        raise ProviderError(
            "invalid_transport_packet", f"wowhead talent-calc-packet produced an invalid talent transport packet: {exc}"
        ) from exc
    return {**payload, "talent_transport_packet": packet, "written_packet_path": None}

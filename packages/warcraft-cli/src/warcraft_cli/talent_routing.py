"""Talent routing behind `warcraft talent-packet` and `warcraft talent-describe`.

The Typer commands own the flag surface; this module routes a source (a Wowhead talent-calc ref, a
Warcraft Logs report actor, or a packet file) to a validated talent transport packet, upgrades a
raw packet through simc, and describes the build with simc. Provider calls arrive as injected
``ProviderCalls`` so the commands keep their seams in ``warcraft_cli.main``. simc reads the packet
in memory: no temp file is written, so none can surface in the output.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import urlparse

import typer
from warcraft_core.cli import fail
from warcraft_core.exit_codes import EXIT_USAGE
from warcraft_core.expansions import wowhead_path_prefixes
from warcraft_core.identity import validate_talent_transport_packet
from warcraft_core.shapes import as_dict
from warcraft_core.wow_specs import WOW_CLASS_NAMES, raiderio_class_slug

from warcraft_cli.providers import (
    DescribeOptions,
    PacketInput,
    ProviderCalls,
    ProviderInvoke,
    SimcCall,
    failed_call,
    provider_payload_data,
)

# Wowhead class path segments that precede /talent-calc in a bare (non-URL) reference: deathknight or death-knight.
_WOWHEAD_CLASS_SLUGS = frozenset(WOW_CLASS_NAMES) | {raiderio_class_slug(class_key) for class_key in WOW_CLASS_NAMES}
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
            route=route,
            provider_result=provider_result,
        )


def _upgrade_transport_packet_with_simc(
    packet: dict[str, Any],
    *,
    packet_path: str | None,
    simc: SimcCall,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    result = simc("validate-talent-transport", PacketInput(packet, packet_path))
    if failed_call(result) is not None:
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


def _fail_talent_route(
    ctx: typer.Context,
    *,
    code: str,
    message: str,
    source: str,
    route: dict[str, Any] | None = None,
    provider_result: dict[str, Any] | None = None,
    exit_code: int | None = None,
) -> NoReturn:
    details: dict[str, Any] = {"source": source}
    if route is not None:
        details["route"] = route
    if provider_result is not None:
        details["provider_result"] = provider_result
        # A failed provider's own exit code covers its provider-specific codes; otherwise map ``code``.
        provider_exit = provider_result.get("exit_code")
        exit_code = provider_exit if isinstance(provider_exit, int) and provider_exit != 0 else None
    fail(ctx, code, message, exit_code=exit_code, details=details)


def _transport_packet_from_provider_result(
    ctx: typer.Context,
    *,
    source: str,
    route: dict[str, Any],
    provider_result: dict[str, Any],
    command_name: str,
) -> dict[str, Any]:
    producer_payload = provider_payload_data(provider_result.get("payload"))
    failure = failed_call(provider_result)
    if failure is not None:
        _fail_talent_route(
            ctx,
            code=str(failure[0]["code"]),
            message=str(failure[0]["message"]),
            source=source,
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
            route=route,
            provider_result=provider_result,
        )


def _wowhead_transport_packet(
    ctx: typer.Context,
    *,
    source: str,
    listed_build_limit: int,
    requested_expansion: str | None,
    invoke: ProviderInvoke,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    route = {"kind": "wowhead_talent_calc", "provider": "wowhead"}
    producer_result = invoke(
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
    invoke: ProviderInvoke,
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
    producer_result = invoke("warcraftlogs", args, expansion=requested_expansion)
    packet = _transport_packet_from_provider_result(
        ctx,
        source=source,
        route=route,
        provider_result=producer_result,
        command_name="warcraftlogs report-player-talents",
    )
    return route, producer_result, packet


def _maybe_upgrade_transport_packet(
    ctx: typer.Context,
    *,
    source: str,
    route: dict[str, Any],
    packet: dict[str, Any],
    validate: bool,
    simc: SimcCall,
) -> tuple[str | None, bool, bool, dict[str, Any] | None, dict[str, Any]]:
    source_status = packet.get("transport_status") if isinstance(packet.get("transport_status"), str) else None
    upgrade_result: dict[str, Any] | None = None
    packet_changed = False
    upgrade_attempted = bool(validate and source_status == "raw_only")
    if upgrade_attempted:
        original_packet = packet
        try:
            # A packet read from a file is the file's content, so simc cites that file; a routed one has none.
            upgrade_result, upgraded_packet = _upgrade_transport_packet_with_simc(
                packet, packet_path=route.get("packet_path"), simc=simc
            )
        except ValueError as exc:
            _fail_talent_route(
                ctx,
                code="packet_upgrade_failed",
                message=f"simc validate-talent-transport returned an invalid upgraded packet: {exc}",
                source=source,
                route=route,
            )
        if (failure := failed_call(upgrade_result)) is not None:
            _fail_talent_route(
                ctx,
                code=str(failure[0]["code"]),
                message=str(failure[0]["message"]),
                source=source,
                route=route,
                provider_result=upgrade_result,
            )
        if upgraded_packet is None:
            _fail_talent_route(
                ctx,
                code="packet_upgrade_failed",
                message="simc validate-talent-transport did not return an upgraded talent transport packet.",
                source=source,
                route=route,
                provider_result=upgrade_result,
            )
        packet = upgraded_packet
        packet_changed = _transport_packet_upgraded(original_packet, packet)
    return source_status, upgrade_attempted, packet_changed, upgrade_result, packet


@dataclass(frozen=True, slots=True)
class TalentSource:
    """Where `talent-packet` and `talent-describe` read a build from, and whether simc may upgrade it."""

    source: str
    actor_id: int | None
    fight_id: int | None
    allow_unlisted: bool
    listed_build_limit: int
    validate: bool
    expansion: str | None


def _resolve_talent_transport(ctx: typer.Context, request: TalentSource, calls: ProviderCalls) -> dict[str, Any]:
    source = request.source
    requested_expansion = request.expansion
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
            code="not_found",
            message=f"Talent transport packet file was not found: {source}",
            source=source,
        )
    elif _looks_like_wowhead_talent_calc_reference(source):
        route, producer_result, packet = _wowhead_transport_packet(
            ctx,
            source=source,
            listed_build_limit=request.listed_build_limit,
            requested_expansion=requested_expansion,
            invoke=calls.invoke,
        )
    elif request.actor_id is not None and _looks_like_warcraftlogs_report_reference(source):
        route, producer_result, packet = _warcraftlogs_transport_packet(
            ctx,
            source=source,
            actor_id=request.actor_id,
            fight_id=request.fight_id,
            allow_unlisted=request.allow_unlisted,
            requested_expansion=requested_expansion,
            invoke=calls.invoke,
        )
    elif _looks_like_transport_packet_path_input(source):
        _fail_talent_route(
            ctx,
            code="not_found",
            message=f"Talent transport packet file was not found: {source}",
            source=source,
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
            exit_code=EXIT_USAGE,
        )

    source_status, upgrade_attempted, packet_changed, upgrade_result, packet = _maybe_upgrade_transport_packet(
        ctx,
        source=source,
        route=route,
        packet=packet,
        validate=request.validate,
        simc=calls.simc,
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


def talent_packet_payload(ctx: typer.Context, request: TalentSource, *, out: str | None, calls: ProviderCalls) -> dict[str, Any]:
    """Route the source to a talent transport packet, writing it to ``out`` when given."""
    resolved = _resolve_talent_transport(ctx, request, calls)
    written_packet_path = _write_transport_packet_or_fail(
        ctx,
        path_value=out,
        packet=resolved["talent_transport_packet"],
        source=request.source,
        route=resolved["route"],
        provider_result=resolved["producer_result"],
    )
    return {"provider": "warcraft", "kind": "talent_transport", **resolved, "written_packet_path": written_packet_path}


def _described_packet_path(route: dict[str, Any], *, packet_out: str | None, upgraded: bool) -> str | None:
    """The file that holds exactly the packet simc describes: ``--packet-out``, else an unchanged packet file.

    ``None`` when no file does, so the describe output cites none.
    """
    if isinstance(packet_out, str) and packet_out.strip():
        return str(Path(packet_out).expanduser().resolve())
    if upgraded:
        return None
    packet_path = route.get("packet_path")
    return packet_path if isinstance(packet_path, str) and packet_path.strip() else None


def talent_describe_payload(
    ctx: typer.Context,
    request: TalentSource,
    *,
    packet_out: str | None,
    describe: DescribeOptions,
    calls: ProviderCalls,
) -> dict[str, Any]:
    """Route the source to a talent transport packet and attach simc describe-build output for it."""
    resolved = _resolve_talent_transport(ctx, request, calls)
    packet = resolved["talent_transport_packet"]
    packet_path = _described_packet_path(resolved["route"], packet_out=packet_out, upgraded=bool(resolved["upgraded"]))
    describe_result = calls.simc("describe-build", PacketInput(packet, packet_path), describe=describe)
    if (failure := failed_call(describe_result)) is not None:
        _fail_talent_route(
            ctx,
            code=str(failure[0]["code"]),
            message=str(failure[0]["message"]),
            source=request.source,
            route=resolved["route"],
            provider_result=describe_result,
        )
    written_packet_path = _write_transport_packet_or_fail(
        ctx,
        path_value=packet_out,
        packet=packet,
        source=request.source,
        route=resolved["route"],
        provider_result=describe_result,
    )
    return {
        "provider": "warcraft",
        "kind": "talent_describe",
        **resolved,
        "written_packet_path": written_packet_path,
        "describe_result": describe_result,
    }

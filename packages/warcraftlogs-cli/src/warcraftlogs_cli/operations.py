"""Output-free report operations used by the CLI and provider compositions.

Callers supply a client so endpoint selection, refresh policy and lifetime stay explicit.
"""

from __future__ import annotations

import re
from contextlib import suppress
from dataclasses import asdict, replace
from typing import Any

from warcraft_core.envelope import Envelope, success_envelope
from warcraft_core.identity import validate_talent_transport_packet
from warcraft_core.provider import ProviderError

from warcraftlogs_cli.client import (
    GRAPHQL_WARNINGS_KEY,
    ReportFilterOptions,
    ReportPlayerDetailsOptions,
    WarcraftLogsClient,
    WarcraftLogsClientError,
)
from warcraftlogs_cli.player_payloads import (
    _normalized_talent_tree_rows,
    _player_detail_actor,
    _player_talent_transport_packet,
    _report_player_details_payload,
)
from warcraftlogs_cli.report_payloads import fight_payload, report_brief_payload
from warcraftlogs_cli.sampling_utils import dict_at, list_at
from warcraftlogs_cli.scope_payloads import _emitted_finished_report_ttl, _emitted_report_ttl, _encounter_summary_payload
from warcraftlogs_cli.services import ReportReference, _parse_report_reference


def _success(client: WarcraftLogsClient, **payload: Any) -> Envelope:
    """Keep partial upstream errors visible when a composition calls a service directly."""
    result = success_envelope(**payload)
    warnings = list(getattr(client, "graphql_warnings", []) or [])
    if warnings:
        result["provenance"]["graphql_warnings"] = warnings
    return result


def report_reference(client: WarcraftLogsClient, reference: str, *, fight_id: int | None = None) -> ReportReference:
    try:
        ref = _parse_report_reference(reference, explicit_fight_id=fight_id)
    except ValueError as exc:
        raise ProviderError("invalid_query", str(exc)) from exc
    if ref.site is not None and ref.site.key != client.site.key:
        raise ProviderError("invalid_query", f"Report site {ref.site.key!r} differs from selected site {client.site.key!r}.")
    return ref


def _validate_slice(options: ReportFilterOptions | ReportPlayerDetailsOptions) -> None:
    if not options.fight_ids and (options.start_time is None or options.end_time is None):
        raise ProviderError("missing_scope", "Provide fight IDs or both start and end times.", exit_code=2)
    if options.start_time is not None and options.end_time is not None and options.start_time > options.end_time:
        raise ProviderError("invalid_query", "Start time must not be after end time.")


def _validate_selected_start(selected: list[dict[str, Any]], start: float | None) -> None:
    ends = [float(row["endTime"]) for row in selected if isinstance(row.get("endTime"), (int, float))]
    end = max(ends, default=None)
    if start is not None and end is not None and start > end:
        raise ProviderError("invalid_query", f"--start-time {start:.0f} is after the selected fights end at {end:.0f}.")


def selected_fights(
    client: WarcraftLogsClient, *, code: str, options: ReportFilterOptions | ReportPlayerDetailsOptions, allow_unlisted: bool
) -> list[dict[str, Any]]:
    """Validate explicit scope without treating nonexistent fights as an empty successful slice."""
    _validate_slice(options)
    if not options.fight_ids and options.encounter_id is None and options.difficulty is None:
        return []
    report = client.report_fights(code=code, allow_unlisted=allow_unlisted, difficulty=options.difficulty)
    fights = [
        row
        for row in list_at(report, "fights")
        if isinstance(row, dict) and (options.encounter_id is None or row.get("encounterID") == options.encounter_id)
    ]
    missing = [fight_id for fight_id in options.fight_ids or [] if fight_id not in {row.get("id") for row in fights}]
    if missing or not fights:
        raise ProviderError(
            "not_found", f"Warcraft Logs report {code} has no fight matching {asdict(options)!r}.", details={"missing_fight_ids": missing}
        )
    selected = [row for row in fights if not options.fight_ids or row.get("id") in options.fight_ids]
    _validate_selected_start(selected, options.start_time)
    return selected


def report_fights(client: WarcraftLogsClient, *, reference: str, difficulty: int | None = None, allow_unlisted: bool = False) -> Envelope:
    ref = report_reference(client, reference)
    payload = client.report_fights(code=ref.code, difficulty=difficulty, allow_unlisted=allow_unlisted)
    fights = [fight_payload(row) for row in list_at(payload, "fights") if isinstance(row, dict)]
    return _success(
        client,
        provider="warcraftlogs",
        command="report-fights",
        kind="report_fights",
        data={"report": report_brief_payload(payload), "difficulty": difficulty, "count": len(fights), "fights": fights},
    )


def report_player_details(
    client: WarcraftLogsClient, *, reference: str, options: ReportPlayerDetailsOptions, allow_unlisted: bool = False
) -> Envelope:
    ref = report_reference(client, reference)
    selected_fights(client, code=ref.code, options=options, allow_unlisted=allow_unlisted)
    payload = client.report_player_details(code=ref.code, options=options, allow_unlisted=allow_unlisted)
    details = _report_player_details_payload(
        payload, report_code=ref.code, fight_id=options.fight_ids[0] if options.fight_ids and len(options.fight_ids) == 1 else None
    )
    if not details["player_details"]["counts"]["total"]:
        raise ProviderError(
            "not_found", f"Warcraft Logs report {ref.code} has no fight matching {asdict(options)!r}, so the roster is empty."
        )
    return _success(
        client, provider="warcraftlogs", command="report-player-details", kind="report_player_details", query=asdict(options), data=details
    )


def report_events(client: WarcraftLogsClient, *, reference: str, options: ReportFilterOptions, allow_unlisted: bool = False) -> Envelope:
    ref = report_reference(client, reference)
    fights = selected_fights(client, code=ref.code, options=options, allow_unlisted=allow_unlisted)
    ends = [float(row["endTime"]) for row in fights if isinstance(row.get("endTime"), (int, float))]
    fights_end = max(ends, default=None)
    if options.start_time is not None and options.end_time is None and fights_end is not None:
        options = replace(options, end_time=fights_end)
    payload = client.report_events(code=ref.code, options=options, allow_unlisted=allow_unlisted)
    events = dict_at(payload, "events")
    data: dict[str, Any] = {
        "report": report_brief_payload(payload),
        "events": events.get("data"),
        "next_page_timestamp": events.get("nextPageTimestamp"),
    }
    if data["events"] is None and options.data_type is None:
        data["notes"] = [
            "events.data is null. Warcraft Logs requires --data-type (e.g. casts, damage-done, healing) for non-null event slices."
        ]
    next_page = data["next_page_timestamp"]
    if next_page is not None:
        end = options.end_time if options.end_time is not None else fights_end
        flag = f" --end-time {end:.0f}" if end is not None else ""
        data["notes"] = [
            f"This is one page: events stop at timestamp {next_page:.0f}, before the end of the slice. Fetch the next "
            f"page with the same filters plus --start-time {next_page:.0f}{flag} before counting events over the whole slice."
        ]
    return _success(client, provider="warcraftlogs", command="report-events", kind="report_events", query=asdict(options), data=data)


def report_player_talents(
    client: WarcraftLogsClient, *, reference: str, actor_id: int, fight_id: int | None = None, allow_unlisted: bool = False
) -> Envelope:
    ref = report_reference(client, reference, fight_id=fight_id)
    report = client.report(code=ref.code, allow_unlisted=allow_unlisted)
    fights_report = client.report_fights(code=ref.code, difficulty=None, allow_unlisted=allow_unlisted)
    fights = [row for row in list_at(fights_report, "fights") if isinstance(row, dict)]
    if ref.fight_id is None and len(fights) != 1:
        raise ProviderError("missing_scope", "Select one fight for the talent transport packet.", exit_code=2)
    fight = next((row for row in fights if ref.fight_id is None or row.get("id") == ref.fight_id), None)
    if fight is None:
        raise ProviderError("not_found", "Selected report fight was not found.")
    encounter = None
    if fight.get("encounterID"):
        with suppress(WarcraftLogsClientError):
            encounter = client.encounter(encounter_id=fight["encounterID"])
    selected_id = int(fight["id"])
    options = ReportPlayerDetailsOptions(
        fight_ids=[selected_id],
        include_combatant_info=True,
        encounter_id=fight.get("encounterID") or None,
        kill_type=("Kills" if fight.get("kill") else "Wipes") if fight.get("encounterID") else None,
    )
    payload = client.report_player_details(code=ref.code, allow_unlisted=allow_unlisted, options=options)
    actor = _player_detail_actor(_report_player_details_payload(payload, report_code=ref.code, fight_id=selected_id), actor_id)
    if actor is None:
        raise ProviderError("not_found", f"Actor ID {actor_id} was not present in the selected fight.")
    rows, invalid = _normalized_talent_tree_rows(actor)
    if not rows:
        raise ProviderError("missing_talent_tree", f"Actor ID {actor_id} did not include combatant_info.talentTree in the selected fight.")
    if invalid:
        raise ProviderError(
            "missing_talent_tree", f"Actor ID {actor_id} included incomplete combatant_info.talentTree rows in the selected fight."
        )
    packet = _player_talent_transport_packet(actor, report_code=ref.code, fight_id=selected_id, actor_id=actor_id, raw_rows=rows)
    try:
        packet = validate_talent_transport_packet(packet)
    except ValueError as exc:
        raise ProviderError("invalid_transport_packet", str(exc)) from exc
    return _success(
        client,
        provider="warcraftlogs",
        command="report-player-talents",
        kind="report_player_talents",
        data={
            **_encounter_summary_payload(
                ref=ref,
                report=report,
                fight=fight,
                encounter=encounter,
                finished_report_ttl=_emitted_finished_report_ttl(client),
                report_ttl=_emitted_report_ttl(client),
            ),
            "player": actor,
            "talent_transport_packet": packet,
            "written_packet_path": None,
        },
    )


def raw_graphql(
    client: WarcraftLogsClient,
    *,
    query: str,
    variables: dict[str, Any] | None = None,
    report_code: str | None = None,
    allow_unlisted: bool = False,
    endpoint: str = "client",
    operation_name: str | None = None,
    cache_ttl_seconds: int = 0,
) -> Envelope:
    variables = dict(variables or {})
    # Inject helpers only into declared variables: literal queries remain untouched.
    declared = set(re.findall(r"\$([A-Za-z_][A-Za-z0-9_]*)\s*:", query))
    if report_code is not None and "code" in declared and "code" not in variables:
        variables["code"] = report_reference(client, report_code).code
    if allow_unlisted and "allowUnlisted" in declared and "allowUnlisted" not in variables:
        variables["allowUnlisted"] = True
    payload, effective = client.raw_graphql(
        query=query, variables=variables, operation_name=operation_name, endpoint=endpoint, cache_ttl_seconds=cache_ttl_seconds
    )
    data = dict(payload or {})
    warnings = data.pop(GRAPHQL_WARNINGS_KEY, None)
    return _success(
        client,
        provider="warcraftlogs",
        command="graphql",
        kind="graphql",
        data=data,
        query={
            "operation_name": operation_name,
            "variables": variables,
            "endpoint": effective,
            "requested_endpoint": endpoint,
            "cache_ttl_seconds": cache_ttl_seconds,
        },
        provenance={"graphql_warnings": warnings} if warnings else {},
    )

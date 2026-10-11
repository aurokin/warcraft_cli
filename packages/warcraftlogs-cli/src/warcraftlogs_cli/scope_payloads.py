"""Report scope and freshness payloads, independent of command output."""

from __future__ import annotations

from typing import Any

from warcraft_core.identity import encounter_identity_payload

from warcraftlogs_cli.client import WarcraftLogsClient
from warcraftlogs_cli.report_payloads import fight_payload as _fight_payload
from warcraftlogs_cli.report_payloads import report_payload as _report_payload
from warcraftlogs_cli.sampling_utils import dict_at
from warcraftlogs_cli.sampling_utils import report_cache_provenance as _report_cache_provenance
from warcraftlogs_cli.sampling_utils import report_is_finished as _report_is_finished
from warcraftlogs_cli.services import ReportReference, _report_reference_payload


def _encounter_payload(encounter: dict[str, Any]) -> dict[str, Any]:
    zone = dict_at(encounter, "zone")
    expansion = dict_at(zone, "expansion")
    return {
        "id": encounter.get("id"),
        "name": encounter.get("name"),
        "journal_id": encounter.get("journalID"),
        "zone": {
            "id": zone.get("id"),
            "name": zone.get("name"),
            "expansion": {"id": expansion.get("id"), "name": expansion.get("name")} if expansion else None,
        }
        if zone
        else None,
    }


def _fight_encounter_id(fight: dict[str, Any]) -> int | None:
    """The fight's boss encounter ID; ``None`` for a trash fight, which Warcraft Logs reports as 0."""
    encounter_id = fight.get("encounterID")
    return encounter_id if isinstance(encounter_id, int) and encounter_id > 0 else None


def _kill_type_for_fight(fight: dict[str, Any]) -> str | None:
    """Kills or Wipes for a boss fight. A trash fight is neither, so its slice carries no kill filter."""
    if _fight_encounter_id(fight) is None:
        return None
    return "Kills" if fight.get("kill") else "Wipes"


def _emitted_finished_report_ttl(client: WarcraftLogsClient) -> int | None:
    """Finished-report TTL as it should appear in emitted provenance/freshness.

    Returns ``None`` when caching is disabled (``WARCRAFTLOGS_CACHE_BACKEND=none|off|disabled``),
    so the trust metadata never claims a TTL for data that is never stored.
    """
    return client._finished_report_ttl if client._cache_store is not None else None


def _emitted_report_ttl(client: WarcraftLogsClient) -> int | None:
    """Short report TTL as it should appear in emitted provenance; ``None`` when caching is off."""
    return client._report_ttl if client._cache_store is not None else None


def _encounter_summary_payload(
    *,
    ref: ReportReference,
    report: dict[str, Any],
    fight: dict[str, Any],
    encounter: dict[str, Any] | None,
    finished_report_ttl: int | None = 86400,
    report_ttl: int | None = 60,
) -> dict[str, Any]:
    encounter_payload = None
    encounter_identity = encounter_identity_payload(
        encounter_id=_fight_encounter_id(fight),
        name=fight.get("name") if isinstance(fight.get("name"), str) else None,
        provider="warcraftlogs",
        source="report_encounter",
        notes=["canonical only within explicit encounter metadata returned by Warcraft Logs"],
    )
    if isinstance(encounter, dict):
        zone = dict_at(encounter, "zone")
        encounter_identity = encounter_identity_payload(
            encounter_id=encounter.get("id") if isinstance(encounter.get("id"), int) else None,
            journal_id=encounter.get("journalID") if isinstance(encounter.get("journalID"), int) else None,
            name=encounter.get("name") if isinstance(encounter.get("name"), str) else None,
            zone_id=zone.get("id") if isinstance(zone.get("id"), int) else None,
            provider="warcraftlogs",
            source="report_encounter",
        )
        encounter_payload = _encounter_payload(encounter)
    return {
        "reference": _report_reference_payload(ref),
        "report": _report_payload(report),
        "fight": _fight_payload(fight),
        "encounter": encounter_payload,
        "encounter_identity": encounter_identity,
        "stability": {
            "report_finished": _report_is_finished(report),
            "cache_policy": "ttl_staleness_budget",
            "immutable": False,
            "live": not _report_is_finished(report),
        },
        # cache_provenance describes the *report's* finish state, resolved from the report
        # metadata lookup (client.report(); REPORT_QUERY selects report-level endTime). It is a
        # property of the log, not a per-namespace cache-entry audit: an encounter command may
        # also return data from other namespaces (report_fights/report_player_details/...), and
        # during the bounded live->finished window (<= the live TTL) those entries can briefly
        # disagree with the report's resolved finish state. See docs/warcraftlogs/CACHING.md.
        "cache_provenance": _report_cache_provenance(
            report,
            finished_ttl=finished_report_ttl,
            live_ttl=report_ttl,
            source="report_detail",
        ),
    }

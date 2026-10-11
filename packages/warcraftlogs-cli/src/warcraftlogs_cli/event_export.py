"""Bounded, replayable event evidence for one explicitly scoped report slice."""

from __future__ import annotations

import json
import math
import os
import tempfile
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from warcraft_core.provider import ProviderError

from warcraftlogs_cli.client import GRAPHQL_WARNINGS_KEY, ReportFilterOptions, WarcraftLogsClient
from warcraftlogs_cli.operations import report_reference, selected_fights
from warcraftlogs_cli.report_payloads import report_brief_payload
from warcraftlogs_cli.sampling_utils import dict_at


def _validate_cursor(paginator: dict[str, Any], *, start_time: float | None) -> float | None:
    if "nextPageTimestamp" not in paginator:
        raise ProviderError("invalid_response", "Event pagination omitted nextPageTimestamp; exhaustion is unknown.")
    cursor = paginator["nextPageTimestamp"]
    if cursor is not None and (
        isinstance(cursor, bool)
        or not isinstance(cursor, (int, float))
        or not math.isfinite(cursor)
        or (start_time is not None and cursor <= start_time)
    ):
        raise ProviderError("invalid_response", "Event pagination did not advance; refusing an incomplete loop.")
    return cursor


@dataclass
class _ExportState:
    events: list[Any] = field(default_factory=list)
    revisions: list[Any] = field(default_factory=list)
    warnings: list[Any] = field(default_factory=list)
    report: dict[str, Any] = field(default_factory=dict)
    pages: int = 0
    next_time: float | None = None
    reason: str | None = "max_pages"
    continuation: dict[str, Any] | None = None

    def accept(self, options: ReportFilterOptions, remaining: int) -> bool:
        revision = self.report.get("revision")
        if revision not in self.revisions:
            self.revisions.append(revision)
        if len(self.revisions) > 1:
            self.reason = "revision_changed"
            self.continuation = {"start_time": options.start_time, "skip_events": 0, "requires_restart": True}
            return True
        self.warnings.extend(self.report.get(GRAPHQL_WARNINGS_KEY) or [])
        paginator = dict_at(self.report, "events")
        rows = paginator.get("data")
        if not isinstance(rows, list):
            self.reason = "missing_event_data"
            self.continuation = {"start_time": options.start_time, "skip_events": 0}
            return True
        # Even a page stopped by local bounds must carry valid upstream pagination metadata.
        self.next_time = _validate_cursor(paginator, start_time=options.start_time)
        self.events.extend(rows[:remaining])
        if len(rows) > remaining:
            self.reason = "max_events"
            self.continuation = {"start_time": options.start_time, "skip_events": remaining}
            return True
        return self.advance()

    def advance(self) -> bool:
        if self.next_time is None:
            self.reason = "partial_graphql_errors" if self.warnings else None
            self.continuation = None
            return True
        self.continuation = {"start_time": self.next_time, "skip_events": 0}
        return False


def collect_events(
    client: WarcraftLogsClient,
    *,
    reference: str,
    options: ReportFilterOptions,
    allow_unlisted: bool = False,
    max_pages: int = 20,
    max_events: int = 100000,
) -> dict[str, Any]:
    """Follow provider continuation timestamps without claiming partial evidence is complete.

    Request limits bound GraphQL calls, not provider API points. No point-cost estimate is invented.
    A shortened upstream page exposes its original start and a skip count for lossless replay.
    """
    if max_pages < 1 or max_events < 1:
        raise ProviderError("invalid_query", "Page and event bounds must be positive.")
    if options.data_type is None:
        raise ProviderError("missing_scope", "Complete event export requires --data-type.", exit_code=2)
    if options.fight_ids and len(options.fight_ids) != 1:
        raise ProviderError("missing_scope", "Complete event export selects exactly one fight or one explicit window.", exit_code=2)
    ref = report_reference(client, reference)
    fights = selected_fights(client, code=ref.code, options=options, allow_unlisted=allow_unlisted)
    end = options.end_time
    if end is None and fights:
        ends = [float(row["endTime"]) for row in fights if isinstance(row.get("endTime"), (int, float))]
        end = max(ends) if ends else None
    scope = replace(options, end_time=end)
    page_options = scope
    state = _ExportState()
    for _ in range(max_pages):
        remaining = max_events - len(state.events)
        page_options = replace(page_options, limit=min(options.limit or 10000, remaining))
        state.report = client.report_events(code=ref.code, options=page_options, allow_unlisted=allow_unlisted)
        for warning in getattr(client, "graphql_warnings", []):
            if warning not in state.warnings:
                state.warnings.append(warning)
        state.pages += 1
        if state.accept(page_options, remaining):
            break
        page_options = replace(page_options, start_time=state.next_time)
        if len(state.events) >= max_events:
            state.reason = "max_events"
            break
    return {
        "report": report_brief_payload(state.report),
        "query": asdict(scope),
        "events": state.events,
        "next_page_timestamp": state.next_time,
        "export": {
            "schema_version": "1",
            "complete": state.reason is None,
            "completeness_basis": "provider pagination exhausted; not a transactional or revision-validated snapshot",
            "truncated": state.reason is not None,
            "stop_reason": state.reason,
            "continuation": state.continuation,
            "pages": state.pages,
            "event_count": len(state.events),
            "bounds": {"max_pages": max_pages, "max_events": max_events, "graphql_request_bound": max_pages + 1, "api_point_budget": None},
            "scope": {"site": client.site.key, "report_code": ref.code, "allow_unlisted": allow_unlisted, "filters": asdict(scope)},
            "freshness": {
                "collected_at": datetime.now(UTC).isoformat(),
                "report_revisions": state.revisions,
                "revision_consistent": len(state.revisions) == 1 and state.revisions[0] is not None,
                "transport": client.transport_counts,
                "note": "Collection time is not upstream fetch time; cached pages may be older. Use --refresh to re-fetch.",
                "revision_warning": (
                    "At least one page has no report revision; pagination completeness does not establish a consistent report snapshot. "
                    "Use --refresh to replace older cached pages."
                    if None in state.revisions
                    else None
                ),
            },
            "citations": [{"url": f"{client.site.root_url}/reports/{ref.code}", "provider": "warcraftlogs"}],
            "graphql_warnings": state.warnings,
        },
    }


def write_event_artifact(path: str, payload: dict[str, Any], *, format: str = "json") -> str:
    """Create a new evidence file; refuse to overwrite an existing investigation artifact."""
    if format not in {"json", "jsonl"}:
        raise ProviderError("invalid_query", "Artifact format must be json or jsonl.")
    output = Path(path).expanduser().resolve()
    temporary: str | None = None
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=output.parent, delete=False) as handle:
            temporary = handle.name
            if format == "json":
                json.dump(payload, handle, indent=2)
                handle.write("\n")
            else:
                handle.write(json.dumps({"artifact": {key: value for key, value in payload.items() if key != "events"}}) + "\n")
                for event in payload["events"]:
                    handle.write(json.dumps({"event": event}) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        # Publish only the finished artifact; exclusive linking never replaces an existing capture.
        os.link(temporary, output)
    except OSError as exc:
        raise ProviderError("artifact_write_failed", str(exc)) from exc
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)
    return str(output)

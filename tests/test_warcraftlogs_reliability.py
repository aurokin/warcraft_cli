"""Synthetic provider pages prove auth selection and bounded evidence completeness."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner
from warcraft_core.provider import ProviderError
from warcraftlogs_cli.client import GRAPHQL_WARNINGS_KEY, RETAIL_PROFILE, ReportFilterOptions, WarcraftLogsClient, WarcraftLogsClientError
from warcraftlogs_cli.event_export import collect_events, write_event_artifact
from warcraftlogs_cli.main import app


class EventClient:
    site = RETAIL_PROFILE

    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self.pages = iter(pages)
        self.options: list[ReportFilterOptions] = []

    @property
    def transport_counts(self) -> dict[str, int]:
        return {"cache_hit_count": 0, "upstream_request_count": len(self.options) + 1}

    def close(self) -> None:
        pass

    def report_fights(self, **kwargs: Any) -> dict[str, Any]:
        return {"fights": [{"id": 1, "startTime": 0, "endTime": 1000}]}

    def report_events(self, *, options: ReportFilterOptions, **kwargs: Any) -> dict[str, Any]:
        self.options.append(options)
        return next(self.pages)


def page(events: list[Any] | None, next_time: float | None, *, revision: int = 1) -> dict[str, Any]:
    return {"code": "abcd1234", "revision": revision, "events": {"data": events, "nextPageTimestamp": next_time}}


def export(client: EventClient, **kwargs: Any) -> dict[str, Any]:
    return collect_events(client, reference="abcd1234", options=ReportFilterOptions(fight_ids=[1], data_type="Casts"), **kwargs)


def test_event_export_follows_exact_continuation_and_preserves_raw_rows() -> None:
    rows = [{"timestamp": 20, "unknown_provider_field": {"x": True}}, {"timestamp": 500}]
    client = EventClient([page(rows[:1], 500), page(rows[1:], None)])
    payload = export(client)
    assert payload["events"] == rows
    assert payload["export"]["complete"] is True
    assert payload["export"]["continuation"] is None
    assert payload["export"]["freshness"]["report_revisions"] == [1]
    assert client.options[1].start_time == 500
    assert all(options.end_time == 1000 for options in client.options)


@pytest.mark.parametrize(("bounds", "reason"), [({"max_pages": 1}, "max_pages"), ({"max_events": 1}, "max_events")])
def test_event_export_exposes_bounds_and_continuation(bounds: dict[str, int], reason: str) -> None:
    client = EventClient([page([{"timestamp": 20}], 500)])
    payload = export(client, **bounds)
    assert payload["export"]["complete"] is False
    assert payload["export"]["stop_reason"] == reason
    assert payload["export"]["continuation"] == {"start_time": 500, "skip_events": 0}
    assert len(client.options) == 1


def test_event_export_does_not_merge_changed_report_revisions() -> None:
    payload = export(EventClient([page([{"id": 1}], 500), page([{"id": 2}], None, revision=2)]))
    assert payload["events"] == [{"id": 1}]
    assert payload["export"]["stop_reason"] == "revision_changed"
    assert payload["export"]["continuation"]["requires_restart"] is True


@pytest.mark.parametrize("next_time", [0, "bad", True, float("nan"), float("inf")])
def test_event_export_rejects_nonadvancing_pagination(next_time: Any) -> None:
    client = EventClient([page([], next_time)])
    with pytest.raises(ProviderError, match="did not advance"):
        collect_events(client, reference="abcd1234", options=ReportFilterOptions(fight_ids=[1], data_type="Casts", start_time=0))


def test_event_export_never_calls_null_event_data_complete() -> None:
    payload = export(EventClient([page(None, None)]))
    assert payload["export"]["stop_reason"] == "missing_event_data"
    assert payload["export"]["complete"] is False


def test_event_export_never_calls_partial_graphql_data_complete() -> None:
    response = {**page([], None), GRAPHQL_WARNINGS_KEY: [{"message": "field failed"}]}
    payload = export(EventClient([response]))
    assert payload["export"]["stop_reason"] == "partial_graphql_errors"


def test_event_export_retains_exact_resume_offset_if_provider_ignores_limit() -> None:
    payload = export(EventClient([page([{"id": 1}, {"id": 2}], 500)]), max_events=1)
    assert payload["events"] == [{"id": 1}]
    assert payload["export"]["continuation"] == {"start_time": None, "skip_events": 1}


@pytest.mark.parametrize("format", ["json", "jsonl"])
def test_event_artifact_contains_scope_provenance_and_raw_events(tmp_path, format: str) -> None:
    payload = export(EventClient([page([{"id": 1}], None)]))
    path = tmp_path / f"evidence.{format}"
    write_event_artifact(str(path), payload, format=format)
    if format == "json":
        artifact = json.loads(path.read_text())
        assert artifact == payload
    else:
        header, record = [json.loads(line) for line in path.read_text().splitlines()]
        assert header["artifact"]["export"] == payload["export"]
        assert record == {"event": {"id": 1}}
    with pytest.raises(ProviderError, match="File exists"):
        write_event_artifact(str(path), payload, format=format)


@pytest.mark.parametrize(
    "options",
    [
        ReportFilterOptions(fight_ids=[1], data_type=None),
        ReportFilterOptions(fight_ids=[1, 2], data_type="Casts"),
        ReportFilterOptions(data_type="Casts"),
    ],
)
def test_event_export_requires_explicit_unambiguous_scope(options: ReportFilterOptions) -> None:
    with pytest.raises(ProviderError) as exc:
        collect_events(EventClient([]), reference="abcd1234", options=options)
    assert exc.value.code == "missing_scope"


def test_public_endpoint_does_not_consume_saved_revoked_user_token(monkeypatch) -> None:
    client = WarcraftLogsClient(endpoint="client")
    monkeypatch.setattr(client, "_has_user_token", lambda: True)
    monkeypatch.setattr(client, "_graphql_user", lambda **kwargs: pytest.fail("public read selected user token"))
    monkeypatch.setattr(client, "_token", lambda: "synthetic-client-token")
    monkeypatch.setattr(client, "_post_graphql", lambda *args, **kwargs: httpx.Response(200, json={"data": {"worldData": {}}}))
    assert client._graphql(
        operation_name="Q", query="query Q { worldData { x } }", variables={}, namespace="test", ttl_seconds=60, use_cache=False
    ) == {"worldData": {}}


@pytest.mark.parametrize("endpoint", ["user", "auto"])
def test_requested_user_endpoint_never_silently_falls_back(monkeypatch, endpoint: str) -> None:
    client = WarcraftLogsClient(endpoint=endpoint)
    monkeypatch.setattr(client, "_has_user_token", lambda: True)
    monkeypatch.setattr(client, "_token", lambda: pytest.fail("user request fell back to client"))

    def rejected(**kwargs: Any) -> Any:
        raise WarcraftLogsClientError("auth_failed", "Synthetic saved user token rejected")

    monkeypatch.setattr(client, "_graphql_user", rejected)
    with pytest.raises(WarcraftLogsClientError, match="rejected"):
        client._graphql(operation_name="Q", query="query Q { x }", variables={}, namespace="test", ttl_seconds=60)


def test_refresh_bypasses_only_reads_and_replaces_queried_entry() -> None:
    client = WarcraftLogsClient(refresh=True)

    class Cache:
        data = {"queried": {"old": True}, "unrelated": {"keep": True}}

        def get(self, key: str) -> Any:
            pytest.fail("refresh consulted old cache")

        def set(self, key: str, value: Any, **kwargs: Any) -> None:
            self.data[key] = value

    cache = Cache()
    client._cache_store = cache
    assert client._read_cache("queried") is None
    client._write_cache("queried", {"new": True}, ttl_seconds=60)
    assert cache.data == {"queried": {"new": True}, "unrelated": {"keep": True}}


def test_report_events_cli_writes_bounded_artifact(monkeypatch, tmp_path) -> None:
    client = EventClient([page([{"timestamp": 20}], None)])
    monkeypatch.setattr("warcraftlogs_cli.main._client", lambda ctx: client)
    path = tmp_path / "evidence.jsonl"
    result = CliRunner().invoke(
        app,
        [
            "report-events",
            "abcd1234",
            "--fight-id",
            "1",
            "--data-type",
            "casts",
            "--all-pages",
            "--out",
            str(path),
            "--artifact-format",
            "jsonl",
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["data"]["export"]["complete"] is True
    assert path.exists()


@pytest.mark.parametrize(("endpoint", "expected", "status"), [("client", "client", "ready"), ("user", "user", "degraded"),
                                                               ("auto", "user", "degraded")])
def test_doctor_readiness_tracks_selected_endpoint(monkeypatch, endpoint: str, expected: str, status: str) -> None:
    from warcraftlogs_cli.services import doctor_payload
    monkeypatch.setattr("warcraftlogs_cli.services.provider_auth_status",
                        lambda provider: {"auth_mode": "pkce", "has_access_token": True, "expired": False})
    monkeypatch.setattr("warcraftlogs_cli.services._runtime_access_payload", lambda site: {"ready": True})
    monkeypatch.setattr("warcraftlogs_cli.services._public_api_access_payload", lambda **kwargs: {"ready": True})
    monkeypatch.setattr("warcraftlogs_cli.services._user_api_access_payload",
                        lambda *args, **kwargs: {"ready": False, "reason": "auth_failed"})
    payload = doctor_payload(live=True, site=RETAIL_PROFILE, endpoint=endpoint)
    assert payload["status"] == status
    assert payload["auth"]["command_endpoint"] == expected
    assert payload["capabilities"]["report"] == ("ready" if expected == "client" else "auth_failed")
    # rate-limit always deliberately uses client credentials.
    assert payload["capabilities"]["rate_limit"] == "ready"

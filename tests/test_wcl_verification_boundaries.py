"""Synthetic boundary checks for conservative pull matching and realm fallback."""

from typing import Any

import pytest
from warcraftlogs_cli.boss_kills import deduplicate_pulls
from warcraftlogs_cli.client import WarcraftLogsClient, WarcraftLogsClientError


def _candidate(code: str, start: int, end: int) -> tuple[dict[str, Any], dict[str, Any]]:
    return (
        {"code": code, "startTime": 100_000, "guild": {"id": 1}},
        {"id": 1, "encounterID": 123, "difficulty": 5, "size": 20, "startTime": start, "endTime": end},
    )


@pytest.mark.parametrize(("start_drift", "end_drift", "expected_count"), [(5_000, 5_000, 1), (5_001, 5_000, 2), (5_000, 5_001, 2)])
def test_guild_pull_matching_requires_both_window_bounds_within_five_seconds(
    start_drift: int, end_drift: int, expected_count: int,
) -> None:
    pulls = deduplicate_pulls([_candidate("first", 0, 60_000), _candidate("second", start_drift, 60_000 + end_drift)])
    assert len(pulls) == expected_count
    if expected_count == 1:
        assert pulls[0].duplicates == [{"report_code": "second", "fight_id": 1}]


@pytest.mark.parametrize(("start_drift", "end_drift", "expected_count"), [(30_000, 30_000, 1), (30_001, 30_000, 2), (30_000, 30_001, 2)])
def test_identical_rosters_do_not_override_the_thirty_second_window_limit(
    start_drift: int, end_drift: int, expected_count: int,
) -> None:
    pulls = deduplicate_pulls(
        [_candidate("first", 0, 60_000), _candidate("second", start_drift, 60_000 + end_drift)],
        roster=lambda report, fight: frozenset({"A-Realm", "B-Realm"}),
    )
    assert len(pulls) == expected_count


@pytest.mark.parametrize("field", ["report_start", "fight_start", "fight_end"])
@pytest.mark.parametrize("value", [None, "not-a-time", float("nan"), float("inf")])
def test_an_unusable_window_never_collapses_two_uploads(field: str, value: object) -> None:
    candidates = [_candidate("first", 0, 60_000), _candidate("second", 0, 60_000)]
    for report, fight in candidates:
        if field == "report_start":
            report["startTime"] = value
        else:
            fight["startTime" if field == "fight_start" else "endTime"] = value
    pulls = deduplicate_pulls(candidates, roster=lambda report, fight: frozenset({"A-Realm"}))
    assert len(pulls) == 2
    assert all(not pull.duplicates for pull in pulls)


@pytest.mark.parametrize("code", ["auth_failed", "network_error"])
@pytest.mark.parametrize("realm", ["Azjol-Nerub", "Ревущий фьорд"])
@pytest.mark.parametrize("first_spelling_missing", [False, True])
def test_realm_spelling_fallback_preserves_auth_and_network_failures(
    monkeypatch: pytest.MonkeyPatch, code: str, realm: str, first_spelling_missing: bool,
) -> None:
    client = WarcraftLogsClient.__new__(WarcraftLogsClient)
    client._static_ttl = 60
    calls: list[str] = []
    failure = WarcraftLogsClientError(code, "synthetic upstream failure")

    def graphql(*, variables: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        calls.append(variables["slug"])
        if first_spelling_missing and len(calls) == 1:
            raise WarcraftLogsClientError("not_found", "synthetic spelling miss")
        raise failure

    def server_slug(region: str, name: str) -> str:
        pytest.fail("an upstream failure must not be retried as a localized realm miss")

    monkeypatch.setattr(client, "_graphql", graphql)
    monkeypatch.setattr(client, "_server_slug_by_name", server_slug)
    with pytest.raises(WarcraftLogsClientError) as caught:
        client.server(region="eu", slug=realm)
    assert caught.value is failure
    assert len(calls) == (2 if first_spelling_missing else 1)

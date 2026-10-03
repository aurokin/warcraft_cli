from __future__ import annotations

from typing import Any

import pytest
from warcraft_core.discovery import discovery_row, resolve_data, search_data, stub_data

from tests.discovery_contract import resolve_data_violations, search_data_violations


def _row(row_id: int, *, command: str | None = "probe entity item 1") -> dict[str, Any]:
    return discovery_row(
        provider="probe",
        kind="item",
        id=row_id,
        name=f"Item {row_id}",
        url=None,
        score=100 - row_id,
        match_reasons=("exact_name",),
        command=command,
        surface="entity",
        ranking_extra={"upstream_rank": row_id},
        follow_up_extra={"reason": "entity_summary"},
        metadata={"icon": "x"},
    )


def test_discovery_row_puts_the_core_beside_the_provider_keys() -> None:
    assert _row(1) == {
        "provider": "probe",
        "kind": "item",
        "id": 1,
        "name": "Item 1",
        "url": None,
        "ranking": {"score": 99, "match_reasons": ["exact_name"], "upstream_rank": 1},
        "follow_up": {"command": "probe entity item 1", "surface": "entity", "reason": "entity_summary"},
        "metadata": {"icon": "x"},
    }


def test_search_data_counts_the_page_and_reports_the_total() -> None:
    ranked = [_row(1), _row(2), _row(3)]

    data = search_data(search_query="item", ranked=ranked, limit=2, query="Item")

    assert data["query"] == "Item"
    assert data["results"] == ranked[:2]
    assert (data["count"], data["total_matches"], data["truncated"]) == (2, 3, True)
    assert search_data_violations(data, provider="probe") == []


def test_search_data_takes_an_upstream_total_and_never_lets_extras_override_the_core() -> None:
    data = search_data(search_query="item", ranked=[_row(1)], limit=5, total_matches=1934, count=99)

    assert (data["count"], data["total_matches"], data["truncated"]) == (1, 1934, True)
    with pytest.raises(ValueError, match="below"):
        search_data(search_query="item", ranked=[_row(1), _row(2)], limit=5, total_matches=1)


def test_resolve_data_judges_every_ranked_row_and_trims_only_candidates() -> None:
    ranked = [_row(1), _row(2)]

    data = resolve_data(search_query="item", ranked=ranked, limit=1, confidence="high", fallback_search_command="probe search item")

    assert data["resolved"] is True
    assert data["match"] == ranked[0] == data["candidates"][0]
    assert data["next_command"] == "probe entity item 1"
    assert data["fallback_search_command"] is None
    assert (data["count"], data["total_matches"], data["truncated"]) == (1, 2, True)
    assert resolve_data_violations(data, provider="probe") == []


@pytest.mark.parametrize("confidence", ["medium", "low"])
def test_an_unresolved_answer_keeps_the_top_row_as_its_match_without_a_command(confidence: Any) -> None:
    ranked = [_row(1), _row(2)]

    data = resolve_data(search_query="item", ranked=ranked, limit=5, confidence=confidence, fallback_search_command="probe search item")

    assert data["resolved"] is False and data["next_command"] is None
    assert data["match"] == ranked[0]
    assert data["match"]["follow_up"]["command"] == "probe entity item 1"
    assert data["fallback_search_command"] == "probe search item"
    assert resolve_data_violations(data, provider="probe") == []


def test_resolve_data_rejects_answers_that_break_the_invariants() -> None:
    with pytest.raises(ValueError, match="needs a ranked row"):
        resolve_data(search_query="item", ranked=[], limit=5, confidence="medium", fallback_search_command=None)
    with pytest.raises(ValueError, match="follow_up.command"):
        resolve_data(search_query="item", ranked=[_row(1, command=None)], limit=5, confidence="high", fallback_search_command=None)


@pytest.mark.parametrize("surface", ["search", "resolve"])
def test_stub_data_is_an_empty_flagged_answer_with_an_unknown_total(surface: Any) -> None:
    data = stub_data(surface=surface, flag="coming_soon", search_query="probe", message="Not yet.", suggested_command="probe item 1")

    rows = data["results"] if surface == "search" else data["candidates"]
    assert rows == [] and data["count"] == 0
    assert data["total_matches"] is None and data["truncated"] is False
    assert data["coming_soon"] is True and data["suggested_command"] == "probe item 1"
    violations = search_data_violations if surface == "search" else resolve_data_violations
    assert violations(data, provider="probe") == []


def test_the_contract_checkers_catch_a_count_that_is_a_total_and_a_resolved_answer_without_high_confidence() -> None:
    search = {**search_data(search_query="item", ranked=[_row(1), _row(2)], limit=1), "count": 2}
    resolve = {
        **resolve_data(search_query="item", ranked=[_row(1)], limit=5, confidence="medium", fallback_search_command=None),
        "resolved": True,
    }

    assert any("count 2" in problem for problem in search_data_violations(search, provider="probe"))
    assert any("disagree" in problem for problem in resolve_data_violations(resolve, provider="probe"))
    assert any("provider is 'probe'" in problem for problem in search_data_violations(search_data(search_query=None, ranked=[_row(1)], limit=1), provider="other"))

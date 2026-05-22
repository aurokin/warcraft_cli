from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from wowhead_cli.main import app
from wowhead_cli.wowhead_client import WowheadClient

runner = CliRunner()


def test_wowhead_client_get_json_returns_deepcopy_from_session_cache(monkeypatch) -> None:
    def fake_request(self, url: str, *, params=None):  # noqa: ANN001
        response = MagicMock()
        response.json.return_value = {"search": "x", "results": [{"id": 1}]}
        return response

    monkeypatch.setattr(WowheadClient, "_request_with_retries", fake_request)
    client = WowheadClient(cache_enabled=False)
    first = client._get_json("https://example.test/search", cache_namespace="json")
    first["results"][0]["id"] = 99
    second = client._get_json("https://example.test/search", cache_namespace="json")
    client.close()
    assert second["results"][0]["id"] == 1


def test_wowhead_client_dedupes_session_json_requests(monkeypatch) -> None:
    calls: list[str] = []

    def fake_request(self, url: str, *, params=None):  # noqa: ANN001
        calls.append(url)
        response = MagicMock()
        response.json.return_value = {"search": "x", "results": []}
        return response

    monkeypatch.setattr(WowheadClient, "_request_with_retries", fake_request)
    client = WowheadClient(cache_enabled=False)
    client.search_suggestions("thunderfury")
    client.search_suggestions("thunderfury")
    client.close()
    assert len(calls) == 1


def test_wowhead_search_stream_emits_jsonl_header_when_results_empty(monkeypatch) -> None:
    monkeypatch.setattr(
        "wowhead_cli.wowhead_client.WowheadClient.search_suggestions",
        lambda self, query: {"search": query, "results": []},
    )
    monkeypatch.setattr(
        "wowhead_cli.main._normalize_search_results",
        lambda results, *, query, expansion: results,
    )

    result = runner.invoke(app, ["--stream", "search", "thunderfury", "--limit", "10"])
    assert result.exit_code == 0
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(lines) == 1
    header = json.loads(lines[0])
    assert header["stream"] == {"field": "results", "count": 0}
    assert header["results"] == []


def test_wowhead_search_stream_emits_jsonl_header_and_records(monkeypatch) -> None:
    monkeypatch.setattr(
        "wowhead_cli.wowhead_client.WowheadClient.search_suggestions",
        lambda self, query: {
            "search": query,
            "results": [
                {"id": 1, "name": "A"},
                {"id": 2, "name": "B"},
            ],
        },
    )
    monkeypatch.setattr(
        "wowhead_cli.main._normalize_search_results",
        lambda results, *, query, expansion: results,
    )

    result = runner.invoke(app, ["--stream", "search", "thunderfury", "--limit", "10"])
    assert result.exit_code == 0
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(lines) == 3
    header = json.loads(lines[0])
    assert header["stream"] == {"field": "results", "count": 2}
    assert header["results"] == []
    record = json.loads(lines[1])
    assert record["record"]["id"] == 1


def test_wowhead_comments_hydration_uses_concurrency(monkeypatch) -> None:
    call_count = {"n": 0}

    def fake_replies(self, comment_id: int):  # noqa: ANN001
        call_count["n"] += 1
        return [{"id": comment_id * 10}]

    monkeypatch.setattr("wowhead_cli.wowhead_client.WowheadClient.comment_replies", fake_replies)
    monkeypatch.setattr(
        "wowhead_cli.main._fetch_entity_page",
        lambda *args, **kwargs: (
            '<html><script>var lv_comments0 = [{"id":1,"nreplies":2,"replies":[]},{"id":2,"nreplies":2,"replies":[]}];</script></html>',
            {"canonical_url": "https://www.wowhead.com/item=1", "title": "T"},
        ),
    )
    monkeypatch.setattr("wowhead_cli.main._resolve_page_fetch_target", lambda *args, **kwargs: MagicMock(page_entity_type="item", page_entity_id=1))

    result = runner.invoke(
        app,
        [
            "comments",
            "item",
            "1",
            "--limit",
            "2",
            "--hydrate-missing-replies",
            "--max-concurrency",
            "2",
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["counts"]["hydrated_reply_threads"] == 2
    assert call_count["n"] == 2

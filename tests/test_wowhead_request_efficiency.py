from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

from typer.testing import CliRunner
from warcraft_core.envelope import REQUIRED_KEYS
from wowhead_cli import main as main_module
from wowhead_cli.main import app
from wowhead_cli.wowhead_client import WowheadClient

runner = CliRunner()


def test_wowhead_client_get_json_returns_deepcopy_from_session_cache(monkeypatch) -> None:
    def fake_request(self, url: str, *, params=None):
        response = MagicMock()
        response.json.return_value = {"search": "x", "results": [{"id": 1}]}
        return response

    monkeypatch.setattr(WowheadClient, "_request_with_retries", fake_request)
    monkeypatch.setenv("WOWHEAD_CACHE_BACKEND", "none")
    client = WowheadClient()
    first = client._get_json("https://example.test/search", cache_namespace="json")
    first["results"][0]["id"] = 99
    second = client._get_json("https://example.test/search", cache_namespace="json")
    client.close()
    assert second["results"][0]["id"] == 1


def test_wowhead_client_dedupes_session_json_requests(monkeypatch) -> None:
    calls: list[str] = []

    def fake_request(self, url: str, *, params=None):
        calls.append(url)
        response = MagicMock()
        response.json.return_value = {"search": "x", "results": []}
        return response

    monkeypatch.setattr(WowheadClient, "_request_with_retries", fake_request)
    monkeypatch.setenv("WOWHEAD_CACHE_BACKEND", "none")
    client = WowheadClient()
    client.search_suggestions("thunderfury")
    client.search_suggestions("thunderfury")
    client.close()
    assert len(calls) == 1


def test_wowhead_client_cache_keys_carry_the_request_params(monkeypatch, tmp_path) -> None:
    calls: list[str] = []

    def fake_request(self: WowheadClient, url: str, *, params: dict[str, Any]) -> MagicMock:
        calls.append(params["q"])
        response = MagicMock()
        response.json.return_value = {"search": params["q"], "results": []}
        return response

    monkeypatch.setattr(WowheadClient, "_request_with_retries", fake_request)
    monkeypatch.setenv("WOWHEAD_CACHE_BACKEND", "file")
    monkeypatch.setenv("WOWHEAD_CACHE_DIR", str(tmp_path))
    with WowheadClient() as client:
        assert [client.search_suggestions(q)["search"] for q in ("thunderfury", "ashkandi")] == ["thunderfury", "ashkandi"]
    # A second client has no session cache, so these answers come from the file cache.
    with WowheadClient() as client:
        assert [client.search_suggestions(q)["search"] for q in ("ashkandi", "thunderfury")] == ["ashkandi", "thunderfury"]
    assert calls == ["thunderfury", "ashkandi"]


def test_wowhead_entity_response_cache_keeps_the_all_comments_variant_apart(monkeypatch, tmp_path) -> None:
    options = {"requested_type": "item", "requested_id": 19019, "data_env": None, "include_comments": True, "linked_entity_preview_limit": 5}
    monkeypatch.setenv("WOWHEAD_CACHE_BACKEND", "file")
    monkeypatch.setenv("WOWHEAD_CACHE_DIR", str(tmp_path))
    with WowheadClient() as client:
        client.set_cached_entity_response({"comments": "top"}, include_all_comments=False, **options)
        assert client.get_cached_entity_response(include_all_comments=True, **options) is None
        assert client.get_cached_entity_response(include_all_comments=False, **options) == {"comments": "top"}


def test_wowhead_search_stream_emits_jsonl_header_when_results_empty(monkeypatch) -> None:
    monkeypatch.setattr(
        "wowhead_cli.wowhead_client.WowheadClient.search_suggestions",
        lambda self, query: {"search": query, "results": []},
    )
    monkeypatch.setattr(
        "wowhead_cli.provider.normalize_search_results",
        lambda results, *, query, expansion, entity_types=(), rank_bonuses=None, literal=False: (results, 0),
    )

    result = runner.invoke(app, ["--stream", "search", "thunderfury", "--limit", "10"])
    assert result.exit_code == 0
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(lines) == 1
    header = json.loads(lines[0])
    assert header["data"]["stream"] == {"field": "results", "count": 0}
    assert header["data"]["results"] == []


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
        "wowhead_cli.provider.normalize_search_results",
        lambda results, *, query, expansion, entity_types=(), rank_bonuses=None, literal=False: (results, 0),
    )

    result = runner.invoke(app, ["--stream", "search", "thunderfury", "--limit", "10"])
    assert result.exit_code == 0
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(lines) == 3
    header = json.loads(lines[0])
    assert set(header) == REQUIRED_KEYS
    assert header["data"]["stream"] == {"field": "results", "count": 2}
    assert header["data"]["results"] == []
    record = json.loads(lines[1])
    assert record["record"]["id"] == 1


def test_wowhead_stream_header_carries_provenance_cache(monkeypatch, tmp_path) -> None:
    # --stream prints its header itself instead of through warcraft_core.cli.emit, so it stamps the block itself.
    def fake_request(self: WowheadClient, url: str, *, params: dict[str, Any]) -> MagicMock:
        response = MagicMock()
        response.json.return_value = {"search": params["q"], "results": [{"id": 1, "name": "A"}]}
        return response

    monkeypatch.setattr(WowheadClient, "_request_with_retries", fake_request)
    monkeypatch.setattr(
        "wowhead_cli.provider.normalize_search_results",
        lambda results, *, query, expansion, entity_types=(), rank_bonuses=None, literal=False: (results, 0),
    )
    monkeypatch.setenv("WOWHEAD_CACHE_BACKEND", "file")
    monkeypatch.setenv("WOWHEAD_CACHE_DIR", str(tmp_path))

    result = runner.invoke(app, ["--stream", "search", "thunderfury"])
    assert result.exit_code == 0
    header = json.loads(result.stdout.splitlines()[0])
    assert header["data"]["stream"] == {"field": "results", "count": 1}
    cache = header["provenance"]["cache"]
    assert (cache["backend"], cache["lookups"], cache["hits"]) == ("file", 1, 0)


def test_wowhead_stream_refuses_a_malformed_envelope(monkeypatch) -> None:
    """--stream writes JSONL itself, so it must apply the envelope check warcraft_core.cli.emit applies."""
    monkeypatch.setattr(
        "wowhead_cli.wowhead_client.WowheadClient.search_suggestions",
        lambda self, query: {"search": query, "results": []},
    )
    wrap = main_module._with_envelope_keys
    monkeypatch.setattr(main_module, "_with_envelope_keys", lambda ctx, payload: {**wrap(ctx, payload), "results": []})

    result = runner.invoke(app, ["--stream", "search", "thunderfury"])
    assert result.stdout == ""
    assert isinstance(result.exception, TypeError)
    assert "unexpected key: results" in str(result.exception)


def test_wowhead_comments_hydration_uses_concurrency(monkeypatch) -> None:
    call_count = {"n": 0}

    def fake_replies(self, comment_id: int):
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
    monkeypatch.setattr("wowhead_cli.main._resolve_page_fetch_target", lambda *args, **
                        kwargs: MagicMock(page_entity_type="item", page_entity_id=1))

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
    assert payload["data"]["counts"]["hydrated_reply_threads"] == 2
    assert call_count["n"] == 2


def test_reply_threads_hydrated_on_worker_threads_are_counted_in_provenance_cache(monkeypatch, tmp_path) -> None:
    # The replies are fetched on a thread pool; their cache lookups must still reach the command's ledger.
    monkeypatch.setenv("WOWHEAD_CACHE_BACKEND", "file")
    monkeypatch.setenv("WOWHEAD_CACHE_DIR", str(tmp_path))

    def fake_request(self, url: str, *, params=None):
        response = MagicMock()
        response.json.return_value = [{"id": params["id"] * 10}]
        return response

    monkeypatch.setattr(WowheadClient, "_request_with_retries", fake_request)
    monkeypatch.setattr(
        "wowhead_cli.main._fetch_entity_page",
        lambda *args, **kwargs: (
            '<html><script>var lv_comments0 = [{"id":1,"nreplies":2,"replies":[]},{"id":2,"nreplies":2,"replies":[]}];</script></html>',
            {"canonical_url": "https://www.wowhead.com/item=1", "title": "T"},
        ),
    )
    monkeypatch.setattr("wowhead_cli.main._resolve_page_fetch_target", lambda *args, **kwargs: MagicMock(page_entity_type="item", page_entity_id=1))
    argv = ["comments", "item", "1", "--limit", "2", "--hydrate-missing-replies", "--max-concurrency", "2"]

    first = json.loads(runner.invoke(app, argv).stdout)
    second = json.loads(runner.invoke(app, argv).stdout)

    assert second["data"]["counts"]["hydrated_reply_threads"] == 2
    assert (first["provenance"]["cache"]["lookups"], first["provenance"]["cache"]["hits"]) == (2, 0)
    assert (second["provenance"]["cache"]["lookups"], second["provenance"]["cache"]["hits"]) == (2, 2)

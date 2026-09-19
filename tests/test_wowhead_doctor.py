from __future__ import annotations

import json

import httpx
from typer.testing import CliRunner
from wowhead_cli.doctor import _latency_bucket, _probe_result
from wowhead_cli.main import app

runner = CliRunner()


def test_wowhead_doctor_no_live_reports_cache_and_skipped_probes(monkeypatch) -> None:
    monkeypatch.setenv("WOWHEAD_CACHE_BACKEND", "none")
    result = runner.invoke(app, ["doctor", "--no-live"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["provider"] == "wowhead"
    assert payload["status"] == "ready"
    assert payload["expansion"] == "retail"
    assert payload["endpoints"]["search_suggestions"]["skipped"] is True
    assert payload["cache"]["enabled"] is False


HEALTHY_ENTITY_HTML = """
<html><head>
  <link rel="canonical" href="https://www.wowhead.com/wotlk/item=19019/thunderfury">
  <script type="application/json" id="data.pageMeta">{"dataEnv":{"env":8}}</script>
</head><body>
  <a href="/npc=12056/baron-geddon">Baron Geddon</a>
  <a href="/npc=12056/baron-geddon">Baron Geddon again</a>
  <script>var lv_comments0 = [{"id": 11, "number": 0, "user": "A", "body": "b", "date": "2024-01-01T00:00:00-06:00", "rating": 1, "nreplies": 0, "replies": []}];</script>
</body></html>
"""


def _mock_transport_client(handler):  # noqa: ANN001, ANN202
    """Give `doctor` an httpx client whose responses come from `handler`, with no socket involved."""
    return lambda timeout=None: httpx.Client(transport=httpx.MockTransport(handler))


def test_latency_bucket_boundaries() -> None:
    assert _latency_bucket(0.0) == "fast"
    assert _latency_bucket(499.9) == "fast"
    assert _latency_bucket(500.0) == "moderate"
    assert _latency_bucket(1999.9) == "moderate"
    assert _latency_bucket(2000.0) == "slow"


def test_probe_result_reports_only_the_fields_it_was_given() -> None:
    minimal = _probe_result(ok=True, latency_ms=12.34)
    assert minimal == {"ok": True, "latency_ms": 12.3, "latency_bucket": "fast"}
    full = _probe_result(ok=False, latency_ms=2500.0, status_code=500, error="boom", shape={"n": 0})
    assert full == {
        "ok": False,
        "latency_ms": 2500.0,
        "latency_bucket": "slow",
        "status_code": 500,
        "error": "boom",
        "shape": {"n": 0},
    }


def test_live_probes_check_the_shapes_they_claim_to_check(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "suggestions" in url:
            return httpx.Response(200, json={"results": [{"id": 19019, "typeName": "Item"}]})
        if "tooltip" in url:
            return httpx.Response(200, json={"name": "Thunderfury", "tooltip": "<table></table>"})
        return httpx.Response(200, text=HEALTHY_ENTITY_HTML)

    monkeypatch.setattr("wowhead_cli.doctor.build_client", _mock_transport_client(handler))
    result = runner.invoke(app, ["--expansion", "wotlk", "doctor"])
    assert result.exit_code == 0

    endpoints = json.loads(result.stdout)["data"]["endpoints"]
    assert endpoints["search_suggestions"]["ok"] is True
    assert endpoints["search_suggestions"]["shape"] == {"result_count": 1}
    assert endpoints["tooltip"]["shape"] == {"has_name": True, "has_tooltip": True}
    assert endpoints["entity_page"]["shape"] == {
        "linked_entity_count": 2,
        "comment_count": 1,
        "data_env_match": True,
    }
    assert json.loads(result.stdout)["data"]["status"] == "ready"


def test_entity_page_probe_rejects_a_page_served_from_another_expansion(monkeypatch) -> None:
    """The page parses perfectly; only its `dataEnv` shows Wowhead served a different data tree."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "suggestions" in url:
            return httpx.Response(200, json={"results": [{"id": 19019, "typeName": "Item"}]})
        if "tooltip" in url:
            return httpx.Response(200, json={"name": "Thunderfury", "tooltip": "<table></table>"})
        return httpx.Response(200, text=HEALTHY_ENTITY_HTML)  # dataEnv 8 is wotlk, not retail

    monkeypatch.setattr("wowhead_cli.doctor.build_client", _mock_transport_client(handler))
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0

    data = json.loads(result.stdout)["data"]
    entity_page = data["endpoints"]["entity_page"]
    assert entity_page["shape"]["data_env_match"] is False
    assert entity_page["shape"]["linked_entity_count"] == 2
    assert entity_page["ok"] is False
    assert entity_page["error"] == "entity page parser checks failed"
    assert data["failed_probes"] == ["entity_page"]
    assert data["status"] == "degraded"


def test_live_probes_flag_a_page_whose_shape_no_longer_matches(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "tooltip" in url:
            return httpx.Response(200, json={"name": "Thunderfury"})
        if "suggestions" in url:
            return httpx.Response(200, json={"results": []})
        return httpx.Response(200, text="<html><body>redesigned<script>var lv_comments0 = [];</script></body></html>")

    monkeypatch.setattr("wowhead_cli.doctor.build_client", _mock_transport_client(handler))
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0

    data = json.loads(result.stdout)["data"]
    assert data["status"] == "error"
    assert sorted(data["failed_probes"]) == ["entity_page", "search_suggestions", "tooltip"]
    assert data["endpoints"]["search_suggestions"]["error"] == "search results missing or empty"
    assert data["endpoints"]["tooltip"]["error"] == "tooltip payload missing name or tooltip"
    assert data["endpoints"]["entity_page"]["error"] == "entity page parser checks failed"


def test_live_probe_transport_failure_becomes_a_failed_probe(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host", request=request)

    monkeypatch.setattr("wowhead_cli.doctor.build_client", _mock_transport_client(handler))
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0

    data = json.loads(result.stdout)["data"]
    assert data["status"] == "error"
    assert data["endpoints"]["tooltip"]["ok"] is False
    assert "no route to host" in data["endpoints"]["tooltip"]["error"]


def test_wowhead_doctor_marks_degraded_when_only_one_probe_fails(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "suggestions" in url:
            return httpx.Response(500, json={})
        if "tooltip" in url:
            return httpx.Response(200, json={"name": "Thunderfury", "tooltip": "<table></table>"})
        return httpx.Response(200, text=HEALTHY_ENTITY_HTML)

    monkeypatch.setattr("wowhead_cli.doctor.build_client", _mock_transport_client(handler))
    result = runner.invoke(app, ["--expansion", "wotlk", "doctor"])
    assert result.exit_code == 0

    data = json.loads(result.stdout)["data"]
    assert data["status"] == "degraded"
    assert data["failed_probes"] == ["search_suggestions"]

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

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


def test_wowhead_doctor_live_probes_use_expansion_profile(monkeypatch) -> None:
    monkeypatch.setattr(
        "wowhead_cli.doctor._probe_search_suggestions",
        lambda profile, **kwargs: {
            "ok": True,
            "latency_ms": 120.0,
            "latency_bucket": "fast",
            "status_code": 200,
        },
    )
    monkeypatch.setattr(
        "wowhead_cli.doctor._probe_tooltip",
        lambda profile, **kwargs: {
            "ok": True,
            "latency_ms": 80.0,
            "latency_bucket": "fast",
            "status_code": 200,
        },
    )
    monkeypatch.setattr(
        "wowhead_cli.doctor._probe_entity_page",
        lambda profile, **kwargs: {
            "ok": True,
            "latency_ms": 400.0,
            "latency_bucket": "fast",
            "status_code": 200,
        },
    )
    result = runner.invoke(app, ["--expansion", "wotlk", "doctor"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["expansion"] == "wotlk"
    assert payload["status"] == "ready"
    assert payload["endpoints"]["search_suggestions"]["latency_bucket"] == "fast"
    assert payload["failed_probes"] == []


def test_wowhead_doctor_marks_degraded_when_a_probe_fails(monkeypatch) -> None:
    monkeypatch.setattr(
        "wowhead_cli.doctor._probe_search_suggestions",
        lambda profile, **kwargs: {"ok": False, "latency_ms": 900.0, "latency_bucket": "moderate", "error": "timeout"},
    )
    monkeypatch.setattr(
        "wowhead_cli.doctor._probe_tooltip",
        lambda profile, **kwargs: {"ok": True, "latency_ms": 80.0, "latency_bucket": "fast", "status_code": 200},
    )
    monkeypatch.setattr(
        "wowhead_cli.doctor._probe_entity_page",
        lambda profile, **kwargs: {"ok": True, "latency_ms": 400.0, "latency_bucket": "fast", "status_code": 200},
    )
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "degraded"
    assert payload["failed_probes"] == ["search_suggestions"]

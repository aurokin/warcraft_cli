"""Forever requests stay scoped and never fall back to another game's provider data."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner
from warcraft_cli.main import app
from warcraft_cli.providers import get_provider, provider_expansion_support, provider_invoke
from warcraft_core.envelope import success_envelope

runner = CliRunner()


@pytest.mark.parametrize("command", ["search", "resolve"])
def test_forever_discovery_calls_only_forever_wowhead(monkeypatch, command):
    calls = []

    def lookup(provider, query, *, expansion=None, **options):
        calls.append((provider, expansion))
        row = {"provider": "wowhead", "kind": "item", "id": 19019, "name": "Thunderfury"}
        return {
            "provider": provider,
            "exit_code": 0,
            "payload": success_envelope(
                provider=provider, command=command, kind=command,
                data={"results": [row], "candidates": [row], "match": row, "resolved": True, "confidence": "high"},
            ),
        }

    monkeypatch.setattr(f"warcraft_cli.main.provider_{command}", lookup)
    result = runner.invoke(app, ["--expansion", "forever", command, "Thunderfury"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)["data"]
    assert calls == [("wowhead", "forever")]
    assert data["included_providers"] == ["wowhead"]
    assert {row["provider"] for row in data["excluded_providers"]} == {
        "method", "icy-veins", "raiderio", "warcraftlogs", "warcraft-wiki", "simc", "raidbots", "blizzard-api", "curseforge", "lorrgs",
    }


@pytest.mark.parametrize("provider,command", [("simc", "simc"), ("blizzard-api", "blizzard"), ("curseforge", "curseforge"), ("warcraftlogs", "warcraftlogs")])
def test_forever_passthrough_rejects_unverified_provider_before_call(monkeypatch, provider, command):
    monkeypatch.setattr("warcraft_cli.main.invoke_provider_command", lambda *_args, **_kwargs: pytest.fail("Unscoped provider called"))
    result = runner.invoke(app, ["--expansion", "forever", command, "doctor"])
    assert result.exit_code == 2, result.output
    error = json.loads(result.stderr)["error"]
    assert error["code"] == "unsupported_provider_expansion"
    assert error["details"]["provider"] == provider
    support = error["details"]["expansion_support"]
    assert support["requested_expansion"] == "forever" and support["allowed"] is False
    assert support["exclusion_reason"] == "provider_game_profile_unverified"
    assert support["verification_note"]


@pytest.mark.parametrize("provider", ["simc", "blizzard-api", "curseforge"])
def test_forever_composite_operation_cannot_bypass_passthrough_guard(monkeypatch, provider):
    monkeypatch.setattr("warcraft_cli.providers._composite_operation", lambda *_args, **_kwargs: pytest.fail("Unscoped operation called"))
    result = provider_invoke(provider, ["doctor"], expansion="forever")
    assert result["exit_code"] == 2
    assert result["payload"]["error"]["code"] == "unsupported_provider_expansion"


def test_forever_capabilities_explain_upstream_verification_gaps():
    for provider in ("simc", "blizzard-api", "curseforge", "warcraftlogs"):
        support = provider_expansion_support(get_provider(provider), requested_expansion="forever")
        assert support["allowed"] is False and support["verification_note"]
    support = provider_expansion_support(get_provider("wowhead"), requested_expansion="forever")
    assert support["allowed"] is True

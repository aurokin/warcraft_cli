"""The pure ``wowhead_cli.provider`` surface must satisfy the shared protocol and envelope."""

from __future__ import annotations

import json

import pytest
from warcraft_core.envelope import ENVELOPE_KEYS, REQUIRED_KEYS, envelope_violations
from warcraft_core.provider import ProviderError, ProviderSurface
from wowhead_cli import provider
from wowhead_cli.main import app
from wowhead_cli.wowhead_client import WowheadClient

from tests.wowhead_testkit import runner

SUGGESTIONS = {
    "results": [
        {"id": 19019, "name": "Thunderfury", "type": 3, "typeName": "Item", "popularity": 900},
        {"id": 19020, "name": "Thunderfury Replica", "type": 3, "typeName": "Item", "popularity": 5},
    ]
}


@pytest.fixture(autouse=True)
def _synthetic_suggestions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(WowheadClient, "search_suggestions", lambda self, query: SUGGESTIONS)


def test_provider_object_satisfies_the_shared_protocol() -> None:
    assert isinstance(provider.PROVIDER, ProviderSurface)
    assert provider.PROVIDER.name == "wowhead"


def test_search_returns_a_conforming_envelope() -> None:
    payload = provider.PROVIDER.search("thunderfury", limit=1)

    assert envelope_violations(payload) == []
    assert set(payload) == REQUIRED_KEYS
    assert payload["kind"] == "search_results"
    assert payload["data"]["count"] == 1
    assert payload["data"]["total_matches"] == 2
    assert payload["data"]["truncated"] is True
    assert len(payload["data"]["results"]) == 1


def test_resolve_returns_a_conforming_envelope() -> None:
    payload = provider.PROVIDER.resolve("thunderfury")

    assert envelope_violations(payload) == []
    assert payload["kind"] == "resolve_match"
    assert payload["data"]["confidence"] == "high"
    assert payload["data"]["next_command"] == "wowhead entity item 19019"


def test_doctor_skips_live_probes_and_conforms() -> None:
    payload = provider.PROVIDER.doctor(live=False)

    assert envelope_violations(payload) == []
    assert payload["data"]["capabilities"]["search"] == "ready"


def test_unknown_expansion_raises_provider_error_instead_of_exiting() -> None:
    with pytest.raises(ProviderError) as excinfo:
        provider.PROVIDER.search("thunderfury", expansion="not-a-real-expansion")

    assert excinfo.value.code == "invalid_argument"
    assert excinfo.value.exit_code == 2


@pytest.mark.parametrize("args", [["search", "thunderfury"], ["expansions"]], ids=["surface", "flat-payload"])
def test_cli_success_envelope_has_only_the_envelope_keys(args: list[str]) -> None:
    """Command payloads live under ``data``; nothing is mirrored at the top level."""
    result = runner.invoke(app, args)

    assert result.exit_code == 0, result.stderr
    assert set(json.loads(result.stdout)) == REQUIRED_KEYS


def test_cli_error_envelope_has_only_the_envelope_keys() -> None:
    result = runner.invoke(app, ["search", "   "])

    assert result.exit_code == 2
    payload = json.loads(result.stderr)
    assert set(payload) == ENVELOPE_KEYS
    assert payload["error"]["code"] == "invalid_query"


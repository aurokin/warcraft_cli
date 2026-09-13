"""The pure ``wowhead_cli.provider`` surface must satisfy the shared protocol and envelope."""

from __future__ import annotations

import pytest
from warcraft_core.envelope import envelope_violations
from warcraft_core.provider import ProviderError, ProviderSurface
from wowhead_cli import provider
from wowhead_cli.wowhead_client import WowheadClient

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


def test_search_returns_a_conforming_envelope_with_legacy_keys() -> None:
    payload = provider.PROVIDER.search("thunderfury", limit=1)

    assert envelope_violations(payload) == []
    assert payload["kind"] == "search_results"
    # Agents read the flat keys; they must survive alongside the envelope.
    assert payload["count"] == 2
    assert len(payload["results"]) == 1


def test_resolve_returns_a_conforming_envelope() -> None:
    payload = provider.PROVIDER.resolve("thunderfury")

    assert envelope_violations(payload) == []
    assert payload["kind"] == "resolve_match"
    assert payload["confidence"] == "high"
    assert payload["next_command"] == "wowhead entity item 19019"


def test_doctor_skips_live_probes_and_conforms() -> None:
    payload = provider.PROVIDER.doctor(live=False)

    assert envelope_violations(payload) == []
    assert payload["capabilities"]["search"] == "ready"


def test_unknown_expansion_raises_provider_error_instead_of_exiting() -> None:
    with pytest.raises(ProviderError) as excinfo:
        provider.PROVIDER.search("thunderfury", expansion="not-a-real-expansion")

    assert excinfo.value.code == "invalid_argument"
    assert excinfo.value.exit_code == 2

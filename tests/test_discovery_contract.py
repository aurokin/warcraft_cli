"""One search/resolve data shape, every provider.

Supported ``PROVIDER`` discovery surfaces use ``warcraft_core.discovery`` shapes. Unsupported
operations fail explicitly; populated offline answers exercise truncation with ``limit=1``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from typer.testing import CliRunner
from warcraft_cli.main import app as warcraft_app
from warcraft_cli.providers import PROVIDERS
from warcraft_core.discovery import RESOLVE_KIND, SEARCH_KIND
from warcraft_core.provider import ProviderError

from tests.cli_testkit import DISCOVERY_STUBS
from tests.discovery_contract import resolve_data_violations, row_violations, search_data_violations

SURFACES = ("search", "resolve")

CASES = [
    pytest.param(registration, surface, id=f"{registration.name}-{surface}")
    for registration in PROVIDERS
    for surface in SURFACES
]


@pytest.mark.parametrize(("registration", "surface"), CASES)
def test_discovery_surface_returns_the_shared_shape(registration: Any, surface: str, monkeypatch: pytest.MonkeyPatch) -> None:
    stub = DISCOVERY_STUBS[registration.name]
    for target, replacement in stub.seams:
        monkeypatch.setattr(target, replacement)
    call = registration.surface.search if surface == "search" else registration.surface.resolve
    capability = registration.wrapper_capabilities[surface]
    if capability == "not_supported":
        with pytest.raises(ProviderError) as failure:
            call(stub.query, limit=1)
        assert failure.value.code == "unsupported_operation"
        assert failure.value.exit_code == 2
        assert failure.value.details["operation"] == surface
        assert failure.value.details["available_commands"]
        return
    payload = call(stub.query, limit=1)

    data = payload["data"]
    assert payload["kind"] == (SEARCH_KIND if surface == "search" else RESOLVE_KIND)
    violations = search_data_violations if surface == "search" else resolve_data_violations
    assert violations(data, provider=registration.name) == []
    rows = data["results"] if surface == "search" else data["candidates"]
    if capability in {"coming_soon", "not_supported"}:
        assert data[capability] is True
        assert rows == [] and data["total_matches"] is None
        assert data["suggested_command"]
        if surface == "resolve":
            assert data["confidence"] == "none"
    elif capability == "ready":
        # A real search finds several rows, so limit=1 truncates.
        assert data["total_matches"] >= 3 and data["truncated"] is True
    else:
        assert rows, f"{registration.name} {surface} answers its explicit reference"


def test_every_provider_has_a_discovery_stub() -> None:
    assert set(DISCOVERY_STUBS) == {registration.name for registration in PROVIDERS}


def test_wrapper_search_merges_each_providers_rows_with_their_own_core(monkeypatch: pytest.MonkeyPatch) -> None:
    """`warcraft search` adds its ranking beside each row and patches nothing in: the core is the provider's."""
    for stub in DISCOVERY_STUBS.values():
        for target, replacement in stub.seams:
            monkeypatch.setattr(target, replacement)

    result = CliRunner().invoke(warcraft_app, ["search", "mage", "--limit", "10"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)["data"]
    assert data["count"] == len(data["results"])
    providers = {row["provider"] for row in data["results"]}
    assert len(providers) >= 3 and providers <= set(data["included_providers"])
    for row in data["results"]:
        assert row_violations(row, provider=row["provider"]) == []

"""One envelope, twelve providers.

Every ``PROVIDER`` surface must return the shape defined in ``warcraft_core.envelope`` regardless of
whether the provider is ready, stubbed, or unsupported. Providers keep their historical payload keys
as deprecated top-level copies, but ``data`` is the canonical location and is what this file asserts.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from cli_testkit import WARCRAFTLOGS_REPORT_QUERY, apply_provider_stubs, run_binary
from warcraft_cli.providers import PROVIDERS
from warcraft_core.envelope import envelope_violations

PROVIDER_IDS = [registration.name for registration in PROVIDERS]
TIERS = {
    "core": {"wowhead", "warcraftlogs", "simc"},
    "supported": {"raiderio", "warcraft-wiki", "icy-veins", "method", "lorrgs"},
    "experimental": {"raidbots", "blizzard-api", "curseforge"},
}


def assert_envelope(payload: Any, *, context: str) -> None:
    problems = envelope_violations(payload)
    assert not problems, f"{context}: {problems}"


def _surface_query(name: str) -> str:
    # Warcraft Logs only resolves explicit report references; free text returns a discovery hint.
    return WARCRAFTLOGS_REPORT_QUERY if name == "warcraftlogs" else "thunderfury"


@pytest.mark.parametrize("registration", PROVIDERS, ids=PROVIDER_IDS)
def test_doctor_returns_a_conforming_envelope(registration: Any) -> None:
    """``envelope["provider"]`` is the registry ``name``, which differs from the binary for blizzard-api."""
    payload = registration.surface.doctor(**registration.doctor_options)
    assert_envelope(payload, context=f"{registration.name} doctor")
    assert payload["command"] == "doctor"
    assert payload["provider"] == registration.name
    assert payload["ok"] is True


@pytest.mark.parametrize("registration", PROVIDERS, ids=PROVIDER_IDS)
@pytest.mark.parametrize("surface", ["search", "resolve"])
def test_ready_surfaces_return_a_conforming_envelope_offline(
    registration: Any, surface: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not registration.wrapper_capabilities.get(surface, "").startswith("ready"):
        pytest.skip(f"{registration.name} {surface} is {registration.wrapper_capabilities.get(surface)!r}, not ready")
    apply_provider_stubs(registration.name, monkeypatch)
    query = _surface_query(registration.name)
    payload = registration.surface.search(query, limit=3) if surface == "search" else registration.surface.resolve(query)

    context = f"{registration.name} {surface}"
    assert_envelope(payload, context=context)
    assert payload["ok"] is True
    assert payload["command"] == surface
    data = payload["data"]
    if surface == "search":
        assert isinstance(data["results"], list), f"{context}: data.results must be a list"
        assert isinstance(data["count"], int), f"{context}: data.count must be an int"
    else:
        assert isinstance(data["resolved"], bool), f"{context}: data.resolved must be a bool"


@pytest.mark.parametrize("registration", PROVIDERS, ids=PROVIDER_IDS)
@pytest.mark.parametrize("surface", ["search", "resolve"])
def test_stubbed_surfaces_return_a_flagged_success_envelope(registration: Any, surface: str) -> None:
    """A surface that is not built yet answers with ``ok: true`` plus an explicit flag, never an error.

    The flag lives under ``data`` (canonical) and is mirrored at the top level by the deprecated
    dual-emit, so an agent probing an advertised surface always gets a machine-readable "not yet".
    """
    capability = registration.wrapper_capabilities.get(surface)
    if capability not in {"coming_soon", "not_supported"}:
        pytest.skip(f"{registration.name} {surface} is {capability!r}")
    query = "probe-query"
    payload = registration.surface.search(query, limit=1) if surface == "search" else registration.surface.resolve(query)

    context = f"{registration.name} {surface}"
    assert_envelope(payload, context=context)
    assert payload["ok"] is True
    assert payload["data"].get(capability) is True, f"{context}: data.{capability} must be True"
    assert payload[capability] is True, f"{context}: the legacy top-level {capability} copy must be kept"
    assert payload["data"].get("suggested_command"), f"{context}: tell the agent what to run instead"


def test_every_provider_is_assigned_to_exactly_one_tier() -> None:
    registry_tiers = {registration.name: registration.tier for registration in PROVIDERS}
    expected = {name: tier for tier, names in TIERS.items() for name in names}
    assert registry_tiers == expected


def test_wrapper_own_commands_return_a_conforming_envelope(tmp_path: Any) -> None:
    """The wrapper's own payloads (not passthrough) carry the envelope keys on success and failure."""
    doctor = run_binary("warcraft", ["doctor"])
    assert doctor.exit_code == 0, doctor.stderr
    payload = json.loads(doctor.stdout)
    assert envelope_violations(payload) == []
    assert payload["provider"] == "warcraft" and payload["command"] == "doctor"
    assert payload["data"]["wrapper"] == payload["wrapper"], "legacy top-level keys are mirrored into data"

    failure = run_binary("warcraft", ["guide-compare", str(tmp_path / "a"), str(tmp_path / "b")])
    assert failure.exit_code != 0
    error_payload = json.loads(failure.stderr)
    assert envelope_violations(error_payload) == []
    assert error_payload["ok"] is False and error_payload["error"]["code"] == "invalid_bundle"


def _offline_provider_result(provider: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
    """What every wrapper provider seam returns when the provider cannot answer."""
    return {
        "provider": provider,
        "exit_code": 5,
        "payload": {
            "ok": False,
            "provider": provider,
            "command": "search",
            "kind": "error",
            "schema_version": "1",
            "query": None,
            "provenance": {},
            "data": {},
            "error": {"code": "network_error", "message": "ConnectError: offline"},
        },
        "stdout": "",
    }


# Every command `warcraft` owns (passthrough proxies are the provider's own envelope, covered above).
_WRAPPER_OWN_COMMANDS = (
    "doctor",
    "schema",
    "search",
    "resolve",
    "guild",
    "guild-ranks",
    "actor-profile",
    "cooldown-packet",
    "guide-compare",
    "guide-compare-query",
    "talent-packet",
    "talent-describe",
    "guide-builds-simc",
)


def _wrapper_command_args(command: str, tmp_path: Any) -> list[str]:
    empty_source = tmp_path / "bundle"
    empty_source.mkdir(exist_ok=True)
    return {
        "doctor": ["doctor"],
        "schema": ["schema"],
        "search": ["search", "thunderfury"],
        "resolve": ["resolve", "thunderfury"],
        "guild": ["guild", "us", "malganis", "gn"],
        "guild-ranks": ["guild-ranks", "us", "malganis", "gn"],
        "actor-profile": ["actor-profile", "abcd1234", "Someone"],
        "cooldown-packet": ["cooldown-packet", "abcd1234", "--fight-id", "1", "--actor-id", "1", "--phase", "1"],
        "guide-compare": ["guide-compare", str(tmp_path / "a"), str(tmp_path / "b")],
        "guide-compare-query": ["guide-compare-query", "mistweaver monk", "--out-root", str(tmp_path / "out")],
        "talent-packet": ["talent-packet", "druid/balance/ABC123", "--no-validate"],
        "talent-describe": ["talent-describe", "druid/balance/ABC123", "--no-validate"],
        "guide-builds-simc": ["guide-builds-simc", str(empty_source)],
    }[command]


@pytest.mark.parametrize("command", _WRAPPER_OWN_COMMANDS)
def test_wrapper_own_command_envelopes_conform_offline(
    command: str, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every wrapper-owned command emits a conforming envelope when no provider can answer."""
    for seam in ("provider_invoke", "provider_search", "provider_resolve"):
        monkeypatch.setattr(f"warcraft_cli.main.{seam}", _offline_provider_result)

    result = run_binary("warcraft", _wrapper_command_args(command, tmp_path))

    stream = result.stdout if result.exit_code == 0 else result.stderr
    payload = json.loads(stream)
    assert envelope_violations(payload) == [], f"{command}: {envelope_violations(payload)}"
    assert payload["provider"] == "warcraft"
    assert payload["command"] == command
    assert payload["ok"] is (result.exit_code == 0)
    if payload["ok"] is False:
        assert set(payload["error"]) <= {"code", "message", "details"}, f"{command}: error keys beyond the contract"

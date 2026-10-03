"""One envelope, every provider.

Every ``PROVIDER`` surface must return the shape defined in ``warcraft_core.envelope`` regardless of
whether the provider is ready, stubbed, or unsupported. The payload lives under ``data``; the wrapper's
own envelopes carry the envelope keys and nothing else.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from warcraft_cli.main import app as warcraft_app
from warcraft_cli.providers import PROVIDERS
from warcraft_core.envelope import ENVELOPE_KEYS, REQUIRED_KEYS, envelope_violations
from warcraft_core.identity import build_reference_transport_packet_payload

from tests.cli_testkit import apply_provider_stubs, run_binary
from tests.discovery_contract import resolve_data_violations, search_data_violations

PROVIDER_IDS = [registration.name for registration in PROVIDERS]
SURFACES = ("search", "resolve")
TIERS = {
    "core": {"wowhead", "warcraftlogs", "simc"},
    "supported": {"raiderio", "warcraft-wiki", "icy-veins", "method", "lorrgs"},
    "experimental": {"raidbots", "blizzard-api", "curseforge"},
}


def assert_envelope(payload: Any, *, context: str) -> None:
    problems = envelope_violations(payload)
    assert not problems, f"{context}: {problems}"


@pytest.mark.parametrize("registration", PROVIDERS, ids=PROVIDER_IDS)
def test_doctor_returns_a_conforming_envelope(registration: Any) -> None:
    """``envelope["provider"]`` is the registry ``name``, which differs from the binary for blizzard-api."""
    payload = registration.surface.doctor(**registration.doctor_options)
    assert_envelope(payload, context=f"{registration.name} doctor")
    assert payload["command"] == "doctor"
    assert payload["provider"] == registration.name
    assert payload["ok"] is True


@pytest.mark.parametrize(
    ("registration", "surface"),
    [pytest.param(registration, surface, id=f"{registration.name}-{surface}") for registration in PROVIDERS for surface in SURFACES],
)
def test_every_surface_answers_an_empty_upstream_with_a_conforming_envelope(
    registration: Any, surface: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ready, explicit-report-only or stubbed, a search/resolve that found nothing still answers ``ok: true``
    in the shared shape (``tests/test_discovery_contract.py`` checks the populated answers).

    An empty resolve hands over no ``fallback_search_command``: the same search would come back empty.
    """
    apply_provider_stubs(registration.name, monkeypatch)
    call = registration.surface.search if surface == "search" else registration.surface.resolve
    payload = call("zzqx nonsense qqq", limit=3)

    context = f"{registration.name} {surface}"
    assert_envelope(payload, context=context)
    assert payload["ok"] is True
    assert payload["command"] == surface
    data = payload["data"]
    violations = search_data_violations if surface == "search" else resolve_data_violations
    assert violations(data, provider=registration.name) == []
    assert (data["results"] if surface == "search" else data["candidates"]) == []
    if surface == "resolve":
        assert (data["confidence"], data["fallback_search_command"]) == ("none", None)


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

    # A missing bundle is the shared loader's not_found, with the exit code the contract maps it to.
    failure = run_binary("warcraft", ["guide-compare", str(tmp_path / "a"), str(tmp_path / "b")])
    assert failure.exit_code == 4
    error_payload = json.loads(failure.stderr)
    assert envelope_violations(error_payload) == []
    assert error_payload["ok"] is False and error_payload["error"]["code"] == "not_found"
    assert error_payload["kind"] == "error"


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
    }


# Every command `warcraft` owns, read from the registered app so a new command cannot skip these
# checks (passthrough proxies are the provider's own envelope, covered above).
_WRAPPER_OWN_COMMANDS = tuple(
    str(command.name)
    for command in warcraft_app.registered_commands
    if command.name not in {registration.command for registration in PROVIDERS}
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
    assert set(payload) == (REQUIRED_KEYS if payload["ok"] else ENVELOPE_KEYS), f"{command}: keys beyond the envelope"
    if payload["ok"] is False:
        assert payload["kind"] == "error", f"{command}: a failure envelope's kind is always error"
        assert payload["data"] == {}
        assert set(payload["error"]) <= {"code", "message", "details"}, f"{command}: error keys beyond the contract"


_WOWHEAD_TALENT_CALC_REF = "https://www.wowhead.com/talent-calc/druid/balance/ABC123"


def _answering_provider_invoke(provider: str, args: list[str], **kwargs: Any) -> dict[str, Any]:
    """A provider that answers, so the wrapper's *success* envelopes get exercised as well."""
    bodies: dict[str, dict[str, Any]] = {
        "guild": {
            "guild": {"name": "gn", "region": "us", "realm": "Mal'Ganis", "faction": "horde", "member_count": 30},
            "raiding": {
                "progression": [{"raid_slug": "manaforge-omega", "mythic_bosses_killed": 8}],
                "rankings": [{"raid_slug": "manaforge-omega", "mythic": {"world": 19, "region": 6, "realm": 2}}],
            },
            "citations": ["https://raider.io/guilds/us/mal-ganis/gn"],
        },
        "report-fights": {"fights": [{"id": 1, "kill": True}]},
        "report-player-details": {
            "player_details": {
                "roles": {
                    "dps": [
                        {
                            "name": "Someone",
                            "id": 1,
                            "server": "Mal'Ganis",
                            "region": "us",
                            "specs": [{"spec": "Balance", "count": 1}],
                        }
                    ]
                }
            }
        },
        "character": {"character": {"name": "Someone", "profile_url": "https://raider.io/x"}},
        "talent-calc-packet": {
            "talent_transport_packet": build_reference_transport_packet_payload(
                ref=_WOWHEAD_TALENT_CALC_REF,
                provider="wowhead",
                source="wowhead_talent_calc_url",
            )
        },
        "describe-build": {"build_spec": {"actor_class": "druid", "spec": "balance"}},
    }
    return {
        "provider": provider,
        "exit_code": 0,
        "payload": {"ok": True, "provider": provider, "command": args[0], "data": bodies.get(args[0], {})},
    }


def _answering_provider_search(provider: str, query: str, **kwargs: Any) -> dict[str, Any]:
    results = [
        {
            "id": 19019,
            "name": "Thunderfury, Blessed Blade of the Windseeker",
            "entity_type": "item",
            "url": "https://www.wowhead.com/item=19019",
            "ranking": {"score": 40},
        }
    ]
    return {
        "provider": provider,
        "exit_code": 0,
        "payload": {"ok": True, "provider": provider, "command": "search", "data": {"results": results, "count": 1}},
    }


def _answering_provider_resolve(provider: str, query: str, **kwargs: Any) -> dict[str, Any]:
    match = {"id": 19019, "name": "Thunderfury, Blessed Blade of the Windseeker", "entity_type": "item"}
    return {
        "provider": provider,
        "exit_code": 0,
        "payload": {
            "ok": True,
            "provider": provider,
            "command": "resolve",
            "data": {
                "resolved": True,
                "confidence": "high",
                "match": match,
                "next_command": f"{provider} item 19019",
            },
        },
    }


# The wrapper commands whose success path a provider stub alone can drive, and the argv that drives
# it. The rest need real files on disk (bundle comparisons) or a multi-provider fixture
# (cooldown-packet); `test_every_wrapper_command_is_covered_on_one_of_the_two_paths` keeps that
# split explicit so a new command cannot quietly land with failure-only coverage.
_SUCCESS_PATH_ARGS = {
    "doctor": ["doctor"],
    "schema": ["schema"],
    "search": ["search", "thunderfury"],
    "resolve": ["resolve", "thunderfury"],
    "guild": ["guild", "us", "malganis", "gn"],
    "actor-profile": ["actor-profile", "abcd1234", "Someone"],
    "talent-packet": ["talent-packet", _WOWHEAD_TALENT_CALC_REF, "--no-validate"],
}
_FAILURE_ONLY_COMMANDS = {
    "cooldown-packet": "needs a Lorrgs fight plus Warcraft Logs casts; covered in tests/test_warcraft_wrapper.py",
    "guide-compare": "needs two exported bundles on disk",
    "guide-compare-query": "needs exported bundles on disk",
    "guide-builds-simc": "needs an exported bundle with build references on disk",
    "talent-describe": "needs a simc checkout to describe the decoded build",
}


@pytest.mark.parametrize("command", sorted(_SUCCESS_PATH_ARGS))
def test_wrapper_own_command_success_envelopes_conform(
    command: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The success envelope is checked too: the offline case only ever produces failure envelopes.

    With every provider seam answering, each command below emits its real payload, so a regression in
    the success shape (a missing ``data`` body, a wrong ``command``, a dropped envelope key) fails
    here instead of passing because the only covered path was the error envelope.
    """
    monkeypatch.setattr("warcraft_cli.main.provider_invoke", _answering_provider_invoke)
    monkeypatch.setattr("warcraft_cli.main.provider_search", _answering_provider_search)
    monkeypatch.setattr("warcraft_cli.main.provider_resolve", _answering_provider_resolve)

    result = run_binary("warcraft", _SUCCESS_PATH_ARGS[command])

    assert result.exit_code == 0, f"{command}: {result.stderr}"
    payload = json.loads(result.stdout)
    assert envelope_violations(payload) == [], f"{command}: {envelope_violations(payload)}"
    assert payload["ok"] is True
    assert set(payload) == REQUIRED_KEYS, f"{command}: keys beyond the envelope"
    assert payload["provider"] == "warcraft"
    assert payload["command"] == command
    assert payload["data"], f"{command}: a success envelope must carry its payload under data"


def test_every_wrapper_command_is_covered_on_one_of_the_two_paths() -> None:
    assert set(_SUCCESS_PATH_ARGS) | set(_FAILURE_ONLY_COMMANDS) == set(_WRAPPER_OWN_COMMANDS)
    assert not set(_SUCCESS_PATH_ARGS) & set(_FAILURE_ONLY_COMMANDS)

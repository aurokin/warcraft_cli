from __future__ import annotations

import ast
import dataclasses
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
import typer
import warcraft_cli
from method_cli.main import app as method_app
from raiderio_cli.client import FetchedJson
from simc_cli.build_input import BuildIdentity, BuildResolution, BuildSpec
from simc_cli.prune import PruneContext
from typer.testing import CliRunner
from warcraft_cli.actor_profile import ACTOR_PROFILE_MAX_SCOPED_FIGHTS
from warcraft_cli.cooldown_packet import normalize_warcraftlogs_actor_casts
from warcraft_cli.guild import guild_rank_rows
from warcraft_cli.main import app as warcraft_app
from warcraft_cli.providers import PROVIDERS, DescribeOptions, PacketInput, get_provider, provider_doctor
from warcraft_content.article_bundle import write_article_bundle
from warcraft_core.cli import error_envelope_for
from warcraft_core.envelope import ENVELOPE_KEYS, REQUIRED_KEYS, envelope_violations
from warcraftlogs_cli.main import app as warcraftlogs_app
from wowhead_cli.main import app as wowhead_app

runner = CliRunner()


def _patch_profile(monkeypatch: pytest.MonkeyPatch, kind: str, fetch: Callable[..., Any]) -> None:
    """Answer ``RaiderIOClient.<kind>_profile`` with the profile body ``fetch`` returns, as a fresh response."""

    def fetched(self: Any, *, region: str, realm: str, name: str, fields: str = "") -> FetchedJson:
        body = fetch(self, region=region, realm=realm, name=name, fields=fields)
        return FetchedJson(payload=body, fetched_at="2026-01-01T00:00:00+00:00", cache_hit=False)

    monkeypatch.setattr(f"raiderio_cli.client.RaiderIOClient.{kind}_profile", fetched)


def _envelope(payload: dict[str, Any]) -> dict[str, Any]:
    """A fake provider payload in real envelope shape: every non-envelope key lives under ``data``.

    The wrapper reads provider fields from ``data`` only, so a fake that put them anywhere else would
    keep a broken wrapper green.
    """
    return {
        "ok": True,
        **{key: value for key, value in payload.items() if key in ENVELOPE_KEYS},
        "data": {key: value for key, value in payload.items() if key not in ENVELOPE_KEYS},
    }


def _assert_wrapper_success_envelope(payload: dict, *, command: str) -> None:
    """A wrapper-owned *success* payload has to conform too, not just its failure envelope."""
    assert envelope_violations(payload) == [], f"{command}: {envelope_violations(payload)}"
    assert set(payload) == REQUIRED_KEYS, f"{command}: keys beyond the envelope"
    assert payload["ok"] is True
    assert payload["provider"] == "warcraft"
    assert payload["command"] == command


@pytest.fixture(autouse=True)
def _stub_lorrgs_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep wrapper fanout tests offline while Lorrgs search/resolve are wrapper-ready."""
    monkeypatch.setattr(
        "lorrgs_cli.client.LorrgsClient.specs",
        lambda self: {"payload": {"specs": []}, "source_url": "test://lorrgs/specs"},
    )
    monkeypatch.setattr(
        "lorrgs_cli.client.LorrgsClient.bosses",
        lambda self: {"payload": {"bosses": []}, "source_url": "test://lorrgs/bosses"},
    )


def _stub_raiderio_profile_lookups(monkeypatch: pytest.MonkeyPatch, *, character: dict | None = None) -> None:
    """Structured `region realm name` queries make raiderio fetch profiles; answer 404 (no match)
    unless a fake character profile is supplied."""

    def not_found(self, *, region: str, realm: str, name: str, fields: str | None = None):  # noqa: ANN001, ANN202
        request = httpx.Request("GET", "https://raider.io/api/v1/profile")
        raise httpx.HTTPStatusError("not found", request=request, response=httpx.Response(404, request=request))

    def fake_character(self, *, region: str, realm: str, name: str, fields: str | None = None):  # noqa: ANN001, ANN202
        return dict(character) if character is not None else not_found(self, region=region, realm=realm, name=name)

    _patch_profile(monkeypatch, "character", fake_character)
    _patch_profile(monkeypatch, "guild", not_found)


_LIQUID_GUILD_PROFILE = {
    "name": "Liquid",
    "region": "us",
    "realm": "Illidan",
    "faction": "horde",
    "profile_url": "https://raider.io/guilds/us/illidan/Liquid",
    "members": [],
}


def _stub_raiderio_guild_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make structured `guild region realm name` queries hit a Raider.IO guild profile."""
    _patch_profile(monkeypatch, "guild",
        lambda self, *, region, realm, name, fields=None: dict(_LIQUID_GUILD_PROFILE),
    )


def _disable_wowhead_page_fetch(monkeypatch) -> None:  # noqa: ANN001
    def fake_page_html(self, page_url: str):  # noqa: ANN001
        raise httpx.ConnectError("network disabled", request=httpx.Request("GET", page_url))

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.page_html", fake_page_html)


def _simc_call_summary(
    command: str, build: PacketInput | str, describe: DescribeOptions | None = None
) -> dict[str, object]:
    """What one in-process ``simc_call`` was handed: the command, the build form, and the describe view."""
    summary: dict[str, object] = {"command": command, "describe": describe}
    if isinstance(build, PacketInput):
        transport_forms = build.packet.get("transport_forms") if isinstance(build.packet.get("transport_forms"), dict) else {}
        summary["build_input"] = "packet"
        summary["packet"] = build.packet
        summary["packet_path"] = build.path
        summary["packet_transport_status"] = build.packet["transport_status"]
        summary["packet_transport_url"] = transport_forms.get("wowhead_talent_calc_url")
        summary["packet_transport_form_keys"] = sorted(transport_forms)
    else:
        summary["build_input"] = "text"
        summary["build_text"] = build
    return summary


def _simc_result(payload: dict[str, object], *, exit_code: int = 0) -> dict[str, object]:
    """A ``simc_call`` result: what the in-process simc command returned."""
    return {"provider": "simc", "exit_code": exit_code, "payload": payload}


class _EndToEndWarcraftLogsClient:
    _finished_report_ttl = 86400
    _report_ttl = 60
    _cache_store = object()  # non-None: provenance emits cache-on TTLs

    def close(self) -> None:
        return None

    def report(self, *, code: str, allow_unlisted: bool = False) -> dict[str, object]:
        assert code == "abcd1234"
        return {
            "code": "abcd1234",
            "title": "Manaforge Omega - Liquid",
            "startTime": 123,
            "endTime": 456,
            "visibility": "public",
            "archiveStatus": {
                "isArchived": True,
                "isAccessible": True,
                "archiveDate": 789,
            },
            "segments": 1,
            "exportedSegments": 0,
            "zone": {"id": 38, "name": "Manaforge Omega"},
        }

    def report_fights(
        self,
        *,
        code: str,
        difficulty: int | None = None,
        allow_unlisted: bool = False,
        ttl_override: int | None = None,
    ) -> dict[str, object]:
        assert code == "abcd1234"
        return {
            "code": "abcd1234",
            "title": "Manaforge Omega - Liquid",
            "zone": {"id": 38, "name": "Manaforge Omega"},
            "fights": [
                {
                    "id": 1,
                    "name": "Dimensius, the All-Devouring",
                    "encounterID": 3012,
                    "difficulty": 5,
                    "kill": True,
                    "completeRaid": False,
                    "startTime": 100000,
                    "endTime": 200000,
                    "fightPercentage": 100,
                    "bossPercentage": 0,
                    "averageItemLevel": 685.2,
                    "size": 20,
                }
            ],
        }

    def encounter(self, *, encounter_id: int) -> dict[str, object]:
        assert encounter_id == 3012
        return {
            "id": 3012,
            "name": "Dimensius, the All-Devouring",
            "journalID": 9001,
            "zone": {"id": 38, "name": "Manaforge Omega", "expansion": {"id": 12, "name": "Midnight"}},
        }

    def report_player_details(self, *, code: str, allow_unlisted: bool = False, options, ttl_override: int | None = None) -> dict[str, object]:  # noqa: ANN001
        assert code == "abcd1234"
        assert options.fight_ids == [1]
        return {
            "code": "abcd1234",
            "title": "Manaforge Omega - Liquid",
            "zone": {"id": 38, "name": "Manaforge Omega"},
            "playerDetails": {
                "data": {
                    "tanks": [],
                    "healers": [],
                    "dps": [
                        {
                            "name": "Auropower",
                            "id": 9,
                            "type": "Paladin",
                            "specs": [{"spec": "Retribution", "count": 1}],
                            "combatantInfo": {
                                "talentTree": [
                                    {"id": 103324, "nodeID": 82244, "rank": 1},
                                    {"id": 109839, "nodeID": 88206, "rank": 1},
                                    {"id": 117176, "nodeID": 94585, "rank": 1},
                                ]
                            },
                        }
                    ],
                }
            },
        }


def _patch_simc_describe_pipeline(
    monkeypatch,
    *,
    transport_form: str,
    transport_status: str,
) -> None:
    # The doubles are simc's own dataclasses, not look-alike objects: a field simc adds or renames
    # then fails these tests instead of silently producing a half-built describe payload.
    def fake_loader(_paths, **kwargs):  # noqa: ANN001
        # The wrapper hands simc the packet in memory, never a --build-packet file.
        packet = kwargs["packet"]
        assert isinstance(packet, PacketInput) and kwargs["build_packet"] is None
        split = transport_form == "simc_split_talents"
        return (
            BuildSpec(
                actor_class="druid",
                spec="balance",
                class_talents="103324:1" if split else None,
                spec_talents="109839:1" if split else None,
                hero_talents="117176:1" if split else None,
                source_kind=transport_form,
                source_notes=["talent transport packet"],
                transport_form=transport_form,
                transport_status=transport_status,
                transport_source=packet.path,
            ),
            BuildIdentity(
                actor_class="druid",
                spec="balance",
                confidence="high",
                source=transport_form,
                candidate_count=1,
                candidates=[("druid", "balance")],
                source_notes=["talent transport packet"],
            ),
        )

    monkeypatch.setattr("simc_cli.main._load_identified_build_spec", fake_loader)

    resolution = BuildResolution(
        actor_class="druid",
        spec="balance",
        enabled_talents={"moonkin_form"},
        talents_by_tree={"class": [], "spec": [], "hero": [], "selection": []},
        source_kind=transport_form,
        generated_profile_text=None,
        source_notes=["talent transport packet"],
    )

    def fake_prune_context(_paths, _build_spec, option_values, targets):  # noqa: ANN001
        assert isinstance(option_values["packet"], PacketInput)
        context = PruneContext(enabled_talents={"moonkin_form"}, disabled_talents=set(), targets=targets, talent_sources={})
        return context, resolution

    monkeypatch.setattr("simc_cli.main._prune_context", fake_prune_context)
    monkeypatch.setattr(
        "simc_cli.main._describe_target_payload",
        lambda _resolved, context, *, start_list, priority_limit, inactive_limit: {
            "targets": context.targets,
            "focus_list": "default",
            "focus_path": ["default"],
            "focus_resolution": "direct",
            "active_priority": [],
            "inactive_priority": [],
            "active_action_names": ["moonfire"],
            "inactive_action_names": [],
            "talent_tree": {
                "class": {"selected": [], "skipped": []},
                "spec": {"selected": [], "skipped": []},
                "hero": {"selected": [], "skipped": []},
            },
            "inactive_talents": [],
            "active_talents": [],
            "explained_intent": {"setup": [], "helpers": [], "burst": [], "priorities": []},
            "runtime_sensitive": [],
        },
    )


def _comparison_payload(
    *,
    provider: str,
    slug: str,
    page_url: str,
    page_title: str,
    analysis_tags: list[str],
    build_code: str | None = None,
) -> dict[str, object]:
    build_items: list[dict[str, object]] = []
    if build_code is not None:
        build_items.append(
            {
                "kind": "build_reference",
                "reference_type": "wowhead_talent_calc_url",
                "url": f"https://www.wowhead.com/talent-calc/monk/mistweaver/{build_code}",
                "label": "Raid Build",
                "build_code": build_code,
                "build_identity": {
                    "kind": "build_identity",
                    "status": "inferred",
                    "class_spec_identity": {"identity": {"actor_class": "monk", "spec": "mistweaver"}},
                },
                "source_urls": [page_url],
            }
        )
    return {
        "guide": {
            "slug": slug,
            "page_url": page_url,
            "section_slug": "overview",
            "section_title": "Overview",
            "page_count": 1,
        },
        "page": {
            "title": page_title,
            "description": page_title,
            "canonical_url": page_url,
        },
        "navigation": {"count": 1, "items": [{"title": "Overview", "url": page_url, "section_slug": "overview", "active": True, "ordinal": 1}]},
        "pages": [
            {
                "guide": {"slug": slug, "page_url": page_url, "section_slug": "overview", "section_title": "Overview"},
                "page": {"title": page_title, "description": page_title, "canonical_url": page_url},
                "article": {
                    "html": "<h2>Overview</h2><p>Guide copy</p>",
                    "text": "Overview Guide copy",
                    "headings": [{"title": "Overview", "level": 2, "ordinal": 1}],
                    "sections": [{"title": "Overview", "level": 2, "ordinal": 1, "text": "Guide copy", "html": "<p>Guide copy</p>"}],
                },
            }
        ],
        "linked_entities": {"count": 0, "items": []},
        "build_references": {"count": len(build_items), "items": build_items},
        "analysis_surfaces": {
            "count": 1,
            "items": [
                {
                    "kind": "guide_analysis_surface",
                    "surface_tags": analysis_tags,
                    "confidence": "high",
                    "source_kind": "section_heading",
                    "provider": provider,
                    "content_family": None,
                    "page_url": page_url,
                    "section_slug": "overview",
                    "section_title": "Overview",
                    "page_title": page_title,
                    "text_preview": "Guide copy",
                    "match_reasons": ["keyword:overview"],
                    "citation": {"page_url": page_url, "section_title": "Overview", "page_title": page_title},
                }
            ],
        },
    }


def test_method_doctor_reports_search_and_resolve_ready() -> None:
    result = runner.invoke(method_app, ["doctor"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["provider"] == "method"
    assert payload["data"]["status"] == "ready"
    assert payload["data"]["capabilities"]["search"] == "ready"
    assert payload["data"]["capabilities"]["resolve"] == "ready"


def test_warcraft_doctor_surfaces_a_providers_cache_config_error(monkeypatch) -> None:
    monkeypatch.setenv("CURSEFORGE_CACHE_BACKEND", "bogus")

    data = json.loads(runner.invoke(warcraft_app, ["doctor"]).stdout)["data"]

    providers = {row["provider"]: row for row in data["providers"]}
    assert providers["curseforge"]["status"] == "degraded"
    assert providers["curseforge"]["cache_error"]["code"] == "invalid_cache_config"
    assert (providers["wowhead"]["status"], providers["wowhead"]["cache_error"]) == ("ready", None)


def test_warcraft_doctor_cache_error_is_an_object_when_the_provider_doctor_fails(monkeypatch) -> None:
    monkeypatch.setenv("WOWHEAD_CACHE_BACKEND", "bogus")

    data = json.loads(runner.invoke(warcraft_app, ["doctor"]).stdout)["data"]

    wowhead = next(row for row in data["providers"] if row["provider"] == "wowhead")
    assert wowhead["status"] == "error"
    assert wowhead["cache_error"]["code"] == "invalid_cache_config"
    assert "WOWHEAD_CACHE_BACKEND" in wowhead["cache_error"]["message"]


def test_warcraft_doctor_wraps_a_redis_ping_failure_as_an_object(monkeypatch) -> None:
    monkeypatch.setattr(
        "warcraft_cli.providers._call_surface",
        lambda *_args, **_kwargs: (0, {"ok": True, "data": {"cache": {"error": "Connection refused."}}}),
    )

    row = provider_doctor("wowhead")

    assert row["status"] == "degraded"
    assert row["cache_error"] == {"code": "cache_unavailable", "message": "Connection refused."}


def test_warcraft_doctor_reports_ready_and_stubbed_providers() -> None:
    result = runner.invoke(warcraft_app, ["doctor"])
    assert result.exit_code == 0

    data = json.loads(result.stdout)["data"]
    assert data["wrapper"]["provider_count"] == 11
    providers = {row["provider"]: row for row in data["providers"]}
    assert providers["wowhead"]["status"] == "ready"
    assert providers["method"]["status"] == "ready"
    assert providers["icy-veins"]["status"] == "ready"
    assert providers["raiderio"]["status"] == "partial"
    assert providers["warcraftlogs"]["status"] == "partial"
    assert providers["warcraft-wiki"]["status"] == "ready"
    assert providers["simc"]["status"] == "partial"
    assert providers["wowhead"]["expansion_support"]["mode"] == "profiled"
    assert providers["wowhead"]["expansion_support"]["review_status"] == "reviewed"
    assert providers["method"]["expansion_support"]["mode"] == "fixed"
    assert providers["method"]["expansion_support"]["review_status"] == "reviewed"
    assert providers["warcraftlogs"]["expansion_support"]["mode"] == "profiled"
    assert providers["warcraftlogs"]["expansion_support"]["review_status"] == "reviewed"
    assert providers["warcraftlogs"]["expansion_support"]["supported_expansions"] == [
        "retail",
        "classic",
        "tbc",
        "wotlk",
        "cata",
        "mop-classic",
        "fresh",
    ]
    assert providers["warcraft-wiki"]["expansion_support"]["mode"] == "fixed"
    assert providers["warcraft-wiki"]["expansion_support"]["review_status"] == "reviewed"
    assert providers["warcraft-wiki"]["expansion_support"]["supported_expansions"] == ["retail"]
    assert providers["method"]["details"]["data"]["capabilities"]["guide"] == "ready"
    assert providers["icy-veins"]["details"]["data"]["capabilities"]["guide"] == "ready"
    assert providers["raiderio"]["details"]["data"]["capabilities"]["search"] == "ready"
    assert providers["warcraftlogs"]["details"]["data"]["capabilities"]["search"] == "ready_explicit_report_only"
    assert providers["warcraft-wiki"]["details"]["data"]["capabilities"]["article"] == "ready"
    # simc's binary-backed capabilities track the binary, not a constant: doctor says "ready" only
    # when the SimulationCraft build is usable, and "unavailable" otherwise (no checkout on CI).
    simc_details = providers["simc"]["details"]["data"]
    simc_binary = simc_details["dependencies"]["simc_binary"]
    binary_backed_state = "ready" if simc_binary["available"] else "unavailable"
    for capability in ("decode_build", "validate_talent_transport"):
        assert capability in simc_binary["required_by"]
        assert simc_details["capabilities"][capability] == binary_backed_state
    # A capability that needs neither the binary nor ripgrep stays ready either way.
    assert simc_details["capabilities"]["repo"] == "ready"
    assert providers["warcraftlogs"]["auth"]["required"] is True
    assert providers["simc"]["wrapper_surfaces"]["search"]["ready"] is False
    assert providers["simc"]["wrapper_surfaces"]["search"]["status"] == "coming_soon"
    assert providers["blizzard-api"]["status"] == "partial"
    assert providers["blizzard-api"]["auth"]["required"] is True
    assert providers["blizzard-api"]["auth"]["flow"] == "oauth_client_credentials"
    assert providers["blizzard-api"]["expansion_support"]["mode"] == "none"
    assert providers["blizzard-api"]["wrapper_surfaces"]["search"]["status"] == "coming_soon"
    assert providers["blizzard-api"]["details"]["data"]["capabilities"]["doctor"] == "ready"
    assert providers["curseforge"]["status"] == "partial"
    assert providers["curseforge"]["auth"]["required"] is True
    assert providers["curseforge"]["auth"]["flow"] == "api_key"
    assert providers["curseforge"]["expansion_support"]["mode"] == "none"
    assert providers["curseforge"]["wrapper_surfaces"]["search"]["status"] == "coming_soon"
    assert providers["curseforge"]["details"]["data"]["capabilities"]["addon"] == "requires_api_key"
    assert providers["lorrgs"]["status"] == "partial"
    assert providers["lorrgs"]["auth"]["required"] is False
    assert providers["lorrgs"]["expansion_support"]["mode"] == "fixed"
    assert providers["lorrgs"]["expansion_support"]["supported_expansions"] == ["retail"]
    assert providers["lorrgs"]["wrapper_surfaces"]["search"]["status"] == "ready"
    assert providers["lorrgs"]["wrapper_surfaces"]["resolve"]["status"] == "ready"
    assert providers["lorrgs"]["details"]["data"]["capabilities"]["spec_ranking"] == "ready"
    assert providers["lorrgs"]["details"]["data"]["capabilities"]["report_overview"] == "ready"
    assert providers["lorrgs"]["details"]["data"]["capabilities"]["current_season"] == "ready"


def _provider_doctor_capabilities(registration) -> dict[str, str]:  # noqa: ANN001
    """Invoke a provider CLI's own doctor and return its reported capabilities map."""
    live_flags = ["--no-live"] if registration.doctor_options.get("live") is False else []
    result = runner.invoke(registration.app, ["doctor", *live_flags])
    assert result.exit_code == 0, f"{registration.name} doctor exited {result.exit_code}"
    payload = json.loads(result.stdout)
    capabilities = payload["data"].get("capabilities")
    assert isinstance(capabilities, dict), f"{registration.name} doctor emitted no capabilities map"
    return capabilities


def test_wrapper_capabilities_match_each_cli_doctor_for_search_and_resolve() -> None:
    # Registry-vs-CLI parity: the wrapper must never advertise a search/resolve
    # surface more capable than the provider CLI itself reports. (The `doctor`
    # surface is intentionally excluded: only warcraftlogs/simc/blizzard-api emit
    # a `doctor` capability key, so it is not a parity surface.)
    overstated = {"coming_soon", "not_supported", "ready_explicit_report_only"}
    assert len(PROVIDERS) == 11
    for registration in PROVIDERS:
        capabilities = _provider_doctor_capabilities(registration)
        for surface in ("search", "resolve"):
            wrapper_status = registration.wrapper_capabilities[surface]
            cli_status = capabilities.get(surface)
            assert cli_status is not None, f"{registration.name} doctor missing `{surface}` capability"
            assert wrapper_status == cli_status, (
                f"{registration.name} wrapper advertises {surface}={wrapper_status!r} "
                f"but the CLI reports {cli_status!r}"
            )
            if wrapper_status == "ready":
                assert cli_status not in overstated, (
                    f"{registration.name} overstates {surface} as ready while the CLI reports {cli_status!r}"
                )


def test_coming_soon_surfaces_emit_structured_stub_not_click_error() -> None:
    # If a provider's own doctor advertises search/resolve as coming_soon, that command must exist
    # and emit a structured coming_soon envelope (exit 0), never Click's "No such command" (exit 2).
    # Regression originally caught on curseforge + blizzard-api search/resolve; simc is the precedent.
    for registration in PROVIDERS:
        capabilities = _provider_doctor_capabilities(registration)
        for surface in ("search", "resolve"):
            if capabilities.get(surface) != "coming_soon":
                continue
            result = runner.invoke(registration.app, [surface, "probe-query"])
            assert result.exit_code == 0, (
                f"{registration.name} advertises {surface}=coming_soon but `{surface}` exited "
                f"{result.exit_code} (Click 'No such command'?): {result.stdout or result.stderr}"
            )
            payload = json.loads(result.stdout)
            assert payload["data"].get("coming_soon") is True, (
                f"{registration.name} {surface} did not emit a structured coming_soon stub"
            )


def _stub_non_warcraftlogs_fanout(monkeypatch) -> None:  # noqa: ANN001
    """Run the real warcraftlogs search/resolve; return empty payloads for everyone else."""
    import warcraft_cli.main as wrapper_main

    real_search = wrapper_main.provider_search
    real_resolve = wrapper_main.provider_resolve

    def only_wcl_search(provider: str, query: str, *, limit: int = 5, expansion=None):  # noqa: ANN001, ANN202
        if provider == "warcraftlogs":
            return real_search(provider, query, limit=limit, expansion=expansion)
        return {"provider": provider, "exit_code": 0, "payload": _envelope({"provider": provider, "count": 0, "results": []})}

    def only_wcl_resolve(provider: str, query: str, *, limit: int = 5, expansion=None):  # noqa: ANN001, ANN202
        if provider == "warcraftlogs":
            return real_resolve(provider, query, limit=limit, expansion=expansion)
        return {"provider": provider, "exit_code": 0, "payload": _envelope({"provider": provider, "resolved": False, "match": None})}

    monkeypatch.setattr("warcraft_cli.main.provider_search", only_wcl_search)
    monkeypatch.setattr("warcraft_cli.main.provider_resolve", only_wcl_resolve)


def test_warcraft_search_keeps_warcraftlogs_as_explicit_report_only_discovery_hint(monkeypatch) -> None:
    _stub_non_warcraftlogs_fanout(monkeypatch)
    # "Liquid" is a guild name, not a report code (6 characters and no digit),
    # so warcraftlogs must return its structured discovery hint, not a fabricated match.
    result = runner.invoke(warcraft_app, ["search", "Liquid"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)

    assert get_provider("warcraftlogs").wrapper_capabilities["search"] == "ready_explicit_report_only"
    assert "warcraftlogs" in payload["data"]["included_providers"]
    assert "warcraftlogs" not in {row["provider"] for row in payload["data"]["excluded_providers"]}

    wcl = {row["provider"]: row for row in payload["data"]["providers"]}["warcraftlogs"]["payload"]["data"]
    assert wcl["count"] == 0
    assert "explicit report URL or a bare report code" in wcl["message"]
    assert wcl["supported_inputs"]
    assert wcl["suggested_commands"]
    # Never surface a fabricated resolved match for a non-report query.
    assert all(row["provider"] != "warcraftlogs" for row in payload["data"]["results"])


def test_warcraft_resolve_never_fabricates_a_warcraftlogs_match_for_non_report_query(monkeypatch) -> None:
    _stub_non_warcraftlogs_fanout(monkeypatch)
    result = runner.invoke(warcraft_app, ["resolve", "Liquid"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)

    assert "warcraftlogs" in payload["data"]["included_providers"]
    wcl = {row["provider"]: row for row in payload["data"]["providers"]}["warcraftlogs"]["payload"]["data"]
    assert wcl["resolved"] is False
    assert wcl["match"] is None
    assert wcl["supported_inputs"]
    assert wcl["suggested_commands"]
    # The wrapper must not resolve to warcraftlogs off a non-report query.
    assert payload["data"]["selected_provider"] != "warcraftlogs"
    assert payload["data"]["resolved"] is False


def test_warcraft_resolve_hands_over_a_bare_report_code_even_when_every_searcher_failed(monkeypatch) -> None:
    """A bare code is a medium match, not a resolve: it is still an answer, never "No provider answered"."""
    import warcraft_cli.main as wrapper_main

    real_resolve = wrapper_main.provider_resolve

    def outage_except_wcl(provider: str, query: str, **kwargs: Any) -> dict[str, Any]:
        if provider == "warcraftlogs":
            return real_resolve(provider, query, **kwargs)
        envelope = {"ok": False, "provider": provider, "command": "resolve", "kind": "error", "data": {},
                    "error": {"code": "network_error", "message": f"{provider} down"}}
        return {"provider": provider, "exit_code": 5, "payload": envelope}

    monkeypatch.setattr("warcraft_cli.main.provider_resolve", outage_except_wcl)

    result = runner.invoke(warcraft_app, ["resolve", "JVFTxcKCqrvpaAzD", "--brief"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)["data"]
    assert (data["resolved"], data["next_command"], data["answered_provider_count"]) == (False, None, 1)
    best = data["best_unresolved_candidate"]
    assert (best["provider"], best["confidence"]) == ("warcraftlogs", "medium")
    assert best["unresolved_reason"] == "provider_did_not_resolve"
    assert best["follow_up_command"].startswith("warcraftlogs report JVFTxcKCqrvpaAzD")


def test_warcraft_doctor_reports_expansion_filtering_state() -> None:
    result = runner.invoke(warcraft_app, ["--expansion", "wotlk", "doctor"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["data"]["wrapper"]["requested_expansion"] == "wotlk"
    assert payload["data"]["wrapper"]["expansion_filter_active"] is True
    assert payload["data"]["included_providers"] == ["wowhead", "warcraftlogs"]
    providers = {row["provider"]: row for row in payload["data"]["providers"]}
    assert providers["warcraftlogs"]["details"]["data"]["site_profile"]["key"] == "classic"
    assert {row["provider"] for row in payload["data"]["excluded_providers"]} == {
        "method",
        "icy-veins",
        "raiderio",
        "warcraft-wiki",
        "simc",
        "raidbots",
        "blizzard-api",
        "curseforge",
        "lorrgs",
    }


def test_warcraft_passthrough_advisory_is_selectable_with_strict_fields() -> None:
    """Output shaping runs after the advisory is attached, so wrapper-added keys can be projected."""
    result = runner.invoke(
        warcraft_app,
        ["--expansion", "wotlk", "--fields", "data.expansion_advisory", "--fields-strict", "blizzard", "doctor"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["data"]["expansion_advisory"]["requested_expansion"] == "wotlk"
    assert "capabilities" not in payload["data"]


def test_warcraft_passthrough_relaxes_none_expansion_provider_with_advisory() -> None:
    # blizzard-api is expansion_mode=none: a wrapper --expansion has no semantics to honor,
    # so the command is passed through unchanged with an advisory note (relax-to-passthrough),
    # not rejected with exit 1.
    result = runner.invoke(warcraft_app, ["--expansion", "wotlk", "blizzard", "doctor"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    # provider payload preserved verbatim, and the advisory added inside it rather than beside it
    assert payload["ok"] is True
    assert set(payload) == REQUIRED_KEYS
    assert payload["provider"] == "blizzard-api"
    assert payload["data"]["capabilities"]["doctor"] == "ready"
    # additive advisory note, inside the provider's own data
    assert payload["data"]["expansion_advisory"]["expansion_filter"] == "passthrough_no_expansion_semantics"
    assert payload["data"]["expansion_advisory"]["requested_expansion"] == "wotlk"
    assert payload["data"]["expansion_advisory"]["provider_expansion_mode"] == "none"


def test_warcraft_passthrough_maps_warcraftlogs_expansion_to_site_profile() -> None:
    result = runner.invoke(warcraft_app, ["--expansion", "wotlk", "warcraftlogs", "auth", "client"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["provider"] == "warcraftlogs"
    assert payload["data"]["client"]["site_profile"] == "classic"
    assert payload["data"]["client"]["client_api_url"] == "https://classic.warcraftlogs.com/api/v2/client"


def test_warcraft_passthrough_maps_fresh_to_warcraftlogs_fresh_site_profile() -> None:
    result = runner.invoke(warcraft_app, ["--expansion", "fresh", "warcraftlogs", "auth", "client"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["provider"] == "warcraftlogs"
    assert payload["data"]["client"]["site_profile"] == "fresh"
    assert payload["data"]["client"]["client_api_url"] == "https://fresh.warcraftlogs.com/api/v2/client"


def test_warcraft_passthrough_rejects_unsupported_warcraftlogs_expansion() -> None:
    result = runner.invoke(warcraft_app, ["--expansion", "ptr", "warcraftlogs", "auth", "client"])
    assert result.exit_code == 2
    payload = json.loads(result.stderr)
    assert payload["kind"] == "error"
    assert payload["query"] == {"provider": "warcraftlogs", "expansion": "ptr"}
    assert payload["error"]["code"] == "unsupported_provider_expansion"
    assert payload["error"]["details"]["provider"] == "warcraftlogs"
    assert payload["error"]["details"]["requested_expansion"] == "ptr"


def test_warcraft_passthrough_rejects_duplicate_warcraftlogs_site_selector() -> None:
    result = runner.invoke(warcraft_app, ["--expansion", "wotlk", "warcraftlogs", "--site", "retail", "auth", "client"])
    assert result.exit_code == 2
    payload = json.loads(result.stderr)
    assert payload["query"] == {"provider": "warcraftlogs", "expansion": "wotlk"}
    assert payload["error"]["code"] == "duplicate_expansion_argument"
    assert payload["error"]["details"]["provider"] == "warcraftlogs"


def test_warcraft_passthrough_rejects_duplicate_warcraftlogs_site_selector_equals_form() -> None:
    result = runner.invoke(warcraft_app, ["--expansion", "wotlk", "warcraftlogs", "--site=retail", "auth", "client"])
    assert result.exit_code == 2
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "duplicate_expansion_argument"
    assert payload["error"]["details"]["provider"] == "warcraftlogs"


def test_warcraft_passthrough_usage_error_is_the_provider_envelope() -> None:
    # A missing argument is a Click usage error inside the provider app, and the provider's own binary
    # labels it with the provider and subcommand, so passthrough must too.
    result = runner.invoke(warcraft_app, ["raiderio", "search"])
    assert result.exit_code == 2
    payload = json.loads(result.stderr)
    assert (payload["provider"], payload["command"]) == ("raiderio", "search")
    assert payload["error"]["code"] == "invalid_argument"


def test_warcraft_passthrough_rejects_fresh_for_wowhead() -> None:
    result = runner.invoke(warcraft_app, ["--expansion", "fresh", "wowhead", "search", "thunderfury"])
    assert result.exit_code == 2
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "unsupported_provider_expansion"
    assert payload["error"]["details"]["provider"] == "wowhead"


def test_warcraft_passthrough_relaxes_none_expansion_simc() -> None:
    # Use `simc doctor` (fully offline, deterministic) rather than `simc version`, which
    # depends on a built SimC binary that CI does not provide.
    result = runner.invoke(warcraft_app, ["--expansion", "wotlk", "simc", "doctor"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["provider"] == "simc"
    assert payload["data"]["capabilities"]["doctor"] == "ready"
    assert payload["data"]["expansion_advisory"]["expansion_filter"] == "passthrough_no_expansion_semantics"
    assert payload["data"]["expansion_advisory"]["provider_expansion_mode"] == "none"


def test_warcraft_passthrough_rejects_fixed_provider_expansion_mismatch() -> None:
    # Fixed/profiled providers asked for an unsupported expansion are a genuine mismatch
    # and still hard-error (only none-expansion providers relax to passthrough).
    result = runner.invoke(warcraft_app, ["--expansion", "wotlk", "method", "search", "foo"])
    assert result.exit_code == 2
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "unsupported_provider_expansion"
    assert payload["error"]["details"]["provider"] == "method"


def test_warcraft_passthrough_advisory_rides_on_a_provider_failure(tmp_path: Path) -> None:
    """A failing none-expansion provider still reports the ignored --expansion, under error.details."""
    missing = tmp_path / "missing-packet.json"
    result = runner.invoke(
        warcraft_app, ["--expansion", "wotlk", "simc", "validate-talent-transport", "--build-packet", str(missing)]
    )
    assert result.exit_code != 0
    payload = json.loads(result.stderr)
    assert payload["ok"] is False
    assert payload["provider"] == "simc"
    assert payload["data"] == {}
    advisory = payload["error"]["details"]["expansion_advisory"]
    assert advisory["expansion_filter"] == "passthrough_no_expansion_semantics"
    assert advisory["requested_expansion"] == "wotlk"


@pytest.mark.parametrize(
    "args",
    [
        ["guide-compare", "only-one-bundle"],
        ["guide-compare-query", "mistweaver monk", "--provider", "raiderio"],
    ],
)
def test_wrapper_invalid_argument_exits_as_a_usage_error(args: list[str]) -> None:
    result = runner.invoke(warcraft_app, args)

    assert result.exit_code == 2, result.output
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_argument"
    assert payload["kind"] == "error"
    assert payload["query"], "a failure echoes the parsed input"


@pytest.mark.parametrize(
    "args",
    [
        ["guide-compare-query", "mistweaver monk", "--provider", "method"],
        # Method and Icy Veins are retail-only, which leaves Wowhead alone.
        ["--expansion", "classic", "guide-compare-query", "mistweaver monk"],
    ],
)
def test_guide_compare_query_that_cannot_reach_two_guides_fails_before_any_provider_call(
    monkeypatch, tmp_path: Path, args: list[str]
) -> None:
    def no_call(*call_args: object, **kwargs: object) -> dict[str, object]:
        raise AssertionError("a run that cannot reach two guides called a provider")

    for seam in ("provider_resolve", "provider_search", "provider_invoke"):
        monkeypatch.setattr(f"warcraft_cli.main.{seam}", no_call)

    result = runner.invoke(warcraft_app, [*args, "--out-root", str(tmp_path / "o")])

    assert result.exit_code == 2, result.output
    assert json.loads(result.stderr)["error"]["code"] == "invalid_argument"


def test_warcraft_passthrough_without_expansion_has_no_advisory() -> None:
    result = runner.invoke(warcraft_app, ["blizzard", "doctor"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert "expansion_advisory" not in payload["data"]


def test_warcraft_doctor_reports_retail_filter_state() -> None:
    result = runner.invoke(warcraft_app, ["--expansion", "retail", "doctor"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["data"]["wrapper"]["requested_expansion"] == "retail"
    assert payload["data"]["wrapper"]["expansion_filter_active"] is True
    assert set(payload["data"]["included_providers"]) == {
        "wowhead",
        "method",
        "icy-veins",
        "raiderio",
        "warcraftlogs",
        "warcraft-wiki",
        "raidbots",
        "lorrgs",
    }
    assert {row["provider"] for row in payload["data"]["excluded_providers"]} == {"simc", "blizzard-api", "curseforge"}


def test_warcraft_doctor_handles_ptr_filter_without_warcraftlogs_site_translation() -> None:
    result = runner.invoke(warcraft_app, ["--expansion", "ptr", "doctor"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    providers = {row["provider"]: row for row in payload["data"]["providers"]}
    assert providers["warcraftlogs"]["expansion_support"]["allowed"] is False
    assert providers["warcraftlogs"]["expansion_support"]["exclusion_reason"] == "provider_does_not_support_requested_expansion"
    assert providers["warcraftlogs"]["details"]["data"]["site_profile"]["key"] == "retail"
    assert "warcraftlogs" not in payload["data"]["included_providers"]


def test_warcraft_doctor_reports_worktree_runtime(monkeypatch, tmp_path) -> None:
    # The worktree runtime isolates only the roots no XDG override claims.
    monkeypatch.delenv("XDG_DATA_HOME")
    monkeypatch.delenv("XDG_CACHE_HOME")
    monkeypatch.setenv("WARCRAFT_WORKTREE_ROOT", str(tmp_path / "repo"))

    result = runner.invoke(warcraft_app, ["doctor"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["data"]["paths"]["data_root"] == str((tmp_path / "repo" / ".warcraft" / "runtime" / "data").resolve())
    assert payload["data"]["paths"]["cache_root"] == str((tmp_path / "repo" / ".warcraft" / "runtime" / "cache").resolve())
    assert payload["data"]["paths"]["worktree_runtime"] == {
        "active": True,
        "worktree_root": str((tmp_path / "repo").resolve()),
        "runtime_root": str((tmp_path / "repo" / ".warcraft" / "runtime").resolve()),
        "isolated_roots": ["data", "cache"],
        "shared_roots": ["config", "state"],
    }


def test_warcraft_doctor_reports_xdg_overrides_in_worktree_runtime(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("WARCRAFT_WORKTREE_ROOT", str(tmp_path / "repo"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    result = runner.invoke(warcraft_app, ["doctor"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["data"]["paths"]["data_root"] == str((tmp_path / "data" / "warcraft").resolve())
    assert payload["data"]["paths"]["cache_root"] == str((tmp_path / "cache" / "warcraft").resolve())
    assert payload["data"]["paths"]["worktree_runtime"] == {
        "active": True,
        "worktree_root": str((tmp_path / "repo").resolve()),
        "runtime_root": str((tmp_path / "repo" / ".warcraft" / "runtime").resolve()),
        "isolated_roots": [],
        "shared_roots": ["data", "cache", "config", "state"],
    }


def test_warcraft_doctor_reports_explicit_runtime_dir_without_worktree_root(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("XDG_DATA_HOME")
    monkeypatch.delenv("XDG_CACHE_HOME")
    monkeypatch.setenv("WARCRAFT_WORKTREE_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setattr("warcraft_core.paths.worktree_root", lambda: None)

    result = runner.invoke(warcraft_app, ["doctor"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["data"]["paths"]["data_root"] == str((tmp_path / "runtime" / "data").resolve())
    assert payload["data"]["paths"]["cache_root"] == str((tmp_path / "runtime" / "cache").resolve())
    assert payload["data"]["paths"]["worktree_runtime"] == {
        "active": True,
        "worktree_root": None,
        "runtime_root": str((tmp_path / "runtime").resolve()),
        "isolated_roots": ["data", "cache"],
        "shared_roots": ["config", "state"],
    }


def test_warcraft_search_brief_rows_keep_each_providers_follow_up_command(monkeypatch) -> None:
    """`--brief` drops the provider payloads, never the command that fetches a row."""
    monkeypatch.setattr(
        "method_cli.main.MethodClient.sitemap_guides",
        lambda self: [{"slug": "thunderfury-guide", "name": "Thunderfury Guide", "url": "https://www.method.gg/guides/thunderfury-guide"}],
    )
    monkeypatch.setattr("icy_veins_cli.main.IcyVeinsClient.sitemap_guides", lambda self: [])
    monkeypatch.setattr("raiderio_cli.client.RaiderIOClient.search", lambda self, *, term, kind=None: {"matches": []})
    monkeypatch.setattr("warcraft_wiki_cli.main.WarcraftWikiClient.search_articles", lambda self, query, *, limit: (0, []))
    monkeypatch.setattr(
        "wowhead_cli.main.WowheadClient.search_suggestions",
        lambda self, query: {"search": query, "results": [{"type": 3, "id": 19019, "name": "Thunderfury", "typeName": "Item"}]},
    )

    result = runner.invoke(warcraft_app, ["search", "thunderfury", "--limit", "3", "--brief"])

    assert result.exit_code == 0, result.output
    rows = {row["provider"]: row for row in json.loads(result.stdout)["data"]["results"]}
    assert rows["wowhead"]["follow_up_command"] == "wowhead entity item 19019"
    assert rows["method"]["follow_up_command"] == "method guide thunderfury-guide"


def test_warcraft_search_and_resolve_keep_provider_warnings_under_brief(monkeypatch) -> None:
    """`--brief` empties `providers`, so a provider's provenance warning (Icy Veins' stale sitemap) is lifted out."""
    stale_row = {
        "slug": "thunderfury-guide",
        "name": "Thunderfury Guide",
        "url": "https://www.icy-veins.com/wow/thunderfury-guide",
        "content_family": None,
        "sitemap_lastmod": "2020-01-01",
    }
    monkeypatch.setattr("icy_veins_cli.main.IcyVeinsClient.sitemap_guides", lambda self: [stale_row])
    monkeypatch.setattr("icy_veins_cli.main.IcyVeinsClient.site_menu_guides", lambda self: [])
    monkeypatch.setattr("method_cli.main.MethodClient.sitemap_guides", lambda self: [])
    monkeypatch.setattr("raiderio_cli.client.RaiderIOClient.search", lambda self, *, term, kind=None: {"matches": []})
    monkeypatch.setattr("warcraft_wiki_cli.main.WarcraftWikiClient.search_articles", lambda self, query, *, limit: (0, []))
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.search_suggestions", lambda self, query: {"search": query, "results": []})

    for command in ("search", "resolve"):
        result = runner.invoke(warcraft_app, [command, "thunderfury", "--brief"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout)["data"]
        assert data["providers"] == []
        warnings = {(row["provider"], row["key"]) for row in data["provider_warnings"]}
        assert warnings == {("icy-veins", "sitemap_warning")}, (command, data["provider_warnings"])


def test_warcraft_search_fans_out_across_providers(monkeypatch) -> None:
    def fake_search(self, query: str):  # noqa: ANN001
        return {
            "search": query,
            "results": [
                {"type": 3, "id": 19019, "name": "Thunderfury", "typeName": "Item", "popularity": 10},
            ],
        }

    monkeypatch.setattr(
        "method_cli.main.MethodClient.sitemap_guides",
        lambda self: [{"slug": "mistweaver-monk", "name": "Mistweaver Monk", "url": "https://www.method.gg/guides/mistweaver-monk"}],
    )
    monkeypatch.setattr("icy_veins_cli.main.IcyVeinsClient.sitemap_guides", lambda self: [])
    monkeypatch.setattr("raiderio_cli.client.RaiderIOClient.search", lambda self, *, term, kind=None: {"matches": []})
    monkeypatch.setattr("warcraft_wiki_cli.main.WarcraftWikiClient.search_articles", lambda self, query, *, limit: (0, []))
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.search_suggestions", fake_search)
    result = runner.invoke(warcraft_app, ["search", "thunderfury", "--limit", "3"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    _assert_wrapper_success_envelope(payload, command="search")
    assert payload["data"]["provider_count"] == 11
    assert payload["data"]["count"] == 1
    assert payload["data"]["results"][0]["provider"] == "wowhead"
    providers = {row["provider"]: row for row in payload["data"]["providers"]}
    assert providers["method"]["payload"]["data"]["count"] == 0
    assert providers["icy-veins"]["payload"]["data"]["count"] == 0
    assert providers["raiderio"]["payload"]["data"]["count"] == 0
    assert providers["warcraftlogs"]["payload"]["data"]["count"] == 0
    assert "explicit report URL or a bare report code" in providers["warcraftlogs"]["payload"]["data"]["message"]
    assert providers["warcraft-wiki"]["payload"]["data"]["count"] == 0
    assert "simc" not in providers
    assert "raidbots" not in providers
    excluded = {row["provider"]: row for row in payload["data"]["excluded_providers"]}
    assert excluded["simc"]["reason"] == "provider_surface_not_ready"
    assert excluded["simc"]["surface_support"]["status"] == "coming_soon"
    assert excluded["raidbots"]["surface_support"]["status"] == "not_supported"
    assert "blizzard-api" not in providers
    assert excluded["blizzard-api"]["surface_support"]["status"] == "coming_soon"
    assert "curseforge" not in providers
    assert excluded["curseforge"]["surface_support"]["status"] == "coming_soon"
    assert providers["lorrgs"]["payload"]["data"]["count"] == 0
    assert providers["wowhead"]["payload"]["data"]["results"][0]["name"] == "Thunderfury"


def test_warcraft_guide_compare_returns_cross_provider_bundle_packet(tmp_path: Path) -> None:
    method_dir = tmp_path / "method-guide"
    icy_dir = tmp_path / "icy-guide"
    write_article_bundle(
        _comparison_payload(
            provider="method",
            slug="mistweaver-monk",
            page_url="https://www.method.gg/guides/mistweaver-monk/talents",
            page_title="Method Talents",
            analysis_tags=["builds_talents", "talent_recommendations"],
            build_code="ABC123",
        ),
        provider="method",
        export_dir=method_dir,
    )
    write_article_bundle(
        _comparison_payload(
            provider="icy-veins",
            slug="mistweaver-monk-pve-healing-guide",
            page_url="https://www.icy-veins.com/wow/mistweaver-monk-pve-healing-guide",
            page_title="Icy Overview",
            analysis_tags=["overview"],
            build_code="ABC123",
        ),
        provider="icy-veins",
        export_dir=icy_dir,
    )

    result = runner.invoke(warcraft_app, ["guide-compare", str(method_dir), str(icy_dir)])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    _assert_wrapper_success_envelope(payload, command="guide-compare")
    assert payload["provider"] == "warcraft"
    assert payload["kind"] == "guide_bundle_comparison"
    assert payload["data"]["compared_bundle_count"] == 2
    assert payload["data"]["comparison_scope"] == ["section_evidence", "analysis_surfaces", "build_references"]
    assert payload["data"]["section_evidence"]["matching_rule"] == "exact_normalized_section_title"
    assert payload["data"]["section_evidence"]["shared"] == ["overview"]
    assert payload["data"]["analysis_surface_tags"]["count"] == 3
    assert payload["data"]["build_references"]["count"] == 1
    assert payload["data"]["build_references"]["shared"] == [
        "monk::mistweaver::ABC123::https://www.wowhead.com/talent-calc/monk/mistweaver/ABC123"
    ]
    # AUR-386: additive freshness + scope/evidence metadata; existing keys preserved.
    assert payload["data"]["citations"]["bundle_paths"]  # preserved
    assert payload["data"]["freshness"]["status"] == "fresh"
    assert payload["data"]["freshness"]["bundle_count"] == 2
    assert payload["data"]["freshness"]["fresh_count"] == 2
    evidence = payload["data"]["comparison_evidence"]
    assert evidence["compared_bundle_count"] == 2
    assert evidence["providers"] == ["method", "icy-veins"]
    assert evidence["matching_rules"]["section_evidence"] == "exact_normalized_section_title"
    assert evidence["freshness"]["status"] == "fresh"
    assert len(evidence["bundle_freshness"]) == 2
    assert all(row["freshness"]["status"] == "fresh" for row in evidence["bundle_freshness"])
    assert evidence["subject_agreement"] == "agree"


def test_warcraft_guide_compare_flags_guides_about_different_specs(tmp_path: Path) -> None:
    """A Frost Mage guide and a Mistweaver Monk guide share section titles, but they are not one subject."""
    for provider, slug, build_code in (("method", "frost-mage", "ABC123"), ("icy-veins", "mistweaver-monk-pve-healing-guide", None)):
        write_article_bundle(
            _comparison_payload(provider=provider, slug=slug, page_url=f"https://example.test/{slug}", page_title=slug,
                                analysis_tags=["overview"], build_code=build_code),
            provider=provider,
            export_dir=tmp_path / provider,
        )

    result = runner.invoke(warcraft_app, ["guide-compare", str(tmp_path / "method"), str(tmp_path / "icy-veins")])

    assert result.exit_code == 0, result.output
    evidence = json.loads(result.stdout)["data"]["comparison_evidence"]
    assert [(row["class"], row["spec"]) for row in evidence["subjects"]] == [("mage", "frost"), ("monk", "mistweaver")]
    assert evidence["subject_agreement"] == "mixed"
    assert evidence["bundles_without_build_references"] == ["icy-veins"]


def test_guide_subject_tells_a_class_hub_from_a_spec_guide() -> None:
    from warcraft_cli.guide_compare import _guide_subject

    assert _guide_subject("Retribution Paladin DPS Guide - Midnight") == {"class": "paladin", "spec": "retribution"}
    assert _guide_subject("Paladin Guide") == {"class": "paladin", "spec": None}
    assert _guide_subject("Havoc Demon Hunter DPS Guide") == {"class": "demonhunter", "spec": "havoc"}
    assert _guide_subject("Thunderfury Transmogrification Guide") == {"class": None, "spec": None}


def test_guide_compare_query_asks_each_provider_for_guides_only(monkeypatch) -> None:
    """Wowhead's `frost mage` is also a spell; resolve and search must ask for its guide."""
    from warcraft_cli.guide_compare import _resolve_guide_compare_candidate
    from warcraft_cli.main import _provider_calls

    seen: list[dict[str, Any]] = []

    def lookup(provider: str, query: str, **kwargs: Any) -> dict[str, Any]:
        seen.append(kwargs)
        return {"provider": provider, "exit_code": 0, "payload": _envelope({"resolved": False, "match": None, "results": []})}

    monkeypatch.setattr("warcraft_cli.main.provider_resolve", lookup)
    monkeypatch.setattr("warcraft_cli.main.provider_search", lookup)

    _resolve_guide_compare_candidate("wowhead", "frost mage", expansion=None, calls=_provider_calls())

    assert [kwargs["entity_types"] for kwargs in seen] == [("guide",), ("guide",)]


def test_guide_compare_query_says_when_only_one_provider_was_selected() -> None:
    result = runner.invoke(warcraft_app, ["guide-compare-query", "frost mage guide", "--provider", "wowhead"])

    assert result.exit_code == 2, result.output
    message = json.loads(result.stderr)["error"]["message"]
    assert "only 1 (wowhead) was selected" in message and "serve expansion" not in message


def test_guide_builds_simc_limit_takes_builds_from_every_provider_in_turn() -> None:
    from warcraft_cli.guide_compare import _collect_build_reference_handoff_rows

    def bundle(provider: str, codes: list[str]) -> tuple[Path, dict[str, Any]]:
        refs = [{"url": f"https://example.test/{code}", "build_code": code} for code in codes]
        return Path(provider), {"manifest": {"provider": provider}, "build_references": refs}

    rows = _collect_build_reference_handoff_rows([bundle("icy-veins", ["x1", "x2"]), bundle("method", ["a1", "a2", "a3"])])

    # Sorted by URL alone, every method build came first and --limit 2 dropped icy-veins entirely.
    assert [row["reference"]["build_code"] for row in rows] == ["x1", "a1", "x2", "a2", "a3"]


def test_guide_compare_manifest_paths_follow_a_copied_root(tmp_path: Path) -> None:
    import shutil

    from warcraft_cli.guide_compare import _write_guide_compare_manifest, load_guide_build_source

    root = tmp_path / "original"
    for provider in ("method", "icy-veins"):
        write_article_bundle(
            _comparison_payload(provider=provider, slug="mistweaver-monk", page_url="https://example.test/mw",
                                page_title="mw", analysis_tags=["overview"]),
            provider=provider,
            export_dir=root / provider,
        )
    rows = [{"provider": provider, "status": "exported", "bundle_path": str(root / provider)} for provider in ("method", "icy-veins")]
    _write_guide_compare_manifest(root=root, query="mw", requested_expansion=None, max_age_hours=24, provider_results=rows)
    copy = tmp_path / "copy"
    shutil.copytree(root, copy)
    shutil.rmtree(root)

    _kind, bundle_inputs, _manifest = load_guide_build_source(copy)

    assert [path for path, _bundle in bundle_inputs] == [copy / "method", copy / "icy-veins"]


def test_warcraft_guide_compare_query_orchestrates_resolve_export_and_compare(
    monkeypatch,
    tmp_path: Path,
) -> None:
    def fake_provider_resolve(provider: str, query: str, *, limit: int = 5, expansion: str | None = None, entity_types: tuple[str, ...] = ()) -> dict[str, object]:
        assert query == "mistweaver monk guide"
        assert limit == 5
        assert expansion is None
        refs = {
            "method": ("mistweaver-monk", "Method Mistweaver Monk Guide"),
            "icy-veins": ("mistweaver-monk-pve-healing-guide", "Icy Veins Mistweaver Monk Guide"),
        }
        ref, name = refs[provider]
        return {
            "provider": provider,
            "exit_code": 0,
            "payload": _envelope({
                "resolved": True,
                "confidence": "high",
                "match": {
                    "id": ref,
                    "name": name,
                    "kind": "guide",
                    "url": f"https://example.test/{ref}",
                },
                "next_command": f"{provider} guide {ref}",
            }),
        }

    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        assert args[0] == "guide-export"
        export_dir = Path(args[3])
        payload = _comparison_payload(
            provider=provider,
            slug=args[1],
            page_url=f"https://example.test/{provider}/{args[1]}",
            page_title=f"{provider} guide",
            analysis_tags=["overview"] if provider == "icy-veins" else ["builds_talents", "talent_recommendations"],
            build_code="ABC123",
        )
        write_article_bundle(payload, provider=provider, export_dir=export_dir)
        return {
            "provider": provider,
            "exit_code": 0,
            "payload": _envelope({"output_dir": str(export_dir), "guide": payload["guide"], "redirect": redirects[provider]}),
        }

    # Icy Veins served another guide than the resolved one (a retired, redirected page).
    redirects: dict[str, dict[str, str] | None] = {
        "method": None,
        "icy-veins": {"requested": "mistweaver-monk-pve-healing-guide", "served": "mistweaver-monk-healing-guide", "message": "moved"},
    }
    monkeypatch.setattr("warcraft_cli.main.provider_resolve", fake_provider_resolve)
    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    result = runner.invoke(
        warcraft_app,
        [
            "guide-compare-query",
            "mistweaver monk guide",
            "--provider",
            "method",
            "--provider",
            "icy-veins",
            "--out-root",
            str(tmp_path / "orchestrated"),
        ],
    )
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    _assert_wrapper_success_envelope(payload, command="guide-compare-query")
    assert payload["kind"] == "guide_bundle_comparison_orchestration"
    assert payload["data"]["exported_bundle_count"] == 2
    assert payload["data"]["comparison"]["kind"] == "guide_bundle_comparison"
    assert payload["data"]["comparison"]["compared_bundle_count"] == 2
    # AUR-386: guide-compare-query's embedded comparison carries the same freshness + evidence
    # packet as the direct guide-compare command (shared builder).
    assert payload["data"]["comparison"]["freshness"]["status"] in {"fresh", "stale", "unknown"}
    assert "comparison_evidence" in payload["data"]["comparison"]
    assert payload["data"]["comparison"]["comparison_evidence"]["compared_bundle_count"] == 2
    assert all(row["status"] == "exported" for row in payload["data"]["provider_results"])
    assert {row["provider"]: row["redirect"] for row in payload["data"]["provider_results"]} == redirects
    assert {row["candidate"]["selection_source"] for row in payload["data"]["provider_results"]} == {"resolve"}

    # A rerun within --max-age-hours reuses both bundles and must still report the redirect.
    rerun = runner.invoke(
        warcraft_app,
        ["guide-compare-query", "mistweaver monk guide", "--provider", "method", "--provider", "icy-veins",
         "--out-root", str(tmp_path / "orchestrated")],
    )
    assert rerun.exit_code == 0
    reused_rows = json.loads(rerun.stdout)["data"]["provider_results"]
    assert all(row["status"] == "reused" for row in reused_rows)
    assert {row["provider"]: row["redirect"] for row in reused_rows} == redirects


def test_warcraft_guide_compare_query_uses_conservative_search_fallback(
    monkeypatch,
    tmp_path: Path,
) -> None:
    def fake_provider_resolve(provider: str, query: str, *, limit: int = 5, expansion: str | None = None, entity_types: tuple[str, ...] = ()) -> dict[str, object]:
        if provider == "method":
            return {
                "provider": provider,
                "exit_code": 0,
                "payload": _envelope({
                    "resolved": True,
                    "confidence": "high",
                    "match": {
                        "id": "mistweaver-monk",
                        "name": "Method Mistweaver Monk Guide",
                        "kind": "guide",
                        "url": "https://example.test/method/mistweaver-monk",
                    },
                    "next_command": "method guide mistweaver-monk",
                }),
            }
        return {
            "provider": provider,
            "exit_code": 0,
            "payload": _envelope({
                "resolved": False,
                "confidence": "none",
                "match": None,
                "next_command": None,
            }),
        }

    def fake_provider_search(provider: str, query: str, *, limit: int = 5, expansion: str | None = None, entity_types: tuple[str, ...] = ()) -> dict[str, object]:
        if provider == "icy-veins":
            return {
                "provider": provider,
                "exit_code": 0,
                "payload": _envelope({
                    "results": [
                        {
                            "id": "mistweaver-monk-pve-healing-guide",
                            "name": "Icy Veins Mistweaver Monk Guide",
                            "kind": "guide",
                            "url": "https://example.test/icy-veins/mistweaver-monk-pve-healing-guide",
                            "ranking": {"score": 72},
                            "follow_up": {"command": "icy-veins guide mistweaver-monk-pve-healing-guide"},
                        },
                        {
                            "id": "mistweaver-monk-pvp-guide",
                            "name": "Icy Veins Mistweaver Monk PvP Guide",
                            "kind": "guide",
                            "url": "https://example.test/icy-veins/mistweaver-monk-pvp-guide",
                            "ranking": {"score": 41},
                        },
                    ]
                }),
            }
        return {"provider": provider, "exit_code": 0, "payload": _envelope({"results": []})}

    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        export_dir = Path(args[3])
        payload = _comparison_payload(
            provider=provider,
            slug=args[1],
            page_url=f"https://example.test/{provider}/{args[1]}",
            page_title=f"{provider} guide",
            analysis_tags=["overview"] if provider == "icy-veins" else ["builds_talents", "talent_recommendations"],
            build_code="ABC123",
        )
        write_article_bundle(payload, provider=provider, export_dir=export_dir)
        return {
            "provider": provider,
            "exit_code": 0,
            "payload": _envelope({"output_dir": str(export_dir), "guide": payload["guide"]}),
        }

    monkeypatch.setattr("warcraft_cli.main.provider_resolve", fake_provider_resolve)
    monkeypatch.setattr("warcraft_cli.main.provider_search", fake_provider_search)
    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    result = runner.invoke(
        warcraft_app,
        [
            "guide-compare-query",
            "mistweaver monk guide",
            "--provider",
            "method",
            "--provider",
            "icy-veins",
            "--out-root",
            str(tmp_path / "orchestrated"),
        ],
    )
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["data"]["comparison"]["compared_bundle_count"] == 2
    icy_row = next(row for row in payload["data"]["provider_results"] if row["provider"] == "icy-veins")
    assert icy_row["candidate"]["selection_source"] == "search_fallback"
    assert icy_row["candidate"]["next_command"] == "icy-veins guide mistweaver-monk-pve-healing-guide"
    assert icy_row["candidate"]["selection_contract"]["minimum_top_score"] == 50
    assert icy_row["candidate"]["selection_contract"]["minimum_margin_over_runner_up"] == 25


def test_warcraft_guide_compare_query_skips_weak_search_fallback(
    monkeypatch,
    tmp_path: Path,
) -> None:
    def fake_provider_resolve(provider: str, query: str, *, limit: int = 5, expansion: str | None = None, entity_types: tuple[str, ...] = ()) -> dict[str, object]:
        if provider == "method":
            return {
                "provider": provider,
                "exit_code": 0,
                "payload": _envelope({
                    "resolved": True,
                    "confidence": "high",
                    "match": {
                        "id": "mistweaver-monk",
                        "name": "Method Mistweaver Monk Guide",
                        "kind": "guide",
                        "url": "https://example.test/method/mistweaver-monk",
                    },
                    "next_command": "method guide mistweaver-monk",
                }),
            }
        return {
            "provider": provider,
            "exit_code": 0,
            "payload": _envelope({
                "resolved": False,
                "confidence": "none",
                "match": None,
                "next_command": None,
            }),
        }

    def fake_provider_search(provider: str, query: str, *, limit: int = 5, expansion: str | None = None, entity_types: tuple[str, ...] = ()) -> dict[str, object]:
        if provider == "icy-veins":
            return {
                "provider": provider,
                "exit_code": 0,
                "payload": _envelope({
                    "results": [
                        {
                            "id": "mistweaver-monk-pve-healing-guide",
                            "name": "Icy Veins Mistweaver Monk Guide",
                            "kind": "guide",
                            "url": "https://example.test/icy-veins/mistweaver-monk-pve-healing-guide",
                            "ranking": {"score": 58},
                        },
                        {
                            "id": "mistweaver-monk-pvp-guide",
                            "name": "Icy Veins Mistweaver Monk PvP Guide",
                            "kind": "guide",
                            "url": "https://example.test/icy-veins/mistweaver-monk-pvp-guide",
                            "ranking": {"score": 42},
                        },
                    ]
                }),
            }
        return {"provider": provider, "exit_code": 0, "payload": _envelope({"results": []})}

    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        export_dir = Path(args[3])
        payload = _comparison_payload(
            provider=provider,
            slug=args[1],
            page_url=f"https://example.test/{provider}/{args[1]}",
            page_title=f"{provider} guide",
            analysis_tags=["builds_talents", "talent_recommendations"],
            build_code="ABC123",
        )
        write_article_bundle(payload, provider=provider, export_dir=export_dir)
        return {
            "provider": provider,
            "exit_code": 0,
            "payload": _envelope({"output_dir": str(export_dir), "guide": payload["guide"]}),
        }

    monkeypatch.setattr("warcraft_cli.main.provider_resolve", fake_provider_resolve)
    monkeypatch.setattr("warcraft_cli.main.provider_search", fake_provider_search)
    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    result = runner.invoke(
        warcraft_app,
        [
            "guide-compare-query",
            "mistweaver monk guide",
            "--provider",
            "method",
            "--provider",
            "icy-veins",
            "--out-root",
            str(tmp_path / "orchestrated"),
        ],
    )
    assert result.exit_code == 1

    payload = json.loads(result.stderr)
    icy_row = next(row for row in payload["error"]["details"]["provider_results"] if row["provider"] == "icy-veins")
    assert icy_row["status"] == "skipped"
    assert icy_row["reason"] == "search_results_not_decisive"
    assert payload["error"]["code"] == "insufficient_guides"


def test_warcraft_guide_compare_query_reuses_fresh_orchestrated_bundles(
    monkeypatch,
    tmp_path: Path,
) -> None:
    invoke_calls: list[tuple[str, str]] = []

    def fake_provider_resolve(provider: str, query: str, *, limit: int = 5, expansion: str | None = None, entity_types: tuple[str, ...] = ()) -> dict[str, object]:
        refs = {
            "method": ("mistweaver-monk", "Method Mistweaver Monk Guide"),
            "icy-veins": ("mistweaver-monk-pve-healing-guide", "Icy Veins Mistweaver Monk Guide"),
        }
        ref, name = refs[provider]
        return {
            "provider": provider,
            "exit_code": 0,
            "payload": _envelope({
                "resolved": True,
                "confidence": "high",
                "match": {
                    "id": ref,
                    "name": name,
                    "kind": "guide",
                    "url": f"https://example.test/{ref}",
                },
                "next_command": f"{provider} guide {ref}",
            }),
        }

    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        invoke_calls.append((provider, args[1]))
        export_dir = Path(args[3])
        payload = _comparison_payload(
            provider=provider,
            slug=args[1],
            page_url=f"https://example.test/{provider}/{args[1]}",
            page_title=f"{provider} guide",
            analysis_tags=["overview"] if provider == "icy-veins" else ["builds_talents", "talent_recommendations"],
            build_code="ABC123",
        )
        write_article_bundle(payload, provider=provider, export_dir=export_dir)
        return {
            "provider": provider,
            "exit_code": 0,
            "payload": _envelope({"output_dir": str(export_dir), "guide": payload["guide"]}),
        }

    monkeypatch.setattr("warcraft_cli.main.provider_resolve", fake_provider_resolve)
    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    out_root = tmp_path / "orchestrated"
    first = runner.invoke(
        warcraft_app,
        ["guide-compare-query", "mistweaver monk guide", "--provider", "method", "--provider", "icy-veins", "--out-root", str(out_root)],
    )
    assert first.exit_code == 0
    second = runner.invoke(
        warcraft_app,
        ["guide-compare-query", "mistweaver monk guide", "--provider", "method", "--provider", "icy-veins", "--out-root", str(out_root)],
    )
    assert second.exit_code == 0

    first_payload = json.loads(first.stdout)
    second_payload = json.loads(second.stdout)
    assert [row["status"] for row in first_payload["data"]["provider_results"]] == ["exported", "exported"]
    assert [row["status"] for row in second_payload["data"]["provider_results"]] == ["reused", "reused"]
    assert len(invoke_calls) == 2


def test_warcraft_guide_compare_query_refreshes_stale_orchestrated_bundles(
    monkeypatch,
    tmp_path: Path,
) -> None:
    invoke_calls: list[tuple[str, str]] = []

    def fake_provider_resolve(provider: str, query: str, *, limit: int = 5, expansion: str | None = None, entity_types: tuple[str, ...] = ()) -> dict[str, object]:
        refs = {
            "method": ("mistweaver-monk", "Method Mistweaver Monk Guide"),
            "icy-veins": ("mistweaver-monk-pve-healing-guide", "Icy Veins Mistweaver Monk Guide"),
        }
        ref, name = refs[provider]
        return {
            "provider": provider,
            "exit_code": 0,
            "payload": _envelope({
                "resolved": True,
                "confidence": "high",
                "match": {
                    "id": ref,
                    "name": name,
                    "kind": "guide",
                    "url": f"https://example.test/{ref}",
                },
                "next_command": f"{provider} guide {ref}",
            }),
        }

    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        invoke_calls.append((provider, args[1]))
        export_dir = Path(args[3])
        payload = _comparison_payload(
            provider=provider,
            slug=args[1],
            page_url=f"https://example.test/{provider}/{args[1]}",
            page_title=f"{provider} guide",
            analysis_tags=["overview"] if provider == "icy-veins" else ["builds_talents", "talent_recommendations"],
            build_code="ABC123",
        )
        write_article_bundle(payload, provider=provider, export_dir=export_dir)
        return {
            "provider": provider,
            "exit_code": 0,
            "payload": _envelope({"output_dir": str(export_dir), "guide": payload["guide"]}),
        }

    monkeypatch.setattr("warcraft_cli.main.provider_resolve", fake_provider_resolve)
    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    out_root = tmp_path / "orchestrated"
    first = runner.invoke(
        warcraft_app,
        ["guide-compare-query", "mistweaver monk guide", "--provider", "method", "--provider", "icy-veins", "--out-root", str(out_root)],
    )
    assert first.exit_code == 0

    manifest_path = out_root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for row in manifest["providers"]:
        row["exported_at"] = "2000-01-01T00:00:00Z"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")

    second = runner.invoke(
        warcraft_app,
        [
            "guide-compare-query",
            "mistweaver monk guide",
            "--provider",
            "method",
            "--provider",
            "icy-veins",
            "--out-root",
            str(out_root),
            "--max-age-hours",
            "1",
        ],
    )
    assert second.exit_code == 0

    second_payload = json.loads(second.stdout)
    assert [row["status"] for row in second_payload["data"]["provider_results"]] == ["exported", "exported"]
    assert len(invoke_calls) == 4


def test_warcraft_guide_compare_query_can_include_simc_build_handoff(
    monkeypatch,
    tmp_path: Path,
) -> None:
    invoke_calls: list[tuple[str, list[str]]] = []
    apl_path = tmp_path / "monk_mistweaver.simc"
    apl_path.write_text("actions=spinning_crane_kick\n", encoding="utf-8")

    def fake_provider_resolve(provider: str, query: str, *, limit: int = 5, expansion: str | None = None, entity_types: tuple[str, ...] = ()) -> dict[str, object]:
        refs = {
            "method": ("mistweaver-monk", "Method Mistweaver Monk Guide"),
            "wowhead": ("mistweaver-monk", "Wowhead Mistweaver Monk Guide"),
        }
        ref, name = refs[provider]
        return {
            "provider": provider,
            "exit_code": 0,
            "payload": _envelope({
                "resolved": True,
                "confidence": "high",
                "match": {
                    "id": ref,
                    "name": name,
                    "kind": "guide",
                    "url": f"https://example.test/{provider}/{ref}",
                },
                "next_command": f"{provider} guide {ref}",
            }),
        }

    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        invoke_calls.append({"provider": provider, "args": args})
        if provider in {"method", "wowhead"}:
            export_dir = Path(args[3])
            payload = _comparison_payload(
                provider=provider,
                slug=args[1],
                page_url=f"https://example.test/{provider}/{args[1]}",
                page_title=f"{provider} guide",
                analysis_tags=["overview"] if provider == "wowhead" else ["builds_talents", "talent_recommendations"],
                build_code="ABC123",
            )
            write_article_bundle(payload, provider=provider, export_dir=export_dir)
            return {
                "provider": provider,
                "exit_code": 0,
                "payload": _envelope({"output_dir": str(export_dir), "guide": payload["guide"]}),
            }
        raise AssertionError(f"unexpected provider call: {provider} {args}")

    def fake_simc_call(command: str, build: PacketInput | str, *, describe: DescribeOptions | None = None) -> dict[str, object]:
        invoke_calls.append({"provider": "simc", **_simc_call_summary(command, build, describe)})
        return _simc_result({"provider": "simc", "kind": command})

    monkeypatch.setattr("warcraft_cli.main.provider_resolve", fake_provider_resolve)
    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)
    monkeypatch.setattr("warcraft_cli.main.simc_call", fake_simc_call)

    result = runner.invoke(
        warcraft_app,
        [
            "guide-compare-query",
            "mistweaver monk guide",
            "--provider",
            "method",
            "--provider",
            "wowhead",
            "--out-root",
            str(tmp_path / "orchestrated"),
            "--simc-build-handoff",
            "--simc-apl-path",
            str(apl_path),
        ],
    )
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    handoff = payload["data"]["simc_build_handoff"]
    assert handoff["kind"] == "guide_builds_simc_handoff"
    assert handoff["source"]["kind"] == "orchestration_root"
    assert handoff["bundle_count"] == 2
    assert handoff["build_reference_count"] == 1
    assert handoff["apl_path"] == str(apl_path)
    assert len(handoff["builds"][0]["sources"]) == 2
    assert handoff["builds"][0]["talent_transport_packet"]["transport_status"] == "exact"
    assert handoff["builds"][0]["simc"]["identify"]["payload"]["kind"] == "identify-build"
    assert handoff["builds"][0]["simc"]["decode"]["payload"]["kind"] == "decode-build"
    assert handoff["builds"][0]["simc"]["describe"]["payload"]["kind"] == "describe-build"
    assert invoke_calls[0] == {
        "provider": "method",
        "args": ["guide-export", "mistweaver-monk", "--out", str(tmp_path / "orchestrated" / "method")],
    }
    assert invoke_calls[1] == {
        "provider": "wowhead",
        "args": ["guide-export", "mistweaver-monk", "--out", str(tmp_path / "orchestrated" / "wowhead")],
    }
    assert invoke_calls[2]["provider"] == "simc"
    assert invoke_calls[2]["command"] == "identify-build"
    assert invoke_calls[2]["build_input"] == "packet"
    assert invoke_calls[2]["packet_transport_status"] == "exact"
    assert invoke_calls[2]["packet_transport_url"] == "https://www.wowhead.com/talent-calc/monk/mistweaver/ABC123"
    assert invoke_calls[3]["command"] == "decode-build"
    assert invoke_calls[3]["build_input"] == "packet"
    assert invoke_calls[4]["command"] == "describe-build"
    assert invoke_calls[4]["build_input"] == "packet"
    assert invoke_calls[4]["describe"] == DescribeOptions(apl_path=str(apl_path))


def test_warcraft_guide_builds_simc_reads_bundle_build_refs(monkeypatch, tmp_path: Path) -> None:
    bundle_dir = tmp_path / "method-guide"
    write_article_bundle(
        _comparison_payload(
            provider="method",
            slug="mistweaver-monk",
            page_url="https://www.method.gg/guides/mistweaver-monk/talents",
            page_title="Method Talents",
            analysis_tags=["builds_talents", "talent_recommendations"],
            build_code="ABC123",
        ),
        provider="method",
        export_dir=bundle_dir,
    )

    invoke_calls: list[dict[str, object]] = []

    def fake_simc_call(command: str, build: PacketInput | str, *, describe: DescribeOptions | None = None) -> dict[str, object]:
        assert command in {"identify-build", "decode-build"}
        invoke_calls.append(_simc_call_summary(command, build, describe))
        return _simc_result(_envelope({
            "provider": "simc",
            "kind": "identify_build" if command == "identify-build" else "decode_build",
            "build_spec": {"talents": "ABC123", "actor_class": "monk", "spec": "mistweaver"},
        }))

    monkeypatch.setattr("warcraft_cli.main.simc_call", fake_simc_call)

    result = runner.invoke(warcraft_app, ["guide-builds-simc", str(bundle_dir)])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    _assert_wrapper_success_envelope(payload, command="guide-builds-simc")
    assert payload["kind"] == "guide_builds_simc_handoff"
    assert payload["data"]["source"]["kind"] == "bundle"
    assert payload["provenance"]["explicit_build_reference_only"] is True
    # write_article_bundle now stamps exported_at, so the single-bundle handoff has a real anchor (AUR-386).
    assert payload["data"]["freshness"]["status"] == "known"
    assert payload["data"]["freshness"]["reason"] == "bundle_manifest_exported_at"
    assert payload["data"]["freshness"]["sampled_at"] is not None
    assert payload["data"]["citations"]["build_reference_urls"] == ["https://www.wowhead.com/talent-calc/monk/mistweaver/ABC123"]
    assert payload["data"]["build_reference_count"] == 1
    assert payload["data"]["summary"]["returned_build_count"] == 1
    assert payload["data"]["summary"]["identify_success_count"] == 1
    assert payload["data"]["summary"]["decode_success_count"] == 1
    assert payload["data"]["builds"][0]["reference"]["build_code"] == "ABC123"
    assert payload["data"]["builds"][0]["talent_transport_packet"]["transport_status"] == "exact"
    assert (
        payload["data"]["builds"][0]["talent_transport_packet"]["transport_forms"]["wowhead_talent_calc_url"]
        == "https://www.wowhead.com/talent-calc/monk/mistweaver/ABC123"
    )
    assert payload["data"]["builds"][0]["evidence"]["explicit_build_reference_only"] is True
    assert payload["data"]["builds"][0]["evidence"]["provider_count"] == 1
    assert payload["data"]["builds"][0]["simc"]["identify"]["payload"]["kind"] == "identify_build"
    assert payload["data"]["builds"][0]["simc"]["decode"]["payload"]["kind"] == "decode_build"
    assert payload["data"]["builds"][0]["simc"]["describe"] is None
    assert len(invoke_calls) == 2
    assert invoke_calls[0]["command"] == "identify-build"
    assert invoke_calls[0]["build_input"] == "packet"
    # The packet reaches simc in memory: it names no file, so no temp path can surface in the output.
    assert invoke_calls[0]["packet_path"] is None
    assert invoke_calls[0]["packet_transport_status"] == "exact"
    assert invoke_calls[0]["packet_transport_url"] == "https://www.wowhead.com/talent-calc/monk/mistweaver/ABC123"
    assert invoke_calls[1]["command"] == "decode-build"
    assert invoke_calls[1]["build_input"] == "packet"


def test_warcraft_guide_builds_simc_reads_orchestration_root_and_dedupes_builds(
    monkeypatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "orchestrated"
    method_dir = root / "method"
    wowhead_dir = root / "wowhead"
    write_article_bundle(
        _comparison_payload(
            provider="method",
            slug="mistweaver-monk",
            page_url="https://www.method.gg/guides/mistweaver-monk/talents",
            page_title="Method Talents",
            analysis_tags=["builds_talents", "talent_recommendations"],
            build_code="ABC123",
        ),
        provider="method",
        export_dir=method_dir,
    )
    write_article_bundle(
        _comparison_payload(
            provider="wowhead",
            slug="mistweaver-monk",
            page_url="https://www.wowhead.com/guide/mistweaver-monk",
            page_title="Wowhead Talents",
            analysis_tags=["overview"],
            build_code="ABC123",
        ),
        provider="wowhead",
        export_dir=wowhead_dir,
    )
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "kind": "guide_compare_orchestration_manifest",
                "query": "mistweaver monk guide",
                "updated_at": "2026-03-15T04:00:00Z",
                "providers": [
                    {
                        "provider": "method",
                        "bundle_path": str(method_dir),
                        "candidate_ref": "mistweaver-monk",
                        "exported_at": "2026-03-15T00:00:00Z",
                    },
                    {
                        "provider": "wowhead",
                        "bundle_path": str(wowhead_dir),
                        "candidate_ref": "mistweaver-monk",
                        "exported_at": "2026-03-15T00:00:00Z",
                    },
                ],
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    invoke_calls: list[dict[str, object]] = []

    def fake_simc_call(command: str, build: PacketInput | str, *, describe: DescribeOptions | None = None) -> dict[str, object]:
        invoke_calls.append(_simc_call_summary(command, build, describe))
        return _simc_result({"provider": "simc", "kind": command})

    monkeypatch.setattr("warcraft_cli.main.simc_call", fake_simc_call)

    result = runner.invoke(warcraft_app, ["guide-builds-simc", str(root), "--no-decode"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["data"]["source"]["kind"] == "orchestration_root"
    assert payload["data"]["source"]["query"] == "mistweaver monk guide"
    assert payload["data"]["bundle_count"] == 2
    assert payload["data"]["build_reference_count"] == 1
    # Sampled when the oldest bundle was exported, not when the root's manifest was last rewritten.
    exported = sorted(json.loads((path / "manifest.json").read_text())["exported_at"] for path in (method_dir, wowhead_dir))
    assert (payload["data"]["freshness"]["status"], payload["data"]["freshness"]["reason"]) == ("known", "oldest_bundle_exported_at")
    assert payload["data"]["freshness"]["sampled_at"] == exported[0]
    assert payload["data"]["freshness"]["manifest_updated_at"] == "2026-03-15T04:00:00Z"
    assert payload["data"]["citations"]["bundle_paths"] == [str(method_dir), str(wowhead_dir)]
    assert len(payload["data"]["builds"][0]["sources"]) == 2
    assert payload["data"]["builds"][0]["evidence"]["provider_count"] == 2
    assert payload["data"]["builds"][0]["talent_transport_packet"]["transport_status"] == "exact"
    assert payload["data"]["builds"][0]["simc"]["decode"] is None
    assert payload["data"]["builds"][0]["simc"]["describe"] is None
    assert len(invoke_calls) == 1
    assert invoke_calls[0]["command"] == "identify-build"
    assert invoke_calls[0]["build_input"] == "packet"
    assert invoke_calls[0]["packet_transport_status"] == "exact"
    assert invoke_calls[0]["packet_transport_url"] == "https://www.wowhead.com/talent-calc/monk/mistweaver/ABC123"


def test_warcraft_guide_builds_simc_can_include_describe_build_with_apl(
    monkeypatch,
    tmp_path: Path,
) -> None:
    bundle_dir = tmp_path / "method-guide"
    apl_path = tmp_path / "monk_mistweaver.simc"
    apl_path.write_text("actions=spinning_crane_kick\n", encoding="utf-8")
    write_article_bundle(
        _comparison_payload(
            provider="method",
            slug="mistweaver-monk",
            page_url="https://www.method.gg/guides/mistweaver-monk/talents",
            page_title="Method Talents",
            analysis_tags=["builds_talents", "talent_recommendations"],
            build_code="ABC123",
        ),
        provider="method",
        export_dir=bundle_dir,
    )

    invoke_calls: list[dict[str, object]] = []

    def fake_simc_call(command: str, build: PacketInput | str, *, describe: DescribeOptions | None = None) -> dict[str, object]:
        invoke_calls.append(_simc_call_summary(command, build, describe))
        return _simc_result({"provider": "simc", "kind": command})

    monkeypatch.setattr("warcraft_cli.main.simc_call", fake_simc_call)

    result = runner.invoke(
        warcraft_app,
        ["guide-builds-simc", str(bundle_dir), "--apl-path", str(apl_path)],
    )
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["data"]["apl_path"] == str(apl_path)
    assert payload["data"]["summary"]["describe_success_count"] == 1
    assert payload["data"]["builds"][0]["talent_transport_packet"]["transport_status"] == "exact"
    assert payload["data"]["builds"][0]["simc"]["describe"]["payload"]["kind"] == "describe-build"
    assert len(invoke_calls) == 3
    assert invoke_calls[0]["command"] == "identify-build"
    assert invoke_calls[0]["build_input"] == "packet"
    assert invoke_calls[1]["command"] == "decode-build"
    assert invoke_calls[1]["build_input"] == "packet"
    assert invoke_calls[2]["command"] == "describe-build"
    assert invoke_calls[2]["build_input"] == "packet"
    assert invoke_calls[2]["describe"] == DescribeOptions(apl_path=str(apl_path))


def test_warcraft_guide_builds_simc_skips_buildless_talent_calc_refs(monkeypatch, tmp_path: Path) -> None:
    bundle_dir = tmp_path / "method-guide"
    payload = _comparison_payload(
        provider="method",
        slug="mistweaver-monk",
        page_url="https://www.method.gg/guides/mistweaver-monk/talents",
        page_title="Method Talents",
        analysis_tags=["builds_talents", "talent_recommendations"],
    )
    payload["build_references"] = {
        "count": 1,
        "items": [
            {
                "kind": "build_reference",
                "reference_type": "wowhead_talent_calc_url",
                "url": "https://www.wowhead.com/talent-calc/monk/mistweaver",
                "label": "Landing Page",
                "build_code": None,
                "source_urls": ["https://www.method.gg/guides/mistweaver-monk/talents"],
            }
        ],
    }
    write_article_bundle(payload, provider="method", export_dir=bundle_dir)

    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        raise AssertionError(f"unexpected provider call: {provider} {args}")

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    result = runner.invoke(warcraft_app, ["guide-builds-simc", str(bundle_dir)])
    assert result.exit_code == 0
    handoff = json.loads(result.stdout)
    assert handoff["data"]["build_reference_count"] == 1
    assert handoff["data"]["summary"]["returned_build_count"] == 0
    assert handoff["data"]["summary"]["excluded_build_count"] == 1
    assert handoff["data"]["builds"] == []


def test_warcraft_guide_builds_simc_backfills_build_code_from_explicit_url(monkeypatch, tmp_path: Path) -> None:
    bundle_dir = tmp_path / "method-guide"
    payload = _comparison_payload(
        provider="method",
        slug="mistweaver-monk",
        page_url="https://www.method.gg/guides/mistweaver-monk/talents",
        page_title="Method Talents",
        analysis_tags=["builds_talents", "talent_recommendations"],
        build_code="ABC123",
    )
    payload["build_references"]["items"][0]["build_code"] = None
    write_article_bundle(payload, provider="method", export_dir=bundle_dir)

    invoke_calls: list[dict[str, object]] = []

    def fake_simc_call(command: str, build: PacketInput | str, *, describe: DescribeOptions | None = None) -> dict[str, object]:
        assert command in {"identify-build", "decode-build"}
        invoke_calls.append(_simc_call_summary(command, build, describe))
        return _simc_result(_envelope({
            "provider": "simc",
            "kind": "identify_build" if command == "identify-build" else "decode_build",
            "build_spec": {"talents": "ABC123", "actor_class": "monk", "spec": "mistweaver"},
        }))

    monkeypatch.setattr("warcraft_cli.main.simc_call", fake_simc_call)

    result = runner.invoke(warcraft_app, ["guide-builds-simc", str(bundle_dir)])
    assert result.exit_code == 0

    handoff = json.loads(result.stdout)
    assert handoff["data"]["build_reference_count"] == 1
    assert handoff["data"]["summary"]["returned_build_count"] == 1
    assert handoff["data"]["summary"]["excluded_build_count"] == 0
    assert handoff["data"]["builds"][0]["reference"]["build_code"] == "ABC123"
    assert handoff["data"]["builds"][0]["talent_transport_packet"]["transport_status"] == "exact"
    assert invoke_calls[0]["build_input"] == "packet"
    assert invoke_calls[0]["packet_transport_url"] == "https://www.wowhead.com/talent-calc/monk/mistweaver/ABC123"


def test_warcraft_guide_compare_query_fails_when_too_few_guides_export(
    monkeypatch,
    tmp_path: Path,
) -> None:
    def fake_provider_resolve(provider: str, query: str, *, limit: int = 5, expansion: str | None = None, entity_types: tuple[str, ...] = ()) -> dict[str, object]:
        if provider == "method":
            return {
                "provider": provider,
                "exit_code": 0,
                "payload": _envelope({
                    "resolved": True,
                    "confidence": "high",
                    "match": {
                        "id": "mistweaver-monk",
                        "name": "Method Mistweaver Monk Guide",
                        "kind": "guide",
                        "url": "https://example.test/method/mistweaver-monk",
                    },
                    "next_command": "method guide mistweaver-monk",
                }),
            }
        return {
            "provider": provider,
            "exit_code": 0,
            "payload": _envelope({
                "resolved": False,
                "confidence": "none",
                "match": None,
                "next_command": None,
            }),
        }

    def fake_provider_search(provider: str, query: str, *, limit: int = 5, expansion: str | None = None, entity_types: tuple[str, ...] = ()) -> dict[str, object]:
        return {
            "provider": provider,
            "exit_code": 0,
            "payload": _envelope({
                "results": [
                    {
                        "id": "mistweaver-monk-pve-healing-guide",
                        "name": "Icy Veins Mistweaver Monk Guide",
                        "kind": "guide",
                        "url": "https://example.test/icy-veins/mistweaver-monk-pve-healing-guide",
                        "ranking": {"score": 25},
                    },
                    {
                        "id": "mistweaver-monk-pvp-guide",
                        "name": "Icy Veins Mistweaver Monk PvP Guide",
                        "kind": "guide",
                        "url": "https://example.test/icy-veins/mistweaver-monk-pvp-guide",
                        "ranking": {"score": 22},
                    },
                ]
            }),
        }

    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        export_dir = Path(args[3])
        payload = _comparison_payload(
            provider=provider,
            slug="mistweaver-monk",
            page_url="https://example.test/method/mistweaver-monk",
            page_title="method guide",
            analysis_tags=["builds_talents", "talent_recommendations"],
            build_code="ABC123",
        )
        write_article_bundle(payload, provider=provider, export_dir=export_dir)
        return {
            "provider": provider,
            "exit_code": 0,
            "payload": _envelope({"output_dir": str(export_dir), "guide": payload["guide"]}),
        }

    monkeypatch.setattr("warcraft_cli.main.provider_resolve", fake_provider_resolve)
    monkeypatch.setattr("warcraft_cli.main.provider_search", fake_provider_search)
    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    result = runner.invoke(
        warcraft_app,
        [
            "guide-compare-query",
            "mistweaver monk guide",
            "--provider",
            "method",
            "--provider",
            "icy-veins",
            "--out-root",
            str(tmp_path / "orchestrated"),
        ],
    )
    assert result.exit_code == 1

    payload = json.loads(result.stderr)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "insufficient_guides"
    # The reason each provider declined survives in the failure envelope's own fields.
    details = payload["error"]["details"]
    assert payload["data"] == {}
    assert details["exported_bundle_count"] == 1
    assert details["required_bundle_count"] == 2
    assert details["selected_providers"] == ["method", "icy-veins"]
    icy_row = next(row for row in details["provider_results"] if row["provider"] == "icy-veins")
    assert icy_row["status"] == "skipped"
    assert icy_row["reason"] == "search_top_guide_score_too_low:25"
    method_row = next(row for row in details["provider_results"] if row["provider"] == "method")
    assert method_row["candidate_ref"] == "mistweaver-monk"
    # A failed run must not leave a manifest behind for the next run to reuse.
    assert not (tmp_path / "orchestrated" / "manifest.json").exists()


def test_warcraft_search_sorts_results_globally_by_ranking(monkeypatch) -> None:
    def fake_wowhead_search(self, query: str):  # noqa: ANN001
        return {
            "search": query,
            "results": [
                {
                    "id": 19019,
                    "name": "Thunderfury",
                    "entity_type": "item",
                    "ranking": {"score": 15, "match_reasons": ["name_contains_query"]},
                },
            ],
        }

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.search_suggestions", fake_wowhead_search)
    monkeypatch.setattr(
        "method_cli.main.MethodClient.sitemap_guides",
        lambda self: [{"slug": "mistweaver-monk", "name": "Mistweaver Monk", "url": "https://www.method.gg/guides/mistweaver-monk"}],
    )
    monkeypatch.setattr(
        "icy_veins_cli.main.IcyVeinsClient.sitemap_guides",
        lambda self: [{"slug": "frost-death-knight-pve-dps-guide", "name": "Frost Death Knight PvE DPS Guide",
                       "url": "https://www.icy-veins.com/wow/frost-death-knight-pve-dps-guide"}],
    )
    monkeypatch.setattr("raiderio_cli.client.RaiderIOClient.search", lambda self, *, term, kind=None: {"matches": []})
    monkeypatch.setattr("warcraft_wiki_cli.main.WarcraftWikiClient.search_articles", lambda self, query, *, limit: (0, []))

    result = runner.invoke(warcraft_app, ["search", "mistweaver monk guide", "--limit", "5"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["data"]["results"][0]["provider"] == "method"
    assert payload["data"]["results"][0]["wrapper_ranking"]["score"] >= payload["data"]["results"][0]["ranking"]["score"]


def test_warcraft_search_expansion_filter_excludes_nonmatching_providers(monkeypatch) -> None:
    def fake_wowhead_search(self, query: str):  # noqa: ANN001
        return {
            "search": query,
            "results": [
                {
                    "id": 19019,
                    "name": "Thunderfury",
                    "entity_type": "item",
                    "ranking": {"score": 20, "match_reasons": ["name_contains_query"]},
                },
            ],
        }

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.search_suggestions", fake_wowhead_search)
    monkeypatch.setattr("method_cli.main.MethodClient.sitemap_guides", lambda self: (
        _ for _ in ()).throw(AssertionError("method should be excluded")))
    monkeypatch.setattr(
        "icy_veins_cli.main.IcyVeinsClient.sitemap_guides",
        lambda self: (_ for _ in ()).throw(AssertionError("icy-veins should be excluded")),
    )
    monkeypatch.setattr(
        "raiderio_cli.client.RaiderIOClient.search",
        lambda self, *, term, kind=None: (_ for _ in ()).throw(AssertionError("raiderio should be excluded")),
    )
    monkeypatch.setattr(
        "warcraft_wiki_cli.main.WarcraftWikiClient.search_articles",
        lambda self, query, *, limit: (_ for _ in ()).throw(AssertionError("warcraft-wiki should be excluded")),
    )

    result = runner.invoke(warcraft_app, ["--expansion", "wotlk", "search", "thunderfury", "--limit", "3"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["data"]["requested_expansion"] == "wotlk"
    assert payload["data"]["expansion_filter_active"] is True
    assert payload["data"]["included_providers"] == ["wowhead", "warcraftlogs"]
    assert payload["data"]["results"][0]["provider"] == "wowhead"
    assert payload["data"]["results"][0]["provider_expansion"]["mode"] == "profiled"
    assert {row["provider"] for row in payload["data"]["excluded_providers"]} == {
        "method",
        "icy-veins",
        "raiderio",
        "warcraft-wiki",
        "simc",
        "raidbots",
        "blizzard-api",
        "curseforge",
        "lorrgs",
    }
    excluded = {row["provider"]: row["expansion_support"]["exclusion_reason"] for row in payload["data"]["excluded_providers"]}
    assert excluded["method"] == "provider_fixed_to_other_expansion"
    assert excluded["warcraft-wiki"] == "provider_fixed_to_other_expansion"


def test_warcraft_search_brief_expansion_debug(monkeypatch) -> None:
    def fake_wowhead_search(self, query: str):  # noqa: ANN001
        return {
            "search": query,
            "results": [
                {
                    "id": 19019,
                    "name": "Thunderfury",
                    "entity_type": "item",
                    "ranking": {"score": 20, "match_reasons": ["name_contains_query"]},
                },
            ],
        }

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.search_suggestions", fake_wowhead_search)
    monkeypatch.setattr("method_cli.main.MethodClient.sitemap_guides", lambda self: (
        _ for _ in ()).throw(AssertionError("method should be excluded")))
    monkeypatch.setattr("icy_veins_cli.main.IcyVeinsClient.sitemap_guides", lambda self: (
        _ for _ in ()).throw(AssertionError("icy-veins should be excluded")))
    monkeypatch.setattr("raiderio_cli.client.RaiderIOClient.search", lambda self, *, term,
                        kind=None: (_ for _ in ()).throw(AssertionError("raiderio should be excluded")))
    monkeypatch.setattr("warcraft_wiki_cli.main.WarcraftWikiClient.search_articles", lambda self, query, *,
                        limit: (_ for _ in ()).throw(AssertionError("warcraft-wiki should be excluded")))

    result = runner.invoke(
        warcraft_app,
        ["--expansion", "wotlk", "search", "thunderfury", "--limit", "3", "--brief", "--expansion-debug"],
    )
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["data"]["providers"] == []
    assert payload["data"]["results"][0]["provider_expansion"]["mode"] == "profiled"
    snapshot = {row["provider"]: row["expansion_support"] for row in payload["data"]["expansion_debug"]}
    assert snapshot["wowhead"]["allowed"] is True
    assert snapshot["wowhead"]["review_status"] == "reviewed"
    assert snapshot["method"]["allowed"] is False
    assert snapshot["method"]["exclusion_reason"] == "provider_fixed_to_other_expansion"
    assert "retail-focused" in snapshot["method"]["policy_note"]


def test_warcraft_search_retail_filter_keeps_fixed_retail_providers_and_excludes_none(monkeypatch) -> None:
    def fake_wowhead_search(self, query: str):  # noqa: ANN001
        return {
            "search": query,
            "results": [
                {
                    "id": 19019,
                    "name": "Thunderfury",
                    "entity_type": "item",
                    "ranking": {"score": 20, "match_reasons": ["name_contains_query"]},
                },
            ],
        }

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.search_suggestions", fake_wowhead_search)
    monkeypatch.setattr(
        "method_cli.main.MethodClient.sitemap_guides",
        lambda self: [{"slug": "mistweaver-monk", "name": "Mistweaver Monk", "url": "https://www.method.gg/guides/mistweaver-monk"}],
    )
    monkeypatch.setattr(
        "icy_veins_cli.main.IcyVeinsClient.sitemap_guides",
        lambda self: [{"slug": "mistweaver-monk-pve-healing-guide", "name": "Mistweaver Monk PvE Healing Guide",
                       "url": "https://www.icy-veins.com/wow/mistweaver-monk-pve-healing-guide"}],
    )
    monkeypatch.setattr("raiderio_cli.client.RaiderIOClient.search", lambda self, *, term, kind=None: {"matches": []})
    monkeypatch.setattr(
        "warcraft_wiki_cli.main.WarcraftWikiClient.search_articles",
        lambda self, query, *, limit: (1, [{"title": "Mistweaver Monk", "pageid": 1}]),
    )

    result = runner.invoke(warcraft_app, ["--expansion", "retail", "search", "mistweaver monk guide", "--limit", "5"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["data"]["requested_expansion"] == "retail"
    assert payload["data"]["expansion_filter_active"] is True
    assert set(payload["data"]["included_providers"]) == {
        "wowhead",
        "method",
        "icy-veins",
        "raiderio",
        "warcraftlogs",
        "warcraft-wiki",
        "lorrgs",
    }
    assert {row["provider"] for row in payload["data"]["excluded_providers"]} == {
        "simc",
        "raidbots",
        "blizzard-api",
        "curseforge",
    }
    results = {row["provider"] for row in payload["data"]["results"]}
    assert {"method", "icy-veins"} & results


def test_warcraft_resolve_retail_filter_keeps_fixed_retail_profile_provider(monkeypatch) -> None:
    _stub_raiderio_profile_lookups(monkeypatch)
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.search_suggestions", lambda self, query: {"search": query, "results": []})
    monkeypatch.setattr("method_cli.main.MethodClient.sitemap_guides", lambda self: [])
    monkeypatch.setattr("icy_veins_cli.main.IcyVeinsClient.sitemap_guides", lambda self: [])
    monkeypatch.setattr(
        "raiderio_cli.client.RaiderIOClient.search",
        lambda self, *, term, kind=None: {"matches": []},
    )
    monkeypatch.setattr(
        "warcraft_wiki_cli.main.WarcraftWikiClient.search_articles",
        lambda self, query, *, limit: (0, []),
    )
    _stub_raiderio_guild_lookup(monkeypatch)

    result = runner.invoke(warcraft_app, ["--expansion", "retail", "resolve", "guild us illidan Liquid"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["data"]["requested_expansion"] == "retail"
    assert payload["data"]["expansion_filter_active"] is True
    assert payload["data"]["selected_provider"] == "raiderio"
    assert set(payload["data"]["included_providers"]) == {
        "wowhead",
        "method",
        "icy-veins",
        "raiderio",
        "warcraftlogs",
        "warcraft-wiki",
        "lorrgs",
    }
    assert {row["provider"] for row in payload["data"]["excluded_providers"]} == {
        "simc",
        "raidbots",
        "blizzard-api",
        "curseforge",
    }


def test_warcraft_search_prefers_profile_provider_for_structured_guild_queries(monkeypatch) -> None:
    _stub_raiderio_profile_lookups(monkeypatch)
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.search_suggestions", lambda self, query: {"search": query, "results": []})
    monkeypatch.setattr(
        "method_cli.main.MethodClient.sitemap_guides",
        lambda self: [{"slug": "liquid-guide", "name": "Liquid Guide", "url": "https://www.method.gg/guides/liquid-guide"}],
    )
    monkeypatch.setattr(
        "icy_veins_cli.main.IcyVeinsClient.sitemap_guides",
        lambda self: [{"slug": "liquid-guide", "name": "Liquid Guide", "url": "https://www.icy-veins.com/wow/liquid-guide"}],
    )
    monkeypatch.setattr("raiderio_cli.client.RaiderIOClient.search", lambda self, *, term, kind=None: {"matches": []})
    monkeypatch.setattr("warcraft_wiki_cli.main.WarcraftWikiClient.search_articles", lambda self, query, *, limit: (0, []))
    _stub_raiderio_guild_lookup(monkeypatch)

    result = runner.invoke(warcraft_app, ["search", "guild us illidan Liquid", "--limit", "5"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["data"]["results"][0]["provider"] == "raiderio"
    assert any(
        "intent:structured_profile:family:profile" in reason
        for reason in payload["data"]["results"][0]["wrapper_ranking"]["reasons"]
    )


def test_warcraft_search_brief_and_ranking_debug(monkeypatch) -> None:
    _stub_raiderio_profile_lookups(monkeypatch)
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.search_suggestions", lambda self, query: {"search": query, "results": []})
    monkeypatch.setattr("method_cli.main.MethodClient.sitemap_guides", lambda self: [])
    monkeypatch.setattr("icy_veins_cli.main.IcyVeinsClient.sitemap_guides", lambda self: [])
    monkeypatch.setattr("raiderio_cli.client.RaiderIOClient.search", lambda self, *, term, kind=None: {"matches": []})
    monkeypatch.setattr("warcraft_wiki_cli.main.WarcraftWikiClient.search_articles", lambda self, query, *, limit: (0, []))
    _stub_raiderio_guild_lookup(monkeypatch)

    result = runner.invoke(warcraft_app, ["search", "guild us illidan Liquid", "--limit", "3", "--brief", "--ranking-debug"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["data"]["providers"] == []
    assert payload["data"]["results"][0]["provider"] == "raiderio"
    assert payload["data"]["ranking_debug"][0]["wrapper_ranking"]["provider_family"] == "profile"


def test_warcraft_resolve_prefers_stronger_later_provider(monkeypatch) -> None:
    def fake_wowhead_search(self, query: str):  # noqa: ANN001
        return {
            "search": query,
            "results": [
                {"type": 100, "id": 2594, "name": "Warlords of Draenor Mistweaver Monk Guide", "typeName": "Guide", "popularity": 8},
            ],
        }

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.search_suggestions", fake_wowhead_search)
    monkeypatch.setattr(
        "method_cli.main.MethodClient.sitemap_guides",
        lambda self: [{"slug": "mistweaver-monk", "name": "Mistweaver Monk", "url": "https://www.method.gg/guides/mistweaver-monk"}],
    )
    monkeypatch.setattr(
        "icy_veins_cli.main.IcyVeinsClient.sitemap_guides",
        lambda self: [{"slug": "mistweaver-monk-pve-healing-guide", "name": "Mistweaver Monk PvE Healing Guide",
                       "url": "https://www.icy-veins.com/wow/mistweaver-monk-pve-healing-guide"}],
    )
    monkeypatch.setattr("raiderio_cli.client.RaiderIOClient.search", lambda self, *, term, kind=None: {"matches": []})
    monkeypatch.setattr(
        "warcraft_wiki_cli.main.WarcraftWikiClient.search_articles",
        lambda self, query, *, limit: (1, [{"title": "Mistweaver Monk", "pageid": 1, "snippet": "Reference page",
                                           "url": "https://warcraft.wiki.gg/wiki/Mistweaver_Monk"}]),
    )

    result = runner.invoke(warcraft_app, ["resolve", "mistweaver monk guide"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["data"]["resolved"] is True
    assert payload["data"]["selected_provider"] == "icy-veins"
    assert payload["data"]["confidence"] == "high"
    assert payload["data"]["next_command"] == "icy-veins guide mistweaver-monk-pve-healing-guide"


def test_warcraft_resolve_expansion_filter_blocks_retail_only_resolution(monkeypatch) -> None:
    monkeypatch.setattr(
        "wowhead_cli.main.WowheadClient.search_suggestions",
        lambda self, query: {"search": query, "results": []},
    )
    monkeypatch.setattr("method_cli.main.MethodClient.sitemap_guides", lambda self: (
        _ for _ in ()).throw(AssertionError("method should be excluded")))
    monkeypatch.setattr("icy_veins_cli.main.IcyVeinsClient.sitemap_guides", lambda self: (
        _ for _ in ()).throw(AssertionError("icy-veins should be excluded")))
    monkeypatch.setattr(
        "raiderio_cli.client.RaiderIOClient.search",
        lambda self, *, term, kind=None: (_ for _ in ()).throw(AssertionError("raiderio should be excluded")),
    )
    monkeypatch.setattr(
        "warcraft_wiki_cli.main.WarcraftWikiClient.search_articles",
        lambda self, query, *, limit: (_ for _ in ()).throw(AssertionError("warcraft-wiki should be excluded")),
    )

    result = runner.invoke(warcraft_app, ["--expansion", "wotlk", "resolve", "guild us illidan Liquid"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["data"]["requested_expansion"] == "wotlk"
    assert payload["data"]["expansion_filter_active"] is True
    assert payload["data"]["resolved"] is False
    assert payload["provider"] == "warcraft"
    assert payload["data"]["selected_provider"] is None
    assert payload["data"]["included_providers"] == ["wowhead", "warcraftlogs"]
    assert {row["provider"] for row in payload["data"]["excluded_providers"]} == {
        "method",
        "icy-veins",
        "raiderio",
        "warcraft-wiki",
        "simc",
        "raidbots",
        "blizzard-api",
        "curseforge",
        "lorrgs",
    }


def test_warcraft_resolve_expansion_debug(monkeypatch) -> None:
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.search_suggestions", lambda self, query: {"search": query, "results": []})
    monkeypatch.setattr("method_cli.main.MethodClient.sitemap_guides", lambda self: (
        _ for _ in ()).throw(AssertionError("method should be excluded")))
    monkeypatch.setattr("icy_veins_cli.main.IcyVeinsClient.sitemap_guides", lambda self: (
        _ for _ in ()).throw(AssertionError("icy-veins should be excluded")))
    monkeypatch.setattr("raiderio_cli.client.RaiderIOClient.search", lambda self, *, term,
                        kind=None: (_ for _ in ()).throw(AssertionError("raiderio should be excluded")))
    monkeypatch.setattr("warcraft_wiki_cli.main.WarcraftWikiClient.search_articles", lambda self, query, *,
                        limit: (_ for _ in ()).throw(AssertionError("warcraft-wiki should be excluded")))

    result = runner.invoke(
        warcraft_app,
        ["--expansion", "wotlk", "resolve", "guild us illidan Liquid", "--brief", "--expansion-debug"],
    )
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    snapshot = {row["provider"]: row["expansion_support"] for row in payload["data"]["expansion_debug"]}
    assert snapshot["wowhead"]["allowed"] is True
    assert snapshot["simc"]["allowed"] is False
    assert snapshot["simc"]["exclusion_reason"] == "provider_has_no_expansion_support"
    assert snapshot["simc"]["review_status"] == "deferred"
    assert "Local repo analysis" in snapshot["simc"]["policy_note"]


def test_warcraft_resolve_prefers_ready_provider(monkeypatch) -> None:
    def fake_search(self, query: str):  # noqa: ANN001
        return {
            "search": query,
            "results": [
                {"type": 5, "id": 86739, "name": "Fairbreeze Favors", "typeName": "Quest", "popularity": 7},
            ],
        }

    monkeypatch.setattr(
        "method_cli.main.MethodClient.sitemap_guides",
        lambda self: [{"slug": "mistweaver-monk", "name": "Mistweaver Monk", "url": "https://www.method.gg/guides/mistweaver-monk"}],
    )
    monkeypatch.setattr("icy_veins_cli.main.IcyVeinsClient.sitemap_guides", lambda self: [])
    monkeypatch.setattr("raiderio_cli.client.RaiderIOClient.search", lambda self, *, term, kind=None: {"matches": []})
    monkeypatch.setattr("warcraft_wiki_cli.main.WarcraftWikiClient.search_articles", lambda self, query, *, limit: (0, []))
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.search_suggestions", fake_search)
    result = runner.invoke(warcraft_app, ["resolve", "fairbreeze favors"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    # The envelope names the binary; the provider the match came from is data.selected_provider.
    _assert_wrapper_success_envelope(payload, command="resolve")
    assert payload["data"]["resolved"] is True
    assert payload["data"]["selected_provider"] == "wowhead"
    assert payload["data"]["next_command"] == "wowhead entity quest 86739"


def test_warcraft_resolve_can_select_raiderio(monkeypatch) -> None:
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.search_suggestions", lambda self, query: {"search": query, "results": []})
    monkeypatch.setattr("method_cli.main.MethodClient.sitemap_guides", lambda self: [])
    monkeypatch.setattr("icy_veins_cli.main.IcyVeinsClient.sitemap_guides", lambda self: [])
    monkeypatch.setattr(
        "raiderio_cli.client.RaiderIOClient.search",
        lambda self, *, term, kind=None: {
            "matches": [
                {
                    "type": "character",
                    "name": "Roguecane",
                    "data": {
                        "id": 39943,
                        "name": "Roguecane",
                        "faction": "horde",
                        "region": {"slug": "us", "name": "United States & Oceania"},
                        "realm": {"slug": "illidan", "name": "Illidan"},
                        "class": {"name": "Rogue", "slug": "rogue"},
                    },
                }
            ]
        },
    )
    monkeypatch.setattr("warcraft_wiki_cli.main.WarcraftWikiClient.search_articles", lambda self, query, *, limit: (0, []))

    result = runner.invoke(warcraft_app, ["resolve", "Roguecane"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["resolved"] is True
    assert payload["data"]["selected_provider"] == "raiderio"
    assert payload["data"]["next_command"] == "raiderio character us illidan Roguecane"


def test_warcraft_resolve_prefers_raiderio_for_character_queries_when_both_resolve(monkeypatch) -> None:
    _stub_raiderio_profile_lookups(monkeypatch, character={"name": "Roguecane", "region": "us", "realm": "Illidan", "class": "Rogue", "faction": "horde", "profile_url": "https://raider.io/characters/us/illidan/Roguecane"})
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.search_suggestions", lambda self, query: {"search": query, "results": []})
    monkeypatch.setattr("method_cli.main.MethodClient.sitemap_guides", lambda self: [])
    monkeypatch.setattr("icy_veins_cli.main.IcyVeinsClient.sitemap_guides", lambda self: [])
    monkeypatch.setattr(
        "raiderio_cli.client.RaiderIOClient.search",
        lambda self, *, term, kind=None: {
            "matches": [
                {
                    "type": "character",
                    "name": "Roguecane",
                    "data": {
                        "id": 39943,
                        "name": "Roguecane",
                        "faction": "horde",
                        "region": {"slug": "us", "name": "United States & Oceania"},
                        "realm": {"slug": "illidan", "name": "Illidan"},
                        "class": {"name": "Rogue", "slug": "rogue"},
                    },
                }
            ]
        },
    )
    monkeypatch.setattr("warcraft_wiki_cli.main.WarcraftWikiClient.search_articles", lambda self, query, *, limit: (0, []))

    result = runner.invoke(warcraft_app, ["resolve", "character us illidan Roguecane", "--ranking-debug"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["resolved"] is True
    assert payload["data"]["selected_provider"] == "raiderio"
    assert payload["data"]["next_command"] == "raiderio character us illidan Roguecane"
    assert payload["data"]["ranking_debug"][0]["provider"] == "raiderio"


def test_warcraft_resolve_can_select_raiderio_guild(monkeypatch) -> None:
    _stub_raiderio_profile_lookups(monkeypatch)
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.search_suggestions", lambda self, query: {"search": query, "results": []})
    monkeypatch.setattr("method_cli.main.MethodClient.sitemap_guides", lambda self: [])
    monkeypatch.setattr("icy_veins_cli.main.IcyVeinsClient.sitemap_guides", lambda self: [])
    monkeypatch.setattr("raiderio_cli.client.RaiderIOClient.search", lambda self, *, term, kind=None: {"matches": []})
    monkeypatch.setattr("warcraft_wiki_cli.main.WarcraftWikiClient.search_articles", lambda self, query, *, limit: (0, []))
    _stub_raiderio_guild_lookup(monkeypatch)

    result = runner.invoke(warcraft_app, ["resolve", "guild us illidan Liquid"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["resolved"] is True
    assert payload["data"]["selected_provider"] == "raiderio"
    assert payload["data"]["next_command"] == "raiderio guild us illidan Liquid"
    assert payload["data"]["match"]["wrapper_ranking"]["provider_family"] == "profile"


def test_warcraft_resolve_can_select_warcraftlogs_for_explicit_report_reference(monkeypatch) -> None:
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.search_suggestions", lambda self, query: {"search": query, "results": []})
    monkeypatch.setattr("method_cli.main.MethodClient.sitemap_guides", lambda self: [])
    monkeypatch.setattr("icy_veins_cli.main.IcyVeinsClient.sitemap_guides", lambda self: [])
    monkeypatch.setattr("raiderio_cli.client.RaiderIOClient.search", lambda self, *, term, kind=None: {"matches": []})
    monkeypatch.setattr("warcraft_wiki_cli.main.WarcraftWikiClient.search_articles", lambda self, query, *, limit: (0, []))

    result = runner.invoke(warcraft_app, ["resolve", "https://www.warcraftlogs.com/reports/abcd1234#fight=3"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["data"]["resolved"] is True
    assert payload["data"]["selected_provider"] == "warcraftlogs"
    assert payload["data"]["next_command"] == "warcraftlogs report-encounter abcd1234 --fight-id 3"
    assert payload["data"]["match"]["wrapper_ranking"]["provider_family"] == "logs"


def test_warcraft_resolve_preserves_warcraftlogs_site_follow_up_for_expansion(monkeypatch) -> None:
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.search_suggestions", lambda self, query: {"search": query, "results": []})

    result = runner.invoke(warcraft_app, ["--expansion", "wotlk", "resolve", "https://classic.warcraftlogs.com/reports/abcd1234#fight=3"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["data"]["resolved"] is True
    assert payload["data"]["selected_provider"] == "warcraftlogs"
    assert payload["data"]["next_command"] == "warcraftlogs --site classic report-encounter abcd1234 --fight-id 3"


def _cooldown_packet_invoke(
    calls: list[tuple[str, list[str]]], *, wcl_fight_difficulty: int | None = 5
) -> Callable[..., dict[str, object]]:
    """Providers answering one cached Lorrgs fight, its Warcraft Logs casts, and a Lorrgs ranking."""

    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        calls.append((provider, args))
        if provider == "warcraftlogs" and args[:1] == ["graphql"]:
            return {"provider": provider, "exit_code": 0, "payload": _wcl_phases(None)}
        if provider == "lorrgs" and args[:1] == ["user-report-fights"]:
            return {
                "provider": provider,
                "exit_code": 0,
                "payload": {
                    "ok": True,
                    "provenance": {"source_url": "test://lorrgs/user-report-fights"},
                    "data": {
                        "fights": [
                            {
                                "fight_id": 22,
                                "duration": 5000,
                                "difficulty": 5,
                                "kill": True,
                                "percent": 0,
                                "start_time": "2026-06-18T00:00:00Z",
                                "phases": [{"ts": 1000}, {"ts": 3000}],
                                "boss": {"boss_slug": "lura", "casts": [{"id": 900001, "ts": 1500, "c": 1}]},
                                "players": [
                                    {
                                        "name": "Buikia",
                                        "source_id": 89,
                                        "class_slug": "warrior",
                                        "spec_slug": "warrior-protection",
                                        "total": 80591,
                                        "deaths": [],
                                    }
                                ],
                            }
                        ]
                    },
                },
            }
        if provider == "lorrgs" and args == ["spec-spells", "warrior-protection"]:
            return {
                "provider": provider,
                "exit_code": 0,
                "payload": {
                    "ok": True,
                    "provenance": {"source_url": "test://lorrgs/spec-spells"},
                    "data": {
                        "107574": {
                            "spell_id": 107574,
                            "name": "Avatar",
                            "event_type": "cast",
                            "spell_type": "warrior",
                            "cooldown": 90,
                            "duration": 20,
                            "query": True,
                            "show": False,
                            "tags": [],
                            "wowhead_data": "spell=107574",
                        },
                        "1160": {
                            "spell_id": 1160,
                            "name": "Demoralizing Shout",
                            "event_type": "cast",
                            "spell_type": "warrior-protection",
                            "cooldown": 45,
                            "duration": 8,
                            "query": True,
                            "show": False,
                            "tags": ["tank"],
                            "wowhead_data": "spell=1160",
                        },
                    },
                },
            }
        if provider == "lorrgs" and args == ["bosses"]:
            # Lorrgs boss ids are Warcraft Logs encounter ids.
            bosses = [{"id": 3001, "full_name_slug": "other-boss"}, {"id": 3183, "full_name_slug": "lura"}]
            return {"provider": provider, "exit_code": 0, "payload": _envelope({"bosses": bosses})}
        if provider == "lorrgs" and args == ["boss-spells", "lura"]:
            return {
                "provider": provider,
                "exit_code": 0,
                "payload": {
                    "ok": True,
                    "provenance": {"source_url": "test://lorrgs/boss-spells"},
                    "data": {
                        "900001": {
                            "spell_id": 900001,
                            "name": "Boss Event",
                            "duration": 3,
                            "cooldown": 0,
                            "query": False,
                            "show": True,
                        }
                    },
                },
            }
        if provider == "warcraftlogs" and args[:1] == ["report-fights"]:
            return {
                "provider": provider,
                "exit_code": 0,
                "payload": _envelope({
                    "ok": True,
                    "report": {"code": "abcd1234", "title": "Test Report"},
                    "fights": [
                        {"id": 22, "start_time": 100000, "end_time": 105000, "encounter_id": 3183, "kill": True,
                         "difficulty": wcl_fight_difficulty},
                    ],
                }),
            }
        if provider == "warcraftlogs" and args[:1] == ["report-player-details"]:
            return {"provider": provider, "exit_code": 0, "payload": _WCL_ROSTER}
        if provider == "warcraftlogs" and args[:1] == ["report-events"]:
            return {
                "provider": provider,
                "exit_code": 0,
                "payload": _envelope({
                    "ok": True,
                    "report": {"code": "abcd1234", "title": "Test Report"},
                    "next_page_timestamp": None,
                    "events": [
                        {"timestamp": 100500, "sourceID": 89, "abilityGameID": 107574, "targetID": -1, "type": "cast"},
                        {"timestamp": 101500, "sourceID": 89, "abilityGameID": 107574, "targetID": -1, "type": "cast"},
                        # Another raider's Avatar, and a spell the actor casts that is not tracked.
                        {"timestamp": 101600, "sourceID": 12, "abilityGameID": 107574, "targetID": -1, "type": "cast"},
                        {"timestamp": 101700, "sourceID": 89, "abilityGameID": 6673, "targetID": -1, "type": "cast"},
                        {"timestamp": 102500, "sourceID": 89, "abilityGameID": 1160, "targetID": -1, "type": "cast"},
                        {"timestamp": 103500, "sourceID": 89, "abilityGameID": 1160, "targetID": -1, "type": "cast"},
                    ],
                }),
            }
        if provider == "lorrgs" and args[:3] == ["spec-ranking", "warrior-protection", "lura"]:
            return {
                "provider": provider,
                "exit_code": 0,
                "payload": {
                    "ok": True,
                    "provenance": {"source_url": "test://lorrgs/spec-ranking"},
                    "data": {
                        "reports": [
                            {
                                "report_id": "toplog",
                                "region": "US",
                                "fights": [
                                    {
                                        "fight_id": 1,
                                        "duration": 6000,
                                        "phases": [{"ts": 1200}, {"ts": 3500}],
                                        "boss": {"boss_slug": "lura", "casts": [{"id": 900001, "ts": 1400, "c": 1}]},
                                        "players": [
                                            {
                                                "name": "Topwar",
                                                "source_id": 7,
                                                "spec_slug": "warrior-protection",
                                                "total": 100000,
                                                "casts": [
                                                    {"id": 1160, "ts": 3600, "c": 1},
                                                    {"id": 107574, "ts": 2000, "c": 2},
                                                    {"id": 107574, "ts": 1500, "c": 1},
                                                ],
                                            }
                                        ],
                                    }
                                ],
                            },
                            {
                                "report_id": "toplog-missing-phase",
                                "region": "EU",
                                "fights": [
                                    {
                                        "fight_id": 2,
                                        "duration": 6000,
                                        "phases": [],
                                        "boss": {"boss_slug": "lura", "casts": [{"id": 900001, "ts": 1400, "c": 1}]},
                                        "players": [
                                            {
                                                "name": "NoPhase",
                                                "source_id": 8,
                                                "spec_slug": "warrior-protection",
                                                "total": 99000,
                                                "casts": [{"id": 107574, "ts": 1500, "c": 1}],
                                            }
                                        ],
                                    },
                                    # Past --sample-limit 2, inside a report that was still being read.
                                    {
                                        "fight_id": 4,
                                        "duration": 6000,
                                        "phases": [{"ts": 1000}],
                                        "players": [{"name": "OverLimit", "source_id": 9, "casts": [{"id": 1160, "ts": 2000}]}],
                                    },
                                ],
                            },
                            {"report_id": "toplog-unread", "region": "KR", "fights": []},
                        ]
                    },
                },
            }
        raise AssertionError((provider, args, expansion))

    return fake_provider_invoke


def test_cooldown_packet_does_not_merge_a_different_singleton_lorrgs_fight(monkeypatch) -> None:
    calls: list[tuple[str, list[str]]] = []
    invoke = _cooldown_packet_invoke(calls)

    def mismatched_fight(provider, args, *, expansion=None):
        result = invoke(provider, args, expansion=expansion)
        if args[0] == "user-report-fights":
            result["payload"]["data"]["fights"][0]["fight_id"] = 999
        return result

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", mismatched_fight)
    result = runner.invoke(
        warcraft_app, [*_cooldown_packet_args(), "--sample-limit", "0", "--spec-slug", "warrior-protection"]
    )

    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)["data"]
    assert data["lorrgs"]["reason"] == "lorrgs_fight_not_found"
    assert data["fight"]["lorrgs"]["fight_id"] is None
    assert data["fight"]["warcraftlogs"]["id"] == 22
    assert data["phase"]["selected"] is None
    assert data["player"]["deaths"] is None
    assert ("warcraftlogs", ["report-player-details", "abcd1234", "--fight-id", "22"]) in calls


@pytest.mark.parametrize("location", ["data", "provenance"])
def test_cooldown_packet_preserves_partial_graphql_errors(monkeypatch, location: str) -> None:
    invoke = _cooldown_packet_invoke([])
    warnings = [{"message": "Partial cast lookup", "path": ["reportData", "report", "events"]}]

    def partial_events(provider, args, *, expansion=None):
        result = invoke(provider, args, expansion=expansion)
        if args[0] == "report-events":
            payload = result["payload"]
            payload.setdefault(location, {})["graphql_warnings"] = warnings
            payload["data"]["events"] = []
            payload["data"]["notes"] = ["Some upstream cast events could not be read."]
        return result

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", partial_events)
    result = runner.invoke(warcraft_app, _cooldown_packet_args())

    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)["data"]
    source = data["sources"]["warcraftlogs_report_events"]
    assert source["status"] == "partial"
    assert source["graphql_warnings"] == warnings
    assert source["notes"] == ["Some upstream cast events could not be read."]
    assert data["cooldowns"]["player_casts"]["complete"] is False
    assert data["comparison"]["status"] == "partial"
    assert data["comparison"]["reason"] == "incomplete_player_casts"
    assert any("partial GraphQL errors" in note for note in data["notes"])


def test_cooldown_packet_marks_paginated_casts_incomplete(monkeypatch) -> None:
    invoke = _cooldown_packet_invoke([])

    def paginated_events(provider, args, *, expansion=None):
        result = invoke(provider, args, expansion=expansion)
        if args[0] == "report-events":
            result["payload"]["data"]["next_page_timestamp"] = 103000
        return result

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", paginated_events)
    result = runner.invoke(warcraft_app, _cooldown_packet_args())

    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)["data"]
    assert data["cooldowns"]["player_casts"]["complete"] is False
    assert data["comparison"]["status"] == "partial"
    assert data["comparison"]["player_casts_complete"] is False


def test_cooldown_packet_marks_an_empty_ranking_as_no_samples(monkeypatch) -> None:
    invoke = _cooldown_packet_invoke([])

    def empty_ranking(provider, args, *, expansion=None):
        result = invoke(provider, args, expansion=expansion)
        if args[0] == "spec-ranking":
            result["payload"]["data"]["reports"] = []
        return result

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", empty_ranking)
    result = runner.invoke(warcraft_app, _cooldown_packet_args())

    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)["data"]
    assert data["comparison"]["status"] == "no_samples"
    assert data["comparison"]["reason"] == "no_top_parse_samples"
    assert data["comparison"]["sample_count"] == 0
    assert data["cooldowns"]["player_casts"]["complete"] is True


def test_cooldown_packet_combines_lorrgs_phase_data_with_warcraftlogs_casts(monkeypatch) -> None:
    calls: list[tuple[str, list[str]]] = []
    monkeypatch.setattr("warcraft_cli.main.provider_invoke", _cooldown_packet_invoke(calls))

    result = runner.invoke(
        warcraft_app,
        [
            "cooldown-packet",
            "https://www.warcraftlogs.com/reports/abcd1234?fight=22&type=damage-done",
            "--actor-id",
            "89",
            "--phase",
            "2",
            "--sample-limit",
            "2",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    _assert_wrapper_success_envelope(payload, command="cooldown-packet")
    assert payload["kind"] == "cooldown_packet"
    assert payload["query"]["report_code"] == "abcd1234"
    assert payload["query"]["report_type"] == "damage-done"
    assert payload["query"]["spec_slug"] == "warrior-protection"
    assert payload["query"]["boss_slug"] == "lura"
    assert payload["data"]["phase"]["selected"]["label"] == "P2"
    assert payload["data"]["phase"]["selected"]["start_ms"] == 1000
    assert payload["data"]["phase"]["selected"]["end_ms"] == 3000
    assert [cast["spell"]["name"] for cast in payload["data"]["cooldowns"]["player_casts"]["selected_phase_casts"]] == [
        "Avatar",
        "Demoralizing Shout",
    ]
    # Only the actor's own casts of tracked spells count: not another raider's Avatar, not Battle Shout.
    player_casts = payload["data"]["cooldowns"]["player_casts"]
    assert (player_casts["tracked_cast_count"], player_casts["other_source_cast_count"]) == (4, 1)
    assert payload["data"]["boss"]["selected_phase_casts"][0]["spell"]["name"] == "Boss Event"
    comparison = payload["data"]["comparison"]
    assert (comparison["sample_count"], comparison["available_report_count"]) == (2, 3)
    assert [cast["timestamp_ms"] for cast in comparison["samples"][0]["selected_phase_casts"]] == [1500, 2000]
    assert comparison["samples"][0]["phase_available"] is True
    assert comparison["samples"][1]["phase_available"] is False
    assert comparison["samples"][1]["phase_unavailable_reason"] == "top_parse_has_no_phase_markers"
    assert comparison["samples"][1]["selected_phase_casts"] == []
    # A spell counts once per sample that cast it; sample_fraction is over the samples with the phase.
    assert comparison["phase_sample_count"] == 1
    assert [
        (row["spell"]["name"], row["sample_count"], row["sample_fraction"], row["total_casts"])
        for row in comparison["selected_phase_spell_frequency"]
    ] == [("Avatar", 1, 1.0, 2)]
    assert any("Some top-parse samples have no window" in note for note in payload["data"]["notes"])
    assert ("lorrgs", ["user-report-fights", "https://www.warcraftlogs.com/reports/abcd1234?fight=22&type=damage-done", "--fight", "22", "--type", "damage-done"]) in calls
    assert ("warcraftlogs", ["report-events", "abcd1234", "--fight-id", "22", "--source-id", "89", "--data-type", "casts", "--limit", "5000"]) in calls
    # No --difficulty: the comparison uses the Warcraft Logs fight's own (5 = mythic).
    assert ("lorrgs", ["spec-ranking", "warrior-protection", "lura", "--difficulty", "mythic"]) in calls
    assert payload["query"]["difficulty"] == "mythic"


def test_cooldown_packet_does_not_compare_a_phase_with_a_marker_less_top_parse_fight(monkeypatch) -> None:
    """The marker-less top parse has one whole-fight window; it is not the player's P1 of a three-phase fight."""
    monkeypatch.setattr("warcraft_cli.main.provider_invoke", _cooldown_packet_invoke([]))

    result = runner.invoke(warcraft_app, [*_cooldown_packet_args()[:-1], "1", "--sample-limit", "2"])

    assert result.exit_code == 0, result.output
    samples = json.loads(result.stdout)["data"]["comparison"]["samples"]
    assert [(sample["phase_available"], sample["phase_unavailable_reason"]) for sample in samples] == [
        (True, None),
        (False, "top_parse_has_no_phase_markers"),
    ]
    assert samples[1]["phase_window"] is None
    assert samples[1]["selected_phase_casts"] == []


def test_cooldown_packet_says_so_when_no_top_parse_sample_has_the_phase(monkeypatch) -> None:
    """Lorrgs ranking fights with ``phases: null`` give no P2 window, so the comparison is not ``ready``."""
    invoke = _cooldown_packet_invoke([])

    def marker_less_ranking(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        response = invoke(provider, args, expansion=expansion)
        if args[:1] == ["spec-ranking"]:
            for report in response["payload"]["data"]["reports"]:  # type: ignore[index]
                for fight in report["fights"]:
                    fight["phases"] = None
        return response

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", marker_less_ranking)

    result = runner.invoke(warcraft_app, [*_cooldown_packet_args(), "--sample-limit", "2"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)["data"]
    comparison = data["comparison"]
    assert (comparison["status"], comparison["reason"]) == ("no_phase_data", "top_parse_has_no_phase_markers")
    assert (comparison["sample_count"], comparison["phase_sample_count"]) == (2, 0)
    assert comparison["selected_phase_spell_frequency"] == []
    assert any("No top-parse sample has a P2 window" in note for note in data["notes"])
    assert not any("Top-parse samples are comparison evidence" in note for note in data["notes"])


def _cooldown_invoke_with_fights(command: str, phases: list[dict[str, int]]) -> Callable[..., dict[str, object]]:
    """The cooldown-packet fixture with every fight of one lorrgs ``command`` given ``phases``."""
    invoke = _cooldown_packet_invoke([])

    def mutated(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        response = invoke(provider, args, expansion=expansion)
        if provider == "lorrgs" and args[:1] == [command]:
            data = response["payload"]["data"]  # type: ignore[index]
            for fight in data.get("fights") or [fight for report in data["reports"] for fight in report["fights"]]:
                fight["phases"] = phases
        return response

    return mutated


def test_cooldown_packet_names_the_reason_when_top_parses_end_before_the_phase(monkeypatch) -> None:
    """Top parses with markers but no third window are not called marker-less."""
    monkeypatch.setattr("warcraft_cli.main.provider_invoke", _cooldown_invoke_with_fights("spec-ranking", [{"ts": 1200}]))

    result = runner.invoke(warcraft_app, [*_cooldown_packet_args()[:-1], "3", "--sample-limit", "2"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)["data"]
    comparison = data["comparison"]
    assert (comparison["status"], comparison["reason"]) == ("no_phase_data", "phase_not_in_top_parse")
    assert not any("often carry no phase markers" in note for note in data["notes"])


def test_cooldown_packet_does_not_use_a_marker_less_top_parse_for_a_single_window_player_fight(monkeypatch) -> None:
    """A P1 wipe has one window, but another top parse shows the encounter has phases, so the
    marker-less top parse's whole fight is not its P1."""
    monkeypatch.setattr("warcraft_cli.main.provider_invoke", _cooldown_invoke_with_fights("user-report-fights", []))

    result = runner.invoke(warcraft_app, [*_cooldown_packet_args()[:-1], "1", "--sample-limit", "2"])

    assert result.exit_code == 0, result.output
    samples = json.loads(result.stdout)["data"]["comparison"]["samples"]
    assert [(sample["phase_available"], sample["phase_unavailable_reason"]) for sample in samples] == [
        (True, None),
        (False, "top_parse_has_no_phase_markers"),
    ]


def test_cooldown_packet_unavailable_comparison_reports_zero_phase_samples(monkeypatch) -> None:
    monkeypatch.setattr("warcraft_cli.main.provider_invoke", _cooldown_packet_invoke([]))

    result = runner.invoke(warcraft_app, [*_cooldown_packet_args(), "--sample-limit", "0"])

    assert result.exit_code == 0, result.output
    comparison = json.loads(result.stdout)["data"]["comparison"]
    assert (comparison["status"], comparison["phase_sample_count"]) == ("unavailable", 0)


def _cooldown_packet_args() -> list[str]:
    return ["cooldown-packet", "https://www.warcraftlogs.com/reports/abcd1234#fight=22", "--actor-id", "89", "--phase", "2"]


def _cooldown_invoke_with_power_infusion(*, player_casts_it: bool):
    """Lorrgs files Power Infusion as an external and a top parse holds one; the player may cast it too."""
    invoke = _cooldown_packet_invoke([])

    def with_power_infusion(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        response = invoke(provider, args, expansion=expansion)
        data = response["payload"]["data"]  # type: ignore[index]
        if args[:1] == ["spec-spells"]:
            data["10060"] = {"spell_id": 10060, "name": "Power Infusion", "event_type": "applybuff",
                             "spell_type": "other-externals", "query": True}
        if args[:1] == ["spec-ranking"]:
            data["reports"][0]["fights"][0]["players"][0]["casts"].append({"id": 10060, "ts": 1300})
        if args[:1] == ["report-events"] and player_casts_it:
            data["events"].append({"timestamp": 101800, "sourceID": 89, "abilityGameID": 10060, "targetID": 89, "type": "cast"})
        return response

    return with_power_infusion


def test_cooldown_packet_leaves_externals_the_player_did_not_cast_out_of_both_sides(monkeypatch) -> None:
    """Lorrgs records Power Infusion on top parses as a buff received; this player never cast it."""
    monkeypatch.setattr("warcraft_cli.main.provider_invoke", _cooldown_invoke_with_power_infusion(player_casts_it=False))

    result = runner.invoke(warcraft_app, [*_cooldown_packet_args(), "--sample-limit", "1"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)["data"]
    assert [spell["spell_id"] for spell in data["cooldowns"]["tracked_spells"]] == [1160, 107574]
    assert [spell["name"] for spell in data["cooldowns"]["received_auras"]] == ["Power Infusion"]
    frequency = data["comparison"]["selected_phase_spell_frequency"]
    assert [row["spell"]["name"] for row in frequency] == ["Avatar"]
    assert any("cooldowns.received_auras" in note for note in data["notes"])


def test_cooldown_packet_compares_an_external_the_player_cast(monkeypatch) -> None:
    """A priest's own Power Infusion is in their casts, so it is compared, not filed as received."""
    monkeypatch.setattr("warcraft_cli.main.provider_invoke", _cooldown_invoke_with_power_infusion(player_casts_it=True))

    result = runner.invoke(warcraft_app, [*_cooldown_packet_args(), "--sample-limit", "1"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)["data"]
    assert [spell["spell_id"] for spell in data["cooldowns"]["tracked_spells"]] == [1160, 10060, 107574]
    assert data["cooldowns"]["received_auras"] == []
    player_counts = {row["spell"]["name"]: row["count"] for row in data["cooldowns"]["player_casts"]["tracked_casts_by_spell"]}
    assert player_counts["Power Infusion"] == 1
    frequency = data["comparison"]["selected_phase_spell_frequency"]
    assert "Power Infusion" in [row["spell"]["name"] for row in frequency]


def test_cooldown_packet_keeps_a_requested_external_tracked(monkeypatch) -> None:
    monkeypatch.setattr("warcraft_cli.main.provider_invoke", _cooldown_invoke_with_power_infusion(player_casts_it=False))

    result = runner.invoke(warcraft_app, [*_cooldown_packet_args(), "--sample-limit", "1", "--spell-id", "10060"])

    assert result.exit_code == 0, result.output
    cooldowns = json.loads(result.stdout)["data"]["cooldowns"]
    assert [spell["spell_id"] for spell in cooldowns["tracked_spells"]] == [10060]
    assert cooldowns["received_auras"] == []


def test_cooldown_packet_does_not_compare_the_player_with_their_own_top_parse(monkeypatch) -> None:
    invoke = _cooldown_packet_invoke([])

    def ranking_with_the_analyzed_fight(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        response = invoke(provider, args, expansion=expansion)
        if args[:1] == ["spec-ranking"]:
            reports = response["payload"]["data"]["reports"]  # type: ignore[index]
            own = {"fight_id": 22, "duration": 5000, "phases": [{"ts": 1000}],
                   "players": [{"name": "Buikia", "source_id": 89, "casts": [{"id": 1160, "ts": 1500}]}]}
            reports.insert(0, {"report_id": "abcd1234", "region": "US", "fights": [own]})
        return response

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", ranking_with_the_analyzed_fight)

    result = runner.invoke(warcraft_app, [*_cooldown_packet_args(), "--sample-limit", "1"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)["data"]
    assert [(sample["report_id"], sample["fight_id"]) for sample in data["comparison"]["samples"]] == [("toplog", 1)]
    assert data["comparison"]["excluded_analyzed_fight"] is True
    assert any("compared with themselves" in note for note in data["notes"])


def test_cooldown_packet_tracks_only_the_explicit_spell_ids(monkeypatch) -> None:
    monkeypatch.setattr("warcraft_cli.main.provider_invoke", _cooldown_packet_invoke([]))

    result = runner.invoke(warcraft_app, [*_cooldown_packet_args(), "--spell-id", "1160"])

    assert result.exit_code == 0, result.output
    cooldowns = json.loads(result.stdout)["data"]["cooldowns"]
    assert [spell["spell_id"] for spell in cooldowns["tracked_spells"]] == [1160]
    assert cooldowns["player_casts"]["tracked_cast_count"] == 2


def test_cooldown_packet_rejects_a_spec_slug_of_another_class_than_the_player(monkeypatch) -> None:
    """Actor 89 is a Protection Warrior; comparing it with Shadow Priest top parses is a wrong answer."""
    monkeypatch.setattr("warcraft_cli.main.provider_invoke", _cooldown_packet_invoke([]))

    result = runner.invoke(warcraft_app, [*_cooldown_packet_args(), "--spec-slug", "priest-shadow"])

    assert result.exit_code == 2, result.output
    error = json.loads(result.stderr)["error"]
    assert error["code"] == "invalid_query"
    assert "priest" in error["message"] and "warrior" in error["message"]


def test_cooldown_packet_reads_the_spec_slug_case_insensitively(monkeypatch) -> None:
    """``WARRIOR-Protection`` names the player's own spec; it used to fail as another class's spec."""
    calls: list[tuple[str, list[str]]] = []
    monkeypatch.setattr("warcraft_cli.main.provider_invoke", _cooldown_packet_invoke(calls))

    result = runner.invoke(warcraft_app, [*_cooldown_packet_args(), "--spec-slug", "WARRIOR-Protection"])

    assert result.exit_code == 0, result.output
    assert ("lorrgs", ["spec-ranking", "warrior-protection", "lura", "--difficulty", "mythic"]) in calls


@pytest.mark.parametrize(
    ("spelling", "player_class", "slug"),
    [
        ("BeastMastery", "hunter", "hunter-beastmastery"),
        ("beast-mastery-hunter", "hunter", "hunter-beastmastery"),
        ("bm hunter", "hunter", "hunter-beastmastery"),
        ("death-knight-frost", "deathknight", "deathknight-frost"),
        ("Frost Death Knight", "deathknight", "deathknight-frost"),
        # A bare spec two classes share takes the player's class.
        ("frost", "deathknight", "deathknight-frost"),
        ("balance-druid", "druid", "druid-balance"),
        ("Balance Druid", "druid", "druid-balance"),
        ("boomkin", "druid", "druid-balance"),
    ],
)
def test_cooldown_packet_takes_any_providers_spec_spelling(monkeypatch, spelling: str, player_class: str, slug: str) -> None:
    """--spec-slug frost-death-knight used to fail as a "frost" class spec, and Frost Death Knight as a Lorrgs 404."""
    calls: list[tuple[str, list[str]]] = []
    # The fake fight's player is a Protection Warrior; here it plays ``player_class`` and ``slug``.
    invoke = _cooldown_packet_invoke([])

    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        calls.append((provider, args))
        result = invoke(provider, ["warrior-protection" if arg == slug else arg for arg in args], expansion=expansion)
        if args[:1] == ["user-report-fights"]:
            result["payload"]["data"]["fights"][0]["players"][0]["class_slug"] = player_class
        return result

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    result = runner.invoke(warcraft_app, [*_cooldown_packet_args(), "--spec-slug", spelling])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert (payload["query"]["spec_slug"], payload["data"]["player"]["spec_slug"]) == (slug, slug)
    assert ("lorrgs", ["spec-spells", slug]) in calls
    assert ("lorrgs", ["spec-ranking", slug, "lura", "--difficulty", "mythic"]) in calls


def test_cooldown_packet_compares_top_parses_at_the_fights_own_difficulty(monkeypatch) -> None:
    """A heroic kill is compared with heroic top parses, not with a mythic default."""
    calls: list[tuple[str, list[str]]] = []
    monkeypatch.setattr("warcraft_cli.main.provider_invoke", _cooldown_packet_invoke(calls, wcl_fight_difficulty=4))

    result = runner.invoke(warcraft_app, _cooldown_packet_args())

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert ("lorrgs", ["spec-ranking", "warrior-protection", "lura", "--difficulty", "heroic"]) in calls
    assert payload["query"]["difficulty"] == "heroic"
    assert payload["data"]["comparison"]["status"] == "ready"


# Warcraft Logs difficulty ids: 1 LFR, 3 Normal; None when the fight row carries none.
@pytest.mark.parametrize("fight_difficulty", [None, 1, 3])
def test_cooldown_packet_says_so_when_the_fight_difficulty_has_no_ranking(monkeypatch, fight_difficulty: int | None) -> None:
    """A fight whose difficulty Lorrgs does not rank gets no comparison and a note, not mythic samples."""
    calls: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(
        "warcraft_cli.main.provider_invoke", _cooldown_packet_invoke(calls, wcl_fight_difficulty=fight_difficulty)
    )

    result = runner.invoke(warcraft_app, _cooldown_packet_args())

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert not [args for provider, args in calls if args[:1] == ["spec-ranking"]]
    assert payload["query"]["difficulty"] is None
    assert (payload["data"]["comparison"]["status"], payload["data"]["comparison"]["reason"]) == ("unavailable", "unranked_difficulty")
    assert any(f"difficulty is {fight_difficulty!r}" in note and "--difficulty" in note for note in payload["data"]["notes"])


def test_cooldown_packet_names_a_disabled_comparison(monkeypatch) -> None:
    calls: list[tuple[str, list[str]]] = []
    monkeypatch.setattr("warcraft_cli.main.provider_invoke", _cooldown_packet_invoke(calls))

    result = runner.invoke(warcraft_app, [*_cooldown_packet_args(), "--sample-limit", "0"])

    assert result.exit_code == 0, result.output
    comparison = json.loads(result.stdout)["data"]["comparison"]
    assert (comparison["status"], comparison["reason"]) == ("unavailable", "disabled_by_sample_limit")
    assert not [args for _provider, args in calls if args[:1] == ["spec-ranking"]]


def test_cooldown_packet_without_a_fight_or_actor_is_a_usage_error(monkeypatch) -> None:
    assert runner.invoke(warcraft_app, ["cooldown-packet", "abcd1234", "--phase", "1"]).exit_code == 2

    def lorrgs_fight(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        assert provider == "lorrgs"
        players = [{"name": "Buikia", "source_id": 89, "spec_slug": "warrior-protection"}]
        return {"provider": provider, "exit_code": 0, "payload": _envelope({"fights": [{"fight_id": 22, "players": players}]})}

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", lorrgs_fight)
    result = runner.invoke(warcraft_app, ["cooldown-packet", "abcd1234", "--fight-id", "22", "--phase", "1"])
    assert result.exit_code == 2, result.output
    assert json.loads(result.stderr)["error"]["code"] == "missing_actor"


@pytest.mark.parametrize(
    ("actor_args", "code"), [(["--actor-name", "Missing"], "actor_name_not_found"), (["--actor-id", "7"], "actor_id_not_found")]
)
def test_cooldown_packet_reports_a_missing_actor_as_not_found(monkeypatch, actor_args: list[str], code: str) -> None:
    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        assert provider == "lorrgs"
        assert args[:1] == ["user-report-fights"]
        return {
            "provider": provider,
            "exit_code": 0,
            "payload": {
                "ok": True,
                "data": {
                    "fights": [
                        {
                            "fight_id": 22,
                            "duration": 5000,
                            "phases": [{"ts": 1000}],
                            "boss": {"boss_slug": "lura"},
                            "players": [{"name": "Buikia", "source_id": 89, "spec_slug": "warrior-protection"}],
                        }
                    ]
                },
            },
        }

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    result = runner.invoke(
        warcraft_app,
        ["cooldown-packet", "abcd1234", "--fight-id", "22", *actor_args, "--phase", "2"],
    )

    assert result.exit_code == 4
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == code
    assert payload["error"]["details"]["available_players"] == [
        {"name": "Buikia", "source_id": 89, "spec_slug": "warrior-protection", "class_slug": None}
    ]


def test_warcraft_resolve_brief_and_ranking_debug(monkeypatch) -> None:
    _stub_raiderio_profile_lookups(monkeypatch)
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.search_suggestions", lambda self, query: {"search": query, "results": []})
    monkeypatch.setattr("method_cli.main.MethodClient.sitemap_guides", lambda self: [])
    monkeypatch.setattr("icy_veins_cli.main.IcyVeinsClient.sitemap_guides", lambda self: [])
    monkeypatch.setattr("raiderio_cli.client.RaiderIOClient.search", lambda self, *, term, kind=None: {"matches": []})
    monkeypatch.setattr("warcraft_wiki_cli.main.WarcraftWikiClient.search_articles", lambda self, query, *, limit: (0, []))
    _stub_raiderio_guild_lookup(monkeypatch)

    result = runner.invoke(warcraft_app, ["resolve", "guild us illidan Liquid", "--brief", "--ranking-debug"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["providers"] == []
    assert payload["data"]["match"]["provider"] == "raiderio"
    assert payload["data"]["ranking_debug"][0]["provider"] == "raiderio"


def test_warcraft_passthrough_to_wowhead(monkeypatch) -> None:
    def fake_search(self, query: str):  # noqa: ANN001
        return {
            "search": query,
            "results": [
                {"type": 3, "id": 19019, "name": "Thunderfury", "typeName": "Item"},
            ],
        }

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.search_suggestions", fake_search)
    result = runner.invoke(warcraft_app, ["wowhead", "search", "thunderfury", "--limit", "1"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["query"] == "thunderfury"
    assert payload["data"]["results"][0]["name"] == "Thunderfury"


def test_warcraft_passthrough_to_wowhead_injects_global_expansion(monkeypatch) -> None:
    def fake_search(self, query: str):  # noqa: ANN001
        return {
            "search": query,
            "results": [
                {"type": 3, "id": 19019, "name": "Thunderfury", "typeName": "Item"},
            ],
        }

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.search_suggestions", fake_search)
    result = runner.invoke(warcraft_app, ["--expansion", "wotlk", "wowhead", "search", "thunderfury", "--limit", "1"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["data"]["expansion"] == "wotlk"


def test_warcraft_passthrough_rejects_unsupported_provider_expansion() -> None:
    result = runner.invoke(warcraft_app, ["--expansion", "wotlk", "method", "guide", "mistweaver-monk"])
    assert result.exit_code == 2

    payload = json.loads(result.output)
    assert payload["error"]["code"] == "unsupported_provider_expansion"
    assert payload["error"]["details"]["provider"] == "method"
    assert payload["error"]["details"]["requested_expansion"] == "wotlk"


def test_warcraft_passthrough_rejects_duplicate_wowhead_expansion() -> None:
    result = runner.invoke(
        warcraft_app,
        ["--expansion", "wotlk", "wowhead", "--expansion", "retail", "search", "thunderfury", "--limit", "1"],
    )
    assert result.exit_code == 2

    payload = json.loads(result.output)
    assert payload["error"]["code"] == "duplicate_expansion_argument"


def test_warcraft_passthrough_to_method(monkeypatch) -> None:
    def fake_fetch(self, guide_ref):  # noqa: ANN001
        return {
            "guide": {
                "slug": "mistweaver-monk",
                "page_url": "https://www.method.gg/guides/mistweaver-monk",
                "section_slug": "introduction",
                "section_title": "Introduction",
                "author": "Tincell",
                "last_updated": "Last Updated: 26th Feb, 2026",
                "patch": "Patch 12.0.1",
            },
            "page": {
                "title": "Method Mistweaver Monk Guide - Introduction - Midnight 12.0.1",
                "description": "Learn the Mistweaver Monk basics.",
                "canonical_url": "https://www.method.gg/guides/mistweaver-monk",
            },
            "navigation": [],
            "article": {"html": "<p>Intro</p>", "text": "Intro", "headings": [], "sections": []},
            "linked_entities": [],
        }

    monkeypatch.setattr("method_cli.main.MethodClient.fetch_guide_page", fake_fetch)
    result = runner.invoke(warcraft_app, ["method", "guide", "mistweaver-monk"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["data"]["guide"]["slug"] == "mistweaver-monk"
    assert payload["data"]["guide"]["author"] == "Tincell"


def test_warcraft_passthrough_to_icy_veins(monkeypatch) -> None:
    def fake_fetch(self, guide_ref):  # noqa: ANN001
        return {
            "guide": {
                "slug": "mistweaver-monk-pve-healing-guide",
                "page_url": "https://www.icy-veins.com/wow/mistweaver-monk-pve-healing-guide",
                "section_slug": "mistweaver-monk-pve-healing-guide",
                "section_title": "Mistweaver Monk Guide",
                "author": "Dhaubbs",
                "last_updated": "2026-03-05T05:19:00+00:00",
                "published_at": "2012-09-13T02:17:00+00:00",
            },
            "page": {
                "title": "Mistweaver Monk Healing Guide - Midnight (12.0.1)",
                "description": "This guide contains everything you need to know to be an excellent Mistweaver Monk.",
                "canonical_url": "https://www.icy-veins.com/wow/mistweaver-monk-pve-healing-guide",
                "page_type": "guides",
            },
            "navigation": [],
            "page_toc": [],
            "article": {"html": "<p>Intro</p>", "text": "Intro", "intro_text": "General Information", "headings": [], "sections": []},
            "linked_entities": [],
            "citations": {"page": "https://www.icy-veins.com/wow/mistweaver-monk-pve-healing-guide"},
        }

    monkeypatch.setattr("icy_veins_cli.main.IcyVeinsClient.fetch_guide_page", fake_fetch)
    result = runner.invoke(warcraft_app, ["icy-veins", "guide", "mistweaver-monk-pve-healing-guide"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["data"]["guide"]["slug"] == "mistweaver-monk-pve-healing-guide"
    assert payload["data"]["guide"]["author"] == "Dhaubbs"


def test_warcraft_passthrough_to_simc(monkeypatch, tmp_path) -> None:
    profile = tmp_path / "example.simc"
    profile.write_text('monk="example"\n')
    repo_root = tmp_path / "simc"
    (repo_root / "build").mkdir(parents=True)
    (repo_root / "build" / "simc").write_text("")

    monkeypatch.setattr(
        "simc_cli.main.run_profile",
        lambda paths, profile_path, simc_args: type("Result", (), {"command": [str(paths.build_simc), str(
            profile_path)], "returncode": 0, "stdout": "Iterations: 1\n", "stderr": ""})(),
    )
    monkeypatch.setattr(
        "simc_cli.main.binary_version",
        lambda paths: type("VersionInfo", (), {"binary_path": paths.build_simc, "available": True,
                           "version_line": "SimulationCraft 1201", "returncode": 1})(),
    )

    result = runner.invoke(warcraft_app, ["simc", "--repo-root", str(repo_root), "run", str(profile)])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["provider"] == "simc"
    assert payload["data"]["status"] == "completed"
    assert payload["data"]["version"] == "SimulationCraft 1201"


def test_warcraft_passthrough_to_simc_validate_talent_transport(monkeypatch) -> None:
    monkeypatch.setattr(
        "simc_cli.main.validate_talent_tree_transport",
        lambda **kwargs: {
            "transport_forms": {
                "simc_split_talents": {
                    "class_talents": "103324:1",
                    "spec_talents": "109839:1",
                    "hero_talents": None,
                }
            },
            "validation": {
                "status": "validated",
                "source": "simc_trait_data_round_trip",
                "actor_class": "druid",
                "spec": "balance",
            },
        },
    )

    result = runner.invoke(
        warcraft_app,
        [
            "simc",
            "validate-talent-transport",
            "--actor-class",
            "druid",
            "--spec",
            "balance",
            "--talent-row",
            "103324:82244:1",
            "--talent-row",
            "109839:88206:1",
        ],
    )
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["provider"] == "simc"
    assert payload["kind"] == "validate_talent_transport"
    assert payload["data"]["transport_status"] == "validated"
    assert payload["data"]["transport_forms"]["simc_split_talents"]["spec_talents"] == "109839:1"


def test_warcraft_packet_handoff_from_warcraftlogs_to_simc(monkeypatch, tmp_path: Path) -> None:
    raw_packet_path = tmp_path / "raw-packet.json"
    validated_packet_path = tmp_path / "validated-packet.json"

    class _PacketClient:
        _finished_report_ttl = 86400
        _report_ttl = 60
        _cache_store = object()  # non-None: provenance emits cache-on TTLs

        def report_player_details(self, **kwargs):  # noqa: ANN003, ANN201
            return {}

        def close(self) -> None:
            return None

    monkeypatch.setattr("warcraftlogs_cli.main._client", lambda ctx: _PacketClient())
    monkeypatch.setattr(
        "warcraftlogs_cli.main._resolve_encounter_scope",
        lambda ctx, *, client, reference, fight_id, allow_unlisted: (
            type("Ref", (), {"code": "abcd1234", "fight_id": 1, "source_url": None})(),
            {"code": "abcd1234", "title": "Test Report", "startTime": 1, "endTime": 2},
            {"id": 1, "encounterID": 3012, "name": "Dimensius", "kill": True, "difficulty": 5, "startTime": 0, "endTime": 1},
            {"id": 3012, "journalID": 3001, "name": "Dimensius", "zone": {
                "id": 38, "name": "Nerub-ar Palace", "expansion": {"id": 10, "name": "Retail"}}},
        ),
    )
    monkeypatch.setattr(
        "warcraftlogs_cli.main._report_player_details_payload",
        lambda payload, *, report_code=None, fight_id=None: {
            "player_details": {
                "roles": {
                    "dps": [
                        {
                            "id": 9,
                            "name": "gubkfc",
                            "class_spec_identity": {"identity": {"actor_class": "druid", "spec": "balance"}},
                            "combatant_info": {
                                "talentTree": [
                                    {"id": 103324, "nodeID": 82244, "rank": 1},
                                    {"id": 109839, "nodeID": 88206, "rank": 1},
                                ]
                            },
                        }
                    ]
                }
            }
        },
    )
    monkeypatch.setattr(
        "warcraftlogs_cli.main.validate_talent_tree_transport",
        lambda **kwargs: {
            "transport_forms": {},
            "validation": {
                "status": "not_validated",
                "reason": "simc_trait_resolution_incomplete",
            },
        },
    )
    monkeypatch.setattr(
        "simc_cli.main.validate_talent_tree_transport",
        lambda **kwargs: {
            "transport_forms": {
                "simc_split_talents": {
                    "class_talents": "103324:1",
                    "spec_talents": "109839:1",
                    "hero_talents": None,
                }
            },
            "validation": {
                "status": "validated",
                "source": "simc_trait_data_round_trip",
                "actor_class": "druid",
                "spec": "balance",
            },
        },
    )

    export_result = runner.invoke(
        warcraft_app,
        [
            "warcraftlogs",
            "report-player-talents",
            "abcd1234",
            "--fight-id",
            "1",
            "--actor-id",
            "9",
            "--out",
            str(raw_packet_path),
        ],
    )
    assert export_result.exit_code == 0
    export_payload = json.loads(export_result.stdout)
    assert export_payload["data"]["written_packet_path"] == str(raw_packet_path.resolve())

    validate_result = runner.invoke(
        warcraft_app,
        [
            "simc",
            "validate-talent-transport",
            "--build-packet",
            str(raw_packet_path),
            "--out",
            str(validated_packet_path),
        ],
    )
    assert validate_result.exit_code == 0
    validate_payload = json.loads(validate_result.stdout)
    assert validate_payload["data"]["transport_status"] == "validated"
    assert validate_payload["data"]["written_packet_path"] == str(validated_packet_path.resolve())

    written_packet = json.loads(validated_packet_path.read_text())
    assert written_packet["transport_status"] == "validated"
    assert written_packet["source"] == {"provider": "warcraftlogs", "source": "warcraftlogs_talent_tree"}
    assert written_packet["scope"] == {"type": "report_fight_actor", "report_code": "abcd1234", "fight_id": 1, "actor_id": 9}
    assert written_packet["transport_forms"]["simc_split_talents"]["spec_talents"] == "109839:1"


def test_warcraft_talent_packet_routes_explicit_wowhead_ref(monkeypatch) -> None:
    calls: list[tuple[str, list[str]]] = []

    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        calls.append((provider, args))
        assert provider == "wowhead"
        return {
            "provider": "wowhead",
            "exit_code": 0,
            "payload": _envelope({
                "provider": "wowhead",
                "kind": "talent_calc_packet",
                "talent_transport_packet": {
                    "kind": "talent_transport_packet",
                    "transport_status": "exact",
                    "transport_forms": {
                        "wowhead_talent_calc_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123",
                    },
                    "build_identity": {
                        "class_spec_identity": {"identity": {"actor_class": "druid", "spec": "balance"}},
                    },
                    "raw_evidence": {"reference_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123"},
                    "validation": {},
                    "scope": {"type": "wowhead_talent_calc", "expansion": "retail"},
                },
            }),
        }

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    result = runner.invoke(warcraft_app, ["talent-packet", "druid/balance/ABC123"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    _assert_wrapper_success_envelope(payload, command="talent-packet")
    assert payload["provider"] == "warcraft"
    assert payload["kind"] == "talent_transport"
    assert payload["data"]["route"] == {"kind": "wowhead_talent_calc", "provider": "wowhead"}
    assert payload["data"]["source_packet_status"] == "exact"
    assert payload["data"]["upgrade_attempted"] is False
    assert payload["data"]["upgraded"] is False
    assert payload["data"]["talent_transport_packet"]["transport_forms"]["wowhead_talent_calc_url"] == "https://www.wowhead.com/talent-calc/druid/balance/ABC123"
    assert calls == [("wowhead", ["talent-calc-packet", "druid/balance/ABC123", "--listed-build-limit", "10"])]


def test_warcraft_talent_packet_fails_on_a_broken_wowhead_cache_config(monkeypatch) -> None:
    """A bad cache setting is one failure envelope, not an error on stderr plus a success on stdout."""
    monkeypatch.setenv("WOWHEAD_PAGE_CACHE_TTL_SECONDS", "abc")

    result = runner.invoke(warcraft_app, ["talent-packet", "druid/balance/ABC123", "--no-validate"])
    assert result.exit_code == 1
    assert result.stdout == ""
    payload = json.loads(result.stderr)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_cache_config"


def test_warcraft_talent_packet_passes_wowhead_listed_build_limit_and_expansion(monkeypatch) -> None:
    calls: list[tuple[str, list[str], str | None]] = []

    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        calls.append((provider, args, expansion))
        return {
            "provider": "wowhead",
            "exit_code": 0,
            "payload": _envelope({
                "provider": "wowhead",
                "kind": "talent_calc_packet",
                "talent_transport_packet": {
                    "kind": "talent_transport_packet",
                    "transport_status": "exact",
                    "transport_forms": {
                        "wowhead_talent_calc_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123",
                    },
                    "build_identity": {
                        "class_spec_identity": {"identity": {"actor_class": "druid", "spec": "balance"}},
                    },
                    "raw_evidence": {"reference_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123"},
                    "validation": {},
                    "scope": {"type": "wowhead_talent_calc", "expansion": "wotlk"},
                },
            }),
        }

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    result = runner.invoke(
        warcraft_app,
        ["--expansion", "wotlk", "talent-packet", "druid/balance/ABC123", "--listed-build-limit", "3"],
    )
    assert result.exit_code == 0
    assert calls == [("wowhead", ["talent-calc-packet", "druid/balance/ABC123", "--listed-build-limit", "3"], "wotlk")]


def test_warcraft_talent_packet_routes_expansion_prefixed_wowhead_ref(monkeypatch) -> None:
    calls: list[tuple[str, list[str], str | None]] = []

    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        calls.append((provider, args, expansion))
        return {
            "provider": "wowhead",
            "exit_code": 0,
            "payload": _envelope({
                "provider": "wowhead",
                "kind": "talent_calc_packet",
                "talent_transport_packet": {
                    "kind": "talent_transport_packet",
                    "transport_status": "exact",
                    "transport_forms": {
                        "wowhead_talent_calc_url": "https://www.wowhead.com/cata/talent-calc/hunter/beast-mastery/XYZ987",
                    },
                    "build_identity": {
                        "class_spec_identity": {"identity": {"actor_class": "hunter", "spec": "beast_mastery"}},
                    },
                    "raw_evidence": {"reference_url": "https://www.wowhead.com/cata/talent-calc/hunter/beast-mastery/XYZ987"},
                    "validation": {},
                    "scope": {"type": "wowhead_talent_calc", "expansion": "retail"},
                },
            }),
        }

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    result = runner.invoke(
        warcraft_app,
        ["talent-packet", "cata/talent-calc/hunter/beast-mastery/XYZ987", "--no-validate"],
    )
    assert result.exit_code == 0
    assert calls == [
        ("wowhead", ["talent-calc-packet", "cata/talent-calc/hunter/beast-mastery/XYZ987", "--listed-build-limit", "10"], None)
    ]
    payload = json.loads(result.stdout)
    assert payload["data"]["route"] == {"kind": "wowhead_talent_calc", "provider": "wowhead"}


def test_warcraft_talent_packet_routes_expansion_prefixed_class_spec_wowhead_ref(monkeypatch) -> None:
    calls: list[tuple[str, list[str], str | None]] = []

    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        calls.append((provider, args, expansion))
        return {
            "provider": "wowhead",
            "exit_code": 0,
            "payload": _envelope({
                "provider": "wowhead",
                "kind": "talent_calc_packet",
                "talent_transport_packet": {
                    "kind": "talent_transport_packet",
                    "transport_status": "exact",
                    "transport_forms": {
                        "wowhead_talent_calc_url": "https://www.wowhead.com/classic/talent-calc/druid/balance/ABC123",
                    },
                    "build_identity": {
                        "class_spec_identity": {"identity": {"actor_class": "druid", "spec": "balance"}},
                    },
                    "raw_evidence": {"reference_url": "https://www.wowhead.com/classic/talent-calc/druid/balance/ABC123"},
                    "validation": {},
                    "scope": {"type": "wowhead_talent_calc", "expansion": "classic"},
                },
            }),
        }

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    result = runner.invoke(
        warcraft_app,
        ["talent-packet", "classic/druid/balance/ABC123", "--no-validate"],
    )
    assert result.exit_code == 0
    assert calls == [
        ("wowhead", ["talent-calc-packet", "classic/druid/balance/ABC123", "--listed-build-limit", "10"], None)
    ]
    payload = json.loads(result.stdout)
    assert payload["data"]["route"] == {"kind": "wowhead_talent_calc", "provider": "wowhead"}


def test_warcraft_talent_packet_passes_allow_unlisted_to_warcraftlogs(monkeypatch) -> None:
    calls: list[tuple[str, list[str], str | None]] = []

    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        calls.append((provider, args, expansion))
        return {
            "provider": "warcraftlogs",
            "exit_code": 0,
            "payload": _envelope({
                "provider": "warcraftlogs",
                "kind": "report_player_talents",
                "talent_transport_packet": {
                    "kind": "talent_transport_packet",
                    "transport_status": "raw_only",
                    "build_identity": {},
                    "transport_forms": {},
                    "raw_evidence": {"talent_tree_entries": [{"entry": 103324, "node_id": 82244, "rank": 1}]},
                    "validation": {"status": "not_validated"},
                    "scope": {"type": "report_fight_actor", "report_code": "abcd1234", "fight_id": 1, "actor_id": 9},
                },
            }),
        }

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    result = runner.invoke(
        warcraft_app,
        ["talent-packet", "abcd1234", "--actor-id", "9", "--fight-id", "1", "--allow-unlisted", "--no-validate"],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["route"]["allow_unlisted"] is True
    assert calls == [("warcraftlogs", ["report-player-talents", "abcd1234",
                      "--actor-id", "9", "--fight-id", "1", "--allow-unlisted"], None)]


def test_warcraft_talent_packet_routes_scheme_less_warcraftlogs_report_ref(monkeypatch) -> None:
    calls: list[tuple[str, list[str], str | None]] = []

    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        calls.append((provider, args, expansion))
        return {
            "provider": "warcraftlogs",
            "exit_code": 0,
            "payload": _envelope({
                "provider": "warcraftlogs",
                "kind": "report_player_talents",
                "talent_transport_packet": {
                    "kind": "talent_transport_packet",
                    "transport_status": "raw_only",
                    "build_identity": {},
                    "transport_forms": {},
                    "raw_evidence": {"talent_tree_entries": [{"entry": 103324, "node_id": 82244, "rank": 1}]},
                    "validation": {"status": "not_validated"},
                    "scope": {"type": "report_fight_actor", "report_code": "abcd1234", "fight_id": 1, "actor_id": 9},
                },
            }),
        }

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    result = runner.invoke(
        warcraft_app,
        ["talent-packet", "warcraftlogs.com/reports/abcd1234#fight=1", "--actor-id", "9", "--no-validate"],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["route"] == {
        "kind": "warcraftlogs_report_actor",
        "provider": "warcraftlogs",
        "actor_id": 9,
        "fight_id": None,
        "allow_unlisted": False,
    }
    assert calls == [
        (
            "warcraftlogs",
            ["report-player-talents", "https://warcraftlogs.com/reports/abcd1234#fight=1", "--actor-id", "9"],
            None,
        )
    ]


def test_warcraft_talent_packet_routes_alpha_only_warcraftlogs_report_code(monkeypatch) -> None:
    """A real report code can be letters only: 16 of them, mixing upper and lower case."""
    calls: list[tuple[str, list[str], str | None]] = []

    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        calls.append((provider, args, expansion))
        return {
            "provider": "warcraftlogs",
            "exit_code": 0,
            "payload": _envelope({
                "provider": "warcraftlogs",
                "kind": "report_player_talents",
                "talent_transport_packet": {
                    "kind": "talent_transport_packet",
                    "transport_status": "raw_only",
                    "build_identity": {},
                    "transport_forms": {},
                    "raw_evidence": {"talent_tree_entries": [{"entry": 103324, "node_id": 82244, "rank": 1}]},
                    "validation": {"status": "not_validated"},
                    "scope": {"type": "report_fight_actor", "report_code": "JVFTxcKCqrvpaAzD", "fight_id": 1, "actor_id": 9},
                },
            }),
        }

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    result = runner.invoke(
        warcraft_app,
        ["talent-packet", "JVFTxcKCqrvpaAzD", "--actor-id", "9", "--fight-id", "1", "--no-validate"],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["route"]["provider"] == "warcraftlogs"
    assert calls == [
        ("warcraftlogs", ["report-player-talents", "JVFTxcKCqrvpaAzD", "--actor-id", "9", "--fight-id", "1"], None)
    ]


def test_warcraft_talent_packet_routes_warcraftlogs_and_upgrades(monkeypatch) -> None:
    calls: list[tuple[str, list[str]]] = []

    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        calls.append((provider, args))
        if provider == "warcraftlogs":
            return {
                "provider": provider,
                "exit_code": 0,
                "payload": _envelope({
                    "provider": provider,
                    "kind": "report_player_talents",
                    "talent_transport_packet": {
                        "kind": "talent_transport_packet",
                        "transport_status": "raw_only",
                        "build_identity": {
                            "class_spec_identity": {
                                "identity": {"actor_class": "druid", "spec": "balance"},
                            }
                        },
                        "transport_forms": {},
                        "raw_evidence": {"talent_tree_entries": [{"entry": 103324, "node_id": 82244, "rank": 1}]},
                        "validation": {"status": "not_validated"},
                        "scope": {"type": "report_fight_actor", "report_code": "abcd1234", "fight_id": 1, "actor_id": 9},
                    },
                }),
            }
        raise AssertionError(f"unexpected provider call: {provider} {args}")

    simc_calls: list[dict[str, object]] = []

    def fake_simc_call(command: str, build: PacketInput | str, *, describe: DescribeOptions | None = None) -> dict[str, object]:
        simc_calls.append(_simc_call_summary(command, build, describe))
        assert isinstance(build, PacketInput)
        assert build.packet["transport_status"] == "raw_only"
        return _simc_result(_envelope({
            "provider": "simc",
            "kind": "validate_talent_transport",
            "input": {"source": "build_packet", "build_packet": build.path},
            "updated_packet": {
                **build.packet,
                "transport_status": "validated",
                "transport_forms": {"simc_split_talents": {"class_talents": "103324:1"}},
                "validation": {
                    "status": "validated",
                    "source": "simc_trait_data_round_trip",
                    "actor_class": "druid",
                    "spec": "balance",
                },
            },
        }))

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)
    monkeypatch.setattr("warcraft_cli.main.simc_call", fake_simc_call)

    result = runner.invoke(warcraft_app, ["talent-packet", "abcd1234", "--fight-id", "1", "--actor-id", "9"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["route"] == {
        "kind": "warcraftlogs_report_actor",
        "provider": "warcraftlogs",
        "actor_id": 9,
        "fight_id": 1,
        "allow_unlisted": False,
    }
    assert payload["data"]["source_packet_status"] == "raw_only"
    assert payload["data"]["upgrade_attempted"] is True
    assert payload["data"]["upgraded"] is True
    assert payload["data"]["talent_transport_packet"]["transport_status"] == "validated"
    assert payload["data"]["talent_transport_packet"]["transport_forms"]["simc_split_talents"]["class_talents"] == "103324:1"
    # The routed packet reached simc in memory, so the upgrade names no packet file.
    assert payload["data"]["upgrade_result"]["payload"]["data"]["input"]["build_packet"] is None
    assert calls == [("warcraftlogs", ["report-player-talents", "abcd1234", "--actor-id", "9", "--fight-id", "1"])]
    assert [(row["command"], row["packet_path"]) for row in simc_calls] == [("validate-talent-transport", None)]


def test_warcraft_talent_packet_preserves_missing_talent_tree_for_non_dict_rows(monkeypatch) -> None:
    monkeypatch.setattr("warcraftlogs_cli.main._client", lambda ctx: _EndToEndWarcraftLogsClient())
    monkeypatch.setattr(
        "warcraftlogs_cli.main._player_detail_actor",
        lambda details_payload, actor_id: {
            "id": actor_id,
            "combatant_info": {
                "talentTree": [
                    {"id": 103324, "nodeID": 82244, "rank": 1},
                    "bad-row",
                ]
            },
            "class_spec_identity": {
                "identity": {"actor_class": "paladin", "spec": "retribution"},
            },
        },
    )

    result = runner.invoke(
        warcraft_app,
        ["talent-packet", "abcd1234", "--fight-id", "1", "--actor-id", "9"],
    )
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "missing_talent_tree"
    assert "incomplete combatant_info.talentTree rows" in payload["error"]["message"]


def test_warcraft_talent_packet_upgrades_packet_file_and_writes_output(monkeypatch, tmp_path: Path) -> None:
    packet_path = tmp_path / "raw-packet.json"
    out_path = tmp_path / "validated-packet.json"
    packet_path.write_text(
        json.dumps(
            {
                "kind": "talent_transport_packet",
                "transport_status": "raw_only",
                "build_identity": {
                    "class_spec_identity": {
                        "identity": {"actor_class": "druid", "spec": "balance"},
                    }
                },
                "transport_forms": {},
                "raw_evidence": {"talent_tree_entries": [{"entry": 103324, "node_id": 82244, "rank": 1}]},
                "validation": {"status": "not_validated"},
                "scope": {"type": "report_fight_actor", "report_code": "abcd1234", "fight_id": 1, "actor_id": 9},
            }
        )
    )

    def fake_simc_call(command: str, build: PacketInput | str, *, describe: DescribeOptions | None = None) -> dict[str, object]:
        assert command == "validate-talent-transport"
        assert isinstance(build, PacketInput)
        assert build.packet["transport_status"] == "raw_only"
        return _simc_result(_envelope({
            "input": {"source": "build_packet", "build_packet": build.path},
            "updated_packet": {
                **build.packet,
                "transport_status": "validated",
                "transport_forms": {"simc_split_talents": {"class_talents": "103324:1"}},
                "validation": {
                    "status": "validated",
                    "source": "simc_trait_data_round_trip",
                    "actor_class": "druid",
                    "spec": "balance",
                },
            }
        }))

    monkeypatch.setattr("warcraft_cli.main.simc_call", fake_simc_call)

    result = runner.invoke(warcraft_app, ["talent-packet", str(packet_path), "--out", str(out_path)])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["route"] == {"kind": "packet_file", "provider": None, "packet_path": str(packet_path.resolve())}
    assert payload["data"]["written_packet_path"] == str(out_path.resolve())
    assert payload["data"]["talent_transport_packet"]["transport_status"] == "validated"
    # simc validated exactly the packet file's content, so the upgrade cites that file.
    assert payload["data"]["upgrade_result"]["payload"]["data"]["input"]["build_packet"] == str(packet_path.resolve())
    written = json.loads(out_path.read_text())
    assert written["transport_status"] == "validated"


def test_warcraft_talent_packet_reemits_unknown_packet_file_without_auto_upgrade(monkeypatch, tmp_path: Path) -> None:
    packet_path = tmp_path / "unknown-packet.json"
    out_path = tmp_path / "unknown-out.json"
    packet = {
        "kind": "talent_transport_packet",
        "transport_status": "unknown",
        "build_identity": {
            "class_spec_identity": {
                "identity": {"actor_class": "druid", "spec": "balance"},
            }
        },
        "transport_forms": {},
        "raw_evidence": {
            "talent_tree_entries": [{"entry": None, "node_id": None, "rank": None}],
        },
        "validation": {"status": "not_validated"},
        "scope": {"type": "report_fight_actor", "report_code": "abcd1234", "fight_id": 1, "actor_id": 9},
    }
    packet_path.write_text(json.dumps(packet))

    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        raise AssertionError(f"unexpected provider call: {provider} {args}")

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    result = runner.invoke(warcraft_app, ["talent-packet", str(packet_path), "--out", str(out_path)])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["source_packet_status"] == "unknown"
    assert payload["data"]["upgrade_attempted"] is False
    assert payload["data"]["upgraded"] is False
    assert payload["data"]["talent_transport_packet"] == packet
    assert json.loads(out_path.read_text()) == packet


def test_warcraft_talent_packet_requires_explicit_source_contract() -> None:
    result = runner.invoke(warcraft_app, ["talent-packet", "hello"])
    assert result.exit_code == 2
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "unsupported_talent_source"


@pytest.mark.parametrize("source", ["abcd1234", "www.warcraftlogs.com/reports/yZ4Qa682XYjRFDtT#fight=19"])
def test_warcraft_talent_packet_names_the_missing_actor_id_for_a_report_ref(source: str) -> None:
    """A scheme-less report URL used to fail as a missing packet file, and a code as an unsupported source."""
    result = runner.invoke(warcraft_app, ["talent-packet", source])
    assert result.exit_code == 2
    error = json.loads(result.stderr)["error"]
    assert error["code"] == "missing_actor_id"
    assert "report-player-details" in error["message"]




def test_warcraft_talent_packet_accepts_hyphenated_wowhead_class_slug(monkeypatch) -> None:
    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        assert provider == "wowhead"
        assert args == ["talent-calc-packet", "death-knight/frost/ABC123", "--listed-build-limit", "10"]
        return {
            "provider": provider,
            "exit_code": 0,
            "payload": _envelope({
                "talent_transport_packet": {
                    "kind": "talent_transport_packet",
                    "transport_status": "exact",
                    "build_identity": {
                        "class_spec_identity": {"identity": {"actor_class": "deathknight", "spec": "frost"}}
                    },
                    "transport_forms": {
                        "wowhead_talent_calc_url": "https://www.wowhead.com/talent-calc/death-knight/frost/ABC123"
                    },
                    "raw_evidence": {
                        "reference_url": "https://www.wowhead.com/talent-calc/death-knight/frost/ABC123"
                    },
                    "validation": {},
                    "scope": {},
                }
            }),
        }

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    result = runner.invoke(warcraft_app, ["talent-packet", "death-knight/frost/ABC123", "--no-validate"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["route"] == {"kind": "wowhead_talent_calc", "provider": "wowhead"}
    assert payload["data"]["talent_transport_packet"]["transport_status"] == "exact"


def test_warcraft_talent_packet_routes_a_wow_forever_calculator_ref_to_wowhead(monkeypatch) -> None:
    """``forever/<class>/<code>`` used to read as a missing packet file; WoW Forever has its own calculator."""
    calls: list[list[str]] = []

    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        calls.append([provider, *args])
        return {
            "provider": provider,
            "exit_code": 2,
            "payload": {"ok": False, "error": {"code": "invalid_tool_ref", "message": "talent-calc packet refs must name a spec"}},
        }

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    result = runner.invoke(warcraft_app, ["talent-packet", "forever/warrior/v205_t0", "--no-validate"])

    assert calls == [["wowhead", "talent-calc-packet", "forever/warrior/v205_t0", "--listed-build-limit", "10"]]
    assert json.loads(result.stderr)["error"]["details"]["route"] == {"kind": "wowhead_talent_calc", "provider": "wowhead"}


def test_warcraft_talent_packet_rejects_invalid_packet_file(tmp_path: Path) -> None:
    packet_path = tmp_path / "broken-packet.json"
    packet_path.write_text("{not json}\n")

    result = runner.invoke(warcraft_app, ["talent-packet", str(packet_path)])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_transport_packet"
    assert payload["error"]["details"]["source"] == str(packet_path)


def test_warcraft_talent_packet_rejects_missing_packet_path_like_input(tmp_path: Path) -> None:
    packet_path = tmp_path / "missing-packet.json"

    result = runner.invoke(warcraft_app, ["talent-packet", str(packet_path)])
    assert result.exit_code == 4
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "not_found"
    assert payload["error"]["message"] == f"Talent transport packet file was not found: {packet_path}"


def test_warcraft_talent_packet_rejects_missing_file_like_class_spec_json_path() -> None:
    result = runner.invoke(warcraft_app, ["talent-packet", "druid/balance/missing-packet.json"])
    assert result.exit_code == 4
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "not_found"
    assert payload["error"]["message"] == "Talent transport packet file was not found: druid/balance/missing-packet.json"


def test_warcraft_talent_packet_rejects_unscoped_relative_packet_like_input() -> None:
    result = runner.invoke(warcraft_app, ["talent-packet", "tmp/foo"])
    assert result.exit_code == 4
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "not_found"
    assert payload["error"]["message"] == "Talent transport packet file was not found: tmp/foo"



def test_warcraft_talent_packet_fails_when_provider_omits_packet(monkeypatch) -> None:
    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        assert provider == "wowhead"
        return {
            "provider": provider,
            "exit_code": 0,
            "payload": {"provider": provider, "kind": "talent_calc_packet"},
        }

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    result = runner.invoke(warcraft_app, ["talent-packet", "druid/balance/ABC123"])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "missing_transport_packet"
    assert payload["error"]["details"]["route"] == {"kind": "wowhead_talent_calc", "provider": "wowhead"}


def test_warcraft_talent_packet_preserves_wowhead_invalid_transport_packet_error(monkeypatch) -> None:
    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        assert provider == "wowhead"
        return {
            "provider": provider,
            "exit_code": 1,
            "payload": {
                "ok": False,
                "error": {
                    "code": "invalid_transport_packet",
                    "message": "wowhead talent-calc-packet produced an invalid talent transport packet: invalid test packet",
                },
            },
        }

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    result = runner.invoke(warcraft_app, ["talent-packet", "druid/balance/ABC123"])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_transport_packet"
    assert payload["error"]["message"] == "wowhead talent-calc-packet produced an invalid talent transport packet: invalid test packet"
    assert payload["error"]["details"]["route"] == {"kind": "wowhead_talent_calc", "provider": "wowhead"}
    assert payload["error"]["details"]["provider_result"]["provider"] == "wowhead"


def test_warcraft_talent_packet_preserves_warcraftlogs_invalid_transport_packet_error(monkeypatch) -> None:
    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        assert provider == "warcraftlogs"
        return {
            "provider": provider,
            "exit_code": 1,
            "payload": {
                "ok": False,
                "error": {
                    "code": "invalid_transport_packet",
                    "message": "warcraftlogs report-player-talents produced an invalid talent transport packet: invalid test packet",
                },
            },
        }

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    result = runner.invoke(warcraft_app, ["talent-packet", "abcd1234", "--fight-id", "1", "--actor-id", "9"])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_transport_packet"
    assert payload["error"]["message"] == "warcraftlogs report-player-talents produced an invalid talent transport packet: invalid test packet"
    assert payload["error"]["details"]["route"] == {
        "kind": "warcraftlogs_report_actor",
        "provider": "warcraftlogs",
        "actor_id": 9,
        "fight_id": 1,
        "allow_unlisted": False,
    }
    assert payload["error"]["details"]["provider_result"]["provider"] == "warcraftlogs"


def test_warcraft_talent_packet_preserves_provider_error_codes(monkeypatch) -> None:
    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        assert provider == "warcraftlogs"
        return {
            "provider": provider,
            "exit_code": 1,
            "payload": {
                "ok": False,
                "error": {
                    "code": "missing_public_auth",
                    "message": "Public Warcraft Logs API access requires client credentials.",
                },
            },
        }

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    result = runner.invoke(warcraft_app, ["talent-packet", "abcd1234", "--fight-id", "1", "--actor-id", "9"])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "missing_public_auth"
    assert payload["error"]["message"] == "Public Warcraft Logs API access requires client credentials."


def test_warcraft_talent_packet_preserves_warcraftlogs_not_found(monkeypatch) -> None:
    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        assert provider == "warcraftlogs"
        return {
            "provider": provider,
            "exit_code": 4,
            "payload": {
                "ok": False,
                "error": {
                    "code": "not_found",
                    "message": "Actor ID 999 was not present in the selected fight.",
                },
            },
        }

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    result = runner.invoke(warcraft_app, ["talent-packet", "abcd1234", "--fight-id", "1", "--actor-id", "999"])
    # The source's own code, with the exit code the contract maps it to, and the parsed input as query.
    assert result.exit_code == 4
    payload = json.loads(result.stderr)
    assert payload["kind"] == "error"
    assert payload["query"]["source"] == "abcd1234"
    assert payload["query"]["actor_id"] == 999
    assert payload["error"]["code"] == "not_found"
    assert payload["error"]["message"] == "Actor ID 999 was not present in the selected fight."
    assert payload["error"]["details"]["route"] == {
        "kind": "warcraftlogs_report_actor",
        "provider": "warcraftlogs",
        "actor_id": 999,
        "fight_id": 1,
        "allow_unlisted": False,
    }


@pytest.mark.parametrize(("code", "provider_exit"), [("missing_client_credentials", 3), ("missing_fight", 2)])
def test_warcraft_talent_packet_exits_with_the_providers_code_for_provider_specific_errors(
    monkeypatch, code: str, provider_exit: int
) -> None:
    # Provider-specific codes are not in the shared exit table, so only the provider's own exit is right.
    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        return {
            "provider": provider,
            "exit_code": provider_exit,
            "payload": {"ok": False, "provider": provider, "error": {"code": code, "message": "m"}},
        }

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    result = runner.invoke(warcraft_app, ["talent-packet", "abcd1234", "--fight-id", "1", "--actor-id", "9"])
    assert result.exit_code == provider_exit
    assert json.loads(result.stderr)["error"]["code"] == code


def test_warcraft_talent_packet_preserves_ok_false_provider_errors(monkeypatch) -> None:
    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        assert provider == "wowhead"
        return {
            "provider": provider,
            "exit_code": 0,
            "payload": {
                "ok": False,
                "error": {
                    "code": "invalid_query",
                    "message": "Buildless Wowhead ref cannot produce an exact packet.",
                },
            },
        }

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    result = runner.invoke(warcraft_app, ["talent-packet", "druid/balance/ABC123"])
    assert result.exit_code == 2
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_query"
    assert payload["error"]["message"] == "Buildless Wowhead ref cannot produce an exact packet."
    assert payload["error"]["details"]["route"] == {"kind": "wowhead_talent_calc", "provider": "wowhead"}


def test_warcraft_talent_packet_rejects_malformed_packet_status(tmp_path: Path) -> None:
    packet_path = tmp_path / "mismatched-status.json"
    packet_path.write_text(
        json.dumps(
            {
                "kind": "talent_transport_packet",
                "transport_status": "exact",
                "build_identity": {},
                "transport_forms": {},
                "raw_evidence": {"talent_tree_entries": [{"entry": 103324, "rank": 1}]},
                "validation": {},
                "scope": {},
            }
        )
    )

    result = runner.invoke(warcraft_app, ["talent-packet", str(packet_path)])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_transport_packet"
    assert "does not match packet contents" in payload["error"]["message"]


def test_warcraft_talent_packet_rejects_malformed_transport_forms(tmp_path: Path) -> None:
    packet_path = tmp_path / "bad-forms.json"
    packet_path.write_text(
        json.dumps(
            {
                "kind": "talent_transport_packet",
                "transport_status": "validated",
                "build_identity": {},
                "transport_forms": {"simc_split_talents": []},
                "raw_evidence": {"talent_tree_entries": [{"entry": 103324, "node_id": 82244, "rank": 1}]},
                "validation": {"status": "validated"},
                "scope": {},
            }
        )
    )

    result = runner.invoke(warcraft_app, ["talent-packet", str(packet_path)])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_transport_packet"
    assert "simc_split_talents" in payload["error"]["message"]


def test_warcraft_talent_packet_rejects_invalid_provider_packet(monkeypatch) -> None:
    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        assert provider == "wowhead"
        return {
            "provider": provider,
            "exit_code": 0,
            "payload": _envelope({
                "talent_transport_packet": {
                    "kind": "talent_transport_packet",
                    "transport_status": "validated",
                    "build_identity": {},
                    "transport_forms": {"wowhead_talent_calc_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123"},
                    "raw_evidence": {"reference_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123"},
                    "validation": {},
                    "scope": {},
                }
            }),
        }

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    result = runner.invoke(warcraft_app, ["talent-packet", "druid/balance/ABC123"])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_transport_packet"
    assert payload["error"]["details"]["route"] == {"kind": "wowhead_talent_calc", "provider": "wowhead"}
    assert payload["error"]["details"]["provider_result"]["provider"] == "wowhead"


def test_warcraft_talent_packet_rejects_invalid_upgraded_packet(monkeypatch, tmp_path: Path) -> None:
    packet_path = tmp_path / "raw-packet.json"
    packet_path.write_text(
        json.dumps(
            {
                "kind": "talent_transport_packet",
                "transport_status": "raw_only",
                "build_identity": {},
                "transport_forms": {},
                "raw_evidence": {"talent_tree_entries": [{"entry": 103324, "node_id": 82244, "rank": 1}]},
                "validation": {"status": "not_validated"},
                "scope": {"type": "report_fight_actor", "report_code": "abcd1234", "fight_id": 1, "actor_id": 9},
            }
        )
    )

    def fake_simc_call(command: str, build: PacketInput | str, *, describe: DescribeOptions | None = None) -> dict[str, object]:
        return {
            "provider": "simc",
            "exit_code": 0,
            "payload": _envelope({
                "updated_packet": {
                    "kind": "talent_transport_packet",
                    "transport_status": "validated",
                    "build_identity": {},
                    "transport_forms": {"wowhead_talent_calc_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123"},
                    "raw_evidence": {"reference_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123"},
                    "validation": {},
                    "scope": {},
                }
            }),
        }

    monkeypatch.setattr("warcraft_cli.main.simc_call", fake_simc_call)

    result = runner.invoke(warcraft_app, ["talent-packet", str(packet_path)])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "packet_upgrade_failed"
    assert "invalid upgraded packet" in payload["error"]["message"]


def test_warcraft_talent_packet_reports_upgrade_failure(monkeypatch, tmp_path: Path) -> None:
    packet_path = tmp_path / "raw-packet.json"
    packet_path.write_text(
        json.dumps(
            {
                "kind": "talent_transport_packet",
                "transport_status": "raw_only",
                "build_identity": {},
                "transport_forms": {},
                "raw_evidence": {"talent_tree_entries": [{"entry": 103324, "node_id": 82244, "rank": 1}]},
                "validation": {"status": "not_validated"},
                "scope": {"type": "report_fight_actor", "report_code": "abcd1234", "fight_id": 1, "actor_id": 9},
            }
        )
    )

    def fake_simc_call(command: str, build: PacketInput | str, *, describe: DescribeOptions | None = None) -> dict[str, object]:
        return {
            "provider": "simc",
            "exit_code": 1,
            "payload": {
                "ok": False,
                "error": {
                    "code": "invalid_build_packet",
                    "message": "Build packet did not contain a validated transport form.",
                },
            },
        }

    monkeypatch.setattr("warcraft_cli.main.simc_call", fake_simc_call)

    result = runner.invoke(warcraft_app, ["talent-packet", str(packet_path)])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_build_packet"
    assert payload["error"]["message"] == "Build packet did not contain a validated transport form."
    assert payload["error"]["details"]["route"] == {"kind": "packet_file", "provider": None, "packet_path": str(packet_path.resolve())}
    assert payload["error"]["details"]["provider_result"]["provider"] == "simc"


def test_warcraft_talent_packet_preserves_upgrade_failure_with_malformed_updated_packet(
    monkeypatch,
    tmp_path: Path,
) -> None:
    packet_path = tmp_path / "raw-packet.json"
    packet_path.write_text(
        json.dumps(
            {
                "kind": "talent_transport_packet",
                "transport_status": "raw_only",
                "build_identity": {},
                "transport_forms": {},
                "raw_evidence": {"talent_tree_entries": [{"entry": 103324, "node_id": 82244, "rank": 1}]},
                "validation": {"status": "not_validated"},
                "scope": {"type": "report_fight_actor", "report_code": "abcd1234", "fight_id": 1, "actor_id": 9},
            }
        )
    )

    def fake_simc_call(command: str, build: PacketInput | str, *, describe: DescribeOptions | None = None) -> dict[str, object]:
        return {
            "provider": "simc",
            "exit_code": 1,
            "payload": _envelope({
                "ok": False,
                "error": {
                    "code": "invalid_build_packet",
                    "message": "Build packet did not contain a validated transport form.",
                },
                "updated_packet": {
                    "kind": "talent_transport_packet",
                    "transport_status": "validated",
                    "build_identity": {},
                    "transport_forms": {"wowhead_talent_calc_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123"},
                    "raw_evidence": {"reference_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123"},
                    "validation": {},
                    "scope": {},
                },
            }),
        }

    monkeypatch.setattr("warcraft_cli.main.simc_call", fake_simc_call)

    result = runner.invoke(warcraft_app, ["talent-packet", str(packet_path)])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_build_packet"
    assert payload["error"]["message"] == "Build packet did not contain a validated transport form."
    assert payload["error"]["details"]["provider_result"]["provider"] == "simc"


def test_warcraft_talent_packet_rejects_successful_validate_without_updated_packet(
    monkeypatch,
    tmp_path: Path,
) -> None:
    packet_path = tmp_path / "raw-packet.json"
    packet_path.write_text(
        json.dumps(
            {
                "kind": "talent_transport_packet",
                "transport_status": "raw_only",
                "build_identity": {},
                "transport_forms": {},
                "raw_evidence": {"talent_tree_entries": [{"entry": 103324, "node_id": 82244, "rank": 1}]},
                "validation": {"status": "not_validated"},
                "scope": {"type": "report_fight_actor", "report_code": "abcd1234", "fight_id": 1, "actor_id": 9},
            }
        )
    )

    def fake_simc_call(command: str, build: PacketInput | str, *, describe: DescribeOptions | None = None) -> dict[str, object]:
        return {
            "provider": "simc",
            "exit_code": 0,
            "payload": {
                "provider": "simc",
                "kind": "validate_talent_transport",
            },
        }

    monkeypatch.setattr("warcraft_cli.main.simc_call", fake_simc_call)

    result = runner.invoke(warcraft_app, ["talent-packet", str(packet_path)])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "packet_upgrade_failed"
    assert payload["error"]["message"] == "simc validate-talent-transport did not return an upgraded talent transport packet."
    assert payload["error"]["details"]["provider_result"]["provider"] == "simc"



def test_warcraft_talent_describe_reports_simc_failure(monkeypatch, tmp_path: Path) -> None:
    packet_path = tmp_path / "exact-packet.json"
    apl_path = tmp_path / "balance.simc"
    apl_path.write_text("actions=wrath\n")
    packet_path.write_text(
        json.dumps(
            {
                "kind": "talent_transport_packet",
                "transport_status": "exact",
                "transport_forms": {
                    "wowhead_talent_calc_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123",
                },
                "build_identity": {
                    "class_spec_identity": {"identity": {"actor_class": "druid", "spec": "balance"}},
                },
                "raw_evidence": {"reference_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123"},
                "validation": {},
                "scope": {"type": "wowhead_talent_calc", "expansion": "retail"},
            }
        )
    )

    def fake_simc_call(command: str, build: PacketInput | str, *, describe: DescribeOptions | None = None) -> dict[str, object]:
        return {
            "provider": "simc",
            "exit_code": 1,
            "payload": {
                "ok": False,
                "error": {
                    "code": "apl_not_found",
                    "message": "APL path did not exist.",
                },
            },
        }

    monkeypatch.setattr("warcraft_cli.main.simc_call", fake_simc_call)

    result = runner.invoke(
        warcraft_app,
        ["talent-describe", str(packet_path), "--no-validate", "--apl-path", str(apl_path)],
    )
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "apl_not_found"
    assert payload["error"]["message"] == "APL path did not exist."
    assert payload["kind"] == "error"
    assert payload["error"]["details"]["route"] == {"kind": "packet_file", "provider": None, "packet_path": str(packet_path.resolve())}
    assert payload["error"]["details"]["provider_result"]["provider"] == "simc"


def test_warcraft_talent_describe_preserves_ok_false_simc_failure(monkeypatch, tmp_path: Path) -> None:
    packet_path = tmp_path / "exact-packet.json"
    apl_path = tmp_path / "balance.simc"
    apl_path.write_text("actions=wrath\n")
    packet_path.write_text(
        json.dumps(
            {
                "kind": "talent_transport_packet",
                "transport_status": "exact",
                "transport_forms": {
                    "wowhead_talent_calc_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123",
                },
                "build_identity": {
                    "class_spec_identity": {"identity": {"actor_class": "druid", "spec": "balance"}},
                },
                "raw_evidence": {"reference_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123"},
                "validation": {},
                "scope": {"type": "wowhead_talent_calc", "expansion": "retail"},
            }
        )
    )

    monkeypatch.setattr(
        "warcraft_cli.main.simc_call",
        lambda command, build, *, describe=None: _simc_result({
            "ok": False,
            "error": {
                "code": "describe_build_failed",
                "message": "Unable to resolve build against the supplied APL.",
            },
        }),
    )

    result = runner.invoke(
        warcraft_app,
        ["talent-describe", str(packet_path), "--no-validate", "--apl-path", str(apl_path)],
    )
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "describe_build_failed"
    assert payload["error"]["message"] == "Unable to resolve build against the supplied APL."
    assert payload["kind"] == "error"
    assert payload["error"]["details"]["provider_result"]["provider"] == "simc"


def test_warcraft_talent_describe_does_not_write_packet_out_on_failure(monkeypatch, tmp_path: Path) -> None:
    packet_path = tmp_path / "exact-packet.json"
    apl_path = tmp_path / "balance.simc"
    out_path = tmp_path / "described-packet.json"
    apl_path.write_text("actions=wrath\n")
    packet_path.write_text(
        json.dumps(
            {
                "kind": "talent_transport_packet",
                "transport_status": "exact",
                "transport_forms": {
                    "wowhead_talent_calc_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123",
                },
                "build_identity": {
                    "class_spec_identity": {"identity": {"actor_class": "druid", "spec": "balance"}},
                },
                "raw_evidence": {"reference_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123"},
                "validation": {},
                "scope": {"type": "wowhead_talent_calc", "expansion": "retail"},
            }
        )
    )

    monkeypatch.setattr(
        "warcraft_cli.main.simc_call",
        lambda command, build, *, describe=None: _simc_result(
            {"ok": False, "error": {"code": "apl_not_found", "message": "APL path did not exist."}}, exit_code=1
        ),
    )

    result = runner.invoke(
        warcraft_app,
        [
            "talent-describe",
            str(packet_path),
            "--no-validate",
            "--apl-path",
            str(apl_path),
            "--packet-out",
            str(out_path),
        ],
    )
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "apl_not_found"
    assert not out_path.exists()


def test_warcraft_talent_describe_route_failures_are_error_kind() -> None:
    result = runner.invoke(warcraft_app, ["talent-describe", "hello"])
    assert result.exit_code == 2
    payload = json.loads(result.stderr)
    assert payload["kind"] == "error"
    assert payload["error"]["code"] == "unsupported_talent_source"




def test_warcraft_talent_packet_reads_a_camel_case_name_as_no_report_code(monkeypatch) -> None:
    """With --actor-id, a bare word routes to Warcraft Logs only when the providers read it as a report code."""

    def no_call(*args: object, **kwargs: object) -> dict[str, object]:
        raise AssertionError("a name was sent to Warcraft Logs as a report code")

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", no_call)

    result = runner.invoke(warcraft_app, ["talent-packet", "HavocDemonHunter", "--actor-id", "1", "--no-validate"])

    assert result.exit_code == 2
    assert json.loads(result.stderr)["error"]["code"] == "unsupported_talent_source"


def test_warcraft_talent_describe_rejects_empty_segment_wowhead_ref(tmp_path: Path) -> None:
    apl_path = tmp_path / "balance.simc"
    apl_path.write_text("actions=wrath\n")

    result = runner.invoke(
        warcraft_app,
        ["talent-describe", "druid//balance/ABC123", "--apl-path", str(apl_path)],
    )
    assert result.exit_code == 2
    payload = json.loads(result.stderr)
    assert payload["kind"] == "error"
    assert payload["error"]["code"] == "invalid_tool_ref"
    assert payload["error"]["message"] == "talent-calc reference must not include empty path segments."


def test_warcraft_talent_describe_rejects_wowhead_ref_with_trailing_extra_segment(tmp_path: Path) -> None:
    apl_path = tmp_path / "balance.simc"
    apl_path.write_text("actions=wrath\n")

    result = runner.invoke(
        warcraft_app,
        ["talent-describe", "https://www.wowhead.com/talent-calc/druid/balance/ABC123/extra", "--apl-path", str(apl_path)],
    )
    assert result.exit_code == 2
    payload = json.loads(result.stderr)
    assert payload["kind"] == "error"
    assert payload["error"]["code"] == "invalid_tool_ref"
    assert payload["error"]["message"] == _WOWHEAD_TALENT_CALC_LAYOUT_MESSAGE




def test_warcraft_talent_packet_normalizes_packet_write_failure(monkeypatch, tmp_path: Path) -> None:
    out_dir = tmp_path / "out-dir"
    out_dir.mkdir()
    monkeypatch.setattr(
        "warcraft_cli.main.provider_invoke",
        lambda provider, args, *, expansion=None: {
            "provider": provider,
            "exit_code": 0,
            "payload": _envelope({
                "provider": "wowhead",
                "talent_transport_packet": {
                    "kind": "talent_transport_packet",
                    "transport_status": "exact",
                    "transport_forms": {
                        "wowhead_talent_calc_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123",
                    },
                    "build_identity": {
                        "class_spec_identity": {"identity": {"actor_class": "druid", "spec": "balance"}},
                    },
                    "raw_evidence": {"reference_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123"},
                    "validation": {},
                    "scope": {"type": "wowhead_talent_calc", "expansion": "retail"},
                },
            }),
        },
    )

    result = runner.invoke(warcraft_app, ["talent-packet", "druid/balance/ABC123", "--out", str(out_dir)])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "transport_packet_write_failed"



def test_warcraft_talent_describe_routes_wowhead_ref_to_simc(monkeypatch) -> None:
    provider_calls: list[tuple[str, list[str]]] = []
    simc_calls: list[dict[str, object]] = []

    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        provider_calls.append((provider, args))
        if provider == "wowhead":
            return {
                "provider": provider,
                "exit_code": 0,
                "payload": _envelope({
                    "provider": provider,
                    "kind": "talent_calc_packet",
                    "talent_transport_packet": {
                        "kind": "talent_transport_packet",
                        "transport_status": "exact",
                        "transport_forms": {
                            "wowhead_talent_calc_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123",
                        },
                        "build_identity": {
                            "class_spec_identity": {"identity": {"actor_class": "druid", "spec": "balance"}},
                        },
                        "raw_evidence": {"reference_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123"},
                        "validation": {},
                        "scope": {"type": "wowhead_talent_calc", "expansion": "retail"},
                    },
                }),
            }
        raise AssertionError(f"unexpected provider call: {provider} {args}")

    def fake_simc_call(command: str, build: PacketInput | str, *, describe: DescribeOptions | None = None) -> dict[str, object]:
        simc_calls.append(_simc_call_summary(command, build, describe))
        return _simc_result(_envelope({"provider": "simc", "kind": "describe_build", "summary": {"active_action_count": 5}}))

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)
    monkeypatch.setattr("warcraft_cli.main.simc_call", fake_simc_call)

    result = runner.invoke(
        warcraft_app,
        ["talent-describe", "druid/balance/ABC123", "--apl-path", "/tmp/druid_balance.simc"],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    _assert_wrapper_success_envelope(payload, command="talent-describe")
    assert payload["provider"] == "warcraft"
    assert payload["kind"] == "talent_describe"
    assert payload["data"]["route"] == {"kind": "wowhead_talent_calc", "provider": "wowhead"}
    assert payload["data"]["source_packet_status"] == "exact"
    assert payload["data"]["upgrade_attempted"] is False
    assert payload["data"]["written_packet_path"] is None
    assert payload["data"]["describe_result"]["payload"]["kind"] == "describe_build"
    assert provider_calls[0] == ("wowhead", ["talent-calc-packet", "druid/balance/ABC123", "--listed-build-limit", "10"])
    assert simc_calls[0]["command"] == "describe-build"
    assert simc_calls[0]["packet_transport_status"] == "exact"
    assert simc_calls[0]["packet_transport_url"] == "https://www.wowhead.com/talent-calc/druid/balance/ABC123"
    # A routed packet never touched disk, so simc is told it has no file to cite.
    assert simc_calls[0]["packet_path"] is None
    assert simc_calls[0]["describe"] == DescribeOptions(apl_path="/tmp/druid_balance.simc")


def test_warcraft_talent_describe_passes_wowhead_listed_build_limit(monkeypatch) -> None:
    provider_calls: list[tuple[str, list[str], str | None]] = []

    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        provider_calls.append((provider, args, expansion))
        if provider == "wowhead":
            return {
                "provider": provider,
                "exit_code": 0,
                "payload": _envelope({
                    "provider": provider,
                    "kind": "talent_calc_packet",
                    "talent_transport_packet": {
                        "kind": "talent_transport_packet",
                        "transport_status": "exact",
                        "transport_forms": {
                            "wowhead_talent_calc_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123",
                        },
                        "build_identity": {
                            "class_spec_identity": {"identity": {"actor_class": "druid", "spec": "balance"}},
                        },
                        "raw_evidence": {"reference_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123"},
                        "validation": {},
                        "scope": {"type": "wowhead_talent_calc", "expansion": "retail"},
                    },
                }),
            }
        raise AssertionError(f"unexpected provider call: {provider} {args}")

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)
    monkeypatch.setattr(
        "warcraft_cli.main.simc_call",
        lambda command, build, *, describe=None: _simc_result(
            _envelope({"provider": "simc", "kind": "describe_build", "summary": {"active_action_count": 3}})
        ),
    )

    result = runner.invoke(
        warcraft_app,
        ["talent-describe", "druid/balance/ABC123", "--listed-build-limit", "4"],
    )
    assert result.exit_code == 0
    assert provider_calls[0] == ("wowhead", ["talent-calc-packet", "druid/balance/ABC123", "--listed-build-limit", "4"], None)


def test_warcraft_talent_describe_passes_expansion_to_wowhead_route(monkeypatch) -> None:
    provider_calls: list[tuple[str, list[str], str | None]] = []

    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        provider_calls.append((provider, args, expansion))
        if provider == "wowhead":
            return {
                "provider": provider,
                "exit_code": 0,
                "payload": _envelope({
                    "provider": provider,
                    "kind": "talent_calc_packet",
                    "talent_transport_packet": {
                        "kind": "talent_transport_packet",
                        "transport_status": "exact",
                        "transport_forms": {
                            "wowhead_talent_calc_url": "https://www.wowhead.com/cata/talent-calc/hunter/beast-mastery/XYZ987",
                        },
                        "build_identity": {
                            "class_spec_identity": {"identity": {"actor_class": "hunter", "spec": "beast_mastery"}},
                        },
                        "raw_evidence": {"reference_url": "https://www.wowhead.com/cata/talent-calc/hunter/beast-mastery/XYZ987"},
                        "validation": {},
                        "scope": {"type": "wowhead_talent_calc", "expansion": "cata"},
                    },
                }),
            }
        raise AssertionError(f"unexpected provider call: {provider} {args}")

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)
    monkeypatch.setattr(
        "warcraft_cli.main.simc_call",
        lambda command, build, *, describe=None: _simc_result(
            _envelope({"provider": "simc", "kind": "describe_build", "summary": {"active_action_count": 3}})
        ),
    )

    result = runner.invoke(
        warcraft_app,
        ["--expansion", "wotlk", "talent-describe", "cata/talent-calc/hunter/beast-mastery/XYZ987"],
    )
    assert result.exit_code == 0
    assert provider_calls[0] == (
        "wowhead",
        ["talent-calc-packet", "cata/talent-calc/hunter/beast-mastery/XYZ987", "--listed-build-limit", "10"],
        "wotlk",
    )




def test_warcraft_talent_describe_passes_allow_unlisted_and_expansion(monkeypatch) -> None:
    provider_calls: list[tuple[str, list[str], str | None]] = []

    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        provider_calls.append((provider, args, expansion))
        if provider == "warcraftlogs":
            return {
                "provider": provider,
                "exit_code": 0,
                "payload": _envelope({
                    "provider": provider,
                    "kind": "report_player_talents",
                    "talent_transport_packet": {
                        "kind": "talent_transport_packet",
                        "transport_status": "exact",
                        "build_identity": {},
                        "transport_forms": {"wow_talent_export": "ABC123"},
                        "raw_evidence": {"reference_type": "wow_talent_export"},
                        "validation": {},
                        "scope": {"type": "report_fight_actor", "report_code": "abcd1234", "fight_id": 1, "actor_id": 9},
                    },
                }),
            }
        raise AssertionError(f"unexpected provider call: {provider} {args}")

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)
    monkeypatch.setattr(
        "warcraft_cli.main.simc_call",
        lambda command, build, *, describe=None: _simc_result(
            _envelope({"provider": "simc", "kind": "describe_build", "summary": {"active_action_count": 6}})
        ),
    )

    result = runner.invoke(
        warcraft_app,
        ["--expansion", "retail", "talent-describe", "abcd1234", "--actor-id", "9", "--fight-id", "1", "--allow-unlisted", "--no-validate"],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["route"]["allow_unlisted"] is True
    assert provider_calls[0] == (
        "warcraftlogs",
        ["report-player-talents", "abcd1234", "--actor-id", "9", "--fight-id", "1", "--allow-unlisted"],
        "retail",
    )



def test_warcraft_talent_describe_uses_packet_file_and_can_write_output(monkeypatch, tmp_path: Path) -> None:
    packet_path = tmp_path / "exact-packet.json"
    out_path = tmp_path / "described-packet.json"
    packet_path.write_text(
        json.dumps(
            {
                "kind": "talent_transport_packet",
                "transport_status": "exact",
                "transport_forms": {
                    "wowhead_talent_calc_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123",
                },
                "build_identity": {
                    "class_spec_identity": {"identity": {"actor_class": "druid", "spec": "balance"}},
                },
                "raw_evidence": {"reference_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123"},
                "validation": {},
                "scope": {"type": "wowhead_talent_calc", "expansion": "retail"},
            }
        )
    )
    simc_calls: list[dict[str, object]] = []

    def fake_simc_call(command: str, build: PacketInput | str, *, describe: DescribeOptions | None = None) -> dict[str, object]:
        simc_calls.append(_simc_call_summary(command, build, describe))
        return {
            "provider": "simc",
            "exit_code": 0,
            "payload": _envelope({
                "provider": "simc",
                "kind": "describe_build",
                "summary": {"active_action_count": 4},
            }),
        }

    monkeypatch.setattr("warcraft_cli.main.simc_call", fake_simc_call)

    result = runner.invoke(
        warcraft_app,
        ["talent-describe", str(packet_path), "--packet-out", str(out_path)],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["route"] == {"kind": "packet_file", "provider": None, "packet_path": str(packet_path.resolve())}
    assert payload["data"]["written_packet_path"] == str(out_path.resolve())
    assert payload["data"]["describe_result"]["payload"]["kind"] == "describe_build"
    assert simc_calls[0]["command"] == "describe-build"
    assert simc_calls[0]["packet_transport_status"] == "exact"
    written = json.loads(out_path.read_text())
    assert written["transport_status"] == "exact"


def test_warcraft_talent_describe_treats_blank_apl_path_as_none(monkeypatch, tmp_path: Path) -> None:
    packet_path = tmp_path / "exact-packet.json"
    packet_path.write_text(
        json.dumps(
            {
                "kind": "talent_transport_packet",
                "transport_status": "exact",
                "transport_forms": {"wowhead_talent_calc_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123"},
                "build_identity": {"class_spec_identity": {"identity": {"actor_class": "druid", "spec": "balance"}}},
                "raw_evidence": {},
                "validation": {},
                "scope": {"type": "wowhead_talent_calc", "expansion": "retail"},
            }
        )
    )
    simc_calls: list[dict[str, object]] = []

    def fake_simc_call(command: str, build: PacketInput | str, *, describe: DescribeOptions | None = None) -> dict[str, object]:
        simc_calls.append(_simc_call_summary(command, build, describe))
        return _simc_result(_envelope({"provider": "simc", "kind": "describe_build"}))

    monkeypatch.setattr("warcraft_cli.main.simc_call", fake_simc_call)

    result = runner.invoke(warcraft_app, ["talent-describe", str(packet_path), "--apl-path", "  "])

    assert result.exit_code == 0, result.stdout
    describe = simc_calls[0]["describe"]
    assert isinstance(describe, DescribeOptions)
    assert describe.apl_path is None


def test_warcraft_talent_describe_skips_auto_upgrade_for_unknown_packet_file(monkeypatch, tmp_path: Path) -> None:
    packet_path = tmp_path / "unknown-packet.json"
    apl_path = tmp_path / "druid_balance.simc"
    apl_path.write_text("actions=wrath\n")
    packet = {
        "kind": "talent_transport_packet",
        "transport_status": "unknown",
        "build_identity": {
            "class_spec_identity": {
                "identity": {"actor_class": "druid", "spec": "balance"},
            }
        },
        "transport_forms": {},
        "raw_evidence": {
            "talent_tree_entries": [{"entry": None, "node_id": None, "rank": None}],
        },
        "validation": {"status": "not_validated"},
        "scope": {"type": "report_fight_actor", "report_code": "abcd1234", "fight_id": 1, "actor_id": 9},
    }
    packet_path.write_text(json.dumps(packet))
    simc_calls: list[dict[str, object]] = []

    def fake_simc_call(command: str, build: PacketInput | str, *, describe: DescribeOptions | None = None) -> dict[str, object]:
        simc_calls.append(_simc_call_summary(command, build, describe))
        assert command == "describe-build"
        return {
            "provider": "simc",
            "exit_code": 0,
            "payload": _envelope({
                "provider": "simc",
                "kind": "describe_build",
                "summary": {"active_action_count": 4},
            }),
        }

    monkeypatch.setattr("warcraft_cli.main.simc_call", fake_simc_call)

    result = runner.invoke(
        warcraft_app,
        ["talent-describe", str(packet_path), "--apl-path", str(apl_path)],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["source_packet_status"] == "unknown"
    assert payload["data"]["upgrade_attempted"] is False
    assert payload["data"]["upgraded"] is False
    assert payload["data"]["talent_transport_packet"] == packet
    assert [row["command"] for row in simc_calls] == ["describe-build"]


def test_warcraft_talent_packet_preserves_wowhead_provider_packet(monkeypatch) -> None:
    _disable_wowhead_page_fetch(monkeypatch)

    direct_result = runner.invoke(wowhead_app, ["talent-calc-packet", "druid/balance/ABC123"])
    assert direct_result.exit_code == 0
    direct_payload = json.loads(direct_result.stdout)

    wrapper_result = runner.invoke(warcraft_app, ["talent-packet", "druid/balance/ABC123"])
    assert wrapper_result.exit_code == 0
    wrapper_payload = json.loads(wrapper_result.stdout)

    assert wrapper_payload["data"]["route"] == {"kind": "wowhead_talent_calc", "provider": "wowhead"}
    assert wrapper_payload["data"]["talent_transport_packet"] == direct_payload["data"]["talent_transport_packet"]
    assert wrapper_payload["data"]["talent_transport_packet"]["transport_status"] == "exact"


def test_warcraft_talent_packet_out_matches_wowhead_provider_file(monkeypatch, tmp_path: Path) -> None:
    direct_path = tmp_path / "wowhead-direct.json"
    wrapper_path = tmp_path / "wowhead-wrapper.json"

    _disable_wowhead_page_fetch(monkeypatch)

    direct_result = runner.invoke(wowhead_app, ["talent-calc-packet", "druid/balance/ABC123", "--out", str(direct_path)])
    assert direct_result.exit_code == 0
    wrapper_result = runner.invoke(warcraft_app, ["talent-packet", "druid/balance/ABC123", "--out", str(wrapper_path)])
    assert wrapper_result.exit_code == 0

    assert direct_path.read_text() == wrapper_path.read_text()


def test_warcraft_talent_packet_preserves_warcraftlogs_provider_packet(monkeypatch) -> None:
    monkeypatch.setattr("warcraftlogs_cli.main._client", lambda ctx: _EndToEndWarcraftLogsClient())
    monkeypatch.setattr(
        "warcraftlogs_cli.main.validate_talent_tree_transport",
        lambda **kwargs: {
            "transport_forms": {
                "simc_split_talents": {
                    "class_talents": "103324:1",
                    "spec_talents": "109839:1",
                    "hero_talents": "117176:1",
                }
            },
            "validation": {
                "status": "validated",
                "source": "simc_trait_data_round_trip",
                "actor_class": "paladin",
                "spec": "retribution",
            },
        },
    )

    direct_result = runner.invoke(
        warcraftlogs_app,
        ["report-player-talents", "abcd1234", "--fight-id", "1", "--actor-id", "9"],
    )
    assert direct_result.exit_code == 0
    direct_payload = json.loads(direct_result.stdout)

    wrapper_result = runner.invoke(
        warcraft_app,
        ["talent-packet", "abcd1234", "--fight-id", "1", "--actor-id", "9"],
    )
    assert wrapper_result.exit_code == 0
    wrapper_payload = json.loads(wrapper_result.stdout)

    assert wrapper_payload["data"]["route"] == {
        "kind": "warcraftlogs_report_actor",
        "provider": "warcraftlogs",
        "actor_id": 9,
        "fight_id": 1,
        "allow_unlisted": False,
    }
    assert wrapper_payload["data"]["upgrade_attempted"] is False
    assert wrapper_payload["data"]["talent_transport_packet"] == direct_payload["data"]["talent_transport_packet"]
    assert wrapper_payload["data"]["talent_transport_packet"]["transport_status"] == "validated"


def test_warcraft_talent_packet_out_matches_warcraftlogs_provider_file(monkeypatch, tmp_path: Path) -> None:
    direct_path = tmp_path / "warcraftlogs-direct.json"
    wrapper_path = tmp_path / "warcraftlogs-wrapper.json"

    monkeypatch.setattr("warcraftlogs_cli.main._client", lambda ctx: _EndToEndWarcraftLogsClient())
    monkeypatch.setattr(
        "warcraftlogs_cli.main.validate_talent_tree_transport",
        lambda **kwargs: {
            "transport_forms": {
                "simc_split_talents": {
                    "class_talents": "103324:1",
                    "spec_talents": "109839:1",
                    "hero_talents": "117176:1",
                }
            },
            "validation": {
                "status": "validated",
                "source": "simc_trait_data_round_trip",
                "actor_class": "paladin",
                "spec": "retribution",
            },
        },
    )

    direct_result = runner.invoke(
        warcraftlogs_app,
        ["report-player-talents", "abcd1234", "--fight-id", "1", "--actor-id", "9", "--out", str(direct_path)],
    )
    assert direct_result.exit_code == 0
    wrapper_result = runner.invoke(
        warcraft_app,
        ["talent-packet", "abcd1234", "--fight-id", "1", "--actor-id", "9", "--out", str(wrapper_path)],
    )
    assert wrapper_result.exit_code == 0

    assert direct_path.read_text() == wrapper_path.read_text()


def test_warcraft_talent_describe_packet_out_matches_wowhead_provider_file(monkeypatch, tmp_path: Path) -> None:
    direct_path = tmp_path / "wowhead-direct.json"
    wrapper_path = tmp_path / "wowhead-described.json"
    apl_path = tmp_path / "druid_balance.simc"
    apl_path.write_text("actions=wrath\n")

    _disable_wowhead_page_fetch(monkeypatch)
    _patch_simc_describe_pipeline(
        monkeypatch,
        transport_form="wowhead_talent_calc_url",
        transport_status="exact",
    )

    direct_result = runner.invoke(wowhead_app, ["talent-calc-packet", "druid/balance/ABC123", "--out", str(direct_path)])
    assert direct_result.exit_code == 0
    wrapper_result = runner.invoke(
        warcraft_app,
        ["talent-describe", "druid/balance/ABC123", "--apl-path", str(apl_path), "--packet-out", str(wrapper_path)],
    )
    assert wrapper_result.exit_code == 0

    assert direct_path.read_text() == wrapper_path.read_text()


def test_warcraft_talent_describe_packet_out_changes_after_validation_upgrade(monkeypatch, tmp_path: Path) -> None:
    direct_path = tmp_path / "warcraftlogs-direct.json"
    wrapper_path = tmp_path / "warcraftlogs-described.json"
    apl_path = tmp_path / "druid_balance.simc"
    apl_path.write_text("actions=wrath\n")

    monkeypatch.setattr("warcraftlogs_cli.main._client", lambda ctx: _EndToEndWarcraftLogsClient())
    monkeypatch.setattr(
        "warcraftlogs_cli.main.validate_talent_tree_transport",
        lambda **kwargs: {
            "transport_forms": {},
            "validation": {
                "status": "not_validated",
                "reason": "simc_trait_resolution_incomplete",
            },
        },
    )
    monkeypatch.setattr(
        "simc_cli.main.validate_talent_tree_transport",
        lambda **kwargs: {
            "transport_forms": {
                "simc_split_talents": {
                    "class_talents": "103324:1",
                    "spec_talents": "109839:1",
                    "hero_talents": "117176:1",
                }
            },
            "validation": {
                "status": "validated",
                "source": "simc_trait_data_round_trip",
                "actor_class": "paladin",
                "spec": "retribution",
            },
        },
    )
    _patch_simc_describe_pipeline(
        monkeypatch,
        transport_form="simc_split_talents",
        transport_status="validated",
    )

    direct_result = runner.invoke(
        warcraftlogs_app,
        ["report-player-talents", "abcd1234", "--fight-id", "1", "--actor-id", "9", "--out", str(direct_path)],
    )
    assert direct_result.exit_code == 0
    wrapper_result = runner.invoke(
        warcraft_app,
        ["talent-describe", "abcd1234", "--fight-id", "1", "--actor-id", "9",
            "--apl-path", str(apl_path), "--packet-out", str(wrapper_path)],
    )
    assert wrapper_result.exit_code == 0
    wrapper_payload = json.loads(wrapper_result.stdout)

    direct_packet = json.loads(direct_path.read_text())
    wrapper_packet = json.loads(wrapper_path.read_text())
    assert direct_path.read_text() != wrapper_path.read_text()
    assert direct_packet["transport_status"] == "raw_only"
    assert wrapper_packet["transport_status"] == "validated"
    assert wrapper_packet["transport_forms"]["simc_split_talents"]["spec_talents"] == "109839:1"
    # The routed packet reached simc in memory; only the --packet-out file is cited, once written.
    assert wrapper_payload["data"]["upgrade_result"]["payload"]["data"]["input"]["build_packet"] is None
    # Provider results carry the parsed envelope only, never the provider's raw output a second time.
    assert set(wrapper_payload["data"]["upgrade_result"]) == {"provider", "exit_code", "payload"}
    assert set(wrapper_payload["data"]["producer_result"]) == {"provider", "exit_code", "payload"}
    assert wrapper_payload["data"]["describe_result"]["payload"]["data"]["build_spec"]["transport_packet"]["path"] == str(wrapper_path.resolve())


def test_warcraft_talent_packet_file_reuse_stays_exact_without_validation(monkeypatch, tmp_path: Path) -> None:
    source_path = tmp_path / "wowhead-source.json"
    routed_path = tmp_path / "wowhead-routed.json"
    described_path = tmp_path / "wowhead-described.json"
    apl_path = tmp_path / "druid_balance.simc"
    apl_path.write_text("actions=wrath\n")

    _disable_wowhead_page_fetch(monkeypatch)
    _patch_simc_describe_pipeline(
        monkeypatch,
        transport_form="wowhead_talent_calc_url",
        transport_status="exact",
    )

    producer_result = runner.invoke(
        wowhead_app,
        ["talent-calc-packet", "druid/balance/ABC123", "--out", str(source_path)],
    )
    assert producer_result.exit_code == 0

    packet_result = runner.invoke(
        warcraft_app,
        ["talent-packet", str(source_path), "--no-validate", "--out", str(routed_path)],
    )
    assert packet_result.exit_code == 0
    describe_result = runner.invoke(
        warcraft_app,
        [
            "talent-describe",
            str(source_path),
            "--no-validate",
            "--apl-path",
            str(apl_path),
            "--packet-out",
            str(described_path),
        ],
    )
    assert describe_result.exit_code == 0

    packet_payload = json.loads(packet_result.stdout)
    describe_payload = json.loads(describe_result.stdout)
    assert packet_payload["data"]["upgrade_attempted"] is False
    assert describe_payload["data"]["upgrade_attempted"] is False
    assert source_path.read_text() == routed_path.read_text()
    assert source_path.read_text() == described_path.read_text()


def test_warcraft_talent_packet_file_reuse_upgrades_raw_packet_consistently(monkeypatch, tmp_path: Path) -> None:
    raw_path = tmp_path / "warcraftlogs-raw.json"
    routed_path = tmp_path / "warcraftlogs-routed.json"
    described_path = tmp_path / "warcraftlogs-described.json"
    apl_path = tmp_path / "druid_balance.simc"
    apl_path.write_text("actions=wrath\n")

    monkeypatch.setattr("warcraftlogs_cli.main._client", lambda ctx: _EndToEndWarcraftLogsClient())
    monkeypatch.setattr(
        "warcraftlogs_cli.main.validate_talent_tree_transport",
        lambda **kwargs: {
            "transport_forms": {},
            "validation": {
                "status": "not_validated",
                "reason": "simc_trait_resolution_incomplete",
            },
        },
    )
    raw_result = runner.invoke(
        warcraftlogs_app,
        ["report-player-talents", "abcd1234", "--fight-id", "1", "--actor-id", "9", "--out", str(raw_path)],
    )
    assert raw_result.exit_code == 0

    monkeypatch.setattr(
        "simc_cli.main.validate_talent_tree_transport",
        lambda **kwargs: {
            "transport_forms": {
                "simc_split_talents": {
                    "class_talents": "103324:1",
                    "spec_talents": "109839:1",
                    "hero_talents": "117176:1",
                }
            },
            "validation": {
                "status": "validated",
                "source": "simc_trait_data_round_trip",
                "actor_class": "paladin",
                "spec": "retribution",
            },
        },
    )
    _patch_simc_describe_pipeline(
        monkeypatch,
        transport_form="simc_split_talents",
        transport_status="validated",
    )

    packet_result = runner.invoke(
        warcraft_app,
        ["talent-packet", str(raw_path), "--out", str(routed_path)],
    )
    assert packet_result.exit_code == 0
    describe_result = runner.invoke(
        warcraft_app,
        [
            "talent-describe",
            str(raw_path),
            "--apl-path",
            str(apl_path),
            "--packet-out",
            str(described_path),
        ],
    )
    assert describe_result.exit_code == 0

    packet_payload = json.loads(packet_result.stdout)
    describe_payload = json.loads(describe_result.stdout)
    raw_packet = json.loads(raw_path.read_text())
    routed_packet = json.loads(routed_path.read_text())
    described_packet = json.loads(described_path.read_text())

    assert packet_payload["data"]["upgrade_attempted"] is True
    assert describe_payload["data"]["upgrade_attempted"] is True
    assert raw_packet["transport_status"] == "raw_only"
    assert routed_packet == described_packet
    assert routed_packet["transport_status"] == "validated"
    assert raw_path.read_text() != routed_path.read_text()


def test_warcraft_talent_round_trip_wowhead_packet_to_describe(monkeypatch, tmp_path: Path) -> None:
    packet_path = tmp_path / "wowhead-packet.json"
    apl_path = tmp_path / "druid_balance.simc"
    apl_path.write_text("actions=wrath\n")
    _disable_wowhead_page_fetch(monkeypatch)
    _patch_simc_describe_pipeline(
        monkeypatch,
        transport_form="wowhead_talent_calc_url",
        transport_status="exact",
    )

    packet_result = runner.invoke(
        warcraft_app,
        ["talent-packet", "druid/balance/ABC123", "--out", str(packet_path)],
    )
    assert packet_result.exit_code == 0
    packet_payload = json.loads(packet_result.stdout)
    assert packet_payload["data"]["route"] == {"kind": "wowhead_talent_calc", "provider": "wowhead"}
    written_packet = json.loads(packet_path.read_text())
    assert written_packet["transport_status"] == "exact"
    assert written_packet["transport_forms"]["wowhead_talent_calc_url"] == "https://www.wowhead.com/talent-calc/druid/balance/ABC123"

    describe_result = runner.invoke(
        warcraft_app,
        ["talent-describe", str(packet_path), "--apl-path", str(apl_path)],
    )
    assert describe_result.exit_code == 0
    payload = json.loads(describe_result.stdout)
    assert payload["data"]["route"] == {"kind": "packet_file", "provider": None, "packet_path": str(packet_path.resolve())}
    transport_packet = payload["data"]["describe_result"]["payload"]["data"]["build_spec"]["transport_packet"]
    assert transport_packet["transport_form"] == "wowhead_talent_calc_url"
    assert transport_packet["transport_status"] == "exact"
    assert transport_packet["path"] == str(packet_path.resolve())


def test_warcraft_talent_round_trip_warcraftlogs_packet_to_describe(monkeypatch, tmp_path: Path) -> None:
    packet_path = tmp_path / "warcraftlogs-packet.json"
    apl_path = tmp_path / "druid_balance.simc"
    apl_path.write_text("actions=wrath\n")
    monkeypatch.setattr("warcraftlogs_cli.main._client", lambda ctx: _EndToEndWarcraftLogsClient())
    monkeypatch.setattr(
        "warcraftlogs_cli.main.validate_talent_tree_transport",
        lambda **kwargs: {
            "transport_forms": {},
            "validation": {
                "status": "not_validated",
                "reason": "simc_trait_resolution_incomplete",
            },
        },
    )
    monkeypatch.setattr(
        "simc_cli.main.validate_talent_tree_transport",
        lambda **kwargs: {
            "transport_forms": {
                "simc_split_talents": {
                    "class_talents": "103324:1",
                    "spec_talents": "109839:1",
                    "hero_talents": "117176:1",
                }
            },
            "validation": {
                "status": "validated",
                "source": "simc_trait_data_round_trip",
                "actor_class": "paladin",
                "spec": "retribution",
            },
        },
    )
    _patch_simc_describe_pipeline(
        monkeypatch,
        transport_form="simc_split_talents",
        transport_status="validated",
    )

    packet_result = runner.invoke(
        warcraft_app,
        ["talent-packet", "abcd1234", "--fight-id", "1", "--actor-id", "9", "--out", str(packet_path)],
    )
    assert packet_result.exit_code == 0
    packet_payload = json.loads(packet_result.stdout)
    assert packet_payload["data"]["route"] == {
        "kind": "warcraftlogs_report_actor",
        "provider": "warcraftlogs",
        "actor_id": 9,
        "fight_id": 1,
        "allow_unlisted": False,
    }
    assert packet_payload["data"]["source_packet_status"] == "raw_only"
    assert packet_payload["data"]["talent_transport_packet"]["transport_status"] == "validated"
    written_packet = json.loads(packet_path.read_text())
    assert written_packet["transport_forms"]["simc_split_talents"]["spec_talents"] == "109839:1"

    describe_result = runner.invoke(
        warcraft_app,
        ["talent-describe", str(packet_path), "--apl-path", str(apl_path)],
    )
    assert describe_result.exit_code == 0
    payload = json.loads(describe_result.stdout)
    assert payload["data"]["route"] == {"kind": "packet_file", "provider": None, "packet_path": str(packet_path.resolve())}
    assert payload["data"]["talent_transport_packet"]["transport_status"] == "validated"
    transport_packet = payload["data"]["describe_result"]["payload"]["data"]["build_spec"]["transport_packet"]
    assert transport_packet["transport_form"] == "simc_split_talents"
    assert transport_packet["transport_status"] == "validated"
    assert transport_packet["path"] == str(packet_path.resolve())


def test_warcraft_talent_describe_hides_stale_packet_path_after_in_memory_upgrade(monkeypatch, tmp_path: Path) -> None:
    packet_path = tmp_path / "raw-packet.json"
    apl_path = tmp_path / "druid_balance.simc"
    apl_path.write_text("actions=wrath\n")
    packet_path.write_text(
        json.dumps(
            {
                "kind": "talent_transport_packet",
                "transport_status": "raw_only",
                "transport_forms": {},
                "build_identity": {
                    "class_spec_identity": {"identity": {"actor_class": "druid", "spec": "balance"}},
                },
                "raw_evidence": {"talent_tree_entries": [{"entry": 103324, "node_id": 82244, "rank": 1}]},
                "validation": {"status": "not_validated"},
                "scope": {"type": "report_fight_actor", "report_code": "abcd1234", "fight_id": 1, "actor_id": 9},
            }
        )
    )

    _patch_simc_describe_pipeline(
        monkeypatch,
        transport_form="simc_split_talents",
        transport_status="validated",
    )
    monkeypatch.setattr(
        "simc_cli.main.validate_talent_tree_transport",
        lambda **kwargs: {
            "transport_forms": {
                "simc_split_talents": {
                    "class_talents": "103324:1",
                    "spec_talents": "109839:1",
                    "hero_talents": "117176:1",
                }
            },
            "validation": {
                "status": "validated",
                "source": "simc_trait_data_round_trip",
                "actor_class": "druid",
                "spec": "balance",
            },
        },
    )

    result = runner.invoke(
        warcraft_app,
        ["talent-describe", str(packet_path), "--apl-path", str(apl_path)],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    transport_packet = payload["data"]["describe_result"]["payload"]["data"]["build_spec"]["transport_packet"]
    assert transport_packet["transport_form"] == "simc_split_talents"
    assert transport_packet["transport_status"] == "validated"
    assert "path" not in transport_packet


def test_warcraft_talent_describe_preserves_packet_path_after_raw_only_refresh(
    monkeypatch,
    tmp_path: Path,
) -> None:
    packet_path = tmp_path / "raw-packet.json"
    apl_path = tmp_path / "druid_balance.simc"
    apl_path.write_text("actions=wrath\n")
    packet_path.write_text(
        json.dumps(
            {
                "kind": "talent_transport_packet",
                "transport_status": "raw_only",
                "transport_forms": {},
                "build_identity": {
                    "class_spec_identity": {"identity": {"actor_class": "druid", "spec": "balance"}},
                },
                "raw_evidence": {"talent_tree_entries": [{"entry": 103324, "node_id": 82244, "rank": 1}]},
                "validation": {"status": "not_validated"},
                "scope": {"type": "report_fight_actor", "report_code": "abcd1234", "fight_id": 1, "actor_id": 9},
            }
        )
    )

    _patch_simc_describe_pipeline(
        monkeypatch,
        transport_form="talent_transport_packet",
        transport_status="raw_only",
    )
    monkeypatch.setattr(
        "simc_cli.main.validate_talent_tree_transport",
        lambda **kwargs: {
            "transport_forms": {},
            "validation": {
                "status": "raw_only",
                "reason": "unresolved_talent_entries",
                "source": "simc_trait_data_round_trip",
            },
        },
    )

    result = runner.invoke(
        warcraft_app,
        ["talent-describe", str(packet_path), "--apl-path", str(apl_path)],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["source_packet_status"] == "raw_only"
    assert payload["data"]["upgraded"] is False
    assert payload["data"]["talent_transport_packet"]["transport_status"] == "raw_only"
    assert payload["data"]["talent_transport_packet"]["validation"] == {
        "status": "raw_only",
        "reason": "unresolved_talent_entries",
        "source": "simc_trait_data_round_trip",
    }
    transport_packet = payload["data"]["describe_result"]["payload"]["data"]["build_spec"]["transport_packet"]
    assert transport_packet["transport_form"] == "talent_transport_packet"
    assert transport_packet["transport_status"] == "raw_only"
    assert transport_packet["path"] == str(packet_path.resolve())


def test_warcraft_talent_packet_rejects_home_relative_missing_packet_path(
    monkeypatch,
    tmp_path: Path,
) -> None:
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    monkeypatch.setenv("HOME", str(home_dir))

    result = runner.invoke(warcraft_app, ["talent-packet", "~/missing-packet.json"])
    assert result.exit_code == 4
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "not_found"
    assert payload["error"]["message"] == "Talent transport packet file was not found: ~/missing-packet.json"


def test_warcraft_talent_describe_rejects_backslash_packet_like_input() -> None:
    result = runner.invoke(warcraft_app, ["talent-describe", r"tmp\missing-packet.json"])
    assert result.exit_code == 4
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "not_found"
    assert payload["error"]["message"] == r"Talent transport packet file was not found: tmp\missing-packet.json"


def test_warcraft_passthrough_to_raiderio(monkeypatch) -> None:
    def fake_profile(self, *, region: str, realm: str, name: str, fields: str = ""):  # noqa: ANN001
        return {
            "name": "Roguecane",
            "region": "us",
            "realm": "Illidan",
            "race": "Blood Elf",
            "class": "Rogue",
            "active_spec_name": "Subtlety",
            "faction": "horde",
            "profile_url": "https://raider.io/characters/us/illidan/Roguecane",
            "thumbnail_url": "https://example.test/thumb.jpg",
            "guild": {"name": "Liquid", "realm": "Illidan", "region": "us"},
            "raid_progression": {},
            "mythic_plus_scores_by_season": [],
            "mythic_plus_ranks": {},
            "mythic_plus_recent_runs": [],
        }

    # The passthrough runs the provider's own command, which reads the profile plus its fetch time.
    monkeypatch.setattr(
        "raiderio_cli.client.RaiderIOClient.character_profile",
        lambda self, **kwargs: FetchedJson(payload=fake_profile(self, **kwargs), fetched_at="2026-01-01T00:00:00Z", cache_hit=False),
    )
    result = runner.invoke(warcraft_app, ["raiderio", "character", "us", "illidan", "Roguecane"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["data"]["character"]["name"] == "Roguecane"


def test_warcraft_passthrough_to_warcraft_wiki(monkeypatch) -> None:
    monkeypatch.setattr(
        "warcraft_wiki_cli.main.WarcraftWikiClient.fetch_article_page",
        lambda self, article_ref: {
            "article": {
                "title": "World of Warcraft API",
                "slug": "world-of-warcraft-api",
                "display_title": "World of Warcraft API",
                "page_url": "https://warcraft.wiki.gg/wiki/World_of_Warcraft_API",
                "section_slug": "world-of-warcraft-api",
                "section_title": "World of Warcraft API",
                "page_count": 1,
            },
            "page": {
                "title": "World of Warcraft API",
                "description": "Programming reference",
                "canonical_url": "https://warcraft.wiki.gg/wiki/World_of_Warcraft_API",
            },
            "navigation": {"count": 0, "items": []},
            "article_content": {"html": "<p>FrameXML</p>", "text": "FrameXML", "headings": [], "sections": []},
            "linked_entities": [],
            "citations": {"page": "https://warcraft.wiki.gg/wiki/World_of_Warcraft_API"},
        },
    )
    result = runner.invoke(warcraft_app, ["warcraft-wiki", "article", "World of Warcraft API"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["data"]["article"]["title"] == "World of Warcraft API"


def test_warcraft_passthrough_to_warcraftlogs() -> None:
    result = runner.invoke(
        warcraft_app,
        ["warcraftlogs", "resolve", "https://www.warcraftlogs.com/reports/abcd1234#fight=3"],
    )
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert payload["provider"] == "warcraftlogs"
    assert payload["data"]["resolved"] is True
    assert payload["data"]["next_command"] == "warcraftlogs report-encounter abcd1234 --fight-id 3"


_RAIDERIO_GN_GUILD_PAYLOAD: dict[str, object] = {
    "guild": {
        "name": "gn",
        "region": "us",
        "realm": "Mal'Ganis",
        "faction": "horde",
        "profile_url": "https://raider.io/guilds/us/malganis/gn",
        "member_count": 1,
    },
    "raiding": {
        "raid_count": 2,
        "progression": [
            {
                "raid_slug": "liberation-of-undermine",
                "summary": "8/8 M",
                "total_bosses": 8,
                "normal_bosses_killed": 8,
                "heroic_bosses_killed": 8,
                "mythic_bosses_killed": 8,
            },
            {
                "raid_slug": "manaforge-omega",
                "summary": "2/8 N",
                "total_bosses": 8,
                "normal_bosses_killed": 2,
                "heroic_bosses_killed": 0,
                "mythic_bosses_killed": 0,
            },
        ],
        "rankings": [
            {
                "raid_slug": "liberation-of-undermine",
                "normal": {"world": 40, "region": 12, "realm": 3},
                "heroic": {"world": 30, "region": 9, "realm": 2},
                "mythic": {"world": 19, "region": 6, "realm": 2},
            }
        ],
    },
    "roster_preview": [
        {
            "name": "Fharg",
            "class_name": "Shaman",
            "active_spec_name": "Enhancement",
            "profile_url": "https://raider.io/characters/us/malganis/Fharg",
        }
    ],
    "citations": {"profile": "https://raider.io/guilds/us/malganis/gn"},
}


def _fake_raiderio_guild_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
    assert provider == "raiderio"
    assert args == ["guild", "us", "malganis", "gn"]
    envelope = {**_envelope(_RAIDERIO_GN_GUILD_PAYLOAD), "provenance": {"source": "raider.io"}}
    return {"provider": provider, "exit_code": 0, "payload": envelope}


def test_warcraft_guild_is_a_single_raiderio_source_and_normalizes_query(monkeypatch) -> None:
    monkeypatch.setattr("warcraft_cli.main.provider_invoke", _fake_raiderio_guild_invoke)

    result = runner.invoke(warcraft_app, ["guild", "na", "Mal'Ganis", "gn"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    assert envelope_violations(payload) == []
    assert payload["kind"] == "guild_snapshot"
    assert payload["query"] == {"region": "us", "realm": "malganis", "name": "gn"}
    assert payload["data"]["guild"] == {"name": "gn", "region": "us", "realm": "Mal'Ganis", "faction": "horde"}
    assert set(payload["data"]["sources"]) == {"raiderio"}
    assert payload["data"]["sources"]["raiderio"]["status"] == "ok"
    # The summary carries what the snapshot uses; the raw Raider.IO envelope is not shipped twice,
    # but its provenance is kept.
    assert "payload" not in payload["data"]["sources"]["raiderio"]
    assert payload["data"]["sources"]["raiderio"]["provenance"] == {"source": "raider.io"}
    summary = payload["data"]["sources"]["raiderio"]["summary"]
    # The raiderio CLI sorts progression and rankings by raid slug and Raider.IO carries no raid
    # window, so the snapshot reports every raid joined to its own ranks instead of calling element [0] the "active" raid.
    assert "active_raid" not in summary
    assert summary["raid_count"] == 2
    assert [row["raid_slug"] for row in summary["raids"]] == ["liberation-of-undermine", "manaforge-omega"]
    assert summary["raids"][0]["ranks"]["mythic"] == {"world": 19, "region": 6, "realm": 2}
    assert summary["raids"][1]["ranks"] == {"normal": None, "heroic": None, "mythic": None}
    assert "conflicts" not in payload["data"]


def test_guild_rank_rows_joins_progression_with_rankings_on_raid_slug() -> None:
    rows = guild_rank_rows(_RAIDERIO_GN_GUILD_PAYLOAD)

    assert [row["raid_slug"] for row in rows] == ["liberation-of-undermine", "manaforge-omega"]
    assert rows[0]["mythic_bosses_killed"] == 8
    assert rows[0]["ranks"] == {
        "normal": {"world": 40, "region": 12, "realm": 3},
        "heroic": {"world": 30, "region": 9, "realm": 2},
        "mythic": {"world": 19, "region": 6, "realm": 2},
    }
    # A raid with progression but no rankings row still appears, with null ranks.
    assert rows[1]["summary"] == "2/8 N"
    assert rows[1]["ranks"] == {"normal": None, "heroic": None, "mythic": None}


def test_warcraft_guild_propagates_the_source_exit_code(monkeypatch) -> None:
    """A missing Raider.IO guild exits with the source's own code (4), not a flat 1."""

    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        assert provider == "raiderio"
        return {
            "provider": provider,
            "exit_code": 4,
            "payload": {"ok": False, "error": {"code": "not_found", "message": "Could not find requested guild"}},
        }

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    result = runner.invoke(warcraft_app, ["guild", "us", "Mal'Ganis", "gn"])
    assert result.exit_code == 4, result.output
    payload = json.loads(result.stderr)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "not_found"


def test_warcraft_guild_looks_up_an_oceanic_guild_in_the_us_region(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        calls.append(args)
        return {"provider": provider, "exit_code": 0, "payload": _envelope(_RAIDERIO_GN_GUILD_PAYLOAD)}

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    result = runner.invoke(warcraft_app, ["guild", "oce", "Frostmourne", "gn"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["query"]["region"] == "us"
    assert calls == [["guild", "us", "frostmourne", "gn"]]


def _wrapper_module_trees() -> dict[str, ast.Module]:
    package_dir = Path(warcraft_cli.__file__).parent
    return {path.name: ast.parse(path.read_text(encoding="utf-8")) for path in sorted(package_dir.glob("*.py"))}


def _imported_top_level_modules(tree: ast.Module) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module)
    return modules


def test_providers_module_is_sole_provider_import_point() -> None:
    """Only warcraft_cli.providers may import a provider package; everything else goes through it."""
    offenders: dict[str, set[str]] = {}
    for filename, tree in _wrapper_module_trees().items():
        if filename == "providers.py":
            continue
        leaked = {
            module
            for module in _imported_top_level_modules(tree)
            if module.split(".")[0].endswith("_cli") and module.split(".")[0] != "warcraft_cli"
        }
        if leaked:
            offenders[filename] = leaked
    assert offenders == {}


def test_wrapper_does_not_import_typer_testing() -> None:
    """CliRunner is a test tool; the shipped wrapper calls provider surfaces and apps directly."""
    offenders = {
        filename
        for filename, tree in _wrapper_module_trees().items()
        if any(module.startswith("typer.testing") for module in _imported_top_level_modules(tree))
    }
    assert offenders == set()


def test_warcraft_doctor_reports_tiers() -> None:
    result = runner.invoke(warcraft_app, ["doctor"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    wrapper = payload["data"]["wrapper"]
    assert wrapper["tiers"] == {
        "core": ["wowhead", "warcraftlogs", "simc"],
        "supported": ["method", "icy-veins", "raiderio", "warcraft-wiki", "lorrgs"],
        "experimental": ["raidbots", "blizzard-api", "curseforge"],
    }
    tier_by_provider = {name: tier for tier, names in wrapper["tiers"].items() for name in names}
    for row in payload["data"]["providers"]:
        assert row["tier"] == tier_by_provider[row["provider"]]


def test_warcraft_search_reports_provider_failure_as_error_row(monkeypatch) -> None:
    """A provider that raises must appear as an error row, never as `payload: null`."""

    def exploding_search(query: str, *, limit: int = 10, **options: object) -> dict[str, object]:
        raise httpx.ConnectError("offline", request=httpx.Request("GET", "https://www.wowhead.com/"))

    monkeypatch.setattr(get_provider("wowhead").surface, "search", exploding_search)
    monkeypatch.setattr("method_cli.main.MethodClient.sitemap_guides", lambda self: [])
    monkeypatch.setattr("icy_veins_cli.main.IcyVeinsClient.sitemap_guides", lambda self: [])
    monkeypatch.setattr("raiderio_cli.client.RaiderIOClient.search", lambda self, *, term, kind=None: {"matches": []})
    monkeypatch.setattr("warcraft_wiki_cli.main.WarcraftWikiClient.search_articles", lambda self, query, *, limit: (0, []))

    result = runner.invoke(warcraft_app, ["search", "thunderfury"])
    assert result.exit_code == 0

    payload = json.loads(result.stdout)
    wowhead_row = next(row for row in payload["data"]["providers"] if row["provider"] == "wowhead")
    assert wowhead_row["ok"] is False
    assert wowhead_row["error"]["code"] == "network_error"
    assert isinstance(wowhead_row["payload"], dict)
    assert all(row.get("provider") != "wowhead" for row in payload["data"]["results"])


def test_warcraft_passthrough_forwards_output_flags(monkeypatch) -> None:
    """Global output flags shape the provider payload behind `warcraft <provider> ...` too."""
    monkeypatch.setattr(
        "wowhead_cli.main.WowheadClient.search_suggestions",
        lambda self, query: {"search": query, "results": []},
    )

    pretty = runner.invoke(warcraft_app, ["--pretty", "wowhead", "search", "defias"])
    assert pretty.exit_code == 0
    assert pretty.stdout.startswith("{\n")

    projected = runner.invoke(warcraft_app, ["--fields", "query", "wowhead", "search", "defias"])
    assert projected.exit_code == 0
    assert json.loads(projected.stdout) == {"query": "defias"}


# `talent-packet` and `talent-describe` share `_resolve_talent_transport`, so every route rejection
# is the same code path with a different payload `kind`. One parametrized pair pins that, instead of
# a copy of each rejection test per command.
# Wowhead's own rejection of a path that is not a talent-calc layout; the wrapper passes it through.
_WOWHEAD_TALENT_CALC_LAYOUT_MESSAGE = (
    "Talent calculator URL must use /talent-calc/<class>/<spec>[/<build-code>] or "
    "/talent-calc/<class>/<build-code> with a WoW class."
)
_SHARED_TALENT_ROUTE_REJECTIONS = [
    ("https://notwowhead.com/talent-calc/druid/balance/ABC123", "unsupported_talent_source", 2, None),
    ("notwowhead.com/talent-calc/druid/balance/ABC123", "invalid_tool_ref", 2,
     _WOWHEAD_TALENT_CALC_LAYOUT_MESSAGE),
    ("talent-calc/foo/talent-calc/druid/balance/ABC123", "invalid_tool_ref", 2,
     _WOWHEAD_TALENT_CALC_LAYOUT_MESSAGE),
    ("tmp/talent-calc/druid/balance/ABC123", "invalid_tool_ref", 2,
     _WOWHEAD_TALENT_CALC_LAYOUT_MESSAGE),
    ("https://www.wowhead.com/items/talent-calc/druid/balance/ABC123", "invalid_tool_ref", 2, None),
    ("druid/balance", "invalid_tool_ref", 2, "talent-calc packet refs must include an explicit build code."),
]


@pytest.mark.parametrize("command", ["talent-packet", "talent-describe"])
@pytest.mark.parametrize("source,code,exit_code,message", _SHARED_TALENT_ROUTE_REJECTIONS)
def test_talent_route_rejections_match_across_packet_and_describe(command, source, code, exit_code, message) -> None:
    result = runner.invoke(warcraft_app, [command, source])

    assert result.exit_code == exit_code, result.output
    payload = json.loads(result.stderr)
    assert payload["kind"] == "error"
    assert payload["error"]["code"] == code
    if message is not None:
        assert payload["error"]["message"] == message


# --- fanout health: a dead fanout must not look like "no results" -------------------------------

_SEARCH_READY_PROVIDERS = {"wowhead", "method", "icy-veins", "raiderio", "warcraftlogs", "warcraft-wiki", "lorrgs"}
# Warcraft Logs only matches explicit report references: free text gets a locally built hint, so it
# is included in the fanout but never counts as having answered one.
_FREE_TEXT_SEARCHERS = _SEARCH_READY_PROVIDERS - {"warcraftlogs"}


def _stub_healthy_search_fanout(monkeypatch) -> None:
    """Every search-ready provider answers, so any `internal_error` row is a broken double."""
    monkeypatch.setattr(
        "wowhead_cli.main.WowheadClient.search_suggestions",
        lambda self, query: {"search": query, "results": [
            {"type": 3, "id": 19019, "name": "Thunderfury", "typeName": "Item", "popularity": 10},
        ]},
    )
    monkeypatch.setattr(
        "method_cli.main.MethodClient.sitemap_guides",
        lambda self: [{"slug": "mistweaver-monk", "name": "Mistweaver Monk", "url": "https://www.method.gg/guides/mistweaver-monk"}],
    )
    monkeypatch.setattr("icy_veins_cli.main.IcyVeinsClient.sitemap_guides", lambda self: [])
    monkeypatch.setattr("raiderio_cli.client.RaiderIOClient.search", lambda self, *, term, kind=None: {"matches": []})
    monkeypatch.setattr(
        "warcraft_wiki_cli.main.WarcraftWikiClient.search_articles",
        lambda self, query, *, limit: (1, [{"title": "Thunderfury", "pageid": 7, "snippet": "blade",
                                            "url": "https://warcraft.wiki.gg/wiki/Thunderfury"}]),
    )


def _break_every_fanout_provider(monkeypatch, surface: str) -> None:
    """Every fanout provider fails the way an offline machine makes them fail.

    Several provider surfaces are read-only attributes, so this replaces the wrapper's own
    ``provider_search``/``provider_resolve`` seam with exactly what ``_call_surface`` builds from a
    transport error: the production converter, not a hand-written envelope. Warcraft Logs keeps its
    real surface: its free-text answer is built locally, so an outage leaves it answering ok.
    """
    real = getattr(warcraft_cli.main, f"provider_{surface}")

    def offline(provider: str, query: str, *, limit: int = 5, expansion: str | None = None) -> dict[str, object]:
        if provider == "warcraftlogs":
            return real(provider, query, limit=limit, expansion=expansion)
        exc = httpx.ConnectError("offline", request=httpx.Request("GET", "https://example.invalid/"))
        envelope, exit_code = error_envelope_for(provider, surface, exc)
        return {"provider": provider, "exit_code": exit_code, "payload": dict(envelope)}

    monkeypatch.setattr(f"warcraft_cli.main.provider_{surface}", offline)


def test_warcraft_search_healthy_fanout_has_no_internal_error_rows(monkeypatch) -> None:
    """Every provider double must match its real signature; a mismatched one shows up as internal_error."""
    _stub_healthy_search_fanout(monkeypatch)

    result = runner.invoke(warcraft_app, ["search", "thunderfury", "--limit", "5"])
    assert result.exit_code == 0

    data = json.loads(result.stdout)["data"]
    assert {row["provider"] for row in data["providers"]} == _SEARCH_READY_PROVIDERS
    assert [row for row in data["providers"] if not row["ok"]] == []
    assert data["failed_provider_count"] == 0
    assert {row["provider"] for row in data["providers"] if row["answered"]} == _FREE_TEXT_SEARCHERS
    assert data["answered_provider_count"] == len(_FREE_TEXT_SEARCHERS)


@pytest.mark.parametrize("command", ["search", "resolve"])
def test_a_fanout_no_included_provider_searches_is_a_usage_error(command: str) -> None:
    """Fresh leaves only Warcraft Logs, which matches report references, so free text reaches nobody."""
    result = runner.invoke(warcraft_app, ["--expansion", "fresh", command, "thunderfury"])

    assert result.exit_code == 2, result.output
    error = json.loads(result.stderr)["error"]
    assert error["code"] == "no_searching_provider"
    assert error["details"]["included_providers"] == ["warcraftlogs"]
    assert "wowhead" in {row["provider"] for row in error["details"]["excluded_providers"]}


def test_warcraft_search_fails_with_the_providers_own_code_when_every_provider_fails(monkeypatch) -> None:
    """All providers dead must be an error envelope, not an empty-looking success."""
    _break_every_fanout_provider(monkeypatch, "search")

    result = runner.invoke(warcraft_app, ["search", "thunderfury"])

    assert result.exit_code == 5, result.output
    payload = json.loads(result.stderr)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "network_error"
    failed = payload["error"]["details"]["failed_providers"]
    assert {row["provider"] for row in failed} == _FREE_TEXT_SEARCHERS


def test_warcraft_search_brief_still_reports_total_provider_failure(monkeypatch) -> None:
    """--brief drops provider payloads; it must not drop the fact that nothing answered."""
    _break_every_fanout_provider(monkeypatch, "search")

    result = runner.invoke(warcraft_app, ["search", "thunderfury", "--brief"])

    assert result.exit_code == 5, result.output
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "network_error"
    assert len(payload["error"]["details"]["failed_providers"]) == len(_FREE_TEXT_SEARCHERS)


def test_warcraft_search_brief_reports_partial_provider_failure(monkeypatch) -> None:
    """A partial failure stays visible under --brief, where `providers` is not emitted."""
    _stub_healthy_search_fanout(monkeypatch)

    def offline(query: str, *, limit: int = 10, **options: object) -> dict[str, object]:
        raise httpx.ConnectError("offline", request=httpx.Request("GET", "https://www.wowhead.com/"))

    monkeypatch.setattr(get_provider("wowhead").surface, "search", offline)

    result = runner.invoke(warcraft_app, ["search", "thunderfury", "--brief"])
    assert result.exit_code == 0

    data = json.loads(result.stdout)["data"]
    assert data["providers"] == []
    assert data["failed_providers"] == [{"provider": "wowhead", "code": "network_error",
                                         "message": "ConnectError: offline", "exit_code": 5}]
    assert data["failed_provider_count"] == 1
    assert data["answered_provider_count"] == len(_FREE_TEXT_SEARCHERS) - 1


def test_warcraft_resolve_fails_with_the_providers_own_code_when_every_provider_fails(monkeypatch) -> None:
    _break_every_fanout_provider(monkeypatch, "resolve")

    result = runner.invoke(warcraft_app, ["resolve", "thunderfury"])

    assert result.exit_code == 5, result.output
    payload = json.loads(result.stderr)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "network_error"
    assert len(payload["error"]["details"]["failed_providers"]) == len(_FREE_TEXT_SEARCHERS)


def test_warcraft_resolve_surfaces_the_provider_fallback_and_best_unresolved_candidate(monkeypatch) -> None:
    """An unresolved resolve must still hand the agent a next step, not a dead end."""

    def unresolved(query: str, *, limit: int = 10, **options: object) -> dict[str, object]:
        return {
            "ok": True,
            "provider": "wowhead",
            "kind": "resolve",
            "data": {
                "resolved": False,
                "confidence": "low",
                "match": {"provider": "wowhead", "id": 230224, "name": "Thunderfury", "kind": "item", "ranking": {"score": 48}},
                "fallback_search_command": "wowhead search 'thunderfury'",
            },
        }

    def fake_provider_resolve(provider: str, query: str, *, limit: int = 5, expansion: str | None = None, entity_types: tuple[str, ...] = ()):
        payload = unresolved(query) if provider == "wowhead" else {"ok": True, "data": {"resolved": False}}
        data = payload.get("data")
        return {"provider": provider, "exit_code": 0, "payload": {**payload, **(data if isinstance(data, dict) else {})}}

    monkeypatch.setattr("warcraft_cli.main.provider_resolve", fake_provider_resolve)

    result = runner.invoke(warcraft_app, ["resolve", "thunderfury"])
    assert result.exit_code == 0

    data = json.loads(result.stdout)["data"]
    assert data["resolved"] is False
    assert data["fallback_search_command"] == "wowhead search 'thunderfury'"
    assert data["fallback_search_commands"] == [{"provider": "wowhead", "command": "wowhead search 'thunderfury'"}]
    assert data["best_unresolved_candidate"]["id"] == 230224
    assert data["best_unresolved_candidate"]["provider"] == "wowhead"
    assert data["best_unresolved_candidate"]["resolved"] is False


@pytest.mark.parametrize("command", ["search", "resolve"])
def test_warcraft_fanout_rejects_a_blank_query_without_asking_any_provider(monkeypatch, command: str) -> None:
    def no_call(*args: object, **kwargs: object) -> dict[str, object]:
        raise AssertionError("a blank query reached a provider")

    monkeypatch.setattr(f"warcraft_cli.main.provider_{command}", no_call)

    result = runner.invoke(warcraft_app, [command, "  "])

    assert result.exit_code == 2
    assert json.loads(result.stderr)["error"]["code"] == "invalid_query"


def test_warcraft_resolve_hands_over_no_fallback_from_a_provider_that_found_nothing(monkeypatch) -> None:
    """Wowhead comes first in registry order, but only the wiki found a candidate for this query."""

    def fake_provider_resolve(provider: str, query: str, *, limit: int = 5, expansion: str | None = None, entity_types: tuple[str, ...] = ()):
        data: dict[str, object] = {"resolved": False, "confidence": "none", "match": None}
        if provider in {"wowhead", "warcraft-wiki"}:
            data["fallback_search_command"] = f"{provider} search 'ashes of al'ar'"
        if provider == "warcraft-wiki":
            data.update(confidence="medium", match={"provider": provider, "id": "Al'ar", "name": "Al'ar", "kind": "article",
                                                    "ranking": {"score": 60}})
        return {"provider": provider, "exit_code": 0, "payload": _envelope(data)}

    monkeypatch.setattr("warcraft_cli.main.provider_resolve", fake_provider_resolve)

    data = json.loads(runner.invoke(warcraft_app, ["resolve", "ashes of al'ar"]).stdout)["data"]

    assert data["fallback_search_commands"] == [{"provider": "warcraft-wiki", "command": "warcraft-wiki search 'ashes of al'ar'"}]
    assert data["fallback_search_command"] == "warcraft-wiki search 'ashes of al'ar'"

    # Nobody found anything: no provider's empty search is handed over as a next step.
    monkeypatch.setattr(
        "warcraft_cli.main.provider_resolve",
        lambda provider, query, *, limit=5, expansion=None: {
            "provider": provider, "exit_code": 0,
            "payload": _envelope({"resolved": False, "match": None, "fallback_search_command": f"{provider} search x"}),
        },
    )
    data = json.loads(runner.invoke(warcraft_app, ["resolve", "zzqqxx"]).stdout)["data"]

    assert (data["fallback_search_command"], data["fallback_search_commands"]) == (None, [])


def test_warcraft_expansion_excluded_providers_carry_a_top_level_reason(monkeypatch) -> None:
    """Expansion exclusions use the same `reason` key surface-readiness exclusions already use."""
    monkeypatch.setattr(
        "wowhead_cli.main.WowheadClient.search_suggestions",
        lambda self, query: {"search": query, "results": []},
    )

    result = runner.invoke(warcraft_app, ["--expansion", "wotlk", "search", "thunderfury", "--brief"])
    assert result.exit_code == 0, result.output

    data = json.loads(result.stdout)["data"]
    excluded = {row["provider"]: row for row in data["excluded_providers"]}
    assert excluded["raiderio"]["reason"] == "provider_fixed_to_other_expansion"
    assert excluded["simc"]["reason"] == "provider_has_no_expansion_support"
    assert all(row.get("reason") for row in data["excluded_providers"])


# --- composite failure contracts ----------------------------------------------------------------


@pytest.mark.parametrize(
    "source_code,source_exit,expected_exit",
    [("not_found", 4, 4), ("network_error", 5, 5), ("auth_failed", 3, 3)],
)
def test_warcraft_guild_propagates_the_source_error_code(monkeypatch, source_code, source_exit, expected_exit) -> None:
    """The envelope's `error.code` must agree with the exit code, not always claim "guild not found"."""

    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        return {
            "provider": provider,
            "exit_code": source_exit,
            "payload": {"ok": False, "error": {"code": source_code, "message": f"raiderio said {source_code}"}},
        }

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    result = runner.invoke(warcraft_app, ["guild", "us", "Mal'Ganis", "gn"])

    assert result.exit_code == expected_exit, result.output
    payload = json.loads(result.stderr)
    assert payload["ok"] is False
    assert payload["error"]["code"] == source_code
    assert payload["error"]["message"] == f"raiderio said {source_code}"


def _guide_bundle(root: Path, *, build_code: str | None) -> Path:
    bundle = root / "method"
    write_article_bundle(
        _comparison_payload(
            provider="method",
            slug="mistweaver-monk",
            page_url="https://www.method.gg/guides/mistweaver-monk",
            page_title="Method Talents",
            analysis_tags=["builds_talents"],
            build_code=build_code,
        ),
        provider="method",
        export_dir=bundle,
    )
    return bundle


def _bundle_with_references(
    root: Path, references: list[dict[str, object]], *, failed_pages: int = 0, redirect: dict[str, str] | None = None
) -> Path:
    """A method bundle carrying exactly ``references``, and optionally pages the export never fetched."""
    bundle = root / "method"
    payload = _comparison_payload(
        provider="method",
        slug="mistweaver-monk",
        page_url="https://www.method.gg/guides/mistweaver-monk",
        page_title="Method Talents",
        analysis_tags=["builds_talents"],
    )
    payload["build_references"] = {"count": len(references), "items": references}
    if failed_pages:
        payload["failed_pages"] = {
            "count": failed_pages,
            "items": [
                {"url": f"https://www.method.gg/guides/mistweaver-monk/page-{index}", "error": "http_503"}
                for index in range(failed_pages)
            ],
        }
    payload["redirect"] = redirect
    write_article_bundle(payload, provider="method", export_dir=bundle)
    return bundle


def test_guide_builds_simc_hands_each_reference_to_simc_in_the_form_its_type_requires(monkeypatch, tmp_path) -> None:
    """A published loadout string is `--build-text`; a Wowhead talent-calc URL is a transport packet.

    Sending the raw import string as a Wowhead reference produces no packet at all, which is how the
    whole decode leg came back empty for guide-published builds.
    """
    bundle = _bundle_with_references(
        tmp_path,
        [
            {
                "kind": "build_reference",
                "reference_type": "wow_talent_export",
                "url": "CEQAAAAAAAAAAAAAAAAAAAAAAYGMzMzYmxMzMmxMzsNzMzYGmxMMzMzMzMbAAAAAAAAAAgZmZmZmZZmZGmxMzMzsNzYmxMbA",
                "label": "Raid Build",
                "build_code": "CEQAAAAAAAAAAAAAAAAAAAAAAYGMzMzYmxMzMmxMzsNzMzYGmxMMzMzMzMbAAAAAAAAAAgZmZmZmZZmZGmxMzMzsNzYmxMbA",
                "source_urls": ["https://www.method.gg/guides/mistweaver-monk"],
            },
            {
                "kind": "build_reference",
                "reference_type": "wowhead_talent_calc_url",
                "url": "https://www.wowhead.com/talent-calc/monk/mistweaver/ABC123",
                "label": "Dungeon Build",
                "build_code": "ABC123",
                "source_urls": ["https://www.method.gg/guides/mistweaver-monk"],
            },
        ],
    )
    identify_calls: list[dict[str, object]] = []

    def fake_simc(command: str, build: PacketInput | str, *, describe: DescribeOptions | None = None) -> dict[str, object]:
        if command == "identify-build":
            identify_calls.append(_simc_call_summary(command, build, describe))
        return _simc_result({"ok": True})

    monkeypatch.setattr("warcraft_cli.main.simc_call", fake_simc)

    result = runner.invoke(warcraft_app, ["guide-builds-simc", str(bundle)])
    assert result.exit_code == 0, result.output

    export_call = next(row for row in identify_calls if row["build_input"] == "text")
    assert str(export_call["build_text"]).startswith("CEQAAAAA"), "the import string itself is the build input"
    packet_call = next(row for row in identify_calls if row["build_input"] == "packet")
    assert packet_call["packet_transport_url"] == "https://www.wowhead.com/talent-calc/monk/mistweaver/ABC123"
    assert json.loads(result.stdout)["data"]["summary"]["identify_success_count"] == 2


def test_guide_builds_simc_names_the_references_it_could_not_hand_over(monkeypatch, tmp_path) -> None:
    """An unusable reference is an excluded row with a reason, not a silently shorter build list."""
    bundle = _bundle_with_references(
        tmp_path,
        [
            {
                "kind": "build_reference",
                "reference_type": "wowhead_talent_calc_url",
                "url": "https://www.wowhead.com/talent-calc/monk/mistweaver",
                "label": "No Code",
                "build_code": None,
                "source_urls": ["https://www.method.gg/guides/mistweaver-monk"],
            }
        ],
    )

    monkeypatch.setattr("warcraft_cli.main.simc_call", lambda command, build, *, describe=None: _simc_result({"ok": True}))

    result = runner.invoke(warcraft_app, ["guide-builds-simc", str(bundle)])
    assert result.exit_code == 0, result.output

    data = json.loads(result.stdout)["data"]
    assert data["build_reference_count"] == 1
    assert data["summary"]["returned_build_count"] == 0
    assert data["summary"]["excluded_build_count"] == 1
    assert [row["reason"] for row in data["excluded_builds"]] == ["missing_build_code"]
    # The guide had a build; none could be handed over. That is not "no build references".
    assert data["summary"]["simc_handoff_status"] == "all_references_excluded"


REDIRECT = {"requested": "mistweaver-monk-remix", "served": "mistweaver-monk", "message": "Method served mistweaver-monk"}


def test_guide_builds_simc_reports_the_pages_the_export_never_fetched(monkeypatch, tmp_path) -> None:
    """A handoff read from a partial bundle says so: builds may be missing from the pages that failed."""
    bundle = _bundle_with_references(
        tmp_path,
        [
            {
                "kind": "build_reference",
                "reference_type": "wowhead_talent_calc_url",
                "url": "https://www.wowhead.com/talent-calc/monk/mistweaver/ABC123",
                "label": "Raid Build",
                "build_code": "ABC123",
                "source_urls": ["https://www.method.gg/guides/mistweaver-monk"],
            }
        ],
        failed_pages=2,
        redirect=REDIRECT,
    )

    monkeypatch.setattr("warcraft_cli.main.simc_call", lambda command, build, *, describe=None: _simc_result({"ok": True}))

    result = runner.invoke(warcraft_app, ["guide-builds-simc", str(bundle)])
    assert result.exit_code == 0, result.output

    data = json.loads(result.stdout)["data"]
    assert data["summary"]["failed_page_count"] == 2
    assert data["bundle_health"]["failed_page_count"] == 2
    assert data["bundle_health"]["bundles"][0]["provider"] == "method"
    # The bundle of a retired guide still says which guide the site served instead.
    assert data["bundle_health"]["bundles"][0]["redirect"] == REDIRECT


def test_guide_builds_simc_fails_when_every_simc_handoff_failed(monkeypatch, tmp_path) -> None:
    """A packet with zero usable simc output is a failure, not a success with empty counters."""
    bundle = _guide_bundle(tmp_path, build_code="ABC123")

    def failing_simc(command: str, build: PacketInput | str, *, describe: DescribeOptions | None = None) -> dict[str, object]:
        return _simc_result({"ok": False, "error": {"code": "network_error", "message": "no simc repo"}}, exit_code=5)

    monkeypatch.setattr("warcraft_cli.main.simc_call", failing_simc)

    result = runner.invoke(warcraft_app, ["guide-builds-simc", str(bundle)])

    assert result.exit_code == 1, result.output
    payload = json.loads(result.stderr)
    assert payload["ok"] is False
    assert envelope_violations(payload) == []
    # A contract error envelope: kind "error", and the packet's provenance kept as the envelope's.
    assert payload["kind"] == "error"
    assert payload["provenance"]["selection_contract"] == "embedded_build_references_only"
    assert payload["error"]["code"] == "simc_handoff_failed"
    summary = payload["error"]["details"]["summary"]
    assert summary["simc_handoff_status"] == "all_handoffs_failed"
    assert summary["empty_requested_legs"] == ["identify", "decode"]
    assert summary["identify_success_count"] == 0
    assert summary["returned_build_count"] >= 1
    # The packet travels under error.details, so each build still names the simc error per leg.
    failures = payload["error"]["details"]["builds"][0]["failures"]
    assert [(failure["leg"], failure["code"]) for failure in failures] == [
        ("identify", "network_error"), ("decode", "network_error"),
    ]


def test_guide_builds_simc_stays_ok_when_one_requested_leg_still_produced_output(monkeypatch, tmp_path) -> None:
    """Mixed legs are not a total failure: decode output is usable even when identify failed."""
    bundle = _guide_bundle(tmp_path, build_code="ABC123")

    def identify_fails(command: str, build: PacketInput | str, *, describe: DescribeOptions | None = None) -> dict[str, object]:
        if command == "identify-build":
            return _simc_result({"ok": False, "error": {"code": "unsupported_build", "message": "no probe"}}, exit_code=1)
        return _simc_result({"ok": True})

    monkeypatch.setattr("warcraft_cli.main.simc_call", identify_fails)

    result = runner.invoke(warcraft_app, ["guide-builds-simc", str(bundle), "--decode"])
    assert result.exit_code == 0, result.output

    summary = json.loads(result.stdout)["data"]["summary"]
    assert summary["decode_success_count"] >= 1
    assert summary["empty_requested_legs"] == ["identify"]
    assert summary["simc_handoff_status"] == "failed"


def test_guide_builds_simc_does_not_count_an_unidentified_build_as_identified(monkeypatch, tmp_path) -> None:
    """identify-build exits 0 with confidence none for a hash no spec decodes; that is a failed leg."""
    bundle = _guide_bundle(tmp_path, build_code="ABC123")

    def unidentified(command: str, build: PacketInput | str, *, describe: DescribeOptions | None = None) -> dict[str, object]:
        if command == "identify-build":
            identity = {"actor_class": None, "spec": None, "confidence": "none", "candidate_count": 0}
            return _simc_result(_envelope({"kind": "identify_build", "identity": identity}))
        return _simc_result({"ok": True})

    monkeypatch.setattr("warcraft_cli.main.simc_call", unidentified)

    result = runner.invoke(warcraft_app, ["guide-builds-simc", str(bundle)])
    assert result.exit_code == 0, result.output

    data = json.loads(result.stdout)["data"]
    assert data["summary"]["identify_success_count"] == 0
    assert data["summary"]["empty_requested_legs"] == ["identify"]
    assert data["summary"]["simc_handoff_status"] == "failed"
    assert [(failure["leg"], failure["code"]) for failure in data["builds"][0]["failures"]] == [
        ("identify", "build_not_identified")
    ]


def test_guide_builds_simc_blames_the_hashes_when_no_build_is_identified(monkeypatch, tmp_path) -> None:
    """With only identify requested and no build identified, the run fails, and the error points at
    the guide hashes rather than at a SimC install that worked."""
    bundle = _guide_bundle(tmp_path, build_code="ABC123")

    def unidentified(command: str, build: PacketInput | str, *, describe: DescribeOptions | None = None) -> dict[str, object]:
        identity = {"actor_class": None, "spec": None, "confidence": "none", "candidate_count": 0}
        return _simc_result(_envelope({"kind": "identify_build", "identity": identity}))

    monkeypatch.setattr("warcraft_cli.main.simc_call", unidentified)

    result = runner.invoke(warcraft_app, ["guide-builds-simc", str(bundle), "--no-decode"])

    assert result.exit_code == 1, result.output
    error = json.loads(result.stderr)["error"]
    assert error["code"] == "simc_handoff_failed"
    assert "build_not_identified" in error["message"]
    assert "simc doctor" not in error["message"]


def test_guide_builds_simc_reports_no_build_references_without_failing(monkeypatch, tmp_path) -> None:
    """An export with nothing to hand off is an explicit empty, not a silent success."""
    bundle = _guide_bundle(tmp_path, build_code=None)

    result = runner.invoke(warcraft_app, ["guide-builds-simc", str(bundle)])
    assert result.exit_code == 0, result.output

    summary = json.loads(result.stdout)["data"]["summary"]
    assert summary["simc_handoff_status"] == "no_build_references"
    assert summary["returned_build_count"] == 0


def test_guide_builds_simc_fails_when_a_requested_leg_produced_nothing(monkeypatch, tmp_path) -> None:
    """`--decode` that decodes nothing is `failed`, never `ok` or `partial` beside a zero counter.

    Zero decodes out of N is not "most of it worked": `partial` is reserved for a leg that succeeded
    for some builds and failed for others, and the per-build reason has to survive in the payload.
    """
    bundle = _guide_bundle(tmp_path, build_code="ABC123")

    def half_working_simc(command: str, build: PacketInput | str, *, describe: DescribeOptions | None = None) -> dict[str, object]:
        if command == "decode-build":
            return _simc_result({"ok": False, "error": {"code": "invalid_query", "message": "no class/spec"}}, exit_code=2)
        return _simc_result({"ok": True})

    monkeypatch.setattr("warcraft_cli.main.simc_call", half_working_simc)

    result = runner.invoke(warcraft_app, ["guide-builds-simc", str(bundle), "--decode"])
    assert result.exit_code == 0, result.output

    data = json.loads(result.stdout)["data"]
    summary = data["summary"]
    assert summary["identify_success_count"] >= 1
    assert summary["decode_success_count"] == 0
    assert summary["empty_requested_legs"] == ["decode"]
    assert summary["partial_requested_legs"] == []
    assert summary["simc_handoff_status"] == "failed"
    # Each build says why its decode leg failed, so the zero counter is explainable without a re-run.
    assert [failure["code"] for failure in data["builds"][0]["failures"]] == ["invalid_query"]
    assert data["builds"][0]["simc"]["decode"]["error"]["message"] == "no class/spec"


def test_guide_builds_simc_is_ok_when_the_requested_legs_all_produced_something(monkeypatch, tmp_path) -> None:
    """A leg nobody asked for (`--apl-path` describe) cannot make the handoff `partial`."""
    bundle = _guide_bundle(tmp_path, build_code="ABC123")

    monkeypatch.setattr("warcraft_cli.main.simc_call", lambda command, build, *, describe=None: _simc_result({"ok": True}))

    result = runner.invoke(warcraft_app, ["guide-builds-simc", str(bundle)])
    assert result.exit_code == 0, result.output

    summary = json.loads(result.stdout)["data"]["summary"]
    assert summary["decode_success_count"] >= 1
    assert summary["describe_success_count"] == 0
    assert summary["empty_requested_legs"] == []
    assert summary["simc_handoff_status"] == "ok"


def test_warcraft_passthrough_propagates_a_provider_nonzero_exit(monkeypatch) -> None:
    """`invoke_provider_command` must re-raise a provider's exit code instead of returning 0."""
    from warcraft_cli.providers import invoke_provider_command

    failing_app = typer.Typer()

    @failing_app.command("boom")
    def boom() -> None:
        raise typer.Exit(4)

    @failing_app.command("fine")
    def fine() -> None:
        return None

    with pytest.raises(typer.Exit) as excinfo:
        invoke_provider_command(failing_app, args=["boom"], prog_name="failing")

    assert excinfo.value.exit_code == 4
    invoke_provider_command(failing_app, args=["fine"], prog_name="failing")


def test_provider_invoke_turns_a_provider_crash_into_an_error_envelope(monkeypatch) -> None:
    """A provider command that raises must reach the agent as an envelope, never a traceback."""
    import dataclasses

    from warcraft_cli.providers import provider_invoke

    crashing_app = typer.Typer()

    @crashing_app.command("search")
    def crashing_search() -> None:
        raise RuntimeError("provider exploded")

    @crashing_app.command("other")
    def other() -> None:
        return None

    crashing = dataclasses.replace(get_provider("method"), app=crashing_app)
    monkeypatch.setattr("warcraft_cli.providers.get_provider", lambda name: crashing)

    result = provider_invoke("method", ["search"])

    assert result["exit_code"] == 1
    assert result["payload"]["ok"] is False
    assert result["payload"]["error"]["code"] == "internal_error"
    assert "provider exploded" in result["payload"]["error"]["message"]


def test_provider_search_crash_echoes_the_query(monkeypatch) -> None:
    """A surface that raises is still reported with the input the wrapper handed it."""
    import dataclasses
    from types import SimpleNamespace

    from warcraft_cli.providers import provider_search

    def crashing_search(query: str, **options: object) -> dict[str, object]:
        raise RuntimeError("provider exploded")

    crashing = dataclasses.replace(get_provider("method"), surface=SimpleNamespace(search=crashing_search))
    monkeypatch.setattr("warcraft_cli.providers.get_provider", lambda name: crashing)

    result = provider_search("method", "mistweaver monk")

    assert result["exit_code"] == 1
    assert result["payload"]["error"]["code"] == "internal_error"
    assert result["payload"]["query"] == "mistweaver monk"


def test_provider_search_rejects_an_expansion_the_provider_cannot_serve() -> None:
    """The registry-level expansion guard is reachable (e.g. `warcraft --expansion wotlk guild`)."""
    from warcraft_cli.providers import provider_search

    result = provider_search("raiderio", "thunderfury", expansion="wotlk")

    assert result["exit_code"] == 2
    assert envelope_violations(result["payload"]) == []
    assert result["payload"]["query"] == "thunderfury"
    assert result["payload"]["error"]["code"] == "unsupported_provider_expansion"
    assert result["payload"]["error"]["details"]["expansion_support"]["exclusion_reason"] == "provider_fixed_to_other_expansion"


def test_warcraft_guild_expansion_mismatch_surfaces_the_registry_guard(monkeypatch) -> None:
    result = runner.invoke(warcraft_app, ["--expansion", "wotlk", "guild", "us", "Mal'Ganis", "gn"])

    assert result.exit_code == 2, result.output
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "unsupported_provider_expansion"


def test_transport_packet_upgraded_when_only_the_status_rank_improves() -> None:
    """A raw_only -> validated upgrade counts even when the transport-form set is unchanged."""
    from warcraft_cli.talent_routing import _transport_packet_upgraded

    forms = {"simc_split_talents": {"class_talents": "1:1"}}
    previous = {"transport_status": "raw_only", "transport_forms": forms}
    updated = {"transport_status": "validated", "transport_forms": forms}

    assert _transport_packet_upgraded(previous, updated) is True
    assert _transport_packet_upgraded(updated, previous) is False


def test_guide_compare_query_default_out_root_is_the_xdg_data_dir(monkeypatch, tmp_path) -> None:
    """Without --out-root the orchestration root is the XDG data dir, never the caller's CWD."""
    from warcraft_cli.guide_compare import default_guide_compare_query_root

    workdir = tmp_path / "cwd"
    workdir.mkdir()
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.chdir(workdir)

    root = default_guide_compare_query_root("Balance Druid guide")

    assert root == tmp_path / "data" / "warcraft" / "guide_compare" / "balance-druid-guide"
    assert workdir not in root.parents


def test_guide_compare_reuse_of_a_corrupt_bundle_becomes_an_error_row(tmp_path) -> None:
    """A manifest row pointing at an unreadable bundle must not be reused as if it loaded."""
    from warcraft_cli.guide_compare import _guide_compare_reuse_row

    export_dir = tmp_path / "method"
    export_dir.mkdir()
    (export_dir / "manifest.json").write_text("{ not json", encoding="utf-8")

    row, bundle_input = _guide_compare_reuse_row(
        "method",
        candidate={"ref": "mistweaver-monk"},
        export_dir=export_dir,
        existing_row={"exported_at": "2026-09-18T00:00:00Z"},
        freshness={"status": "fresh"},
    )

    assert bundle_input is None
    assert row["status"] == "error"
    assert row["reason"] == "invalid_exported_bundle"
    assert row["bundle_path"] == str(export_dir)
    assert row["error"]


def test_cooldown_packet_source_command_is_a_runnable_command_line() -> None:
    from warcraft_cli.cooldown_packet import source_command

    assert source_command("lorrgs", ["user-report-fights", "abc 123", "--fight", "11"]) == (
        "warcraft lorrgs user-report-fights 'abc 123' --fight 11"
    )


# --- cooldown-packet without a Lorrgs-cached report ----------------------------------------------

_LORRGS_SPEC_SPELLS = {
    "ok": True,
    "data": {
        "107574": {"spell_id": 107574, "name": "Avatar", "query": True, "show": True, "cooldown": 90},
    },
}
_WCL_FIGHTS = _envelope({"fights": [{"id": 22, "start_time": 1000, "name": "Lura"}]})
# The roster of fight 22 alone: a player elsewhere in the report is not in it.
_WCL_ROSTER = _envelope({"player_details": {"roles": {"tanks": [{"id": 89, "name": "Buikia", "type": "Warrior"}]}}})
_WCL_EVENTS = _envelope({
    "events": [
        {"abilityGameID": 107574, "timestamp": 2500, "type": "cast", "sourceID": 89},
        {"abilityGameID": 107574, "timestamp": 6000, "type": "cast", "sourceID": 89},
    ],
})


def _wcl_phases(phase_transitions: list[dict[str, int]] | None) -> dict[str, Any]:
    """The `warcraftlogs graphql` answer for fight 22's phase transitions (null: a fight without phases)."""
    fight = {"id": 22, "encounterID": 7, "startTime": 1000, "endTime": 9000, "phaseTransitions": phase_transitions}
    phases = [{"encounterID": 7, "phases": [{"id": 1, "name": "Lura"}, {"id": 2, "name": "Void Rift"}]}]
    return _envelope({"reportData": {"report": {"phases": phases, "fights": [fight]}}})


def _uncached_lorrgs_invoke(calls: list[tuple[str, list[str]]], phase_transitions: list[dict[str, int]] | None = None):
    """Lorrgs 404s the user report (the normal case) but still serves its static spec metadata."""

    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        calls.append((provider, args))
        if provider == "warcraftlogs" and args[0] == "graphql":
            return {"provider": provider, "exit_code": 0, "payload": _wcl_phases(phase_transitions)}
        if provider == "lorrgs" and args[0] == "user-report-fights":
            return {
                "provider": provider,
                "exit_code": 4,
                "payload": {"ok": False, "error": {"code": "not_found", "message": "404 user_reports/abcd1234/fights"}},
            }
        if provider == "lorrgs" and args[0] == "spec-spells":
            return {"provider": provider, "exit_code": 0, "payload": _LORRGS_SPEC_SPELLS}
        if provider == "warcraftlogs" and args[0] == "report-fights":
            return {"provider": provider, "exit_code": 0, "payload": _WCL_FIGHTS}
        if provider == "warcraftlogs" and args[0] == "report-player-details":
            return {"provider": provider, "exit_code": 0, "payload": _WCL_ROSTER}
        if provider == "warcraftlogs" and args[0] == "report-events":
            return {"provider": provider, "exit_code": 0, "payload": _WCL_EVENTS}
        raise AssertionError((provider, args))

    return fake_provider_invoke


def test_phase_windows_keep_the_last_phase_when_lorrgs_omits_the_fight_duration() -> None:
    from warcraft_cli.cooldown_packet import build_phase_windows, timestamp_in_window

    windows = build_phase_windows([{"ts": 60000}, {"ts": 120000}], None)

    assert [(row["label"], row["start_ms"], row["end_ms"]) for row in windows] == [
        ("P1", 0, 60000), ("P2", 60000, 120000), ("P3", 120000, None)
    ]
    assert (windows[2]["end_source"], windows[2]["duration_ms"]) == ("unknown", None)
    assert timestamp_in_window(150000, windows[2]) and not timestamp_in_window(150000, windows[1])


def test_cooldown_packet_counts_an_empowered_press_once() -> None:
    """An empowered press arrives as empowerstart + cast + empowerend; only the cast is one use."""
    events = {"events": [
        {"abilityGameID": 355936, "timestamp": 1000 + offset, "type": event_type, "sourceID": 14}
        for offset, event_type in ((0, "empowerstart"), (1, "cast"), (900, "empowerend"))
    ]}
    casts = normalize_warcraftlogs_actor_casts(
        events, fight_start_time_ms=0, catalog={}, spell_ids={355936}, source_id=14, window={"start_ms": 0, "end_ms": 5000}
    )
    assert (casts["tracked_cast_count"], casts["selected_phase_cast_count"]) == (1, 1)
    assert [row["count"] for row in casts["tracked_casts_by_spell"]] == [1]


def test_cooldown_packet_degrades_to_the_warcraftlogs_half_when_lorrgs_has_no_cached_report(monkeypatch) -> None:
    """An ordinary (uncached) report must still produce the Warcraft Logs casts, flagged as degraded."""
    calls: list[tuple[str, list[str]]] = []
    monkeypatch.setattr("warcraft_cli.main.provider_invoke", _uncached_lorrgs_invoke(calls))

    result = runner.invoke(
        warcraft_app,
        [
            "cooldown-packet", "abcd1234", "--fight-id", "22", "--actor-id", "89",
            "--spec-slug", "warrior-protection", "--phase", "2",
        ],
    )

    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)["data"]
    assert data["lorrgs"]["status"] == "unavailable"
    assert data["lorrgs"]["reason"] == "lorrgs_fight_lookup_failed"
    assert data["lorrgs"]["source"]["code"] == "not_found"
    assert data["phase"]["status"] == "unavailable"
    assert data["phase"]["selected"] is None
    assert data["phase"]["windows"] == []
    # The Warcraft Logs half is real: both casts are tracked, none are claimed for the phase.
    assert data["cooldowns"]["player_casts"]["tracked_cast_count"] == 2
    assert data["cooldowns"]["player_casts"]["selected_phase_cast_count"] == 0
    assert data["player"]["source_id"] == 89
    # No Lorrgs roster: name and class come from the Warcraft Logs roster.
    assert (data["player"]["name"], data["player"]["class_slug"]) == ("Buikia", "warrior")
    # Deaths come only from the Lorrgs timeline, so they are unknown here, not zero.
    assert data["player"]["deaths"] is None
    assert "player_deaths" in data["lorrgs"]["missing"]
    assert any("player.deaths is null" in note for note in data["notes"])
    assert any("--phase was not applied" in note for note in data["notes"])
    assert data["phase"]["source"] is None
    assert ("warcraftlogs", ["report-events", "abcd1234", "--fight-id", "22", "--source-id", "89",
                            "--data-type", "casts", "--limit", "5000"]) in calls
    # The actor is checked against the fight's roster, not the whole report's.
    assert ("warcraftlogs", ["report-player-details", "abcd1234", "--fight-id", "22"]) in calls
    assert data["sources"]["lorrgs_user_report_fights"]["command"] == (
        "warcraft lorrgs user-report-fights abcd1234 --fight 22"
    )


@pytest.mark.parametrize(("phase", "selected_casts"), [("2", 1), ("3", 0), ("4", 1)])
def test_cooldown_packet_without_lorrgs_takes_phase_windows_from_warcraftlogs(monkeypatch, phase: str, selected_casts: int) -> None:
    """Lorrgs 404s most reports; the Warcraft Logs fight's phase transitions still scope the casts.
    Windows are numbered in order like Lorrgs ones, so encounter phase 2's two visits are P2 and P4,
    and each holds one of the casts (1.5 s and 5 s after the pull)."""
    transitions = [{"id": 1, "startTime": 1000}, {"id": 2, "startTime": 2000}, {"id": 1, "startTime": 3000},
                   {"id": 2, "startTime": 5500}, {"id": 1, "startTime": 7000}]
    calls: list[tuple[str, list[str]]] = []
    monkeypatch.setattr("warcraft_cli.main.provider_invoke", _uncached_lorrgs_invoke(calls, transitions))

    result = runner.invoke(warcraft_app, [
        "cooldown-packet", "abcd1234", "--fight-id", "22", "--actor-id", "89", "--spec-slug", "warrior-protection",
        "--phase", phase,
    ])

    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)["data"]
    assert (data["phase"]["status"], data["phase"]["source"], data["phase"]["unavailable_reason"]) == ("ready", "warcraftlogs", None)
    assert [(row["label"], row["phase_id"], row["start_ms"], row["end_ms"]) for row in data["phase"]["windows"]] == [
        ("P1", 1, 0, 1000), ("P2", 2, 1000, 2000), ("P3", 1, 2000, 4500), ("P4", 2, 4500, 6000), ("P5", 1, 6000, 8000)]
    assert data["phase"]["selected"]["name"] == ("Lura" if phase == "3" else "Void Rift")
    assert data["cooldowns"]["player_casts"]["selected_phase_cast_count"] == selected_casts
    assert "phase_windows" not in data["lorrgs"]["missing"]
    assert any("phase windows come from the Warcraft Logs fight" in note for note in data["notes"])
    assert data["sources"]["warcraftlogs_phase_transitions"]["status"] == "ok"
    assert [args[:1] + args[3:] for provider, args in calls if args[0] == "graphql"] == [
        ["graphql", "--report-code", "abcd1234", "--fight-id", "22"]]


def test_cooldown_packet_keeps_the_whole_fight_when_the_warcraftlogs_phase_lookup_fails(monkeypatch) -> None:
    uncached = _uncached_lorrgs_invoke([])

    def invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        if args[0] == "graphql":
            return {"provider": provider, "exit_code": 5, "payload": {"ok": False, "error": {"code": "network_error", "message": "x"}}}
        return uncached(provider, args, expansion=expansion)

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", invoke)

    result = runner.invoke(warcraft_app, [
        "cooldown-packet", "abcd1234", "--fight-id", "22", "--actor-id", "89", "--spec-slug", "warrior-protection", "--phase", "2",
    ])

    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)["data"]
    assert (data["phase"]["status"], data["phase"]["unavailable_reason"]) == ("unavailable", "lorrgs_fight_lookup_failed")
    assert data["sources"]["warcraftlogs_phase_transitions"]["error"]["code"] == "network_error"
    assert data["cooldowns"]["player_casts"]["tracked_cast_count"] == 2


def _wcl_segmented_cooldown_packet(monkeypatch, sample_fights: dict[str, Any], sample_args: list[list[str]]) -> dict[str, Any]:
    """Run a cooldown packet whose player windows come from Warcraft Logs and whose top parses (one per
    ``sample_fights`` alias, Lorrgs-marked only at 2500 ms) are segmented by ``sample_fights``. Like
    GraphQL, a sample fight carries ``encounterID`` only when the query selects it."""
    transitions = [{"id": 1, "startTime": 1000}, {"id": 2, "startTime": 2000}, {"id": 1, "startTime": 3000}]
    uncached = _uncached_lorrgs_invoke([], transitions)

    def top_fight(report_id: str) -> dict[str, Any]:
        player = {"name": "Topwar", "source_id": 7, "casts": [{"id": 107574, "ts": 1500}, {"id": 107574, "ts": 3000}]}
        return {"report_id": report_id, "fights": [{"fight_id": 1, "duration": 6000, "phases": [{"ts": 2500}], "players": [player]}]}

    def invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        if args[0] == "graphql" and "--variables-json" in args:
            sample_args.append(args)
            selects_encounter = "encounterID" in args[args.index("--query") + 1]
            answers = {
                alias: fight if fight is None else {"fights": [{**fight, **({"encounterID": 7} if selects_encounter else {})}]}
                for alias, fight in sample_fights.items()
            }
            return {"provider": provider, "exit_code": 0, "payload": _envelope({"reportData": answers})}
        if args[0] == "boss-spells":
            return {"provider": provider, "exit_code": 0, "payload": _envelope({})}
        if args[0] == "spec-ranking":
            reports = [top_fight(f"report-{alias}") for alias in sample_fights]
            return {"provider": provider, "exit_code": 0, "payload": _envelope({"reports": reports})}
        return uncached(provider, args, expansion=expansion)

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", invoke)

    result = runner.invoke(warcraft_app, [
        "cooldown-packet", "abcd1234", "--fight-id", "22", "--actor-id", "89", "--spec-slug", "warrior-protection",
        "--phase", "2", "--boss-slug", "lura", "--difficulty", "mythic",
    ])

    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)["data"]


def test_cooldown_packet_segments_top_parses_by_warcraftlogs_phases_when_the_player_windows_came_from_there(
    monkeypatch,
) -> None:
    """Lorrgs marks only some transitions (here, only the intermission's end), so its P2 is not the
    Warcraft Logs P2. Each top parse is segmented by its own Warcraft Logs transitions instead, and a
    sample whose P2 is another encounter phase, or whose transitions could not be read, is left out."""
    sample_fights = {
        "r0": {"id": 1, "startTime": 50000, "endTime": 56000, "phaseTransitions": [
            {"id": 1, "startTime": 50000}, {"id": 2, "startTime": 51200}, {"id": 1, "startTime": 52000}]},
        "r1": {"id": 1, "startTime": 0, "endTime": 6000, "phaseTransitions": [
            {"id": 1, "startTime": 0}, {"id": 3, "startTime": 1000}]},
        "r2": None,
    }
    sample_args: list[list[str]] = []

    comparison = _wcl_segmented_cooldown_packet(monkeypatch, sample_fights, sample_args)["comparison"]

    assert (comparison["status"], comparison["phase_sample_count"]) == ("ready", 1)
    matched, other_phase, unreadable = comparison["samples"]
    assert (matched["phase_window"]["start_ms"], matched["phase_window"]["end_ms"]) == (1200, 2000)
    assert matched["phase_window"]["name"] == "Void Rift"
    assert [cast["timestamp_ms"] for cast in matched["selected_phase_casts"]] == [1500]
    assert other_phase["phase_unavailable_reason"] == "phase_not_in_top_parse"
    assert unreadable["phase_unavailable_reason"] == "top_parse_phase_lookup_failed"
    assert json.loads(sample_args[0][-1]) == {
        "c0": "report-r0", "f0": [1], "c1": "report-r1", "f1": [1], "c2": "report-r2", "f2": [1]}


def test_cooldown_packet_does_not_blame_lorrgs_markers_when_warcraftlogs_top_parses_have_no_transitions(
    monkeypatch,
) -> None:
    """On the Warcraft Logs path the samples' windows come from their own transitions, not Lorrgs markers."""
    sample_fights = {"r0": {"id": 1, "startTime": 0, "endTime": 6000, "phaseTransitions": None}}

    data = _wcl_segmented_cooldown_packet(monkeypatch, sample_fights, [])

    assert (data["comparison"]["status"], data["comparison"]["reason"]) == ("no_phase_data", "top_parse_has_no_phase_markers")
    assert not any("Lorrgs ranking fights" in note for note in data["notes"])


@pytest.mark.parametrize(
    ("actor_args", "exit_code", "code"),
    [
        (["--actor-id", "89", "--spec-slug", "priest-shadow"], 2, "invalid_query"),
        (["--actor-id", "7", "--spec-slug", "warrior-protection"], 4, "actor_id_not_found"),
    ],
)
def test_cooldown_packet_without_lorrgs_checks_the_flags_against_the_fight_roster(
    monkeypatch, actor_args: list[str], exit_code: int, code: str
) -> None:
    """Without Lorrgs the actor comes from flags; a wrong one must fail, not compare the wrong spec."""
    monkeypatch.setattr("warcraft_cli.main.provider_invoke", _uncached_lorrgs_invoke([]))

    result = runner.invoke(warcraft_app, ["cooldown-packet", "abcd1234", "--fight-id", "22", *actor_args, "--phase", "2"])

    assert result.exit_code == exit_code, result.output
    assert json.loads(result.stderr)["error"]["code"] == code


def _playerless_lorrgs_fight_invoke(calls: list[tuple[str, list[str]]]):
    """Lorrgs cached fight 22 with an empty roster; everything else answers as for an uncached report."""
    uncached = _uncached_lorrgs_invoke(calls)

    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        if provider == "lorrgs" and args[0] == "user-report-fights":
            calls.append((provider, args))
            fights = [{"fight_id": 22, "players": [], "phases": []}]
            return {"provider": provider, "exit_code": 0, "payload": _envelope({"fights": fights})}
        return uncached(provider, args, expansion=expansion)

    return fake_provider_invoke


def test_cooldown_packet_degrades_when_lorrgs_cached_the_fight_without_players(monkeypatch) -> None:
    """A playerless Lorrgs fight cannot name the actor, so the fallback flags build the packet."""
    monkeypatch.setattr("warcraft_cli.main.provider_invoke", _playerless_lorrgs_fight_invoke([]))

    result = runner.invoke(
        warcraft_app,
        ["cooldown-packet", "abcd1234", "--fight-id", "22", "--actor-id", "89",
         "--spec-slug", "warrior-protection", "--phase", "1"],
    )

    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)["data"]
    assert (data["lorrgs"]["status"], data["lorrgs"]["reason"]) == ("unavailable", "lorrgs_fight_has_no_players")
    assert data["player"]["name"] == "Buikia"
    assert data["cooldowns"]["player_casts"]["tracked_cast_count"] == 2


def test_cooldown_packet_without_lorrgs_takes_the_actor_and_spec_from_the_warcraftlogs_roster(monkeypatch) -> None:
    """--actor-name and the spec come from the fight's roster, which Lorrgs would otherwise have supplied."""
    calls: list[tuple[str, list[str]]] = []
    playerless = _playerless_lorrgs_fight_invoke(calls)
    identity = {"identity": {"actor_class": "warrior", "spec": "protection"}}
    roster = _envelope({"player_details": {"roles": {"tanks": [
        {"id": 89, "name": "Buikia", "type": "Warrior", "class_spec_identity": identity}]}}})

    def invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        if args[0] == "report-player-details":
            return {"provider": provider, "exit_code": 0, "payload": roster}
        return playerless(provider, args, expansion=expansion)

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", invoke)

    result = runner.invoke(warcraft_app, ["cooldown-packet", "abcd1234", "--fight-id", "22", "--actor-name", "buikia", "--phase", "1"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert (payload["query"]["actor_id"], payload["query"]["spec_slug"]) == (89, "warrior-protection")
    assert payload["data"]["cooldowns"]["player_casts"]["tracked_cast_count"] == 2
    assert ("lorrgs", ["spec-spells", "warrior-protection"]) in calls


def test_cooldown_packet_without_lorrgs_asks_for_the_spec_the_roster_does_not_name(monkeypatch) -> None:
    monkeypatch.setattr("warcraft_cli.main.provider_invoke", _uncached_lorrgs_invoke([]))

    result = runner.invoke(warcraft_app, ["cooldown-packet", "abcd1234", "--fight-id", "22", "--actor-id", "89", "--phase", "2"])

    assert result.exit_code == 2, result.output
    error = json.loads(result.stderr)["error"]
    assert error["code"] == "spec_slug_missing"
    assert "no cached copy" in error["message"] and "--spec-slug" in error["message"]


def test_cooldown_packet_without_lorrgs_reports_a_missing_fight_before_reading_its_roster(monkeypatch) -> None:
    calls: list[tuple[str, list[str]]] = []
    monkeypatch.setattr("warcraft_cli.main.provider_invoke", _uncached_lorrgs_invoke(calls))

    result = runner.invoke(
        warcraft_app,
        ["cooldown-packet", "abcd1234", "--fight-id", "9999", "--actor-id", "89", "--spec-slug", "warrior-protection", "--phase", "1"],
    )

    assert result.exit_code == 4, result.output
    assert json.loads(result.stderr)["error"]["code"] == "fight_not_found"
    assert not any(args[0] == "report-player-details" for _provider, args in calls)


def test_actor_profile_puts_structured_context_under_error_details(monkeypatch) -> None:
    """ERROR_CONTRACT.md reserves `error` for code/message/details; context is not a sibling key."""

    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        assert provider == "warcraftlogs"
        return {
            "provider": provider,
            "exit_code": 0,
            "payload": _envelope({
                "ok": True,
                "player_details": {"roles": {"dps": [{"name": "Someone", "server": "Mal'Ganis", "region": "us"}]}},
            }),
        }

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    result = runner.invoke(warcraft_app, ["actor-profile", "abcd1234", "Missing", "--fight-id", "1"])

    assert result.exit_code == 4, result.output
    payload = json.loads(result.stderr)
    assert set(payload["error"]) == {"code", "message", "details"}
    assert payload["error"]["code"] == "actor_not_found"
    assert payload["error"]["details"]["available_actors"] == ["Someone"]


def test_actor_profile_propagates_the_warcraftlogs_exit_code(monkeypatch) -> None:
    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        return {
            "provider": provider,
            "exit_code": 3,
            "payload": {"ok": False, "error": {"code": "auth_failed", "message": "no token"}},
        }

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    result = runner.invoke(warcraft_app, ["actor-profile", "abcd1234", "Someone"])

    assert result.exit_code == 3, result.output
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "warcraftlogs_lookup_failed"
    assert payload["error"]["details"]["source"]["code"] == "auth_failed"


def test_actor_profile_success_envelope_conforms(monkeypatch) -> None:
    """The crosswalk's success payload is checked against the envelope, not only its failures."""

    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        payload: dict[str, object] = _envelope(
            {"player_details": {"roles": {"dps": [
                {"name": "Someone", "id": 1, "server": "Mal'Ganis", "region": "us", "specs": [{"spec": "Frost", "count": 1}]},
            ]}}}
            if provider == "warcraftlogs"
            else {"character": {"name": "Someone", "profile_url": "https://raider.io/characters/us/malganis/Someone"}}
        )
        return {"provider": provider, "exit_code": 0, "payload": payload}

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    result = runner.invoke(warcraft_app, ["actor-profile", "abcd1234", "Someone", "--fight-id", "1"])
    assert result.exit_code == 0, result.output

    payload = json.loads(result.stdout)
    _assert_wrapper_success_envelope(payload, command="actor-profile")
    assert payload["data"]["sources"]["raiderio"]["profile_url"] == (
        "https://raider.io/characters/us/malganis/Someone"
    )


def _actor_profile_invoke(seen: dict[str, list[str]], fights: list[dict[str, object]]):
    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        seen[args[0]] = args
        if args[0] == "report-fights":
            payload: dict[str, object] = _envelope({"fights": fights})
        elif args[0] == "report-player-details":
            payload = _envelope({"player_details": {"roles": {"dps": [
                {"name": "Someone", "id": 1, "server": "Mal'Ganis", "region": "us", "specs": [{"spec": "Frost", "count": 1}]},
            ]}}})
        else:
            payload = _envelope({"character": {"name": "Someone", "profile_url": "https://raider.io/x"}})
        return {"provider": provider, "exit_code": 0, "payload": payload}

    return fake_provider_invoke


def test_actor_profile_without_fight_id_scopes_player_details_to_the_report_fights(monkeypatch) -> None:
    """Warcraft Logs only answers a scoped playerDetails query, so "whole report" means "its fights".

    An unscoped `report-player-details` fails with `missing_scope` (exit 2), so omitting
    `--fight-id` has to enumerate the report's fights and name them in the call and in `query`.
    """
    seen: dict[str, list[str]] = {}
    monkeypatch.setattr(
        "warcraft_cli.main.provider_invoke",
        _actor_profile_invoke(seen, [{"id": 3}, {"id": 7}]),
    )

    result = runner.invoke(warcraft_app, ["actor-profile", "abcd1234", "Someone"])

    assert result.exit_code == 0, result.output
    assert seen["report-player-details"] == [
        "report-player-details", "abcd1234", "--fight-id", "3", "--fight-id", "7",
    ]
    query = json.loads(result.stdout)["query"]  # `query` is an envelope key, not a data field
    assert query["scoped_fight_ids"] == [3, 7]
    assert query["fight_scope"] == {
        "rule": "kills_first_then_report_order",
        "report_fight_count": 2,
        "kill_fight_count": 0,
        "scoped_fight_count": 2,
        "max_scoped_fights": ACTOR_PROFILE_MAX_SCOPED_FIGHTS,
        "truncated": False,
    }


def test_actor_profile_reads_the_code_and_fight_from_a_report_url(monkeypatch) -> None:
    seen: dict[str, list[str]] = {}
    monkeypatch.setattr("warcraft_cli.main.provider_invoke", _actor_profile_invoke(seen, [{"id": 3}]))

    result = runner.invoke(
        warcraft_app, ["actor-profile", "https://www.warcraftlogs.com/reports/abcd1234EFGH5678#fight=9", "Someone"]
    )

    assert result.exit_code == 0, result.output
    assert "report-fights" not in seen
    assert seen["report-player-details"] == ["report-player-details", "abcd1234EFGH5678", "--fight-id", "9"]


@pytest.mark.parametrize(
    "reference", ["", "  ", "https://www.warcraftlogs.com.evil.com/reports/abcd1234EFGH5678", "https://www.warcraftlogs.com/", "ab/cd"]
)
def test_actor_profile_rejects_a_reference_that_names_no_report_before_any_request(monkeypatch, reference: str) -> None:
    monkeypatch.setattr("warcraft_cli.main.provider_invoke", lambda *args, **kwargs: pytest.fail("no request expected"))

    result = runner.invoke(warcraft_app, ["actor-profile", reference, "Someone"])

    assert result.exit_code == 2, result.output
    assert json.loads(result.stderr)["error"]["code"] == "invalid_query"


def test_actor_profile_scopes_a_long_report_to_its_kills_and_says_so(monkeypatch) -> None:
    """A wipe night must not become fifty --fight-id flags, and the cut must be reported."""
    seen: dict[str, list[str]] = {}
    fights: list[dict[str, object]] = [{"id": index, "kill": index in {40, 44}} for index in range(1, 51)]
    monkeypatch.setattr("warcraft_cli.main.provider_invoke", _actor_profile_invoke(seen, fights))

    result = runner.invoke(warcraft_app, ["actor-profile", "abcd1234", "Someone"])

    assert result.exit_code == 0, result.output
    scoped = [int(value) for value in seen["report-player-details"][3::2]]
    assert len(scoped) == ACTOR_PROFILE_MAX_SCOPED_FIGHTS
    assert scoped[:2] == [40, 44], "kills come first: they carry the roster the caller means"
    query = json.loads(result.stdout)["query"]  # `query` is an envelope key, not a data field
    assert query["scoped_fight_ids"] == scoped
    assert query["fight_scope"]["truncated"] is True
    assert query["fight_scope"]["report_fight_count"] == 50
    assert query["fight_scope"]["kill_fight_count"] == 2


def test_actor_profile_reemits_the_report_fights_failure_with_its_own_exit_code(monkeypatch) -> None:
    """The fight lookup is a real call: when it fails the crosswalk stops with the provider's code.

    Without this the only covered failure is the roster call, so a fight lookup that swallowed an
    auth failure (or reported exit 1 for it) would pass the whole wrapper suite.
    """

    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        assert args[0] == "report-fights", args
        return {
            "provider": provider,
            "exit_code": 3,
            "payload": {
                "ok": False,
                "error": {"code": "missing_credentials", "message": "Warcraft Logs credentials are not configured."},
            },
        }

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    result = runner.invoke(warcraft_app, ["actor-profile", "abcd1234", "Someone"])

    assert result.exit_code == 3, result.output
    error = json.loads(result.stderr)["error"]
    assert error["code"] == "warcraftlogs_lookup_failed"
    assert error["details"]["source"]["code"] == "missing_credentials"
    assert error["details"]["provider"] == "warcraftlogs"


def test_actor_profile_fails_not_found_when_the_report_has_no_fights(monkeypatch) -> None:
    """An empty fight list is a not-found report (exit 4), not an empty roster reported as success."""

    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        assert args[0] == "report-fights", args
        return {"provider": provider, "exit_code": 0, "payload": _envelope({"fights": []})}

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_provider_invoke)

    result = runner.invoke(warcraft_app, ["actor-profile", "abcd1234", "Someone"])

    assert result.exit_code == 4, result.output
    assert json.loads(result.stderr)["error"]["code"] == "report_has_no_fights"


# --- merged search ranking: provider score scales are normalized before the merge ----------------

# Raw scores taken from the live `warcraft search "Un'Goro Crater"` the audit reported: the wiki
# scale runs to 154 for its best row while Wowhead's exact zone match tops out at 47.
_UNGORO_ROWS: dict[str, list[dict[str, object]]] = {
    "warcraft-wiki": [
        {"provider": "warcraft-wiki", "id": 1, "name": "Un'Goro Crater", "kind": "article", "ranking": {"score": 154}},
        {"provider": "warcraft-wiki", "id": 2, "name": "Un'Goro Crater (Classic)", "kind": "article", "ranking": {"score": 94}},
        {"provider": "warcraft-wiki", "id": 3, "name": "Ravasaur", "kind": "article", "ranking": {"score": 60}},
        {"provider": "warcraft-wiki", "id": 4, "name": "Diemetradon", "kind": "article", "ranking": {"score": 58}},
    ],
    "wowhead": [
        {"provider": "wowhead", "id": 490, "name": "Un'Goro Crater", "kind": "zone", "ranking": {"score": 47}},
        {"provider": "wowhead", "id": 491, "name": "Un'Goro Crater Fishing", "kind": "quest", "ranking": {"score": 12}},
    ],
}


def _ungoro_provider_search(provider: str, query: str, *, limit: int = 5, expansion: str | None = None) -> dict[str, object]:
    rows = _UNGORO_ROWS.get(provider, [])
    # The wiki reports MediaWiki's own hit count, far above the rows it returns.
    total = 1934 if provider == "warcraft-wiki" else len(rows)
    data = {"results": rows, "count": len(rows), "total_matches": total, "truncated": total > len(rows)}
    return {"provider": provider, "exit_code": 0, "payload": _envelope({"ok": True, "provider": provider, **data})}


def test_warcraft_search_normalizes_provider_scales_before_merging(monkeypatch) -> None:
    """Wowhead's exact zone match must reach the merged list ahead of the wiki's filler rows.

    This drives the whole `search` wiring, not `wrapper_search_ranking` alone: merging the providers'
    raw scores (or dividing every provider by the same constant) puts all four wiki rows on top and
    pushes Wowhead's exact match off the default page, which is the bug the audit hit live.
    """
    monkeypatch.setattr("warcraft_cli.main.provider_search", _ungoro_provider_search)

    result = runner.invoke(warcraft_app, ["search", "un'goro crater", "--limit", "5"])
    assert result.exit_code == 0, result.output

    data = json.loads(result.stdout)["data"]
    assert [(row["provider"], row["name"]) for row in data["results"]][:3] == [
        ("wowhead", "Un'Goro Crater"),
        ("warcraft-wiki", "Un'Goro Crater"),
        ("warcraft-wiki", "Un'Goro Crater (Classic)"),
    ]
    # The bare query names the zone exactly, so the entity provider's row anchors the page.
    assert data["results"][0]["wrapper_ranking"]["anchor"] is True
    assert data["results"][0]["wrapper_ranking"]["provider_score"] == 47
    assert data["results"][0]["wrapper_ranking"]["provider_max_score"] == 47


def test_warcraft_search_counts_the_page_and_keeps_each_providers_total_under_brief(monkeypatch) -> None:
    """`count` is the merged page's length, `truncated` says whether `--limit` dropped merged rows,
    and each provider's own `total_matches` survives `--brief` in the merge policy."""
    monkeypatch.setattr("warcraft_cli.main.provider_search", _ungoro_provider_search)

    cut = json.loads(runner.invoke(warcraft_app, ["search", "un'goro crater", "--limit", "5", "--brief"]).stdout)["data"]
    assert cut["count"] == len(cut["results"]) == 5
    assert cut["truncated"] is True
    assert cut["merge_policy"]["candidate_row_count"] == 6
    totals = cut["merge_policy"]["provider_total_matches"]
    assert (totals["warcraft-wiki"], totals["wowhead"], totals["method"]) == (1934, 2, 0)
    assert set(totals) == set(cut["included_providers"])

    whole = json.loads(runner.invoke(warcraft_app, ["search", "un'goro crater", "--limit", "10"]).stdout)["data"]
    assert whole["count"] == len(whole["results"]) == 6
    assert whole["truncated"] is False


def test_search_brief_shrinks_the_payload_and_compact_stays_a_global_flag(monkeypatch) -> None:
    """One word, one meaning: `--brief` shrinks the wrapper payload, `--compact` truncates strings."""
    monkeypatch.setattr("warcraft_cli.main.provider_search", _ungoro_provider_search)

    brief = runner.invoke(warcraft_app, ["search", "un'goro crater", "--brief"])
    assert brief.exit_code == 0, brief.output
    assert json.loads(brief.stdout)["data"]["providers"] == []

    after_subcommand = runner.invoke(warcraft_app, ["search", "un'goro crater", "--compact"])
    assert after_subcommand.exit_code == 2, after_subcommand.output

    before_subcommand = runner.invoke(warcraft_app, ["--compact", "search", "un'goro crater"])
    assert before_subcommand.exit_code == 0, before_subcommand.output
    assert json.loads(before_subcommand.stdout)["data"]["providers"] != []


def test_resolve_brief_shrinks_the_payload_and_compact_stays_a_global_flag(monkeypatch) -> None:
    def fake_provider_resolve(provider: str, query: str, *, limit: int = 5, expansion: str | None = None, entity_types: tuple[str, ...] = ()) -> dict[str, object]:
        return {"provider": provider, "exit_code": 0, "payload": _envelope({"ok": True, "resolved": False})}

    monkeypatch.setattr("warcraft_cli.main.provider_resolve", fake_provider_resolve)

    brief = runner.invoke(warcraft_app, ["resolve", "un'goro crater", "--brief"])
    assert brief.exit_code == 0, brief.output
    assert json.loads(brief.stdout)["data"]["providers"] == []

    after_subcommand = runner.invoke(warcraft_app, ["resolve", "un'goro crater", "--compact"])
    assert after_subcommand.exit_code == 2, after_subcommand.output


def _failing_lorrgs_invoke(error: dict[str, object], exit_code: int):
    """Lorrgs is down: every Lorrgs call fails with `error`, while Warcraft Logs answers."""

    def fake_provider_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        if provider == "lorrgs":
            return {"provider": provider, "exit_code": exit_code, "payload": {"ok": False, "error": error}}
        if provider == "warcraftlogs" and args[0] == "graphql":
            return {"provider": provider, "exit_code": 0, "payload": _wcl_phases(None)}
        if provider == "warcraftlogs" and args[0] == "report-player-details":
            return {"provider": provider, "exit_code": 0, "payload": _WCL_ROSTER}
        if provider == "warcraftlogs" and args[0] == "report-fights":
            return {"provider": provider, "exit_code": 0, "payload": _WCL_FIGHTS}
        if provider == "warcraftlogs" and args[0] == "report-events":
            return {"provider": provider, "exit_code": 0, "payload": _WCL_EVENTS}
        raise AssertionError((provider, args))

    return fake_provider_invoke


def test_cooldown_packet_degrade_reports_the_real_lorrgs_failure(monkeypatch) -> None:
    """Only a `not_found` means "not cached"; a timeout must say so instead of blaming the report."""
    monkeypatch.setattr(
        "warcraft_cli.main.provider_invoke",
        _failing_lorrgs_invoke({"code": "timeout", "message": "Lorrgs API request timed out."}, 5),
    )

    # The retry the failure advises: --actor-id, --spec-slug and, with Lorrgs down, --spell-id.
    result = runner.invoke(
        warcraft_app,
        ["cooldown-packet", "abcd1234", "--fight-id", "22", "--actor-id", "89",
         "--spec-slug", "warrior-protection", "--spell-id", "107574", "--phase", "2"],
    )
    assert result.exit_code == 0, result.output

    data = json.loads(result.stdout)["data"]
    # Lorrgs' spell names are gone with it, so the cooldowns are named by id and a note says why.
    assert data["cooldowns"]["player_casts"]["tracked_cast_count"] == 2
    assert data["cooldowns"]["tracked_spells"] == [{"spell_id": 107574, "name": "spell:107574"}]
    assert any("spec spell metadata was unavailable" in note for note in data["notes"])
    assert data["lorrgs"]["status"] == "unavailable"
    assert data["lorrgs"]["source"]["code"] == "timeout"
    assert "no cached copy" not in data["lorrgs"]["message"]
    assert "timed out" in data["lorrgs"]["message"]
    # The requested phase could not be applied, and the payload says which phase was asked for.
    assert data["phase"]["requested"] == 2
    assert data["phase"]["selected"] is None


def test_cooldown_packet_without_an_actor_lists_the_warcraftlogs_roster(monkeypatch) -> None:
    """Without an actor flag the command fails naming the true Lorrgs reason and the fight's players."""
    monkeypatch.setattr(
        "warcraft_cli.main.provider_invoke",
        _failing_lorrgs_invoke({"code": "rate_limited", "message": "Lorrgs API returned HTTP 429."}, 5),
    )

    result = runner.invoke(warcraft_app, ["cooldown-packet", "abcd1234", "--fight-id", "22", "--phase", "2"])

    assert result.exit_code == 2, result.output
    error = json.loads(result.stderr)["error"]
    assert error["code"] == "missing_actor"
    assert "HTTP 429" in error["message"] and "--actor-name" in error["message"]
    assert "only serves reports it has already cached" not in error["message"]
    assert error["details"]["available_players"] == [{"id": 89, "name": "Buikia", "type": "Warrior"}]


# --- resolve agrees with search ---------------------------------------------------------------------


def _match(provider: str, name: str, kind: str, score: int) -> dict[str, Any]:
    return {"provider": provider, "kind": kind, "id": f"{provider}:{name}", "name": name, "ranking": {"score": score}}


def _stub_resolve_seam(monkeypatch, answers: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Each provider's resolve answer; providers not named answer with no match. Records the kwargs."""
    seen: list[dict[str, Any]] = []

    def fake_resolve(provider: str, query: str, **kwargs: Any) -> dict[str, Any]:
        seen.append(kwargs)
        data = answers.get(provider, {"resolved": False, "confidence": "none", "match": None})
        return {"provider": provider, "exit_code": 0, "payload": _envelope(data)}

    monkeypatch.setattr("warcraft_cli.main.provider_resolve", fake_resolve)
    return seen


def test_warcraft_resolve_asks_providers_at_once_and_keeps_registry_order_and_cache_counts(monkeypatch) -> None:
    """The fan-out runs concurrently: each provider still lands in registry order, and the lookups a
    provider makes on a worker thread still count in the wrapper's provenance.cache."""
    import threading
    import time

    from warcraft_core.cache_ledger import record_cache_lookup

    threads: set[str] = set()

    def slow_first(provider: str, query: str, **kwargs: Any) -> dict[str, Any]:
        threads.add(threading.current_thread().name)
        # Earlier providers answer later, so collecting in completion order would reverse them.
        time.sleep(0.05 if provider == "wowhead" else 0)
        record_cache_lookup("file", hit=True, age_seconds=10, ttl_seconds=60)
        return {"provider": provider, "exit_code": 0, "payload": _envelope({"resolved": False, "confidence": "none", "match": None})}

    monkeypatch.setattr("warcraft_cli.main.provider_resolve", slow_first)

    payload = json.loads(runner.invoke(warcraft_app, ["resolve", "un'goro crater"]).stdout)

    included = payload["data"]["included_providers"]
    assert [row["provider"] for row in payload["data"]["providers"]] == included
    assert payload["provenance"]["cache"]["lookups"] == len(included) > 1
    assert len(threads) > 1


def test_warcraft_resolve_answers_with_the_row_search_ranks_first(monkeypatch) -> None:
    """A bare entity name resolves to the entity, however large the wiki's local score scale is."""
    zone = _match("wowhead", "Un'Goro Crater", "zone", 89)
    article = _match("warcraft-wiki", "Un'Goro Crater", "article", 116)
    _stub_resolve_seam(monkeypatch, {
        "wowhead": {"resolved": True, "confidence": "high", "match": zone, "next_command": "wowhead entity zone 490"},
        "warcraft-wiki": {"resolved": True, "confidence": "high", "match": article, "next_command": "warcraft-wiki article X"},
    })
    rows = {"wowhead": [zone], "warcraft-wiki": [article]}
    monkeypatch.setattr(
        "warcraft_cli.main.provider_search",
        lambda provider, query, **kwargs: {
            "provider": provider, "exit_code": 0, "payload": _envelope({"results": rows.get(provider, [])}),
        },
    )

    resolved = json.loads(runner.invoke(warcraft_app, ["resolve", "un'goro crater"]).stdout)["data"]
    searched = json.loads(runner.invoke(warcraft_app, ["search", "un'goro crater"]).stdout)["data"]

    assert searched["results"][0]["provider"] == "wowhead"
    assert resolved["selected_provider"] == "wowhead"
    assert resolved["next_command"] == "wowhead entity zone 490"


def test_warcraft_resolve_never_answers_a_guide_query_with_lorrgs_spec_metadata(monkeypatch) -> None:
    """Live `frost mage guide` scales: Lorrgs 96 against Wowhead 40 and Method 33, all 'high'."""
    lorrgs = {"resolved": True, "confidence": "high", "match": {**_match("lorrgs", "Frost Mage", "spec", 96), "kind": "spec"},
              "next_command": "lorrgs spec mage-frost"}
    answers = {
        "lorrgs": lorrgs,
        "wowhead": {"resolved": True, "confidence": "high", "match": _match("wowhead", "Frost Mage DPS Guide", "guide", 40),
                    "next_command": "wowhead guide 3047"},
        "method": {"resolved": True, "confidence": "high", "match": _match("method", "Frost Mage", "guide", 33),
                   "next_command": "method guide frost-mage"},
    }
    seen = _stub_resolve_seam(monkeypatch, answers)

    data = json.loads(runner.invoke(warcraft_app, ["resolve", "frost mage guide", "--limit", "1"]).stdout)["data"]
    assert data["resolved"] is True
    assert data["selected_provider"] in {"wowhead", "method"}
    # The caller's --limit never reaches a provider: it would hide the rivals its confidence needs.
    assert all("limit" not in kwargs for kwargs in seen)

    _stub_resolve_seam(monkeypatch, {"lorrgs": lorrgs})
    alone = json.loads(runner.invoke(warcraft_app, ["resolve", "frost mage guide"]).stdout)["data"]
    assert alone["resolved"] is False
    assert alone["next_command"] is None
    assert alone["best_unresolved_candidate"]["provider"] == "lorrgs"
    assert alone["best_unresolved_candidate"]["unresolved_reason"] == "provider_family_ranked_down_by_query_intent"


def test_warcraft_resolve_does_not_answer_when_a_better_ranked_candidate_is_unresolved(monkeypatch) -> None:
    """The top-ranked row decides; a lower row's 'high' does not stand in for a medium match above it."""
    _stub_resolve_seam(monkeypatch, {
        "wowhead": {"resolved": False, "confidence": "medium", "match": _match("wowhead", "Thunderfury", "item", 48)},
        "warcraft-wiki": {"resolved": True, "confidence": "high", "match": _match("warcraft-wiki", "Thunderfury lore", "article", 60),
                          "next_command": "warcraft-wiki article Thunderfury"},
    })

    data = json.loads(runner.invoke(warcraft_app, ["resolve", "thunderfury", "--ranking-debug"]).stdout)["data"]

    assert data["resolved"] is False
    assert data["best_unresolved_candidate"]["provider"] == "wowhead"
    assert data["best_unresolved_candidate"]["unresolved_reason"] == "provider_did_not_resolve"
    assert [(row["provider"], row["resolved"]) for row in data["ranking_debug"]] == [
        ("wowhead", False), ("warcraft-wiki", True)]


def test_warcraft_resolve_never_lets_a_low_confidence_guess_block_another_providers_answer(monkeypatch) -> None:
    """Lorrgs reports a tie it could not break as its top row at `low`; ranked first, it is skipped."""
    lorrgs = {"resolved": False, "confidence": "low", "fallback_search_command": "lorrgs search 'frost ulgrax'",
              "match": _match("lorrgs", "Frost Death Knight on Ulgrax the Devourer", "spec_ranking", 99)}
    _stub_resolve_seam(monkeypatch, {
        "lorrgs": lorrgs,
        "warcraft-wiki": {"resolved": True, "confidence": "high", "match": _match("warcraft-wiki", "Ulgrax", "article", 60),
                          "next_command": "warcraft-wiki article Ulgrax"},
    })

    data = json.loads(runner.invoke(warcraft_app, ["resolve", "frost ulgrax", "--ranking-debug"]).stdout)["data"]

    assert [row["provider"] for row in data["ranking_debug"]] == ["lorrgs", "warcraft-wiki"]
    assert (data["resolved"], data["selected_provider"]) == (True, "warcraft-wiki")
    assert data["next_command"] == "warcraft-wiki article Ulgrax"
    assert data["best_unresolved_candidate"] is None

    _stub_resolve_seam(monkeypatch, {"lorrgs": lorrgs})
    alone = json.loads(runner.invoke(warcraft_app, ["resolve", "frost ulgrax"]).stdout)["data"]
    assert (alone["resolved"], alone["next_command"]) == (False, None)
    assert alone["best_unresolved_candidate"]["provider"] == "lorrgs"
    assert alone["best_unresolved_candidate"]["unresolved_reason"] == "provider_did_not_resolve"
    assert alone["fallback_search_command"] == "lorrgs search 'frost ulgrax'"


def test_warcraft_resolve_hints_follow_the_ranking_and_name_a_blocked_resolved_answer(monkeypatch) -> None:
    """Live `frost dk mythic+ build`: Icy Veins' Mythic Plus page ties with its builds page (low) and
    outscores the wiki's off-intent medium row. The low tie still cannot be the answer, but the hints
    follow the ranking, so they start at the Icy Veins page instead of the wiki's Pit of Saron, and the
    page another provider resolved below them stays one command away."""
    _stub_resolve_seam(monkeypatch, {
        "icy-veins": {"resolved": False, "confidence": "low", "fallback_search_command": "icy-veins search 'frost dk mythic+ build'",
                      "match": _match("icy-veins", "Frost Death Knight PvE DPS Mythic Plus Tips", "guide", 21)},
        "warcraft-wiki": {"resolved": False, "confidence": "medium",
                          "fallback_search_command": "warcraft-wiki search 'frost dk mythic+ build'",
                          "match": _match("warcraft-wiki", "Pit of Saron", "article", 30)},
        "method": {"resolved": True, "confidence": "high", "next_command": "method guide frost-death-knight",
                   "match": _match("method", "Frost Death Knight Mythic+", "guide", 5)},
    })

    data = json.loads(runner.invoke(warcraft_app, ["resolve", "frost dk mythic+ build", "--ranking-debug"]).stdout)["data"]

    assert [row["provider"] for row in data["ranking_debug"]] == ["icy-veins", "warcraft-wiki", "method"]
    assert data["resolved"] is False
    assert data["best_unresolved_candidate"]["provider"] == "icy-veins"
    assert [row["provider"] for row in data["fallback_search_commands"]] == ["icy-veins", "warcraft-wiki"]
    assert data["fallback_search_command"] == "icy-veins search 'frost dk mythic+ build'"
    assert [(row["provider"], row["next_command"]) for row in data["provider_resolved_candidates"]] == [
        ("method", "method guide frost-death-knight")]


def test_warcraft_resolve_does_not_answer_with_a_provider_match_at_medium_confidence(monkeypatch) -> None:
    """Live `lorrgs resolve storm` found a partial boss match at medium; that is not the answer."""
    _stub_resolve_seam(monkeypatch, {
        "lorrgs": {"resolved": False, "confidence": "medium",
                   "match": _match("lorrgs", "Composition ranking for Raszageth the Storm-Eater", "comp_ranking", 50)},
    })

    data = json.loads(runner.invoke(warcraft_app, ["resolve", "storm"]).stdout)["data"]

    assert (data["resolved"], data["selected_provider"], data["next_command"]) == (False, None, None)
    assert data["best_unresolved_candidate"]["provider"] == "lorrgs"
    assert data["best_unresolved_candidate"]["confidence"] == "medium"
    assert data["best_unresolved_candidate"]["unresolved_reason"] == "provider_did_not_resolve"


def test_warcraft_search_forwards_the_requested_limit_to_every_provider(monkeypatch) -> None:
    limits: list[object] = []

    def spy(provider: str, query: str, **kwargs: Any) -> dict[str, Any]:
        limits.append(kwargs.get("limit"))
        return {"provider": provider, "exit_code": 0, "payload": _envelope({"results": []})}

    monkeypatch.setattr("warcraft_cli.main.provider_search", spy)

    assert runner.invoke(warcraft_app, ["search", "thunderfury", "--limit", "7"]).exit_code == 0
    assert limits and set(limits) == {7}


# --- fanout failure exit codes ----------------------------------------------------------------------


def _failing_search(codes: dict[str, tuple[str, int]]) -> Callable[..., dict[str, Any]]:
    """Providers named in ``codes`` fail with that code and exit code; the others answer empty."""

    def fake(provider: str, query: str, **kwargs: Any) -> dict[str, Any]:
        if provider not in codes:
            return {"provider": provider, "exit_code": 0, "payload": _envelope({"results": []})}
        code, exit_code = codes[provider]
        envelope = {"ok": False, "provider": provider, "command": "search", "kind": "error",
                    "data": {}, "error": {"code": code, "message": f"{provider} {code}"}}
        return {"provider": provider, "exit_code": exit_code, "payload": envelope}

    return fake


def test_warcraft_search_crash_of_the_only_searcher_is_not_reported_as_retryable(monkeypatch) -> None:
    """`--expansion classic` leaves Wowhead as the only free-text searcher; its crash exits 1, not 5."""
    monkeypatch.setattr("warcraft_cli.main.provider_search", _failing_search({"wowhead": ("internal_error", 1)}))

    result = runner.invoke(warcraft_app, ["--expansion", "classic", "search", "thunderfury"])

    assert result.exit_code == 1, result.output
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "internal_error"
    assert payload["error"]["details"]["failed_providers"][0]["exit_code"] == 1


@pytest.mark.parametrize(
    ("failures", "code", "exit_code"),
    [
        ({"wowhead": ("internal_error", 1), "warcraftlogs": ("network_error", 5)}, "providers_failed", 1),
        ({"wowhead": ("timeout", 5), "warcraftlogs": ("network_error", 5)}, "upstream_error", 5),
    ],
    ids=["crash_and_outage", "two_outages"],
)
def test_warcraft_search_mixed_failures_exit_retryable_only_when_every_one_is(
    monkeypatch, failures: dict[str, tuple[str, int]], code: str, exit_code: int
) -> None:
    monkeypatch.setattr("warcraft_cli.main.provider_search", _failing_search(failures))

    result = runner.invoke(warcraft_app, ["--expansion", "classic", "search", "thunderfury"])

    assert result.exit_code == exit_code, result.output
    assert json.loads(result.stderr)["error"]["code"] == code


# --- guide-compare-query: guide selection and provider failures ------------------------------------


def _guide_seam(monkeypatch, *, resolve: dict[str, Any], search: list[dict[str, Any]]) -> None:
    monkeypatch.setattr(
        "warcraft_cli.main.provider_resolve",
        lambda provider, query, **kwargs: {"provider": provider, "exit_code": 0, "payload": _envelope(resolve)},
    )
    monkeypatch.setattr(
        "warcraft_cli.main.provider_search",
        lambda provider, query, **kwargs: {"provider": provider, "exit_code": 0, "payload": _envelope({"results": search})},
    )


def _guide_row(slug: str, score: int, kind: str = "guide") -> dict[str, Any]:
    return {"provider": "method", "kind": kind, "id": slug, "name": slug, "url": f"https://example.test/{slug}",
            "ranking": {"score": score}, "follow_up": {"command": f"method guide {slug}"}}


def test_guide_compare_query_does_not_accept_an_unresolved_match(monkeypatch) -> None:
    from warcraft_cli.guide_compare import _resolve_guide_compare_candidate
    from warcraft_cli.main import _provider_calls

    _guide_seam(monkeypatch, resolve={"resolved": False, "confidence": "medium", "match": _guide_row("weak-guess", 40)},
                search=[_guide_row("decisive-guide", 90)])

    candidate, decline = _resolve_guide_compare_candidate("method", "mw guide", expansion=None, calls=_provider_calls())

    assert candidate is not None and candidate["ref"] == "decisive-guide"
    assert candidate["selection_source"] == "search_fallback"
    assert decline == {}


@pytest.mark.parametrize(
    ("resolve", "search", "reason", "resolve_reason"),
    [
        ({"resolved": True, "confidence": "high", "match": _guide_row("thunderfury", 90, kind="item")}, [],
         "provider_search_returned_no_results", "resolved_non_guide:item"),
        ({"resolved": False, "match": None}, [_guide_row("only-hit", 60)],
         "search_single_result_not_strong_enough", "provider_did_not_resolve_query"),
        ({"resolved": False, "match": None}, [_guide_row("an-item", 95, kind="item"), _guide_row("a-guide", 20)],
         "search_top_non_guide:item", "provider_did_not_resolve_query"),
    ],
    ids=["resolved_non_guide", "weak_single_hit", "search_top_non_guide"],
)
def test_guide_compare_query_declines_each_unusable_selection(
    monkeypatch, resolve: dict[str, Any], search: list[dict[str, Any]], reason: str, resolve_reason: str
) -> None:
    from warcraft_cli.guide_compare import _resolve_guide_compare_candidate
    from warcraft_cli.main import _provider_calls

    _guide_seam(monkeypatch, resolve=resolve, search=search)

    candidate, decline = _resolve_guide_compare_candidate("method", "q", expansion=None, calls=_provider_calls())

    assert candidate is None
    assert (decline["status"], decline["reason"], decline["resolve_reason"]) == ("skipped", reason, resolve_reason)


def _network_failure(provider: str, command: str) -> dict[str, Any]:
    envelope = {"ok": False, "provider": provider, "command": command, "kind": "error", "data": {},
                "error": {"code": "network_error", "message": "ConnectError: offline"}}
    return {"provider": provider, "exit_code": 5, "payload": envelope}


def test_guide_compare_query_total_provider_outage_is_a_network_failure(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("warcraft_cli.main.provider_resolve", lambda provider, query, **kwargs: _network_failure(provider, "resolve"))
    monkeypatch.setattr("warcraft_cli.main.provider_search", lambda provider, query, **kwargs: _network_failure(provider, "search"))

    result = runner.invoke(warcraft_app, ["guide-compare-query", "fury warrior guide", "--out-root", str(tmp_path / "o")])

    assert result.exit_code == 5, result.output
    error = json.loads(result.stderr)["error"]
    assert error["code"] == "network_error"
    rows = error["details"]["provider_results"]
    assert {row["reason"] for row in rows} == {"provider_failed"}
    assert all(row["error"] == {"code": "network_error", "message": "ConnectError: offline"} for row in rows)


def test_guide_compare_query_reports_a_resolve_outage_that_search_could_not_make_up_for(monkeypatch) -> None:
    from warcraft_cli.guide_compare import _resolve_guide_compare_candidate
    from warcraft_cli.main import _provider_calls

    monkeypatch.setattr("warcraft_cli.main.provider_resolve", lambda provider, query, **kwargs: _network_failure(provider, "resolve"))
    monkeypatch.setattr(
        "warcraft_cli.main.provider_search",
        lambda provider, query, **kwargs: {"provider": provider, "exit_code": 0, "payload": _envelope({"results": []})},
    )

    candidate, decline = _resolve_guide_compare_candidate("icy-veins", "mw guide", expansion=None, calls=_provider_calls())

    assert candidate is None
    assert (decline["status"], decline["reason"], decline["resolve_reason"]) == ("error", "provider_failed", "provider_failed")
    assert decline["error"]["code"] == "network_error"


def test_guide_compare_query_one_missing_guide_and_one_outage_is_insufficient_guides(monkeypatch, tmp_path) -> None:
    """Only an outage of every provider that contributed nothing is retryable; a missing guide is not."""
    monkeypatch.setattr(
        "warcraft_cli.main.provider_resolve",
        lambda provider, query, **kwargs: {"provider": provider, "exit_code": 0, "payload": _envelope({"resolved": False, "match": None})},
    )
    monkeypatch.setattr(
        "warcraft_cli.main.provider_search",
        lambda provider, query, **kwargs: _network_failure(provider, "search") if provider == "icy-veins"
        else {"provider": provider, "exit_code": 0, "payload": _envelope({"results": []})},
    )

    result = runner.invoke(warcraft_app, [
        "guide-compare-query", "mw guide", "--provider", "method", "--provider", "icy-veins", "--out-root", str(tmp_path / "o"),
    ])

    assert result.exit_code == 1, result.output
    error = json.loads(result.stderr)["error"]
    assert error["code"] == "insufficient_guides"
    assert [(row["provider"], row["reason"]) for row in error["details"]["provider_results"]] == [
        ("method", "provider_search_returned_no_results"), ("icy-veins", "provider_failed")]


def test_guide_compare_query_export_outage_keeps_each_providers_error(monkeypatch, tmp_path) -> None:
    _guide_seam(monkeypatch, resolve={"resolved": True, "confidence": "high", "match": _guide_row("mw", 90)}, search=[])
    monkeypatch.setattr("warcraft_cli.main.provider_invoke", lambda p, args, **k: _network_failure(p, args[0]))

    result = runner.invoke(warcraft_app, [
        "guide-compare-query", "mistweaver monk guide", "--provider", "method", "--provider", "icy-veins",
        "--out-root", str(tmp_path / "o"),
    ])

    assert result.exit_code == 5, result.output
    rows = json.loads(result.stderr)["error"]["details"]["provider_results"]
    assert [(row["reason"], row["error"]["code"], row["bundle_path"]) for row in rows] == [
        ("guide_export_failed", "network_error", None)] * 2


def test_guide_compare_query_fails_when_the_requested_simc_handoff_produced_nothing(monkeypatch, tmp_path) -> None:
    _guide_seam(monkeypatch, resolve={"resolved": True, "confidence": "high", "match": _guide_row("mw", 90)}, search=[])

    def fake_invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, Any]:
        if provider == "simc":
            envelope = {"ok": False, "error": {"code": "simc_not_found", "message": "no simc binary"}}
            return {"provider": "simc", "exit_code": 1, "payload": envelope}
        export_dir = Path(args[3])
        write_article_bundle(
            _comparison_payload(provider=provider, slug=args[1], page_url=f"https://example.test/{provider}",
                                page_title="g", analysis_tags=["builds_talents"], build_code="ABC123"),
            provider=provider, export_dir=export_dir,
        )
        return {"provider": provider, "exit_code": 0, "payload": _envelope({"output_dir": str(export_dir)})}

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", fake_invoke)

    result = runner.invoke(warcraft_app, [
        "guide-compare-query", "mistweaver monk guide", "--provider", "method", "--provider", "icy-veins",
        "--out-root", str(tmp_path / "o"), "--simc-build-handoff",
    ])

    assert result.exit_code == 1, result.output
    error = json.loads(result.stderr)["error"]
    assert error["code"] == "simc_handoff_failed"
    assert error["details"]["simc_build_handoff"]["summary"]["simc_handoff_status"] == "all_handoffs_failed"
    # The comparison itself is still valid evidence and rides along.
    assert error["details"]["comparison"]["compared_bundle_count"] == 2


def test_guide_builds_simc_reports_partial_when_a_leg_worked_for_some_builds(monkeypatch, tmp_path) -> None:
    references = [
        {"kind": "build_reference", "reference_type": "wowhead_talent_calc_url",
         "url": f"https://www.wowhead.com/talent-calc/monk/mistweaver/{code}", "label": code, "build_code": code,
         "source_urls": ["https://www.method.gg/guides/mistweaver-monk"]}
        for code in ("ABC123", "DEF456")
    ]
    bundle = _bundle_with_references(tmp_path, references)
    decodes: list[PacketInput | str] = []

    def second_decode_fails(command: str, build: PacketInput | str, *, describe: DescribeOptions | None = None) -> dict[str, object]:
        if command == "decode-build":
            decodes.append(build)
            if len(decodes) == 2:
                return _simc_result({"ok": False, "error": {"code": "invalid_query", "message": "bad build"}}, exit_code=2)
        return _simc_result({"ok": True})

    monkeypatch.setattr("warcraft_cli.main.simc_call", second_decode_fails)

    result = runner.invoke(warcraft_app, ["guide-builds-simc", str(bundle)])
    assert result.exit_code == 0, result.output

    summary = json.loads(result.stdout)["data"]["summary"]
    assert (summary["decode_success_count"], summary["partial_requested_legs"]) == (1, ["decode"])
    assert summary["simc_handoff_status"] == "partial"


# --- cooldown-packet: completeness notes, overrides and not-found paths ----------------------------


def _cooldown_invoke_with(
    calls: list[tuple[str, list[str]]], overrides: dict[tuple[str, str], dict[str, Any]]
) -> Callable[..., dict[str, object]]:
    """``_cooldown_packet_invoke`` with some ``(provider, command)`` answers replaced."""
    base = _cooldown_packet_invoke(calls)

    def invoke(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, object]:
        override = overrides.get((provider, args[0]))
        if override is None:
            return base(provider, args, expansion=expansion)
        calls.append((provider, args))
        return {"provider": provider, **override}

    return invoke


def _ok_answer(data: dict[str, Any]) -> dict[str, Any]:
    return {"exit_code": 0, "payload": _envelope(data)}


def _failed_answer(code: str, exit_code: int) -> dict[str, Any]:
    return {"exit_code": exit_code, "payload": {"ok": False, "error": {"code": code, "message": f"lorrgs {code}"}}}


def test_cooldown_packet_says_when_warcraftlogs_truncated_the_cast_events(monkeypatch) -> None:
    events = {"next_page_timestamp": 104000, "events": [
        {"timestamp": 100500, "sourceID": 89, "abilityGameID": 107574, "targetID": -1, "type": "cast"}]}
    monkeypatch.setattr("warcraft_cli.main.provider_invoke",
                        _cooldown_invoke_with([], {("warcraftlogs", "report-events"): _ok_answer(events)}))

    result = runner.invoke(warcraft_app, _cooldown_packet_args())

    assert result.exit_code == 0, result.output
    assert any("next_page_timestamp" in note for note in json.loads(result.stdout)["data"]["notes"])


def test_cooldown_packet_ranks_at_the_explicit_difficulty_and_metric(monkeypatch) -> None:
    calls: list[tuple[str, list[str]]] = []
    monkeypatch.setattr("warcraft_cli.main.provider_invoke", _cooldown_packet_invoke(calls, wcl_fight_difficulty=5))

    result = runner.invoke(warcraft_app, [*_cooldown_packet_args(), "--difficulty", "heroic", "--metric", "hps"])

    assert result.exit_code == 0, result.output
    assert ("lorrgs", ["spec-ranking", "warrior-protection", "lura", "--difficulty", "heroic", "--metric", "hps"]) in calls
    assert json.loads(result.stdout)["query"]["difficulty"] == "heroic"


def test_cooldown_packet_names_a_failed_ranking_lookup(monkeypatch) -> None:
    monkeypatch.setattr("warcraft_cli.main.provider_invoke",
                        _cooldown_invoke_with([], {("lorrgs", "spec-ranking"): _failed_answer("network_error", 5)}))

    result = runner.invoke(warcraft_app, _cooldown_packet_args())

    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)["data"]
    assert (data["comparison"]["status"], data["comparison"]["reason"]) == ("unavailable", "lorrgs_spec_ranking_failed")
    assert any("sources.lorrgs_spec_ranking.error" in note for note in data["notes"])
    assert not any("Top-parse samples are comparison evidence" in note for note in data["notes"])


def test_cooldown_packet_names_a_failed_boss_spell_lookup(monkeypatch) -> None:
    monkeypatch.setattr("warcraft_cli.main.provider_invoke",
                        _cooldown_invoke_with([], {("lorrgs", "boss-spells"): _failed_answer("network_error", 5)}))

    result = runner.invoke(warcraft_app, _cooldown_packet_args())

    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)["data"]
    assert data["boss"]["selected_phase_casts"][0]["spell"]["name"] == "spell:900001"
    assert any("sources.lorrgs_boss_spells.error" in note for note in data["notes"])


def test_cooldown_packet_fails_when_the_spec_has_no_tracked_spells(monkeypatch) -> None:
    monkeypatch.setattr("warcraft_cli.main.provider_invoke",
                        _cooldown_invoke_with([], {("lorrgs", "spec-spells"): _ok_answer({})}))

    result = runner.invoke(warcraft_app, _cooldown_packet_args())

    assert result.exit_code == 1, result.output
    assert json.loads(result.stderr)["error"]["code"] == "no_tracked_spells"


@pytest.mark.parametrize(
    ("fights", "code", "exit_code"),
    [
        ([{"id": 7, "start_time": 1}, {"id": 9, "start_time": 2}], "fight_not_found", 4),
        ([{"id": 22}], "fight_start_missing", 1),
    ],
    ids=["fight_missing", "start_missing"],
)
def test_cooldown_packet_tells_a_missing_fight_from_a_missing_start(
    monkeypatch, fights: list[dict[str, Any]], code: str, exit_code: int
) -> None:
    monkeypatch.setattr("warcraft_cli.main.provider_invoke",
                        _cooldown_invoke_with([], {("warcraftlogs", "report-fights"): _ok_answer({"fights": fights})}))

    result = runner.invoke(warcraft_app, _cooldown_packet_args())

    assert result.exit_code == exit_code, result.output
    error = json.loads(result.stderr)["error"]
    assert error["code"] == code
    if code == "fight_not_found":
        assert error["details"]["available_fight_ids"] == [7, 9]


def test_cooldown_packet_degrades_when_lorrgs_cached_the_report_but_not_the_fight(monkeypatch) -> None:
    other_fights = _ok_answer({"fights": [{"fight_id": 7, "players": []}, {"fight_id": 9, "players": []}]})
    monkeypatch.setattr("warcraft_cli.main.provider_invoke",
                        _cooldown_invoke_with([], {("lorrgs", "user-report-fights"): other_fights}))

    refused = runner.invoke(warcraft_app, _cooldown_packet_args())
    assert refused.exit_code == 2, refused.output
    assert json.loads(refused.stderr)["error"]["code"] == "spec_slug_missing"

    calls: list[tuple[str, list[str]]] = []
    monkeypatch.setattr("warcraft_cli.main.provider_invoke",
                        _cooldown_invoke_with(calls, {("lorrgs", "user-report-fights"): other_fights}))
    degraded = runner.invoke(warcraft_app, [*_cooldown_packet_args(), "--spec-slug", "warrior-protection"])
    assert degraded.exit_code == 0, degraded.output
    payload = json.loads(degraded.stdout)
    assert payload["data"]["lorrgs"]["reason"] == "lorrgs_fight_not_found"
    # Lorrgs never named the boss, but its boss list maps the fight's encounter 3183 to it.
    assert payload["query"]["boss_slug"] == "lura"
    assert payload["data"]["comparison"]["status"] == "ready"
    assert ("lorrgs", ["spec-ranking", "warrior-protection", "lura", "--difficulty", "mythic"]) in calls


def test_cooldown_packet_without_lorrgs_names_boss_slug_and_claims_no_phase_or_samples(monkeypatch) -> None:
    monkeypatch.setattr("warcraft_cli.main.provider_invoke", _uncached_lorrgs_invoke([]))

    result = runner.invoke(warcraft_app, [
        "cooldown-packet", "abcd1234", "--fight-id", "22", "--actor-id", "89",
        "--spec-slug", "warrior-protection", "--phase", "2",
    ])

    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)["data"]
    assert (data["comparison"]["status"], data["comparison"]["reason"]) == ("unavailable", "no_boss_slug")
    notes = " ".join(data["notes"])
    assert "--boss-slug" in notes
    for claim in ("Phase windows are derived", "Top-parse samples are comparison evidence", "Cached Lorrgs user-report data"):
        assert claim not in notes


# --- in-process provider capture -------------------------------------------------------------------


def test_captured_usage_error_is_invalid_argument_labelled_with_the_subcommand() -> None:
    from warcraft_cli.providers import provider_invoke

    result = provider_invoke("lorrgs", ["spec-spells", "--bogus"])

    assert result["exit_code"] == 2
    assert result["payload"]["error"]["code"] == "invalid_argument"
    assert result["payload"]["command"] == "spec-spells"


def test_captured_system_exit_keeps_its_code() -> None:
    from warcraft_cli.providers import _capture_command

    app = typer.Typer()

    @app.command()
    def bail() -> None:
        raise SystemExit(3)

    @app.command()
    def other() -> None:
        """A second command, so `bail` is a subcommand as in every provider app."""

    exit_code, payload = _capture_command(app, ["bail"], prog_name="fake")

    assert (exit_code, payload) == (3, None)


def test_simc_call_failure_is_an_envelope_echoing_the_inputs_it_was_handed(tmp_path) -> None:
    """An in-process simc failure reads like the CLI's: a simc envelope, its exit code, and the inputs as query."""
    from warcraft_cli.providers import simc_call

    packet = {
        "kind": "talent_transport_packet",
        "transport_status": "exact",
        "transport_forms": {"wowhead_talent_calc_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123"},
        "build_identity": {"class_spec_identity": {"identity": {"actor_class": "druid", "spec": "balance"}}},
        "raw_evidence": {"reference_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123"},
        "validation": {},
        "scope": {},
    }
    missing_apl = str(tmp_path / "missing.simc")

    result = simc_call("describe-build", PacketInput(packet), describe=DescribeOptions(apl_path=missing_apl))

    assert result["exit_code"] == 4
    payload = result["payload"]
    assert envelope_violations(payload) == []
    assert (payload["provider"], payload["command"], payload["error"]["code"]) == ("simc", "describe-build", "not_found")
    assert payload["query"] == {"build_packet": None, **dataclasses.asdict(DescribeOptions(apl_path=missing_apl))}


def _parse_with_the_real_provider_cli(provider: str, args: list[str]) -> None:
    """Parse ``args`` exactly as ``provider``'s own CLI would, raising on any usage error."""
    group = typer.main.get_command(get_provider(provider).app)
    assert isinstance(group, typer.core.TyperGroup), provider
    command = group.commands[args[0]]
    command.make_context(args[0], list(args[1:]), parent=group.make_context(provider, []))


def test_composite_argv_parses_with_the_real_provider_clis(monkeypatch, tmp_path) -> None:
    """A renamed provider flag must break the composite's tests, not only the provider's own."""
    calls: list[tuple[str, list[str]]] = []
    monkeypatch.setattr("warcraft_cli.main.provider_invoke", _cooldown_packet_invoke(calls))
    assert runner.invoke(warcraft_app, [*_cooldown_packet_args(), "--metric", "hps"]).exit_code == 0

    def export(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, Any]:
        calls.append((provider, args))
        write_article_bundle(
            _comparison_payload(provider=provider, slug=args[1], page_url=f"https://example.test/{provider}",
                                page_title="g", analysis_tags=["builds_talents"], build_code="ABC123"),
            provider=provider, export_dir=Path(args[3]),
        )
        return {"provider": provider, "exit_code": 0, "payload": _envelope({})}

    _guide_seam(monkeypatch, resolve={"resolved": True, "confidence": "high", "match": _guide_row("mw", 90)}, search=[])
    monkeypatch.setattr("warcraft_cli.main.provider_invoke", export)
    # simc runs in-process from typed arguments, so it has no argv to parse.
    monkeypatch.setattr("warcraft_cli.main.simc_call", lambda command, build, *, describe=None: _simc_result({"ok": True}))
    result = runner.invoke(warcraft_app, [
        "guide-compare-query", "mistweaver monk guide", "--provider", "method", "--provider", "icy-veins",
        "--out-root", str(tmp_path / "o"), "--simc-build-handoff",
    ])
    assert result.exit_code == 0, result.output

    assert {(provider, args[0]) for provider, args in calls} >= {
        ("lorrgs", "user-report-fights"), ("lorrgs", "spec-ranking"), ("warcraftlogs", "report-events"),
        ("method", "guide-export"),
    }
    for provider, args in calls:
        _parse_with_the_real_provider_cli(provider, args)


@pytest.mark.parametrize(
    ("argv", "first_call"),
    [
        (["guild", "us", "illidan", "Liquid Guild"], ("raiderio", "guild")),
        (["actor-profile", "abcd1234", "Buikia", "--fight-id", "3"], ("warcraftlogs", "report-player-details")),
        (["talent-packet", "https://www.wowhead.com/talent-calc/monk/mistweaver/ABC123"], ("wowhead", "talent-calc-packet")),
        (["talent-describe", "abcd1234", "--actor-id", "5", "--fight-id", "3"], ("warcraftlogs", "report-player-talents")),
    ],
    ids=["guild", "actor-profile", "talent-packet", "talent-describe"],
)
def test_single_provider_composites_hand_over_argv_the_provider_cli_parses(
    monkeypatch, argv: list[str], first_call: tuple[str, str]
) -> None:
    """Each composite's first provider call parses with that provider's real CLI (the call itself fails)."""
    calls: list[tuple[str, list[str]]] = []

    def failing(provider: str, args: list[str], *, expansion: str | None = None) -> dict[str, Any]:
        calls.append((provider, args))
        return _network_failure(provider, args[0])

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", failing)
    runner.invoke(warcraft_app, argv)

    assert (calls[0][0], calls[0][1][0]) == first_call
    for provider, args in calls:
        _parse_with_the_real_provider_cli(provider, args)


def test_guide_compare_rejects_the_same_bundle_twice_with_a_usage_envelope(tmp_path) -> None:
    export_dir = tmp_path / "bundle"
    write_article_bundle(
        _comparison_payload(provider="method", slug="mw", page_url="https://example.test/method", page_title="g",
                            analysis_tags=["builds_talents"], build_code="ABC123"),
        provider="method", export_dir=export_dir,
    )

    result = runner.invoke(warcraft_app, ["guide-compare", str(export_dir), str(tmp_path / "." / "bundle")])

    assert result.exit_code == 2, result.output
    error = json.loads(result.stderr)
    assert error["error"]["code"] == "invalid_argument"
    assert error["query"]["bundles"]



def test_warcraft_resolve_names_the_single_word_cap_when_it_is_why_the_best_candidate_was_not_accepted(monkeypatch) -> None:
    """Live `warcraft resolve illidan` (2026-10): the providers capped "Illidan Stormrage" at medium; the wrapper reads their rule."""
    _stub_resolve_seam(monkeypatch, {
        "warcraft-wiki": {"resolved": False, "confidence": "medium", "confidence_cap": {"rule": "single_word_query", "from": "high"},
                          "match": _match("warcraft-wiki", "Illidan Stormrage", "article", 60)},
    })

    data = json.loads(runner.invoke(warcraft_app, ["resolve", "illidan"]).stdout)["data"]

    assert (data["resolved"], data["next_command"]) == (False, None)
    assert data["best_unresolved_candidate"]["unresolved_reason"] == "single_word_query_not_named_exactly"


def test_wrapper_name_match_folds_apostrophes_as_the_providers_one_word_rule_does() -> None:
    """The providers' one-word rule let "karesh" name "K'aresh" while the wrapper's ranking did not."""
    from warcraft_cli.provider_contract import name_match_strength

    assert name_match_strength("karesh", "K'aresh") == "exact"
    assert name_match_strength("ashes of alar", "Ashes of Al'ar: Reborn") == "title_prefix"

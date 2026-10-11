"""Bounded discovery and output-free provider composition contracts."""

from __future__ import annotations

import json
import subprocess
import sys
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner
from warcraft_cli.discovery_scope import filter_candidates
from warcraft_cli.main import app
from warcraft_cli.providers import get_provider, provider_invoke, provider_resolve, provider_search
from warcraft_core.envelope import success_envelope

runner = CliRunner()


@pytest.mark.parametrize("command", ["search", "resolve"])
def test_provider_and_entity_scope_excludes_calls(monkeypatch, command):
    calls = []

    def lookup(provider, query, *, expansion=None, entity_types=(), **kwargs):
        calls.append((provider, entity_types))
        row = {"provider": provider, "kind": "item", "name": query, "id": 1, "ranking": {"score": 100}}
        data = {"results": [row], "candidates": [row], "match": row, "resolved": True, "confidence": "high"}
        return {
            "provider": provider,
            "exit_code": 0,
            "payload": success_envelope(provider=provider, command=command, kind=command, data=data),
        }

    monkeypatch.setattr(f"warcraft_cli.main.provider_{command}", lookup)
    result = runner.invoke(app, [command, "Thunderfury", "--provider", "wowhead", "--provider", "method", "--entity-type", "item"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)["data"]
    assert calls == [("wowhead", ("item",))]
    assert data["filters"] == {"providers": ["wowhead", "method"], "entity_types": ["item"]}
    assert {row["provider"]: row["reason"] for row in data["excluded_providers"]}["method"] == "provider_has_no_requested_entity_type"


@pytest.mark.parametrize("flag,value", [("--provider", "unknown"), ("--entity-type", "mount")])
def test_invalid_scope_fails_before_any_call(monkeypatch, flag, value):
    monkeypatch.setattr("warcraft_cli.main.provider_search", lambda *_args, **_kwargs: pytest.fail("Issued discovery call"))
    result = runner.invoke(app, ["search", "Thunderfury", flag, value])
    assert result.exit_code == 2
    assert json.loads(result.stderr)["error"]["code"] == "invalid_query"


def test_expansion_and_scope_intersect_before_calls(monkeypatch):
    monkeypatch.setattr("warcraft_cli.main.provider_search", lambda *_args, **_kwargs: pytest.fail("Issued excluded provider call"))
    result = runner.invoke(app, ["--expansion", "wotlk", "search", "guide", "--provider", "method"])
    assert result.exit_code == 2
    assert json.loads(result.stderr)["error"]["code"] == "no_searching_provider"


def test_bounded_filter_preserves_unknown_total_and_does_not_promote_resolve():
    row = {"kind": "guide", "name": "Guide"}
    other = {"kind": "item", "name": "Item"}
    payload = success_envelope(
        provider="fake",
        command="resolve",
        kind="resolve",
        data={
            "match": other,
            "resolved": True,
            "next_command": "fake item 1",
            "confidence": "high",
            "candidates": [other, row],
            "total_matches": 200,
        },
    )
    scoped = filter_candidates(payload, entity_types=("guide",), surface="resolve", limit=5)["data"]
    assert scoped["candidates"] == [row]
    assert scoped["resolved"] is False and scoped["match"] is None and scoped["next_command"] is None
    assert scoped["unfiltered_total_matches"] == 200 and "total_matches" not in scoped
    assert scoped["entity_scope"]["exhaustive"] is False


def test_native_raiderio_scope_is_forwarded(monkeypatch):
    calls = []

    def search(self, query, *, limit, **options):
        calls.append(options)
        return success_envelope(provider="raiderio", command="search", kind="search", data={"results": []})

    monkeypatch.setattr(type(get_provider("raiderio").surface), "search", search)
    result = provider_search("raiderio", "Example", entity_types=("character",))
    assert calls == [{"kind": "character"}]
    assert result["payload"]["data"]["entity_scope"]["mode"] == "native"


def test_article_resolve_filtered_without_claiming_a_match(monkeypatch):
    monkeypatch.setattr(
        "warcraft_cli.providers.method_provider.resolve",
        lambda *_args, **_kwargs: success_envelope(
            provider="method",
            command="resolve",
            kind="resolve",
            data={"match": {"kind": "item"}, "resolved": True, "candidates": []},
        ),
    )
    result = provider_resolve("method", "Example", entity_types=("guide",))
    assert result["payload"]["data"]["resolved"] is False
    assert result["payload"]["data"]["entity_scope"]["mode"] == "bounded_candidate_filter"


def test_profile_operation_runs_without_initializing_a_typer_command(monkeypatch):
    calls = []
    expected = success_envelope(provider="raiderio", command="character", kind="character_profile", data={"character": {"name": "Example"}})
    monkeypatch.setattr("warcraft_cli.providers.raiderio_operations.character_profile", lambda *args: calls.append(args) or expected)
    monkeypatch.setattr("typer.main.get_command", lambda *_args, **_kwargs: pytest.fail("Initialized provider CLI"))
    result = provider_invoke("raiderio", ["character", "us", "illidan", "Example"])
    assert result["exit_code"] == 0 and result["payload"]["data"] == expected["data"]
    assert calls == [("us", "illidan", "Example")]


@pytest.mark.parametrize(
    "args", [["spec-spells", "mage-frost", "--bogus"], ["spec-ranking", "mage-frost", "boss", "--metric"], ["spec-spells"]]
)
def test_malformed_operation_never_calls_the_service(monkeypatch, args):
    monkeypatch.setattr("warcraft_cli.providers.lorrgs_operations.spec_spells", lambda *_args: pytest.fail("Called malformed operation"))
    monkeypatch.setattr(
        "warcraft_cli.providers.lorrgs_operations.spec_ranking", lambda *_args, **_kwargs: pytest.fail("Called malformed operation")
    )
    result = provider_invoke("lorrgs", args)
    assert result["exit_code"] == 2 and result["payload"]["error"]["code"] == "invalid_argument"


def test_contract_modules_import_without_provider_registry():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import warcraft_cli.provider_calls; import warcraft_cli.provider_contract; assert 'warcraft_cli.providers' not in sys.modules",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_wrapper_ranking_evidence_agrees_with_provider_facts():
    from warcraft_cli import providers, ranking_facts

    assert ranking_facts.STALE_GUIDE_REASON == providers.STALE_GUIDE_REASON
    assert ranking_facts.WIKI_QUERY_COVERAGE_REASONS == providers.WIKI_QUERY_COVERAGE_REASONS


@pytest.mark.parametrize("endpoint", ["user", "auto"])
def test_private_composite_endpoint_reaches_fetch_and_only_wcl(monkeypatch, endpoint):
    calls = []

    def invoke(provider, args, *, expansion=None, **options):
        calls.append((provider, options))
        return {"provider": provider, "exit_code": 0, "payload": success_envelope(provider=provider, command=args[0], kind="test", data={})}

    def actor_profile(ctx, *, fetch, **kwargs):
        fetch("warcraftlogs", ["report-fights", "abcd1234"], expansion=None)
        fetch("raiderio", ["character", "us", "illidan", "Example"], expansion=None)
        return {"kind": "actor_profile", "query": kwargs}

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", invoke)
    monkeypatch.setattr("warcraft_cli.main.actor_profile_payload", actor_profile)
    result = runner.invoke(app, ["--warcraftlogs-endpoint", endpoint, "actor-profile", "abcd1234", "Example"])
    assert result.exit_code == 0, result.output
    assert calls == [("warcraftlogs", {"warcraftlogs_endpoint": endpoint}), ("raiderio", {})]


def test_private_talent_route_keeps_rejected_user_error_without_retry(monkeypatch):
    from warcraft_core.envelope import error_envelope

    calls = []

    def invoke(provider, args, *, expansion=None, **options):
        calls.append((provider, options))
        return {
            "provider": provider,
            "exit_code": 3,
            "payload": error_envelope(provider=provider, command=args[0], code="auth_failed", message="Synthetic user rejected"),
        }

    monkeypatch.setattr("warcraft_cli.main.provider_invoke", invoke)
    result = runner.invoke(app, ["--warcraftlogs-endpoint", "user", "talent-packet", "abcd1234", "--actor-id", "9", "--fight-id", "1"])
    assert result.exit_code == 3, result.output
    assert json.loads(result.stderr)["error"]["code"] == "auth_failed"
    assert calls == [("warcraftlogs", {"warcraftlogs_endpoint": "user"})]


@pytest.mark.parametrize("command", [["report-fights", "abcd1234"], ["graphql", "--query", "query { worldData { expansions { id } } }"]])
def test_typed_wcl_operation_uses_selected_client_and_graphql_endpoint(monkeypatch, command):
    endpoints = []

    class Client:
        def __init__(self, *, endpoint, **kwargs):
            self.endpoint = endpoint
            endpoints.append(endpoint)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

    def operation(client, **kwargs):
        assert client.endpoint == "user"
        if "query" in kwargs:
            assert kwargs["endpoint"] == "user"
        return success_envelope(provider="warcraftlogs", command=command[0], kind="test", data={})

    monkeypatch.setattr("warcraft_cli.providers.WarcraftLogsClient", Client)
    monkeypatch.setattr("warcraft_cli.providers.wcl_operations.report_fights", operation)
    monkeypatch.setattr("warcraft_cli.providers.wcl_operations.raw_graphql", operation)
    result = provider_invoke("warcraftlogs", command, warcraftlogs_endpoint="user")
    assert result["exit_code"] == 0, result
    assert endpoints == ["user"]


def test_wrapper_doctor_receives_composite_endpoint_policy(monkeypatch):
    calls = []
    monkeypatch.setattr("warcraft_cli.main.global_doctor_payload", lambda **kwargs: calls.append(kwargs) or {})
    result = runner.invoke(app, ["--warcraftlogs-endpoint", "user", "doctor"])
    assert result.exit_code == 0
    assert calls == [{"requested_expansion": None, "warcraftlogs_endpoint": "user"}]


def test_native_passthrough_keeps_its_explicit_wcl_endpoint(monkeypatch):
    calls = []
    monkeypatch.setattr("warcraft_cli.main.invoke_provider_command", lambda _app, *, args, prog_name: calls.append((args, prog_name)))
    result = runner.invoke(app, ["--warcraftlogs-endpoint", "user", "warcraftlogs", "--endpoint", "client", "report", "abcd1234"])
    assert result.exit_code == 0, result.output
    assert calls == [(["--endpoint", "client", "report", "abcd1234"], "warcraftlogs")]


def test_unknown_wcl_endpoint_fails_before_any_composite(monkeypatch):
    monkeypatch.setattr("warcraft_cli.main.provider_invoke", lambda *_args, **_kwargs: pytest.fail("Issued provider call"))
    args = ["--warcraftlogs-endpoint", "invalid", "talent-packet", "abcd1234", "--actor-id", "9"]
    result = runner.invoke(app, args)
    assert result.exit_code == 2
    # Global option errors reach the binary guard before the callback creates its config.
    process = subprocess.run(
        [sys.executable, "-c", "from warcraft_cli.main import run; run()", *args], capture_output=True, text=True, check=False
    )
    assert process.returncode == 2
    assert json.loads(process.stderr)["error"]["code"] == "invalid_argument"


def test_lorrgs_invalid_report_reference_preserves_native_usage_exit():
    from lorrgs_cli.main import app as lorrgs_app

    native = runner.invoke(lorrgs_app, ["user-report-fights", "invalid", "--fight-id", "1"])
    wrapper = provider_invoke("lorrgs", ["user-report-fights", "invalid", "--fight-id", "1"])
    assert native.exit_code == wrapper["exit_code"] == 2
    assert json.loads(native.stderr)["error"]["code"] == wrapper["payload"]["error"]["code"] == "invalid_report_ref"


@pytest.mark.parametrize(
    "code", ["missing_client_credentials", "missing_public_auth", "missing_user_auth", "site_profile_mismatch", "user_token_expired"]
)
@pytest.mark.parametrize("stage", ["client", "operation"])
def test_typed_wcl_auth_failures_keep_auth_exit_without_fallback(monkeypatch, code, stage):
    from warcraftlogs_cli.client import WarcraftLogsClientError

    endpoints = []

    class Client:
        def __init__(self, *, endpoint, **_kwargs):
            endpoints.append(endpoint)
            if stage == "client":
                raise WarcraftLogsClientError(code, "Synthetic authentication failure")

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

    def reject(*_args, **_kwargs):
        raise WarcraftLogsClientError(code, "Synthetic authentication failure")

    monkeypatch.setattr("warcraft_cli.providers.WarcraftLogsClient", Client)
    monkeypatch.setattr("warcraft_cli.providers.wcl_operations.report_fights", reject)
    result = provider_invoke("warcraftlogs", ["report-fights", "abcd1234"], warcraftlogs_endpoint="user")
    assert result["exit_code"] == 3 and result["payload"]["error"]["code"] == code
    assert endpoints == ["user"]


def test_wcl_phase_fallback_injects_fight_scope_through_typed_adapter(monkeypatch):
    from warcraft_cli.cooldown_packet_flow import CooldownState, _load_warcraftlogs_phases
    from warcraft_cli.main import _provider_payload_result

    calls = []

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

    def graphql(_client, *, variables, **_kwargs):
        calls.append(variables)
        return success_envelope(
            provider="warcraftlogs",
            command="graphql",
            kind="graphql",
            data={
                "reportData": {
                    "report": {
                        "phases": [{"encounterID": 7, "phases": [{"id": 1, "name": "First"}, {"id": 2, "name": "Second"}]}],
                        "fights": [
                            {
                                "id": 22,
                                "encounterID": 7,
                                "startTime": 1000,
                                "endTime": 9000,
                                "phaseTransitions": [{"id": 1, "startTime": 1000}, {"id": 2, "startTime": 5000}],
                            }
                        ],
                    }
                }
            },
        )

    monkeypatch.setattr("warcraft_cli.providers.WarcraftLogsClient", Client)
    monkeypatch.setattr("warcraft_cli.providers.wcl_operations.raw_graphql", graphql)
    state = CooldownState(report_code="abcd1234", fight_id=22)
    _load_warcraftlogs_phases(SimpleNamespace(allow_unlisted=False, expansion=None), state, _provider_payload_result)
    assert calls == [{"fightIDs": [22]}]
    assert [(row["name"], row["start_ms"], row["end_ms"]) for row in state.phase_windows] == [("First", 0, 4000), ("Second", 4000, 8000)]


@pytest.mark.parametrize(
    "declared,explicit,expected", [("fightIDs: [Int]", {}, [1, 2]), ("fightID: Int", {}, 1), ("fightIDs: [Int]", {"fightIDs": [9]}, [9])]
)
def test_graphql_adapter_fight_helpers_respect_declared_shape_and_explicit_variables(monkeypatch, declared, explicit, expected):
    from warcraft_cli.providers import _wcl_report_operation

    calls = []
    monkeypatch.setattr("warcraft_cli.providers.wcl_operations.raw_graphql", lambda _client, **kwargs: calls.append(kwargs) or {})
    _wcl_report_operation(
        None,
        "graphql",
        [
            "--query",
            f'query (${declared}) {{ reportData {{ report(code: "abcd1234") {{ code }} }} }}',
            "--variables-json",
            json.dumps(explicit),
            "--fight-id",
            "1",
            "--fight-id",
            "2",
        ],
        endpoint="user",
    )
    variable_name = declared.partition(":")[0]
    assert calls[0]["variables"] == {variable_name: expected}


@pytest.mark.parametrize(
    "reference_type,url",
    [("wow_talent_export", "ABC123"), ("wowhead_talent_calc_url", "https://www.wowhead.com/talent-calc/monk/mistweaver/ABC123")],
)
def test_singular_guide_source_url_survives_handoff_without_inventing_source_identity(tmp_path, reference_type, url):
    from warcraft_cli.guide_compare import guide_builds_simc_payload

    source_url = "https://www.wowhead.com/guide/classes/monk/mistweaver/overview-pve-healer"
    unknown = {"status": "unknown", "confidence": "none", "class_spec_identity": {"identity": {"actor_class": None, "spec": None}}}
    packet = guide_builds_simc_payload(
        source_path=tmp_path,
        source_kind="bundle",
        source_manifest={},
        bundle_inputs=[
            (
                tmp_path,
                {
                    "manifest": {"provider": "wowhead"},
                    "build_references": [
                        {
                            "url": url,
                            "reference_type": reference_type,
                            "build_code": "ABC123",
                            "source_url": source_url,
                            "build_identity": unknown,
                        }
                    ],
                },
            )
        ],
        decode=False,
        apl_path=None,
        limit=20,
        simc=lambda *_args, **_kwargs: {
            "exit_code": 0,
            "payload": success_envelope(provider="simc", command="identify-build", kind="build_identity", data={}),
        },
    )
    build = packet["builds"][0]
    assert packet["citations"]["source_urls"] == [source_url]
    assert build["sources"][0]["source_urls"] == build["evidence"]["source_urls"] == [source_url]
    assert build["sources"][0]["build_identity"] == build["reference"]["build_identity"] == unknown
    if reference_type == "wowhead_talent_calc_url":
        assert build["talent_transport_packet"]["raw_evidence"]["source_urls"] == [source_url]

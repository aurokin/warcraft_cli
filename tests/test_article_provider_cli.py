from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import typer
from cli_testkit import apply_provider_stubs
from icy_veins_cli.main import app as icy_veins_app
from icy_veins_cli.page_parser import parse_guide_page as parse_icy_veins_page
from lorrgs_cli.main import app as lorrgs_app
from method_cli.main import app as method_app
from method_cli.page_parser import parse_guide_page as parse_method_page
from raidbots_cli.main import app as raidbots_app
from typer.testing import CliRunner
from warcraft_content.article_provider_cli import (
    build_article_resolve_response,
    build_article_search_response,
    unsupported_guide_surface_message,
)
from warcraft_core.envelope import ENVELOPE_KEYS


def test_build_article_search_response_includes_scope_hint_when_present() -> None:
    payload = build_article_search_response(
        query="patch notes",
        search_query="patch notes",
        results=[],
        total_count=0,
        scope_hint={"code": "patch_notes", "message": "out of scope"},
    )

    assert payload["count"] == 0
    assert payload["results"] == []
    assert payload["scope_hint"]["code"] == "patch_notes"


def test_build_article_resolve_response_includes_scope_hint_when_present() -> None:
    payload = build_article_resolve_response(
        provider_command="icy-veins",
        query="latest class changes",
        search_query="latest class changes",
        results=[],
        total_count=0,
        resolved=False,
        scope_hint={"code": "class_changes", "message": "out of scope"},
    )

    assert payload["resolved"] is False
    assert payload["count"] == 0
    assert payload["scope_hint"]["code"] == "class_changes"
    assert payload["fallback_search_command"] == "icy-veins search 'latest class changes'"


def test_unsupported_guide_surface_message_is_provider_specific() -> None:
    message = unsupported_guide_surface_message(
        provider_name="Method",
        slug="tier-list",
        content_family="unsupported_index",
    )

    assert message == "Unsupported Method guide surface for slug='tier-list' family='unsupported_index'."


_FIXTURES = Path(__file__).parent / "fixtures"
_APPS: dict[str, typer.Typer] = {
    "icy-veins": icy_veins_app,
    "method": method_app,
    "lorrgs": lorrgs_app,
    "raidbots": raidbots_app,
}
_BUNDLE = "{bundle}"
_REPORT = "https://www.warcraftlogs.com/reports/abcd1234EFGH5678#fight=3"
_SIMC_INPUT = 'mage="Main"\nspec=frost\ntalents=CYG\n'
_QUICK_SIM_REPORT = {"sim": {"players": [{"name": "Main", "specialization": "Frost Mage"}]}, "simbot": {"simType": "quick"}}
# Every command of the four apps, with an argv that succeeds offline once _stub_network is applied.
# `test_every_command_has_an_offline_argv` holds this table to the Typer apps, so a new command must join it.
_OFFLINE_ARGV: dict[str, dict[str, list[str]]] = {
    **{
        binary: {
            "doctor": ["doctor"],
            "search": ["search", "mistweaver monk"],
            "resolve": ["resolve", "mistweaver monk"],
            "guide": ["guide", guide_ref],
            "guide-full": ["guide-full", guide_ref],
            "guide-export": ["guide-export", guide_ref, "--out", _BUNDLE],
            "guide-query": ["guide-query", _BUNDLE, "vivify"],
        }
        for binary, guide_ref in (("icy-veins", "mistweaver-monk-pve-healing-guide"), ("method", "mistweaver-monk"))
    },
    "lorrgs": {
        "doctor": ["doctor"],
        "roles": ["roles"],
        "classes": ["classes"],
        "specs": ["specs"],
        "search": ["search", "frost mage"],
        "resolve": ["resolve", "frost mage"],
        "spec": ["spec", "mage-frost"],
        "spec-spells": ["spec-spells", "mage-frost"],
        "zones": ["zones"],
        "season": ["season", "current"],
        "current-season": ["current-season"],
        "zone": ["zone", "53"],
        "zone-bosses": ["zone-bosses", "53"],
        "bosses": ["bosses"],
        "boss": ["boss", "lura"],
        "boss-spells": ["boss-spells", "lura"],
        "spell": ["spell", "116670"],
        "trinkets": ["trinkets"],
        "spec-ranking": ["spec-ranking", "mage-frost", "lura"],
        "spec-ranking-info": ["spec-ranking-info", "mage-frost", "lura"],
        "comp-ranking": ["comp-ranking", "lura"],
        "user-report": ["user-report", _REPORT],
        "report-overview": ["report-overview", _REPORT],
        "user-report-fights": ["user-report-fights", _REPORT],
    },
    "raidbots": {
        "doctor": ["doctor"],
        "inspect-report": ["inspect-report", "abc123XYZ"],
        "input": ["input", "abc123XYZ"],
        "explain-input": ["explain-input", "--text", _SIMC_INPUT],
    },
}


def _guide_page_stub(binary: str) -> Any:
    if binary == "icy-veins":
        html = (_FIXTURES / "icy_veins" / "spec_guide.html").read_text(encoding="utf-8")
        return lambda self, guide_ref: parse_icy_veins_page(html, source_url=f"https://www.icy-veins.com/wow/{guide_ref}")
    html = (_FIXTURES / "method" / "captured_talents_page.html").read_text(encoding="utf-8")
    return lambda self, guide_ref: parse_method_page(html, source_url=f"https://www.method.gg/guides/{guide_ref}")


def _lorrgs_json(client: object, url: str, **kwargs: object) -> httpx.Response:
    return httpx.Response(200, json={}, request=httpx.Request("GET", url))


def _stub_network(binary: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Answer every upstream call from captured pages or empty payloads so each command succeeds."""
    apply_provider_stubs(binary, monkeypatch)
    if binary in ("icy-veins", "method"):
        client = "icy_veins_cli.client.IcyVeinsClient" if binary == "icy-veins" else "method_cli.client.MethodClient"
        monkeypatch.setattr(f"{client}.fetch_guide_page", _guide_page_stub(binary))
    if binary == "lorrgs":
        monkeypatch.setattr("lorrgs_cli.client.request_with_retries", _lorrgs_json)
    if binary == "raidbots":
        monkeypatch.setenv("RAIDBOTS_CACHE_BACKEND", "none")
        monkeypatch.setattr("raidbots_cli.client.RaidbotsClient.report_data", lambda self, report_id: _QUICK_SIM_REPORT)
        monkeypatch.setattr("raidbots_cli.client.RaidbotsClient.report_input", lambda self, report_id: _SIMC_INPUT)


def test_every_command_has_an_offline_argv() -> None:
    for binary, app in _APPS.items():
        assert set(_OFFLINE_ARGV[binary]) == {command.name for command in app.registered_commands}, binary


@pytest.mark.parametrize(
    ("binary", "command"),
    [(binary, command) for binary, commands in _OFFLINE_ARGV.items() for command in commands],
)
def test_every_command_emits_only_the_envelope_keys(
    binary: str, command: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Payload keys live under ``data`` only; one command re-adding a top-level copy turns this red."""
    _stub_network(binary, monkeypatch)
    bundle = str(tmp_path / "bundle")
    if command == "guide-query":
        export_argv = [bundle if arg == _BUNDLE else arg for arg in _OFFLINE_ARGV[binary]["guide-export"]]
        assert CliRunner().invoke(_APPS[binary], export_argv).exit_code == 0
    argv = [bundle if arg == _BUNDLE else arg for arg in _OFFLINE_ARGV[binary][command]]

    result = CliRunner().invoke(_APPS[binary], argv)

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert (payload["ok"], payload["command"]) == (True, command)
    assert set(payload) == ENVELOPE_KEYS - {"error"}


@pytest.mark.parametrize(
    ("app", "failing_args"),
    [
        (icy_veins_app, ["guide-query", "/nonexistent/bundle", "talents"]),
        (method_app, ["guide-query", "/nonexistent/bundle", "talents"]),
        (lorrgs_app, ["report-overview", "not a report"]),
        (raidbots_app, ["explain-input", "--text", " "]),
    ],
)
def test_failure_envelopes_carry_only_the_envelope_keys(app: typer.Typer, failing_args: list[str]) -> None:
    failure = CliRunner().invoke(app, failing_args)

    assert failure.exit_code != 0
    assert set(json.loads(failure.stderr)) == ENVELOPE_KEYS

"""Wowhead tool-state commands: talent calculator, profession tree, dressing room, profiler."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
from wowhead_cli.main import app
from wowhead_cli.wowhead_client import WowheadClient

from tests.wowhead_testkit import (
    SAMPLE_DRESSING_ROOM_HTML,
    SAMPLE_PROFESSION_TREE_HTML,
    SAMPLE_PROFILER_HTML,
    SAMPLE_TALENT_CALC_HTML,
    captured_page,
    runner,
)

TALENT_CALC_SHAPE_ERROR = (
    "Talent calculator URL must use /talent-calc/<class>/<spec>[/<build-code>] or /talent-calc/<class>/<build-code> with a WoW class."
)


def test_talent_calc_command_decodes_url_and_embedded_builds(monkeypatch) -> None:
    def fake_page_html(self, page_url: str):
        assert page_url.endswith("/talent-calc/druid/balance/ABC123")
        return SAMPLE_TALENT_CALC_HTML

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.page_html", fake_page_html)
    result = runner.invoke(app, ["talent-calc", "druid/balance/ABC123", "--listed-build-limit", "5"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["tool"]["class_slug"] == "druid"
    assert payload["data"]["tool"]["spec_slug"] == "balance"
    assert payload["data"]["tool"]["build_code"] == "ABC123"
    assert payload["data"]["tool"]["state_url"].endswith("/talent-calc/druid/balance/ABC123")
    assert payload["data"]["build_identity"]["status"] == "inferred"
    assert payload["data"]["build_identity"]["class_spec_identity"]["identity"] == {"actor_class": "druid", "spec": "balance"}
    assert payload["data"]["listed_builds"]["count"] == 2
    assert payload["data"]["listed_builds"]["items"][0]["name"] == "Leveling"


def test_talent_calc_command_supports_expansion_prefixed_relative_ref(monkeypatch) -> None:
    def fake_page_html(self, page_url: str):
        assert page_url.endswith("/cata/talent-calc/hunter/beast-mastery/XYZ987")
        return SAMPLE_TALENT_CALC_HTML

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.page_html", fake_page_html)
    result = runner.invoke(app, ["talent-calc", "cata/talent-calc/hunter/beast-mastery/XYZ987"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["expansion"] == "cata"
    assert payload["data"]["tool"]["state_url"] == "https://www.wowhead.com/cata/talent-calc/hunter/beast-mastery/XYZ987"
    assert payload["data"]["tool"]["class_slug"] == "hunter"
    assert payload["data"]["tool"]["spec_slug"] == "beast-mastery"
    assert payload["data"]["tool"]["build_code"] == "XYZ987"


def test_talent_calc_command_supports_expansion_prefixed_class_spec_ref(monkeypatch) -> None:
    def fake_page_html(self, page_url: str):
        assert page_url.endswith("/classic/talent-calc/druid/balance/ABC123")
        return SAMPLE_TALENT_CALC_HTML

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.page_html", fake_page_html)
    result = runner.invoke(app, ["talent-calc", "classic/druid/balance/ABC123"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["expansion"] == "classic"
    assert payload["data"]["tool"]["state_url"] == "https://www.wowhead.com/classic/talent-calc/druid/balance/ABC123"
    assert payload["data"]["tool"]["class_slug"] == "druid"
    assert payload["data"]["tool"]["spec_slug"] == "balance"
    assert payload["data"]["tool"]["build_code"] == "ABC123"


def test_talent_calc_command_supports_scheme_less_wowhead_ref(monkeypatch) -> None:
    def fake_page_html(self, page_url: str):
        assert page_url == "https://wowhead.com/talent-calc/druid/balance/ABC123"
        return SAMPLE_TALENT_CALC_HTML

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.page_html", fake_page_html)
    result = runner.invoke(app, ["talent-calc", "wowhead.com/talent-calc/druid/balance/ABC123"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["tool"]["state_url"] == "https://wowhead.com/talent-calc/druid/balance/ABC123"


def test_talent_calc_command_rejects_empty_segment_ref() -> None:
    result = runner.invoke(app, ["talent-calc", "druid//balance/ABC123"])
    assert result.exit_code == 2
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_tool_ref"
    assert payload["error"]["message"] == "talent-calc reference must not include empty path segments."


def test_talent_calc_command_rejects_trailing_extra_segment_ref() -> None:
    result = runner.invoke(app, ["talent-calc", "https://www.wowhead.com/talent-calc/druid/balance/ABC123/extra"])
    assert result.exit_code == 2
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_tool_ref"
    assert payload["error"]["message"] == TALENT_CALC_SHAPE_ERROR


def test_talent_calc_command_rejects_malformed_non_url_ref() -> None:
    result = runner.invoke(app, ["talent-calc", "foo/talent-calc/druid/balance/ABC123"])
    assert result.exit_code == 2
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_tool_ref"
    assert payload["error"]["message"] == TALENT_CALC_SHAPE_ERROR


def test_talent_calc_command_rejects_nested_talent_calc_non_url_ref() -> None:
    result = runner.invoke(app, ["talent-calc", "talent-calc/foo/talent-calc/druid/balance/ABC123"])
    assert result.exit_code == 2
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_tool_ref"
    assert payload["error"]["message"] == TALENT_CALC_SHAPE_ERROR


def test_talent_calc_command_rejects_buried_real_wowhead_path() -> None:
    result = runner.invoke(app, ["talent-calc", "https://www.wowhead.com/items/talent-calc/druid/balance/ABC123"])
    assert result.exit_code == 2
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_tool_ref"
    assert payload["error"]["message"] == "Talent calculator URL must point to /talent-calc."


def test_talent_calc_packet_rejects_a_build_code_with_characters_no_build_code_uses() -> None:
    result = runner.invoke(app, ["talent-calc-packet", "https://www.wowhead.com/talent-calc/mage/frost/garbage!!!"])
    assert result.exit_code == 2
    assert json.loads(result.stderr)["error"] == {
        "code": "invalid_tool_ref",
        "message": "Talent calculator build code 'garbage!!!' holds characters no build code uses.",
    }


def test_talent_calc_reads_a_classic_calculator_url_as_class_and_build_code(monkeypatch) -> None:
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.page_html", lambda self, url: SAMPLE_TALENT_CALC_HTML)
    url = "https://www.wowhead.com/classic/talent-calc/warrior/30305001302-05050005525010051"

    result = runner.invoke(app, ["talent-calc", url])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)["data"]
    assert (data["expansion"], data["tool"]["class_slug"], data["tool"]["spec_slug"]) == ("classic", "warrior", None)
    assert data["tool"]["build_code"] == "30305001302-05050005525010051"
    assert data["build_identity"]["confidence"] == "none"

    packet = runner.invoke(app, ["talent-calc-packet", url])
    assert packet.exit_code == 2
    assert json.loads(packet.stderr)["error"]["code"] == "invalid_tool_ref"


def test_talent_calc_rejects_a_url_that_names_no_wow_class() -> None:
    for command in ("talent-calc", "talent-calc-packet"):
        result = runner.invoke(app, [command, "https://www.wowhead.com/talent-calc/foo/bar/ABC123"])
        assert result.exit_code == 2
        assert json.loads(result.stderr)["error"] == {"code": "invalid_tool_ref", "message": TALENT_CALC_SHAPE_ERROR}


def test_talent_calc_lists_only_the_requested_specs_builds(monkeypatch) -> None:
    """Wowhead embeds every spec's listed builds on each talent-calc page; only the ref's spec answers."""
    two_spec_html = SAMPLE_TALENT_CALC_HTML.replace(
        '"118": {"id": 118, "isListed": true, "name": "Mythic+", "spec": 102, "hash": "BBB222"}',
        '"118": {"id": 118, "isListed": true, "name": "Mythic+", "spec": 102, "hash": "BBB222"},\n'
        '        "119": {"id": 119, "isListed": true, "name": "(12.0.5) Leveling - Shado-Pan", "spec": 269, "hash": "C0QA"}',
    )
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.page_html", lambda self, url: two_spec_html)

    result = runner.invoke(app, ["talent-calc", "druid/balance"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)["data"]
    assert data["tool"]["spec_id"] == 102
    assert data["listed_builds"]["count"] == 2
    assert {row["spec_id"] for row in data["listed_builds"]["items"]} == {102}


def test_talent_calc_rejects_a_spec_that_is_not_the_classes() -> None:
    for command in ("talent-calc", "talent-calc-packet"):
        result = runner.invoke(app, [command, "paladin/frost/CYGAAAAAAAA"])
        assert result.exit_code == 2
        assert json.loads(result.stderr)["error"] == {
            "code": "invalid_tool_ref",
            "message": "Talent calculator spec 'frost' is not a paladin spec.",
        }


def test_talent_calc_accepts_a_classic_calculators_own_spec_name(monkeypatch) -> None:
    """MoP Classic calls rogue's second spec ``combat``; the retail spec table does not apply there."""
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.page_html", lambda self, url: SAMPLE_TALENT_CALC_HTML)
    result = runner.invoke(app, ["talent-calc", "https://www.wowhead.com/mop-classic/talent-calc/rogue/combat"])
    assert result.exit_code == 0, result.output
    tool = json.loads(result.stdout)["data"]["tool"]
    assert (tool["expansion"], tool["spec_slug"], tool["spec_id"]) == ("mop-classic", "combat", None)


def test_talent_calc_rejects_a_build_code_whose_loadout_header_is_another_spec(monkeypatch) -> None:
    """C0QA... is a Windwalker loadout (spec 269); it used to come back as an exact druid/balance packet."""
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.page_html", lambda self, url: SAMPLE_TALENT_CALC_HTML)
    for command in ("talent-calc", "talent-calc-packet"):
        result = runner.invoke(app, [command, "druid/balance/C0QAAAAAAAAAAAAAAAAAAAA"])
        assert result.exit_code == 2
        assert json.loads(result.stderr)["error"] == {
            "code": "invalid_tool_ref",
            "message": "Build code is a monk/windwalker loadout, not druid/balance.",
        }

    balance = runner.invoke(app, ["talent-calc-packet", "druid/balance/CYGAAAAAAAAAAAAAAAAAAAA"])
    assert balance.exit_code == 0, balance.output
    assert json.loads(balance.stdout)["data"]["talent_transport_packet"]["transport_status"] == "exact"


def test_talent_calc_packet_command_emits_exact_transport_packet(monkeypatch) -> None:
    def fake_page_html(self, page_url: str):
        assert page_url.endswith("/talent-calc/druid/balance/ABC123")
        return SAMPLE_TALENT_CALC_HTML

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.page_html", fake_page_html)
    result = runner.invoke(app, ["talent-calc-packet", "druid/balance/ABC123", "--listed-build-limit", "5"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["provider"] == "wowhead"
    assert payload["kind"] == "talent_calc_packet"
    assert payload["data"]["tool"]["state_url"].endswith("/talent-calc/druid/balance/ABC123")
    assert payload["data"]["talent_transport_packet"]["transport_status"] == "exact"
    assert (
        payload["data"]["talent_transport_packet"]["transport_forms"]["wowhead_talent_calc_url"]
        == "https://www.wowhead.com/talent-calc/druid/balance/ABC123"
    )
    assert payload["data"]["talent_transport_packet"]["build_identity"]["class_spec_identity"]["identity"] == {
        "actor_class": "druid",
        "spec": "balance",
    }
    assert payload["data"]["talent_transport_packet"]["scope"] == {"type": "wowhead_talent_calc", "expansion": "retail"}
    assert payload["data"]["listed_builds"]["count"] == 2


def test_talent_calc_packet_command_supports_expansion_prefixed_relative_ref(monkeypatch) -> None:
    def fake_page_html(self, page_url: str):
        assert page_url.endswith("/cata/talent-calc/hunter/beast-mastery/XYZ987")
        return SAMPLE_TALENT_CALC_HTML

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.page_html", fake_page_html)
    result = runner.invoke(app, ["talent-calc-packet", "cata/talent-calc/hunter/beast-mastery/XYZ987"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["tool"]["state_url"] == "https://www.wowhead.com/cata/talent-calc/hunter/beast-mastery/XYZ987"
    assert payload["data"]["expansion"] == "cata"
    assert (
        payload["data"]["talent_transport_packet"]["transport_forms"]["wowhead_talent_calc_url"]
        == "https://www.wowhead.com/cata/talent-calc/hunter/beast-mastery/XYZ987"
    )
    assert payload["data"]["talent_transport_packet"]["scope"]["expansion"] == "cata"


def test_talent_calc_packet_command_supports_expansion_prefixed_class_spec_ref(monkeypatch) -> None:
    def fake_page_html(self, page_url: str):
        assert page_url.endswith("/classic/talent-calc/druid/balance/ABC123")
        return SAMPLE_TALENT_CALC_HTML

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.page_html", fake_page_html)
    result = runner.invoke(app, ["talent-calc-packet", "classic/druid/balance/ABC123"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["tool"]["state_url"] == "https://www.wowhead.com/classic/talent-calc/druid/balance/ABC123"
    assert payload["data"]["expansion"] == "classic"
    assert (
        payload["data"]["talent_transport_packet"]["transport_forms"]["wowhead_talent_calc_url"]
        == "https://www.wowhead.com/classic/talent-calc/druid/balance/ABC123"
    )
    assert payload["data"]["talent_transport_packet"]["scope"]["expansion"] == "classic"


def test_talent_calc_packet_command_supports_scheme_less_wowhead_ref(monkeypatch) -> None:
    def fake_page_html(self, page_url: str):
        assert page_url == "https://wowhead.com/talent-calc/druid/balance/ABC123"
        return SAMPLE_TALENT_CALC_HTML

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.page_html", fake_page_html)
    result = runner.invoke(app, ["talent-calc-packet", "wowhead.com/talent-calc/druid/balance/ABC123"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["tool"]["state_url"] == "https://wowhead.com/talent-calc/druid/balance/ABC123"
    assert (
        payload["data"]["talent_transport_packet"]["transport_forms"]["wowhead_talent_calc_url"]
        == "https://wowhead.com/talent-calc/druid/balance/ABC123"
    )


def test_talent_calc_packet_command_can_write_exact_transport_packet(monkeypatch, tmp_path: Path) -> None:
    out_path = tmp_path / "balance-packet.json"

    def fake_page_html(self, page_url: str):
        assert page_url.endswith("/talent-calc/druid/balance/ABC123")
        return SAMPLE_TALENT_CALC_HTML

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.page_html", fake_page_html)
    result = runner.invoke(app, ["talent-calc-packet", "druid/balance/ABC123", "--out", str(out_path)])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["written_packet_path"] == str(out_path.resolve())

    written_packet = json.loads(out_path.read_text())
    assert written_packet == payload["data"]["talent_transport_packet"]
    assert written_packet["transport_status"] == "exact"


def test_talent_calc_packet_command_does_not_require_page_fetch_for_exact_ref(monkeypatch) -> None:
    def fake_page_html(self, page_url: str):
        raise httpx.ConnectError("network down", request=httpx.Request("GET", page_url))

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.page_html", fake_page_html)
    result = runner.invoke(app, ["talent-calc-packet", "druid/balance/ABC123"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["tool"]["state_url"].endswith("/talent-calc/druid/balance/ABC123")
    assert payload["data"]["tool"]["page_url"].endswith("/talent-calc/druid/balance/ABC123")
    # The input URL used to be reported as the fetched page's canonical URL, with nothing saying the fetch failed.
    assert payload["data"]["page"]["canonical_url"] is None
    assert payload["data"]["page"]["fetch_error"]["code"] == "network_error"
    assert "listed_builds" not in payload["data"]
    assert payload["data"]["talent_transport_packet"]["transport_status"] == "exact"
    assert payload["data"]["talent_transport_packet"]["raw_evidence"]["source_url"].endswith("/talent-calc/druid/balance/ABC123")


def test_talent_calc_packet_command_falls_back_on_http_status_error(monkeypatch, tmp_path: Path) -> None:
    out_path = tmp_path / "exact-packet.json"

    def fake_page_html(self, page_url: str):
        request = httpx.Request("GET", page_url)
        response = httpx.Response(503, request=request)
        raise httpx.HTTPStatusError("service unavailable", request=request, response=response)

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.page_html", fake_page_html)

    stdout_result = runner.invoke(app, ["talent-calc-packet", "druid/balance/ABC123"])
    assert stdout_result.exit_code == 0
    stdout_payload = json.loads(stdout_result.stdout)
    assert stdout_payload["data"]["talent_transport_packet"]["transport_status"] == "exact"
    assert "listed_builds" not in stdout_payload["data"]

    out_result = runner.invoke(app, ["talent-calc-packet", "druid/balance/ABC123", "--out", str(out_path)])
    assert out_result.exit_code == 0
    out_payload = json.loads(out_result.stdout)
    assert out_payload["data"]["written_packet_path"] == str(out_path.resolve())
    assert json.loads(out_path.read_text()) == out_payload["data"]["talent_transport_packet"] == stdout_payload["data"]["talent_transport_packet"]


def test_talent_calc_packet_command_normalizes_write_failure(monkeypatch, tmp_path: Path) -> None:
    out_dir = tmp_path / "out-dir"
    out_dir.mkdir()

    def fake_page_html(self, page_url: str):
        assert page_url.endswith("/talent-calc/druid/balance/ABC123")
        return SAMPLE_TALENT_CALC_HTML

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.page_html", fake_page_html)
    result = runner.invoke(app, ["talent-calc-packet", "druid/balance/ABC123", "--out", str(out_dir)])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "transport_packet_write_failed"


def test_talent_calc_packet_command_rejects_ref_without_build_code(monkeypatch) -> None:
    def fake_page_html(self, page_url: str):
        assert page_url.endswith("/talent-calc/druid/balance")
        return SAMPLE_TALENT_CALC_HTML.replace("/talent-calc/druid/balance/ABC123", "/talent-calc/druid/balance")

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.page_html", fake_page_html)
    result = runner.invoke(app, ["talent-calc-packet", "druid/balance"])
    assert result.exit_code == 2
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_tool_ref"
    assert payload["error"]["message"] == "talent-calc packet refs must include an explicit build code."


def test_talent_calc_packet_command_rejects_non_wowhead_absolute_url() -> None:
    result = runner.invoke(app, ["talent-calc-packet", "https://notwowhead.com/talent-calc/druid/balance/ABC123"])
    assert result.exit_code == 2
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_tool_ref"
    assert payload["error"]["message"] == "talent-calc URL must point to wowhead.com."


def test_talent_calc_packet_command_rejects_malformed_non_url_ref() -> None:
    result = runner.invoke(app, ["talent-calc-packet", "foo/talent-calc/druid/balance/ABC123"])
    assert result.exit_code == 2
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_tool_ref"
    assert payload["error"]["message"] == TALENT_CALC_SHAPE_ERROR


def test_talent_calc_packet_command_rejects_nested_talent_calc_non_url_ref() -> None:
    result = runner.invoke(app, ["talent-calc-packet", "talent-calc/foo/talent-calc/druid/balance/ABC123"])
    assert result.exit_code == 2
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_tool_ref"
    assert payload["error"]["message"] == TALENT_CALC_SHAPE_ERROR


def test_talent_calc_packet_command_rejects_empty_segment_ref() -> None:
    result = runner.invoke(app, ["talent-calc-packet", "druid//balance/ABC123"])
    assert result.exit_code == 2
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_tool_ref"
    assert payload["error"]["message"] == "talent-calc reference must not include empty path segments."


def test_talent_calc_packet_command_rejects_trailing_extra_segment_ref() -> None:
    result = runner.invoke(app, ["talent-calc-packet", "https://www.wowhead.com/talent-calc/druid/balance/ABC123/extra"])
    assert result.exit_code == 2
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_tool_ref"
    assert payload["error"]["message"] == TALENT_CALC_SHAPE_ERROR


def test_talent_calc_packet_command_rejects_buried_real_wowhead_path() -> None:
    result = runner.invoke(app, ["talent-calc-packet", "https://www.wowhead.com/items/talent-calc/druid/balance/ABC123"])
    assert result.exit_code == 2
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_tool_ref"
    assert payload["error"]["message"] == "Talent calculator URL must point to /talent-calc."


def test_talent_calc_packet_command_rejects_invalid_transport_packet(monkeypatch) -> None:
    def fake_page_html(self, page_url: str):
        assert page_url.endswith("/talent-calc/druid/balance/ABC123")
        return SAMPLE_TALENT_CALC_HTML

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.page_html", fake_page_html)
    monkeypatch.setattr(
        "wowhead_cli.talent_services.build_reference_transport_packet_payload",
        lambda **kwargs: {
            "kind": "talent_transport_packet",
            "transport_status": "validated",
            "build_identity": {},
            "transport_forms": {"wowhead_talent_calc_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123"},
            "raw_evidence": {"reference_url": "https://www.wowhead.com/talent-calc/druid/balance/ABC123"},
            "validation": {},
            "scope": {},
        },
    )

    result = runner.invoke(app, ["talent-calc-packet", "druid/balance/ABC123"])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_transport_packet"


def test_profession_tree_command_decodes_url(monkeypatch) -> None:
    def fake_page_html(self, page_url: str):
        assert page_url.endswith("/profession-tree-calc/alchemy/BCuA")
        return SAMPLE_PROFESSION_TREE_HTML

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.page_html", fake_page_html)
    result = runner.invoke(app, ["profession-tree", "alchemy/BCuA"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["tool"]["profession_slug"] == "alchemy"
    assert payload["data"]["tool"]["loadout_code"] == "BCuA"
    assert payload["data"]["tool"]["state_url"].endswith("/profession-tree-calc/alchemy/BCuA")


def test_dressing_room_command_normalizes_hash_ref(monkeypatch) -> None:
    def fake_page_html(self, page_url: str):
        assert page_url == "https://www.wowhead.com/dressing-room"
        return SAMPLE_DRESSING_ROOM_HTML

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.page_html", fake_page_html)
    result = runner.invoke(app, ["dressing-room", "#fz8zz0zb89c8mM8YB"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["tool"]["share_hash"] == "fz8zz0zb89c8mM8YB"
    assert payload["data"]["tool"]["has_share_hash"] is True
    assert payload["data"]["tool"]["state_url"].startswith("https://www.wowhead.com/dressing-room#")


def test_dressing_room_reads_a_classic_share_url_from_classic_and_never_cites_it_as_canonical(monkeypatch) -> None:
    fetched: list[str] = []

    def fake_page_html(self: WowheadClient, page_url: str) -> str:
        fetched.append(page_url)
        return "<html><head><title>Dressing Room</title></head></html>"

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.page_html", fake_page_html)
    result = runner.invoke(app, ["dressing-room", "https://www.wowhead.com/classic/dressing-room#abc123"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)["data"]
    assert fetched == ["https://www.wowhead.com/classic/dressing-room"]
    assert data["expansion"] == "classic"
    assert data["page"]["canonical_url"] is None
    assert data["page"]["note"] == "The fetched page carries no canonical link."


def test_tool_commands_report_the_expansion_their_url_names(monkeypatch) -> None:
    pages = {"profession-tree": SAMPLE_PROFESSION_TREE_HTML, "profiler": SAMPLE_PROFILER_HTML}
    urls = {
        "profession-tree": "https://www.wowhead.com/classic/profession-tree-calc/alchemy/BCuA",
        "profiler": "https://www.wowhead.com/classic/list?list=97060220/us/illidan/Roguecane",
    }
    for command, url in urls.items():
        monkeypatch.setattr("wowhead_cli.main.WowheadClient.page_html", lambda self, page_url, html=pages[command]: html)
        result = runner.invoke(app, [command, url])
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["data"]["expansion"] == "classic", command


def test_profiler_command_normalizes_list_ref(monkeypatch) -> None:
    def fake_page_html(self, page_url: str):
        assert page_url == "https://www.wowhead.com/list?list=97060220/us/illidan/Roguecane"
        return SAMPLE_PROFILER_HTML

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.page_html", fake_page_html)
    result = runner.invoke(app, ["profiler", "97060220/us/illidan/Roguecane"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["tool"]["list_id"] == "97060220"
    assert payload["data"]["tool"]["region_slug"] == "us"
    assert payload["data"]["tool"]["realm_slug"] == "illidan"
    assert payload["data"]["tool"]["character_name"] == "Roguecane"


def test_profiler_reads_wowheads_canonical_list_path(monkeypatch) -> None:
    """`profiler 1` reports https://www.wowhead.com/list=1/default-lists as canonical; that URL used to exit 2."""
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.page_html", lambda self, page_url: SAMPLE_PROFILER_HTML)

    named = runner.invoke(app, ["profiler", "https://www.wowhead.com/list=1/default-lists"])
    assert named.exit_code == 0, named.output
    assert json.loads(named.stdout)["data"]["tool"]["list_parts"] == ["1"]

    character = runner.invoke(app, ["profiler", "https://www.wowhead.com/list=5961/us/illidan/Roguecane"])
    assert character.exit_code == 0, character.output
    tool = json.loads(character.stdout)["data"]["tool"]
    assert (tool["list_id"], tool["region_slug"], tool["realm_slug"], tool["character_name"]) == ("5961", "us", "illidan", "Roguecane")


def test_profiler_fails_not_found_when_wowhead_serves_its_missing_list_page(monkeypatch) -> None:
    """Wowhead answers a list that does not exist with HTTP 200 and an "Error" page (captured)."""
    fetched: list[str] = []

    def fake_page_html(self, page_url: str) -> str:
        fetched.append(page_url)
        return captured_page("profiler_missing_list_page.html")

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.page_html", fake_page_html)
    result = runner.invoke(app, ["profiler", "97060220/us/illidan/Roguecane"])
    assert fetched == ["https://www.wowhead.com/list?list=97060220/us/illidan/Roguecane"]
    assert result.exit_code == 4
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "not_found"
    assert "doesn't exist or has been removed" in payload["error"]["message"]


def test_profiler_refuses_a_url_off_wowhead_without_fetching_it(monkeypatch) -> None:
    # The host only ends with "wowhead.com"; it is not wowhead.com or a subdomain of it.
    def fail_fetch(self, page_url: str) -> str:
        raise AssertionError(page_url)

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.page_html", fail_fetch)
    result = runner.invoke(app, ["profiler", "https://evilwowhead.com/list?list=1/us/a/b"])
    assert result.exit_code == 2
    assert json.loads(result.stderr)["error"]["code"] == "invalid_tool_ref"


def test_profiler_reports_no_canonical_url_when_the_fetched_page_names_none(monkeypatch) -> None:
    """The page's og:url says /list; the ref URL was never a canonical the page claimed."""
    html = SAMPLE_PROFILER_HTML.replace('<link rel="canonical" href="https://www.wowhead.com/list">', "")
    monkeypatch.setattr("wowhead_cli.main.WowheadClient.page_html", lambda self, page_url: html)
    result = runner.invoke(app, ["profiler", "97060220/us/illidan/Roguecane"])
    assert result.exit_code == 0
    page = json.loads(result.stdout)["data"]["page"]
    assert page["canonical_url"] is None
    assert page["note"] == "The fetched page carries no canonical link."


def test_pure_talent_packet_keeps_exact_transport_when_page_fetch_fails(monkeypatch, capsys) -> None:
    from wowhead_cli.provider import talent_calc_packet

    def unavailable(self, page_url: str):
        raise httpx.ConnectError("synthetic offline page")

    monkeypatch.setattr(WowheadClient, "page_html", unavailable)
    result = talent_calc_packet("druid/balance/ABC123")
    assert result["data"]["page"]["fetch_error"]["code"] == "network_error"
    assert result["data"]["talent_transport_packet"]["transport_forms"]["wowhead_talent_calc_url"] == (
        "https://www.wowhead.com/talent-calc/druid/balance/ABC123"
    )
    assert capsys.readouterr().out == ""


def test_pure_talent_packet_rejects_missing_build_before_opening_client(monkeypatch) -> None:
    import pytest
    from warcraft_core.provider import ProviderError
    from wowhead_cli.provider import talent_calc_packet

    monkeypatch.setattr("wowhead_cli.provider.open_client", lambda profile: pytest.fail("invalid ref opened client"))
    with pytest.raises(ProviderError) as exc:
        talent_calc_packet("druid/balance")
    assert exc.value.code == "invalid_tool_ref"
    assert exc.value.exit_code == 2


def test_pure_talent_packet_closes_page_client(monkeypatch) -> None:
    from wowhead_cli.expansion_profiles import resolve_expansion
    from wowhead_cli.provider import talent_calc_packet

    closed = []
    client = WowheadClient(expansion=resolve_expansion(None))
    monkeypatch.setattr(client, "page_html", lambda url: SAMPLE_TALENT_CALC_HTML)
    monkeypatch.setattr(client, "close", lambda: closed.append(True))
    monkeypatch.setattr("wowhead_cli.provider.open_client", lambda profile: client)
    assert talent_calc_packet("druid/balance/ABC123")["ok"] is True
    assert closed == [True]

"""Wowhead tool-state commands: talent calculator, profession tree, dressing room, profiler."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import typer
from wowhead_cli.main import app

from tests.wowhead_testkit import (
    SAMPLE_DRESSING_ROOM_HTML,
    SAMPLE_PROFESSION_TREE_HTML,
    SAMPLE_PROFILER_HTML,
    SAMPLE_TALENT_CALC_HTML,
    captured_page,
    runner,
)


def test_talent_calc_command_decodes_url_and_embedded_builds(monkeypatch) -> None:
    def fake_page_html(self, page_url: str):  # noqa: ANN001
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
    def fake_page_html(self, page_url: str):  # noqa: ANN001
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
    def fake_page_html(self, page_url: str):  # noqa: ANN001
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
    def fake_page_html(self, page_url: str):  # noqa: ANN001
        assert page_url == "https://wowhead.com/talent-calc/druid/balance/ABC123"
        return SAMPLE_TALENT_CALC_HTML

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.page_html", fake_page_html)
    result = runner.invoke(app, ["talent-calc", "wowhead.com/talent-calc/druid/balance/ABC123"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["tool"]["state_url"] == "https://wowhead.com/talent-calc/druid/balance/ABC123"



def test_talent_calc_command_rejects_empty_segment_ref() -> None:
    result = runner.invoke(app, ["talent-calc", "druid//balance/ABC123"])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_tool_ref"
    assert payload["error"]["message"] == "talent-calc reference must not include empty path segments."



def test_talent_calc_command_rejects_trailing_extra_segment_ref() -> None:
    result = runner.invoke(app, ["talent-calc", "https://www.wowhead.com/talent-calc/druid/balance/ABC123/extra"])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_tool_ref"
    assert payload["error"]["message"] == "Talent calculator URL must use /talent-calc/<class>/<spec>[/<build-code>]."



def test_talent_calc_command_rejects_malformed_non_url_ref() -> None:
    result = runner.invoke(app, ["talent-calc", "foo/talent-calc/druid/balance/ABC123"])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_tool_ref"
    assert payload["error"]["message"] == "talent-calc reference must be a Wowhead talent-calc path or class/spec ref."



def test_talent_calc_command_rejects_nested_talent_calc_non_url_ref() -> None:
    result = runner.invoke(app, ["talent-calc", "talent-calc/foo/talent-calc/druid/balance/ABC123"])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_tool_ref"
    assert payload["error"]["message"] == "talent-calc reference must be a Wowhead talent-calc path or class/spec ref."



def test_talent_calc_command_rejects_buried_real_wowhead_path() -> None:
    result = runner.invoke(app, ["talent-calc", "https://www.wowhead.com/items/talent-calc/druid/balance/ABC123"])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_tool_ref"
    assert payload["error"]["message"] == "Talent calculator URL must point to /talent-calc."



def test_talent_calc_packet_command_emits_exact_transport_packet(monkeypatch) -> None:
    def fake_page_html(self, page_url: str):  # noqa: ANN001
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
    def fake_page_html(self, page_url: str):  # noqa: ANN001
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
    def fake_page_html(self, page_url: str):  # noqa: ANN001
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
    def fake_page_html(self, page_url: str):  # noqa: ANN001
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

    def fake_page_html(self, page_url: str):  # noqa: ANN001
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
    def fake_page_html(self, page_url: str):  # noqa: ANN001
        raise httpx.ConnectError("network down", request=httpx.Request("GET", page_url))

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.page_html", fake_page_html)
    result = runner.invoke(app, ["talent-calc-packet", "druid/balance/ABC123"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["tool"]["state_url"].endswith("/talent-calc/druid/balance/ABC123")
    assert payload["data"]["tool"]["page_url"].endswith("/talent-calc/druid/balance/ABC123")
    assert payload["data"]["page"]["canonical_url"].endswith("/talent-calc/druid/balance/ABC123")
    assert "listed_builds" not in payload["data"]
    assert payload["data"]["talent_transport_packet"]["transport_status"] == "exact"



def test_talent_calc_packet_command_does_not_require_client_init_for_exact_ref(monkeypatch, tmp_path: Path) -> None:
    out_path = tmp_path / "exact-packet.json"
    monkeypatch.setattr("wowhead_cli.main._client", lambda ctx: (_ for _ in ()).throw(typer.Exit(1)))
    stdout_result = runner.invoke(app, ["talent-calc-packet", "druid/balance/ABC123"])
    assert stdout_result.exit_code == 0
    stdout_payload = json.loads(stdout_result.stdout)
    assert stdout_payload["data"]["tool"]["state_url"].endswith("/talent-calc/druid/balance/ABC123")
    assert stdout_payload["data"]["talent_transport_packet"]["transport_status"] == "exact"
    assert "listed_builds" not in stdout_payload["data"]

    out_result = runner.invoke(app, ["talent-calc-packet", "druid/balance/ABC123", "--out", str(out_path)])
    assert out_result.exit_code == 0
    out_payload = json.loads(out_result.stdout)
    assert out_payload["data"]["written_packet_path"] == str(out_path.resolve())
    assert json.loads(out_path.read_text()) == out_payload["data"]["talent_transport_packet"] == stdout_payload["data"]["talent_transport_packet"]



def test_talent_calc_packet_command_falls_back_on_http_status_error(monkeypatch, tmp_path: Path) -> None:
    out_path = tmp_path / "exact-packet.json"

    def fake_page_html(self, page_url: str):  # noqa: ANN001
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

    def fake_page_html(self, page_url: str):  # noqa: ANN001
        assert page_url.endswith("/talent-calc/druid/balance/ABC123")
        return SAMPLE_TALENT_CALC_HTML

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.page_html", fake_page_html)
    result = runner.invoke(app, ["talent-calc-packet", "druid/balance/ABC123", "--out", str(out_dir)])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "transport_packet_write_failed"



def test_talent_calc_packet_command_rejects_ref_without_build_code(monkeypatch) -> None:
    def fake_page_html(self, page_url: str):  # noqa: ANN001
        assert page_url.endswith("/talent-calc/druid/balance")
        return SAMPLE_TALENT_CALC_HTML.replace("/talent-calc/druid/balance/ABC123", "/talent-calc/druid/balance")

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.page_html", fake_page_html)
    result = runner.invoke(app, ["talent-calc-packet", "druid/balance"])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_tool_ref"
    assert payload["error"]["message"] == "talent-calc packet refs must include an explicit build code."



def test_talent_calc_packet_command_rejects_non_wowhead_absolute_url() -> None:
    result = runner.invoke(app, ["talent-calc-packet", "https://notwowhead.com/talent-calc/druid/balance/ABC123"])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_tool_ref"
    assert payload["error"]["message"] == "talent-calc URL must point to wowhead.com."



def test_talent_calc_packet_command_rejects_malformed_non_url_ref() -> None:
    result = runner.invoke(app, ["talent-calc-packet", "foo/talent-calc/druid/balance/ABC123"])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_tool_ref"
    assert payload["error"]["message"] == "talent-calc reference must be a Wowhead talent-calc path or class/spec ref."



def test_talent_calc_packet_command_rejects_nested_talent_calc_non_url_ref() -> None:
    result = runner.invoke(app, ["talent-calc-packet", "talent-calc/foo/talent-calc/druid/balance/ABC123"])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_tool_ref"
    assert payload["error"]["message"] == "talent-calc reference must be a Wowhead talent-calc path or class/spec ref."



def test_talent_calc_packet_command_rejects_empty_segment_ref() -> None:
    result = runner.invoke(app, ["talent-calc-packet", "druid//balance/ABC123"])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_tool_ref"
    assert payload["error"]["message"] == "talent-calc reference must not include empty path segments."



def test_talent_calc_packet_command_rejects_trailing_extra_segment_ref() -> None:
    result = runner.invoke(app, ["talent-calc-packet", "https://www.wowhead.com/talent-calc/druid/balance/ABC123/extra"])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_tool_ref"
    assert payload["error"]["message"] == "Talent calculator URL must use /talent-calc/<class>/<spec>[/<build-code>]."



def test_talent_calc_packet_command_rejects_buried_real_wowhead_path() -> None:
    result = runner.invoke(app, ["talent-calc-packet", "https://www.wowhead.com/items/talent-calc/druid/balance/ABC123"])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_tool_ref"
    assert payload["error"]["message"] == "Talent calculator URL must point to /talent-calc."



def test_talent_calc_packet_command_rejects_invalid_transport_packet(monkeypatch) -> None:
    def fake_page_html(self, page_url: str):  # noqa: ANN001
        assert page_url.endswith("/talent-calc/druid/balance/ABC123")
        return SAMPLE_TALENT_CALC_HTML

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.page_html", fake_page_html)
    monkeypatch.setattr(
        "wowhead_cli.main.build_reference_transport_packet_payload",
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
    def fake_page_html(self, page_url: str):  # noqa: ANN001
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
    def fake_page_html(self, page_url: str):  # noqa: ANN001
        assert page_url == "https://www.wowhead.com/dressing-room"
        return SAMPLE_DRESSING_ROOM_HTML

    monkeypatch.setattr("wowhead_cli.main.WowheadClient.page_html", fake_page_html)
    result = runner.invoke(app, ["dressing-room", "#fz8zz0zb89c8mM8YB"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["tool"]["share_hash"] == "fz8zz0zb89c8mM8YB"
    assert payload["data"]["tool"]["has_share_hash"] is True
    assert payload["data"]["tool"]["state_url"].startswith("https://www.wowhead.com/dressing-room#")



def test_profiler_command_normalizes_list_ref(monkeypatch) -> None:
    def fake_page_html(self, page_url: str):  # noqa: ANN001
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
    assert result.exit_code == 1
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

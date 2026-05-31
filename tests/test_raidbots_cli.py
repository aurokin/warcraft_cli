from __future__ import annotations

import json
import shlex

import httpx
import pytest
from raidbots_cli.client import InvalidReportReference, resolve_report_id
from raidbots_cli.main import app
from raidbots_cli.report import parse_report
from raidbots_cli.simc_input import classify_simc_input, simc_handoff
from typer.testing import CliRunner

runner = CliRunner()

QUICK_SIM_REPORT = {
    "version": "1130-01",
    "sim": {
        "options": {
            "iterations": 10000,
            "target_error": 0.1,
            "fight_style": "Patchwerk",
            "desired_targets": 1,
            "max_time": 300,
            "threads": 8,
            "dbc": {"version_used": "live", "live": {"wow_version": "11.1.0"}},
        },
        "statistics": {"simulation_length": {"count": 9000}},
        "players": [
            {
                "name": "Frostmage",
                "specialization": "Frost Mage",
                "role": "spell",
                "class": "mage",
                "level": 80,
                "race": "troll",
                "talents": "CYGAAA",
                "collected_data": {
                    "dps": {"mean": 1234567.8, "count": 10000},
                    "dpse": {"mean": 1230000.0},
                    "dtps": {"mean": 0.0},
                    "hps": {"mean": 0.0},
                    "fight_length": {"mean": 300.0, "count": 10000},
                },
            }
        ],
    },
    "simbot": {"simType": "quick", "title": "Quick Sim"},
}

MULTI_PROFILE_REPORT = {
    "version": "1130-01",
    "sim": {
        "options": {
            "iterations": 10000,
            "target_error": 0.05,
            "fight_style": "Patchwerk",
            "dbc": {"version_used": "live", "live": {"wow_version": "11.1.0"}},
        },
        "statistics": {},
        "players": [
            {"name": "Baseline", "specialization": "Frost Mage", "role": "spell", "class": "mage", "talents": "CYG"}
        ],
        "profilesets": {
            "metric": ["dps"],
            "results": [
                {"name": "Trinket A", "mean": 2000000.0, "min": 1.0, "max": 3.0, "median": 2.0, "stddev": 1000.0},
                {"name": "Trinket B", "mean": 2500000.0},
                {"name": "Trinket C", "mean": 1500000.0},
            ],
        },
    },
    "simbot": {"simType": "droptimizer", "title": "Droptimizer"},
}

TOP_GEAR_INPUT = 'mage="Main"\nspec=frost\ntalents=CYG\nprofileset."Option 1"+="talents=AAA"\nprofileset."Option 2"+="talents=BBB"\n'


@pytest.fixture(autouse=True)
def _disable_raidbots_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAIDBOTS_CACHE_BACKEND", "none")


def test_resolve_report_id_handles_bare_id_and_urls() -> None:
    assert resolve_report_id("abc123XYZ") == "abc123XYZ"
    assert resolve_report_id("https://www.raidbots.com/simbot/report/abc123XYZ") == "abc123XYZ"
    assert resolve_report_id("https://www.raidbots.com/simbot/report/abc123XYZ/data.json") == "abc123XYZ"
    with pytest.raises(InvalidReportReference):
        resolve_report_id("")
    with pytest.raises(InvalidReportReference):
        resolve_report_id("not a report")


def test_parse_report_quick_sim_extracts_actor_and_metrics() -> None:
    parsed = parse_report(QUICK_SIM_REPORT, report_id="abc")
    assert parsed["kind"] == "quick_sim"
    assert parsed["actor"]["name"] == "Frostmage"
    assert parsed["actor"]["spec"] == "Frost Mage"
    assert parsed["actor"]["talents_present"] is True
    assert parsed["metrics"]["dps"] == 1234567.8
    assert parsed["game_version"] == "11.1.0"
    assert parsed["simbot"]["sim_type"] == "quick"
    assert parsed["run_settings"]["iterations_completed"] == 10000
    assert parsed["run_settings"]["stop_reason"] == "target_error_requested"
    assert "profilesets" not in parsed


def test_parse_report_multi_profile_ranks_results_and_omits_metrics() -> None:
    parsed = parse_report(MULTI_PROFILE_REPORT, report_id="def")
    assert parsed["kind"] == "multi_profile"
    assert parsed["baseline_actor"]["name"] == "Baseline"
    assert "metrics" not in parsed  # per-actor damage/buff data is stripped for multi-profile sims
    profilesets = parsed["profilesets"]
    assert profilesets["metric"] == "dps"
    assert profilesets["result_count"] == 3
    # Sorted descending by mean: B (2.5M), A (2.0M), C (1.5M).
    assert [row["name"] for row in profilesets["results"]] == ["Trinket B", "Trinket A", "Trinket C"]


def test_parse_report_empty_profilesets_stays_multi_profile() -> None:
    # A multi-profile run that produced no rows must NOT be misread as a quick sim off players[0].
    report = {
        "version": "1130-01",
        "sim": {
            "options": {},
            "statistics": {},
            "players": [{"name": "Baseline", "specialization": "Frost Mage"}],
            "profilesets": {"metric": ["dps"], "results": []},
        },
    }
    parsed = parse_report(report, report_id="empty")
    assert parsed["kind"] == "multi_profile"
    assert parsed["profilesets"]["result_count"] == 0
    assert "metrics" not in parsed


def test_parse_report_profilesets_name_row_mapping() -> None:
    report = {
        "version": "1130-01",
        "sim": {
            "options": {},
            "statistics": {},
            "players": [{"name": "Baseline"}],
            "profilesets": {"metric": ["dps"], "Trinket A": {"mean": 2.0}, "Trinket B": {"mean": 5.0}},
        },
    }
    parsed = parse_report(report, report_id="mapping")
    assert parsed["kind"] == "multi_profile"
    assert [row["name"] for row in parsed["profilesets"]["results"]] == ["Trinket B", "Trinket A"]


def test_parse_report_rejects_non_simc_payload() -> None:
    with pytest.raises(ValueError, match="SimC"):
        parse_report({"not": "a sim"}, report_id="x")


def test_classify_simc_input_detects_quick_sim() -> None:
    classification = classify_simc_input('mage="Main"\nspec=frost\ntalents=CYG\n')
    assert classification["sim_type_guess"] == "quick_sim"
    assert classification["actor_class"] == "mage"
    assert classification["spec"] == "frost"
    assert classification["talents_present"] is True
    assert classification["profileset_count"] == 0


def test_classify_simc_input_detects_profilesets() -> None:
    classification = classify_simc_input(TOP_GEAR_INPUT)
    assert classification["sim_type_guess"] == "top_gear_or_droptimizer"
    assert classification["profileset_count"] == 2


def test_classify_simc_input_falls_back_to_advanced() -> None:
    classification = classify_simc_input("desired_targets=3\nmax_time=120\n")
    assert classification["sim_type_guess"] == "advanced"
    assert classification["actor_class"] is None


def test_doctor_reports_partial_status_and_url_templates() -> None:
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["provider"] == "raidbots"
    assert payload["status"] == "partial"
    assert payload["capabilities"]["search"] == "not_supported"
    assert payload["capabilities"]["inspect_report"] == "ready"
    assert payload["url_templates"]["simc_input"].endswith("/simc")


def test_inspect_report_quick_sim_includes_scope_and_citations(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("raidbots_cli.main.RaidbotsClient.report_data", lambda self, report_id: QUICK_SIM_REPORT)
    result = runner.invoke(app, ["inspect-report", "https://www.raidbots.com/simbot/report/abc123"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["report"]["kind"] == "quick_sim"
    assert payload["scope"] == {"type": "raidbots_report", "kind": "quick_sim"}
    assert payload["citations"]["data_json_url"] == "https://www.raidbots.com/simbot/report/abc123/data.json"
    assert payload["freshness"]["cache_ttl_seconds"] == 86400
    assert payload["freshness"]["from_cache"] is False
    assert "retrieved_at" in payload["freshness"]
    assert payload["raw"] == QUICK_SIM_REPORT


def test_inspect_report_no_raw_omits_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("raidbots_cli.main.RaidbotsClient.report_data", lambda self, report_id: MULTI_PROFILE_REPORT)
    result = runner.invoke(app, ["inspect-report", "def456", "--no-raw"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert "raw" not in payload
    assert payload["report"]["kind"] == "multi_profile"


def test_inspect_report_maps_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(self, report_id):  # noqa: ANN001, ANN202
        request = httpx.Request("GET", "https://www.raidbots.com/simbot/report/missing/data.json")
        raise httpx.HTTPStatusError("not found", request=request, response=httpx.Response(404, request=request))

    monkeypatch.setattr("raidbots_cli.main.RaidbotsClient.report_data", _raise)
    result = runner.invoke(app, ["inspect-report", "missing"])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "not_found"


def test_inspect_report_maps_transport_error_to_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(self, report_id):  # noqa: ANN001, ANN202
        request = httpx.Request("GET", "https://www.raidbots.com/simbot/report/x/data.json")
        raise httpx.ConnectError("connection refused", request=request)

    monkeypatch.setattr("raidbots_cli.main.RaidbotsClient.report_data", _raise)
    result = runner.invoke(app, ["inspect-report", "x"])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "upstream_error"


def test_input_maps_transport_error_to_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(self, report_id):  # noqa: ANN001, ANN202
        request = httpx.Request("GET", "https://www.raidbots.com/simbot/report/x/simc")
        raise httpx.ReadTimeout("timed out", request=request)

    monkeypatch.setattr("raidbots_cli.main.RaidbotsClient.report_input", _raise)
    result = runner.invoke(app, ["input", "x"])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "upstream_error"


def test_inspect_report_rejects_unparseable_reference() -> None:
    result = runner.invoke(app, ["inspect-report", "not a report"])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_report"


def test_input_command_emits_handoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "raidbots_cli.main.RaidbotsClient.report_input",
        lambda self, report_id: 'mage="Main"\nspec=frost\ntalents=CYG\n',
    )
    result = runner.invoke(app, ["input", "abc123"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["report_id"] == "abc123"
    assert payload["handoff"]["classification"]["sim_type_guess"] == "quick_sim"
    assert payload["scope"]["sim_type_guess"] == "quick_sim"
    commands = [entry["command"] for entry in payload["handoff"]["suggested_simc_commands"]]
    assert "simc sim -" in commands
    # decode/describe must carry class+spec so the bare talent code resolves.
    decode = next(cmd for cmd in commands if cmd.startswith("simc decode-build"))
    assert "--actor-class mage" in decode
    assert "--spec frost" in decode
    assert "--talents CYG" in decode
    assert payload["citations"]["simc_input_url"] == "https://www.raidbots.com/simbot/report/abc123/simc"


def test_explain_input_via_text_option() -> None:
    result = runner.invoke(app, ["explain-input", "--text", TOP_GEAR_INPUT])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["scope"]["sim_type_guess"] == "top_gear_or_droptimizer"
    assert payload["handoff"]["classification"]["profileset_count"] == 2


def test_explain_input_requires_content() -> None:
    result = runner.invoke(app, ["explain-input", "--text", "   "])
    assert result.exit_code == 1
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "invalid_query"


def test_simc_handoff_shell_quotes_untrusted_talents() -> None:
    malicious = "x'; touch /tmp/pwned; '"
    text = f'mage="Main"\nspec=frost\ntalents={malicious}\n'
    handoff = simc_handoff(text, classify_simc_input(text))
    decode = next(c["command"] for c in handoff["suggested_simc_commands"] if c["command"].startswith("simc decode-build"))
    # Re-tokenizing the suggested command keeps the injected value as one inert argument.
    tokens = shlex.split(decode)
    assert tokens[tokens.index("--talents") + 1] == malicious
    assert "touch" not in tokens  # the injected command never becomes its own token


def test_simc_handoff_emits_split_talent_flags() -> None:
    # A profile using the split talent keys (no combined talents= line) must still produce the
    # decode/describe handoff, carrying each split string via its own flag. simc-cli treats this
    # form as first-class and simc decode-build/describe-build accept --class/spec/hero-talents.
    text = 'warrior="Main"\nspec=fury\nclass_talents=CIEAAAA\nspec_talents=DAEAAAA\nhero_talents=EAEAAAA\n'
    classification = classify_simc_input(text)
    assert classification["talents_present"] is True
    commands = [c["command"] for c in simc_handoff(text, classification)["suggested_simc_commands"]]
    decode = next(c for c in commands if c.startswith("simc decode-build"))
    assert "--class-talents CIEAAAA" in decode
    assert "--spec-talents DAEAAAA" in decode
    assert "--hero-talents EAEAAAA" in decode
    assert decode.count("--talents ") == 0  # no combined flag when only split keys are present
    assert any(c.startswith("simc describe-build") for c in commands)


def test_simc_handoff_prefers_combined_talents_over_split() -> None:
    # When both a combined talents= line and split keys are present, the combined form wins
    # (it is the canonical addon-export shape); split flags are not also appended.
    text = 'mage="Main"\nspec=frost\ntalents=CYG\nclass_talents=IGNOREDD\n'
    decode = next(
        c["command"]
        for c in simc_handoff(text, classify_simc_input(text))["suggested_simc_commands"]
        if c["command"].startswith("simc decode-build")
    )
    assert "--talents CYG" in decode
    assert "--class-talents" not in decode


def test_classify_strips_trailing_inline_comments() -> None:
    # SimC allows trailing `# ...` comments; they must not leak into the actor match or the
    # spec/talents values (which would corrupt the suggested decode/describe commands).
    text = 'mage="Main" # my actor\nspec=frost # cleave\ntalents=CYG # raid build\n'
    classification = classify_simc_input(text)
    assert classification["actor_class"] == "mage"
    assert classification["spec"] == "frost"
    assert classification["sim_type_guess"] == "quick_sim"
    decode = next(
        c["command"]
        for c in simc_handoff(text, classification)["suggested_simc_commands"]
        if c["command"].startswith("simc decode-build")
    )
    assert "--spec frost" in decode
    assert "--talents CYG" in decode
    assert "#" not in decode


def test_classify_recognizes_underscore_class_tokens() -> None:
    # SimC's addon/profiles use the no-underscore actor form (deathknight=), but death_knight=
    # / demon_hunter= are documented, SimC-accepted manual-creation keywords a report could
    # carry. Both must classify as a quick sim and emit the handoff with the canonical class.
    text = 'death_knight="Main"\nspec=frost\ntalents=CYG\n'
    classification = classify_simc_input(text)
    assert classification["actor_class"] == "deathknight"
    assert classification["sim_type_guess"] == "quick_sim"
    decode = next(
        c["command"]
        for c in simc_handoff(text, classification)["suggested_simc_commands"]
        if c["command"].startswith("simc decode-build")
    )
    assert "--actor-class deathknight" in decode
    assert classify_simc_input('demon_hunter="Alt"\nspec=havoc\n')["actor_class"] == "demonhunter"


def test_simc_handoff_describe_build_flags_apl_requirement() -> None:
    # describe-build (unlike decode-build) needs a default/explicit APL, so the suggestion
    # must surface that an agent won't otherwise know it can fail with not_found.
    text = 'mage="Main"\nspec=frost\ntalents=CYG\n'
    handoff = simc_handoff(text, classify_simc_input(text))
    commands = handoff["suggested_simc_commands"]
    describe = next(c for c in commands if c["command"].startswith("simc describe-build"))
    decode = next(c for c in commands if c["command"].startswith("simc decode-build"))
    assert "apl" in describe["requires"].lower()
    assert "requires" not in decode  # decode-build is self-sufficient


def test_simc_handoff_omits_decode_without_class_and_spec() -> None:
    # An advanced/raw input with a talents line but no actor declaration cannot resolve
    # class/spec, so only the full-profile run is suggested.
    handoff = simc_handoff("talents=CYG\ndesired_targets=3\n", classify_simc_input("talents=CYG\ndesired_targets=3\n"))
    commands = [c["command"] for c in handoff["suggested_simc_commands"]]
    assert commands == ["simc sim -"]

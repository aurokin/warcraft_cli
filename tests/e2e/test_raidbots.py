"""End-to-end journeys for the ``raidbots`` binary.

Raidbots publishes no report index and its reports expire, so there is no discoverable public
report to pin. The always-on journeys therefore cover doctor, the two not-supported stubs, the
fully local ``explain-input`` surface, and the ``inspect-report``/``input`` error paths. Set
``WARCRAFT_E2E_RAIDBOTS_REPORT`` to a live report URL or id to also exercise the happy path.
"""

from __future__ import annotations

from pathlib import Path

from tests.e2e.harness import EXIT_GENERIC, EXIT_NOT_FOUND, EXIT_USAGE, run

# A report id that matches Raidbots' slug alphabet but has never existed.
MISSING_REPORT_ID = "warcraftcliE2Emissing"

QUICK_SIM_INPUT = 'mage="E2ETestchar"\nlevel=80\nrace=troll\nspec=frost\ntalents=CYQAAA\n'
MULTI_PROFILE_INPUT = 'priest="E2ETestchar"\nspec=shadow\nprofileset."a"=talents=X\nprofileset."b"=talents=Y\n'


def test_doctor_reports_url_templates_cache_and_capabilities(require) -> None:
    require("raidbots")
    result = run("raidbots", "doctor")
    assert result.data["auth"] == {"required": False, "deferred": True}

    templates = result.data["url_templates"]
    assert templates["base_url"] == "https://www.raidbots.com"
    assert templates["report"].endswith("/simbot/report/{id}")
    assert templates["data_json"].endswith("/data.json")
    assert templates["simc_input"].endswith("/simc")

    cache = result.data["cache"]
    assert cache["enabled"] is True
    # The session cache root is isolated, so doctor must report that directory and not ~/.cache.
    assert cache["backend"] == "file"
    assert cache["ttls"]["report"] > 0

    capabilities = result.data["capabilities"]
    assert capabilities["inspect_report"] == "ready"
    assert capabilities["input"] == "ready"
    assert capabilities["search"] == "not_supported"
    assert capabilities["submit"] == "not_supported"


def test_doctor_cache_dir_lives_under_the_isolated_cache_root(require, cache_root: Path) -> None:
    require("raidbots")
    result = run("raidbots", "doctor")
    assert Path(result.data["cache"]["cache_dir"]).is_relative_to(cache_root)


def test_search_and_resolve_are_structured_not_supported_stubs(require) -> None:
    require("raidbots")
    for command, kind in (("search", "search_results"), ("resolve", "resolve_match")):
        result = run("raidbots", command, "droptimizer for my mage")
        assert result.payload["kind"] == kind
        assert result.data["not_supported"] is True
        assert result.data["results"] == []
        assert result.data["count"] == 0
        assert result.data["suggested_command"] == "raidbots inspect-report <url-or-id>"
        assert "no public report index" in result.data["message"]


def test_explain_input_classifies_a_quick_sim_profile(require) -> None:
    require("raidbots")
    result = run("raidbots", "explain-input", "--text", QUICK_SIM_INPUT)
    assert result.data["scope"] == {"type": "raidbots_simc_input", "sim_type_guess": "quick_sim"}
    classification = result.data["handoff"]["classification"]
    assert classification["actor_class"] == "mage"
    assert classification["actor_name"] == "E2ETestchar"
    assert classification["spec"] == "frost"
    assert classification["talents_present"] is True
    assert result.data["handoff"]["ready_to_paste"] == QUICK_SIM_INPUT
    commands = [row["command"] for row in result.data["handoff"]["suggested_simc_commands"]]
    assert "simc sim -" in commands
    assert any(command.startswith("simc decode-build ") for command in commands)


def test_explain_input_reads_stdin_and_a_file(require, out_dir: Path) -> None:
    require("raidbots")
    from_stdin = run("raidbots", "explain-input", stdin=MULTI_PROFILE_INPUT)
    assert from_stdin.data["scope"]["sim_type_guess"] == "top_gear_or_droptimizer"
    assert from_stdin.data["handoff"]["classification"]["profileset_count"] == 2

    path = out_dir / "profile.simc"
    path.write_text(MULTI_PROFILE_INPUT, encoding="utf-8")
    from_file = run("raidbots", "explain-input", "--file", str(path))
    assert from_file.data == from_stdin.data


def test_explain_input_rejects_empty_and_contradictory_input(require) -> None:
    require("raidbots")
    empty = run("raidbots", "explain-input", stdin="", expect=EXIT_USAGE, error_code="invalid_query")
    assert "--text, --file, or stdin" in empty.payload["error"]["message"]

    both = run("raidbots", "explain-input", "--text", "x", "--file", "y", expect=EXIT_USAGE, error_code="invalid_query")
    assert "only one of --text or --file" in both.payload["error"]["message"]

    missing = run("raidbots", "explain-input", "--file", "/nope/does-not-exist.simc", expect=EXIT_USAGE, error_code="invalid_query")
    assert "Could not read input file" in missing.payload["error"]["message"]


def test_an_unparseable_reference_is_rejected_before_the_network(require) -> None:
    require("raidbots")
    result = run("raidbots", "inspect-report", "https://www.raidbots.com/x/", expect=EXIT_GENERIC, error_code="invalid_report")
    assert "Could not extract a report ID" in result.payload["error"]["message"]


def test_a_missing_report_is_not_found_on_both_report_surfaces(require) -> None:
    require("raidbots")
    # data.json redirects to a public GCS bucket that answers 403 for an object that is absent or
    # expired; Raidbots takes no credentials, so that can only mean "no such report".
    inspect = run("raidbots", "inspect-report", MISSING_REPORT_ID, expect=EXIT_NOT_FOUND, error_code="not_found")
    assert inspect.payload["error"]["details"]["status_code"] in {403, 404}
    assert "expired or is private" in inspect.payload["error"]["message"]

    # /simc answers HTTP 200 with the Raidbots single-page app instead of 404, so the CLI has to
    # reject the page rather than hand HTML back as SimC input.
    simc_input = run("raidbots", "input", MISSING_REPORT_ID, expect=EXIT_NOT_FOUND, error_code="not_found")
    assert "SimC input" in simc_input.payload["error"]["message"]
    assert "<html" not in simc_input.stderr.lower()


def test_a_live_report_round_trips_through_inspect_and_input(require, optional) -> None:
    require("raidbots")
    # Raidbots reports expire, so no pin can stay valid; point this at a fresh public report to
    # exercise the happy path (see tmp/handoffs/e2e-experimental-contract.md).
    reference = optional("raidbots-report", "WARCRAFT_E2E_RAIDBOTS_REPORT")

    report = run("raidbots", "inspect-report", reference)
    assert report.payload["kind"] == "report"
    parsed = report.data["report"]
    assert parsed["kind"] in {"quick_sim", "multi_profile", "unknown"}
    assert report.data["scope"] == {"type": "raidbots_report", "kind": parsed["kind"]}
    assert report.data["citations"]["report_url"].endswith(parsed["report_id"])
    assert report.data["freshness"]["from_cache"] is False
    assert report.data["raw"]

    trimmed = run("raidbots", "inspect-report", reference, "--no-raw")
    assert "raw" not in trimmed.data
    assert trimmed.data["report"] == parsed
    # The first fetch populated the isolated report cache.
    assert trimmed.data["freshness"]["from_cache"] is True

    simc_input = run("raidbots", "input", reference)
    text = simc_input.data["input"]
    assert text.strip()
    assert not text.lstrip().lower().startswith("<!doctype")
    assert simc_input.data["report_id"] == parsed["report_id"]
    assert simc_input.data["handoff"]["ready_to_paste"] == text

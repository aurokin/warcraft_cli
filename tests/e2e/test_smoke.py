"""Harness self-check: the contract holds on the simplest journeys before the real ones run."""

from __future__ import annotations

from tests.e2e import harness
from tests.e2e.harness import EXIT_NETWORK, EXIT_USAGE, run, run_text


def test_warcraft_doctor_reports_every_provider(doctor_rows):
    assert {"wowhead", "warcraftlogs", "simc", "raiderio", "warcraft-wiki", "icy-veins", "method", "lorrgs", "raidbots", "blizzard-api", "curseforge"} <= set(doctor_rows)
    assert all(row.get("tier") in {"core", "supported", "experimental"} for row in doctor_rows.values())


def test_help_is_plain_text_for_every_binary():
    for binary in ("warcraft", "wowhead", "warcraftlogs", "simc", "raiderio", "warcraft-wiki", "icy-veins", "method", "lorrgs", "raidbots", "blizzard", "curseforge"):
        result = run_text(binary, "--help")
        assert "Usage:" in result.stdout, result.describe()


def test_usage_error_exits_2_without_traceback():
    result = harness.run_raw("wowhead", "entity")
    assert result.exit_code == EXIT_USAGE, result.describe()
    assert "Traceback" not in result.stderr


def test_network_failure_is_an_exit_5_envelope_on_stderr():
    # The session cache may already hold this item from another journey; a dead proxy proves
    # nothing unless the cache is bypassed too.
    result = run("wowhead", "entity", "item", "19019", expect=EXIT_NETWORK, env={**harness.dead_proxy_env(), **harness.no_cache_env()})
    assert result.error_code in {"network_error", "timeout"}, result.describe()
    assert result.stdout == ""

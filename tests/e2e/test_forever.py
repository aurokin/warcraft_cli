"""Installed-binary Forever scope and handoff boundaries, without private provider access."""

from __future__ import annotations

import pytest

from tests.e2e.harness import EXIT_USAGE, run


def test_forever_doctor_discloses_only_verified_provider_and_reasons():
    result = run("warcraft", "--expansion", "forever", "doctor")
    assert result.data["included_providers"] == ["wowhead"], result.describe()
    providers = {row["provider"]: row for row in result.data["providers"]}
    for provider in ("simc", "blizzard-api", "curseforge", "warcraftlogs"):
        support = providers[provider]["expansion_support"]
        assert support["allowed"] is False and support["verification_note"], result.describe()


@pytest.mark.parametrize("provider", ["simc", "blizzard", "curseforge", "warcraftlogs", "raiderio"])
def test_forever_unverified_passthrough_is_rejected(provider):
    result = run("warcraft", "--expansion", "forever", provider, "doctor", expect=EXIT_USAGE)
    assert result.error_code == "unsupported_provider_expansion", result.describe()


@pytest.mark.parametrize("args", [
    ("--expansion", "forever", "actor-profile", "ABC123", "Asha Brightvale"),
    ("--expansion", "forever", "talent-packet", "warrior/fury/ABC123"),
    ("talent-describe", "https://www.wowhead.com/forever/talent-calc/warrior/v21-1-1_t0"),
])
def test_forever_source_cannot_use_retail_handoff(args):
    result = run("warcraft", *args, expect=EXIT_USAGE)
    assert result.error_code == "unsupported_game_variant", result.describe()

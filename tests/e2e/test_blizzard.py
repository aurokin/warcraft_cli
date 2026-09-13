"""End-to-end journeys for the ``blizzard`` binary.

These run against the real Battle.net Game Data and Profile APIs with the OAuth client credentials
in ``~/.config/warcraft/providers``. The identifiers come from tests/e2e/pins.py because Blizzard
publishes no index to discover them from, and all three (a 2005 item, a large realm, the
maintainer's character) are stable.
"""

from __future__ import annotations

from pathlib import Path

from tests.e2e.harness import EXIT_AUTH, EXIT_GENERIC, EXIT_NOT_FOUND, run
from tests.e2e.pins import CHARACTER_NAME, GUILD_REALM, ITEM_ID, ITEM_NAME, REALM_SLUG


def test_doctor_reports_configured_credentials_and_live_routing(require) -> None:
    require("blizzard-api")
    result = run("blizzard", "doctor")
    auth = result.data["auth"]
    assert auth["required"] is True
    assert auth["configured"] is True, "Blizzard client credentials are not configured on this machine"
    assert auth["flow"] == "oauth_client_credentials"

    region = result.data["region"]
    assert region["default"] == "us"
    assert region["routing"] == "ready"
    # Everything but CN is confirmed live; CN's host and OAuth server are unreachable from here.
    assert region["verification"]["retail"] == "live_confirmed"
    assert region["verification"]["unverified_regions"] == ["cn"]

    capabilities = result.data["capabilities"]
    assert capabilities["game_data"] == "ready"
    assert capabilities["profile"] == "ready"
    assert capabilities["search"] == "coming_soon"


def test_realm_read_uses_the_dynamic_namespace(require) -> None:
    require("blizzard-api")
    result = run("blizzard", "realm", REALM_SLUG)
    assert result.payload["kind"] == "realm"
    assert result.payload["provenance"]["namespace"] == "dynamic-us"
    assert result.payload["provenance"]["namespace_class"] == "dynamic"
    assert result.payload["provenance"]["verified"] is True
    assert result.data["slug"] == REALM_SLUG
    assert isinstance(result.data["id"], int)
    assert result.data["name"].lower() == REALM_SLUG
    assert result.data["connected_realm"]["href"].startswith("https://us.api.blizzard.com/")


def test_item_read_uses_the_static_namespace_and_honours_locale(require) -> None:
    require("blizzard-api")
    result = run("blizzard", "item", str(ITEM_ID))
    assert result.payload["provenance"]["namespace"] == "static-us"
    assert result.data["id"] == ITEM_ID
    assert result.data["name"] == ITEM_NAME
    assert result.data["quality"]["type"] == "LEGENDARY"

    localized = run("blizzard", "item", str(ITEM_ID), "--locale", "de_DE")
    assert localized.payload["provenance"]["locale"] == "de_DE"
    assert "locale=de_DE" in localized.payload["provenance"]["source_url"]
    # Same item, translated by Blizzard: proof the locale reaches the API rather than being dropped.
    assert localized.data["id"] == ITEM_ID
    assert localized.data["name"] != ITEM_NAME


def test_character_read_uses_the_profile_namespace(require) -> None:
    require("blizzard-api")
    result = run("blizzard", "character", GUILD_REALM, CHARACTER_NAME)
    assert result.payload["kind"] == "character"
    assert result.payload["provenance"]["namespace"] == "profile-us"
    assert result.payload["provenance"]["namespace_class"] == "profile"
    assert result.data["name"] == CHARACTER_NAME
    assert result.data["realm"]["slug"] == GUILD_REALM
    assert isinstance(result.data["level"], int)
    assert result.data["character_class"]["name"]


def test_region_and_game_version_change_the_namespace(require) -> None:
    require("blizzard-api")
    european = run("blizzard", "item", str(ITEM_ID), "--region", "eu")
    assert european.payload["provenance"]["namespace"] == "static-eu"
    assert european.payload["provenance"]["source_url"].startswith("https://eu.api.blizzard.com/")
    assert european.data["id"] == ITEM_ID

    classic = run("blizzard", "item", str(ITEM_ID), "--classic")
    assert classic.payload["provenance"]["namespace"] == "static-classic-us"
    assert classic.payload["provenance"]["game_version"] == "classic"
    assert classic.data["id"] == ITEM_ID

    explicit = run("blizzard", "item", str(ITEM_ID), "--game-version", "classic")
    assert explicit.payload["provenance"]["namespace"] == classic.payload["provenance"]["namespace"]


def test_search_and_resolve_are_structured_coming_soon_stubs(require) -> None:
    require("blizzard-api")
    for command in ("search", "resolve"):
        result = run("blizzard", command, "thunderfury")
        assert result.payload["kind"] == "coming_soon"
        assert result.data["coming_soon"] is True
        assert result.data["results"] == []
        assert result.data["suggested_command"].startswith("blizzard ")
        assert f"blizzard {command} is not implemented yet" in result.data["message"]


def test_an_unknown_item_is_not_found(require) -> None:
    require("blizzard-api")
    result = run("blizzard", "item", "1", expect=EXIT_NOT_FOUND, error_code="not_found")
    assert result.payload["error"]["details"]["status_code"] == 404
    assert "/data/wow/item/1" in result.payload["error"]["details"]["url"]


def test_routing_contradictions_are_refused_before_the_network(require) -> None:
    require("blizzard-api")
    region = run("blizzard", "item", str(ITEM_ID), "--region", "oc", expect=EXIT_GENERIC, error_code="unsupported_region")
    assert "'oc'" in region.payload["error"]["message"]

    versions = run(
        "blizzard", "item", str(ITEM_ID), "--classic", "--game-version", "retail",
        expect=EXIT_GENERIC, error_code="unsupported_game_version",
    )
    assert "--classic conflicts with" in versions.payload["error"]["message"]

    profile = run(
        "blizzard", "character", GUILD_REALM, CHARACTER_NAME, "--classic",
        expect=EXIT_GENERIC, error_code="classic_profile_unsupported",
    )
    assert "retail-only" in profile.payload["error"]["message"]


def test_missing_credentials_exit_3_with_a_recovery_hint(require, tmp_path: Path) -> None:
    require("blizzard-api")
    # Point credential discovery at an empty config root so the real key is never consulted.
    blank = {"XDG_CONFIG_HOME": str(tmp_path / "config"), "BLIZZARD_CLIENT_ID": "", "BLIZZARD_CLIENT_SECRET": ""}
    result = run("blizzard", "item", str(ITEM_ID), expect=EXIT_AUTH, error_code="missing_client_credentials", env=blank)
    assert "BLIZZARD_CLIENT_ID" in result.payload["error"]["message"]
    assert "BLIZZARD_CLIENT_SECRET" in result.payload["error"]["message"]

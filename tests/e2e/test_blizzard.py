"""End-to-end journeys for the ``blizzard`` binary.

These run against the real Battle.net Game Data and Profile APIs with the OAuth client credentials
in ``~/.config/warcraft/providers``. The identifiers come from tests/e2e/pins.py because Blizzard
publishes no index to discover them from, and all three (a 2005 item, a large realm, the
maintainer's character) are stable.
"""

from __future__ import annotations

from pathlib import Path

from tests.e2e.harness import EXIT_AUTH, EXIT_NOT_FOUND, EXIT_USAGE, Result, dead_proxy_env, no_cache_env, run
from tests.e2e.pins import CHARACTER_NAME, GUILD_REALM, ITEM_ID, ITEM_NAME, REALM_SLUG


def _assert_the_namespace_reached_the_api(result: Result, namespace: str) -> None:
    """The routing label and the URL that was actually sent have to agree.

    ``provenance.namespace`` is the routing object describing itself, so on its own it proves
    nothing: Blizzard serves Thunderfury under both the retail and the classic namespace, and a
    client that routed one while labelling itself the other would look identical. ``source_url`` is
    ``str(response.request.url)``, so it is the only witness of what went on the wire.
    """
    provenance = result.payload["provenance"]
    assert provenance["namespace"] == namespace, result.describe()
    assert f"namespace={namespace}" in provenance["source_url"], result.describe()


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
    _assert_the_namespace_reached_the_api(result, "dynamic-us")
    assert result.payload["provenance"]["namespace_class"] == "dynamic"
    assert result.payload["provenance"]["verified"] is True
    assert result.data["slug"] == REALM_SLUG
    assert isinstance(result.data["id"], int)
    assert result.data["name"].lower() == REALM_SLUG
    connected_realm = result.data["connected_realm"]["href"]
    assert connected_realm.startswith("https://us.api.blizzard.com/")
    # The connected-realm link is keyed by this realm's own id; a mismatch means the record and the
    # link came from different realms.
    assert f"/connected-realm/{result.data['id']}?" in connected_realm, result.describe()


def test_item_read_uses_the_static_namespace_and_honours_locale(require) -> None:
    require("blizzard-api")
    result = run("blizzard", "item", str(ITEM_ID))
    _assert_the_namespace_reached_the_api(result, "static-us")
    assert result.data["id"] == ITEM_ID
    assert result.data["name"] == ITEM_NAME
    assert result.data["quality"]["type"] == "LEGENDARY"

    localized = run("blizzard", "item", str(ITEM_ID), "--locale", "de_DE")
    assert localized.payload["provenance"]["locale"] == "de_DE"
    assert "locale=de_DE" in localized.payload["provenance"]["source_url"]
    # Same item, translated by Blizzard: proof the locale reaches the API rather than being dropped.
    assert localized.data["id"] == ITEM_ID
    assert localized.data["name"] != ITEM_NAME


def test_character_read_uses_the_profile_namespace_and_agrees_with_the_realm_read(require) -> None:
    require("blizzard-api")
    result = run("blizzard", "character", GUILD_REALM, CHARACTER_NAME)
    assert result.payload["kind"] == "character"
    _assert_the_namespace_reached_the_api(result, "profile-us")
    assert result.payload["provenance"]["namespace_class"] == "profile"
    assert result.data["name"] == CHARACTER_NAME
    assert result.data["realm"]["slug"] == GUILD_REALM
    assert isinstance(result.data["level"], int)
    assert result.data["character_class"]["name"]

    # Two namespaces, one realm: the profile read and the dynamic Game Data read have to name the
    # same realm record, or one of the two routings is pointed somewhere else.
    realm = run("blizzard", "realm", GUILD_REALM)
    assert realm.data["id"] == result.data["realm"]["id"]
    assert realm.data["name"] == result.data["realm"]["name"]


def test_a_realm_display_name_reaches_the_realm_its_slug_names(require) -> None:
    """Blizzard slugs drop apostrophes, so the name a player types has to be tried as a slug spelling."""
    require("blizzard-api")
    by_slug = run("blizzard", "realm", "malganis")
    by_name = run("blizzard", "realm", "Mal'Ganis")
    assert by_name.data["slug"] == "malganis", by_name.describe()
    assert by_name.data["id"] == by_slug.data["id"], by_name.describe()


def test_region_and_game_version_change_the_namespace(require) -> None:
    require("blizzard-api")
    european = run("blizzard", "item", str(ITEM_ID), "--region", "eu")
    _assert_the_namespace_reached_the_api(european, "static-eu")
    assert european.payload["provenance"]["source_url"].startswith("https://eu.api.blizzard.com/")
    assert european.data["id"] == ITEM_ID
    # The EU host echoes itself in the links it returns, so the region is confirmed by the answer.
    assert european.data["_links"]["self"]["href"].startswith("https://eu.api.blizzard.com/"), european.describe()

    retail = run("blizzard", "item", str(ITEM_ID))
    classic = run("blizzard", "item", str(ITEM_ID), "--classic")
    _assert_the_namespace_reached_the_api(classic, "static-classic-us")
    assert classic.payload["provenance"]["game_version"] == "classic"
    assert classic.data["id"] == ITEM_ID
    # The classic dataset answers with the 2005 record: Blizzard stamps its own build-qualified
    # namespace into every link it returns, and the two datasets disagree about the item itself.
    assert "-classic-us" in classic.data["_links"]["self"]["href"], classic.describe()
    assert "-classic-" not in retail.data["_links"]["self"]["href"], retail.describe()
    assert (classic.data["level"], classic.data["required_level"]) != (retail.data["level"], retail.data["required_level"])

    explicit = run("blizzard", "item", str(ITEM_ID), "--game-version", "classic")
    _assert_the_namespace_reached_the_api(explicit, "static-classic-us")
    assert explicit.data == classic.data, "--game-version classic and --classic must read one dataset"


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


def test_bad_routing_flags_are_usage_errors_refused_before_the_network(require) -> None:
    """Every routing rejection is a "fix the command" answer: exit 2, and no round trip spent.

    The dead proxy and the disabled cache are the proof of "before the network": any of these that
    reached Blizzard would come back as a network failure (exit 5) instead.
    """
    require("blizzard-api")
    offline = {**dead_proxy_env(), **no_cache_env()}

    region = run("blizzard", "item", str(ITEM_ID), "--region", "oc", expect=EXIT_USAGE, error_code="unsupported_region", env=offline)
    assert "'oc'" in region.payload["error"]["message"]

    version = run(
        "blizzard", "item", str(ITEM_ID), "--game-version", "bogus",
        expect=EXIT_USAGE, error_code="unsupported_game_version", env=offline,
    )
    assert "'bogus'" in version.payload["error"]["message"]

    conflict = run(
        "blizzard", "item", str(ITEM_ID), "--classic", "--game-version", "retail",
        expect=EXIT_USAGE, error_code="unsupported_game_version", env=offline,
    )
    assert "--classic conflicts with" in conflict.payload["error"]["message"]

    profile = run(
        "blizzard", "character", GUILD_REALM, CHARACTER_NAME, "--classic",
        expect=EXIT_USAGE, error_code="classic_profile_unsupported", env=offline,
    )
    assert "retail-only" in profile.payload["error"]["message"]

    # A value Click itself rejects takes the same exit code through the shared envelope.
    bad_id = run("blizzard", "item", "not-an-item-id", expect=EXIT_USAGE, error_code="invalid_argument", env=offline)
    assert "item_id" in bad_id.payload["error"]["message"]


def test_missing_credentials_exit_3_with_a_recovery_hint(require, tmp_path: Path) -> None:
    require("blizzard-api")
    # Point credential discovery at an empty config root so the real key is never consulted.
    blank = {"XDG_CONFIG_HOME": str(tmp_path / "config"), "BLIZZARD_CLIENT_ID": "", "BLIZZARD_CLIENT_SECRET": ""}
    result = run("blizzard", "item", str(ITEM_ID), expect=EXIT_AUTH, error_code="missing_client_credentials", env=blank)
    assert "BLIZZARD_CLIENT_ID" in result.payload["error"]["message"]
    assert "BLIZZARD_CLIENT_SECRET" in result.payload["error"]["message"]

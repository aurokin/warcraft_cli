"""End-to-end journeys for the ``curseforge`` binary.

These run against the real CurseForge Core API with the key in ``~/.config/warcraft/providers``.
Deadly Boss Mods (mod id 3358) is the pin: it has been the same WoW project for over a decade, so
both resolution paths — numeric mod id and slug search — can be checked against each other.
"""

from __future__ import annotations

from pathlib import Path

from tests.e2e.harness import EXIT_AUTH, EXIT_NOT_FOUND, EXIT_USAGE, Result, run
from tests.e2e.pins import CURSEFORGE_ADDON_ID, CURSEFORGE_ADDON_SLUG


def _assert_addon_payload(result: Result) -> None:
    """Every addon envelope carries metadata, latest files, and the newest file's changelog."""
    metadata = result.data["metadata"]
    assert metadata["id"] == int(CURSEFORGE_ADDON_ID)
    assert metadata["slug"] == CURSEFORGE_ADDON_SLUG
    assert metadata["gameId"] == 1
    assert metadata["links"]["websiteUrl"].startswith("https://www.curseforge.com/wow/addons/")

    latest_files = result.data["latest_files"]
    assert latest_files, result.describe()
    assert all(isinstance(row["id"], int) and row["fileName"] for row in latest_files), result.describe()

    changelog = result.data["changelog"]
    assert "error" not in changelog, result.describe()
    assert changelog["file_id"] in {row["id"] for row in latest_files}
    assert changelog["source_url"].endswith("/changelog")


def test_doctor_reports_the_api_key_and_capabilities(require) -> None:
    require("curseforge")
    result = run("curseforge", "doctor")
    assert result.data["tier"] == "experimental"
    auth = result.data["auth"]
    assert auth["required"] is True
    assert auth["configured"] is True, "no CurseForge API key is configured on this machine"
    assert auth["flow"] == "api_key"
    assert auth["key_env"] == "CURSEFORGE_API_KEY"

    capabilities = result.data["capabilities"]
    assert capabilities["addon"] == "ready"
    assert capabilities["search"] == "coming_soon"
    assert capabilities["resolve"] == "coming_soon"


def test_addon_by_numeric_mod_id(require) -> None:
    require("curseforge")
    result = run("curseforge", "addon", CURSEFORGE_ADDON_ID)
    assert result.payload["kind"] == "addon"
    provenance = result.payload["provenance"]
    assert provenance["resolved_by"] == "id"
    assert provenance["mod_id"] == int(CURSEFORGE_ADDON_ID)
    assert provenance["game_id"] == 1
    assert provenance["verified"] is True
    # The id path never calls /v1/mods/search, so no search citation is emitted.
    assert set(provenance["source_urls"]) == {"mod", "changelog"}
    _assert_addon_payload(result)


def test_addon_by_slug_resolves_to_the_same_mod(require) -> None:
    require("curseforge")
    result = run("curseforge", "addon", CURSEFORGE_ADDON_SLUG)
    provenance = result.payload["provenance"]
    # Slug lookups go through /v1/mods/search, which is a separately scoped CurseForge capability;
    # a key without search access fails here with an actionable auth_failed instead.
    assert provenance["resolved_by"] == "slug_search"
    assert provenance["mod_id"] == int(CURSEFORGE_ADDON_ID)
    assert "/v1/mods/search" in provenance["source_urls"]["search"]
    assert f"slug={CURSEFORGE_ADDON_SLUG}" in provenance["source_urls"]["search"]
    _assert_addon_payload(result)

    # The two resolution paths must land on one mod record, not merely on two records that each
    # happen to carry the pinned id.
    by_id = run("curseforge", "addon", CURSEFORGE_ADDON_ID)
    assert result.data["metadata"] == by_id.data["metadata"], result.describe()


def test_search_and_resolve_are_structured_coming_soon_stubs(require) -> None:
    require("curseforge")
    for command in ("search", "resolve"):
        result = run("curseforge", command, "boss mods")
        assert result.payload["kind"] == "coming_soon"
        assert result.data["coming_soon"] is True
        assert result.data["results"] == []
        assert result.data["suggested_command"] == "curseforge addon deadly-boss-mods"
        assert f"curseforge {command} is not implemented yet" in result.data["message"]

        # The stubs still validate their one flag rather than accepting anything until they ship.
        rejected = run("curseforge", command, "boss mods", "--limit", "0", expect=EXIT_USAGE, error_code="invalid_argument")
        assert "--limit" in rejected.payload["error"]["message"], rejected.describe()


def test_unknown_slug_and_unknown_id_are_not_found(require) -> None:
    require("curseforge")
    slug = run("curseforge", "addon", "no-such-warcraft-addon-xyz", expect=EXIT_NOT_FOUND, error_code="addon_not_found")
    assert "no-such-warcraft-addon-xyz" in slug.payload["error"]["message"]

    mod_id = run("curseforge", "addon", "999999999", expect=EXIT_NOT_FOUND, error_code="addon_not_found")
    assert "999999999" in mod_id.payload["error"]["message"]


def test_a_non_wow_mod_id_is_refused(require) -> None:
    require("curseforge")
    # CurseForge mod ids are global across games; provenance hardcodes game_id=1, so a Minecraft
    # project must not come back as a WoW addon.
    result = run("curseforge", "addon", "238222", expect=EXIT_NOT_FOUND, error_code="addon_not_found")
    assert "not a World of Warcraft addon" in result.payload["error"]["message"]


def test_a_missing_api_key_exits_3_with_a_recovery_hint(require, tmp_path: Path) -> None:
    require("curseforge")
    blank = {"XDG_CONFIG_HOME": str(tmp_path / "config"), "CURSEFORGE_API_KEY": ""}
    result = run("curseforge", "addon", CURSEFORGE_ADDON_ID, expect=EXIT_AUTH, error_code="missing_api_key", env=blank)
    assert "CURSEFORGE_API_KEY" in result.payload["error"]["message"]

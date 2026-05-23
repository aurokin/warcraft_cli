from __future__ import annotations

from wowhead_cli.expansion_profiles import (
    build_entity_url,
    build_guide_lookup_url,
    detect_expansion_from_url,
    expansion_url_policy_issues,
    list_profiles,
    parse_entity_from_wowhead_url,
    resolve_expansion,
    url_matches_expansion_profile,
)
from wowhead_cli.wowhead_client import entity_url, guide_url, search_url


def test_resolve_expansion_aliases() -> None:
    assert resolve_expansion(None).key == "retail"
    assert resolve_expansion("default").key == "retail"
    assert resolve_expansion("wrath").key == "wotlk"
    assert resolve_expansion("cataclysm").key == "cata"
    assert resolve_expansion("mists").key == "mop-classic"
    assert resolve_expansion("classicptr").key == "classic-ptr"


def test_build_entity_url_with_prefix() -> None:
    profile = resolve_expansion("wotlk")
    assert build_entity_url(profile, "item", 19019) == "https://www.wowhead.com/wotlk/item=19019"


def test_profile_list_contains_expected_keys() -> None:
    keys = {profile.key for profile in list_profiles()}
    assert {"retail", "classic", "tbc", "wotlk", "cata", "mop-classic"}.issubset(keys)


def test_public_url_helpers_support_expansion() -> None:
    assert entity_url("item", 19019, expansion="classic") == "https://www.wowhead.com/classic/item=19019"
    assert search_url("thunderfury", expansion="wotlk").startswith("https://www.wowhead.com/wotlk/search?q=")
    assert guide_url(3143, expansion="retail") == "https://www.wowhead.com/guide=3143"


def test_build_guide_lookup_url_with_prefix() -> None:
    profile = resolve_expansion("wotlk")
    assert build_guide_lookup_url(profile, 3143) == "https://www.wowhead.com/wotlk/guide=3143"


def test_detect_expansion_from_url_matches_profile_builders() -> None:
    profile = resolve_expansion("cata")
    url = build_entity_url(profile, "spell", 12345)
    assert detect_expansion_from_url(url) == profile
    assert url_matches_expansion_profile(url, profile)


def test_expansion_url_policy_issues_reports_mismatch() -> None:
    profile = resolve_expansion("retail")
    issues = expansion_url_policy_issues("https://www.wowhead.com/wotlk/item=19019", profile=profile)
    assert issues
    assert "wotlk" in issues[0]


def test_parse_entity_from_wowhead_url_rejects_unknown_types() -> None:
    assert parse_entity_from_wowhead_url("https://www.wowhead.com/not-a-real-type=1") is None

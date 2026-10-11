from __future__ import annotations

import json
from pathlib import Path

import pytest
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


def test_parse_entity_from_wowhead_url_rejects_other_hosts() -> None:
    assert parse_entity_from_wowhead_url("https://evil.example.com/item=19019") is None
    assert parse_entity_from_wowhead_url("https://notwowhead.com/item=19019") is None
    assert parse_entity_from_wowhead_url("https://classic.wowhead.com/item=19019") == ("item", 19019)


def test_resolve_expansion_rejects_shared_keys_without_a_wowhead_site() -> None:
    with pytest.raises(ValueError, match=r"Unknown expansion 'fresh'\. Supported: retail, classic, .*, forever$"):
        resolve_expansion("fresh")


def test_forever_profile_matches_wowheads_captured_routing_metadata() -> None:
    # Captured from /forever/item=19019 on 2026-10-11; preserved without changing fields.
    page_meta = json.loads((Path(__file__).parent / "fixtures/wowhead/forever_page_meta.json").read_text())
    profile = resolve_expansion("wow-forever")
    assert profile.key == "forever"
    assert profile.data_env == page_meta["dataEnv"]["env"] == 16
    assert profile.path_prefix == "forever"
    assert profile.wowhead_base == "https://www.wowhead.com/forever"
    assert profile.nether_base == "https://nether.wowhead.com/forever"
    assert detect_expansion_from_url(build_entity_url(profile, "item", 19019)) == profile
    assert not url_matches_expansion_profile("https://www.wowhead.com/item=19019", profile)
    assert not url_matches_expansion_profile("https://www.wowhead.com/classic/item=19019", profile)

from __future__ import annotations

import pytest
from warcraft_core.expansions import (
    expansion_keys,
    list_expansions,
    normalize_expansion_key,
    resolve_expansion,
    warcraftlogs_site_for_expansion,
    wowhead_path_prefixes,
)


def test_expansion_keys_keep_wowhead_order_then_fresh() -> None:
    assert expansion_keys() == ("retail", "classic", "tbc", "wotlk", "cata", "mop-classic", "ptr", "beta", "classic-ptr", "fresh")


def test_resolve_expansion_accepts_aliases_and_defaults_to_retail() -> None:
    assert resolve_expansion(None).key == "retail"
    assert resolve_expansion("  ").key == "retail"
    assert resolve_expansion("Wrath").key == "wotlk"
    assert resolve_expansion("mop_classic").key == "mop-classic"
    assert resolve_expansion("anniversary").key == "fresh"
    assert resolve_expansion("classic-fresh").key == "fresh"
    assert normalize_expansion_key(" Classic_PTR ") == "classic-ptr"


def test_resolve_expansion_rejects_unknown_values_listing_all_keys() -> None:
    with pytest.raises(ValueError, match=r"Unknown expansion 'bogus'\. Supported: retail, classic, .*, fresh$"):
        resolve_expansion("bogus")


def test_warcraftlogs_site_mapping() -> None:
    assert warcraftlogs_site_for_expansion("retail") == "retail"
    assert warcraftlogs_site_for_expansion("wotlk") == "classic"
    assert warcraftlogs_site_for_expansion("fresh") == "fresh"
    with pytest.raises(ValueError, match="does not map to a Warcraft Logs site profile"):
        warcraftlogs_site_for_expansion("ptr")


def test_wowhead_path_prefixes_exclude_retail_and_fresh() -> None:
    assert wowhead_path_prefixes() == frozenset({"classic", "tbc", "wotlk", "cata", "mop-classic", "ptr", "beta", "classic-ptr"})
    fresh = next(expansion for expansion in list_expansions() if expansion.key == "fresh")
    assert fresh.wowhead_path_prefix is None

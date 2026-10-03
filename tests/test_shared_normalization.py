from __future__ import annotations

from warcraft_core.wow_normalization import (
    normalize_name,
    normalize_region,
    primary_realm_slug,
    profile_region,
    realm_slug_variants,
)


def test_shared_normalization_region_and_name() -> None:
    assert normalize_region("NA") == "us"
    assert normalize_region("north-america") == "us"
    assert normalize_name("  gn   ") == "gn"


def test_profile_region_reads_oceania_as_us() -> None:
    # Oceanic realms live in the US region on Blizzard and Raider.IO; `oc` is no profile region.
    assert normalize_region("oce") == "oc"
    assert profile_region("oce") == profile_region("Oceanic") == "us"
    assert profile_region("EU") == "eu"


def test_shared_normalization_realm_variants() -> None:
    # Blizzard, Raider.IO and Warcraft Logs all slug Mal'Ganis as malganis; mal-ganis fails WCL.
    assert realm_slug_variants("Mal'Ganis") == ["malganis", "mal-ganis"]
    assert realm_slug_variants("mal-ganis") == ["mal-ganis", "malganis"]
    assert primary_realm_slug("Area 52") == "area-52"


def test_shared_normalization_keeps_accented_and_cyrillic_realm_letters() -> None:
    # Dropping the letters sent festung-der-st-rme upstream. Raider.IO accepts ревущий-фьорд, but
    # Blizzard and Warcraft Logs slug native-script realm names in English (howling-fjord).
    assert primary_realm_slug("Festung der Stürme") == "festung-der-stürme"
    assert primary_realm_slug("Aggra (Português)") == "aggra-português"
    assert primary_realm_slug("Ревущий фьорд") == "ревущий-фьорд"
    assert realm_slug_variants("aggra-português") == ["aggra-português", "aggraportuguês"]

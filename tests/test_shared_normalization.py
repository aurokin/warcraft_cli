from __future__ import annotations

from warcraft_core.wow_normalization import normalize_name, normalize_region, primary_realm_slug, realm_slug_variants


def test_shared_normalization_region_and_name() -> None:
    assert normalize_region("NA") == "us"
    assert normalize_region("north-america") == "us"
    assert normalize_name("  gn   ") == "gn"


def test_shared_normalization_realm_variants() -> None:
    # Blizzard, Raider.IO and Warcraft Logs all slug Mal'Ganis as malganis; mal-ganis fails WCL.
    assert realm_slug_variants("Mal'Ganis") == ["malganis", "mal-ganis"]
    assert realm_slug_variants("mal-ganis") == ["mal-ganis", "malganis"]
    assert primary_realm_slug("Area 52") == "area-52"


def test_shared_normalization_keeps_accented_and_cyrillic_realm_letters() -> None:
    # Blizzard's realm slug and Raider.IO's realm param keep the letters (both confirmed live):
    # festung-der-stürme, ревущий-фьорд. Dropping them sent festung-der-st-rme upstream.
    assert primary_realm_slug("Festung der Stürme") == "festung-der-stürme"
    assert primary_realm_slug("Aggra (Português)") == "aggra-português"
    assert primary_realm_slug("Ревущий фьорд") == "ревущий-фьорд"
    assert realm_slug_variants("aggra-português") == ["aggra-português", "aggraportuguês"]

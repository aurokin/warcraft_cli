from __future__ import annotations

import re

REGION_ALIASES = {
    "us": "us",
    "na": "us",
    "northamerica": "us",
    "north-america": "us",
    "eu": "eu",
    "europe": "eu",
    "kr": "kr",
    "korea": "kr",
    "tw": "tw",
    "taiwan": "tw",
    "cn": "cn",
    "china": "cn",
    "world": "world",
    "oc": "oc",
    "oce": "oc",
    "oceania": "oc",
    "oceanic": "oc",
}


def normalize_region(value: str) -> str:
    token = value.strip().lower()
    compact = re.sub(r"[^a-z0-9]+", "", token)
    if token in REGION_ALIASES:
        return REGION_ALIASES[token]
    if compact in REGION_ALIASES:
        return REGION_ALIASES[compact]
    return token


def profile_region(value: str) -> str:
    """The region a character or guild profile lives in: ``normalize_region``, with Oceania read as ``us``.

    Oceanic realms are in Blizzard's and Raider.IO's US region, and no profile API has an ``oc`` one.
    """
    region = normalize_region(value)
    return "us" if region == "oc" else region


def normalized_text(value: str) -> str:
    parts = [part for part in re.split(r"[^a-z0-9]+", value.strip().lower()) if part]
    return " ".join(parts)


def normalize_name(value: str) -> str:
    return " ".join(value.strip().split())


_APOSTROPHES = r"['\u2019]"


def slug_parts(value: str) -> list[str]:
    """The lowercased words of a realm name, the way Blizzard and Raider.IO slug it.

    Apostrophes join their word (``Mal'Ganis`` -> ``malganis``) and accented or non-Latin letters
    are kept (``Festung der Stürme`` -> ``festung``, ``der``, ``stürme``); any other character
    separates words.
    """
    text = re.sub(_APOSTROPHES, "", value.strip().lower())
    return [part for part in re.split(r"[\W_]+", text) if part]


def realm_slug_variants(value: str) -> list[str]:
    """Every slug spelling of a realm, the upstream one first.

    Then the words run together (``area52``) and the apostrophe read as a word break
    (``mal-ganis``), a spelling users and older commands still type.
    """
    parts = slug_parts(value)
    broken = slug_parts(re.sub(_APOSTROPHES, " ", value))
    return list(dict.fromkeys(slug for slug in ("-".join(parts), "".join(parts), "-".join(broken)) if slug))


def primary_realm_slug(value: str) -> str:
    variants = realm_slug_variants(value)
    return variants[0] if variants else value.strip().lower()

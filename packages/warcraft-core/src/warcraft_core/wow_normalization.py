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


def normalized_text(value: str) -> str:
    parts = [part for part in re.split(r"[^a-z0-9]+", value.strip().lower()) if part]
    return " ".join(parts)


def normalize_name(value: str) -> str:
    return " ".join(value.strip().split())


def slug_parts(value: str) -> list[str]:
    return [part for part in re.split(r"[^a-z0-9]+", value.strip().lower()) if part]


def realm_slug_variants(value: str) -> list[str]:
    parts = slug_parts(value)
    if not parts:
        raw = value.strip().lower()
        return [raw] if raw else []
    candidates = [
        "-".join(parts),
        "".join(parts),
    ]
    seen: set[str] = set()
    normalized: list[str] = []
    for candidate in candidates:
        if candidate and candidate not in seen:
            seen.add(candidate)
            normalized.append(candidate)
    return normalized


def primary_realm_slug(value: str) -> str:
    variants = realm_slug_variants(value)
    return variants[0] if variants else value.strip().lower()

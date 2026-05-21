"""Pinned Warcraft Logs live-matrix inputs (AUR-319).

Rotate identifiers here when retail reports age out of retention; matrix tests
read only from this module.
"""

from __future__ import annotations

# Reports
PUBLIC_REPORT_CODE = "qQVdxDcWyB3wGznL"
PRIVATE_REPORT_CODE = "7Rc3HPCWGYy1z4tT"

# Guild / character
GUILD_REGION = "us"
GUILD_REALM = "malganis"
GUILD_NAME = "gn"
CHARACTER_NAME = "Aurow"

# Zone / encounters (Manaforge Omega)
ZONE_ID = 44
ENCOUNTER_PLEXUS = 3129
ENCOUNTER_IMPERIAL = 3176
ENCOUNTER_CROWN = 3181

PUBLIC_DIFFICULTY = 4  # Heroic
PRIVATE_DIFFICULTY = 5  # Mythic

# Cross-report sampled analytics defaults (keep pages small for points)
SAMPLED_ANALYTICS_TAIL: tuple[str, ...] = (
    "--zone-id",
    str(ZONE_ID),
    "--boss-id",
    str(ENCOUNTER_PLEXUS),
    "--difficulty",
    str(PUBLIC_DIFFICULTY),
    "--top",
    "3",
    "--report-pages",
    "1",
    "--reports-per-page",
    "5",
)

"""Stable identity pins for the Warcraft Logs live matrix (AUR-319).

Only identities that outlive a raid tier live here. Everything volatile — zone, encounter,
difficulty, report codes, fight IDs, ability IDs — is discovered at runtime by
``tests/test_live_command_matrix.py`` from these pins, because retail tiers roll over and reports
age out of Warcraft Logs retention.
"""

from __future__ import annotations

# Guild / character identities used by the profile commands.
GUILD_REGION = "us"
GUILD_REALM = "malganis"
GUILD_NAME = "gn"
CHARACTER_NAME = "Aurow"

# A Warcraft Logs zone is a raid when it exposes the Normal/Heroic/Mythic difficulty triple;
# Mythic+ and Delves zones only expose their own difficulty IDs.
RAID_DIFFICULTY_IDS = frozenset({3, 4, 5})

# Difficulties worth anchoring the matrix on, in preference order (Heroic, then Mythic). Both are
# ranked and widely logged, so encounter rankings exist for whichever one discovery lands on.
ANCHOR_DIFFICULTIES: tuple[int, ...] = (4, 5)

# How many of the zone's most recent reports discovery scans for an anchor kill.
DISCOVERY_REPORT_LIMIT = 10

# Sampled-analytics cohort. The report-list window is centred on the anchor report's start time so
# the cohort provably contains the anchor kill instead of racing the live report firehose.
SAMPLE_REPORT_PAGES = 1
SAMPLE_REPORTS_PER_PAGE = 25
SAMPLE_WINDOW_PADDING_MS = 1000

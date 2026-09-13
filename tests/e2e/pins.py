"""Stable upstream identifiers shared across journeys.

Only truly stable things belong here (long-lived items, realms, well-known addons). Anything that
ages out (report codes, seasons, news slugs, current tier bosses) must be discovered at run time by
the journey that needs it, so the suite never rots on a pin. Keep this file small.
"""

from __future__ import annotations

# Wowhead / Blizzard: Thunderfury has been item 19019 since 2005.
ITEM_ID = 19019
ITEM_NAME = "Thunderfury, Blessed Blade of the Windseeker"
ITEM_SEARCH_QUERY = "thunderfury"
NPC_SEARCH_QUERY = "defias ringleader"
SPELL_SEARCH_QUERY = "fireball"

# Blizzard: a large US realm.
REALM_SLUG = "illidan"
REGION = "us"

# Warcraft Logs / Raider.IO / WowProgress: the maintainer's guild and character (small but stable).
GUILD_REGION = "us"
GUILD_REALM = "malganis"
GUILD_REALM_DISPLAY = "Mal'Ganis"
GUILD_NAME = "gn"
CHARACTER_NAME = "Aurow"

# Guides: a spec that every guide provider covers.
GUIDE_QUERY = "mistweaver monk guide"
GUIDE_CLASS = "monk"
GUIDE_SPEC = "mistweaver"

# CurseForge: Deadly Boss Mods, mod id 3358 (slug search needs an API key with search access).
CURSEFORGE_ADDON_ID = "3358"
CURSEFORGE_ADDON_SLUG = "deadly-boss-mods"

# Warcraft Wiki: canonical API and event pages.
WIKI_API_FUNCTION = "CreateFrame"
WIKI_EVENT = "PLAYER_ENTERING_WORLD"
WIKI_LORE_QUERY = "Argent Dawn"

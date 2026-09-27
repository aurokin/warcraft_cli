from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx
from warcraft_api.cache import CacheSettings, CacheTTLConfig, build_cache_store, load_prefixed_cache_settings_from_env
from warcraft_api.http import DEFAULT_RETRY_ATTEMPTS, build_client, request_with_retries
from warcraft_core.auth import load_provider_auth_state, save_provider_auth_state
from warcraft_core.env import find_env_file, read_env_keys
from warcraft_core.exit_codes import error_code_for_http_status
from warcraft_core.paths import provider_cache_root, provider_env_path
from warcraft_core.wow_normalization import normalize_name, normalize_region, primary_realm_slug

from warcraftlogs_cli.sampling_utils import report_is_finished

DEFAULT_CACHE_DIR = provider_cache_root("warcraftlogs") / "http"
CLIENT_CREDENTIALS_STATE_PROVIDER = "warcraftlogs-client-credentials"

RATE_LIMIT_QUERY = """
query RateLimit {
  rateLimitData {
    limitPerHour
    pointsSpentThisHour
    pointsResetIn
  }
}
"""

CURRENT_USER_QUERY = """
query CurrentUser {
  userData {
    currentUser {
      id
      name
      avatar
    }
  }
}
"""

REGIONS_QUERY = """
query Regions {
  worldData {
    regions {
      id
      compactName
      name
      slug
    }
  }
}
"""

EXPANSIONS_QUERY = """
query Expansions {
  worldData {
    expansions {
      id
      name
      zones {
        id
        name
        frozen
      }
    }
  }
}
"""

SERVER_QUERY = """
query Server($region: String!, $slug: String!) {
  worldData {
    server(region: $region, slug: $slug) {
      id
      name
      normalizedName
      slug
      region {
        id
        compactName
        name
        slug
      }
      subregion {
        id
        name
      }
      connectedRealmID
      seasonID
    }
  }
}
"""

ZONES_QUERY = """
query Zones($expansionId: Int) {
  worldData {
    zones(expansion_id: $expansionId) {
      id
      name
      frozen
      expansion {
        id
        name
      }
      difficulties {
        id
        name
        sizes
      }
      encounters {
        id
        name
        journalID
      }
    }
  }
}
"""

ZONE_QUERY = """
query Zone($id: Int!) {
  worldData {
    zone(id: $id) {
      id
      name
      frozen
      expansion {
        id
        name
      }
      difficulties {
        id
        name
        sizes
      }
      encounters {
        id
        name
        journalID
      }
      partitions {
        id
        name
        compactName
        default
      }
    }
  }
}
"""

ENCOUNTER_QUERY = """
query Encounter($id: Int!) {
  worldData {
    encounter(id: $id) {
      id
      name
      journalID
      zone {
        id
        name
        expansion {
          id
          name
        }
      }
    }
  }
}
"""

_GRAPHQL_ENUM_LITERAL_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
GRAPHQL_WARNINGS_KEY = "__warcraftlogs_warnings__"
_CACHE_MISS = object()


def _prune_null_variables(variables: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(variables, dict):
        return {}
    return {key: value for key, value in variables.items() if value is not None}


def _normalize_graphql_error(error: Any) -> dict[str, Any]:
    if not isinstance(error, dict):
        return {"message": str(error)}
    payload: dict[str, Any] = {}
    message = error.get("message")
    if isinstance(message, str):
        payload["message"] = message
    path = error.get("path")
    if isinstance(path, list):
        payload["path"] = list(path)
    locations = error.get("locations")
    if isinstance(locations, list):
        payload["locations"] = list(locations)
    extensions = error.get("extensions")
    if isinstance(extensions, dict):
        payload["extensions"] = dict(extensions)
    return payload


def _response_has_useful_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, dict):
        return any(_response_has_useful_value(item) for item in value.values())
    if isinstance(value, list):
        if not value:
            return True
        return any(_response_has_useful_value(item) for item in value)
    return True


def _encounter_rankings_request(*, encounter_id: int, options: EncounterRankingsOptions) -> tuple[str, dict[str, Any]]:
    variables: dict[str, Any] = {"id": encounter_id}
    optional_variable_lines: list[str] = []
    optional_argument_lines: list[str] = []

    for name, graphql_type, value in (
        ("bracket", "Int", options.bracket),
        ("difficulty", "Int", options.difficulty),
        ("filter", "String", options.filter),
        ("page", "Int", options.page),
        ("partition", "Int", options.partition),
        ("serverRegion", "String", options.server_region),
        ("serverSlug", "String", options.server_slug),
        ("size", "Int", options.size),
        ("hardModeLevel", "HardModeLevelRankFilter", options.hard_mode_level),
        ("metric", "CharacterRankingMetricType", options.metric),
        ("includeCombatantInfo", "Boolean", options.include_combatant_info),
        ("includeOtherPlayers", "Boolean", options.include_other_players),
        ("className", "String", options.class_name),
        ("specName", "String", options.spec_name),
    ):
        if value is None:
            continue
        optional_variable_lines.append(f"  ${name}: {graphql_type}")
        optional_argument_lines.append(f"        {name}: ${name}")
        variables[name] = value

    if options.leaderboard is not None:
        if not _GRAPHQL_ENUM_LITERAL_RE.fullmatch(options.leaderboard):
            raise ValueError(f"Invalid GraphQL enum literal for leaderboard: {options.leaderboard!r}")
        optional_argument_lines.append(f"        leaderboard: {options.leaderboard}")

    variable_block = ""
    if optional_variable_lines:
        variable_block = "\n" + "\n".join(optional_variable_lines)

    if optional_argument_lines:
        character_rankings_field = "      characterRankings(\n" + "\n".join(optional_argument_lines) + "\n      )"
    else:
        character_rankings_field = "      characterRankings"

    query = f"""
query EncounterRankings(
  $id: Int!{variable_block}
) {{
  worldData {{
    encounter(id: $id) {{
      id
      name
      journalID
      zone {{
        id
        name
        expansion {{
          id
          name
        }}
      }}
{character_rankings_field}
    }}
  }}
}}
"""
    return query, variables


GUILD_QUERY = """
query Guild($name: String!, $serverSlug: String!, $serverRegion: String!, $zoneId: Int) {
  guildData {
    guild(name: $name, serverSlug: $serverSlug, serverRegion: $serverRegion) {
      id
      name
      description
      competitionMode
      stealthMode
      tags {
        id
        name
      }
      faction {
        id
        name
      }
      server {
        id
        name
        normalizedName
        slug
        region {
          id
          compactName
          name
          slug
        }
        subregion {
          id
          name
        }
      }
      zoneRanking(zoneId: $zoneId) {
        progress {
          worldRank {
            number
            color
          }
          regionRank {
            number
            color
          }
          serverRank {
            number
            color
          }
        }
      }
    }
  }
}
"""

GUILD_RANKINGS_QUERY = """
query GuildRankings($name: String!, $serverSlug: String!, $serverRegion: String!, $zoneId: Int, $size: Int, $difficulty: Int) {
  guildData {
    guild(name: $name, serverSlug: $serverSlug, serverRegion: $serverRegion) {
      id
      name
      server {
        id
        name
        normalizedName
        slug
        region {
          id
          compactName
          name
          slug
        }
        subregion {
          id
          name
        }
      }
      zoneRanking(zoneId: $zoneId) {
        progress(size: $size) {
          worldRank {
            number
            color
          }
          regionRank {
            number
            color
          }
          serverRank {
            number
            color
          }
        }
        speed(size: $size, difficulty: $difficulty) {
          worldRank {
            number
            color
          }
          regionRank {
            number
            color
          }
          serverRank {
            number
            color
          }
        }
        completeRaidSpeed(size: $size, difficulty: $difficulty) {
          worldRank {
            number
            color
          }
          regionRank {
            number
            color
          }
          serverRank {
            number
            color
          }
        }
      }
    }
  }
}
"""

GUILD_MEMBERS_QUERY = """
query GuildMembers($name: String!, $serverSlug: String!, $serverRegion: String!, $limit: Int, $page: Int) {
  guildData {
    guild(name: $name, serverSlug: $serverSlug, serverRegion: $serverRegion) {
      id
      name
      server {
        id
        name
        normalizedName
        slug
        region {
          id
          compactName
          name
          slug
        }
        subregion {
          id
          name
        }
      }
      members(limit: $limit, page: $page) {
        data {
          id
          canonicalID
          name
          level
          classID
          hidden
          guildRank
          faction {
            id
            name
          }
          server {
            id
            name
            normalizedName
            slug
            region {
              id
              compactName
              name
              slug
            }
            subregion {
              id
              name
            }
            connectedRealmID
            seasonID
          }
        }
        total
        per_page
        current_page
        from
        to
        last_page
        has_more_pages
      }
    }
  }
}
"""

GUILD_ATTENDANCE_QUERY = """
query GuildAttendance(
  $name: String!,
  $serverSlug: String!,
  $serverRegion: String!,
  $guildTagID: Int,
  $limit: Int,
  $page: Int,
  $zoneID: Int,
) {
  guildData {
    guild(name: $name, serverSlug: $serverSlug, serverRegion: $serverRegion) {
      id
      name
      server {
        id
        name
        normalizedName
        slug
        region {
          id
          compactName
          name
          slug
        }
        subregion {
          id
          name
        }
      }
      attendance(guildTagID: $guildTagID, limit: $limit, page: $page, zoneID: $zoneID) {
        data {
          code
          startTime
          zone {
            id
            name
          }
          players {
            name
            type
            presence
          }
        }
        total
        per_page
        current_page
        from
        to
        last_page
        has_more_pages
      }
    }
  }
}
"""

CHARACTER_QUERY = """
query Character($name: String!, $serverSlug: String!, $serverRegion: String!) {
  characterData {
    character(name: $name, serverSlug: $serverSlug, serverRegion: $serverRegion) {
      id
      canonicalID
      name
      level
      classID
      hidden
      faction {
        id
        name
      }
      guildRank
      server {
        id
        name
        normalizedName
        slug
        region {
          id
          compactName
          name
          slug
        }
        subregion {
          id
          name
        }
        connectedRealmID
        seasonID
      }
      guilds {
        id
        name
        server {
          id
          name
          normalizedName
          slug
          region {
            id
            compactName
            name
            slug
          }
          subregion {
            id
            name
          }
          connectedRealmID
          seasonID
        }
      }
    }
  }
}
"""

CHARACTER_RANKINGS_QUERY = """
query CharacterRankings(
  $name: String!,
  $serverSlug: String!,
  $serverRegion: String!,
  $zoneID: Int,
  $difficulty: Int,
  $metric: CharacterPageRankingMetricType,
  $size: Int,
  $specName: String
) {
  characterData {
    character(name: $name, serverSlug: $serverSlug, serverRegion: $serverRegion) {
      id
      canonicalID
      name
      classID
      level
      faction {
        id
        name
      }
      server {
        id
        name
        normalizedName
        slug
        region {
          id
          compactName
          name
          slug
        }
        subregion {
          id
          name
        }
      }
      zoneRankings(zoneID: $zoneID, difficulty: $difficulty, metric: $metric, size: $size, specName: $specName)
    }
  }
}
"""

REPORT_QUERY = """
query Report($code: String!, $allowUnlisted: Boolean) {
  reportData {
    report(code: $code, allowUnlisted: $allowUnlisted) {
      code
      title
      startTime
      endTime
      visibility
      archiveStatus {
        isArchived
        isAccessible
        archiveDate
      }
      segments
      exportedSegments
      zone {
        id
        name
      }
      guild {
        id
        name
        server {
          slug
          region {
            slug
          }
        }
      }
    }
  }
}
"""

REPORTS_QUERY = """
query Reports(
  $guildName: String,
  $guildServerSlug: String,
  $guildServerRegion: String,
  $limit: Int,
  $page: Int,
  $startTime: Float,
  $endTime: Float,
  $zoneID: Int,
  $gameZoneID: Int
) {
  reportData {
    reports(
      guildName: $guildName,
      guildServerSlug: $guildServerSlug,
      guildServerRegion: $guildServerRegion,
      limit: $limit,
      page: $page,
      startTime: $startTime,
      endTime: $endTime,
      zoneID: $zoneID,
      gameZoneID: $gameZoneID
    ) {
      data {
        code
        title
        startTime
        endTime
        visibility
        archiveStatus {
          isArchived
          isAccessible
          archiveDate
        }
        segments
        exportedSegments
        zone {
          id
          name
        }
        guild {
          id
          name
          server {
            id
            name
            normalizedName
            slug
            region {
              id
              compactName
              name
              slug
            }
            subregion {
              id
              name
            }
          }
        }
      }
      total
      per_page
      current_page
      from
      to
      last_page
      has_more_pages
    }
  }
}
"""

REPORT_FIGHTS_QUERY = """
query ReportFights($code: String!, $difficulty: Int, $allowUnlisted: Boolean) {
  reportData {
    report(code: $code, allowUnlisted: $allowUnlisted) {
      code
      title
      endTime
      zone {
        id
        name
      }
      fights(difficulty: $difficulty) {
        id
        name
        encounterID
        difficulty
        kill
        completeRaid
        startTime
        endTime
        fightPercentage
        bossPercentage
        averageItemLevel
        size
      }
    }
  }
}
"""

REPORT_EVENTS_QUERY = """
query ReportEvents(
  $code: String!,
  $allowUnlisted: Boolean,
  $abilityID: Float,
  $dataType: EventDataType,
  $difficulty: Int,
  $encounterID: Int,
  $endTime: Float,
  $fightIDs: [Int],
  $filterExpression: String,
  $hostilityType: HostilityType,
  $killType: KillType,
  $limit: Int,
  $sourceID: Int,
  $startTime: Float,
  $targetID: Int,
  $translate: Boolean
) {
  reportData {
    report(code: $code, allowUnlisted: $allowUnlisted) {
      code
      title
      endTime
      zone {
        id
        name
      }
      events(
        abilityID: $abilityID,
        dataType: $dataType,
        difficulty: $difficulty,
        encounterID: $encounterID,
        endTime: $endTime,
        fightIDs: $fightIDs,
        filterExpression: $filterExpression,
        hostilityType: $hostilityType,
        killType: $killType,
        limit: $limit,
        sourceID: $sourceID,
        startTime: $startTime,
        targetID: $targetID,
        translate: $translate
      ) {
        data
        nextPageTimestamp
      }
    }
  }
}
"""

REPORT_TABLE_QUERY = """
query ReportTable(
  $code: String!,
  $allowUnlisted: Boolean,
  $abilityID: Float,
  $dataType: TableDataType,
  $difficulty: Int,
  $encounterID: Int,
  $endTime: Float,
  $fightIDs: [Int],
  $filterExpression: String,
  $hostilityType: HostilityType,
  $killType: KillType,
  $sourceID: Int,
  $startTime: Float,
  $targetID: Int,
  $translate: Boolean,
  $viewBy: ViewType,
  $wipeCutoff: Int
) {
  reportData {
    report(code: $code, allowUnlisted: $allowUnlisted) {
      code
      title
      endTime
      zone {
        id
        name
      }
      table(
        abilityID: $abilityID,
        dataType: $dataType,
        difficulty: $difficulty,
        encounterID: $encounterID,
        endTime: $endTime,
        fightIDs: $fightIDs,
        filterExpression: $filterExpression,
        hostilityType: $hostilityType,
        killType: $killType,
        sourceID: $sourceID,
        startTime: $startTime,
        targetID: $targetID,
        translate: $translate,
        viewBy: $viewBy,
        wipeCutoff: $wipeCutoff
      )
    }
  }
}
"""

REPORT_GRAPH_QUERY = """
query ReportGraph(
  $code: String!,
  $allowUnlisted: Boolean,
  $abilityID: Float,
  $dataType: GraphDataType,
  $difficulty: Int,
  $encounterID: Int,
  $endTime: Float,
  $fightIDs: [Int],
  $filterExpression: String,
  $hostilityType: HostilityType,
  $killType: KillType,
  $sourceID: Int,
  $startTime: Float,
  $targetID: Int,
  $translate: Boolean,
  $viewBy: ViewType,
  $wipeCutoff: Int
) {
  reportData {
    report(code: $code, allowUnlisted: $allowUnlisted) {
      code
      title
      endTime
      zone {
        id
        name
      }
      graph(
        abilityID: $abilityID,
        dataType: $dataType,
        difficulty: $difficulty,
        encounterID: $encounterID,
        endTime: $endTime,
        fightIDs: $fightIDs,
        filterExpression: $filterExpression,
        hostilityType: $hostilityType,
        killType: $killType,
        sourceID: $sourceID,
        startTime: $startTime,
        targetID: $targetID,
        translate: $translate,
        viewBy: $viewBy,
        wipeCutoff: $wipeCutoff
      )
    }
  }
}
"""

REPORT_MASTER_DATA_QUERY = """
query ReportMasterData(
  $code: String!,
  $allowUnlisted: Boolean,
  $translate: Boolean,
  $actorType: String,
  $actorSubType: String
) {
  reportData {
    report(code: $code, allowUnlisted: $allowUnlisted) {
      code
      title
      endTime
      zone {
        id
        name
      }
      masterData(translate: $translate) {
        logVersion
        gameVersion
        lang
        abilities {
          gameID
          icon
          name
          type
        }
        actors(type: $actorType, subType: $actorSubType) {
          gameID
          icon
          id
          name
          petOwner
          server
          subType
          type
        }
      }
    }
  }
}
"""

REPORT_PLAYER_DETAILS_QUERY = """
query ReportPlayerDetails(
  $code: String!,
  $allowUnlisted: Boolean,
  $difficulty: Int,
  $encounterID: Int,
  $endTime: Float,
  $fightIDs: [Int],
  $killType: KillType,
  $startTime: Float,
  $translate: Boolean,
  $includeCombatantInfo: Boolean
) {
  reportData {
    report(code: $code, allowUnlisted: $allowUnlisted) {
      code
      title
      endTime
      zone {
        id
        name
      }
      playerDetails(
        difficulty: $difficulty,
        encounterID: $encounterID,
        endTime: $endTime,
        fightIDs: $fightIDs,
        killType: $killType,
        startTime: $startTime,
        translate: $translate,
        includeCombatantInfo: $includeCombatantInfo
      )
    }
  }
}
"""

REPORT_RANKINGS_QUERY = """
query ReportRankings(
  $code: String!,
  $allowUnlisted: Boolean,
  $compare: RankingCompareType,
  $difficulty: Int,
  $encounterID: Int,
  $fightIDs: [Int],
  $playerMetric: ReportRankingMetricType,
  $timeframe: RankingTimeframeType
) {
  reportData {
    report(code: $code, allowUnlisted: $allowUnlisted) {
      code
      title
      endTime
      zone {
        id
        name
      }
      rankings(
        compare: $compare,
        difficulty: $difficulty,
        encounterID: $encounterID,
        fightIDs: $fightIDs,
        playerMetric: $playerMetric,
        timeframe: $timeframe
      )
    }
  }
}
"""


@dataclass(frozen=True, slots=True)
class WarcraftLogsSiteProfile:
    key: str
    label: str
    root_url: str
    oauth_authorize_url: str
    oauth_token_url: str
    api_url: str
    user_api_url: str


RETAIL_PROFILE = WarcraftLogsSiteProfile(
    key="retail",
    label="Retail / Main",
    root_url="https://www.warcraftlogs.com",
    oauth_authorize_url="https://www.warcraftlogs.com/oauth/authorize",
    oauth_token_url="https://www.warcraftlogs.com/oauth/token",  # noqa: S106 - OAuth endpoint URL, not a credential
    api_url="https://www.warcraftlogs.com/api/v2/client",
    user_api_url="https://www.warcraftlogs.com/api/v2/user",
)

CLASSIC_PROFILE = WarcraftLogsSiteProfile(
    key="classic",
    label="Classic",
    root_url="https://classic.warcraftlogs.com",
    oauth_authorize_url="https://classic.warcraftlogs.com/oauth/authorize",
    oauth_token_url="https://classic.warcraftlogs.com/oauth/token",  # noqa: S106 - OAuth endpoint URL, not a credential
    api_url="https://classic.warcraftlogs.com/api/v2/client",
    user_api_url="https://classic.warcraftlogs.com/api/v2/user",
)

FRESH_PROFILE = WarcraftLogsSiteProfile(
    key="fresh",
    label="Classic Fresh / Anniversary",
    root_url="https://fresh.warcraftlogs.com",
    oauth_authorize_url="https://fresh.warcraftlogs.com/oauth/authorize",
    oauth_token_url="https://fresh.warcraftlogs.com/oauth/token",  # noqa: S106 - OAuth endpoint URL, not a credential
    api_url="https://fresh.warcraftlogs.com/api/v2/client",
    user_api_url="https://fresh.warcraftlogs.com/api/v2/user",
)

_SITE_PROFILES: tuple[WarcraftLogsSiteProfile, ...] = (RETAIL_PROFILE, CLASSIC_PROFILE, FRESH_PROFILE)
_SITE_PROFILES_BY_KEY = {profile.key: profile for profile in _SITE_PROFILES}
_SITE_PROFILE_ALIASES = {
    "main": "retail",
    "www": "retail",
    "live": "retail",
    "classic-era": "classic",
    "classic-fresh": "fresh",
    "classicfresh": "fresh",
    "anniversary": "fresh",
    "classic-anniversary": "fresh",
}


def normalize_site_profile_key(value: str) -> str:
    return value.strip().lower().replace("_", "-")


def resolve_site_profile(value: str | None) -> WarcraftLogsSiteProfile:
    if value is None or value.strip() == "":
        return RETAIL_PROFILE
    normalized = normalize_site_profile_key(value)
    key = _SITE_PROFILE_ALIASES.get(normalized, normalized)
    profile = _SITE_PROFILES_BY_KEY.get(key)
    if profile is None:
        options = ", ".join(profile.key for profile in _SITE_PROFILES)
        raise ValueError(f"Unknown Warcraft Logs site profile {value!r}. Supported: {options}")
    return profile


def saved_user_token_site_key(payload: dict[str, Any]) -> str:
    raw_site = payload.get("site_profile")
    if not isinstance(raw_site, str) or not raw_site.strip():
        return RETAIL_PROFILE.key
    try:
        return resolve_site_profile(raw_site).key
    except ValueError:
        return normalize_site_profile_key(raw_site)


def _token_fingerprint(token: str) -> str:
    """Stable non-secret account scope for cache keys: a truncated digest of an access token.

    The digest is one-way and never emitted; it only keeps one account's cached user-endpoint
    responses out of another account's reach.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class WarcraftLogsAuthConfig:
    client_id: str | None
    client_secret: str | None
    env_file: str | None

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)


def warcraftlogs_provider_env_path() -> str:
    return str(provider_env_path("warcraftlogs"))


CLIENT_ID_ENV = "WARCRAFTLOGS_CLIENT_ID"
CLIENT_SECRET_ENV = "WARCRAFTLOGS_CLIENT_SECRET"  # noqa: S105 - env var name, not a credential
MANAGED_ENV_KEYS = (CLIENT_ID_ENV, CLIENT_SECRET_ENV)


def _first_graphql_error_message(errors: list[Any], operation_name: str | None) -> str:
    first = errors[0] if errors else None
    message = first.get("message") if isinstance(first, dict) else None
    return str(message) if message else f"Warcraft Logs returned GraphQL errors for {operation_name}."


def _graphql_error_code(message: str) -> str:
    """Classify a GraphQL error by its message so unknown ids and permission denials get contract codes.

    Warcraft Logs answers both cases with ``report: null`` plus an error, so the payload shape alone
    cannot tell "does not exist" (exit 4) from "no permission" (exit 3).
    """
    lowered = message.lower()
    if "does not exist" in lowered or "not found" in lowered or "no such" in lowered:
        return "not_found"
    if "permission" in lowered or "not authorized" in lowered or "unauthorized" in lowered:
        return "auth_failed"
    return "graphql_error"


class WarcraftLogsClientError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _request(client: httpx.Client, url: str, **kwargs: Any) -> httpx.Response:
    """``request_with_retries`` with transport and status failures mapped to the shared error vocabulary.

    Every Warcraft Logs HTTP call goes through here so the CLI layer only ever sees
    ``WarcraftLogsClientError`` and can pick the contract exit code from the error code.
    """
    try:
        return request_with_retries(client, url, **kwargs)
    except httpx.TimeoutException as exc:
        raise WarcraftLogsClientError("timeout", f"Warcraft Logs request to {url} timed out: {exc}") from exc
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        code = error_code_for_http_status(status)
        raise WarcraftLogsClientError(code, f"Warcraft Logs returned HTTP {status} for {url}.") from exc
    except httpx.RequestError as exc:
        raise WarcraftLogsClientError("network_error", f"Warcraft Logs request to {url} failed: {type(exc).__name__}: {exc}") from exc


def _response_json(response: httpx.Response) -> Any:
    """The body of a Warcraft Logs response; a body that is not JSON is a classified upstream failure."""
    try:
        return response.json()
    except ValueError as exc:
        raise WarcraftLogsClientError("invalid_response", f"Warcraft Logs returned a body that is not JSON: {exc}") from exc


@dataclass(frozen=True, slots=True)
class ReportFilterOptions:
    ability_id: float | None = None
    data_type: str | None = None
    difficulty: int | None = None
    encounter_id: int | None = None
    end_time: float | None = None
    fight_ids: list[int] | None = None
    filter_expression: str | None = None
    hostility_type: str | None = None
    kill_type: str | None = None
    limit: int | None = None
    source_id: int | None = None
    start_time: float | None = None
    target_id: int | None = None
    translate: bool | None = None
    view_by: str | None = None
    wipe_cutoff: int | None = None


@dataclass(frozen=True, slots=True)
class ReportPlayerDetailsOptions:
    difficulty: int | None = None
    encounter_id: int | None = None
    end_time: float | None = None
    fight_ids: list[int] | None = None
    include_combatant_info: bool | None = None
    kill_type: str | None = None
    start_time: float | None = None
    translate: bool | None = None


@dataclass(frozen=True, slots=True)
class ReportRankingsOptions:
    compare: str | None = None
    difficulty: int | None = None
    encounter_id: int | None = None
    fight_ids: list[int] | None = None
    player_metric: str | None = None
    timeframe: str | None = None


@dataclass(frozen=True, slots=True)
class EncounterRankingsOptions:
    bracket: int | None = None
    difficulty: int | None = None
    filter: str | None = None
    page: int | None = None
    partition: int | None = None
    server_region: str | None = None
    server_slug: str | None = None
    size: int | None = None
    leaderboard: str | None = None
    hard_mode_level: str | None = None
    metric: str | None = None
    include_combatant_info: bool | None = None
    include_other_players: bool | None = None
    class_name: str | None = None
    spec_name: str | None = None


def load_warcraftlogs_auth_config(*, start_dir: str | None = None) -> WarcraftLogsAuthConfig:
    """Resolve the client credentials from .env.local, then the provider env file, then the process environment.

    The first layer with both ``WARCRAFTLOGS_CLIENT_ID`` and ``WARCRAFTLOGS_CLIENT_SECRET`` wins.

    Every layer is a pure read: ``os.environ`` is never mutated, so a concurrent command in the same
    process cannot observe (or inherit) credentials this call discovered.
    """
    layers: list[tuple[str | None, dict[str, str]]] = []
    local_path = find_env_file(start_dir=start_dir)
    if local_path is not None:
        layers.append((str(local_path), read_env_keys(local_path, MANAGED_ENV_KEYS)))
    provider_path = warcraftlogs_provider_env_path()
    layers.append((provider_path, read_env_keys(provider_path, MANAGED_ENV_KEYS)))
    layers.append((None, {key: os.environ[key] for key in MANAGED_ENV_KEYS if os.environ.get(key)}))

    def pair(values: dict[str, str]) -> tuple[str | None, str | None]:
        client_id = (values.get(CLIENT_ID_ENV) or "").strip() or None
        client_secret = (values.get(CLIENT_SECRET_ENV) or "").strip() or None
        return client_id, client_secret

    # A client ID and secret belong to one OAuth client, so the first layer holding a complete
    # pair wins; halves from different layers are never combined.
    for source, values in layers:
        client_id, client_secret = pair(values)
        if client_id and client_secret:
            return WarcraftLogsAuthConfig(client_id=client_id, client_secret=client_secret, env_file=source)
    # No layer is complete: report the highest-precedence layer's half so doctor can say what is
    # missing, without stitching it to a half from another layer.
    for _, values in layers:
        client_id, client_secret = pair(values)
        if client_id or client_secret:
            return WarcraftLogsAuthConfig(client_id=client_id, client_secret=client_secret, env_file=None)
    return WarcraftLogsAuthConfig(client_id=None, client_secret=None, env_file=None)


def load_warcraftlogs_cache_settings_from_env() -> tuple[CacheSettings, int, int, int, int]:
    """Cache settings plus the guild, static, report-listing and finished-report TTLs, in that order."""
    settings = load_prefixed_cache_settings_from_env(
        env_prefix="WARCRAFTLOGS",
        default_cache_dir=DEFAULT_CACHE_DIR,
        default_redis_prefix="warcraftlogs_cli",
        ttl_defaults=CacheTTLConfig(
            entity_page_html=300,
            guide_page_html=21600,
            page_html=60,
            report_finished=86400,
        ),
        ttl_env_overrides={
            "entity_page_html": "WARCRAFTLOGS_GUILD_CACHE_TTL_SECONDS",
            "guide_page_html": "WARCRAFTLOGS_STATIC_CACHE_TTL_SECONDS",
            "page_html": "WARCRAFTLOGS_REPORT_CACHE_TTL_SECONDS",
            "report_finished": "WARCRAFTLOGS_FINISHED_REPORT_CACHE_TTL_SECONDS",
        },
    )
    return (
        settings,
        settings.ttls.entity_page_html,
        settings.ttls.guide_page_html,
        settings.ttls.page_html,
        settings.ttls.report_finished,
    )


class WarcraftLogsClient:
    def __init__(
        self,
        *,
        site: WarcraftLogsSiteProfile = RETAIL_PROFILE,
        timeout_seconds: float = 20.0,
        retry_attempts: int = DEFAULT_RETRY_ATTEMPTS,
    ) -> None:
        auth = load_warcraftlogs_auth_config()
        self._site = site
        self._http_client: httpx.Client | None = None
        self._timeout_seconds = timeout_seconds
        self._retry_attempts = max(1, retry_attempts)
        self._client_id = auth.client_id or ""
        self._client_secret = auth.client_secret or ""
        self._credential_hint = auth.env_file or str(find_env_file() or ".env.local")
        self._access_token: str | None = None
        self._token_expires_at = 0.0
        settings, guild_ttl, static_ttl, report_ttl, finished_report_ttl = load_warcraftlogs_cache_settings_from_env()
        self._cache_store = build_cache_store(settings) if settings.enabled else None
        self._guild_ttl = guild_ttl
        self._static_ttl = static_ttl
        self._report_ttl = report_ttl
        self._finished_report_ttl = finished_report_ttl
        self._graphql_warnings: list[dict[str, Any]] = []
        # Transport tally, reported by sampled payloads so a caller can tell a live scan from one
        # replayed out of the cache.
        self._cache_hit_count = 0
        self._upstream_request_count = 0

    def close(self) -> None:
        if self._http_client is not None:
            self._http_client.close()
            self._http_client = None

    def __enter__(self) -> WarcraftLogsClient:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def _client(self) -> httpx.Client:
        if self._http_client is None:
            self._http_client = build_client(timeout=self._timeout_seconds)
        return self._http_client

    def _post_graphql(self, url: str, *, token: str, body: dict[str, Any]) -> httpx.Response:
        """One GraphQL POST, counted so callers can report how much of a payload came from upstream."""
        self._upstream_request_count += 1
        return _request(
            self._client(),
            url,
            method="POST",
            retry_attempts=self._retry_attempts,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json=body,
        )

    @property
    def transport_counts(self) -> dict[str, int]:
        """Cache hits and upstream GraphQL calls served so far by this client instance."""
        return {"cache_hit_count": self._cache_hit_count, "upstream_request_count": self._upstream_request_count}

    def _cache_key(self, namespace: str, payload: dict[str, Any]) -> str:
        raw = json.dumps({"site": self._site.key, "namespace": namespace, "payload": payload}, sort_keys=True, separators=(",", ":"))
        return f"{namespace}:{hashlib.sha256(raw.encode('utf-8')).hexdigest()}"

    def _read_cache(self, key: str) -> Any | None:
        if self._cache_store is None:
            return None
        cached = self._cache_store.get(key)
        if cached is not None:
            self._cache_hit_count += 1
        self._record_warnings(cached)
        return cached

    def _read_raw_cache(self, key: str) -> Any:
        if self._cache_store is None:
            return _CACHE_MISS
        cached = self._cache_store.get(key)
        if cached is None:
            return _CACHE_MISS
        self._cache_hit_count += 1
        self._record_warnings(cached)
        return cached

    def _record_warnings(self, payload: Any) -> None:
        """Add a response's partial-error warnings to the tally the command reports at emit time."""
        warnings = payload.get(GRAPHQL_WARNINGS_KEY) if isinstance(payload, dict) else None
        if isinstance(warnings, list):
            self._graphql_warnings.extend(warnings)

    def _write_cache(self, key: str, payload: Any, *, ttl_seconds: int) -> None:
        # A partial-error response is a transient upstream failure: caching it (for 24h on a finished
        # report) would keep serving the gap after Warcraft Logs recovers.
        if self._cache_store is None or (isinstance(payload, dict) and GRAPHQL_WARNINGS_KEY in payload):
            return
        self._cache_store.set(key, payload, ttl_seconds=ttl_seconds)

    def _has_client_credentials(self) -> bool:
        return bool(self._client_id and self._client_secret)

    def _require_client_credentials(self) -> None:
        if self._has_client_credentials():
            return
        raise WarcraftLogsClientError(
            "missing_client_credentials",
            "Missing Warcraft Logs credentials. Set WARCRAFTLOGS_CLIENT_ID and WARCRAFTLOGS_CLIENT_SECRET "
            f"(for example in {self._credential_hint}).",
        )

    def _client_credentials_cache_key(self) -> str:
        raw = f"{self._site.key}\0{self._client_id}\0{self._client_secret}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _load_shared_client_token(self, *, now: float) -> str | None:
        try:
            payload = load_provider_auth_state(CLIENT_CREDENTIALS_STATE_PROVIDER)
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        if payload.get("auth_mode") != "client_credentials":
            return None
        if payload.get("credential_key") != self._client_credentials_cache_key():
            return None
        token = payload.get("access_token")
        expires_at = payload.get("expires_at")
        if not isinstance(token, str) or not token.strip():
            return None
        if not isinstance(expires_at, (int, float)):
            return None
        if now >= float(expires_at) - 60:
            return None
        self._access_token = token
        self._token_expires_at = float(expires_at)
        return token

    def _save_shared_client_token(self, *, token: str, expires_at: float) -> None:
        try:
            save_provider_auth_state(
                CLIENT_CREDENTIALS_STATE_PROVIDER,
                {
                    "access_token": token,
                    "auth_mode": "client_credentials",
                    "credential_key": self._client_credentials_cache_key(),
                    "expires_at": expires_at,
                    "site": self._site.key,
                    "token_type": "Bearer",
                },
            )
        except OSError:
            # Shared token caching is an optimization. Public commands should
            # still work even if the local state directory is unavailable.
            return

    def _token(self) -> str:
        now = time.time()
        if self._access_token and now < self._token_expires_at - 60:
            return self._access_token
        if not self._has_client_credentials():
            raise WarcraftLogsClientError(
                "missing_public_auth",
                "Public Warcraft Logs commands need WARCRAFTLOGS_CLIENT_ID and WARCRAFTLOGS_CLIENT_SECRET. "
                f"Set them (for example in {self._credential_hint}).",
            )
        shared_token = self._load_shared_client_token(now=now)
        if shared_token is not None:
            return shared_token
        response = _request(
            self._client(),
            self._site.oauth_token_url,
            method="POST",
            data={"grant_type": "client_credentials"},
            auth=(self._client_id, self._client_secret),
            retry_attempts=self._retry_attempts,
        )
        payload = _response_json(response)
        token = payload.get("access_token")
        expires_in = payload.get("expires_in", 3600)
        if not isinstance(token, str) or not token:
            raise WarcraftLogsClientError("auth_failed", "Warcraft Logs token response did not include an access token.")
        self._access_token = token
        self._token_expires_at = now + int(expires_in)
        self._save_shared_client_token(token=token, expires_at=self._token_expires_at)
        return token

    def _user_token(self) -> str:
        payload = load_provider_auth_state("warcraftlogs")
        if not isinstance(payload, dict):
            raise WarcraftLogsClientError("missing_user_auth", "No Warcraft Logs user token is saved locally.")
        auth_mode = payload.get("auth_mode")
        token = payload.get("access_token")
        expires_at = payload.get("expires_at")
        if auth_mode not in {"authorization_code", "pkce"}:
            raise WarcraftLogsClientError("missing_user_auth", "Saved Warcraft Logs auth state is not a user-auth token.")
        if not isinstance(token, str) or not token.strip():
            raise WarcraftLogsClientError("missing_user_auth", "Saved Warcraft Logs auth state does not include an access token.")
        if isinstance(expires_at, (int, float)) and time.time() >= float(expires_at):
            raise WarcraftLogsClientError("user_token_expired", "Saved Warcraft Logs user token has expired. Re-run the auth flow.")
        token_site = saved_user_token_site_key(payload)
        selected_site = getattr(self, "_site", RETAIL_PROFILE)
        if token_site != selected_site.key:
            raise WarcraftLogsClientError(
                "site_profile_mismatch",
                (
                    f"Saved Warcraft Logs user token is for site profile {token_site!r}, not {selected_site.key!r}. "
                    f"Re-run `warcraftlogs --site {selected_site.key} auth login` for user-endpoint requests on this site."
                ),
            )
        return token

    def _has_user_token(self) -> bool:
        payload = load_provider_auth_state("warcraftlogs")
        if not isinstance(payload, dict):
            return False
        if payload.get("auth_mode") not in {"authorization_code", "pkce"}:
            return False
        token = payload.get("access_token")
        if not isinstance(token, str) or not token.strip():
            return False
        expires_at = payload.get("expires_at")
        if isinstance(expires_at, (int, float)) and time.time() >= float(expires_at):
            return False
        return saved_user_token_site_key(payload) == getattr(self, "_site", RETAIL_PROFILE).key

    @property
    def site(self) -> WarcraftLogsSiteProfile:
        return self._site

    @property
    def graphql_warnings(self) -> list[dict[str, Any]]:
        """Every partial-error warning this client has seen, in request order. One client serves one command."""
        return list(self._graphql_warnings)

    @property
    def client_id(self) -> str:
        return self._client_id

    def authorization_code_url(self, *, redirect_uri: str, state: str) -> str:
        self._require_client_credentials()
        params = {
            "client_id": self._client_id,
            "state": state,
            "redirect_uri": redirect_uri,
            "response_type": "code",
        }
        return f"{self._site.oauth_authorize_url}?{urlencode(params)}"

    def pkce_code_url(self, *, redirect_uri: str, state: str, code_challenge: str) -> str:
        self._require_client_credentials()
        params = {
            "client_id": self._client_id,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "state": state,
            "redirect_uri": redirect_uri,
            "response_type": "code",
        }
        return f"{self._site.oauth_authorize_url}?{urlencode(params)}"

    def exchange_authorization_code(self, *, code: str, redirect_uri: str) -> dict[str, Any]:
        self._require_client_credentials()
        response = _request(
            self._client(),
            self._site.oauth_token_url,
            method="POST",
            data={
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
                "code": code,
            },
            auth=(self._client_id, self._client_secret),
            retry_attempts=self._retry_attempts,
        )
        payload = _response_json(response)
        if not isinstance(payload, dict):
            raise WarcraftLogsClientError("auth_failed", "Warcraft Logs returned an invalid authorization-code token response.")
        return payload

    def exchange_pkce_code(self, *, code: str, redirect_uri: str, code_verifier: str) -> dict[str, Any]:
        self._require_client_credentials()
        response = _request(
            self._client(),
            self._site.oauth_token_url,
            method="POST",
            data={
                "client_id": self._client_id,
                "code_verifier": code_verifier,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
                "code": code,
            },
            auth=(self._client_id, self._client_secret),
            retry_attempts=self._retry_attempts,
        )
        payload = _response_json(response)
        if not isinstance(payload, dict):
            raise WarcraftLogsClientError("auth_failed", "Warcraft Logs returned an invalid PKCE token response.")
        return payload

    def _graphql_user(
        self,
        *,
        operation_name: str | None,
        query: str,
        variables: dict[str, Any] | None,
        namespace: str,
        ttl_seconds: int,
        use_cache: bool = True,
        ttl_resolver: Callable[[dict[str, Any]], int] | None = None,
    ) -> dict[str, Any]:
        request_variables = _prune_null_variables(variables)
        token = self._user_token()
        # User-endpoint responses are account-scoped (private reports, saved identity), so the
        # cache entry must belong to the token that fetched it and never to "whoever is logged in".
        cache_payload = {
            "endpoint": "user",
            "account": _token_fingerprint(token),
            "operation_name": operation_name,
            "query": query,
            "variables": request_variables,
        }
        cache_key = self._cache_key(namespace, cache_payload)
        if use_cache:
            cached = self._read_cache(cache_key)
            if isinstance(cached, dict):
                return cached
        data = self._uncached_graphql_user(
            operation_name=operation_name,
            query=query,
            variables=request_variables,
            token=token,
        )
        if use_cache:
            write_ttl = ttl_resolver(data) if ttl_resolver is not None else ttl_seconds
            self._write_cache(cache_key, data, ttl_seconds=write_ttl)
        return data

    def _uncached_graphql_user(
        self,
        *,
        operation_name: str | None,
        query: str,
        variables: dict[str, Any] | None,
        token: str,
    ) -> dict[str, Any]:
        """One user-endpoint GraphQL call, straight to Warcraft Logs."""
        response = self._post_graphql(
            self._site.user_api_url,
            token=token,
            body={
                "operationName": operation_name,
                "query": query,
                "variables": variables,
            },
        )
        return self._consume_graphql_response(_response_json(response), operation_name=operation_name, endpoint="user")

    def current_user(self) -> dict[str, Any]:
        """The account behind the saved user token. Never cached: identity must be live, not remembered."""
        data = self._uncached_graphql_user(
            operation_name="CurrentUser",
            query=CURRENT_USER_QUERY,
            variables={},
            token=self._user_token(),
        )
        user_data = data.get("userData")
        if not isinstance(user_data, dict):
            raise WarcraftLogsClientError("not_found", "Warcraft Logs user data was missing from the current-user response.")
        current_user = user_data.get("currentUser")
        if not isinstance(current_user, dict):
            raise WarcraftLogsClientError("not_found", "Warcraft Logs did not return the current user for the saved token.")
        return current_user

    def _consume_graphql_response(
        self,
        payload: Any,
        *,
        operation_name: str | None,
        endpoint: str,
    ) -> dict[str, Any]:
        if not isinstance(payload, dict):
            label = "user " if endpoint == "user" else ""
            raise WarcraftLogsClientError(
                "invalid_response",
                f"Unexpected Warcraft Logs {label}response shape for {operation_name}.",
            )
        errors = payload.get("errors") if isinstance(payload.get("errors"), list) else []
        raw_data = payload.get("data")
        data: dict[str, Any] | None = raw_data if isinstance(raw_data, dict) else None
        has_useful_data = data is not None and bool(data) and _response_has_useful_value(data)
        if errors and not has_useful_data:
            message = _first_graphql_error_message(errors, operation_name)
            raise WarcraftLogsClientError(_graphql_error_code(message), message)
        if not data:
            label = "user " if endpoint == "user" else ""
            raise WarcraftLogsClientError(
                "invalid_response",
                f"Warcraft Logs returned no {label}data for {operation_name}.",
            )
        if errors:
            warnings = [_normalize_graphql_error(error) for error in errors]
            self._graphql_warnings.extend(warnings)
            return {**data, GRAPHQL_WARNINGS_KEY: warnings}
        return data

    def _consume_raw_graphql_response(
        self,
        payload: Any,
        *,
        operation_name: str | None,
        endpoint: str,
    ) -> dict[str, Any] | None:
        if not isinstance(payload, dict):
            label = "user " if endpoint == "user" else ""
            raise WarcraftLogsClientError(
                "invalid_response",
                f"Unexpected Warcraft Logs {label}response shape for {operation_name}.",
            )
        errors = payload.get("errors") if isinstance(payload.get("errors"), list) else []
        data = payload.get("data")
        if "data" not in payload:
            if errors:
                message = _first_graphql_error_message(errors, operation_name)
                raise WarcraftLogsClientError(_graphql_error_code(message), message)
            label = "user " if endpoint == "user" else ""
            raise WarcraftLogsClientError(
                "invalid_response",
                f"Warcraft Logs returned no {label}data for {operation_name}.",
            )
        if errors:
            if data is None:
                message = _first_graphql_error_message(errors, operation_name)
                raise WarcraftLogsClientError(_graphql_error_code(message), message)
            warnings = [_normalize_graphql_error(error) for error in errors]
            self._graphql_warnings.extend(warnings)
            return {**data, GRAPHQL_WARNINGS_KEY: warnings} if isinstance(data, dict) else None
        return data if isinstance(data, dict) else None

    def _raw_graphql_request(
        self,
        *,
        operation_name: str | None,
        query: str,
        variables: dict[str, Any] | None,
        endpoint: str,
        namespace: str,
        ttl_seconds: int,
        use_cache: bool,
    ) -> dict[str, Any] | None:
        use_cache = use_cache and endpoint != "user"
        cache_payload = {
            "endpoint": endpoint,
            "operation_name": operation_name,
            "query": query,
            "variables": variables or {},
        }
        cache_key = self._cache_key(namespace, cache_payload)
        if use_cache:
            cached = self._read_raw_cache(cache_key)
            if cached is not _CACHE_MISS:
                return cached if isinstance(cached, dict) else None
        url = self._site.user_api_url if endpoint == "user" else self._site.api_url
        token = self._user_token() if endpoint == "user" else self._token()
        response = self._post_graphql(
            url,
            token=token,
            body={
                "operationName": operation_name,
                "query": query,
                "variables": variables or {},
            },
        )
        data = self._consume_raw_graphql_response(_response_json(response), operation_name=operation_name, endpoint=endpoint)
        if use_cache:
            self._write_cache(cache_key, data, ttl_seconds=ttl_seconds)
        return data

    def _graphql(
        self,
        *,
        operation_name: str | None,
        query: str,
        variables: dict[str, Any] | None,
        namespace: str,
        ttl_seconds: int,
        use_cache: bool = True,
        force_client: bool = False,
        ttl_resolver: Callable[[dict[str, Any]], int] | None = None,
    ) -> dict[str, Any]:
        if not force_client and self._has_user_token():
            return self._graphql_user(
                operation_name=operation_name,
                query=query,
                variables=variables,
                namespace=namespace,
                ttl_seconds=ttl_seconds,
                use_cache=use_cache,
                ttl_resolver=ttl_resolver,
            )
        request_variables = _prune_null_variables(variables)
        cache_payload = {
            "endpoint": "client",
            "operation_name": operation_name,
            "query": query,
            "variables": request_variables,
        }
        cache_key = self._cache_key(namespace, cache_payload)
        if use_cache:
            cached = self._read_cache(cache_key)
            if isinstance(cached, dict):
                return cached
        response = self._post_graphql(
            self._site.api_url,
            token=self._token(),
            body={
                "operationName": operation_name,
                "query": query,
                "variables": request_variables,
            },
        )
        data = self._consume_graphql_response(_response_json(response), operation_name=operation_name, endpoint="client")
        if use_cache:
            write_ttl = ttl_resolver(data) if ttl_resolver is not None else ttl_seconds
            self._write_cache(cache_key, data, ttl_seconds=write_ttl)
        return data

    def raw_graphql(
        self,
        *,
        operation_name: str | None,
        query: str,
        variables: dict[str, Any] | None,
        endpoint: str = "auto",
        cache_ttl_seconds: int = 0,
    ) -> tuple[dict[str, Any] | None, str]:
        use_cache = cache_ttl_seconds > 0
        ttl_seconds = max(0, cache_ttl_seconds)
        if endpoint == "user":
            return (
                self._raw_graphql_request(
                    operation_name=operation_name,
                    query=query,
                    variables=variables,
                    endpoint="user",
                    namespace="raw_graphql",
                    ttl_seconds=ttl_seconds,
                    use_cache=False,
                ),
                "user",
            )
        if endpoint == "client":
            return (
                self._raw_graphql_request(
                    operation_name=operation_name,
                    query=query,
                    variables=variables,
                    endpoint="client",
                    namespace="raw_graphql",
                    ttl_seconds=ttl_seconds,
                    use_cache=use_cache,
                ),
                "client",
            )
        if endpoint != "auto":
            raise WarcraftLogsClientError("invalid_argument", "endpoint must be one of: auto, client, user.")
        effective_endpoint = "user" if self._has_user_token() else "client"
        return (
            self._raw_graphql_request(
                operation_name=operation_name,
                query=query,
                variables=variables,
                endpoint=effective_endpoint,
                namespace="raw_graphql",
                ttl_seconds=ttl_seconds,
                use_cache=use_cache and effective_endpoint != "user",
            ),
            effective_endpoint,
        )

    def _report_query_variables(self, *, code: str, allow_unlisted: bool, options: ReportFilterOptions) -> dict[str, Any]:
        return {
            "code": code.strip(),
            "allowUnlisted": allow_unlisted,
            "abilityID": options.ability_id,
            "dataType": options.data_type,
            "difficulty": options.difficulty,
            "encounterID": options.encounter_id,
            "endTime": options.end_time,
            "fightIDs": options.fight_ids,
            "filterExpression": options.filter_expression,
            "hostilityType": options.hostility_type,
            "killType": options.kill_type,
            "limit": options.limit,
            "sourceID": options.source_id,
            "startTime": options.start_time,
            "targetID": options.target_id,
            "translate": options.translate,
            "viewBy": options.view_by,
            "wipeCutoff": options.wipe_cutoff,
        }

    def _report_player_details_variables(
        self,
        *,
        code: str,
        allow_unlisted: bool,
        options: ReportPlayerDetailsOptions,
    ) -> dict[str, Any]:
        return {
            "code": code.strip(),
            "allowUnlisted": allow_unlisted,
            "difficulty": options.difficulty,
            "encounterID": options.encounter_id,
            "endTime": options.end_time,
            "fightIDs": options.fight_ids,
            "killType": options.kill_type,
            "startTime": options.start_time,
            "translate": options.translate,
            "includeCombatantInfo": options.include_combatant_info,
        }

    def _report_rankings_variables(
        self,
        *,
        code: str,
        allow_unlisted: bool,
        options: ReportRankingsOptions,
    ) -> dict[str, Any]:
        return {
            "code": code.strip(),
            "allowUnlisted": allow_unlisted,
            "compare": options.compare,
            "difficulty": options.difficulty,
            "encounterID": options.encounter_id,
            "fightIDs": options.fight_ids,
            "playerMetric": options.player_metric,
            "timeframe": options.timeframe,
        }

    def _report_finish_ttl_resolver(self, *, ttl_override: int | None = None) -> Callable[[dict[str, Any]], int]:
        """Resolve the cache TTL from a report response's finish state.

        Finished reports (``report_is_finished``) use the finished TTL (or an explicit
        ``ttl_override``); live/unknown reports use the short report TTL so a live
        report is never stored under the finished TTL. By design ``ttl_override``
        scopes the *finished* TTL only — it is the "how long to keep this finished
        report" knob for the sampling path; live reports always fall back to the
        short live TTL regardless of ``ttl_override`` (it is not a cache-bypass flag).

        Tradeoff (accepted): the cache *key* is finish-state-agnostic, so a report
        fetched while live (cached for the short report TTL) can still be served from
        that live entry for up to ``self._report_ttl`` seconds after it finishes. We
        deliberately cache live reports (per the AUR-388 spec decision) rather than
        no-cache them; the short live TTL bounds the staleness window and finished WoW
        logs are immutable thereafter. See docs/warcraftlogs/CACHING.md.
        """
        finished_ttl = ttl_override if ttl_override is not None else self._finished_report_ttl
        live_ttl = self._report_ttl

        def resolver(data: dict[str, Any]) -> int:
            report_data = data.get("reportData") if isinstance(data, dict) else None
            report = report_data.get("report") if isinstance(report_data, dict) else None
            if isinstance(report, dict) and report_is_finished(report):
                return finished_ttl
            return live_ttl

        return resolver

    def _report_lookup(
        self,
        *,
        operation_name: str,
        query: str,
        namespace: str,
        code: str,
        allow_unlisted: bool,
        variables: dict[str, Any],
        ttl_override: int | None = None,
        finish_state_ttl: bool = True,
    ) -> dict[str, Any]:
        # Most report-detail payloads (fights/events/tables/graphs/master-data/player-details)
        # are immutable once the report finishes, so they key on finish state and earn the long
        # finished TTL. Callers with population-relative or otherwise mutable output (rankings)
        # pass finish_state_ttl=False to stay on the short live TTL.
        ttl_resolver = self._report_finish_ttl_resolver(ttl_override=ttl_override) if finish_state_ttl else None
        data = self._graphql(
            operation_name=operation_name,
            query=query,
            variables=variables,
            namespace=namespace,
            ttl_seconds=self._report_ttl,
            ttl_resolver=ttl_resolver,
        )
        report_data = data.get("reportData")
        report = report_data.get("report") if isinstance(report_data, dict) else None
        if not isinstance(report, dict):
            raise WarcraftLogsClientError("not_found", f"Report {code!r} was not found.")
        return report

    def rate_limit(self) -> dict[str, Any]:
        data = self._graphql(
            operation_name="RateLimit",
            query=RATE_LIMIT_QUERY,
            variables=None,
            namespace="rate_limit",
            ttl_seconds=60,
            force_client=True,
        )
        payload = data.get("rateLimitData")
        if not isinstance(payload, dict):
            raise WarcraftLogsClientError("not_found", "Warcraft Logs rate limit data was not available.")
        return payload

    def probe_live_public_api(self) -> dict[str, Any]:
        data = self._graphql(
            operation_name="RateLimit",
            query=RATE_LIMIT_QUERY,
            variables=None,
            namespace="rate_limit",
            ttl_seconds=60,
            use_cache=False,
            force_client=True,
        )
        payload = data.get("rateLimitData")
        if not isinstance(payload, dict):
            raise WarcraftLogsClientError("not_found", "Warcraft Logs rate limit data was not available.")
        return payload

    def regions(self) -> list[dict[str, Any]]:
        data = self._graphql(
            operation_name="Regions",
            query=REGIONS_QUERY,
            variables=None,
            namespace="regions",
            ttl_seconds=self._static_ttl,
        )
        world_data = data.get("worldData")
        regions = world_data.get("regions") if isinstance(world_data, dict) else None
        if not isinstance(regions, list):
            raise WarcraftLogsClientError("not_found", "Warcraft Logs region data was not available.")
        return [region for region in regions if isinstance(region, dict)]

    def expansions(self) -> list[dict[str, Any]]:
        data = self._graphql(
            operation_name="Expansions",
            query=EXPANSIONS_QUERY,
            variables=None,
            namespace="expansions",
            ttl_seconds=self._static_ttl,
        )
        world_data = data.get("worldData")
        expansions = world_data.get("expansions") if isinstance(world_data, dict) else None
        if not isinstance(expansions, list):
            raise WarcraftLogsClientError("not_found", "Warcraft Logs expansion data was not available.")
        return [expansion for expansion in expansions if isinstance(expansion, dict)]

    def server(self, *, region: str, slug: str) -> dict[str, Any]:
        data = self._graphql(
            operation_name="Server",
            query=SERVER_QUERY,
            variables={"region": normalize_region(region), "slug": primary_realm_slug(slug)},
            namespace="server",
            ttl_seconds=self._static_ttl,
        )
        world_data = data.get("worldData")
        server = world_data.get("server") if isinstance(world_data, dict) else None
        if not isinstance(server, dict):
            raise WarcraftLogsClientError("not_found", f"Server {slug!r} was not found for region {region!r}.")
        return server

    def zones(self, *, expansion_id: int | None = None) -> list[dict[str, Any]]:
        data = self._graphql(
            operation_name="Zones",
            query=ZONES_QUERY,
            variables={"expansionId": expansion_id},
            namespace="zones",
            ttl_seconds=self._static_ttl,
        )
        world_data = data.get("worldData")
        zones = world_data.get("zones") if isinstance(world_data, dict) else None
        if not isinstance(zones, list):
            raise WarcraftLogsClientError("not_found", "Warcraft Logs zone data was not available.")
        return [zone for zone in zones if isinstance(zone, dict)]

    def zone(self, *, zone_id: int) -> dict[str, Any]:
        data = self._graphql(
            operation_name="Zone",
            query=ZONE_QUERY,
            variables={"id": zone_id},
            namespace="zone",
            ttl_seconds=self._static_ttl,
        )
        world_data = data.get("worldData")
        zone = world_data.get("zone") if isinstance(world_data, dict) else None
        if not isinstance(zone, dict):
            raise WarcraftLogsClientError("not_found", f"Zone {zone_id!r} was not found.")
        return zone

    def encounter(self, *, encounter_id: int) -> dict[str, Any]:
        data = self._graphql(
            operation_name="Encounter",
            query=ENCOUNTER_QUERY,
            variables={"id": encounter_id},
            namespace="encounter",
            ttl_seconds=self._static_ttl,
        )
        world_data = data.get("worldData")
        encounter = world_data.get("encounter") if isinstance(world_data, dict) else None
        if not isinstance(encounter, dict):
            raise WarcraftLogsClientError("not_found", f"Encounter {encounter_id!r} was not found.")
        return encounter

    def encounter_rankings(self, *, encounter_id: int, options: EncounterRankingsOptions) -> dict[str, Any]:
        try:
            request_options = EncounterRankingsOptions(
                bracket=options.bracket,
                difficulty=options.difficulty,
                filter=options.filter,
                page=options.page,
                partition=options.partition,
                server_region=normalize_region(options.server_region) if options.server_region else None,
                server_slug=primary_realm_slug(options.server_slug) if options.server_slug else None,
                size=options.size,
                leaderboard=options.leaderboard,
                hard_mode_level=options.hard_mode_level,
                metric=options.metric,
                include_combatant_info=options.include_combatant_info,
                include_other_players=options.include_other_players,
                class_name=options.class_name,
                spec_name=options.spec_name,
            )
            query, variables = _encounter_rankings_request(encounter_id=encounter_id, options=request_options)
        except ValueError as exc:
            raise WarcraftLogsClientError("invalid_argument", str(exc)) from exc
        data = self._graphql(
            operation_name="EncounterRankings",
            query=query,
            variables=variables,
            namespace="encounter_rankings",
            ttl_seconds=self._guild_ttl,
        )
        world_data = data.get("worldData")
        encounter = world_data.get("encounter") if isinstance(world_data, dict) else None
        if not isinstance(encounter, dict):
            raise WarcraftLogsClientError("not_found", f"Encounter {encounter_id!r} was not found.")
        return encounter

    def guild(self, *, region: str, realm: str, name: str, zone_id: int | None = None) -> dict[str, Any]:
        data = self._graphql(
            operation_name="Guild",
            query=GUILD_QUERY,
            variables={
                "name": normalize_name(name),
                "serverSlug": primary_realm_slug(realm),
                "serverRegion": normalize_region(region),
                "zoneId": zone_id,
            },
            namespace="guild",
            ttl_seconds=self._guild_ttl,
        )
        guild_data = data.get("guildData")
        guild = guild_data.get("guild") if isinstance(guild_data, dict) else None
        if not isinstance(guild, dict):
            raise WarcraftLogsClientError("not_found", f"Guild {name!r} was not found on {region}/{realm}.")
        return guild

    def guild_rankings(
        self,
        *,
        region: str,
        realm: str,
        name: str,
        zone_id: int | None = None,
        size: int | None = None,
        difficulty: int | None = None,
    ) -> dict[str, Any]:
        data = self._graphql(
            operation_name="GuildRankings",
            query=GUILD_RANKINGS_QUERY,
            variables={
                "name": normalize_name(name),
                "serverSlug": primary_realm_slug(realm),
                "serverRegion": normalize_region(region),
                "zoneId": zone_id,
                "size": size,
                "difficulty": difficulty,
            },
            namespace="guild_rankings",
            ttl_seconds=self._guild_ttl,
        )
        guild_data = data.get("guildData")
        guild = guild_data.get("guild") if isinstance(guild_data, dict) else None
        if not isinstance(guild, dict):
            raise WarcraftLogsClientError("not_found", f"Guild {name!r} was not found on {region}/{realm}.")
        return guild

    def guild_members(
        self,
        *,
        region: str,
        realm: str,
        name: str,
        limit: int = 100,
        page: int = 1,
    ) -> dict[str, Any]:
        data = self._graphql(
            operation_name="GuildMembers",
            query=GUILD_MEMBERS_QUERY,
            variables={
                "name": normalize_name(name),
                "serverSlug": primary_realm_slug(realm),
                "serverRegion": normalize_region(region),
                "limit": limit,
                "page": page,
            },
            namespace="guild_members",
            ttl_seconds=self._guild_ttl,
        )
        guild_data = data.get("guildData")
        guild = guild_data.get("guild") if isinstance(guild_data, dict) else None
        if not isinstance(guild, dict):
            raise WarcraftLogsClientError("not_found", f"Guild {name!r} was not found on {region}/{realm}.")
        return guild

    def guild_attendance(
        self,
        *,
        region: str,
        realm: str,
        name: str,
        guild_tag_id: int | None = None,
        limit: int = 16,
        page: int = 1,
        zone_id: int | None = None,
    ) -> dict[str, Any]:
        data = self._graphql(
            operation_name="GuildAttendance",
            query=GUILD_ATTENDANCE_QUERY,
            variables={
                "name": normalize_name(name),
                "serverSlug": primary_realm_slug(realm),
                "serverRegion": normalize_region(region),
                "guildTagID": guild_tag_id,
                "limit": limit,
                "page": page,
                "zoneID": zone_id,
            },
            namespace="guild_attendance",
            ttl_seconds=self._guild_ttl,
        )
        guild_data = data.get("guildData")
        guild = guild_data.get("guild") if isinstance(guild_data, dict) else None
        if not isinstance(guild, dict):
            raise WarcraftLogsClientError("not_found", f"Guild {name!r} was not found on {region}/{realm}.")
        return guild

    def character(self, *, region: str, realm: str, name: str) -> dict[str, Any]:
        data = self._graphql(
            operation_name="Character",
            query=CHARACTER_QUERY,
            variables={
                "name": normalize_name(name),
                "serverSlug": primary_realm_slug(realm),
                "serverRegion": normalize_region(region),
            },
            namespace="character",
            ttl_seconds=self._guild_ttl,
        )
        character_data = data.get("characterData")
        character = character_data.get("character") if isinstance(character_data, dict) else None
        if not isinstance(character, dict):
            raise WarcraftLogsClientError("not_found", f"Character {name!r} was not found on {region}/{realm}.")
        return character

    def character_rankings(
        self,
        *,
        region: str,
        realm: str,
        name: str,
        zone_id: int | None = None,
        difficulty: int | None = None,
        metric: str | None = None,
        size: int | None = None,
        spec_name: str | None = None,
    ) -> dict[str, Any]:
        data = self._graphql(
            operation_name="CharacterRankings",
            query=CHARACTER_RANKINGS_QUERY,
            variables={
                "name": normalize_name(name),
                "serverSlug": primary_realm_slug(realm),
                "serverRegion": normalize_region(region),
                "zoneID": zone_id,
                "difficulty": difficulty,
                "metric": metric,
                "size": size,
                "specName": spec_name,
            },
            namespace="character_rankings",
            ttl_seconds=self._guild_ttl,
        )
        character_data = data.get("characterData")
        character = character_data.get("character") if isinstance(character_data, dict) else None
        if not isinstance(character, dict):
            raise WarcraftLogsClientError("not_found", f"Character {name!r} was not found on {region}/{realm}.")
        return character

    def report(self, *, code: str, allow_unlisted: bool = False) -> dict[str, Any]:
        data = self._graphql(
            operation_name="Report",
            query=REPORT_QUERY,
            variables={"code": code.strip(), "allowUnlisted": allow_unlisted},
            namespace="report",
            ttl_seconds=self._report_ttl,
            ttl_resolver=self._report_finish_ttl_resolver(),
        )
        report_data = data.get("reportData")
        report = report_data.get("report") if isinstance(report_data, dict) else None
        if not isinstance(report, dict):
            raise WarcraftLogsClientError("not_found", f"Report {code!r} was not found.")
        return report

    def reports(
        self,
        *,
        guild_region: str | None = None,
        guild_realm: str | None = None,
        guild_name: str | None = None,
        limit: int = 25,
        page: int = 1,
        start_time: float | None = None,
        end_time: float | None = None,
        zone_id: int | None = None,
        game_zone_id: int | None = None,
    ) -> dict[str, Any]:
        data = self._graphql(
            operation_name="Reports",
            query=REPORTS_QUERY,
            variables={
                "guildName": normalize_name(guild_name) if guild_name else None,
                "guildServerSlug": primary_realm_slug(guild_realm) if guild_realm else None,
                "guildServerRegion": normalize_region(guild_region) if guild_region else None,
                "limit": limit,
                "page": page,
                "startTime": start_time,
                "endTime": end_time,
                "zoneID": zone_id,
                "gameZoneID": game_zone_id,
            },
            namespace="reports",
            ttl_seconds=self._report_ttl,
        )
        report_data = data.get("reportData")
        reports = report_data.get("reports") if isinstance(report_data, dict) else None
        if not isinstance(reports, dict):
            raise WarcraftLogsClientError("not_found", "Warcraft Logs report listing data was not available.")
        return reports

    def report_fights(
        self,
        *,
        code: str,
        difficulty: int | None = None,
        allow_unlisted: bool = False,
        ttl_override: int | None = None,
    ) -> dict[str, Any]:
        return self._report_lookup(
            operation_name="ReportFights",
            query=REPORT_FIGHTS_QUERY,
            namespace="report_fights",
            code=code,
            allow_unlisted=allow_unlisted,
            variables={"code": code.strip(), "difficulty": difficulty, "allowUnlisted": allow_unlisted},
            ttl_override=ttl_override,
        )

    def report_events(self, *, code: str, allow_unlisted: bool = False, options: ReportFilterOptions) -> dict[str, Any]:
        return self._report_lookup(
            operation_name="ReportEvents",
            query=REPORT_EVENTS_QUERY,
            namespace="report_events",
            code=code,
            allow_unlisted=allow_unlisted,
            variables=self._report_query_variables(code=code, allow_unlisted=allow_unlisted, options=options),
        )

    def report_table(self, *, code: str, allow_unlisted: bool = False, options: ReportFilterOptions) -> dict[str, Any]:
        return self._report_lookup(
            operation_name="ReportTable",
            query=REPORT_TABLE_QUERY,
            namespace="report_table",
            code=code,
            allow_unlisted=allow_unlisted,
            variables=self._report_query_variables(code=code, allow_unlisted=allow_unlisted, options=options),
        )

    def report_graph(self, *, code: str, allow_unlisted: bool = False, options: ReportFilterOptions) -> dict[str, Any]:
        return self._report_lookup(
            operation_name="ReportGraph",
            query=REPORT_GRAPH_QUERY,
            namespace="report_graph",
            code=code,
            allow_unlisted=allow_unlisted,
            variables=self._report_query_variables(code=code, allow_unlisted=allow_unlisted, options=options),
        )

    def report_master_data(
        self,
        *,
        code: str,
        allow_unlisted: bool = False,
        translate: bool | None = None,
        actor_type: str | None = None,
        actor_sub_type: str | None = None,
    ) -> dict[str, Any]:
        return self._report_lookup(
            operation_name="ReportMasterData",
            query=REPORT_MASTER_DATA_QUERY,
            namespace="report_master_data",
            code=code,
            allow_unlisted=allow_unlisted,
            variables={
                "code": code.strip(),
                "allowUnlisted": allow_unlisted,
                "translate": translate,
                "actorType": actor_type,
                "actorSubType": actor_sub_type,
            },
        )

    def report_player_details(
        self,
        *,
        code: str,
        allow_unlisted: bool = False,
        options: ReportPlayerDetailsOptions,
        ttl_override: int | None = None,
    ) -> dict[str, Any]:
        return self._report_lookup(
            operation_name="ReportPlayerDetails",
            query=REPORT_PLAYER_DETAILS_QUERY,
            namespace="report_player_details",
            code=code,
            allow_unlisted=allow_unlisted,
            variables=self._report_player_details_variables(code=code, allow_unlisted=allow_unlisted, options=options),
            ttl_override=ttl_override,
        )

    def report_rankings(
        self,
        *,
        code: str,
        allow_unlisted: bool = False,
        options: ReportRankingsOptions,
    ) -> dict[str, Any]:
        return self._report_lookup(
            operation_name="ReportRankings",
            query=REPORT_RANKINGS_QUERY,
            namespace="report_rankings",
            code=code,
            allow_unlisted=allow_unlisted,
            variables=self._report_rankings_variables(code=code, allow_unlisted=allow_unlisted, options=options),
            # Rankings are population-relative percentiles that Warcraft Logs keeps recomputing
            # after a log finishes, so they must NOT inherit the long finished-report TTL.
            finish_state_ttl=False,
        )

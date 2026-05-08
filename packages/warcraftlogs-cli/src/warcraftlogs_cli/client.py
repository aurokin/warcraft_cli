from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx
from warcraft_api.cache import CacheSettings, CacheTTLConfig, build_cache_store, load_prefixed_cache_settings_from_env
from warcraft_api.http import DEFAULT_RETRY_ATTEMPTS, request_with_retries
from warcraft_content.paths import provider_cache_root
from warcraft_core.auth import load_provider_auth_state, save_provider_auth_state
from warcraft_core.env import find_env_file, load_env_file, load_explicit_env_file
from warcraft_core.paths import provider_env_path
from warcraft_core.wow_normalization import normalize_name, normalize_region, primary_realm_slug

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
query GuildAttendance($name: String!, $serverSlug: String!, $serverRegion: String!, $guildTagID: Int, $limit: Int, $page: Int, $zoneID: Int) {
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
    oauth_token_url="https://www.warcraftlogs.com/oauth/token",
    api_url="https://www.warcraftlogs.com/api/v2/client",
    user_api_url="https://www.warcraftlogs.com/api/v2/user",
)


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


class WarcraftLogsClientError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


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
    previous_client_id = os.environ.get("WARCRAFTLOGS_CLIENT_ID")
    previous_client_secret = os.environ.get("WARCRAFTLOGS_CLIENT_SECRET")
    env_path = load_env_file(start_dir=start_dir, override=True)
    try:
        if not (os.getenv("WARCRAFTLOGS_CLIENT_ID") and os.getenv("WARCRAFTLOGS_CLIENT_SECRET")):
            provider_env_path = warcraftlogs_provider_env_path()
            if load_explicit_env_file(provider_env_path, override=True) is not None:
                env_path = provider_env_path
        client_id = os.getenv("WARCRAFTLOGS_CLIENT_ID")
        client_secret = os.getenv("WARCRAFTLOGS_CLIENT_SECRET")
    finally:
        if previous_client_id is None:
            os.environ.pop("WARCRAFTLOGS_CLIENT_ID", None)
        else:
            os.environ["WARCRAFTLOGS_CLIENT_ID"] = previous_client_id
        if previous_client_secret is None:
            os.environ.pop("WARCRAFTLOGS_CLIENT_SECRET", None)
        else:
            os.environ["WARCRAFTLOGS_CLIENT_SECRET"] = previous_client_secret
    return WarcraftLogsAuthConfig(
        client_id=client_id.strip() if client_id else None,
        client_secret=client_secret.strip() if client_secret else None,
        env_file=str(env_path) if env_path is not None else None,
    )


def load_warcraftlogs_cache_settings_from_env() -> tuple[CacheSettings, int, int, int, int]:
    settings = load_prefixed_cache_settings_from_env(
        env_prefix="WARCRAFTLOGS",
        default_cache_dir=DEFAULT_CACHE_DIR,
        default_redis_prefix="warcraftlogs_cli",
        ttl_defaults=CacheTTLConfig(
            search_suggestions=900,
            entity_page_html=300,
            guide_page_html=21600,
            page_html=60,
        ),
        ttl_env_overrides={
            "search_suggestions": "WARCRAFTLOGS_METADATA_CACHE_TTL_SECONDS",
            "entity_page_html": "WARCRAFTLOGS_GUILD_CACHE_TTL_SECONDS",
            "guide_page_html": "WARCRAFTLOGS_STATIC_CACHE_TTL_SECONDS",
            "page_html": "WARCRAFTLOGS_REPORT_CACHE_TTL_SECONDS",
        },
    )
    return (
        settings,
        settings.ttls.search_suggestions,
        settings.ttls.entity_page_html,
        settings.ttls.guide_page_html,
        settings.ttls.page_html,
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
        settings, metadata_ttl, guild_ttl, static_ttl, report_ttl = load_warcraftlogs_cache_settings_from_env()
        self._cache_settings = settings
        self._cache_store = build_cache_store(settings) if settings.enabled else None
        self._metadata_ttl = metadata_ttl
        self._guild_ttl = guild_ttl
        self._static_ttl = static_ttl
        self._report_ttl = report_ttl
        self._last_warnings: list[dict[str, Any]] = []

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
            self._http_client = httpx.Client(timeout=self._timeout_seconds, follow_redirects=True)
        return self._http_client

    def _cache_key(self, namespace: str, payload: dict[str, Any]) -> str:
        raw = json.dumps({"site": self._site.key, "namespace": namespace, "payload": payload}, sort_keys=True, separators=(",", ":"))
        return f"{namespace}:{hashlib.sha256(raw.encode('utf-8')).hexdigest()}"

    def _read_cache(self, key: str) -> Any | None:
        if self._cache_store is None:
            return None
        cached = self._cache_store.get(key)
        if isinstance(cached, dict):
            warnings = cached.get(GRAPHQL_WARNINGS_KEY)
            self._last_warnings = list(warnings) if isinstance(warnings, list) else []
        return cached

    def _write_cache(self, key: str, payload: Any, *, ttl_seconds: int) -> None:
        if self._cache_store is None:
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
        response = request_with_retries(
            self._client(),
            self._site.oauth_token_url,
            method="POST",
            data={"grant_type": "client_credentials"},
            auth=(self._client_id, self._client_secret),
            retry_attempts=self._retry_attempts,
        )
        payload = response.json()
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
        return not (isinstance(expires_at, (int, float)) and time.time() >= float(expires_at))

    @property
    def site(self) -> WarcraftLogsSiteProfile:
        return self._site

    @property
    def last_warnings(self) -> list[dict[str, Any]]:
        return list(self._last_warnings)

    @property
    def client_id(self) -> str:
        return self._client_id

    def authorization_code_url(self, *, redirect_uri: str, state: str) -> str:
        self._require_client_credentials()
        return f"{self._site.oauth_authorize_url}?{urlencode({'client_id': self._client_id, 'state': state, 'redirect_uri': redirect_uri, 'response_type': 'code'})}"

    def pkce_code_url(self, *, redirect_uri: str, state: str, code_challenge: str) -> str:
        self._require_client_credentials()
        return f"{self._site.oauth_authorize_url}?{urlencode({'client_id': self._client_id, 'code_challenge': code_challenge, 'code_challenge_method': 'S256', 'state': state, 'redirect_uri': redirect_uri, 'response_type': 'code'})}"

    def exchange_authorization_code(self, *, code: str, redirect_uri: str) -> dict[str, Any]:
        self._require_client_credentials()
        response = request_with_retries(
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
        payload = response.json()
        if not isinstance(payload, dict):
            raise WarcraftLogsClientError("auth_failed", "Warcraft Logs returned an invalid authorization-code token response.")
        return payload

    def exchange_pkce_code(self, *, code: str, redirect_uri: str, code_verifier: str) -> dict[str, Any]:
        self._require_client_credentials()
        response = request_with_retries(
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
        payload = response.json()
        if not isinstance(payload, dict):
            raise WarcraftLogsClientError("auth_failed", "Warcraft Logs returned an invalid PKCE token response.")
        return payload

    def _graphql_user(
        self,
        *,
        operation_name: str,
        query: str,
        variables: dict[str, Any] | None,
        namespace: str,
        ttl_seconds: int,
        use_cache: bool = True,
    ) -> dict[str, Any]:
        request_variables = _prune_null_variables(variables)
        cache_payload = {
            "endpoint": "user",
            "operation_name": operation_name,
            "query": query,
            "variables": request_variables,
        }
        cache_key = self._cache_key(namespace, cache_payload)
        if use_cache:
            cached = self._read_cache(cache_key)
            if isinstance(cached, dict):
                return cached
        response = request_with_retries(
            self._client(),
            self._site.user_api_url,
            method="POST",
            retry_attempts=self._retry_attempts,
            headers={
                "Authorization": f"Bearer {self._user_token()}",
                "Content-Type": "application/json",
            },
            json={
                "operationName": operation_name,
                "query": query,
                "variables": request_variables,
            },
        )
        data = self._consume_graphql_response(response.json(), operation_name=operation_name, endpoint="user")
        if use_cache:
            self._write_cache(cache_key, data, ttl_seconds=ttl_seconds)
        return data

    def current_user(self) -> dict[str, Any]:
        data = self._graphql_user(
            operation_name="CurrentUser",
            query=CURRENT_USER_QUERY,
            variables=None,
            namespace="user_current",
            ttl_seconds=self._metadata_ttl,
        )
        user_data = data.get("userData")
        if not isinstance(user_data, dict):
            raise WarcraftLogsClientError("not_found", "Warcraft Logs user data was missing from the current-user response.")
        current_user = user_data.get("currentUser")
        if not isinstance(current_user, dict):
            raise WarcraftLogsClientError("not_found", "Warcraft Logs did not return the current user for the saved token.")
        return current_user

    def probe_live_user_api(self) -> dict[str, Any]:
        data = self._graphql_user(
            operation_name="CurrentUser",
            query=CURRENT_USER_QUERY,
            variables=None,
            namespace="user_current",
            ttl_seconds=self._metadata_ttl,
            use_cache=False,
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
        operation_name: str,
        endpoint: str,
    ) -> dict[str, Any]:
        if not isinstance(payload, dict):
            label = "user " if endpoint == "user" else ""
            raise WarcraftLogsClientError(
                "invalid_response",
                f"Unexpected Warcraft Logs {label}response shape for {operation_name}.",
            )
        errors = payload.get("errors")
        data = payload.get("data")
        has_data_dict = isinstance(data, dict) and bool(data)
        has_useful_data = has_data_dict and any(value is not None for value in data.values())
        has_errors = isinstance(errors, list) and bool(errors)
        if has_errors and not has_useful_data:
            first = errors[0]
            message = first.get("message") if isinstance(first, dict) else None
            raise WarcraftLogsClientError(
                "graphql_error",
                message or f"Warcraft Logs returned GraphQL errors for {operation_name}.",
            )
        if not has_data_dict:
            label = "user " if endpoint == "user" else ""
            raise WarcraftLogsClientError(
                "invalid_response",
                f"Warcraft Logs returned no {label}data for {operation_name}.",
            )
        if has_errors:
            warnings = [_normalize_graphql_error(error) for error in errors]
            self._last_warnings = warnings
            return {**data, GRAPHQL_WARNINGS_KEY: warnings}
        self._last_warnings = []
        return data

    def _graphql(
        self,
        *,
        operation_name: str,
        query: str,
        variables: dict[str, Any] | None,
        namespace: str,
        ttl_seconds: int,
        use_cache: bool = True,
        force_client: bool = False,
    ) -> dict[str, Any]:
        if not force_client and self._has_user_token():
            return self._graphql_user(
                operation_name=operation_name,
                query=query,
                variables=variables,
                namespace=namespace,
                ttl_seconds=ttl_seconds,
                use_cache=use_cache,
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
        response = request_with_retries(
            self._client(),
            self._site.api_url,
            method="POST",
            retry_attempts=self._retry_attempts,
            headers={
                "Authorization": f"Bearer {self._token()}",
                "Content-Type": "application/json",
            },
            json={
                "operationName": operation_name,
                "query": query,
                "variables": request_variables,
            },
        )
        data = self._consume_graphql_response(response.json(), operation_name=operation_name, endpoint="client")
        if use_cache:
            self._write_cache(cache_key, data, ttl_seconds=ttl_seconds)
        return data

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
    ) -> dict[str, Any]:
        data = self._graphql(
            operation_name=operation_name,
            query=query,
            variables=variables,
            namespace=namespace,
            ttl_seconds=ttl_override if ttl_override is not None else self._report_ttl,
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
        self, *, code: str, difficulty: int | None = None, allow_unlisted: bool = False, ttl_override: int | None = None,
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
        )
